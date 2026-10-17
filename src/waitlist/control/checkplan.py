"""What the HTTP check loop does next, and where each HTTP-checked watch's checking is (docs/waitlist/design.md §4.5).

Pure: no database, no aiohttp, no clock reads (`now` is passed in) and no randomness. waitlist-control's check
loop (waitlist.control.checks) plans each tick with `plan_checks`; waitlist-web imports `check_state` too, so the
dashboard and the loop always agree on a watch's state.

A watch is checked from its window's `opens_at` (plus RELEASE_DELAY_S) until `closes_at`. Each tick takes the due
watches oldest first and admits one only if all of its queries fit in its provider's request budget; identical
queries are coalesced across watches and users, and every other checkable watch whose queries all made the round
rides along for free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

import msgspec

from waitlist.providers import REGISTRY, Registry
from waitlist.providers.base import CheckErrorKind, CheckState, CheckWindow

from .planner import UserView

TICK_SECONDS = 5.0  # the longest the loop sleeps: web's "Check now" and new watches are seen within this
MIN_SLEEP_SECONDS = 0.25
REPORT_SECONDS = 30.0  # fleet_state 'check_report' cadence
MIN_INTERVAL_S = 30.0  # floor for any provider's interval
JITTER = 0.10  # ±10 % on every computed next_check_at
RELEASE_DELAY_S = 2.0  # the first check this long after window.opens_at
OUR_SIDE_RETRY_S = 30.0  # fetcher_unavailable / bad_reply: try again this soon, charging nothing
CHECKER_DOWN_ALERT_AFTER_S = 180.0
BACKOFF_AFTER = 3  # consecutive provider-side errors before a watch backs off
BACKOFF_MINUTES = (5, 10, 20, 40, 60)
FAILING_ALERT_AFTER = 5  # consecutive provider-side errors before the owner is told (once per streak)
MAX_OPENINGS_PER_HOUR = 6  # per watch; more are held back (flapping, or a forged inventory)
REALERT_COOLDOWN_S = 120.0  # an option that closes within this of being alerted stays remembered
RATE_LIMITED_COOLDOWN_S = 300  # provider-wide after a 429, or Retry-After if longer (≤ 3600)
BLOCKED_COOLDOWN_S = 1800  # provider-wide after a 401/403
ARCHIVE_AFTER_CLOSE_S = 43_200  # control archives an HTTP watch 12 h after its window closes
STALE_REPORT_S = 120.0  # web: an older check_report means checks aren't running

CHECKABLE: frozenset[CheckState] = frozenset({"pending", "checking", "error"})
OUR_SIDE: frozenset[CheckErrorKind] = frozenset({"fetcher_unavailable", "bad_reply"})
PROVIDER_WIDE: frozenset[CheckErrorKind] = frozenset({"rate_limited", "blocked"})
NO_REMINDER: frozenset[CheckState] = frozenset({"paused", "provider_paused", "over"})


def _at(now: float) -> datetime:
    return datetime.fromtimestamp(now, UTC)


def check_state(  # noqa: PLR0911 - one return per state, in precedence order
    *,
    enabled: bool,
    paused_reason: str | None,
    owner_active: bool,
    window: CheckWindow,
    quiet: bool,
    provider_paused: bool,
    provider_running: bool,
    backoff_until: float | None,
    cooldown_until: float | None,
    consecutive_errors: int,
    attempted_at: float | None,
    now: float,
) -> CheckState:
    """The first that applies:
     1 paused           not enabled, paused_reason set, or not owner_active
     2 over             window.phase(now) == "after"
     3 provider_paused  maintenance is on, or the provider is in fleet_state.paused_providers
     4 waiting_release  window.phase(now) == "before"
     5 quiet            in the owner's quiet hours and not lifted (trip_runtime.quiet_lifted_until)
     6 unavailable      not provider_running
     7 backoff          backoff_until > now, or the provider's cooldown_until > now
     8 error            consecutive_errors >= BACKOFF_AFTER   (still checked)
     9 pending          attempted_at is None
    10 checking"""
    phase = window.phase(_at(now))
    if not enabled or paused_reason is not None or not owner_active:
        return "paused"
    if phase == "after":
        return "over"
    if provider_paused:
        return "provider_paused"
    if phase == "before":
        return "waiting_release"
    if quiet:
        return "quiet"
    if not provider_running:
        return "unavailable"
    if (backoff_until is not None and backoff_until > now) or (cooldown_until is not None and cooldown_until > now):
        return "backoff"
    if consecutive_errors >= BACKOFF_AFTER:
        return "error"
    if attempted_at is None:
        return "pending"
    return "checking"


def owner_active(user: UserView | None) -> bool:
    """Whether a watch's owner lets it be checked: active and not "Pause all"-ed (the pre-0004 users.paused, which
    the fleet's planner honours too). A user missing from the users query is not active."""
    return user is not None and user.active and not user.paused


def in_quiet_hours(user: UserView | None, quiet_lifted_until: float | None, now: float) -> bool:
    """In the owner's quiet hours at `now`, and not lifted for this watch (a manual check lifts them)."""
    if user is None or not user.quiet_now(_at(now)):
        return False
    return not (quiet_lifted_until is not None and now < quiet_lifted_until)


@dataclass(frozen=True, slots=True)
class WatchInput:
    id: str
    user_id: str
    provider: str
    rev: int
    spec: object  # the provider's loaded SpecT
    window: CheckWindow
    enabled: bool
    paused_reason: str | None
    next_check_at: float | None
    backoff_until: float | None
    quiet_lifted_until: float | None
    consecutive_errors: int
    attempted_at: float | None


@dataclass(frozen=True, slots=True)
class Job:  # one upstream request
    provider: str
    query: msgspec.Struct
    watch_ids: tuple[str, ...]  # sorted: every checkable watch whose queries this round includes in full


@dataclass(frozen=True, slots=True)
class Reminder:
    watch_id: str  # the lowest id among the owner's watches for that date
    user_id: str
    date: date  # the watch date (Pacific)


@dataclass(slots=True)
class CheckPlan:
    jobs: list[Job]
    reminders: list[Reminder]
    states: dict[str, CheckState]  # every watch's state at `now`
    deferred: dict[str, int]  # per provider: due watches that didn't fit the budget this round
    ended: list[str]  # watch ids whose window closed ≥ ARCHIVE_AFTER_CLOSE_S ago
    wake_at: float | None  # the earliest future instant at which something becomes due


def watch_state(
    watch: WatchInput,
    user: UserView | None,
    *,
    provider_paused: bool,
    provider_running: bool,
    cooldown_until: float | None,
    now: float,
) -> CheckState:
    """check_state with a watch's own inputs: what both the loop and waitlist-web call."""
    return check_state(
        enabled=watch.enabled,
        paused_reason=watch.paused_reason,
        owner_active=owner_active(user),
        window=watch.window,
        quiet=in_quiet_hours(user, watch.quiet_lifted_until, now),
        provider_paused=provider_paused,
        provider_running=provider_running,
        backoff_until=watch.backoff_until,
        cooldown_until=cooldown_until,
        consecutive_errors=watch.consecutive_errors,
        attempted_at=watch.attempted_at,
        now=now,
    )


def _released(watch: WatchInput, now: float) -> bool:
    return now >= watch.window.opens_at.timestamp() + RELEASE_DELAY_S


def plan_checks(
    watches: Sequence[WatchInput],
    users: Mapping[str, UserView],
    registry: Registry = REGISTRY,
    *,
    provider_paused: frozenset[str],
    cooldown_until: Mapping[str, float],
    tokens: Mapping[str, int],
    now: float,
) -> CheckPlan:
    """The tick's jobs, reminders and archives, every watch's state, and when to wake next (design.md §4.5)."""
    states: dict[str, CheckState] = {}
    queries: dict[str, tuple[msgspec.Struct, ...]] = {}
    for w in watches:
        provider = registry.http_provider(w.provider)
        states[w.id] = watch_state(
            w,
            users.get(w.user_id),
            provider_paused=w.provider in provider_paused,
            provider_running=True,  # control is the runner: it keeps trying while the fetcher is down
            cooldown_until=cooldown_until.get(w.provider),
            now=now,
        )
        queries[w.id] = provider.queries(w.spec) if provider is not None else ()
    checkable = [w for w in watches if states[w.id] in CHECKABLE and queries[w.id] and _released(w, now)]
    due = sorted(
        (w for w in checkable if (w.next_check_at or 0.0) <= now),
        key=lambda w: (w.next_check_at if w.next_check_at is not None else w.window.opens_at.timestamp(), w.id),
    )
    jobs, included = _jobs(checkable, due, queries, tokens)
    deferred: dict[str, int] = {}
    for w in due:
        if w.id not in included:
            deferred[w.provider] = deferred.get(w.provider, 0) + 1
    return CheckPlan(
        jobs=jobs,
        reminders=_reminders(watches, users, registry, states, now),
        states=states,
        deferred=deferred,
        # 6. Watches whose window closed long enough ago to clear away.
        ended=sorted(w.id for w in watches if now >= w.window.closes_at.timestamp() + ARCHIVE_AFTER_CLOSE_S),
        wake_at=_wake_at(watches, registry, states, cooldown_until, now),
    )


def _jobs(
    checkable: Sequence[WatchInput],
    due: Sequence[WatchInput],
    queries: Mapping[str, tuple[msgspec.Struct, ...]],
    tokens: Mapping[str, int],
) -> tuple[list[Job], set[str]]:
    """Rules 2-4: the due watches, oldest first, each admitted only if all of its queries fit what's left of its
    provider's budget (a query already in the round costs nothing); then every checkable watch whose queries are
    all in the round rides along; one job per distinct query. Also returns the watches the round includes."""
    left = dict(tokens)
    in_round: set[tuple[str, msgspec.Struct]] = set()
    for w in due:
        new = {(w.provider, q) for q in queries[w.id]} - in_round
        if len(new) <= left.get(w.provider, 0):
            left[w.provider] = left.get(w.provider, 0) - len(new)
            in_round |= new
    riders: dict[tuple[str, msgspec.Struct], list[str]] = {key: [] for key in in_round}
    included: set[str] = set()
    for w in checkable:
        keys = [(w.provider, q) for q in queries[w.id]]
        if all(key in in_round for key in keys):
            included.add(w.id)
            for key in keys:
                riders[key].append(w.id)
    ordered = sorted(riders.items(), key=lambda item: (item[0][0], min(item[1]), repr(item[0][1])))
    return [Job(provider=p, query=q, watch_ids=tuple(sorted(ids))) for (p, q), ids in ordered], included


def _reminders(
    watches: Sequence[WatchInput],
    users: Mapping[str, UserView],
    registry: Registry,
    states: Mapping[str, CheckState],
    now: float,
) -> list[Reminder]:
    """Rule 5: once per owner and watch date, from reminder_at until the release, never in quiet hours."""
    found: dict[tuple[str, date], Reminder] = {}
    for w in watches:
        provider = registry.http_provider(w.provider)
        user = users.get(w.user_id)
        if provider is None or states[w.id] in NO_REMINDER or user is None or user.quiet_now(_at(now)):
            continue
        remind = provider.reminder_at(w.spec)
        if remind is None or not remind.timestamp() <= now < w.window.opens_at.timestamp():
            continue
        day = provider.watch_date(w.spec)
        current = found.get((w.user_id, day))
        if current is None or w.id < current.watch_id:
            found[(w.user_id, day)] = Reminder(watch_id=w.id, user_id=w.user_id, date=day)
    return sorted(found.values(), key=lambda r: (r.user_id, r.date))


def _wake_at(
    watches: Sequence[WatchInput],
    registry: Registry,
    states: Mapping[str, CheckState],
    cooldown_until: Mapping[str, float],
    now: float,
) -> float | None:
    """Rule 7: the earliest future instant at which something becomes due (a release, a reminder, a next check, an
    archive), or None."""
    instants: list[float] = []
    for w in watches:
        opens, closes = w.window.opens_at.timestamp(), w.window.closes_at.timestamp()
        state = states[w.id]
        if state == "waiting_release":
            instants.append(opens + RELEASE_DELAY_S)
        provider = registry.http_provider(w.provider)
        remind = provider.reminder_at(w.spec) if provider is not None else None
        if remind is not None:
            instants.append(remind.timestamp())
        if state in CHECKABLE:  # due at its next check, and never before the release delay
            instants.append(max(w.next_check_at or 0.0, opens + RELEASE_DELAY_S))
        elif state == "backoff":
            instants.append(max(w.next_check_at or 0.0, w.backoff_until or 0.0, cooldown_until.get(w.provider, 0.0)))
        instants.append(closes + ARCHIVE_AFTER_CLOSE_S)
    future = [t for t in instants if t > now]
    return min(future) if future else None
