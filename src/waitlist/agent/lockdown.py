"""Lockdown self-tests (docs/multi-tenant/design-reference.md §5.6).

They run after every Chromium launch (never during a hold, since a launch never happens then) and fail
closed: until they pass, the agent never carts and the gate never opens. A pod whose self-tests fail
reports it and exits after 5 minutes.

1. Chromium's managed policy is in force: browser internals, local files and view-source are blocked
   by the administrator, and a host outside the egress allowlist doesn't load.
2. The pod listens only where it should: TCP exactly on the gate (:6080) and the probes (:8080), and
   Xvnc only on its unix socket, readable by this user alone.
3. Page script can't reach the VNC from inside: from a page, neither the gate nor a would-be Xvnc
   WebSocket completes, and the outside host doesn't load.
"""

from __future__ import annotations

import ipaddress
import logging
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError

log = logging.getLogger(__name__)

GATE_PORT = 6080
PROBE_PORT = 8080
EXPECTED_TCP = frozenset({("0.0.0.0", GATE_PORT), ("0.0.0.0", PROBE_PORT)})  # noqa: S104 - the pod's own listeners
BLOCKED_BY_POLICY = ("chrome://version", "file:///etc/passwd", "view-source:https://www.bcferries.com/")
OUTSIDE_HOST = "https://example.com/"
EXIT_AFTER_FAILURE_S = 5 * 60
_LISTEN = "0A"  # TCP_LISTEN in /proc/net/tcp
_UNIX_LISTENING = 0x00010000  # __SO_ACCEPTCON in /proc/net/unix
_RFB_MODE = 0o600

# Run from a page: none of these may open or load. Each settles within 5 s.
_PAGE_PROBES = """
async () => {
  const opened = (url) => new Promise((resolve) => {
    let ws;
    try { ws = new WebSocket(url, ["binary"]); } catch (e) { resolve(false); return; }
    const timer = setTimeout(() => { ws.close(); resolve(false); }, 5000);
    ws.onopen = () => { clearTimeout(timer); ws.close(); resolve(true); };
    ws.onerror = () => { clearTimeout(timer); resolve(false); };
  });
  const loaded = async (url) => {
    try {
      const controller = new AbortController();
      setTimeout(() => controller.abort(), 5000);
      await fetch(url, { mode: "no-cors", signal: controller.signal });
      return true;
    } catch (e) { return false; }
  };
  return {
    "gate websocket": await opened("ws://127.0.0.1:6080/ws"),
    "xvnc websocket": await opened("ws://127.0.0.1:6081/websockify"),
    "xvnc http": await loaded("http://127.0.0.1:6081/"),
    "outside host": await loaded("https://example.com/"),
  };
}
"""


@dataclass
class Lockdown:
    """The latest self-test result: None until the first launch, then True or False."""

    ok: bool | None = None
    failures: list[str] = field(default_factory=list)
    failed_at: float | None = None

    def passed(self) -> bool:
        return self.ok is True

    def record(self, failures: list[str], now: float) -> None:
        self.failures = failures
        self.ok = not failures
        if failures:
            self.failed_at = self.failed_at or now
            log.error("Lockdown self-tests failed: %s", "; ".join(failures))
        else:
            self.failed_at = None
            log.info("Lockdown self-tests passed")

    def give_up(self, now: float) -> bool:
        """Failed for long enough that the pod should exit (it's reported meanwhile)."""
        return self.failed_at is not None and now - self.failed_at >= EXIT_AFTER_FAILURE_S


# ---- listeners


def _address(hex_address: str) -> str:
    raw = bytes.fromhex(hex_address)
    # /proc writes each 32-bit word in host (little-endian) order.
    words = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    return str(ipaddress.ip_address(words))


def tcp_listeners(tables: Iterable[str]) -> set[tuple[str, int]]:
    """Listening (address, port) pairs from the text of /proc/net/tcp and /proc/net/tcp6."""
    found: set[tuple[str, int]] = set()
    for table in tables:
        for line in table.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _LISTEN:  # st is the fourth column
                continue
            address, port = fields[1].split(":")
            found.add((_address(address), int(port, 16)))
    return found


def unix_listeners(table: str) -> set[str]:
    """Paths of listening unix sockets from the text of /proc/net/unix (abstract ones start with @)."""
    found: set[str] = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and int(fields[3], 16) & _UNIX_LISTENING:  # with a path
            found.add(fields[7])
    return found


def check_listeners(proc_net: Path, rfb_socket: Path) -> list[str]:
    failures = []
    tables = [(proc_net / name).read_text() for name in ("tcp", "tcp6") if (proc_net / name).exists()]
    tcp = tcp_listeners(tables)
    if tcp != EXPECTED_TCP:
        extra = sorted(tcp - EXPECTED_TCP)
        missing = sorted(EXPECTED_TCP - tcp)
        failures.append(f"TCP listeners: unexpected {extra}, missing {missing}")
    unix = unix_listeners((proc_net / "unix").read_text())
    if str(rfb_socket) not in unix:
        failures.append("Xvnc isn't listening on its unix socket")
    else:
        mode = rfb_socket.stat().st_mode
        if not stat.S_ISSOCK(mode) or stat.S_IMODE(mode) != _RFB_MODE:
            failures.append(f"Xvnc's socket has mode {stat.S_IMODE(mode):o}, not 600")
    others = sorted(unix - {str(rfb_socket)})
    if others:
        log.info("Other unix listeners: %s", others)  # X11's own socket, Chromium's singleton
    return failures


# ---- Chromium


async def check_chromium(context: BrowserContext) -> list[str]:
    failures = []
    page = await context.new_page()
    try:
        for url in BLOCKED_BY_POLICY:
            try:
                await page.goto(url, timeout=10_000)
            except PlaywrightError as e:
                if "ERR_BLOCKED_BY_ADMINISTRATOR" not in str(e):
                    failures.append(f"{url.split(':')[0]}: failed, but not blocked by policy")
            else:
                failures.append(f"{url.split(':')[0]}: loaded")
        try:
            await page.goto(OUTSIDE_HOST, timeout=10_000)
        except PlaywrightError:
            pass  # expected: not on the egress allowlist
        else:
            failures.append("an outside host loaded")
    finally:
        await page.close()
    # The page probes run in a fresh tab, which is at about:blank with an opaque origin. (Sending a tab
    # that shows an error page back to about:blank only lands on another error page.)
    probe = await context.new_page()
    try:
        results = await probe.evaluate(_PAGE_PROBES)
    finally:
        await probe.close()
    failures += [f"page script reached the {name}" for name, reached in sorted(results.items()) if reached]
    return failures


async def self_test(context: BrowserContext, lockdown: Lockdown, *, proc_net: Path, rfb_socket: Path) -> None:
    """Run every check and record the result. Never raises: an error is a failure."""
    try:
        failures = check_listeners(proc_net, rfb_socket) + await check_chromium(context)
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        failures = [f"self-test error: {type(e).__name__}"]
    lockdown.record(failures, time.time())
