"""`ferry-control`: the operator's entrypoints (docs/multi-tenant/design-reference.md §6.1).

- `ferry-control migrate`: apply migrations and grants, as the database owner (the db-migrate Job).
- `ferry-control run`: the hub endpoint (:8766) and the fleet loop, as `ferrywatch_control`.

Configuration is the deployment's JSON (FW_CONTROL_CONFIG); database credentials come from the libpq
variables. Only one `run` works at a time: it takes a Postgres advisory lock, and a second replica idles
(not ready) until the first goes away.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

import aiohttp
import asyncpg
from aiohttp import web

from .config import ConfigError, ControlConfig, load
from .db import grant, migrate
from .fleet import Fleet
from .hub import HubEndpoint
from .kube import KubeCluster
from .store import ControlStore

log = logging.getLogger("ferry_watch.control")

RUN_LOCK_KEY = 0x6677_6374  # "fwct": one ferry-control at a time
LOCK_RETRY_SECONDS = 10.0


async def run_migrate(config: ControlConfig) -> None:
    conn = await asyncpg.connect()
    try:
        applied = await migrate(conn)
        await grant(conn, config.db_roles())
    finally:
        await conn.close()
    log.info("Database ready (%s)", ", ".join(applied) if applied else "nothing new")


async def run_control(config: ControlConfig, stop: asyncio.Event) -> None:
    ready = asyncio.Event()
    probes = web.Application()

    async def livez(_: web.Request) -> web.Response:
        return web.Response(text="ok\n")

    async def readyz(_: web.Request) -> web.Response:
        return web.Response(text="ok\n") if ready.is_set() else web.Response(status=503, text="waiting\n")

    probes.router.add_get("/livez", livez)
    probes.router.add_get("/readyz", readyz)
    probe_runner = web.AppRunner(probes, access_log=None)
    await probe_runner.setup()
    await web.TCPSite(probe_runner, "0.0.0.0", config.probe_port).start()  # noqa: S104 - the kubelet probes

    pool = await asyncpg.create_pool(min_size=1, max_size=8)
    try:
        async with pool.acquire() as lock_conn:
            while not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", RUN_LOCK_KEY):
                log.info("Another ferry-control is running; waiting")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), LOCK_RETRY_SECONDS)
                if stop.is_set():
                    return
            async with aiohttp.ClientSession() as http:
                await _serve(config, pool, http, ready, stop)
    finally:
        await pool.close()
        await probe_runner.cleanup()


async def _serve(
    config: ControlConfig,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    ready: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    settings = config.hub_settings()
    cluster = KubeCluster(http, namespace=config.pod.namespace)
    fleet: Fleet | None = None
    hub = HubEndpoint(
        ControlStore(pool, config.user_limits()),
        cluster,
        settings,
        stretch=lambda: fleet.stretch if fleet is not None else 1.0,
    )
    fleet = Fleet(
        pool,
        cluster,
        hub,
        config.pod_params(),
        settings,
        limits=config.user_limits(),
        max_browsers_total=config.fleet.max_browsers_total,
        short_check_slots=config.fleet.short_check_slots,
    )
    runner = web.AppRunner(hub.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.listen_port).start()  # noqa: S104 - agents dial in
    tasks = [asyncio.create_task(hub.run(), name="hub"), asyncio.create_task(fleet.run(), name="fleet")]
    ready.set()
    log.info("ferry-control running")
    try:
        await stop.wait()
    finally:
        ready.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FW_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(prog="ferry-control")
    parser.add_argument("command", choices=["run", "migrate"])
    args = parser.parse_args()
    try:
        config = load(Path(os.environ.get("FW_CONTROL_CONFIG", "/etc/ferry-control/config.json")))
    except ConfigError as e:
        sys.exit(f"ferry-control: {e}")
    if args.command == "migrate":
        asyncio.run(run_migrate(config))
        return

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_control(config, stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
