"""The browser pod's agent as a whole: settings, lockdown checks, its processes, and a run end to end."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import msgspec
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from playwright.async_api import Error as PlaywrightError

from ferry_watch.agent.lockdown import (
    EXIT_AFTER_FAILURE_S,
    EXPECTED_TCP,
    Lockdown,
    check_chromium,
    check_listeners,
    tcp_listeners,
    unix_listeners,
)
from ferry_watch.agent.main import Agent, termination_line
from ferry_watch.agent.processes import MATCHBOX_ARGV, Processes, Spec, xvnc_argv
from ferry_watch.agent.settings import AgentSettings, SettingsError
from ferry_watch.agent.watch import Finish
from ferry_watch.models import Trip
from ferry_watch.protocol import Alert, Bye, Command, Configure, HoldLimits, TripSpec
from ferry_watch.tickets import mint
from tests.fakes import FakeSite, sailing
from tests.test_agent import FakeOperator
from tests.test_gate import GREETING, FakeXvnc

KEY = Ed25519PrivateKey.generate()
RAW_KEY = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
TRIP_ID = "3f9c0a1b2c3d4e5f"
ORIGIN = "https://ferry-checkout.chestr.dev"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


ENV = {
    "FW_TRIP_ID": TRIP_ID,
    "FW_POD_NAME": f"fw-{TRIP_ID}-g3",
    "FW_OWNER": "user-7",
    "FW_DEADLINE_AT": "1790000000",
    "FW_HUB_URL": "ws://ferry-control.ferry-watch.svc.cluster.local.:8766/agent/v1",
    "FW_VNC_ORIGIN": ORIGIN,
    "FW_VNC_PUBKEYS": f"k1:{b64(RAW_KEY)}",
}

# ---- settings


def test_settings_from_the_pod_spec() -> None:
    settings = AgentSettings.from_env(ENV | {"FW_SANDBOX": "1"})
    assert (settings.trip_id, settings.pod_name, settings.gen) == (TRIP_ID, f"fw-{TRIP_ID}-g3", 3)
    assert set(settings.vnc_keys) == {"k1"}
    assert settings.sandbox
    assert settings.rfb_socket == Path("/tmp/kasm/rfb.sock")


@pytest.mark.parametrize(
    "change",
    [
        {"FW_TRIP_ID": ""},
        {"FW_TRIP_ID": "../x"},
        {"FW_POD_NAME": "fw-ffffffffffffffff-g3"},  # another trip's pod name
        {"FW_OWNER": "User 7"},
        {"FW_DEADLINE_AT": "tomorrow"},
        {"FW_HUB_URL": "https://ferry-control/agent/v1"},
        {"FW_VNC_ORIGIN": "http://ferry-checkout.chestr.dev"},
        {"FW_VNC_ORIGIN": "https://ferry-checkout.chestr.dev/path"},
        {"FW_VNC_PUBKEYS": "k1:short"},
    ],
)
def test_bad_settings_stop_the_agent(change: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        AgentSettings.from_env(ENV | change)


def test_the_termination_line_carries_enums_only() -> None:
    assert termination_line(Finish("hold_ended", "booked"), None) == "hold_ended -\n"
    assert termination_line(Finish("fatal"), "markup_changed") == "fatal markup_changed\n"
    assert termination_line(None, None) == "startup_failed -\n"


# ---- lockdown

TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:17C0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 1 1
   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 2 1
   2: 0100007F:A1B2 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1001        0 3 1
"""
TCP6_DEVTOOLS = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue
   0: 00000000000000000000000001000000:2382 00000000000000000000000000000000:0000 0A 00000000:00000000
"""
UNIX = """\
Num       RefCount Protocol Flags    Type St Inode Path
0000000000000000: 00000002 00000000 00010000 0001 01 1001 {rfb}
0000000000000000: 00000002 00000000 00010000 0001 01 1002 /tmp/.X11-unix/X99
0000000000000000: 00000003 00000000 00000000 0001 03 1003 {rfb}
0000000000000000: 00000002 00000000 00010000 0001 01 1004 @/tmp/.ICE-unix/1
"""


def test_tcp_listeners_are_read_from_proc() -> None:
    assert tcp_listeners([TCP]) == EXPECTED_TCP
    assert tcp_listeners([TCP6_DEVTOOLS]) == {("::1", 9090)}


def test_unix_listeners_are_read_from_proc() -> None:
    assert unix_listeners(UNIX.format(rfb="/tmp/kasm/rfb.sock")) == {
        "/tmp/kasm/rfb.sock",
        "/tmp/.X11-unix/X99",
        "@/tmp/.ICE-unix/1",
    }


def _proc(tmp_path: Path, rfb: Path, *, tcp: str = TCP, tcp6: str = "header\n") -> Path:
    proc = tmp_path / "net"
    proc.mkdir()
    (proc / "tcp").write_text(tcp)
    (proc / "tcp6").write_text(tcp6)
    (proc / "unix").write_text(UNIX.format(rfb=rfb))
    return proc


def _rfb(tmp_path: Path, mode: int) -> tuple[Path, socket.socket]:
    path = tmp_path / "rfb.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    os.chmod(path, mode)  # noqa: PTH101
    return path, server


def test_listeners_pass_when_only_the_gate_probes_and_xvnc_listen(tmp_path: Path) -> None:
    rfb, server = _rfb(tmp_path, 0o600)
    with server:
        assert check_listeners(_proc(tmp_path, rfb), rfb) == []


def test_listeners_fail_on_anything_extra_or_a_loose_socket(tmp_path: Path) -> None:
    rfb, server = _rfb(tmp_path, 0o666)
    with server:
        failures = check_listeners(_proc(tmp_path, rfb, tcp6=TCP6_DEVTOOLS), rfb)
    assert len(failures) == 2
    assert "unexpected [('::1', 9090)]" in failures[0]
    assert "mode 666" in failures[1]


class FakePage:
    def __init__(self, blocked: bool = True, outside_loads: bool = False, reached: bool = False) -> None:
        self.blocked = blocked
        self.outside_loads = outside_loads
        self.reached = reached
        self.closed = False

    async def goto(self, url: str, **_: object) -> None:
        if url == "about:blank":  # as Chromium does, from a tab showing an error page
            raise PlaywrightError('Navigation to "about:blank" is interrupted by another navigation')
        if url.startswith("https://example.com"):
            if not self.outside_loads:
                raise PlaywrightError("net::ERR_NAME_NOT_RESOLVED")
            return
        raise PlaywrightError("net::ERR_BLOCKED_BY_ADMINISTRATOR" if self.blocked else "net::ERR_ABORTED")

    async def evaluate(self, _: str) -> dict[str, bool]:
        return {"gate websocket": self.reached, "xvnc websocket": False, "xvnc http": False, "outside host": False}

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page


async def test_chromium_checks_pass_under_the_policy() -> None:
    page = FakePage()
    assert await check_chromium(FakeContext(page)) == []  # type: ignore[arg-type]
    assert page.closed


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (FakePage(blocked=False), "failed, but not blocked by policy"),
        (FakePage(outside_loads=True), "an outside host loaded"),
        (FakePage(reached=True), "page script reached the gate websocket"),
    ],
)
async def test_chromium_checks_fail_closed(page: FakePage, expected: str) -> None:
    failures = await check_chromium(FakeContext(page))  # type: ignore[arg-type]
    assert any(expected in f for f in failures)


def test_a_failed_lockdown_gives_up_after_five_minutes() -> None:
    lockdown = Lockdown()
    assert not lockdown.passed()
    lockdown.record(["bad"], now=100)
    assert not lockdown.give_up(100 + EXIT_AFTER_FAILURE_S - 1)
    assert lockdown.give_up(100 + EXIT_AFTER_FAILURE_S)
    lockdown.record([], now=200)
    assert lockdown.passed()
    assert not lockdown.give_up(10_000)


# ---- processes


def test_xvnc_listens_on_its_unix_socket_only() -> None:
    argv = xvnc_argv(":99", (1280, 1000), Path("/tmp/kasm/rfb.sock"))
    assert argv[argv.index("-rfbunixpath") + 1] == "/tmp/kasm/rfb.sock"
    assert argv[argv.index("-rfbunixmode") + 1] == "0600"
    for flag in ("-websocketPort", "-interface", "-httpd", "-AlwaysShared", "-MaxIdleTime"):
        assert flag not in argv


def test_xvnc_lets_the_gates_client_see_the_screen_and_use_it() -> None:
    # Without it, a client with no kasmpasswd user (the gate's, on the unix socket) gets no frames and no input.
    assert "-DisableBasicAuth" in xvnc_argv(":99", (1280, 1000), Path("/tmp/kasm/rfb.sock"))


def test_xvnc_sends_a_scroll_as_a_copy_of_what_the_viewer_already_has() -> None:
    # A quarter to a tenth of the bytes per scroll of the checkout, which a phone on cellular notices.
    assert "-DetectScrolling" in xvnc_argv(":99", (1280, 1000), Path("/tmp/kasm/rfb.sock"))


def test_matchbox_has_no_key_bindings() -> None:
    # Its default kbdconfig closes the window on Alt+C, under the checkout viewer's keys.
    assert MATCHBOX_ARGV[MATCHBOX_ARGV.index("-kbdconfig") + 1] == "/dev/null"


async def test_processes_start_report_a_death_and_stop() -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    processes = Processes([Spec("one", sleeper), Spec("two", sleeper)], dict(os.environ))
    await processes.start()
    died = asyncio.create_task(processes.died())
    await asyncio.sleep(0.05)
    assert not died.done()
    processes._running["two"].kill()
    assert await asyncio.wait_for(died, 5) == "two"
    await processes.stop()


async def test_a_process_that_never_gets_ready_fails_the_start() -> None:
    processes = Processes([Spec("quitter", [sys.executable, "-c", "pass"], ready=lambda: False)], dict(os.environ))
    with pytest.raises(RuntimeError, match="exited"):
        await processes.start()
    await processes.stop()


# ---- the agent, end to end


def soon() -> Trip:
    return Trip(id=TRIP_ID, origin="SWB", destination="TSA", date=date.today() + timedelta(days=2))  # noqa: DTZ011


def configure(rev: int = 1, **kw: Any) -> Configure:
    fields: dict[str, Any] = {
        "active": True,
        "interval_s": 300,
        "auto_cart": True,
        "vnc_allowed": True,
        "hold": HoldLimits(unclaimed_s=1800, session_s=1800, viewer_gone_s=600, hard_s=3600),
    }
    return Configure(
        seq=rev,
        trip=TRIP_ID,
        boot="0" * 16,
        rev=rev,
        trip_spec=msgspec.convert(soon().to_json(), TripSpec),
        **(fields | kw),
    )


async def test_a_pod_carts_opens_the_checkout_and_drains(aiohttp_server: Callable[..., Any], tmp_path: Path) -> None:
    operator = FakeOperator(after_hello=[configure()])
    app = web.Application()
    app.router.add_get("/agent/v1", operator.handler)
    server: TestServer = await aiohttp_server(app)
    (tmp_path / "token").write_text(operator.token)
    xvnc = FakeXvnc(tmp_path / "kasm" / "rfb.sock")
    xvnc.path.parent.mkdir()
    await xvnc.start()
    settings = replace(
        AgentSettings.from_env(ENV | {"FW_HUB_URL": str(server.make_url("/agent/v1")).replace("http", "ws", 1)}),
        token_path=tmp_path / "token",
        deadline_at=1e12,
        tmp=tmp_path,
        gate_port=0,
        probe_port=0,
    )
    site = FakeSite(sailings=[sailing(9), sailing(11, available=False)])

    async with aiohttp.ClientSession() as http:
        agent = Agent(settings, http, site, lockdown=Lockdown(ok=True), agent_version="0.6.0", hub_backoff=(0.05,))
        run = asyncio.create_task(agent.run())
        try:
            # It connects, takes the trip, checks, asks for a claim and holds the 09:00 sailing.
            await operator.wait_for(
                lambda: any(isinstance(m, Alert) and m.kind == "cart_held" for m in operator.received)
            )
            cart = agent.watcher.cart
            assert cart is not None
            assert operator.hellos[0].pod_name == settings.pod_name

            # The probes answer.
            assert agent.probe_port is not None
            async with http.get(f"http://127.0.0.1:{agent.probe_port}/livez") as response:
                assert response.status == 200

            # The owner opens the checkout with a ticket and sees the screen.
            ticket = mint(KEY, "k1", sub="user-7", trip=TRIP_ID, pod=settings.pod_name, cart_id=cart.id)
            ws = await http.ws_connect(
                f"http://127.0.0.1:{agent.gate_port}/ws",
                protocols=["binary", f"fw.t1.{ticket}", "fw.n1." + "n" * 32],
                origin=ORIGIN,
            )
            frame = await asyncio.wait_for(ws.receive(), 5)
            assert frame.data == GREETING
            await operator.wait_for(
                lambda: any(getattr(m, "code", None) == "viewer_connected" for m in operator.received)
            )

            # The pod is deleted: it closes the viewer, empties the cart and says goodbye.
            agent.terminate()
            finish = await asyncio.wait_for(run, 10)
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            await xvnc.stop()

    assert finish == Finish(
        "drained", "released", cart_emptied=True, release_check="landing_url", viewer_was_connected=True
    )
    assert "empty cart" in site.session.calls
    byes = [m for m in operator.received if isinstance(m, Bye)]
    assert [(b.reason, b.outcome, b.viewer_was_connected) for b in byes] == [("drained", "released", True)]
    assert ws.closed or (await asyncio.wait_for(ws.receive(), 5)).type in (
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.CLOSING,
    )


async def test_the_operator_can_drain_and_revoke(aiohttp_server: Callable[..., Any], tmp_path: Path) -> None:
    drain = Command(seq=2, trip=TRIP_ID, boot="0" * 16, rev=1, id=1, op="drain", reason="recycle")
    operator = FakeOperator(after_hello=[configure(active=False), drain])
    app = web.Application()
    app.router.add_get("/agent/v1", operator.handler)
    server: TestServer = await aiohttp_server(app)
    (tmp_path / "token").write_text(operator.token)
    settings = replace(
        AgentSettings.from_env(ENV | {"FW_HUB_URL": str(server.make_url("/agent/v1")).replace("http", "ws", 1)}),
        token_path=tmp_path / "token",
        deadline_at=1e12,
        tmp=tmp_path,
        gate_port=0,
        probe_port=0,
    )
    async with aiohttp.ClientSession() as http:
        agent = Agent(settings, http, FakeSite(), lockdown=Lockdown(ok=True), agent_version="0.6.0")
        finish = await asyncio.wait_for(agent.run(), 10)
    assert finish == Finish("recycle")
    assert [m.reason for m in operator.received if isinstance(m, Bye)] == ["recycle"]
