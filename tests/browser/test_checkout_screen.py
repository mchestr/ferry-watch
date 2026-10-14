"""The checkout's screen end to end: a viewer sees the pod's Chromium and can use it.

The pod's own processes (KasmVNC's Xvnc with the pod's flags, and matchbox), Chromium launched as the agent
launches it, the VNC gate, and KasmVNC's stock client through the shim, served as test_checkout_edge.py serves
it. That file's stand-in RFB server can't say what the real Xvnc lets a client see or do: without
-DisableBasicAuth the client connected and showed a black screen that ignored every tap.

Needs FW_KASMVNC_WWW (as test_checkout_edge.py does), and Xvnc from the KasmVNC package and
matchbox-window-manager on PATH (CI installs both, as the browser image does; see .github/workflows/ci.yml).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer, unused_port
from playwright.async_api import BrowserContext, Page

from ferry_watch.agent.gate import Gate, GateSettings
from ferry_watch.agent.processes import pod_processes
from ferry_watch.bcferries.site import PodBrowser
from tests.browser import test_checkout_edge as edge

pytestmark = pytest.mark.browser

# The same fixtures: KasmVNC's client built into vnc-edge's site, and the viewer's Chromium and page.
site = edge.site
browser = edge.browser
page = edge.page

SCREEN = (1280, 1000)  # the pod's (AgentSettings.screen)
COLOUR = (0, 170, 119)
WAIT_S = 10.0

# The checkout tab: one solid colour, and a note of every click and key.
SHOW_COLOUR = f"""() => {{
  document.body.style.cssText = "margin: 0; height: 100vh; background: rgb{COLOUR}";
  window.clicks = [];
  window.keys = [];
  addEventListener("click", (event) => window.clicks.push([event.clientX, event.clientY]));
  addEventListener("keydown", (event) => window.keys.push(event.key));
}}"""

# What the viewer's canvas shows: its size, where it is on the page, and the share of its pixels that are the
# checkout's colour, over the whole screen and over its top 10 rows.
CANVAS = """([r, g, b]) => {
  const canvas = [...document.querySelectorAll("canvas")].sort((x, y) => y.width * y.height - x.width * x.height)[0];
  if (!canvas || !canvas.width || !canvas.height) return null;
  const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
  const share = (rows) => {
    let matching = 0;
    for (let i = 0; i < rows * canvas.width * 4; i += 4) {
      const close = Math.abs(data[i] - r) <= 8 && Math.abs(data[i + 1] - g) <= 8 && Math.abs(data[i + 2] - b) <= 8;
      if (close && data[i + 3] === 255) matching++;
    }
    return matching / (rows * canvas.width);
  };
  const box = canvas.getBoundingClientRect();
  return {
    width: canvas.width, height: canvas.height, box: [box.x, box.y, box.width, box.height],
    shown: share(canvas.height), top: share(Math.min(10, canvas.height)),
  };
}"""


@dataclass
class Pod:
    rfb_socket: Path
    tab: Page  # the checkout tab, all a viewer should see


def _free_display() -> str:
    for number in range(90, 200):
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return f":{number}"
    raise RuntimeError("no free X display")


@pytest.fixture
async def pod() -> AsyncIterator[Pod]:
    """A browser pod's screen, as agent/main.py's run_pod starts it, showing the checkout tab."""
    if not (shutil.which("Xvnc") and shutil.which("matchbox-window-manager")):
        pytest.skip("Xvnc (KasmVNC's) and matchbox-window-manager aren't on PATH")
    # Short, since Chromium's singleton socket goes under TMPDIR and a unix socket's path is limited.
    work = Path(tempfile.mkdtemp(prefix="fw-"))
    (work / "home").mkdir()
    x11 = Path("/tmp/.X11-unix")
    if not x11.exists():
        x11.mkdir()
        x11.chmod(0o1777)
    display = _free_display()
    env = {"DISPLAY": display, "HOME": str(work / "home"), "TMPDIR": str(work), "TZ": "America/Vancouver"}
    rfb_socket = work / "kasm" / "rfb.sock"
    processes = pod_processes(display, SCREEN, rfb_socket, {**env, "PATH": os.environ["PATH"]})
    launched: list[BrowserContext] = []

    async def keep(context: BrowserContext) -> None:
        launched.append(context)

    chromium = PodBrowser(
        None,  # type: ignore[arg-type]  # no terminals: it never searches
        profile_dir=work / "profile",
        window_size=SCREEN,
        env=env,
        after_launch=keep,
    )
    try:
        await processes.start()
        await chromium.start()
        await chromium.new_session("checkout")
        tab = launched[0].pages[0]
        await tab.evaluate(SHOW_COLOUR)
        yield Pod(rfb_socket, tab)
    finally:
        await chromium.stop()
        await processes.stop()
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture
async def landing(site: edge.Site, pod: Pod) -> AsyncIterator[str]:
    """vnc-edge, with the gate in front of the pod's Xvnc. Yields the landing page's URL, with a ticket."""
    port = unused_port()
    origin = f"http://127.0.0.1:{port}"
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
    )
    server = TestServer(edge.edge_app(site, gate, []), host="127.0.0.1", port=port)
    await server.start_server()
    try:
        yield f"{origin}{edge.LANDING}#{edge.ticket()}"
    finally:
        await gate.close("test over")
        await server.close()


async def _until(read: Callable[[], Awaitable[Any]], done: Callable[[Any], bool]) -> Any:
    """Read until `done` says yes, for up to WAIT_S. Returns the last reading either way."""
    deadline = asyncio.get_running_loop().time() + WAIT_S
    while True:
        value = await read()
        if done(value) or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.2)


async def test_the_viewer_sees_the_checkout_and_can_use_it(pod: Pod, landing: str, page: Page) -> None:
    await page.goto(landing)
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")

    async def canvas() -> dict[str, Any] | None:
        result: dict[str, Any] | None = await page.evaluate(CANVAS, list(COLOUR))
        return result

    # Frames arrive, and the screen is the checkout and nothing else.
    shown = await _until(canvas, lambda c: c is not None and c["shown"] > 0.99)
    assert shown is not None, "the viewer has no screen"
    assert shown["shown"] > 0.5, f"the viewer doesn't show the checkout: {shown['shown']:.0%} of it is the page"
    assert shown["top"] > 0.99, "something covers the top of the checkout"
    assert shown["shown"] > 0.99, f"something covers part of the checkout: {shown['shown']:.0%} of it is the page"

    # The screen follows the viewer's size: the client resizes it, and the tab with it.
    async def sizes() -> tuple[list[int], list[int]]:
        c = await canvas()
        assert c is not None
        tab: list[int] = await pod.tab.evaluate("[innerWidth, innerHeight]")
        return [c["width"], c["height"]], tab

    viewer, tab = await _until(sizes, lambda s: s[0] == s[1])
    assert viewer == tab, f"the viewer shows {viewer}, the checkout tab is {tab}"
    assert tab != list(SCREEN), "the screen wasn't resized to fit the viewer"

    # A click on the viewer lands on the tab, at the same spot, and then typing reaches it.
    final = await canvas()
    assert final is not None
    x, y, width, height = final["box"]
    await page.mouse.click(x + width / 2, y + height / 2)
    clicks: list[list[int]] = await _until(lambda: pod.tab.evaluate("window.clicks"), bool)
    assert len(clicks) == 1, "the click didn't reach the checkout"
    ((clicked_x, clicked_y),) = clicks
    assert abs(clicked_x - final["width"] / 2) <= 2
    assert abs(clicked_y - final["height"] / 2) <= 2
    await page.keyboard.type("4502")
    keys: list[str] = await _until(lambda: pod.tab.evaluate("window.keys"), lambda k: len(k) >= 4)
    assert keys == ["4", "5", "0", "2"], f"typing 4502 on the viewer reached the checkout as {keys}"


async def test_the_viewer_cant_close_or_leave_the_checkout(pod: Pod, landing: str, page: Page) -> None:
    """matchbox's own key bindings are off: Alt+C would close the window, Alt+Tab and Alt+N would switch to
    another, Alt+D would show the desktop and Ctrl+Alt+X would start a terminal. (Chromium's own Ctrl+W and
    Alt+F4 are taken by the viewer's own browser or desktop before the page sees them.)"""
    await page.goto(landing)
    await page.click("#open")
    await page.wait_for_selector("html.noVNC_connected")
    shown = await _until(lambda: page.evaluate(CANVAS, list(COLOUR)), lambda c: c is not None and c["shown"] > 0.99)
    assert shown is not None
    x, y, width, height = shown["box"]
    await page.mouse.click(x + width / 2, y + height / 2)  # the viewer takes keys once it's been tapped
    for keys in ("Alt+c", "Alt+n", "Alt+p", "Alt+Tab", "Alt+Shift+Tab", "Alt+d", "Control+Alt+x", "Alt+Escape"):
        await page.keyboard.press(keys)
    await page.keyboard.type("1")  # after them: once this reaches the tab, they all have too
    typed: list[str] = await _until(lambda: pod.tab.evaluate("window.keys"), lambda k: "1" in k)
    assert "1" in typed, "the checkout tab stopped getting keys"
    await asyncio.sleep(1)  # anything a binding started has had time to show
    assert not pod.tab.is_closed(), "a key closed the checkout"
    shown = await page.evaluate(CANVAS, list(COLOUR))
    assert shown is not None, "the viewer has no screen any more"
    assert shown["shown"] > 0.99, "the viewer no longer shows only the checkout"
