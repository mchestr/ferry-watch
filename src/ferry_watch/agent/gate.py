"""The VNC gate: the only way into a browser pod's screen (docs/multi-tenant/design-reference.md §5.5).

An aiohttp server on :6080 that serves one thing, the WebSocket upgrade at ``GET /ws``, and bridges it to
Xvnc's unix socket (Xvnc has no network listener at all). Every upgrade must pass, in order:

1. ``Origin`` is the checkout site. Page script in the pod's own Chromium carries bcferries.com's origin,
   so it can't get in from inside.
2. The agent says a viewer may open the checkout now: lockdown passed, not draining, ferry-control
   hasn't revoked VNC, and a READY cart is held with every hold clock still running.
3. Either a **first connect**, carrying a checkout ticket (``fw.t1.<ticket>``) that names this pod, its
   trip, its owner and the held cart, plus a nonce (``fw.n1.<N>``) the viewer's browser keeps; or a
   **reconnect** by that same nonce. The first connect locks the cart to ``sha256(N)``, so only the
   device that opened it can come back, and nobody can join a second time with another ticket.

The response selects the ``binary`` subprotocol. One viewer at a time: a reconnect replaces a stale
socket. The agent closes the gate on release, expiry, revocation and drain.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ferry_watch.tickets import TicketError, verify

log = logging.getLogger(__name__)

PATH = "/ws"
SUBPROTOCOL = "binary"
TICKET_PREFIX = "fw.t1."
NONCE_PREFIX = "fw.n1."
HEARTBEAT_SECONDS = 20.0  # also keeps Cloudflare from idling out a viewer who is just reading
MAX_FAILURES_PER_MINUTE = 5
RATE_WINDOW_SECONDS = 60.0
MAX_CLIENT_FRAME_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024

_NONCE = re.compile(r"[A-Za-z0-9_-]{22,64}")  # at least 128 bits


@dataclass(frozen=True, slots=True)
class GateSettings:
    """All pinned in the pod's spec by the admission policy."""

    origin: str  # FW_VNC_ORIGIN, e.g. https://ferry-checkout.chestr.dev
    owner: str  # FW_OWNER, the user id tickets must name
    trip_id: str  # FW_TRIP_ID
    pod_name: str  # FW_POD_NAME
    keys: Mapping[str, Ed25519PublicKey]  # FW_VNC_PUBKEYS
    rfb_socket: Path  # Xvnc's unix socket


@dataclass(slots=True)
class _Lock:
    cart_id: str
    digest: bytes


@dataclass(slots=True)
class _Viewer:
    ws: web.WebSocketResponse
    done: asyncio.Event  # its Xvnc connection is closed and it no longer counts as the viewer
    bridge: asyncio.Task[None] | None = None


class Refused(Exception):
    """An upgrade the gate turned down. The reason is logged, never sent."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


class Gate:
    def __init__(
        self,
        settings: GateSettings,
        *,
        checkout_cart: Callable[[], str | None],
        viewer_changed: Callable[[bool], None],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        `checkout_cart` returns the id of the READY cart a viewer may open right now, or None when
        nobody may (step 2 above). `viewer_changed` is told when a viewer connects or goes, which drives
        the hold clocks.
        """
        self._settings = settings
        self._checkout_cart = checkout_cart
        self._viewer_changed = viewer_changed
        self._clock = clock
        self._monotonic = monotonic
        self._lock: _Lock | None = None
        self._used: set[str] = set()  # ticket ids, for the pod's lifetime
        self._failures: deque[float] = deque(maxlen=MAX_FAILURES_PER_MINUTE)  # the latest are enough
        self._viewer: _Viewer | None = None
        self._swap = asyncio.Lock()  # one viewer change at a time
        self._connected_at: float | None = None
        self._viewer_seconds = 0.0

    # ---- what the agent calls

    @property
    def viewer_connected(self) -> bool:
        return self._viewer is not None

    def viewer_seconds(self) -> float:
        """Seconds a viewer has been connected, over the pod's life (charged to the user's VNC minutes)."""
        current = self._monotonic() - self._connected_at if self._connected_at is not None else 0.0
        return self._viewer_seconds + current

    async def close(self, reason: str) -> None:
        """Disconnect the viewer, if any (release, a clock ran out, revocation, drain)."""
        async with self._swap:
            if self._viewer is not None:
                log.info("Closing the checkout viewer: %s", reason)
                await self._disconnect(self._viewer)

    def app(self) -> web.Application:
        app = web.Application(handler_args={"max_field_size": 4096})
        app.router.add_get(PATH, self._handle, allow_head=False)
        return app

    # ---- the upgrade

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        try:
            return await self._upgrade(request)
        except Refused as e:
            log.info("Refused a checkout viewer: %s", e)
            return web.Response(status=e.status, text=f"{e.status}\n")

    async def _upgrade(self, request: web.Request) -> web.StreamResponse:
        ws, cart_id, reconnect = self._admit(request)
        async with self._swap:
            # The agent may have released the cart while this request waited for the previous viewer.
            if self._checkout_cart() != cart_id:
                raise Refused(409, "the hold ended")
            if self._viewer is not None:
                await self._disconnect(self._viewer)
            try:
                reader, writer = await asyncio.open_unix_connection(self._settings.rfb_socket)
            except OSError as e:
                log.warning("Couldn't reach Xvnc: %s", e)
                raise Refused(502, "Xvnc unreachable") from None
            try:
                await ws.prepare(request)
            except BaseException:
                writer.close()
                raise
            viewer = _Viewer(ws, asyncio.Event())
            self._viewer = viewer
            self._connected_at = self._monotonic()
            log.info("Checkout viewer %s", "reconnected" if reconnect else "connected")
            self._viewer_changed(True)
            viewer.bridge = asyncio.create_task(self._serve(viewer, reader, writer), name="checkout bridge")
        try:
            await asyncio.wait([viewer.bridge])
        finally:
            viewer.bridge.cancel()
            await ws.close()  # waits a while for a stale client's close frame, but holds up nobody
        return ws

    async def _serve(self, viewer: _Viewer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _bridge(viewer.ws, reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self._gone(viewer)

    def _admit(self, request: web.Request) -> tuple[web.WebSocketResponse, str, bool]:
        """Steps 1-3. Synchronous on purpose: the lock is taken with no await in between."""
        settings = self._settings
        if request.headers.get("Origin") != settings.origin:
            raise Refused(403, "wrong origin")
        cart_id = self._checkout_cart()
        if cart_id is None:
            raise Refused(409, "no checkout open")
        ws = web.WebSocketResponse(
            protocols=(SUBPROTOCOL,), heartbeat=HEARTBEAT_SECONDS, max_msg_size=MAX_CLIENT_FRAME_BYTES
        )
        ready = ws.can_prepare(request)
        if not ready.ok or ready.protocol != SUBPROTOCOL:
            raise Refused(400, "not a binary WebSocket upgrade")

        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        nonce = _one(offered, NONCE_PREFIX)
        if nonce is None or not _NONCE.fullmatch(nonce):
            raise self._failed("no nonce")
        digest = hashlib.sha256(nonce.encode()).digest()

        lock = self._lock if self._lock is not None and self._lock.cart_id == cart_id else None
        if lock is not None:
            if not hmac.compare_digest(lock.digest, digest):
                raise self._failed("wrong nonce for a claimed checkout")
            return ws, cart_id, True

        self._check_rate()
        ticket = _one(offered, TICKET_PREFIX)
        if ticket is None:
            raise self._failed("no ticket")
        try:
            claims = verify(ticket, settings.keys, now=self._clock())
        except TicketError as e:
            raise self._failed(f"ticket: {e}") from None
        if (claims.sub, claims.trip, claims.pod, claims.cart_id) != (
            settings.owner,
            settings.trip_id,
            settings.pod_name,
            cart_id,
        ):
            raise self._failed("ticket is for another user, pod or cart")
        if claims.jti in self._used:
            raise self._failed("ticket already used")
        self._used.add(claims.jti)
        self._lock = _Lock(cart_id, digest)
        return ws, cart_id, False

    def _check_rate(self) -> None:
        now = self._monotonic()
        while self._failures and now - self._failures[0] >= RATE_WINDOW_SECONDS:
            self._failures.popleft()
        if len(self._failures) >= MAX_FAILURES_PER_MINUTE:
            raise Refused(429, "too many failed attempts")

    def _failed(self, reason: str) -> Refused:
        self._failures.append(self._monotonic())
        return Refused(403, reason)

    # ---- viewers

    async def _disconnect(self, viewer: _Viewer) -> None:
        """End a viewer's bridge and wait until its Xvnc connection is closed (Xvnc takes one at a time)."""
        if viewer.bridge is not None:
            viewer.bridge.cancel()
        await viewer.done.wait()

    def _gone(self, viewer: _Viewer) -> None:
        if self._viewer is viewer:
            self._viewer = None
            if self._connected_at is not None:
                self._viewer_seconds += self._monotonic() - self._connected_at
                self._connected_at = None
            log.info("Checkout viewer disconnected")
            self._viewer_changed(False)
        viewer.done.set()


def _one(offered: list[str], prefix: str) -> str | None:
    """The value of the single subprotocol with this prefix (None if absent or repeated)."""
    values = [p.removeprefix(prefix) for p in offered if p.startswith(prefix)]
    return values[0] if len(values) == 1 else None


async def _bridge(ws: web.WebSocketResponse, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes both ways until either side closes."""

    async def to_xvnc() -> None:
        async for frame in ws:
            if frame.type != WSMsgType.BINARY:
                break  # RFB is binary only
            writer.write(frame.data)
            await writer.drain()

    async def to_viewer() -> None:
        while data := await reader.read(READ_CHUNK_BYTES):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(to_xvnc()), asyncio.create_task(to_viewer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, ConnectionError):
                log.warning("Checkout bridge failed: %r", result)
