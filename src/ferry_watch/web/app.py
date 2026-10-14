"""ferry-web's application: middleware order, sign-in routes and the per-user API."""

from __future__ import annotations

import asyncpg
from aiohttp import web

from ferry_watch.control.limits import Limits

from . import admin, api, auth
from .accounts import Accounts
from .api import CHECKOUT, STORE, CheckoutSigner
from .http import errors, same_origin, security_headers
from .identity import OidcClient
from .repo import WebStore


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
) -> web.Application:
    # Outermost first: headers on every answer, errors as JSON, then the session, then the write checks.
    app = web.Application(
        middlewares=[security_headers, errors, auth.require_session, same_origin], client_max_size=64 * 1024
    )
    app[auth.ACCOUNTS] = accounts
    app[auth.OIDC] = oidc
    app[STORE] = store
    app[CHECKOUT] = checkout
    app.router.add_routes(auth.routes())
    app.router.add_routes(api.routes())
    admin.install(app, pool, limits)
    app.router.add_get("/livez", livez, allow_head=False)
    return app
