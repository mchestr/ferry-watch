"""A stream session's images (PROTOCOL.md §4, DESIGN §4.5).

The front page's images reach the viewer in one of three modes:
- live: Chromium's screencast (JPEG q50, every frame), each frame acked at once;
- still: a screenshot every 500 ms, sent only when it shows something new, for an animated page nobody is
  touching (a spinner costs 143% of a core in live mode, 19% in still);
- paused: nothing, while the viewer is hidden.

Every start (the session's, a page switch, a return from hidden, a new screen size) is seeded with a screenshot,
since the screencast sends nothing until the page changes. At most 2 binary messages are un-acked and at most 15
go out a second; a newer image replaces one waiting. An image byte-identical to one of the last two sent goes as a
32-byte repeat of its slot. Each one sent is remembered (cast_input.ImageRing) for what the viewer aims at it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final, Literal

from .cast_cdp import CALL_SECONDS, Cdp, CdpError
from .cast_input import ImageInfo, ImageRing
from .cast_pages import PageEntry
from .cast_protocol import FIRST, IMAGE, MAX_JPEG_BYTES, REPEAT, ImageHeader

log = logging.getLogger(__name__)

QUALITY: Final = 50
EVERY_NTH_FRAME: Final = 1  # 2 can skip a change's last frame, leaving a page that went still shown as it was
MAX_PER_SECOND: Final = 15
WINDOW: Final = 2  # binary messages un-acked
STILL_AFTER_SECONDS: Final = 1.0  # without input, ...
STILL_FRAMES: Final = 8  # ... while Chromium delivered this many frames in that second
STILL_EVERY_SECONDS: Final = 0.5
STILL_UNCHANGED: Final = 4  # captures in a row with nothing new: back to live
CAPTURE_SECONDS: Final = 1.0
MODE_CHECK_SECONDS: Final = 0.25
SIZE_CHANGE_SECONDS: Final = 1.0  # images of another size are dropped this long after the screen changed


@dataclass(slots=True)
class _Image:
    page: int
    jpeg: bytes
    w: int
    h: int
    scale: float
    sx: float
    sy: float
    top: float = 0.0
    flags: int = 0
    digest: bytes = b""

    def __post_init__(self) -> None:
        self.digest = hashlib.sha256(self.jpeg).digest()

    @property
    def key(self) -> tuple[object, ...]:
        """What makes it a different image to send (still mode)."""
        return (self.page, self.digest, self.w, self.h, self.scale, self.sx, self.sy)


Mode = Literal["live", "still", "paused"]


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """A JPEG's pixel width and height, from its frame header."""
    i = 2
    while i + 9 < len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4], "big")
        if 0xC0 <= marker <= 0xC3:
            return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
        i += 2 + length
    return None


def _pixels(value: object, low: int = 1, high: int = 65535) -> int:
    number = float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
    return min(max(round(number), low), high) if math.isfinite(number) else low


def _real(value: object, default: float = 0.0) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return min(max(float(value), -1e9), 1e9)
    return default


class FramePump:
    """The front page's images, in one of three modes (PROTOCOL §4.3), through a window of 2 un-acked binary
    messages at no more than 15 a second."""

    def __init__(
        self,
        emit: Callable[[bytes], Awaitable[bool]],
        images: ImageRing,
        *,
        sent: Callable[[int], None] = lambda _: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`emit` sends one binary message (after the session's verdict); `sent` hears each one's seq."""
        self._emit = emit
        self._images = images
        self._sent = sent
        self._clock = clock
        self._lock = asyncio.Lock()  # one change of mode or page at a time, in the order asked
        self._wake = asyncio.Event()
        self.mode: Mode = "live"
        self._visible = True
        self._entry: PageEntry | None = None  # being shown
        self._front_id: int | None = None  # the front page, even while it can't be shown
        self._casting: Cdp | None = None  # the session the screencast runs on
        self._listening: set[Cdp] = set()
        self._still: asyncio.Task[None] | None = None
        self._dsf = 1
        self._expect: tuple[int, int, float] | None = None  # (w, h, until) after a screen change
        self._pending: _Image | None = None
        self._last_key: tuple[object, ...] | None = None
        self._last_size: tuple[int, int] | None = None
        self._slots: OrderedDict[bytes, int] = OrderedDict()  # digest -> slot, least recently used first
        self._unacked: set[int] = set()
        self._seq = 0
        self._sent_at = -math.inf
        self._frames: deque[float] = deque()  # Chromium's screencast frames in the last second
        self._last_input = clock()
        self._side: set[asyncio.Task[Any]] = set()  # frame acks, and the way back from still

    @property
    def seq(self) -> int:
        """The newest binary message's seq (0 before the first)."""
        return self._seq

    def note_input(self) -> None:
        """A viewer message other than `ack` arrived."""
        self._last_input = self._clock()

    def ack(self, seq: int) -> None:
        if seq in self._unacked:
            self._unacked.discard(seq)
            self._wake.set()

    async def show(self, entry: PageEntry | None, dsf: int) -> None:
        """Show this page (seeded with a screenshot, then live), or nothing for now (None: no page, or one a
        dialog blocks)."""
        async with self._lock:
            self._dsf = dsf
            await self._halt()
            first = entry is not None and entry.id != self._front_id
            if entry is not None:
                self._front_id = entry.id
            if self._pending is not None and (entry is None or self._pending.page != entry.id):
                self._pending = None
            self._entry = entry
            if entry is None or not self._visible:
                return
            await self._seed(entry, FIRST if first else 0)
            await self._go_live()

    def resizing(self, w: int, h: int, dsf: int) -> None:
        """The screen is changing: images of another size are from before it, for a while."""
        self._dsf = dsf
        self._expect = (w, h, self._clock() + SIZE_CHANGE_SECONDS)
        if self._entry is not None:
            self._entry.size = None

    async def resized(self) -> None:
        """The page reports the new size: an image of it now, even if the page doesn't change again."""
        async with self._lock:
            entry = self._entry
            if entry is not None and self._visible:
                await self._seed(entry, 0)

    async def visible(self, shown: bool) -> None:
        """§3.8: hidden, nothing at all; shown again, a fresh screenshot at once, then live."""
        async with self._lock:
            if shown == self._visible:
                return
            self._visible = shown
            if not shown:
                await self._halt()
                self.mode = "paused"
                self._pending = None
                return
            entry = self._entry
            if entry is not None:
                await self._seed(entry, 0)
                await self._go_live()

    async def input(self) -> None:
        """A viewer message is about to be carried out: from still, the screencast restarts first."""
        self._last_input = self._clock()
        if self.mode == "still":
            async with self._lock:
                if self._polling() and self._entry is not None:
                    await self._halt()
                    await self._go_live()

    async def run(self) -> None:
        """Send images as the window and the rate allow, and watch for an animated page nobody touches."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._send_loop())
            group.create_task(self._watch_mode())

    async def stop(self) -> None:
        """Teardown: no more images, the screencast stopped. Bounded; never raises."""
        self._visible = False
        try:
            async with asyncio.timeout(CALL_SECONDS):
                async with self._lock:
                    await self._halt()
        except TimeoutError:
            log.debug("The screencast didn't stop in time")
        for task in self._side:
            task.cancel()

    # ---- modes

    async def _halt(self) -> None:
        """Stop the screencast or the still-image polling (under the lock)."""
        if self._still is not None:
            self._still.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._still
            self._still = None
        casting, self._casting = self._casting, None
        if casting is not None:
            with contextlib.suppress(CdpError):
                await casting.send("Page.stopScreencast")

    async def _go_live(self) -> None:
        entry = self._entry
        if entry is None or entry.cdp is None:
            return
        cdp = entry.cdp
        if cdp not in self._listening:
            self._listening.add(cdp)
            cdp.on("Page.screencastFrame", lambda event: self._on_frame(cdp, event))
        self.mode = "live"
        self._frames.clear()
        self._casting = cdp
        try:
            await cdp.send(
                "Page.startScreencast", {"format": "jpeg", "quality": QUALITY, "everyNthFrame": EVERY_NTH_FRAME}
            )
        except CdpError as e:
            log.debug("No screencast: %s", e)  # the page went; its seeded image is what the viewer has

    async def _watch_mode(self) -> None:
        while True:
            await asyncio.sleep(MODE_CHECK_SECONDS)
            now = self._clock()
            while self._frames and now - self._frames[0] > STILL_AFTER_SECONDS:
                self._frames.popleft()
            quiet = now - self._last_input >= STILL_AFTER_SECONDS
            if self.mode == "live" and self._casting is not None and quiet and len(self._frames) >= STILL_FRAMES:
                async with self._lock:
                    if not self._polling() and self._entry is not None and self._casting is not None:
                        await self._halt()
                        self.mode = "still"
                        self._still = asyncio.ensure_future(self._poll(self._entry))

    async def _poll(self, entry: PageEntry) -> None:
        """Still mode: a screenshot every 500 ms, sent only when it shows something new; back to live after 4 in a
        row that don't (or on input)."""
        unchanged = 0
        while True:
            started = self._clock()
            image = await self._capture(entry, CAPTURE_SECONDS)
            if image is not None and image.key != self._last_key:
                self._queue(image)
                unchanged = 0
            elif image is not None:
                unchanged += 1
            if unchanged >= STILL_UNCHANGED:
                self._aside(self._back_to_live(entry))
                return
            await asyncio.sleep(max(0.0, STILL_EVERY_SECONDS - (self._clock() - started)))

    def _polling(self) -> bool:
        """In still mode (asked again after waiting for the lock: another change may have come first)."""
        return self.mode == "still"

    async def _back_to_live(self, entry: PageEntry) -> None:
        async with self._lock:
            if self._polling() and self._entry is entry:
                self._still = None
                await self._go_live()

    # ---- images in

    async def _seed(self, entry: PageEntry, flags: int) -> None:
        """A screenshot-based image (the screencast sends nothing until the page changes)."""
        image = await self._capture(entry, CALL_SECONDS)
        if image is not None:
            image.flags |= flags
            self._queue(image)

    async def _capture(self, entry: PageEntry, seconds: float) -> _Image | None:
        cdp = entry.cdp
        if cdp is None or entry.blocked:
            return None
        try:
            metrics = await cdp.send("Page.getLayoutMetrics")
            shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": QUALITY}, seconds=seconds)
        except CdpError as e:
            log.debug("No screenshot: %s", e)
            return None
        jpeg = _jpeg(shot.get("data"))
        if jpeg is None:
            return None
        viewport = metrics.get("cssVisualViewport", {})
        size = entry.size or jpeg_size(jpeg)
        if size is None:
            return None
        w, h = size if entry.size else (round(size[0] / self._dsf), round(size[1] / self._dsf))
        return _Image(
            entry.id,
            jpeg,
            _pixels(w),
            _pixels(h),
            _real(viewport.get("scale"), 1.0),
            _real(viewport.get("pageX")),
            _real(viewport.get("pageY")),
        )

    def _on_frame(self, cdp: Cdp, event: dict[str, Any]) -> None:
        session_id = event.get("sessionId")
        if isinstance(session_id, int):  # acked at once, whatever becomes of it
            self._aside(_quietly(cdp.send("Page.screencastFrameAck", {"sessionId": session_id})))
        entry = self._entry
        if cdp is not self._casting or entry is None or self.mode != "live" or not self._visible:
            return
        now = self._clock()
        self._frames.append(now)
        meta = event.get("metadata", {})
        w, h = _pixels(meta.get("deviceWidth")), _pixels(meta.get("deviceHeight"))
        expect = self._expect
        if expect is not None and now < expect[2] and (w != expect[0] or (entry.main and h != expect[1])):
            return  # from before the screen changed
        jpeg = _jpeg(event.get("data"))
        if jpeg is None:
            return
        entry.size = (w, h)
        scale = _real(meta.get("pageScaleFactor"), 1.0)
        sx, sy, top = _real(meta.get("scrollOffsetX")), _real(meta.get("scrollOffsetY")), _real(meta.get("offsetTop"))
        self._queue(_Image(entry.id, jpeg, w, h, scale, sx, sy, top))

    def _aside(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(work)
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    def _queue(self, image: _Image) -> None:
        """Newest wins: a pending image of the same page passes its flags on."""
        pending = self._pending
        if pending is not None and pending.page == image.page:
            image.flags |= pending.flags
        self._pending = image
        self._last_key = image.key
        self._wake.set()

    # ---- images out

    async def _send_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending is not None and len(self._unacked) < WINDOW:
                if (wait := self._sent_at + 1 / MAX_PER_SECOND - self._clock()) > 0:
                    await asyncio.sleep(wait)
                    continue
                image, self._pending = self._pending, None
                if not await self._send(image):
                    return

    async def _send(self, image: _Image) -> bool:
        flags = image.flags | (FIRST if (image.w, image.h) != self._last_size else 0)
        if image.digest in self._slots:
            kind, slot, payload = REPEAT, self._slots[image.digest], b""
            self._slots.move_to_end(image.digest)
        else:
            free = {0, 1} - set(self._slots.values())
            slot = min(free) if free else self._slots.popitem(last=False)[1]
            self._slots[image.digest] = slot
            kind, payload = IMAGE, image.jpeg
        self._seq += 1
        header = ImageHeader(
            kind, slot, image.page, self._seq, image.w, image.h, image.scale, image.sx, image.sy, image.top, self._dsf,
            flags,
        )  # fmt: skip
        self._images.add(ImageInfo(self._seq, image.page, image.w, image.h, image.scale, image.sx, image.sy))
        self._unacked.add(self._seq)
        self._sent_at = self._clock()
        self._last_size = (image.w, image.h)
        if not await self._emit(header.pack() + payload):
            return False
        self._sent(self._seq)
        return True


def _jpeg(data: object) -> bytes | None:
    """A screencast frame's or screenshot's image, if it's one the protocol can carry."""
    if not isinstance(data, str) or len(data) > (MAX_JPEG_BYTES + 2) // 3 * 4:
        return None
    try:
        jpeg = base64.b64decode(data, validate=True)
    except binascii.Error:
        return None
    return jpeg if 0 < len(jpeg) <= MAX_JPEG_BYTES else None


async def _quietly(work: Awaitable[Any]) -> None:
    with contextlib.suppress(CdpError):
        await work
