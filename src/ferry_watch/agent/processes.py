"""The pod's other processes: Xvnc (the screen) and matchbox (keeps the one window full screen).

Each runs in its own session, so tini's TERM reaches only the agent, which stops them last, after the
cart is emptied (docs/multi-tenant/design-reference.md §5.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0


def xvnc_argv(display: str, screen: tuple[int, int], rfb_socket: Path) -> list[str]:
    """Xvnc with RFB on a unix socket only: no TCP, no WebSocket, no web server (§5.6)."""
    width, height = screen
    return [
        "Xvnc",
        display,
        "-geometry",
        f"{width}x{height}",
        "-depth",
        "24",
        "-rfbunixpath",
        str(rfb_socket),
        "-rfbunixmode",
        "0600",
        "-SecurityTypes",
        "None",
        # Despite its name, this is what gives the gate's unix-socket client view and input rights: without it
        # KasmVNC looks a user up in ~/.kasmpasswd, finds none, and never sends a frame (a black checkout).
        # With -rfbunixpath there's no WebSocket or HTTP listener, so there's no basic auth to disable; access
        # stays the socket's 0600 mode and the gate.
        "-DisableBasicAuth",
        "-NeverShared",
        "-DisconnectClients=0",
        "-SendCutText=0",
        "-AcceptCutText=1",
        "-DLP_ClipAcceptMax",
        "256",
        "-IgnoreClientSettingsKasm",
        "-FrameRate",
        "15",
        "-MaxVideoResolution",
        "1920x1080",
        "-MaxConnectionTime",
        "6000",
        "-AcceptSetDesktopSize",
        "-publicIP",
        "127.0.0.1",
        "-Log",
        "*:stderr:10",
    ]


MATCHBOX_ARGV = ["matchbox-window-manager", "-use_titlebar", "no"]


@dataclass(frozen=True, slots=True)
class Spec:
    name: str
    argv: Sequence[str]
    ready: Callable[[], bool] = lambda: True  # started when this is true


class Processes:
    def __init__(self, specs: Sequence[Spec], env: Mapping[str, str]) -> None:
        self._specs = specs
        self._env = dict(env)
        self._running: dict[str, asyncio.subprocess.Process] = {}

    async def start(self) -> None:
        """Start each in order, waiting until it's ready before the next."""
        for spec in self._specs:
            process = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.DEVNULL,
                env=self._env,
                start_new_session=True,
            )
            self._running[spec.name] = process
            try:
                async with asyncio.timeout(START_TIMEOUT_SECONDS):
                    while not spec.ready():
                        if process.returncode is not None:
                            raise RuntimeError(f"{spec.name} exited with {process.returncode}")
                        await asyncio.sleep(0.05)
            except TimeoutError:
                raise RuntimeError(f"{spec.name} wasn't ready after {START_TIMEOUT_SECONDS:.0f}s") from None
            log.info("Started %s (pid %d)", spec.name, process.pid)

    async def died(self) -> str:
        """Wait until any of them exits; returns its name."""
        waits = {asyncio.create_task(p.wait()): name for name, p in self._running.items()}
        if not waits:
            await asyncio.Event().wait()
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
        return waits[next(iter(done))]

    async def stop(self) -> None:
        for name, process in reversed(list(self._running.items())):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), STOP_TIMEOUT_SECONDS)
                except TimeoutError:
                    log.warning("%s didn't stop; killing it", name)
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        self._running.clear()


def pod_processes(display: str, screen: tuple[int, int], rfb_socket: Path, env: Mapping[str, str]) -> Processes:
    x_socket = Path("/tmp/.X11-unix") / f"X{display.lstrip(':')}"  # noqa: S108 - X11's fixed location
    rfb_socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(rfb_socket.parent, 0o700)  # noqa: PTH101 - an existing directory too
    return Processes(
        [
            Spec("Xvnc", xvnc_argv(display, screen, rfb_socket), lambda: x_socket.exists() and rfb_socket.exists()),
            Spec("matchbox", MATCHBOX_ARGV),
        ],
        env,
    )
