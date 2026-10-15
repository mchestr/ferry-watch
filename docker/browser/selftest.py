"""Run the agent's lockdown self-test, then look at the screen, inside a running browser container.

    docker exec <container> python3 -I /smoke/selftest.py

The agent's own Chromium starts only when ferry-control sends it a trip, so the smoke test launches one
the same way the agent does (PodBrowser, with the self-test after the launch) and runs the same checks: the
policy blocks browser internals, files and view-source, an outside host doesn't load, page script can't reach
the gate or Xvnc, and the only listeners are the gate, the probes and Xvnc's unix socket.

Then it does what the gate's viewer does, on the agent's own Xvnc: it connects to the unix socket, asks for
the whole screen and checks that the tab fills it within 5 s, then clicks and types and checks the tab got
both. (Xvnc started without -DisableBasicAuth sends nothing and ignores input: a black checkout.)

Last, the screen a streamed viewer gets (agent/screen.py, with the image's xrandr and xsettingsd): a phone's
390x664 at 2x, which the tab must report, then the pod's own screen back, 1280x1000 at 1x.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from ferry_watch.agent.lockdown import Lockdown, self_test
from ferry_watch.agent.screen import Screen
from ferry_watch.bcferries.site import PodBrowser

DISPLAY = ":99"
RFB_SOCKET = Path("/tmp/kasm/rfb.sock")  # noqa: S108 - the container's memory-backed /tmp
SCREEN = (1280, 1000)  # the agent's
SCREEN_WAIT_S = 5.0
PHONE = (390, 664, 2)  # a streamed viewer's screen: CSS px and scale factor
COLOUR = (0, 170, 119)
KEY = "q"

# The tab shows one solid colour and notes every click and key.
SHOW_COLOUR = f"""() => {{
  document.body.style.cssText = "margin: 0; height: 100vh; background: rgb{COLOUR}";
  window.clicks = [];
  window.keys = [];
  addEventListener("click", (event) => window.clicks.push([event.clientX, event.clientY]));
  addEventListener("keydown", (event) => window.keys.push(event.key));
}}"""


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[int, int]:
    """RFB 3.8 with no authentication, as the KasmVNC client does through the gate. Returns the screen size."""
    version = await reader.readexactly(12)
    if not version.startswith(b"RFB "):
        raise ValueError(f"not RFB: {version!r}")
    writer.write(b"RFB 003.008\n")
    (count,) = struct.unpack(">B", await reader.readexactly(1))
    if 1 not in await reader.readexactly(count):  # 1 is None
        raise ValueError("Xvnc doesn't offer the None security type")
    writer.write(b"\x01")
    (result,) = struct.unpack(">I", await reader.readexactly(4))
    if result:
        raise ValueError("Xvnc refused the connection")
    writer.write(b"\x01")  # ClientInit: shared, as the client asks
    width, height = struct.unpack(">HH", await reader.readexactly(4))
    await reader.readexactly(16)  # the server's pixel format, replaced below
    (name_length,) = struct.unpack(">I", await reader.readexactly(4))
    await reader.readexactly(name_length)
    # 32 bits per pixel, little-endian, true colour: each pixel arrives as blue, green, red, padding.
    writer.write(struct.pack(">Bxxx", 0) + struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
    writer.write(struct.pack(">BxHi", 2, 1, 0))  # SetEncodings: Raw only
    return width, height


async def _update(reader: asyncio.StreamReader, frame: bytearray, width: int) -> None:
    """Read server messages until one framebuffer update, and paint it into `frame`."""
    while True:
        (kind,) = struct.unpack(">B", await reader.readexactly(1))
        if kind == 2:  # Bell
            continue
        if kind == 3:  # ServerCutText
            await reader.readexactly(3)
            (length,) = struct.unpack(">I", await reader.readexactly(4))
            await reader.readexactly(length)
            continue
        if kind != 0:
            raise ValueError(f"unexpected server message {kind}")
        (rects,) = struct.unpack(">xH", await reader.readexactly(3))
        for _ in range(rects):
            x, y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
            if encoding != 0:
                raise ValueError(f"unexpected encoding {encoding}")
            data = await reader.readexactly(w * h * 4)
            for row in range(h):
                start = ((y + row) * width + x) * 4
                frame[start : start + w * 4] = data[row * w * 4 : (row + 1) * w * 4]
        return


def _share(frame: bytearray, colour: tuple[int, int, int]) -> float:
    """The share of pixels that are `colour`."""
    red, green, blue = colour
    wanted = red << 16 | green << 8 | blue
    pixels = memoryview(frame).cast("I")  # little-endian, as asked for, like the image's platforms
    return sum(pixel & 0xFFFFFF == wanted for pixel in pixels) / len(pixels)


async def _until(read: Callable[[], Awaitable[Any]], seconds: float) -> Any:
    """Read until it gives something truthy, for up to `seconds`. Returns the last reading either way."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        value = await read()
        if value or loop.time() > deadline:
            return value
        await asyncio.sleep(0.1)


async def _shows_the_tab(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, width: int, height: int
) -> list[str]:
    """Ask for the whole screen, then for changes, until it's all the tab's colour, for up to SCREEN_WAIT_S."""
    frame = bytearray(width * height * 4)
    frames = 0
    try:
        async with asyncio.timeout(SCREEN_WAIT_S):
            while frames == 0 or _share(frame, COLOUR) < 0.99:
                writer.write(struct.pack(">BBHHHH", 3, int(frames > 0), 0, 0, width, height))
                await writer.drain()
                await _update(reader, frame, width)
                frames += 1
    except TimeoutError:
        if not frames:
            return [f"no frame from Xvnc within {SCREEN_WAIT_S:.0f}s"]
        return [
            f"the screen isn't the tab: {_share(frame, COLOUR):.0%} of it is the tab's colour, "
            f"{_share(frame, (0, 0, 0)):.0%} is black"
        ]
    return []


async def _takes_input(tab: Page, writer: asyncio.StreamWriter, width: int, height: int) -> list[str]:
    """Click the middle of the screen, then type a key: the tab must get both."""
    x, y = width // 2, height // 2
    for mask in (0, 1, 0):  # move there, press, release
        # KasmVNC's PointerEvent has a 16-bit button mask and a scroll delta (SMsgReader::readPointerEvent).
        writer.write(struct.pack(">BHHHhh", 5, mask, x, y, 0, 0))
    await writer.drain()
    clicks = await _until(lambda: tab.evaluate("window.clicks"), SCREEN_WAIT_S)
    if not clicks or any(abs(cx - x) > 2 or abs(cy - y) > 2 for cx, cy in clicks):
        return [f"a click at {x},{y} on the screen reached the tab at {clicks or 'no point'}"]
    for down in (1, 0):
        writer.write(struct.pack(">BBxxI", 4, down, ord(KEY)))  # a Latin-1 letter's keysym is its code
    await writer.drain()
    keys = await _until(lambda: tab.evaluate("window.keys"), SCREEN_WAIT_S)
    if keys != [KEY]:
        return [f"typing {KEY!r} on the screen reached the tab as {keys}"]
    return []


async def check_screen(tab: Page, rfb_socket: Path) -> list[str]:
    """Look at the screen and use it, as the gate's viewer would. Returns what's wrong."""
    await tab.evaluate(SHOW_COLOUR)
    try:
        reader, writer = await asyncio.open_unix_connection(rfb_socket)
    except OSError as e:
        return [f"can't connect to Xvnc: {e}"]
    try:
        width, height = await asyncio.wait_for(_handshake(reader, writer), SCREEN_WAIT_S)
        failures = await _shows_the_tab(reader, writer, width, height) or await _takes_input(tab, writer, width, height)
    except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError) as e:
        failures = [f"screen check error: {type(e).__name__}: {e}"]
    finally:
        writer.close()  # before anything else connects: Xvnc refuses a second client
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return failures


XSETTINGS_DIR = Path("/tmp/smoke-xsettings")  # noqa: S108 - not the agent's /tmp/xsettings


async def check_stream_screen(tab: Page, display: str = DISPLAY, work: Path = XSETTINGS_DIR) -> list[str]:
    """A streamed viewer's screen, then the pod's own back, as the tab sees them. Returns what's wrong."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp/home"}  # noqa: S108
    screen = Screen(display, SCREEN, work, env)
    failures: list[str] = []

    async def reported(wanted: tuple[int, int, int]) -> list[str]:
        seen = await _until(lambda: _matches(tab, wanted), SCREEN_WAIT_S)
        return [] if seen else [f"the tab doesn't report {wanted[0]}x{wanted[1]} at {wanted[2]}x"]

    try:
        if not await screen.apply(*PHONE):
            failures.append("the screen was already a phone's")
        failures += await reported(PHONE)
        await screen.restore()
        failures += await reported((*SCREEN, 1))
    except Exception as e:  # noqa: BLE001 - reported, like every other check here
        failures.append(f"stream screen error: {type(e).__name__}: {e}")
    finally:
        await screen.close()
    return failures


async def _matches(tab: Page, wanted: tuple[int, int, int]) -> bool:
    size: list[int] = await tab.evaluate("[innerWidth, innerHeight, devicePixelRatio]")
    return tuple(size) == wanted


async def main() -> int:
    lockdown = Lockdown()
    launched: list[BrowserContext] = []

    async def after_launch(context: BrowserContext) -> None:
        launched.append(context)
        await self_test(context, lockdown, proc_net=Path("/proc/net"), rfb_socket=RFB_SOCKET)

    browser = PodBrowser(
        None,  # no terminals: it never searches
        profile_dir=Path("/tmp/smoke-profile"),  # noqa: S108 - not the agent's /tmp/profile
        window_size=SCREEN,
        env={"DISPLAY": DISPLAY, "HOME": "/tmp/home", "TMPDIR": "/tmp", "TZ": "America/Vancouver"},  # noqa: S108
        after_launch=after_launch,
    )
    await browser.start()
    try:
        await browser.new_session("smoke")
        tab = launched[0].pages[0]
        screen = await check_screen(tab, RFB_SOCKET)
        stream = await check_stream_screen(tab)
    finally:
        await browser.stop()
    sys.stdout.write(f"lockdown ok={lockdown.ok} failures={lockdown.failures}\n")
    sys.stdout.write(f"screen ok={not screen} failures={screen}\n")
    sys.stdout.write(f"stream screen ok={not stream} failures={stream}\n")
    return 0 if lockdown.ok and not screen and not stream else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
