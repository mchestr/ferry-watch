"""The midpoint integration checkpoint (docs/multi-tenant/streamed-viewer/DESIGN.md §11), kept as a regression test.

Everything real: the pod's screen and Chromium (test_checkout_stream.py's), the gate with the stream session
behind it, served where nginx proxies the checkout's socket by vnc-edge's site (test_checkout_edge.py's), and the
viewer's own files under their policy in a phone, a computer and a 2560x1440 window (test_stream_viewer.py's
devices). The viewer is driven as a person would drive it: real touches, clicks, the keyboard proxy, the native
pickers. An init script records what crosses the viewer's socket, without changing what the viewer does, so the
tests can wait for what it has drawn and see when it sent what.

Needs what both of those files need: FW_KASMVNC_WWW, and Xvnc, matchbox-window-manager, xrandr and xsettingsd on
PATH.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from playwright.async_api import Browser

from tests.browser import test_checkout_edge as edge
from tests.browser import test_checkout_stream as stream
from tests.browser import test_stream_viewer as viewers
from tests.browser.test_checkout_stream import Pod, Rig, Sites, frame_rect, middle
from tests.browser.test_stream_viewer import Viewer, drag, tap, touch

pytestmark = [pytest.mark.browser, pytest.mark.timeout(180)]

site = edge.site
browser = edge.browser
pod = stream.pod
sites = stream.sites

WAIT_S = 10.0

# What crosses the viewer's socket, kept in window.__wire: every JSON message each way, with Date.now(), and every
# image's header. Only on viewer.html (the shim on vnc.html wraps WebSocket itself). It observes and changes nothing.
WIRE = """(() => {
  if (location.pathname !== "/fw/viewer.html") return;
  const wire = { sockets: 0, sent: [], got: [], images: [] };
  Object.defineProperty(window, "__wire", { value: wire });
  const Native = WebSocket;
  const send = Native.prototype.send;
  Native.prototype.send = function (data) {
    if (typeof data === "string") wire.sent.push({ socket: this.__socket, at: Date.now(), msg: JSON.parse(data) });
    return send.call(this, data);
  };
  window.WebSocket = class extends Native {
    constructor(url, protocols) {
      super(url, protocols);
      const socket = ++wire.sockets;
      Object.defineProperty(this, "__socket", { value: socket });
      this.addEventListener("message", (event) => {
        const at = Date.now();
        if (typeof event.data === "string") {
          wire.got.push({ socket, at, msg: JSON.parse(event.data) });
          return;
        }
        if (event.data.byteLength < 32) return;
        const v = new DataView(event.data);
        wire.images.push({
          socket, at, kind: v.getUint8(0), page: v.getUint16(2, true), seq: v.getUint32(4, true),
          w: v.getUint16(8, true), h: v.getUint16(10, true), scale: v.getFloat32(12, true),
          sx: v.getFloat32(16, true), sy: v.getFloat32(20, true), dsf: v.getUint8(28), flags: v.getUint8(29),
        });
      });
    }
  };
})();"""

# The message types a field map must have caught up with (PROTOCOL.md §8.3).
INPUTS = {"tap", "wheel", "pinch", "key", "text", "compose", "choose", "date", "dialog", "page", "resize"}


@dataclass
class Checkout:
    rig: Rig
    offers: list[list[str]]

    @property
    def url(self) -> str:
        return self.rig.origin

    @property
    def pod(self) -> Pod:
        return self.rig.pod

    @property
    def sites(self) -> Sites:
        return self.rig.sites

    async def tab(self, script: str, arg: Any = None) -> Any:
        return await self.rig.pod.tab.evaluate(script, arg)


@pytest.fixture
async def checkout(pod: Pod, sites: Sites, site: edge.Site) -> AsyncIterator[Checkout]:
    """The pod's gate behind vnc-edge's site, where nginx proxies the checkout's socket."""
    offers: list[list[str]] = []
    path = f"/t/{edge.TRIP}/g/{edge.GEN}/ws"
    async with stream.make_rig(pod, sites, app=lambda gate: edge.edge_app(site, gate, offers), path=path) as rig:
        yield Checkout(rig, offers)


@contextlib.asynccontextmanager
async def device(browser: Browser, name: str) -> AsyncIterator[Viewer]:
    """A tab of this device's size with the socket recorder; it may not break the viewer's policy or throw."""
    async with viewers.device_viewer(browser, name) as viewer:
        await viewer.page.context.add_init_script(WIRE)
        yield viewer


async def until(read: Callable[[], Awaitable[Any]], done: Callable[[Any], bool], wait_s: float = WAIT_S) -> Any:
    return await viewers.until(read, done, wait_s)


async def wire(viewer: Viewer) -> dict[str, Any]:
    """What crossed the viewer's latest socket so far."""
    found: dict[str, Any] = await viewer.evaluate(
        """() => {
          const w = window.__wire, s = w.sockets;
          return {
            socket: s,
            sent: w.sent.filter((m) => m.socket === s),
            got: w.got.filter((m) => m.socket === s),
            images: w.images.filter((m) => m.socket === s),
          };
        }"""
    )
    return found


def drawn(seen: dict[str, Any]) -> dict[str, Any] | None:
    """The header of the image the viewer drew last (it acks each after drawing it)."""
    acks = [m["msg"]["seq"] for m in seen["sent"] if m["msg"]["t"] == "ack"]
    if not acks:
        return None
    return next((i for i in seen["images"] if i["seq"] == acks[-1]), None)


def last_input(seen: dict[str, Any]) -> int:
    """The number of the viewer's latest message a field map must have caught up with (PROTOCOL.md §1, §8.3)."""
    number, last = 0, 0
    for sent in seen["sent"]:
        if sent["msg"]["t"] == "ack":
            continue
        number += 1
        if sent["msg"]["t"] in INPUTS:
            last = number
    return last


def usable_map(seen: dict[str, Any]) -> dict[str, Any] | None:
    """The field map, if it is usable for the drawn image (PROTOCOL.md §8.3; no prediction or zoom is drawn when a
    test asks)."""
    image = drawn(seen)
    latest: dict[str, Any] | None = None
    for got in seen["got"]:
        msg = got["msg"]
        if msg["t"] == "fields":
            latest = dict(msg)
        elif msg["t"] == "fields_ok" and latest is not None and msg["rev"] == latest["rev"]:
            latest.update(seq=msg["seq"], n=msg["n"])
    if image is None or latest is None:
        return None
    usable = (
        latest["page"] == image["page"]
        and abs(latest["sx"] - image["sx"]) <= 0.5
        and abs(latest["sy"] - image["sy"]) <= 0.5
        and abs(latest["scale"] - image["scale"]) <= 0.001
        and abs(image["seq"] - latest["seq"]) <= 4
        and latest["n"] >= last_input(seen)
    )
    return latest if usable else None


async def showing(viewer: Viewer, pod: Pod) -> dict[str, Any]:
    """The image the viewer draws, once it shows the pod's page (`pod.tab`) where it is now (scroll and zoom)."""

    async def matching() -> dict[str, Any] | None:
        live = await pod.tab.evaluate("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]")
        image = drawn(await wire(viewer))
        if image is None:
            return None
        same = abs(image["sx"] - live[0]) <= 1 and abs(image["sy"] - live[1]) <= 1
        return image if same and abs(image["scale"] - live[2]) < 0.01 else None

    image: dict[str, Any] | None = await until(matching, bool)
    assert image is not None, "the viewer never drew the page as it is"
    return image


async def field_for(viewer: Viewer, pod: Pod, selector: str, frame: str | None = None) -> dict[str, Any]:
    """The usable map's field for this element of the pod's page (`pod.tab`, or the iframe `frame` in it), once the
    viewer has one: within 2 px of where the element is, in frame px."""

    async def found() -> dict[str, Any] | None:
        map_ = usable_map(await wire(viewer))
        if map_ is None:
            return None
        rect = await frame_rect(pod, selector, frame)
        return next((f for f in map_["f"] if all(abs(a - b) <= 2 for a, b in zip(f["r"], rect, strict=True))), None)

    await showing(viewer, pod)
    field: dict[str, Any] | None = await until(found, bool)
    assert field is not None, f"no usable map has {selector}"
    return field


async def opened(viewer: Viewer, checkout: Checkout, page: str = "index.html") -> dict[str, Any]:
    """The pod at the shop's `page`, and the viewer opened through the landing page with a stream ticket, as Ferry
    Watch opens it. The first image it drew."""
    await checkout.pod.at(f"{checkout.sites.shop}/{page}")
    await viewer.page.goto(f"{checkout.url}{edge.LANDING}#v=s1&t={edge.ticket()}")
    await viewer.page.click("#open")
    await viewer.page.wait_for_url(f"{checkout.url}{edge.STREAM_VIEWER}")
    image = await showing(viewer, checkout.pod)
    await until(lambda: viewers.status(viewer), lambda s: not s["shown"])
    return image


async def pod_focus(checkout: Checkout, frame: str | None = None) -> str:
    """The id of the element with focus in the pod's page, or in the iframe `frame` of it."""
    return await stream.focused(checkout.pod, frame)


async def focus_reaches(checkout: Checkout, element: str, frame: str | None = None) -> None:
    """Waits for the pod to focus `element` (an id), in its page or in the iframe `frame`."""
    found = await until(lambda: pod_focus(checkout, frame), lambda f: f == element)
    assert found == element, f"#{element} never got focus: #{found} has it"


async def value_reaches(read: Callable[[], Awaitable[Any]], expected: Any) -> None:
    """Waits for `read` to give `expected`."""
    found = await until(read, lambda value: value == expected)
    assert found == expected


def now_ms() -> float:
    """Date.now() in the viewer's and the pod's browsers, which share this machine's clock."""
    return time.time() * 1000


# ---- 1. taps into the page and the cross-site card frame; the proxy focused inside touchend


async def test_taps_reach_the_page_and_the_card_frame_and_the_keyboard_opens_inside_the_tap(
    browser: Browser, checkout: Checkout
) -> None:
    async with device(browser, "phone") as phone:
        await opened(phone, checkout)
        await viewers.watch_touchend(phone)

        field = await field_for(phone, checkout.pod, "#name")
        await tap(phone, *middle(field["r"]))
        await focus_reaches(checkout, "name")
        seen = (await viewers.in_touchend(phone))[-1]
        assert seen["focused"] == "proxy", "the proxy wasn't focused inside the touchend"
        assert seen["attributes"]["autocomplete"] == "name"
        assert seen["attributes"]["type"] == "text"

        await checkout.tab("scrollTo(0, 455)")
        field = await field_for(phone, checkout.pod, "#cc", "#card")
        await tap(phone, *middle(field["r"]))
        await focus_reaches(checkout, "cc", "#card")
        assert await pod_focus(checkout) == "card"
        seen = (await viewers.in_touchend(phone))[-1]
        assert seen["focused"] == "proxy"
        assert (seen["attributes"]["autocomplete"], seen["attributes"]["inputmode"]) == ("cc-number", "numeric")

        # What the phone's keyboard types goes into the card frame.
        browser_ = await viewers.cdp(phone)
        for c in "4242":
            await browser_.send("Input.insertText", {"text": c})
        frame = await stream.card(checkout.pod)
        await value_reaches(lambda: frame.input_value("#cc"), "4242")


async def test_a_computers_clicks_and_keys_reach_the_page_and_the_card_frame(
    browser: Browser, checkout: Checkout
) -> None:
    async with device(browser, "desktop") as desktop:
        image = await opened(desktop, checkout)
        assert (image["w"], image["h"], image["dsf"]) == (1280, 800, 1)
        await checkout.tab("window.__log = []")
        await desktop.page.mouse.click(*middle(await frame_rect(checkout.pod, "#name")))
        await focus_reaches(checkout, "name")
        # The keyboard proxy takes the computer's typing once the agent reports the field (no tap needed).
        assert await until(lambda: desktop.evaluate("document.activeElement.id"), lambda f: f == "proxy") == "proxy"
        await desktop.page.keyboard.down("Shift")
        await desktop.page.keyboard.press("M")
        await desktop.page.keyboard.up("Shift")
        await desktop.page.keyboard.type("ike")
        await value_reaches(lambda: checkout.pod.tab.input_value("#name"), "Mike")
        keys = [e[:5] for e in await checkout.tab("window.__log") if e[0] == "keydown"]
        assert keys[:2] == [["keydown", "name", 0, 1, "Shift"], ["keydown", "name", 0, 1, "M"]]

        await checkout.tab("scrollTo(0, 300)")
        await showing(desktop, checkout.pod)
        await desktop.page.mouse.click(*middle(await frame_rect(checkout.pod, "#cvc", "#card")))
        await focus_reaches(checkout, "cvc", "#card")
        await desktop.page.keyboard.type("123")
        await desktop.page.keyboard.press("Shift+Tab")
        await focus_reaches(checkout, "exp", "#card")
        frame = await stream.card(checkout.pod)
        assert await frame.input_value("#cvc") == "123"


# ---- 2. a pinch with a pan, then a tap on the zoomed page


async def pinch(viewer: Viewer, start: list[tuple[float, float]], end: list[tuple[float, float]]) -> None:
    """Two fingers from `start` to `end` in ten moves at 60 Hz, then lifted."""
    await touch(viewer, "touchStart", start)
    for step in range(1, 11):
        await asyncio.sleep(1 / 60)
        points = [
            (a[0] + (b[0] - a[0]) * step / 10, a[1] + (b[1] - a[1]) * step / 10)
            for a, b in zip(start, end, strict=True)
        ]
        await touch(viewer, "touchMove", points)
    await touch(viewer, "touchEnd", [])


async def test_a_tap_after_a_pinch_with_a_pan_lands_on_the_zoomed_field(browser: Browser, checkout: Checkout) -> None:
    async with device(browser, "phone") as phone:
        await opened(phone, checkout)
        # Fingers 100 px apart about (195, 300), then 200 px apart about (195, 260): twice the size, 40 px up.
        point = await checkout.tab("[visualViewport.pageLeft + 195, visualViewport.pageTop + 300]")
        await pinch(phone, [(145, 300), (245, 300)], [(95, 260), (295, 260)])
        sent = [m["msg"] for m in (await wire(phone))["sent"] if m["msg"]["t"] == "pinch"]
        assert len(sent) == 1, sent
        assert (sent[0]["x0"], sent[0]["y0"], sent[0]["x1"], sent[0]["y1"]) == (195, 300, 195, 260)
        assert abs(sent[0]["scale"] - 2) < 0.05

        image = await until(lambda: wire(phone), lambda w: (drawn(w) or {}).get("scale", 1) > 1.9)
        assert drawn(image) is not None
        left, top, scale = await checkout.tab("[visualViewport.pageLeft, visualViewport.pageTop, visualViewport.scale]")
        assert abs(scale - sent[0]["scale"]) < 0.01
        assert abs((point[0] - left) * scale - 195) <= 2
        assert abs((point[1] - top) * scale - 260) <= 2

        # The map's rectangles are the zoomed page's: the postal code field, twice its size, cut by the image.
        field = await field_for(phone, checkout.pod, "#postal")
        assert field["r"][2] == pytest.approx(351 * scale, abs=2)
        assert field["c"][0] == 0
        await viewers.watch_touchend(phone)
        await tap(phone, *middle(field["c"]))
        await focus_reaches(checkout, "postal")
        assert (await viewers.in_touchend(phone))[-1]["focused"] == "proxy", "the zoomed map wasn't usable"


# ---- 3. a paste with a line break into an input and a textarea


async def test_a_pasted_line_break_is_a_space_in_an_input_and_kept_in_a_textarea(
    browser: Browser, checkout: Checkout
) -> None:
    async with device(browser, "phone") as phone:
        await phone.page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=checkout.url)
        await opened(phone, checkout)
        address = "123 Main St\\r\\nVictoria BC\\n"

        # The menu's Paste, into the postal code field the user tapped.
        await tap(phone, *middle((await field_for(phone, checkout.pod, "#postal"))["r"]))
        await focus_reaches(checkout, "postal")
        await phone.evaluate(f"navigator.clipboard.writeText('{address}')")
        await phone.page.tap("#menu-handle")
        await until(lambda: phone.evaluate("document.getElementById('menu').classList.contains('open')"), bool)
        await phone.page.tap("#menu-paste")
        postal = await until(lambda: checkout.pod.tab.input_value("#postal"), bool)
        assert postal == "123 Main St Victoria BC"

        # The browser's own paste into the keyboard proxy, for the notes the user tapped.
        await until(lambda: phone.page.is_hidden("#menu"), bool)
        await tap(phone, *middle((await field_for(phone, checkout.pod, "#notes"))["r"]))
        await focus_reaches(checkout, "notes")
        assert await phone.evaluate("document.activeElement.id") == "proxy"
        await phone.evaluate("navigator.clipboard.writeText('Ramp\\npl\\u00e9ase')")
        await phone.page.keyboard.press("Control+V")
        notes = await until(lambda: checkout.pod.tab.input_value("#notes"), bool)
        assert notes == "Ramp\npléase"
        pasted = [m["msg"] for m in (await wire(phone))["sent"] if m["msg"]["t"] == "text"]
        assert pasted == [{"t": "text", "s": "123 Main St\nVictoria BC\n"}, {"t": "text", "s": "Ramp\npléase"}]


# ---- 4. a drag and its coast at 60 Hz


async def test_a_drag_and_its_coast_scroll_the_page_exactly_and_end_with_the_viewer(
    browser: Browser, checkout: Checkout
) -> None:
    async with device(browser, "phone") as phone:
        await opened(phone, checkout)
        await checkout.tab("window.__scrolls = []; addEventListener('scroll', () => __scrolls.push(Date.now()))")
        before = await checkout.tab("scrollY")
        # A finger 400 px up the screen at 60 Hz, lifted while moving: the viewer coasts on.
        await drag(phone, [(195, 600 - 20 * i) for i in range(21)], step_ms=16)
        await until(lambda: wire(phone), coasted, wait_s=5)
        await asyncio.sleep(0.5)

        wheels = [m for m in (await wire(phone))["sent"] if m["msg"]["t"] == "wheel"]
        gaps = [b["at"] - a["at"] for a, b in itertools.pairwise(wheels)]
        assert min(gaps) >= 30, f"wheels closer than 33 ms apart: {gaps}"
        total = sum(m["msg"]["dy"] for m in wheels)
        assert total > 450, f"it didn't coast: {total} px in all"
        finished = (await checkout.tab("window.__scrolls"))[-1]
        assert finished - wheels[-1]["at"] < 100, f"the page last scrolled {finished - wheels[-1]['at']} ms late"
        scrolled = await checkout.tab("scrollY") - before
        assert abs(scrolled - total) <= 2, f"scrolled {scrolled} CSS px for {total} frame px at page scale 1"
        assert not [m for m in (await wire(phone))["got"] if m["msg"]["t"] == "error"]


def coasted(seen: dict[str, Any]) -> bool:
    """The viewer sent wheels and has sent none for 300 ms: the coast is over."""
    wheels = [m["at"] for m in seen["sent"] if m["msg"]["t"] == "wheel"]
    return bool(wheels) and now_ms() - wheels[-1] > 300


# ---- 5. native pickers over the fields nothing covers; a covered field is never clicked

# A cover over the first select that can't be seen, so nothing on the screen changes: the viewer's map still says
# the select is clear when the user picks from its picker. It logs what it gets.
COVER = """() => {
  const cover = document.createElement("div");
  cover.style.cssText = "position: fixed; left: 0; top: 50px; width: 390px; height: 60px; z-index: 9";
  cover.addEventListener("mousedown", () => log.push("cover mousedown"));
  document.body.append(cover);
}"""


async def settled(viewer: Viewer, quiet_s: float = 1.0) -> dict[str, Any]:
    """What crossed the viewer's socket once no image has come for `quiet_s`, with a usable map."""

    async def calm() -> dict[str, Any] | None:
        seen = await wire(viewer)
        last = max((i["at"] for i in seen["images"]), default=0)
        return seen if now_ms() - last >= quiet_s * 1000 and usable_map(seen) is not None else None

    seen: dict[str, Any] | None = await until(calm, bool)
    assert seen is not None, "the viewer's images never settled with a usable map"
    return seen


async def overlays(viewer: Viewer) -> list[list[str]]:
    """The native pickers laid over the image: tag and top."""
    found: list[list[str]] = await viewer.evaluate(
        "[...document.querySelectorAll('#overlays > *')].map((e) => [e.tagName, e.style.top])"
    )
    return found


async def test_the_pickers_set_clear_fields_and_a_covered_field_is_refused(
    browser: Browser, checkout: Checkout
) -> None:
    async with device(browser, "phone") as phone:
        await opened(phone, checkout, "occl.html")
        await field_for(phone, checkout.pod, "#s3")
        # The selects and the date field clear of the "Pay now" footer get pickers; the half-covered select, the
        # covered one and the date under the footer don't (PROTOCOL.md §8.3).
        clear = [["SELECT", "60px"], ["INPUT", "120px"], ["SELECT", "180px"]]
        assert await until(lambda: overlays(phone), lambda o: o == clear) == clear

        await phone.page.select_option('#overlays > select[style*="top: 60px"]', label="ON")
        await value_reaches(lambda: checkout.tab("document.getElementById('s3').value"), "ON")
        await phone.page.fill('#overlays > input[style*="top: 120px"]', "2026-10-16")
        await phone.evaluate("document.activeElement.blur()")  # the phone's date picker closes
        # The agent types the date's digits one by one: the year is 0002 for a moment.
        await value_reaches(lambda: checkout.tab("document.getElementById('d2').value"), "2026-10-16")
        log = await checkout.tab("log")
        assert "s3 change true ON" in log
        assert "d2 change true 2026-10-16" in log

        # Covered between the map and the pick: the agent finds no point where the select gets the click.
        await checkout.tab("document.activeElement.blur()")
        await settled(phone)
        assert await until(lambda: overlays(phone), lambda o: o == clear) == clear
        await checkout.tab(COVER)
        await asyncio.sleep(0.5)
        assert await overlays(phone) == clear, "the cover changed what the viewer shows"
        await phone.page.select_option('#overlays > select[style*="top: 60px"]', label="AB")
        error = await until(lambda: wire(phone), lambda w: any(m["msg"]["t"] == "error" for m in w["got"]), wait_s=5)
        assert [m["msg"] for m in error["got"] if m["msg"]["t"] == "error"] == [
            {"t": "error", "code": "stale", "ref": "choose"}
        ]
        await asyncio.sleep(0.3)
        assert await checkout.tab("document.getElementById('s3').value") == "ON"
        log = await checkout.tab("log")
        assert "cover mousedown" not in log
        assert not [entry for entry in log if entry.startswith("PAY")], log


# ---- 6. a reconnect changes nothing the page can see


async def test_a_reconnect_from_the_same_phone_keeps_the_page_as_it_was(browser: Browser, checkout: Checkout) -> None:
    async with device(browser, "phone") as phone:
        await opened(phone, checkout, "swap.html")
        await checkout.tab("window.__sizes = []; addEventListener('resize', () => __sizes.push(innerWidth))")
        keys = await viewers.cdp(phone)
        for selector, typed in (("#name", "Mike"), ("#cc", "4242")):
            await tap(phone, *middle((await field_for(phone, checkout.pod, selector))["r"]))
            await focus_reaches(checkout, selector[1:])
            for c in typed:
                await keys.send("Input.insertText", {"text": c})
        values = ["Mike", "4242"]
        read = "[...document.querySelectorAll('input')].map((e) => e.value)"
        await value_reaches(lambda: checkout.tab(read), values)
        first = (await wire(phone))["sent"][0]["msg"]
        renders = await checkout.tab("__renders")
        assert renders[-1][0] == "mobile"

        # The phone comes back to the checkout (a reload: the bank app, a lock screen, a lost connection).
        await phone.page.reload()
        await until(lambda: phone.evaluate("window.__wire.sockets"), lambda s: s == 1)
        opened_at = await phone.evaluate("Date.now()")
        seen = await until(lambda: wire(phone), lambda w: drawn(w) is not None, wait_s=5)
        image = drawn(seen)
        assert image is not None
        assert image["at"] - opened_at < 1000, "the first image after the reconnect was late"
        assert seen["sent"][0]["msg"] == first, "the hello wasn't the same"
        assert not [m for m in seen["sent"] if m["msg"]["t"] == "resize"]
        assert await checkout.tab("__sizes") == []
        assert await checkout.tab("__renders") == renders, "the page re-rendered for another breakpoint"
        assert await checkout.tab(read) == values


# ---- 7. a window wider than any viewport the viewer asks for


async def test_a_wide_window_shows_the_page_letterboxed_and_clicks_land_where_they_should(
    browser: Browser, checkout: Checkout
) -> None:
    async with device(browser, "wide") as wide:
        image = await opened(wide, checkout)
        assert (image["w"], image["h"], image["dsf"]) == (1920, 1440, 1)
        assert await checkout.tab("[innerWidth, innerHeight, devicePixelRatio]") == [1920, 1440, 1]
        left = (2560 - 1920) / 2  # the image is centred, at its own size (PROTOCOL.md §8.1)
        assert viewers.close_to(await viewers.pixel(wide, left / 2, 700), viewers.LETTERBOX)
        assert viewers.close_to(await viewers.pixel(wide, 2560 - left / 2, 700), viewers.LETTERBOX)

        for selector, frame in (("#name", None), ("#cc", "#card")):
            x, y = middle(await frame_rect(checkout.pod, selector, frame))
            await showing(wide, checkout.pod)
            await wide.page.mouse.click(left + x, y)
            await focus_reaches(checkout, selector[1:], frame)
        taps = [m["msg"] for m in (await wire(wide))["sent"] if m["msg"]["t"] == "tap"]
        assert len(taps) == 2
        await wide.page.keyboard.type("4242")
        card = await stream.card(checkout.pod)
        await value_reaches(lambda: card.input_value("#cc"), "4242")
