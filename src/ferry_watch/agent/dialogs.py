"""JavaScript dialogs on the checkout, kept for its viewer to answer (PROTOCOL.md §5.5, DESIGN §4.4).

With no dialog listener, Playwright dismisses every alert, confirm, prompt and beforeunload the moment it opens:
right for the agent's own browsing, wrong for a person checking out, whose `confirm()` would return false unseen.
So from the first viewer of a hold on (the gate calls `keep()` when it admits one of either kind), the keeper
listens and leaves each dialog open:

- a classic (KasmVNC) viewer sees it where Chromium draws it, on the pod's screen, and answers it there;
- a stream session `attach()`es and gets each open dialog, with the origin of the frame that opened it, to show
  in its own sheet; it answers through `answer()`. Dialogs stay open across reconnects (`detach()`).

When the checkout is over, access is revoked or the pod drains, the gate calls `release()`: every open dialog is
dismissed (a beforeunload accepted, as Playwright does) and new ones are dismissed unseen again, so the agent's
own navigation afterwards (emptying the cart) can't stall behind one.

The keeper has a CDP session with the Page domain enabled on each page of the context. That gives each dialog
its frame's URL (`Page.javascriptDialogOpening`; Playwright's Dialog only knows the page), and says when one
closes without us (`Page.javascriptDialogClosed`: answered on the screen, or the page navigated away), which
Playwright doesn't notice. Page.enable doesn't return while a dialog is open on that page, so sessions are opened
in the background, as soon as keeping starts and as pages open, before there's a dialog to wait for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from .cast_protocol import MAX_DIALOG_MESSAGE, MAX_PROMPT_DEFAULT, DialogKind, clean

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Dialog, Page

log = logging.getLogger(__name__)

CALL_SECONDS: Final = 2.0  # each browser call the keeper makes
ORIGIN_WAIT_SECONDS: Final = 0.5  # for a dialog's frame URL, before it's shown without one
STALE_OPENING_SECONDS: Final = 5.0  # a frame URL nothing claimed in this long is dropped
MAX_URL: Final = 4096
MAX_NUMBER: Final = 65535  # dialog ids in one stream session (PROTOCOL §5.5)
_KINDS: Final = frozenset({"alert", "confirm", "prompt", "beforeunload"})


@dataclass(eq=False, slots=True)
class KeptDialog:
    """An open dialog. The text is the page's, cleaned and capped as PROTOCOL §5.5 allows."""

    page: Page
    kind: DialogKind
    message: str
    default: str  # a prompt's default text, else ""
    handle: Dialog
    url: str | None = None  # of the frame that opened it; "" when unknown, None until known
    _waiting: asyncio.TimerHandle | None = field(default=None, repr=False)


class DialogSink(Protocol):
    """A stream session: the dialogs to show (with this session's ids) and the ones that went without it."""

    def dialog_opened(self, number: int, dialog: KeptDialog) -> None: ...

    def dialog_closed(self, number: int) -> None: ...


@dataclass(slots=True)
class _Opening:
    """`Page.javascriptDialogOpening` that arrived before Playwright's dialog."""

    kind: str
    message: str
    url: str
    at: float


class DialogKeeper:
    def __init__(self, context: Callable[[], BrowserContext | None], *, call_seconds: float = CALL_SECONDS) -> None:
        """`context` gives the browser context holding the checkout now (or None), when keeping starts."""
        self._target = context
        self._call_seconds = call_seconds
        self._context: BrowserContext | None = None  # the one being kept
        self._open: list[KeptDialog] = []  # oldest first
        self._openings: dict[Page, list[_Opening]] = {}
        self._answered: dict[Page, int] = {}  # closings of our own answers, still to come
        self._watched: set[Page] = set()
        self._sessions: dict[Page, CDPSession] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._sink: DialogSink | None = None
        self._numbers: dict[KeptDialog, int] = {}  # in the attached session
        self._next_number = 1

    @property
    def keeping(self) -> bool:
        return self._context is not None

    @property
    def open_dialogs(self) -> list[KeptDialog]:
        return list(self._open)

    # ---- the gate

    async def keep(self) -> None:
        """Leave the checkout's dialogs open from now on. Never raises."""
        context = self._target()
        if context is self._context:
            return
        if self._context is not None:
            await self.release()  # another browser session: the old one's dialogs mean nothing now
        if context is None:
            return
        self._context = context
        context.on("dialog", self._on_dialog)
        context.on("page", self._on_page)
        for page in context.pages:
            self._on_page(page)
        log.info("Keeping the checkout's dialogs for its viewer")

    async def release(self) -> None:
        """Dismiss every open dialog, and let Playwright dismiss new ones unseen again. Bounded; never raises."""
        context, self._context = self._context, None
        if context is None:
            return
        with contextlib.suppress(Exception):
            context.remove_listener("dialog", self._on_dialog)  # first: a dialog opening now is Playwright's
            context.remove_listener("page", self._on_page)
        await self.dismiss_all()
        for task in self._tasks:
            task.cancel()
        sessions, self._sessions = list(self._sessions.values()), {}
        self._watched.clear()
        for session in sessions:
            await self._call(session.detach())
        self._openings.clear()
        self._answered.clear()
        log.info("Dialogs are dismissed again")

    async def dismiss_all(self) -> None:
        """Dismiss every open dialog (accept a beforeunload, letting the navigation go on). Never raises."""
        while self._open:
            dialog = self._open[0]
            self._drop(dialog, ours=True, tell=True)
            handle = dialog.handle
            await self._call(handle.accept() if dialog.kind == "beforeunload" else handle.dismiss())

    # ---- a stream session

    def attach(self, sink: DialogSink) -> list[tuple[int, KeptDialog]]:
        """A stream session shows the dialogs from now on. Returns the open ones with its ids for them, oldest
        first; later ones (and ones that go without it) reach `sink`."""
        self._sink = sink
        self._numbers = {}
        self._next_number = 1
        return [(n, d) for d in self._open if d.url is not None and (n := self._number(d)) is not None]

    def detach(self, sink: DialogSink) -> None:
        """The session is over. Its dialogs stay open for the next viewer."""
        if self._sink is sink:
            self._sink = None
            self._numbers = {}

    async def answer(self, number: int, ok: bool, text: str | None = None) -> bool:
        """Answer the attached session's dialog `number` (OK or Cancel; `text` for a prompt). False when it's
        not open any more. `text` is typed by the user: never logged."""
        dialog = next((d for d, n in self._numbers.items() if n == number), None)
        if dialog is None or dialog not in self._open:
            return False
        self._drop(dialog, ours=True, tell=False)  # the viewer knows: it answered
        handle = dialog.handle
        prompt_text = text if dialog.kind == "prompt" else None
        return await self._call(handle.accept(prompt_text) if ok else handle.dismiss())

    # ---- Playwright's and the browser's events

    def _on_dialog(self, handle: Dialog) -> None:
        page = handle.page
        kind = handle.type if handle.type in _KINDS else "alert"
        if page is None:  # not a page's: nothing could show it
            self._spawn(self._call(handle.dismiss()))
            return
        dialog = KeptDialog(
            page=page,
            kind=cast(DialogKind, kind),
            message=clean(handle.message, MAX_DIALOG_MESSAGE, keep_lf=True),
            default=clean(handle.default_value, MAX_PROMPT_DEFAULT) if kind == "prompt" else "",
            handle=handle,
        )
        self._open.append(dialog)
        opening = self._claim_opening(page, handle.type, handle.message)
        if opening is not None:
            self._located(dialog, opening.url)
        else:
            dialog._waiting = asyncio.get_running_loop().call_later(ORIGIN_WAIT_SECONDS, self._located, dialog, "")

    def _on_opening(self, page: Page, event: dict[str, Any]) -> None:
        kind, message, url = str(event.get("type", "")), str(event.get("message", "")), str(event.get("url", ""))
        waiting = next(
            (
                d
                for d in self._open
                if d.page is page and d.url is None and d.kind == kind and d.handle.message == message
            ),
            None,
        )
        if waiting is not None:
            self._located(waiting, url)
            return
        now = time.monotonic()
        queued = [o for o in self._openings.get(page, []) if now - o.at < STALE_OPENING_SECONDS]
        self._openings[page] = [*queued, _Opening(kind, message, url[:MAX_URL], now)]

    def _claim_opening(self, page: Page, kind: str, message: str) -> _Opening | None:
        queued = self._openings.get(page, [])
        for i, opening in enumerate(queued):
            if opening.kind == kind and opening.message == message:
                del queued[i]
                return opening
        return None

    def _located(self, dialog: KeptDialog, url: str) -> None:
        if dialog._waiting is not None:
            dialog._waiting.cancel()
            dialog._waiting = None
        dialog.url = url[:MAX_URL]
        if dialog in self._open and self._sink is not None:
            number = self._number(dialog)
            if number is not None:
                self._tell(lambda sink: sink.dialog_opened(number, dialog))

    def _on_closed(self, page: Page) -> None:
        """A dialog on this page closed: one we answered, or else its oldest open one went without us."""
        if self._answered.get(page, 0) > 0:
            self._answered[page] -= 1
            return
        dialog = next((d for d in self._open if d.page is page), None)
        if dialog is not None:
            self._drop(dialog, ours=False, tell=True)

    def _on_page(self, page: Page) -> None:
        if page in self._watched or self._context is None:
            return
        self._watched.add(page)
        page.on("close", self._on_page_closed)
        self._spawn(self._watch(self._context, page))

    def _on_page_closed(self, page: Page) -> None:
        for dialog in [d for d in self._open if d.page is page]:
            self._drop(dialog, ours=False, tell=True)
        self._watched.discard(page)
        self._sessions.pop(page, None)
        self._openings.pop(page, None)
        self._answered.pop(page, None)

    async def _watch(self, context: BrowserContext, page: Page) -> None:
        try:
            async with asyncio.timeout(self._call_seconds):
                session = await context.new_cdp_session(page)
        except Exception as e:  # noqa: BLE001 - the page is going, or the browser is: no dialogs to keep there
            log.debug("No dialog session for a page: %s", type(e).__name__)
            return
        self._sessions[page] = session
        session.on("Page.javascriptDialogOpening", lambda event: self._on_opening(page, event))
        session.on("Page.javascriptDialogClosed", lambda _: self._on_closed(page))
        # Waits while a dialog is open on the page; the request carries on and the events start afterwards.
        await self._call(session.send("Page.enable"))

    # ---- bookkeeping

    def _number(self, dialog: KeptDialog) -> int | None:
        if dialog in self._numbers:
            return self._numbers[dialog]
        if self._next_number > MAX_NUMBER:
            return None  # left for the classic viewer
        self._numbers[dialog] = number = self._next_number
        self._next_number += 1
        return number

    def _drop(self, dialog: KeptDialog, *, ours: bool, tell: bool) -> None:
        """Forget an open dialog: `ours` when we're about to close it (its closing event is ours to ignore);
        `tell` the attached session it went, if it was showing it."""
        if dialog._waiting is not None:
            dialog._waiting.cancel()
            dialog._waiting = None
        with contextlib.suppress(ValueError):
            self._open.remove(dialog)
        if ours:
            self._answered[dialog.page] = self._answered.get(dialog.page, 0) + 1
        number = self._numbers.pop(dialog, None)
        if number is not None and tell:
            self._tell(lambda sink: sink.dialog_closed(number))

    def _tell(self, call: Callable[[DialogSink], None]) -> None:
        if self._sink is None:
            return
        try:
            call(self._sink)
        except Exception:
            log.exception("A stream session failed to take a dialog")

    def _spawn(self, work: Any) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _call(self, work: Any) -> bool:
        """One browser call, bounded. False when it failed (the dialog had gone, the page or browser too)."""
        try:
            async with asyncio.timeout(self._call_seconds):
                await work
        except Exception as e:  # noqa: BLE001 - never the message: it may quote the page
            log.debug("A dialog call failed: %s", type(e).__name__)
            return False
        return True
