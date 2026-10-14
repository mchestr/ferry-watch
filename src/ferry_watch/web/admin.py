"""The admin API (docs/multi-tenant/design-reference.md §9.4), for admins only.

Every change needs a reason and writes the audit log. Admins manage people, limits and the fleet's two
switches; they never get a tenant's screen, a shell or a screenshot, and see no tenant's sailings beyond
the cart summary.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

import asyncpg
from aiohttp import web

from ferry_watch.control.limits import CEILINGS, Limits

from .accounts import INVITE_DAYS, SignedIn
from .auth import ACCOUNTS, signed_in
from .http import ApiError, json_body

POOL = web.AppKey("admin_pool", asyncpg.Pool)
LIMITS = web.AppKey("admin_limits", Limits)
_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}")
_USER_ID = re.compile(r"[a-z0-9]{20}")
MAX_REASON = 500


def admin(request: web.Request) -> SignedIn:
    user = signed_in(request)
    if not user.is_admin:
        raise ApiError(403, "Admins only")
    return user


def _reason(body: dict[str, Any]) -> str:
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise ApiError(400, "Say why", "reason")
    return reason.strip()


def _user_id(request: web.Request) -> str:
    uid = request.match_info["user_id"]
    if not _USER_ID.fullmatch(uid):
        raise ApiError(404, "No such user")
    return uid


async def _audit(
    conn: asyncpg.pool.PoolConnectionProxy,
    actor: SignedIn,
    action: str,
    target_kind: str,
    target: str,
    reason: str,
    before: Any = None,
    after: Any = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log (actor_kind, actor, action, target_kind, target_id, before, after, reason)"
        " VALUES ('admin', $1, $2, $3, $4, $5, $6, $7)",
        actor.uid,
        action,
        target_kind,
        target,
        json.dumps(before) if before is not None else None,
        json.dumps(after) if after is not None else None,
        reason,
    )


async def overview(request: web.Request) -> web.Response:
    admin(request)
    pool = request.app[POOL]
    pods = await pool.fetch("SELECT mode, phase, count(*) AS n FROM browser_pods WHERE ended_at IS NULL GROUP BY 1, 2")
    holds = await pool.fetch(
        "SELECT h.user_id, h.trip_id, h.claimed_at, s.cart FROM hold_claims h LEFT JOIN trip_state s"
        " ON s.trip_id = h.trip_id ORDER BY h.claimed_at"
    )
    switches = {r["key"]: json.loads(r["value"]) for r in await pool.fetch("SELECT key, value FROM fleet_state")}
    queued = await pool.fetchval("SELECT count(*) FROM trip_runtime WHERE queue_position IS NOT NULL")
    outcomes = await pool.fetch(
        "SELECT outcome, count(*) AS n FROM hold_outcomes WHERE created_at > now() - interval '7 days' GROUP BY 1"
    )
    return web.json_response(
        {
            "pods": [{"mode": r["mode"], "phase": r["phase"], "count": r["n"]} for r in pods],
            "queued": queued,
            "holds": [
                {
                    "user": r["user_id"],
                    "trip": r["trip_id"],
                    "since": r["claimed_at"].timestamp(),
                    # The cart summary only: no sailings, nothing the user typed.
                    "cart": {
                        k: v for k, v in json.loads(r["cart"]).items() if k in ("status", "depart", "hard_deadline")
                    }
                    if r["cart"]
                    else None,
                }
                for r in holds
            ],
            "switches": switches,
            "outcomes_7d": {r["outcome"]: r["n"] for r in outcomes},
        }
    )


async def list_users(request: web.Request) -> web.Response:
    admin(request)
    pool, limits = request.app[POOL], request.app[LIMITS]
    users = await pool.fetch(
        "SELECT u.id, u.username, u.email, u.state, u.is_admin, u.paused, u.created_at, u.last_seen_at,"
        " (SELECT count(*) FROM trips t WHERE t.user_id = u.id AND t.archived_at IS NULL) AS trips"
        " FROM users u ORDER BY u.created_at"
    )
    overrides: dict[str, dict[str, int]] = {}
    for r in await pool.fetch("SELECT user_id, key, value FROM user_limits"):
        overrides.setdefault(r["user_id"], {})[r["key"]] = r["value"]
    invites = await pool.fetch(
        "SELECT id, match_value, email, reason, created_at, expires_at FROM invites"
        " WHERE used_at IS NULL AND expires_at > now() ORDER BY created_at"
    )
    return web.json_response(
        {
            "users": [
                {
                    "id": u["id"],
                    "username": u["username"],
                    "email": u["email"],
                    "state": u["state"],
                    "is_admin": u["is_admin"],
                    "paused": u["paused"],
                    "trips": u["trips"],
                    "created_at": u["created_at"].timestamp(),
                    "last_seen_at": u["last_seen_at"].timestamp() if u["last_seen_at"] else None,
                    "limits": {
                        key: {"value": e.value, "source": e.source}
                        for key in CEILINGS
                        for e in [limits.effective(key, overrides.get(u["id"], {}).get(key))]
                    },
                }
                for u in users
            ],
            "invites": [
                {
                    "id": i["id"],
                    "username": i["match_value"],
                    "email": i["email"],
                    "reason": i["reason"],
                    "expires_at": i["expires_at"].timestamp(),
                }
                for i in invites
            ],
        }
    )


async def unmatched(request: web.Request) -> web.Response:
    admin(request)
    rows = await request.app[POOL].fetch(
        "SELECT iss, preferred_username, email, first_seen, last_seen FROM unmatched_identities"
        " WHERE last_seen > now() - interval '30 days' ORDER BY last_seen DESC"
    )
    return web.json_response(
        [
            {
                "issuer": r["iss"],
                "username": r["preferred_username"],
                "email": r["email"],
                "last_seen": r["last_seen"].timestamp(),
            }
            for r in rows
        ]
    )


async def invite(request: web.Request) -> web.Response:
    actor = admin(request)
    body = await json_body(request)
    reason = _reason(body)
    username, email = body.get("username"), body.get("email")
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ApiError(400, "Their sign-in username", "username")
    if email is not None and (not isinstance(email, str) or not _EMAIL.fullmatch(email)):
        raise ApiError(400, "Not an email address", "email")
    limits = body.get("limits") or {}
    if (
        not isinstance(limits, dict)
        or set(limits) - set(CEILINGS)
        or not all(isinstance(v, int) and 0 <= v <= CEILINGS[k] for k, v in limits.items())
    ):
        raise ApiError(400, "Limits must be known keys within their ceilings", "limits")
    invite_id = secrets.token_hex(8)
    pool = request.app[POOL]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO invites (id, match_claim, match_value, email, limits, reason, created_by, expires_at)"
            " VALUES ($1, 'preferred_username', $2, $3, $4, $5, $6, now() + make_interval(days => $7))",
            invite_id,
            username,
            email,
            json.dumps(limits),
            reason,
            actor.uid,
            INVITE_DAYS,
        )
        await _audit(conn, actor, "invite", "invite", invite_id, reason, after={"username": username})
        if email is not None:
            await conn.execute(
                "INSERT INTO notifications (to_email, kind, params) VALUES ($1, 'invited', $2)",
                email,
                json.dumps({"username": username}),
            )
    return web.json_response({"id": invite_id}, status=201)


async def revoke_invite(request: web.Request) -> web.Response:
    actor = admin(request)
    reason = _reason(await json_body(request))
    invite_id = request.match_info["invite_id"]
    async with request.app[POOL].acquire() as conn, conn.transaction():
        result = await conn.execute("DELETE FROM invites WHERE id = $1 AND used_at IS NULL", invite_id)
        if result.endswith(" 0"):
            raise ApiError(404, "No such open invitation")
        await _audit(conn, actor, "revoke_invite", "invite", invite_id, reason)
    return web.json_response({"ok": True})


async def set_state(request: web.Request) -> web.Response:
    """Suspend (revoking every session; ferry-control drains their pods) or reinstate a user."""
    actor = admin(request)
    uid = _user_id(request)
    body = await json_body(request)
    reason = _reason(body)
    state = body.get("state")
    if state not in ("active", "suspended"):
        raise ApiError(400, "active or suspended", "state")
    if uid == actor.uid:
        raise ApiError(409, "You can't suspend yourself")
    async with request.app[POOL].acquire() as conn, conn.transaction():
        before = await conn.fetchval("SELECT state FROM users WHERE id = $1 FOR UPDATE", uid)
        if before is None:
            raise ApiError(404, "No such user")
        await conn.execute("UPDATE users SET state = $2 WHERE id = $1", uid, state)
        await _audit(conn, actor, "set_state", "user", uid, reason, before={"state": before}, after={"state": state})
    if state == "suspended":
        await request.app[ACCOUNTS].revoke(uid=uid, reason="suspended")
    return web.json_response({"ok": True})


async def set_limit(request: web.Request) -> web.Response:
    actor = admin(request)
    uid = _user_id(request)
    body = await json_body(request)
    reason = _reason(body)
    key, value = body.get("key"), body.get("value")
    if key not in CEILINGS:
        raise ApiError(400, "Not a limit", "key")
    if value is not None and (not isinstance(value, int) or value < 0):
        raise ApiError(400, "A whole number, or null for the default", "value")
    async with request.app[POOL].acquire() as conn, conn.transaction():
        if await conn.fetchval("SELECT 1 FROM users WHERE id = $1", uid) is None:
            raise ApiError(404, "No such user")
        before = await conn.fetchval("SELECT value FROM user_limits WHERE user_id = $1 AND key = $2", uid, key)
        if value is None:
            await conn.execute("DELETE FROM user_limits WHERE user_id = $1 AND key = $2", uid, key)
        else:
            await conn.execute(
                "INSERT INTO user_limits (user_id, key, value, set_by, reason) VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (user_id, key) DO UPDATE SET value = $3, set_by = $4, reason = $5, set_at = now()",
                uid,
                key,
                value,
                actor.uid,
                reason,
            )
        await _audit(conn, actor, "set_limit", "user", uid, reason, before={key: before}, after={key: value})
    effective = request.app[LIMITS].effective(key, value)  # never past the ceiling, whatever was asked
    return web.json_response({"value": effective.value, "source": effective.source})


async def set_switch(request: web.Request) -> web.Response:
    """Maintenance (no browsers) and pause carting (alert only, e.g. for a Talos upgrade)."""
    actor = admin(request)
    body = await json_body(request)
    reason = _reason(body)
    key, value = body.get("switch"), body.get("on")
    if key not in ("maintenance", "pause_carting") or not isinstance(value, bool):
        raise ApiError(400, "maintenance or pause_carting, on or off", "switch")
    async with request.app[POOL].acquire() as conn, conn.transaction():
        await conn.execute("UPDATE fleet_state SET value = $2 WHERE key = $1", key, json.dumps(value))
        await _audit(conn, actor, "set_switch", "fleet", key, reason, after={key: value})
    return web.json_response({"ok": True})


def routes() -> list[web.RouteDef]:
    user = "/api/admin/users/{user_id}"
    return [
        web.get("/api/admin/overview", overview, allow_head=False),
        web.get("/api/admin/users", list_users, allow_head=False),
        web.get("/api/admin/unmatched", unmatched, allow_head=False),
        web.post("/api/admin/invites", invite),
        web.delete("/api/admin/invites/{invite_id:[a-f0-9]{16}}", revoke_invite),
        web.post(f"{user}/state", set_state),
        web.post(f"{user}/limits", set_limit),
        web.post("/api/admin/switches", set_switch),
    ]


def install(app: web.Application, pool: asyncpg.Pool, limits: Limits) -> None:
    app[POOL] = pool
    app[LIMITS] = limits
    app.router.add_routes(routes())
