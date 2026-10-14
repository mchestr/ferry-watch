"""What ferry-control tells each pod: its `configure` message, and whether it may cart now.

Pure functions over what the database says (docs/multi-tenant/design-reference.md §6.5, §9.3), shared by
the hub endpoint (on connect, on a trip change, and on each cart claim) and its tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import msgspec

from ferry_watch.models import PACIFIC, Trip
from ferry_watch.polling import PollPolicy, QuietHours, time_until
from ferry_watch.protocol import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    Brake,
    ClaimReason,
    Configure,
    HoldLimits,
    Mode,
    TripSpec,
)

PUSH_CHANGE = 0.10  # a new interval is pushed only if it differs by more than this
MAX_CHECKS_PER_MINUTE = 40  # the owner's ceiling for the budget


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Deployment values the hub applies to every pod."""

    policy: PollPolicy = field(default_factory=PollPolicy)
    hold: HoldLimits = field(
        default_factory=lambda: HoldLimits(unclaimed_s=1800, session_s=1800, viewer_gone_s=600, hard_s=3600)
    )
    checks_per_minute: int = 20  # the BC Ferries budget (ceiling 40)
    auto_cart: bool = True  # fleet-wide; off makes everything alert-only
    alert_only_fares: frozenset[str] = frozenset()
    alert_only_vehicles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 1 <= self.checks_per_minute <= MAX_CHECKS_PER_MINUTE:
            raise ValueError("checks_per_minute must be between 1 and 40")


@dataclass(frozen=True, slots=True)
class Switches:
    """Fleet-wide state from `fleet_state`."""

    maintenance: bool = False
    pause_carting: bool = False
    brake: Brake = field(default_factory=Brake)
    breaker_open: bool = False


@dataclass(frozen=True, slots=True)
class Allowance:
    """A user's limits, today's usage (Pacific day) and quiet hours."""

    holds_per_day: int
    cart_attempts_per_day: int
    vnc_minutes_per_day: int
    holds_today: int = 0
    cart_attempts_today: int = 0
    vnc_seconds_today: int = 0
    quiet: QuietHours | None = None  # none unless the user set some

    def quiet_now(self, at: datetime) -> bool:
        return self.quiet is not None and self.quiet.contains(at)


def cart_gate(
    trip: Trip,
    allowance: Allowance,
    switches: Switches,
    settings: HubSettings,
    now: datetime,
    *,
    lifted: bool = False,
) -> ClaimReason:
    """Whether a new cart may be put in now ("ok"), or why not. Holding elsewhere is the claim's own check.

    `lifted`: the user lifted their quiet hours for this trip (a manual check, or "Hold it for me"). Quiet
    hours are for when they don't want to hear from us, so they don't apply to someone who is there asking;
    everything else does.
    """
    if switches.maintenance or switches.pause_carting or not settings.auto_cart:
        return "carting_paused"
    if allowance.quiet_now(now) and not lifted:
        return "quiet_hours"
    if allowance.holds_today >= allowance.holds_per_day:
        return "no_holds_left"
    vnc_left_s = allowance.vnc_minutes_per_day * 60 - allowance.vnc_seconds_today
    if allowance.cart_attempts_today >= allowance.cart_attempts_per_day or vnc_left_s < settings.hold.session_s:
        return "limit"
    if trip.fare.value in settings.alert_only_fares or trip.vehicle.value in settings.alert_only_vehicles:
        return "limit"
    return "ok"


def curve_interval(trip: Trip, now: datetime, policy: PollPolicy) -> int:
    """The poll curve's interval for a warm browser on this trip, in seconds."""
    until = time_until(trip, now)
    interval = policy.interval(until) if until is not None else None
    return int((interval or policy.tiers[-1].interval).total_seconds())


def demand(curve_intervals: Iterable[int]) -> float:
    """Checks a minute the warm browsers would make on the curve alone."""
    return sum(60 / interval for interval in curve_intervals)


def stretch(curve_intervals: Iterable[int], checks_per_minute: int) -> float:
    """How much to slow every warm browser so the fleet stays within the budget (1: not at all)."""
    return max(1.0, demand(curve_intervals) / checks_per_minute)


def interval_s(curve: int, stretch_by: float, brake: Brake) -> int:
    return int(min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, curve * stretch_by) * brake.multiplier))


def worth_pushing(old: int | None, new: int) -> bool:
    """A changed interval is pushed only when it moves by more than 10 % (tiers move it far more)."""
    return old is None or abs(new - old) > old * PUSH_CHANGE


@dataclass(frozen=True, slots=True)
class TripRow:
    trip: Trip
    rev: int
    enabled: bool
    paused_reason: str | None
    alerted: tuple[str, ...] = ()
    offered: tuple[str, ...] = ()
    hold_requested_until: float | None = None  # "Hold it for me", until then
    quiet_lifted_until: float | None = None  # a manual check lifted the user's quiet hours for this trip

    def hold_requested(self, now: float) -> bool:
        return self.hold_requested_until is not None and now < self.hold_requested_until

    def quiet_lifted(self, now: float) -> bool:
        """Whether the user's quiet hours are lifted for this trip now (a manual check, or "Hold it for me")."""
        return self.hold_requested(now) or (self.quiet_lifted_until is not None and now < self.quiet_lifted_until)


def build_configure(
    envelope: dict[str, object],
    row: TripRow,
    *,
    mode: Mode,
    user_active: bool,
    allowance: Allowance,
    switches: Switches,
    settings: HubSettings,
    stretch_by: float,
    now: float,
) -> Configure:
    at = datetime.fromtimestamp(now, PACIFIC)
    trip = row.trip
    if row.hold_requested(now):
        trip = replace(trip, auto_cart=True)  # the user asked for this hold, whatever the trip's own setting
    lifted = row.quiet_lifted(now)
    active = user_active and row.enabled and row.paused_reason is None
    active = active and not switches.maintenance and not switches.brake.paused
    active = active and (lifted or not allowance.quiet_now(at))  # quiet hours: no checks at all
    interval = interval_s(curve_interval(trip, at, settings.policy), stretch_by, switches.brake)
    return Configure(
        **envelope,  # type: ignore[arg-type]
        trip_spec=msgspec.convert(trip.to_json(), TripSpec),
        active=active,
        mode=mode,
        interval_s=interval,
        auto_cart=cart_gate(trip, allowance, switches, settings, at, lifted=lifted) == "ok",
        hold=settings.hold,
        alerted=list(row.alerted)[:100],
        offered=list(row.offered)[:100],
        vnc_allowed=user_active,
        brake=switches.brake,
        breaker="open" if switches.breaker_open else "closed",
    )


def pacific_date(now: float) -> date:
    """The Pacific day `now` falls on (usage is counted per Pacific day)."""
    return datetime.fromtimestamp(now, PACIFIC).date()
