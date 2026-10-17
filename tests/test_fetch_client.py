"""waitlist-control's client for waitlist-fetcher, against a fake fetcher (docs/waitlist/design.md §4.5): every way a
fetch ends, and whose trouble each is."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.fakes import FakeHttpProvider, FakeInventory, FakeQuery
from waitlist.control.fetch import CheckFailed, FetchClient
from waitlist.fetchwire import MAX_REPLY_BYTES, FetchFailed, FetchOk, decode_request, encode

PROVIDER = FakeHttpProvider()
QUERY = FakeQuery(date="2027-07-01", part="a")
Handler = Callable[[web.Request, str], Awaitable[web.StreamResponse]]


@dataclass
class FakeFetcher:
    """Answers /v1/fetch with whatever `answer` makes of the request (its id)."""

    answer: Handler
    seen: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        decoded = decode_request(body)
        self.seen.append({"content_type": request.content_type, "body": json.loads(body)})
        return await self.answer(request, decoded.id)


async def ok(_: web.Request, request_id: str) -> web.StreamResponse:
    inventory = {"date": "2027-07-01", "part": "a", "status": "open"}
    return web.Response(body=encode(FetchOk(id=request_id, inventory=inventory, elapsed_ms=12)))


@dataclass
class Rig:
    fetcher: FakeFetcher
    client: FetchClient


@pytest.fixture
async def rig(aiohttp_server: Callable[..., Awaitable[TestServer]]) -> AsyncIterator[Rig]:
    fetcher = FakeFetcher(ok)
    app = web.Application()
    app.router.add_post("/v1/fetch", fetcher.handle)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as http:
        yield Rig(fetcher, FetchClient(http, str(server.make_url("/")), timeout_s=0.5))


async def failure(rig: Rig) -> CheckFailed:
    with pytest.raises(CheckFailed) as caught:
        await rig.client.fetch(PROVIDER, QUERY)
    return caught.value


async def test_an_inventory_comes_back_as_the_providers_type(rig: Rig) -> None:
    inventory = await rig.client.fetch(PROVIDER, QUERY)
    assert inventory == FakeInventory(date="2027-07-01", part="a", status="open")
    (seen,) = rig.fetcher.seen
    assert seen["content_type"] == "application/json"
    assert seen["body"]["provider"] == "buntzen"
    assert seen["body"]["query"] == {"date": "2027-07-01", "part": "a"}
    assert set(seen["body"]) == {"v", "id", "provider", "query"}  # nothing about who is watching


async def test_a_refused_connection_is_the_fetcher_unavailable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens there
    async with aiohttp.ClientSession() as http:
        client = FetchClient(http, f"http://127.0.0.1:{port}")
        with pytest.raises(CheckFailed) as caught:
            await client.fetch(PROVIDER, QUERY)
    assert (caught.value.kind, caught.value.replied) == ("fetcher_unavailable", False)


async def test_a_slow_fetcher_is_unavailable(rig: Rig) -> None:
    async def slow(request: web.Request, request_id: str) -> web.StreamResponse:
        await asyncio.sleep(2)
        return await ok(request, request_id)

    rig.fetcher.answer = slow
    e = await failure(rig)
    assert (e.kind, e.replied) == ("fetcher_unavailable", False)


def answering(status: int = 200, body: bytes | None = None, *, wrong_id: bool = False) -> Handler:
    async def handler(_: web.Request, request_id: str) -> web.StreamResponse:
        reply = FetchOk(id="0" * 16 if wrong_id else request_id, inventory={}, elapsed_ms=1)
        return web.Response(status=status, body=body if body is not None else encode(reply))

    return handler


@pytest.mark.parametrize(
    "handler",
    [
        answering(500),
        answering(400, b""),
        answering(body=b"not json"),
        answering(body=b'{"result":"ok","v":1,"id":"0123456789abcdef","inventory":{},"elapsed_ms":1,"extra":1}'),
        answering(body=b"x" * (MAX_REPLY_BYTES + 1)),
        answering(wrong_id=True),
    ],
    ids=["status-500", "status-400", "not-json", "extra-field", "too-big", "id-mismatch"],
)
async def test_an_answer_that_isnt_a_reply_is_a_bad_reply(rig: Rig, handler: Handler) -> None:
    rig.fetcher.answer = handler
    e = await failure(rig)
    assert (e.kind, e.replied) == ("bad_reply", False)


async def test_an_inventory_that_isnt_the_providers_is_a_bad_reply(rig: Rig) -> None:
    async def wrong(_: web.Request, request_id: str) -> web.StreamResponse:
        inventory = {"date": "2027-07-01", "part": "c", "status": "open"}  # no part c
        return web.Response(body=encode(FetchOk(id=request_id, inventory=inventory, elapsed_ms=1)))

    rig.fetcher.answer = wrong
    assert (await failure(rig)).kind == "bad_reply"


async def test_a_streamed_oversized_reply_is_a_bad_reply(rig: Rig) -> None:
    async def streamed(request: web.Request, _: str) -> web.StreamResponse:
        response = web.StreamResponse()
        response.enable_chunked_encoding()
        await response.prepare(request)
        for _chunk in range(5):
            await response.write(b"x" * (MAX_REPLY_BYTES // 4))
        await response.write_eof()
        return response

    rig.fetcher.answer = streamed
    assert (await failure(rig)).kind == "bad_reply"


async def test_a_reply_in_several_chunks_is_read_whole(rig: Rig) -> None:
    async def chunked(request: web.Request, request_id: str) -> web.StreamResponse:
        inventory = {"date": "2027-07-01", "part": "a", "status": "maybe"}
        body = encode(FetchOk(id=request_id, inventory=inventory, elapsed_ms=1))
        response = web.StreamResponse()
        response.enable_chunked_encoding()
        await response.prepare(request)
        for i in range(0, len(body), 7):
            await response.write(body[i : i + 7])
            await asyncio.sleep(0.01)
        await response.write_eof()
        return response

    rig.fetcher.answer = chunked
    assert await rig.client.fetch(PROVIDER, QUERY) == FakeInventory(date="2027-07-01", part="a", status="maybe")


def failed(**kw: Any) -> Handler:
    async def handler(_: web.Request, request_id: str) -> web.StreamResponse:
        return web.Response(body=encode(FetchFailed(id=request_id, **kw)))

    return handler


@pytest.mark.parametrize(
    ("reply", "kind", "retry_after_s"),
    [
        ({"error": "site_changed", "http_status": 404}, "site_changed", None),
        ({"error": "rate_limited", "http_status": 429, "retry_after_s": 120}, "rate_limited", 120),  # the site's
        ({"error": "blocked", "http_status": 403}, "blocked", None),
        ({"error": "upstream_error", "http_status": 503, "retry_after_s": 30}, "upstream_error", 30),
        ({"error": "timeout"}, "timeout", None),
        ({"error": "network"}, "network", None),
        ({"error": "too_large", "http_status": 200}, "too_large", None),
        ({"error": "unexpected"}, "unexpected", None),
    ],
)
async def test_a_failed_fetch_passes_through(rig: Rig, reply: dict[str, Any], kind: str, retry_after_s: int) -> None:
    rig.fetcher.answer = failed(**reply)
    e = await failure(rig)
    assert (e.kind, e.retry_after_s, e.replied) == (kind, retry_after_s, True)


async def test_the_fetchers_own_cap_is_ours_not_the_sites(rig: Rig) -> None:
    rig.fetcher.answer = failed(error="rate_limited")  # the fetcher's refusals never carry a status
    e = await failure(rig)
    assert (e.kind, e.replied) == ("fetcher_unavailable", True)
