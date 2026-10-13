"""The agent's connection to the operator (ferry-control): one WebSocket that the agent dials out.

The agent always dials, so nothing needs to reach into a browser pod. It authenticates with its pod's
projected ServiceAccount token (re-read on every connect, since the kubelet rotates it), sends a hello
carrying a state snapshot and everything not yet acknowledged, and then exchanges protocol messages.
Events, alerts and the final bye are kept until the operator acks them, and ride along in the next
hello after a reconnect. State is not buffered: only the latest matters.
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
    Bye,
    ControlMessage,
    Event,
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

Buffered = Event | Alert | Bye


@dataclass(frozen=True, slots=True)
class Identity:
    """Who this pod is, as its own environment says (all pinned by the admission policy)."""

    trip_id: str
    pod_name: str
    gen: int
    image_digest: str
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
    ) -> None:
        self._http = http
        self._url = url
        self._token_path = token_path
        self._identity = identity
        self._handle = handle
        self._rev = rev
        self._backoff = tuple(backoff)
        self.boot = secrets.token_hex(8)
        self._seq = itertools.count(1)
        self._outbox: dict[int, Buffered] = {}  # unacknowledged, oldest first
        self._unsent: list[int] = []
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

    def publish(self, state: State) -> None:
        """The trip's latest state. Only the newest is sent."""
        self._state = state
        self._state_dirty = True
        self._wake.set()

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
                image_digest=identity.image_digest,
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
        elif isinstance(message, HelloAck):
            self.connected.set()
        else:
            await self._handle(message)

    async def _write(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._unsent:
                message = self._outbox.get(self._unsent.pop(0))
                if message is not None:  # not dropped or acked meanwhile
                    await ws.send_bytes(encode(message))
            if self._state_dirty and self._state is not None:
                self._state_dirty = False
                with contextlib.suppress(ConnectionResetError):
                    await ws.send_bytes(encode(self._state))
