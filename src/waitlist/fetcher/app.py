"""waitlist-fetcher's HTTP service (design.md §4.3-4.4): `POST /v1/fetch`, `GET /livez`, `GET /readyz`, nothing else.

Stateless, and holding nothing: no database, no token, no secret. waitlist-control (the only caller the network
policy admits) names a provider and that provider's typed query; this process makes the provider's request
through `AiohttpTransport`, parses the answer with the provider's client, and replies with the typed inventory
or an error kind. Every reply to a valid request is HTTP 200 (a `FetchFailed` included), so "the site failed"
stays apart from "the fetcher failed". Each provider has its own hard cap here, a token bucket and a
concurrency limit, which holds whatever control asks; an exhausted cap answers `rate_limited` with no
`http_status` at once, and nothing queues.

One INFO line per request; never a body, a header, or the query in a log line.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, get_args

import aiohttp
import msgspec
from aiohttp import web

from waitlist import fetchwire
from waitlist.fetchwire import FetchFailed, FetchOk, FetchRequest
from waitlist.providers import REGISTRY, Registry
from waitlist.providers.base import PROVIDER_ID, Fetcher, FetchError, FetchErrorKind, HttpProvider, Transport

from .config import ConfigError, FetcherConfig
from .registry import FETCHERS
from .transport import AiohttpTransport, PublicOnlyResolver

log = logging.getLogger("waitlist.fetcher")

# A fetch may take this much longer than the upstream timeout (the session's own) before it is cut off.
GRACE_SECONDS: Final = 2.0
MAX_ELAPSED_MS: Final = 600_000
FETCH_ERROR_KINDS: Final[frozenset[str]] = frozenset(get_args(FetchErrorKind))
# The wire's own patterns end in `$`, which lets a trailing newline through; these keep every logged id and
# provider on one line.
REQUEST_ID: Final = re.compile(r"[a-f0-9]{16}")

TransportFactory = Callable[[frozenset[str]], Transport]
"""Makes the transport for one fetcher, from the hosts it declares."""


def upstream_session(config: FetcherConfig) -> aiohttp.ClientSession:
    """The one shared session to the providers' sites: public addresses only, no cookies, no proxy from the
    environment, default TLS verification, the upstream timeout. Every request it makes passes
    allow_redirects=False (AiohttpTransport). Bodies arrive as sent (auto_decompress=False): aiohttp would
    inflate each read whole before any cap saw it, so AiohttpTransport inflates them itself, within its cap."""
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=8, ttl_dns_cache=60),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=config.upstream_timeout_seconds),
        auto_decompress=False,
    )


def aiohttp_transports(session: aiohttp.ClientSession, config: FetcherConfig) -> TransportFactory:
    """The production TransportFactory: AiohttpTransport over the shared session, with the body cap."""

    def make(hosts: frozenset[str]) -> Transport:
        return AiohttpTransport(session, hosts, max_bytes=config.max_body_bytes)

    return make


@dataclass(slots=True)
class TokenBucket:
    """Capacity `per_minute`, refilled at per_minute/60 a second, starting full."""

    per_minute: int
    clock: Callable[[], float]
    tokens: float = field(init=False)
    updated: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.per_minute)
        self.updated = self.clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(float(self.per_minute), self.tokens + max(0.0, now - self.updated) * self.per_minute / 60)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


@dataclass(slots=True)
class Gate:
    """One provider's cap: its token bucket and its fetches in flight (never more than `concurrency`)."""

    bucket: TokenBucket
    concurrency: int
    in_flight: int = 0

    def enter(self) -> bool:
        if self.in_flight >= self.concurrency or not self.bucket.take():
            return False
        self.in_flight += 1
        return True

    def leave(self) -> None:
        self.in_flight -= 1


@dataclass(frozen=True, slots=True)
class Route:
    """A configured provider: its pure half (types, budget), its client and its cap."""

    provider: HttpProvider[Any, Any, Any]
    fetcher: Fetcher[Any, Any]
    gate: Gate


READY = web.AppKey("ready", dict[str, bool])


class FetchService:
    """The routes' handlers, over the configured providers."""

    def __init__(
        self,
        routes: Mapping[str, Route],
        transport_factory: TransportFactory,
        *,
        timeout_s: float,
        clock: Callable[[], float],
    ) -> None:
        self.routes = dict(routes)
        self._transport_factory = transport_factory
        self._timeout_s = timeout_s
        self._clock = clock

    async def fetch(self, request: web.Request) -> web.Response:
        started = self._clock()
        req: FetchRequest | None = None
        if _plain_body(request):
            try:
                req = fetchwire.decode_request(await request.read())
            except (web.HTTPRequestEntityTooLarge, fetchwire.WireError):
                req = None
        if req is None or not REQUEST_ID.fullmatch(req.id) or not PROVIDER_ID.fullmatch(req.provider):
            log.warning("fetch refused a request that isn't a valid FetchRequest")
            return web.Response(status=400)
        route = self.routes.get(req.provider)
        if route is None:
            return self._reply(req, FetchFailed(id=req.id, error="unexpected"), started)
        try:
            query = msgspec.convert(req.query, route.provider.query_type, strict=True)
        except msgspec.ValidationError:
            log.warning("fetch provider=%s id=%s refused: not a query of this provider", req.provider, req.id)
            return web.Response(status=400)
        if not route.gate.enter():  # our own cap: answered at once, nothing queues
            return self._reply(req, FetchFailed(id=req.id, error="rate_limited"), started)
        try:
            reply = await self._fetch(req, route, query)
        finally:
            route.gate.leave()
        return self._reply(req, reply, started)

    async def livez(self, _: web.Request) -> web.Response:
        return web.Response(text="ok\n")

    async def readyz(self, request: web.Request) -> web.Response:
        if request.app[READY]["ready"]:
            return web.Response(text="ok\n")
        return web.Response(status=503, text="starting\n")

    async def _fetch(self, req: FetchRequest, route: Route, query: msgspec.Struct) -> FetchOk | FetchFailed:
        try:
            async with asyncio.timeout(self._timeout_s):
                transport = self._transport_factory(frozenset(route.fetcher.hosts))
                inventory = await route.fetcher.fetch(transport, query)
        except FetchError as e:
            return _failed(req, e)
        except TimeoutError:
            return FetchFailed(id=req.id, error="timeout")
        except Exception as e:  # noqa: BLE001 - a client's bug answers "unexpected"; only its class is logged
            log.warning("fetch provider=%s id=%s raised %s", req.provider, req.id, type(e).__name__)
            return FetchFailed(id=req.id, error="unexpected")
        builtins = _checked_inventory(req, route, inventory)
        if builtins is None:
            return FetchFailed(id=req.id, error="unexpected")
        return FetchOk(id=req.id, inventory=builtins, elapsed_ms=0)  # its size is checked as sent, in _reply

    def _reply(self, req: FetchRequest, reply: FetchOk | FetchFailed, started: float) -> web.Response:
        elapsed = min(max(int((self._clock() - started) * 1000), 0), MAX_ELAPSED_MS)
        reply = msgspec.structs.replace(reply, elapsed_ms=elapsed)
        body = fetchwire.encode(reply)
        if isinstance(reply, FetchOk) and len(body) > fetchwire.MAX_REPLY_BYTES:  # measured with its real elapsed_ms
            reply = FetchFailed(id=req.id, error="too_large", elapsed_ms=elapsed)
            body = fetchwire.encode(reply)
        failed = reply if isinstance(reply, FetchFailed) else None
        log.info(
            "fetch provider=%s id=%s result=%s error=%s http_status=%s elapsed_ms=%d",
            req.provider,
            req.id,
            "failed" if failed else "ok",
            failed.error if failed else "-",
            failed.http_status if failed and failed.http_status is not None else "-",
            elapsed,
        )
        return web.Response(body=body, content_type="application/json")


def build(
    config: FetcherConfig,
    *,
    transport_factory: TransportFactory,
    fetchers: Mapping[str, Fetcher[Any, Any]] = FETCHERS,
    registry: Registry = REGISTRY,
    clock: Callable[[], float] = time.monotonic,
) -> web.Application:
    """The fetcher's application. Raises ConfigError for a configured provider it has no client for."""
    routes: dict[str, Route] = {}
    for provider_id, limit in config.providers.items():
        provider = registry.http_provider(provider_id)
        fetcher = fetchers.get(provider_id)
        if provider is None or fetcher is None:
            raise ConfigError(f"providers.{provider_id}: not a provider waitlist-fetcher can check")
        gate = Gate(TokenBucket(limit.max_per_minute, clock), concurrency=provider.budget.concurrency)
        routes[provider_id] = Route(provider=provider, fetcher=fetcher, gate=gate)
    service = FetchService(
        routes, transport_factory, timeout_s=config.upstream_timeout_seconds + GRACE_SECONDS, clock=clock
    )

    async def readiness(app: web.Application) -> AsyncIterator[None]:
        app[READY]["ready"] = True
        yield
        app[READY]["ready"] = False

    # auto_decompress off: aiohttp would inflate a compressed request whole before client_max_size is checked.
    app = web.Application(client_max_size=fetchwire.MAX_REQUEST_BYTES, handler_args={"auto_decompress": False})
    app[READY] = {"ready": False}
    app.cleanup_ctx.append(readiness)
    app.router.add_post(fetchwire.PATH, service.fetch)
    app.router.add_get("/livez", service.livez, allow_head=False)
    app.router.add_get("/readyz", service.readyz, allow_head=False)
    return app


def _plain_body(request: web.Request) -> bool:
    """No Content-Encoding (or identity), and no declared length over MAX_REQUEST_BYTES: refused before reading."""
    encoding = request.headers.get("Content-Encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        return False
    return request.content_length is None or request.content_length <= fetchwire.MAX_REQUEST_BYTES


def _checked_inventory(req: FetchRequest, route: Route, inventory: object) -> dict[str, Any] | None:
    """The inventory as builtins, if it is the provider's type and within every bound control will check."""
    if not isinstance(inventory, route.provider.inventory_type):
        log.warning("fetch provider=%s id=%s: the client returned %s", req.provider, req.id, type(inventory).__name__)
        return None
    builtins = msgspec.to_builtins(inventory)
    try:  # a Struct built in Python is never checked against its bounds; control's convert would refuse it
        msgspec.convert(builtins, route.provider.inventory_type, strict=True)
    except msgspec.ValidationError:
        log.warning("fetch provider=%s id=%s: the client's inventory is out of bounds", req.provider, req.id)
        return None
    return builtins if isinstance(builtins, dict) else None


def _failed(req: FetchRequest, e: FetchError) -> FetchFailed:
    """A FetchError as the wire carries it, every field inside its bounds."""
    kind: FetchErrorKind = e.kind if e.kind in FETCH_ERROR_KINDS else "unexpected"
    status = e.http_status if isinstance(e.http_status, int) and 100 <= e.http_status <= 599 else None
    retry = e.retry_after_s
    retry = min(max(retry, 0), 3600) if isinstance(retry, int) else None
    return FetchFailed(id=req.id, error=kind, http_status=status, retry_after_s=retry)


async def serve(config: FetcherConfig, stop: asyncio.Event) -> None:
    """Run until `stop`: the shared upstream session, the app on 0.0.0.0:listenPort, no access log (it would
    carry the caller's headers)."""
    async with upstream_session(config) as session:
        app = build(config, transport_factory=aiohttp_transports(session, config))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", config.listen_port).start()  # noqa: S104 - control's pods call it
            log.info(
                "waitlist-fetcher serving on :%d for %s",
                config.listen_port,
                ", ".join(config.providers) or "no provider",
            )
            await stop.wait()
        finally:
            await runner.cleanup()
