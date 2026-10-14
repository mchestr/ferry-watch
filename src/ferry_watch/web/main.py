"""`ferry-web run`: the internet-facing app (docs/multi-tenant/design-reference.md §6.1).

It serves the SPA and its API on :8765. It has no Kubernetes access: it reads and writes rows, and
ferry-control acts on them. Its secrets (the OIDC client secret and the checkout signing key) come from
mounted files; the database from the libpq environment.

`ferry-web keys` prints a fresh checkout key pair and VAPID key, for setting up a deployment.
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

from ferry_watch.bcferries.errors import BookingError
from ferry_watch.bcferries.terminals import TerminalDirectory
from ferry_watch.bcferries.urls import USER_AGENT

from .accounts import Accounts
from .api import CheckoutSigner
from .app import build
from .config import ConfigError, WebConfig, load, new_keys, read_secret, signing_key
from .identity import OidcClient
from .messages import Messages
from .outbox import Mailer, Outbox
from .push import Vapid
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


async def run_web(
    config: WebConfig, client_secret: str, checkout_key: str, vapid_key: str | None, stop: asyncio.Event
) -> None:
    pool = await asyncpg.create_pool(min_size=1, max_size=10)
    tasks: list[asyncio.Task[None]] = []
    try:
        async with aiohttp.ClientSession() as http:
            limits = config.user_limits()
            accounts = Accounts(pool, config.identity_policy())
            terminals = TerminalDirectory(http, USER_AGENT)
            vapid = Vapid.from_raw(vapid_key, config.vapid_subject) if vapid_key else None
            smtp = config.smtp
            outbox = Outbox(
                pool,
                http,
                Messages(config.base_url, lambda: terminals.cached),
                vapid=vapid,
                mailer=Mailer(smtp.host, smtp.port, smtp.sender) if smtp else None,
            )
            app = build(
                accounts,
                OidcClient(http, config.oidc_settings(client_secret)),
                WebStore(pool, limits, config.policy(), clock=time.time),
                CheckoutSigner(signing_key(checkout_key), config.checkout.key_id, config.checkout.origin),
                pool=pool,
                limits=limits,
                terminals=terminals,
                public={"links": {"kofi": config.kofi_url}, "push": {"key": vapid.public_key if vapid else None}},
            )
            tasks = [asyncio.create_task(outbox.run(), name="outbox"), asyncio.create_task(_warm(terminals))]
            if config.static_dir:
                add_static(app, Path(config.static_dir))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", config.listen_port).start()  # noqa: S104 - behind the gateway
            log.info("ferry-web listening on :%d", config.listen_port)
            try:
                await stop.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await runner.cleanup()
    finally:
        await pool.close()


async def _warm(terminals: TerminalDirectory) -> None:
    """Load the terminal list early, retrying, so alerts and the trip editor have route names."""
    delay = 5.0
    while True:
        try:
            await terminals.get()
        except BookingError as e:
            log.warning("%s; retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
        else:
            return


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FW_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="ferry-web")
    parser.add_argument("command", choices=["run", "keys"])
    parser.add_argument("--key-id", default="k1", help="keys: the checkout key's id (k2 when rotating)")
    args = parser.parse_args()
    if args.command == "keys":
        try:
            sys.stdout.write(new_keys(args.key_id) + "\n")
        except ConfigError as e:
            sys.exit(f"ferry-web: {e}")
        return
    try:
        client_id = None
        if os.environ.get("FW_OIDC_CLIENT_ID_FILE"):  # the IdP's generated id, beside the client secret
            client_id = read_secret(os.environ, "FW_OIDC_CLIENT_ID_FILE")
        config = load(Path(os.environ.get("FW_WEB_CONFIG", "/etc/ferry-web/config.json")), client_id=client_id)
        client_secret = read_secret(os.environ, "FW_OIDC_CLIENT_SECRET_FILE")
        checkout_key = read_secret(os.environ, "FW_CHECKOUT_KEY_FILE")
        signing_key(checkout_key)
        vapid_key = read_secret(os.environ, "FW_VAPID_KEY_FILE") if os.environ.get("FW_VAPID_KEY_FILE") else None
    except ConfigError as e:
        sys.exit(f"ferry-web: {e}")

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_web(config, client_secret, checkout_key, vapid_key, stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
