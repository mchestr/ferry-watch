"""Run the agent's lockdown self-test against a second Chromium inside a running browser container.

    docker exec <container> python3 -I /smoke/selftest.py

The agent's own Chromium starts only when ferry-control sends it a trip, so the smoke test launches one
the same way the agent does and runs the same checks: the policy blocks browser internals, files and
view-source, an outside host doesn't load, page script can't reach the gate or Xvnc, and the only
listeners are the gate, the probes and Xvnc's unix socket.
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ferry_watch.agent.lockdown import Lockdown, self_test


async def main() -> int:
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            "/tmp/smoke-profile",  # noqa: S108 - the container's memory-backed /tmp
            headless=False,
            args=["--kiosk"],
            env={"DISPLAY": ":99", "HOME": "/tmp/home"},  # noqa: S108
            handle_sigterm=False,
            chromium_sandbox=False,
        )
        lockdown = Lockdown()
        try:
            await self_test(context, lockdown, proc_net=Path("/proc/net"), rfb_socket=Path("/tmp/kasm/rfb.sock"))  # noqa: S108
        finally:
            await context.close()
    sys.stdout.write(f"lockdown ok={lockdown.ok} failures={lockdown.failures}\n")
    return 0 if lockdown.ok else 1


sys.exit(asyncio.run(main()))
