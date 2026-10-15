"""The streamed checkout viewer's session: one admitted `fw.cast1` connection (PROTOCOL.md, DESIGN §6.3).

The gate runs a `CastSession` for each stream it admits. The session waits for the viewer's `hello`, sets the
pod's screen to the viewer's size (screen.py), and from then on sends the checkout page as JPEG images
(cast_frames.py), with the page list (cast_pages.py), the hold's clock, JavaScript dialogs (dialogs.py), the
field map and focus reports (cast_fields.py), while it carries out the viewer's input one message at a time
(cast_input.py). Here:

- `RateLimiter` and `InboundQueue`: §7.1's buckets and drop limit, and the bounded, coalescing queue of §3.4;
- `CastSession`: the lifecycle, and the authorization of everything: `verdict()` (the gate's, from the watcher)
  is asked before carrying out any message but `ack` and `visible`, before sending anything, and every second,
  and its code closes the connection at once (C35).

Every browser call goes through the allowlist (cast_cdp.Cdp) and is bounded. A page with an open JavaScript
dialog answers almost no CDP (DESIGN §13), so nothing waits on such a page: the session's opening messages go
out without it, and its images, field map and input wait until the dialog is answered. Typed text is never
logged (C37); nothing here logs a message's content.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from aiohttp import WSMsgType, web

from .cast_cdp import CALL_SECONDS, Cdp, CdpError, Worlds
from .cast_fields import FieldMapper, FocusReporter, Front
from .cast_frames import FramePump
from .cast_input import ImageRing, InputMapper
from .cast_pages import PageEntry, PageList
from .cast_protocol import (
    DROP_WINDOW_SECONDS,
    MAX_DROPS,
    MAX_QUEUED,
    OVERALL_RATE,
    RATES,
    Ack,
    Answer,
    Choose,
    Clock,
    CloseCode,
    Compose,
    DialogClosed,
    Error,
    ErrorCode,
    Hello,
    Inbound,
    InboundType,
    Key,
    Move,
    Outbound,
    PageOp,
    Pinch,
    ProtocolViolation,
    Ready,
    Resize,
    SetDate,
    Tap,
    Text,
    Visible,
    Wheel,
    check,
    decode,
    dialog_message,
    encode,
    message_type,
    numbered,
    scale_factor,
)
from .cast_scripts import VIEWPORT

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

    from .dialogs import DialogSink, KeptDialog

log = logging.getLogger(__name__)

HELLO_SECONDS: Final = 5.0
TICK_SECONDS: Final = 1.0  # verdict, clock and page titles
SETTLE_SECONDS: Final = 1.0  # for the page to report a new screen size
SETTLE_POLL_SECONDS: Final = 0.02
RESIZE_QUIET_SECONDS: Final = 0.3
MAX_SIZES: Final = 16  # distinct screen sizes in one session
ERROR_EVERY_SECONDS: Final = 1.0  # one `error` a second per code
MAX_MERGED_DELTA: Final = 8000.0
LOCALE: Final = "en-US"

# What the field map waits for (PROTOCOL §5.3.4), once carried out.
_MAP_AFTER: Final = (Tap, Wheel, Key, Text, Compose, Choose, SetDate, Answer, PageOp, Pinch, Resize)
_FOCUS_KEYS: Final = frozenset({"Tab", "Enter", "Escape"})


class ScreenControl(Protocol):
    async def apply(self, w: int, h: int, dsf: int) -> bool:
        """The pod's screen at this size and scale factor (screen.py). False when it already was."""


class DialogControl(Protocol):
    def attach(self, sink: DialogSink) -> list[tuple[int, KeptDialog]]: ...
    def detach(self, sink: DialogSink) -> None: ...
    async def answer(self, number: int, ok: bool, text: str | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class CastPod:
    """What a stream session needs of the agent."""

    target: Callable[[], tuple[BrowserContext, Page] | None]  # the held cart's browser context and page
    clock: Callable[[], Clock | None]  # the hold's clocks (TripWatcher.clock_info)
    screen: ScreenControl
    dialogs: DialogControl


def factory(pod: CastPod) -> Callable[[web.WebSocketResponse, Callable[[], CloseCode | None]], CastSession]:
    """The gate's `cast` factory."""

    def make(ws: web.WebSocketResponse, verdict: Callable[[], CloseCode | None]) -> CastSession:
        return CastSession(ws, verdict, pod)

    return make


# ---- rates (§7.1)


@dataclass(slots=True)
class _Bucket:
    rate: float
    burst: float
    tokens: float
    at: float

    def has(self, amount: float, now: float) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        return self.tokens >= amount


class RateLimiter:
    """A token bucket per message type (two for `text`: messages and code points), one for everything, and the
    drop limit: more than 200 dropped messages in any 10 s, or a message over the overall bucket, closes 4429."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        now = clock()
        self._buckets = {t: _Bucket(r.rate, r.burst, r.burst, now) for t, r in RATES.items()}
        text = RATES["text"]
        self._code_points = _Bucket(text.cp_rate, text.cp_burst, text.cp_burst, now)
        self._overall = _Bucket(OVERALL_RATE.rate, OVERALL_RATE.burst, OVERALL_RATE.burst, now)
        self._drops: deque[float] = deque()

    def admit(self, message_type: str, code_points: int = 0) -> Literal["ok", "rate", "close"]:
        """A message that passed the value rules: admitted (taking its tokens), dropped, or the end."""
        now = self._clock()
        bucket = self._buckets[message_type]
        over = not bucket.has(1, now) or (message_type == "text" and not self._code_points.has(code_points, now))
        if over:
            return "close" if self._dropped(now) else "rate"
        if not self._overall.has(1, now):
            return "close"
        bucket.tokens -= 1
        self._overall.tokens -= 1
        if message_type == "text":
            self._code_points.tokens -= code_points
        return "ok"

    def invalid(self) -> Literal["invalid", "close"]:
        """A message dropped by the value rules (it takes no tokens, but counts toward the drop limit)."""
        return "close" if self._dropped(self._clock()) else "invalid"

    def _dropped(self, now: float) -> bool:
        self._drops.append(now)
        while now - self._drops[0] >= DROP_WINDOW_SECONDS:
            self._drops.popleft()
        return len(self._drops) > MAX_DROPS


# ---- the inbound queue (§3.4)


@dataclass(slots=True)
class Waiting:
    """A message waiting to be carried out, with the numbers of the messages merged into it."""

    message: Inbound
    numbers: list[int]
    arrived: float


class InboundQueue:
    """Messages waiting to be carried out, in order, at most 64. A `wheel` arriving behind a waiting `wheel` for
    the same page is summed into it (while each sum stays within ±8000); a `move` behind a `move` replaces it."""

    def __init__(self, limit: int = MAX_QUEUED) -> None:
        self._limit = limit
        self._waiting: deque[Waiting] = deque()
        self._ready = asyncio.Event()

    @property
    def waiting(self) -> list[Inbound]:
        return [w.message for w in self._waiting]

    def head(self) -> Inbound | None:
        return self._waiting[0].message if self._waiting else None

    def put(self, message: Inbound, number: int, arrived: float = 0.0) -> bool:
        """False when it would be one too many (the connection closes 4429)."""
        last = self._waiting[-1] if self._waiting else None
        if last is not None and isinstance(message, Wheel) and isinstance(last.message, Wheel):
            dx, dy = last.message.dx + message.dx, last.message.dy + message.dy
            if last.message.page == message.page and abs(dx) <= MAX_MERGED_DELTA and abs(dy) <= MAX_MERGED_DELTA:
                last.message = Wheel(page=message.page, x=message.x, y=message.y, dx=dx, dy=dy)
                last.numbers.append(number)
                return True
        if last is not None and isinstance(message, Move) and isinstance(last.message, Move):
            last.message = message
            last.numbers.append(number)
            return True
        if len(self._waiting) >= self._limit:
            return False
        self._waiting.append(Waiting(message, [number], arrived))
        self._ready.set()
        return True

    async def get(self) -> Waiting:
        while not self._waiting:
            self._ready.clear()
            await self._ready.wait()
        return self._waiting.popleft()


class Numbers:
    """Message numbers (§1): every viewer message but `ack` gets the next; `n` is how many of them are carried out
    (all of 1..n, so a message applied out of turn doesn't count before the ones in front of it)."""

    def __init__(self) -> None:
        self._next = 1
        self._done: set[int] = set()
        self.n = 0

    def take(self) -> int:
        number, self._next = self._next, self._next + 1
        return number

    def done(self, *numbers: int | None) -> None:
        self._done.update(n for n in numbers if n is not None)
        while self.n + 1 in self._done:
            self.n += 1
            self._done.remove(self.n)


# ---- the session


class _Known:
    """What the value rules need from the session (cast_protocol.Known)."""

    def __init__(self, images: ImageRing, mapper: FieldMapper) -> None:
        self._images = images
        self._mapper = mapper

    def image(self, seq: int) -> tuple[int, int] | None:
        image = self._images.get(seq)
        return (image.w, image.h) if image is not None else None

    def latest(self, page: int) -> tuple[int, int] | None:
        image = self._images.latest(page)
        return (image.w, image.h) if image is not None else None

    def field_kind(self, rev: int, field: int) -> str | None:
        revision = self._mapper.revision(rev)
        ref = revision.refs.get(field) if revision is not None else None
        return ref.k if ref is not None else None


@dataclass(slots=True)
class _Screen:
    w: int = 0
    h: int = 0
    dsf: int = 1
    sizes: set[tuple[int, int, int]] = field(default_factory=set)


class CastSession:
    """One streamed viewer's connection. The gate calls `run()` (it reads and writes the socket until either side
    ends it) and then `close()` (the teardown), whatever ended it."""

    def __init__(
        self,
        ws: web.WebSocketResponse,
        verdict: Callable[[], CloseCode | None],
        pod: CastPod,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ws = ws
        self._verdict = verdict
        self._pod = pod
        self._clock = clock
        self._ended = asyncio.Event()
        self._code: int | None = None
        self._closed = False
        self._ready = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._numbers = Numbers()
        self._limiter = RateLimiter(clock)
        self._queue = InboundQueue()
        self._images = ImageRing()
        self._input = InputMapper(self._images)
        self._pump = FramePump(self._emit, self._images, sent=self._image_sent, clock=clock)
        self._mapper = FieldMapper(self._front_worlds, self._progress, self._send, clock=clock)
        self._focus = FocusReporter(self._front_worlds, self._progress, self._send, clock=clock)
        self._known = _Known(self._images, self._mapper)
        self._screen = _Screen()
        self._context: BrowserContext | None = None
        self._pages: PageList | None = None
        self._dialogs: dict[int, Page] = {}  # dialog number -> its page
        self._early: list[Outbound] = []  # dialogs that opened before `ready` went out
        self._attached = False
        self._errors: dict[str, float] = {}
        self._clock_sent: Clock | None = None
        self._locale: str | None = None

    # ---- what the gate calls

    async def run(self) -> None:
        hello = await self._hello()
        if hello is not None:
            self._spawn(self._read())
            await self._start(hello)
            await self._ended.wait()
        for task in self._tasks:
            task.cancel()  # nothing more is carried out or sent; close() finishes them
        if self._code is not None and not self._ws.closed:
            await self._ws.close(code=self._code)

    async def close(self) -> None:
        """Teardown (PROTOCOL §3.5): bounded, and it leaves the screen and any open dialog as they are."""
        if self._closed:
            return
        self._closed = True
        self._ended.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(CALL_SECONDS):
                await self._input.release()
        await self._pump.stop()
        if self._attached:
            self._pod.dialogs.detach(self)
        pages = self._pages
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.remove_listener("page", self._on_page)
        if pages is not None:
            for entry in pages.listed:
                with contextlib.suppress(Exception):
                    entry.page.remove_listener("close", self._on_page_closed)
            await asyncio.gather(
                *(e.worlds.close() for e in pages.listed if e.worlds is not None),
                *(e.cdp.detach() for e in pages.listed if e.cdp is not None),
            )

    # ---- the dialog keeper's (DialogSink)

    def dialog_opened(self, number: int, dialog: KeptDialog) -> None:
        entry = self._pages.of(dialog.page) if self._pages is not None else None
        if entry is None:
            return  # not a page the viewer knows: it stays for the classic viewer
        self._dialogs[number] = dialog.page
        entry.dialogs += 1
        # The page sends no frames now and answers almost nothing: images, the map and input wait (entry.blocked).
        message = dialog_message(number, entry.id, dialog.kind, dialog.url or "", dialog.message, dialog.default)
        if self._ready:
            self._spawn(self._send(message))
        else:
            self._early.append(message)

    def dialog_closed(self, number: int) -> None:
        page = self._dialogs.pop(number, None)
        if page is None:
            return
        if self._ready:
            self._spawn(self._send(DialogClosed(id=number)))
        else:
            self._early.append(DialogClosed(id=number))
        self._unblock(page)

    # ---- opening

    async def _hello(self) -> Hello | None:
        try:
            async with asyncio.timeout(HELLO_SECONDS):
                frame = await self._ws.receive()
        except TimeoutError:
            self._end(CloseCode.NO_HELLO)
            return None
        if frame.type is not WSMsgType.TEXT:
            self._end(CloseCode.PROTOCOL if frame.type is WSMsgType.BINARY else None)
            return None
        try:
            hello = decode(frame.data)
        except ProtocolViolation:
            hello = None
        if not isinstance(hello, Hello) or check(hello) is not None:
            self._end(CloseCode.PROTOCOL)
            return None
        self._numbers.take()
        return hello

    async def _start(self, hello: Hello) -> None:
        """§3.2 steps 3-4: the screen, then ready, pages, clock, open dialogs and the first image."""
        target = self._pod.target()
        if target is None:
            self._end(self._verdict() or CloseCode.ENDED)
            return
        context, main = target
        self._context = context
        pages = self._pages = PageList(context, main)
        for page in [main, *(p for p in context.pages if p is not main)]:
            self._track(page)
        context.on("page", self._on_page)
        if pages.of(main) is None:
            self._end(self._verdict() or CloseCode.ENDED)
            return
        opened = self._pod.dialogs.attach(self)  # dialogs opened since are reported to dialog_opened
        self._attached = True
        for number, dialog in opened:
            self.dialog_opened(number, dialog)
        await self._apply(hello.w, hello.h, scale_factor(hello.w, hello.h, hello.dpr))
        dsf: Literal[1, 2] = 2 if self._screen.dsf == 2 else 1
        if not await self._send(Ready(w=hello.w, h=hello.h, dsf=dsf)):
            return
        self._ready = True
        self._numbers.done(1)
        if (listing := await pages.message()) is not None:
            await self._send(listing)
        await self._send_clock()
        early, self._early = self._early, []
        for message in early:
            await self._send(message)
        front = pages.newest()
        if front is not None:
            await self._show(front)
        for work in (self._work(), self._tick(), self._pump.run(), self._mapper.run(), self._focus.run()):
            self._spawn(work)

    async def _apply(self, w: int, h: int, dsf: int) -> bool:
        """The screen at the viewer's size; once changed, until the page reports it (at most 1 s)."""
        changed = await self._pod.screen.apply(w, h, dsf)
        self._screen.w, self._screen.h, self._screen.dsf = w, h, dsf
        self._screen.sizes.add((w, h, dsf))
        if changed:
            self._pump.resizing(w, h, dsf)
            await self._settle(w, h, dsf)
            await self._pump.resized()
        return changed

    async def _settle(self, w: int, h: int, dsf: int) -> None:
        main = self._main_entry()
        if main is None or main.blocked:
            return
        deadline = self._clock() + SETTLE_SECONDS
        with contextlib.suppress(CdpError):
            worlds = await self._worlds(main)
            world = await worlds.get(main.page.main_frame)
            while world is not None and self._clock() < deadline:
                reported = await worlds.evaluate(world, VIEWPORT)
                if isinstance(reported, list) and reported[:3] == [w, h, dsf]:
                    return
                await asyncio.sleep(SETTLE_POLL_SECONDS)

    # ---- pages

    def _main_entry(self) -> PageEntry | None:
        pages = self._pages
        return next((e for e in pages.listed if e.main), None) if pages is not None else None

    def _track(self, page: Page) -> PageEntry | None:
        pages = self._pages
        if pages is None:
            return None
        known = pages.of(page) is not None
        entry = pages.track(page)
        if entry is not None and not known:
            page.on("close", self._on_page_closed)
        return entry

    def _on_page(self, page: Page) -> None:
        """A pop-up or new tab: it becomes the front page (if it is listed)."""
        entry = self._track(page)
        if entry is not None and self._ready:
            self._spawn(self._new_front(entry))

    async def _new_front(self, entry: PageEntry) -> None:
        await self._show(entry)
        await self._send_pages()

    def _on_page_closed(self, page: Page) -> None:
        pages = self._pages
        if pages is None:
            return
        was_front = pages.front is not None and pages.front.page is page
        entry = pages.remove(page)
        if entry is None:
            return
        for number in [n for n, p in self._dialogs.items() if p is page]:
            del self._dialogs[number]
        if entry.main:
            self._end(self._verdict() or CloseCode.ENDED)  # the cart's own page went
            return
        self._spawn(self._closed_page(entry, was_front))

    async def _closed_page(self, entry: PageEntry, was_front: bool) -> None:
        pages = self._pages
        if pages is not None and was_front and (front := pages.newest()) is not None:
            await self._show(front)
        await self._send_pages()
        await asyncio.gather(
            *([entry.worlds.close()] if entry.worlds is not None else []),
            *([entry.cdp.detach()] if entry.cdp is not None else []),
        )

    async def _show(self, entry: PageEntry) -> None:
        """Make it the front page: its images, its map, and input to it."""
        pages = self._pages
        if pages is None:
            return
        pages.front = entry
        self._mapper.reset()
        try:
            cdp = await pages.cdp(entry)
        except CdpError:
            self._input.aim(entry.id, None)
            await self._pump.show(None, self._screen.dsf)
            return
        self._input.aim(entry.id, cdp)
        if entry.blocked:
            await self._pump.show(None, self._screen.dsf)  # until the dialog is answered
            return
        await self._worlds(entry)
        with contextlib.suppress(Exception):
            async with asyncio.timeout(CALL_SECONDS):
                await entry.page.bring_to_front()
        await self._pump.show(entry, self._screen.dsf)

    async def _worlds(self, entry: PageEntry) -> Worlds:
        if entry.worlds is None:
            cdp = await self._pages_cdp(entry)
            if entry.worlds is None:
                assert self._context is not None  # noqa: S101 - pages exist only once there is a context
                entry.worlds = Worlds(self._context, entry.page, cdp)
        return entry.worlds

    async def _pages_cdp(self, entry: PageEntry) -> Cdp:
        if self._pages is None:
            raise CdpError("no pages")
        return await self._pages.cdp(entry)

    def _front_worlds(self) -> Front | None:
        """The front page for the field map and focus: None while there's none, or a dialog blocks it."""
        front = self._pages.front if self._pages is not None else None
        if front is None or front.blocked or front.worlds is None:
            return None
        return front.id, front.worlds

    def _unblock(self, page: Page | None) -> None:
        entry = self._pages.of(page) if self._pages is not None and page is not None else None
        if entry is None:
            return
        entry.dialogs = max(0, entry.dialogs - 1)
        if not entry.blocked and self._pages is not None and self._pages.front is entry and self._ready:
            self._spawn(self._show(entry))  # the page answers again: its images, map and input resume

    async def _send_pages(self) -> None:
        if self._pages is not None and (listing := await self._pages.message()) is not None:
            await self._send(listing)

    # ---- inbound

    async def _read(self) -> None:
        async for frame in self._ws:
            if frame.type is WSMsgType.TEXT:
                self._inbound(frame.data)
            else:
                self._end(CloseCode.PROTOCOL if frame.type is WSMsgType.BINARY else None)
                return
            if self._ended.is_set():
                return
        self._end(None)  # the viewer left

    def _inbound(self, text: str) -> None:
        """§6's two steps, the rates, then the verdict, then the queue (ack and visible are applied at once)."""
        try:
            message = decode(text)
        except ProtocolViolation as e:
            self._end(e.code)
            return
        if isinstance(message, Hello) or not self._ready:
            self._end(CloseCode.PROTOCOL)  # a second hello, or anything before ready
            return
        kind = message_type(message)
        number = self._numbers.take() if numbered(kind) else None
        if not self._admitted(message, kind):
            self._numbers.done(number)  # dropped, and answered unless the connection is closing
        elif isinstance(message, Ack):
            self._pump.ack(message.seq)
        elif isinstance(message, Visible):
            self._pump.note_input()
            self._spawn(self._visible(message.v, number))
        elif (code := self._verdict()) is not None:
            self._end(code)
        else:
            self._pump.note_input()
            assert number is not None  # noqa: S101 - every type but ack is numbered
            if not self._queue.put(message, number, self._clock()):
                self._end(CloseCode.RATE)

    def _admitted(self, message: Inbound, kind: str) -> bool:
        """The value rules (§6 step 2), then the rates (§7.1). A message that fails either is answered (`invalid`,
        `rate`), or the connection closes 4429 at the drop limit."""
        if check(message, self._known) is not None:
            outcome: str = self._limiter.invalid()
        else:
            outcome = self._limiter.admit(kind, len(message.s) if isinstance(message, Text) else 0)
        if outcome == "close":
            self._end(CloseCode.RATE)
        elif outcome != "ok":
            self._error("invalid" if outcome == "invalid" else "rate", kind)
        return outcome == "ok"

    async def _visible(self, shown: bool, number: int | None) -> None:
        await self._pump.visible(shown)
        self._numbers.done(number)

    async def _work(self) -> None:
        """Carry the queued messages out, one at a time, in order (§3.4)."""
        while True:
            waiting = await self._queue.get()
            if (code := self._verdict()) is not None:
                self._end(code)
                return
            message = waiting.message
            list_open, self._focus.popup = self._focus.popup and isinstance(message, Choose), False
            try:
                await self._pump.input()
                await self._carry_out(message, waiting.arrived, list_open)
            except CdpError as e:
                log.debug("A %s wasn't carried out: %s", message_type(message), e)
                self._error("failed", message_type(message))
            finally:
                self._numbers.done(*waiting.numbers)
            if isinstance(message, _MAP_AFTER):
                self._mapper.due()

    async def _carry_out(self, message: Inbound, arrived: float, list_open: bool) -> None:
        front = self._pages.front if self._pages is not None else None
        blocked = front is None or front.blocked  # nothing reaches a page a dialog blocks (§5.5)
        error: ErrorCode | None = None
        if isinstance(message, Tap | Move | Wheel | Pinch):
            error = await self._pointer(message, blocked)
        elif isinstance(message, Key | Text | Compose):
            error = "stale" if blocked else await self._type(message)
        elif isinstance(message, Choose | SetDate):
            error = await self._picker(message, list_open)
            self._focus.after()
        elif isinstance(message, Answer):
            await self._answer(message)
        elif isinstance(message, PageOp):
            error = await self._page_op(message)
        elif isinstance(message, Resize):
            error = await self._resize(message, arrived)
        if error is not None:
            self._error(error, message_type(message))

    async def _pointer(self, message: Tap | Move | Wheel | Pinch, blocked: bool) -> ErrorCode | None:
        if isinstance(message, Move | Wheel):
            if not blocked:  # a move or wheel nobody can see go anywhere is simply dropped
                await (self._input.move(message) if isinstance(message, Move) else self._input.wheel(message))
            return None
        if blocked:
            return "stale"
        if isinstance(message, Pinch):
            return await self._input.pinch(message)
        error = await self._input.tap(message)
        if error is None:
            self._focus.after((message.x, message.y))
        return error

    async def _type(self, message: Key | Text | Compose) -> ErrorCode | None:
        if isinstance(message, Key):
            await self._input.key(message)
            if message.k in _FOCUS_KEYS:
                self._focus.after()
                return None
        elif isinstance(message, Text):
            await self._input.text(message)
        else:
            await self._input.compose(message)
        self._focus.typing()
        return None

    async def _picker(self, message: Choose | SetDate, list_open: bool) -> ErrorCode | None:
        """§6.11-6.12, for a field of a map revision the agent still holds, on the front page."""
        front = self._pages.front if self._pages is not None else None
        revision = self._mapper.revision(message.rev)
        ref = revision.refs.get(message.f) if revision is not None else None
        usable = front is not None and not front.blocked and message.page == front.id
        if not usable or revision is None or ref is None or revision.page != message.page:
            return "stale"
        if isinstance(message, Choose):
            if ref.k != "select" or ref.opts is None:
                return "stale"
            return await self._input.choose(revision.worlds, ref, message.i, list_open=list_open)
        if ref.k not in ("date", "time"):
            return "stale"
        if not (await self._locale_of()).startswith(LOCALE):
            return "unsupported"
        return await self._input.date(revision.worlds, ref, message.v)

    async def _locale_of(self) -> str:
        """navigator.language, read once a session in the main page's main frame."""
        if self._locale is None:
            main = self._main_entry()
            if main is None or main.blocked:
                return ""
            worlds = await self._worlds(main)
            world = await worlds.get(main.page.main_frame)
            reported = await worlds.evaluate(world, VIEWPORT) if world is not None else None
            self._locale = str(reported[8]) if isinstance(reported, list) and len(reported) > 8 else ""
        return self._locale

    async def _answer(self, message: Answer) -> None:
        page = self._dialogs.get(message.id)
        if page is None:
            return  # unknown or already closed: ignored
        text = message.s if isinstance(message.s, str) else None
        if await self._pod.dialogs.answer(message.id, message.ok, text):
            self._dialogs.pop(message.id, None)
            self._unblock(page)
        self._focus.after()

    async def _page_op(self, message: PageOp) -> ErrorCode | None:
        pages = self._pages
        entry = pages.by_id(message.page) if pages is not None else None
        if entry is None:
            return None  # unknown pages are ignored
        if message.op == "show":
            await self._show(entry)
            await self._send_pages()
            self._focus.after()
            return None
        if entry.main:
            return "failed"
        try:
            async with asyncio.timeout(CALL_SECONDS):
                await entry.page.close(run_before_unload=True)
        except Exception as e:  # noqa: BLE001 - it's going anyway, or a beforeunload holds it
            log.debug("A page didn't close: %s", type(e).__name__)
        return None

    async def _resize(self, message: Resize, arrived: float) -> ErrorCode | None:
        """§6.2: the latest size, once 300 ms pass without another; at most 16 distinct sizes a session."""
        if (wait := arrived + RESIZE_QUIET_SECONDS - self._clock()) > 0:
            await asyncio.sleep(wait)
        if isinstance(self._queue.head(), Resize):
            return None  # a later one replaces it
        dsf = scale_factor(message.w, message.h, message.dpr)
        if (message.w, message.h, dsf) not in self._screen.sizes and len(self._screen.sizes) >= MAX_SIZES:
            return "rate"
        await self._apply(message.w, message.h, dsf)
        return None

    # ---- outbound

    async def _send(self, message: Outbound) -> bool:
        """One JSON message, if the viewer may still have it (C35)."""
        if not self._authorized():
            return False
        try:
            await self._ws.send_str(encode(message).decode())
        except ConnectionError:
            self._end(None)
            return False
        return True

    async def _emit(self, data: bytes) -> bool:
        """One binary message, if the viewer may still have it (C35)."""
        if not self._authorized():
            return False
        try:
            await self._ws.send_bytes(data)
        except ConnectionError:
            self._end(None)
            return False
        return True

    def _authorized(self) -> bool:
        if self._ended.is_set():
            return False
        if (code := self._verdict()) is not None:
            self._end(code)
            return False
        return True

    def _error(self, code: ErrorCode, ref: str) -> None:
        """At most one `error` a second for each code (§5.7)."""
        now = self._clock()
        if now - self._errors.get(code, -math.inf) < ERROR_EVERY_SECONDS:
            return
        self._errors[code] = now
        self._spawn(self._send(Error(code=code, ref=cast(InboundType, ref))))

    async def _send_clock(self) -> None:
        clock = self._pod.clock()
        if clock is not None and clock != self._clock_sent and await self._send(clock):
            self._clock_sent = clock

    def _image_sent(self, seq: int) -> None:
        self._mapper.image_sent(seq)

    def _progress(self) -> tuple[int, int]:
        return self._pump.seq, self._numbers.n

    async def _tick(self) -> None:
        """Every second: the verdict, the clock, and the pages' titles."""
        titles: list[tuple[str, str]] | None = None
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if (code := self._verdict()) is not None:
                self._end(code)
                return
            await self._send_clock()
            if self._pages is not None and (listing := await self._pages.message()) is not None:
                now = [(p.title, p.origin) for p in listing.pages]
                if titles is not None and now != titles:
                    await self._send(listing)
                titles = now

    # ---- the session's own tasks

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> None:
        if self._closed:
            work.close()
            return
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if isinstance(error, CdpError):
            log.debug("A stream task stopped: %s", error)  # a page went, or didn't answer: the session goes on
        elif error is not None:
            log.warning("The checkout stream failed: %s", type(error).__name__)  # never its text: page or typed
            self._end(CloseCode.INTERNAL)

    def _end(self, code: int | None) -> None:
        """Close with this code (None: the viewer left), once; nothing is carried out or sent after it."""
        if not self._ended.is_set():
            self._code = code
            self._ended.set()
