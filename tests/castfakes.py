"""Stand-ins for Chromium and Playwright, as a stream session sees them (CDP sessions, frames, pages, a context).

The page answers the CDP calls the stream makes from a little state of its own (its viewport, its scroll, what
its fixed scripts find), records every call, and can be made to hang on a method, as a page with an open
JavaScript dialog does.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ferry_watch.agent.cast_scripts import script_of


class Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, event: str, f: Callable[..., Any]) -> None:
        self.handlers.setdefault(event, []).append(f)

    def remove_listener(self, event: str, f: Callable[..., Any]) -> None:
        self.handlers[event].remove(f)

    def emit(self, event: str, *args: Any) -> None:
        for f in list(self.handlers.get(event, [])):
            f(*args)


def jpeg(width: int, height: int, tag: int = 0) -> bytes:
    """A stand-in JPEG: just a start-of-frame header with its size, and `tag` to make images differ."""
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return b"\xff\xd8" + sof + bytes(10) + tag.to_bytes(4, "big") + b"\xff\xd9"


class FakeCdp(Emitter):
    """A Playwright CDPSession onto one page (or one of its frames)."""

    def __init__(self, page: FakePage, frame_id: str = "main") -> None:
        super().__init__()
        self.page = page
        self.frame_id = frame_id
        self.detached = False
        self.hang: set[str] = set()  # methods that never answer

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        sent = dict(params or {})
        self.page.calls.append((method, sent))
        if method in self.hang or method in self.page.hang:
            await asyncio.Event().wait()
        if self.detached:
            raise RuntimeError("Target closed")
        return await self.page.answer(self, method, sent)

    async def detach(self) -> None:
        if self.detached:
            raise RuntimeError("already detached")
        self.detached = True


@dataclass(eq=False)
class FakeFrame:
    url: str = "https://shop.example/checkout"
    name: str = ""
    parent_frame: FakeFrame | None = None
    child_frames: list[FakeFrame] = field(default_factory=list)
    out_of_process: bool = False
    detached: bool = False
    page: FakePage | None = None

    def is_detached(self) -> bool:
        return self.detached


_contexts = itertools.count(100)


class FakePage(Emitter):
    """A page: its viewport and scroll, and what each fixed script finds in it."""

    def __init__(self, url: str = "https://shop.example/checkout", title: str = "Checkout") -> None:
        super().__init__()
        self.url = url
        self.title = title
        self.main_frame = FakeFrame(url, page=self)
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.hang: set[str] = set()
        self.w, self.h, self.dpr = 1280, 1000, 1
        self.page_x = self.page_y = 0.0
        self.scale = 1.0
        self.shot = 0  # changes the screenshot's bytes
        self.scripts: dict[str, Any] = {}  # a fixed script's name -> its value (or a callable of its arguments)
        self.input_hold: asyncio.Event | None = None  # Input calls wait for it, if set

    @property
    def frames(self) -> list[FakeFrame]:
        found, todo = [], [self.main_frame]
        while todo:
            frame = todo.pop(0)
            found.append(frame)
            todo += frame.child_frames
        return found

    def is_closed(self) -> bool:
        return self.closed

    async def bring_to_front(self) -> None:
        self.calls.append(("bring_to_front", {}))

    async def close(self, run_before_unload: bool = False) -> None:
        self.calls.append(("close", {"run_before_unload": run_before_unload}))
        self.closed = True
        self.emit("close", self)

    def sent(self, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if c[0].startswith(prefix)]

    async def answer(self, cdp: FakeCdp, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method.startswith("Input.") and self.input_hold is not None:
            await self.input_hold.wait()
        viewport = {
            "pageX": self.page_x,
            "pageY": self.page_y,
            "scale": self.scale,
            "offsetX": 0.0,
            "offsetY": 0.0,
            "clientWidth": self.w,
            "clientHeight": self.h,
        }
        answers: dict[str, dict[str, Any]] = {
            "Page.getFrameTree": {"frameTree": {"frame": {"id": cdp.frame_id, "url": self.url}}},
            "Page.createIsolatedWorld": {"executionContextId": next(_contexts)},
            "Page.getLayoutMetrics": {"cssVisualViewport": viewport},
            "Page.captureScreenshot": {
                "data": base64.b64encode(jpeg(self.w * self.dpr, self.h * self.dpr, self.shot)).decode()
            },
            "Target.getTargetInfo": {"targetInfo": {"title": self.title}},
            "DOM.getFrameOwner": {"backendNodeId": 7},
            "DOM.resolveNode": {"object": {"objectId": f"owner-{params.get('backendNodeId')}"}},
            "Runtime.callFunctionOn": {"result": {"value": True}},
        }
        if method == "Runtime.evaluate":
            return {"result": {"value": self._script(script_of(params["expression"]) or "", params["expression"])}}
        return answers.get(method, {})

    def _script(self, name: str, expression: str) -> Any:
        if name in self.scripts:
            value = self.scripts[name]
            return value(expression) if callable(value) else value
        if name == "VIEWPORT":
            return [self.w, self.h, self.dpr, self.page_x, self.page_y, 0, 0, self.scale, "en-US"]
        if name == "FIELDS":
            return {
                "f": [],
                "trunc": False,
                "slow": False,
                "clip": [0, 0, self.w, self.h],
                "vv": [self.page_x, self.page_y, 0, 0, self.scale],
                "cw": self.w,
                "ch": 5000,
                "own": [],
            }
        return None


class FakeContext(Emitter):
    def __init__(self, *pages: FakePage) -> None:
        super().__init__()
        self.pages = list(pages)
        self.sessions: list[FakeCdp] = []
        self.opening: asyncio.Event | None = None  # new sessions wait for it, if set (and are made all the same)

    async def new_cdp_session(self, target: FakePage | FakeFrame) -> FakeCdp:
        if self.opening is not None:
            await self.opening.wait()
        if isinstance(target, FakeFrame):
            if not target.out_of_process:
                raise RuntimeError("This frame does not have a separate CDP session")
            page = next(p for p in self.pages if target in p.frames)
            session = FakeCdp(page, frame_id=f"oopif-{id(target)}")
        else:
            session = FakeCdp(target)
        self.sessions.append(session)
        return session

    def open(self, page: FakePage) -> None:
        """A pop-up or new tab."""
        self.pages.append(page)
        self.emit("page", page)
