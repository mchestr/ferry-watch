"""The admin API (docs/multi-tenant/design-reference.md §9.4), for admins only.

Every change needs a reason and writes the audit log in the same transaction. Admins manage people,
limits and the fleet's switches, and may release a held cart or recycle a pod; they never get a tenant's
screen, a shell or a screenshot, and see no tenant's sailings beyond the cart summary.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import asyncpg
from aiohttp import web

from ferry_watch.control.limits import CEILINGS, Limits
from ferry_watch.models import PACIFIC

from .accounts import INVITE_DAYS, IdentityPolicy, SignedIn
from .auth import ACCOUNTS, signed_in
from .http import ApiError, json_body

POOL = web.AppKey("admin_pool", asyncpg.Pool)
LIMITS = web.AppKey("admin_limits", Limits)
POLICY = web.AppKey("admin_policy", IdentityPolicy)
_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}")
_USER_ID = re.compile(r"[a-z0-9]{20}")
MAX_REASON = 500
CART_SUMMARY = ("status", "depart", "hard_deadline", "payment_in_progress")  # what the overview shows of a cart
ATTENTION_DAYS = 7
MAX_ATTENTION = 50
DELIVERY_FAILING_AFTER = 3  # attempts
ATTENTION_ALERTS = {  # alerts that are an admin's business, in plain words
    "release_unverified": "A released cart couldn't be confirmed empty",
    "checkout_blocked": "A checkout hit a blocked page",
}


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


async def _command(
    conn: asyncpg.pool.PoolConnectionProxy, actor: SignedIn, trip_id: str, op: str, cart_id: str | None = None
) -> None:
    """Queue a command for the trip's pod; ferry-control's hub passes it on."""
    await conn.execute(
        "INSERT INTO agent_commands (trip_id, op, cart_id, requested_by) VALUES ($1, $2, $3, $4)",
        trip_id,
        op,
        cart_id,
        f"admin:{actor.uid}",
    )


def _cart(cart: str | None) -> dict[str, Any]:
    """A trip's cart as its pod last reported it (`trip_state.cart`), or {} for none."""
    data = json.loads(cart) if cart else None
    return data if isinstance(data, dict) else {}


def _held_cart(cart: dict[str, Any]) -> str | None:
    """The id of the cart a trip's pod is holding. A pod releases only that: a cart still being added
    gets a new id once it's in, so a release sent for it would be dropped."""
    cart_id = cart.get("id")
    return cart_id if cart.get("status") == "ready" and isinstance(cart_id, str) and cart_id else None


def _paying(cart: dict[str, Any]) -> bool:
    """Whether they're paying for the cart now. Pulling it then could charge them for nothing."""
    return cart.get("payment_in_progress") is True


def _bootstrap(policy: IdentityPolicy, identities: Iterable[asyncpg.Record]) -> bool:
    """A bootstrap admin, by any of their sign-ins: the config makes them admin, not this API."""
    return any((i["iss"], i["username"]) in policy.bootstrap_admins for i in identities)


def _emails_seen(email: str | None, identities: Iterable[asyncpg.Record]) -> list[str]:
    """Emails their sign-ins carried other than the one on file (the account's email changed, or it changed hands)."""
    on_file = (email or "").lower()
    return sorted({i["email"] for i in identities if i["email"] and i["email"].lower() != on_file})


def _control_seen(alive: Any, fleet: dict[str, Any] | None) -> float | None:
    """When ferry-control was last heard from: its heartbeat if it keeps one, else its last fleet pass."""
    for seen in (alive, fleet.get("at") if fleet else None):
        if isinstance(seen, int | float) and not isinstance(seen, bool):
            return float(seen)
    return None


async def _attention(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """What needs an admin, newest first: ids, usernames and plain words, never sailings or anything typed."""

    def item(kind: str, row: asyncpg.Record, at: datetime, detail: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "trip": row["trip_id"],
            "user": row["user_id"],
            "username": row["username"],
            "at": at.timestamp(),
            "detail": detail,
        }

    items = [
        item("needs_attention", r, r["updated_at"], f"Its browser failed {r['failures']} times; checks are stopped")
        for r in await pool.fetch(
            "SELECT rt.trip_id, t.user_id, u.username, rt.failures, rt.updated_at FROM trip_runtime rt"
            " JOIN trips t ON t.id = rt.trip_id JOIN users u ON u.id = t.user_id"
            " WHERE rt.needs_attention AND t.archived_at IS NULL ORDER BY rt.updated_at DESC LIMIT $1",
            MAX_ATTENTION,
        )
    ]
    for r in await pool.fetch(
        "SELECT n.kind, n.trip_id, n.user_id, u.username, n.created_at, n.sent_at, n.attempts, n.last_error"
        " FROM notifications n LEFT JOIN users u ON u.id = n.user_id"
        " WHERE n.created_at > now() - make_interval(days => $1)"
        " AND (n.kind = ANY($2::text[]) OR (n.sent_at IS NULL AND n.attempts >= $3))"
        " ORDER BY n.created_at DESC LIMIT $4",
        ATTENTION_DAYS,
        list(ATTENTION_ALERTS),
        DELIVERY_FAILING_AFTER,
        MAX_ATTENTION,
    ):
        if r["kind"] in ATTENTION_ALERTS:
            items.append(item(r["kind"], r, r["created_at"], ATTENTION_ALERTS[r["kind"]]))
        if r["sent_at"] is None and r["attempts"] >= DELIVERY_FAILING_AFTER:
            why = f" ({r['last_error']})" if r["last_error"] else ""  # an exception's class name, nothing more
            detail = f"Not delivered after {r['attempts']} tries: {r['kind'].replace('_', ' ')}{why}"
            items.append(item("delivery_failing", r, r["created_at"], detail))
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:MAX_ATTENTION]


async def overview(request: web.Request) -> web.Response:
    admin(request)
    pool = request.app[POOL]
    pods = await pool.fetch("SELECT mode, phase, count(*) AS n FROM browser_pods WHERE ended_at IS NULL GROUP BY 1, 2")
    holds = await pool.fetch(
        "SELECT h.user_id, h.trip_id, h.pod, h.claimed_at, s.cart FROM hold_claims h LEFT JOIN trip_state s"
        " ON s.trip_id = h.trip_id ORDER BY h.claimed_at"
    )
    switches = {r["key"]: json.loads(r["value"]) for r in await pool.fetch("SELECT key, value FROM fleet_state")}
    fleet = switches.pop("fleet_report", None)  # what ferry-control's last pass knew; not a switch
    fleet = fleet if isinstance(fleet, dict) else None
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
                    "pod": r["pod"],
                    "since": r["claimed_at"].timestamp(),
                    # The cart summary only: no sailings, nothing the user typed.
                    "cart": {k: v for k, v in json.loads(r["cart"]).items() if k in CART_SUMMARY}
                    if r["cart"]
                    else None,
                }
                for r in holds
            ],
            "switches": switches,
            "outcomes_7d": {r["outcome"]: r["n"] for r in outcomes},
            "fleet": fleet,
            "control_seen": _control_seen(switches.get("control_alive_at"), fleet),
            "attention": await _attention(pool),
        }
    )


async def list_users(request: web.Request) -> web.Response:
    admin(request)
    pool, limits, policy = request.app[POOL], request.app[LIMITS], request.app[POLICY]
    users = await pool.fetch(
        "SELECT u.id, u.username, u.email, u.state, u.is_admin, u.created_at, u.last_seen_at,"
        " (SELECT count(*) FROM trips t WHERE t.user_id = u.id AND t.archived_at IS NULL) AS trips"
        " FROM users u ORDER BY u.created_at"
    )
    overrides: dict[str, dict[str, int]] = {}
    for r in await pool.fetch("SELECT user_id, key, value FROM user_limits"):
        overrides.setdefault(r["user_id"], {})[r["key"]] = r["value"]
    sessions = {
        r["user_id"]: r["n"]
        for r in await pool.fetch(
            "SELECT user_id, count(*) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at > now()"
            " AND idle_expires_at > now() GROUP BY 1"
        )
    }
    devices = {
        r["user_id"]: r["n"]
        for r in await pool.fetch("SELECT user_id, count(*) AS n FROM push_subscriptions GROUP BY 1")
    }
    usage: dict[str, dict[str, int]] = {}
    for r in await pool.fetch(
        "SELECT user_id, kind, amount FROM usage_ledger WHERE day = $1", datetime.now(PACIFIC).date()
    ):
        usage.setdefault(r["user_id"], {})[r["kind"]] = r["amount"]
    identities: dict[str, list[asyncpg.Record]] = {}
    for r in await pool.fetch("SELECT uid, iss, username, email FROM identities"):
        identities.setdefault(r["uid"], []).append(r)
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
                    "paused": False,  # for a release, for the old dashboard: trips are paused one at a time now
                    "trips": u["trips"],
                    "created_at": u["created_at"].timestamp(),
                    "last_seen_at": u["last_seen_at"].timestamp() if u["last_seen_at"] else None,
                    "limits": {
                        key: {"value": e.value, "source": e.source}
                        for key in CEILINGS
                        for e in [limits.effective(key, overrides.get(u["id"], {}).get(key))]
                    },
                    "sessions": sessions.get(u["id"], 0),
                    "devices": devices.get(u["id"], 0),
                    "usage_today": usage.get(u["id"], {}),
                    "bootstrap": _bootstrap(policy, identities.get(u["id"], [])),
                    "emails_seen": _emails_seen(u["email"], identities.get(u["id"], [])),
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
    """Suspend (revoking every session; ferry-control drains their pods) or reinstate a user.

    A pod holding their cart isn't drained: the admin chooses to release the cart now, or let the hold finish.
    Releasing leaves a cart they're paying for to finish, and recycles a pod still adding one (its drain empties
    the cart once it's in).
    """
    actor = admin(request)
    uid = _user_id(request)
    body = await json_body(request)
    reason = _reason(body)
    state, holds = body.get("state"), body.get("holds", "finish")
    if state not in ("active", "suspended"):
        raise ApiError(400, "active or suspended", "state")
    if holds not in ("release", "finish"):
        raise ApiError(400, "release or finish", "holds")
    if uid == actor.uid:
        raise ApiError(409, "You can't suspend yourself")
    after = {"state": state} | ({"holds": holds} if state == "suspended" else {})
    async with request.app[POOL].acquire() as conn, conn.transaction():
        before = await conn.fetchval("SELECT state FROM users WHERE id = $1 FOR UPDATE", uid)
        if before is None:
            raise ApiError(404, "No such user")
        await conn.execute("UPDATE users SET state = $2 WHERE id = $1", uid, state)
        if state == "suspended" and holds == "release":
            paying: list[str] = []
            for r in await conn.fetch(
                "SELECT s.trip_id, s.cart FROM trip_state s JOIN trips t ON t.id = s.trip_id"
                " WHERE t.user_id = $1 AND s.cart IS NOT NULL ORDER BY s.trip_id",
                uid,
            ):
                cart = _cart(r["cart"])
                cart_id = _held_cart(cart)
                if cart.get("status") == "adding":
                    # Its pod drains, emptying the cart once it's in; a suspended user's trip gets no next pod.
                    await _command(conn, actor, r["trip_id"], "recycle")
                elif cart_id is not None and _paying(cart):
                    paying.append(r["trip_id"])
                elif cart_id is not None:
                    await _command(conn, actor, r["trip_id"], "release", cart_id)
            after |= {"left_paying": paying} if paying else {}
        await _audit(conn, actor, "set_state", "user", uid, reason, before={"state": before}, after=after)
    if state == "suspended":
        await request.app[ACCOUNTS].revoke(uid=uid, reason="suspended")
    return web.json_response({"ok": True})


async def set_admin(request: web.Request) -> web.Response:
    """Make someone an admin, or stop them being one. Bootstrap admins are the config's to change."""
    actor = admin(request)
    uid = _user_id(request)
    body = await json_body(request)
    reason = _reason(body)
    is_admin = body.get("is_admin")
    if not isinstance(is_admin, bool):
        raise ApiError(400, "true or false", "is_admin")
    if uid == actor.uid:
        raise ApiError(409, "You can't change your own admin")
    async with request.app[POOL].acquire() as conn, conn.transaction():
        before = await conn.fetchval("SELECT is_admin FROM users WHERE id = $1 FOR UPDATE", uid)
        if before is None:
            raise ApiError(404, "No such user")
        identities = await conn.fetch("SELECT iss, username FROM identities WHERE uid = $1", uid)
        if not is_admin and _bootstrap(request.app[POLICY], identities):
            raise ApiError(409, "Bootstrap admins are set in the config")
        await conn.execute("UPDATE users SET is_admin = $2 WHERE id = $1", uid, is_admin)
        await _audit(
            conn, actor, "set_admin", "user", uid, reason, before={"is_admin": before}, after={"is_admin": is_admin}
        )
    return web.json_response({"is_admin": is_admin})


async def revoke_sessions(request: web.Request) -> web.Response:
    """Sign someone out everywhere. They may sign in again; suspending them stops that."""
    actor = admin(request)
    uid = _user_id(request)
    reason = _reason(await json_body(request))
    async with request.app[POOL].acquire() as conn, conn.transaction():
        if await conn.fetchval("SELECT 1 FROM users WHERE id = $1", uid) is None:
            raise ApiError(404, "No such user")
        revoked = await request.app[ACCOUNTS].revoke(uid=uid, reason="revoked by an admin", conn=conn)
        await _audit(conn, actor, "revoke_sessions", "user", uid, reason, after={"revoked": revoked})
    return web.json_response({"revoked": revoked})


async def delete_user(request: web.Request) -> web.Response:
    """Delete a suspended user and everything that's theirs. The audit log, hold outcomes and usage stay,
    under an id that no longer names anyone.

    ferry-control drains a pod whose trip is gone (the planner's "deleted"), but not one holding a cart, and
    an agent still reporting has no trip left to report about; so this waits until their browsers stopped.
    """
    actor = admin(request)
    uid = _user_id(request)
    reason = _reason(await json_body(request))
    if uid == actor.uid:
        raise ApiError(409, "You can't delete yourself")
    async with request.app[POOL].acquire() as conn, conn.transaction():
        user = await conn.fetchrow("SELECT username, state FROM users WHERE id = $1 FOR UPDATE", uid)
        if user is None:
            raise ApiError(404, "No such user")
        if user["state"] != "suspended":
            raise ApiError(409, "Suspend them first, so their browsers stop")
        if await conn.fetchval("SELECT 1 FROM browser_pods WHERE user_id = $1 AND ended_at IS NULL LIMIT 1", uid):
            raise ApiError(409, "Their browsers are still stopping; try again in a few minutes")
        await _audit(conn, actor, "delete_user", "user", uid, reason, before={"username": user["username"]})
        await conn.execute("DELETE FROM events WHERE user_id = $1", uid)
        # The rest goes with the row: identities, sessions, limits, trips and their state, devices,
        # notifications and hold claims.
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
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
    """Maintenance (no browsers) and pause carting (alert only, e.g. for a Talos upgrade) on or off; and closing
    the breaker, which only ferry-control opens."""
    actor = admin(request)
    body = await json_body(request)
    reason = _reason(body)
    key, on = body.get("switch"), body.get("on")
    if key not in ("maintenance", "pause_carting", "breaker") or not isinstance(on, bool):
        raise ApiError(400, "maintenance, pause_carting or breaker, on or off", "switch")
    if key == "breaker" and on:
        raise ApiError(400, "Only ferry-control opens the breaker", "switch")
    value = "closed" if key == "breaker" else on
    async with request.app[POOL].acquire() as conn, conn.transaction():
        await conn.execute("UPDATE fleet_state SET value = $2 WHERE key = $1", key, json.dumps(value))
        await _audit(conn, actor, "set_switch", "fleet", key, reason, after={key: value})
    return web.json_response({"ok": True})


async def release_cart(request: web.Request) -> web.Response:
    """Empty a trip's held cart. Its pod tells the owner, as when they release it themselves."""
    actor = admin(request)
    reason = _reason(await json_body(request))
    trip_id = request.match_info["trip_id"]
    async with request.app[POOL].acquire() as conn, conn.transaction():
        cart = _cart(await conn.fetchval("SELECT cart FROM trip_state WHERE trip_id = $1", trip_id))
        if cart.get("status") == "adding":
            raise ApiError(409, "Still being added; try again in a moment")
        if (cart_id := _held_cart(cart)) is None:
            raise ApiError(404, "Nothing is held")
        if _paying(cart):
            raise ApiError(409, "They're paying for it now")
        await _command(conn, actor, trip_id, "release", cart_id)
        await _audit(conn, actor, "release_cart", "trip", trip_id, reason, after={"cart_id": cart_id})
    return web.json_response({"ok": True})


async def recycle_pod(request: web.Request) -> web.Response:
    """Restart a trip's browser: the pod drains (handing back any cart) and the fleet starts the next generation."""
    actor = admin(request)
    reason = _reason(await json_body(request))
    trip_id = request.match_info["trip_id"]
    async with request.app[POOL].acquire() as conn, conn.transaction():
        pod = await conn.fetchval("SELECT name FROM browser_pods WHERE trip_id = $1 AND ended_at IS NULL", trip_id)
        if pod is None:
            raise ApiError(404, "No browser is running for this trip")
        await _command(conn, actor, trip_id, "recycle")
        await _audit(conn, actor, "recycle_pod", "trip", trip_id, reason, after={"pod": pod})
    return web.json_response({"ok": True})


def routes() -> list[web.RouteDef]:
    user = "/api/admin/users/{user_id}"
    trip = "/api/admin/trips/{trip_id:[a-f0-9]{16}}"
    return [
        web.get("/api/admin/overview", overview, allow_head=False),
        web.get("/api/admin/users", list_users, allow_head=False),
        web.get("/api/admin/unmatched", unmatched, allow_head=False),
        web.post("/api/admin/invites", invite),
        web.delete("/api/admin/invites/{invite_id:[a-f0-9]{16}}", revoke_invite),
        web.patch(user, set_admin),
        web.delete(user, delete_user),
        web.post(f"{user}/state", set_state),
        web.post(f"{user}/limits", set_limit),
        web.post(f"{user}/revoke-sessions", revoke_sessions),
        web.post("/api/admin/switches", set_switch),
        web.post(f"{trip}/release", release_cart),
        web.post(f"{trip}/recycle", recycle_pod),
    ]


def install(app: web.Application, pool: asyncpg.Pool, limits: Limits, policy: IdentityPolicy) -> None:
    app[POOL] = pool
    app[LIMITS] = limits
    app[POLICY] = policy
    app.router.add_routes(routes())
