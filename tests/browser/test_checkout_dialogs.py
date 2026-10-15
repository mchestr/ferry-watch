"""JavaScript dialogs in the classic checkout viewer: shown on the pod's screen and answered there.

With nothing listening, Playwright dismisses a dialog the moment it opens: in 0.6.4 a checkout's confirm()
returned false before anyone saw it. From the first viewer of a hold on, the agent's dialog keeper
(agent/dialogs.py) leaves them open; the pod's Chromium draws them on the screen, and the viewer answers them
there with the mouse and keyboard. When the checkout is over they're dismissed again.

The same pod screen as test_checkout_screen.py: KasmVNC's Xvnc and matchbox, Chromium as the agent launches it,
the gate (with the keeper) and KasmVNC's stock client through the shim. What the test looks for is what the
viewer's own canvas shows: Chromium's dialog, with its blue OK button.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer, unused_port
from playwright.async_api import Page

from ferry_watch.agent.dialogs import DialogKeeper
from ferry_watch.agent.gate import Gate, GateSettings
from tests.browser import test_checkout_edge as edge
from tests.browser import test_checkout_screen as screen

pytestmark = pytest.mark.browser

site = edge.site
browser = edge.browser
page = edge.page
pod = screen.pod
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


@pytest.fixture
async def keeper(pod: screen.Pod) -> DialogKeeper:
    return DialogKeeper(lambda: pod.tab.context)


@pytest.fixture
async def gate(pod: screen.Pod, keeper: DialogKeeper) -> AsyncIterator[tuple[Gate, str]]:
    """The gate in front of the pod's Xvnc, as the agent builds it: with the dialog keeper. Yields it and its
    origin."""
    origin = f"http://127.0.0.1:{unused_port()}"
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
        dialogs=keeper,
    )
    yield gate, origin
    await gate.close("test over")


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


async def connect(viewer: Page, landing: str, pod: screen.Pod) -> None:
    await viewer.goto(landing)
    await viewer.click("#open")
    await viewer.wait_for_selector("html.noVNC_connected")
    await screen._fitted(viewer, pod)


async def shot(viewer: Page, name: str) -> None:
    """Keep what the viewer showed, when FW_TEST_SHOTS names a directory (to look at by eye)."""
    directory = os.environ.get("FW_TEST_SHOTS")
    if directory:
        await viewer.screenshot(path=Path(directory) / f"{name}.png")


async def test_without_a_viewer_a_dialog_is_dismissed_as_before(pod: screen.Pod, keeper: DialogKeeper) -> None:
    """Nobody is looking: the agent's own browsing gets Playwright's behaviour, as in 0.6.4."""
    assert await pod.tab.evaluate("confirm('Anyone?')") is False
    assert not keeper.keeping


async def test_the_classic_viewer_sees_a_confirm_on_the_screen_and_answers_it(
    pod: screen.Pod, landing: str, page: Page
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


async def test_a_prompt_is_typed_into_on_the_screen(pod: screen.Pod, landing: str, page: Page) -> None:
    await connect(page, landing, pod)
    await pod.tab.evaluate("setTimeout(() => { window.code = prompt('Promo code?', 'none'); }, 0)")
    await shown(page)
    await shot(page, "classic-prompt-shown")
    await page.keyboard.type("FERRY10")  # the default is selected: typing replaces it
    await page.keyboard.press("Enter")
    assert await screen._until(lambda: ok_button(page), lambda p: p is None) is None
    assert await pod.tab.evaluate("window.code") == "FERRY10"


async def test_a_dialog_waits_across_a_reconnect_and_goes_when_the_checkout_ends(
    pod: screen.Pod, landing: str, page: Page, gate: tuple[Gate, str], keeper: DialogKeeper
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


async def test_an_iframes_dialog_is_shown_too(pod: screen.Pod, landing: str, page: Page) -> None:
    await pod.tab.set_content("<iframe srcdoc='<p>card</p>'></iframe>")
    await connect(page, landing, pod)
    frame = pod.tab.frames[1]
    await frame.evaluate("setTimeout(() => { parent.window.fromFrame = confirm('From the card frame'); }, 0)")
    await page.mouse.click(*await shown(page))
    assert await screen._until(lambda: ok_button(page), lambda p: p is None) is None
    assert await pod.tab.evaluate("window.fromFrame") is True


async def test_on_a_phone_the_dialog_fits_and_a_tap_answers_it(pod: screen.Pod, landing: str, phone: Page) -> None:
    await phone.goto(landing)
    await phone.tap("#open")
    await phone.wait_for_selector("html.noVNC_connected")
    await screen._fitted(phone, pod)
    await pod.tab.evaluate("setTimeout(() => { window.answer = confirm('Proceed to payment?'); }, 0)")
    ok = await shown(phone)
    await shot(phone, "classic-phone-confirm-shown")
    width, height = screen.PHONE["viewport"]["width"], screen.PHONE["viewport"]["height"]
    assert 0 < ok[0] < width
    assert 0 < ok[1] < height
    await phone.touchscreen.tap(*ok)
    assert await screen._until(lambda: ok_button(phone), lambda p: p is None) is None
    assert await pod.tab.evaluate("window.answer") is True
