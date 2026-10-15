"""A stand-in for the pod's agent on the streamed viewer's socket (docs/multi-tenant/streamed-viewer/PROTOCOL.md).

The viewer's browser tests run the real viewer against it. It admits ``fw.cast1`` upgrades as the gate does (the
Origin, exactly one viewer kind, the device's nonce, a ticket until the first connect locks the checkout to that
nonce), and checks every message the viewer sends against PROTOCOL.md §6: anything the real agent would close the
connection for (4400, 1009) or drop with ``error invalid`` is recorded in ``Session.problems``, which a test expects
to stay empty. Nothing is carried out: the test decides what the agent sends back, with the conformance pack's
messages (tests/fixtures/cast1/) where it can.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import re
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec
from aiohttp import WSMsgType, web

PACK = Path(__file__).resolve().parents[1] / "fixtures" / "cast1"
SUBPROTOCOL = "fw.cast1"
MAX_FRAME_BYTES = 4096
HELLO_TIMEOUT_S = 5.0
WAIT_S = 10.0  # for what a test waits on
HEADER = struct.Struct("<BBHIHHffffBBH")
U16 = 0xFFFF
U32 = 0xFFFFFFFF
_NONCE = re.compile(r"[A-Za-z0-9_-]{22,64}")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
NAMED_KEYS = frozenset(
    "Backspace Delete Enter Tab Escape ArrowLeft ArrowRight ArrowUp ArrowDown Home End PageUp PageDown".split()
)
WORD_KEYS = frozenset({"Backspace", "Delete", "ArrowLeft", "ArrowRight"})


def pack(name: str) -> Any:
    return json.loads((PACK / name).read_text("utf-8"))


# ---- what the viewer may send (§6)


class _Inbound(msgspec.Struct, tag_field="t", forbid_unknown_fields=True):
    pass


class Hello(_Inbound, tag="hello"):
    v: int
    w: int
    h: int
    dpr: float


class Resize(_Inbound, tag="resize"):
    w: int
    h: int
    dpr: float


class Ack(_Inbound, tag="ack"):
    seq: int


class Tap(_Inbound, tag="tap"):
    page: int
    seq: int
    x: float
    y: float
    ms: int = 0


class Move(_Inbound, tag="move"):
    page: int
    x: float
    y: float


class Wheel(_Inbound, tag="wheel"):
    page: int
    x: float
    y: float
    dx: float
    dy: float


class Visible(_Inbound, tag="visible"):
    v: bool


class Pinch(_Inbound, tag="pinch"):
    page: int
    seq: int
    x0: float
    y0: float
    x1: float
    y1: float
    scale: float


class Key(_Inbound, tag="key"):
    k: str | msgspec.UnsetType = msgspec.UNSET
    c: str | msgspec.UnsetType = msgspec.UNSET
    shift: bool = False
    word: bool = False
    ph: str | msgspec.UnsetType = msgspec.UNSET


class Text(_Inbound, tag="text"):
    s: str


class Compose(_Inbound, tag="compose"):
    s: str


class Choose(_Inbound, tag="choose"):
    page: int
    rev: int
    f: int
    i: int


class Date(_Inbound, tag="date"):
    page: int
    rev: int
    f: int
    v: str


class Dialog(_Inbound, tag="dialog"):
    id: int
    ok: bool
    s: str | msgspec.UnsetType = msgspec.UNSET


class Page(_Inbound, tag="page"):
    op: str
    page: int


Inbound = (
    Hello | Resize | Ack | Tap | Move | Wheel | Visible | Pinch | Key | Text | Compose | Choose | Date | Dialog | Page
)
_decoder = msgspec.json.Decoder(Inbound)


@dataclass(frozen=True, slots=True)
class Image:
    """What the agent remembers of a binary message it sent (§4.2)."""

    seq: int
    page: int
    w: int
    h: int


@dataclass(frozen=True, slots=True)
class Context:
    """What a message's values are checked against: the images sent, and the field map's kinds by field id."""

    images: tuple[Image, ...] = ()
    fields: Mapping[int, str] = field(default_factory=dict)

    def named(self, seq: int) -> Image | None:
        """The image a `tap` or `pinch` names, if it is among the last 16 sent."""
        return next((image for image in self.images[-16:] if image.seq == seq), None)

    def latest(self, page: int) -> Image | None:
        """The image `move` and `wheel` coordinates are in: the latest of their page."""
        return next((image for image in reversed(self.images) if image.page == page), None)


class Refused(Exception):
    """A structural violation (§6 step 1): the connection closes with ``code``."""

    def __init__(self, reason: str, code: int = 4400) -> None:
        super().__init__(reason)
        self.code = code


def decode(frame: str | bytes) -> _Inbound:
    """Step 1, structure: raises Refused for whatever the real agent closes 4400 (or 1009) for."""
    if isinstance(frame, bytes):
        raise Refused("a binary frame")
    if len(frame.encode()) > MAX_FRAME_BYTES:
        raise Refused("over 4096 bytes", 1009)
    try:
        msg: _Inbound = _decoder.decode(frame)
    except msgspec.ValidationError as e:
        raise Refused(f"not a viewer message: {e}") from None
    except msgspec.DecodeError as e:
        raise Refused(f"not JSON: {e}") from None
    return msg


def _clean(s: str, *, lines: bool = False) -> bool:
    for ch in s:
        cp = ord(ch)
        if cp < 0x20 and not (lines and cp in (0x09, 0x0A)):
            return False
        if 0x7F <= cp <= 0x9F or 0xD800 <= cp <= 0xDFFF:
            return False
    return True


def _real_date(v: str) -> bool:
    match = _DATE.fullmatch(v)
    if not match:
        return False
    try:
        datetime.date(*(int(part) for part in match.groups()))
    except ValueError:
        return False
    return True


def _key_ok(msg: Key) -> bool:
    k, c, ph = msg.k, msg.c, msg.ph
    if (k is msgspec.UNSET) == (c is msgspec.UNSET):
        return False
    if ph is not msgspec.UNSET and (ph not in ("down", "up") or msg.shift or msg.word):
        return False
    if isinstance(c, str):
        return len(c) == 1 and _clean(c) and not msg.shift and not msg.word
    if k == "Shift":
        return ph is not msgspec.UNSET
    return k in NAMED_KEYS and (not msg.word or k in WORD_KEYS)


def _in(image: Image | None, *points: float) -> bool:
    """Coordinates in [0, w] by [0, h] of the image (§6); a message about an image the agent doesn't know passes."""
    if image is None:
        return True
    xs, ys = points[0::2], points[1::2]
    return all(0 <= x <= image.w for x in xs) and all(0 <= y <= image.h for y in ys)


def _size(w: int, h: int, dpr: float) -> bool:
    return 240 <= w <= 1920 and 240 <= h <= 1600 and 0.5 <= dpr <= 5


def _date_ok(msg: Date, kind: str | None) -> bool:
    """A date field takes a real date, a time field a time; a field the map doesn't name, either."""
    date, time = _real_date(msg.v), _TIME.fullmatch(msg.v) is not None
    form = time if kind == "time" else date if kind is not None else date or time
    return 0 <= msg.rev <= U32 and 1 <= msg.f <= U16 and form


def _seq(seq: int) -> bool:
    return 1 <= seq <= U32


def _dialog_ok(msg: Dialog) -> bool:
    return 1 <= msg.id <= U16 and (not isinstance(msg.s, str) or (len(msg.s) <= 256 and _clean(msg.s)))


_VALUES: dict[type[_Inbound], Callable[[Any, Context], bool]] = {
    Hello: lambda m, _: m.v == 1 and _size(m.w, m.h, m.dpr),
    Resize: lambda m, _: _size(m.w, m.h, m.dpr),
    Ack: lambda m, _: _seq(m.seq),
    Tap: lambda m, seen: _seq(m.seq) and 0 <= m.ms <= 300 and _in(seen.named(m.seq), m.x, m.y),
    Move: lambda m, seen: _in(seen.latest(m.page), m.x, m.y),
    Wheel: lambda m, seen: max(abs(m.dx), abs(m.dy)) <= 4000 and _in(seen.latest(m.page), m.x, m.y),
    Pinch: lambda m, seen: (_seq(m.seq) and 0.25 <= m.scale <= 4 and _in(seen.named(m.seq), m.x0, m.y0, m.x1, m.y1)),
    Key: lambda m, _: _key_ok(m),
    Text: lambda m, _: 1 <= len(m.s) <= 256 and len(m.s.encode()) <= 1024 and _clean(m.s, lines=True),
    Compose: lambda m, _: len(m.s) <= 64 and _clean(m.s),
    Choose: lambda m, _: 0 <= m.rev <= U32 and 1 <= m.f <= U16 and 0 <= m.i <= 399,
    Date: lambda m, seen: _date_ok(m, seen.fields.get(m.f)),
    Dialog: lambda m, _: _dialog_ok(m),
    Page: lambda m, _: m.op in ("show", "close"),
}


def valid(msg: _Inbound, context: Context | None = None) -> bool:
    """Step 2, values: False for what the real agent drops with ``error invalid``."""
    if not 1 <= getattr(msg, "page", 1) <= U16:
        return False
    check = _VALUES.get(type(msg))
    return check is None or check(msg, context or Context())


# ---- the stand-in agent


@dataclass
class Session:
    """One admitted connection, from the agent's side."""

    ws: web.WebSocketResponse
    offered: list[str]
    hello: dict[str, Any] | None = None
    received: list[dict[str, Any]] = field(default_factory=list)  # every message after `hello`, acks included
    problems: list[str] = field(default_factory=list)
    acks: list[int] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    fields: dict[int, str] = field(default_factory=dict)
    ready: bool = False
    seq: int = 0
    greeted: asyncio.Event = field(default_factory=asyncio.Event)  # it said hello, or closed without
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int | None = None
    _arrived: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def start(self, *, dsf: int | None = None, front: int = 1) -> None:
        """`ready` for the viewer's hello, then `pages` and `clock` as the conformance pack has them."""
        assert self.hello is not None
        w, h, dpr = self.hello["w"], self.hello["h"], self.hello["dpr"]
        scale = dsf if dsf is not None else 2 if dpr >= 1.5 and w * h * 4 <= 1_600_000 else 1
        await self.send({"t": "ready", "v": 1, "w": w, "h": h, "dsf": scale, "features": []})
        self.ready = True
        outbound = {case["id"]: case["msg"] for case in pack("messages.json")["outbound"]}
        await self.send({**outbound["out-pages"], "front": front})
        await self.send(outbound["out-clock"])

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(msg))

    async def image(
        self,
        jpeg: bytes,
        *,
        slot: int = 0,
        page: int = 1,
        w: int | None = None,
        h: int | None = None,
        scale: float = 1.0,
        sx: float = 0.0,
        sy: float = 0.0,
        dsf: int = 1,
        first: bool = False,
    ) -> int:
        """Sends an image (kind 1); its seq."""
        return await self._binary(1, slot, page, w, h, scale, sx, sy, dsf, first, jpeg)

    async def repeat(self, slot: int, *, page: int = 1, w: int | None = None, h: int | None = None) -> int:
        """Sends a repeat (kind 2) of what `slot` holds; its seq."""
        return await self._binary(2, slot, page, w, h, 1.0, 0.0, 0.0, 1, False, b"")

    async def _binary(
        self,
        kind: int,
        slot: int,
        page: int,
        w: int | None,
        h: int | None,
        scale: float,
        sx: float,
        sy: float,
        dsf: int,
        first: bool,
        jpeg: bytes,
    ) -> int:
        assert self.hello is not None
        width = self.hello["w"] if w is None else w
        height = self.hello["h"] if h is None else h
        self.seq += 1
        header = HEADER.pack(kind, slot, page, self.seq, width, height, scale, sx, sy, 0.0, dsf, int(first), 0)
        self.images.append(Image(self.seq, page, width, height))
        await self.ws.send_bytes(header + jpeg)
        return self.seq

    async def close(self, code: int) -> None:
        await self.ws.close(code=code)

    async def wait_for(self, t: str, *, count: int = 1) -> list[dict[str, Any]]:
        """The first `count` messages of type `t` the viewer has sent, once it has."""

        def arrived() -> bool:
            return sum(1 for m in self.received if m.get("t") == t) >= count

        async with asyncio.timeout(WAIT_S), self._arrived:
            await self._arrived.wait_for(arrived)
        return [m for m in self.received if m.get("t") == t][:count]

    async def acked(self, seq: int) -> None:
        async with asyncio.timeout(WAIT_S), self._arrived:
            await self._arrived.wait_for(lambda: seq in self.acks)

    async def run(self) -> None:
        try:
            first = await self.ws.receive(timeout=HELLO_TIMEOUT_S)
        except TimeoutError:
            await self._refuse("no hello in 5 s", 4408)
            return
        if not await self._take_hello(first):
            return
        async for frame in self.ws:
            if frame.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                break
            if not await self._take(frame.data):
                return

    async def _take_hello(self, frame: Any) -> bool:
        if frame.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            return False  # the viewer went before saying anything
        if frame.type is not WSMsgType.TEXT:
            await self._refuse("the first frame isn't text")
            return False
        try:
            msg = decode(frame.data)
        except Refused as e:
            await self._refuse(f"hello: {e}", e.code)
            return False
        if not isinstance(msg, Hello) or not valid(msg):
            await self._refuse(f"the first message is a bad {type(msg).__name__}")
            return False
        self.hello = json.loads(frame.data)
        self.greeted.set()
        return True

    async def _take(self, data: str | bytes) -> bool:
        try:
            msg = decode(data)
        except Refused as e:
            await self._refuse(str(e), e.code)
            return False
        if isinstance(msg, Hello) or not self.ready:
            await self._refuse(f"{type(msg).__name__} {'again' if isinstance(msg, Hello) else 'before ready'}")
            return False
        raw = json.loads(data)
        if not valid(msg, Context(tuple(self.images), self.fields)):
            self.problems.append(f"error invalid: {raw['t']}")
            await self.send({"t": "error", "code": "invalid", "ref": raw["t"]})
        if isinstance(msg, Ack):
            self.acks.append(msg.seq)
        async with self._arrived:
            self.received.append(raw)
            self._arrived.notify_all()
        return True

    async def _refuse(self, reason: str, code: int = 4400) -> None:
        self.problems.append(f"close {code}: {reason}")
        await self.ws.close(code=code)


class FakeAgent:
    """The gate's admission for `fw.cast1`, and a Session for each admitted connection."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.sessions: list[Session] = []
        self.refusals: list[int] = []  # the HTTP status of each refused upgrade
        self.refuse_with: int | None = None  # refuse every upgrade with this status (the viewer sees 1006)
        self._lock: str | None = None  # the nonce the first connect locked the checkout to
        self._changed = asyncio.Condition()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        status = self._admit(request, offered)
        if status is not None:
            async with self._changed:
                self.refusals.append(status)
                self._changed.notify_all()
            return web.Response(status=status, text=f"{status}\n")
        ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), max_msg_size=MAX_FRAME_BYTES, heartbeat=20.0)
        await ws.prepare(request)
        session = Session(ws, offered)
        for old in self.sessions:
            if not old.ws.closed:
                await old.close(4001)  # one viewer at a time
        async with self._changed:
            self.sessions.append(session)
            self._changed.notify_all()
        try:
            await session.run()
        finally:
            session.close_code = ws.close_code
            session.greeted.set()
            session.closed.set()
            with contextlib.suppress(ConnectionError):
                await ws.close()
        return ws

    def _admit(self, request: web.Request, offered: list[str]) -> int | None:
        """The HTTP status the upgrade is refused with, or None to admit it."""
        kinds = [p for p in offered if p in ("binary", SUBPROTOCOL)]
        nonces = [p.removeprefix("fw.n1.") for p in offered if p.startswith("fw.n1.")]
        tickets = [p for p in offered if p.startswith("fw.t1.")]
        if request.headers.get("Origin") != self.origin:
            return 403
        if kinds != [SUBPROTOCOL] or offered[0] != SUBPROTOCOL:
            return 400  # this stand-in serves the streamed viewer only
        if self.refuse_with is not None:
            return self.refuse_with
        nonce = nonces[0] if len(nonces) == 1 and _NONCE.fullmatch(nonces[0]) and len(tickets) <= 1 else None
        if nonce is None or self._lock not in (None, nonce) or (self._lock is None and not tickets):
            return 403
        self._lock = nonce
        return None

    async def session(self, index: int) -> Session:
        """The `index`-th admitted session (from 0), once it has said hello."""
        async with asyncio.timeout(WAIT_S):
            async with self._changed:
                await self._changed.wait_for(lambda: len(self.sessions) > index)
            session = self.sessions[index]
            await session.greeted.wait()
        return session

    async def refused(self, count: int) -> None:
        """Once `count` upgrades have been refused."""
        async with asyncio.timeout(WAIT_S), self._changed:
            await self._changed.wait_for(lambda: len(self.refusals) >= count)

    def problems(self) -> list[str]:
        return [problem for session in self.sessions for problem in session.problems]
