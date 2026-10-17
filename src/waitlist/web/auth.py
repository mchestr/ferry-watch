"""waitlist-web's sign-in routes and the session check on every API call (design-reference §6.2.2).

- `GET /auth/config`: what the SPA needs to start a sign-in (nothing secret).
- `POST /auth/session`: the SPA brings back the code; the answer is a bearer token, once.
- `POST /auth/logout`, `POST /auth/logout-everywhere`.

Every `/api/*` route needs `Authorization: Bearer <token>` for an active user: 401 with a reason
(missing, expired, reverify, revoked) otherwise. Nothing here ever sets a cookie.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .accounts import Accounts, Refused, SignedIn
from .identity import OidcClient, SignInError

log = logging.getLogger(__name__)

USER: web.AppKey[SignedIn] = web.AppKey("user", SignedIn)  # set on the request by the middleware
ACCOUNTS = web.AppKey("accounts", Accounts)
OIDC = web.AppKey("oidc", OidcClient)
PUBLIC_API = frozenset({"/api/ws"})  # authenticates with its first message instead
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")  # RFC 7636
_OPAQUE = re.compile(r"[A-Za-z0-9._~-]{1,512}")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def bearer(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header.removeprefix("Bearer ").strip() or None if header.startswith("Bearer ") else None


def signed_in(request: web.Request) -> SignedIn:
    user = request.get("user")
    if not isinstance(user, SignedIn):
        raise web.HTTPUnauthorized
    return user


@web.middleware
async def require_session(request: web.Request, handler: Handler) -> web.StreamResponse:
    if request.path.startswith("/api/") and request.path not in PUBLIC_API:
        result = await request.app[ACCOUNTS].resolve(bearer(request))
        if not isinstance(result, SignedIn):
            return web.json_response({"reason": result}, status=401)
        request["user"] = result
    return await handler(request)


async def auth_config(request: web.Request) -> web.Response:
    try:
        return web.json_response(await request.app[OIDC].public_config())
    except SignInError as e:
        return web.json_response({"error": e.reason}, status=503)


async def create_session(request: web.Request) -> web.Response:
    body = await _json(request)
    code, verifier, nonce = body.get("code"), body.get("code_verifier"), body.get("nonce")
    if not (isinstance(code, str) and _OPAQUE.fullmatch(code)):
        raise web.HTTPBadRequest(text="code")
    if not (isinstance(verifier, str) and _VERIFIER.fullmatch(verifier)):
        raise web.HTTPBadRequest(text="code_verifier")
    if not (isinstance(nonce, str) and _OPAQUE.fullmatch(nonce)):
        raise web.HTTPBadRequest(text="nonce")
    accounts = request.app[ACCOUNTS]
    try:
        principal = await request.app[OIDC].sign_in(code, verifier, nonce)
        uid = await accounts.admit(principal)
    except SignInError as e:
        return web.json_response({"error": e.reason}, status=400)
    except Refused as e:
        return web.json_response({"error": e.reason}, status=403)
    # Re-verification: the old token is rotated out, if it was this user's.
    old = bearer(request)
    if old is not None and await accounts.owner_of(old) == uid:
        await accounts.revoke(token=old, reason="rotated")
    token = await accounts.create_session(uid, principal, _client_label(request.headers.get("User-Agent", "")))
    log.info("Signed in %s", uid)
    return web.json_response({"token": token, "user": uid})


async def logout(request: web.Request) -> web.Response:
    token = bearer(request)
    if token is not None:
        await request.app[ACCOUNTS].revoke(token=token, reason="logout")
    return web.json_response({"ok": True})


async def logout_everywhere(request: web.Request) -> web.Response:
    accounts = request.app[ACCOUNTS]
    result = await accounts.resolve(bearer(request))
    if not isinstance(result, SignedIn):
        return web.json_response({"reason": result}, status=401)
    count = await accounts.revoke(uid=result.uid, reason="sign out everywhere")
    return web.json_response({"revoked": count})


def routes() -> list[web.RouteDef]:
    return [
        web.get("/auth/config", auth_config, allow_head=False),
        web.post("/auth/session", create_session),
        web.post("/auth/logout", logout),
        web.post("/auth/logout-everywhere", logout_everywhere),
    ]


async def _json(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Not JSON") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Not an object")
    return body


def _client_label(user_agent: str) -> str:
    """A coarse label for the Account card ("iPhone · Safari"), never the full user agent."""
    device = next((d for d in ("iPhone", "iPad", "Android", "Mac", "Windows", "Linux") if d in user_agent), "Browser")
    browser = next((b for b in ("Firefox", "Edg", "Chrome", "Safari") if b in user_agent), "")
    return f"{device} · {browser.replace('Edg', 'Edge')}" if browser else device
