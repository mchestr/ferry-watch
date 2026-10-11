"""Entry point: `ferry-watch` (or `python -m ferry_watch`)."""

from __future__ import annotations

import logging
import sys

from aiohttp import web

from . import __version__
from .server import create_app
from .settings import Settings, SettingsError


def main() -> None:
    try:
        settings = Settings.from_env()
    except SettingsError as e:
        sys.exit(f"ferry-watch: {e}")
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log = logging.getLogger("ferry_watch")
    log.info(
        "Ferry Watch %s: http://%s:%d, config %s, Pushover %s, BC Ferries login %s",
        __version__,
        settings.host,
        settings.port,
        settings.config_path,
        "on" if settings.pushover_token else "off",
        "on" if settings.credentials else "off",
    )
    web.run_app(create_app(settings), host=settings.host, port=settings.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
