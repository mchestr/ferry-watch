"""The HTTP check loop's planning, pure (docs/waitlist/design.md §4.5): states, due watches, coalescing, the
budget, reminders, archives and when to wake."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any

import pytest

from tests.fakes import FakeHttpProvider, FakeSpec, fake_registry
from waitlist.control.checkplan import (
    ARCHIVE_AFTER_CLOSE_S,
    BACKOFF_AFTER,
    MIN_INTERVAL_S,
    RELEASE_DELAY_S,
    TICK_SECONDS,
    WatchInput,
    check_state,
    plan_checks,
)
from waitlist.control.planner import UserView
from waitlist.models import PACIFIC
from waitlist.polling import QuietHours
from waitlist.providers import REGISTRY as PROVIDERS
from waitlist.providers.base import CheckWindow
from waitlist.providers.buntzen import PROVIDER as BUNTZEN
from waitlist.providers.buntzen import BuntzenSpec
from waitlist.providers.buntzen.model import Pass

DAY = date(2027, 7, 1)
PROVIDER = FakeHttpProvider()
REGISTRY = fake_registry(PROVIDER)
WINDOW = PROVIDER.window(FakeSpec(DAY))
OPENS = WINDOW.opens_at.timestamp()  # 07:00 PT on Jun 30
CLOSES = WINDOW.closes_at.timestamp()  # 18:00 PT on Jul 1
NOON = datetime.combine(DAY, time(12, 0), tzinfo=PACIFIC).timestamp()
ANN, BOB = "a" * 20, "b" * 20
NO_QUIET = QuietHours(time(3, 0), time(3, 1))
USERS = {u: UserView(u, quiet=NO_QUIET) for u in (ANN, BOB)}


def watch(watch_id: str, user: str = ANN, day: date = DAY, **changes: Any) -> WatchInput:
    spec = FakeSpec(day)
    base = WatchInput(
        id=watch_id,
        user_id=user,
        provider="buntzen",
        rev=1,
        spec=spec,
        window=PROVIDER.window(spec),
        enabled=True,
        paused_reason=None,
        next_check_at=None,
        backoff_until=None,
        quiet_lifted_until=None,
        consecutive_errors=0,
        attempted_at=None,
    )
    return replace(base, **changes)


def plan(watches: list[WatchInput], *, now: float = NOON, tokens: int = 12, **kw: Any) -> Any:
    return plan_checks(
        watches,
        kw.pop("users", USERS),
        REGISTRY,
        provider_paused=kw.pop("provider_paused", frozenset()),
        cooldown_until=kw.pop("cooldown_until", {}),
        tokens={"buntzen": tokens},
        now=now,
        checked_at=kw.pop("checked_at", None),
    )


# ---- check_state


def state(**changes: Any) -> str:
    inputs: dict[str, Any] = {
        "enabled": True,
        "paused_reason": None,
        "owner_active": True,
        "window": WINDOW,
        "quiet": False,
        "provider_paused": False,
        "provider_running": True,
        "backoff_until": None,
        "cooldown_until": None,
        "consecutive_errors": 0,
        "attempted_at": NOON - 60,
        "now": NOON,
    } | changes
    return check_state(**inputs)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"enabled": False, "now": CLOSES + 1}, "paused"),  # 1 before 2
        ({"paused_reason": "after_checkout"}, "paused"),
        ({"owner_active": False, "provider_paused": True}, "paused"),
        ({"now": CLOSES, "provider_paused": True}, "over"),  # 2 before 3
        ({"provider_paused": True, "now": OPENS - 1}, "provider_paused"),  # 3 before 4
        ({"now": OPENS - 1, "quiet": True}, "waiting_release"),  # 4 before 5
        ({"quiet": True, "provider_running": False}, "quiet"),  # 5 before 6
        ({"provider_running": False, "backoff_until": NOON + 60}, "unavailable"),  # 6 before 7
        ({"backoff_until": NOON + 60, "consecutive_errors": 9}, "backoff"),  # 7 before 8
        ({"cooldown_until": NOON + 60}, "backoff"),
        ({"backoff_until": NOON, "cooldown_until": NOON - 1}, "checking"),  # only a future one counts
        ({"consecutive_errors": BACKOFF_AFTER, "attempted_at": None}, "error"),  # 8 before 9
        ({"consecutive_errors": BACKOFF_AFTER - 1, "attempted_at": None}, "pending"),
        ({}, "checking"),
        ({"now": OPENS}, "checking"),  # the window is open from opens_at
    ],
)
def test_check_state_takes_the_first_that_applies(changes: dict[str, Any], expected: str) -> None:
    assert state(**changes) == expected


def test_web_reads_checks_not_running_as_unavailable() -> None:
    assert state(provider_running=False) == "unavailable"
    assert state(provider_running=True) == "checking"


@pytest.mark.parametrize(
    "user",
    [
        UserView(ANN, paused=True, quiet=NO_QUIET),  # the pre-0004 "Pause all", as planner._ineligible has it
        UserView(ANN, active=False, quiet=NO_QUIET),  # suspended
    ],
)
def test_an_owner_who_isnt_active_pauses_their_watches(user: UserView) -> None:
    result = plan([watch("1" * 16)], users={ANN: user})
    assert result.states == {"1" * 16: "paused"}
    assert result.jobs == []


def test_an_owner_missing_from_the_users_isnt_active() -> None:
    assert plan([watch("1" * 16)], users={}).states == {"1" * 16: "paused"}


# ---- what's due, coalescing and the budget


def test_identical_queries_are_coalesced_across_users() -> None:
    watches = [watch("1" * 16), watch("2" * 16), watch("3" * 16, BOB)]
    result = plan(watches)
    assert [(j.query.part, j.watch_ids) for j in result.jobs] == [
        ("a", ("1" * 16, "2" * 16, "3" * 16)),
        ("b", ("1" * 16, "2" * 16, "3" * 16)),
    ]
    assert result.deferred == {}


def test_the_budget_admits_whole_watches_only() -> None:
    result = plan([watch("1" * 16)], tokens=1)  # it needs two
    assert result.jobs == []
    assert result.deferred == {"buntzen": 1}
    other = DAY + timedelta(days=1)
    result = plan([watch("1" * 16), watch("2" * 16, day=other)], tokens=3)
    assert {j.watch_ids for j in result.jobs} == {("1" * 16,)}
    assert result.deferred == {"buntzen": 1}


def test_due_watches_are_taken_oldest_first() -> None:
    older = watch("2" * 16, day=DAY + timedelta(days=1), next_check_at=NOON - 300)
    newer = watch("1" * 16, next_check_at=NOON - 10)
    result = plan([newer, older], tokens=2)
    assert {j.watch_ids for j in result.jobs} == {("2" * 16,)}
    assert result.deferred == {"buntzen": 1}


def test_a_watch_without_a_next_check_never_goes_ahead_of_watches_already_due() -> None:
    edited = watch("1" * 16, attempted_at=NOON - 20)  # an edit reset next_check_at; its window opened yesterday
    waiting = watch("2" * 16, day=DAY + timedelta(days=1), next_check_at=NOON - 10)
    result = plan([edited, waiting], tokens=2)
    assert {j.watch_ids for j in result.jobs} == {("2" * 16,)}
    assert result.deferred == {"buntzen": 1}


def test_a_watch_reset_every_tick_fetches_each_query_at_most_once_per_min_interval() -> None:
    checked: dict[str, float] = {}
    fetches: dict[str, list[float]] = {"a": [], "b": []}
    now = NOON
    while now < NOON + 300:
        result = plan([watch("1" * 16)], now=now, checked_at=checked)  # next_check_at NULL, as after every PATCH
        for job in result.jobs:
            checked.update(dict.fromkeys(job.watch_ids, now))
            fetches[job.query.part].append(now)
        now += TICK_SECONDS
    for times in fetches.values():
        assert times[0] == NOON
        assert all(b - a >= MIN_INTERVAL_S for a, b in pairwise(times))
        assert len(times) == 300 / MIN_INTERVAL_S


def test_a_watch_held_back_by_min_interval_is_neither_checked_nor_deferred() -> None:
    w = watch("1" * 16)
    jobs = plan([w]).jobs
    checked = {w.id: NOON - 10}
    held = plan([w], checked_at=checked)
    assert (held.jobs, held.deferred) == ([], {})
    assert held.wake_at == NOON + MIN_INTERVAL_S - 10
    assert plan([w], now=NOON + MIN_INTERVAL_S - 10, checked_at=checked).jobs == jobs
    other = watch("2" * 16, BOB, day=DAY + timedelta(days=1))  # another watch's last check doesn't hold it
    assert {j.watch_ids for j in plan([w, other], checked_at=checked).jobs} == {("2" * 16,)}
    # Only its own last check holds a watch: another watch on the same queries is due, and this one rides along.
    same = watch("3" * 16, BOB)
    assert {j.watch_ids for j in plan([w, same], checked_at=checked).jobs} == {("1" * 16, "3" * 16)}


def test_a_watch_sharing_each_query_with_a_different_watch_is_still_checked_on_time() -> None:
    # B wants the all-day pass and the morning one; A only the first, C only the second. Through the release burst,
    # with A and C each checked on their own cadence, B must be checked on its own: other watches fetching its
    # queries at other moments never hold it back (the loop remembers each watch's last check, not each query's).
    def buntzen(watch_id: str, passes: tuple[Pass, ...], offset: float) -> WatchInput:
        spec = BuntzenSpec(date=DAY, passes=passes)
        start = BUNTZEN.window(spec).opens_at.timestamp() + RELEASE_DELAY_S
        return replace(
            watch(watch_id), spec=spec, window=BUNTZEN.window(spec), next_check_at=start + offset, attempted_at=start
        )

    watches = {
        "a" * 16: buntzen("a" * 16, ("full_day",), 0.0),
        "b" * 16: buntzen("b" * 16, ("full_day", "morning"), 8.0),
        "c" * 16: buntzen("c" * 16, ("morning",), 15.0),
    }
    cadence = {"a" * 16: 31.0, "b" * 16: 30.5, "c" * 16: 33.0}  # 30 s, +JITTER, as HttpChecks schedules them
    start = watches["a" * 16].window.opens_at.timestamp() + RELEASE_DELAY_S
    checked: dict[str, float] = {}
    times: dict[str, list[float]] = {watch_id: [start] for watch_id in watches}
    now = start
    while now < start + 1200:  # the release's 20 minutes at 30 s
        checked = {k: at for k, at in checked.items() if now - at < MIN_INTERVAL_S}
        result = plan_checks(
            list(watches.values()),
            USERS,
            PROVIDERS,
            provider_paused=frozenset(),
            cooldown_until={},
            tokens={"buntzen": 12},
            now=now,
            checked_at=checked,
        )
        for watch_id in sorted({watch_id for job in result.jobs for watch_id in job.watch_ids}):
            checked[watch_id] = now
            times[watch_id].append(now)
            watches[watch_id] = replace(watches[watch_id], next_check_at=now + cadence[watch_id], attempted_at=now)
        assert result.wake_at is not None
        now = max(now + 0.25, min(now + TICK_SECONDS, result.wake_at)) + 0.5  # each round takes half a second
    for watch_id, seen in times.items():
        assert max(b - a for a, b in pairwise([*seen, now])) <= MIN_INTERVAL_S + TICK_SECONDS, watch_id


def test_a_watch_not_due_rides_along_for_free() -> None:
    due = watch("1" * 16, next_check_at=NOON - 1)
    later = watch("2" * 16, BOB, next_check_at=NOON + 50, attempted_at=NOON - 10)
    result = plan([due, later], tokens=2)
    assert [j.watch_ids for j in result.jobs] == [("1" * 16, "2" * 16)] * 2
    alone = plan([later])
    assert alone.jobs == []
    assert alone.wake_at == NOON + 50


def test_nothing_is_due_before_the_release_delay() -> None:
    for now in (OPENS - 60, OPENS, OPENS + RELEASE_DELAY_S - 0.01):  # after the 06:55 reminder
        result = plan([watch("1" * 16)], now=now)
        assert result.jobs == []
        assert result.wake_at == OPENS + RELEASE_DELAY_S
    result = plan([watch("1" * 16)], now=OPENS + RELEASE_DELAY_S)
    assert len(result.jobs) == 2


@pytest.mark.parametrize("release_day", [date(2027, 3, 14), date(2027, 11, 7)])  # DST starts, DST ends
def test_the_first_check_is_at_07_00_02_local_across_dst(release_day: date) -> None:
    w = watch("1" * 16, day=release_day + timedelta(days=1))
    six_59 = datetime.combine(release_day, time(6, 59), tzinfo=PACIFIC).timestamp()
    wake = plan([w], now=six_59).wake_at
    assert wake is not None
    assert datetime.fromtimestamp(wake, PACIFIC).replace(tzinfo=None) == datetime.combine(release_day, time(7, 0, 2))


def test_a_watch_in_quiet_hours_waits_unless_lifted() -> None:
    quiet = {ANN: UserView(ANN, quiet=QuietHours(time(11, 0), time(13, 0)))}
    assert plan([watch("1" * 16)], users=quiet).states == {"1" * 16: "quiet"}
    assert plan([watch("1" * 16)], users=quiet).jobs == []
    lifted = watch("1" * 16, quiet_lifted_until=NOON + 600)
    assert len(plan([lifted], users=quiet).jobs) == 2
    expired = watch("1" * 16, quiet_lifted_until=NOON - 1)
    assert plan([expired], users=quiet).jobs == []


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"paused_reason": "admin"},
        {"backoff_until": NOON + 60},
        {"next_check_at": NOON + 1},
    ],
)
def test_paused_backing_off_and_not_yet_due_watches_arent_checked(changes: dict[str, Any]) -> None:
    assert plan([watch("1" * 16, **changes)]).jobs == []


def test_over_and_provider_paused_watches_arent_checked() -> None:
    assert plan([watch("1" * 16)], now=CLOSES).jobs == []
    assert plan([watch("1" * 16)], now=CLOSES).states == {"1" * 16: "over"}
    paused = plan([watch("1" * 16)], provider_paused=frozenset({"buntzen"}))
    assert (paused.jobs, paused.states) == ([], {"1" * 16: "provider_paused"})
    cooling = plan([watch("1" * 16)], cooldown_until={"buntzen": NOON + 300})
    assert (cooling.jobs, cooling.states) == ([], {"1" * 16: "backoff"})
    assert cooling.wake_at == NOON + 300


def test_an_erroring_watch_is_still_checked() -> None:
    result = plan([watch("1" * 16, consecutive_errors=BACKOFF_AFTER + 1, attempted_at=NOON - 60)])
    assert result.states == {"1" * 16: "error"}
    assert len(result.jobs) == 2


# ---- reminders


REMIND = datetime.combine(DAY - timedelta(days=1), time(6, 55), tzinfo=PACIFIC).timestamp()


def test_a_reminder_goes_once_per_user_and_date_with_the_lowest_id() -> None:
    watches = [watch("2" * 16), watch("1" * 16), watch("3" * 16, BOB)]
    result = plan(watches, now=REMIND)
    assert [(r.watch_id, r.user_id, r.date) for r in result.reminders] == [("1" * 16, ANN, DAY), ("3" * 16, BOB, DAY)]
    assert plan(watches, now=REMIND - 1).reminders == []
    assert len(plan(watches, now=OPENS - 1).reminders) == 2  # until the release
    assert plan(watches, now=OPENS).reminders == []  # never at or after it


def test_no_reminder_in_quiet_hours_or_for_paused_watches() -> None:
    quiet = {ANN: UserView(ANN, quiet=QuietHours(time(6, 0), time(7, 0)))}
    assert plan([watch("1" * 16)], now=REMIND, users=quiet).reminders == []
    lifted = watch("1" * 16, quiet_lifted_until=REMIND + 600)  # a lifted quiet hour still isn't a time to remind
    assert plan([lifted], now=REMIND, users=quiet).reminders == []
    assert plan([watch("1" * 16, enabled=False)], now=REMIND).reminders == []
    assert plan([watch("1" * 16)], now=REMIND, provider_paused=frozenset({"buntzen"})).reminders == []


def test_no_reminder_for_a_watch_without_one() -> None:
    class Unreminded(FakeHttpProvider):
        def reminder_at(self, spec: FakeSpec) -> datetime | None:  # noqa: ARG002 - never
            return None

    registry = fake_registry(Unreminded())
    result = plan_checks(
        [watch("1" * 16)],
        USERS,
        registry,
        provider_paused=frozenset(),
        cooldown_until={},
        tokens={"buntzen": 12},
        now=REMIND,
    )
    assert result.reminders == []


def test_the_reminder_wakes_the_loop() -> None:
    assert plan([watch("1" * 16)], now=REMIND - 3600).wake_at == REMIND


# ---- archives


def test_ended_lists_watches_12_hours_after_their_window_closes() -> None:
    watches = [watch("1" * 16), watch("2" * 16, enabled=False), watch("3" * 16, day=DAY + timedelta(days=1))]
    assert plan(watches, now=CLOSES + ARCHIVE_AFTER_CLOSE_S - 1).ended == []
    assert plan(watches, now=CLOSES + ARCHIVE_AFTER_CLOSE_S).ended == ["1" * 16, "2" * 16]
    assert plan([watch("1" * 16)], now=CLOSES + 60).wake_at == CLOSES + ARCHIVE_AFTER_CLOSE_S


def test_nothing_to_plan_wakes_nobody() -> None:
    result = plan([])
    assert (result.jobs, result.reminders, result.states, result.ended, result.wake_at) == ([], [], {}, [], None)


def test_a_window_is_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CheckWindow(opens_at=datetime(2027, 6, 30, 7), closes_at=datetime(2027, 7, 1, 18))  # noqa: DTZ001
