"""waitlist-control's hub endpoint: where browser pods' agents dial in (docs/multi-tenant/design-reference.md §6.5).

A socket is bound to a pod by the API server, not by anything the agent says: its projected token goes
through TokenReview, which names the pod's uid and name, and those must match a live `browser_pods` row.
After that, every message must be about that row's trip. The endpoint records what the agent reports,
answers cart claims (one hold per user), sends the trip's configuration when it changes, and forwards
commands. It never sends anything that could steer the browser: the protocol has no such message.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import msgspec
from aiohttp import WSMsgType, web

from waitlist.models import PACIFIC
from waitlist.protocol import (
    MAX_FRAME_BYTES,
    VERSION,
    Ack,
    Alert,
    Bye,
    CartClaim,
    CartClaimResult,
    ClaimReason,
    Command,
    CommandOp,
    CommandReason,
    Configure,
    ControlMessage,
    Event,
    Hello,
    HelloAck,
    ProtocolError,
    State,
    decode_agent_message,
    encode,
)
from waitlist.versions import STREAM_VIEWER, at_least

from .configure import HubSettings, build_configure, cart_gate, worth_pushing
from .kube import Cluster
from .pods import HUB_TOKEN_AUDIENCE
from .store import Binding, ControlStore, HeldBack

log = logging.getLogger(__name__)

PATH = "/agent/v1"
HELLO_TIMEOUT_SECONDS = 10.0
HEARTBEAT_SECONDS = 20.0
TICK_SECONDS = 5.0  # commands and configuration changes are picked up at least this often
CLOSE_NOT_YET_RECORDED = 4401  # the pod's uid isn't in browser_pods yet: the agent retries
CLOSE_REPLACED = 4000
CLOSE_VIOLATION = 4403


@dataclass(slots=True)
class Peer:
    binding: Binding
    ws: web.WebSocketResponse
    boot: str
    agent_version: str = ""  # as its hello said: what it can be sent
    rev: int = 0
    last_heard: float = field(default_factory=time.time)
    sent_configure: Configure | None = None
    vnc_seconds: int = 0  # as last reported on this socket, to skip the store when nothing is new
    seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def envelope(self) -> dict[str, Any]:
        return {"seq": next(self.seq), "trip": self.binding.trip_id, "boot": self.boot, "rev": self.rev}

    @property
    def streams(self) -> bool:
        """Whether the agent has the streamed checkout viewer, and so knows configure's `cast_allowed`."""
        return at_least(self.agent_version, STREAM_VIEWER)


class Violation(Exception):
    """An agent said something about a trip or pod it isn't bound to."""


class HubEndpoint:
    def __init__(
        self,
        store: ControlStore,
        cluster: Cluster,
        settings: HubSettings,
        *,
        stretch: Callable[[], float] = lambda: 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._cluster = cluster
        self._settings = settings
        self._stretch = stretch
        self._clock = clock
        self._peers: dict[str, Peer] = {}
        self.started_at = clock()

    # ---- what the fleet loop uses

    def connected(self, pod: str) -> bool:
        return pod in self._peers

    def last_heard(self, pod: str) -> float | None:
        peer = self._peers.get(pod)
        return peer.last_heard if peer else None

    async def command(self, pod: str, op: CommandOp, reason: CommandReason) -> bool:
        """Send drain or vnc_revoke to a connected pod. False if it isn't connected."""
        peer = self._peers.get(pod)
        if peer is None:
            return False
        await self._send(peer, Command(**peer.envelope(), id=0, op=op, reason=reason))
        return True

    async def refresh(self) -> None:
        """Push configuration changes and queued commands to every connected pod."""
        for peer in list(self._peers.values()):
            try:
                await self._push_configure(peer)
                await self._push_commands(peer)
            except (ConnectionError, RuntimeError):
                log.info("Couldn't reach %s; it will reconnect", peer.binding.pod)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self.refresh()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(PATH, self._handle, allow_head=False)
        return app

    # ---- a connection

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise web.HTTPUnauthorized
        identity = await self._cluster.review_token(header.removeprefix("Bearer ").strip(), HUB_TOKEN_AUDIENCE)
        if identity is None:
            raise web.HTTPUnauthorized
        binding = await self._store.binding(identity.pod_uid, identity.pod_name)
        if binding is None:
            log.warning("A token for unknown pod %s", identity.pod_name)
            raise web.HTTPForbidden
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT_SECONDS, max_msg_size=MAX_FRAME_BYTES)
        await ws.prepare(request)
        if binding == "pending":
            await ws.close(code=CLOSE_NOT_YET_RECORDED, message=b"not recorded yet")
            return ws
        try:
            peer = await self._greet(binding, ws)
        except (ProtocolError, Violation, TimeoutError) as e:
            log.warning("%s: refused: %s", binding.pod, e)
            await ws.close(code=CLOSE_VIOLATION)
            return ws
        try:
            await self._serve(peer)
        except Violation as e:
            log.error("%s: %s; deleting the pod", binding.pod, e)  # noqa: TRY400 - the reason is the record
            await ws.close(code=CLOSE_VIOLATION)
            await self._cluster.delete_pod(binding.pod)
        except ProtocolError as e:
            log.warning("%s: bad message: %s", binding.pod, e)
            await ws.close(code=CLOSE_VIOLATION)
        finally:
            if self._peers.get(binding.pod) is peer:
                del self._peers[binding.pod]
        return ws

    async def _greet(self, binding: Binding, ws: web.WebSocketResponse) -> Peer:
        frame = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT_SECONDS)
        if frame.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
            raise ProtocolError("No hello")
        hello = decode_agent_message(frame.data)
        if not isinstance(hello, Hello):
            raise ProtocolError("The first message must be a hello")
        if hello.trip != binding.trip_id or hello.pod_name != binding.pod or hello.gen != binding.gen:
            raise Violation("The hello names another trip or pod")
        if VERSION not in hello.proto:
            raise ProtocolError("No protocol version in common")
        await self._store.record_agent_version(binding, hello.agent_version)  # for the admin page
        peer = Peer(binding, ws, hello.boot, hello.agent_version)
        old = self._peers.get(binding.pod)
        self._peers[binding.pod] = peer  # one live socket per pod: the newest wins
        if old is not None:
            await old.ws.close(code=CLOSE_REPLACED)
        await self._send(
            peer, HelloAck(**peer.envelope(), server_time=self._clock(), accepted_proto=VERSION, resend_from=0)
        )
        await self._receive(peer, hello.snapshot)
        for message in hello.unacked:
            await self._receive(peer, message)
        await self._push_configure(peer)
        await self._push_commands(peer)
        return peer

    async def _serve(self, peer: Peer) -> None:
        async for frame in peer.ws:
            if frame.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                break
            message = decode_agent_message(frame.data)
            if isinstance(message, Hello):
                raise ProtocolError("A second hello")
            await self._receive(peer, message)

    async def _receive(self, peer: Peer, message: State | Event | Alert | Bye | CartClaim) -> None:
        binding, now = peer.binding, self._clock()
        if message.trip != binding.trip_id:
            raise Violation(f"A message about trip {message.trip}")
        if message.boot != peer.boot:
            raise ProtocolError("A message from another boot")
        peer.last_heard = now
        if isinstance(message, State):
            await self._store.record_state(binding, peer.boot, message)
            if message.viewer.seconds > peer.vnc_seconds:  # the store counts each second once, across reconnects
                await self._store.count_vnc_seconds(binding, peer.boot, message.viewer.seconds, now)
                peer.vnc_seconds = message.viewer.seconds
        elif isinstance(message, CartClaim):
            await self._answer_claim(peer, message)
        else:
            if isinstance(message, Event):
                await self._store.record_event(binding, peer.boot, message)
            elif isinstance(message, Alert):
                held_back = await self._held_back(binding, now) if message.kind == "bookable" else None
                await self._store.record_alert(binding, peer.boot, message, now, held_back)
            else:
                await self._store.record_bye(binding, peer.boot, message, now)
            await self._send(peer, Ack(**peer.envelope(), acked=message.seq))
        await self._store.notify(binding.user_id)

    async def _answer_claim(self, peer: Peer, claim: CartClaim) -> None:
        binding, now = peer.binding, self._clock()
        found = await self._store.trip_row(binding.trip_id)
        reason: ClaimReason
        if found is None or not found[1]:
            reason = "carting_paused"
        else:
            row, _ = found
            allowance = await self._store.allowance(binding.user_id, now)
            switches = await self._store.switches()
            at = datetime.fromtimestamp(now, PACIFIC)
            lifted = row.quiet_lifted(now)
            gate = cart_gate(row.trip, allowance, switches, self._settings, at, lifted=lifted)
            reason = await self._store.claim(binding, gate, now, lifted=lifted)
        log.info("%s: cart claim for %s: %s", binding.pod, claim.depart, reason)
        await self._send(
            peer, CartClaimResult(**peer.envelope(), claim=claim.seq, granted=reason == "ok", reason=reason)
        )

    async def _held_back(self, binding: Binding, now: float) -> HeldBack:
        """Why a pod alerted a bookable sailing instead of holding it, as far as the records say."""
        found = await self._store.trip_row(binding.trip_id)
        if found is None or not found[1]:
            return "carting_paused"
        row, _ = found
        if not row.trip.auto_cart and not row.hold_requested(now):
            return "auto_cart_off"
        allowance = await self._store.allowance(binding.user_id, now)  # with the user's own quiet hours
        switches = await self._store.switches()
        at = datetime.fromtimestamp(now, PACIFIC)
        gate = cart_gate(row.trip, allowance, switches, self._settings, at, lifted=row.quiet_lifted(now))
        if gate != "ok":
            return gate
        return "holding_elsewhere" if await self._store.holding_elsewhere(binding) else "unknown"

    # ---- what the endpoint sends

    async def _push_configure(self, peer: Peer) -> None:
        binding, now = peer.binding, self._clock()
        found = await self._store.trip_row(binding.trip_id)
        if found is None:
            return  # deleted: the fleet loop drains the pod
        row, user_active = found
        peer.rev = row.rev
        configure = build_configure(
            peer.envelope(),
            row,
            mode=binding.mode,
            user_active=user_active,
            allowance=await self._store.allowance(binding.user_id, now),
            switches=await self._store.switches(),
            settings=self._settings,
            stretch_by=self._stretch(),
            now=now,
        )
        if not peer.streams:  # never sent to it (_encode_for), so a change to it is no change at all
            configure = msgspec.structs.replace(configure, cast_allowed=False)
        last = peer.sent_configure
        if last is not None:
            same_otherwise = _without(last, "interval_s") == _without(configure, "interval_s")
            if same_otherwise and not worth_pushing(last.interval_s, configure.interval_s):
                return
        peer.sent_configure = configure
        await self._send(peer, configure)

    async def _push_commands(self, peer: Peer) -> None:
        for row in await self._store.pending_commands(peer.binding.trip_id, peer.binding.pod):
            # Consumed first: a reconnect's greeting and the refresh tick can both find the same row.
            if not await self._store.consume_command(row["id"]):
                continue
            if row["op"] != "recycle":
                await self._send(peer, Command(**peer.envelope(), id=row["id"], op=row["op"], cart_id=row["cart_id"]))
            elif not row["stale"]:
                # An admin's recycle: the pod drains (handing back any cart) and the fleet starts the next one.
                await self._send(peer, Command(**peer.envelope(), id=row["id"], op="drain", reason="recycle"))

    async def _send(self, peer: Peer, message: ControlMessage) -> None:
        with contextlib.suppress(ConnectionResetError):
            await peer.ws.send_bytes(_encode_for(peer, message))


def _encode_for(peer: Peer, message: ControlMessage) -> bytes:
    """The frame for this agent. Agents before 0.7.0 refuse a configure with a field they don't know, so theirs
    leaves out `cast_allowed`: they have no streamed viewer, and an absent field reads as false on newer ones."""
    if isinstance(message, Configure) and not peer.streams:
        data = msgspec.to_builtins(message)
        del data["cast_allowed"]
        return msgspec.json.encode(data)
    return encode(message)


def _without(configure: Configure, *fields: str) -> dict[str, Any]:
    """The configure's content, minus its envelope and the given fields (for "has anything changed?")."""
    data = msgspec.to_builtins(configure)
    for name in ("seq", "boot", "rev", *fields):
        data.pop(name, None)
    return data  # type: ignore[no-any-return]
