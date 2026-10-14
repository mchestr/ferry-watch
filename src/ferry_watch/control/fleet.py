"""ferry-control's fleet loop: make the browser pods match what the rows want (design-reference §6.6, §8).

Level-triggered and idempotent: each pass reads the rows and the pods, reconciles one against the other,
reaps pods whose timers ran out, keeps the hold-egress label in step with held carts, and applies the
planner's starts and drains. A pass that dies half-way is simply run again.

Pods are never touched just because ferry-control restarted. A holding pod is never reaped for silence,
only by the operator's own backstop clock or by Kubernetes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import asyncpg

from ferry_watch.models import PACIFIC, Trip, ValidationError
from ferry_watch.protocol import CommandOp, CommandReason

from .configure import HubSettings, curve_interval, stretch
from .kube import Cluster, KubeError, ObservedPod
from .limits import Limits
from .planner import FleetView, PodView, Start, TripView, UserView, plan
from .pods import DEADLINE_SECONDS, EGRESS_ALLOWLIST, EGRESS_HOLD, LABEL_EGRESS, PodParams, browser_pod, pod_name
from .store import USER_CHANNEL

log = logging.getLogger(__name__)

PASS_SECONDS = 10.0
STARTUP_TIMEOUT_S = 15 * 60  # create -> first check (a short check's whole run, unless it carts)
PENDING_TIMEOUT_S = 5 * 60  # unschedulable
SILENCE_S = 5 * 60  # a Watching pod not heard from (only once the hub has been up this long too)
HUB_SETTLE_S = 2 * 60
HOLD_BACKSTOP_S = 5 * 60  # past the hard cap
ORPHAN_GRACE_S = 2 * 60
FAILED_RETENTION_S = 30 * 60  # kept for `kubectl logs`, if it never held a cart or had a viewer
EGRESS_GIVE_UP_S = 60  # a pod whose egress can't be set back to the allowlist by then is deleted
BACKOFF_AFTER = 3  # pod failures in a row
BACKOFF_MINUTES = (5, 10, 20, 40, 60)
NEEDS_ATTENTION_AFTER = 5
SHORT_CHECK_JITTER = 0.15


class FleetHub(Protocol):
    """What the fleet loop needs from the hub endpoint (they share a process)."""

    started_at: float

    def last_heard(self, pod: str) -> float | None: ...
    async def command(self, pod: str, op: CommandOp, reason: CommandReason) -> bool: ...


@dataclass(slots=True)
class _Row:
    name: str
    trip_id: str
    user_id: str
    gen: int
    mode: str
    phase: str
    uid: str | None
    egress: str
    created_at: float
    hold_ready_at: float | None = None
    viewed: bool = False


@dataclass(slots=True)
class PassReport:
    started: list[str] = field(default_factory=list)
    drained: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    egress: list[tuple[str, str]] = field(default_factory=list)


def _epoch(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class Fleet:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cluster: Cluster,
        hub: FleetHub,
        params: PodParams,
        settings: HubSettings,
        *,
        limits: Limits | None = None,
        max_browsers_total: int = 7,
        short_check_slots: int = 1,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._pool = pool
        self._cluster = cluster
        self._hub = hub
        self._params = params
        self._settings = settings
        self._limits = limits or Limits()
        self._max_total = max_browsers_total
        self._short_slots = short_check_slots
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - jitter
        self._egress_stuck_since: dict[str, float] = {}
        self.stretch = 1.0  # the budget stretch, read by the hub

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("Fleet pass failed")
            await asyncio.sleep(PASS_SECONDS)

    async def reconcile(self) -> PassReport:
        now = self._clock()
        report = PassReport()
        observed = {p.name: p for p in await self._cluster.list_pods()}
        rows = await self._live_rows()
        await self._sync(rows, observed, now, report)
        rows = await self._live_rows()
        await self._reap(rows, observed, now, report)
        rows = await self._live_rows()
        await self._egress(rows, observed, now, report)
        await self._apply_plan(rows, now, report)
        return report

    # ---- rows and pods

    async def _live_rows(self) -> dict[str, _Row]:
        records = await self._pool.fetch(
            "SELECT p.name, p.trip_id, p.user_id, p.gen, p.mode, p.phase, p.uid, p.egress, p.created_at,"
            " s.hold_ready_at, s.first_viewer_at, s.pod AS state_pod"
            " FROM browser_pods p LEFT JOIN trip_state s ON s.trip_id = p.trip_id WHERE p.ended_at IS NULL"
        )
        rows = {}
        for r in records:
            mine = r["state_pod"] == r["name"]
            rows[r["name"]] = _Row(
                name=r["name"],
                trip_id=r["trip_id"],
                user_id=r["user_id"],
                gen=r["gen"],
                mode=r["mode"],
                phase=r["phase"],
                uid=r["uid"],
                egress=r["egress"],
                created_at=r["created_at"].timestamp(),
                hold_ready_at=_epoch(r["hold_ready_at"]) if mine else None,
                viewed=mine and r["first_viewer_at"] is not None,
            )
        return rows

    async def _end(self, row: _Row, phase: str, reason: str, *, failure: bool = False) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE browser_pods SET ended_at = $4, phase = $2, end_reason = coalesce(end_reason, $3)"
                " WHERE name = $1 AND ended_at IS NULL",
                row.name,
                phase,
                reason,
                _ts(self._clock()),
            )
            await conn.execute("DELETE FROM hold_claims WHERE pod = $1", row.name)
            if failure:
                failures = await conn.fetchval(
                    "UPDATE trip_runtime SET failures = failures + 1, updated_at = now() WHERE trip_id = $1"
                    " RETURNING failures",
                    row.trip_id,
                )
                if failures is not None and failures >= BACKOFF_AFTER:
                    minutes = BACKOFF_MINUTES[min(failures - BACKOFF_AFTER, len(BACKOFF_MINUTES) - 1)]
                    await conn.execute(
                        "UPDATE trip_runtime SET backoff_until = $2, needs_attention = $3 WHERE trip_id = $1",
                        row.trip_id,
                        _ts(self._clock() + minutes * 60),
                        failures >= NEEDS_ATTENTION_AFTER,
                    )
        await self._pool.execute("SELECT pg_notify($1, $2)", USER_CHANNEL, row.user_id)
        log.info("%s ended: %s (%s)", row.name, phase, reason)

    async def _sync(
        self, rows: dict[str, _Row], observed: dict[str, ObservedPod], now: float, report: PassReport
    ) -> None:
        """Match rows to pods: pods that finished or vanished end their rows; strangers are swept."""
        for row in rows.values():
            pod = observed.get(row.name)
            if pod is None:
                if row.uid is not None or now - row.created_at > ORPHAN_GRACE_S:
                    await self._end(row, "gone", "pod vanished" if row.uid else "never created")
                continue
            if row.uid is None:
                await self._pool.execute(
                    "UPDATE browser_pods SET uid = $2, node = $3 WHERE name = $1", row.name, pod.uid, pod.node
                )
            if pod.phase == "Succeeded":
                await self._end(row, "gone", (pod.termination_message or "exited").split(" ")[0])
                await self._delete(pod.name, report)
            elif pod.phase == "Failed":
                await self._end(row, "failed", pod.termination_message or "failed", failure=True)
                if row.hold_ready_at is not None or row.viewed:
                    await self._delete(pod.name, report)  # it held a cart or had a viewer: nothing kept
        live = {r.name for r in rows.values()}
        ended = {
            r["name"]: r["ended_at"].timestamp()
            for r in await self._pool.fetch(
                "SELECT name, ended_at FROM browser_pods WHERE ended_at IS NOT NULL AND name = ANY($1::text[])",
                list(observed),
            )
        }
        for pod in observed.values():
            if pod.name in live or pod.deleting:
                continue
            ended_at = ended.get(pod.name)
            if ended_at is not None and pod.phase == "Failed" and now - ended_at < FAILED_RETENTION_S:
                continue  # kept for a post-mortem
            if ended_at is not None or now - pod.created > ORPHAN_GRACE_S:
                await self._delete(pod.name, report)

    async def _reap(
        self, rows: dict[str, _Row], observed: dict[str, ObservedPod], now: float, report: PassReport
    ) -> None:
        hub_settled = now - self._hub.started_at >= HUB_SETTLE_S
        hard_s = self._settings.hold.hard_s
        for row in rows.values():
            pod = observed.get(row.name)
            age = now - row.created_at
            if pod is not None and pod.unschedulable and age > PENDING_TIMEOUT_S:
                reason = "unschedulable"
            elif row.phase in ("creating", "starting") and age > STARTUP_TIMEOUT_S:
                reason = "startup timeout"
            elif row.hold_ready_at is not None and now > row.hold_ready_at + hard_s + HOLD_BACKSTOP_S:
                reason = "hold backstop"
            elif row.phase == "watching" and hub_settled and now - (self._hub.last_heard(row.name) or 0) > SILENCE_S:
                reason = "silent"
            else:
                continue
            log.warning("%s: %s; deleting it", row.name, reason)
            await self._end(row, "failed", reason, failure=reason != "hold backstop")
            await self._delete(row.name, report)

    async def _egress(
        self, rows: dict[str, _Row], observed: dict[str, ObservedPod], now: float, report: PassReport
    ) -> None:
        """Open public HTTPS for a pod while it holds a cart, and close it again after (§7.6)."""
        for row in rows.values():
            pod = observed.get(row.name)
            if pod is None or pod.deleting:
                continue
            want = EGRESS_HOLD if row.hold_ready_at is not None and row.phase == "holding" else EGRESS_ALLOWLIST
            if pod.labels.get(LABEL_EGRESS) == want:
                self._egress_stuck_since.pop(row.name, None)
                continue
            try:
                await self._cluster.set_egress(row.name, want)
            except KubeError as e:
                log.warning("%s: couldn't set egress to %s: %s", row.name, want, e)
                if want == EGRESS_ALLOWLIST:
                    since = self._egress_stuck_since.setdefault(row.name, now)
                    if now - since > EGRESS_GIVE_UP_S:
                        await self._end(row, "failed", "egress stuck open")
                        await self._delete(row.name, report)
                continue
            self._egress_stuck_since.pop(row.name, None)
            await self._pool.execute("UPDATE browser_pods SET egress = $2 WHERE name = $1", row.name, want)
            report.egress.append((row.name, want))

    async def _delete(self, name: str, report: PassReport) -> None:
        try:
            await self._cluster.delete_pod(name)
        except KubeError as e:
            log.warning("Couldn't delete %s: %s", name, e)
            return
        report.deleted.append(name)

    # ---- the plan

    async def _apply_plan(self, rows: dict[str, _Row], now: float, report: PassReport) -> None:
        users, trips, fleet = await self._inputs(now)
        pods = [
            PodView(r.name, r.trip_id, r.user_id, r.mode, r.phase, r.created_at)  # type: ignore[arg-type]
            for r in rows.values()
        ]
        result = plan(users, trips, pods, fleet, self._settings.policy, now)
        at = datetime.fromtimestamp(now, PACIFIC)
        by_trip = {t.id: t for t in trips}
        warm = [by_trip[p.trip_id].trip for p in pods if p.mode == "warm" and p.trip_id in by_trip]
        self.stretch = stretch(
            (curve_interval(t, at, self._settings.policy) for t in warm), self._settings.checks_per_minute
        )
        for drain in result.drain:
            await self._drain(rows[drain.pod], drain.reason, report)
        for start in result.start:
            await self._start(start, by_trip[start.trip_id].trip, now, report)
        await self._record_queue(result.queued, now)

    async def _inputs(self, now: float) -> tuple[list[UserView], list[TripView], FleetView]:
        user_records = await self._pool.fetch("SELECT id, state, paused FROM users")
        overrides: dict[str, dict[str, int]] = {}
        for r in await self._pool.fetch("SELECT user_id, key, value FROM user_limits"):
            overrides.setdefault(r["user_id"], {})[r["key"]] = r["value"]
        users = [
            UserView(
                r["id"],
                active=r["state"] == "active",
                paused=r["paused"],
                max_active_browsers=self._limits.value("maxActiveBrowsers", overrides.get(r["id"], {})),
            )
            for r in user_records
        ]
        trips = []
        for r in await self._pool.fetch(
            "SELECT t.id, t.user_id, t.spec, t.enabled, t.paused_reason, rt.next_check_at, rt.backoff_until,"
            " rt.needs_attention, rt.queued_since FROM trips t LEFT JOIN trip_runtime rt ON rt.trip_id = t.id"
            " WHERE t.archived_at IS NULL"
        ):
            try:
                trip = Trip.from_json(json.loads(r["spec"]) | {"enabled": r["enabled"]})
            except (ValidationError, ValueError):
                log.exception("Trip %s has an invalid spec", r["id"])
                continue
            trips.append(
                TripView(
                    trip,
                    r["user_id"],
                    paused_reason=r["paused_reason"],
                    next_check_at=_epoch(r["next_check_at"]),
                    backoff_until=_epoch(r["backoff_until"]),
                    needs_attention=bool(r["needs_attention"]),
                    queued_since=_epoch(r["queued_since"]),
                )
            )
        switches = {
            r["key"]: json.loads(r["value"]) for r in await self._pool.fetch("SELECT key, value FROM fleet_state")
        }
        starts = await self._pool.fetch(
            "SELECT user_id, max(created_at) AS last FROM browser_pods WHERE created_at > $1 GROUP BY user_id",
            _ts(now - 3600),
        )
        fleet = FleetView(
            max_browsers_total=self._max_total,
            short_check_slots=self._short_slots,
            maintenance=bool(switches.get("maintenance", False)) or switches.get("breaker") == "open",
            starts_last_hour=await self._pool.fetchval(
                "SELECT count(*) FROM browser_pods WHERE created_at > $1", _ts(now - 3600)
            ),
            last_start_by_user={r["user_id"]: r["last"].timestamp() for r in starts},
        )
        return users, trips, fleet

    async def _drain(self, row: _Row, reason: CommandReason, report: PassReport) -> None:
        if not await self._hub.command(row.name, "drain", reason):
            await self._end(row, "gone", f"drained ({reason}) while not connected")
            await self._delete(row.name, report)  # not connected: its own SIGTERM drain runs instead
        else:
            await self._pool.execute("UPDATE browser_pods SET phase = 'draining' WHERE name = $1", row.name)
        if reason == "superseded":
            # Back to short checks, the next one due 6 h after its last check.
            await self._pool.execute(
                "UPDATE trip_runtime SET next_check_at = coalesce(last_check_at, $3) + $2, mode = 'short_check'"
                " WHERE trip_id = $1",
                row.trip_id,
                self._settings.policy.short_check_interval,
                _ts(self._clock()),
            )
        report.drained.append(row.name)

    async def _start(self, start: Start, trip: Trip, now: float, report: PassReport) -> None:
        interval = self._settings.policy.short_check_interval.total_seconds()
        next_check = now + interval * self._rng.uniform(1 - SHORT_CHECK_JITTER, 1 + SHORT_CHECK_JITTER)
        async with self._pool.acquire() as conn, conn.transaction():
            gen = await conn.fetchval(
                "INSERT INTO trip_runtime (trip_id, gen) VALUES ($1, 1) ON CONFLICT (trip_id)"
                " DO UPDATE SET gen = trip_runtime.gen + 1 RETURNING gen",
                trip.id,
            )
            name = pod_name(trip.id, gen)
            await conn.execute(
                "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                name,
                trip.id,
                start.user_id,
                gen,
                start.mode,
                _ts(now + DEADLINE_SECONDS),
                _ts(now),
            )
            await conn.execute(
                "UPDATE trip_runtime SET mode = $2, phase = 'starting', queue_position = NULL, queued_since = NULL,"
                " next_check_at = CASE WHEN $2 = 'short_check' THEN $3 ELSE next_check_at END, updated_at = now()"
                " WHERE trip_id = $1",
                trip.id,
                start.mode,
                _ts(next_check),
            )
        spec = browser_pod(trip.id, gen, start.user_id, self._params, now=now)
        try:
            pod = await self._cluster.create_pod(spec)
        except KubeError as e:
            log.warning("Couldn't create %s: %s", name, e)
            await self._pool.execute(
                "UPDATE browser_pods SET ended_at = $2, phase = 'gone', end_reason = 'create failed' WHERE name = $1",
                name,
                _ts(now),
            )
            return
        await self._pool.execute("UPDATE browser_pods SET uid = $2, phase = 'starting' WHERE name = $1", name, pod.uid)
        await self._pool.execute("SELECT pg_notify($1, $2)", USER_CHANNEL, start.user_id)
        log.info("Started %s (%s)", name, start.mode)
        report.started.append(name)

    async def _record_queue(self, queued: dict[str, int], now: float) -> None:
        for trip_id, position in queued.items():
            await self._pool.execute(
                "INSERT INTO trip_runtime (trip_id, phase, mode, queue_position, queued_since)"
                " VALUES ($1, 'queued', 'warm', $2, $3)"
                " ON CONFLICT (trip_id) DO UPDATE SET phase = 'queued', mode = 'warm', queue_position = $2,"
                " queued_since = coalesce(trip_runtime.queued_since, $3)",
                trip_id,
                position,
                _ts(now),
            )
        await self._pool.execute(
            "UPDATE trip_runtime SET queue_position = NULL, queued_since = NULL"
            " WHERE queue_position IS NOT NULL AND NOT (trip_id = ANY($1::text[]))",
            list(queued),
        )
