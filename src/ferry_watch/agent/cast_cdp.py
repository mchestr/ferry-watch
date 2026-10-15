"""The stream session's only way into Chromium: an allowlisted CDP wrapper, and isolated worlds per frame.

`Cdp` wraps one CDP session and refuses any method or parameter shape outside C34's allowlist
(DESIGN §5.2): screencast and screenshots, layout metrics, the frame tree and isolated worlds; mouse, key, text,
IME and pinch input with no modifier but Shift (and Ctrl for word-wise keys), no right or middle button, no
touch; `Runtime.evaluate` only of a fixed script (cast_scripts.py) in a world this wrapper created, with
`returnByValue` and the 100 ms timeout; `Runtime.callFunctionOn` only to store an owner iframe it resolved; the
DOM calls that find and resolve an owner iframe; `Target.getTargetInfo`. Nothing a viewer sends is ever a method
name, a script or a script argument. Every call is bounded (2 s): a page with an open JavaScript dialog answers
almost nothing (DESIGN §13), and a stuck call must not hold up the session or its teardown.

`Worlds` keeps an isolated world (which page script can't see) in each frame of one page, the out-of-process
frames' own CDP sessions, and each child frame's owner iframe kept in its parent's world, for the owner chain
(PROTOCOL §5.3.2). A frame's world is dropped when it navigates or detaches, with its descendants'; a call that
fails drops it too, and it's made again when next needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from .cast_protocol import KEYS, MAX_COMPOSE_CODE_POINTS, MAX_TEXT_CODE_POINTS, clean_text_ok, typed_text_ok
from .cast_scripts import STORE_OWNER, Argument, call, script_of

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Frame, Page

log = logging.getLogger(__name__)

CALL_SECONDS: Final = 2.0
EVALUATE_TIMEOUT_MS: Final = 100  # Runtime.evaluate's own budget in the renderer (callFunctionOn ignores one)
WORLD_NAME: Final = "fw-cast"
MAX_DEPTH: Final = 3  # frames inside iframes, at most this deep
MAX_WORLDS: Final = 24  # worlds kept for one page (the field map evaluates at most 8 frames of them)


class CdpError(Exception):
    """A call that failed or had no answer in time: the page or frame went, or is blocked by a dialog."""


class ScriptError(CdpError):
    """A fixed script threw, or ran past its timeout. Its world is fine."""


class NotAllowed(Exception):
    """A call outside the allowlist (C34): an agent bug, refused before anything reaches Chromium."""


def _finite(value: object, low: float = -1e7, high: float = 1e7) -> bool:
    return (
        isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high
    )


def _integer(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _shape(params: Mapping[str, Any], required: set[str], optional: frozenset[str] = frozenset()) -> bool:
    return required <= params.keys() <= required | optional


_MOUSE_TYPES: Final = frozenset({"mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"})
_KEY_TYPES: Final = frozenset({"keyDown", "rawKeyDown", "keyUp"})
_MODIFIERS: Final = frozenset({0, 2, 8, 10})  # none, Ctrl (word-wise keys only), Shift, both


def _mouse(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    if not _shape(params, {"type", "x", "y"}, frozenset({"button", "clickCount", "deltaX", "deltaY", "modifiers"})):
        return False
    wheel = params["type"] == "mouseWheel"
    return (
        params["type"] in _MOUSE_TYPES
        and _finite(params["x"])
        and _finite(params["y"])
        and params.get("button", "none") in ("left", "none")
        and params.get("clickCount", 1) == 1
        and params.get("modifiers", 0) == 0
        and all(_finite(params[d]) and wheel for d in ("deltaX", "deltaY") if d in params)
    )


def _one_key(value: object) -> bool:
    return isinstance(value, str) and (value in KEYS or (len(value) == 1 and clean_text_ok(value)))


def _key(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    optional = frozenset({"text", "unmodifiedText", "modifiers", "autoRepeat"})
    if not _shape(params, {"type", "key", "code", "windowsVirtualKeyCode"}, optional):
        return False
    code = params["code"]
    texts_ok = all(
        params[t] == "\r" or (isinstance(params[t], str) and len(params[t]) == 1 and clean_text_ok(params[t]))
        for t in ("text", "unmodifiedText")
        if t in params
    )
    return (
        params["type"] in _KEY_TYPES
        and _one_key(params["key"])
        and isinstance(code, str)
        and len(code) <= 16
        and (code == "" or (code.isascii() and code.isalnum()))
        and _integer(params["windowsVirtualKeyCode"], 0, 255)
        and texts_ok
        and params.get("modifiers", 0) in _MODIFIERS
        and params.get("autoRepeat", True) is True  # only ever set, and only on a down
        and ("autoRepeat" not in params or params["type"] != "keyUp")
    )


def _insert_text(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    text = params.get("text")
    return (
        _shape(params, {"text"})
        and isinstance(text, str)
        and 1 <= len(text) <= MAX_TEXT_CODE_POINTS
        and typed_text_ok(text)
    )


def _compose(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    text = params.get("text")
    return (
        _shape(params, {"text", "selectionStart", "selectionEnd"})
        and isinstance(text, str)
        and len(text) <= MAX_COMPOSE_CODE_POINTS
        and clean_text_ok(text)
        and _integer(params["selectionStart"], 0, 2 * MAX_COMPOSE_CODE_POINTS)
        and _integer(params["selectionEnd"], 0, 2 * MAX_COMPOSE_CODE_POINTS)
    )


def _pinch(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    return (
        _shape(params, {"x", "y", "scaleFactor", "gestureSourceType"}, frozenset({"relativeSpeed"}))
        and _finite(params["x"])
        and _finite(params["y"])
        and _finite(params["scaleFactor"], 0.25, 4)
        and params["gestureSourceType"] == "mouse"
        and _integer(params.get("relativeSpeed", 800), 1, 10_000)
    )


def _screencast(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    return (
        _shape(params, {"format", "quality", "everyNthFrame"})
        and params["format"] == "jpeg"
        and _integer(params["quality"], 1, 100)
        and _integer(params["everyNthFrame"], 1, 10)
    )


def _screenshot(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    return _shape(params, {"format", "quality"}) and params["format"] == "jpeg" and _integer(params["quality"], 1, 100)


def _frame_ack(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    return _shape(params, {"sessionId"}) and _integer(params["sessionId"], 0, 2**31)


def _none(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    return not params


def _frame_id(params: Mapping[str, Any], _cdp: Cdp) -> bool:
    frame = params.get("frameId")
    return _shape(params, {"frameId"}) and isinstance(frame, str) and 0 < len(frame) <= 64


def _create_world(params: Mapping[str, Any], cdp: Cdp) -> bool:
    return (
        _shape(params, {"frameId", "worldName"})
        and _frame_id({"frameId": params["frameId"]}, cdp)
        and params["worldName"] == WORLD_NAME
    )


def _evaluate(params: Mapping[str, Any], cdp: Cdp) -> bool:
    expression = params.get("expression")
    return (
        _shape(params, {"expression", "contextId", "returnByValue", "timeout"})
        and isinstance(expression, str)
        and script_of(expression) is not None
        and params["contextId"] in cdp.worlds
        and params["returnByValue"] is True
        and params["timeout"] == EVALUATE_TIMEOUT_MS
    )


def _store_owner(params: Mapping[str, Any], cdp: Cdp) -> bool:
    arguments = params.get("arguments")
    return (
        _shape(params, {"functionDeclaration", "objectId", "arguments", "returnByValue"})
        and params["functionDeclaration"] == STORE_OWNER
        and params["objectId"] in cdp.objects
        and isinstance(arguments, list)
        and len(arguments) == 1
        and isinstance(arguments[0], Mapping)
        and arguments[0].keys() == {"value"}
        and _integer(arguments[0]["value"], 0, 2**31)
        and params["returnByValue"] is True
    )


def _resolve_node(params: Mapping[str, Any], cdp: Cdp) -> bool:
    return (
        _shape(params, {"backendNodeId", "executionContextId"})
        and _integer(params["backendNodeId"], 0, 2**31)
        and params["executionContextId"] in cdp.worlds
    )


# C34: every method the stream may call, and the shape of its parameters.
ALLOWED: Final[Mapping[str, Callable[[Mapping[str, Any], Cdp], bool]]] = {
    "Page.startScreencast": _screencast,
    "Page.stopScreencast": _none,
    "Page.screencastFrameAck": _frame_ack,
    "Page.captureScreenshot": _screenshot,
    "Page.getLayoutMetrics": _none,
    "Page.getFrameTree": _none,
    "Page.createIsolatedWorld": _create_world,
    "Input.dispatchMouseEvent": _mouse,
    "Input.dispatchKeyEvent": _key,
    "Input.insertText": _insert_text,
    "Input.imeSetComposition": _compose,
    "Input.synthesizePinchGesture": _pinch,
    "Runtime.evaluate": _evaluate,
    "Runtime.callFunctionOn": _store_owner,
    "DOM.enable": _none,
    "DOM.getFrameOwner": _frame_id,
    "DOM.resolveNode": _resolve_node,
    "Target.getTargetInfo": _none,
}


class Cdp:
    """One CDP session of a stream session, through the allowlist."""

    def __init__(self, session: CDPSession, *, seconds: float = CALL_SECONDS) -> None:
        self._session = session
        self._seconds = seconds
        self.worlds: set[int] = set()  # execution contexts of the isolated worlds this wrapper created
        self.objects: set[str] = set()  # owner iframes it resolved into one of them
        self._listeners: list[tuple[str, Callable[[Any], None]]] = []
        self._dom = False
        self.detached = False

    async def send(
        self, method: str, params: Mapping[str, Any] | None = None, *, seconds: float | None = None
    ) -> dict[str, Any]:
        """One allowlisted call, bounded (by `seconds`, if shorter than the session's bound). Raises NotAllowed
        before sending anything outside the allowlist, and CdpError when it fails or has no answer in time (the
        error says which method, never what was sent)."""
        sent = dict(params or {})
        rule = ALLOWED.get(method)
        if rule is None or not rule(sent, self):
            raise NotAllowed(f"{method} isn't allowed with these parameters")
        if self.detached:
            raise CdpError(f"{method}: the session is detached")
        try:
            async with asyncio.timeout(min(self._seconds, seconds or self._seconds)):
                result: dict[str, Any] = await self._session.send(method, sent)
        except TimeoutError:
            raise CdpError(f"{method} had no answer in time") from None
        except Exception as e:  # noqa: BLE001 - Playwright's errors; their text is never passed on
            raise CdpError(f"{method} failed ({type(e).__name__})") from None
        if method == "Page.createIsolatedWorld" and isinstance(result.get("executionContextId"), int):
            self.worlds.add(result["executionContextId"])
        elif method == "DOM.resolveNode":
            object_id = result.get("object", {}).get("objectId")
            if isinstance(object_id, str):
                self.objects.add(object_id)
        return result

    async def evaluate(self, context: int, script: str, *args: Argument) -> Any:
        """A fixed script's value in one of this session's worlds. Raises ScriptError if it threw or timed out."""
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": call(script, *args),
                "contextId": context,
                "returnByValue": True,
                "timeout": EVALUATE_TIMEOUT_MS,
            },
        )
        if "exceptionDetails" in result:
            raise ScriptError("a script threw or ran out of time")
        return result.get("result", {}).get("value")

    async def enable_dom(self) -> None:
        if not self._dom:
            await self.send("DOM.enable")
            self._dom = True

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self._session.on(event, handler)
        self._listeners.append((event, handler))

    async def detach(self) -> None:
        """Remove its listeners and detach. Bounded; never raises."""
        if self.detached:
            return
        self.detached = True
        for event, handler in self._listeners:
            with contextlib.suppress(Exception):
                self._session.remove_listener(event, handler)
        self._listeners.clear()
        try:
            async with asyncio.timeout(self._seconds):
                await self._session.detach()
        except Exception as e:  # noqa: BLE001 - already gone with its page, most likely
            log.debug("A CDP session didn't detach: %s", type(e).__name__)


_late: set[asyncio.Task[None]] = set()  # detaching sessions nobody waited for


async def open_session(context: BrowserContext, target: Page | Frame, *, seconds: float = CALL_SECONDS) -> CDPSession:
    """A new CDP session onto a page or an out-of-process frame, bounded. Playwright makes the session whether or
    not anyone still waits for it: when the wait is cancelled or runs out, the session is detached as soon as it's
    there, so none is left attached for the rest of the target's life. Raises what Playwright raises, or
    TimeoutError."""
    opening = asyncio.ensure_future(context.new_cdp_session(target))
    try:
        async with asyncio.timeout(seconds):
            return await asyncio.shield(opening)
    except BaseException:
        opening.add_done_callback(_detach_late)
        raise


def _detach_late(opening: asyncio.Future[CDPSession]) -> None:
    if opening.cancelled() or opening.exception() is not None:
        return
    task = asyncio.ensure_future(_detach_quietly(opening.result()))
    _late.add(task)
    task.add_done_callback(_late.discard)


async def _detach_quietly(session: CDPSession) -> None:
    try:
        async with asyncio.timeout(CALL_SECONDS):
            await session.detach()
    except Exception as e:  # noqa: BLE001 - gone with its target already, most likely
        log.debug("A CDP session didn't detach: %s", type(e).__name__)


@dataclass(eq=False, slots=True)
class World:
    """An isolated world in one frame."""

    frame: Frame
    cdp: Cdp  # the session that reaches the frame
    frame_id: str
    context: int
    parent: World | None = None
    owner: int | None = None  # the frame's owner iframe, kept in the parent's world under this number
    depth: int = 0
    own_session: bool = False  # `cdp` is the frame's own (an out-of-process frame): detached with the world


class Worlds:
    """The isolated worlds of one page's frames."""

    def __init__(self, context: BrowserContext, page: Page, cdp: Cdp) -> None:
        """`cdp` is the page's own session (the main frame's, and that of every frame in its process)."""
        self._context = context
        self._page = page
        self._cdp = cdp
        self._worlds: dict[Frame, World] = {}
        self._making: dict[Frame, asyncio.Task[World | None]] = {}
        self._next_owner = 1
        self._closed = False
        self._detaching: set[asyncio.Task[None]] = set()
        page.on("framenavigated", self.drop)
        page.on("framedetached", self.drop)

    @property
    def page(self) -> Page:
        return self._page

    def alive(self, world: World) -> bool:
        return not self._closed and self._worlds.get(world.frame) is world

    def known(self, frame: Frame) -> World | None:
        return self._worlds.get(frame)

    async def get(self, frame: Frame) -> World | None:
        """The frame's world, made if need be; None when it can't be (gone, too deep, too many, or failing)."""
        world = self._worlds.get(frame)
        if world is not None or self._closed:
            return world
        making = self._making.get(frame)
        if making is None:
            if len(self._worlds) + len(self._making) >= MAX_WORLDS:
                return None
            making = asyncio.ensure_future(self._make(frame))
            self._making[frame] = making
            making.add_done_callback(lambda done: self._forget(frame, done))
        try:
            return await asyncio.shield(making)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if making.cancelled() and (current is None or not current.cancelling()):
                return None  # the frame was dropped meanwhile, not this caller
            raise

    def _forget(self, frame: Frame, done: asyncio.Task[World | None]) -> None:
        if self._making.get(frame) is done:
            del self._making[frame]

    async def evaluate(self, world: World, script: str, *args: Argument) -> Any:
        """A script's value in a world. A failed call (not a script's own error) drops the world."""
        try:
            return await world.cdp.evaluate(world.context, script, *args)
        except ScriptError:
            raise
        except CdpError:
            self.drop(world.frame)
            raise

    def drop(self, frame: Frame) -> None:
        """Forget a frame's world and its descendants' (it navigated, detached, or failed)."""
        doomed = [frame]
        while doomed:
            current = doomed.pop()
            world = self._worlds.pop(current, None)
            making = self._making.pop(current, None)
            if making is not None:
                making.cancel()
            doomed.extend(f for f, w in self._worlds.items() if w.parent is not None and w.parent.frame is current)
            if world is not None and world.own_session:
                self._detach(world.cdp)

    async def close(self) -> None:
        """Drop every world and detach the frames' own sessions. Bounded; never raises."""
        if self._closed:
            return
        self._closed = True
        for event in ("framenavigated", "framedetached"):
            with contextlib.suppress(Exception):
                self._page.remove_listener(event, self.drop)
        for making in list(self._making.values()):
            making.cancel()
        sessions = [w.cdp for w in self._worlds.values() if w.own_session]
        self._worlds.clear()
        await asyncio.gather(*(s.detach() for s in sessions), *self._detaching)

    def _detach(self, cdp: Cdp) -> None:
        task = asyncio.ensure_future(cdp.detach())
        self._detaching.add(task)
        task.add_done_callback(self._detaching.discard)

    async def _make(self, frame: Frame) -> World | None:
        try:
            if frame.is_detached():
                return None
            parent_frame = frame.parent_frame
            if parent_frame is None:
                world = await self._main_world(frame)
            else:
                parent = await self.get(parent_frame)
                if parent is None or parent.depth >= MAX_DEPTH:
                    return None
                world = await self._child_world(frame, parent)
        except (CdpError, LookupError, TypeError) as e:  # failed, or an answer without what it should have
            log.debug("No isolated world for a frame: %s", type(e).__name__ if not isinstance(e, CdpError) else e)
            return None
        if self._closed or frame.is_detached() or (world.parent is not None and not self.alive(world.parent)):
            if world.own_session:
                self._detach(world.cdp)
            return None
        self._worlds[frame] = world
        return world

    async def _main_world(self, frame: Frame) -> World:
        tree = await self._cdp.send("Page.getFrameTree")
        frame_id = str(tree["frameTree"]["frame"]["id"])
        return World(frame, self._cdp, frame_id, await self._create(self._cdp, frame_id))

    async def _child_world(self, frame: Frame, parent: World) -> World:
        try:
            session = await open_session(self._context, frame)  # an out-of-process frame has its own
        except Exception:  # noqa: BLE001 - Playwright refuses for a frame in its parent's process
            cdp, own = parent.cdp, False
            frame_id = await self._frame_id_in(parent, frame)
        else:
            cdp, own, frame_id = Cdp(session), True, ""
        try:  # from here a failure, or the frame dropped meanwhile, detaches the frame's own session
            if own:
                tree = await cdp.send("Page.getFrameTree")
                frame_id = str(tree["frameTree"]["frame"]["id"])
            context = await self._create(cdp, frame_id)
            owner = await self._keep_owner(parent, frame_id)
        except BaseException:
            if own:
                self._detach(cdp)
            raise
        return World(frame, cdp, frame_id, context, parent, owner, parent.depth + 1, own)

    async def _create(self, cdp: Cdp, frame_id: str) -> int:
        made = await cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": WORLD_NAME})
        context = made.get("executionContextId")
        if not isinstance(context, int):
            raise CdpError("no isolated world")
        return context

    async def _keep_owner(self, parent: World, frame_id: str) -> int:
        """Resolve the frame's owner iframe into the parent's world and keep it there; its number."""
        cdp = parent.cdp
        await cdp.enable_dom()
        owner = await cdp.send("DOM.getFrameOwner", {"frameId": frame_id})
        node = owner.get("backendNodeId")
        if not isinstance(node, int):
            raise CdpError("no owner")
        resolved = await cdp.send("DOM.resolveNode", {"backendNodeId": node, "executionContextId": parent.context})
        object_id = resolved.get("object", {}).get("objectId")
        number, self._next_owner = self._next_owner, self._next_owner + 1
        stored = await cdp.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": STORE_OWNER,
                "objectId": object_id,
                "arguments": [{"value": number}],
                "returnByValue": True,
            },
        )
        if stored.get("result", {}).get("value") is not True:
            raise CdpError("the owner wasn't kept")
        return number

    async def _frame_id_in(self, parent: World, frame: Frame) -> str:
        """The id of a frame in its parent's process, from that session's frame tree: the parent's child with the
        frame's URL and name that no other world has."""
        tree = (await parent.cdp.send("Page.getFrameTree"))["frameTree"]
        node = _find(tree, parent.frame_id)
        taken = {w.frame_id for w in self._worlds.values() if w.cdp is parent.cdp}
        for child in (node or {}).get("childFrames", []):
            info = child.get("frame", {})
            if info.get("url") == frame.url and info.get("name", "") == frame.name and info.get("id") not in taken:
                return str(info["id"])
        raise CdpError("the frame isn't in its parent's tree")


def _find(node: Mapping[str, Any], frame_id: str) -> Mapping[str, Any] | None:
    if node.get("frame", {}).get("id") == frame_id:
        return node
    for child in node.get("childFrames", []):
        found = _find(child, frame_id)
        if found is not None:
            return found
    return None
