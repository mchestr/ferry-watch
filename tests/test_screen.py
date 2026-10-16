"""The streamed viewer's screen (agent/screen.py) against stand-in xrandr and xsettingsd on PATH.

Each stand-in writes a line to a log for every run: xrandr its arguments and the DPI config as it was at that
moment (so the order of DPI and mode shows), xsettingsd its start and the signals it gets. xrandr also keeps the
X server's modes and refuses what Xvnc refuses (measured on KasmVNC 1.5.0's).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ferry_watch.agent.screen import MAX_MODES, Screen, ScreenError
from ferry_watch.agent.settings import AgentSettings

# The X server as Xvnc starts it: its own mode of the pod's size, on its one output and in use.
LAUNCH: dict[str, Any] = {"modes": {"1280x1000": [1280, 1000]}, "output": ["1280x1000"], "current": "1280x1000"}

# xrandr as Xvnc answers it, for what the screen does: the modes on the X server and on its one output, the one in
# use, and the refusals that matter (a name taken, a mode in use, one that isn't there). State in xrandr.json.
XRANDR = f"""#!{sys.executable} -I
import json, os, sys, time
from pathlib import Path

root = Path(os.environ["FW_TEST_DIR"])
config = root / "xsettings" / "xsettingsd.conf"
dpi = config.read_text().strip() if config.exists() else ""
args = sys.argv[1:]
with open(root / "log", "a") as log:
    log.write(f"xrandr {{' '.join(args)}} | dpi={{dpi}}\\n")
if (root / "hang").exists():
    with open(root / "log", "a") as log:
        log.write(f"hung {{os.getpid()}}\\n")
    time.sleep(60)
if (root / "fail").exists() or (root / f"fail{{args[0]}}").exists():
    sys.exit(1)
path = root / "xrandr.json"
state = json.loads(path.read_text()) if path.exists() else {LAUNCH!r}
modes, output = state["modes"], state["output"]


def refuse(why):
    sys.exit(f"X Error of failed request: {{why}}")


if args == ["--query"]:
    w, h = modes[state["current"]]
    print(f"Screen 0: minimum 32 x 32, current {{w}} x {{h}}, maximum 32768 x 32768")
    print(f"VNC-0 connected primary {{w}}x{{h}}+0+0 0mm x 0mm")
    for name in output:
        print(f"   {{name}}       60.00{{'*' if name == state['current'] else ' '}}")
    for name in [n for n in modes if n not in output]:
        print(f"  {{name}} (0x237) 60.000MHz")
        print(f"        h: width  {{modes[name][0]}} start  {{modes[name][0]}}")
        print(f"        v: height {{modes[name][1]}} start  {{modes[name][1]}}")
elif args[0] == "--newmode":
    if args[1] in modes:
        refuse("BadName")
    modes[args[1]] = [int(args[3]), int(args[7])]
elif args[0] == "--addmode":
    if args[2] not in modes:
        refuse("cannot find mode")
    if args[2] not in output:
        output.append(args[2])
elif args[0] == "--delmode":
    if args[2] not in output or args[2] == state["current"]:
        refuse("BadMatch")
    output.remove(args[2])
elif args[0] == "--rmmode":
    if args[1] not in modes or args[1] in output:
        refuse("BadAccess")
    del modes[args[1]]
elif args[0] == "--output":
    if args[3] not in output:
        refuse("cannot find mode")
    state["current"] = args[3]
path.write_text(json.dumps(state))
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

    def x_server(self) -> dict[str, Any]:
        """The stand-in's modes, the output's and the one in use."""
        path = self.root / "xrandr.json"
        state: dict[str, Any] = json.loads(path.read_text()) if path.exists() else LAUNCH
        return state

    def ours(self) -> list[str]:
        return [name for name in self.x_server()["modes"] if name.startswith("fw")]

    def kasmvnc(self, width: int, height: int) -> None:
        """A classic viewer resizes the screen to its own size, as Xvnc does it (measured): a mode named for the
        size, in use, and the one Xvnc made before (at launch, or for the last such resize) deleted."""
        state = self.x_server()
        name, old = f"{width}x{height}", state.get("preferred", "1280x1000")
        state["modes"][name] = [width, height]
        state["output"].append(name)
        state["current"] = name
        if old != name and old in state["modes"]:
            state["output"].remove(old)
            del state["modes"][old]
        state["preferred"] = name
        (self.root / "xrandr.json").write_text(json.dumps(state))


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


def xsettingsd_running(work: Path) -> bool:
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if str(work).encode() in (entry / "cmdline").read_bytes():
                    return True
    return False


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
    changed = [tools.screen.changed]  # since the screen was last restored
    await tools.screen.apply(390, 664, 2)
    changed.append(tools.screen.changed)
    await tools.screen.apply(1280, 800, 1)
    await tools.screen.apply(390, 664, 2)
    await until(lambda: sum(line.startswith("xsettingsd -c") for line in tools.lines()) == 2)
    tools.forget()
    started = time.monotonic()
    await tools.screen.restore()
    assert time.monotonic() - started < 3
    calls = tools.calls()
    assert calls == [
        "xrandr --query",  # Xvnc's own mode of the pod's size is still there
        "xsettingsd term",
        "xrandr --output VNC-0 --mode 1280x1000",
        "xrandr --delmode VNC-0 fw1280x800",  # least recently used first
        "xrandr --rmmode fw1280x800",
        "xrandr --delmode VNC-0 fw780x1328",
        "xrandr --rmmode fw780x1328",
    ]
    assert tools.screen.current is None
    changed.append(tools.screen.changed)
    assert changed == [False, True, False]
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
    assert not xsettingsd_running(tools.work)  # back to 1x whatever xrandr did
    assert tools.screen.current is None
    (tools.root / "hang").unlink()
    tools.forget()
    await tools.screen.restore()  # the mode left behind is removed next time
    assert tools.calls() == [
        "xrandr --query",
        "xrandr --output VNC-0 --mode 1280x1000",
        "xrandr --delmode VNC-0 fw780x1328",
        "xrandr --rmmode fw780x1328",
    ]


async def test_restore_makes_the_pods_own_mode_once_a_classic_viewer_has_had_its_own(
    tools: Tools, caplog: pytest.LogCaptureFixture
) -> None:
    """Classic, then a stream, then classic again: Xvnc deleted its 1280x1000 mode when the first classic viewer
    resized the screen, so the restore makes one of its own, and the next classic viewer finds nothing to undo."""
    tools.kasmvnc(390, 664)
    await tools.screen.apply(390, 664, 2)
    await until(lambda: any(line.startswith("xsettingsd -c") for line in tools.lines()))
    tools.forget()
    await tools.screen.restore()
    assert tools.calls() == [
        "xrandr --query",
        "xrandr --newmode fw1280x1000 60 1280 1280 1280 1280 1000 1000 1000 1000",
        "xrandr --addmode VNC-0 fw1280x1000",
        "xsettingsd term",
        "xrandr --output VNC-0 --mode fw1280x1000",
        "xrandr --delmode VNC-0 fw780x1328",
        "xrandr --rmmode fw780x1328",
    ]
    assert tools.x_server()["current"] == "fw1280x1000"
    assert tools.ours() == ["fw1280x1000"]
    assert not tools.screen.changed
    assert "Couldn't restore" not in caplog.text

    tools.kasmvnc(390, 664)  # the classic viewer's own size
    tools.forget()
    await tools.screen.restore()  # its reconnect: nothing to undo, nothing the page sees
    assert tools.calls() == []
    await tools.screen.apply(390, 664, 2)  # a stream, then classic again: the mode made before is used
    tools.forget()
    await tools.screen.restore()
    assert tools.calls() == [
        "xrandr --query",
        "xsettingsd term",
        "xrandr --output VNC-0 --mode fw1280x1000",
        "xrandr --delmode VNC-0 fw780x1328",
        "xrandr --rmmode fw780x1328",
    ]
    assert not tools.screen.changed


async def test_a_restore_that_cant_switch_still_removes_our_other_modes(
    tools: Tools, caplog: pytest.LogCaptureFixture
) -> None:
    await tools.screen.apply(1280, 800, 1)
    await tools.screen.apply(390, 664, 2)
    (tools.root / "fail--output").touch()  # the switch to the pod's own mode is refused
    tools.forget()
    await tools.screen.restore()
    assert tools.calls()[-3:] == [
        "xrandr --output VNC-0 --mode 1280x1000",
        "xrandr --delmode VNC-0 fw1280x800",
        "xrandr --rmmode fw1280x800",
    ]
    assert tools.ours() == ["fw780x1328"]  # the one on the screen stays
    assert "Couldn't restore the pod's screen: ScreenError" in caplog.text
    assert tools.screen.changed  # to be tried again


WIDEN = [
    "xrandr --query",
    "xrandr --newmode fw500x664 60 500 500 500 500 664 664 664 664",
    "xrandr --addmode VNC-0 fw500x664",
    "xrandr --output VNC-0 --mode fw500x664",
]


async def test_a_dialog_gets_room_on_a_phones_classic_screen_and_gives_it_back(tools: Tools) -> None:
    tools.kasmvnc(390, 664)  # a classic viewer on a phone: narrower than Chromium's window
    tools.forget()
    await tools.screen.widen()
    assert tools.calls() == WIDEN
    tools.forget()
    await tools.screen.widen()  # looked at again while the dialog is open: still widened
    assert tools.calls() == ["xrandr --query"]
    tools.forget()
    await tools.screen.narrow()
    assert tools.calls() == [
        "xrandr --query",
        "xrandr --output VNC-0 --mode 390x664",  # KasmVNC's own, 1:1 on the phone again
        "xrandr --delmode VNC-0 fw500x664",
        "xrandr --rmmode fw500x664",
    ]
    assert tools.x_server()["current"] == "390x664"
    assert tools.ours() == []
    assert not tools.screen.changed
    tools.forget()
    await tools.screen.narrow()  # nothing widened
    await tools.screen.restore()  # and nothing for the next classic viewer to undo
    assert tools.calls() == []


async def test_a_computers_classic_screen_needs_no_room(tools: Tools) -> None:
    tools.kasmvnc(1280, 800)
    tools.forget()
    await tools.screen.widen()
    await tools.screen.narrow()
    assert tools.calls() == ["xrandr --query"]


async def test_a_phone_turned_while_widened_keeps_its_new_size(tools: Tools) -> None:
    tools.kasmvnc(390, 664)
    await tools.screen.widen()
    tools.kasmvnc(664, 390)  # KasmVNC resized the screen: wide enough for the dialog now
    tools.forget()
    await tools.screen.widen()
    assert tools.calls() == ["xrandr --query", "xrandr --delmode VNC-0 fw500x664", "xrandr --rmmode fw500x664"]
    tools.kasmvnc(390, 664)  # and back
    tools.forget()
    await tools.screen.widen()
    assert tools.calls() == WIDEN
    tools.kasmvnc(414, 700)  # another turn of the phone, then the dialog goes
    tools.forget()
    await tools.screen.narrow()
    assert tools.calls() == ["xrandr --query", "xrandr --delmode VNC-0 fw500x664", "xrandr --rmmode fw500x664"]
    assert tools.x_server()["current"] == "414x700"


async def test_room_not_given_back_is_left_for_the_restore(tools: Tools, caplog: pytest.LogCaptureFixture) -> None:
    tools.kasmvnc(390, 664)
    await tools.screen.widen()
    (tools.root / "fail--output").touch()
    await tools.screen.narrow()
    assert "Couldn't put the classic viewer's screen back: ScreenError" in caplog.text
    (tools.root / "fail--output").unlink()
    tools.forget()
    await tools.screen.restore()  # before the next classic viewer
    assert tools.calls() == [
        "xrandr --query",
        "xrandr --newmode fw1280x1000 60 1280 1280 1280 1280 1000 1000 1000 1000",
        "xrandr --addmode VNC-0 fw1280x1000",
        "xrandr --output VNC-0 --mode fw1280x1000",
        "xrandr --delmode VNC-0 fw500x664",
        "xrandr --rmmode fw500x664",
    ]
    tools.forget()
    await tools.screen.narrow()
    assert tools.calls() == []


async def test_a_failed_apply_raises_and_is_tried_again(tools: Tools) -> None:
    (tools.root / "fail").touch()
    with pytest.raises(ScreenError):
        await tools.screen.apply(390, 664, 2)
    assert tools.screen.current is None
    (tools.root / "fail").unlink()
    tools.forget()
    assert await tools.screen.apply(390, 664, 2)
    assert "xrandr --output VNC-0 --mode fw780x1328" in tools.calls()


async def test_any_number_of_sizes_with_at_most_16_modes_the_oldest_making_room(tools: Tools) -> None:
    """A pod's life can see any number of viewer sizes (a window resized again and again, zoom, rotation, other
    devices): each new one replaces the mode used least recently, never the one on the screen."""
    for i in range(MAX_MODES):
        await tools.screen.apply(400 + i, 700, 1)
    assert len(tools.ours()) == MAX_MODES
    tools.forget()
    assert await tools.screen.apply(400, 700, 1)  # the first size again: used now, so not the one to go
    assert tools.calls() == ["xrandr --output VNC-0 --mode fw400x700"]
    tools.forget()
    assert await tools.screen.apply(500, 700, 1)
    assert tools.calls() == [
        "xrandr --delmode VNC-0 fw401x700",
        "xrandr --rmmode fw401x700",
        "xrandr --newmode fw500x700 60 500 500 500 500 700 700 700 700",
        "xrandr --addmode VNC-0 fw500x700",
        "xrandr --output VNC-0 --mode fw500x700",
    ]
    for i in range(24):  # and on and on, at 1x and 2x
        assert await tools.screen.apply(600 + i, 700 + i % 3, 1 + i % 2)
        assert tools.screen.current == (600 + i, 700 + i % 3, 1 + i % 2)
        assert len(tools.ours()) <= MAX_MODES
        assert tools.x_server()["current"] == f"fw{(600 + i) * (1 + i % 2)}x{(700 + i % 3) * (1 + i % 2)}"
    await tools.screen.restore()
    assert tools.ours() == []


@pytest.mark.parametrize(("w", "h", "dsf"), [(390, 664, 3), (0, 664, 1), (1921, 1601, 2), (-390, 664, 2)])
async def test_only_sizes_a_hello_can_ask_for(tools: Tools, w: int, h: int, dsf: int) -> None:
    with pytest.raises(ValueError, match="no screen"):
        await tools.screen.apply(w, h, dsf)
    assert tools.calls() == []
