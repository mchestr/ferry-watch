"""The field map and focus reports of a stream session (PROTOCOL.md §5.3-5.4, DESIGN §4.4).

The viewer can only open a phone's keyboard, or a native picker, for a field it already knows is under the
user's finger, so the agent keeps it supplied with a map of the front page's editable fields: their rectangles in
frame px, a 3x3 mask of where a click would reach each one, and the attributes the viewer's keyboard proxy copies
(never a field's value, but a select's index and a date or time). The map is read by the fixed scripts in the
frames' isolated worlds (cast_scripts.py, cast_cdp.Worlds), bounded inside the page, and sent on one schedule:
due after a newer image, a frame change or an input, started at most every 250 ms and at least 50 ms after what
made it due. An unchanged map is sent as a small `fields_ok`.

A frame inside an iframe gets its offset and its occlusion from the owner chain: at each level the parent's world
says where the iframe's content box is and which points a click there would deliver to it (OWNER_HIT), and the
masks are ANDed. Rectangles follow PROTOCOL §5.3.3 (`frame_rect`).

Focus reports say which editable element the front page's innermost focused frame has, after the inputs that
move focus and while typing, so the viewer can retarget its keyboard proxy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, cast

import msgspec

from .cast_cdp import MAX_DEPTH, CdpError, ScriptError, World, Worlds
from .cast_protocol import (
    MAX_LABEL,
    MAX_OUTBOUND_BYTES,
    AutoCapitalize,
    EnterKeyHint,
    Field,
    FieldKind,
    Fields,
    FieldsOk,
    FieldType,
    Focus,
    InputMode,
    Outbound,
    clean,
    strip_credential_tokens,
)
from .cast_scripts import FIELDS, FOCUS, MAX_POINT_NUMBERS, OWNER_HIT

log = logging.getLogger(__name__)

MAX_FRAMES: Final = 8  # evaluated per map
MAX_CHILDREN: Final = 16  # child frames of one frame looked at
EVERY_SECONDS: Final = 0.25  # an evaluation starts at most this often ...
AFTER_SECONDS: Final = 0.05  # ... and this long after what made it due
SLOW_SECONDS: Final = 1.0  # after one that ran out of time
FOCUS_AFTER_SECONDS: Final = 0.05
FOCUS_TYPING_EVERY_SECONDS: Final = 0.25
MAX_COORDINATE: Final = 1e6  # frame px; anything further away is a broken page, not a field
KEPT_REVISIONS: Final = 2

Rect = tuple[float, float, float, float]

_INPUT_TYPES: Final = frozenset({"text", "email", "tel", "url", "number", "password", "search", "date", "time"})
_INPUT_MODES: Final = frozenset({"none", "text", "decimal", "numeric", "tel", "search", "email", "url"})
_ENTER_KEY_HINTS: Final = frozenset({"enter", "done", "go", "next", "previous", "search", "send"})
_CAPITALIZE: Final = frozenset({"off", "none", "on", "sentences", "words", "characters"})


def frame_rect(
    s: float,
    ox: float,
    oy: float,
    origins: Sequence[tuple[float, float]],
    clip: Rect,
    client: Rect,
) -> tuple[Rect, Rect | None]:
    """A field's `r` and `c` in frame px (PROTOCOL §5.3.3): its client rectangle in its own frame, the content-box
    origins of the owner iframes above it (innermost first), its frame's clip, and the main frame's visual viewport
    offset and scale. `c` is None when nothing of the field is inside the clip."""
    ax = sum(a for a, _ in origins)
    ay = sum(b for _, b in origins)
    x, y, w, h = client
    r = ((x + ax - ox) * s, (y + ay - oy) * s, w * s, h * s)
    cx, cy, cw, ch = clip
    left, top = max(x, cx), max(y, cy)
    right, bottom = min(x + w, cx + cw), min(y + h, cy + ch)
    if right <= left or bottom <= top:
        return r, None
    return r, ((left + ax - ox) * s, (top + ay - oy) * s, (right - left) * s, (bottom - top) * s)


def samples(clip: Rect, client: Rect) -> list[float]:
    """The 9 points FIELDS hit-tests for a field, [x, y, ...] in its frame's client coordinates."""
    cx, cy, cw, ch = clip
    x, y, w, h = client
    x0, y0 = max(x, cx), max(y, cy)
    x1, y1 = min(x + w, cx + cw), min(y + h, cy + ch)
    points: list[float] = []
    for j in range(9):
        points += [x0 + (x1 - x0) * (2 * (j % 3) + 1) / 6, y0 + (y1 - y0) * (2 * (j // 3) + 1) / 6]
    return points


# ---- what the scripts return, as decoded (anything else is dropped as a failed read)


class RawField(msgspec.Struct, frozen=True):
    tag: str
    type: str
    im: str
    ac: str
    ekh: str
    cap: str
    max: int | None
    ro: bool
    r: Rect
    m: int
    i: int = -1
    n: int = 0
    opts: list[tuple[str, bool]] | None = None
    sel: int = -1
    ot: bool = False
    v: str = ""


Viewport = tuple[float, float, float, float, float]  # pageLeft, pageTop, offsetLeft, offsetTop, scale


class RawFrame(msgspec.Struct, frozen=True):
    f: list[RawField]
    trunc: bool
    slow: bool
    clip: Rect
    vv: Viewport | None
    cw: float
    ch: float
    own: list[tuple[int, int]]


class RawHit(msgspec.Struct, frozen=True):
    o: tuple[float, float]
    h: list[int]
    focused: bool
    vis: bool
    slow: bool


class RawFocus(msgspec.Struct, frozen=True):
    f: RawField | None = None
    clip: Rect | None = None
    vv: Viewport | None = None
    frame: int | None = None


def _decode[T](raw: object, kind: type[T]) -> T | None:
    try:
        return msgspec.convert(raw, kind)
    except msgspec.ValidationError:
        return None


@dataclass(frozen=True, slots=True)
class Lifted:
    """A frame's points carried up to the main frame: each owner's content-box origin (innermost first), and
    for each point whether every ancestor would deliver a click there to the frame."""

    origins: list[tuple[float, float]]
    hits: list[int]
    slow: bool


async def lift(worlds: Worlds, world: World, points: list[float]) -> Lifted | None:
    """Points of a frame (its client coordinates), through every ancestor's world. None when an owner has gone."""
    hits = [1] * (len(points) // 2)
    origins: list[tuple[float, float]] = []
    slow = False
    current = world
    while current.parent is not None and current.owner is not None:
        raw = _decode(await worlds.evaluate(current.parent, OWNER_HIT, current.owner, points), RawHit)
        if raw is None or len(raw.h) != len(hits):
            return None
        ox, oy = raw.o
        hits = [a & (1 if b else 0) for a, b in zip(hits, raw.h, strict=True)]
        origins.append((ox, oy))
        slow = slow or raw.slow
        points = [p + (ox if j % 2 == 0 else oy) for j, p in enumerate(points)]
        current = current.parent
    return Lifted(origins, hits, slow)


def _mask(bits: Sequence[int]) -> int:
    return sum(1 << j for j, bit in enumerate(bits) if bit)


def _kind(raw: RawField) -> tuple[FieldKind, FieldType] | None:
    if raw.tag == "i":
        field_type = raw.type if raw.type in _INPUT_TYPES else "text"
        kind = field_type if field_type in ("date", "time") else "text"
        return cast(FieldKind, kind), cast(FieldType, field_type)
    others: dict[str, tuple[FieldKind, FieldType]] = {
        "a": ("area", "textarea"),
        "e": ("area", "contenteditable"),
        "s": ("select", "select"),
    }
    return others.get(raw.tag)


def _value(kind: str, value: str) -> str:
    """A date or time field's value as PROTOCOL §5.3.1 has it (YYYY-MM-DD, HH:MM), or ""."""
    if kind == "date":
        return value if len(value) == 10 and value[4] == "-" and value[7] == "-" else ""
    return value[:5] if len(value) >= 5 and value[2] == ":" else ""


def _near(rect: Rect) -> bool:
    return all(abs(v) <= MAX_COORDINATE for v in rect)


def field(raw: RawField, r: Rect, c: Rect, m: int, number: int | None = None) -> Field | None:
    """The protocol's field object for what a script found (attributes checked against the protocol's values,
    `autocomplete` without credential tokens, labels cleaned). None for anything the protocol can't carry."""
    kinds = _kind(raw)
    if kinds is None or not (1 <= m <= 511) or not (_near(r) and _near(c)):
        return None
    k, field_type = kinds
    maxlength = raw.max if raw.max is not None and 1 <= raw.max <= 524288 else None
    common = {
        "k": k,
        "r": r,
        "c": c,
        "m": m,
        "type": field_type,
        "im": cast(InputMode, raw.im if raw.im in _INPUT_MODES else ""),
        "ac": strip_credential_tokens(raw.ac),
        "ekh": cast(EnterKeyHint, raw.ekh if raw.ekh in _ENTER_KEY_HINTS else ""),
        "cap": cast(AutoCapitalize, raw.cap if raw.cap in _CAPITALIZE else ""),
        "max": maxlength,
        "ro": raw.ro,
    }
    if number is not None:
        common["id"] = number
    if k == "select":
        nopt = min(max(raw.n, 0), 1_000_000)
        options = [(clean(label, MAX_LABEL), disabled) for label, disabled in (raw.opts or [])[:400]]
        selected = raw.sel if -1 <= raw.sel < nopt else -1
        extra: dict[str, object] = {"nopt": nopt, "sel": selected, "trunc": raw.opts is None or raw.ot}
        if raw.opts is not None:
            extra["opts"] = options
        return Field(**common, **extra)  # type: ignore[arg-type]
    if k in ("date", "time"):
        return Field(**common, v=_value(k, raw.v))  # type: ignore[arg-type]
    return Field(**common)  # type: ignore[arg-type]


# ---- the map


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A field of a map revision: where it is (its frame's world and its index in that evaluation), and what the
    agent told the viewer about it."""

    world: World
    ev: int  # the evaluation that kept its element
    index: int
    k: str
    opts: tuple[tuple[str, bool], ...] | None  # as sent
    r: Rect


@dataclass(slots=True)
class Revision:
    rev: int
    page: int
    worlds: Worlds
    refs: dict[int, FieldRef]
    key: bytes  # everything fields_ok compares
    ev: int  # the evaluation whose elements `refs` name


Front = tuple[int, Worlds]  # the front page's id and its worlds


class FieldMapper:
    """The field map's schedule and evaluation (PROTOCOL §5.3.2-5.3.4)."""

    def __init__(
        self,
        front: Callable[[], Front | None],
        progress: Callable[[], tuple[int, int]],
        send: Callable[[Outbound], Awaitable[bool]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`front` is the front page to map, None while there is none or a dialog blocks it; `progress` the
        newest binary message's seq and the number of viewer messages carried out (the map's `seq` and `n`)."""
        self._front = front
        self._progress = progress
        self._send = send
        self._clock = clock
        self._wanted = asyncio.Event()
        self._due_since: float | None = None
        self._started_at = float("-inf")
        self._slow = False
        self._evaluated_seq = 0
        self._ev = 0
        self._next_rev = 1
        self._revisions: deque[Revision] = deque(maxlen=KEPT_REVISIONS)

    def due(self) -> None:
        """Something changed what the map may say: evaluate (on the schedule)."""
        if self._due_since is None:
            self._due_since = self._clock()
        self._wanted.set()

    def image_sent(self, seq: int) -> None:
        if seq > self._evaluated_seq:
            self.due()

    def reset(self) -> None:
        """The front page changed: the maps held were of the old one."""
        self._revisions.clear()
        self.due()

    def revision(self, rev: int) -> Revision | None:
        """A map revision the agent still holds (the latest 2)."""
        return next((r for r in self._revisions if r.rev == rev), None)

    async def run(self) -> None:
        while True:
            await self._wanted.wait()
            due_since = self._due_since if self._due_since is not None else self._clock()
            start = max(due_since + AFTER_SECONDS, self._started_at + (SLOW_SECONDS if self._slow else EVERY_SECONDS))
            if (wait := start - self._clock()) > 0:
                await asyncio.sleep(wait)
                continue
            self._wanted.clear()
            self._due_since = None
            front = self._front()
            if front is None:
                continue  # due again when the page is back
            self._started_at = self._clock()
            seq, n = self._progress()
            self._evaluated_seq = seq
            try:
                self._slow = await self._evaluate(front, seq, n)
            except CdpError as e:
                log.debug("No field map this time: %s", e)
                self._slow = True
                self.due()  # again, a second from now: the page may have been between documents

    async def _evaluate(self, front: Front, seq: int, n: int) -> bool:
        """One evaluation, sent as `fields` or `fields_ok`. Returns whether it ran out of time."""
        page_id, worlds = front
        self._ev += 1
        ev = self._ev
        keep = ([r.ev for r in self._revisions] + [0, 0])[:KEPT_REVISIONS]  # their elements stay in the worlds
        frames, trunc, slow = await _read_frames(worlds, ev, keep)
        view = _view(frames[0][1])
        lifts = await asyncio.gather(*(_lift_frame(worlds, w, raw) for w, raw in frames[1:]))
        fields: list[Field] = []
        refs: dict[int, FieldRef] = {}
        for (world, raw), lifted in zip(frames, [Lifted([], [], False), *lifts], strict=True):
            if lifted is None:
                trunc = True
                continue
            slow = slow or lifted.slow
            for j, raw_field in enumerate(raw.f):
                r, c = frame_rect(view.s, view.ox, view.oy, lifted.origins, raw.clip, raw_field.r)
                bits = lifted.hits[9 * j : 9 * j + 9] if world.parent is not None else [1] * 9
                made = None if c is None else field(raw_field, r, c, raw_field.m & _mask(bits), len(fields) + 1)
                if made is None:
                    continue
                fields.append(made)
                refs[len(fields)] = FieldRef(world, ev, raw_field.i, made.k, _opts(made), r)
        message = _fit(
            Fields(
                page=page_id,
                rev=0,
                seq=seq,
                n=n,
                sx=view.sx,
                sy=view.sy,
                scale=view.s,
                cw=view.cw,
                ch=view.ch,
                trunc=trunc,
                f=fields,
            )
        )
        refs = {number: ref for number, ref in refs.items() if number <= len(message.f)}
        key = msgspec.json.encode(msgspec.structs.replace(message, seq=0, n=0))
        latest = self._revisions[-1] if self._revisions else None
        if latest is not None and latest.key == key and latest.worlds is worlds:
            latest.refs, latest.ev = refs, ev  # the same map, with the elements of this evaluation
            await self._send(FieldsOk(page=page_id, rev=latest.rev, seq=seq, n=n))
            return slow
        rev, self._next_rev = self._next_rev, self._next_rev + 1
        self._revisions.append(Revision(rev, page_id, worlds, refs, key, ev))
        await self._send(msgspec.structs.replace(message, rev=rev))
        return slow


def _opts(made: Field) -> tuple[tuple[str, bool], ...] | None:
    return tuple(made.opts) if isinstance(made.opts, list) else None


@dataclass(frozen=True, slots=True)
class View:
    """The main frame's visual viewport and document size, from the evaluation."""

    sx: float
    sy: float
    ox: float
    oy: float
    s: float
    cw: float
    ch: float


def _view(raw: RawFrame) -> View:
    if raw.vv is None:
        raise CdpError("the main frame reported no visual viewport")
    sx, sy, ox, oy, s = raw.vv
    return View(sx, sy, ox, oy, s, raw.cw, raw.ch)


async def _read_frames(worlds: Worlds, ev: int, keep: list[int]) -> tuple[list[tuple[World, RawFrame]], bool, bool]:
    """FIELDS in the main frame, then level by level in each child frame whose owner its parent saw inside the
    clip, at most MAX_FRAMES in all. Returns them in that order, whether the map is incomplete, and whether
    anything ran out of time. Raises CdpError when the main frame can't be read."""
    main = await worlds.get(worlds.page.main_frame)
    if main is None:
        raise CdpError("no world in the main frame")
    done: list[tuple[World, RawFrame]] = []
    trunc = slow = False
    level, budget = [main], MAX_FRAMES
    while level and budget > 0:
        level, rest = level[:budget], level[budget:]
        trunc = trunc or bool(rest)
        budget -= len(level)
        await asyncio.gather(*(_child_worlds(worlds, w) for w in level if w.depth < MAX_DEPTH))
        reads = await asyncio.gather(*(_read(worlds, w, ev, keep) for w in level))
        following: list[World] = []
        for world, (raw, timed_out) in zip(level, reads, strict=True):
            slow = slow or timed_out
            if raw is None:
                if world is main:
                    raise CdpError("the main frame's fields couldn't be read")
                trunc = True
                continue
            done.append((world, raw))
            trunc, slow = trunc or raw.trunc, slow or raw.slow
            visible = {k for k, shown in raw.own if shown}
            following += [w for w in _known_children(worlds, world) if w.owner in visible]
        level = following
    return done, trunc or bool(level), slow


async def _child_worlds(worlds: Worlds, world: World) -> None:
    """Worlds (and kept owners) for a frame's children, so that its FIELDS reports which of them it can see."""
    children = [f for f in world.frame.child_frames if not f.is_detached()][:MAX_CHILDREN]
    await asyncio.gather(*(worlds.get(f) for f in children))


def _known_children(worlds: Worlds, world: World) -> list[World]:
    found = (worlds.known(f) for f in world.frame.child_frames[:MAX_CHILDREN])
    return [w for w in found if w is not None and w.parent is world]


async def _read(worlds: Worlds, world: World, ev: int, keep: list[int]) -> tuple[RawFrame | None, bool]:
    """One frame's FIELDS, and whether it timed out."""
    try:
        return _decode(await worlds.evaluate(world, FIELDS, ev, *keep), RawFrame), False
    except ScriptError:
        return None, True
    except CdpError:
        return None, False


async def _lift_frame(worlds: Worlds, world: World, raw: RawFrame) -> Lifted | None:
    points: list[float] = []
    for f in raw.f[: MAX_POINT_NUMBERS // 18]:
        points += samples(raw.clip, f.r)
    if not raw.f:
        return Lifted([], [], False)
    try:
        return await lift(worlds, world, points)
    except CdpError:
        return None


def _fit(message: Fields) -> Fields:
    """The map within 65,536 bytes (PROTOCOL §5.3.2 step 4): selects lose their options, largest first, then fields
    are dropped from the end, and the map says it's incomplete."""
    encode = msgspec.json.encode
    size = len(encode(message))
    if size <= MAX_OUTBOUND_BYTES:
        return message
    fields = list(message.f)
    for i in sorted(
        (i for i, f in enumerate(fields) if isinstance(f.opts, list)), key=lambda i: -len(encode(fields[i].opts))
    ):
        smaller = msgspec.structs.replace(fields[i], opts=msgspec.UNSET, trunc=True)
        size -= len(encode(fields[i])) - len(encode(smaller))
        fields[i] = smaller
        if size <= MAX_OUTBOUND_BYTES:
            return msgspec.structs.replace(message, f=fields)
    while fields and size > MAX_OUTBOUND_BYTES - 16:  # room for "trunc": true
        size -= len(encode(fields.pop())) + 1
    return msgspec.structs.replace(message, f=fields, trunc=True)


# ---- focus


class FocusReporter:
    """`focus` reports (PROTOCOL §5.4): 50 ms after a message that moves focus was carried out, and 50 ms after
    typing pauses (at most every 250 ms while it goes on)."""

    def __init__(
        self,
        front: Callable[[], Front | None],
        progress: Callable[[], tuple[int, int]],
        send: Callable[[Outbound], Awaitable[bool]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._front = front
        self._progress = progress
        self._send = send
        self._clock = clock
        self._wanted = asyncio.Event()
        self._at = 0.0
        self._tap: tuple[float, float] | None = None
        self._typing_since: float | None = None
        self.popup = False  # the latest report said the agent's own click opened a select's list

    def after(self, tap: tuple[float, float] | None = None) -> None:
        """A message that may have moved focus was carried out (`tap`: the point of a tap, frame px)."""
        self._at = self._clock() + FOCUS_AFTER_SECONDS
        self._tap = tap
        self._typing_since = None
        self._wanted.set()

    def typing(self) -> None:
        now = self._clock()
        if self._typing_since is None:
            self._typing_since = now
        self._at = min(now + FOCUS_AFTER_SECONDS, self._typing_since + FOCUS_TYPING_EVERY_SECONDS)
        self._tap = None
        self._wanted.set()

    async def run(self) -> None:
        while True:
            await self._wanted.wait()
            if (wait := self._at - self._clock()) > 0:
                await asyncio.sleep(wait)
                continue
            self._wanted.clear()
            tap, self._tap, self._typing_since = self._tap, None, None
            front = self._front()
            if front is None:
                continue
            seq, n = self._progress()
            try:
                found = await read_focus(front[1], tap)
            except CdpError as e:
                log.debug("No focus report this time: %s", e)
                continue
            focused, self.popup = found
            await self._send(Focus(page=front[0], seq=seq, n=n, f=focused, popup=self.popup))


async def read_focus(worlds: Worlds, tap: tuple[float, float] | None) -> tuple[Field | None, bool]:
    """The innermost focused frame's focused editable element as a field, and whether `tap` (the point of the tap
    just carried out) opened a select's list. None when nothing editable has focus, or nothing of it can be
    clicked (scrolled out of view, or covered)."""
    world = await worlds.get(worlds.page.main_frame)
    if world is None:
        raise CdpError("no world in the main frame")
    raw = _decode(await worlds.evaluate(world, FOCUS), RawFocus)
    view = raw.vv if raw is not None else None
    for _ in range(MAX_DEPTH):
        if raw is None or raw.frame is None:
            break
        child = await _child_with_owner(worlds, world, raw.frame)
        if child is None:
            return None, False
        world = child
        raw = _decode(await worlds.evaluate(world, FOCUS), RawFocus)
    if raw is None or raw.f is None or raw.clip is None or view is None or raw.frame is not None:
        return None, False
    _, _, ox, oy, s = view
    lifted = (
        await lift(worlds, world, samples(raw.clip, raw.f.r)) if world.parent is not None else Lifted([], [], False)
    )
    if lifted is None:
        return None, False
    r, c = frame_rect(s, ox, oy, lifted.origins, raw.clip, raw.f.r)
    bits = lifted.hits if world.parent is not None else [1] * 9
    made = None if c is None else field(raw.f, r, c, raw.f.m & _mask(bits))
    if made is None:
        return None, False
    popup = tap is not None and made.k == "select" and _inside(tap, r)
    return made, popup


def _inside(point: tuple[float, float], rect: Rect) -> bool:
    x, y, w, h = rect
    return x <= point[0] < x + w and y <= point[1] < y + h


async def _child_with_owner(worlds: Worlds, world: World, owner: int) -> World | None:
    for attempt in range(2):
        child = next((w for w in _known_children(worlds, world) if w.owner == owner), None)
        if child is not None or attempt:
            return child
        await _child_worlds(worlds, world)  # a frame no map has looked into yet
    return None
