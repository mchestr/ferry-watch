from __future__ import annotations

import subprocess
import sys


def test_loads_without_playwright() -> None:
    """The web API and the control plane import these without a browser installed (docs/multi-tenant)."""
    code = (
        "import sys, ferry_watch.bcferries.urls, ferry_watch.polling\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'playwright')\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603 - fixed argv, no shell
