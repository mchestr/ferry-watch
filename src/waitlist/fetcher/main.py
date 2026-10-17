"""`waitlist-fetcher run`: the fetch service on :8767 (design.md §4.4).

Configuration is the deployment's JSON (WL_FETCHER_CONFIG, default /etc/waitlist-fetcher/config.json); the log
level is WL_LOG_LEVEL. It reads no other environment: no database, no token, no secret. A bad config exits with
`waitlist-fetcher: <message>`, as waitlist-web and waitlist-control do.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .app import serve
from .config import ConfigError, FetcherConfig, load

DEFAULT_CONFIG = "/etc/waitlist-fetcher/config.json"


async def run(config: FetcherConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await serve(config, stop)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=os.environ.get("WL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        prog="waitlist-fetcher",
        description="Make HTTP providers' requests for waitlist-control (POST /v1/fetch). Holds no secrets.",
    )
    parser.add_argument("command", choices=["run"])
    parser.parse_args(argv)
    try:
        config = load(Path(os.environ.get("WL_FETCHER_CONFIG", DEFAULT_CONFIG)))
    except ConfigError as e:
        sys.exit(f"waitlist-fetcher: {e}")
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
