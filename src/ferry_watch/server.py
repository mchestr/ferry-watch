"""HTTP API, WebSocket push, and the built frontend (see docs/api.md).

There is no authentication here: the service is meant to sit behind an authenticating proxy.
What it does guard against is a malicious page in the user's browser driving it cross-site:
mutating requests must be JSON (which a cross-site form can't send) and same-origin, and
WebSocket upgrades must come from the same origin.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
import mimetypes
import re
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .bcferries.errors import BookingError
from .bcferries.site import SiteBrowser
from .bcferries.terminals import TerminalDirectory, Terminals
from .bcferries.urls import USER_AGENT
from .models import Trip, ValidationError, today
from .notify import AlertDispatcher, LogNotifier, Message, NotifyError, PushoverNotifier, TextAlerts, trip_title
from .settings import Settings
from .state import AlertMemory, EventKind, RuntimeState
from .store import ConfigStore, TripNotFound
from .views import HEARTBEAT_STALE_SECONDS, state_view
from .watcher import Browser, Watcher

log = logging.getLogger(__name__)

PING_SECONDS = 15
SEND_TIMEOUT_SECONDS = 10
COALESCE_SECONDS = 0.1
TEST_ALERT_COOLDOWN_SECONDS = 30

_dumps = functools.partial(json.dumps, allow_nan=False)
mimetypes.add_type("application/manifest+json", ".webmanifest")


class ApiError(Exception):
    def __init__(self, status: int, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field


@dataclass(slots=True)
class Services:
    settings: Settings
    store: ConfigStore
    state: RuntimeState
    terminals: TerminalDirectory
    alerts: AlertDispatcher
    watcher: Watcher
    hub: Hub = field(init=False)
    watcher_task: asyncio.Task[None] | None = None
    last_test_alert: float = 0.0

    def __post_init__(self) -> None:
        self.hub = Hub(self.snapshot)
        self.state.subscribe(self.hub.notify)
        self.store.subscribe(self.hub.notify)

    def title(self, trip: Trip) -> str:
        terminals = self.terminals.cached
        route = (
            terminals.route_name(trip.origin, trip.destination) if terminals else f"{trip.origin} → {trip.destination}"
        )
        return trip_title(trip, route)

    def snapshot(self) -> dict[str, Any]:
        return state_view(
            self.store.config,
            self.state,
            self.terminals.cached,
            checkout_url=self.settings.checkout_url,
            login_configured=self.settings.credentials is not None,
            alerts_configured=self.alerts.configured,
        )


SERVICES = web.AppKey("services", Services)


# ---- WebSocket fan-out


class _Client:
    """One WebSocket: a sender task that only ever sends the newest snapshot."""

    def __init__(self, ws: web.WebSocketResponse, request: web.Request) -> None:
        self.ws = ws
        self.request = request
        self.pending: str | None = None
        self.ready = asyncio.Event()

    def offer(self, payload: str) -> None:
        self.pending = payload  # replaces anything not sent yet
        self.ready.set()

    async def run(self) -> None:
        while not self.ws.closed:
            try:
                await asyncio.wait_for(self.ready.wait(), PING_SECONDS)
            except TimeoutError:
                payload = _dumps({"type": "ping", "server_time": time.time()})
            else:
                self.ready.clear()
                payload, self.pending = self.pending or "", None
                if not payload:
                    continue
            try:
                await asyncio.wait_for(self.ws.send_str(payload), SEND_TIMEOUT_SECONDS)
            except (TimeoutError, ConnectionError, RuntimeError):
                # A client that stopped reading (e.g. a suspended phone tab): drop it hard.
                if self.request.transport is not None:
                    self.request.transport.abort()
                return


class Hub:
    """Sends a fresh state snapshot to every WebSocket client after changes (coalesced)."""

    def __init__(self, snapshot: Callable[[], dict[str, Any]]) -> None:
        self._snapshot = snapshot
        self._clients: set[_Client] = set()
        self._dirty = asyncio.Event()

    def notify(self) -> None:
        self._dirty.set()

    def _payload(self) -> str:
        return _dumps({"type": "state", "data": self._snapshot()})

    async def run(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(COALESCE_SECONDS)  # one snapshot for a burst of changes
            self._dirty.clear()
            if not self._clients:
                continue
            try:
                payload = self._payload()
            except Exception:
                log.exception("Couldn't build the state snapshot")
                continue
            for client in list(self._clients):
                client.offer(payload)

    async def serve(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=PING_SECONDS * 2)
        await ws.prepare(request)
        client = _Client(ws, request)
        client.offer(self._payload())
        self._clients.add(client)
        sender = asyncio.create_task(client.run())
        try:
            async for _msg in ws:  # nothing is expected from the client; this just keeps it open
                pass
        finally:
            self._clients.discard(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        return ws

    async def close_all(self) -> None:
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                await client.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutting down")


# ---- middleware


def _json_error(status: int, message: str, field: str | None = None) -> web.Response:
    return web.json_response({"error": message, "field": field}, status=status, dumps=_dumps)


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is not None:
        return urllib.parse.urlsplit(origin).netloc == request.host
    # No Origin header: older browsers send Sec-Fetch-Site; non-browser clients send neither.
    return request.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")


@web.middleware
async def error_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as e:
        return _json_error(e.status, e.message, e.field)
    except ValidationError as e:
        return _json_error(400, e.message, e.field)
    except TripNotFound:
        return _json_error(404, "No such trip")
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Unhandled error in %s %s", request.method, request.path)
        return _json_error(500, "Something went wrong; see the server log")


@web.middleware
async def cross_site_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _same_origin(request):
            raise ApiError(403, "Cross-origin requests aren't allowed")
        if request.content_type != "application/json":
            raise ApiError(415, "Send a JSON body with Content-Type: application/json")
    return await handler(request)


def content_security_policy(index_html: str) -> str:
    """A strict CSP for the built page; its inline scripts (the theme bootstrap) are allowed by hash."""
    hashes = [
        f"'sha256-{base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()}'"
        for script in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index_html, flags=re.DOTALL)
    ]
    return "; ".join(
        [
            "default-src 'self'",
            " ".join(["script-src 'self'", *hashes]),
            "style-src 'self' 'unsafe-inline'",  # toast and dialog libraries inject styles at runtime
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def security_headers_middleware(csp: str | None) -> Callable[..., Awaitable[web.StreamResponse]]:
    @web.middleware
    async def middleware(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if csp:
            # Only documents act on it, and the file response's type isn't known until it's sent.
            response.headers.setdefault("Content-Security-Policy", csp)
        return response

    return middleware


@web.middleware
async def cache_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    response = await handler(request)
    if request.path.startswith("/assets/"):
        # Vite fingerprints asset file names, so they never change.
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    else:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---- handlers


def _services(request: web.Request) -> Services:
    return request.app[SERVICES]


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json(loads=functools.partial(json.loads, parse_constant=_reject_constant))
    except ValueError:
        raise ApiError(400, "The body isn't valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "The body must be a JSON object")
    return data


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} isn't allowed")


def _state(services: Services) -> web.Response:
    return web.json_response(services.snapshot(), dumps=_dumps)


async def _terminals_or_503(services: Services) -> Terminals:
    try:
        return await services.terminals.get()
    except BookingError:
        raise ApiError(503, "BC Ferries' terminal list is unavailable right now; try again shortly") from None


def _check_route(trip: Trip, terminals: Terminals) -> None:
    if trip.origin not in terminals:
        raise ValidationError("from", "Unknown terminal")
    if not terminals.has_route(trip.origin, trip.destination):
        raise ValidationError("to", "BC Ferries has no bookable route between these terminals")


def _check_date(trip: Trip) -> None:
    if trip.date < today():
        raise ValidationError("date", "That date has passed")


async def get_state(request: web.Request) -> web.Response:
    return _state(_services(request))


async def get_terminals(request: web.Request) -> web.Response:
    terminals = await _terminals_or_503(_services(request))
    items = sorted((t.to_json() for t in terminals.values()), key=lambda t: t["label"])
    return web.json_response({"terminals": items}, dumps=_dumps)


async def patch_settings(request: web.Request) -> web.Response:
    services = _services(request)
    data = await _body(request)
    before = services.store.config.enabled
    config = services.store.update_settings(data)
    if config.enabled != before:
        services.state.event(EventKind.INFO, "Watching resumed" if config.enabled else "Watching paused")
    return _state(services)


async def post_trip(request: web.Request) -> web.Response:
    services = _services(request)
    data = await _body(request)
    data.pop("id", None)  # the server assigns ids
    trip = Trip.from_json(data)
    _check_route(trip, await _terminals_or_503(services))
    _check_date(trip)
    services.store.add_trip(trip)
    services.state.event(EventKind.INFO, "Trip added", trip.id, services.title(trip))
    return _state(services)


async def patch_trip(request: web.Request) -> web.Response:
    services = _services(request)
    current = services.store.require(request.match_info["trip_id"])
    data = await _body(request)
    trip = Trip.from_json(data, base=current)
    if (trip.origin, trip.destination) != (current.origin, current.destination):
        _check_route(trip, await _terminals_or_503(services))
    if trip.date != current.date:
        _check_date(trip)
    services.store.replace_trip(trip)
    if trip.search_key() != current.search_key():
        services.state.trip(trip.id).reset_results()
    if set(data) - {"enabled"}:
        services.state.event(EventKind.INFO, "Trip updated", trip.id, services.title(trip))
    return _state(services)


async def delete_trip(request: web.Request) -> web.Response:
    services = _services(request)
    trip = services.store.delete_trip(request.match_info["trip_id"])
    services.state.event(EventKind.INFO, "Trip removed", trip.id, services.title(trip))
    return _state(services)


async def release_cart(request: web.Request) -> web.Response:
    services = _services(request)
    trip_id = request.match_info["trip_id"]
    services.store.require(trip_id)
    cart_id = (await _body(request)).get("cart_id")
    if not isinstance(cart_id, str) or not services.watcher.release_cart(trip_id, cart_id):
        raise ApiError(409, "There's no held cart with that id to release")
    return _state(services)


async def dismiss_cart(request: web.Request) -> web.Response:
    services = _services(request)
    trip_id = request.match_info["trip_id"]
    services.store.require(trip_id)
    cart_id = (await _body(request)).get("cart_id")
    if not isinstance(cart_id, str) or not services.watcher.dismiss_cart(trip_id, cart_id):
        raise ApiError(409, "There's no failed cart with that id to dismiss")
    return _state(services)


async def check_now(request: web.Request) -> web.Response:
    services = _services(request)
    await _body(request)
    services.watcher.check_now()
    return _state(services)


async def test_alert(request: web.Request) -> web.Response:
    services = _services(request)
    await _body(request)
    if not services.alerts.configured:
        raise ApiError(409, "Pushover isn't configured")
    if time.monotonic() - services.last_test_alert < TEST_ALERT_COOLDOWN_SECONDS:
        raise ApiError(429, f"Wait {TEST_ALERT_COOLDOWN_SECONDS} seconds between test alerts")
    services.last_test_alert = time.monotonic()
    try:
        await services.alerts.send_now(Message(title="⛴ Ferry Watch test", body="Alerts are working."))
    except NotifyError as e:
        raise ApiError(502, str(e)) from None
    services.state.event(EventKind.INFO, "Test alert sent")
    return _state(services)


async def healthz(request: web.Request) -> web.Response:
    services = _services(request)
    task = services.watcher_task
    if task is not None and task.done():
        return web.Response(status=503, text="watcher stopped")
    if time.time() - services.state.heartbeat_at > HEARTBEAT_STALE_SECONDS:
        return web.Response(status=503, text="watcher stalled")
    return web.Response(text="ok")


async def websocket(request: web.Request) -> web.StreamResponse:
    if not _same_origin(request):
        raise ApiError(403, "Cross-origin WebSocket connections aren't allowed")
    return await _services(request).hub.serve(request)


# ---- app


def build_app(services: Services | None = None, static_dir: Path | None = None) -> web.Application:
    """Routes and middleware. `services` is set now (tests) or later by the production lifecycle."""
    index = static_dir / "index.html" if static_dir is not None else None
    csp = content_security_policy(index.read_text()) if index is not None and index.is_file() else None
    app = web.Application(
        middlewares=[security_headers_middleware(csp), cache_middleware, error_middleware, cross_site_middleware]
    )
    if services is not None:
        app[SERVICES] = services
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/terminals", get_terminals)
    app.router.add_get("/api/ws", websocket)
    app.router.add_patch("/api/settings", patch_settings)
    app.router.add_post("/api/trips", post_trip)
    app.router.add_patch("/api/trips/{trip_id}", patch_trip)
    app.router.add_delete("/api/trips/{trip_id}", delete_trip)
    app.router.add_post("/api/trips/{trip_id}/cart/release", release_cart)
    app.router.add_post("/api/trips/{trip_id}/cart/dismiss", dismiss_cart)
    app.router.add_post("/api/check", check_now)
    app.router.add_post("/api/alerts/test", test_alert)
    if static_dir is not None:
        index = static_dir / "index.html"

        async def index_page(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(index)

        app.router.add_get("/", index_page)
        if (static_dir / "assets").is_dir():
            app.router.add_static("/assets", static_dir / "assets")
        for item in sorted(static_dir.iterdir()):  # favicon, icons, manifest.webmanifest
            if item.is_file() and item.name != "index.html":
                app.router.add_get(f"/{item.name}", functools.partial(_file, item))
    return app


async def _file(path: Path, _request: web.Request) -> web.FileResponse:
    return web.FileResponse(path)


def create_app(settings: Settings) -> web.Application:
    """The production app: builds the services, and runs the watcher alongside the server."""
    app = build_app(static_dir=settings.static_dir)
    app.cleanup_ctx.append(functools.partial(_lifecycle, settings))
    return app


async def _lifecycle(settings: Settings, app: web.Application) -> AsyncIterator[None]:
    async with aiohttp.ClientSession() as http:
        terminals = TerminalDirectory(http, USER_AGENT, cache_path=settings.config_path.with_name("terminals.json"))
        state = RuntimeState()
        store = ConfigStore(settings.config_path)
        notifier = (
            PushoverNotifier(http, settings.pushover_token, settings.pushover_user_key)
            if settings.pushover_token and settings.pushover_user_key
            else LogNotifier()
        )

        def alert_failed(message: Message, error: str) -> None:
            state.event(EventKind.ERROR, f"Couldn't deliver the alert “{message.title}”: {error}")

        alerts = AlertDispatcher(notifier, alert_failed)
        browser = SiteBrowser(
            terminals, headless=settings.headless, window_size=settings.window_size, debug_dir=settings.debug_dir
        )
        await browser.start()
        site: Browser = browser
        watcher = Watcher(
            store,
            state,
            site,
            TextAlerts(alerts, terminals, settings.checkout_url),
            terminals,
            memory=AlertMemory(settings.config_path.with_name("alerted.json")),
            credentials=settings.credentials,
            cart_hold_minutes=settings.cart_hold_minutes,
        )
        services = Services(settings, store, state, terminals, alerts, watcher)
        app[SERVICES] = services

        services.watcher_task = asyncio.create_task(watcher.run(), name="watcher")
        services.watcher_task.add_done_callback(_watcher_done)
        hub_task = asyncio.create_task(services.hub.run(), name="websocket hub")
        warm = asyncio.create_task(_warm_terminals(terminals), name="load terminals")
        state.event(EventKind.INFO, "Ferry Watch started")
        try:
            yield
        finally:
            for task in (services.watcher_task, hub_task, warm):
                task.cancel()
            await asyncio.gather(services.watcher_task, hub_task, warm, return_exceptions=True)
            await services.hub.close_all()
            await alerts.aclose()
            await browser.stop()


def _watcher_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        # run() survives errors per cycle, so this is a bug; /healthz now fails and the pod restarts.
        log.critical("The watcher stopped", exc_info=task.exception())


async def _warm_terminals(terminals: TerminalDirectory) -> None:
    """Load the terminal list early (retrying), so the UI has route names from the start."""
    delay = 5.0
    while True:
        try:
            await terminals.get()
        except BookingError as e:
            log.warning("%s; retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
        else:
            return
