"""The agent's connection to the operator (ferry-control): one WebSocket that the agent dials out.

The agent always dials, so nothing needs to reach into a browser pod. It authenticates with its pod's
projected ServiceAccount token (re-read on every connect, since the kubelet rotates it), sends a hello
carrying a state snapshot and everything not yet acknowledged, and then exchanges protocol messages.
Events, alerts and the final bye are kept until the operator acks them, and ride along in the next
hello after a reconnect. State is not buffered: only the latest matters. A cart claim is a question
that needs an answer now, so it isn't buffered either: without a connection the answer is no.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from ferry_watch.protocol import (
    MAX_FRAME_BYTES,
    MAX_UNACKED,
    VERSION,
    Ack,
    Alert,
    AlertKind,
    AlertParams,
    Bye,
    CartClaim,
    CartClaimResult,
    ControlMessage,
    Event,
    EventCode,
    EventParams,
    FarePreference,
    Hello,
    HelloAck,
    ProtocolError,
    State,
    decode_control_message,
    encode,
)

log = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
HEARTBEAT_SECONDS = 20.0
CLAIM_TIMEOUT_SECONDS = 10.0

Buffered = Event | Alert | Bye


@dataclass(frozen=True, slots=True)
class Identity:
    """Who this pod is, as its own environment says (all pinned by the admission policy)."""

    trip_id: str
    pod_name: str
    gen: int
    agent_version: str


class HubClient:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        url: str,
        token_path: Path,
        identity: Identity,
        handle: Callable[[ControlMessage], Awaitable[None]],
        *,
        rev: Callable[[], int] = lambda: 0,
        backoff: Sequence[float] = RECONNECT_BACKOFF_SECONDS,
        claim_timeout: float = CLAIM_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http
        self._url = url
        self._token_path = token_path
        self._identity = identity
        self._handle = handle
        self._rev = rev
        self._backoff = tuple(backoff)
        self._claim_timeout = claim_timeout
        self.boot = secrets.token_hex(8)
        self._seq = itertools.count(1)
        self._outbox: dict[int, Buffered] = {}  # unacknowledged, oldest first
        self._unsent: list[int] = []
        self._claims: dict[int, asyncio.Future[bool]] = {}  # waiting for an answer, by the claim's seq
        self._unsent_claims: list[CartClaim] = []
        self._state: State | None = None
        self._state_dirty = False
        self._wake = asyncio.Event()
        self._started = time.monotonic()
        self._last_contact: float | None = None
        self.connected = asyncio.Event()

    # ---- what the agent calls

    def envelope(self) -> dict[str, Any]:
        """The fields every message starts with; each call takes the next sequence number."""
        return {"seq": next(self._seq), "trip": self._identity.trip_id, "boot": self.boot, "rev": max(self._rev(), 0)}

    def send(self, message: Buffered) -> None:
        """Queue an event, alert or bye; it's kept until the operator acknowledges it."""
        self._outbox[message.seq] = message
        while len(self._outbox) > MAX_UNACKED:
            oldest = next(iter(self._outbox))
            log.warning("Dropping unacknowledged %s %d: too many waiting", type(self._outbox[oldest]).__name__, oldest)
            del self._outbox[oldest]
        self._unsent.append(message.seq)
        self._wake.set()

    def event(self, code: EventCode, params: EventParams | None = None) -> None:
        self.send(Event(**self.envelope(), code=code, params=params or EventParams()))

    def alert(self, kind: AlertKind, params: AlertParams | None = None) -> int:
        """Send an alert; returns its sequence number, to learn when the operator has it."""
        message = Alert(**self.envelope(), kind=kind, params=params or AlertParams())
        self.send(message)
        return message.seq

    def publish(self, state: State) -> None:
        """The trip's latest state. Only the newest is sent."""
        self._state = state
        self._state_dirty = True
        self._wake.set()

    def acked(self, seq: int) -> bool:
        """Whether the operator has acknowledged this message (or it was dropped as too old)."""
        return seq not in self._outbox

    async def claim(self, depart: str, fare: FarePreference) -> bool:
        """Ask the operator whether this pod may cart now. No connection or no answer means no."""
        if not self.connected.is_set():
            return False
        request = CartClaim(**self.envelope(), depart=depart, fare=fare)
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._claims[request.seq] = answer
        self._unsent_claims.append(request)
        self._wake.set()
        try:
            async with asyncio.timeout(self._claim_timeout):
                return await answer
        except TimeoutError:
            log.warning("No answer to cart claim %d in %.0fs", request.seq, self._claim_timeout)
            return False
        finally:
            self._claims.pop(request.seq, None)

    @property
    def unacked(self) -> list[Buffered]:
        return list(self._outbox.values())

    def silent_for(self) -> float:
        """Seconds since the operator was last heard from (or since starting, if never)."""
        return time.monotonic() - (self._last_contact if self._last_contact is not None else self._started)

    # ---- connection

    async def run(self) -> None:
        """Stay connected until cancelled, reconnecting with back-off."""
        failures = 0
        while True:
            try:
                await self._connection()
                failures = 0
            except (aiohttp.ClientError, OSError, ProtocolError, TimeoutError) as e:
                log.info("Operator connection lost: %s", e)
            finally:
                self.connected.clear()
                self._unsent_claims.clear()
                for answer in self._claims.values():
                    if not answer.done():
                        answer.set_result(False)  # the question went down with the connection
            await asyncio.sleep(self._backoff[min(failures, len(self._backoff) - 1)])
            failures += 1

    async def _connection(self) -> None:
        token = self._token_path.read_text().strip()
        async with self._http.ws_connect(
            self._url,
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=HEARTBEAT_SECONDS,
            max_msg_size=MAX_FRAME_BYTES,
        ) as ws:
            identity = self._identity
            hello = Hello(
                **self.envelope(),
                proto=[VERSION],
                agent_version=identity.agent_version,
                pod_name=identity.pod_name,
                gen=identity.gen,
                applied_rev=max(self._rev(), 0),
                snapshot=self._state or State(**self.envelope(), phase="starting"),
                unacked=self.unacked,
            )
            await ws.send_bytes(encode(hello))
            self._unsent.clear()  # everything unacknowledged rode along in the hello
            self._state_dirty = False
            writer = asyncio.create_task(self._write(ws), name="hub writer")
            try:
                async for frame in ws:
                    if frame.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        break
                    await self._receive(decode_control_message(frame.data))
            finally:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)

    async def _receive(self, message: ControlMessage) -> None:
        self._last_contact = time.monotonic()
        if isinstance(message, Ack):
            self._outbox.pop(message.acked, None)
        elif isinstance(message, CartClaimResult):
            answer = self._claims.get(message.claim)
            if answer is not None and not answer.done():
                answer.set_result(message.granted)
            log.info(
                "Cart claim %d: %s (%s)", message.claim, "granted" if message.granted else "refused", message.reason
            )
        elif isinstance(message, HelloAck):
            self.connected.set()
        else:
            await self._handle(message)

    async def _write(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._unsent_claims:
                await ws.send_bytes(encode(self._unsent_claims.pop(0)))
            while self._unsent:
                message = self._outbox.get(self._unsent.pop(0))
                if message is not None:  # not dropped or acked meanwhile
                    await ws.send_bytes(encode(message))
            if self._state_dirty and self._state is not None:
                self._state_dirty = False
                with contextlib.suppress(ConnectionResetError):
                    await ws.send_bytes(encode(self._state))
