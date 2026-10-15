"""The stream session (agent/stream.py) over a real WebSocket, against stand-ins for Chromium and Playwright
(tests/castfakes.py), a stand-in screen and dialog keeper, and a client that behaves as PROTOCOL.md §8 says a
viewer must (tests/castclient.py). The pod's real screen is tests/browser/test_checkout_stream.py's."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gc
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from ferry_watch.agent import cast_cdp, cast_frames, stream
from ferry_watch.agent.cast_cdp import Cdp, Worlds
from ferry_watch.agent.cast_fields import FieldRef
from ferry_watch.agent.cast_input import ImageRing, InputMapper
from ferry_watch.agent.cast_protocol import REPEAT, Clock, CloseCode, Wheel
from ferry_watch.agent.dialogs import KeptDialog
from ferry_watch.agent.screen import ScreenError
from ferry_watch.agent.stream import CastPod, CastSession, InboundQueue
from ferry_watch.holds import HoldLimits, HoldState, release_due
from tests.castclient import Viewer
from tests.castfakes import FakeCdp, FakeContext, FakePage, jpeg

PHONE = {"t": "hello", "v": 1, "w": 390, "h": 664, "dpr": 3}
CARD_NUMBER = "4242 4242 4242 4242"


class FakeScreen:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.applied: list[tuple[int, int, int]] = []
        self.current: tuple[int, int, int] | None = None
        self.hold: asyncio.Event | None = None
        self.failing: set[tuple[int, int, int]] = set()

    async def apply(self, w: int, h: int, dsf: int) -> bool:
        if self.hold is not None:
            await self.hold.wait()
        self.applied.append((w, h, dsf))
        if (w, h, dsf) in self.failing:
            self.current = None  # as screen.Screen: half done, to be applied again
            raise ScreenError("xrandr --output failed (1)")
        if self.current == (w, h, dsf):
            return False
        self.current = (w, h, dsf)
        self.page.w, self.page.h, self.page.dpr = w, h, dsf
        return True


@dataclass
class FakeDialogs:
    open: list[tuple[int, KeptDialog]] = field(default_factory=list)
    answers: list[tuple[int, bool, str | None]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    sink: Any = None

    @property
    def open_dialogs(self) -> list[KeptDialog]:
        return [d for _, d in self.open]

    def attach(self, sink: Any) -> list[tuple[int, KeptDialog]]:
        self.sink = sink
        self.log.append("attach")
        return list(self.open)

    def detach(self, sink: Any) -> None:
        self.log.append("detach")
        if self.sink is sink:
            self.sink = None

    async def answer(self, number: int, ok: bool, text: str | None = None) -> bool:
        self.answers.append((number, ok, text))
        return True


def dialog(page: FakePage, kind: str = "confirm", message: str = "Leave checkout?") -> KeptDialog:
    return KeptDialog(cast(Any, page), cast(Any, kind), message, "", cast(Any, None), page.url)


@dataclass
class Rig:
    client: TestClient
    page: FakePage
    context: FakeContext
    screen: FakeScreen
    dialogs: FakeDialogs
    verdict: list[CloseCode | None]
    sessions: list[CastSession]
    closed: list[CastSession]
    failures: list[BaseException]
    viewers: list[Viewer] = field(default_factory=list)

    async def connect(self, hello: dict[str, Any] | None = PHONE, *, ack: bool = True) -> Viewer:
        ws = await self.client.ws_connect("/ws", protocols=["fw.cast1"])
        viewer = Viewer(ws, ack=ack)
        viewer.start()
        self.viewers.append(viewer)
        if hello is not None:
            await viewer.send(hello)
        return viewer

    async def opened(self, hello: dict[str, Any] = PHONE, *, ack: bool = True) -> Viewer:
        """A viewer that has its first image, with the screencast started after it."""
        viewer = await self.connect(hello, ack=ack)
        await viewer.image()
        await eventually(lambda: self.page.sent("Page.startScreencast"))
        return viewer

    @property
    def cdp(self) -> Any:
        """The stream's CDP session onto the main page (the first it opened)."""
        return next(s for s in self.context.sessions if s.page is self.page)

    def frame(self, tag: int, *, scroll: float = 0.0, session: int = 1) -> None:
        """Chromium's screencast sends a frame."""
        self.cdp.emit(
            "Page.screencastFrame",
            {
                "data": base64.b64encode(jpeg(780, 1328, tag)).decode(),
                "metadata": {
                    "deviceWidth": 390,
                    "deviceHeight": 664,
                    "pageScaleFactor": 1,
                    "scrollOffsetX": 0,
                    "scrollOffsetY": scroll,
                    "offsetTop": 0,
                },
                "sessionId": session,
            },
        )


async def eventually(done: Callable[[], Any], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "it didn't happen in time"
        await asyncio.sleep(0.01)


@pytest.fixture
async def rig(aiohttp_client: Callable[..., Any]) -> AsyncIterator[Rig]:
    page = FakePage()
    context = FakeContext(page)
    screen = FakeScreen(page)
    dialogs = FakeDialogs()
    verdict: list[CloseCode | None] = [None]
    sessions: list[CastSession] = []
    closed: list[CastSession] = []
    failures: list[BaseException] = []
    hard = time.time() + 3600
    pod = CastPod(
        target=lambda: (cast(Any, context), cast(Any, page)),
        clock=lambda: Clock(ends=hard - 600, hard=hard, paused=False),
        screen=screen,
        dialogs=dialogs,
    )

    async def handle(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=("fw.cast1",), max_msg_size=4097)
        await ws.prepare(request)
        session = CastSession(ws, lambda: verdict[0], pod)
        sessions.append(session)
        code = 1000
        try:  # what the gate does with a session (gate._serve_cast): run it, then tear it down, whatever happens
            await session.run()
        except Exception as e:  # noqa: BLE001 - the gate closes 4500 for any
            failures.append(e)
            code = CloseCode.INTERNAL
        finally:
            await asyncio.shield(session.close())
            closed.append(session)
            await ws.close(code=code)
        return ws

    app = web.Application()
    app.router.add_get("/ws", handle)
    client: TestClient = await aiohttp_client(app)
    the_rig = Rig(client, page, context, screen, dialogs, verdict, sessions, closed, failures)
    yield the_rig
    for viewer in the_rig.viewers:
        await viewer.ws.close()
    assert failures == [], "a session failed"


def input_calls(page: FakePage) -> list[tuple[str, dict[str, Any]]]:
    return page.sent("Input.")


# ---- opening (§3.2)


async def test_it_opens_with_ready_pages_clock_then_a_seeded_image(rig: Rig) -> None:
    viewer = await rig.opened()
    assert [m["t"] for m in viewer.messages[:3]] == ["ready", "pages", "clock"]
    assert viewer.messages[0] == {"t": "ready", "v": 1, "w": 390, "h": 664, "dsf": 2, "features": []}
    assert viewer.messages[1]["pages"] == [
        {"id": 1, "title": "Checkout", "origin": "https://shop.example", "main": True}
    ]
    header, data = viewer.images[0]
    assert (header.kind, header.seq, header.page, header.w, header.h, header.dsf) == (1, 1, 1, 390, 664, 2)
    assert header.first
    assert data == jpeg(780, 1328, 0)  # the screenshot
    methods = [m for m, _ in rig.page.calls]
    assert methods.index("Page.captureScreenshot") < methods.index("Page.startScreencast")
    assert "Page.enable" not in methods
    assert rig.screen.applied == [(390, 664, 2)]
    start = rig.page.sent("Page.startScreencast")[0][1]
    assert start == {"format": "jpeg", "quality": 50, "everyNthFrame": 1}


@pytest.mark.parametrize(
    ("first", "code"),
    [
        ({"t": "tap", "page": 1, "seq": 1, "x": 1, "y": 1}, 4400),
        ({"t": "hello", "v": 2, "w": 390, "h": 664, "dpr": 3}, 4400),
        ({"t": "hello", "v": 1, "w": 2000, "h": 664, "dpr": 3}, 4400),
        ("not json", 4400),
        (b"\x01\x02", 4400),
    ],
    ids=["tap", "version", "too-wide", "malformed", "binary"],
)
async def test_a_bad_first_message_closes_4400(rig: Rig, first: Any, code: int) -> None:
    viewer = await rig.connect(None)
    if isinstance(first, bytes):
        await viewer.ws.send_bytes(first)
    else:
        await viewer.ws.send_str(first if isinstance(first, str) else json.dumps(first))
    assert await viewer.closed() == code
    assert rig.screen.applied == []


async def test_no_hello_in_time_closes_4408(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream, "HELLO_SECONDS", 0.2)
    viewer = await rig.connect(None)
    assert await viewer.closed() == CloseCode.NO_HELLO


async def test_anything_before_ready_or_a_second_hello_closes_4400(rig: Rig) -> None:
    rig.screen.hold = asyncio.Event()  # the screen is still changing: no ready yet
    viewer = await rig.connect(PHONE)
    await asyncio.sleep(0.1)
    await viewer.send({"t": "visible", "v": False})
    rig.screen.hold.set()
    assert await viewer.closed() == CloseCode.PROTOCOL

    rig.screen.hold = None
    again = await rig.opened()
    await again.send(PHONE)
    assert await again.closed() == CloseCode.PROTOCOL


# ---- value rules, rates and the queue


async def test_value_errors_are_answered_and_the_drop_limit_closes_4429(rig: Rig) -> None:
    viewer = await rig.opened()
    await viewer.send({"t": "key", "k": "F12"})
    assert await viewer.message("error") == {"t": "error", "code": "invalid", "ref": "key"}
    for _ in range(200):
        await viewer.send({"t": "key", "k": "F12"})
    assert await viewer.closed() == CloseCode.RATE
    assert len([m for m in viewer.messages if m["t"] == "error"]) == 1  # one a second for each code
    assert input_calls(rig.page) == []


async def test_waiting_wheels_are_summed_and_order_is_kept(rig: Rig) -> None:
    viewer = await rig.opened()
    rig.page.input_hold = asyncio.Event()  # the first wheel is being dispatched; the rest wait in the queue
    wheel = {"t": "wheel", "page": 1, "x": 195, "y": 300, "dx": 0}
    await viewer.send({**wheel, "dy": 1})
    await viewer.until(lambda: input_calls(rig.page))  # it's being dispatched
    for dy in range(2, 17):
        await viewer.send({**wheel, "dy": dy})
    await viewer.send({"t": "tap", "page": 1, "seq": 1, "x": 10, "y": 20})
    for dy in (100, 200, 300):
        await viewer.send({**wheel, "dy": dy})
    number = viewer.number
    await asyncio.sleep(0.2)
    rig.page.input_hold.set()
    await viewer.until(lambda: rig.sessions[0]._numbers.n >= number)
    calls = [(p["type"], p.get("deltaY")) for _, p in input_calls(rig.page)]
    assert calls == [
        ("mouseWheel", 1),
        ("mouseWheel", sum(range(2, 17))),  # 15 wheels, one dispatch, the exact sum
        ("mouseMoved", None),
        ("mousePressed", None),
        ("mouseReleased", None),
        ("mouseWheel", 600),  # nothing merges across the tap
    ]


def test_a_hundred_wheels_waiting_are_one() -> None:
    queue = InboundQueue()
    for number in range(1, 101):
        assert queue.put(Wheel(page=1, x=10, y=20, dx=0.5, dy=number / 4), number)
    (merged,) = queue.waiting
    assert isinstance(merged, Wheel)
    assert merged.dy == sum(n / 4 for n in range(1, 101))
    assert merged.dx == 50


# ---- images (§4)


async def test_two_images_un_acked_at_most_the_newest_waits_and_repeats_are_32_bytes(rig: Rig) -> None:
    viewer = await rig.opened(ack=False)
    rig.frame(1)  # sent: two un-acked now
    await viewer.until(lambda: len(viewer.images) == 2)
    rig.frame(2)
    rig.frame(3)  # replaces 2, which is never sent
    await asyncio.sleep(0.3)
    assert len(viewer.images) == 2
    await viewer.ws.send_str(json.dumps({"t": "ack", "seq": 1}))
    header, data = await viewer.image(2)
    assert data == jpeg(780, 1328, 3)
    assert header.seq == 3
    await viewer.ws.send_str(json.dumps({"t": "ack", "seq": 2}))
    await viewer.ws.send_str(json.dumps({"t": "ack", "seq": 3}))
    rig.frame(1, scroll=40)  # the same bytes as image 2, at another scroll
    repeat, payload = await viewer.image(3)
    assert (repeat.kind, repeat.slot, repeat.sy, payload) == (REPEAT, viewer.images[1][0].slot, 40.0, b"")
    acked = [p["sessionId"] for _, p in rig.page.sent("Page.screencastFrameAck")]
    assert acked == [1, 1, 1, 1]  # every frame, at once


async def animate(rig: Rig, seconds: float) -> list[str]:
    """A spinner: Chromium sends frame after frame, and each screenshot differs too. The pump's modes meanwhile."""
    pump = rig.sessions[0]._pump
    modes: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rig.page.shot += 1
        rig.frame(1000 + rig.page.shot)
        modes.append(pump.mode)
        await asyncio.sleep(0.02)
    return modes


async def test_an_animated_page_nobody_touches_goes_still_and_input_brings_it_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cast_frames, "STILL_AFTER_SECONDS", 0.3)
    monkeypatch.setattr(cast_frames, "STILL_EVERY_SECONDS", 0.05)
    monkeypatch.setattr(cast_frames, "MODE_CHECK_SECONDS", 0.02)
    viewer = await rig.opened()
    pump = rig.sessions[0]._pump
    modes = await animate(rig, 1.0)
    assert modes[0] == "live"
    assert modes[-1] == "still", "an animated page nobody touches stayed live"
    assert rig.page.sent("Page.stopScreencast")
    images = len(viewer.images)
    await asyncio.sleep(0.1)
    assert len(viewer.images) - images <= 2  # still: a screenshot every 50 ms here, sent when it shows something new

    # The animation stops: 4 unchanged captures in a row, and the screencast is back.
    shots = len(rig.page.sent("Page.captureScreenshot"))
    await eventually(lambda: pump.mode == "live", 2)
    assert len(rig.page.sent("Page.captureScreenshot")) >= shots + 2  # those in flight may count among the 4
    assert len(rig.page.sent("Page.startScreencast")) == 2

    # Still again, and a tap: the screencast restarts before the tap is carried out.
    assert (await animate(rig, 1.0))[-1] == "still"
    await viewer.send({"t": "tap", "page": 1, "seq": viewer.latest.seq, "x": 10, "y": 10})
    await eventually(lambda: input_calls(rig.page))
    methods = [m for m, _ in rig.page.calls]
    restart = len(methods) - 1 - methods[::-1].index("Page.startScreencast")
    assert restart < methods.index("Input.dispatchMouseEvent")
    assert pump.mode == "live"


async def test_hidden_means_nothing_and_shown_again_a_fresh_image(rig: Rig) -> None:
    viewer = await rig.opened()
    await viewer.send({"t": "visible", "v": False})
    await viewer.until(lambda: rig.page.sent("Page.stopScreencast"))
    count = len(viewer.images)
    rig.frame(5)
    await asyncio.sleep(0.2)
    assert len(viewer.images) == count
    rig.page.shot = 9
    await viewer.send({"t": "visible", "v": True})
    header, data = await viewer.image(viewer.latest.seq)
    assert data == jpeg(780, 1328, 9)  # a screenshot, not the frame from while it was hidden
    assert not header.first
    await viewer.until(lambda: len(rig.page.sent("Page.startScreencast")) == 2)


async def test_a_new_page_is_seeded_and_shown_and_the_main_one_comes_back(rig: Rig) -> None:
    viewer = await rig.opened()
    popup = FakePage("https://bank.example/approve", "Your bank")
    rig.context.open(popup)
    listing = await viewer.message("pages", where=lambda m: len(m["pages"]) == 2)
    assert listing["front"] == 2
    assert listing["pages"][1] == {"id": 2, "title": "Your bank", "origin": "https://bank.example", "main": False}
    header, _ = await viewer.until(lambda: next((i for i in viewer.images if i[0].page == 2), None))
    assert header.first
    await viewer.send({"t": "page", "op": "show", "page": 1})
    back, _ = await viewer.until(lambda: next((i for i in viewer.images if i[0].seq > header.seq), None))
    assert (back.page, back.first) == (1, True)
    count = len(viewer.messages)
    await viewer.send({"t": "page", "op": "close", "page": 2})
    await viewer.message("pages", where=lambda m: len(m["pages"]) == 1, after=count)
    assert ("close", {"run_before_unload": True}) in popup.calls


# ---- authorization (§7.2, C35)


async def test_a_hold_that_runs_out_on_the_clock_closes_the_stream_with_nothing_more_sent(rig: Rig) -> None:
    """The watch loop is frozen: nothing but the clock says the hold is over, and the viewer only acks."""
    now = [1000.0]
    hold, limits = HoldState(ready_at=900.0, first_viewer_at=950.0), HoldLimits(hard_s=150)
    rig.verdict = []  # replaced by the clock's

    def verdict() -> CloseCode | None:
        return CloseCode.ENDED if release_due(hold, limits, now[0]) is not None else None

    viewer = await rig.connect(None)
    session = rig.sessions[0]
    session._verdict = verdict
    await viewer.send(PHONE)
    await viewer.image()
    now[0] = 1050.0  # the hard cap
    count = len(viewer.images)
    started = time.monotonic()
    rig.frame(7)  # an image after the verdict changed is never sent
    assert await viewer.closed() == CloseCode.ENDED
    assert time.monotonic() - started < 1.5
    assert len(viewer.images) == count


async def test_the_verdict_is_asked_every_second_without_any_message(rig: Rig) -> None:
    viewer = await rig.opened()
    started = time.monotonic()
    rig.verdict[0] = CloseCode.WITHDRAWN  # the kill switch
    assert await viewer.closed() == CloseCode.WITHDRAWN
    assert time.monotonic() - started < 1.5


async def test_no_input_is_carried_out_once_the_verdict_says_no(rig: Rig) -> None:
    viewer = await rig.opened()
    rig.verdict[0] = CloseCode.REVOKED
    await viewer.send({"t": "tap", "page": 1, "seq": 1, "x": 10, "y": 10})
    assert await viewer.closed() == CloseCode.REVOKED
    assert input_calls(rig.page) == []


async def test_a_tap_at_an_image_the_page_moved_away_from_sends_nothing(rig: Rig) -> None:
    viewer = await rig.opened()
    rig.page.page_y = 120.0  # it scrolled since image 1
    await viewer.send({"t": "tap", "page": 1, "seq": 1, "x": 10, "y": 10})
    assert await viewer.message("error") == {"t": "error", "code": "stale", "ref": "tap"}
    assert input_calls(rig.page) == []


# ---- dialogs (§5.5)


async def test_a_dialog_open_at_the_start_is_shown_before_any_image_and_input_waits_for_it(rig: Rig) -> None:
    rig.dialogs.open = [(1, dialog(rig.page))]
    rig.page.hang = {"Page.captureScreenshot", "Page.getLayoutMetrics", "Runtime.evaluate", "Page.startScreencast"}
    viewer = await rig.connect()
    shown = await viewer.message("dialog")
    assert shown == {
        "t": "dialog",
        "id": 1,
        "page": 1,
        "kind": "confirm",
        "origin": "https://shop.example",
        "msg": "Leave checkout?",
        "def": "",
    }
    assert [m["t"] for m in viewer.messages[:4]] == ["ready", "pages", "clock", "dialog"]
    await viewer.send({"t": "key", "c": "x"})
    assert await viewer.message("error") == {"t": "error", "code": "stale", "ref": "key"}
    assert viewer.images == []
    assert input_calls(rig.page) == []

    rig.page.hang = set()
    await viewer.send({"t": "dialog", "id": 1, "ok": True})
    header, _ = await viewer.image()  # the page answers again: its first image follows the answer
    assert header.first
    assert rig.dialogs.answers == [(1, True, None)]


async def test_dialogs_that_open_and_close_while_streaming_are_reported(rig: Rig) -> None:
    viewer = await rig.opened()
    rig.dialogs.sink.dialog_opened(4, dialog(rig.page, "alert", "Session ends soon"))
    assert (await viewer.message("dialog"))["id"] == 4
    rig.dialogs.sink.dialog_closed(4)  # answered on the screen, or the page navigated
    assert await viewer.message("dialog_closed") == {"t": "dialog_closed", "id": 4}
    await viewer.send({"t": "dialog", "id": 4, "ok": True})  # too late: ignored
    await asyncio.sleep(0.1)
    assert rig.dialogs.answers == []


async def test_a_click_that_opens_a_dialog_is_done_when_the_dialog_opens(rig: Rig) -> None:
    """Chromium doesn't acknowledge a click in a cross-site frame until the dialog it opened there is answered. The
    answer, the viewer's next message, doesn't wait behind the click, and later input waits for the click's end."""
    viewer = await rig.opened()
    header = viewer.latest
    rig.page.input_hold = asyncio.Event()  # the click goes into a frame whose dialog holds it up
    await viewer.send({"t": "tap", "page": 1, "seq": header.seq, "x": 100, "y": 200, "ms": 0})
    await eventually(lambda: rig.page.sent("Input.dispatchMouseEvent"))
    rig.dialogs.sink.dialog_opened(1, dialog(rig.page, "alert", "Card declined"))
    assert (await viewer.message("dialog"))["id"] == 1
    await viewer.send({"t": "dialog", "id": 1, "ok": True})
    await eventually(lambda: rig.dialogs.answers == [(1, True, None)], seconds=1)
    await viewer.send({"t": "key", "c": "4"})
    await asyncio.sleep(0.1)
    assert [p["type"] for _, p in input_calls(rig.page)] == ["mouseMoved"], "the key didn't wait for the click"

    rig.page.input_hold.set()  # answered: the frame takes the rest of the click, then the key
    await eventually(lambda: len(input_calls(rig.page)) == 5)
    assert [p["type"] for _, p in input_calls(rig.page)] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
        "keyDown",
        "keyUp",
    ]
    assert not [m for m in viewer.messages if m["t"] == "error"]


# ---- pickers (§6.11-6.12, C40)


@pytest.fixture
async def picker() -> AsyncIterator[tuple[FakePage, InputMapper, Worlds, FieldRef]]:
    page = FakePage()
    page.w, page.h = 390, 664
    context = FakeContext(page)
    session = await context.new_cdp_session(page)
    cdp = Cdp(cast(Any, session))
    worlds = Worlds(cast(Any, context), cast(Any, page), cdp)
    world = await worlds.get(cast(Any, page.main_frame))
    assert world is not None
    input_mapper = InputMapper(ImageRing(), sleep=lambda _: asyncio.sleep(0))
    input_mapper.aim(1, cdp)
    options = (("Choose", True), ("BC", False), ("AB", False))
    yield page, input_mapper, worlds, FieldRef(world, 1, 0, "select", options, (20, 60, 300, 40))
    await worlds.close()


async def test_choose_sends_nothing_for_a_field_gone_or_covered(picker: Any) -> None:
    page, input_mapper, worlds, ref = picker
    for answer in ({"why": "gone"}, {"why": "changed"}, {"pts": [], "sel": 0, "v": ""}):
        page.scripts["CLICK_POINTS"] = answer
        assert await input_mapper.choose(worlds, ref, 2, list_open=False) == "stale"
    page.scripts["CLICK_POINTS"] = {"pts": [500.0, 80.0], "sel": 0, "v": ""}  # outside the visual viewport
    assert await input_mapper.choose(worlds, ref, 2, list_open=False) == "stale"
    assert await input_mapper.choose(worlds, ref, 0, list_open=False) == "failed"  # a disabled option
    assert input_calls(page) == []


async def test_choose_presses_no_key_but_escape_unless_the_select_has_focus(picker: Any) -> None:
    page, input_mapper, worlds, ref = picker
    page.scripts["CLICK_POINTS"] = {"pts": [170.0, 80.0], "sel": 0, "v": ""}
    page.scripts["IS_FOCUSED"] = False
    assert await input_mapper.choose(worlds, ref, 2, list_open=False) == "failed"
    sent = [(p["type"], p.get("key")) for _, p in input_calls(page)]
    assert sent == [
        ("mouseMoved", None),
        ("mousePressed", None),
        ("mouseReleased", None),
        ("rawKeyDown", "Escape"),
        ("keyUp", "Escape"),
    ]


async def test_choose_picks_with_keys_once_focused(picker: Any) -> None:
    page, input_mapper, worlds, ref = picker
    reads = iter([{"pts": [170.0, 80.0], "sel": 0, "v": ""}, {"pts": [170.0, 80.0], "sel": 2, "v": ""}])
    page.scripts["CLICK_POINTS"] = lambda _: next(reads)
    page.scripts["IS_FOCUSED"] = True
    assert await input_mapper.choose(worlds, ref, 2, list_open=False) is None
    keys = [p["key"] for _, p in input_calls(page) if p["type"] in ("rawKeyDown", "keyDown")]
    assert keys == ["Home", "ArrowDown", "Enter"]


async def test_a_date_gets_no_digit_unless_focused(picker: Any) -> None:
    page, input_mapper, worlds, ref = picker
    date_ref = FieldRef(ref.world, 1, 0, "date", None, ref.r)
    page.scripts["CLICK_POINTS"] = {"pts": [32.0, 80.0], "sel": -1, "v": ""}
    page.scripts["IS_FOCUSED"] = False
    assert await input_mapper.date(worlds, date_ref, "2026-10-16") == "failed"
    assert [p["type"] for _, p in input_calls(page)] == ["mouseMoved", "mousePressed", "mouseReleased"]


# ---- the rest of the session


async def test_a_resize_waits_for_quiet_and_sizes_are_limited(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    viewer = await rig.opened()
    await viewer.send({"t": "resize", "w": 500, "h": 600, "dpr": 3})
    await viewer.send({"t": "resize", "w": 664, "h": 390, "dpr": 3})  # within 300 ms: only this one counts
    await viewer.until(lambda: rig.screen.current == (664, 390, 2))
    assert rig.screen.applied == [(390, 664, 2), (664, 390, 2)]
    header, _ = await viewer.until(lambda: next((i for i in viewer.images if i[0].w == 664), None))
    assert header.first

    monkeypatch.setattr(stream, "RESIZE_QUIET_SECONDS", 0.0)
    for w in range(300, 314):  # 14 more distinct sizes: 16 in all
        await viewer.send({"t": "resize", "w": w, "h": 600, "dpr": 1})
        await asyncio.sleep(0.25)  # the bucket: 4 a second
    await viewer.until(lambda: rig.screen.current == (313, 600, 1))
    await asyncio.sleep(1.1)
    await viewer.send({"t": "resize", "w": 700, "h": 600, "dpr": 1})
    assert await viewer.message("error", where=lambda m: m["ref"] == "resize") == {
        "t": "error",
        "code": "rate",
        "ref": "resize",
    }


async def test_a_resize_the_screen_cant_take_is_answered_and_the_session_goes_on(rig: Rig) -> None:
    viewer = await rig.opened()
    rig.screen.failing.add((664, 390, 2))
    await viewer.send({"t": "resize", "w": 664, "h": 390, "dpr": 3})
    assert await viewer.message("error", where=lambda m: m["ref"] == "resize") == {
        "t": "error",
        "code": "failed",
        "ref": "resize",
    }
    assert rig.screen.applied == [(390, 664, 2), (664, 390, 2), (390, 664, 2)]  # back as it was, DPI and all
    assert rig.screen.current == (390, 664, 2)
    await viewer.send({"t": "resize", "w": 500, "h": 600, "dpr": 3})  # and the next works
    await viewer.until(lambda: rig.screen.current == (500, 600, 2))
    assert not viewer.ws.closed


async def test_the_field_map_is_sent_on_its_schedule_and_unchanged_is_fields_ok(rig: Rig) -> None:
    raw = {
        "tag": "i", "type": "email", "im": "email", "ac": "email username", "ekh": "next", "cap": "",
        "max": None, "ro": False, "r": [20, 100, 300, 40], "m": 511, "i": 0,
    }  # fmt: skip
    found = {"f": [raw], "trunc": False, "slow": False, "clip": [0, 0, 390, 664], "vv": [0, 0, 0, 0, 1],
             "cw": 390, "ch": 3000, "own": []}  # fmt: skip
    rig.page.scripts["FIELDS"] = found
    viewer = await rig.opened()
    fields = await viewer.message("fields")
    assert fields["rev"] == 1
    assert fields["f"] == [
        {"id": 1, "k": "text", "r": [20, 100, 300, 40], "c": [20, 100, 300, 40], "m": 511, "type": "email",
         "im": "email", "ac": "email", "ekh": "next", "cap": "", "max": None, "ro": False}
    ]  # fmt: skip
    number = await viewer.send({"t": "wheel", "page": 1, "x": 10, "y": 10, "dx": 0, "dy": 5})
    ok = await viewer.message("fields_ok", where=lambda m: m["n"] >= number)
    assert ok["rev"] == 1
    rig.page.scripts["FIELDS"] = {**found, "f": [{**raw, "m": 63}]}
    number = await viewer.send({"t": "wheel", "page": 1, "x": 10, "y": 10, "dx": 0, "dy": 5})
    changed = await viewer.message("fields", where=lambda m: m["n"] >= number)
    assert changed["rev"] == 2
    assert changed["f"][0]["m"] == 63


async def test_a_failed_map_is_tried_again_and_a_navigation_makes_one_due(rig: Rig) -> None:
    """Neither waits for another image: a map read while the page was between documents fails, and a page that
    loads a new document may send no image after its last one (PROTOCOL §5.3.4)."""
    empty = {"f": [], "trunc": False, "slow": False, "clip": [0, 0, 390, 664], "vv": [0, 0, 0, 0, 1], "cw": 390,
             "ch": 3000, "own": []}  # fmt: skip
    calls: list[float] = []

    def fields(_: str) -> Any:
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("Execution context was destroyed")
        return empty

    rig.page.scripts["FIELDS"] = fields
    viewer = await rig.opened()
    first = await viewer.message("fields")
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.9  # a second after the failure
    rig.page.scripts["FIELDS"] = {**empty, "ch": 4000}
    count = len(viewer.messages)
    rig.page.emit("framenavigated", rig.page.main_frame)
    again = await viewer.message("fields", after=count)
    assert again["rev"] == first["rev"] + 1
    assert again["ch"] == 4000


async def test_the_map_starts_every_250_ms_while_images_keep_coming(rig: Rig) -> None:
    """No debounce: a page that never stops changing still gets a map, one every 250 ms (PROTOCOL §5.3.4)."""
    started: list[float] = []

    def fields(_: str) -> Any:
        started.append(time.monotonic())
        return rig.page._script("", "") or {
            "f": [], "trunc": False, "slow": False, "clip": [0, 0, 390, 664], "vv": [0, 0, 0, 0, 1], "cw": 390,
            "ch": 3000, "own": [],
        }  # fmt: skip

    rig.page.scripts["FIELDS"] = fields
    viewer = await rig.opened()
    for tag in range(40):  # 1.2 s of images
        rig.frame(300 + tag)
        await asyncio.sleep(0.03)
    gaps = [b - a for a, b in itertools.pairwise(started)]
    assert len(started) >= 4
    assert min(gaps) >= 0.24
    assert viewer.messages


async def test_focus_follows_a_tap_and_a_select_opened_by_it_is_a_popup(rig: Rig) -> None:
    raw = {"tag": "s", "type": "", "im": "", "ac": "", "ekh": "", "cap": "", "max": None, "ro": False,
           "r": [20, 60, 300, 40], "m": 511, "n": 2, "opts": [["BC", False], ["AB", False]], "sel": 0}  # fmt: skip
    rig.page.scripts["FOCUS"] = {"f": raw, "clip": [0, 0, 390, 664], "vv": [0, 0, 0, 0, 1]}
    viewer = await rig.opened()
    await viewer.send({"t": "tap", "page": 1, "seq": 1, "x": 100, "y": 80})
    focus = await viewer.message("focus")
    assert focus["popup"] is True
    assert focus["f"]["k"] == "select"
    assert focus["f"]["opts"] == [["BC", False], ["AB", False]]


# ---- teardown (§3.5)


async def test_teardown_releases_keys_stops_the_screencast_and_detaches_bounded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cast_cdp.Cdp.__init__.__kwdefaults__, "seconds", 0.2)
    viewer = await rig.opened()
    await viewer.send({"t": "key", "k": "Shift", "ph": "down"})
    await viewer.send({"t": "key", "c": "a", "ph": "down"})
    await viewer.until(lambda: len(input_calls(rig.page)) == 2)
    rig.page.hang = {"Page.stopScreencast"}  # a page that doesn't answer: the teardown goes on regardless
    started = time.monotonic()
    await viewer.close()
    await eventually(lambda: rig.closed)
    assert time.monotonic() - started < 2
    ups = [(p["key"], p["modifiers"]) for _, p in input_calls(rig.page) if p["type"] == "keyUp"]
    assert ups == [("a", 8), ("Shift", 0)]
    methods = [m for m, _ in rig.page.calls]
    assert methods.index("Page.stopScreencast") > max(i for i, m in enumerate(methods) if m.startswith("Input."))
    assert rig.dialogs.log == ["attach", "detach"]
    assert all(s.detached for s in rig.context.sessions)
    assert rig.screen.applied == [(390, 664, 2)]  # no restore: the same device may come straight back
    assert rig.page.handlers.get("close") == []
    assert rig.context.handlers.get("page") == []


async def test_a_frame_that_arrives_as_the_session_ends_leaves_no_call_unawaited() -> None:
    """Chromium can send a screencast frame just as the teardown stops the pump, which then cancels the frame's ack
    before it has run. Nothing may be left behind that was never awaited (the warning fails the test)."""
    pump = cast_frames.FramePump(lambda _: asyncio.sleep(0, True), ImageRing())
    cdp = Cdp(cast(Any, FakeCdp(FakePage())))
    pump._on_frame(cdp, {"sessionId": 1})
    await pump.stop()
    gc.collect()


@dataclass
class StreamedScreen:
    changed: bool
    restored: int = 0

    async def restore(self) -> None:
        self.restored += 1


async def test_a_classic_viewer_after_a_stream_finds_the_pages_at_scale_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before a classic viewer, after a stream: every page the stream zoomed is pinched back to scale 1, then the
    screen is restored. Without a stream since the last classic viewer, the pages aren't touched at all, and a page
    with an open dialog (which answers no CDP) is left for the classic viewer."""
    zoomed, plain, asking = FakePage(), FakePage("https://pay.example/bank"), FakePage("https://pay.example/3ds")
    zoomed.scale, zoomed.w, zoomed.h = 2.0, 390, 664
    asking.hang = {"Page.getLayoutMetrics"}
    context = FakeContext(zoomed, plain, asking)
    dialogs = FakeDialogs()
    dialogs.open = [(1, dialog(asking, "alert", "Approve in your bank's app"))]

    def classic(screen: StreamedScreen) -> stream.ClassicScreen:
        return stream.ClassicScreen(screen, lambda: (cast(Any, context), cast(Any, zoomed)), dialogs)

    untouched = StreamedScreen(changed=False)
    await classic(untouched).restore()
    assert (untouched.restored, context.sessions) == (1, [])

    after_a_stream = StreamedScreen(changed=True)
    started = time.monotonic()
    await classic(after_a_stream).restore()
    assert time.monotonic() - started < 1
    assert after_a_stream.restored == 1
    (pinch,) = zoomed.sent("Input.")
    assert pinch == (
        "Input.synthesizePinchGesture",
        {"x": 195.0, "y": 332.0, "scaleFactor": 0.5, "gestureSourceType": "mouse", "relativeSpeed": 3000},
    )
    assert plain.sent("Input.") == []
    assert asking.calls == []
    assert [s.page for s in context.sessions] == [zoomed, plain]
    assert all(s.detached for s in context.sessions)

    # A page that answers nothing for another reason holds the classic viewer up only so long.
    monkeypatch.setattr(stream, "UNZOOM_SECONDS", 0.2)
    dialogs.open = []
    started = time.monotonic()
    await classic(after_a_stream).restore()
    assert time.monotonic() - started < 1
    assert after_a_stream.restored == 2


async def test_typed_text_is_never_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rig.dialogs.open = [(1, dialog(rig.page, "prompt", "Promo code?"))]
    viewer = await rig.connect()
    await viewer.message("dialog")
    await viewer.send({"t": "dialog", "id": 1, "ok": True, "s": "FERRY10"})
    await viewer.image()
    await viewer.send({"t": "text", "s": CARD_NUMBER})
    await viewer.send({"t": "compose", "s": "Chester"})
    await viewer.send({"t": "text", "s": "SECRET\u0001CODE"})  # a value error
    await viewer.send({"t": "key", "c": "Q"})
    rig.page.hang = {"Input.insertText"}  # and one that fails
    await viewer.send({"t": "text", "s": "4000 0566 5566 5556"})
    await viewer.message("error", where=lambda m: m["code"] == "invalid")
    with contextlib.suppress(AssertionError):
        await viewer.message("error", where=lambda m: m["code"] == "failed")
    await viewer.close()
    await eventually(lambda: rig.closed)
    for secret in ("FERRY10", "4242", "Chester", "SECRET", "CODE", "4000 0566", "'Q'"):
        assert secret not in caplog.text
    assert ("Input.insertText", {"text": CARD_NUMBER}) in rig.page.calls
