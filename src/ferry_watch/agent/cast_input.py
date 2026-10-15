"""A stream viewer's input, carried out on the pod's page (PROTOCOL.md §6.4-6.12, DESIGN §4.3, §6.5).

Everything is mouse, wheel or keyboard, as from a Mac with a trackpad: no touch, no emulation. A tap or a pinch
names the image it was aimed at, and is refused (`error stale`, nothing sent) when the page has moved since.
Keys come from a fixed table with no modifier but Shift (and Ctrl for word-wise editing); held keys are tracked,
repeated as the viewer's keyboard repeats them, and released at teardown. `choose` and `date` never click a point
where anything but the field would get the click, and press no key until the field has focus (C40).

Coordinates: the viewer sends frame px of the image; CDP Input takes CSS px of the visual viewport, so a point
(x, y) is dispatched at (x/s, y/s) with s the page scale (PROTOCOL §1).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import msgspec

from .cast_cdp import Cdp, CdpError, World, Worlds
from .cast_fields import FieldRef, lift
from .cast_protocol import Compose, ErrorCode, Key, Move, Pinch, Tap, Text, Wheel
from .cast_scripts import CLICK_POINTS, IS_FOCUSED, OWNER_HIT

log = logging.getLogger(__name__)

KEPT_IMAGES: Final = 16  # binary messages remembered for taps and pinches (PROTOCOL §4.2)
MAX_HELD: Final = 6
MOVED_CSS_PX: Final = 1.0  # a tap's image is stale once the page scrolled further than this ...
MOVED_SCALE: Final = 0.01  # ... or zoomed
PINCH_SPEED: Final = 3000  # px/s of the synthetic trackpad pinch
SETTLE_SECONDS: Final = 0.05  # after a picker's click, and after its Enter, before looking
SHIFT: Final = 8
CTRL: Final = 2

# key, code, Windows virtual key code (DESIGN §6.5)
NAMED: Final[Mapping[str, tuple[str, int]]] = {
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowDown": ("ArrowDown", 40),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "Shift": ("ShiftLeft", 16),
}


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """What a binary message showed (its header), for what the viewer aims at it."""

    seq: int
    page: int
    w: int
    h: int
    scale: float
    sx: float
    sy: float


class ImageRing:
    """The last 16 binary messages sent, and each page's latest."""

    def __init__(self) -> None:
        self._kept: deque[ImageInfo] = deque(maxlen=KEPT_IMAGES)
        self._latest: dict[int, ImageInfo] = {}

    def add(self, image: ImageInfo) -> None:
        self._kept.append(image)
        self._latest[image.page] = image

    def get(self, seq: int) -> ImageInfo | None:
        return next((i for i in self._kept if i.seq == seq), None)

    def latest(self, page: int) -> ImageInfo | None:
        return self._latest.get(page)


@dataclass(frozen=True, slots=True)
class _Key:
    key: str
    code: str
    vk: int
    text: str | None = None  # a keyDown's text: the character, or "\r" for Enter
    unmodified: str | None = None


def _named(name: str) -> _Key:
    code, vk = NAMED[name]
    return _Key(name, code, vk, "\r" if name == "Enter" else None)


def _character(c: str) -> _Key:
    ascii_alnum = c.isascii() and c.isalnum()
    code = (f"Key{c.upper()}" if c.isalpha() else f"Digit{c}") if ascii_alnum else ""
    lower = c.lower()
    return _Key(c, code, ord(c.upper()) if ascii_alnum else 0, c, lower if len(lower) == 1 else c)


def date_chars(kind: str, value: str) -> str:
    """The characters an en-US date (MMDDYYYY) or time (hhmm, then A or P) field takes for a value (§6.12)."""
    if kind == "date":
        year, month, day = value.split("-")
        return month + day + year
    hours, minutes = (int(part) for part in value.split(":"))
    return f"{(hours % 12) or 12:02d}{minutes:02d}{'P' if hours >= 12 else 'A'}"


def choose_keys(options: Sequence[tuple[str, bool]], index: int) -> list[str] | None:
    """The keys that pick option `index` of a focused select (§6.11 step 5), or None when it's missing or
    disabled."""
    if not 0 <= index < len(options) or options[index][1]:
        return None
    enabled = [k for k, (_, disabled) in enumerate(options) if not disabled]
    return ["Home", *["ArrowDown"] * enabled.index(index), "Enter"]


@dataclass(frozen=True, slots=True)
class Live:
    """The front page's visual viewport now (Page.getLayoutMetrics)."""

    page_x: float
    page_y: float
    scale: float
    offset_x: float
    offset_y: float
    width: float
    height: float


class InputMapper:
    """Carries out input messages on the front page's CDP session."""

    def __init__(self, images: ImageRing, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._images = images
        self._sleep = sleep
        self._front: int | None = None
        self._cdp: Cdp | None = None
        self._held: dict[tuple[str, str], _Key] = {}  # in the order pressed
        self._pressed: tuple[Cdp, float, float] | None = None  # a tap's button, down

    def aim(self, front: int | None, cdp: Cdp | None) -> None:
        """Input goes to this page from now on."""
        self._front, self._cdp = front, cdp

    async def live(self) -> Live:
        metrics = await self._session().send("Page.getLayoutMetrics")
        viewport = metrics.get("cssVisualViewport", {})
        return Live(
            float(viewport.get("pageX", 0.0)),
            float(viewport.get("pageY", 0.0)),
            float(viewport.get("scale", 1.0)) or 1.0,
            float(viewport.get("offsetX", 0.0)),
            float(viewport.get("offsetY", 0.0)),
            float(viewport.get("clientWidth", 0.0)),
            float(viewport.get("clientHeight", 0.0)),
        )

    # ---- pointer

    async def tap(self, message: Tap) -> ErrorCode | None:
        scale = await self._aimed(message.page, message.seq)
        if scale is None:
            return "stale"
        x, y = message.x / scale, message.y / scale
        cdp = self._session()
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self._button(cdp, "mousePressed", x, y)
        self._pressed = (cdp, x, y)
        await self._sleep(message.ms / 1000)
        self._pressed = None
        await self._button(cdp, "mouseReleased", x, y)
        return None

    async def move(self, message: Move) -> None:
        image = self._images.latest(message.page)
        if message.page != self._front or image is None:
            return
        x, y = message.x / image.scale, message.y / image.scale
        await self._session().send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    async def wheel(self, message: Wheel) -> None:
        image = self._images.latest(message.page)
        if message.page != self._front or image is None:
            return
        await self._wheel(message.x / image.scale, message.y / image.scale, message.dx, message.dy)

    async def pinch(self, message: Pinch) -> ErrorCode | None:
        scale = await self._aimed(message.page, message.seq)
        if scale is None:
            return "stale"
        await self._session().send(
            "Input.synthesizePinchGesture",
            {
                "x": message.x0 / scale,
                "y": message.y0 / scale,
                "scaleFactor": message.scale,
                "gestureSourceType": "mouse",
                "relativeSpeed": PINCH_SPEED,
            },
        )
        dx, dy = message.x0 - message.x1, message.y0 - message.y1
        if dx or dy:
            after = (await self.live()).scale
            await self._wheel(message.x1 / after, message.y1 / after, dx, dy)
        return None

    async def click(self, x: float, y: float) -> None:
        """A plain click at a point of the visual viewport (CSS px)."""
        cdp = self._session()
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self._button(cdp, "mousePressed", x, y)
        await self._button(cdp, "mouseReleased", x, y)

    async def _aimed(self, page: int, seq: int) -> float | None:
        """The live scale, if image `seq` shows the front page as it is now (§6.4 steps 1-2); else None."""
        image = self._images.get(seq)
        if self._cdp is None or page != self._front or image is None or image.page != self._front:
            return None
        live = await self.live()
        moved = abs(image.sx - live.page_x) > MOVED_CSS_PX or abs(image.sy - live.page_y) > MOVED_CSS_PX
        return None if moved or abs(image.scale - live.scale) > MOVED_SCALE else live.scale

    async def _wheel(self, x: float, y: float, dx: float, dy: float) -> None:
        await self._session().send(
            "Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy}
        )

    @staticmethod
    async def _button(cdp: Cdp, kind: str, x: float, y: float) -> None:
        await cdp.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})

    # ---- keys and text

    async def key(self, message: Key) -> None:
        if isinstance(message.k, str):
            held, key = ("k", message.k), _named(message.k)
        else:
            assert isinstance(message.c, str)  # noqa: S101 - check() lets only one of them through
            held, key = ("c", message.c), _character(message.c)
        if not isinstance(message.ph, str):
            modifiers = (SHIFT if message.shift or self._shift_held() else 0) | (CTRL if message.word else 0)
            await self._down(key, modifiers)
            await self._up(key, modifiers)
        elif message.ph == "down":
            if held in self._held:  # the viewer's keyboard repeating a key held down (CDP never repeats one itself)
                if held != ("k", "Shift"):
                    await self._down(key, self._modifiers(), repeat=True)
                return
            if len(self._held) >= MAX_HELD:
                await self._release(next(iter(self._held)))
            self._held[held] = key
            await self._down(key, self._modifiers())
        elif held in self._held:
            await self._release(held)

    async def press(self, name: str) -> None:
        """A whole press of a named key, with no modifier (for `choose`)."""
        key = _named(name)
        await self._down(key, 0)
        await self._up(key, 0)

    async def type(self, characters: str) -> None:
        """Whole presses of characters, with no modifier (for `date`)."""
        for c in characters:
            key = _character(c)
            await self._down(key, 0)
            await self._up(key, 0)

    async def text(self, message: Text) -> None:
        await self._session().send("Input.insertText", {"text": message.s})

    async def compose(self, message: Compose) -> None:
        caret = len(message.s.encode("utf-16-le")) // 2
        await self._session().send(
            "Input.imeSetComposition", {"text": message.s, "selectionStart": caret, "selectionEnd": caret}
        )

    async def release(self) -> None:
        """Teardown (§3.5): a tap's button and every held key back up, Shift last. Never raises."""
        pressed, self._pressed = self._pressed, None
        if pressed is not None:
            cdp, x, y = pressed
            await _quietly(self._button(cdp, "mouseReleased", x, y))
        for held in sorted(self._held, key=lambda h: h == ("k", "Shift")):
            await _quietly(self._release(held))
        self._held.clear()

    def _shift_held(self) -> bool:
        return ("k", "Shift") in self._held

    def _modifiers(self) -> int:
        return SHIFT if self._shift_held() else 0

    async def _release(self, held: tuple[str, str]) -> None:
        key = self._held.pop(held)
        await self._up(key, self._modifiers())

    async def _down(self, key: _Key, modifiers: int, *, repeat: bool = False) -> None:
        event: dict[str, Any] = {"key": key.key, "code": key.code, "windowsVirtualKeyCode": key.vk}
        if key.text is None:
            event["type"] = "rawKeyDown"
        else:
            event |= {"type": "keyDown", "text": key.text}
            if key.unmodified is not None:
                event["unmodifiedText"] = key.unmodified
        if repeat:
            event["autoRepeat"] = True
        await self._session().send("Input.dispatchKeyEvent", {**event, "modifiers": modifiers})

    async def _up(self, key: _Key, modifiers: int) -> None:
        await self._session().send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key.key,
                "code": key.code,
                "windowsVirtualKeyCode": key.vk,
                "modifiers": modifiers,
            },
        )

    def _session(self) -> Cdp:
        if self._cdp is None:
            raise CdpError("no front page")
        return self._cdp

    # ---- pickers (§6.11-6.12)

    async def choose(self, worlds: Worlds, field: FieldRef, index: int, *, list_open: bool) -> ErrorCode | None:
        """Pick option `index` of a select the map described. `list_open`: the agent's own click opened its list
        and nothing was carried out since, so it isn't clicked again."""
        keys = choose_keys(field.opts or (), index)
        if keys is None:
            return "failed"
        point = await self._click_point(worlds, field, 0)
        if not isinstance(point, tuple):
            return point
        if not (list_open and await _focused(worlds, field)):
            await self.click(*point)
            await self._sleep(SETTLE_SECONDS)
            if not await _focused(worlds, field):
                await self.press("Escape")
                return "failed"
        for name in keys:
            await self.press(name)
        await self._sleep(SETTLE_SECONDS)
        now = await _points(worlds, field, 0)
        if now is None or now.sel != index:
            await self.press("Escape")
            return "failed"
        return None

    async def date(self, worlds: Worlds, field: FieldRef, value: str) -> ErrorCode | None:
        """Set a date or time field the map described, as an en-US keyboard user would."""
        point = await self._click_point(worlds, field, 1)
        if not isinstance(point, tuple):
            return point
        await self.click(*point)
        await self._sleep(SETTLE_SECONDS)
        if not await _focused(worlds, field):
            return "failed"
        await self.type(date_chars(field.k, value))
        now = await _points(worlds, field, 1)
        return None if now is not None and now.v == value else "failed"

    async def _click_point(self, worlds: Worlds, field: FieldRef, mode: int) -> tuple[float, float] | ErrorCode:
        """Where to click the field so that it, and nothing else, gets the click (§6.11 steps 2-3), in the visual
        viewport's CSS px; `stale` when it's gone, changed, or covered everywhere."""
        if not worlds.alive(field.world):
            return "stale"
        found = await _points(worlds, field, mode)
        if found is None:
            return "stale"
        lifted = await lift(worlds, field.world, list(found.pts))
        if lifted is None:
            return "stale"
        live = await self.live()
        ax, ay = sum(a for a, _ in lifted.origins), sum(b for _, b in lifted.origins)
        for j in range(len(found.pts) // 2):
            x, y = found.pts[2 * j] + ax - live.offset_x, found.pts[2 * j + 1] + ay - live.offset_y
            inside = 0 <= x < live.width and 0 <= y < live.height  # the visual viewport, for the main frame's
            if lifted.hits[j] and (inside or field.world.parent is not None):
                return x, y
        return "stale"


class _Points(msgspec.Struct, frozen=True):
    pts: list[float]
    sel: int
    v: str


async def _points(worlds: Worlds, field: FieldRef, mode: int) -> _Points | None:
    """CLICK_POINTS: None when the element is gone or no longer the one the map described."""
    raw = await worlds.evaluate(field.world, CLICK_POINTS, field.ev, field.index, mode)
    if not isinstance(raw, dict) or "why" in raw:
        return None
    try:
        return msgspec.convert(raw, _Points)
    except msgspec.ValidationError:
        return None


async def _focused(worlds: Worlds, field: FieldRef) -> bool:
    """The element is its document's focused element, and each ancestor's focus is on the iframe leading to it."""
    if not await worlds.evaluate(field.world, IS_FOCUSED, field.ev, field.index):
        return False
    world: World = field.world
    while world.parent is not None and world.owner is not None:
        raw = await worlds.evaluate(world.parent, OWNER_HIT, world.owner, [])
        if not isinstance(raw, dict) or raw.get("focused") is not True:
            return False
        world = world.parent
    return True


async def _quietly(work: Awaitable[None]) -> None:
    try:
        await work
    except CdpError as e:
        log.debug("Teardown input not sent: %s", e)
