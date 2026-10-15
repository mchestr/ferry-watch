"""The pod's X screen for a streamed viewer: the viewer's size, at a Mac's scale factor (DESIGN §4.1).

A stream lays the checkout out at the viewer's CSS size and renders it at 1x or 2x. The screen gets one RandR
mode of the viewer's size times the scale factor (`xrandr`, which resizes the kiosk window through matchbox),
and at 2x `xsettingsd` serves `Xft/DPI` 192, which Chromium follows live (devicePixelRatio 2, no relaunch).

- `apply()` does nothing when the screen already has that size and scale factor, so a phone reconnecting
  changes nothing the page can see. Otherwise it sets the DPI first and then the mode, without waiting in
  between, which moves the page's width monotonically from the old one to the new. At most MAX_MODES of our
  modes are on the X server at once; a new size replaces the one used least recently.
- `restore()` puts back the pod's own screen (its launch size at 1x, xsettingsd stopped, our modes deleted but
  the one it shows, if Xvnc no longer has its own of that size). The gate calls it only before it bridges a
  classic (KasmVNC) viewer, which therefore finds the pod exactly as 0.6.4 left it. It's bounded and never raises.
- `widen()` and `narrow()` are for a classic viewer narrower than Chromium's minimum window (a phone): while a
  JavaScript dialog is open the screen is made that wide, so the whole dialog is on it, and then put back to the
  viewer's own mode (DESIGN §13). Both are bounded and never raise.

Every argument is built from validated integers, and nothing runs through a shell. xsettingsd opens no
listener (it is an X client), and if it dies the page falls back to 1x.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

DPI_PER_SCALE: Final = 96 * 1024  # Xft/DPI is in 1024ths of a dot per inch
XRANDR_TIMEOUT_SECONDS: Final = 2.0
RESTORE_SECONDS: Final = 3.0
FIT_SECONDS: Final = 3.0  # for widen() or narrow() in all
DIALOG_WIDTH: Final = 500  # Chromium's minimum window width, in DIP: it centres a JavaScript dialog in it
XSETTINGSD_STOP_SECONDS: Final = 1.0
MAX_MODES: Final = 16  # of ours on the X server at once: a new one replaces the least recently used
MAX_PIXELS: Final = (3840, 3200)  # 1920x1600 CSS px at 2x, the most a hello can ask for
CONFIG_NAME: Final = "xsettingsd.conf"


_SIZE: Final = re.compile(r"\bcurrent (\d+) x (\d+)")
_OUTPUT_MODE: Final = re.compile(r" {3}(\S+)(.*)")  # "   390x664     60.00*+": a mode on the output (* in use)


@dataclass(frozen=True, slots=True)
class _Shown:
    """What `xrandr --query` says: the output, the screen's size, the output's modes and the one in use."""

    output: str
    size: tuple[int, int]
    modes: frozenset[str]
    active: str | None


class ScreenError(Exception):
    """The screen couldn't be changed: a session that can't start closes with 4500, a resize is answered `error
    failed`."""


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
        self._modes: dict[str, bool] = {}  # ours on the X server, least recently used first: name -> on the output
        self._showing: str | None = None  # ours that we last switched the output to
        self._xsettingsd: asyncio.subprocess.Process | None = None
        self._dsf = 1  # what Chromium is told
        self._touched = False  # anything to restore
        self._wide: str | None = None  # our mode widen() switched to
        self._narrow: str | None = None  # the classic viewer's mode it switched from
        self._busy = asyncio.Lock()  # one change at a time
        self.current: tuple[int, int, int] | None = None  # (w, h, dsf) a stream applied; None: the pod's own

    async def apply(self, w: int, h: int, dsf: int) -> bool:
        """Show the page at `w`x`h` CSS px and scale factor `dsf`. False when that's already the screen."""
        if self.current == (w, h, dsf):
            return False
        width, height = w * dsf, h * dsf
        if dsf not in (1, 2) or not (0 < width <= MAX_PIXELS[0] and 0 < height <= MAX_PIXELS[1]):
            raise ValueError(f"no screen of {w}x{h} at {dsf}x")
        async with self._busy:
            self.current = None  # until it's all done: a failure leaves it to be applied again
            self._touched = True
            output = await self._output_name()
            name = await self._ours(output, width, height)
            await self._set_scale(dsf)  # DPI first, then the mode, with no wait in between
            await self._show(output, name)
            self.current = (w, h, dsf)
        return True

    @property
    def changed(self) -> bool:
        """A stream has changed the screen since it was last restored."""
        return self._touched

    async def restore(self) -> None:
        """The pod's own screen: its launch size at 1x, xsettingsd stopped, our other modes gone. Never raises."""
        if not self._touched and self._wide is None:
            return
        restored = False
        try:
            async with asyncio.timeout(RESTORE_SECONDS), self._busy:
                try:
                    output, name = await self._mode_for(*self._default)
                    await self._set_scale(1)  # as apply(): DPI, then the mode, with no wait in between
                    await self._show(output, name)
                except ScreenError:
                    await self._tidy()  # our other modes go all the same
                    raise
                await self._tidy()
                restored = True
        except Exception as e:  # noqa: BLE001 - a classic viewer is waiting: it gets the screen as it is
            log.warning("Couldn't restore the pod's screen: %s", type(e).__name__)
        finally:
            await self._stop_xsettingsd(grace=0)  # only if the budget ran out first
            self._dsf = 1
            self.current = None
            self._touched = not restored  # tried again next time
            self._wide = self._narrow = None

    async def widen(self) -> None:
        """Room for a JavaScript dialog on a classic viewer's screen: when it's narrower than DIALOG_WIDTH, the screen
        at that width and its height, until `narrow()` (KasmVNC scales it into the viewer). A dialog is centred in
        Chromium's window, which is never narrower, so on a phone it ran off the screen, an alert's only button with
        it. Called again while widened, it checks the classic viewer hasn't resized the screen since (a phone
        turned): that viewer's new mode is then the one to go back to. Bounded; never raises."""
        try:
            async with asyncio.timeout(FIT_SECONDS), self._busy:
                shown = await self._query()
                if self._wide is not None:
                    if shown.active == self._wide:
                        return
                    await self._unwiden(shown)  # the viewer resized the screen since: its new mode counts
                width, height = shown.size
                if width >= DIALOG_WIDTH or shown.active is None:
                    return
                self._narrow = shown.active
                self._wide = await self._ours(shown.output, DIALOG_WIDTH, height)
                await self._show(shown.output, self._wide)
        except Exception as e:  # noqa: BLE001 - the dialog is there, if only partly on the screen
            log.warning("Couldn't make room for the checkout's dialog: %s", type(e).__name__)

    async def narrow(self) -> None:
        """Back to the classic viewer's own mode after `widen()`, unless that viewer has resized the screen since.
        Bounded; never raises."""
        try:
            async with asyncio.timeout(FIT_SECONDS), self._busy:
                if self._wide is not None:  # else nothing runs
                    await self._unwiden(await self._query())
        except Exception as e:  # noqa: BLE001 - left for the next classic viewer's restore
            log.warning("Couldn't put the classic viewer's screen back: %s", type(e).__name__)

    async def _unwiden(self, shown: _Shown) -> None:
        wide, narrow = self._wide, self._narrow
        if shown.active == wide and narrow is not None:
            await self._show(shown.output, narrow)
        elif self._showing == wide:
            self._showing = None  # the classic viewer's own mode is on the screen since
        if wide is not None and wide in self._modes:
            await self._remove(shown.output, wide)
        self._wide = self._narrow = None

    async def close(self) -> None:
        """Stop xsettingsd (the agent is exiting)."""
        await self._stop_xsettingsd()

    # ---- modes

    async def _mode_for(self, width: int, height: int) -> tuple[str, str]:
        """The output, and a mode of this size for it: the X server's own of that name if it has one, else ours.
        Xvnc makes one of the pod's size at launch, but replaces it with one of a classic viewer's size as soon as
        that viewer resizes the screen."""
        shown = await self._query()
        name = f"{width}x{height}"
        if name not in shown.modes:
            name = await self._ours(shown.output, width, height)
        return shown.output, name

    async def _show(self, output: str, name: str) -> None:
        await self._xrandr("--output", output, "--mode", name)
        self._showing = name if name in self._modes else None

    async def _tidy(self) -> None:
        """Remove our modes but the one the output shows."""
        output = await self._output_name()
        for name in [n for n in self._modes if n != self._showing]:
            await self._remove(output, name)

    async def _ours(self, output: str, width: int, height: int) -> str:
        """Our mode of this size, on the output and now the most recently used. Making a new one first removes the
        least recently used of ours while there are MAX_MODES: a pod's life sees any number of viewer sizes (window
        resizes, zoom, rotation, reconnects from other devices)."""
        name = f"fw{width}x{height}"
        if name in self._modes:
            self._modes[name] = self._modes.pop(name)
        else:
            while len(self._modes) >= MAX_MODES:
                await self._remove(output, next(n for n in self._modes if n != self._showing))
            await self._xrandr("--newmode", name, "60", *[str(width)] * 4, *[str(height)] * 4)
            self._modes[name] = False
        if not self._modes[name]:
            await self._xrandr("--addmode", output, name)
            self._modes[name] = True
        return name

    async def _remove(self, output: str, name: str) -> None:
        """One of our modes off the output and off the X server (it mustn't be the one shown)."""
        if self._modes[name]:
            await self._xrandr("--delmode", output, name)
            self._modes[name] = False
        await self._xrandr("--rmmode", name)
        del self._modes[name]
        if self._showing == name:
            self._showing = None

    # ---- the tools

    async def _output_name(self) -> str:
        if self._output is None:
            await self._query()
        assert self._output is not None  # noqa: S101 - _query found it
        return self._output

    async def _query(self) -> _Shown:
        """The screen as the X server has it now."""
        lines = (await self._xrandr("--query")).splitlines()
        size = next((m for line in lines if (m := _SIZE.search(line))), None)
        at = next((i for i, line in enumerate(lines) if " connected" in line), None)
        if size is None or at is None:
            raise ScreenError("no connected RandR output")
        self._output = lines[at].split()[0]
        modes: set[str] = set()
        active: str | None = None
        for line in lines[at + 1 :]:
            if not line.startswith(" "):
                break  # the next output's
            if (mode := _OUTPUT_MODE.match(line)) is not None:
                modes.add(mode[1])
                if "*" in mode[2]:
                    active = mode[1]
        return _Shown(self._output, (int(size[1]), int(size[2])), frozenset(modes), active)

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
