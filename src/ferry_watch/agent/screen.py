"""The pod's X screen for a streamed viewer: the viewer's size, at a Mac's scale factor (DESIGN §4.1).

A stream lays the checkout out at the viewer's CSS size and renders it at 1x or 2x. The screen gets one RandR
mode of the viewer's size times the scale factor (`xrandr`, which resizes the kiosk window through matchbox),
and at 2x `xsettingsd` serves `Xft/DPI` 192, which Chromium follows live (devicePixelRatio 2, no relaunch).

- `apply()` does nothing when the screen already has that size and scale factor, so a phone reconnecting
  changes nothing the page can see. Otherwise it sets the DPI first and then the mode, without waiting in
  between, which moves the page's width monotonically from the old one to the new.
- `restore()` puts back the pod's own screen (its launch size at 1x, xsettingsd stopped, our modes deleted). The
  gate calls it only before it bridges a classic (KasmVNC) viewer, which therefore finds the pod exactly as
  0.6.4 left it. It's bounded and never raises.

Every argument is built from validated integers, and nothing runs through a shell. xsettingsd opens no
listener (it is an X client), and if it dies the page falls back to 1x.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

DPI_PER_SCALE: Final = 96 * 1024  # Xft/DPI is in 1024ths of a dot per inch
XRANDR_TIMEOUT_SECONDS: Final = 2.0
RESTORE_SECONDS: Final = 3.0
XSETTINGSD_STOP_SECONDS: Final = 1.0
MAX_MODES: Final = 16  # of ours on the X server at once
MAX_PIXELS: Final = (3840, 3200)  # 1920x1600 CSS px at 2x, the most a hello can ask for
CONFIG_NAME: Final = "xsettingsd.conf"


class ScreenError(Exception):
    """The screen couldn't be changed (the stream session closes with 4500)."""


class Screen:
    def __init__(self, display: str, default: tuple[int, int], work: Path, env: Mapping[str, str]) -> None:
        """
        `default` is the pod's own screen size (AgentSettings.screen); `work` the private directory for
        xsettingsd's config (AgentSettings.xsettings_dir); `env` the pod processes' environment.
        """
        self._default = default
        self._work = work
        self._env = {**env, "DISPLAY": display}
        self._output: str | None = None
        self._modes: dict[str, bool] = {}  # our modes on the X server: name -> added to the output
        self._xsettingsd: asyncio.subprocess.Process | None = None
        self._dsf = 1  # what Chromium is told
        self._touched = False  # anything to restore
        self.current: tuple[int, int, int] | None = None  # (w, h, dsf) a stream applied; None: the pod's own

    async def apply(self, w: int, h: int, dsf: int) -> bool:
        """Show the page at `w`x`h` CSS px and scale factor `dsf`. False when that's already the screen."""
        if self.current == (w, h, dsf):
            return False
        width, height = w * dsf, h * dsf
        if dsf not in (1, 2) or not (0 < width <= MAX_PIXELS[0] and 0 < height <= MAX_PIXELS[1]):
            raise ValueError(f"no screen of {w}x{h} at {dsf}x")
        self.current = None  # until it's all done: a failure leaves it to be applied again
        self._touched = True
        output = await self._output_name()
        name = f"fw{width}x{height}"
        if name not in self._modes:
            if len(self._modes) >= MAX_MODES:
                raise ScreenError(f"already {MAX_MODES} modes")
            await self._xrandr("--newmode", name, "60", *[str(width)] * 4, *[str(height)] * 4)
            self._modes[name] = False
        if not self._modes[name]:
            await self._xrandr("--addmode", output, name)
            self._modes[name] = True
        await self._set_scale(dsf)  # DPI first, then the mode, with no wait in between
        await self._xrandr("--output", output, "--mode", name)
        self.current = (w, h, dsf)
        return True

    @property
    def changed(self) -> bool:
        """A stream has changed the screen since it was last restored."""
        return self._touched

    async def restore(self) -> None:
        """The pod's own screen: its launch size at 1x, xsettingsd stopped, our modes gone. Never raises."""
        if not self._touched:
            return
        try:
            async with asyncio.timeout(RESTORE_SECONDS):
                output = await self._output_name()
                width, height = self._default
                await self._set_scale(1)  # as apply(): DPI, then the mode, with no wait in between
                await self._xrandr("--output", output, "--mode", f"{width}x{height}")
                for name, added in list(self._modes.items()):
                    if added:
                        await self._xrandr("--delmode", output, name)
                        self._modes[name] = False
                    await self._xrandr("--rmmode", name)
                    del self._modes[name]
        except Exception as e:  # noqa: BLE001 - a classic viewer is waiting: it gets the screen as it is
            log.warning("Couldn't restore the pod's screen: %s", type(e).__name__)
        finally:
            await self._stop_xsettingsd(grace=0)  # only if the budget ran out first
            self._dsf = 1
            self.current = None
            self._touched = bool(self._modes)  # modes left behind are tried again next time

    async def close(self) -> None:
        """Stop xsettingsd (the agent is exiting)."""
        await self._stop_xsettingsd()

    # ---- the tools

    async def _output_name(self) -> str:
        if self._output is None:
            for line in (await self._xrandr("--query")).splitlines():
                if " connected" in line:
                    self._output = line.split()[0]
                    break
            else:
                raise ScreenError("no connected RandR output")
        return self._output

    async def _xrandr(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "xrandr",
            *args,
            env=self._env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(XRANDR_TIMEOUT_SECONDS):
                out, _ = await process.communicate()
        except TimeoutError:
            raise ScreenError(f"xrandr {args[0]} timed out") from None
        finally:
            if process.returncode is None:  # timed out or cancelled
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if process.returncode != 0:
            raise ScreenError(f"xrandr {args[0]} failed ({process.returncode})")
        return out.decode(errors="replace")

    async def _set_scale(self, dsf: int) -> None:
        """Tell Chromium the scale factor through Xft/DPI. Returns once xsettingsd has it, not once Chromium has."""
        running = self._xsettingsd is not None and self._xsettingsd.returncode is None
        if dsf == self._dsf and (dsf == 1 or running):
            return
        self._dsf = dsf
        if dsf == 1:
            await self._stop_xsettingsd()
            return
        config = self._write_config(dsf)
        if running and self._xsettingsd is not None:
            self._xsettingsd.send_signal(signal.SIGHUP)  # it reads its config again
            return
        self._xsettingsd = await asyncio.create_subprocess_exec(
            "xsettingsd",
            "-c",
            str(config),
            env=self._env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # tini's TERM is the agent's alone
        )

    def _write_config(self, dsf: int) -> Path:
        self._work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._work.chmod(0o700)
        config = self._work / CONFIG_NAME
        temporary = config.with_name(f".{CONFIG_NAME}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as f:
            f.write(f"Xft/DPI {DPI_PER_SCALE * dsf}\n")
        temporary.replace(config)
        return config

    async def _stop_xsettingsd(self, grace: float = XSETTINGSD_STOP_SECONDS) -> None:
        process, self._xsettingsd = self._xsettingsd, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM if grace else signal.SIGKILL)
        try:
            async with asyncio.timeout(grace or XSETTINGSD_STOP_SECONDS):
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
