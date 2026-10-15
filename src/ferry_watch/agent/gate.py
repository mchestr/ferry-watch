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

The upgrade offers exactly one of two kinds of viewer, and the response selects it: ``binary``, KasmVNC's RFB
bridged to Xvnc (the classic viewer), or ``fw.cast1``, the streamed viewer (docs/multi-tenant/streamed-viewer/
PROTOCOL.md), which runs a stream session instead and is admitted only while the operator allows it. Both share
the admission above, the lock, the one viewer slot (a reconnect of either kind replaces the other), the viewer
seconds and a 6000 s connection cap. The agent closes the gate on release, expiry, revocation, drain and, for a
stream, the streamed viewer's withdrawal.

Before a classic viewer is bridged the pod's screen is restored, so KasmVNC finds it as 0.6.4 left it. From the
first viewer of a hold on, JavaScript dialogs are kept for the viewer to answer (a stream in its own sheet, a
classic viewer on the pod's screen, made wide enough for the dialog while one is open) instead of being dismissed
unseen; they're dismissed when the checkout is over, access is revoked or the pod drains.
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
from typing import Literal, Protocol

from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ferry_watch.tickets import TicketError, verify

from .cast_protocol import MAX_INBOUND_BYTES, CloseCode

log = logging.getLogger(__name__)

PATH = "/ws"
SUBPROTOCOL = "binary"
CAST_SUBPROTOCOL = "fw.cast1"
TICKET_PREFIX = "fw.t1."
NONCE_PREFIX = "fw.n1."
HEARTBEAT_SECONDS = 20.0  # also keeps Cloudflare from idling out a viewer who is just reading
MAX_FAILURES_PER_MINUTE = 5
RATE_WINDOW_SECONDS = 60.0
MAX_CLIENT_FRAME_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024
CLOSE_TIMEOUT_SECONDS = 2.0  # for a stale viewer's close frame, before the socket is dropped anyway
MAX_CONNECTION_SECONDS = 6000.0  # as Xvnc's -MaxConnectionTime, for both kinds
CAST_START_SPACING_SECONDS = 1.0  # at most one stream session start a second (a reconnect loop waits)
# A checkout that is over, access revoked, the pod draining: no viewer will answer a dialog any more.
_DIALOGS_END: frozenset[CloseCode] = frozenset({CloseCode.ENDED, CloseCode.REVOKED, CloseCode.DRAINING})

ViewerKind = Literal["rfb", "cast"]
_KINDS: Mapping[str, ViewerKind] = {SUBPROTOCOL: "rfb", CAST_SUBPROTOCOL: "cast"}

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
    done: asyncio.Event  # its Xvnc connection or stream session is finished and it no longer counts as the viewer
    bridge: asyncio.Task[None] | None = None
    kind: ViewerKind = "rfb"
    close_code: CloseCode | None = None  # how the gate ended it (a stream's close code; a classic viewer gets 1000)


class CastSession(Protocol):
    """A streamed viewer's session (stream.py): it reads and writes the socket until either side ends it."""

    async def run(self) -> None: ...

    async def close(self) -> None:
        """Teardown (PROTOCOL §3.5): bounded, and complete before the next viewer is let in."""


# The session for an admitted `fw.cast1` socket. Its verdict says whether it may go on (PROTOCOL §7.2).
CastFactory = Callable[[web.WebSocketResponse, Callable[[], CloseCode | None]], CastSession]
Verdict = Callable[[str, ViewerKind], CloseCode | None]


class ScreenControl(Protocol):
    async def restore(self) -> None:
        """The pod's own screen back (screen.py). Bounded; never raises."""

    async def fit_dialogs(self) -> None:
        """While a classic viewer is bridged: room on its screen for each open dialog (stream.ClassicScreen). Runs
        until cancelled, then puts the viewer's own screen back, bounded. Never raises."""


class DialogControl(Protocol):
    async def keep(self) -> None:
        """From now on, leave JavaScript dialogs for the viewer to answer (dialogs.py). Never raises."""

    async def release(self) -> None:
        """Dismiss every open dialog and dismiss new ones unseen again. Bounded; never raises."""


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
        cast: CastFactory | None = None,
        verdict: Verdict | None = None,
        screen: ScreenControl | None = None,
        dialogs: DialogControl | None = None,
        max_connection_seconds: float = MAX_CONNECTION_SECONDS,
    ) -> None:
        """
        `checkout_cart` returns the id of the READY cart a viewer may open right now, or None when
        nobody may (step 2 above). `viewer_changed` is told when a viewer connects or goes, which drives
        the hold clocks. `cast` makes stream sessions and `verdict` (TripWatcher.viewer_verdict) says whether
        a viewer may go on; without both, `fw.cast1` is refused.
        """
        self._settings = settings
        self._checkout_cart = checkout_cart
        self._viewer_changed = viewer_changed
        self._clock = clock
        self._monotonic = monotonic
        self._cast = cast
        self._verdict = verdict
        self._screen = screen
        self._dialogs = dialogs
        self._max_connection_seconds = max_connection_seconds
        self._last_cast_start = float("-inf")  # event loop time
        self._pacing = asyncio.Lock()  # stream session starts, one a second
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

    async def close(self, reason: str, code: CloseCode = CloseCode.ENDED) -> None:
        """Disconnect the viewer, if any: release or a clock ran out (ENDED), revocation (REVOKED), drain
        (DRAINING), or the streamed viewer withdrawn (WITHDRAWN, which leaves a classic viewer alone).

        Unless the stream was only withdrawn, open dialogs are dismissed too: nobody will answer them now.
        """
        async with self._swap:
            viewer = self._viewer
            if viewer is not None and (code is not CloseCode.WITHDRAWN or viewer.kind == "cast"):
                log.info("Closing the checkout viewer: %s", reason)
                viewer.close_code = code
                await self._disconnect(viewer)
            if code in _DIALOGS_END and self._dialogs is not None:
                await self._dialogs.release()

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
        ws, cart_id, reconnect, kind = self._admit(request)
        if kind == "cast":
            async with self._pacing:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0.0, self._last_cast_start + CAST_START_SPACING_SECONDS - loop.time()))
                viewer = await self._start_cast(request, ws, cart_id, reconnect)
                self._last_cast_start = loop.time()
        else:
            viewer = await self._start_rfb(request, ws, cart_id, reconnect)
        bridge = viewer.bridge
        assert bridge is not None  # noqa: S101 - set by both
        try:
            await asyncio.wait([bridge])
        finally:
            bridge.cancel()
            # A stream's code says why it ended; a classic viewer gets 1000 whatever the reason, as in 0.6.4.
            code = viewer.close_code if kind == "cast" and viewer.close_code is not None else 1000
            await ws.close(code=code)  # waits a while for a stale client's close frame, but holds up nobody
        return ws

    async def _start_rfb(
        self, request: web.Request, ws: web.WebSocketResponse, cart_id: str, reconnect: bool
    ) -> _Viewer:
        async with self._swap:
            # The agent may have released the cart while this request waited for the previous viewer.
            if self._checkout_cart() != cart_id:
                raise Refused(409, "the hold ended")
            await self._replace()
            if self._dialogs is not None:
                await self._dialogs.keep()  # the viewer answers them on the screen
            if self._screen is not None:
                await self._screen.restore()  # KasmVNC finds the pod's own screen, as in 0.6.4
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
            viewer = self._connected(_Viewer(ws, asyncio.Event()), reconnect)
            viewer.bridge = asyncio.create_task(self._serve(viewer, reader, writer), name="checkout bridge")
        return viewer

    async def _start_cast(
        self, request: web.Request, ws: web.WebSocketResponse, cart_id: str, reconnect: bool
    ) -> _Viewer:
        async with self._swap:
            if self._checkout_cart() != cart_id or self._cast_verdict(cart_id) is not None:
                raise Refused(409, "the hold ended or the stream was withdrawn")
            if request.transport is None or request.transport.is_closing():
                raise Refused(400, "the viewer left while it waited its turn")  # don't replace anyone for it
            await self._replace()
            if self._dialogs is not None:
                await self._dialogs.keep()  # the stream session shows them in its own sheet
            await ws.prepare(request)
            viewer = self._connected(_Viewer(ws, asyncio.Event(), kind="cast"), reconnect)
            viewer.bridge = asyncio.create_task(self._serve_cast(viewer, cart_id), name="checkout stream")
        return viewer

    async def _replace(self) -> None:
        if self._viewer is not None:
            self._viewer.close_code = CloseCode.REPLACED
            await self._disconnect(self._viewer)

    def _connected(self, viewer: _Viewer, reconnect: bool) -> _Viewer:
        self._viewer = viewer
        self._connected_at = self._monotonic()
        log.info(
            "Checkout %sviewer %s",
            "stream " if viewer.kind == "cast" else "",
            "reconnected" if reconnect else "connected",
        )
        self._viewer_changed(True)
        return viewer

    async def _serve(self, viewer: _Viewer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fitting = asyncio.ensure_future(self._screen.fit_dialogs()) if self._screen is not None else None
        try:
            async with asyncio.timeout(self._max_connection_seconds):
                await _bridge(viewer.ws, reader, writer)
        except TimeoutError:
            log.info("Checkout viewer reached the connection time cap")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            cancelled = False
            if fitting is not None:
                fitting.cancel()
                cancelled = await _finish(fitting)  # the viewer's own screen back before the next viewer
            self._gone(viewer)
            if cancelled:
                raise asyncio.CancelledError

    async def _serve_cast(self, viewer: _Viewer, cart_id: str) -> None:
        cap = asyncio.timeout(self._max_connection_seconds)
        session: CastSession | None = None
        try:
            assert self._cast is not None  # noqa: S101 - admitted only with a factory
            session = self._cast(viewer.ws, lambda: self._cast_verdict(cart_id))
            async with cap:
                await session.run()
        except TimeoutError:
            viewer.close_code = CloseCode.TIME_CAP if cap.expired() else CloseCode.INTERNAL
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the viewer is told and the next can come
            log.warning("The checkout stream failed: %s", type(e).__name__)  # never the message: it may hold page text
            viewer.close_code = CloseCode.INTERNAL
        finally:
            # The teardown finishes before the viewer counts as gone, however often this task is cancelled
            # meanwhile: the next viewer must not find a half torn-down session (PROTOCOL §3.5).
            cancelled = False
            if session is not None:
                closing = asyncio.ensure_future(session.close())
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        cancelled = True
                if not closing.cancelled() and closing.exception() is not None:
                    log.warning("The checkout stream's teardown failed: %s", type(closing.exception()).__name__)
            self._gone(viewer)
            if cancelled:
                raise asyncio.CancelledError

    def _cast_verdict(self, cart_id: str) -> CloseCode | None:
        if self._verdict is None:
            return CloseCode.WITHDRAWN
        return self._verdict(cart_id, "cast")

    def _admit(self, request: web.Request) -> tuple[web.WebSocketResponse, str, bool, ViewerKind]:
        """Steps 1-3. Synchronous on purpose: the lock is taken with no await in between."""
        settings = self._settings
        if request.headers.get("Origin") != settings.origin:
            raise Refused(403, "wrong origin")
        cart_id = self._checkout_cart()
        if cart_id is None:
            raise Refused(409, "no checkout open")
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        ws, kind = self._socket(request, offered, cart_id)

        nonce = _one(offered, NONCE_PREFIX)
        if nonce is None or not _NONCE.fullmatch(nonce):
            raise self._failed("no nonce")
        digest = hashlib.sha256(nonce.encode()).digest()

        lock = self._lock if self._lock is not None and self._lock.cart_id == cart_id else None
        if lock is not None:
            if not hmac.compare_digest(lock.digest, digest):
                raise self._failed("wrong nonce for a claimed checkout")
            return ws, cart_id, True, kind

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
        return ws, cart_id, False, kind

    def _socket(
        self, request: web.Request, offered: list[str], cart_id: str
    ) -> tuple[web.WebSocketResponse, ViewerKind]:
        """The one kind of viewer offered, and the socket for it. A stream needs the operator's leave."""
        kinds = [p for p in offered if p in _KINDS]
        if len(kinds) != 1:
            raise Refused(400, "offer exactly one of binary and fw.cast1")
        protocol = kinds[0]
        kind = _KINDS[protocol]
        ws = web.WebSocketResponse(
            protocols=(protocol,),
            heartbeat=HEARTBEAT_SECONDS,
            # aiohttp refuses a message of max_msg_size bytes or more: a stream's may be 4096 bytes, not more.
            max_msg_size=MAX_INBOUND_BYTES + 1 if kind == "cast" else MAX_CLIENT_FRAME_BYTES,
            timeout=CLOSE_TIMEOUT_SECONDS,
        )
        ready = ws.can_prepare(request)
        if not ready.ok or ready.protocol != protocol:
            raise Refused(400, "not a WebSocket upgrade")
        if kind == "cast" and (self._cast is None or self._cast_verdict(cart_id) is CloseCode.WITHDRAWN):
            raise Refused(400, "the streamed viewer isn't allowed")  # the kill switch: not a failed attempt
        return ws, kind

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
        """End a viewer's bridge and wait until its Xvnc connection is closed (Xvnc takes one at a time), or its
        stream session has finished its teardown."""
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


async def _finish(task: asyncio.Future[None]) -> bool:
    """Wait until `task` is done, however often this one is cancelled meanwhile. True if it was."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


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
