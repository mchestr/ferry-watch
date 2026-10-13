"""How often a trip is checked: rarely while its sailing is far off, more often as it gets close.

The aim is to be a good citizen of bcferries.com (docs/multi-tenant/decisions.md, round 3):
- a trip can be at most 30 days ahead;
- each user's nearest trip within 7 days gets a browser kept warm between checks, and is checked on a
  curve that tightens as the sailing approaches;
- every other trip gets a short check every 6 hours;
- overnight ("quiet hours") checks carry on, but an opening only alerts, since nobody is awake to
  check out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import PACIFIC, Trip

# Nothing checks faster than this, whatever a policy says.
MIN_INTERVAL = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class Tier:
    """Check every `interval` while the trip's first departure is at most `within` away."""

    within: timedelta
    interval: timedelta


@dataclass(frozen=True, slots=True)
class PollPolicy:
    tiers: tuple[Tier, ...] = (
        Tier(timedelta(hours=6), timedelta(seconds=30)),
        Tier(timedelta(hours=24), timedelta(seconds=60)),
        Tier(timedelta(days=3), timedelta(minutes=2)),
        Tier(timedelta(days=7), timedelta(minutes=5)),
    )
    short_check_interval: timedelta = timedelta(hours=6)  # trips without a warm browser
    horizon: timedelta = timedelta(days=30)  # how far ahead a trip may be
    quiet_start: time = time(23, 0)  # Pacific
    quiet_end: time = time(6, 0)

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("A poll policy needs at least one tier")
        previous: Tier | None = None
        for tier in self.tiers:
            if tier.interval < MIN_INTERVAL:
                raise ValueError(f"Checks can't be more often than every {MIN_INTERVAL.seconds} seconds")
            if previous and tier.within <= previous.within:
                raise ValueError("Tiers must be ordered from the nearest departure to the furthest")
            if previous and tier.interval < previous.interval:
                raise ValueError("A trip further away can't be checked more often than a nearer one")
            previous = tier
        if self.short_check_interval < self.tiers[-1].interval:
            raise ValueError("Short checks can't be more often than the slowest warm tier")
        if self.horizon < self.warm_window:
            raise ValueError("The horizon can't be shorter than the warm window")

    @property
    def warm_window(self) -> timedelta:
        """How close a trip must be to get a warm browser."""
        return self.tiers[-1].within

    def interval(self, until: timedelta) -> timedelta | None:
        """How often a warm browser checks a trip whose first departure is `until` away.

        None means the trip is too far off for a warm browser; it gets short checks instead.
        """
        for tier in self.tiers:
            if until <= tier.within:
                return tier.interval
        return None

    def quiet(self, at: datetime) -> bool:
        """Whether `at` falls in quiet hours (Pacific), when an opening only alerts."""
        now = at.astimezone(PACIFIC).time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= now < self.quiet_end
        return now >= self.quiet_start or now < self.quiet_end

    def within_horizon(self, trip_date: date, today: date) -> bool:
        """Whether a trip on `trip_date` may be watched (not past, and not too far ahead)."""
        return today <= trip_date <= today + timedelta(days=self.horizon.days)


def time_until(trip: Trip, now: datetime) -> timedelta | None:
    """How long until the trip's first departure; zero once inside its window, None once the window has passed."""
    first = datetime.combine(trip.date, trip.earliest, tzinfo=PACIFIC)
    last = datetime.combine(trip.date, trip.latest, tzinfo=PACIFIC)
    if now > last:
        return None
    return max(first - now, timedelta(0))


def warm_trip(trips: Iterable[Trip], now: datetime, policy: PollPolicy) -> Trip | None:
    """The trip that gets the user's warm browser: the enabled one leaving soonest within the warm window."""
    candidates: list[tuple[timedelta, str, Trip]] = []
    for trip in trips:
        until = time_until(trip, now)
        if trip.enabled and until is not None and until <= policy.warm_window:
            candidates.append((until, trip.id, trip))
    return min(candidates, key=lambda c: (c[0], c[1]))[2] if candidates else None
