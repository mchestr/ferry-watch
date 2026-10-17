"""vnc-edge end to end: KasmVNC's stock client, through the shim, into a real VNC gate.

A local server stands in for nginx (the same routes and security headers, with the gate mounted where
nginx proxies the socket), and a minimal RFB server stands in for Xvnc. If a KasmVNC upgrade changes
how the client builds its socket URL or picks subprotocols, these fail (docs/multi-tenant/design-reference.md
§7.9).

The client's files come from the KasmVNC package: set WL_KASMVNC_WWW to its extracted ``www`` directory
(CI does; see .github/workflows/ci.yml).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import importlib.util
import json
import os
import shutil
import struct
import sys
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from playwright.async_api import Browser, Page, Route, async_playwright

from waitlist.agent.gate import Gate, GateSettings
from waitlist.tickets import Display, mint

pytestmark = pytest.mark.browser

EDGE = Path(__file__).resolve().parents[2] / "vnc-edge"
APP = "https://waitlist.chestr.dev"
KEY = Ed25519PrivateKey.generate()
OWNER = "a" * 20
TRIP = "3f9c0a1b2c3d4e5f"
GEN = 4
POD = f"wl-{TRIP}-g{GEN}"
CART = "cart-1"
LANDING = f"/t/{TRIP}/g/{GEN}/"
RECORD = f"wl:{TRIP}:{GEN}"
STREAM_VIEWER = f"/wl/viewer.html#t={TRIP}&g={GEN}"
# vnc.html's policy as 0.6.4 served it with KasmVNC 1.5.0's client (but for the checkout host's new name): the
# streamed viewer changes nothing the classic viewer gets (docs/multi-tenant/streamed-viewer/DESIGN.md §9).
CLASSIC_CSP = (
    "default-src 'self'; script-src 'self' 'sha256-+w4uuxDuaRJ0kH5I4sifAwedyfxg1il+lsKFqslG+L0=' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self' wss://waitlist-checkout.chestr.dev data:; worker-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


@functools.cache
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


def viewer_policy(host: str) -> str:
    """The streamed viewer's policy, served on a test server: its socket is ws://, not wss://."""
    policy: str = _load_build().viewer_csp(host)
    secure = f"connect-src wss://{host}"
    assert policy.count(secure) == 1
    return policy.replace(secure, f"connect-src ws://{host}")


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Site:
    www = os.environ.get("WL_KASMVNC_WWW")
    if not www:
        pytest.skip("WL_KASMVNC_WWW isn't set to KasmVNC's www directory")
    built = _load_build().build(
        Path(www), tmp_path_factory.mktemp("vnc-edge"), checkout_host="waitlist-checkout.chestr.dev", app_origin=APP
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


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def edge_app(site: Site, gate: Gate | Handler, offers: list[list[str]]) -> web.Application:
    """vnc-edge's nginx config, in aiohttp, with the pod's gate (or a stand-in's upgrade handler) behind the
    checkout's socket path."""

    @web.middleware
    async def headers(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if request.path.endswith("/ws"):
            offers.append([p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")])
            return await handler(request)
        response = await handler(request)
        viewer = request.path == "/wl/viewer.html"
        response.headers.update(
            {
                "Content-Security-Policy": viewer_policy(request.host) if viewer else site.csp,
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
    app.router.add_get(r"/t/{trip:[a-f0-9]{16}}/g/{gen:[0-9]{1,6}}/", file(site.www / "wl" / "landing.html"))
    app.router.add_get("/vnc.html", file(site.www / "vnc.html"))
    app.router.add_get("/gone.html", file(site.www / "gone.html"))
    for directory in ("assets", "app", "core", "wl"):
        app.router.add_static(f"/{directory}", site.www / directory)
    if isinstance(gate, Gate):
        (upgrade,) = gate.app().router.routes()  # GET /ws, which nginx proxies this path to
        gate = upgrade.handler
    app.router.add_get(f"/t/{TRIP}/g/{GEN}/ws", gate, allow_head=False)
    return app


@pytest.fixture
async def edge(site: Site, tmp_path: Path) -> AsyncIterator[Edge]:
    async with serve_edge(site, tmp_path) as served:
        yield served


@contextlib.asynccontextmanager
async def serve_edge(site: Site, tmp_path: Path) -> AsyncIterator[Edge]:
    """vnc-edge serving `site`, with the gate in front of a stand-in Xvnc."""
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
    assert await page.text_content("h1") == "Opening your checkout…"
    assert await page.is_visible("#open")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    (first,) = edge.offers
    nonce = next(p for p in first if p.startswith("wl.n1."))
    assert sorted(first) == sorted(["binary", nonce, f"wl.t1.{issued}"])
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


async def test_an_expired_ticket_goes_back_to_waitlist_to_open_it_again(edge: Edge, page: Page) -> None:
    async def dashboard(route: Route) -> None:
        await route.fulfill(body="Waitlist", content_type="text/plain")

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


def forged(trip: str = TRIP, **disp: Any) -> str:
    """A link anyone can make: a well-formed ticket with claims of their own and no valid signature."""
    claims = {"trip": trip, "exp": int(time.time()) + 600} | (
        {"disp": {"route": "Payment failed: call +1 800 555 0100", "fare": "or your booking is cancelled", **disp}}
    )
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"wl1.k1.{payload}.AAAA"


async def page_text(page: Page) -> str:
    return str(await page.evaluate("document.body.innerText"))


async def test_a_made_up_link_shows_none_of_its_words_and_keeps_no_deadline_of_its_own(edge: Edge, page: Page) -> None:
    """Found in review: anyone could make a link that showed their own words on the checkout host (the ticket's
    signature isn't checked there) and left a device record that never went."""
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={forged(hard_deadline=4102444800, date='2026-10-16')}")
    text = await page_text(page)
    assert "Payment failed" not in text
    assert "cancelled" not in text
    assert "Oct 16" not in text
    assert "Checkout closes" not in text
    record = await device_record(page)
    assert "disp" not in record
    deadline = record["hard_deadline"]
    assert isinstance(deadline, int)
    assert time.time() < deadline <= time.time() + 90 * 60 + 1  # as if the ticket had said nothing
    await page.goto("about:blank")


async def test_a_made_up_link_neither_ends_nor_keeps_forever_a_checkout_on_this_device(edge: Edge, page: Page) -> None:
    """The review's stronger case: with a checkout's trip and gen, a made-up link brought the device record's deadline
    forward, so the viewer gave up and the next opening dropped the device's nonce (and with it the held cart)."""
    await page.goto(f"{edge.url}{LANDING}#{ticket(hold_s=1800)}")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    record = await device_record(page)
    for made_up in (forged(hard_deadline=1), forged(hard_deadline=4102444800), forged(hard_deadline="soon")):
        await page.goto("about:blank")
        await page.goto(f"{edge.url}{LANDING}#{made_up}")
        await page.wait_for_selector("html.noVNC_connected")  # it opens this device's own checkout, by its nonce
        again = await device_record(page)
        assert (again["n"], again["opened"], again["hard_deadline"]) == (record["n"], True, record["hard_deadline"])
    # A record that 0.6.4's page kept from such a link, with a deadline no hold could have, goes.
    stale = {"n": "QW5vdGhlckRldmljZXNOb25jZQ", "opened": False, "hard_deadline": 4102444800}
    await page.evaluate("(r) => localStorage.setItem('wl:0123456789abcdef:1', JSON.stringify(r))", stale)
    await page.goto("about:blank")
    await page.goto(f"{edge.url}{LANDING}")
    assert await page.evaluate("localStorage.getItem('wl:0123456789abcdef:1')") is None
    assert (await device_record(page))["n"] == record["n"]


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
    stock = (Path(os.environ["WL_KASMVNC_WWW"]) / "vnc.html").read_text()
    assert html.replace('<script src="/wl/shim.js"></script>', "", 1) == stock


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
    assert sorted(edge.offers[-1]) == sorted(["binary", f"wl.n1.{record['n']}", f"wl.t1.{issued}"])


@pytest.mark.parametrize(
    ("page_from", "script_from"), [("0.6.4", "0.7.0"), ("0.7.0", "0.6.4")], ids=["old-page", "old-script"]
)
async def test_a_classic_ticket_opens_vnc_html_while_vnc_edge_rolls(
    site: Site, tmp_path: Path, page: Page, page_from: str, script_from: str
) -> None:
    """vnc-edge rolls one replica at a time (and back, on a rollback): the landing page and its script can come from
    different versions. Each must stand on its own, and a classic link works with either pairing."""
    mixed = tmp_path / "www"
    shutil.copytree(site.www, mixed)
    old = Path(__file__).resolve().parents[1] / "fixtures" / "vnc-edge-0.6.4"
    if page_from == "0.6.4":
        html = (old / "landing.html").read_text().replace("__WL_APP_ORIGIN__", APP)
        (mixed / "wl" / "landing.html").write_text(html)
        (mixed / "wl" / "session.js").unlink()  # that replica has none
    else:
        (mixed / "wl" / "landing.js").write_text((old / "landing.js").read_text())
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    async with serve_edge(Site(mixed, site.csp), tmp_path) as edge:
        issued = ticket()
        await page.goto(f"{edge.url}{LANDING}#{issued}")
        await page.click("#open")
        await page.wait_for_selector("html.noVNC_connected")
        assert page.url == f"{edge.url}{classic_url()}"
        assert sorted(edge.offers[-1]) == sorted(
            ["binary", f"wl.n1.{(await device_record(page))['n']}", f"wl.t1.{issued}"]
        )
    assert errors == []


async def test_a_stream_ticket_opens_the_streamed_viewer(edge: Edge, page: Page) -> None:
    issued = ticket()
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={issued}")
    assert page.url == f"{edge.url}{LANDING}"  # the ticket is out of the address bar and history
    assert await page.text_content("#beta") == "Opens in the new checkout viewer (beta)."
    await page.click("#open")
    await page.wait_for_url(f"{edge.url}{STREAM_VIEWER}")
    record = await device_record(page)
    assert (record["viewer"], record["viewer_fallback"]) == ("stream", False)
    # The streamed viewer offers its own subprotocol, with the ticket (this gate refuses it: 0.6.4's takes binary).
    await _until_offered(edge, 1)
    assert edge.offers[0] == ["wl.cast1", f"wl.n1.{record['n']}", f"wl.t1.{issued}"]


async def test_once_the_streamed_viewer_gives_way_the_checkout_stays_classic_on_this_device(
    edge: Edge, page: Page
) -> None:
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={ticket()}")
    # What "Use the classic viewer" does on the streamed viewer.
    await page.evaluate("([trip, gen]) => WlSession.useClassic(trip, gen)", [TRIP, str(GEN)])
    nonce = (await device_record(page))["n"]

    # Waitlist still hands this device stream tickets for the checkout: they open the classic viewer.
    await page.goto("about:blank")  # a new page, not a new fragment on this one
    await page.goto(f"{edge.url}{LANDING}#v=s1&t={ticket()}")
    assert not await page.is_visible("#beta")
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    assert page.url == f"{edge.url}{classic_url()}"
    record = await device_record(page)
    assert (record["n"], record["viewer"], record["viewer_fallback"]) == (nonce, "stream", True)


async def _until_offered(edge: Edge, count: int) -> None:
    for _ in range(100):
        if len(edge.offers) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{len(edge.offers)} sockets opened, expected {count}")
