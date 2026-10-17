"""waitlist-web's reads and writes, always for one user (docs/multi-tenant/design-reference.md §6.3, §9.2).

Every query that touches a trip names the user too, so a trip that belongs to someone else is
indistinguishable from one that doesn't exist. Limits are checked in the same transaction as the write,
with the user's row locked, so two requests can't both slip under a limit.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as clock_time
from typing import Any, Literal, cast

import asyncpg
import msgspec

from waitlist.control.checkplan import STALE_REPORT_S, check_state
from waitlist.control.configure import Allowance, HubSettings, Switches, cart_gate
from waitlist.control.limits import DEFAULTS, LimitKey, Limits
from waitlist.control.store import CLAIM_LAPSE_SECONDS, USER_CHANNEL
from waitlist.models import PACIFIC, Trip, ValidationError, format_time
from waitlist.polling import DEFAULT_QUIET_HOURS, PollPolicy, QuietHours, quiet_hours
from waitlist.providers import REGISTRY, Registry
from waitlist.providers.base import CheckState, HttpProvider, Option, ProviderMeta, search_hash

from .config import Offer
from .http import ApiError
from .push import Subscription

log = logging.getLogger(__name__)

BCFERRIES = "bcferries"
# What a full limit group says, by the limit that caps it (ProviderMeta.limit_key).
AT_LIMIT = {
    "maxTrips": "You can watch up to {n} trips",
    "maxAlertOnlyWatches": "You can have up to {n} alert-only watches",
}
FULL = "You're watching as many as you can"
NOT_OFFERED = "Not available to you"
SHOWN_LIMITS: tuple[LimitKey, ...] = (
    "maxTrips",
    "maxAlertOnlyWatches",
    "maxActiveBrowsers",
    "maxHoldsPerDay",
    "vncMinutesPerDay",
    "manualChecksPerHour",
)

AFTER_CHECKOUT = "after_checkout"
# Which viewer opens the user's checkouts (`user_settings.checkout_viewer`): KasmVNC, or the streamed viewer beta.
CheckoutViewer = Literal["classic", "stream"]
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


def _seconds(value: Any) -> float | None:
    """An epoch time from JSON, or None for anything else."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _today(now: float) -> date:
    return datetime.fromtimestamp(now, PACIFIC).date()


async def _archive(conn: asyncpg.pool.PoolConnectionProxy, uid: str, trip_id: str) -> bool:
    """Stop watching a trip (removed, booked, or "No, cancel"), in the caller's transaction. A new rev, so its pod
    hears at once; waitlist-control drains it, which hands back any cart first. False: no such live trip."""
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


def _after_quiet(due: float | None, context: HoldContext, lifted_until: float | None) -> float | None:
    """When a short check due at `due` runs: not in the user's quiet hours, unless they're lifted for it."""
    if due is None or (lifted_until is not None and due < lifted_until):
        return due
    return context.allowance.quiet.end_after(datetime.fromtimestamp(due, PACIFIC)).timestamp()


def _quiet_json(quiet: QuietHours) -> dict[str, Any]:
    """Quiet hours as the API has them: {"start": "HH:MM", "end": "HH:MM"}."""
    return {"start": quiet.start.strftime("%H:%M"), "end": quiet.end.strftime("%H:%M")}


@dataclass(frozen=True, slots=True)
class HoldContext:
    """What decides whether this user's trips may hold a cart now (waitlist-control's gate decides for real).

    waitlist-web doesn't see waitlist-control's own settings (fleet-wide auto-cart, alert-only fares, the session
    length), so it takes the defaults; at worst a hold it offers is refused and the alert says why.
    """

    allowance: Allowance  # with the user's quiet hours
    switches: Switches
    claimed_trip: str | None  # the trip holding the user's one cart claim, if any
    settings: HubSettings
    at: datetime

    def quiet(self, lifted_until: float | None, now: float) -> bool:
        """Whether a trip isn't checked now for the user's quiet hours: it's in them, and they're not lifted."""
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
    agent_version: str | None  # what the pod's agent said hello with; None until it has


class WebStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        limits: Limits,
        policy: PollPolicy,
        *,
        clock: Callable[[], float],
        registry: Registry = REGISTRY,
        offers: Mapping[str, Offer] | None = None,
    ) -> None:
        self._pool = pool
        self._limits = limits
        self._policy = policy
        self._clock = clock
        self.registry = registry
        # Each HTTP-checked provider's offer; one the config doesn't name is "off".
        self.offers: dict[str, Offer] = {p: (offers or {}).get(p, "off") for p in registry.http}
        self._unloadable: set[tuple[str, int]] = set()  # (id, rev) of watches already logged as not loading

    @property
    def horizon_days(self) -> int:
        """How far ahead a BC Ferries trip may be: the deployment's polling.horizonDays, which add_trip enforces."""
        return self._policy.horizon.days

    async def notify(self, uid: str) -> None:
        """Wake this user's other dashboards (LISTEN wl_user)."""
        await self._pool.execute("SELECT pg_notify($1, $2)", USER_CHANNEL, uid)

    # ---- limits

    async def overrides(self, conn: asyncpg.pool.PoolConnectionProxy | asyncpg.Pool, uid: str) -> dict[str, int]:
        rows = await conn.fetch("SELECT key, value FROM user_limits WHERE user_id = $1", uid)
        return {r["key"]: r["value"] for r in rows}

    async def limit(self, conn: asyncpg.pool.PoolConnectionProxy | asyncpg.Pool, uid: str, key: LimitKey) -> int:
        return self._limits.value(key, await self.overrides(conn, uid))

    # ---- trips

    async def trip(self, uid: str, trip_id: str) -> tuple[Trip, int]:
        """The user's live BC Ferries trip. Another provider's watch is "No such trip": the browser's actions (a
        command, a manual check, a hold, a checkout) are BC Ferries' alone (docs/waitlist/design.md §2.4)."""
        row = await self._pool.fetchrow(
            "SELECT spec, enabled, rev FROM trips"
            " WHERE id = $1 AND user_id = $2 AND archived_at IS NULL AND provider = 'bcferries'",
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
            count = await conn.fetchval(
                "SELECT count(*) FROM trips WHERE user_id = $1 AND archived_at IS NULL AND provider = 'bcferries'", uid
            )
            if count >= limit:
                raise ApiError(409, f"You can watch up to {limit} trips", "trips", limit=limit, usage=count)
            await conn.execute(
                "INSERT INTO trips (id, user_id, provider, spec, enabled) VALUES ($1, $2, 'bcferries', $3, $4)",
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
                "SELECT spec, enabled FROM trips"
                " WHERE id = $1 AND user_id = $2 AND archived_at IS NULL AND provider = 'bcferries' FOR UPDATE",
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

    async def archive_trip(self, uid: str, trip_id: str, *, provider: str | None = "bcferries") -> None:
        """Stop watching. waitlist-control drains its pod, which hands back any cart first. `provider`: only a
        watch of that provider (the /api/trips alias: BC Ferries), or None for any (DELETE /api/watches)."""
        async with self._pool.acquire() as conn, conn.transaction():
            if provider is not None:
                found = await conn.fetchval(
                    "SELECT 1 FROM trips WHERE id = $1 AND user_id = $2 AND archived_at IS NULL AND provider = $3"
                    " FOR UPDATE",
                    trip_id,
                    uid,
                    provider,
                )
                if found is None:
                    raise ApiError(404, "No such trip")
            if not await _archive(conn, uid, trip_id):
                raise ApiError(404, "No such trip" if provider is not None else "No such watch")

    # ---- watches on any provider (docs/waitlist/design.md §6.2)

    def _limit_key(self, meta: ProviderMeta) -> LimitKey:
        if meta.limit_key not in DEFAULTS:
            raise ValueError(f"{meta.id}: {meta.limit_key} isn't a limit")
        return cast(LimitKey, meta.limit_key)

    def _at(self, now: float) -> datetime:
        return datetime.fromtimestamp(now, UTC)  # what a provider is handed: always timezone-aware

    async def watch_provider(self, uid: str, watch_id: str) -> str:
        """The provider of the user's live watch, read before anything else (its spec is the provider's to parse)."""
        provider = await self._pool.fetchval(
            "SELECT provider FROM trips WHERE id = $1 AND user_id = $2 AND archived_at IS NULL", watch_id, uid
        )
        if provider is None:
            raise ApiError(404, "No such watch")
        return str(provider)

    async def add_watch(
        self, uid: str, provider: HttpProvider[Any, Any, Any], data: Mapping[str, Any], *, enabled: bool = True
    ) -> str:
        """A new watch on an HTTP-checked provider, within the user's limit for its kind; due as soon as its window
        allows. Returns its id."""
        spec = provider.parse_spec(data, base=None, now=self._at(self._clock()))
        key = self._limit_key(provider.meta)
        group = [m.id for m in self.registry.metas if m.limit_key == provider.meta.limit_key]
        watch_id = secrets.token_hex(8)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid)
            limit = await self.limit(conn, uid, key)
            count = await conn.fetchval(
                "SELECT count(*) FROM trips WHERE user_id = $1 AND archived_at IS NULL AND provider = ANY($2::text[])",
                uid,
                group,
            )
            if count >= limit:
                message = AT_LIMIT.get(key, "You can't add more of these").format(n=limit)
                raise ApiError(409, message, "provider", limit=limit, usage=count)
            await conn.execute(
                "INSERT INTO trips (id, user_id, provider, spec, enabled) VALUES ($1, $2, $3, $4, $5)",
                watch_id,
                uid,
                provider.meta.id,
                json.dumps(provider.dump_spec(spec)),
                enabled,
            )
            await conn.execute("INSERT INTO trip_runtime (trip_id) VALUES ($1)", watch_id)  # NULL: due when it can be
        return watch_id

    async def update_watch(
        self,
        uid: str,
        watch_id: str,
        provider: HttpProvider[Any, Any, Any],
        patch: Mapping[str, Any] | None,
        enabled: bool | None,
    ) -> None:
        """A partial edit of an HTTP-checked watch. A changed spec makes it due at once (its stored options catch up
        within a tick); a changed search forgets what was alerted; turning it back on makes it due too."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT provider, spec, enabled, rev FROM trips"
                " WHERE id = $1 AND user_id = $2 AND archived_at IS NULL FOR UPDATE",
                watch_id,
                uid,
            )
            if row is None or row["provider"] != provider.meta.id:
                raise ApiError(404, "No such watch")
            current = json.loads(row["spec"])
            base = provider.load_spec(current)
            new = provider.parse_spec(patch or {}, base=base, now=self._at(self._clock()))
            stored = provider.dump_spec(new)
            now_enabled = row["enabled"] if enabled is None else enabled
            await conn.execute(
                "UPDATE trips SET spec = $3, enabled = $4, rev = rev + 1, updated_at = now()"
                " WHERE id = $1 AND user_id = $2",
                watch_id,
                uid,
                json.dumps(stored),
                now_enabled,
            )
            if stored != current or (now_enabled and not row["enabled"]):
                await conn.execute("UPDATE trip_runtime SET next_check_at = NULL WHERE trip_id = $1", watch_id)
            if search_hash(provider.search_key(new)) != search_hash(provider.search_key(base)):
                await conn.execute("DELETE FROM alert_memory WHERE trip_id = $1", watch_id)  # a new search

    async def check_watch(self, uid: str, watch_id: str, provider: HttpProvider[Any, Any, Any]) -> None:
        """A manual check of an HTTP-checked watch: due now, with the user's quiet hours lifted for it for a few
        minutes, counted against manualChecksPerHour. Refused, consuming nothing, while a check couldn't happen."""
        now = self._clock()
        row = await self._pool.fetchrow(
            "SELECT t.spec, t.enabled, t.paused_reason, rt.backoff_until, rt.quiet_lifted_until,"
            " cr.consecutive_errors, cr.attempted_at, u.state, u.paused"
            " FROM trips t JOIN users u ON u.id = t.user_id LEFT JOIN trip_runtime rt ON rt.trip_id = t.id"
            " LEFT JOIN check_results cr ON cr.trip_id = t.id"
            " WHERE t.id = $1 AND t.user_id = $2 AND t.archived_at IS NULL AND t.provider = $3",
            watch_id,
            uid,
            provider.meta.id,
        )
        if row is None:
            raise ApiError(404, "No such watch")
        spec = provider.load_spec(json.loads(row["spec"]))
        window = provider.window(spec)
        context = await self.hold_context(uid, now)
        provider_paused, running, cooldown_until = self.check_inputs(provider.meta.id, await self.switches(), now)
        backoff_until = _epoch(row["backoff_until"])
        state: CheckState = check_state(
            enabled=row["enabled"],
            paused_reason=row["paused_reason"],
            owner_active=row["state"] == "active" and not row["paused"],
            window=window,
            quiet=context.quiet(_epoch(row["quiet_lifted_until"]), now),
            provider_paused=provider_paused,
            provider_running=running,
            backoff_until=backoff_until,
            cooldown_until=cooldown_until,
            consecutive_errors=row["consecutive_errors"] or 0,
            attempted_at=_epoch(row["attempted_at"]),
            now=now,
        )
        name = provider.meta.name
        resume = max(backoff_until or 0.0, cooldown_until or 0.0)
        if state == "waiting_release":
            opens = window.opens_at.astimezone(PACIFIC)
            raise ApiError(409, f"Checks start {opens:%a %b %-d} at {format_time(opens.time())}")
        if state == "over":
            raise ApiError(409, "It's too late for this watch")
        if state == "provider_paused":
            raise ApiError(409, f"Checks for {name} are paused right now")
        if state == "unavailable":
            raise ApiError(409, f"Checks for {name} aren't running right now")
        if state == "paused":
            raise ApiError(409, "This watch is paused")
        if state == "backoff" or (state == "quiet" and resume > now):
            # Web can't clear a backoff, so a check now would spend a manual check on nothing.
            raise ApiError(409, f"Checks resume at {format_time(datetime.fromtimestamp(resume, PACIFIC).time())}")
        async with self._pool.acquire() as conn, conn.transaction():
            await self._check_now(conn, uid, watch_id, now)
            await conn.execute(
                "UPDATE trip_runtime SET quiet_lifted_until = $2 WHERE trip_id = $1", watch_id, _ts(now + QUIET_LIFT_S)
            )

    async def done_watch(self, uid: str, watch_id: str) -> None:
        """ "I booked it", for an alert-only watch: archived, with a record of why."""
        async with self._pool.acquire() as conn, conn.transaction():
            if not await _archive(conn, uid, watch_id):
                raise ApiError(404, "No such watch")
            await conn.execute(
                "INSERT INTO events (user_id, trip_id, kind, code, params) VALUES ($1, $2, 'watch', 'booked', '{}')",
                uid,
                watch_id,
            )

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
        for the next few minutes, and checks now. waitlist-control's gate still decides; every other limit stays.
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
            claimed_trip=await self._pool.fetchval(  # a claim that lapsed doesn't count, as in waitlist-control
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
                "SELECT paused_reason FROM trips"
                " WHERE id = $1 AND user_id = $2 AND archived_at IS NULL AND provider = 'bcferries' FOR UPDATE",
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

    async def user_quiet_hours(self, uid: str) -> QuietHours:
        """The user's quiet hours: the default until they change them."""
        row = await self._pool.fetchrow("SELECT quiet_start, quiet_end FROM user_settings WHERE user_id = $1", uid)
        return quiet_hours(row["quiet_start"], row["quiet_end"]) if row else DEFAULT_QUIET_HOURS

    async def checkout_viewer(self, uid: str) -> CheckoutViewer:
        """The viewer the user chose for their checkouts: classic until they turn the beta on."""
        viewer = await self._pool.fetchval("SELECT checkout_viewer FROM user_settings WHERE user_id = $1", uid)
        return "stream" if viewer == "stream" else "classic"

    async def set_settings(
        self, uid: str, *, quiet: QuietHours | None = None, viewer: CheckoutViewer | None = None
    ) -> None:
        """The user's own settings: their quiet hours and their checkout viewer, each only when given. A new row
        starts at the defaults for the rest."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO user_settings (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", uid)
            if quiet is not None:
                await conn.execute(
                    "UPDATE user_settings SET quiet_start = $2, quiet_end = $3, updated_at = now() WHERE user_id = $1",
                    uid,
                    quiet.start,
                    quiet.end,
                )
            if viewer is not None:
                await conn.execute(
                    "UPDATE user_settings SET checkout_viewer = $2, updated_at = now() WHERE user_id = $1", uid, viewer
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
            "SELECT s.cart, s.pod, p.gen, p.agent_version FROM trip_state s JOIN browser_pods p ON p.name = s.pod"
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
            agent_version=row["agent_version"],
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
                " VALUES ($1, $2, $3, $4, 'wl-vnc', $5, $6)",
                jti,
                uid,
                held.trip.id,
                held.pod,
                held.cart_id,
                _ts(now),
            )

    # ---- what the dashboard shows

    async def switches(self) -> dict[str, Any]:
        """The fleet switches and waitlist-control's check report, as stored."""
        return {
            r["key"]: json.loads(r["value"])
            for r in await self._pool.fetch(
                "SELECT key, value FROM fleet_state WHERE key IN ('maintenance', 'paused_providers', 'check_report')"
            )
        }

    def provider_paused(self, provider_id: str, switches: Mapping[str, Any]) -> bool:
        """An HTTP-checked provider's checks are paused: maintenance, or an admin paused it. Never BC Ferries."""
        if provider_id not in self.registry.http:
            return False
        paused = switches.get("paused_providers")
        return switches.get("maintenance") is True or (isinstance(paused, list) and provider_id in paused)

    def offered(self, meta: ProviderMeta, *, is_admin: bool) -> bool:
        """Whether this user may start a watch on the provider. BC Ferries is always offered."""
        if meta.id not in self.registry.http:
            return True
        offer = self.offers.get(meta.id, "off")
        return offer == "everyone" or (offer == "admins" and is_admin)

    def providers(self, *, is_admin: bool, switches: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The catalog (GET /api/providers, and every state's `providers`), in picker order, built per request.
        Providers not offered to the user are listed too, so an existing watch still names its provider."""
        catalog = []
        for meta in self.registry.metas:
            entry = meta.to_json() | {
                "offered": self.offered(meta, is_admin=is_admin),
                "paused": self.provider_paused(meta.id, switches),
            }
            if meta.id == BCFERRIES:  # what add_trip enforces (PollPolicy.within_horizon), so the SPA agrees
                entry["horizon_days"] = self.horizon_days
            catalog.append(entry)
        return catalog

    def _capacity(self, live: Iterable[str], overrides: Mapping[str, int], *, is_admin: bool) -> dict[str, Any]:
        """Whether the user may add a watch, per provider: each counts the live watches of every provider capped by
        the same limit. A paused provider doesn't stop adding: the watch waits until checks resume."""
        counts: dict[str, int] = {}
        for provider_id in live:
            meta = self.registry.meta(provider_id)
            if meta is not None:
                counts[meta.limit_key] = counts.get(meta.limit_key, 0) + 1
        per_provider: dict[str, Any] = {}
        for meta in self.registry.metas:
            limit = self._limits.value(self._limit_key(meta), overrides)
            count = counts.get(meta.limit_key, 0)
            offered = self.offered(meta, is_admin=is_admin)
            reason = None
            if not offered:
                reason = NOT_OFFERED
            elif count >= limit:
                reason = AT_LIMIT.get(meta.limit_key, "You can't add more of these").format(n=limit)
            per_provider[meta.id] = {"can_add": reason is None, "reason": reason, "limit": limit, "count": count}
        can_add = any(p["can_add"] for p in per_provider.values())
        return {"can_add": can_add, "reason": None if can_add else FULL, "providers": per_provider}

    def _degraded(self, r: asyncpg.Record, why: str) -> dict[str, Any]:
        """What's left of a watch this build can't show: enough to list it and remove it."""
        if (r["id"], r["rev"]) not in self._unloadable:
            self._unloadable.add((r["id"], r["rev"]))
            log.warning("Watch %s (rev %d, %s) is shown as unknown: %s", r["id"], r["rev"], r["provider"], why)
        return {
            "provider": r["provider"],
            "id": r["id"],
            "rev": r["rev"],
            "enabled": r["enabled"],
            "paused_reason": r["paused_reason"],
            "quiet": False,
        }

    def _bcferries_entry(self, r: asyncpg.Record, context: HoldContext, now: float) -> dict[str, Any]:
        """A BC Ferries trip's entry: 0.7.1's exactly, plus `provider`, `id` and `enabled`."""
        spec = json.loads(r["spec"]) | {"enabled": r["enabled"]}
        sailings = json.loads(r["sailings"]) if r["sailings"] else []
        requested_until = _epoch(r["hold_requested_until"])
        # A manual check or "Hold it for me" lifts the user's quiet hours for the trip.
        lifted = max((t for t in (requested_until, _epoch(r["quiet_lifted_until"])) if t is not None), default=None)
        quiet = context.quiet(lifted, now)
        hold = context.hold(
            Trip.from_json(spec),
            paused=not r["enabled"] or r["paused_reason"] is not None,
            quiet=quiet,
            sailings=sailings,
            alerted=r["alerted"] or (),
            requested_until=requested_until,
            now=now,
        )
        return {
            "provider": BCFERRIES,
            "id": r["id"],
            "enabled": r["enabled"],
            "trip": spec,
            "rev": r["rev"],
            "paused_reason": r["paused_reason"],
            "quiet": quiet,  # in the user's quiet hours, and not lifted: it isn't being checked
            "hold": hold,
            "browser": {
                "phase": r["agent_phase"] or r["phase"] or "off",
                "mode": r["mode"] or "off",
                "queue_position": r["queue_position"],
                "next_check_at": _after_quiet(_epoch(r["next_check_at"]), context, lifted),
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

    def check_inputs(
        self, provider_id: str, switches: Mapping[str, Any], now: float
    ) -> tuple[bool, bool, float | None]:
        """What waitlist-web passes check_state for a provider: (provider_paused, provider_running, cooldown_until).
        Checks are running when waitlist-control's check report is fresh and says the fetcher answers."""
        report = switches.get("check_report")
        at = _seconds(report.get("at")) if isinstance(report, dict) else None
        providers = report.get("providers") if isinstance(report, dict) else None
        fresh = at is not None and at >= now - STALE_REPORT_S and isinstance(providers, dict)
        entry = providers.get(provider_id) if fresh and isinstance(providers, dict) else None
        running = isinstance(entry, dict) and entry.get("fetcher") == "ok"
        cooldown_until = _seconds(entry.get("cooldown_until")) if isinstance(entry, dict) else None
        return self.provider_paused(provider_id, switches), running, cooldown_until

    def _http_entry(
        self,
        r: asyncpg.Record,
        provider: HttpProvider[Any, Any, Any],
        context: HoldContext,
        switches: Mapping[str, Any],
        *,
        owner_active: bool,
        now: float,
    ) -> dict[str, Any]:
        """An HTTP-checked watch's entry (design.md §6.3). Raises ValueError (SpecError) if its spec won't load."""
        spec = provider.load_spec(json.loads(r["spec"]))
        window = provider.window(spec)
        at = datetime.fromtimestamp(now, UTC)
        lifted = _epoch(r["quiet_lifted_until"])
        quiet = context.quiet(lifted, now)
        provider_paused, running, cooldown_until = self.check_inputs(r["provider"], switches, now)
        state = check_state(
            enabled=r["enabled"],
            paused_reason=r["paused_reason"],
            owner_active=owner_active,
            window=window,
            quiet=quiet,
            provider_paused=provider_paused,
            provider_running=running,
            backoff_until=_epoch(r["backoff_until"]),
            cooldown_until=cooldown_until,
            consecutive_errors=r["cr_consecutive_errors"] or 0,
            attempted_at=_epoch(r["cr_attempted_at"]),
            now=now,
        )
        placeholders: tuple[Option, ...] = provider.evaluate(spec, {}, at)
        outcome = r["cr_outcome"] or "pending"
        options = list(placeholders)
        if r["cr_search_hash"] is not None and r["cr_search_hash"] == search_hash(provider.search_key(spec)):
            options = remark(_stored_options(r["cr_options"]), placeholders)
        else:  # a result made for an earlier search (or none yet) is never shown for this one
            outcome = "pending"
        booking = provider.booking_url(spec, options)
        return {
            "provider": r["provider"],
            "id": r["id"],
            "rev": r["rev"],
            "enabled": r["enabled"],
            "paused_reason": r["paused_reason"],
            "quiet": quiet,
            "spec": provider.dump_spec(spec),
            "check": {
                "state": state,
                "opens_at": window.opens_at.timestamp(),
                "closes_at": window.closes_at.timestamp(),
                "last_check_at": _epoch(r["cr_attempted_at"]),
                "checked_at": _epoch(r["cr_checked_at"]),
                "next_check_at": _after_quiet(_epoch(r["next_check_at"]), context, lifted),
                "outcome": outcome,
                "error_class": r["cr_error_class"],
                "consecutive_errors": r["cr_consecutive_errors"] or 0,
            },
            "options": [msgspec.to_builtins(o) for o in options],
            "booking_url": booking if booking is not None and provider.meta.allows_booking_url(booking) else None,
        }

    async def snapshot(self, uid: str) -> dict[str, Any]:
        now = self._clock()
        # For a release: a waitlist-web from before 0004 can still "Pause all" (users.paused) while it's being
        # replaced. Do what 0004 did, so each of the user's trips is paused instead (waitlist-control pauses
        # them all until then).
        await self._pool.execute(
            "WITH cleared AS (UPDATE users SET paused = false WHERE id = $1 AND paused RETURNING id)"
            " UPDATE trips SET enabled = false, spec = jsonb_set(spec, '{enabled}', 'false'), rev = rev + 1,"
            " updated_at = now() WHERE archived_at IS NULL AND enabled AND user_id IN (SELECT id FROM cleared)"
            " AND provider = 'bcferries'",
            uid,
        )
        user = await self._pool.fetchrow(
            "SELECT id, username, display, is_admin, state, paused FROM users WHERE id = $1", uid
        )
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
            "SELECT t.id, t.provider, t.spec, t.enabled, t.paused_reason, t.rev, rt.phase, rt.mode, rt.queue_position,"
            " rt.next_check_at, rt.last_check_at, rt.hold_requested_until, rt.quiet_lifted_until, rt.backoff_until,"
            " s.agent_phase, s.checked_at, s.next_due, s.effective_interval,"
            " s.sailings, s.error_class, s.consecutive_errors, s.cart, s.viewer_connected, s.heartbeat_at, m.alerted,"
            " cr.search_hash AS cr_search_hash, cr.outcome AS cr_outcome, cr.options AS cr_options,"
            " cr.attempted_at AS cr_attempted_at, cr.checked_at AS cr_checked_at, cr.error_class AS cr_error_class,"
            " cr.consecutive_errors AS cr_consecutive_errors"
            " FROM trips t LEFT JOIN trip_runtime rt ON rt.trip_id = t.id LEFT JOIN trip_state s ON s.trip_id = t.id"
            " LEFT JOIN alert_memory m ON m.trip_id = t.id LEFT JOIN check_results cr ON cr.trip_id = t.id"
            " WHERE t.user_id = $1 AND t.archived_at IS NULL ORDER BY t.created_at",
            uid,
        )
        context = await self.hold_context(uid, now)
        switches = await self.switches()
        owner_active = user["state"] == "active" and not user["paused"]
        watches = []
        for r in rows:
            provider = self.registry.http_provider(r["provider"])
            try:
                if r["provider"] == BCFERRIES:
                    watches.append(self._bcferries_entry(r, context, now))
                elif provider is not None:
                    watches.append(self._http_entry(r, provider, context, switches, owner_active=owner_active, now=now))
                else:
                    watches.append(self._degraded(r, "a provider this build doesn't know"))
            except ValueError as e:  # ValidationError, SpecError: one bad row never fails the state
                watches.append(self._degraded(r, f"its spec doesn't load ({e})"))
            except Exception as e:  # nor does a provider's bug
                if r["provider"] == BCFERRIES:
                    raise  # BC Ferries' entry is 0.7.1's, and failed as it would have
                watches.append(self._degraded(r, f"its provider failed on it ({type(e).__name__})"))
        limits: dict[str, dict[str, Any]] = {}
        for key in SHOWN_LIMITS:
            effective = self._limits.effective(key, overrides.get(key))
            limits[key] = {"value": effective.value, "source": effective.source}
        events = await self._pool.fetch(
            "SELECT id, trip_id, ts, kind, code, params FROM events WHERE user_id = $1 ORDER BY ts DESC LIMIT 50", uid
        )
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
            "watches": watches,
            "providers": self.providers(is_admin=user["is_admin"], switches=switches),
            "capacity": self._capacity((r["provider"] for r in rows), overrides, is_admin=user["is_admin"]),
            # waitlist-web adds whether the streamed viewer is offered to the user (api.user_state).
            "settings": {"quiet_hours": quiet_json, "checkout_viewer": await self.checkout_viewer(uid)},
            # The same quiet hours, and whether it's quiet now.
            "quiet_hours": quiet_json | {"now": context.allowance.quiet_now(context.at)},
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


def _stored_options(text: str | None) -> list[Option]:
    """check_results.options, as waitlist-control wrote them; anything unreadable reads as none."""
    if not text:
        return []
    try:
        return msgspec.json.decode(text, type=list[Option])
    except (msgspec.ValidationError, msgspec.DecodeError):
        log.warning("Stored options that aren't options; showing placeholders")
        return []


def remark(stored: Iterable[Option], placeholders: Iterable[Option]) -> list[Option]:
    """Stored options re-marked against the current spec (design.md §6.3): the placeholders' keys and order (the
    spec's alone), each the stored option of that key with the placeholder's `wanted`, or the placeholder itself
    if none is stored. Stored keys the placeholders don't list are dropped. For an unchanged spec, the identity."""
    by_key = {o.key: o for o in stored}
    return [msgspec.structs.replace(by_key[p.key], wanted=p.wanted) if p.key in by_key else p for p in placeholders]
