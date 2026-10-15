"""The streamed checkout viewer on the pod's real screen (DESIGN §10.2).

The pod's own processes (KasmVNC's Xvnc with the pod's flags, and matchbox), Chromium launched as the agent
launches it, the agent's screen (xrandr and xsettingsd), its dialog keeper, the VNC gate with the stream session
behind it, and a viewer: a Python client speaking fw.cast1 as PROTOCOL.md §8 says a viewer must. The checkout is
the test site in tests/fixtures/checkout-site, served on two loopback addresses so that its card iframe is
cross-site: its own renderer process and CDP target, as a payment provider's is.

Needs Xvnc (KasmVNC's), matchbox-window-manager, xrandr and xsettingsd on PATH (CI installs them, as the browser
image does), and FW_KASMVNC_WWW for the test that hands the checkout to the classic viewer.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port
from playwright.async_api import BrowserContext, Page

from ferry_watch.agent import cast_fields
from ferry_watch.agent.cast_frames import jpeg_size
from ferry_watch.agent.cast_protocol import Clock, CloseCode, ImageHeader
from ferry_watch.agent.dialogs import DialogKeeper
from ferry_watch.agent.gate import Gate, GateSettings, ViewerKind
from ferry_watch.agent.processes import pod_processes
from ferry_watch.agent.screen import Screen
from ferry_watch.agent.stream import CastPod, factory
from ferry_watch.bcferries.site import PodBrowser
from tests.browser import test_checkout_edge as edge
from tests.browser import test_checkout_screen as screen
from tests.castclient import WAIT_S, Viewer

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120)]

SITE = Path(__file__).resolve().parents[1] / "fixtures" / "checkout-site"
SCREEN = (1280, 1000)  # the pod's (AgentSettings.screen)
PHONE = {"t": "hello", "v": 1, "w": 390, "h": 664, "dpr": 3}  # an iPhone 13's viewport
DESKTOP = {"t": "hello", "v": 1, "w": 1280, "h": 800, "dpr": 2}
NONCE = "c3RyZWFtLXZpZXdlci1ub25jZS0x"


# ---- the test site


@dataclass(frozen=True)
class Sites:
    shop: str
    pay: str


def _site_app(directory: Path, origins: dict[str, str]) -> web.Application:
    async def serve(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = directory / name
        if "/" in name or not path.is_file():
            raise web.HTTPNotFound
        text = path.read_text("utf-8")
        for placeholder, origin in origins.items():
            text = text.replace(placeholder, origin)
        return web.Response(text=text, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{name}", serve)
    return app


@pytest.fixture
async def sites() -> AsyncIterator[Sites]:
    """The shop on 127.0.0.1 and the payment provider on 127.0.0.2: two sites, so the card frame is out of
    process."""
    shop_port, pay_port = unused_port(), unused_port()
    origins = {"{{SHOP}}": f"http://127.0.0.1:{shop_port}", "{{PAY}}": f"http://127.0.0.2:{pay_port}"}
    servers = [
        TestServer(_site_app(SITE / "shop", origins), host="127.0.0.1", port=shop_port),
        TestServer(_site_app(SITE / "pay", origins), host="127.0.0.2", port=pay_port),
    ]
    for server in servers:
        await server.start_server()
    try:
        yield Sites(origins["{{SHOP}}"], origins["{{PAY}}"])
    finally:
        for server in servers:
            await server.close()


# ---- the pod


@dataclass
class Pod:
    display: str
    env: dict[str, str]
    work: Path
    rfb_socket: Path
    context: BrowserContext
    tab: Page  # the checkout's own page

    async def at(self, url: str) -> None:
        await self.tab.goto(url)
        await self.tab.wait_for_load_state("load")


@pytest.fixture
async def pod() -> AsyncIterator[Pod]:
    """A browser pod's screen, as agent/main.py's run_pod starts it (test_checkout_screen.py's, with the tools a
    stream needs)."""
    for tool in ("Xvnc", "matchbox-window-manager", "xrandr", "xsettingsd"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} isn't on PATH")
    work = Path(tempfile.mkdtemp(prefix="fw-"))
    (work / "home").mkdir()
    display = screen._free_display()
    env = {"DISPLAY": display, "HOME": str(work / "home"), "TMPDIR": str(work), "TZ": "America/Vancouver"}
    rfb_socket = work / "kasm" / "rfb.sock"
    processes = pod_processes(display, SCREEN, rfb_socket, {**env, "PATH": os.environ["PATH"]})
    launched: list[BrowserContext] = []

    async def keep(context: BrowserContext) -> None:
        launched.append(context)

    chromium = PodBrowser(
        None,  # type: ignore[arg-type]  # no terminals: it never searches
        profile_dir=work / "profile",
        window_size=SCREEN,
        env=env,
        after_launch=keep,
    )
    try:
        await processes.start()
        await chromium.start()
        session = await chromium.new_session("checkout")
        yield Pod(display, {**env, "PATH": os.environ["PATH"]}, work, rfb_socket, launched[0], session.page)
    finally:
        await chromium.stop()
        await processes.stop()
        shutil.rmtree(work, ignore_errors=True)


# ---- the gate, the stream behind it, and a viewer


@dataclass
class Rig:
    pod: Pod
    sites: Sites
    gate: Gate
    screen: Screen
    keeper: DialogKeeper
    origin: str
    http: aiohttp.ClientSession
    cast_allowed: list[bool]
    cart: list[str | None]
    path: str = "/ws"  # where the gate's upgrade is
    viewers: list[Viewer] = field(default_factory=list)

    async def connect(
        self, hello: dict[str, Any] | None = PHONE, *, nonce: str = NONCE, ticket: str | None = None
    ) -> Viewer:
        """A stream viewer, first connect (with a ticket) or a reconnect (by the device's nonce), that has sent
        `hello` and got `ready`."""
        if ticket is None and not self.viewers:
            ticket = edge.ticket()
        protocols = ["fw.cast1", f"fw.n1.{nonce}", *([f"fw.t1.{ticket}"] if ticket else [])]
        ws = await self.http.ws_connect(f"{self.origin}{self.path}", protocols=protocols, origin=self.origin)
        viewer = Viewer(ws)
        viewer.start()
        self.viewers.append(viewer)
        if hello is not None:
            await viewer.send(hello)
            await viewer.message("ready")
        return viewer

    async def tab(self, script: str, arg: Any = None) -> Any:
        return await self.pod.tab.evaluate(script, arg)


@contextlib.asynccontextmanager
async def make_rig(
    pod: Pod, sites: Sites, app: Callable[[Gate], web.Application] | None = None, path: str = "/ws"
) -> AsyncIterator[Rig]:
    """The gate in front of the pod, as agent/main.py builds it (the stream session, the screen and the dialog
    keeper), served by `app` (the gate's own, or vnc-edge's with the gate where nginx proxies)."""
    screen_ = Screen(pod.display, SCREEN, pod.work / "xsettings", pod.env)
    keeper = DialogKeeper(lambda: pod.context)
    cast_allowed: list[bool] = [True]
    cart: list[str | None] = [edge.CART]
    hard = time.time() + 3600

    def verdict(cart_id: str, kind: ViewerKind) -> CloseCode | None:
        if kind == "cast" and not cast_allowed[0]:
            return CloseCode.WITHDRAWN
        return None if cart[0] == cart_id else CloseCode.ENDED

    port = unused_port()
    origin = f"http://127.0.0.1:{port}"
    gate = Gate(
        GateSettings(
            origin=origin,
            owner=edge.OWNER,
            trip_id=edge.TRIP,
            pod_name=edge.POD,
            keys={"k1": edge.KEY.public_key()},
            rfb_socket=pod.rfb_socket,
        ),
        checkout_cart=lambda: cart[0],
        viewer_changed=lambda _: None,
        cast=factory(
            CastPod(
                target=lambda: (pod.context, pod.tab),
                clock=lambda: Clock(ends=hard - 600, hard=hard, paused=False),
                screen=screen_,
                dialogs=keeper,
            )
        ),
        verdict=verdict,
        screen=screen_,
        dialogs=keeper,
    )
    server = TestServer(app(gate) if app is not None else gate.app(), host="127.0.0.1", port=port)
    await server.start_server()
    http = aiohttp.ClientSession()
    the_rig = Rig(pod, sites, gate, screen_, keeper, origin, http, cast_allowed, cart, path)
    try:
        yield the_rig
    finally:
        for viewer in the_rig.viewers:
            await viewer.ws.close()
        await http.close()
        await gate.close("test over")
        await server.close()
        await screen_.close()


@pytest.fixture
async def rig(pod: Pod, sites: Sites) -> AsyncIterator[Rig]:
    async with make_rig(pod, sites) as the_rig:
        yield the_rig


async def _until(read: Callable[[], Awaitable[Any]], done: Callable[[Any], bool], seconds: float = WAIT_S) -> Any:
    """Read until `done` says yes, for up to `seconds`. Returns the last reading either way."""
    deadline = time.monotonic() + seconds
    while True:
        value = await read()
        if done(value) or time.monotonic() > deadline:
            return value
        await asyncio.sleep(0.05)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    size = jpeg_size(data)
    assert size is not None, "not a JPEG"
    return size


# ---- what tests wait for (functions, so that each binds its own values)


def width_is(width: int) -> Callable[[Any], bool]:
    return lambda sizes: bool(sizes) and sizes[-1][0] == width


def ends_with(last: list[int]) -> Callable[[Any], bool]:
    return lambda sizes: bool(sizes) and sizes[-1] == last


def since(found: Callable[[], list[Any]], count: int) -> Callable[[], list[Any]]:
    """What `found` has beyond its first `count` items."""
    return lambda: found()[count:]


def covers(number: int) -> Callable[[dict[str, Any]], bool]:
    """A map or focus report taken after the viewer's message `number`."""
    return lambda message: message["n"] >= number


def on_page(page_id: int) -> Callable[[ImageHeader], bool]:
    return lambda header: header.page == page_id


def sized(w: int, h: int) -> Callable[[ImageHeader], bool]:
    return lambda header: (header.w, header.h) == (w, h)


def image_of(
    viewer: Viewer, wanted: Callable[[ImageHeader], bool], *, latest: bool = False
) -> Callable[[], ImageHeader | None]:
    """The first image the viewer got (or its latest, if that is it) that `wanted` likes."""
    if latest:
        return lambda: viewer.latest if viewer.images and wanted(viewer.latest) else None
    return lambda: next((h for h, _ in viewer.images if wanted(h)), None)


# ---- 1. admission, then images


async def test_images_start_with_a_screenshot_at_the_phones_size_and_twice_its_pixels(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    header, jpeg = await viewer.image()
    ready = viewer.messages[0]
    assert ready == {"t": "ready", "v": 1, "w": 390, "h": 664, "dsf": 2, "features": []}
    assert [m["t"] for m in viewer.messages[:3]] == ["ready", "pages", "clock"]
    assert (header.kind, header.seq, header.page, header.dsf, header.first) == (1, 1, 1, 2, True)
    assert (header.w, header.h) == (390, 664)
    assert _jpeg_size(jpeg) == (780, 1328)
    assert await rig.tab("[innerWidth, innerHeight, devicePixelRatio]") == [390, 664, 2]
    card_targets = [t for t in await _targets(rig.pod) if t["type"] == "iframe"]
    assert card_targets, "the card iframe isn't its own CDP target: the test site isn't cross-site"

    await viewer.close()
    desktop = await rig.connect(DESKTOP)
    assert desktop.messages[0]["dsf"] == 1
    header, jpeg = await desktop.image()
    assert (header.w, header.h, header.dsf) == (1280, 800, 1)
    assert _jpeg_size(jpeg) == (1280, 800)
    assert await rig.tab("[innerWidth, innerHeight, devicePixelRatio]") == [1280, 800, 1]


async def _targets(pod: Pod) -> list[dict[str, Any]]:
    session = await pod.context.new_cdp_session(pod.tab)
    try:
        found: list[dict[str, Any]] = (await session.send("Target.getTargets"))["targetInfos"]
        return found
    finally:
        await session.detach()


# ---- helpers: what the pod's page shows, in the image's frame px

# An element's border box in frame px of the image: (client - visual viewport offset) * scale (PROTOCOL §1). In
# an iframe, its frame's owner's content box comes first.
FRAME_RECT = """([selector, frameSelector]) => {
  const v = visualViewport;
  let x = 0, y = 0;
  if (frameSelector) {
    const owner = document.querySelector(frameSelector), box = owner.getBoundingClientRect();
    const s = getComputedStyle(owner);
    x = box.x + owner.clientLeft + parseFloat(s.paddingLeft);
    y = box.y + owner.clientTop + parseFloat(s.paddingTop);
  }
  return [x, y];
}"""

ELEMENT_RECT = """(selector) => {
  const r = document.querySelector(selector).getBoundingClientRect();
  return [r.x, r.y, r.width, r.height];
}"""


async def frame_rect(pod: Pod, selector: str, frame: str | None = None) -> list[float]:
    """Where an element is in the image, frame px (`frame`: the selector of the iframe it's in)."""
    origin = await pod.tab.evaluate(FRAME_RECT, [selector, frame])
    target = pod.tab if frame is None else await (await pod.tab.query_selector(frame)).content_frame()  # type: ignore[union-attr]
    assert target is not None
    x, y, w, h = await target.evaluate(ELEMENT_RECT, selector)
    ox, oy, s = await pod.tab.evaluate("[visualViewport.offsetLeft, visualViewport.offsetTop, visualViewport.scale]")
    return [(x + origin[0] - ox) * s, (y + origin[1] - oy) * s, w * s, h * s]


def middle(rect: list[float]) -> tuple[float, float]:
    x, y, w, h = rect
    return x + w / 2, y + h / 2


async def current(viewer: Viewer, pod: Pod) -> ImageHeader:
    """The latest image, once it shows the page where it is now (scroll and zoom)."""

    async def matching() -> ImageHeader | None:
        live = await pod.tab.evaluate("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]")
        header = viewer.latest if viewer.images else None
        if header is None:
            return None
        same = abs(header.sx - live[0]) <= 1 and abs(header.sy - live[1]) <= 1 and abs(header.scale - live[2]) < 0.01
        return header if same else None

    header: ImageHeader | None = await _until(matching, bool)
    if header is None:
        live = await pod.tab.evaluate("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]")
        shown = [(h.seq, h.kind, h.sx, h.sy, h.scale) for h, _ in viewer.images[-3:]]
        raise AssertionError(f"no image shows the page as it is ({live}): the latest are {shown}")
    return header


async def tap(viewer: Viewer, pod: Pod, point: tuple[float, float], ms: int = 40) -> int:
    header = await current(viewer, pod)
    return await viewer.send(
        {"t": "tap", "page": header.page, "seq": header.seq, "x": point[0], "y": point[1], "ms": ms}
    )


async def focused(pod: Pod, frame: str | None = None) -> str:
    if frame is None:
        return str(await pod.tab.evaluate("document.activeElement.id"))
    content = await (await pod.tab.query_selector(frame)).content_frame()  # type: ignore[union-attr]
    assert content is not None
    return str(await content.evaluate("document.activeElement.id"))


async def card(pod: Pod) -> Any:
    content = await (await pod.tab.query_selector("#card")).content_frame()  # type: ignore[union-attr]
    assert content is not None
    return content


def monotone(widths: list[int]) -> bool:
    return widths == sorted(widths) or widths == sorted(widths, reverse=True)


# ---- 2-3. the screen across connections


async def test_a_reconnect_from_the_same_device_changes_nothing_the_page_can_see(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/swap.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await rig.pod.tab.fill("#name", "Mike Chester")  # what the user typed before the bank app took over
    await rig.tab("window.__resizes = 0; addEventListener('resize', () => window.__resizes++)")
    renders = len(await rig.tab("window.__renders"))
    await viewer.close()

    again = await rig.connect(PHONE)
    started = time.monotonic()
    header, _ = await again.image()
    assert time.monotonic() - started < 1.0, "a reconnect to a static page got no image within 1 s"
    assert header.first
    assert await rig.tab("window.__resizes") == 0
    assert len(await rig.tab("window.__renders")) == renders, "the page swapped its components"
    assert await rig.pod.tab.input_value("#name") == "Mike Chester"


async def test_rotation_is_one_resize_and_widths_move_one_way(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    for w, h in ((664, 390), (390, 664)):  # to landscape and back: the same scale factor, one resize each
        await rig.tab("window.__sizes = []")
        await viewer.send({"t": "resize", "w": w, "h": h, "dpr": 3})
        await _until(lambda: rig.tab("window.__sizes"), width_is(w))
        await asyncio.sleep(0.5)
        assert await rig.tab("window.__sizes") == [[w, h, 2]], "a rotation isn't one resize"
        header = await viewer.until(image_of(viewer, sized(w, h)))
        assert header.first
        assert header.dsf == 2

    # Phone to desktop and back (DESIGN §4.1: DPI first, then the mode, so the width moves one way).
    for hello, last in ((DESKTOP, [1280, 800, 1]), (PHONE, [390, 664, 2])):
        old = await rig.tab("innerWidth")
        await viewer.close()
        await rig.tab("window.__sizes = []")
        viewer = await rig.connect(hello)
        await viewer.image()
        widths = await _until(lambda: rig.tab("window.__sizes"), ends_with(last))
        assert widths[-1] == last
        moved = [old] + [w for w, _, _ in widths]
        moved = [w for i, w in enumerate(moved) if i == 0 or w != moved[i - 1]]
        assert monotone(moved), f"the page's width went back and forth: {widths}"
        assert len(moved) <= 3, f"more than one width on the way: {widths}"  # the old, one on the way, the new


# ---- 4-7. input


async def test_taps_focus_fields_in_the_page_and_the_card_frame_and_a_stale_tap_does_nothing(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#name")))
    assert await _until(lambda: focused(rig.pod), lambda f: f == "name") == "name"
    for scroll in (300, 455, 610):  # the card frame at three places on the screen
        await rig.tab(f"scrollTo(0, {scroll}); document.activeElement.blur()")
        await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#cc", "#card")))
        assert await _until(lambda: focused(rig.pod, "#card"), lambda f: f == "cc") == "cc", f"at scroll {scroll}"
        assert await focused(rig.pod) == "card"
        frame = await card(rig.pod)
        await frame.evaluate("document.activeElement.blur()")

    # A tap aimed at an image the page has since scrolled away from is refused, and nothing is clicked.
    await rig.tab("scrollTo(0, 0)")
    old = await current(viewer, rig.pod)
    clicks = len(await rig.tab("window.__log.filter(e => e[0] === 'click')"))
    await rig.tab("scrollTo(0, 300)")
    await current(viewer, rig.pod)
    errors = len([m for m in viewer.messages if m["t"] == "error"])
    await viewer.send({"t": "tap", "page": 1, "seq": old.seq, "x": 100, "y": 100})
    error = await viewer.message("error", after=0, where=lambda m: m["ref"] == "tap")
    assert error == {"t": "error", "code": "stale", "ref": "tap"}
    assert len([m for m in viewer.messages if m["t"] == "error"]) == errors + 1
    await asyncio.sleep(0.3)
    assert len(await rig.tab("window.__log.filter(e => e[0] === 'click')")) == clicks


async def test_wheels_from_a_120_hz_gesture_finish_with_the_viewer_and_scroll_exactly(rig: Rig) -> None:
    """A drag and its coast at 120 Hz, which the viewer sums into one `wheel` per 33 ms (PROTOCOL §6.6), starting
    over the card frame (which doesn't scroll, so the page does)."""
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await rig.tab(
        "scrollTo(0, 300); window.__scrolls = []; addEventListener('scroll', () => __scrolls.push(Date.now()))"
    )
    await current(viewer, rig.pod)
    before = await rig.tab("scrollY")
    start = middle(await frame_rect(rig.pod, "#card"))
    total, step, summed, sent_at, last = 0.0, 18.0, 0.0, 0.0, 0.0
    for _ in range(150):  # 1.25 s of finger and coast, a frame every 8.3 ms
        summed += step
        step = max(step * 0.985, 1.0)
        if time.monotonic() - sent_at >= 0.033:
            await viewer.send({"t": "wheel", "page": 1, "x": start[0], "y": start[1], "dx": 0, "dy": summed})
            total, summed, sent_at, last = total + summed, 0.0, time.monotonic(), time.time()
        await asyncio.sleep(1 / 120)
    if summed:
        await viewer.send({"t": "wheel", "page": 1, "x": start[0], "y": start[1], "dx": 0, "dy": summed})
        total, last = total + summed, time.time()
    await asyncio.sleep(0.5)
    finished = (await rig.tab("window.__scrolls"))[-1] / 1000
    assert finished - last < 0.1, f"the page last scrolled {finished - last:.3f} s after the viewer's last wheel"
    scrolled = await rig.tab("scrollY") - before
    assert abs(scrolled - total) <= 2, f"scrolled {scrolled} CSS px for {total} frame px at page scale 1"
    assert not [m for m in viewer.messages if m["t"] == "error"]


async def test_a_pinch_zooms_about_where_it_started_and_pans_to_where_it_ended(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    header = await current(viewer, rig.pod)
    # The document point under (300, 400) at scale 1.
    point = await rig.tab("[visualViewport.pageLeft + 300, visualViewport.pageTop + 400]")
    await viewer.send(
        {"t": "pinch", "page": 1, "seq": header.seq, "x0": 300, "y0": 400, "x1": 250, "y1": 300, "scale": 2}
    )
    await viewer.until(lambda: next((i for i in viewer.images if abs(i[0].scale - 2) < 0.01), None))
    view = await _until(
        lambda: rig.tab("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]"),
        lambda v: abs(v[2] - 2) < 0.01,
    )
    await asyncio.sleep(0.3)
    left, top, scale = await rig.tab("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]")
    assert abs(scale - 2) < 0.01, view
    assert abs((point[0] - left) * scale - 250) <= 2
    assert abs((point[1] - top) * scale - 300) <= 2
    log = await rig.tab("window.__log")
    assert any(e[0] == "wheel" and e[2] for e in log), "the page saw no ctrl+wheel (a trackpad pinch)"
    assert not any(e[0].startswith("touch") for e in log)
    assert await rig.tab("navigator.maxTouchPoints") == 0


async def test_text_keys_and_composition_reach_the_page_and_the_card_frame(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    # A pasted address with a line break: an input gets a space (Chromium's paste rules), a textarea keeps it.
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#postal")))
    assert await _until(lambda: focused(rig.pod), lambda f: f == "postal") == "postal"
    await viewer.send({"t": "text", "s": "123 Main St\nVictoria BC\n"})
    assert await _until(lambda: rig.pod.tab.input_value("#postal"), bool) == "123 Main St Victoria BC"
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#notes")))
    assert await _until(lambda: focused(rig.pod), lambda f: f == "notes") == "notes"
    await viewer.send({"t": "text", "s": "Ramp\nplease"})
    assert await _until(lambda: rig.pod.tab.input_value("#notes"), bool) == "Ramp\nplease"

    # IME: a composition, then its commit.
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#name")))
    assert await _until(lambda: focused(rig.pod), lambda f: f == "name") == "name"
    await viewer.send({"t": "text", "s": "Mike "})
    for s in ("Ch", "Che", "Ches"):
        await viewer.send({"t": "compose", "s": s})
    await viewer.send({"t": "text", "s": "Chester"})
    assert await _until(lambda: rig.pod.tab.input_value("#name"), lambda v: v == "Mike Chester") == "Mike Chester"

    # Keys into the cross-site card frame; Backspace; Tab moves on.
    frame = await card(rig.pod)
    await rig.tab("scrollTo(0, 450)")
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#cvc", "#card")))
    assert await _until(lambda: focused(rig.pod, "#card"), lambda f: f == "cvc") == "cvc"
    for c in "4242":
        await viewer.send({"t": "key", "c": c})
    await viewer.send({"t": "key", "k": "Backspace"})
    assert await _until(lambda: frame.input_value("#cvc"), lambda v: v == "424") == "424"
    await viewer.send({"t": "key", "k": "Tab", "shift": True})
    assert await _until(lambda: focused(rig.pod, "#card"), lambda f: f == "exp") == "exp"
    focus = await viewer.message("focus", where=lambda m: m["f"] is not None and m["f"]["ac"] == "cc-exp")
    assert focus["f"]["im"] == "numeric"


async def test_a_desktops_keys_are_real_presses_with_shift(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(DESKTOP)
    await viewer.image()
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, "#name")))
    assert await _until(lambda: focused(rig.pod), lambda f: f == "name") == "name"
    await rig.tab("window.__log = []")
    for message in (
        {"t": "key", "k": "Shift", "ph": "down"},
        {"t": "key", "c": "M", "ph": "down"},
        {"t": "key", "c": "M", "ph": "up"},
        {"t": "key", "k": "Shift", "ph": "up"},
        {"t": "key", "c": "i", "ph": "down"},
        {"t": "key", "c": "k", "ph": "down"},
        {"t": "key", "c": "i", "ph": "up"},
        {"t": "key", "c": "k", "ph": "up"},
    ):
        await viewer.send(message)
    assert await _until(lambda: rig.pod.tab.input_value("#name"), lambda v: v == "Mik") == "Mik"
    keys = [e[:5] for e in await rig.tab("window.__log") if e[0] in ("keydown", "keyup")]
    assert keys[:4] == [
        ["keydown", "name", 0, 1, "Shift"],
        ["keydown", "name", 0, 1, "M"],
        ["keyup", "name", 0, 1, "M"],
        ["keyup", "name", 0, 0, "Shift"],
    ]
    assert [k[0] + k[4] for k in keys[4:]] == ["keydowni", "keydownk", "keyupi", "keyupk"]


# ---- 8. pickers that never click what the user didn't pick


def field_at(fields: dict[str, Any], rect: list[float]) -> dict[str, Any] | None:
    """The map's field whose rectangle is this one (within 2 px)."""
    return next((f for f in fields["f"] if all(abs(a - b) <= 2 for a, b in zip(f["r"], rect, strict=True))), None)


async def mapped(viewer: Viewer, pod: Pod, selector: str, frame: str | None = None) -> tuple[int, dict[str, Any]]:
    """The latest map that has this element: its rev, and the element's field."""
    rect = await frame_rect(pod, selector, frame)

    def find() -> tuple[int, dict[str, Any]] | None:
        for message in reversed(viewer.messages):
            if message["t"] == "fields":
                found = field_at(message, rect)
                return (message["rev"], found) if found is not None else None
        return None

    found: tuple[int, dict[str, Any]] = await viewer.until(find)
    return found


async def new_map(viewer: Viewer, after_rev: int) -> dict[str, Any]:
    return await viewer.message("fields", where=lambda m: m["rev"] > after_rev)


async def footer(rig: Rig, height: int) -> None:
    await rig.tab(f"document.getElementById('pay').style.height = '{height}px'")


async def test_choose_and_date_click_only_where_the_field_itself_gets_the_click(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/occl.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()

    def errors() -> list[dict[str, Any]]:
        return [m for m in viewer.messages if m["t"] == "error"]

    # Half under the "Pay now" footer: picked through its visible half.
    rev, s2 = await mapped(viewer, rig.pod, "#s2")
    assert s2["m"] == 0b000111111  # its bottom row is under the footer
    await viewer.send({"t": "choose", "page": 1, "rev": rev, "f": s2["id"], "i": 2})
    assert await _until(lambda: rig.tab("document.getElementById('s2').value"), lambda v: v == "ON") == "ON"
    assert "s2 change true ON" in await rig.tab("window.log")

    # A custom select whose list never opens (its mousedown is cancelled): the agent's click doesn't focus it.
    rev, s4 = await mapped(viewer, rig.pod, "#s4")
    await viewer.send({"t": "choose", "page": 1, "rev": rev, "f": s4["id"], "i": 1})
    assert await viewer.message("error", where=lambda m: m["ref"] == "choose") == {
        "t": "error",
        "code": "failed",
        "ref": "choose",
    }
    assert await rig.tab("document.getElementById('s4').selectedIndex") == 0

    # A clear date field is set as an en-US keyboard user would.
    rev, d2 = await mapped(viewer, rig.pod, "#d2")
    assert d2["k"] == "date"
    await viewer.send({"t": "date", "page": 1, "rev": rev, "f": d2["id"], "v": "2026-10-16"})
    assert await _until(lambda: rig.pod.tab.input_value("#d2"), lambda v: v == "2026-10-16") == "2026-10-16"

    # Fields covered between the map and the message: nothing is clicked, nothing is typed.
    for selector, frame, message in (
        ("#s3", None, {"t": "choose", "i": 1}),
        ("#d1", None, {"t": "date", "v": "2028-02-29"}),
        ("#expy", "#card", {"t": "choose", "i": 1}),  # in the card frame, under the parent page's footer
    ):
        await footer(rig, 0)  # out of the way, so the map has the field
        rev, f = await mapped(viewer, rig.pod, selector, frame)
        await footer(rig, 664)  # now it covers everything
        await new_map(viewer, rev)
        before = len(errors())
        await asyncio.sleep(1.1)  # the agent sends one `error` a second for each code (PROTOCOL §5.7)
        await viewer.send({**message, "page": 1, "rev": rev, "f": f["id"]})
        error = await viewer.until(since(errors, before))
        assert error[0]["code"] == "stale", selector

    await asyncio.sleep(0.3)
    assert not [e for e in await rig.tab("window.log") if e.startswith("PAY")], "the footer was clicked"
    assert await rig.tab("document.getElementById('s3').selectedIndex") == 0
    assert await rig.pod.tab.input_value("#d1") == ""
    assert await (await card(rig.pod)).evaluate("document.getElementById('expy').selectedIndex") == 0


# ---- 12. dialogs


async def press_button(viewer: Viewer, rig: Rig, selector: str, frame: str | None = None) -> None:
    """Scroll it into view (by script: what the user would have done), and tap it."""
    if frame is None:
        await rig.tab("(s) => document.querySelector(s).scrollIntoView({block: 'center'})", selector)
    else:
        await rig.tab("(s) => document.querySelector(s).scrollIntoView({block: 'center'})", frame)
    await tap(viewer, rig.pod, middle(await frame_rect(rig.pod, selector, frame)))


async def test_dialogs_show_with_their_frames_origin_and_wait_across_a_reconnect(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()

    await press_button(viewer, rig, "#alertbtn")
    alert = await viewer.message("dialog")
    assert {k: alert[k] for k in ("page", "kind", "origin", "msg", "def")} == {
        "page": 1,
        "kind": "alert",
        "origin": rig.sites.shop,
        "msg": "Your session will expire soon",
        "def": "",
    }
    await viewer.send({"t": "dialog", "id": alert["id"], "ok": True})
    await _until(lambda: rig.tab("window.__dialogs.length"), lambda n: n == 1)

    await press_button(viewer, rig, "#confirmbtn")
    confirm = await viewer.message("dialog", where=lambda m: m["kind"] == "confirm")
    await viewer.send({"t": "dialog", "id": confirm["id"], "ok": True})
    await press_button(viewer, rig, "#promptbtn")
    prompt = await viewer.message("dialog", where=lambda m: m["kind"] == "prompt")
    assert prompt["def"] == "none"
    await viewer.send({"t": "dialog", "id": prompt["id"], "ok": True, "s": "FERRY10"})
    answers = await _until(lambda: rig.tab("window.__dialogs"), lambda d: len(d) == 3)
    assert answers == [["alert"], ["confirm", True], ["prompt", "FERRY10"]]

    # From the cross-site card frame: the sheet must say it's that site talking, not the shop.
    await press_button(viewer, rig, "#cardalert", "#card")
    from_card = await viewer.message("dialog", where=lambda m: m["msg"].startswith("Card declined"))
    assert from_card["origin"] == rig.sites.pay
    await viewer.send({"t": "dialog", "id": from_card["id"], "ok": True})

    # A confirm left open across a reconnect (the bank app, a screen lock) is announced again, and answered.
    await press_button(viewer, rig, "#confirmbtn")
    await viewer.message("dialog", where=lambda m: m["kind"] == "confirm" and m["id"] != confirm["id"])
    await viewer.close()
    again = await rig.connect(PHONE)
    reopened = await again.message("dialog")
    assert reopened["kind"] == "confirm"
    assert not again.images, "an image of a page a dialog blocks"  # it can't be captured until it's answered
    await again.send({"t": "dialog", "id": reopened["id"], "ok": True})
    answers = await _until(lambda: rig.tab("window.__dialogs"), lambda d: len(d) == 4)
    assert answers[-1] == ["confirm", True]
    header, _ = await again.image()
    assert header.first


# ---- 13. pop-ups and tabs


async def test_pop_ups_and_tabs_become_the_front_page_take_input_and_close(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    for selector, frame in (("#pop", None), ("#newtab", None), ("#pop3ds", "#card")):
        await rig.tab("scrollTo(0, 450)" if frame else "scrollTo(0, 0)")
        count = len(viewer.messages)
        await press_button(viewer, rig, selector, frame)
        listing = await viewer.message("pages", where=lambda m: len(m["pages"]) == 2, after=count)
        assert listing["front"] == listing["pages"][1]["id"]
        assert listing["pages"][1] == {
            "id": listing["front"],
            "title": "Your bank",
            "origin": rig.sites.pay,
            "main": False,
        }
        popup = rig.pod.context.pages[-1]
        header = await viewer.until(image_of(viewer, on_page(listing["front"])))
        assert header.first

        # The bank's one-time code, typed into the pop-up.
        box = await popup.evaluate(ELEMENT_RECT, "#otp")
        await viewer.until(image_of(viewer, on_page(listing["front"]), latest=True))
        await viewer.send(
            {"t": "tap", "page": listing["front"], "seq": viewer.latest.seq, "x": box[0] + 20, "y": box[1] + 10}
        )
        await _until(functools.partial(popup.evaluate, "document.activeElement.id"), lambda f: f == "otp")
        await viewer.send({"t": "text", "s": "123456"})
        assert await _until(functools.partial(popup.input_value, "#otp"), bool) == "123456"

        await viewer.send({"t": "page", "op": "close", "page": listing["front"]})
        back = await viewer.message("pages", where=lambda m: len(m["pages"]) == 1, after=count)
        assert back["front"] == 1
    await viewer.send({"t": "page", "op": "close", "page": 1})
    assert await viewer.message("error", where=lambda m: m["ref"] == "page") == {
        "t": "error",
        "code": "failed",
        "ref": "page",
    }
    assert not rig.pod.tab.is_closed()


# ---- 9-10. the field map


def map_for(viewer: Viewer, header: ImageHeader) -> dict[str, Any] | None:
    """The latest map (its `fields`; a later `fields_ok` only confirms it) if it was read where the image shows."""
    latest = next((m for m in reversed(viewer.messages) if m["t"] == "fields"), None)
    if latest is None or abs(latest["sy"] - header.sy) > 0.5 or abs(latest["sx"] - header.sx) > 0.5:
        return None
    return latest if abs(latest["scale"] - header.scale) <= 0.001 else None


async def latest_map(viewer: Viewer, after: int = 0) -> dict[str, Any]:
    """The latest `fields` message once at least one came after the `after`th message."""
    await viewer.message("fields", after=after)
    return next(m for m in reversed(viewer.messages) if m["t"] == "fields")


async def map_after(viewer: Viewer, number: int) -> dict[str, Any]:
    """The map in force once one was taken after message `number` (`fields`, or `fields_ok` confirming the last)."""
    await viewer.until(
        lambda: next((m for m in viewer.messages if m["t"] in ("fields", "fields_ok") and m["n"] >= number), None)
    )
    return next(m for m in reversed(viewer.messages) if m["t"] == "fields")


async def settled_map(viewer: Viewer, pod: Pod, number: int) -> dict[str, Any]:
    """The latest map, once it was taken after message `number` and where the page is now."""

    async def read() -> dict[str, Any] | None:
        live = await pod.tab.evaluate("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]")
        found = next((m for m in reversed(viewer.messages) if m["t"] == "fields"), None)
        ok = next((m for m in reversed(viewer.messages) if m["t"] in ("fields", "fields_ok")), None)
        if found is None or ok is None or ok["n"] < number or ok["rev"] != found["rev"]:
            return None
        same = abs(found["sx"] - live[0]) <= 0.5 and abs(found["sy"] - live[1]) <= 0.5
        return found if same and abs(found["scale"] - live[2]) <= 0.001 else None

    found: dict[str, Any] | None = await _until(read, bool)
    assert found is not None, "no map of the page as it is"
    return found


async def test_the_field_map_is_where_the_fields_are_zoomed_or_not_and_follows_inner_scrolling(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await rig.tab("scrollTo(0, 420)")
    await current(viewer, rig.pod)
    for zoomed in (False, True):
        number = viewer.number
        if zoomed:  # scale 2, the visual viewport moved: PROTOCOL §11.3's case
            header = await current(viewer, rig.pod)
            number = await viewer.send(
                {"t": "pinch", "page": 1, "seq": header.seq, "x0": 300, "y0": 400, "x1": 250, "y1": 300, "scale": 2}
            )
        fields = await settled_map(viewer, rig.pod, number)
        assert abs(fields["scale"] - (2 if zoomed else 1)) < 0.01
        header = await current(viewer, rig.pod)
        checked = 0
        for selector, frame in (
            ("#name", None),
            ("#prov", None),
            ("#notes", None),
            ("#cc", "#card"),
            ("#cvc", "#card"),
        ):
            truth = await frame_rect(rig.pod, selector, frame)
            x, y, w, h = truth
            visible = x < header.w and y < header.h and x + w > 0 and y + h > 0
            if visible:
                assert field_at(fields, truth) is not None, f"{selector} isn't where it is (zoomed: {zoomed})"
                checked += 1
        assert checked >= 2, "too few fields in view to check"

    # A covered field isn't in the map at all.
    await rig.pod.at(f"{rig.sites.shop}/occl.html")
    fields = await latest_map(viewer, len(viewer.messages))
    assert field_at(fields, await frame_rect(rig.pod, "#s1")) is None
    assert field_at(fields, await frame_rect(rig.pod, "#s3")) is not None

    # An inner scroller the viewer scrolled: the next map's rectangles moved with it.
    await rig.pod.at(f"{rig.sites.shop}/inner.html")
    count = len(viewer.messages)
    before = field_at(await latest_map(viewer, count), await frame_rect(rig.pod, "#tel"))
    assert before is not None
    header = await current(viewer, rig.pod)
    number = await viewer.send({"t": "wheel", "page": header.page, "x": 195, "y": 400, "dx": 0, "dy": 120})
    after = await viewer.message("fields", where=lambda m: m["n"] >= number)
    moved = field_at(after, await frame_rect(rig.pod, "#tel"))
    assert moved is not None
    assert abs(moved["r"][1] - (before["r"][1] - 120)) <= 2
    assert after["sy"] == 0, "the document itself didn't scroll"


async def test_a_page_that_never_stops_animating_still_gets_a_map_on_time(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/anim.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await viewer.message("fields")
    for _ in range(3):
        number = await viewer.send({"t": "wheel", "page": 1, "x": 195, "y": 400, "dx": 0, "dy": 40})
        started = time.monotonic()
        await viewer.message("fields", where=covers(number))
        assert time.monotonic() - started < 0.5, "no map within 500 ms on an animated page"
        await asyncio.sleep(0.3)


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    raise AssertionError("no VmRSS")


async def test_page_data_is_bounded_inside_the_page(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations: list[tuple[float, float]] = []  # when each started, and how long it took
    evaluate = cast_fields.FieldMapper._evaluate

    async def timed(self: cast_fields.FieldMapper, *args: Any) -> bool:
        started = time.monotonic()
        slow = await evaluate(self, *args)  # one that fails (the page between documents) is tried again, unrecorded
        evaluations.append((started, time.monotonic() - started))
        return slow

    monkeypatch.setattr(cast_fields.FieldMapper, "_evaluate", timed)
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    rss = _rss_mb()
    # 5,000 inputs piled on one spot are past the 500-candidate cap and make each hit test slow enough for the
    # 30 ms budget too. (200,000, as in the design's headless spike, don't load on the pod's screen in minutes.)
    wheel = {"t": "wheel", "page": 1, "x": 195, "y": 300, "dx": 0, "dy": 1}  # the bomb pages don't scroll
    for query, trunc in (("label=100000", False), ("ac=2000&n=5000", True), ("n=5000", True)):
        await rig.pod.at(f"{rig.sites.shop}/bomb.html?{query}")
        loaded = time.monotonic()  # evaluations while it loaded wait for a busy renderer: not the scripts' time
        # The map of the loaded document (the agent may have sent maps while it loaded): one evaluated after this
        # message, as `fields`, or `fields_ok` if it's the map already sent.
        fields = await map_after(viewer, await viewer.send(wheel))
        assert len(json.dumps(fields).encode()) <= 65536
        assert fields["trunc"] is trunc, query
        for f in fields["f"]:
            assert len(f["ac"]) <= 64
            assert all(len(label) <= 64 for label, _ in f.get("opts", []))
        for _ in range(3):
            number = await viewer.send(wheel)
            await viewer.message("fields_ok", where=covers(number))
        durations = [took for started, took in evaluations if started >= loaded]
        assert len(durations) >= 4
        # The first evaluation of a new document makes its worlds while the page is still settling; the ones after
        # are the scripts and their bounds alone.
        assert durations[0] < 0.5, f"the first evaluation took {durations[0]:.3f} s on {query}"
        assert max(durations[1:]) < 0.15, f"an evaluation took {max(durations[1:]):.3f} s on {query}"
    assert _rss_mb() - rss < 20, "the agent grew reading the bomb pages"

    count = len(viewer.messages)
    await rig.pod.at(f"{rig.sites.shop}/bomb.html?title=20000000")
    listing = await viewer.message("pages", after=count, where=lambda m: m["pages"][0]["title"].startswith("T"))
    assert listing["pages"][0]["title"] == "T" * 80


# ---- 11. still mode


def _browser_cpu_seconds(profile: Path) -> float:
    """CPU seconds of the pod's Chromium: the browser process (its profile on the command line) and its children."""
    tick = os.sysconf("SC_CLK_TCK")
    parents: dict[int, int] = {}
    times: dict[int, int] = {}
    root = None
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            command = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        pid = int(entry.name)
        parents[pid], times[pid] = int(stat[1]), int(stat[11]) + int(stat[12])
        if any(str(profile).encode() in c for c in command) and not any(c.startswith(b"--type=") for c in command):
            root = pid
    assert root is not None, "no Chromium for the pod's profile"
    tree, grown = {root}, True
    while grown:
        grown = False
        for pid, parent in parents.items():
            if parent in tree and pid not in tree:
                tree.add(pid)
                grown = True
    return sum(times.get(pid, 0) for pid in tree) / tick


async def test_an_animated_page_nobody_touches_is_sent_as_stills(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/anim.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await asyncio.sleep(2)  # no input: live, then still once a second of frames went by
    count, cpu, started = len(viewer.images), _browser_cpu_seconds(rig.pod.work / "profile"), time.monotonic()
    await asyncio.sleep(5)
    elapsed = time.monotonic() - started
    rate = (len(viewer.images) - count) / elapsed
    share = (_browser_cpu_seconds(rig.pod.work / "profile") - cpu) / elapsed
    assert rate <= 2.2, f"{rate:.1f} images a second in still mode"
    assert share < 0.4, f"Chromium used {share:.0%} of a core on a page nobody touches"

    count = len(viewer.images)
    await tap(viewer, rig.pod, (195, 500))
    await asyncio.sleep(1)
    assert len(viewer.images) - count > 4, "a tap didn't bring the screencast back"


# ---- 15. the hold ends


async def test_when_the_hold_ends_the_stream_closes_and_its_dialogs_are_dismissed(rig: Rig) -> None:
    await rig.pod.at(f"{rig.sites.shop}/index.html")
    viewer = await rig.connect(PHONE)
    await viewer.image()
    await press_button(viewer, rig, "#confirmbtn")
    await viewer.message("dialog")
    await rig.gate.close("hold ended", CloseCode.ENDED)
    assert await viewer.closed() == CloseCode.ENDED
    assert await _until(lambda: rig.tab("window.__dialogs"), bool) == [["confirm", False]]
    assert rig.keeper.open_dialogs == []
    assert await rig.tab("confirm('Anyone?')") is False  # the agent's own browsing is as before


# ---- 14. the classic viewer, after a stream


# vnc-edge's site, and the viewer's browser (test_checkout_edge.py's).
site = edge.site
browser = edge.browser
page = edge.page

# The tab's sizes from now on, as each resize leaves it.
SIZES = (
    "window.__sizes = []; addEventListener('resize', () => __sizes.push([innerWidth, innerHeight, devicePixelRatio]))"
)

# The device's record, as the landing page keeps it once the checkout was opened (here: by the stream).
DEVICE_RECORD = """([key, nonce, hardDeadline]) => {
  localStorage.setItem(key, JSON.stringify({n: nonce, opened: true, disp: {}, hard_deadline: hardDeadline,
                                            session_deadline: null}));
}"""


async def _xsettingsd_running(config_dir: Path) -> bool:
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if str(config_dir).encode() in (entry / "cmdline").read_bytes():
                    return True
    return False


async def _modes(pod: Pod) -> str:
    xrandr = await asyncio.create_subprocess_exec("xrandr", "--query", env=pod.env, stdout=asyncio.subprocess.PIPE)
    out, _ = await xrandr.communicate()
    return out.decode()


async def test_the_classic_viewer_finds_the_pods_own_screen_after_a_stream(
    pod: Pod, sites: Sites, site: edge.Site, page: Page
) -> None:
    offers: list[list[str]] = []
    path = f"/t/{edge.TRIP}/g/{edge.GEN}/ws"
    async with make_rig(pod, sites, app=lambda gate: edge.edge_app(site, gate, offers), path=path) as rig:
        await rig.pod.tab.evaluate(screen.SHOW_COLOUR)
        viewer = await rig.connect(PHONE)
        await viewer.image()
        assert await rig.tab("[innerWidth, innerHeight, devicePixelRatio]") == [390, 664, 2]
        await viewer.close()
        await asyncio.sleep(0.5)
        # A stream's teardown leaves the screen as it was: the same device may come straight back.
        assert await rig.tab("[innerWidth, innerHeight, devicePixelRatio]") == [390, 664, 2]
        await rig.tab(SIZES)

        # The classic viewer on the same device: the landing page, then KasmVNC's client, by the stream's nonce.
        await page.goto(f"{rig.origin}/gone.html")
        await page.evaluate(DEVICE_RECORD, [f"fw:{edge.TRIP}:{edge.GEN}", NONCE, int(time.time()) + 3600])
        await page.goto(f"{rig.origin}{edge.LANDING}")
        await page.click("#open")
        await page.wait_for_selector("html.noVNC_connected")
        assert sorted(offers[-1]) == sorted(["binary", f"fw.n1.{NONCE}"])

        inner = await page.evaluate("[innerWidth, innerHeight]")
        sizes = await _until(lambda: rig.tab("window.__sizes"), lambda s: bool(s) and s[-1] == [*inner, 1])
        assert sizes[-1] == [*inner, 1], sizes
        # Scale factor 1 first (at the old width), the pod's own 1280x1000, and only then the client's resize.
        assert [1280, 1000, 1] in sizes[:-1], f"the pod's own screen wasn't there before the client's resize: {sizes}"
        assert all(dpr == 1 for _, _, dpr in sizes)
        assert not await _xsettingsd_running(pod.work / "xsettings")
        assert " fw" not in await _modes(pod), "a stream's RandR mode was left behind"
        assert rig.screen.current is None

        # And KasmVNC's client works as test_checkout_screen.py has it: fitted, the checkout's colour, a click
        # where it was clicked and typing.
        classic = screen.Pod(pod.rfb_socket, pod.tab)
        box = await screen._fitted(page, classic)
        shown = await _until(
            lambda: page.evaluate(screen.CANVAS, list(screen.COLOUR)), lambda c: c and c["shown"] > 0.99
        )
        assert shown is not None
        assert shown["shown"] > 0.99
        x, y, width, height = box
        await page.mouse.click(x + width / 2, y + height / 2)
        clicks = await _until(lambda: pod.tab.evaluate("window.clicks"), bool)
        tab_width, tab_height = await pod.tab.evaluate("[innerWidth, innerHeight]")
        assert len(clicks) == 1
        assert abs(clicks[0][0] - tab_width / 2) <= 2
        assert abs(clicks[0][1] - tab_height / 2) <= 2
        await page.keyboard.type("4502")
        assert await _until(lambda: pod.tab.evaluate("window.keys"), lambda k: len(k) >= 4) == ["4", "5", "0", "2"]
