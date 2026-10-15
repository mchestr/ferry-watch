"""The streamed checkout viewer in a real browser, against a stand-in agent (docs/multi-tenant/streamed-viewer/DESIGN.md
§10.3).

vnc-edge's real files (the landing page, fw/session.js, viewer.html and its modules) are served as
test_checkout_edge.py serves them, with viewer.html under its own Content-Security-Policy (C38), and the checkout's
socket goes to tests/browser/fake_agent.py, which checks everything the viewer sends against PROTOCOL.md and sends
what each test scripts. The viewer runs as an iPhone-sized phone (390x664 CSS px at 3x, touch), a 1280x800 computer
and a 2560x1440 one, wider than any viewport the viewer may ask for.

Playwright's wait_for_function compiles its predicate with eval, which the viewer's policy refuses: these tests poll
with evaluate instead.

Needs FW_KASMVNC_WWW, as test_checkout_edge.py does.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp.test_utils import TestServer, unused_port
from playwright.async_api import Browser, BrowserContext, Page

from tests.browser import test_checkout_edge as edge
from tests.browser.fake_agent import Context, FakeAgent, Hello, Image, Refused, Session, decode, pack, valid

pytestmark = pytest.mark.browser

site = edge.site
browser = edge.browser

DEVICES: dict[str, dict[str, Any]] = {
    "phone": {
        "viewport": {"width": 390, "height": 664},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
    },
    "desktop": {"viewport": {"width": 1280, "height": 800}},
    "wide": {"viewport": {"width": 2560, "height": 1440}},
}
HELLOS: dict[str, dict[str, Any]] = {
    "phone": {"t": "hello", "v": 1, "w": 390, "h": 664, "dpr": 3},
    "desktop": {"t": "hello", "v": 1, "w": 1280, "h": 800, "dpr": 1},
    "wide": {"t": "hello", "v": 1, "w": 1920, "h": 1440, "dpr": 1},  # clamped (PROTOCOL.md §8.1)
}
RED = (220, 38, 38)
BLUE = (37, 99, 235)
GREEN = (22, 163, 74)
LETTERBOX = (214, 211, 209)  # viewer.css's --letterbox, light
WAIT_S = 10.0
BROKEN_MODULE = 'export class Frames {}\nthrow new Error("broken");'  # links, then throws as it runs


def test_the_stand_in_agent_judges_messages_as_the_conformance_pack_does() -> None:
    """fake_agent.py's checks are the agent's (PROTOCOL.md §6), or a viewer bug could pass these tests."""
    messages = pack("messages.json")
    images = tuple(Image(**image) for image in messages["context"]["images"])
    context = Context(images, {f["id"]: f["k"] for f in messages["context"]["fields"]})
    for case in messages["cases"]:
        assert judged(case, context) == (case["outcome"], case.get("code")), case["id"]


def judged(case: dict[str, Any], context: Context) -> tuple[str, int | None]:
    if "hex" in case:
        frame: str | bytes = bytes.fromhex(case["hex"])
    elif "raw" in case:
        frame = case["raw"] + " " * (case.get("pad_to", 0) - len(case["raw"].encode()))
    else:
        frame = json.dumps(case["msg"])
    state = case.get("state", "ready")
    try:
        msg = decode(frame)
    except Refused as e:
        return "close", e.code
    if state == "hello" or (state == "open") != isinstance(msg, Hello):
        return "close", 4400
    if not valid(msg, context):
        return ("close", 4400) if isinstance(msg, Hello) else ("invalid", None)
    return "ok", None


# ---- the viewer against the stand-in agent


@dataclass
class Edge:
    url: str
    agent: FakeAgent
    offers: list[list[str]]


@pytest.fixture
async def checkout(site: edge.Site) -> AsyncIterator[Edge]:
    port = unused_port()
    origin = f"http://127.0.0.1:{port}"
    agent = FakeAgent(origin)
    offers: list[list[str]] = []
    server = TestServer(edge.edge_app(site, agent.handle, offers), host="127.0.0.1", port=port)
    await server.start_server()
    try:
        yield Edge(origin, agent, offers)
    finally:
        for session in agent.sessions:
            await session.ws.close()
        await server.close()
    assert agent.problems() == [], "the agent would have refused what the viewer sent"


@dataclass
class Viewer:
    page: Page
    device: str
    reports: list[str]  # what broke the viewer's Content-Security-Policy or Trusted Types, or the protocol
    errors: list[str]  # uncaught errors

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        return await self.page.evaluate(script, arg)


@contextlib.asynccontextmanager
async def device_viewer(browser: Browser, device: str) -> AsyncIterator[Viewer]:
    """A browser tab of this device's size; none of its pages may break the policy or throw."""
    context = await browser.new_context(**DEVICES[device])
    page = await context.new_page()
    viewer = Viewer(page, device, [], [])
    page.on("console", lambda m: viewer.reports.append(m.text) if _reported(m.text) else None)
    page.on("pageerror", lambda e: viewer.errors.append(str(e)))
    try:
        yield viewer
    finally:
        await context.close()
    assert viewer.reports == [], "the viewer broke its Content-Security-Policy, or tried to break the protocol"
    assert viewer.errors == [], "the viewer threw"


@pytest.fixture(params=list(DEVICES))
async def viewer(request: pytest.FixtureRequest, browser: Browser) -> AsyncIterator[Viewer]:
    async with device_viewer(browser, request.param) as tab:
        yield tab


@pytest.fixture
async def phone(browser: Browser) -> AsyncIterator[Viewer]:
    async with device_viewer(browser, "phone") as tab:
        yield tab


def _reported(text: str) -> bool:
    """A policy report, or a message the viewer's own encoder refused to send."""
    return "Content Security Policy" in text or "Trusted" in text or text.startswith("fw.cast1: ")


async def open_viewer(viewer: Viewer, checkout: Edge, *, hold_s: int = 3600) -> str:
    """Through the landing page with a stream ticket, as Ferry Watch opens it. The ticket."""
    issued = edge.ticket(hold_s=hold_s)
    await viewer.page.goto(f"{checkout.url}{edge.LANDING}#v=s1&t={issued}")
    await viewer.page.click("#open")
    await viewer.page.wait_for_url(f"{checkout.url}{edge.STREAM_VIEWER}")
    return issued


async def until(read: Callable[[], Awaitable[Any]], done: Callable[[Any], bool], wait_s: float = WAIT_S) -> Any:
    """Read until `done` says yes, for up to `wait_s`. Returns the last reading either way."""
    deadline = time.monotonic() + wait_s
    while True:
        value = await read()
        if done(value) or time.monotonic() > deadline:
            return value
        await asyncio.sleep(0.05)


_jpegs: dict[tuple[int, int, tuple[int, int, int]], bytes] = {}


async def jpeg(context: BrowserContext, w: int, h: int, colour: tuple[int, int, int]) -> bytes:
    """A JPEG of one colour, made by Chromium on a page of its own (no policy there)."""
    key = (w, h, colour)
    if key not in _jpegs:
        page = await context.new_page()
        data_url: str = await page.evaluate(
            """([w, h, [r, g, b]]) => {
              const canvas = document.createElement("canvas");
              canvas.width = w;
              canvas.height = h;
              const context = canvas.getContext("2d");
              context.fillStyle = `rgb(${r}, ${g}, ${b})`;
              context.fillRect(0, 0, w, h);
              return canvas.toDataURL("image/jpeg", 0.95);
            }""",
            [w, h, list(colour)],
        )
        await page.close()
        _jpegs[key] = base64.b64decode(data_url.split(",", 1)[1])
    return _jpegs[key]


async def shown_image(viewer: Viewer, session: Session, colour: tuple[int, int, int], **image: Any) -> int:
    """Sends a full-size image of one colour, as the session's screen would; its seq, once acked."""
    assert session.hello is not None
    dsf = image.pop("dsf", 1)
    w, h = image.get("w", session.hello["w"]), image.get("h", session.hello["h"])
    data = await jpeg(viewer.page.context, w * dsf, h * dsf, colour)
    seq = await session.image(data, dsf=dsf, **image)
    await session.acked(seq)
    return seq


async def pixel(viewer: Viewer, x: float, y: float) -> tuple[int, int, int]:
    """The colour the viewer's canvas shows at (x, y) CSS px."""
    rgb: list[int] = await viewer.evaluate(
        """([x, y]) => {
          const canvas = document.getElementById("screen");
          const px = Math.floor((x * canvas.width) / innerWidth);
          const py = Math.floor((y * canvas.height) / innerHeight);
          return [...canvas.getContext("2d").getImageData(px, py, 1, 1).data.slice(0, 3)];
        }""",
        [x, y],
    )
    return (rgb[0], rgb[1], rgb[2])


def close_to(rgb: tuple[int, int, int], colour: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 12 for a, b in zip(rgb, colour, strict=True))


async def device_record(viewer: Viewer) -> dict[str, Any]:
    record: dict[str, Any] = await viewer.evaluate("(key) => JSON.parse(localStorage.getItem(key))", edge.RECORD)
    return record


async def status(viewer: Viewer) -> dict[str, Any]:
    """What the status screen shows, if anything."""
    shown: dict[str, Any] = await viewer.evaluate(
        """() => {
          const visible = (id) => !document.getElementById(id).hidden;
          return {
            shown: visible("status"),
            title: document.getElementById("status-title").textContent,
            here: visible("status-here"),
            classic: visible("status-classic"),
            prominent: document.getElementById("status-classic").classList.contains("primary"),
            back: visible("status-back"),
            escape: visible("fw-escape"),
          };
        }"""
    )
    return shown


async def until_status(viewer: Viewer, title: str) -> dict[str, Any]:
    shown: dict[str, Any] = await until(lambda: status(viewer), lambda s: s["shown"] and s["title"] == title)
    assert shown["title"] == title
    return shown


# ---- 1: hello, drawing, acks, slots and repeats


async def test_the_viewer_says_hello_and_draws_each_image_in_order_then_acks_it(viewer: Viewer, checkout: Edge) -> None:
    issued = await open_viewer(viewer, checkout)
    session = await checkout.agent.session(0)
    assert session.hello == HELLOS[viewer.device]
    nonce = (await device_record(viewer))["n"]
    assert session.offered == ["fw.cast1", f"fw.n1.{nonce}", f"fw.t1.{issued}"]
    # The first open locks the checkout to the device: the ticket is spent.
    assert (await device_record(viewer))["opened"] is True
    assert await viewer.evaluate("(key) => sessionStorage.getItem(key)", f"{edge.RECORD}:ticket") is None

    await session.start()
    assert (await status(viewer))["title"] == "Opening your checkout…"  # nothing to show yet
    dsf = 2 if viewer.device == "phone" else 1
    await shown_image(viewer, session, RED, dsf=dsf, first=True)
    assert not (await until(lambda: status(viewer), lambda s: not s["shown"]))["shown"]

    # The image fills a box of the viewer's size, centred: on the wide screen, 1920 px wide with 320 px either side.
    width, height = DEVICES[viewer.device]["viewport"].values()
    image_w = HELLOS[viewer.device]["w"]
    left = (width - image_w) / 2
    assert close_to(await pixel(viewer, left + 5, 5), RED)
    assert close_to(await pixel(viewer, left + image_w - 5, height - 5), RED)
    if left:
        assert close_to(await pixel(viewer, left - 5, height / 2), LETTERBOX)
        assert close_to(await pixel(viewer, width - left + 5, height / 2), LETTERBOX)

    # Slots and repeats: a repeat draws what its slot holds, with its own header.
    await shown_image(viewer, session, BLUE, slot=1, dsf=dsf)
    assert close_to(await pixel(viewer, width / 2, height / 2), BLUE)
    seq = await session.repeat(0)
    await session.acked(seq)
    assert close_to(await pixel(viewer, width / 2, height / 2), RED)
    seq = await session.repeat(1)
    await session.acked(seq)
    assert close_to(await pixel(viewer, width / 2, height / 2), BLUE)

    # A burst is drawn and acked strictly in order, repeats waiting for the image before them.
    green = await jpeg(viewer.page.context, image_w * dsf, HELLOS[viewer.device]["h"] * dsf, GREEN)
    burst = [await session.image(green, slot=0, dsf=dsf), await session.repeat(0), await session.repeat(1)]
    await session.acked(burst[-1])
    assert session.acks == list(range(1, burst[-1] + 1))
    assert close_to(await pixel(viewer, width / 2, height / 2), BLUE)

    # A pop-up's shorter page is centred in the viewer.
    short = HELLOS[viewer.device]["h"] - 64
    await shown_image(viewer, session, GREEN, page=2, h=short, slot=1)
    assert close_to(await pixel(viewer, width / 2, 16), LETTERBOX)
    assert close_to(await pixel(viewer, width / 2, 48), GREEN)
    assert [m["t"] for m in session.received] == ["ack"] * len(session.acks)


async def test_a_reconnect_says_hello_again_and_numbers_from_the_start(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout)
    first = await checkout.agent.session(0)
    await first.start()
    await shown_image(phone, first, RED, dsf=2, first=True)
    await first.close(1011)  # a proxy on the way gave up: reconnect with backoff
    await until_status(phone, "Reconnecting…")

    second = await checkout.agent.session(1)
    nonce = (await device_record(phone))["n"]
    assert second.offered == ["fw.cast1", f"fw.n1.{nonce}"]  # no ticket once opened
    assert second.hello == HELLOS["phone"]
    await second.start()
    seq = await shown_image(phone, second, BLUE, dsf=2, first=True)
    assert seq == 1
    assert second.acks == [1]
    assert not (await until(lambda: status(phone), lambda s: not s["shown"]))["shown"]
    assert close_to(await pixel(phone, 195, 332), BLUE)


async def test_a_hidden_viewer_says_so(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout)
    session = await checkout.agent.session(0)
    await session.start()
    await shown_image(phone, session, RED, dsf=2, first=True)
    await phone.evaluate(
        """() => {
          let state = "visible";
          Object.defineProperty(document, "visibilityState", { get: () => state, configurable: true });
          for (state of ["hidden", "visible"]) document.dispatchEvent(new Event("visibilitychange"));
        }"""
    )
    visible = await session.wait_for("visible", count=2)
    assert [m["v"] for m in visible] == [False, True]


# ---- 10: close codes


async def test_when_the_checkout_ends_the_viewer_says_so_and_stops(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout)
    session = await checkout.agent.session(0)
    await session.start()
    await shown_image(phone, session, RED, dsf=2, first=True)
    await session.close(4000)
    shown = await until_status(phone, "This checkout has ended")
    assert shown["back"]
    assert not shown["classic"]
    await asyncio.sleep(1.5)
    assert len(checkout.agent.sessions) == 1, "it reconnected"
    assert await phone.page.get_attribute("#status-back", "href") == f"{edge.APP}/"


async def test_opened_elsewhere_the_viewer_waits_to_be_asked_back(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout)
    session = await checkout.agent.session(0)
    await session.start()
    await shown_image(phone, session, RED, dsf=2, first=True)
    await session.close(4001)
    shown = await until_status(phone, "Opened elsewhere on this device")
    assert shown["here"]
    await asyncio.sleep(1)
    assert len(checkout.agent.sessions) == 1, "it reconnected by itself"
    await phone.page.tap("#status-here")
    again = await checkout.agent.session(1)
    await again.start()
    await shown_image(phone, again, BLUE, dsf=2, first=True)
    assert not (await until(lambda: status(phone), lambda s: not s["shown"]))["shown"]


async def test_withdrawn_the_viewer_goes_to_the_classic_one_for_good(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout)
    session = await checkout.agent.session(0)
    await session.start()
    await session.close(4005)
    await phone.page.wait_for_url(f"{checkout.url}{edge.classic_url(touch=True)}")
    record = await device_record(phone)
    assert (record["viewer"], record["viewer_fallback"]) == ("stream", True)


async def test_refused_the_viewer_backs_off_then_offers_the_classic_viewer(phone: Viewer, checkout: Edge) -> None:
    """Connections that never open (the gate refusing, PROTOCOL.md §3.7): 0.5, 1, 2 s apart; after three, the
    classic viewer prominently; after 3 s without `ready`, the escape bar too, until the viewer is ready."""
    checkout.agent.refuse_with = 409
    await open_viewer(phone, checkout)
    await checkout.agent.refused(1)
    refused = [time.monotonic()]
    for count in (2, 3):
        await checkout.agent.refused(count)
        refused.append(time.monotonic())
        shown = await until_status(phone, "Reconnecting…")
        assert shown["classic"]
        assert shown["prominent"] == (count == 3)
    gaps = [b - a for a, b in itertools.pairwise(refused)]
    assert 0.4 <= gaps[0] <= 0.95, gaps
    assert 0.9 <= gaps[1] <= 1.6, gaps
    shown = await until(lambda: status(phone), lambda s: s["escape"])
    assert shown["escape"], "the escape bar didn't show"

    checkout.agent.refuse_with = None
    session = await checkout.agent.session(0)  # 2 s after the third
    await session.start()
    await shown_image(phone, session, RED, dsf=2, first=True)
    shown = await until(lambda: status(phone), lambda s: not s["shown"] and not s["escape"])
    assert not shown["shown"]
    assert not shown["escape"], "the escape bar stayed once the viewer was ready"


async def test_a_broken_image_is_dropped_and_the_viewer_reconnects(phone: Viewer, checkout: Edge) -> None:
    """A binary message the viewer can't take is an agent bug (PROTOCOL.md §4.2): not drawn, not acked; the viewer
    closes with 1000 and carries on as after a 4500, prominently offering the classic viewer after two."""
    await open_viewer(phone, checkout)
    for index, prominent in ((0, False), (1, True)):
        session = await checkout.agent.session(index)
        await session.start()
        await session.ws.send_bytes(bytes(31))
        await session.closed.wait()
        assert session.close_code == 1000
        assert session.acks == []
        shown = await until_status(phone, "Reconnecting…")
        assert shown["prominent"] == prominent
    await checkout.agent.session(2)


async def test_after_the_hold_ends_the_viewer_goes_to_gone_html(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout, hold_s=2)
    session = await checkout.agent.session(0)
    await session.start()
    await shown_image(phone, session, RED, dsf=2, first=True)
    await asyncio.sleep(2.5)
    await session.close(1011)
    await phone.page.wait_for_url(f"{checkout.url}/gone.html")
    assert await phone.page.text_content("h1") == "No checkout is open for this trip"
    assert len(checkout.agent.sessions) == 1


# ---- 11: the escape hatch


async def test_without_its_module_the_viewer_still_offers_the_classic_viewer(phone: Viewer, checkout: Edge) -> None:
    await phone.page.route("**/fw/viewer/main.js", lambda route: route.fulfill(status=404, body="Not found"))
    await open_viewer(phone, checkout)
    loaded = time.monotonic()
    shown = await until(lambda: status(phone), lambda s: s["escape"], wait_s=5)
    assert shown["escape"]
    assert time.monotonic() - loaded < 3.5
    assert shown["title"] == "Opening your checkout…"
    assert checkout.agent.sessions == []
    await phone.page.tap("#fw-escape-classic")
    await phone.page.wait_for_url(f"{checkout.url}{edge.classic_url(touch=True)}")
    assert (await device_record(phone))["viewer_fallback"] is True


async def test_a_module_that_throws_shows_the_escape_bar_at_once(phone: Viewer, checkout: Edge) -> None:
    await phone.page.route(
        "**/fw/viewer/frames.js",
        lambda route: route.fulfill(body=BROKEN_MODULE, content_type="text/javascript"),
    )
    await open_viewer(phone, checkout)
    loaded = time.monotonic()
    shown = await until(lambda: status(phone), lambda s: s["escape"], wait_s=5)
    assert shown["escape"]
    assert time.monotonic() - loaded < 1.5, "it waited for the 3 s watchdog"
    (error,) = phone.errors
    assert "broken" in error
    phone.errors.clear()


async def test_a_working_viewer_never_shows_the_escape_bar(phone: Viewer, checkout: Edge) -> None:
    await open_viewer(phone, checkout)
    session = await checkout.agent.session(0)
    await session.start()
    await shown_image(phone, session, RED, dsf=2, first=True)
    await asyncio.sleep(3.5)
    assert not (await status(phone))["escape"]
