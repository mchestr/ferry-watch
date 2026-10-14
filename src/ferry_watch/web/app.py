"""ferry-web's application: middleware order, sign-in routes and the per-user API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
from aiohttp import web

from ferry_watch.bcferries.terminals import TerminalDirectory
from ferry_watch.control.limits import Limits

from . import admin, api, auth
from .accounts import Accounts
from .api import CHECKOUT, PUBLIC, STORE, TERMINALS, CheckoutSigner
from .http import errors, same_origin, security_headers
from .identity import OidcClient
from .live import LiveHub
from .repo import WebStore

LIVE = web.AppKey("live", LiveHub)


async def livez(_: web.Request) -> web.Response:
    return web.Response(text="ok\n")


def build(
    accounts: Accounts,
    oidc: OidcClient,
    store: WebStore,
    checkout: CheckoutSigner,
    *,
    pool: asyncpg.Pool,
    limits: Limits,
    terminals: TerminalDirectory | None = None,
    public: dict[str, Any] | None = None,
) -> web.Application:
    # Outermost first: headers on every answer, errors as JSON, then the session, then the write checks.
    app = web.Application(
        middlewares=[security_headers, errors, auth.require_session, same_origin], client_max_size=64 * 1024
    )
    app[auth.ACCOUNTS] = accounts
    app[auth.OIDC] = oidc
    app[STORE] = store
    app[CHECKOUT] = checkout
    if terminals is not None:
        app[TERMINALS] = terminals
    app[PUBLIC] = public or {}
    app.router.add_routes(auth.routes())
    app.router.add_routes(api.routes())
    admin.install(app, pool, limits, accounts.policy)
    app.router.add_get("/livez", livez, allow_head=False)
    hub = LiveHub(pool, accounts, store.snapshot)
    app[LIVE] = hub
    app.router.add_get("/api/ws", hub.serve, allow_head=False)

    async def change_feed(app: web.Application) -> AsyncIterator[None]:
        task = asyncio.create_task(app[LIVE].listen(), name="change feed")
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(change_feed)
    return app
