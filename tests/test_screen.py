"""The streamed viewer's screen (agent/screen.py) against stand-in xrandr and xsettingsd on PATH.

Each stand-in writes a line to a log for every run: xrandr its arguments and the DPI config as it was at that
moment (so the order of DPI and mode shows), xsettingsd its start and the signals it gets.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from ferry_watch.agent.screen import MAX_MODES, Screen, ScreenError
from ferry_watch.agent.settings import AgentSettings

XRANDR = """#!/bin/sh
log="$FW_TEST_DIR/log"
echo "xrandr $* | dpi=$(cat "$FW_TEST_DIR/xsettings/xsettingsd.conf" 2>/dev/null)" >> "$log"
if [ -e "$FW_TEST_DIR/hang" ]; then echo "hung $$" >> "$log"; exec sleep 60; fi
if [ -e "$FW_TEST_DIR/fail" ]; then exit 1; fi
if [ "$1" = "--query" ]; then
  echo "Screen 0: minimum 32 x 32, current 1280 x 1000, maximum 32768 x 32768"
  echo "VNC-0 connected primary 1280x1000+0+0 0mm x 0mm"
  echo "   1280x1000     60.00*"
fi
"""

XSETTINGSD = """#!/bin/sh
log="$FW_TEST_DIR/log"
echo "xsettingsd $* | dpi=$(cat "$2")" >> "$log"
trap 'echo "xsettingsd hup | dpi=$(cat "$2")" >> "$log"' HUP
trap 'echo "xsettingsd term" >> "$log"; exit 0' TERM
while :; do sleep 0.05; done
"""


@dataclass
class Tools:
    root: Path
    screen: Screen

    @property
    def work(self) -> Path:
        return self.root / "xsettings"

    def lines(self) -> list[str]:
        log = self.root / "log"
        return log.read_text().splitlines() if log.exists() else []

    def calls(self) -> list[str]:
        """The xrandr arguments and xsettingsd events, in order, without the DPI column."""
        return [line.split(" | ")[0] for line in self.lines() if not line.startswith("hung")]

    def forget(self) -> None:
        (self.root / "log").unlink(missing_ok=True)


@pytest.fixture
async def tools(tmp_path: Path) -> AsyncIterator[Tools]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("xrandr", XRANDR), ("xsettingsd", XSETTINGSD)):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FW_TEST_DIR": str(tmp_path)}
    screen = Screen(":99", (1280, 1000), tmp_path / "xsettings", env)
    yield Tools(tmp_path, screen)
    await screen.close()


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110 - the stand-ins write their log on their own
            await asyncio.sleep(0.01)


def test_xsettingsd_keeps_its_config_in_the_pods_memory_backed_tmp() -> None:
    settings = AgentSettings(
        trip_id="t", pod_name="fw-t-g1", gen=1, owner="u", deadline_at=0, hub_url="ws://x", vnc_origin="https://x",
        vnc_keys={},
    )  # fmt: skip
    assert settings.xsettings_dir == Path("/tmp/xsettings")


async def test_a_phone_gets_its_size_at_2x_dpi_first(tools: Tools) -> None:
    assert await tools.screen.apply(390, 664, 2)
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    calls = tools.calls()
    xrandr = [c for c in calls if c.startswith("xrandr")]
    assert xrandr == [
        "xrandr --query",
        "xrandr --newmode fw780x1328 60 780 780 780 780 1328 1328 1328 1328",
        "xrandr --addmode VNC-0 fw780x1328",
        "xrandr --output VNC-0 --mode fw780x1328",
    ]
    output = next(line for line in tools.lines() if "--output" in line)
    assert output.endswith("dpi=Xft/DPI 196608")  # the DPI was there before the mode changed
    config = tools.work / "xsettingsd.conf"
    assert config.read_text() == "Xft/DPI 196608\n"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(tools.work.stat().st_mode) == 0o700
    assert tools.screen.current == (390, 664, 2)


async def test_the_same_size_again_changes_nothing(tools: Tools) -> None:
    await tools.screen.apply(390, 664, 2)
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    tools.forget()
    assert not await tools.screen.apply(390, 664, 2)
    await asyncio.sleep(0.2)
    assert tools.calls() == []


async def test_another_size_at_the_same_scale_is_one_mode_change(tools: Tools) -> None:
    await tools.screen.apply(390, 664, 2)
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    tools.forget()
    assert await tools.screen.apply(664, 390, 2)  # rotated
    assert tools.calls() == [
        "xrandr --newmode fw1328x780 60 1328 1328 1328 1328 780 780 780 780",
        "xrandr --addmode VNC-0 fw1328x780",
        "xrandr --output VNC-0 --mode fw1328x780",
    ]
    tools.forget()
    assert await tools.screen.apply(390, 664, 2)  # and back: the mode is there already
    assert tools.calls() == ["xrandr --output VNC-0 --mode fw780x1328"]


async def test_a_desktop_at_1x_stops_xsettingsd_before_the_mode(tools: Tools) -> None:
    await tools.screen.apply(390, 664, 2)
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    tools.forget()
    assert await tools.screen.apply(1280, 800, 1)
    assert tools.calls() == [
        "xrandr --newmode fw1280x800 60 1280 1280 1280 1280 800 800 800 800",
        "xrandr --addmode VNC-0 fw1280x800",
        "xsettingsd term",
        "xrandr --output VNC-0 --mode fw1280x800",
    ]
    tools.forget()
    assert await tools.screen.apply(390, 664, 2)  # a phone again: xsettingsd starts again
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    assert [c for c in tools.calls() if c.startswith("xrandr")] == ["xrandr --output VNC-0 --mode fw780x1328"]


async def test_restore_puts_back_the_pods_own_screen(tools: Tools) -> None:
    await tools.screen.restore()  # nothing to restore: nothing runs
    assert tools.calls() == []
    await tools.screen.apply(390, 664, 2)
    await tools.screen.apply(1280, 800, 1)
    await tools.screen.apply(390, 664, 2)
    await until(lambda: sum(line.startswith("xsettingsd -c") for line in tools.lines()) == 2)
    tools.forget()
    started = time.monotonic()
    await tools.screen.restore()
    assert time.monotonic() - started < 3
    calls = tools.calls()
    assert calls == [
        "xsettingsd term",
        "xrandr --output VNC-0 --mode 1280x1000",
        "xrandr --delmode VNC-0 fw780x1328",
        "xrandr --rmmode fw780x1328",
        "xrandr --delmode VNC-0 fw1280x800",
        "xrandr --rmmode fw1280x800",
    ]
    assert tools.screen.current is None
    tools.forget()
    await tools.screen.restore()  # done: nothing more to do
    assert tools.calls() == []
    tools.forget()
    assert await tools.screen.apply(390, 664, 2)  # the next stream starts from the pod's own screen again
    assert "xrandr --newmode fw780x1328 60 780 780 780 780 1328 1328 1328 1328" in tools.calls()


async def test_restore_is_bounded_and_never_raises(tools: Tools) -> None:
    await tools.screen.apply(390, 664, 2)
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    (tools.root / "hang").touch()
    started = time.monotonic()
    await tools.screen.restore()
    assert time.monotonic() - started < 3.5
    hung = [int(line.split()[1]) for line in tools.lines() if line.startswith("hung")]
    assert hung
    for pid in hung:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # killed, not left behind
    assert "xsettingsd term" in tools.calls()  # back to 1x whatever xrandr did
    assert tools.screen.current is None
    (tools.root / "hang").unlink()
    tools.forget()
    await tools.screen.restore()  # the mode left behind is removed next time
    assert tools.calls() == [
        "xrandr --output VNC-0 --mode 1280x1000",
        "xrandr --delmode VNC-0 fw780x1328",
        "xrandr --rmmode fw780x1328",
    ]


async def test_a_failed_apply_raises_and_is_tried_again(tools: Tools) -> None:
    (tools.root / "fail").touch()
    with pytest.raises(ScreenError):
        await tools.screen.apply(390, 664, 2)
    assert tools.screen.current is None
    (tools.root / "fail").unlink()
    tools.forget()
    assert await tools.screen.apply(390, 664, 2)
    assert "xrandr --output VNC-0 --mode fw780x1328" in tools.calls()


async def test_at_most_16_modes(tools: Tools) -> None:
    for i in range(MAX_MODES):
        await tools.screen.apply(400 + i, 700, 1)
    with pytest.raises(ScreenError):
        await tools.screen.apply(500, 700, 1)
    await tools.screen.restore()
    assert await tools.screen.apply(500, 700, 1)  # a restore clears them


@pytest.mark.parametrize(("w", "h", "dsf"), [(390, 664, 3), (0, 664, 1), (1921, 1601, 2), (-390, 664, 2)])
async def test_only_sizes_a_hello_can_ask_for(tools: Tools, w: int, h: int, dsf: int) -> None:
    with pytest.raises(ValueError, match="no screen"):
        await tools.screen.apply(w, h, dsf)
    assert tools.calls() == []
