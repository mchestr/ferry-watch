"""JavaScript dialogs in the classic checkout viewer: shown on the pod's screen and answered there.

With nothing listening, Playwright dismisses a dialog the moment it opens: in 0.6.4 a checkout's confirm()
returned false before anyone saw it. From the first viewer of a hold on, the agent's dialog keeper
(agent/dialogs.py) leaves them open; the pod's Chromium draws them on the screen, and the viewer answers them
there with the mouse and keyboard. When the checkout is over they're dismissed again.

The same pod screen as test_checkout_screen.py: KasmVNC's Xvnc and matchbox, Chromium as the agent launches it,
the gate as the agent builds it (with the keeper and the classic viewer's screen, which makes room for a dialog on a
phone) and KasmVNC's stock client through the shim. What the test looks for is what the viewer's own canvas shows:
Chromium's dialog, with its blue OK button.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer, unused_port
from playwright.async_api import Page

from tests.browser import test_checkout_edge as edge
from tests.browser import test_checkout_screen as screen
from tests.browser import test_checkout_stream as stream
from waitlist.agent.dialogs import DialogKeeper
from waitlist.agent.gate import Gate, GateSettings
from waitlist.agent.screen import Screen
from waitlist.agent.stream import ClassicScreen

pytestmark = pytest.mark.browser

site = edge.site
browser = edge.browser
page = edge.page
pod = stream.pod  # the pod's screen, with xrandr for the classic viewer's screen
phone = screen.phone

OK_BLUE = (13, 80, 187)  # Chromium's dialog button, as the pod's screen draws it

# Where the dialog's OK button is on the viewer, in the viewer's CSS px, or null when the canvas shows none.
OK_BUTTON = """([r, g, b]) => {
  const canvas = [...document.querySelectorAll("canvas")].sort((x, y) => y.width * y.height - x.width * x.height)[0];
  if (!canvas || !canvas.width || !canvas.height) return null;
  const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
  let count = 0, left = Infinity, right = -1, top = Infinity, bottom = -1;
  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const i = (y * canvas.width + x) * 4;
      if (Math.abs(data[i] - r) <= 24 && Math.abs(data[i + 1] - g) <= 24 && Math.abs(data[i + 2] - b) <= 24) {
        count++;
        left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y);
      }
    }
  }
  if (count < 200) return null;
  const box = canvas.getBoundingClientRect();
  const [scaleX, scaleY] = [box.width / canvas.width, box.height / canvas.height];
  return [box.x + (left + right) / 2 * scaleX, box.y + (top + bottom) / 2 * scaleY];
}"""


# The dialog on the viewer's canvas, in canvas px: the box of its white (Chromium dims the page behind it), and the
# box of its OK button, the lowest blue thing (a prompt's input has a blue border above it). `tap` is the middle of
# OK on the viewer, in its CSS px. Null when the canvas shows no dialog.
DIALOG = """([r, g, b]) => {
  const canvas = [...document.querySelectorAll("canvas")].sort((x, y) => y.width * y.height - x.width * x.height)[0];
  if (!canvas || !canvas.width || !canvas.height) return null;
  const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
  const at = (x, y) => (y * canvas.width + x) * 4;
  const blue = (i) => Math.abs(data[i] - r) <= 24 && Math.abs(data[i + 1] - g) <= 24 && Math.abs(data[i + 2] - b) <= 24;
  const white = [Infinity, Infinity, -1, -1];
  let lowest = -1;
  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const i = at(x, y);
      if (data[i] >= 250 && data[i + 1] >= 250 && data[i + 2] >= 250) {
        white[0] = Math.min(white[0], x);
        white[1] = Math.min(white[1], y);
        white[2] = Math.max(white[2], x);
        white[3] = Math.max(white[3], y);
      }
      if (blue(i)) lowest = y;
    }
  }
  if (lowest < 0) return null;
  const ok = [Infinity, lowest - 12, -1, lowest];
  for (let y = ok[1]; y <= lowest; y++) {
    for (let x = 0; x < canvas.width; x++) {
      if (blue(at(x, y))) {
        ok[0] = Math.min(ok[0], x);
        ok[2] = Math.max(ok[2], x);
      }
    }
  }
  const box = canvas.getBoundingClientRect();
  const [scaleX, scaleY] = [box.width / canvas.width, box.height / canvas.height];
  const tap = [box.x + (ok[0] + ok[2]) / 2 * scaleX, box.y + (lowest - 6) * scaleY];
  return { width: canvas.width, height: canvas.height, dialog: white, ok, tap };
}"""


@pytest.fixture
async def keeper(pod: stream.Pod) -> DialogKeeper:
    return DialogKeeper(lambda: pod.context)


@pytest.fixture
async def gate(pod: stream.Pod, keeper: DialogKeeper) -> AsyncIterator[tuple[Gate, str]]:
    """The gate in front of the pod's Xvnc, as the agent builds it: with the dialog keeper and the classic viewer's
    screen. Yields it and its origin."""
    origin = f"http://127.0.0.1:{unused_port()}"
    pod_screen = Screen(pod.display, screen.SCREEN, pod.work / "xsettings", pod.env)
    gate = Gate(
        GateSettings(
            origin=origin,
            owner=edge.OWNER,
            trip_id=edge.TRIP,
            pod_name=edge.POD,
            keys={"k1": edge.KEY.public_key()},
            rfb_socket=pod.rfb_socket,
        ),
        checkout_cart=lambda: edge.CART,
        viewer_changed=lambda _: None,
        screen=ClassicScreen(pod_screen, lambda: (pod.context, pod.tab), keeper),
        dialogs=keeper,
    )
    yield gate, origin
    await gate.close("test over")
    await pod_screen.close()


@pytest.fixture
async def landing(site: edge.Site, gate: tuple[Gate, str]) -> AsyncIterator[str]:
    the_gate, origin = gate
    server = TestServer(edge.edge_app(site, the_gate, []), host="127.0.0.1", port=int(origin.rsplit(":", 1)[1]))
    await server.start_server()
    try:
        yield f"{origin}{edge.LANDING}#{edge.ticket()}"
    finally:
        await server.close()


async def ok_button(viewer: Page) -> list[float] | None:
    point: list[float] | None = await viewer.evaluate(OK_BUTTON, list(OK_BLUE))
    return point


async def shown(viewer: Page) -> list[float]:
    """Wait for the dialog on the viewer; where its OK button is. Then as long as a person would take to reach
    it: Chromium ignores a click that comes too soon after a dialog appears (its guard against stray clicks)."""
    ok: list[float] | None = await screen._until(lambda: ok_button(viewer), bool)
    assert ok is not None, "the viewer doesn't show the dialog"
    await asyncio.sleep(1)
    return ok


async def connect(viewer: Page, landing: str, pod: stream.Pod) -> None:
    await viewer.goto(landing)
    await viewer.click("#open")
    await viewer.wait_for_selector("html.noVNC_connected")
    await screen._fitted(viewer, screen.Pod(pod.rfb_socket, pod.tab))


async def shot(viewer: Page, name: str) -> None:
    """Keep what the viewer showed, when WL_TEST_SHOTS names a directory (to look at by eye)."""
    directory = os.environ.get("WL_TEST_SHOTS")
    if directory:
        await viewer.screenshot(path=Path(directory) / f"{name}.png")


async def test_without_a_viewer_a_dialog_is_dismissed_as_before(pod: stream.Pod, keeper: DialogKeeper) -> None:
    """Nobody is looking: the agent's own browsing gets Playwright's behaviour, as in 0.6.4."""
    assert await pod.tab.evaluate("confirm('Anyone?')") is False
    assert not keeper.keeping


async def test_the_classic_viewer_sees_a_confirm_on_the_screen_and_answers_it(
    pod: stream.Pod, landing: str, page: Page
) -> None:
    await connect(page, landing, pod)
    assert await ok_button(page) is None
    await pod.tab.evaluate("setTimeout(() => { window.answer = confirm('Proceed to payment?'); }, 0)")
    ok = await shown(page)
    await shot(page, "classic-confirm-shown")
    await page.mouse.click(*ok)  # the viewer's mouse, over VNC
    gone = await screen._until(lambda: ok_button(page), lambda p: p is None)
    assert gone is None, "the dialog is still on the screen"
    await shot(page, "classic-confirm-answered")
    assert await pod.tab.evaluate("window.answer") is True


async def test_a_prompt_is_typed_into_on_the_screen(pod: stream.Pod, landing: str, page: Page) -> None:
    await connect(page, landing, pod)
    await pod.tab.evaluate("setTimeout(() => { window.code = prompt('Promo code?', 'none'); }, 0)")
    await shown(page)
    await shot(page, "classic-prompt-shown")
    await page.keyboard.type("FERRY10")  # the default is selected: typing replaces it
    await page.keyboard.press("Enter")
    assert await screen._until(lambda: ok_button(page), lambda p: p is None) is None
    assert await pod.tab.evaluate("window.code") == "FERRY10"


async def test_a_dialog_waits_across_a_reconnect_and_goes_when_the_checkout_ends(
    pod: stream.Pod, landing: str, page: Page, gate: tuple[Gate, str], keeper: DialogKeeper
) -> None:
    await connect(page, landing, pod)
    await pod.tab.evaluate("setTimeout(() => { window.left = confirm('Leave checkout?'); }, 0)")
    assert await screen._until(lambda: ok_button(page), bool)
    await page.reload()  # the phone was locked, say: the device comes back by its nonce
    await page.wait_for_selector("html.noVNC_connected")
    assert await screen._until(lambda: ok_button(page), bool), "the dialog went with the viewer"
    assert len(keeper.open_dialogs) == 1
    the_gate, _ = gate
    await the_gate.close("hold ended")  # released, expired, booked: nobody will answer it now
    assert keeper.open_dialogs == []
    assert await pod.tab.evaluate("window.left") is False
    assert await pod.tab.evaluate("confirm('Anyone?')") is False  # and the agent's own browsing is as before


async def test_an_iframes_dialog_is_shown_too(pod: stream.Pod, landing: str, page: Page) -> None:
    await pod.tab.set_content("<iframe srcdoc='<p>card</p>'></iframe>")
    await connect(page, landing, pod)
    frame = pod.tab.frames[1]
    await frame.evaluate("setTimeout(() => { parent.window.fromFrame = confirm('From the card frame'); }, 0)")
    await page.mouse.click(*await shown(page))
    assert await screen._until(lambda: ok_button(page), lambda p: p is None) is None
    assert await pod.tab.evaluate("window.fromFrame") is True


@pytest.mark.parametrize(
    ("kind", "script", "answer"),
    [
        ("confirm", "window.answer = confirm('Proceed to payment?')", True),
        ("alert", "alert('Your session will expire soon'); window.answer = 'seen'", "seen"),
        ("prompt", "window.answer = prompt('Promo code?', 'none')", "none"),
    ],
)
async def test_on_a_phone_the_whole_dialog_is_on_the_screen_and_a_tap_answers_it(
    pod: stream.Pod, landing: str, phone: Page, kind: str, script: str, answer: object
) -> None:
    """Chromium centres a dialog in its window, which is never narrower than 500 px: on a phone's 390 px screen it
    ran off the right edge, an alert's only button with it. While it's open the screen is 500 px wide (KasmVNC
    scales it into the phone), and the phone's own size comes back once it's answered."""
    classic = screen.Pod(pod.rfb_socket, pod.tab)
    await phone.goto(landing)
    await phone.tap("#open")
    await phone.wait_for_selector("html.noVNC_connected")
    await screen._fitted(phone, classic)
    await pod.tab.evaluate(f"setTimeout(() => {{ {script}; }}, 0)")
    await shown(phone)

    async def dialog() -> dict[str, Any] | None:
        found: dict[str, Any] | None = await phone.evaluate(DIALOG, list(OK_BLUE))
        return found

    fits = await screen._until(dialog, lambda d: d is not None and d["dialog"][2] < d["width"] - 2)
    await shot(phone, f"classic-phone-{kind}-shown")
    assert fits is not None
    left, top, right, bottom = fits["dialog"]
    assert left >= 2, f"the dialog runs off the screen: {fits}"
    assert right < fits["width"] - 2, f"the dialog runs off the screen: {fits}"
    assert top >= 0
    assert bottom < fits["height"]
    ok_left, _, ok_right, _ = fits["ok"]
    assert ok_left > left, f"OK isn't inside the dialog: {fits}"
    assert ok_right < right, f"OK isn't inside the dialog: {fits}"
    assert ok_right - ok_left >= 40, f"only a sliver of OK shows: {fits}"
    await asyncio.sleep(1)  # Chromium ignores a click too soon after the screen changed under the dialog
    await phone.touchscreen.tap(*fits["tap"])
    assert await screen._until(lambda: ok_button(phone), lambda p: p is None) is None
    assert await pod.tab.evaluate("window.answer") == answer
    await screen._fitted(phone, classic)  # the phone's own size again, 1:1
    assert await pod.tab.evaluate("[innerWidth, innerHeight, devicePixelRatio]") == [390, 664, 1]
