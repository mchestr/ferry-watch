"""waitlist-web's application: middleware order, sign-in routes and the per-user API."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
from aiohttp import web

from waitlist.bcferries.terminals import TerminalDirectory
from waitlist.control.limits import Limits

from . import admin, api, auth, watches
from .accounts import Accounts
from .api import CHECKOUT, PUBLIC, PUBLIC_KEYS, STORE, TERMINALS, CheckoutSigner
from .http import SITE_ORIGIN, errors, same_origin, security_headers
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
    origin: str | None = None,
) -> web.Application:
    """`origin` is the site's own (`baseUrl`): what a browser's Origin must be on a write or the live socket.
    `public` is what every state carries besides the user's own: `links` and `push`, nothing else."""
    if set(public or {}) - PUBLIC_KEYS:
        raise ValueError(f"Every state carries only {sorted(PUBLIC_KEYS)} besides the user's own")
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
    if origin is not None:
        app[SITE_ORIGIN] = origin
    app.router.add_routes(auth.routes())
    app.router.add_routes(api.routes())
    watches.install(app, store)
    admin.install(app, pool, limits, accounts.policy)
    app.router.add_get("/livez", livez, allow_head=False)
    # The same state as GET /api/state: the push key, the links and the trips' booking links included.
    hub = LiveHub(pool, accounts, functools.partial(api.user_state, app))
    app[LIVE] = hub
    app.router.add_get("/api/ws", hub.serve, allow_head=False)

    async def change_feed(app: web.Application) -> AsyncIterator[None]:
        task = asyncio.create_task(app[LIVE].listen(), name="change feed")
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(change_feed)
    return app
