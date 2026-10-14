"""The HTTP API and WebSocket, against a real app with a fake site and a local route-info server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ferry_watch import server as server_module
from ferry_watch.bcferries.terminals import TerminalDirectory
from ferry_watch.models import Fare
from ferry_watch.notify import AlertDispatcher, Message, NotifyError, TextAlerts
from ferry_watch.server import Services, build_app
from ferry_watch.settings import Settings
from ferry_watch.state import Cart, CartStatus, RuntimeState
from ferry_watch.store import ConfigStore
from ferry_watch.watcher import Watcher
from tests.fakes import FakeSite

JSON = {"Content-Type": "application/json"}
NEW_TRIP = {"from": "SWB", "to": "TSA", "date": "2026-10-12", "adults": 2}


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.fail = False

    @property
    def configured(self) -> bool:
        return True

    async def send(self, message: Message) -> None:
        if self.fail:
            raise NotifyError("Pushover returned 400: invalid token")
        self.sent.append(message)


@pytest.fixture
async def route_info(fixtures: Path) -> AsyncIterator[TestServer]:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(json.loads((fixtures / "route-info.json").read_text()))

    app = web.Application()
    app.router.add_get("/route-info", handler)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
async def services(tmp_path: Path, route_info: TestServer, notifier: FakeNotifier) -> AsyncIterator[Services]:
    async with aiohttp.ClientSession() as http:
        terminals = TerminalDirectory(http, "ua", url=str(route_info.make_url("/route-info")))
        await terminals.get()
        store = ConfigStore(tmp_path / "config.json")
        state = RuntimeState()
        alerts = AlertDispatcher(notifier, lambda _m, _e: None)
        watcher = Watcher(
            store,
            state,
            FakeSite(),
            TextAlerts(alerts, terminals, "https://vnc"),
            terminals,
            memory=None,
            credentials=None,
        )
        yield Services(Settings(checkout_url="https://vnc"), store, state, terminals, alerts, watcher)


@pytest.fixture
async def client(services: Services, aiohttp_client: Callable[[web.Application], Awaitable[TestClient]]) -> TestClient:
    return await aiohttp_client(build_app(services))


async def add_trip(client: TestClient, **overrides: Any) -> dict[str, Any]:
    resp = await client.post("/api/trips", json={**NEW_TRIP, **overrides})
    assert resp.status == 200, await resp.text()
    state: dict[str, Any] = await resp.json()
    return state["trips"][-1]  # type: ignore[no-any-return]


async def test_state_shape(client: TestClient) -> None:
    trip = await add_trip(client)
    state = await (await client.get("/api/state")).json()
    assert set(state) == {
        "boot_id",
        "seq",
        "server_time",
        "enabled",
        "interval_seconds",
        "next_check_at",
        "max_trips",
        "trip_defaults",
        "trips",
        "events",
        "checkout_url",
        "login_configured",
        "alerts_configured",
        "watcher",
    }
    assert set(trip) >= {
        "id",
        "title",
        "route",
        "past",
        "sailings",
        "checked_at",
        "checking",
        "error",
        "error_at",
        "retry_at",
        "session",
        "cart",
        "booking_url",
        "from",
        "to",
        "date",
        "auto_cart",
    }
    assert trip["title"] == "Swartz Bay → Tsawwassen · Mon Oct 12"
    assert trip["route"] == {
        "from_place": "Swartz Bay",
        "from_area": "Victoria",
        "to_place": "Tsawwassen",
        "to_area": "Vancouver",
    }
    assert trip["booking_url"].startswith("https://www.bcferries.com/?")
    assert trip["past"] is False
    assert state["trip_defaults"]["date"] == "2026-10-12"  # tomorrow
    assert state["trip_defaults"]["auto_cart"] is True  # a guest cart, no login needed
    assert state["events"][0]["trip_title"] == "Swartz Bay → Tsawwassen · Mon Oct 12"
    assert state["watcher"]["healthy"] is True


async def test_seq_increases_with_changes(client: TestClient) -> None:
    first = await (await client.get("/api/state")).json()
    await add_trip(client)
    second = await (await client.get("/api/state")).json()
    assert second["boot_id"] == first["boot_id"]
    assert second["seq"] > first["seq"]


async def test_terminals(client: TestClient) -> None:
    body = await (await client.get("/api/terminals")).json()
    swb = next(t for t in body["terminals"] if t["code"] == "SWB")
    assert swb == {
        "code": "SWB",
        "label": "Sidney - Victoria (Swartz Bay)",
        "place": "Swartz Bay",
        "area": "Victoria",
        "dests": ["TSA"],
    }


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"to": "HSB"}, "to"),
        ({"from": "QQQ"}, "from"),
        ({"date": "2026-10-10"}, "date"),
        ({"earliest": ""}, "earliest"),
        ({"adults": 0}, "adults"),
    ],
)
async def test_trip_validation(client: TestClient, body: dict[str, Any], field: str) -> None:
    resp = await client.post("/api/trips", json={**NEW_TRIP, **body})
    assert resp.status == 400
    assert (await resp.json())["field"] == field


async def test_patch_changes_only_what_was_sent(client: TestClient, services: Services) -> None:
    trip = await add_trip(client)
    resp = await client.patch(f"/api/trips/{trip['id']}", json={"latest": "17:00"})
    assert resp.status == 200
    updated = (await resp.json())["trips"][0]
    assert updated["latest"] == "17:00"
    assert updated["adults"] == 2


async def test_changing_the_search_resets_results(client: TestClient, services: Services) -> None:
    trip = await add_trip(client)
    services.state.trip(trip["id"]).alerted = {clock_time(9)}
    await client.patch(f"/api/trips/{trip['id']}", json={"latest": "20:00"})
    assert services.state.trip(trip["id"]).alerted == {clock_time(9)}  # window change: same search
    await client.patch(f"/api/trips/{trip['id']}", json={"date": "2026-10-13"})
    assert services.state.trip(trip["id"]).alerted == set()


async def test_delete(client: TestClient) -> None:
    trip = await add_trip(client)
    resp = await client.delete(f"/api/trips/{trip['id']}", json={})
    assert resp.status == 200
    assert (await resp.json())["trips"] == []
    assert (await client.delete(f"/api/trips/{trip['id']}", json={})).status == 404


async def test_settings(client: TestClient) -> None:
    resp = await client.patch("/api/settings", json={"enabled": False, "interval_seconds": 60})
    state = await resp.json()
    assert (state["enabled"], state["interval_seconds"]) == (False, 60)
    assert state["events"][0]["text"] == "Watching paused"
    assert (await client.patch("/api/settings", json={"interval_seconds": 1})).status == 400


async def test_cart_actions_need_the_right_cart(client: TestClient, services: Services) -> None:
    trip = await add_trip(client)
    url = f"/api/trips/{trip['id']}/cart"
    assert (await client.post(f"{url}/release", json={"cart_id": "x"})).status == 409

    runtime = services.state.trip(trip["id"])
    runtime.cart = Cart(CartStatus.READY, clock_time(9), fare=Fare("Saver", "$93.45", selectable=True))
    assert (await client.post(f"{url}/release", json={"cart_id": "wrong"})).status == 409
    assert (await client.post(f"{url}/release", json={"cart_id": runtime.cart.id})).status == 200
    assert runtime.release_requested

    runtime.cart = Cart(CartStatus.FAILED, clock_time(9), error="nope")
    assert (await client.post(f"{url}/dismiss", json={"cart_id": runtime.cart.id})).status == 200
    assert runtime.cart is None


async def test_a_held_cart_shows_when_its_released(client: TestClient, services: Services) -> None:
    trip = await add_trip(client)
    services.state.trip(trip["id"]).cart = Cart(
        CartStatus.READY, clock_time(9), fare=Fare("Saver", "$93.45", selectable=True), expires_at=2_000_000_000.0
    )
    state = await (await client.get("/api/state")).json()
    assert state["trips"][0]["cart"]["expires_at"] == 2_000_000_000.0


async def test_cross_site_requests_are_refused(client: TestClient) -> None:
    resp = await client.post("/api/check", json={}, headers={"Origin": "https://evil.example"})
    assert resp.status == 403
    resp = await client.post("/api/check", data="{}", headers={"Content-Type": "text/plain"})
    assert resp.status == 415
    resp = await client.post("/api/check", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403
    resp = await client.post("/api/check", json={}, headers={"Origin": f"http://{client.host}:{client.port}"})
    assert resp.status == 200


async def test_bad_bodies(client: TestClient) -> None:
    assert (await client.post("/api/trips", data="{nope", headers=JSON)).status == 400
    assert (await client.post("/api/trips", data="[1]", headers=JSON)).status == 400
    nan = '{"from":"SWB","to":"TSA","date":"2026-10-12","height_ft":NaN}'
    assert (await client.post("/api/trips", data=nan, headers=JSON)).status == 400


async def test_test_alert(client: TestClient, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await client.post("/api/alerts/test", json={})).status == 200
    assert [m.title for m in notifier.sent] == ["⛴ Ferry Watch test"]
    assert (await client.post("/api/alerts/test", json={})).status == 429
    monkeypatch.setattr(server_module, "TEST_ALERT_COOLDOWN_SECONDS", 0)
    notifier.fail = True
    resp = await client.post("/api/alerts/test", json={})
    assert resp.status == 502
    assert "invalid token" in (await resp.json())["error"]


async def test_healthz(client: TestClient, services: Services) -> None:
    assert (await client.get("/healthz")).status == 200
    services.state.heartbeat_at = time.time() - 3600
    assert (await client.get("/healthz")).status == 503


async def test_websocket_pushes_state(client: TestClient, services: Services) -> None:
    hub_task = asyncio.create_task(services.hub.run())
    try:
        async with client.ws_connect("/api/ws") as ws:
            first = await ws.receive_json(timeout=5)
            assert first["type"] == "state"
            assert first["data"]["trips"] == []
            await add_trip(client)
            update = await ws.receive_json(timeout=5)
            assert update["type"] == "state"
            assert len(update["data"]["trips"]) == 1
            assert update["data"]["seq"] > first["data"]["seq"]
    finally:
        hub_task.cancel()


async def test_websocket_refuses_other_origins(client: TestClient) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
        await client.ws_connect("/api/ws", headers={"Origin": "https://evil.example"})
    assert exc.value.status == 403


async def test_websocket_pings_when_idle(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "PING_SECONDS", 0.2)
    async with client.ws_connect("/api/ws") as ws:
        assert (await ws.receive_json(timeout=5))["type"] == "state"
        assert (await ws.receive_json(timeout=5))["type"] == "ping"


async def test_clients_cant_choose_trip_ids(client: TestClient) -> None:
    trip = await add_trip(client, id="mine")
    assert trip["id"] != "mine"


async def test_frontend_is_served_with_a_strict_csp(
    tmp_path: Path, services: Services, aiohttp_client: Callable[[web.Application], Awaitable[TestClient]]
) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    script = "document.documentElement.classList.add('dark')"
    (static / "index.html").write_text(
        f"<!doctype html><html><head><script>{script}</script></head><body></body></html>"
    )
    (static / "assets" / "app-1234.js").write_text("console.log(1)")
    (static / "favicon.svg").write_text("<svg/>")
    (static / "manifest.webmanifest").write_text("{}")
    (static / "icon-192.png").write_bytes(b"\x89PNG")
    client = await aiohttp_client(build_app(services, static_dir=static))

    index = await client.get("/")
    assert index.status == 200
    csp = index.headers["Content-Security-Policy"]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp
    assert "frame-ancestors 'none'" in csp
    assert index.headers["Cache-Control"] == "no-store"
    assert index.headers["X-Content-Type-Options"] == "nosniff"

    asset = await client.get("/assets/app-1234.js")
    assert asset.status == 200
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert (await client.get("/favicon.svg")).status == 200
    assert (await client.get("/manifest.webmanifest")).content_type == "application/manifest+json"
    assert (await client.get("/icon-192.png")).content_type == "image/png"
    assert (await client.get("/assets/../index.html")).status in (403, 404)
    assert (await client.get("/nope")).status == 404
