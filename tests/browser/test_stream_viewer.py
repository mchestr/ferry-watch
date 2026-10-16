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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
LETTERBOX = (213, 219, 227)  # viewer.css's --letterbox, light
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


def shown_as(*, prominent: bool) -> Callable[[Any], bool]:
    """A status reading that is shown, prominently or not."""
    return lambda s: bool(s["shown"]) and s["prominent"] == prominent


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
        # The viewer hears of the refusal a moment after the agent made it.
        prominent = count == 3
        shown = await until(lambda: status(phone), shown_as(prominent=prominent))
        assert shown["title"] == "Reconnecting…"
        assert shown["classic"]
        assert shown["prominent"] == prominent
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
        shown = await until(lambda: status(phone), shown_as(prominent=prominent))
        assert shown["title"] == "Reconnecting…"
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


# ---- input: touches, clicks, the keyboard proxy, the native pickers (viewer tests 1-8)

SETTLE_S = 0.25  # for a message to the viewer to be taken


def text_field(fid: int, r: list[float], **values: Any) -> dict[str, Any]:
    """A field of a field map (PROTOCOL.md §5.3.1), a text input unless `values` say otherwise."""
    field = {"id": fid, "k": "text", "r": r, "c": r, "m": 511, "type": "text", "im": "", "ac": "", "ekh": "", "cap": ""}
    return {**field, "max": None, "ro": False, **values}


async def send_map(session: Session, fields: list[dict[str, Any]], *, seq: int, rev: int = 1, **map_: Any) -> None:
    """A field map taken at image `seq`, after everything the viewer has sent (unless `n` says otherwise)."""
    msg = {"t": "fields", "page": 1, "rev": rev, "seq": seq, "n": session.number, "sx": 0, "sy": 0, "scale": 1}
    msg |= {"cw": 390, "ch": 3000, "trunc": False, "f": fields, **map_}
    session.fields.update({field["id"]: field["k"] for field in fields})
    await session.send(msg)
    await asyncio.sleep(SETTLE_S)


async def cdp(viewer: Viewer) -> Any:
    if not hasattr(viewer, "_cdp"):
        viewer._cdp = await viewer.page.context.new_cdp_session(viewer.page)  # type: ignore[attr-defined]
    return viewer._cdp  # type: ignore[attr-defined]


async def touch(viewer: Viewer, kind: str, points: Sequence[tuple[float, float]]) -> None:
    """A real touch event: `kind` touchStart, touchMove or touchEnd, with the fingers down after it."""
    session = await cdp(viewer)
    touches = [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(points)]
    await session.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": touches})


async def tap(viewer: Viewer, x: float, y: float, ms: int = 60) -> None:
    await touch(viewer, "touchStart", [(x, y)])
    await asyncio.sleep(ms / 1000)
    await touch(viewer, "touchEnd", [])


async def drag(viewer: Viewer, path: Sequence[tuple[float, float]], step_ms: int = 16) -> None:
    """One finger along `path`, a point every `step_ms`, lifted at the last."""
    await touch(viewer, "touchStart", [path[0]])
    for point in path[1:]:
        await asyncio.sleep(step_ms / 1000)
        await touch(viewer, "touchMove", [point])
    await touch(viewer, "touchEnd", [])


def sent(session: Session, t: str) -> list[dict[str, Any]]:
    return [m for m in session.received if m["t"] == t]


async def watch_touchend(viewer: Viewer) -> None:
    """Records, inside each touchend's own task, what has focus and the keyboard proxy's attributes."""
    await viewer.evaluate(
        """() => {
          window.inTouchend = [];
          addEventListener("touchend", () => {
            const proxy = document.getElementById("proxy");
            const attributes = Object.fromEntries([...proxy.attributes].map((a) => [a.name, a.value]));
            inTouchend.push({ focused: document.activeElement.id, attributes });
          });
        }"""
    )


async def in_touchend(viewer: Viewer) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = await viewer.evaluate("() => inTouchend")
    return seen


async def started(viewer: Viewer, checkout: Edge, colour: tuple[int, int, int] = RED) -> tuple[Session, int]:
    """The viewer open on a session that has drawn one full-size image; that session and the image's seq."""
    await open_viewer(viewer, checkout)
    session = await checkout.agent.session(0)
    await session.start()
    seq = await shown_image(viewer, session, colour, dsf=2 if viewer.device == "phone" else 1, first=True)
    await until(lambda: status(viewer), lambda s: not s["shown"])
    return session, seq


async def test_a_tap_is_sent_with_the_image_it_was_on(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 2: `tap` carries the seq of the image drawn when it ended, the point in its frame px, and how
    long the finger was down."""
    session, first = await started(phone, checkout)
    await tap(phone, 120, 300, ms=80)
    (sent_tap,) = await session.wait_for("tap")
    assert {k: sent_tap[k] for k in ("page", "seq", "x", "y")} == {"page": 1, "seq": first, "x": 120, "y": 300}
    assert 60 <= sent_tap["ms"] <= 300
    second = await shown_image(phone, session, BLUE, dsf=2, slot=1)
    await tap(phone, 200.5, 10)
    taps = await session.wait_for("tap", count=2)
    assert (taps[1]["seq"], taps[1]["x"], taps[1]["y"]) == (second, 200.5, 10)


async def test_taps_on_a_letterboxed_image_are_mapped_into_it(browser: Browser, checkout: Edge) -> None:
    """Viewer test 1: on a 2560x1440 screen the 1920-wide image is centred; a click is a tap in its frame px, a click
    on the letterbox sends nothing, and the mouse sends `move` while over the image."""
    async with device_viewer(browser, "wide") as wide:
        session, seq = await started(wide, checkout)
        await wide.page.mouse.move(100, 700)
        await wide.page.mouse.down()
        await wide.page.mouse.up()
        await wide.page.mouse.move(1280, 700, steps=4)
        await wide.page.mouse.down()
        await asyncio.sleep(0.09)
        await wide.page.mouse.up()
        (sent_tap,) = await session.wait_for("tap")
        assert {k: sent_tap[k] for k in ("seq", "x", "y")} == {"seq": seq, "x": 960, "y": 700}
        assert 80 <= sent_tap["ms"] <= 300
        moves = sent(session, "move")
        assert moves
        assert all(0 <= m["x"] <= 1920 for m in moves)
        assert (moves[-1]["x"], moves[-1]["y"]) == (960, 700)


async def test_no_tap_while_a_scroll_prediction_is_drawn(phone: Viewer, checkout: Edge) -> None:
    """Viewer tests 2 and 6: once an image shows the document scrolling, the picture follows the finger ahead of
    the images, and a tap on it isn't sent until it has gone; when the images don't move (an inner element
    scrolled), nothing is drawn ahead and a tap goes at once."""
    session, _ = await started(phone, checkout)
    for confirmed in (True, False):
        before = len(sent(session, "tap"))
        await touch(phone, "touchStart", [(200, 500)])
        for i in range(1, 9):
            await asyncio.sleep(0.016)
            await touch(phone, "touchMove", [(200, 500 - 10 * i)])
        await session.wait_for("wheel", count=1)
        await shown_image(phone, session, RED, dsf=2, slot=1, sy=50 if confirmed else 0)
        for i in range(1, 9):
            await asyncio.sleep(0.016)
            await touch(phone, "touchMove", [(200, 420 - 10 * i)])
        for i in range(1, 6):  # slowly, so that it doesn't coast
            await asyncio.sleep(0.016)
            await touch(phone, "touchMove", [(200, 340 - i)])
        bottom = await pixel(phone, 195, 655)
        await touch(phone, "touchEnd", [])
        await tap(phone, 100, 100, ms=30)
        await asyncio.sleep(0.4)
        if confirmed:
            assert close_to(bottom, (255, 255, 255)), "no prediction was drawn"
            assert len(sent(session, "tap")) == before, "a tap was sent on a predicted picture"
            assert close_to(await pixel(phone, 195, 655), RED), "the prediction stayed"
            await tap(phone, 100, 100, ms=30)
            await session.wait_for("tap", count=before + 1)
        else:
            assert close_to(bottom, RED), "a prediction was drawn though the document didn't move"
            await session.wait_for("tap", count=before + 1)
    wheels = sent(session, "wheel")
    assert all((m["x"], m["y"]) in ((200, 500),) for m in wheels)


async def test_a_drag_scrolls_by_the_fingers_travel_and_a_flick_coasts(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 6: `wheel` from where the finger began, at most one per 33 ms, summing exactly to the travel;
    a flick coasts on and stops by itself; a tap that stops a coast is no tap."""
    session, _ = await started(phone, checkout)
    began = time.monotonic()
    await drag(phone, [(200, 500 - 5 * i) for i in range(41)] + [(200, 300)] * 3, step_ms=16)
    took = time.monotonic() - began
    await asyncio.sleep(0.3)
    wheels = sent(session, "wheel")
    assert sum(m["dy"] for m in wheels) == pytest.approx(200)
    assert sum(m["dx"] for m in wheels) == pytest.approx(0)
    assert len(wheels) <= took / 0.033 + 2
    assert all((m["x"], m["y"]) == (200, 500) for m in wheels)

    # Each move waits here for the viewer's next frame (about 50 ms apart): 25 px a step is a flick.
    before = len(wheels)
    await drag(phone, [(200, 500 - 25 * i) for i in range(9)], step_ms=16)
    await asyncio.sleep(0.3)
    await tap(phone, 100, 100, ms=30)  # stops the coast
    await asyncio.sleep(0.5)
    flick = sent(session, "wheel")[before:]
    assert sum(m["dy"] for m in flick) > 200, "it didn't coast"
    stopped = len(flick)
    await asyncio.sleep(0.4)
    assert len(sent(session, "wheel")) - before == stopped, "the coast went on after the tap"
    assert sent(session, "tap") == []

    before = len(sent(session, "wheel"))
    await drag(phone, [(200, 300 + 25 * i) for i in range(9)], step_ms=16)
    await asyncio.sleep(4)
    coast = sent(session, "wheel")[before:]
    assert sum(m["dy"] for m in coast) < -200
    settled = len(sent(session, "wheel"))
    await asyncio.sleep(0.5)
    assert len(sent(session, "wheel")) == settled, "the coast never stopped"


async def test_two_fingers_zoom_the_picture_then_send_one_pinch(phone: Viewer, checkout: Edge) -> None:
    """Viewer tests 2 and 7: the picture zooms under the fingers, then one `pinch` with the start and end
    centroids and the scale; a tap on the zoomed picture isn't sent until an image at the new scale comes."""
    session, seq = await started(phone, checkout)
    await touch(phone, "touchStart", [(150, 300), (250, 300)])
    for step in range(1, 6):
        await asyncio.sleep(0.016)
        await touch(phone, "touchMove", [(150 + 5 * step, 300 + 4 * step), (250 - 5 * step, 300 + 4 * step)])
    corner = await pixel(phone, 5, 5)
    await touch(phone, "touchEnd", [])
    (pinch,) = await session.wait_for("pinch")
    assert pinch == {"t": "pinch", "page": 1, "seq": seq, "x0": 200, "y0": 300, "x1": 200, "y1": 320, "scale": 0.5}
    assert close_to(corner, (255, 255, 255)), "the picture wasn't zoomed out"
    await tap(phone, 100, 100, ms=30)
    await asyncio.sleep(0.2)
    assert sent(session, "tap") == []
    second = await shown_image(phone, session, BLUE, dsf=2, slot=1, scale=1.5)
    await tap(phone, 100, 100, ms=30)
    (sent_tap,) = await session.wait_for("tap")
    assert sent_tap["seq"] == second
    assert len(sent(session, "pinch")) == 1


FIELDS = [
    text_field(1, [20, 40, 350, 40], ac="given-name"),
    text_field(2, [20, 100, 350, 40], type="email", ac="email", im="email"),
    text_field(3, [20, 160, 350, 40], type="tel", ac="tel"),
    text_field(4, [20, 220, 350, 40], im="numeric", ac="cc-number"),
    text_field(5, [20, 280, 350, 40], type="password", ac="current-password"),
    text_field(6, [20, 340, 350, 40], ac="username email", ekh="next"),
    text_field(7, [20, 400, 350, 40], type="number"),
    text_field(8, [20, 460, 350, 80], k="area", type="textarea"),
]
PROXIES = {
    1: {"type": "text", "autocomplete": "given-name", "autocapitalize": "words"},
    2: {"type": "email", "inputmode": "email", "autocomplete": "email", "autocapitalize": "off"},
    3: {"type": "tel", "autocomplete": "tel", "autocapitalize": "off"},
    4: {"type": "text", "inputmode": "numeric", "autocomplete": "cc-number", "autocapitalize": "off"},
    5: {"type": "text", "autocomplete": "off", "autocapitalize": "off"},
    6: {"type": "text", "autocomplete": "email", "autocapitalize": "off", "enterkeyhint": "next"},
    7: {"type": "text", "inputmode": "decimal", "autocomplete": "off", "autocapitalize": "off"},
    8: {"type": "text", "autocomplete": "off", "autocapitalize": "sentences"},
}
CREDENTIAL_TOKENS = {"username", "current-password", "new-password", "webauthn"}


async def test_a_tap_on_a_field_opens_the_right_keyboard_inside_the_tap(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 3: on a usable map the proxy has focus within the `touchend` that tapped the field, with the
    attributes for its kind; a password field's proxy is plain text; no credential token ever reaches it."""
    session, seq = await started(phone, checkout)
    await send_map(session, FIELDS, seq=seq)
    await watch_touchend(phone)
    for field in FIELDS:
        x, y, w, h = field["r"]
        await tap(phone, x + w / 2, y + h / 2)
        seen = (await in_touchend(phone))[-1]
        assert seen["focused"] == "proxy", field["id"]
        attributes = seen["attributes"]
        expected = PROXIES[field["id"]]
        for name in ("type", "inputmode", "autocomplete", "autocapitalize", "enterkeyhint"):
            assert attributes.get(name) == expected.get(name), (field["id"], name)
        assert (attributes["autocorrect"], attributes["spellcheck"]) == ("off", "false")
        assert not CREDENTIAL_TOKENS & set(attributes["autocomplete"].split())
        # Every tap went to the pod as well, and the map is out of date until the agent says it has seen it.
        await session.wait_for("tap", count=field["id"])
        await send_map(session, FIELDS, seq=seq)
    await asyncio.sleep(0.1)
    assert await phone.evaluate("document.activeElement.id") == "proxy"


async def test_a_map_out_of_date_opens_no_keyboard_but_the_tap_goes(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 3: a map taken before the user's latest input, or five images before the drawn one, isn't used:
    no keyboard opens, and the tap is sent; the agent's focus report then offers "Tap to type"."""
    session, seq = await started(phone, checkout)
    await watch_touchend(phone)
    await tap(phone, 10, 600)
    await session.wait_for("tap")
    await send_map(session, FIELDS, seq=seq, n=session.number - 1)  # before that tap
    await tap(phone, 195, 60)
    await session.wait_for("tap", count=2)
    assert (await in_touchend(phone))[-1]["focused"] != "proxy"

    images = [await shown_image(phone, session, RED, dsf=2, slot=1) for _ in range(5)]
    latest = images[-1]
    await send_map(session, FIELDS, seq=latest - 5)
    await tap(phone, 195, 60)
    await session.wait_for("tap", count=3)
    assert (await in_touchend(phone))[-1]["focused"] != "proxy"

    focus = {k: v for k, v in FIELDS[0].items() if k != "id"}
    await session.send({"t": "focus", "page": 1, "seq": latest, "n": session.number, "f": focus, "popup": False})
    assert await until(lambda: phone.page.is_visible("#type-chip"), bool)
    await phone.page.tap("#type-chip")
    assert await phone.evaluate("document.activeElement.id") == "proxy"
    assert not await phone.page.is_visible("#type-chip")
    assert await phone.page.get_attribute("#proxy", "autocomplete") == "given-name"


async def test_the_phones_keyboard_doesnt_hide_the_field(phone: Viewer, checkout: Edge) -> None:
    """When the phone's keyboard comes up over a field, the pod's page is scrolled to show the field above it, as a
    browser would scroll its own page."""
    session, seq = await started(phone, checkout)
    await send_map(session, FIELDS, seq=seq)
    await tap(phone, 195, 500)  # the textarea, 460 to 540
    await session.wait_for("tap")
    await phone.evaluate(
        """() => {
          Object.defineProperty(visualViewport, "height", { get: () => 300, configurable: true });
          visualViewport.dispatchEvent(new Event("resize"));
          visualViewport.dispatchEvent(new Event("resize"));
        }"""
    )
    (wheel,) = await session.wait_for("wheel")
    assert wheel == {"t": "wheel", "page": 1, "x": 195, "y": 500, "dx": 0, "dy": 540 - (300 - 16)}
    await asyncio.sleep(0.2)
    assert len(sent(session, "wheel")) == 1, "once for each time the keyboard comes up"


async def test_taps_and_drags_on_a_focused_field_reach_the_pod(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 4: the proxy takes no touches, so a second tap in the field moves the pod's caret (a `tap`) and
    keeps the keyboard, and a drag that starts on it scrolls."""
    session, seq = await started(phone, checkout)
    await send_map(session, FIELDS, seq=seq)
    await tap(phone, 100, 60)
    await session.wait_for("tap")
    assert await phone.evaluate("document.activeElement.id") == "proxy"
    await send_map(session, FIELDS, seq=seq)
    await tap(phone, 300, 60)
    taps = await session.wait_for("tap", count=2)
    assert (taps[1]["x"], taps[1]["y"]) == (300, 60)
    assert await phone.evaluate("document.activeElement.id") == "proxy"
    await drag(phone, [(100, 60 + 6 * i) for i in range(12)], step_ms=30)
    await session.wait_for("wheel")
    await asyncio.sleep(0.2)
    assert sum(m["dy"] for m in sent(session, "wheel")) == pytest.approx(-66)
    assert len(sent(session, "tap")) == 2


async def test_what_the_phone_types_goes_to_the_pod(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 5: a key typed is `key {c}`; pasted text is normalised and split as text.json says; an IME
    composition is `compose` then `text`; Backspace and Enter are keys; nothing typed stays in the proxy."""
    session, seq = await started(phone, checkout)
    await send_map(session, FIELDS, seq=seq)
    await tap(phone, 195, 240)  # the card number
    await session.wait_for("tap")
    browser = await cdp(phone)

    before = len(session.received)
    await browser.send("Input.insertText", {"text": "4"})
    await browser.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Backspace", "code": "Backspace"})
    await browser.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace"})
    await browser.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Enter", "code": "Enter"})
    await browser.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter"})
    await browser.send("Input.insertText", {"text": "42"})
    typed = (await session.arrived(before + 4))[before:]
    assert typed == [
        {"t": "key", "c": "4"},
        {"t": "key", "k": "Backspace"},
        {"t": "key", "k": "Enter"},
        {"t": "text", "s": "42"},
    ]
    assert await phone.evaluate("document.getElementById('proxy').value") == ""

    cases = [c for c in pack("text.json")["cases"] if c["as"] == "text" and c["via"] == "paste"]
    for case in cases:
        before = len(session.received)
        await phone.evaluate(
            """(input) => {
              const data = new DataTransfer();
              data.setData("text/plain", JSON.parse(input));
              const event = { inputType: "insertFromPaste", dataTransfer: data, bubbles: true, cancelable: true };
              document.getElementById("proxy").dispatchEvent(new InputEvent("beforeinput", event));
            }""",
            json.dumps(case["input"]),
        )
        if case["messages"]:
            await session.arrived(before + len(case["messages"]))
        await asyncio.sleep(0.05)
        assert session.received[before:] == case["messages"], case["id"]

    before = len(session.received)
    await browser.send("Input.imeSetComposition", {"text": "Ches", "selectionStart": 4, "selectionEnd": 4})
    await browser.send("Input.imeSetComposition", {"text": "Chester", "selectionStart": 7, "selectionEnd": 7})
    await browser.send("Input.insertText", {"text": "Chester"})
    await asyncio.sleep(SETTLE_S)
    assert session.received[before:] == [
        {"t": "compose", "s": "Ches"},
        {"t": "compose", "s": "Chester"},
        {"t": "text", "s": "Chester"},
    ]
    assert await phone.evaluate("document.getElementById('proxy').value") == ""


async def test_a_name_keeps_what_was_typed_for_the_phones_capitals(phone: Viewer, checkout: Edge) -> None:
    """In a name the phone's keyboard capitalises from the text before the caret, so the proxy keeps what was typed
    since the tap (and only there); a new tap starts again."""
    session, seq = await started(phone, checkout)
    await send_map(session, FIELDS, seq=seq)
    await tap(phone, 195, 60)
    browser = await cdp(phone)
    for text in ("M", "i", "k", "e", " "):
        await browser.send("Input.insertText", {"text": text})
    assert await phone.evaluate("document.getElementById('proxy').value") == "Mike "
    await browser.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Backspace", "code": "Backspace"})
    assert await phone.evaluate("document.getElementById('proxy').value") == "Mike"
    await session.wait_for("key", count=6)
    await send_map(session, FIELDS, seq=seq)
    await tap(phone, 300, 60)
    assert await phone.evaluate("document.getElementById('proxy').value") == ""


@pytest.mark.parametrize("device", ["desktop", "wide"])
async def test_a_computers_keys_go_down_and_up(browser: Browser, checkout: Edge, device: str) -> None:
    """Viewer test 5: on a computer, keys are sent as they go down and up, Shift included; shortcuts aren't."""
    async with device_viewer(browser, device) as viewer:
        await computer_keys(viewer, checkout)


async def computer_keys(viewer: Viewer, checkout: Edge) -> None:
    session, _ = await started(viewer, checkout)
    keyboard = viewer.page.keyboard
    await keyboard.down("Shift")
    await keyboard.down("M")
    await keyboard.up("M")
    await keyboard.up("Shift")
    await keyboard.press("Tab")
    await keyboard.press("Control+a")
    await keyboard.press("4")
    keys = await session.wait_for("key", count=8)
    assert keys == [
        {"t": "key", "k": "Shift", "ph": "down"},
        {"t": "key", "c": "M", "ph": "down"},
        {"t": "key", "c": "M", "ph": "up"},
        {"t": "key", "k": "Shift", "ph": "up"},
        {"t": "key", "k": "Tab", "ph": "down"},
        {"t": "key", "k": "Tab", "ph": "up"},
        {"t": "key", "c": "4", "ph": "down"},
        {"t": "key", "c": "4", "ph": "up"},
    ]
    # A key held down repeats as the computer's keyboard repeats it: another `down` each time (Playwright sends a
    # held key's later downs as repeats, as a keyboard does). Shift doesn't repeat.
    await keyboard.down("Shift")
    await keyboard.down("Shift")
    await keyboard.up("Shift")
    await keyboard.down("Backspace")
    for _ in range(2):
        await asyncio.sleep(0.05)
        await keyboard.down("Backspace")
    await keyboard.up("Backspace")
    keys = await session.wait_for("key", count=14)
    backspace = {"t": "key", "k": "Backspace"}
    assert keys[8:] == [
        {"t": "key", "k": "Shift", "ph": "down"},
        {"t": "key", "k": "Shift", "ph": "up"},
        *[backspace | {"ph": "down"}] * 3,
        backspace | {"ph": "up"},
    ]
    # Repeats closer together than 25 ms: the first goes, the rest wait for the next.
    await viewer.evaluate(
        """() => {
          const key = (type, repeat) => document.body.dispatchEvent(
            new KeyboardEvent(type, { key: "ArrowLeft", code: "ArrowLeft", repeat, bubbles: true }));
          key("keydown", false);
          for (let i = 0; i < 5; i++) key("keydown", true);
          key("keyup", false);
        }"""
    )
    keys = await session.wait_for("key", count=17)
    left = {"t": "key", "k": "ArrowLeft"}
    assert keys[14:] == [left | {"ph": "down"}, left | {"ph": "down"}, left | {"ph": "up"}]
    # A key held when the window loses focus is let go.
    await keyboard.down("x")
    await viewer.evaluate("() => dispatchEvent(new Event('blur'))")
    keys = await session.wait_for("key", count=19)
    assert keys[-2:] == [{"t": "key", "c": "x", "ph": "down"}, {"t": "key", "c": "x", "ph": "up"}]
    await keyboard.up("x")


OCCLUSION = next(c for c in pack("hits.json")["cases"] if c["id"] == "occlusion-page")


async def overlays(viewer: Viewer) -> list[dict[str, Any]]:
    shown: list[dict[str, Any]] = await viewer.evaluate(
        """() => [...document.querySelectorAll("#overlays > *")].map((e) => {
          const r = e.getBoundingClientRect();
          return { tag: e.tagName.toLowerCase(), type: e.type, r: [r.x, r.y, r.width, r.height] };
        })"""
    )
    return shown


async def test_native_pickers_lie_over_the_fields_a_usable_map_clears(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 8: a native select or date picker over each select, date or time field the map says nothing
    covers, and nowhere else; what is picked goes as `choose` or `date` with the map's revision; covered cells of
    a field don't open the keyboard."""
    session, seq = await started(phone, checkout)
    fields = OCCLUSION["map"]["f"]
    await send_map(session, fields, seq=seq, rev=3)
    shown = await overlays(phone)
    assert sorted((o["tag"], o["type"], tuple(o["r"])) for o in shown) == [
        ("input", "date", (20, 120, 300, 40)),
        ("select", "select-one", (20, 60, 300, 40)),
        ("select", "select-one", (20, 180, 300, 40)),
    ]
    labels = await phone.evaluate("[...document.querySelector('#overlays select').options].map((o) => o.text)")
    assert labels == ["Pick a terminal", "Victoria", "Vancouver", "Nanaimo"]
    await phone.page.locator("#overlays select").first.select_option(index=2)
    (choose,) = await session.wait_for("choose")
    assert choose == {"t": "choose", "page": 1, "rev": 3, "f": 1, "i": 2}
    await send_map(session, fields, seq=seq, rev=3)
    await phone.page.locator("#overlays input").fill("2026-10-16")
    (date,) = await session.wait_for("date")
    assert date == {"t": "date", "page": 1, "rev": 3, "f": 2, "v": "2026-10-16"}

    # The map is out of date once the user does anything else: the pickers go.
    await tap(phone, 200, 620)
    await session.wait_for("tap")
    assert await until(lambda: overlays(phone), lambda o: o == []) == []

    # A text field whose bottom row is covered: a tap there opens no keyboard.
    await watch_touchend(phone)
    covered = text_field(9, [20, 300, 300, 60], m=0b000111111)
    await send_map(session, [covered], seq=seq, rev=4)
    await tap(phone, 170, 350)
    assert (await in_touchend(phone))[-1]["focused"] != "proxy"
    await send_map(session, [covered], seq=seq, rev=4)
    await tap(phone, 170, 310)
    assert (await in_touchend(phone))[-1]["focused"] == "proxy"


async def test_a_pick_the_agent_couldnt_carry_out_gives_the_field_back_to_the_page(
    phone: Viewer, checkout: Edge
) -> None:
    """`error failed` (or `stale`, `unsupported`) for a picker's `choose` or `date`: the user is told, and that
    field's picker goes for as long as the map's revision lasts, so the next tap there goes to the page itself."""
    session, seq = await started(phone, checkout)
    fields = OCCLUSION["map"]["f"]
    await send_map(session, fields, seq=seq, rev=3)
    await phone.page.locator('#overlays > select[style*="top: 180px"]').select_option(index=1)
    (choose,) = await session.wait_for("choose")
    assert await phone.evaluate("document.getElementById('notice').hidden")
    await session.send({"t": "error", "code": "failed", "ref": "choose"})
    notice = await until(
        lambda: phone.evaluate(
            "(() => { const n = document.getElementById('notice'); return n.hidden ? null : n.textContent; })()"
        ),
        bool,
    )
    assert notice == "That didn't take. Tap the field to try again."
    await send_map(session, fields, seq=seq, rev=3)  # the same map again (fields_ok, say): still none there
    shown = sorted((o["tag"], o["type"], tuple(o["r"])) for o in await overlays(phone))
    assert shown == [("input", "date", (20, 120, 300, 40)), ("select", "select-one", (20, 60, 300, 40))]
    await tap(phone, 170, 200)  # the field: the page's own control gets the tap
    (tapped,) = await session.wait_for("tap")
    assert (tapped["x"], tapped["y"]) == (170, 200)
    await send_map(session, fields, seq=seq, rev=4)  # a new revision: the picker is back
    assert len(await overlays(phone)) == 3


# ---- the sheets: dialogs, the option sheet, the menu (viewer tests 9 and 12)

MARKUP = '<img src=x onerror="document.title = 1">'  # what a page might put in its title, a label or a dialog
PAGES = next(case["msg"] for case in pack("messages.json")["outbound"] if case["id"] == "out-pages")


async def dialog_shown(viewer: Viewer) -> dict[str, Any]:
    shown: dict[str, Any] = await until(
        lambda: viewer.evaluate(
            """() => {
              const $ = (id) => document.getElementById(id);
              return {
                shown: !$("dialog").hidden,
                origin: $("dialog-origin").textContent,
                message: $("dialog-message").textContent,
                input: $("dialog-input").hidden ? null : $("dialog-input").value,
                cancel: !$("dialog-cancel").hidden,
              };
            }"""
        ),
        lambda d: d["shown"],
    )
    return shown


async def test_a_dialog_says_which_page_opened_it_and_takes_the_answer(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 9: the dialog sheet is headed with the origin of the frame that opened the dialog, as the browser
    words it, and sends the user's answer; dialogs wait their turn; one the agent closes goes."""
    session, _ = await started(phone, checkout)
    main_origin = PAGES["pages"][0]["origin"]
    await session.send(
        {
            "t": "dialog",
            "id": 1,
            "page": 1,
            "kind": "alert",
            "origin": main_origin,
            "main": True,
            "msg": "Hi\nthere",
            "def": "",
        }
    )
    await session.send(
        {
            "t": "dialog",
            "id": 2,
            "page": 1,
            "kind": "confirm",
            "origin": "https://pay.example",
            "main": False,
            "msg": "Pay?",
            "def": "",
        }
    )
    shown = await dialog_shown(phone)
    assert shown == {
        "shown": True,
        "origin": f"{main_origin} says:",
        "message": "Hi\nthere",
        "input": None,
        "cancel": False,
    }
    await phone.page.tap("#dialog-ok")
    shown = await until(lambda: dialog_shown(phone), lambda d: d["origin"] != f"{main_origin} says:")
    assert (shown["origin"], shown["message"], shown["cancel"]) == (
        "An embedded page at https://pay.example says:",
        "Pay?",
        True,
    )
    await phone.page.tap("#dialog-cancel")
    answers = await session.wait_for("dialog", count=2)
    assert answers == [{"t": "dialog", "id": 1, "ok": True}, {"t": "dialog", "id": 2, "ok": False}]
    assert await phone.page.is_hidden("#dialog")

    # A frame inside the page whose URL gives no origin (a third party's srcdoc or data: child) is never the page.
    await session.send(
        {"t": "dialog", "id": 3, "page": 1, "kind": "alert", "origin": "", "main": False, "msg": "Lure", "def": ""}
    )
    shown = await dialog_shown(phone)
    assert (shown["origin"], shown["message"]) == ("An embedded page on this page says:", "Lure")
    await phone.page.tap("#dialog-ok")
    await until(lambda: phone.page.is_hidden("#dialog"), bool)
    await session.wait_for("dialog", count=3)

    await session.send(
        {
            "t": "dialog",
            "id": 4,
            "page": 1,
            "kind": "prompt",
            "origin": "",
            "main": True,
            "msg": "Code?",
            "def": "FERRY10",
        }
    )
    shown = await dialog_shown(phone)
    assert (shown["origin"], shown["input"]) == ("This page says:", "FERRY10")
    attributes = await phone.evaluate(
        "Object.fromEntries([...document.getElementById('dialog-input').attributes].map((a) => [a.name, a.value]))"
    )
    assert attributes["type"] == "text"
    assert (attributes["autocomplete"], attributes["autocapitalize"], attributes["spellcheck"]) == (
        "off",
        "off",
        "false",
    )
    await phone.page.fill("#dialog-input", "SAVE\t5")
    await phone.page.tap("#dialog-ok")
    answers = await session.wait_for("dialog", count=4)
    assert answers[3] == {"t": "dialog", "id": 4, "ok": True, "s": "SAVE5"}

    await session.send(
        {
            "t": "dialog",
            "id": 5,
            "page": 1,
            "kind": "beforeunload",
            "origin": main_origin,
            "main": True,
            "msg": "",
            "def": "",
        }  # fmt: skip
    )
    shown = await dialog_shown(phone)
    assert await phone.page.text_content("#dialog-ok") == "Leave"
    await session.send({"t": "dialog_closed", "id": 5})
    assert await until(lambda: phone.page.is_hidden("#dialog"), bool)
    assert len(sent(session, "dialog")) == 4


async def test_a_computer_answers_a_dialog_from_the_keyboard(browser: Browser, checkout: Edge) -> None:
    async with device_viewer(browser, "desktop") as desktop:
        session, _ = await started(desktop, checkout)
        await session.send(
            {
                "t": "dialog",
                "id": 1,
                "page": 1,
                "kind": "confirm",
                "origin": "",
                "main": True,
                "msg": "Sure?",
                "def": "",
            }  # fmt: skip
        )
        await dialog_shown(desktop)
        await desktop.page.keyboard.press("Escape")
        await session.send(
            {"t": "dialog", "id": 2, "page": 1, "kind": "prompt", "origin": "", "main": True, "msg": "", "def": ""}
        )
        await dialog_shown(desktop)
        await desktop.page.keyboard.type("OK10")
        await desktop.page.keyboard.press("Enter")
        answers = await session.wait_for("dialog", count=2)
        assert answers == [{"t": "dialog", "id": 1, "ok": False}, {"t": "dialog", "id": 2, "ok": True, "s": "OK10"}]
        assert sent(session, "key") == [], "the keys were the dialog's, not the checkout's"


SELECT = text_field(
    3,
    [20, 300, 350, 40],
    k="select",
    type="select",
    nopt=4,
    opts=[["Province", True], ["British Columbia", False], ["Alberta", False], ["Yukon", False]],
    sel=0,
    trunc=False,
)


async def options_shown(viewer: Viewer) -> list[list[Any]]:
    shown: list[list[Any]] = await until(
        lambda: viewer.evaluate(
            """() => document.getElementById("options").classList.contains("open")
              ? [...document.querySelectorAll("#options-list .option")].map((b) => [
                  b.textContent.trim(), b.disabled, b.getAttribute("aria-selected") === "true"])
              : []"""
        ),
        bool,
    )
    return shown


async def test_a_select_the_pod_opened_gets_the_option_sheet(phone: Viewer, checkout: Edge) -> None:
    """PROTOCOL.md §8.6: when the agent's click opened a select's list in the pod (`focus.popup`), the viewer
    shows the options itself and sends `choose` for the map's select; closing the sheet closes the pod's list."""
    session, seq = await started(phone, checkout)
    await send_map(session, [SELECT], seq=seq, rev=2)
    focus = {k: v for k, v in SELECT.items() if k != "id"}
    await session.send({"t": "focus", "page": 1, "seq": seq, "n": session.number, "f": focus, "popup": True})
    assert await options_shown(phone) == [
        ["Province", True, True],
        ["British Columbia", False, False],
        ["Alberta", False, False],
        ["Yukon", False, False],
    ]
    await phone.page.tap("#options-list li:nth-child(3) .option")
    (choose,) = await session.wait_for("choose")
    assert choose == {"t": "choose", "page": 1, "rev": 2, "f": 3, "i": 2}

    await send_map(session, [SELECT], seq=seq, rev=2)
    await session.send({"t": "focus", "page": 1, "seq": seq, "n": session.number, "f": focus, "popup": True})
    await options_shown(phone)
    await asyncio.sleep(0.35)
    await phone.page.tap("#options .close")
    (escape,) = await session.wait_for("key")
    assert escape == {"t": "key", "k": "Escape"}

    long = {**focus, "nopt": 1200, "opts": focus["opts"][:2], "trunc": True}
    await session.send({"t": "focus", "page": 1, "seq": seq, "n": session.number, "f": long, "popup": True})
    assert len(await options_shown(phone)) == 2
    assert await phone.page.is_visible("#options-more")


async def test_the_menu_shows_the_time_left_and_the_checkouts_pages(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 9: the menu's handle counts the open pages; the menu shows the time left and the pages, brings
    one to the front or closes it (never the main one)."""
    session, _ = await started(phone, checkout)
    assert await phone.page.text_content("#menu-badge") == "2"
    await session.send({"t": "clock", "ends": time.time() + 605, "hard": time.time() + 3000, "paused": False})
    await phone.page.tap("#menu-handle")
    await until(lambda: phone.evaluate("document.getElementById('menu').classList.contains('open')"), bool)
    clock = await phone.page.text_content("#menu-clock")
    assert clock in ("10:04 left", "10:03 left", "10:05 left"), clock
    rows = await phone.evaluate(
        """[...document.querySelectorAll("#menu-pages .page")].map((row) => [
          row.querySelector(".page-title").textContent,
          row.querySelector(".page-origin").textContent,
          row.getAttribute("aria-current"),
          Boolean(row.querySelector(".page-close")),
        ])"""
    )
    assert rows == [
        ["Checkout | BC Ferries", "https://www.bcferries.com", "page", False],
        ["https://pay.example:8443", "", None, True],
    ]
    await phone.page.tap("#menu-pages .page:nth-child(2) .page-close")
    await phone.page.tap("#menu-pages .page:nth-child(2) .page-show")
    pages = await session.wait_for("page", count=2)
    assert pages == [{"t": "page", "op": "close", "page": 2}, {"t": "page", "op": "show", "page": 2}]
    assert await until(lambda: phone.page.is_hidden("#menu"), bool)

    await session.send({"t": "clock", "ends": None, "hard": time.time() + 3000, "paused": True})
    await phone.page.tap("#menu-handle")
    assert await until(lambda: phone.page.text_content("#menu-clock"), lambda t: t == "Clock paused") == "Clock paused"
    assert "payment page" in (await phone.page.text_content("#menu-deadline") or "")
    await phone.page.tap("#menu-classic")
    await phone.page.wait_for_url(f"{checkout.url}{edge.classic_url(touch=True)}")
    assert (await device_record(phone))["viewer_fallback"] is True


async def test_the_menus_paste_types_the_clipboard_into_the_checkout(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 9: the menu's Paste reads the clipboard inside its own tap and sends it as typed text."""
    await phone.page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=checkout.url)
    session, _ = await started(phone, checkout)
    await phone.evaluate("navigator.clipboard.writeText('4242 4242 4242 4242\\r\\n')")
    await phone.page.tap("#menu-handle")
    await until(lambda: phone.evaluate("document.getElementById('menu').classList.contains('open')"), bool)
    await phone.page.tap("#menu-paste")
    (text,) = await session.wait_for("text")
    assert text == {"t": "text", "s": "4242 4242 4242 4242\n"}
    assert await until(lambda: phone.page.is_hidden("#menu"), bool)


async def test_what_the_checkouts_pages_say_is_shown_as_text(phone: Viewer, checkout: Edge) -> None:
    """Viewer test 12: a dialog's message and origin, a page's title and an option's label holding markup are
    shown as the text they are, under a policy that would refuse the markup's script anyway."""
    session, seq = await started(phone, checkout)
    pages = {"t": "pages", "front": 1, "pages": [{**PAGES["pages"][0], "title": MARKUP}, PAGES["pages"][1]]}
    await session.send(pages)
    select = {**SELECT, "opts": [[MARKUP, False], ["Yukon", False]], "nopt": 2}
    await send_map(session, [select], seq=seq, rev=5)
    focus = {k: v for k, v in select.items() if k != "id"}
    await session.send({"t": "focus", "page": 1, "seq": seq, "n": session.number, "f": focus, "popup": True})
    assert (await options_shown(phone))[0][0] == MARKUP
    await asyncio.sleep(0.35)
    await phone.page.tap("#options .close")
    await session.send(
        {
            "t": "dialog",
            "id": 1,
            "page": 1,
            "kind": "alert",
            "origin": MARKUP[:100],
            "main": False,
            "msg": MARKUP,
            "def": "",
        }  # fmt: skip
    )
    shown = await dialog_shown(phone)
    assert shown["message"] == MARKUP
    assert shown["origin"] == f"An embedded page at {MARKUP[:100]} says:"
    await phone.page.tap("#dialog-ok")
    await phone.page.tap("#menu-handle")
    title = await until(lambda: phone.page.text_content("#menu-pages .page-title"), lambda t: t == MARKUP)
    assert title == MARKUP
    assert await phone.evaluate("document.querySelectorAll('img').length") == 0
    assert await phone.evaluate("document.title") == "Ferry Watch checkout"


async def test_the_proxy_follows_the_pods_focus(viewer: Viewer, checkout: Edge) -> None:
    """PROTOCOL.md §5.4: when the page moves the focus itself (a card number on to its expiry), the proxy takes the
    new field's keyboard; when nothing editable has focus, the keyboard goes. A computer's proxy takes focus from
    the reports alone, with no tap."""
    session, seq = await started(viewer, checkout)
    card, expiry = ({k: v for k, v in FIELDS[3].items() if k != "id"}, {**FIELDS[1], "ac": "cc-exp", "im": "numeric"})
    expiry.pop("id")
    if viewer.device == "phone":
        await send_map(session, FIELDS, seq=seq)
        await tap(viewer, 195, 240)
        await session.wait_for("tap")
    focus = {"t": "focus", "page": 1, "seq": seq, "popup": False}
    await session.send({**focus, "n": session.number, "f": card})
    assert await until(lambda: viewer.evaluate("document.activeElement.id"), lambda a: a == "proxy") == "proxy"
    assert await viewer.page.get_attribute("#proxy", "autocomplete") == "cc-number"
    await session.send({**focus, "n": session.number, "f": expiry})
    autocomplete = await until(lambda: viewer.page.get_attribute("#proxy", "autocomplete"), lambda a: a == "cc-exp")
    assert autocomplete == "cc-exp"
    assert await viewer.evaluate("document.activeElement.id") == "proxy"
    await session.send({**focus, "n": session.number, "f": None})
    assert await until(lambda: viewer.evaluate("document.activeElement.id"), lambda a: a != "proxy") != "proxy"
    assert not await viewer.page.is_visible("#type-chip")
