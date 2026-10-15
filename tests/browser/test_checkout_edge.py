"""vnc-edge end to end: KasmVNC's stock client, through the shim, into a real VNC gate.

A local server stands in for nginx (the same routes and security headers, with the gate mounted where
nginx proxies the socket), and a minimal RFB server stands in for Xvnc. If a KasmVNC upgrade changes
how the client builds its socket URL or picks subprotocols, these fail (docs/multi-tenant/design-reference.md
§7.9).

The client's files come from the KasmVNC package: set FW_KASMVNC_WWW to its extracted ``www`` directory
(CI does; see .github/workflows/ci.yml).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import struct
import sys
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from playwright.async_api import Browser, Page, Route, async_playwright

from ferry_watch.agent.gate import Gate, GateSettings
from ferry_watch.tickets import Display, mint

pytestmark = pytest.mark.browser

EDGE = Path(__file__).resolve().parents[2] / "vnc-edge"
APP = "https://ferry.chestr.dev"
KEY = Ed25519PrivateKey.generate()
OWNER = "a" * 20
TRIP = "3f9c0a1b2c3d4e5f"
GEN = 4
POD = f"fw-{TRIP}-g{GEN}"
CART = "cart-1"
LANDING = f"/t/{TRIP}/g/{GEN}/"
RECORD = f"fw:{TRIP}:{GEN}"
STREAM_VIEWER = f"/fw/viewer.html#t={TRIP}&g={GEN}"
# vnc.html's policy exactly as 0.6.4 served it with KasmVNC 1.5.0's client: the streamed viewer changes nothing
# the classic viewer gets (docs/multi-tenant/streamed-viewer/DESIGN.md §9).
CLASSIC_CSP = (
    "default-src 'self'; script-src 'self' 'sha256-+w4uuxDuaRJ0kH5I4sifAwedyfxg1il+lsKFqslG+L0=' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://ferry-checkout.chestr.dev "
    "data:; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _load_build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vnc_edge_build", EDGE / "build.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Site:
    www: Path
    csp: str


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Site:
    www = os.environ.get("FW_KASMVNC_WWW")
    if not www:
        pytest.skip("FW_KASMVNC_WWW isn't set to KasmVNC's www directory")
    built = _load_build().build(
        Path(www), tmp_path_factory.mktemp("vnc-edge"), checkout_host="ferry-checkout.chestr.dev", app_origin=APP
    )
    return Site(built.www, built.csp)


@dataclass
class FakeXvnc:
    """Just enough RFB 3.8 for the client to connect: no authentication, a 640x480 screen."""

    path: Path
    connections: int = 0
    writers: list[asyncio.StreamWriter] = field(default_factory=list)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            writer.write(b"RFB 003.008\n")
            await reader.readexactly(12)
            writer.write(bytes([1, 1]))  # one security type: None
            await reader.readexactly(1)
            writer.write(struct.pack(">I", 0))  # security result: OK
            await reader.readexactly(1)  # ClientInit
            pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            name = b"checkout"
            writer.write(struct.pack(">HH", 640, 480) + pixel_format + struct.pack(">I", len(name)) + name)
            while await reader.read(4096):
                pass  # the client's requests go unanswered; a still screen is enough
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@dataclass
class Edge:
    url: str
    gate: Gate
    offers: list[list[str]]  # the subprotocols of each socket the client opened
    viewers: list[bool]
    xvnc: FakeXvnc


def edge_app(site: Site, gate: Gate, offers: list[list[str]]) -> web.Application:
    """vnc-edge's nginx config, in aiohttp."""

    @web.middleware
    async def headers(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if request.path.endswith("/ws"):
            offers.append([p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")])
            return await handler(request)
        response = await handler(request)
        response.headers.update(
            {
                "Content-Security-Policy": site.csp,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            }
        )
        return response

    def file(path: Path) -> Callable[[web.Request], Awaitable[web.FileResponse]]:
        async def serve(_: web.Request) -> web.FileResponse:
            return web.FileResponse(path)

        return serve

    app = web.Application(middlewares=[headers])
    app.router.add_get(r"/t/{trip:[a-f0-9]{16}}/g/{gen:[0-9]{1,6}}/", file(site.www / "fw" / "landing.html"))
    app.router.add_get("/vnc.html", file(site.www / "vnc.html"))
    app.router.add_get("/gone.html", file(site.www / "gone.html"))
    for directory in ("assets", "app", "core", "fw"):
        app.router.add_static(f"/{directory}", site.www / directory)
    (upgrade,) = gate.app().router.routes()  # GET /ws, which nginx proxies this path to
    app.router.add_get(f"/t/{TRIP}/g/{GEN}/ws", upgrade.handler, allow_head=False)
    return app


@pytest.fixture
async def edge(site: Site, tmp_path: Path) -> AsyncIterator[Edge]:
    xvnc = FakeXvnc(tmp_path / "rfb.sock")
    rfb = await asyncio.start_unix_server(xvnc.serve, path=xvnc.path)
    port = unused_port()
    origin = f"http://127.0.0.1:{port}"
    offers: list[list[str]] = []
    viewers: list[bool] = []
    gate = Gate(
        GateSettings(
            origin=origin,
            owner=OWNER,
            trip_id=TRIP,
            pod_name=POD,
            keys={"k1": KEY.public_key()},
            rfb_socket=xvnc.path,
        ),
        checkout_cart=lambda: CART,
        viewer_changed=viewers.append,
    )
    server = TestServer(edge_app(site, gate, offers), host="127.0.0.1", port=port)
    await server.start_server()
    try:
        yield Edge(origin, gate, offers, viewers, xvnc)
    finally:
        await gate.close("test over")
        await server.close()
        for writer in xvnc.writers:
            writer.close()
        rfb.close()
        await rfb.wait_closed()


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
        yield browser
        await browser.close()


@pytest.fixture
async def page(browser: Browser) -> AsyncIterator[Page]:
    context = await browser.new_context()
    page = await context.new_page()
    yield page
    await context.close()


def ticket(*, issued: float | None = None, hold_s: int = 3600) -> str:
    now = time.time() if issued is None else issued
    display = Display(
        route="Swartz Bay → Tsawwassen",
        date="2026-10-16",
        depart="09:00",
        fare="Saver",
        session_s=900,
        hard_deadline=int(now) + hold_s,
    )
    return mint(KEY, "k1", sub=OWNER, trip=TRIP, pod=POD, cart_id=CART, disp=display, now=now)


def csp_violations(page: Page) -> list[str]:
    violations: list[str] = []
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    return violations


async def test_the_client_opens_the_checkout_with_the_ticket_then_comes_back_with_the_nonce(
    edge: Edge, page: Page
) -> None:
    violations = csp_violations(page)
    issued = ticket()
    await page.goto(f"{edge.url}{LANDING}#{issued}")
    assert page.url == f"{edge.url}{LANDING}"  # the ticket is out of the address bar and history
    assert await page.text_content("#sailing") == "Swartz Bay → Tsawwassen · Fri, Oct 16 · 9:00 am · Saver"
    assert "(paused while you're on the payment page)" in (await page.text_content("#deadline") or "")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    (first,) = edge.offers
    nonce = next(p for p in first if p.startswith("fw.n1."))
    assert sorted(first) == sorted(["binary", nonce, f"fw.t1.{issued}"])
    assert edge.viewers == [True]

    # A reload is a reconnect: the same nonce, and the ticket is gone.
    await page.reload()
    await page.wait_for_selector("html.noVNC_connected")  # the gate let it in before the client saw it open
    assert edge.viewers[-1]
    assert sorted(edge.offers[1]) == ["binary", nonce]
    assert edge.xvnc.connections == 2
    assert violations == []


@pytest.mark.parametrize("touch", [False, True], ids=["mouse", "touch"])
async def test_the_viewer_is_the_devices_own_size_and_a_touch_screen_gets_a_keyboard_button(
    edge: Edge, browser: Browser, touch: bool
) -> None:
    context = await browser.new_context(has_touch=touch)
    try:
        page = await context.new_page()
        await page.goto(f"{edge.url}{LANDING}#{ticket()}")
        await page.click("#open")
        await page.wait_for_selector("html.noVNC_connected")
        settings = urllib.parse.urlsplit(page.url).fragment.split("&")
        assert settings == [
            f"path=t/{TRIP}/g/{GEN}/ws",
            "autoconnect=1",
            "reconnect=1",
            "reconnect_delay=1000",
            "idle_disconnect=120",
            "anti_aliasing=1",  # no auto-scaling on a high-DPI phone
            *(["virtual_keyboard_visible=1"] if touch else []),
        ]
        assert await page.is_visible("#noVNC_keyboard_control") == touch
        # The side panel is closed as the client connects, and its handle opens it again.
        assert not await page.is_visible("#noVNC_control_bar.noVNC_open")
        await page.click("#noVNC_control_bar_handle")
        await page.wait_for_selector("#noVNC_control_bar.noVNC_open")
    finally:
        await context.close()


async def test_an_expired_ticket_goes_back_to_ferry_watch_to_open_it_again(edge: Edge, page: Page) -> None:
    async def dashboard(route: Route) -> None:
        await route.fulfill(body="Ferry Watch", content_type="text/plain")

    await page.route(f"{APP}/**", dashboard)
    await page.goto(f"{edge.url}{LANDING}#{ticket(issued=time.time() - 120)}")
    await page.wait_for_url(f"{APP}/c/{TRIP}")
    assert edge.offers == []


async def test_without_a_ticket_or_a_key_there_is_only_the_way_back(edge: Edge, page: Page) -> None:
    await page.goto(f"{edge.url}/vnc.html#path=t/{TRIP}/g/{GEN}/ws&autoconnect=1")
    await page.wait_for_url(f"{edge.url}{LANDING}")  # the shim had nothing to open it with
    assert await page.text_content("h1") == "No checkout here"
    assert not await page.is_visible("#details")
    assert await page.get_attribute("#back", "href") == f"{APP}/"
    assert edge.offers == []


async def test_after_the_hold_ends_the_page_stops_reconnecting(edge: Edge, page: Page) -> None:
    await page.goto(f"{edge.url}{LANDING}#{ticket(hold_s=2)}")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    await asyncio.sleep(2)
    await edge.gate.close("the hold ended")
    await page.wait_for_url(f"{edge.url}/gone.html")
    assert await page.text_content("h1") == "No checkout is open for this trip"
    assert len(edge.offers) == 1


def classic_url(*, touch: bool = False) -> str:
    """0.6.4's vnc.html fragment."""
    settings = [
        f"path=t/{TRIP}/g/{GEN}/ws",
        "autoconnect=1",
        "reconnect=1",
        "reconnect_delay=1000",
        "idle_disconnect=120",
        "anti_aliasing=1",
        *(["virtual_keyboard_visible=1"] if touch else []),
    ]
    return f"/vnc.html#{'&'.join(settings)}"


async def device_record(page: Page) -> dict[str, object]:
    record: dict[str, object] = await page.evaluate("(key) => JSON.parse(localStorage.getItem(key))", RECORD)
    return record


async def test_vnc_html_and_its_policy_are_as_0_6_4_served_them(edge: Edge, site: Site) -> None:
    assert site.csp == CLASSIC_CSP
    async with aiohttp.ClientSession() as http, http.get(f"{edge.url}/vnc.html") as response:
        assert response.headers["Content-Security-Policy"] == CLASSIC_CSP
        html = await response.text()
    stock = (Path(os.environ["FW_KASMVNC_WWW"]) / "vnc.html").read_text()
    assert html.replace('<script src="/fw/shim.js"></script>', "", 1) == stock


@pytest.mark.parametrize("form", ["{t}", "t={t}", "v=s9&t={t}"], ids=["0.6.4", "no-viewer", "unknown-viewer"])
async def test_a_classic_ticket_opens_vnc_html_as_0_6_4_did(edge: Edge, page: Page, form: str) -> None:
    issued = ticket()
    await page.goto(f"{edge.url}{LANDING}#{form.format(t=issued)}")
    assert page.url == f"{edge.url}{LANDING}"
    assert not await page.is_visible("#beta")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    assert page.url == f"{edge.url}{classic_url()}"
    record = await device_record(page)
    assert (record["viewer"], record["viewer_fallback"]) == ("classic", False)
    assert sorted(edge.offers[-1]) == sorted(["binary", f"fw.n1.{record['n']}", f"fw.t1.{issued}"])


async def test_a_stream_ticket_opens_the_streamed_viewer(edge: Edge, page: Page) -> None:
    issued = ticket()
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={issued}")
    assert page.url == f"{edge.url}{LANDING}"  # the ticket is out of the address bar and history
    assert await page.text_content("#sailing") == "Swartz Bay → Tsawwassen · Fri, Oct 16 · 9:00 am · Saver"
    assert await page.text_content("#beta") == "Opens in the new checkout viewer (beta)."
    record = await device_record(page)
    assert (record["viewer"], record["viewer_fallback"], record["opened"]) == ("stream", False, False)
    pending = await page.evaluate("(key) => JSON.parse(sessionStorage.getItem(key)).ticket", f"{RECORD}:ticket")
    assert pending == issued
    async with page.expect_request(f"{edge.url}/fw/viewer.html") as opening:
        await page.click("#open")
    assert (await opening.value).is_navigation_request()


async def test_once_the_streamed_viewer_gives_way_the_checkout_stays_classic_on_this_device(
    edge: Edge, page: Page
) -> None:
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={ticket()}")
    # What "Use the classic viewer" does on the streamed viewer.
    await page.evaluate("([trip, gen]) => FwSession.useClassic(trip, gen)", [TRIP, str(GEN)])
    nonce = (await device_record(page))["n"]

    # Ferry Watch still hands this device stream tickets for the checkout: they open the classic viewer.
    await page.goto("about:blank")  # a new page, not a new fragment on this one
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={ticket()}")
    assert not await page.is_visible("#beta")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    assert page.url == f"{edge.url}{classic_url()}"
    record = await device_record(page)
    assert (record["n"], record["viewer"], record["viewer_fallback"]) == (nonce, "stream", True)
