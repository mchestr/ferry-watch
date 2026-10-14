"""ferry-web's reads and writes, always for one user (docs/multi-tenant/design-reference.md §6.3, §9.2).

Every query that touches a trip names the user too, so a trip that belongs to someone else is
indistinguishable from one that doesn't exist. Limits are checked in the same transaction as the write,
with the user's row locked, so two requests can't both slip under a limit.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from ferry_watch.control.limits import LimitKey, Limits
from ferry_watch.models import PACIFIC, Trip, ValidationError
from ferry_watch.polling import PollPolicy

from .http import ApiError

AFTER_CHECKOUT = "after_checkout"
BOOKED_REASONS = frozenset(
    {"needed_account", "tried_sign_in", "payment_failed", "checkout_broke", "ran_out_of_time", "changed_mind"}
)
MANUAL_CHECK_WINDOW_S = 3600
MINT_WINDOW_S = 60


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _epoch(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _today(now: float) -> date:
    return datetime.fromtimestamp(now, PACIFIC).date()


@dataclass(frozen=True, slots=True)
class HeldCart:
    """A user's held cart and the pod holding it: what a checkout ticket binds to."""

    trip: Trip
    cart_id: str
    pod: str
    gen: int
    depart: str
    fare: str | None
    hard_deadline: float
    session_s: int


class WebStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        limits: Limits,
        policy: PollPolicy,
        *,
        clock: Callable[[], float],
    ) -> None:
        self._pool = pool
        self._limits = limits
        self._policy = policy
        self._clock = clock

    # ---- limits

    async def overrides(self, conn: asyncpg.pool.PoolConnectionProxy | asyncpg.Pool, uid: str) -> dict[str, int]:
        rows = await conn.fetch("SELECT key, value FROM user_limits WHERE user_id = $1", uid)
        return {r["key"]: r["value"] for r in rows}

    async def limit(self, conn: asyncpg.pool.PoolConnectionProxy | asyncpg.Pool, uid: str, key: LimitKey) -> int:
        return self._limits.value(key, await self.overrides(conn, uid))

    # ---- trips

    async def trip(self, uid: str, trip_id: str) -> tuple[Trip, int]:
        row = await self._pool.fetchrow(
            "SELECT spec, enabled, rev FROM trips WHERE id = $1 AND user_id = $2 AND archived_at IS NULL",
            trip_id,
            uid,
        )
        if row is None:
            raise ApiError(404, "No such trip")
        return Trip.from_json(json.loads(row["spec"]) | {"enabled": row["enabled"]}), row["rev"]

    def _check_date(self, trip: Trip) -> None:
        if not self._policy.within_horizon(trip.date, _today(self._clock())):
            days = self._policy.horizon.days
            raise ValidationError("date", f"Pick a date from today to {days} days ahead")

    async def add_trip(self, uid: str, data: dict[str, Any]) -> Trip:
        data = {k: v for k, v in data.items() if k != "id"} | {"id": secrets.token_hex(8)}
        trip = Trip.from_json(data)
        self._check_date(trip)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid)
            limit = await self.limit(conn, uid, "maxTrips")
            count = await conn.fetchval("SELECT count(*) FROM trips WHERE user_id = $1 AND archived_at IS NULL", uid)
            if count >= limit:
                raise ApiError(409, f"You can watch up to {limit} trips", "trips", limit=limit, usage=count)
            await conn.execute(
                "INSERT INTO trips (id, user_id, spec, enabled) VALUES ($1, $2, $3, $4)",
                trip.id,
                uid,
                json.dumps(trip.to_json()),
                trip.enabled,
            )
            await conn.execute("INSERT INTO trip_runtime (trip_id) VALUES ($1)", trip.id)  # a short check, now
        return trip

    async def update_trip(self, uid: str, trip_id: str, data: dict[str, Any]) -> Trip:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT spec, enabled FROM trips WHERE id = $1 AND user_id = $2 AND archived_at IS NULL FOR UPDATE",
                trip_id,
                uid,
            )
            if row is None:
                raise ApiError(404, "No such trip")
            current = Trip.from_json(json.loads(row["spec"]) | {"enabled": row["enabled"]})
            trip = Trip.from_json(data, base=current)
            if trip.date != current.date:
                self._check_date(trip)
            await conn.execute(
                "UPDATE trips SET spec = $3, enabled = $4, rev = rev + 1, updated_at = now()"
                " WHERE id = $1 AND user_id = $2",
                trip_id,
                uid,
                json.dumps(trip.to_json()),
                trip.enabled,
            )
            if trip.search_key() != current.search_key():
                await conn.execute("DELETE FROM alert_memory WHERE trip_id = $1", trip_id)  # a new search
            if trip.enabled and not current.enabled:
                await conn.execute("UPDATE trip_runtime SET next_check_at = NULL WHERE trip_id = $1", trip_id)
        return trip

    async def archive_trip(self, uid: str, trip_id: str) -> None:
        """Stop watching. ferry-control drains its pod, which hands back any cart first."""
        result = await self._pool.execute(
            "UPDATE trips SET archived_at = now(), enabled = false, rev = rev + 1"
            " WHERE id = $1 AND user_id = $2 AND archived_at IS NULL",
            trip_id,
            uid,
        )
        if result.endswith(" 0"):
            raise ApiError(404, "No such trip")

    # ---- what users ask of their browser

    async def command(self, uid: str, trip_id: str, op: str, cart_id: str | None = None) -> None:
        await self.trip(uid, trip_id)
        await self._pool.execute(
            "INSERT INTO agent_commands (trip_id, op, cart_id, requested_by) VALUES ($1, $2, $3, $4)",
            trip_id,
            op,
            cart_id,
            uid,
        )

    async def check_now(self, uid: str, trip_id: str) -> None:
        """A manual check: the warm browser checks at once; any other trip gets a short check now."""
        await self.trip(uid, trip_id)
        now = self._clock()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid)
            limit = await self.limit(conn, uid, "manualChecksPerHour")
            recent = await conn.fetchval(
                "SELECT count(*) FROM agent_commands"
                " WHERE requested_by = $1 AND op = 'check_now' AND created_at > $2",
                uid,
                _ts(now - MANUAL_CHECK_WINDOW_S),
            )
            if recent >= limit:
                raise ApiError(429, f"Up to {limit} checks an hour", limit=limit, usage=recent)
            warm = await conn.fetchval(
                "SELECT 1 FROM browser_pods WHERE trip_id = $1 AND mode = 'warm' AND ended_at IS NULL", trip_id
            )
            await conn.execute(
                "INSERT INTO agent_commands (trip_id, op, requested_by, consumed_at) VALUES ($1, 'check_now', $2, $3)",
                trip_id,
                uid,
                None if warm else _ts(now),  # no warm pod: recorded for the limit; the short check is the action
            )
            if not warm:
                await conn.execute("UPDATE trip_runtime SET next_check_at = $2 WHERE trip_id = $1", trip_id, _ts(now))

    async def answer_booked(self, uid: str, trip_id: str, booked: bool, reason: str | None) -> None:
        """ "Did you book it?": yes archives the trip, no resumes it."""
        if reason is not None and reason not in BOOKED_REASONS:
            raise ValidationError("reason", "Not a reason we know")
        async with self._pool.acquire() as conn, conn.transaction():
            paused = await conn.fetchval(
                "SELECT paused_reason FROM trips WHERE id = $1 AND user_id = $2 AND archived_at IS NULL FOR UPDATE",
                trip_id,
                uid,
            )
            if paused != AFTER_CHECKOUT:
                raise ApiError(409, "There's no checkout to answer for")
            if booked:
                await conn.execute(
                    "UPDATE trips SET archived_at = now(), paused_reason = NULL, rev = rev + 1 WHERE id = $1", trip_id
                )
            else:
                await conn.execute(
                    "UPDATE trips SET paused_reason = NULL, rev = rev + 1, updated_at = now() WHERE id = $1", trip_id
                )
                await conn.execute("UPDATE trip_runtime SET next_check_at = NULL WHERE trip_id = $1", trip_id)
            await conn.execute(
                "UPDATE hold_outcomes SET answer_reason = $3 WHERE id = (SELECT id FROM hold_outcomes"
                " WHERE trip_id = $1 AND user_id = $2 ORDER BY created_at DESC LIMIT 1)",
                trip_id,
                uid,
                "booked" if booked else (reason or "not_booked"),
            )

    async def set_paused(self, uid: str, paused: bool) -> None:
        await self._pool.execute("UPDATE users SET paused = $2 WHERE id = $1", uid, paused)

    # ---- checkout tickets

    async def held_cart(self, uid: str, trip_id: str) -> HeldCart:
        """The user's held cart on this trip, if a checkout may be opened for it now."""
        trip, _ = await self.trip(uid, trip_id)
        row = await self._pool.fetchrow(
            "SELECT s.cart, s.pod, p.gen FROM trip_state s JOIN browser_pods p ON p.name = s.pod"
            " WHERE s.trip_id = $1 AND p.ended_at IS NULL AND p.user_id = $2",
            trip_id,
            uid,
        )
        cart = json.loads(row["cart"]) if row is not None and row["cart"] else None
        if row is None or cart is None or cart.get("status") != "ready":
            raise ApiError(409, "There's no cart held for this trip")
        hard_deadline = float(cart.get("hard_deadline") or 0)
        if hard_deadline <= self._clock():
            raise ApiError(409, "This hold has ended")
        session_deadline = cart.get("session_deadline")
        return HeldCart(
            trip=trip,
            cart_id=cart["id"],
            pod=row["pod"],
            gen=row["gen"],
            depart=cart["depart"],
            fare=cart.get("fare"),
            hard_deadline=hard_deadline,
            session_s=int(session_deadline - self._clock()) if session_deadline else 0,
        )

    async def record_mint(self, uid: str, held: HeldCart, jti: str) -> None:
        """Count a checkout ticket against the user's limits (10 a minute, and VNC minutes left)."""
        now = self._clock()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid)
            per_minute = await self.limit(conn, uid, "mintsPerMinute")
            recent = await conn.fetchval(
                "SELECT count(*) FROM vnc_mints WHERE user_id = $1 AND created_at > $2", uid, _ts(now - MINT_WINDOW_S)
            )
            if recent >= per_minute:
                raise ApiError(429, "Too many tries; wait a minute")
            minutes = await self.limit(conn, uid, "vncMinutesPerDay")
            used = await conn.fetchval(
                "SELECT coalesce(sum(amount), 0) FROM usage_ledger"
                " WHERE user_id = $1 AND day = $2 AND kind = 'vnc_seconds'",
                uid,
                _today(now),
            )
            if used >= minutes * 60:
                raise ApiError(409, "You've used today's checkout time")
            await conn.execute(
                "INSERT INTO vnc_mints (jti, user_id, trip_id, pod, aud, cart_id, created_at)"
                " VALUES ($1, $2, $3, $4, 'fw-vnc', $5, $6)",
                jti,
                uid,
                held.trip.id,
                held.pod,
                held.cart_id,
                _ts(now),
            )

    # ---- what the dashboard shows

    async def snapshot(self, uid: str) -> dict[str, Any]:
        now = self._clock()
        user = await self._pool.fetchrow("SELECT id, username, display, is_admin, paused FROM users WHERE id = $1", uid)
        if user is None:
            raise ApiError(404, "No such user")
        overrides = await self.overrides(self._pool, uid)
        usage = {
            r["kind"]: r["amount"]
            for r in await self._pool.fetch(
                "SELECT kind, amount FROM usage_ledger WHERE user_id = $1 AND day = $2", uid, _today(now)
            )
        }
        rows = await self._pool.fetch(
            "SELECT t.id, t.spec, t.enabled, t.paused_reason, t.rev, rt.phase, rt.mode, rt.queue_position,"
            " rt.next_check_at, rt.last_check_at, s.agent_phase, s.checked_at, s.next_due, s.effective_interval,"
            " s.sailings, s.error_class, s.consecutive_errors, s.cart, s.viewer_connected, s.heartbeat_at"
            " FROM trips t LEFT JOIN trip_runtime rt ON rt.trip_id = t.id LEFT JOIN trip_state s ON s.trip_id = t.id"
            " WHERE t.user_id = $1 AND t.archived_at IS NULL ORDER BY t.created_at",
            uid,
        )
        trips = []
        for r in rows:
            spec = json.loads(r["spec"]) | {"enabled": r["enabled"]}
            trips.append(
                {
                    "trip": spec,
                    "rev": r["rev"],
                    "paused_reason": r["paused_reason"],
                    "browser": {
                        "phase": r["agent_phase"] or r["phase"] or "off",
                        "mode": r["mode"] or "off",
                        "queue_position": r["queue_position"],
                        "next_check_at": _epoch(r["next_check_at"]),
                        "last_check_at": _epoch(r["last_check_at"]),
                        "checked_at": _epoch(r["checked_at"]),
                        "next_due": _epoch(r["next_due"]),
                        "effective_interval": r["effective_interval"],
                        "heartbeat_at": _epoch(r["heartbeat_at"]),
                        "sailings": json.loads(r["sailings"]) if r["sailings"] else [],
                        "error_class": r["error_class"],
                        "consecutive_errors": r["consecutive_errors"] or 0,
                        "cart": json.loads(r["cart"]) if r["cart"] else None,
                        "viewer_connected": bool(r["viewer_connected"]),
                    },
                }
            )
        shown: tuple[LimitKey, ...] = (
            "maxTrips",
            "maxActiveBrowsers",
            "maxHoldsPerDay",
            "vncMinutesPerDay",
            "manualChecksPerHour",
        )
        limits: dict[str, dict[str, Any]] = {}
        for key in shown:
            effective = self._limits.effective(key, overrides.get(key))
            limits[key] = {"value": effective.value, "source": effective.source}
        events = await self._pool.fetch(
            "SELECT id, trip_id, ts, kind, code, params FROM events WHERE user_id = $1 ORDER BY ts DESC LIMIT 50", uid
        )
        max_trips = limits["maxTrips"]["value"]
        at = datetime.fromtimestamp(now, PACIFIC)
        return {
            "me": {
                "id": user["id"],
                "username": user["username"],
                "display": user["display"],
                "is_admin": user["is_admin"],
                "paused": user["paused"],
            },
            "limits": limits,
            "usage": usage,
            "trips": trips,
            "capacity": {
                "can_add": len(trips) < max_trips,
                "reason": None if len(trips) < max_trips else f"You can watch up to {max_trips} trips",
            },
            "quiet_hours": {
                "start": self._policy.quiet_start.strftime("%H:%M"),
                "end": self._policy.quiet_end.strftime("%H:%M"),
                "now": self._policy.quiet(at),
            },
            "events": [
                {
                    "id": e["id"],
                    "trip_id": e["trip_id"],
                    "ts": e["ts"].timestamp(),
                    "kind": e["kind"],
                    "code": e["code"],
                    "params": json.loads(e["params"]),
                }
                for e in events
            ],
        }
