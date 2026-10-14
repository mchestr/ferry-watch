"""ferry-web's reads and writes, always for one user (docs/multi-tenant/design-reference.md §6.3, §9.2).

Every query that touches a trip names the user too, so a trip that belongs to someone else is
indistinguishable from one that doesn't exist. Limits are checked in the same transaction as the write,
with the user's row locked, so two requests can't both slip under a limit.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as clock_time
from typing import Any

import asyncpg

from ferry_watch.control.configure import Allowance, HubSettings, Switches, cart_gate
from ferry_watch.control.limits import LimitKey, Limits
from ferry_watch.control.store import CLAIM_LAPSE_SECONDS, USER_CHANNEL
from ferry_watch.models import PACIFIC, Trip, ValidationError
from ferry_watch.polling import PollPolicy, QuietHours, quiet_hours

from .http import ApiError
from .push import Subscription

AFTER_CHECKOUT = "after_checkout"
BOOKED_REASONS = frozenset(
    {"needed_account", "tried_sign_in", "payment_failed", "checkout_broke", "ran_out_of_time", "changed_mind"}
)
MANUAL_CHECK_WINDOW_S = 3600
MINT_WINDOW_S = 60
HOLD_REQUEST_S = 600  # how long "Hold it for me" lets the trip's pod cart through quiet hours
QUIET_LIFT_S = 600  # how long a manual check lifts the user's quiet hours for the trip
# Why a hold the user asks for can't happen now, in their words.
HOLD_REFUSED = {
    "carting_paused": "Holding carts is paused right now",
    "holding_elsewhere": "You're already holding a cart on another trip",
    "no_holds_left": "You've used today's holds",
    "limit": "You've reached today's limit for holding carts",
    "already_held": "It was held for you once already; book it yourself on bcferries.com",
    "paused": "This trip is paused",
}


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _epoch(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _today(now: float) -> date:
    return datetime.fromtimestamp(now, PACIFIC).date()


async def _archive(conn: asyncpg.pool.PoolConnectionProxy, uid: str, trip_id: str) -> bool:
    """Stop watching a trip (removed, booked, or "No, cancel"), in the caller's transaction. A new rev, so its pod
    hears at once; ferry-control drains it, which hands back any cart first. False: no such live trip."""
    result = await conn.execute(
        "UPDATE trips SET archived_at = now(), enabled = false, paused_reason = NULL, rev = rev + 1, updated_at = now()"
        " WHERE id = $1 AND user_id = $2 AND archived_at IS NULL",
        trip_id,
        uid,
    )
    if result.endswith(" 0"):
        return False
    await conn.execute("UPDATE trip_runtime SET hold_requested_until = NULL WHERE trip_id = $1", trip_id)
    return True


def _quiet_json(quiet: QuietHours | None) -> dict[str, Any] | None:
    """Quiet hours as the API has them: {"start": "HH:MM", "end": "HH:MM"}, or None for none."""
    if quiet is None:
        return None
    return {"start": quiet.start.strftime("%H:%M"), "end": quiet.end.strftime("%H:%M")}


@dataclass(frozen=True, slots=True)
class HoldContext:
    """What decides whether this user's trips may hold a cart now (ferry-control's gate decides for real).

    ferry-web doesn't see ferry-control's own settings (fleet-wide auto-cart, alert-only fares, the session
    length), so it takes the defaults; at worst a hold it offers is refused and the alert says why.
    """

    allowance: Allowance  # with the user's quiet hours
    switches: Switches
    claimed_trip: str | None  # the trip holding the user's one cart claim, if any
    settings: HubSettings
    at: datetime

    def quiet(self, lifted_until: float | None, now: float) -> bool:
        """Whether a trip isn't checked now for the user's quiet hours: they're on, and not lifted for it."""
        return self.allowance.quiet_now(self.at) and not (lifted_until is not None and now < lifted_until)

    def hold(
        self,
        trip: Trip,
        *,
        paused: bool,
        quiet: bool,
        sailings: list[dict[str, Any]],
        alerted: Iterable[str],
        requested_until: float | None,
        now: float,
    ) -> dict[str, Any]:
        """Why auto-hold won't hold this trip's sailings now (None: it would), whether asking would help, and
        which bookable departures a hold could still be for (`holdable`). `quiet`: in the user's quiet hours,
        not lifted for this trip."""
        if paused:  # nothing is watching it: what the last check saw is history
            return {
                "reason": "paused",
                "can_request": False,
                "holdable": [],
                "requested_at": None,
                "requested_until": None,
            }
        bookable = bookable_in_window(trip, sailings)
        # A departure held once (then released, expired or viewed) is never carted again by the pod.
        holdable = [d for d in bookable if d not in set(alerted)]
        # What a hold asked for now would face: asking lifts quiet hours.
        blocked: str = cart_gate(trip, self.allowance, self.switches, self.settings, self.at, lifted=True)
        if blocked == "ok" and self.claimed_trip not in (None, trip.id):
            blocked = "holding_elsewhere"
        if blocked == "ok" and bookable and not holdable:
            blocked = "already_held"
        reason: str | None = blocked
        if blocked == "ok":
            reason = "quiet_hours" if quiet else None
            if reason is None and not trip.auto_cart:
                reason = "auto_cart_off"
        active = requested_until is not None and requested_until > now
        return {
            "reason": reason,
            "can_request": blocked == "ok",
            "holdable": holdable,
            "requested_at": requested_until - HOLD_REQUEST_S if active and requested_until else None,
            "requested_until": requested_until if active else None,
        }


def bookable_in_window(trip: Trip, sailings: list[dict[str, Any]]) -> list[str]:
    """The departures the trip's latest check found open, inside its window ("HH:MM")."""
    found = []
    for s in sailings:
        try:
            if s.get("status") == "available" and trip.in_window(clock_time.fromisoformat(s["depart"])):
                found.append(s["depart"])
        except (KeyError, TypeError, ValueError):
            continue  # not a sailing as the protocol sends them; never worth failing the whole state for
    return found


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

    async def notify(self, uid: str) -> None:
        """Wake this user's other dashboards (LISTEN fw_user)."""
        await self._pool.execute("SELECT pg_notify($1, $2)", USER_CHANNEL, uid)

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
            # A hold asked for was for the trip as it was; asked again if still wanted.
            await conn.execute("UPDATE trip_runtime SET hold_requested_until = NULL WHERE trip_id = $1", trip_id)
            if trip.enabled and not current.enabled:
                await conn.execute("UPDATE trip_runtime SET next_check_at = NULL WHERE trip_id = $1", trip_id)
        return trip

    async def archive_trip(self, uid: str, trip_id: str) -> None:
        """Stop watching. ferry-control drains its pod, which hands back any cart first."""
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _archive(conn, uid, trip_id):
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
        """A manual check: the warm browser checks at once; any other trip gets a short check now. It lifts the
        user's quiet hours for the trip for the next few minutes, so the check happens and may hold a cart."""
        await self.trip(uid, trip_id)
        now = self._clock()
        async with self._pool.acquire() as conn, conn.transaction():
            await self._check_now(conn, uid, trip_id, now)
            await conn.execute(
                "UPDATE trip_runtime SET quiet_lifted_until = $2 WHERE trip_id = $1", trip_id, _ts(now + QUIET_LIFT_S)
            )

    async def request_hold(self, uid: str, trip_id: str) -> None:
        """ "Hold it for me": the trip's pod may cart through quiet hours (and with the trip's auto-cart off)
        for the next few minutes, and checks now. ferry-control's gate still decides; every other limit stays.
        """
        trip, _ = await self.trip(uid, trip_id)
        now = self._clock()
        row = await self._pool.fetchrow(
            "SELECT t.paused_reason, s.sailings, s.cart, m.alerted FROM trips t"
            " LEFT JOIN trip_state s ON s.trip_id = t.id LEFT JOIN alert_memory m ON m.trip_id = t.id"
            " WHERE t.id = $1 AND t.user_id = $2",
            trip_id,
            uid,
        )
        if row is None:
            raise ApiError(404, "No such trip")
        if not trip.enabled or row["paused_reason"] is not None:  # paused, or waiting on "Did you book it?"
            raise ApiError(409, HOLD_REFUSED["paused"])
        cart = json.loads(row["cart"]) if row["cart"] else None
        if cart is not None and cart.get("status") in ("adding", "ready"):
            raise ApiError(409, "A cart is already being held for this trip")
        sailings = json.loads(row["sailings"]) if row["sailings"] else []
        if not bookable_in_window(trip, sailings):
            raise ApiError(409, "Nothing in your times is bookable right now")
        hold = (await self.hold_context(uid, now)).hold(
            trip,
            paused=False,
            quiet=False,  # asking lifts quiet hours
            sailings=sailings,
            alerted=row["alerted"] or (),
            requested_until=None,
            now=now,
        )
        if not hold["can_request"]:
            raise ApiError(409, HOLD_REFUSED.get(hold["reason"], "A cart can't be held right now"))
        async with self._pool.acquire() as conn, conn.transaction():
            await self._check_now(conn, uid, trip_id, now)
            await conn.execute(
                "UPDATE trip_runtime SET hold_requested_until = $2 WHERE trip_id = $1",
                trip_id,
                _ts(now + HOLD_REQUEST_S),
            )

    async def hold_context(self, uid: str, now: float) -> HoldContext:
        overrides = await self.overrides(self._pool, uid)
        usage = {
            r["kind"]: r["amount"]
            for r in await self._pool.fetch(
                "SELECT kind, amount FROM usage_ledger WHERE user_id = $1 AND day = $2", uid, _today(now)
            )
        }
        switches = {
            r["key"]: json.loads(r["value"])
            for r in await self._pool.fetch(
                "SELECT key, value FROM fleet_state WHERE key IN ('maintenance', 'pause_carting')"
            )
        }
        return HoldContext(
            allowance=Allowance(
                holds_per_day=self._limits.value("maxHoldsPerDay", overrides),
                cart_attempts_per_day=self._limits.value("maxCartAttemptsPerDay", overrides),
                vnc_minutes_per_day=self._limits.value("vncMinutesPerDay", overrides),
                holds_today=usage.get("holds", 0),
                cart_attempts_today=usage.get("cart_attempts", 0),
                vnc_seconds_today=usage.get("vnc_seconds", 0),
                quiet=await self.user_quiet_hours(uid),
            ),
            switches=Switches(
                maintenance=bool(switches.get("maintenance", False)),
                pause_carting=bool(switches.get("pause_carting", False)),
            ),
            claimed_trip=await self._pool.fetchval(  # a claim that lapsed doesn't count, as in ferry-control
                "SELECT trip_id FROM hold_claims WHERE user_id = $1"
                " AND (cart_id IS NOT NULL OR claimed_at > now() - make_interval(secs => $2))",
                uid,
                CLAIM_LAPSE_SECONDS,
            ),
            settings=HubSettings(policy=self._policy),
            at=datetime.fromtimestamp(now, PACIFIC),
        )

    async def _check_now(self, conn: asyncpg.pool.PoolConnectionProxy, uid: str, trip_id: str, now: float) -> None:
        """The manual check itself, in the caller's transaction (limited per hour)."""
        await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid)
        limit = await self.limit(conn, uid, "manualChecksPerHour")
        recent = await conn.fetchval(
            "SELECT count(*) FROM agent_commands" " WHERE requested_by = $1 AND op = 'check_now' AND created_at > $2",
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

    async def answer_booked(
        self, uid: str, trip_id: str, booked: bool, reason: str | None, *, stop: bool = False
    ) -> None:
        """ "Did you book it?": yes archives the trip, no resumes it, and "No, cancel" (`stop`) archives it too."""
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
            if booked or stop:
                await _archive(conn, uid, trip_id)
            else:
                await conn.execute(
                    "UPDATE trips SET paused_reason = NULL, rev = rev + 1, updated_at = now() WHERE id = $1", trip_id
                )
                await conn.execute("UPDATE trip_runtime SET next_check_at = NULL WHERE trip_id = $1", trip_id)
            await conn.execute(
                "UPDATE hold_outcomes SET answer_reason = $3 WHERE id = (SELECT id FROM hold_outcomes"
                " WHERE trip_id = $1 AND user_id = $2 ORDER BY created_at DESC, id DESC LIMIT 1)",
                trip_id,
                uid,
                "booked" if booked else "cancelled" if stop else (reason or "not_booked"),
            )

    # ---- settings

    async def user_quiet_hours(self, uid: str) -> QuietHours | None:
        """The user's quiet hours (none unless they set some)."""
        row = await self._pool.fetchrow("SELECT quiet_start, quiet_end FROM user_settings WHERE user_id = $1", uid)
        return quiet_hours(row["quiet_start"], row["quiet_end"]) if row else None

    async def set_settings(self, uid: str, *, quiet: QuietHours | None) -> None:
        """The user's own settings: their quiet hours, or None for none."""
        await self._pool.execute(
            "INSERT INTO user_settings (user_id, quiet_start, quiet_end) VALUES ($1, $2, $3) ON CONFLICT (user_id)"
            " DO UPDATE SET quiet_start = $2, quiet_end = $3, updated_at = now()",
            uid,
            quiet.start if quiet else None,
            quiet.end if quiet else None,
        )

    # ---- alerts

    async def subscribe(self, uid: str, subscription: Subscription) -> None:
        """A device's push subscription. A shared device belongs to whoever subscribed last."""
        await self._pool.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (endpoint) DO UPDATE SET user_id = $1, p256dh = $3, auth = $4, failures = 0",
            uid,
            subscription.endpoint,
            subscription.p256dh,
            subscription.auth,
        )

    async def unsubscribe(self, uid: str, endpoint: str) -> None:
        await self._pool.execute("DELETE FROM push_subscriptions WHERE user_id = $1 AND endpoint = $2", uid, endpoint)

    async def test_alert(self, uid: str) -> None:
        now = self._clock()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid)
            limit = await self.limit(conn, uid, "testAlertsPerHour")
            recent = await conn.fetchval(
                "SELECT count(*) FROM notifications WHERE user_id = $1 AND kind = 'test' AND created_at > $2",
                uid,
                _ts(now - 3600),
            )
            if recent >= limit:
                raise ApiError(429, f"Up to {limit} test alerts an hour")
            await conn.execute("INSERT INTO notifications (user_id, kind) VALUES ($1, 'test')", uid)

    async def devices(self, uid: str) -> int:
        return int(await self._pool.fetchval("SELECT count(*) FROM push_subscriptions WHERE user_id = $1", uid))

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
        user = await self._pool.fetchrow("SELECT id, username, display, is_admin FROM users WHERE id = $1", uid)
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
            " rt.next_check_at, rt.last_check_at, rt.hold_requested_until, rt.quiet_lifted_until,"
            " s.agent_phase, s.checked_at, s.next_due, s.effective_interval,"
            " s.sailings, s.error_class, s.consecutive_errors, s.cart, s.viewer_connected, s.heartbeat_at, m.alerted"
            " FROM trips t LEFT JOIN trip_runtime rt ON rt.trip_id = t.id LEFT JOIN trip_state s ON s.trip_id = t.id"
            " LEFT JOIN alert_memory m ON m.trip_id = t.id"
            " WHERE t.user_id = $1 AND t.archived_at IS NULL ORDER BY t.created_at",
            uid,
        )
        context = await self.hold_context(uid, now)
        trips = []
        for r in rows:
            spec = json.loads(r["spec"]) | {"enabled": r["enabled"]}
            sailings = json.loads(r["sailings"]) if r["sailings"] else []
            requested_until = _epoch(r["hold_requested_until"])
            # A manual check or "Hold it for me" lifts the user's quiet hours for the trip.
            lifted = [t for t in (requested_until, _epoch(r["quiet_lifted_until"])) if t is not None]
            quiet = context.quiet(max(lifted, default=None), now)
            hold = context.hold(
                Trip.from_json(spec),
                paused=not r["enabled"] or r["paused_reason"] is not None,
                quiet=quiet,
                sailings=sailings,
                alerted=r["alerted"] or (),
                requested_until=requested_until,
                now=now,
            )
            trips.append(
                {
                    "trip": spec,
                    "rev": r["rev"],
                    "paused_reason": r["paused_reason"],
                    "quiet": quiet,  # in the user's quiet hours, and not lifted: it isn't being checked
                    "hold": hold,
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
                        "sailings": sailings,
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
        quiet_json = _quiet_json(context.allowance.quiet)
        return {
            "me": {
                "id": user["id"],
                "username": user["username"],
                "display": user["display"],
                "is_admin": user["is_admin"],
                "paused": False,  # for a release, for the old dashboard: trips are paused one at a time now
            },
            "limits": limits,
            "usage": usage,
            "trips": trips,
            "capacity": {
                "can_add": len(trips) < max_trips,
                "reason": None if len(trips) < max_trips else f"You can watch up to {max_trips} trips",
            },
            "settings": {"quiet_hours": quiet_json},
            # The same quiet hours, and whether it's quiet now (None: the user has none).
            "quiet_hours": quiet_json | {"now": context.allowance.quiet_now(context.at)} if quiet_json else None,
            "devices": await self.devices(uid),
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
