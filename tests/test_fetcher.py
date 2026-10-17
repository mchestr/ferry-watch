"""waitlist-fetcher's service (design.md §4.3-4.4), on an aiohttp test server with fake fetchers and transports."""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import logging
import re
import socket
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, get_args

import aiohttp
import msgspec
import pytest
from aiohttp.test_utils import TestClient, TestServer

from waitlist import fetchwire
from waitlist.fetcher import app as fetcher_app
from waitlist.fetcher.app import aiohttp_transports, build, serve, upstream_session
from waitlist.fetcher.config import ConfigError, FetcherConfig, ProviderLimit
from waitlist.fetcher.main import main
from waitlist.fetcher.transport import AiohttpTransport
from waitlist.fetchwire import FetchFailed, FetchOk, decode_reply
from waitlist.providers import Registry
from waitlist.providers.base import (
    Budget,
    Capabilities,
    FetchError,
    FetchErrorKind,
    HttpProvider,
    HttpResponse,
    ProviderMeta,
    Transport,
)
from waitlist.providers.buntzen import PROVIDER
from waitlist.providers.buntzen.client import FETCHER as BUNTZEN_CLIENT
from waitlist.providers.buntzen.model import BuntzenInventory, BuntzenQuery, PassItem

ServerFactory = Callable[..., Awaitable[TestServer]]
ClientFactory = Callable[..., Awaitable[TestClient]]

ID = "5be0d1c27f6a9e31"
QUERY = {"place_id": 10672, "date": "2027-07-01"}
MARKER = "M4RK3R-zq"  # must never reach a log line
INVENTORY = BuntzenInventory(
    place_id=10672, date="2027-07-01", items=[PassItem(period="morning", availability="open", status="Available")]
)
FIXTURES = Path(__file__).parent / "fixtures" / "buntzen"


def request_body(**changes: Any) -> bytes:
    return json.dumps({"v": 1, "id": ID, "provider": "buntzen", "query": QUERY, **changes}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class NoTransport:
    """The transport a fake fetcher gets when it shouldn't need one."""

    async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse:  # noqa: ARG002 - a fake
        raise AssertionError("no I/O expected")

    async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse:  # noqa: ARG002 - a fake
        raise AssertionError("no I/O expected")


@dataclass
class FakeFetcher:
    """Buntzen's id and hosts; what fetch does is up to each test."""

    behaviour: Callable[[Transport, Any], Awaitable[Any]]
    provider_id: str = "buntzen"
    hosts: tuple[str, ...] = ("api.yodelpass.com",)
    calls: list[Any] = field(default_factory=list)

    async def fetch(self, transport: Transport, query: Any) -> Any:
        self.calls.append(query)
        return await self.behaviour(transport, query)


def returns(value: object) -> Callable[[Transport, Any], Awaitable[Any]]:
    async def behaviour(transport: Transport, query: Any) -> Any:
        return value

    return behaviour


RETURNS_INVENTORY = returns(INVENTORY)


def raises(error: BaseException) -> Callable[[Transport, Any], Awaitable[Any]]:
    async def behaviour(transport: Transport, query: Any) -> Any:
        raise error

    return behaviour


@dataclass
class Rig:
    client: TestClient
    fetcher: FakeFetcher
    clock: Clock
    hosts_seen: list[frozenset[str]]

    async def post(self, body: bytes) -> tuple[int, FetchOk | FetchFailed | None]:
        resp = await self.client.post(fetchwire.PATH, data=body, headers={"Content-Type": "application/json"})
        raw = await resp.read()
        if resp.status != 200:
            assert raw == b""
            return resp.status, None
        assert resp.content_type == "application/json"
        assert len(raw) <= fetchwire.MAX_REPLY_BYTES
        return resp.status, decode_reply(raw)


@pytest.fixture
def rig_factory(aiohttp_server: ServerFactory, aiohttp_client: ClientFactory) -> Callable[..., Awaitable[Rig]]:
    async def make(
        behaviour: Callable[[Transport, Any], Awaitable[Any]] = RETURNS_INVENTORY,
        *,
        per_minute: int = 30,
        providers: dict[str, ProviderLimit] | None = None,
        fetcher: Any = None,
        transport: Transport | None = None,
        registry: Registry | None = None,
        timeout_s: int = 10,
    ) -> Rig:
        clock = Clock()
        fake = FakeFetcher(behaviour)
        hosts_seen: list[frozenset[str]] = []

        def transports(hosts: frozenset[str]) -> Transport:
            hosts_seen.append(hosts)
            return transport if transport is not None else NoTransport()

        config = FetcherConfig(
            upstream_timeout_seconds=timeout_s,
            providers={"buntzen": ProviderLimit(max_per_minute=per_minute)} if providers is None else providers,
        )
        extra: dict[str, Any] = {} if registry is None else {"registry": registry}
        chosen = fetcher if fetcher is not None else fake
        application = build(
            config, transport_factory=transports, fetchers={chosen.provider_id: chosen}, clock=clock, **extra
        )
        server = await aiohttp_server(application, access_log=None)  # as serve() runs it
        return Rig(client=await aiohttp_client(server), fetcher=fake, clock=clock, hosts_seen=hosts_seen)

    return make


async def test_a_fetch_replies_with_the_inventory(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    rig = await rig_factory()
    status, reply = await rig.post(request_body())
    assert status == 200
    assert isinstance(reply, FetchOk)
    assert reply.id == ID
    assert reply.inventory == msgspec.to_builtins(INVENTORY)
    assert msgspec.convert(reply.inventory, BuntzenInventory, strict=True) == INVENTORY  # as control reads it
    assert isinstance(reply.elapsed_ms, int)
    assert rig.fetcher.calls == [BuntzenQuery(place_id=10672, date="2027-07-01")]
    assert rig.hosts_seen == [frozenset({"api.yodelpass.com"})]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"[]",
        b"null",
        request_body(v=2),
        request_body(id="not-hex"),
        request_body(id="5BE0D1C27F6A9E31"),
        request_body(id=f"{ID}\n"),  # the wire's `$` lets a trailing newline through; it would split a log line
        request_body(provider="Bad Name"),
        request_body(provider="buntzen\n"),
        request_body(provider="nope\n"),
        request_body(url="https://evil.example/"),  # an extra field: refused, never a proxy
        request_body(headers={"x": "y"}),
        request_body(query="10672"),
        json.dumps({"v": 1, "id": ID, "query": QUERY}).encode(),  # no provider
        request_body(query={**QUERY, "url": "https://evil.example/"}),  # an extra query field
        request_body(query={"place_id": 99999, "date": "2027-07-01"}),  # a place the wire can't name
        request_body(query={"place_id": "10672", "date": "2027-07-01"}),  # strict: a string isn't a place
        request_body(query={"place_id": 10672, "date": "July 1"}),
        request_body(query={"place_id": 10672}),
        request_body(query={"place_id": 10672, "date": "2027-07-01" + " " * 5000}),  # over 4 KiB
    ],
)
async def test_an_invalid_request_is_400_with_an_empty_body(
    rig_factory: Callable[..., Awaitable[Rig]], body: bytes
) -> None:
    rig = await rig_factory()
    assert await rig.post(body) == (400, None)
    assert rig.fetcher.calls == []


@pytest.mark.parametrize("provider", ["bcferries", "nope", "buntzen"])
async def test_a_provider_that_isnt_configured_is_unexpected(
    rig_factory: Callable[..., Awaitable[Rig]], provider: str
) -> None:
    rig = await rig_factory(providers={})  # "off": the fetcher serves nobody
    status, reply = await rig.post(request_body(provider=provider))
    assert status == 200
    assert reply == FetchFailed(id=ID, error="unexpected", elapsed_ms=reply.elapsed_ms if reply else 0)
    assert rig.fetcher.calls == []


async def test_the_token_bucket_caps_without_queueing(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    rig = await rig_factory(per_minute=2)
    assert isinstance((await rig.post(request_body()))[1], FetchOk)
    assert isinstance((await rig.post(request_body()))[1], FetchOk)
    status, reply = await rig.post(request_body())
    assert status == 200
    assert isinstance(reply, FetchFailed)
    assert (reply.error, reply.http_status, reply.retry_after_s) == ("rate_limited", None, None)  # ours: no status
    assert len(rig.fetcher.calls) == 2
    rig.clock.now += 30  # refills 2/60 a second: one token
    assert isinstance((await rig.post(request_body()))[1], FetchOk)
    assert isinstance((await rig.post(request_body()))[1], FetchFailed)
    rig.clock.now += 3600  # never more than its capacity
    assert isinstance((await rig.post(request_body()))[1], FetchOk)
    assert isinstance((await rig.post(request_body()))[1], FetchOk)
    assert isinstance((await rig.post(request_body()))[1], FetchFailed)
    assert len(rig.fetcher.calls) == 5


async def test_the_concurrency_cap_answers_at_once(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    in_flight = 0

    async def slow(transport: Transport, query: Any) -> Any:
        nonlocal in_flight
        in_flight += 1
        if in_flight == PROVIDER.budget.concurrency:
            entered.set()
        await release.wait()
        return INVENTORY

    rig = await rig_factory(slow)
    first = [asyncio.create_task(rig.post(request_body())) for _ in range(PROVIDER.budget.concurrency)]
    await asyncio.wait_for(entered.wait(), 5)
    status, reply = await asyncio.wait_for(rig.post(request_body()), 5)  # answered while the others still wait
    assert status == 200
    assert isinstance(reply, FetchFailed)
    assert (reply.error, reply.http_status) == ("rate_limited", None)
    assert not any(t.done() for t in first)
    release.set()
    assert all(isinstance(r, FetchOk) for _, r in await asyncio.gather(*first))
    assert isinstance((await rig.post(request_body()))[1], FetchOk)  # the slots came back
    assert len(rig.fetcher.calls) == PROVIDER.budget.concurrency + 1


async def test_a_sites_429_passes_through_with_its_status(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    rig = await rig_factory(raises(FetchError("rate_limited", http_status=429, retry_after_s=120)))
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert (reply.error, reply.http_status, reply.retry_after_s) == ("rate_limited", 429, 120)


@pytest.mark.parametrize("kind", get_args(FetchErrorKind))
async def test_every_fetch_error_becomes_a_fetch_failed(rig_factory: Callable[..., Awaitable[Rig]], kind: str) -> None:
    rig = await rig_factory(raises(FetchError(kind, http_status=503, retry_after_s=7)))  # type: ignore[arg-type]
    status, reply = await rig.post(request_body())
    assert status == 200
    assert isinstance(reply, FetchFailed)
    assert (reply.id, reply.error, reply.http_status, reply.retry_after_s) == (ID, kind, 503, 7)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FetchError("upstream_error", http_status=999, retry_after_s=99_999), ("upstream_error", None, 3600)),
        (FetchError("upstream_error", http_status=42, retry_after_s=-5), ("upstream_error", None, 0)),
        (FetchError("bogus"), ("unexpected", None, None)),  # type: ignore[arg-type]
    ],
)
async def test_a_fetch_errors_fields_are_kept_within_the_wires_bounds(
    rig_factory: Callable[..., Awaitable[Rig]], error: FetchError, expected: tuple[object, ...]
) -> None:
    rig = await rig_factory(raises(error))
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert (reply.error, reply.http_status, reply.retry_after_s) == expected


async def test_any_other_exception_is_unexpected_and_logged_by_class_only(
    rig_factory: Callable[..., Awaitable[Rig]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = await rig_factory(raises(ZeroDivisionError(f"secret {MARKER}")))
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert (reply.error, reply.http_status) == ("unexpected", None)
    assert "ZeroDivisionError" in caplog.text
    assert MARKER not in caplog.text


async def test_a_fetch_that_runs_past_its_deadline_is_a_timeout(
    rig_factory: Callable[..., Awaitable[Rig]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetcher_app, "GRACE_SECONDS", -0.95)  # upstream 1 s + grace: a 50 ms deadline

    async def hangs(transport: Transport, query: Any) -> Any:
        await asyncio.sleep(30)
        return INVENTORY

    rig = await rig_factory(hangs, timeout_s=1)
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert reply.error == "timeout"
    rig = await rig_factory(raises(TimeoutError()))
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert reply.error == "timeout"


async def test_an_inventory_that_isnt_the_providers_is_unexpected(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    rig = await rig_factory(returns({"place_id": 10672, "date": "2027-07-01", "items": []}))
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert reply.error == "unexpected"
    unbounded = BuntzenInventory(  # built in Python, so nothing checked its bounds
        place_id=10672, date="2027-07-01", items=[PassItem(period="morning", availability="open", status="<b>" * 40)]
    )
    rig = await rig_factory(returns(unbounded))
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchFailed)
    assert reply.error == "unexpected"


class BigQuery(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    n: int


class BigInventory(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    blob: str


class BigProvider:
    """Only what the fetcher reads of a provider: its meta, budget and types."""

    meta = ProviderMeta(
        id="bigprov",
        name="Big",
        noun="thing",
        description="A provider whose answers can be large",
        checker="http",
        capabilities=Capabilities(auto_hold=False, checkout=False, alert_only=True),
        limit_key="maxAlertOnlyWatches",
        booking_prefixes=("https://big.example/",),
    )
    budget = Budget(default_per_minute=10, max_per_minute=30, concurrency=1)
    query_type = BigQuery
    inventory_type = BigInventory


async def test_the_reply_size_is_capped(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    registry, body, limits = big_rig_parts()

    for size, ok in ((60_000, True), (fetchwire.MAX_REPLY_BYTES, False)):
        fetcher = FakeFetcher(returns(BigInventory(blob="x" * size)), provider_id="bigprov", hosts=("big.example",))
        rig = await rig_factory(providers=limits, fetcher=fetcher, registry=registry)
        _, reply = await rig.post(body)
        if ok:
            assert isinstance(reply, FetchOk)
        else:
            assert isinstance(reply, FetchFailed)
            assert reply.error == "too_large"


def big_rig_parts() -> tuple[Registry, bytes, dict[str, ProviderLimit]]:
    provider = cast(HttpProvider[Any, Any, Any], BigProvider())
    registry = Registry(metas=(provider.meta,), http={"bigprov": provider})
    body = json.dumps({"v": 1, "id": ID, "provider": "bigprov", "query": {"n": 1}}).encode()
    return registry, body, {"bigprov": ProviderLimit(max_per_minute=30)}


def taking(clocks: list[Clock], seconds: float, inventory: BigInventory) -> Callable[[Transport, Any], Awaitable[Any]]:
    async def behaviour(transport: Transport, query: Any) -> Any:
        clocks[0].now += seconds
        return inventory

    return behaviour


async def test_the_reply_size_is_measured_with_the_elapsed_ms_it_is_sent_with(
    rig_factory: Callable[..., Awaitable[Rig]],
) -> None:
    registry, body, limits = big_rig_parts()
    empty = len(fetchwire.encode(FetchOk(id=ID, inventory={"blob": ""}, elapsed_ms=0)))
    inventory = BigInventory(blob="x" * (fetchwire.MAX_REPLY_BYTES - 2 - empty))  # 2 bytes under, at elapsed_ms=0
    # elapsed_ms 0 and 100 fit (100 is exactly the cap); 600000, and anything clamped to it, is 3 bytes over.
    for seconds, elapsed, ok in ((0, 0, True), (0.1, 100, True), (600, 600_000, False), (5000, 600_000, False)):
        clocks: list[Clock] = []
        fetcher = FakeFetcher(taking(clocks, seconds, inventory), provider_id="bigprov", hosts=("big.example",))
        rig = await rig_factory(providers=limits, fetcher=fetcher, registry=registry)
        clocks.append(rig.clock)
        _, reply = await rig.post(body)  # never more than MAX_REPLY_BYTES on the wire (Rig.post)
        if ok:
            assert isinstance(reply, FetchOk), seconds
        else:
            assert isinstance(reply, FetchFailed), seconds
            assert reply.error == "too_large"
        assert reply.elapsed_ms == elapsed


@functools.cache
def gzip_of_zeros(mib: int) -> bytes:
    """A small gzip body that inflates to `mib` MiB."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    block = bytes(1 << 20)
    return b"".join([*(compressor.compress(block) for _ in range(mib)), compressor.flush()])


async def in_pieces(data: bytes) -> AsyncIterator[bytes]:
    """A body sent chunked, with no Content-Length."""
    for start in range(0, len(data), 16 * 1024):
        yield data[start : start + 16 * 1024]


async def test_a_compressed_request_is_400_and_never_inflated(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    rig = await rig_factory()
    for encoding, data in (
        ("gzip", gzip.compress(request_body())),  # a valid request once inflated: still refused
        ("GZIP", gzip.compress(request_body())),
        ("deflate", zlib.compress(request_body())),
        ("br", request_body()),
        ("identity, gzip", gzip.compress(request_body())),
    ):
        headers = {"Content-Type": "application/json", "Content-Encoding": encoding}
        resp = await rig.client.post(fetchwire.PATH, data=data, headers=headers)
        assert (resp.status, await resp.read()) == (400, b""), encoding
    assert rig.fetcher.calls == []
    identity = {"Content-Type": "application/json", "Content-Encoding": "identity"}
    resp = await rig.client.post(fetchwire.PATH, data=request_body(), headers=identity)
    assert resp.status == 200  # identity is no encoding at all
    assert len(rig.fetcher.calls) == 1
    bomb = gzip_of_zeros(250)  # about 250 KB that inflates to 250 MiB, over the pod's 192Mi limit
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip", "Connection": "close"}
    tracemalloc.start()
    try:
        sent: tuple[bytes | AsyncIterator[bytes], ...] = (bomb, in_pieces(bomb))  # then chunked, with no length
        for each in sent:
            resp = await rig.client.post(fetchwire.PATH, data=each, headers=headers)
            assert (resp.status, await resp.read()) == (400, b"")
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20, f"peak {peak} bytes"  # aiohttp inflating it itself peaks at about 250 MiB
    assert len(rig.fetcher.calls) == 1


async def test_only_three_routes_exist(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    rig = await rig_factory()
    for path in ("/livez", "/readyz"):
        resp = await rig.client.get(path)
        assert (resp.status, await resp.text()) == (200, "ok\n")
    assert (await rig.client.get(fetchwire.PATH)).status == 405
    assert (await rig.client.post("/livez")).status == 405
    assert (await rig.client.head("/livez")).status == 405
    for path in ("/", "/metrics", "/v1", "/v2/fetch", "/v1/fetch/x", "/admin"):
        assert (await rig.client.get(path)).status == 404
        assert (await rig.client.post(path, data=request_body())).status in (404, 405)


async def test_no_body_or_header_ever_reaches_the_log(
    rig_factory: Callable[..., Awaitable[Rig]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    class MarkedTransport:
        async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse:  # noqa: ARG002 - a fake
            return HttpResponse(status=200, content_type="application/json", body=json.dumps({"x": MARKER}).encode())

        async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse:  # noqa: ARG002 - a fake
            raise AssertionError

    outcomes = iter(
        [
            INVENTORY,
            FetchError("site_changed", http_status=200),
            RuntimeError(MARKER),
            ValueError(f"body was {MARKER}"),
        ]
    )

    async def reads_then(transport: Transport, query: Any) -> Any:
        response = await transport.post_json("https://api.yodelpass.com/x", {"date": query.date}, headers={})
        assert MARKER.encode() in response.body
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    rig = await rig_factory(reads_then, transport=MarkedTransport())
    headers = {"User-Agent": MARKER, "X-Marker": MARKER, "Cookie": f"s={MARKER}", "Content-Type": "application/json"}
    for _ in range(4):
        resp = await rig.client.post(fetchwire.PATH, data=request_body(), headers=headers)
        assert resp.status == 200
    for bad in (request_body(query={**QUERY, "x": MARKER}), MARKER.encode(), request_body(provider="nope")):
        await rig.client.post(fetchwire.PATH, data=bad, headers=headers)
    assert MARKER not in caplog.text
    assert "2027-07-01" not in caplog.text  # nor the query's date
    lines = [r.getMessage() for r in caplog.records if r.name == "waitlist.fetcher" and r.levelno == logging.INFO]
    pattern = re.compile(
        r"fetch provider=[a-z][a-z0-9_]+ id=[a-f0-9]{16} result=(ok|failed) error=([a-z_]+|-) "
        r"http_status=([0-9]{3}|-) elapsed_ms=[0-9]+"
    )
    assert len(lines) == 5  # one per request that reached a provider decision
    assert all(pattern.fullmatch(line) for line in lines), lines
    assert lines[0].startswith(f"fetch provider=buntzen id={ID} result=ok error=- http_status=-")
    assert "result=failed error=site_changed http_status=200" in lines[1]


async def test_the_real_buntzen_client_through_the_service(rig_factory: Callable[..., Awaitable[Rig]]) -> None:
    body = (FIXTURES / "synth-halfday-ha-quirk.json").read_bytes()
    seen: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    class FixtureTransport:
        async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse:
            seen.append((url, dict(payload), dict(headers)))
            return HttpResponse(status=200, content_type="application/json", body=body)

        async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse:  # noqa: ARG002 - a fake
            raise AssertionError

    rig = await rig_factory(fetcher=BUNTZEN_CLIENT, transport=FixtureTransport())
    _, reply = await rig.post(request_body())
    assert isinstance(reply, FetchOk)
    inventory = msgspec.convert(reply.inventory, PROVIDER.inventory_type, strict=True)
    assert [(i.period, i.availability) for i in inventory.items] == [("morning", "open"), ("unknown", "sold_out")]
    assert seen[0][0] == "https://api.yodelpass.com/api/portal-catalog-items"


def test_build_refuses_a_provider_it_has_no_client_for() -> None:
    config = FetcherConfig(providers={"buntzen": ProviderLimit(max_per_minute=30)})
    with pytest.raises(ConfigError, match="buntzen"):
        build(config, transport_factory=lambda _hosts: NoTransport(), fetchers={})
    with pytest.raises(ConfigError, match="bcferries"):
        build(
            FetcherConfig(providers={"bcferries": ProviderLimit(max_per_minute=3)}),
            transport_factory=lambda _hosts: NoTransport(),
        )


async def test_the_production_transports_and_session() -> None:
    config = FetcherConfig(upstream_timeout_seconds=7, max_body_bytes=4096)
    async with upstream_session(config) as session:
        assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        assert session.trust_env is False
        assert session.auto_decompress is False  # AiohttpTransport inflates within its cap instead
        assert session.timeout.total == 7
        transport = aiohttp_transports(session, config)(frozenset({"api.yodelpass.com"}))
        assert isinstance(transport, AiohttpTransport)
        with pytest.raises(FetchError, match="unexpected"):  # https:443 at a declared host, nothing else
            await transport.get("https://example.com/", headers={})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def test_serve_runs_until_stopped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(serve(FetcherConfig(listen_port=port), stop))
    async with aiohttp.ClientSession() as http:
        for _ in range(100):
            try:
                async with http.get(f"http://127.0.0.1:{port}/readyz") as resp:
                    if resp.status == 200:
                        break
            except aiohttp.ClientConnectionError:
                await asyncio.sleep(0.05)
        async with http.post(
            f"http://127.0.0.1:{port}{fetchwire.PATH}", data=request_body(), headers={"User-Agent": MARKER}
        ) as resp:
            reply = decode_reply(await resp.read())
    assert isinstance(reply, FetchFailed)
    assert reply.error == "unexpected"  # no provider configured: nothing is fetched
    stop.set()
    await asyncio.wait_for(task, 10)
    assert MARKER not in caplog.text  # no access log


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    assert "waitlist-fetcher" in capsys.readouterr().out


def test_a_bad_config_exits_with_its_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WL_FETCHER_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit) as caught:
        main(["run"])
    assert str(caught.value.code).startswith("waitlist-fetcher: ")
    (tmp_path / "bad.json").write_text(json.dumps({"providers": {"bcferries": {"maxPerMinute": 1}}}))
    monkeypatch.setenv("WL_FETCHER_CONFIG", str(tmp_path / "bad.json"))
    with pytest.raises(SystemExit) as caught:
        main(["run"])
    assert "not a provider waitlist-fetcher can check" in str(caught.value.code)
    with pytest.raises(SystemExit) as caught:
        main(["serve"])
    assert caught.value.code == 2
