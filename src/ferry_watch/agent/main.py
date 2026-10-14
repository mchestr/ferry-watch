"""The browser pod's agent: `ferry-watch-agent`, PID 2 under tini (docs/multi-tenant/design-reference.md §5.1).

It starts Xvnc and matchbox, then runs the one-trip watcher, the VNC gate (:6080), the probes (:8080)
and the link to ferry-control. On SIGTERM it stops checking, refuses viewers, empties any held cart,
says goodbye, and exits. The termination log gets one line, `<bye reason> <error class|->`, and nothing
the page or a user typed.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.metadata
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from ferry_watch.bcferries.site import PodBrowser
from ferry_watch.bcferries.terminals import TerminalDirectory
from ferry_watch.bcferries.urls import USER_AGENT
from ferry_watch.protocol import (
    Bye,
    ByeReason,
    Command,
    CommandReason,
    Configure,
    ControlMessage,
    ProtocolError,
    encode,
)

from .gate import Gate, GateSettings
from .hub import RECONNECT_BACKOFF_SECONDS, HubClient, Identity
from .lockdown import Lockdown, self_test
from .processes import pod_processes
from .settings import AgentSettings, SettingsError
from .store import OneTripStore
from .watch import Browser, Finish, TripWatcher

log = logging.getLogger("ferry_watch.agent")

STATE_EVERY_SECONDS = 15.0  # at least this often
STATE_CHECK_SECONDS = 0.25  # and on any change, coalesced over this
LOCKDOWN_CHECK_SECONDS = 10.0
BYE_ACK_SECONDS = 5.0
SHUTDOWN_SECONDS = 3.0  # for the servers' last connections, within the drain budget
DEADLINE_MARGIN_SECONDS = 120.0  # a TERM this close to the deadline is the deadline
TERMINATION_LOG = Path("/dev/termination-log")
CLEAN_EXITS = frozenset({"hold_ended", "drained", "deadline", "recycle", "smoke_done", "short_check_done"})
_QUIET_ENVELOPE: dict[str, Any] = {"seq": 0, "trip": "0", "boot": "00000000", "rev": 0}
_DRAIN_REASONS: dict[CommandReason, ByeReason] = {"recycle": "recycle"}


class Agent:
    def __init__(
        self,
        settings: AgentSettings,
        http: aiohttp.ClientSession,
        browser: Browser,
        *,
        lockdown: Lockdown,
        agent_version: str,
        hub_backoff: tuple[float, ...] = RECONNECT_BACKOFF_SECONDS,
    ) -> None:
        self.settings = settings
        self.lockdown = lockdown
        self.store = OneTripStore(settings.trip_id)
        identity = Identity(settings.trip_id, settings.pod_name, settings.gen, agent_version)
        self.hub = HubClient(
            http,
            settings.hub_url,
            settings.token_path,
            identity,
            self._handle,
            rev=lambda: self.store.rev,
            backoff=hub_backoff,
        )
        self.watcher = TripWatcher(
            self.store,
            browser,
            self.hub,
            deadline_at=settings.deadline_at,
            lockdown_ok=lockdown.passed,
            close_viewer=self._close_viewer,
        )
        self.gate = Gate(
            GateSettings(
                origin=settings.vnc_origin,
                owner=settings.owner,
                trip_id=settings.trip_id,
                pod_name=settings.pod_name,
                keys=settings.vnc_keys,
                rfb_socket=settings.rfb_socket,
            ),
            checkout_cart=self.watcher.checkout_cart,
            viewer_changed=self.watcher.viewer_changed,
        )
        self.gate_port: int | None = None
        self.probe_port: int | None = None

    def terminate(self) -> None:
        """SIGTERM: drain (pod deletion, a node drain, or the pod's deadline)."""
        near_deadline = time.time() >= self.settings.deadline_at - DEADLINE_MARGIN_SECONDS
        self.watcher.drain("deadline" if near_deadline else "drained")

    async def run(self, processes_died: Awaitable[str] | None = None) -> Finish:
        runners = [
            web.AppRunner(self.gate.app(), access_log=None, shutdown_timeout=SHUTDOWN_SECONDS),
            web.AppRunner(_probes(), access_log=None, shutdown_timeout=SHUTDOWN_SECONDS),
        ]
        for runner in runners:
            await runner.setup()
        tasks: list[asyncio.Task[Any]] = []
        try:
            gate_site = web.TCPSite(runners[0], "0.0.0.0", self.settings.gate_port)  # noqa: S104 - vnc-edge connects
            probe_site = web.TCPSite(runners[1], "0.0.0.0", self.settings.probe_port)  # noqa: S104 - the kubelet probes
            await gate_site.start()
            await probe_site.start()
            self.gate_port = _port(runners[0])
            self.probe_port = _port(runners[1])
            tasks = [
                asyncio.create_task(self.hub.run(), name="hub"),
                asyncio.create_task(self._publish(), name="state"),
                asyncio.create_task(self._watch_lockdown(), name="lockdown"),
            ]
            if processes_died is not None:
                tasks.append(asyncio.create_task(self._watch_processes(processes_died), name="processes"))
            finish = await self.watcher.run()
            await self._goodbye(finish)
            return finish
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for runner in reversed(runners):
                await runner.cleanup()

    # ---- the operator

    async def _handle(self, message: ControlMessage) -> None:
        if isinstance(message, Configure):
            try:
                self.store.apply(message)
            except ProtocolError as e:
                log.error("Refusing a configure: %s", e)  # noqa: TRY400 - the reason is enough
                self.watcher.drain("fatal")
        elif isinstance(message, Command):
            await self._command(message)

    async def _command(self, command: Command) -> None:
        watcher = self.watcher
        if command.op == "check_now":
            watcher.check_now()
        elif command.op == "release" and command.cart_id is not None:
            watcher.release(command.cart_id)
        elif command.op == "dismiss" and command.cart_id is not None:
            watcher.dismiss(command.cart_id)
        elif command.op == "drain":
            watcher.drain(_DRAIN_REASONS.get(command.reason or "maintenance", "drained"))
        elif command.op == "vnc_revoke":
            await watcher.revoke_viewer(f"revoked ({command.reason})")

    async def _close_viewer(self, reason: str) -> None:
        await self.gate.close(reason)

    async def _publish(self) -> None:
        """Send the trip's state when it changes (coalesced) and at least every 15 s."""
        last: bytes | None = None
        sent_at = 0.0
        while True:
            current = encode(self.watcher.state(_QUIET_ENVELOPE))
            if current != last or time.monotonic() - sent_at >= STATE_EVERY_SECONDS:
                self.hub.publish(self.watcher.state(self.hub.envelope()))
                self.watcher.navs_sent()
                last, sent_at = encode(self.watcher.state(_QUIET_ENVELOPE)), time.monotonic()
            await asyncio.sleep(STATE_CHECK_SECONDS)

    async def _goodbye(self, finish: Finish) -> None:
        self.hub.publish(self.watcher.state(self.hub.envelope()))
        bye = Bye(
            **self.hub.envelope(),
            reason=finish.reason,
            outcome=finish.outcome,
            cart_emptied=finish.cart_emptied,
            release_check=finish.release_check,
            viewer_was_connected=finish.viewer_was_connected,
        )
        self.hub.send(bye)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(BYE_ACK_SECONDS):
                while not self.hub.acked(bye.seq):  # noqa: ASYNC110 - the ack arrives on another task
                    await asyncio.sleep(0.05)
        log.info("Goodbye: %s (%s)", finish.reason, finish.outcome)

    # ---- the pod

    async def _watch_lockdown(self) -> None:
        while True:
            await asyncio.sleep(LOCKDOWN_CHECK_SECONDS)
            if self.lockdown.give_up(time.time()):
                self.watcher.drain("lockdown_failed")
                return

    async def _watch_processes(self, died: Awaitable[str]) -> None:
        name = await died
        log.error("%s exited unexpectedly", name)
        self.watcher.drain("fatal")


def _probes() -> web.Application:
    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="ok\n")

    app = web.Application()
    app.router.add_get("/livez", ok, allow_head=False)
    app.router.add_get("/readyz", ok, allow_head=False)
    return app


def _port(runner: web.AppRunner) -> int:
    address = runner.addresses[0]
    return int(address[1])


def termination_line(finish: Finish | None, error_class: str | None) -> str:
    """The one line for /dev/termination-log: enums only, since the operator can read it."""
    reason = finish.reason if finish is not None else "startup_failed"
    return f"{reason} {error_class or '-'}\n"


async def run_pod(settings: AgentSettings) -> tuple[Finish, str | None]:
    """Everything in a real pod: the processes, Chromium, and the agent. Returns how it ended."""
    for directory in (settings.home, settings.tmp / "kasm"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        "DISPLAY": settings.display,
        "HOME": str(settings.home),
        "TMPDIR": str(settings.tmp),
        "TZ": "America/Vancouver",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    processes = pod_processes(settings.display, settings.screen, settings.rfb_socket, env)
    lockdown = Lockdown()
    try:
        await processes.start()
    except (OSError, RuntimeError):
        log.exception("Couldn't start the display")
        await processes.stop()
        return Finish("startup_failed"), None
    try:
        async with aiohttp.ClientSession() as http:
            terminals = TerminalDirectory(http, USER_AGENT)
            browser = PodBrowser(
                terminals,
                profile_dir=settings.profile_dir,
                window_size=settings.screen,
                env={k: v for k, v in env.items() if k != "PATH"},
                sandbox=settings.sandbox,
                debug_dir=settings.debug_dir,
                after_launch=functools.partial(
                    self_test, lockdown=lockdown, proc_net=settings.proc_net, rfb_socket=settings.rfb_socket
                ),
            )
            await browser.start()
            try:
                agent = Agent(settings, http, browser, lockdown=lockdown, agent_version=_version())
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGTERM, agent.terminate)
                loop.add_signal_handler(signal.SIGINT, agent.terminate)
                finish = await agent.run(processes.died())
                error = agent.watcher.error
                return finish, error[0] if error else None
            finally:
                await browser.stop()
    finally:
        await processes.stop()


def _version() -> str:
    try:
        return importlib.metadata.version("ferry-watch")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FW_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    try:
        settings = AgentSettings.from_env(os.environ)
    except SettingsError as e:
        log.critical("Bad settings: %s", e)
        with contextlib.suppress(OSError):
            TERMINATION_LOG.write_text(termination_line(None, None))
        sys.exit(1)
    finish, error_class = asyncio.run(run_pod(settings))
    with contextlib.suppress(OSError):
        settings.termination_log.write_text(termination_line(finish, error_class))
    sys.exit(0 if finish.reason in CLEAN_EXITS else 1)


if __name__ == "__main__":
    main()
