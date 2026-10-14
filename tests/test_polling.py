from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from ferry_watch.models import PACIFIC, Trip
from ferry_watch.polling import PollPolicy, QuietHours, Tier, quiet_hours, time_until, warm_trip

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=PACIFIC)
POLICY = PollPolicy()


def trip(
    trip_id: str = "t1", *, on: date, earliest: time = time(7, 0), latest: time = time(21, 0), enabled: bool = True
) -> Trip:
    return Trip(id=trip_id, origin="SWB", destination="TSA", date=on, earliest=earliest, latest=latest, enabled=enabled)


@pytest.mark.parametrize(
    ("until", "every"),
    [
        (timedelta(0), timedelta(seconds=30)),
        (timedelta(hours=6), timedelta(seconds=30)),
        (timedelta(hours=6, minutes=1), timedelta(seconds=60)),
        (timedelta(hours=24), timedelta(seconds=60)),
        (timedelta(days=2), timedelta(minutes=2)),
        (timedelta(days=3), timedelta(minutes=2)),
        (timedelta(days=5), timedelta(minutes=5)),
        (timedelta(days=7), timedelta(minutes=5)),
    ],
)
def test_checks_get_more_frequent_as_the_sailing_approaches(until: timedelta, every: timedelta) -> None:
    assert POLICY.interval(until) == every


def test_too_far_off_for_a_warm_browser() -> None:
    assert POLICY.interval(timedelta(days=7, seconds=1)) is None
    assert POLICY.short_check_interval == timedelta(hours=6)


def test_time_until_the_first_departure() -> None:
    tomorrow = trip(on=date(2026, 10, 12))
    assert time_until(tomorrow, NOW) == timedelta(hours=19)  # until 07:00 tomorrow
    today = trip(on=date(2026, 10, 11))
    assert time_until(today, NOW) == timedelta(0)  # inside the window: check at the fastest rate
    earlier = trip(on=date(2026, 10, 11), earliest=time(6, 0), latest=time(9, 0))
    assert time_until(earlier, NOW) is None  # every departure in the window has left


NIGHT = QuietHours(time(23, 0), time(6, 0))


@pytest.mark.parametrize(
    ("hour", "minute", "quiet"),
    [(22, 59, False), (23, 0, True), (2, 30, True), (5, 59, True), (6, 0, False), (12, 0, False)],
)
def test_quiet_hours_wrap_past_midnight(hour: int, minute: int, quiet: bool) -> None:
    assert NIGHT.contains(datetime(2026, 10, 11, hour, minute, tzinfo=PACIFIC)) is quiet


@pytest.mark.parametrize(
    ("hour", "minute", "quiet"),
    [(12, 59, False), (13, 0, True), (14, 30, True), (15, 0, False), (0, 0, False)],
)
def test_quiet_hours_within_a_day(hour: int, minute: int, quiet: bool) -> None:
    nap = QuietHours(time(13, 0), time(15, 0))
    assert nap.contains(datetime(2026, 10, 11, hour, minute, tzinfo=PACIFIC)) is quiet


def test_quiet_hours_are_pacific_whatever_the_clock_zone() -> None:
    assert NIGHT.contains(datetime(2026, 10, 12, 6, 30, tzinfo=UTC)) is True  # 23:30 Pacific (PDT)
    assert NIGHT.contains(datetime(2026, 10, 11, 19, 0, tzinfo=UTC)) is False  # noon Pacific


def test_quiet_hours_cover_something() -> None:
    with pytest.raises(ValueError, match="different times"):
        QuietHours(time(6, 0), time(6, 0))
    assert quiet_hours(time(23, 0), time(6, 0)) == NIGHT  # as stored
    assert quiet_hours(None, None) is None  # none: the default


def test_horizon_is_thirty_days() -> None:
    today = date(2026, 10, 11)
    assert POLICY.within_horizon(today, today)
    assert POLICY.within_horizon(today + timedelta(days=30), today)
    assert not POLICY.within_horizon(today + timedelta(days=31), today)
    assert not POLICY.within_horizon(today - timedelta(days=1), today)


def test_the_nearest_trip_within_a_week_gets_the_warm_browser() -> None:
    far = trip("far", on=date(2026, 10, 20))  # 9 days out: short checks only
    soon = trip("soon", on=date(2026, 10, 14))
    sooner = trip("sooner", on=date(2026, 10, 13))
    paused = trip("paused", on=date(2026, 10, 12), enabled=False)
    gone = trip("gone", on=date(2026, 10, 11), earliest=time(6, 0), latest=time(9, 0))
    assert warm_trip([far, soon, sooner, paused, gone], NOW, POLICY) is sooner
    assert warm_trip([far, paused, gone], NOW, POLICY) is None


def test_ties_break_by_trip_id() -> None:
    a = trip("a", on=date(2026, 10, 13))
    b = trip("b", on=date(2026, 10, 13))
    assert warm_trip([b, a], NOW, POLICY) is a


@pytest.mark.parametrize(
    "tiers",
    [
        (),
        (Tier(timedelta(hours=6), timedelta(seconds=15)),),  # faster than the floor
        (Tier(timedelta(days=1), timedelta(minutes=1)), Tier(timedelta(hours=6), timedelta(minutes=2))),  # out of order
        (
            Tier(timedelta(hours=6), timedelta(minutes=2)),
            Tier(timedelta(days=1), timedelta(minutes=1)),
        ),  # further = faster
    ],
)
def test_bad_policies_are_rejected(tiers: tuple[Tier, ...]) -> None:
    with pytest.raises(ValueError, match=r"tier|often|ordered"):
        PollPolicy(tiers=tiers)


def test_short_checks_and_horizon_must_fit_the_tiers() -> None:
    with pytest.raises(ValueError, match="Short checks"):
        PollPolicy(short_check_interval=timedelta(minutes=1))
    with pytest.raises(ValueError, match="horizon"):
        PollPolicy(horizon=timedelta(days=3))
