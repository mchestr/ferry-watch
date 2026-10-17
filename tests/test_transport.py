"""waitlist-fetcher's transport (design.md §4.4): the refusals before any I/O, the public-only resolver, and the real
request path (no redirects, the multi-chunk read, the byte cap, compressed bodies, Retry-After, no cookies) against
a local server through the `_scheme`/`_port` test seam."""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import re
import socket
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import aiohttp
import aiohttp.abc
import aiohttp.connector
import pytest
import yarl
from aiohttp import web
from aiohttp.test_utils import TestServer

from waitlist.fetcher.app import upstream_session
from waitlist.fetcher.config import FetcherConfig
from waitlist.fetcher.transport import ACCEPT_ENCODING, AiohttpTransport, PublicOnlyResolver, retry_after
from waitlist.providers.base import FetchError

ROOT = Path(__file__).parent.parent
ServerFactory = Callable[..., Awaitable[TestServer]]


def port_of(server: TestServer) -> int:
    assert server.port is not None
    return server.port


class Untouchable:
    """A session that fails the test if the transport ever reaches it."""

    def request(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the transport made a request it should have refused")


def production(hosts: frozenset[str] = frozenset({"api.yodelpass.com"})) -> AiohttpTransport:
    return AiohttpTransport(cast(aiohttp.ClientSession, Untouchable()), hosts, max_bytes=1024)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api",  # not a declared host
        "https://api.yodelpass.com.evil.example/api",
        "https://yodelpass.com/api",
        "https://api.yodelpass.com:8443/api",  # not 443
        "https://api.yodelpass.com:80/api",
        "http://api.yodelpass.com/api",  # not https
        "ftp://api.yodelpass.com/api",
        "https://user:pass@api.yodelpass.com/api",  # userinfo
        "https://user@api.yodelpass.com/api",
        "https://api.yodelpass.com:99999/api",  # a port that doesn't parse
        "https://10.0.0.5/api",
        "https://127.0.0.1/api",
        "//api.yodelpass.com/api",
        "api.yodelpass.com/api",
        "",
    ],
)
async def test_refusals_happen_before_any_io(url: str, caplog: pytest.LogCaptureFixture) -> None:
    transport = production()
    with pytest.raises(FetchError) as caught:
        await transport.post_json(url, {"a": 1}, headers={})
    assert caught.value.kind == "unexpected"
    with pytest.raises(FetchError, match="unexpected"):
        await transport.get(url, headers={})
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_https_443_at_a_declared_host_is_allowed_through() -> None:
    for url in ("https://api.yodelpass.com/api/portal-catalog-items", "https://API.YODELPASS.COM:443/x"):
        with pytest.raises(AssertionError, match="should have refused"):  # reached the session: allowed
            await production().get(url, headers={})


def test_nothing_in_src_or_scripts_passes_the_test_seam() -> None:
    passing = re.compile(r"(?<![.\w])_(?:scheme|port)\s*=(?!=)|[\"']_(?:scheme|port)[\"']")
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for top in ("src", "scripts")
        for path in sorted((ROOT / top).rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if passing.search(line)
    ]
    assert offenders == []
    assert passing.search("AiohttpTransport(s, h, max_bytes=1, _scheme='http', _port=1)")  # the check bites


class FakeResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses
        self.closed = False

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        return [
            {"hostname": host, "host": a, "port": port, "family": family, "proto": 0, "flags": 0}
            for a in self.addresses
        ]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "address", ["10.0.0.5", "192.168.1.1", "172.16.0.1", "100.64.0.1", "127.0.0.1", "169.254.169.254", "::1", "fd00::1"]
)
async def test_the_resolver_refuses_non_public_answers(address: str) -> None:
    with pytest.raises(OSError, match="no public address"):
        await PublicOnlyResolver(FakeResolver(address)).resolve("api.yodelpass.com", 443)


async def test_the_resolver_keeps_only_the_public_answers() -> None:
    inner = FakeResolver("10.0.0.5", "104.18.1.1", "::1", "2606:4700::1")
    resolver = PublicOnlyResolver(inner)
    found = await resolver.resolve("api.yodelpass.com", 443)
    assert [r["host"] for r in found] == ["104.18.1.1", "2606:4700::1"]
    await resolver.close()
    assert inner.closed


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.1", "100.64.0.1", "::1", "127.0.0.1"])
async def test_a_private_answer_is_a_network_error_through_the_transport(
    address: str, aiohttp_server: ServerFactory
) -> None:
    app = web.Application()
    app.router.add_get("/x", reached)
    server = await aiohttp_server(app)
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(FakeResolver(address)), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar()) as session:
        transport = AiohttpTransport(
            session, frozenset({"api.yodelpass.test"}), max_bytes=1024, _scheme="http", _port=port_of(server)
        )
        with pytest.raises(FetchError) as caught:
            await transport.get(f"http://api.yodelpass.test:{port_of(server)}/x", headers={})
    assert caught.value.kind == "network"


async def test_the_production_session_resolves_through_the_public_only_resolver(
    aiohttp_server: ServerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """upstream_session itself (as serve() and the capture script build it), with DNS answering loopback: the
    request must fail as `network` and never arrive, and the answer must have come from PublicOnlyResolver's inner
    resolver (aiohttp's own default resolver, which a connector without `resolver=` uses, is made to fail)."""
    arrived: list[str] = []
    asked: list[str] = []

    async def counted(request: web.Request) -> web.Response:
        arrived.append(request.path)
        return web.Response(text="reached")

    class Recording(FakeResolver):
        async def resolve(
            self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
        ) -> list[aiohttp.abc.ResolveResult]:
            asked.append(host)
            return await super().resolve(host, port, family)

    class Unreachable(aiohttp.abc.AbstractResolver):
        """Stands in for aiohttp's DefaultResolver, so a connector that bypasses PublicOnlyResolver never sends a
        real DNS query from the test."""

        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def resolve(
            self,
            host: str,
            port: int = 0,  # noqa: ARG002
            family: socket.AddressFamily = socket.AF_INET,  # noqa: ARG002
        ) -> list[aiohttp.abc.ResolveResult]:
            raise AssertionError(f"{host} was resolved without PublicOnlyResolver")

        async def close(self) -> None:
            pass

    app = web.Application()
    app.router.add_get("/", counted)
    server = await aiohttp_server(app)
    monkeypatch.setattr(aiohttp, "ThreadedResolver", lambda: Recording("127.0.0.1"))  # PublicOnlyResolver's inner
    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", Unreachable)  # what TCPConnector() alone would use
    async with upstream_session(FetcherConfig()) as session:
        transport = AiohttpTransport(
            session, frozenset({"api.yodelpass.test"}), max_bytes=1024, _scheme="http", _port=port_of(server)
        )
        with pytest.raises(FetchError) as caught:
            await transport.get(f"http://api.yodelpass.test:{port_of(server)}/", headers={})
    assert caught.value.kind == "network"
    assert arrived == []
    assert asked == ["api.yodelpass.test"]  # upstream_session asked PublicOnlyResolver, not aiohttp's default


# ---- the real request path, through the test seam


async def reached(_: web.Request) -> web.Response:
    return web.Response(text="reached")


async def redirect(_: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "/echo"})


async def limited(request: web.Request) -> web.Response:
    return web.Response(status=429, headers={"Retry-After": request.query["after"]}, text="slow down")


async def typed(_: web.Request) -> web.Response:
    return web.Response(body=b"{}", headers={"Content-Type": "Application/JSON; charset=UTF-8"})


async def chunks(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Type": "application/json"})
    await response.prepare(request)
    for part in (b'{"a": "' + b"x" * 300, b"y" * 300, b'z"}'):
        await response.write(part)
        await asyncio.sleep(0.05)
    await response.write_eof()
    return response


async def stream(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse()  # chunked: no Content-Length
    await response.prepare(request)
    for _ in range(10):
        await response.write(b"x" * 500)
    await response.write_eof()
    return response


async def declared_big(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Length": "100000"})
    await response.prepare(request)
    await response.write(b"x" * 10)  # never finishes: the transport must not wait for it
    return response


async def echo(request: web.Request) -> web.Response:
    seen = {
        "method": request.method,
        "body": (await request.read()).decode(),
        "content_type": request.headers.get("Content-Type"),
        "agent": request.headers.get("User-Agent"),
        "cookie": request.headers.get("Cookie"),
        "accept_encoding": request.headers.get("Accept-Encoding"),
    }
    return web.json_response(seen, headers={"Set-Cookie": "session=abc; Path=/"})


async def slow(request: web.Request) -> web.Response:
    await asyncio.sleep(5)
    return web.Response(text="late")


def deflate(data: bytes, wbits: int) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, wbits)
    return compressor.compress(data) + compressor.flush()


@functools.cache
def gzip_of_zeros(mib: int) -> bytes:
    """A small gzip body that inflates to `mib` MiB."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    block = bytes(1 << 20)
    return b"".join([*(compressor.compress(block) for _ in range(mib)), compressor.flush()])


JSON_BODY = json.dumps({"a": "x" * 600}).encode()
ENCODED: dict[str, tuple[str, bytes]] = {
    "gzip": ("gzip", gzip.compress(JSON_BODY)),
    "x-gzip": ("x-gzip", gzip.compress(JSON_BODY)),
    "deflate": ("deflate", deflate(JSON_BODY, 15)),  # zlib-wrapped, as RFC 9110 means it
    "raw-deflate": ("deflate", deflate(JSON_BODY, -15)),  # what some servers send instead
    "upper": ("GZIP", gzip.compress(JSON_BODY)),
    "identity": ("identity", JSON_BODY),
    "over": ("gzip", gzip.compress(b"x" * 1025)),  # small on the wire, over the 1024-byte cap once inflated
    "corrupt": ("gzip", b"\x1f\x8b\x08\x00" + b"\xff" * 40),
    "truncated": ("gzip", gzip.compress(JSON_BODY)[:-12]),
    "br": ("br", JSON_BODY),  # never asked for
    "compress": ("compress", JSON_BODY),
    "two": ("gzip, gzip", gzip.compress(gzip.compress(JSON_BODY))),
    "bomb": ("gzip", gzip_of_zeros(250)),
}


async def encoded(request: web.Request) -> web.StreamResponse:
    encoding, body = ENCODED[request.query["case"]]
    response = web.StreamResponse(headers={"Content-Type": "application/json", "Content-Encoding": encoding})
    response.content_length = len(body)
    await response.prepare(request)
    for start in range(0, len(body), 16 * 1024):  # several reads, as a real network gives them
        await response.write(body[start : start + 16 * 1024])
    await response.write_eof()
    return response


def routes() -> web.Application:
    app = web.Application()
    app.router.add_get("/chunks", chunks)
    app.router.add_get("/stream", stream)
    app.router.add_get("/declared-big", declared_big)
    app.router.add_route("*", "/echo", echo)
    app.router.add_get("/slow", slow)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/limited", limited)
    app.router.add_get("/typed", typed)
    app.router.add_get("/encoded", encoded)
    return app


Local = tuple[AiohttpTransport, str, aiohttp.ClientSession]


@pytest.fixture
async def local_session(aiohttp_server: ServerFactory) -> AsyncIterator[Local]:
    server = await aiohttp_server(routes())
    # The production session: 127.0.0.1 is an IP literal, so aiohttp never asks its resolver about it.
    async with upstream_session(FetcherConfig(upstream_timeout_seconds=1)) as session:
        transport = AiohttpTransport(
            session, frozenset({"127.0.0.1"}), max_bytes=1024, _scheme="http", _port=port_of(server)
        )
        yield transport, f"http://127.0.0.1:{port_of(server)}", session


@pytest.fixture
def local(local_session: Local) -> tuple[AiohttpTransport, str]:
    transport, base, _session = local_session
    return transport, base


async def test_a_body_sent_in_three_delayed_chunks_is_read_whole(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    response = await transport.get(f"{base}/chunks", headers={})
    assert response.status == 200
    assert response.body == b'{"a": "' + b"x" * 300 + b"y" * 300 + b'z"}'
    assert response.content_type == "application/json"


async def test_a_redirect_is_never_followed(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    response = await transport.get(f"{base}/redirect", headers={})
    assert response.status == 302  # arrives as itself; /echo was never asked
    assert response.body == b""


async def test_over_the_cap_by_streaming_is_too_large(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    with pytest.raises(FetchError) as caught:
        await transport.get(f"{base}/stream", headers={})
    assert (caught.value.kind, caught.value.http_status) == ("too_large", 200)


async def test_over_the_cap_by_content_length_is_too_large_before_reading(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    with pytest.raises(FetchError) as caught:
        await asyncio.wait_for(transport.get(f"{base}/declared-big", headers={}), 2)
    assert caught.value.kind == "too_large"


async def test_post_json_sends_the_payload_and_the_callers_headers(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    headers = {"user-agent": "Mozilla/5.0 test", "content-type": "application/json"}
    response = await transport.post_json(f"{base}/echo", {"placeId": "10672", "x": None}, headers=headers)
    seen = response_json(response.body)
    assert seen["method"] == "POST"
    assert seen["body"] == '{"placeId":"10672","x":null}'
    assert seen["content_type"] == "application/json"
    assert seen["agent"] == "Mozilla/5.0 test"
    response = await transport.get(f"{base}/echo", headers={})
    assert response_json(response.body)["body"] == ""
    assert seen["accept_encoding"] == ACCEPT_ENCODING == "gzip, deflate"  # only what _read_inflated can inflate
    response = await transport.get(f"{base}/echo", headers={"accept-encoding": "identity"})
    assert response_json(response.body)["accept_encoding"] == "identity"  # a caller's own choice is kept


def response_json(body: bytes) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(body)
    return data


async def test_no_cookie_is_stored(local_session: Local) -> None:
    transport, base, session = local_session
    first = await transport.get(f"{base}/echo", headers={})  # the server sets a cookie
    assert response_json(first.body)["cookie"] is None
    second = await transport.get(f"{base}/echo", headers={})
    assert response_json(second.body)["cookie"] is None  # and it never comes back
    # aiohttp's ordinary jar would drop that cookie too, from an IP-address host; a hostname's it would keep.
    assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
    session.cookie_jar.update_cookies({"s": "x"}, yarl.URL("https://api.yodelpass.com/"))
    assert len(session.cookie_jar) == 0
    real = aiohttp.CookieJar()
    real.update_cookies({"s": "x"}, yarl.URL("https://api.yodelpass.com/"))
    assert len(real) == 1  # the check bites


@pytest.mark.parametrize("case", ["gzip", "x-gzip", "deflate", "raw-deflate", "upper", "identity"])
async def test_a_compressed_body_is_inflated(local: tuple[AiohttpTransport, str], case: str) -> None:
    transport, base = local
    response = await transport.get(f"{base}/encoded?case={case}", headers={})
    assert (response.status, response.body) == (200, JSON_BODY)


@pytest.mark.parametrize(
    ("case", "kind"),
    [
        ("over", "too_large"),
        ("corrupt", "site_changed"),
        ("truncated", "site_changed"),
        ("br", "site_changed"),
        ("compress", "site_changed"),
        ("two", "site_changed"),
    ],
)
async def test_a_compressed_body_that_cant_be_read_within_the_cap_is_refused(
    local: tuple[AiohttpTransport, str], case: str, kind: str
) -> None:
    transport, base = local
    with pytest.raises(FetchError) as caught:
        await transport.get(f"{base}/encoded?case={case}", headers={})
    assert (caught.value.kind, caught.value.http_status) == (kind, 200)


async def test_a_gzip_bomb_is_too_large_without_being_inflated(local_session: Local) -> None:
    _transport, base, session = local_session
    bomb = gzip_of_zeros(250)
    assert len(bomb) < 262_144 < 250 << 20  # under the production cap on the wire, far over it inflated
    transport = AiohttpTransport(
        session, frozenset({"127.0.0.1"}), max_bytes=262_144, _scheme="http", _port=yarl.URL(base).port or 0
    )
    tracemalloc.start()
    try:
        with pytest.raises(FetchError) as caught:
            await transport.get(f"{base}/encoded?case=bomb", headers={})
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (caught.value.kind, caught.value.http_status) == ("too_large", 200)
    assert peak < 8 << 20, f"peak {peak} bytes"  # aiohttp inflating it itself peaks at about 250 MiB


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120),
        (" 30 ", 30),
        ("0", 0),
        ("99999", 3600),
        ("99999999999", 3600),  # delay-seconds has no length limit
        ("9" * 400, 3600),
        ("00030", 30),
        ("0000000000000000000000000000000000000060", 60),
        ("3600", 3600),
        ("3601", 3600),
        ("Wed, 21 Oct 2027 07:28:00 GMT", None),
        ("-1", None),
        ("1.5", None),
    ],
)
async def test_retry_after_is_parsed_and_clamped(
    local: tuple[AiohttpTransport, str], header: str, expected: int | None
) -> None:
    transport, base = local
    response = await transport.get(f"{base}/limited?after={header.strip()}", headers={})
    assert response.status == 429
    assert response.retry_after_s == expected
    assert retry_after(header) == expected
    assert retry_after(None) is None
    assert retry_after("١٢٠") is None  # digits, but not ASCII ones


async def test_the_content_type_is_lowercased_without_parameters(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    assert (await transport.get(f"{base}/typed", headers={})).content_type == "application/json"


async def test_a_slow_server_is_a_timeout(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    with pytest.raises(FetchError) as caught:
        await transport.get(f"{base}/slow", headers={})
    assert caught.value.kind == "timeout"


async def test_a_refused_connection_is_a_network_error() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound, never listening: connections are refused
        async with upstream_session(FetcherConfig()) as session:
            transport = AiohttpTransport(session, frozenset({"127.0.0.1"}), max_bytes=1024, _scheme="http", _port=port)
            with pytest.raises(FetchError) as caught:
                await transport.get(f"http://127.0.0.1:{port}/", headers={})
    assert caught.value.kind == "network"
