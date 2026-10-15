"""The dialog keeper (agent/dialogs.py) against a stand-in browser context that behaves as Playwright's does:
with no "dialog" listener a dialog is dismissed at once, and its CDP sessions report each dialog's frame URL and
its closing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from ferry_watch.agent.dialogs import ORIGIN_WAIT_SECONDS, DialogKeeper, KeptDialog


class Gone(Exception):
    """Playwright's "No dialog is showing"."""


class Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, event: str, f: Callable[..., Any]) -> None:
        self.handlers.setdefault(event, []).append(f)

    def remove_listener(self, event: str, f: Callable[..., Any]) -> None:
        self.handlers[event].remove(f)

    def emit(self, event: str, *args: Any) -> bool:
        listeners = list(self.handlers.get(event, []))
        for f in listeners:
            f(*args)
        return bool(listeners)


class FakePage(Emitter):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def close(self) -> None:
        self.emit("close", self)


@dataclass(eq=False)
class FakeDialog:
    page: FakePage | None
    type: str
    message: str
    default_value: str = ""
    answers: list[tuple[str, str | None]] = field(default_factory=list)
    gone: bool = False

    async def accept(self, prompt_text: str | None = None) -> None:
        if self.gone:
            raise Gone
        self.answers.append(("accept", prompt_text))
        self.gone = True

    async def dismiss(self) -> None:
        if self.gone:
            raise Gone
        self.answers.append(("dismiss", None))
        self.gone = True


class FakeSession(Emitter):
    def __init__(self, page: FakePage, enable: asyncio.Event) -> None:
        super().__init__()
        self.page = page
        self.enable = enable
        self.sent: list[str] = []
        self.detached = False

    async def send(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append(method)
        if method == "Page.enable":
            await self.enable.wait()  # doesn't return while a dialog is open
        return {}

    async def detach(self) -> None:
        self.detached = True


class FakeContext(Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[FakePage] = [FakePage("https://www.bcferries.com/checkout")]
        self.sessions: list[FakeSession] = []
        self.enable = asyncio.Event()
        self.enable.set()

    async def new_cdp_session(self, page: FakePage) -> FakeSession:
        session = FakeSession(page, self.enable)
        self.sessions.append(session)
        return session

    def session(self, page: FakePage) -> FakeSession:
        return next(s for s in self.sessions if s.page is page and not s.detached)

    def open_page(self, url: str) -> FakePage:
        page = FakePage(url)
        self.pages.append(page)
        self.emit("page", page)
        return page

    async def dialog(
        self, kind: str, message: str, *, page: FakePage | None = None, frame: str | None = None, cdp_first: bool = True
    ) -> FakeDialog:
        """A dialog opens: Playwright's event and the CDP one, in either order."""
        page = page or self.pages[0]
        dialog = FakeDialog(page, kind, message, "D" if kind == "prompt" else "")
        opening = {"url": frame or page.url, "message": message, "type": kind, "hasBrowserHandler": True}
        sessions = [s for s in self.sessions if s.page is page and not s.detached]
        if cdp_first:
            for s in sessions:
                s.emit("Page.javascriptDialogOpening", opening)
        if not self.emit("dialog", dialog):
            await (dialog.accept() if kind == "beforeunload" else dialog.dismiss())  # Playwright's own
        if not cdp_first:
            for s in sessions:
                s.emit("Page.javascriptDialogOpening", opening)
        await asyncio.sleep(0)
        return dialog

    def closed(self, page: FakePage | None = None) -> None:
        """A dialog on the page closed (answered on the screen, or by anyone: CDP says so to every session)."""
        for s in [s for s in self.sessions if s.page is (page or self.pages[0]) and not s.detached]:
            s.emit("Page.javascriptDialogClosed", {"result": True, "userInput": ""})


@dataclass
class Sink:
    opened: list[tuple[int, KeptDialog]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)

    def dialog_opened(self, number: int, dialog: KeptDialog) -> None:
        self.opened.append((number, dialog))

    def dialog_closed(self, number: int) -> None:
        self.closed.append(number)


@pytest.fixture
async def context() -> FakeContext:
    return FakeContext()


@pytest.fixture
async def keeper(context: FakeContext) -> DialogKeeper:
    keeper = DialogKeeper(lambda: cast(Any, context), call_seconds=0.5)
    await keeper.keep()
    await asyncio.sleep(0)  # the page's session
    return keeper


def numbered(pairs: list[tuple[int, KeptDialog]]) -> list[tuple[int, str]]:
    return [(n, d.message) for n, d in pairs]


async def test_without_a_viewer_dialogs_are_dismissed_as_playwright_does(context: FakeContext) -> None:
    keeper = DialogKeeper(lambda: cast(Any, context))
    confirm = await context.dialog("confirm", "Leave?")
    assert confirm.answers == [("dismiss", None)]
    assert not keeper.keeping


async def test_once_kept_a_dialog_waits_for_its_viewer(context: FakeContext, keeper: DialogKeeper) -> None:
    confirm = await context.dialog("confirm", "Proceed to payment?")
    assert confirm.answers == []
    assert [d.message for d in keeper.open_dialogs] == ["Proceed to payment?"]
    assert context.sessions[0].sent == ["Page.enable"]


async def test_each_dialog_carries_its_frames_url_whichever_event_comes_first(
    context: FakeContext, keeper: DialogKeeper
) -> None:
    sink = Sink()
    keeper.attach(sink)
    await context.dialog("alert", "From the card", frame="http://pay.test:8002/card.html", cdp_first=True)
    context.closed()  # answered on the screen
    await context.dialog("alert", "From the shop", cdp_first=False)
    assert [(n, d.message, d.url) for n, d in sink.opened] == [
        (1, "From the card", "http://pay.test:8002/card.html"),
        (2, "From the shop", "https://www.bcferries.com/checkout"),
    ]


async def test_a_dialog_without_its_frames_url_is_shown_after_a_moment(
    context: FakeContext, keeper: DialogKeeper
) -> None:
    sink = Sink()
    keeper.attach(sink)
    context.sessions[0].detached = True  # no CDP event will come
    await context.dialog("alert", "Hello")
    assert sink.opened == []
    await asyncio.sleep(ORIGIN_WAIT_SECONDS + 0.1)
    assert [(n, d.url) for n, d in sink.opened] == [(1, "")]


async def test_dialogs_stay_open_across_sessions_with_new_ids(context: FakeContext, keeper: DialogKeeper) -> None:
    first = Sink()
    assert keeper.attach(first) == []
    await context.dialog("confirm", "One?")
    keeper.detach(first)
    bank = context.open_page("https://bank.example/3ds")
    await asyncio.sleep(0.01)  # its session
    await context.dialog("prompt", "Two?", page=bank)
    assert first.opened[0][0] == 1
    assert len(first.opened) == 1  # detached: the second waits for the next session
    second = Sink()
    assert numbered(keeper.attach(second)) == [(1, "One?"), (2, "Two?")]
    await context.dialog("alert", "Three")
    assert numbered(second.opened) == [(3, "Three")]


async def test_the_viewer_answers(context: FakeContext, keeper: DialogKeeper) -> None:
    sink = Sink()
    keeper.attach(sink)
    prompt = await context.dialog("prompt", "Promo code?")
    pay = context.open_page("https://pay.example/")
    await asyncio.sleep(0.01)
    confirm = await context.dialog("confirm", "Sure?", page=pay)
    numbers = {d.message: n for n, d in sink.opened}
    assert await keeper.answer(numbers["Promo code?"], ok=True, text="FERRY10")
    assert prompt.answers == [("accept", "FERRY10")]
    context.closed()  # the closing of our own answer
    assert await keeper.answer(numbers["Sure?"], ok=False, text="ignored")
    assert confirm.answers == [("dismiss", None)]
    assert keeper.open_dialogs == []
    assert sink.closed == []  # the viewer answered them: nothing to tell it
    assert not await keeper.answer(numbers["Sure?"], ok=True)  # already answered
    assert not await keeper.answer(99, ok=True)


async def test_our_own_answers_closing_never_takes_another_dialog(context: FakeContext, keeper: DialogKeeper) -> None:
    sink = Sink()
    keeper.attach(sink)
    await context.dialog("confirm", "First?")
    assert await keeper.answer(1, ok=True)
    await context.dialog("alert", "Second")  # opened before the first one's closing event arrived
    context.closed()
    assert [d.message for d in keeper.open_dialogs] == ["Second"]
    assert sink.closed == []


async def test_a_dialog_answered_on_the_screen_goes(context: FakeContext, keeper: DialogKeeper) -> None:
    sink = Sink()
    keeper.attach(sink)
    confirm = await context.dialog("confirm", "Proceed?")
    confirm.gone = True  # the classic viewer clicked OK
    context.closed()
    assert keeper.open_dialogs == []
    assert sink.closed == [1]
    assert not await keeper.answer(1, ok=True)


async def test_a_closed_page_takes_its_dialogs(context: FakeContext, keeper: DialogKeeper) -> None:
    sink = Sink()
    keeper.attach(sink)
    popup = context.open_page("https://bank.example/3ds")
    await asyncio.sleep(0)
    await context.dialog("alert", "Bank", page=popup)
    await context.dialog("alert", "Shop")
    popup.close()
    assert [d.message for d in keeper.open_dialogs] == ["Shop"]
    assert sink.closed == [1]


async def test_dismiss_all(context: FakeContext, keeper: DialogKeeper) -> None:
    sink = Sink()
    keeper.attach(sink)
    alert = await context.dialog("alert", "A")
    leave = await context.dialog("beforeunload", "")
    await keeper.dismiss_all()
    assert sink.closed == [1, 2]  # the agent dismissed them: the viewer is told
    assert alert.answers == [("dismiss", None)]
    assert leave.answers == [("accept", None)]  # a navigation the page held up goes ahead, as Playwright does
    assert keeper.keeping
    later = await context.dialog("confirm", "Kept?")
    assert later.answers == []  # still keeping


async def test_release_dismisses_and_lets_playwright_dismiss_again(context: FakeContext, keeper: DialogKeeper) -> None:
    confirm = await context.dialog("confirm", "Leave?")
    await keeper.release()
    assert confirm.answers == [("dismiss", None)]
    assert not keeper.keeping
    assert all(s.detached for s in context.sessions)
    later = await context.dialog("confirm", "Again?")
    assert later.answers == [("dismiss", None)]  # Playwright's own, at once
    await keeper.release()  # twice is fine


async def test_a_dialog_that_went_meanwhile_doesnt_stop_a_release(context: FakeContext, keeper: DialogKeeper) -> None:
    gone = await context.dialog("confirm", "Answered on the screen")
    gone.gone = True  # its closing event never came
    other = await context.dialog("alert", "Other", page=context.open_page("https://x.example/"))
    await keeper.release()
    assert other.answers == [("dismiss", None)]
    assert keeper.open_dialogs == []


async def test_keeping_starts_at_once_even_while_a_dialog_holds_up_page_enable(context: FakeContext) -> None:
    context.enable.clear()  # Page.enable doesn't return while a dialog is open
    keeper = DialogKeeper(lambda: cast(Any, context), call_seconds=0.2)
    async with asyncio.timeout(0.1):
        await keeper.keep()
    confirm = await context.dialog("confirm", "Kept?")
    assert confirm.answers == []
    await asyncio.sleep(0.3)  # the enable gave up waiting; the session still reports
    assert keeper.open_dialogs
    await keeper.release()


async def test_keep_follows_the_checkouts_context(context: FakeContext) -> None:
    current: list[FakeContext | None] = [None]
    keeper = DialogKeeper(lambda: cast(Any, current[0]))
    await keeper.keep()
    assert not keeper.keeping  # no checkout
    current[0] = context
    await keeper.keep()
    await keeper.keep()  # the same: nothing more
    assert len(context.handlers["dialog"]) == 1
    old = await context.dialog("alert", "Old")
    current[0] = FakeContext()
    await keeper.keep()  # a new browser session: the old one's dialogs are dismissed
    assert old.answers == [("dismiss", None)]
    assert context.handlers["dialog"] == []


async def test_answers_are_never_logged(
    context: FakeContext, keeper: DialogKeeper, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    keeper.attach(Sink())
    prompt = await context.dialog("prompt", "Card number?")
    prompt.gone = True  # the answer fails: the failure is logged, the text isn't
    await keeper.answer(1, ok=True, text="4242 4242 4242 4242")
    assert "4242" not in caplog.text
