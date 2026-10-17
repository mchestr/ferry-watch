"""The HTTP check loop: when each HTTP-checked watch is checked, and what is recorded and said when it is
(docs/waitlist/design.md §4.5).

`HttpChecks` runs in waitlist-control beside the hub and the fleet, under the same run lock. Each tick it reads
the live watches of the configured providers, plans with waitlist.control.checkplan (due watches, coalesced
queries, the request budget, reminders, archives), asks waitlist-fetcher for each distinct query, and records
each watch in its own transaction. Records are per watch, evaluation per query: a failing query never stops a
watch from alerting on what its other queries found. Our own trouble reaching the fetcher (`fetcher_unavailable`,
`bad_reply`) charges no watch (I11). Every write to `trip_runtime.next_check_at` is a compare-and-set against the
value read when planning, so a "Check now" made while a fetch is in flight survives.

It never touches Kubernetes or a browser (an import-linter contract), and is given plain values (the rates, the
fetcher client), never waitlist-control's config.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg
import msgspec

from waitlist import __version__
from waitlist.models import PACIFIC
from waitlist.polling import quiet_hours
from waitlist.providers import REGISTRY, Registry
from waitlist.providers.base import ALERTABLE, CheckErrorKind, HttpProvider, Option, search_hash

from .checkplan import (
    BACKOFF_AFTER,
    BACKOFF_MINUTES,
    BLOCKED_COOLDOWN_S,
    CHECKABLE,
    CHECKER_DOWN_ALERT_AFTER_S,
    FAILING_ALERT_AFTER,
    JITTER,
    MAX_OPENINGS_PER_HOUR,
    MIN_INTERVAL_S,
    MIN_SLEEP_SECONDS,
    OUR_SIDE,
    OUR_SIDE_RETRY_S,
    PROVIDER_WIDE,
    RATE_LIMITED_COOLDOWN_S,
    REALERT_COOLDOWN_S,
    REPORT_SECONDS,
    TICK_SECONDS,
    CheckPlan,
    Job,
    Reminder,
    WatchInput,
    plan_checks,
)
from .fetch import CheckFailed, FetchClient
from .planner import UserView
from .store import USER_CHANNEL

log = logging.getLogger(__name__)

HOUR_S = 3600.0
# Who to tell about a provider's trouble: the first active admin, as every admin-only kind does.
FIRST_ADMIN = "SELECT id FROM users WHERE is_admin AND state = 'active' ORDER BY id LIMIT 1"
WATCHES = (
    "SELECT t.id, t.user_id, t.provider, t.spec, t.enabled, t.paused_reason, t.rev,"
    " rt.next_check_at, rt.backoff_until, rt.quiet_lifted_until, rt.failures,"
    " cr.consecutive_errors, cr.attempted_at, cr.error_since, m.alerted"
    " FROM trips t"
    " LEFT JOIN trip_runtime rt ON rt.trip_id = t.id"
    " LEFT JOIN check_results cr ON cr.trip_id = t.id"
    " LEFT JOIN alert_memory m ON m.trip_id = t.id"
    " WHERE t.archived_at IS NULL AND t.provider = ANY($1::text[])"
)
# The fleet's users query, copied: this module may not import waitlist.control.fleet.
USERS = (
    "SELECT u.id, u.state, u.paused, s.quiet_start, s.quiet_end FROM users u"
    " LEFT JOIN user_settings s ON s.user_id = u.id"
)
# A record's next check, compare-and-set against the value read when planning ($6).
RUNTIME_CAS = (
    "INSERT INTO trip_runtime (trip_id, last_check_at, next_check_at, failures, backoff_until, updated_at)"
    " VALUES ($1, $2, $3, $4, $5, now())"
    " ON CONFLICT (trip_id) DO UPDATE SET"
    " last_check_at = coalesce(EXCLUDED.last_check_at, trip_runtime.last_check_at),"
    " failures = EXCLUDED.failures, backoff_until = EXCLUDED.backoff_until, updated_at = now(),"
    " next_check_at = CASE WHEN trip_runtime.next_check_at IS NOT DISTINCT FROM $6"
    " THEN EXCLUDED.next_check_at ELSE trip_runtime.next_check_at END"
)


def _ts(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch is not None else None


def _epoch(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


@dataclass(slots=True)
class _Bucket:
    """A token bucket: `rate` requests a minute, holding at most `rate`, starting full."""

    rate: int
    tokens: float
    at: float

    def available(self, now: float) -> int:
        self.tokens = min(float(self.rate), self.tokens + max(0.0, now - self.at) * self.rate / 60.0)
        self.at = now
        return int(self.tokens)

    def take(self, n: int) -> None:
        self.tokens -= n


@dataclass(slots=True)
class _Stats:
    """What the check report says of a provider's fetches over the last hour."""

    fetches: deque[float] = field(default_factory=deque)
    errors: deque[float] = field(default_factory=deque)
    last_ok_at: float | None = None
    last_error: CheckErrorKind | None = None
    last_error_at: float | None = None

    def prune(self, now: float) -> None:
        for times in (self.fetches, self.errors):
            while times and times[0] <= now - HOUR_S:
                times.popleft()


@dataclass(frozen=True, slots=True)
class _Watch:
    """A watch as read for this tick: the planner's input, and what recording it needs besides."""

    input: WatchInput
    provider: HttpProvider[Any, Any, Any]
    planned: datetime | None  # trip_runtime.next_check_at exactly as read: the compare-and-set's expected value
    failures: int
    error_since: float | None

    @property
    def id(self) -> str:
        return self.input.id

    @property
    def user_id(self) -> str:
        return self.input.user_id


Outcome = msgspec.Struct | CheckFailed


class HttpChecks:
    def __init__(
        self,
        pool: asyncpg.Pool,
        client: FetchClient,
        *,
        rates: Mapping[str, int],
        registry: Registry = REGISTRY,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._pool = pool
        self._client = client
        self._rates = dict(rates)
        self._registry = registry
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not secrets
        start = clock()
        self._buckets = {p: _Bucket(rate, float(rate), start) for p, rate in self._rates.items()}
        self._stats = {p: _Stats() for p in self._rates}
        self._cooldown_until: dict[str, float] = {}
        self._down_since: dict[str, float] = {}
        self._reminded: set[str] = set()  # reminder dedup keys this process inserted (the database dedups too)
        self._bad_specs: set[tuple[str, int]] = set()  # (id, rev) already logged
        self._reported_at: float | None = None

    # ---- the loop

    async def run(self, stop: asyncio.Event) -> None:
        """Restore the providers' cooldowns, then tick until `stop`: never longer than TICK_SECONDS apart, and at
        the instant something becomes due (a release, a reminder, a next check)."""
        try:
            await self.restore()
        except (asyncpg.PostgresError, OSError):
            log.exception("Couldn't read the last check report; starting without cooldowns")
        while not stop.is_set():
            delay = TICK_SECONDS
            try:
                plan = await self.tick()
                if plan.wake_at is not None:
                    delay = min(TICK_SECONDS, max(MIN_SLEEP_SECONDS, plan.wake_at - self._clock()))
            except Exception:
                log.exception("HTTP check tick failed")  # the next tick tries again
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)

    async def restore(self) -> None:
        """Each provider's cooldown from the last check report, where it's still ahead: a restart doesn't forget a
        site's 429."""
        value = await self._pool.fetchval("SELECT value FROM fleet_state WHERE key = 'check_report'")
        report = json.loads(value) if value else None
        providers = report.get("providers") if isinstance(report, dict) else None
        if not isinstance(providers, dict):
            return
        now = self._clock()
        for provider_id, entry in providers.items():
            until = entry.get("cooldown_until") if isinstance(entry, dict) else None
            if not isinstance(until, int | float) or isinstance(until, bool):
                continue
            if provider_id in self._rates and until > now:
                self._cooldown_until[provider_id] = float(until)

    async def tick(self) -> CheckPlan:
        now = self._clock()
        watches, counts, users, provider_paused = await self._read()
        plan = plan_checks(
            [w.input for w in watches.values()],
            users,
            self._registry,
            provider_paused=provider_paused,
            cooldown_until={p: t for p, t in self._cooldown_until.items() if t > now},
            tokens={p: bucket.available(now) for p, bucket in self._buckets.items()},
            now=now,
        )
        affected = await self._remind(plan.reminders, now)
        affected |= await self._archive(plan.ended, watches, now)
        results = await self._fetch(plan.jobs, now)
        affected |= await self._provider_effects(plan.jobs, results, now)
        for watch_id in sorted({watch_id for job in plan.jobs for watch_id in job.watch_ids}):
            if await self._record(watches[watch_id], results, now):
                affected.add(watches[watch_id].user_id)
        for user_id in sorted(affected):
            await self._pool.execute("SELECT pg_notify($1, $2)", USER_CHANNEL, user_id)
        if self._reported_at is None or now - self._reported_at >= REPORT_SECONDS:
            await self._report(plan, watches, counts, provider_paused, now)
            self._reported_at = now
        return plan

    # ---- 1-2. reading

    async def _read(self) -> tuple[dict[str, _Watch], dict[str, int], dict[str, UserView], frozenset[str]]:
        rows = await self._pool.fetch(WATCHES, sorted(self._rates))
        users = {
            r["id"]: UserView(
                r["id"],
                active=r["state"] == "active",
                paused=r["paused"],
                quiet=quiet_hours(r["quiet_start"], r["quiet_end"]),
            )
            for r in await self._pool.fetch(USERS)
        }
        switches = {
            r["key"]: json.loads(r["value"])
            for r in await self._pool.fetch(
                "SELECT key, value FROM fleet_state WHERE key IN ('maintenance', 'paused_providers')"
            )
        }
        configured = frozenset(self._rates)
        paused = switches.get("paused_providers")
        paused_ids = frozenset(p for p in paused if isinstance(p, str)) if isinstance(paused, list) else frozenset()
        provider_paused = configured if switches.get("maintenance") is True else configured & paused_ids
        watches: dict[str, _Watch] = {}
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["provider"]] = counts.get(r["provider"], 0) + 1
            watch = self._load(r)
            if watch is not None:
                watches[watch.id] = watch
        return watches, counts, users, provider_paused

    def _load(self, r: asyncpg.Record) -> _Watch | None:
        """A row as a watch, its stored spec validated again by its provider (I10); None if it won't load."""
        provider = self._registry.http_provider(r["provider"])
        if provider is None:
            return None
        try:
            spec = provider.load_spec(json.loads(r["spec"]))
            window = provider.window(spec)
        except ValueError as e:  # SpecError, or not JSON
            self._warn(r, f"its spec doesn't load ({e})")
            return None
        except Exception:  # noqa: BLE001 - a provider's bug skips the watch, not the tick
            self._warn(r, "its provider failed to load it", error=True)
            return None
        return _Watch(
            input=WatchInput(
                id=r["id"],
                user_id=r["user_id"],
                provider=r["provider"],
                rev=r["rev"],
                spec=spec,
                window=window,
                enabled=r["enabled"],
                paused_reason=r["paused_reason"],
                next_check_at=_epoch(r["next_check_at"]),
                backoff_until=_epoch(r["backoff_until"]),
                quiet_lifted_until=_epoch(r["quiet_lifted_until"]),
                consecutive_errors=r["consecutive_errors"] or 0,
                attempted_at=_epoch(r["attempted_at"]),
            ),
            provider=provider,
            planned=r["next_check_at"],
            failures=r["failures"] or 0,
            error_since=_epoch(r["error_since"]),
        )

    def _warn(self, r: asyncpg.Record, why: str, *, error: bool = False) -> None:
        key = (r["id"], r["rev"])
        if key in self._bad_specs:
            return
        self._bad_specs.add(key)
        if error:
            log.exception("Skipping watch %s (rev %d): %s", r["id"], r["rev"], why)
        else:
            log.warning("Skipping watch %s (rev %d): %s", r["id"], r["rev"], why)

    # ---- 4-5. reminders and archives

    async def _remind(self, reminders: Sequence[Reminder], now: float) -> set[str]:
        affected: set[str] = set()
        keys = {f"remind:{r.user_id}:{r.date.isoformat()}": r for r in reminders}
        self._reminded &= set(keys)  # the database dedups as well; this only saves a write a tick
        for key, r in keys.items():
            if key in self._reminded:
                continue
            try:
                result = await self._pool.execute(
                    "INSERT INTO notifications (user_id, trip_id, kind, params, dedup_key, created_at)"
                    " VALUES ($1, $2, 'release_reminder', '{}', $3, $4) ON CONFLICT (dedup_key) DO NOTHING",
                    r.user_id,
                    r.watch_id,
                    key,
                    _ts(now),
                )
            except asyncpg.PostgresError:
                log.exception("Couldn't queue the release reminder for watch %s", r.watch_id)
                continue
            self._reminded.add(key)
            if result == "INSERT 0 1":
                affected.add(r.user_id)
        return affected

    async def _archive(self, ended: Iterable[str], watches: Mapping[str, _Watch], now: float) -> set[str]:
        """Clear away watches whose window closed ARCHIVE_AFTER_CLOSE_S ago. An ended watch tells nobody anything."""
        affected: set[str] = set()
        for watch_id in ended:
            watch = watches[watch_id]
            try:
                async with self._pool.acquire() as conn, conn.transaction():
                    result = await conn.execute(
                        "UPDATE trips SET archived_at = now(), updated_at = now()"
                        " WHERE id = $1 AND archived_at IS NULL",
                        watch_id,
                    )
                    if result == "UPDATE 1":
                        await conn.execute(
                            "INSERT INTO events (user_id, trip_id, kind, code, params, dedup_key, ts)"
                            " VALUES ($1, $2, 'check', 'watch_ended', '{}', $3, $4) ON CONFLICT (dedup_key) DO NOTHING",
                            watch.user_id,
                            watch_id,
                            f"ended:{watch_id}",
                            _ts(now),
                        )
                        affected.add(watch.user_id)
            except asyncpg.PostgresError:
                log.exception("Couldn't archive ended watch %s", watch_id)
        return affected

    # ---- 6-7. fetching, and what a provider's answers mean for all of its watches

    async def _fetch(self, jobs: Sequence[Job], now: float) -> dict[tuple[str, msgspec.Struct], Outcome]:
        limits = {p: asyncio.Semaphore(self._registry.http[p].budget.concurrency) for p in {j.provider for j in jobs}}

        async def one(job: Job) -> Outcome:
            async with limits[job.provider]:
                try:
                    return await self._client.fetch(self._registry.http[job.provider], job.query)
                except CheckFailed as e:
                    return e
                except Exception:  # our own bug: charged to nobody, like a bad reply
                    log.exception("Fetching a %s query failed unexpectedly", job.provider)
                    return CheckFailed("bad_reply")

        for job in jobs:
            self._buckets[job.provider].take(1)
        outcomes = await asyncio.gather(*(one(job) for job in jobs))
        for job, outcome in zip(jobs, outcomes, strict=True):
            stats = self._stats[job.provider]
            stats.fetches.append(now)
            if isinstance(outcome, CheckFailed):
                stats.errors.append(now)
                stats.last_error, stats.last_error_at = outcome.kind, now
            else:
                stats.last_ok_at = now
        return {(job.provider, job.query): outcome for job, outcome in zip(jobs, outcomes, strict=True)}

    async def _provider_effects(
        self, jobs: Sequence[Job], results: Mapping[tuple[str, msgspec.Struct], Outcome], now: float
    ) -> set[str]:
        affected: set[str] = set()
        by_provider: dict[str, list[Outcome]] = {}
        for job in jobs:
            by_provider.setdefault(job.provider, []).append(results[(job.provider, job.query)])
        for provider_id, outcomes in by_provider.items():
            failures = [o for o in outcomes if isinstance(o, CheckFailed)]
            for f in failures:
                # Only a site's own 429 or 401/403 cools the provider down; the fetcher's own cap arrives as
                # fetcher_unavailable and costs nothing.
                if f.kind == "rate_limited":
                    self._cool(provider_id, now + max(RATE_LIMITED_COOLDOWN_S, f.retry_after_s or 0))
                elif f.kind == "blocked":
                    self._cool(provider_id, now + BLOCKED_COOLDOWN_S)
            if any(f.kind == "site_changed" for f in failures):
                day = datetime.fromtimestamp(now, PACIFIC).strftime("%Y-%m-%d")
                affected |= await self._tell_admin(
                    "site_changed", {"provider": provider_id}, f"site_changed:{provider_id}:{day}", now
                )
            if all(isinstance(o, CheckFailed) and not o.replied for o in outcomes):
                self._down_since.setdefault(provider_id, now)
            else:
                self._down_since.pop(provider_id, None)
            since = self._down_since.get(provider_id)
            if since is not None and now - since >= CHECKER_DOWN_ALERT_AFTER_S:
                hour = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d%H")
                affected |= await self._tell_admin(
                    "checker_down",
                    {"provider": provider_id, "since": since},
                    f"checker_down:{provider_id}:{hour}",
                    now,
                )
        return affected

    def _cool(self, provider_id: str, until: float) -> None:
        self._cooldown_until[provider_id] = max(until, self._cooldown_until.get(provider_id, 0.0))

    async def _tell_admin(self, kind: str, params: dict[str, Any], dedup_key: str, now: float) -> set[str]:
        """An admin-only notification (waitlist-web renders it), to the first active admin; none: nobody."""
        try:
            admin = await self._pool.fetchval(FIRST_ADMIN)
            if admin is None:
                return set()
            result = await self._pool.execute(
                "INSERT INTO notifications (user_id, trip_id, kind, params, dedup_key, created_at)"
                " VALUES ($1, NULL, $2, $3, $4, $5) ON CONFLICT (dedup_key) DO NOTHING",
                admin,
                kind,
                json.dumps(params),
                dedup_key,
                _ts(now),
            )
        except asyncpg.PostgresError:
            log.exception("Couldn't queue the %s notification", kind)
            return set()
        return {admin} if result == "INSERT 0 1" else set()

    # ---- 8. recording one watch

    async def _record(self, watch: _Watch, results: Mapping[tuple[str, msgspec.Struct], Outcome], now: float) -> bool:
        """One watch's record, in its own transaction. False: dropped (the watch changed since planning, or the
        record failed) or nothing anyone sees changed (our own failure)."""
        queries = watch.provider.queries(watch.input.spec)
        outcomes = [results[(watch.input.provider, q)] for q in queries]
        ok = {q: o for q, o in zip(queries, outcomes, strict=True) if not isinstance(o, CheckFailed)}
        failed = [o for o in outcomes if isinstance(o, CheckFailed)]
        theirs = [f for f in failed if f.kind not in OUR_SIDE]
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "SELECT rev, enabled, archived_at, paused_reason FROM trips WHERE id = $1 FOR KEY SHARE", watch.id
                )
                if (
                    row is None
                    or row["rev"] != watch.input.rev
                    or row["archived_at"] is not None
                    or not row["enabled"]
                    or row["paused_reason"] is not None
                ):
                    return False  # deleted, edited, archived, disabled or paused since planning: the next tick replans
                if ok:
                    options, outcome = await self._evaluate(conn, watch, ok, now)
                    if not failed:
                        await self._success(conn, watch, options, outcome, now)
                    elif theirs:
                        await self._partial(conn, watch, options, theirs, ours=len(theirs) < len(failed), now=now)
                    else:
                        await self._partial_ours(conn, watch, options, outcome, now)
                elif theirs:
                    await self._provider_failure(conn, watch, theirs, now)
                else:
                    await conn.execute(
                        "UPDATE trip_runtime SET next_check_at = $2, updated_at = now()"
                        " WHERE trip_id = $1 AND next_check_at IS NOT DISTINCT FROM $3",
                        watch.id,
                        _ts(now + OUR_SIDE_RETRY_S),
                        watch.planned,
                    )
                    return False
        except asyncpg.PostgresError:
            log.exception("Recording watch %s's check failed", watch.id)
            return False
        except Exception:  # a provider's bug drops this watch's record, not the tick's
            log.exception("Evaluating watch %s's check failed", watch.id)
            return False
        return True

    def _interval_at(self, watch: _Watch, now: float) -> float:
        """The next check after one at `now`: the provider's interval, ±JITTER, never under MIN_INTERVAL_S."""
        interval = watch.provider.interval(watch.input.spec, datetime.fromtimestamp(now, UTC)).total_seconds()
        return now + max(MIN_INTERVAL_S, interval * self._rng.uniform(1 - JITTER, 1 + JITTER))

    def _search_hash(self, watch: _Watch) -> str:
        return search_hash(watch.provider.search_key(watch.input.spec))

    async def _runtime(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        *,
        last_check_at: float | None,
        next_check_at: float,
        failures: int,
        backoff_until: float | None,
    ) -> None:
        await conn.execute(
            RUNTIME_CAS,
            watch.id,
            _ts(last_check_at),
            _ts(next_check_at),
            failures,
            _ts(backoff_until),
            watch.planned,
        )

    async def _event(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        code: str,
        params: dict[str, Any],
        now: float,
        dedup_key: str | None = None,
    ) -> None:
        await conn.execute(
            "INSERT INTO events (user_id, trip_id, kind, code, params, dedup_key, ts)"
            " VALUES ($1, $2, 'check', $3, $4, $5, $6) ON CONFLICT (dedup_key) DO NOTHING",
            watch.user_id,
            watch.id,
            code,
            json.dumps(params),
            dedup_key,
            _ts(now),
        )

    async def _notify_owner(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        kind: str,
        params: dict[str, Any],
        dedup_key: str,
        now: float,
    ) -> None:
        await conn.execute(
            "INSERT INTO notifications (user_id, trip_id, kind, params, dedup_key, created_at)"
            " VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (dedup_key) DO NOTHING",
            watch.user_id,
            watch.id,
            kind,
            json.dumps(params),
            dedup_key,
            _ts(now),
        )

    async def _evaluate(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        ok: Mapping[msgspec.Struct, msgspec.Struct],
        now: float,
    ) -> tuple[tuple[Option, ...], str]:
        """The options from the queries that answered, the openings they make, and the watch's alert memory."""
        options: tuple[Option, ...] = watch.provider.evaluate(watch.input.spec, ok, datetime.fromtimestamp(now, UTC))
        status = {o.key: o.status for o in options}
        wanted = {o.status for o in options if o.wanted}
        found = {"open", "maybe", "sold_out", "not_released"}
        outcome = "no_inventory" if not wanted & found and "not_offered" in wanted else "ok"
        unchecked = {o.key for o in options if o.status == "unchecked"}  # not seen this round: memory kept
        alertable = [o.key for o in options if o.wanted and o.status in ALERTABLE]
        previous = set(await conn.fetchval("SELECT alerted FROM alert_memory WHERE trip_id = $1", watch.id) or ())
        gone = previous - set(alertable) - unchecked
        recent: set[str] = set()
        if gone:
            recent = {
                r["option"]
                for r in await conn.fetch(
                    "SELECT DISTINCT params->>'option' AS option FROM events WHERE trip_id = $1"
                    " AND code = 'opening_seen' AND ts > $2 AND params->>'option' = ANY($3::text[])",
                    watch.id,
                    _ts(now - REALERT_COOLDOWN_S),
                    sorted(gone),
                )
            }
        fresh = [k for k in alertable if k not in previous]
        for key in [o.key for o in options if o.key in gone - recent] + sorted(gone - recent - set(status)):
            await self._event(conn, watch, "opening_gone", {"option": key}, now)
        if fresh:
            n = await conn.fetchval(
                "SELECT count(*) FROM notifications WHERE trip_id = $1 AND kind = 'opening' AND created_at > $2",
                watch.id,
                _ts(now - HOUR_S),
            )
            if n >= MAX_OPENINGS_PER_HOUR:  # not remembered: they alert once the hour allows
                hour = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d%H")
                await self._event(conn, watch, "alert_held_back", {"count": n}, now, f"held:{watch.id}:{hour}")
                fresh = []
            else:
                for key in fresh:
                    await self._event(conn, watch, "opening_seen", {"option": key}, now)
                uncertain = [k for k in fresh if status[k] == "maybe"]
                await self._notify_owner(
                    conn,
                    watch,
                    "opening",
                    {"options": fresh, "uncertain": uncertain},
                    f"open:{watch.id}:{int(now * 1000)}",
                    now,
                )
        memory = sorted((previous & (set(alertable) | unchecked)) | recent | set(fresh))
        await conn.execute(
            "INSERT INTO alert_memory (trip_id, key_hash, alerted, offered) VALUES ($1, '', $2, '{}')"
            " ON CONFLICT (trip_id) DO UPDATE SET key_hash = '', alerted = $2, offered = '{}'",
            watch.id,
            memory,
        )
        return options, outcome

    async def _success(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        options: Sequence[Option],
        outcome: str,
        now: float,
    ) -> None:
        await conn.execute(
            "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at,"
            " error_class, consecutive_errors, error_since, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $5, NULL, 0, NULL, now())"
            " ON CONFLICT (trip_id) DO UPDATE SET search_hash = EXCLUDED.search_hash, outcome = EXCLUDED.outcome,"
            " options = EXCLUDED.options, attempted_at = EXCLUDED.attempted_at, checked_at = EXCLUDED.checked_at,"
            " error_class = NULL, consecutive_errors = 0, error_since = NULL, updated_at = now()",
            watch.id,
            self._search_hash(watch),
            outcome,
            msgspec.json.encode(list(options)).decode(),
            _ts(now),
        )
        await self._runtime(
            conn, watch, last_check_at=now, next_check_at=self._interval_at(watch, now), failures=0, backoff_until=None
        )

    async def _partial(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        options: Sequence[Option],
        theirs: Sequence[CheckFailed],
        *,
        ours: bool,
        now: float,
    ) -> None:
        """Part of the watch failed on the site's side: what did answer was alerted on; an error is counted, but the
        watch never backs off (its healthy queries keep their pace)."""
        kind = theirs[0].kind
        errors = watch.input.consecutive_errors + 1
        since = watch.error_since or now
        await conn.execute(
            "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at,"
            " error_class, consecutive_errors, error_since, updated_at)"
            " VALUES ($1, $2, 'error', $3, $4, $4, $5, $6, $7, now())"
            " ON CONFLICT (trip_id) DO UPDATE SET search_hash = EXCLUDED.search_hash, outcome = 'error',"
            " options = EXCLUDED.options, attempted_at = EXCLUDED.attempted_at, checked_at = EXCLUDED.checked_at,"
            " error_class = EXCLUDED.error_class, consecutive_errors = EXCLUDED.consecutive_errors,"
            " error_since = EXCLUDED.error_since, updated_at = now()",
            watch.id,
            self._search_hash(watch),
            msgspec.json.encode(list(options)).decode(),
            _ts(now),
            kind,
            errors,
            _ts(since),
        )
        if any(f.kind in PROVIDER_WIDE for f in theirs):  # the provider waits
            next_check = self._cooldown_until.get(watch.input.provider, now + OUR_SIDE_RETRY_S)
        elif ours:
            next_check = now + OUR_SIDE_RETRY_S
        else:
            next_check = self._interval_at(watch, now)
        await self._runtime(
            conn, watch, last_check_at=now, next_check_at=next_check, failures=errors, backoff_until=None
        )
        await self._failure_notes(conn, watch, kind, errors, since, now)

    async def _partial_ours(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        options: Sequence[Option],
        outcome: str,
        now: float,
    ) -> None:
        """Part of the watch failed on our side (I11): what did answer counts, our trouble changes no counter, no
        outcome and no event."""
        await conn.execute(
            "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at)"
            " VALUES ($1, $2, $3, $4, $5, $5)"
            " ON CONFLICT (trip_id) DO UPDATE SET search_hash = EXCLUDED.search_hash, options = EXCLUDED.options,"
            " attempted_at = EXCLUDED.attempted_at, checked_at = EXCLUDED.checked_at, updated_at = now()",
            watch.id,
            self._search_hash(watch),
            outcome,
            msgspec.json.encode(list(options)).decode(),
            _ts(now),
        )
        await self._runtime(
            conn,
            watch,
            last_check_at=now,
            next_check_at=now + OUR_SIDE_RETRY_S,
            failures=watch.failures,
            backoff_until=watch.input.backoff_until,
        )

    async def _provider_failure(
        self, conn: asyncpg.pool.PoolConnectionProxy, watch: _Watch, theirs: Sequence[CheckFailed], now: float
    ) -> None:
        """Every query failed, at least one on the site's side: no evaluation, the last options kept. The only case
        that backs the watch off."""
        kind = theirs[0].kind
        errors = watch.input.consecutive_errors + 1
        since = watch.error_since or now
        await conn.execute(
            "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at,"
            " error_class, consecutive_errors, error_since, updated_at)"
            " VALUES ($1, $2, 'error', '[]', $3, $4, $5, $6, now())"
            " ON CONFLICT (trip_id) DO UPDATE SET outcome = 'error', error_class = EXCLUDED.error_class,"
            " consecutive_errors = EXCLUDED.consecutive_errors, error_since = EXCLUDED.error_since,"
            " attempted_at = EXCLUDED.attempted_at, updated_at = now()",
            watch.id,
            self._search_hash(watch),
            _ts(now),
            kind,
            errors,
            _ts(since),
        )
        backoff: float | None = None
        if any(f.kind in PROVIDER_WIDE for f in theirs):  # the provider waits, not the watch
            next_check = self._cooldown_until.get(watch.input.provider, now + OUR_SIDE_RETRY_S)
        else:
            if errors >= BACKOFF_AFTER:
                backoff = now + BACKOFF_MINUTES[min(errors - BACKOFF_AFTER, len(BACKOFF_MINUTES) - 1)] * 60
            next_check = max(backoff or 0.0, self._interval_at(watch, now))
        await self._runtime(
            conn, watch, last_check_at=None, next_check_at=next_check, failures=errors, backoff_until=backoff
        )
        await self._failure_notes(conn, watch, kind, errors, since, now)

    async def _failure_notes(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
        watch: _Watch,
        kind: CheckErrorKind,
        errors: int,
        since: float,
        now: float,
    ) -> None:
        """A streak's first error and its FAILING_ALERT_AFTER-th are events; the latter also tells the owner, once
        per streak."""
        if errors in (1, FAILING_ALERT_AFTER):
            await self._event(conn, watch, "check_failed", {"class": kind}, now)
        if errors == FAILING_ALERT_AFTER:
            await self._notify_owner(
                conn, watch, "failing", {"error_class": kind}, f"failing:{watch.id}:{int(since)}", now
            )

    # ---- 10. the check report

    async def _report(
        self,
        plan: CheckPlan,
        watches: Mapping[str, _Watch],
        counts: Mapping[str, int],
        provider_paused: frozenset[str],
        now: float,
    ) -> None:
        at = datetime.fromtimestamp(now, UTC)
        providers: dict[str, Any] = {}
        for provider_id, rate in sorted(self._rates.items()):
            mine = [w for w in watches.values() if w.input.provider == provider_id]
            checking = [w for w in mine if plan.states.get(w.id) in CHECKABLE]
            intervals: dict[msgspec.Struct, float] = {}  # each distinct query's quickest interval
            for w in checking:
                seconds = max(MIN_INTERVAL_S, w.provider.interval(w.input.spec, at).total_seconds())
                for q in w.provider.queries(w.input.spec):
                    intervals[q] = min(intervals.get(q, seconds), seconds)
            stats = self._stats[provider_id]
            stats.prune(now)
            down = self._down_since.get(provider_id)
            cooldown = self._cooldown_until.get(provider_id)
            paused = provider_id in provider_paused
            providers[provider_id] = {
                "running": not paused,
                "paused": paused,
                "fetcher": "down" if down is not None else "ok",
                "fetcher_down_since": down,
                "budget_per_minute": rate,
                "demand_per_minute": round(sum(60.0 / s for s in intervals.values()), 2),
                "fetches_last_hour": len(stats.fetches),
                "errors_last_hour": len(stats.errors),
                "last_ok_at": stats.last_ok_at,
                "last_error": stats.last_error,
                "last_error_at": stats.last_error_at,
                "cooldown_until": cooldown if cooldown is not None and cooldown > now else None,
                "watches": counts.get(provider_id, 0),
                "checking": len(checking),
                "deferred": plan.deferred.get(provider_id, 0),
            }
        report = {"at": now, "version": __version__, "providers": providers}
        await self._pool.execute(
            "INSERT INTO fleet_state (key, value) VALUES ('check_report', $1)"
            " ON CONFLICT (key) DO UPDATE SET value = $1",
            json.dumps(report),
        )
