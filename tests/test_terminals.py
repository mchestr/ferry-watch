from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ferry_watch.bcferries.errors import BookingError
from ferry_watch.bcferries.terminals import TerminalDirectory, Terminals


def test_terminal_names(terminals: Terminals) -> None:
    swb = terminals["SWB"]
    assert (swb.name, swb.city) == ("Victoria (Swartz Bay)", "Sidney")
    assert swb.label == "Sidney - Victoria (Swartz Bay)"
    assert (swb.place, swb.area) == ("Swartz Bay", "Victoria")
    assert swb.destinations == ("TSA",)


def test_routes(terminals: Terminals) -> None:
    assert terminals.has_route("SWB", "TSA")
    assert not terminals.has_route("SWB", "HSB")
    assert not terminals.has_route("XXX", "TSA")
    assert terminals.route_name("SWB", "TSA") == "Swartz Bay → Tsawwassen"
    assert terminals.route_name("SWB", "XXX") == "Swartz Bay → XXX"


def test_unexpected_route_info_is_an_error() -> None:
    with pytest.raises(BookingError):
        Terminals.from_route_info([{"code": "SWB"}])


async def route_info_server(fixtures: Path, status: int = 200) -> TestServer:
    async def handler(_request: web.Request) -> web.Response:
        if status != 200:
            return web.Response(status=status)
        return web.json_response(json.loads((fixtures / "route-info.json").read_text()))

    app = web.Application()
    app.router.add_get("/route-info", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_directory_fetches_and_caches(fixtures: Path, tmp_path: Path) -> None:
    server = await route_info_server(fixtures)
    cache = tmp_path / "terminals.json"
    async with aiohttp.ClientSession() as http:
        directory = TerminalDirectory(http, "ua", url=str(server.make_url("/route-info")), cache_path=cache)
        before = directory.cached
        assert before is None
        terminals = await directory.get()
        assert "SWB" in terminals
        assert directory.cached is terminals
    await server.close()
    assert "SWB" in {t["code"] for t in json.loads(cache.read_text())}


async def test_directory_falls_back_to_the_disk_cache(fixtures: Path, tmp_path: Path) -> None:
    cache = tmp_path / "terminals.json"
    cache.write_text((fixtures / "route-info.json").read_text())
    server = await route_info_server(fixtures, status=503)
    async with aiohttp.ClientSession() as http:
        directory = TerminalDirectory(http, "ua", url=str(server.make_url("/route-info")), cache_path=cache)
        assert "TSA" in await directory.get()
    await server.close()


async def test_directory_without_cache_raises(fixtures: Path) -> None:
    server = await route_info_server(fixtures, status=503)
    async with aiohttp.ClientSession() as http:
        directory = TerminalDirectory(http, "ua", url=str(server.make_url("/route-info")))
        with pytest.raises(BookingError):
            await directory.get()
    await server.close()
