"""`ferry-web run`: the internet-facing app (docs/multi-tenant/design-reference.md §6.1).

It serves the SPA and its API on :8765. It has no Kubernetes access: it reads and writes rows, and
ferry-control acts on them. Its secrets (the OIDC client secret and the checkout signing key) come from
mounted files; the database from the libpq environment.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import signal
import sys
import time
from pathlib import Path

import aiohttp
import asyncpg
from aiohttp import web

from .accounts import Accounts
from .api import CheckoutSigner
from .app import build
from .config import ConfigError, WebConfig, load, read_secret, signing_key
from .identity import OidcClient
from .repo import WebStore

log = logging.getLogger("ferry_watch.web")

SPA_ROUTES = ("/", "/c/{trip_id:[a-f0-9]{16}}", "/admin", "/settings", "/auth/callback")  # all serve index.html


def add_static(app: web.Application, static_dir: Path) -> None:
    """The SPA shell on explicit routes (no wildcard), and its assets."""
    index = static_dir / "index.html"

    async def shell(_: web.Request) -> web.FileResponse:
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    for route in SPA_ROUTES:
        app.router.add_get(route, shell, allow_head=False)
    for item in static_dir.iterdir():
        if item.is_dir():
            app.router.add_static(f"/{item.name}", item, follow_symlinks=False)
        elif item.name != "index.html":
            app.router.add_get(f"/{item.name}", functools.partial(_file, item), allow_head=False)


async def _file(path: Path, _: web.Request) -> web.FileResponse:
    return web.FileResponse(path)


async def run_web(config: WebConfig, client_secret: str, checkout_key: str, stop: asyncio.Event) -> None:
    pool = await asyncpg.create_pool(min_size=1, max_size=10)
    try:
        async with aiohttp.ClientSession() as http:
            limits = config.user_limits()
            accounts = Accounts(pool, config.identity_policy())
            app = build(
                accounts,
                OidcClient(http, config.oidc_settings(client_secret)),
                WebStore(pool, limits, config.policy(), clock=time.time),
                CheckoutSigner(signing_key(checkout_key), config.checkout.key_id, config.checkout.origin),
                pool=pool,
                limits=limits,
            )
            if config.static_dir:
                add_static(app, Path(config.static_dir))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", config.listen_port).start()  # noqa: S104 - behind the gateway
            log.info("ferry-web listening on :%d", config.listen_port)
            try:
                await stop.wait()
            finally:
                await runner.cleanup()
    finally:
        await pool.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FW_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="ferry-web")
    parser.add_argument("command", choices=["run"])
    parser.parse_args()
    try:
        config = load(Path(os.environ.get("FW_WEB_CONFIG", "/etc/ferry-web/config.json")))
        client_secret = read_secret(os.environ, "FW_OIDC_CLIENT_SECRET_FILE")
        checkout_key = read_secret(os.environ, "FW_CHECKOUT_KEY_FILE")
        signing_key(checkout_key)
    except ConfigError as e:
        sys.exit(f"ferry-web: {e}")

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_web(config, client_secret, checkout_key, stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
