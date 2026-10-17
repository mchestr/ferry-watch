"""waitlist-fetcher's transport (design.md §4.4): the refusals before any I/O, the public-only resolver, and the real
request path (no redirects, the multi-chunk read, the byte cap, Retry-After, no cookies) against a local server
through the `_scheme`/`_port` test seam."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import aiohttp
import aiohttp.abc
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from waitlist.fetcher.app import upstream_session
from waitlist.fetcher.config import FetcherConfig
from waitlist.fetcher.transport import AiohttpTransport, PublicOnlyResolver, retry_after
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
    }
    return web.json_response(seen, headers={"Set-Cookie": "session=abc; Path=/"})


async def slow(request: web.Request) -> web.Response:
    await asyncio.sleep(5)
    return web.Response(text="late")


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
    return app


@pytest.fixture
async def local(aiohttp_server: ServerFactory) -> AsyncIterator[tuple[AiohttpTransport, str]]:
    server = await aiohttp_server(routes())
    # The production session: 127.0.0.1 is an IP literal, so aiohttp never asks its resolver about it.
    async with upstream_session(FetcherConfig(upstream_timeout_seconds=1)) as session:
        transport = AiohttpTransport(
            session, frozenset({"127.0.0.1"}), max_bytes=1024, _scheme="http", _port=port_of(server)
        )
        yield transport, f"http://127.0.0.1:{port_of(server)}"


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


def response_json(body: bytes) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(body)
    return data


async def test_no_cookie_is_stored(local: tuple[AiohttpTransport, str]) -> None:
    transport, base = local
    first = await transport.get(f"{base}/echo", headers={})  # the server sets a cookie
    assert response_json(first.body)["cookie"] is None
    second = await transport.get(f"{base}/echo", headers={})
    assert response_json(second.body)["cookie"] is None  # and it never comes back


@pytest.mark.parametrize(
    ("header", "expected"),
    [("120", 120), (" 30 ", 30), ("0", 0), ("99999", 3600), ("Wed, 21 Oct 2027 07:28:00 GMT", None), ("-1", None)],
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
