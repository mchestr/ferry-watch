"""fw.cast1, the streamed checkout viewer's wire protocol, as the agent speaks it.

The normative text is docs/multi-tenant/streamed-viewer/PROTOCOL.md; section numbers below are its. This module
has no I/O: the inbound messages and their two decoding steps (§6), the image header (§4), the outbound
messages with the caps and cleaning every page-derived string gets before it leaves the pod (§5), the close
codes (§3.6) and the rate table (§7.1). The stream session (stream.py) and the gate use it.

Typed text (the content of `text`, `compose`, `key` and `dialog.s`) is never logged or stored (C37): the structs
that hold it have a `__repr__` that shows only its length, and a structural error names no value.
"""

from __future__ import annotations

import re
import struct
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import IntEnum
from typing import Annotated, Any, Final, Literal, Protocol

import msgspec

MAX_INBOUND_BYTES: Final = 4096  # one viewer→agent text frame (§6); the gate sets the socket's limit to it
MAX_OUTBOUND_BYTES: Final = 65_536  # one agent→viewer JSON message (§5)
MAX_JPEG_BYTES: Final = 2_097_152  # one image (§4.1)
VERSION: Final = 1  # hello.v and ready.v (§9)

U32: Final = 2**32 - 1


class CloseCode(IntEnum):
    """How the agent closes a stream (§3.6)."""

    ENDED = 4000  # the checkout is over
    REPLACED = 4001  # another connection from this device took the slot
    REVOKED = 4002  # checkout access withdrawn
    DRAINING = 4003  # the pod is shutting down
    TIME_CAP = 4004  # the 6000 s connection cap
    WITHDRAWN = 4005  # the streamed viewer was withdrawn (the kill switch): the viewer goes to the classic one
    PROTOCOL = 4400  # a structurally malformed or unexpected message
    NO_HELLO = 4408
    RATE = 4429  # the queue, the drop limit or the overall rate
    INTERNAL = 4500  # the agent couldn't run the session


class ProtocolViolation(Exception):
    """A frame the session must close for (§6 step 1). The reason names no value the viewer sent."""

    def __init__(self, reason: str, code: CloseCode = CloseCode.PROTOCOL) -> None:
        super().__init__(reason)
        self.code = code


# ---- viewer → agent (§6): types only; every range, length and combination rule is in check()


class _In(msgspec.Struct, tag_field="t", forbid_unknown_fields=True, frozen=True, kw_only=True):
    pass


class Hello(_In, tag="hello", frozen=True, kw_only=True):
    v: int
    w: int
    h: int
    dpr: float


class Resize(_In, tag="resize", frozen=True, kw_only=True):
    w: int
    h: int
    dpr: float


class Ack(_In, tag="ack", frozen=True, kw_only=True):
    seq: int


class Tap(_In, tag="tap", frozen=True, kw_only=True):
    page: int
    seq: int
    x: float
    y: float
    ms: int = 0


class Move(_In, tag="move", frozen=True, kw_only=True):
    page: int
    x: float
    y: float


class Wheel(_In, tag="wheel", frozen=True, kw_only=True):
    page: int
    x: float
    y: float
    dx: float
    dy: float


class Visible(_In, tag="visible", frozen=True, kw_only=True):
    v: bool


class Pinch(_In, tag="pinch", frozen=True, kw_only=True):
    page: int
    seq: int
    x0: float
    y0: float
    x1: float
    y1: float
    scale: float


class Key(_In, tag="key", frozen=True, kw_only=True):
    k: str | msgspec.UnsetType = msgspec.UNSET
    c: str | msgspec.UnsetType = msgspec.UNSET
    shift: bool = False
    word: bool = False
    ph: str | msgspec.UnsetType = msgspec.UNSET

    def __repr__(self) -> str:
        named = self.k if isinstance(self.k, str) and self.k in KEYS else "?"
        return f"Key(k={named}, c={'set' if isinstance(self.c, str) else 'unset'})"


class Text(_In, tag="text", frozen=True, kw_only=True):
    s: str

    def __repr__(self) -> str:
        return f"Text(<{len(self.s)} code points>)"


class Compose(_In, tag="compose", frozen=True, kw_only=True):
    s: str

    def __repr__(self) -> str:
        return f"Compose(<{len(self.s)} code points>)"


class Choose(_In, tag="choose", frozen=True, kw_only=True):
    page: int
    rev: int
    f: int
    i: int


class SetDate(_In, tag="date", frozen=True, kw_only=True):
    page: int
    rev: int
    f: int
    v: str


class Answer(_In, tag="dialog", frozen=True, kw_only=True):
    id: int
    ok: bool
    s: str | msgspec.UnsetType = msgspec.UNSET

    def __repr__(self) -> str:
        return f"Answer(id={self.id}, ok={self.ok})"


class PageOp(_In, tag="page", frozen=True, kw_only=True):
    op: str
    page: int


Inbound = (
    Hello
    | Resize
    | Ack
    | Tap
    | Move
    | Wheel
    | Visible
    | Pinch
    | Key
    | Text
    | Compose
    | Choose
    | SetDate
    | Answer
    | PageOp
)
_decoder = msgspec.json.Decoder(Inbound)


def decode(frame: str | bytes) -> Inbound:
    """§6 step 1: a text frame holding one JSON object of a known type. Raises ProtocolViolation.

    Binary frames are the caller's to refuse (they never reach here as `str`); `bytes` is UTF-8 JSON text.
    """
    try:
        message: Inbound = _decoder.decode(frame)
    except msgspec.DecodeError:
        raise ProtocolViolation("malformed JSON") from None
    except msgspec.ValidationError:
        raise ProtocolViolation("not a fw.cast1 message") from None  # msgspec's text may quote the viewer's keys
    return message


def numbered(message_type: str) -> bool:
    """Whether a viewer→agent message gets a message number (§1): all but `ack`."""
    return message_type != "ack"


# ---- the value rules (§6 step 2)

NAMED_KEYS: Final = frozenset(
    {
        "Backspace",
        "Delete",
        "Enter",
        "Tab",
        "Escape",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)
KEYS: Final = NAMED_KEYS | {"Shift"}  # Shift only as a half press (`ph`)
WORD_KEYS: Final = frozenset({"Backspace", "Delete", "ArrowLeft", "ArrowRight"})
PRESS_HALVES: Final = frozenset({"down", "up"})
PAGE_OPS: Final = frozenset({"show", "close"})
MAX_TEXT_CODE_POINTS: Final = 256
MAX_TEXT_BYTES: Final = 1024
MAX_COMPOSE_CODE_POINTS: Final = 64
MAX_ANSWER_CODE_POINTS: Final = 256
MAX_OPTION_INDEX: Final = 399
MAX_DELTA: Final = 4000.0
MAX_TAP_MS: Final = 300

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # fullmatch: no trailing LF, ASCII digits only
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


class Known(Protocol):
    """What the session knows when a message arrives, for the rules that depend on it (§6, §6.12)."""

    def image(self, seq: int) -> tuple[int, int] | None:
        """`w`, `h` of a binary message the agent still remembers (the last 16), or None."""

    def latest(self, page: int) -> tuple[int, int] | None:
        """`w`, `h` of the latest binary message sent for this page, or None if none was."""

    def field_kind(self, rev: int, field: int) -> str | None:
        """`k` of a field of a map revision the agent holds, or None."""


def _bad_char(cp: int, *, tab_lf: bool) -> bool:
    if cp < 0x20:
        return not (tab_lf and cp in (0x09, 0x0A))
    return 0x7F <= cp <= 0x9F or 0xD800 <= cp <= 0xDFFF


def clean_text_ok(s: str) -> bool:
    """Clean text (§1): no C0 or C1 control, DEL or surrogate."""
    return not any(_bad_char(ord(ch), tab_lf=False) for ch in s)


def typed_text_ok(s: str) -> bool:
    """Typed text (§1, §6.10): clean text that may also hold TAB and LF."""
    return not any(_bad_char(ord(ch), tab_lf=True) for ch in s)


def scale_factor(w: int, h: int, dpr: float) -> int:
    """The pod screen's scale factor for a viewer (§6.1): 2 within the 1.6 MP budget for a high-DPI viewer."""
    return 2 if dpr >= 1.5 and w * h * 4 <= 1_600_000 else 1


def _within(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def _id(value: int, high: int = 65535) -> bool:
    return 1 <= value <= high


def _size_ok(w: int, h: int, dpr: float) -> bool:
    return _within(w, 240, 1920) and _within(h, 240, 1600) and _within(dpr, 0.5, 5)


def _on(size: tuple[int, int] | None, *points: tuple[float, float]) -> bool:
    """Points in [0, w] x [0, h] of an image; any point when the image isn't known (§6)."""
    if size is None:
        return True
    w, h = size
    return all(0 <= x <= w and 0 <= y <= h for x, y in points)


def _hello(m: Hello, _known: Known | None) -> str | None:
    if m.v != VERSION:
        return "v"
    return None if _size_ok(m.w, m.h, m.dpr) else "size"


def _resize(m: Resize, _known: Known | None) -> str | None:
    return None if _size_ok(m.w, m.h, m.dpr) else "size"


def _ack(m: Ack, _known: Known | None) -> str | None:
    return None if _id(m.seq, U32) else "seq"


def _tap(m: Tap, known: Known | None) -> str | None:
    if not (_id(m.page) and _id(m.seq, U32)):
        return "page or seq"
    if not _within(m.ms, 0, MAX_TAP_MS):
        return "ms"
    size = known.image(m.seq) if known is not None else None
    return None if _on(size, (m.x, m.y)) else "point"


def _move(m: Move, known: Known | None) -> str | None:
    if not _id(m.page):
        return "page"
    size = known.latest(m.page) if known is not None else None
    return None if _on(size, (m.x, m.y)) else "point"


def _wheel(m: Wheel, known: Known | None) -> str | None:
    if not _id(m.page):
        return "page"
    if not (_within(m.dx, -MAX_DELTA, MAX_DELTA) and _within(m.dy, -MAX_DELTA, MAX_DELTA)):
        return "delta"
    size = known.latest(m.page) if known is not None else None
    return None if _on(size, (m.x, m.y)) else "point"


def _visible(_m: Visible, _known: Known | None) -> str | None:
    return None


def _pinch(m: Pinch, known: Known | None) -> str | None:
    if not (_id(m.page) and _id(m.seq, U32)):
        return "page or seq"
    if not _within(m.scale, 0.25, 4):
        return "scale"
    size = known.image(m.seq) if known is not None else None
    return None if _on(size, (m.x0, m.y0), (m.x1, m.y1)) else "point"


def _key(m: Key, _known: Known | None) -> str | None:
    half = isinstance(m.ph, str)
    if isinstance(m.k, str) == isinstance(m.c, str):
        return "exactly one of k and c"
    if half and (m.ph not in PRESS_HALVES or m.shift or m.word):
        return "ph"
    if isinstance(m.k, str):
        named_ok = m.k in NAMED_KEYS or (m.k == "Shift" and half)
        return None if named_ok and (not m.word or m.k in WORD_KEYS) else "k or word"
    c = m.c if isinstance(m.c, str) else ""
    return None if len(c) == 1 and clean_text_ok(c) and not (m.shift or m.word) else "c, shift or word"


def _text(m: Text, _known: Known | None) -> str | None:
    if not 1 <= len(m.s) <= MAX_TEXT_CODE_POINTS or len(m.s.encode()) > MAX_TEXT_BYTES:
        return "length"
    return None if typed_text_ok(m.s) else "characters"


def _compose(m: Compose, _known: Known | None) -> str | None:
    if len(m.s) > MAX_COMPOSE_CODE_POINTS:
        return "length"
    return None if clean_text_ok(m.s) else "characters"


def _map_ref(page: int, rev: int, field: int) -> bool:
    return _id(page) and 0 <= rev <= U32 and _id(field)


def _choose(m: Choose, _known: Known | None) -> str | None:
    if not _map_ref(m.page, m.rev, m.f):
        return "page, rev or f"
    return None if _within(m.i, 0, MAX_OPTION_INDEX) else "i"


def _set_date(m: SetDate, known: Known | None) -> str | None:
    if not _map_ref(m.page, m.rev, m.f):
        return "page, rev or f"
    is_date, is_time = date_value_ok(m.v), _TIME.fullmatch(m.v) is not None
    kind = known.field_kind(m.rev, m.f) if known is not None else None
    if kind == "date":
        return None if is_date else "v"
    if kind == "time":
        return None if is_time else "v"
    return None if is_date or is_time else "v"  # not a date or time field the agent holds: `stale` later


def date_value_ok(v: str) -> bool:
    """A real calendar date, YYYY-MM-DD in ASCII digits (§6.12)."""
    if _DATE.fullmatch(v) is None:
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _answer(m: Answer, _known: Known | None) -> str | None:
    if not _id(m.id):
        return "id"
    if isinstance(m.s, str) and (len(m.s) > MAX_ANSWER_CODE_POINTS or not clean_text_ok(m.s)):
        return "s"
    return None


def _page_op(m: PageOp, _known: Known | None) -> str | None:
    return None if m.op in PAGE_OPS and _id(m.page) else "op or page"


_RULES: Mapping[type[Any], Callable[[Any, Known | None], str | None]] = {
    Hello: _hello,
    Resize: _resize,
    Ack: _ack,
    Tap: _tap,
    Move: _move,
    Wheel: _wheel,
    Visible: _visible,
    Pinch: _pinch,
    Key: _key,
    Text: _text,
    Compose: _compose,
    Choose: _choose,
    SetDate: _set_date,
    Answer: _answer,
    PageOp: _page_op,
}


def check(message: Inbound, known: Known | None = None) -> str | None:
    """§6 step 2: None when every value rule holds, else what broke one (a rule's name, never a value).

    A `hello` that breaks a rule closes the connection (4400); any other message is dropped with
    `error invalid`. Without `known`, the rules that need the session's images and field map are skipped.
    """
    return _RULES[type(message)](message, known)


def message_type(message: Inbound) -> str:
    """The message's `t`."""
    tag = message.__struct_config__.tag
    assert isinstance(tag, str)  # noqa: S101 - every inbound struct is tagged
    return tag


# ---- images (§4)

HEADER: Final = struct.Struct("<BBHIHHffffBBH")
IMAGE: Final = 1
REPEAT: Final = 2
FIRST: Final = 0x01  # flag bit 0


@dataclass(frozen=True, slots=True)
class ImageHeader:
    """The 32 bytes in front of every binary message."""

    kind: int  # IMAGE or REPEAT
    slot: int
    page: int
    seq: int
    w: int
    h: int
    scale: float
    sx: float
    sy: float
    top: float
    dsf: int
    flags: int = 0

    def pack(self) -> bytes:
        return HEADER.pack(
            self.kind,
            self.slot,
            self.page,
            self.seq,
            self.w,
            self.h,
            self.scale,
            self.sx,
            self.sy,
            self.top,
            self.dsf,
            self.flags,
            0,
        )

    @classmethod
    def unpack(cls, data: bytes) -> ImageHeader:
        kind, slot, page, seq, w, h, scale, sx, sy, top, dsf, flags, _ = HEADER.unpack_from(data)
        return cls(kind, slot, page, seq, w, h, scale, sx, sy, top, dsf, flags)

    @property
    def first(self) -> bool:
        return bool(self.flags & FIRST)


# ---- agent → viewer (§5): every message the agent sends is one of these, encoded by encode()


class _Out(msgspec.Struct, tag_field="t", forbid_unknown_fields=True, frozen=True, kw_only=True):
    pass


class _Part(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    pass


MAX_DIALOG_MESSAGE: Final = 500
MAX_PROMPT_DEFAULT: Final = 256
MAX_TITLE: Final = 80
MAX_ORIGIN: Final = 100
MAX_LABEL: Final = 64

Id = Annotated[int, msgspec.Meta(ge=1, le=65535)]
Count = Annotated[int, msgspec.Meta(ge=0, le=U32)]
Number = Annotated[float, msgspec.Meta(ge=-1e9, le=1e9)]
Rect = tuple[Number, Number, Number, Number]
Title = Annotated[str, msgspec.Meta(max_length=MAX_TITLE)]
Origin = Annotated[str, msgspec.Meta(max_length=MAX_ORIGIN)]
Label = Annotated[str, msgspec.Meta(max_length=MAX_LABEL)]  # option labels and `ac`
Message = Annotated[str, msgspec.Meta(max_length=MAX_DIALOG_MESSAGE)]
PromptDefault = Annotated[str, msgspec.Meta(max_length=MAX_PROMPT_DEFAULT)]
FieldValue = Annotated[str, msgspec.Meta(max_length=10)]  # a date or time field's value


class Ready(_Out, tag="ready", frozen=True, kw_only=True):
    v: Literal[1] = 1
    w: Annotated[int, msgspec.Meta(ge=240, le=1920)]
    h: Annotated[int, msgspec.Meta(ge=240, le=1600)]
    dsf: Literal[1, 2]
    features: Annotated[list[str], msgspec.Meta(max_length=0)] = []


class PageInfo(_Part, frozen=True, kw_only=True):
    id: Id
    title: Title
    origin: Origin
    main: bool


class Pages(_Out, tag="pages", frozen=True, kw_only=True):
    front: Id
    pages: Annotated[list[PageInfo], msgspec.Meta(min_length=1, max_length=8)]


FieldKind = Literal["text", "area", "select", "date", "time"]
FieldType = Literal[
    "text",
    "email",
    "tel",
    "url",
    "number",
    "password",
    "search",
    "date",
    "time",
    "textarea",
    "select",
    "contenteditable",
]
InputMode = Literal["none", "text", "decimal", "numeric", "tel", "search", "email", "url", ""]
EnterKeyHint = Literal["enter", "done", "go", "next", "previous", "search", "send", ""]
AutoCapitalize = Literal["off", "none", "on", "sentences", "words", "characters", ""]
Option = tuple[Label, bool]  # label, disabled


class Field(_Part, frozen=True, kw_only=True):
    """One editable field (§5.3.1). `id` only in a map, not in `focus`; the select and date/time parts only there."""

    id: Id | msgspec.UnsetType = msgspec.UNSET
    k: FieldKind
    r: Rect
    c: Rect
    m: Annotated[int, msgspec.Meta(ge=1, le=511)]
    type: FieldType
    im: InputMode
    ac: Label
    ekh: EnterKeyHint
    cap: AutoCapitalize
    max: Annotated[int, msgspec.Meta(ge=1, le=524288)] | None
    ro: bool
    nopt: Annotated[int, msgspec.Meta(ge=0, le=1_000_000)] | msgspec.UnsetType = msgspec.UNSET
    opts: Annotated[list[Option], msgspec.Meta(max_length=400)] | msgspec.UnsetType = msgspec.UNSET
    sel: Annotated[int, msgspec.Meta(ge=-1, le=1_000_000)] | msgspec.UnsetType = msgspec.UNSET
    trunc: bool | msgspec.UnsetType = msgspec.UNSET
    v: FieldValue | msgspec.UnsetType = msgspec.UNSET


class Fields(_Out, tag="fields", frozen=True, kw_only=True):
    page: Id
    rev: Count
    seq: Count
    n: Count
    sx: Number
    sy: Number
    scale: Number
    cw: Number
    ch: Number
    trunc: bool
    f: Annotated[list[Field], msgspec.Meta(max_length=800)]


class FieldsOk(_Out, tag="fields_ok", frozen=True, kw_only=True):
    page: Id
    rev: Count
    seq: Count
    n: Count


class Focus(_Out, tag="focus", frozen=True, kw_only=True):
    page: Id
    seq: Count
    n: Count
    f: Field | None
    popup: bool


DialogKind = Literal["alert", "confirm", "prompt", "beforeunload"]


class Dialog(_Out, tag="dialog", frozen=True, kw_only=True):
    id: Id
    page: Id
    kind: DialogKind
    origin: Origin
    msg: Message
    default: PromptDefault = msgspec.field(name="def", default="")


class DialogClosed(_Out, tag="dialog_closed", frozen=True, kw_only=True):
    id: Id


ErrorCode = Literal["invalid", "stale", "failed", "rate", "unsupported"]
InboundType = Literal[
    "hello", "resize", "ack", "tap", "move", "wheel", "visible", "pinch", "key", "text", "compose", "choose", "date",
    "dialog", "page",
]  # fmt: skip


class Error(_Out, tag="error", frozen=True, kw_only=True):
    code: ErrorCode
    ref: InboundType


class Clock(_Out, tag="clock", frozen=True, kw_only=True):
    ends: float | None
    hard: float
    paused: bool


Outbound = Ready | Pages | Fields | FieldsOk | Focus | Dialog | DialogClosed | Error | Clock
_outbound = msgspec.json.Decoder(Outbound)
_encoder = msgspec.json.Encoder()


def encode(message: Outbound) -> bytes:
    """One agent→viewer message, checked against §5's limits (a ValueError is an agent bug, never sent)."""
    data = _encoder.encode(message)
    if len(data) > MAX_OUTBOUND_BYTES:
        raise ValueError(f"a {type(message).__name__} message of {len(data)} bytes")
    try:
        _outbound.decode(data)
    except msgspec.ValidationError as e:
        raise ValueError(f"a {type(message).__name__} message outside the protocol: {e}") from None
    return data


def decode_outbound(data: str | bytes) -> Outbound:
    """The agent's own outbound schema, for tests (raises msgspec's errors)."""
    message: Outbound = _outbound.decode(data)
    return message


# ---- page-derived strings (§5, C39): bounded and cleaned before they leave the pod


_UNCLEAN = re.compile("[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_UNCLEAN_BUT_LF = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


def clean(s: str, limit: int, *, keep_lf: bool = False) -> str:
    """Page text as clean text (§1), cut to `limit` code points. `keep_lf` keeps line feeds (dialog messages).

    Only the first `4 * limit` code points are looked at, so a huge string costs no more than a short one.
    """
    return (_UNCLEAN_BUT_LF if keep_lf else _UNCLEAN).sub("", s[: limit * 4])[:limit]


def origin_of(url: str) -> str:
    """`scheme://host[:port]` of an http(s) URL (§5.2, §5.5), or "" for anything else.

    An origin too long for the protocol is "" rather than cut: a cut one could read as another site's.
    """
    try:
        parts = urllib.parse.urlsplit(url[:4096])
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = 443 if parts.scheme == "https" else 80
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port is not None and port != default else "")
    return origin if len(origin) <= MAX_ORIGIN and clean_text_ok(origin) else ""


CREDENTIAL_TOKENS: Final = frozenset({"username", "current-password", "new-password", "webauthn"})
_ASCII_WHITESPACE = re.compile(r"[\t\n\f\r ]+")
_TOKEN = re.compile(r"[a-z0-9-]+")
MAX_AUTOCOMPLETE: Final = 64


def strip_credential_tokens(autocomplete: str) -> str:
    """A field's `ac` (§5.3.1): its `autocomplete` tokens, lowercased, without credential tokens or odd ones.

    A token with any character outside [a-z0-9-] is dropped, never repaired, so `user_name` can't become
    `username` on its way to the viewer's proxy.
    """
    tokens = _ASCII_WHITESPACE.split(autocomplete[:MAX_AUTOCOMPLETE].lower())
    kept = [t for t in tokens if _TOKEN.fullmatch(t) and t not in CREDENTIAL_TOKENS]
    return " ".join(kept)


def dialog_message(
    number: int, page: int, kind: DialogKind, origin_url: str, message: str, default: str = ""
) -> Dialog:
    """A `dialog` (§5.5) from what the page gave: origin from the opening frame's URL, text capped and cleaned."""
    return Dialog(
        id=number,
        page=page,
        kind=kind,
        origin=origin_of(origin_url),
        msg=clean(message, MAX_DIALOG_MESSAGE, keep_lf=True),
        default=clean(default, MAX_PROMPT_DEFAULT) if kind == "prompt" else "",
    )


# ---- rates (§7.1)


@dataclass(frozen=True, slots=True)
class Rate:
    rate: float  # messages a second
    burst: float
    cp_rate: float = 0.0  # `text` only: code points a second
    cp_burst: float = 0.0


RATES: Final[Mapping[str, Rate]] = {
    "ack": Rate(40, 40),
    "tap": Rate(10, 5),
    "move": Rate(40, 40),
    "wheel": Rate(40, 20),
    "pinch": Rate(4, 2),
    "key": Rate(60, 40),
    "text": Rate(20, 10, cp_rate=512, cp_burst=1024),
    "compose": Rate(60, 30),
    "choose": Rate(4, 2),
    "date": Rate(4, 2),
    "dialog": Rate(4, 2),
    "page": Rate(4, 2),
    "resize": Rate(4, 4),
    "visible": Rate(4, 4),
}
OVERALL_RATE: Final = Rate(300, 400)
MAX_DROPS: Final = 200  # dropped messages (over a bucket, or `error invalid`) ...
DROP_WINDOW_SECONDS: Final = 10.0  # ... in any 10 s, before 4429
MAX_QUEUED: Final = 64  # messages waiting to be carried out (§3.4)
