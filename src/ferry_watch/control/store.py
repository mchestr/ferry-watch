"""ferry-control's reads and writes (docs/multi-tenant/design-reference.md §6.4-6.6), as `ferrywatch_control`.

Everything an agent reports lands here; nothing here can reach into a browser. Writes that a replayed
message could repeat (events, alerts, outcomes) are keyed by `pod:boot:seq`, so a reconnect never
counts anything twice.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg
import msgspec

from ferry_watch.models import Trip, ValidationError
from ferry_watch.protocol import Alert, Brake, Bye, ClaimReason, Event, Mode, State

from .configure import Allowance, Switches, TripRow, pacific_date
from .limits import Limits

log = logging.getLogger(__name__)

USER_CHANNEL = "fw_user"  # NOTIFY payload: a user id whose dashboard changed
CLAIM_LAPSE_SECONDS = 120  # a claim without cart_held or cart_failed by then is dropped
AFTER_CHECKOUT = "after_checkout"  # trips.paused_reason until "Did you book it?" is answered
_PHASES = {"starting": "starting", "watching": "watching", "holding": "holding", "draining": "draining"}


def _ts(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch is not None else None


@dataclass(frozen=True, slots=True)
class Binding:
    """A connected agent's pod, as ferry-control's own records describe it."""

    pod: str
    uid: str
    trip_id: str
    user_id: str
    gen: int
    mode: Mode

    def key(self, boot: str, seq: int) -> str:
        return f"{self.pod}:{boot}:{seq}"


class ControlStore:
    def __init__(self, pool: asyncpg.Pool, limits: Limits | None = None) -> None:
        self._pool = pool
        self._limits = limits or Limits()

    # ---- binding a socket to a pod

    async def binding(self, pod_uid: str, pod_name: str) -> Binding | Literal["pending"] | None:
        """The pod's record. None: no such live pod. "pending": it exists, but its uid isn't recorded yet."""
        row = await self._pool.fetchrow(
            "SELECT name, uid, trip_id, user_id, gen, mode FROM browser_pods WHERE uid = $1 AND ended_at IS NULL",
            pod_uid,
        )
        if row is None:
            pending = await self._pool.fetchval(
                "SELECT 1 FROM browser_pods WHERE name = $1 AND uid IS NULL AND ended_at IS NULL", pod_name
            )
            return "pending" if pending else None
        if row["name"] != pod_name:
            return None
        return Binding(row["name"], row["uid"], row["trip_id"], row["user_id"], row["gen"], row["mode"])

    # ---- what a pod is told

    async def trip_row(self, trip_id: str) -> tuple[TripRow, bool] | None:
        """The trip as configured, and whether its owner is active."""
        row = await self._pool.fetchrow(
            "SELECT t.spec, t.rev, t.enabled, t.paused_reason, t.archived_at, u.state, m.alerted, m.offered"
            " FROM trips t JOIN users u ON u.id = t.user_id LEFT JOIN alert_memory m ON m.trip_id = t.id"
            " WHERE t.id = $1",
            trip_id,
        )
        if row is None:
            return None
        try:
            trip = Trip.from_json(json.loads(row["spec"]))
        except (ValidationError, ValueError):
            log.exception("Trip %s has an invalid spec", trip_id)
            return None
        trip_row = TripRow(
            trip=trip,
            rev=row["rev"],
            enabled=row["enabled"] and row["archived_at"] is None,
            paused_reason=row["paused_reason"],
            alerted=tuple(row["alerted"] or ()),
            offered=tuple(row["offered"] or ()),
        )
        return trip_row, row["state"] == "active"

    async def allowance(self, user_id: str, now: float) -> Allowance:
        overrides = {
            r["key"]: r["value"]
            for r in await self._pool.fetch("SELECT key, value FROM user_limits WHERE user_id = $1", user_id)
        }
        usage = {
            r["kind"]: r["amount"]
            for r in await self._pool.fetch(
                "SELECT kind, amount FROM usage_ledger WHERE user_id = $1 AND day = $2", user_id, pacific_date(now)
            )
        }
        return Allowance(
            holds_per_day=self._limits.value("maxHoldsPerDay", overrides),
            cart_attempts_per_day=self._limits.value("maxCartAttemptsPerDay", overrides),
            vnc_minutes_per_day=self._limits.value("vncMinutesPerDay", overrides),
            holds_today=usage.get("holds", 0),
            cart_attempts_today=usage.get("cart_attempts", 0),
            vnc_seconds_today=usage.get("vnc_seconds", 0),
        )

    async def switches(self) -> Switches:
        values = {
            r["key"]: json.loads(r["value"]) for r in await self._pool.fetch("SELECT key, value FROM fleet_state")
        }
        brake = values.get("brake") or {}
        return Switches(
            maintenance=bool(values.get("maintenance", False)),
            pause_carting=bool(values.get("pause_carting", False)),
            brake=Brake(multiplier=float(brake.get("multiplier", 1.0)), paused=bool(brake.get("paused", False))),
            breaker_open=values.get("breaker") == "open",
        )

    async def pending_commands(self, trip_id: str, pod: str) -> list[asyncpg.Record]:
        """What's queued for the trip's pod. `stale`: a recycle asked for before this pod existed (it was
        meant for an earlier generation, which has gone anyway)."""
        return await self._pool.fetch(
            "SELECT c.id, c.op, c.cart_id, c.op = 'recycle' AND c.created_at < p.created_at AS stale"
            " FROM agent_commands c LEFT JOIN browser_pods p ON p.name = $2"
            " WHERE c.trip_id = $1 AND c.consumed_at IS NULL ORDER BY c.id",
            trip_id,
            pod,
        )

    async def consume_command(self, command_id: int) -> None:
        await self._pool.execute("UPDATE agent_commands SET consumed_at = now() WHERE id = $1", command_id)

    # ---- what a pod reports

    async def record_state(self, binding: Binding, boot: str, state: State) -> None:
        cart = state.cart
        ready_at = cart.ready_at if cart is not None and cart.status == "ready" else None
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO trip_state (trip_id, pod, boot, seq, agent_phase, checked_at, next_due,"
                " effective_interval, sailings, error_class, consecutive_errors, cart, hold_ready_at,"
                " first_viewer_at, viewer_connected, heartbeat_at, lockdown_ok, working_set_mb, updated_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, now(), $16, $17, now())"
                " ON CONFLICT (trip_id) DO UPDATE SET pod = $2, boot = $3, seq = $4, agent_phase = $5,"
                " checked_at = $6, next_due = $7, effective_interval = $8, sailings = $9, error_class = $10,"
                " consecutive_errors = $11, cart = $12, hold_ready_at = $13, first_viewer_at = $14,"
                " viewer_connected = $15, heartbeat_at = now(), lockdown_ok = $16, working_set_mb = $17,"
                " updated_at = now()",
                binding.trip_id,
                binding.pod,
                boot,
                state.seq,
                state.phase,
                _ts(state.checked_at),
                _ts(state.next_due),
                state.effective_interval,
                _json(state.sailings),
                state.error.kind if state.error else None,
                state.consecutive_errors,
                _json(cart) if cart is not None else None,
                _ts(ready_at),
                _ts(state.viewer.first_at),
                state.viewer.connected,
                state.lockdown_ok,
                state.working_set_mb,
            )
            await conn.execute(
                "UPDATE browser_pods SET phase = $2 WHERE name = $1 AND phase NOT IN ('gone', 'failed')",
                binding.pod,
                _PHASES[state.phase],
            )
            if state.checked_at is not None:
                await conn.execute(
                    "UPDATE trip_runtime SET last_check_at = greatest(last_check_at, $2), updated_at = now()"
                    " WHERE trip_id = $1",
                    binding.trip_id,
                    _ts(state.checked_at),
                )
            if cart is not None and cart.status == "ready":
                await conn.execute(
                    "UPDATE hold_claims SET cart_id = $3 WHERE user_id = $1 AND pod = $2",
                    binding.user_id,
                    binding.pod,
                    cart.id,
                )
            await conn.execute(
                "INSERT INTO alert_memory (trip_id, key_hash, alerted, offered) VALUES ($1, '', $2, $3)"
                " ON CONFLICT (trip_id) DO UPDATE SET alerted = $2, offered = $3",
                binding.trip_id,
                state.alerted,
                state.offered,
            )

    async def record_event(self, binding: Binding, boot: str, event: Event) -> None:
        await self._pool.execute(
            "INSERT INTO events (user_id, trip_id, kind, code, params, dedup_key) VALUES ($1, $2, 'agent', $3, $4, $5)"
            " ON CONFLICT (dedup_key) DO NOTHING",
            binding.user_id,
            binding.trip_id,
            event.code,
            _json(event.params),
            binding.key(boot, event.seq),
        )

    async def record_alert(self, binding: Binding, boot: str, alert: Alert, now: float) -> bool:
        """Queue the alert for the user. False if it was already queued (a replay)."""
        async with self._pool.acquire() as conn, conn.transaction():
            queued = await conn.fetchval(
                "INSERT INTO notifications (user_id, trip_id, kind, params, dedup_key) VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
                binding.user_id,
                binding.trip_id,
                alert.kind,
                _json(alert.params),
                binding.key(boot, alert.seq),
            )
            if queued is None:
                return False
            if alert.kind == "cart_held":
                await _count(conn, binding.user_id, now, "holds")
            elif alert.kind in ("cart_failed", "cart_lost", "cart_expired"):
                await conn.execute(
                    "DELETE FROM hold_claims WHERE user_id = $1 AND pod = $2", binding.user_id, binding.pod
                )
            return True

    async def claim(self, binding: Binding, gate: ClaimReason, now: float) -> ClaimReason:
        """One hold per user: grant only if the gate allows it and the user holds nothing anywhere."""
        if gate != "ok":
            return gate
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM hold_claims WHERE user_id = $1 AND cart_id IS NULL"
                " AND claimed_at < now() - make_interval(secs => $2)",
                binding.user_id,
                CLAIM_LAPSE_SECONDS,
            )
            granted = await conn.fetchval(
                "INSERT INTO hold_claims (user_id, trip_id, pod) VALUES ($1, $2, $3)"
                " ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
                binding.user_id,
                binding.trip_id,
                binding.pod,
            )
            if granted is None:
                return "holding_elsewhere"
            await _count(conn, binding.user_id, now, "cart_attempts")
        return "ok"

    async def record_bye(self, binding: Binding, boot: str, bye: Bye, now: float) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM hold_claims WHERE user_id = $1 AND pod = $2", binding.user_id, binding.pod)
            await conn.execute(
                "UPDATE browser_pods SET phase = 'draining', end_reason = $2 WHERE name = $1 AND ended_at IS NULL",
                binding.pod,
                bye.reason,
            )
            if bye.outcome == "none" and not bye.viewer_was_connected:
                return
            key = binding.key(boot, bye.seq)
            inserted = await conn.fetchval(
                "INSERT INTO hold_outcomes (user_id, trip_id, day, outcome, release_check, pod, dedup_key)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
                binding.user_id,
                binding.trip_id,
                pacific_date(now),
                bye.outcome,
                bye.release_check,
                binding.pod,
                key,
            )
            if inserted is None or not bye.viewer_was_connected:
                return
            if bye.outcome == "booked":
                await conn.execute(
                    "UPDATE trips SET archived_at = now(), updated_at = now() WHERE id = $1", binding.trip_id
                )
                kind = "booked"
            else:
                # Did they book it? The trip stays paused until they answer (owner, round 3).
                await conn.execute(
                    "UPDATE trips SET paused_reason = $2, updated_at = now() WHERE id = $1",
                    binding.trip_id,
                    AFTER_CHECKOUT,
                )
                kind = "booked_it"
            await conn.execute(
                "INSERT INTO notifications (user_id, trip_id, kind, dedup_key) VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (dedup_key) DO NOTHING",
                binding.user_id,
                binding.trip_id,
                kind,
                key,
            )

    async def notify(self, user_id: str) -> None:
        """Tell ferry-web this user's dashboard has something new (LISTEN fw_user)."""
        await self._pool.execute("SELECT pg_notify($1, $2)", USER_CHANNEL, user_id)

    async def add_vnc_seconds(self, user_id: str, seconds: int, now: float) -> None:
        if seconds > 0:
            async with self._pool.acquire() as conn:
                await _count(conn, user_id, now, "vnc_seconds", seconds)


async def _count(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, user_id: str, now: float, kind: str, amount: int = 1
) -> None:
    await conn.execute(
        "INSERT INTO usage_ledger (user_id, day, kind, amount) VALUES ($1, $2, $3, $4)"
        " ON CONFLICT (user_id, day, kind) DO UPDATE SET amount = usage_ledger.amount + $4",
        user_id,
        pacific_date(now),
        kind,
        amount,
    )


def _json(value: Any | Sequence[Any]) -> str:
    return msgspec.json.encode(value).decode()
