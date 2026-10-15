"""The stream's way into Chromium: the fixed scripts (cast_scripts.py), the CDP allowlist and the frames' isolated
worlds (cast_cdp.py), against stand-in CDP sessions (tests/castfakes.py)."""

from __future__ import annotations

import asyncio
import math
from typing import Any, cast

import pytest

from ferry_watch.agent.cast_cdp import ALLOWED, EVALUATE_TIMEOUT_MS, WORLD_NAME, Cdp, CdpError, NotAllowed, Worlds
from ferry_watch.agent.cast_scripts import FIELDS, SCRIPTS, STORE_OWNER, VIEWPORT, call, script_of
from tests.castfakes import FakeCdp, FakeContext, FakeFrame, FakePage

# ---- the fixed scripts


def test_a_script_is_applied_to_numbers_only() -> None:
    assert call(FIELDS, 3, 1, 2).endswith(")(3,1,2)")
    assert call(VIEWPORT).endswith(")()")
    expression = call(SCRIPTS["OWNER_HIT"], 4, [1.5, -2.0, 0.1, 1e16])
    assert expression.endswith(")(4,[1.5,-2.0,0.1,1e+16])")
    for bad in ("1", True, None, float("nan"), math.inf, [1, "2"], {"x": 1}, 2**60):
        with pytest.raises((TypeError, ValueError)):
            call(FIELDS, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fixed"):
        call("function () { return document.cookie; }")
    with pytest.raises(ValueError, match="2048"):
        call(SCRIPTS["OWNER_HIT"], 1, [0.0] * 2049)


def test_only_a_fixed_scripts_call_is_recognised() -> None:
    for name, script in SCRIPTS.items():
        assert script_of(call(script, 1)) == name
    good = call(FIELDS, 1, 2, 3)
    assert script_of(good + ";fetch('https://evil.example')") is None
    assert script_of(good.replace("(1,2,3)", "(alert(1))")) is None
    assert script_of(good.replace("(1,2,3)", "(`${x}`)")) is None
    assert script_of("(() => document.cookie)()") is None
    assert script_of(f"({STORE_OWNER})(1)") is None  # run only with callFunctionOn, on an owner it resolved


# ---- the allowlist (C34)


class Session:
    """A Playwright CDPSession that answers everything, noting what it was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.hang = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append((method, dict(params or {})))
        if self.hang:
            await asyncio.Event().wait()
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 9}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "owner-1"}}
        return {"result": {"value": 1}}

    def on(self, event: str, handler: Any) -> None: ...

    def remove_listener(self, event: str, handler: Any) -> None: ...

    async def detach(self) -> None: ...


@pytest.fixture
def session() -> Session:
    return Session()


@pytest.fixture
def cdp(session: Session) -> Cdp:
    return Cdp(cast(Any, session), seconds=0.2)


REFUSED: list[tuple[str, dict[str, Any]]] = [
    ("Page.navigate", {"url": "https://evil.example"}),
    ("Page.enable", {}),
    ("Page.reload", {}),
    ("Runtime.compileScript", {"expression": "1", "sourceURL": "", "persistScript": False}),
    ("Network.getCookies", {}),
    ("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 664, "deviceScaleFactor": 3, "mobile": True}),
    ("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": []}),
    ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1, "button": "right", "clickCount": 1}),
    ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1, "button": "middle", "clickCount": 1}),
    ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1, "button": "left", "clickCount": 2}),
    ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1, "modifiers": 2}),
    ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": math.nan, "y": 1}),
    ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1, "deltaY": 4}),
    ("Input.dispatchMouseEvent", {"type": "mouseDragged", "x": 1, "y": 1}),
    (
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "t", "code": "KeyT", "windowsVirtualKeyCode": 84, "modifiers": 4},
    ),
    (
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "t", "code": "KeyT", "windowsVirtualKeyCode": 84, "modifiers": 1},
    ),
    ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "F12", "code": "F12", "windowsVirtualKeyCode": 123}),
    ("Input.dispatchKeyEvent", {"type": "char", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65}),
    (
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "text": "ab"},
    ),
    ("Input.insertText", {"text": "a\u0000b"}),
    ("Input.insertText", {"text": "x" * 257}),
    ("Input.imeSetComposition", {"text": "a\nb", "selectionStart": 3, "selectionEnd": 3}),
    ("Input.synthesizePinchGesture", {"x": 1, "y": 1, "scaleFactor": 2, "gestureSourceType": "touch"}),
    ("Input.synthesizePinchGesture", {"x": 1, "y": 1, "scaleFactor": 9, "gestureSourceType": "mouse"}),
    ("Page.startScreencast", {"format": "png", "quality": 50, "everyNthFrame": 1}),
    ("Page.captureScreenshot", {"format": "jpeg", "quality": 50, "captureBeyondViewport": True}),
    ("Page.createIsolatedWorld", {"frameId": "f", "worldName": WORLD_NAME, "grantUniveralAccess": True}),
    ("Page.createIsolatedWorld", {"frameId": "f", "worldName": "page"}),
    ("Target.getTargetInfo", {"targetId": "another"}),
    ("Target.attachToTarget", {"targetId": "another", "flatten": True}),
    ("DOM.getDocument", {}),
    ("DOM.resolveNode", {"backendNodeId": 1}),
]


@pytest.mark.parametrize(("method", "params"), REFUSED, ids=[f"{m} {i}" for i, (m, _) in enumerate(REFUSED)])
async def test_anything_off_the_allowlist_is_refused_before_it_is_sent(
    cdp: Cdp, session: Session, method: str, params: dict[str, Any]
) -> None:
    with pytest.raises(NotAllowed):
        await cdp.send(method, params)
    assert session.sent == []


async def test_scripts_run_only_in_worlds_it_made_with_the_timeout_and_by_value(cdp: Cdp, session: Session) -> None:
    good = {"expression": call(VIEWPORT), "contextId": 9, "returnByValue": True, "timeout": EVALUATE_TIMEOUT_MS}
    with pytest.raises(NotAllowed):
        await cdp.send("Runtime.evaluate", good)  # no such world yet: the page's own world, say
    made = await cdp.send("Page.createIsolatedWorld", {"frameId": "main", "worldName": WORLD_NAME})
    assert made["executionContextId"] == 9
    assert await cdp.evaluate(9, VIEWPORT) == 1
    assert session.sent[-1] == ("Runtime.evaluate", good)
    for changed in (
        {"timeout": 2000},
        {"returnByValue": False},
        {"contextId": 1},
        {"expression": "document.cookie"},
        {"expression": call(VIEWPORT) + ";1"},
        {"awaitPromise": True},
    ):
        with pytest.raises(NotAllowed):
            await cdp.send("Runtime.evaluate", {**good, **changed})
    without_timeout = dict(good)
    del without_timeout["timeout"]
    with pytest.raises(NotAllowed):
        await cdp.send("Runtime.evaluate", without_timeout)


async def test_call_function_on_only_stores_an_owner_it_resolved(cdp: Cdp, session: Session) -> None:
    await cdp.send("Page.createIsolatedWorld", {"frameId": "main", "worldName": WORLD_NAME})
    store = {
        "functionDeclaration": STORE_OWNER,
        "objectId": "owner-1",
        "arguments": [{"value": 1}],
        "returnByValue": True,
    }
    with pytest.raises(NotAllowed):
        await cdp.send("Runtime.callFunctionOn", store)  # not resolved by it
    await cdp.send("DOM.resolveNode", {"backendNodeId": 7, "executionContextId": 9})
    await cdp.send("Runtime.callFunctionOn", store)
    for changed in (
        {"functionDeclaration": "function () { return document.cookie; }"},
        {"arguments": [{"value": "1"}]},
        {"arguments": [{"value": 1}, {"value": 2}]},
        {"executionContextId": 9},
    ):
        with pytest.raises(NotAllowed):
            await cdp.send("Runtime.callFunctionOn", {**store, **changed})


async def test_every_call_is_bounded_and_its_error_says_nothing_sent(cdp: Cdp, session: Session) -> None:
    session.hang = True
    with pytest.raises(CdpError, match=r"^Input\.insertText had no answer in time$"):
        await cdp.send("Input.insertText", {"text": "4242 4242 4242 4242"})


def test_the_allowlist_is_c34s() -> None:
    assert set(ALLOWED) == {
        "Page.startScreencast",
        "Page.stopScreencast",
        "Page.screencastFrameAck",
        "Page.captureScreenshot",
        "Page.getLayoutMetrics",
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Input.imeSetComposition",
        "Input.synthesizePinchGesture",
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "DOM.enable",
        "DOM.getFrameOwner",
        "DOM.resolveNode",
        "Target.getTargetInfo",
    }


# ---- isolated worlds


def page_with_frames() -> tuple[FakeContext, FakePage, FakeFrame, FakeFrame]:
    page = FakePage()
    card = FakeFrame("https://pay.example/card", parent_frame=page.main_frame, out_of_process=True)
    banner = FakeFrame("about:srcdoc", name="banner", parent_frame=page.main_frame)
    page.main_frame.child_frames = [card, banner]
    return FakeContext(page), page, card, banner


async def test_a_world_in_each_frame_with_its_owner_kept_in_the_parents_world() -> None:
    context, page, card, banner = page_with_frames()
    tree = {
        "frameTree": {
            "frame": {"id": "main", "url": page.url},
            "childFrames": [{"frame": {"id": "srcdoc-1", "url": "about:srcdoc", "name": "banner"}}],
        }
    }
    own = FakeCdp(page)
    original = page.answer

    async def answer(cdp: FakeCdp, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Page.getFrameTree" and cdp is own:
            return tree
        return await original(cdp, method, params)

    page.answer = answer  # type: ignore[method-assign]
    worlds = Worlds(cast(Any, context), cast(Any, page), Cdp(cast(Any, own)))
    main = await worlds.get(cast(Any, page.main_frame))
    child = await worlds.get(cast(Any, card))
    inner = await worlds.get(cast(Any, banner))
    assert main is not None
    assert child is not None
    assert inner is not None
    assert child.own_session  # an out-of-process frame: its own session
    assert child.parent is main
    assert child.owner is not None
    assert not inner.own_session  # in its parent's process: reached through the parent's
    assert inner.cdp is main.cdp
    assert inner.frame_id == "srcdoc-1"
    assert child.owner != inner.owner
    methods = [m for m, _ in page.calls]
    assert methods.count("DOM.enable") == 1
    assert methods.count("Runtime.callFunctionOn") == 2  # each owner, kept once
    assert await worlds.get(cast(Any, card)) is child  # kept, not made again
    assert [m for m, _ in page.calls] == methods

    # A frame that navigates or detaches loses its world (and its own session goes).
    page.emit("framenavigated", card)
    assert worlds.known(cast(Any, card)) is None
    await asyncio.sleep(0)
    assert context.sessions[0].detached
    page.emit("framenavigated", page.main_frame)  # the main frame: every world goes
    assert worlds.known(cast(Any, banner)) is None
    await worlds.close()


async def test_frames_too_deep_get_no_world() -> None:
    page = FakePage()
    context = FakeContext(page)
    parent = page.main_frame
    chain = []
    for depth in range(5):
        frame = FakeFrame(f"https://pay{depth}.example/", parent_frame=parent, out_of_process=True)
        parent.child_frames = [frame]
        chain.append(frame)
        parent = frame
    worlds = Worlds(cast(Any, context), cast(Any, page), Cdp(cast(Any, FakeCdp(page))))
    found = [await worlds.get(cast(Any, f)) for f in chain]
    assert [w is not None for w in found] == [True, True, True, False, False]
    await worlds.close()


async def test_a_failing_call_drops_the_world_and_the_next_one_is_made_afresh() -> None:
    page = FakePage()
    context = FakeContext(page)
    session = FakeCdp(page)
    worlds = Worlds(cast(Any, context), cast(Any, page), Cdp(cast(Any, session), seconds=0.1))
    main = await worlds.get(cast(Any, page.main_frame))
    assert main is not None
    page.hang = {"Runtime.evaluate"}  # a dialog opened, say
    with pytest.raises(CdpError):
        await worlds.evaluate(main, VIEWPORT)
    assert worlds.known(cast(Any, page.main_frame)) is None
    page.hang = set()
    again = await worlds.get(cast(Any, page.main_frame))
    assert again is not None
    assert again.context != main.context
    await worlds.close()
