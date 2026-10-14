"""Which browser pods should exist (docs/multi-tenant/design-reference.md §6.6, §8.1).

A pure function from what the database and the cluster say now to what the fleet loop should do: which
pods to start, which to drain, and where the rest wait. The loop applies it; tests drive it directly.

- Each user's nearest eligible trip within 7 days gets a *warm* pod (or the N nearest, if an admin
  raised `maxActiveBrowsers`). The warm browser moves to a nearer trip only while its pod is Watching;
  a pod that is starting, holding or draining stays where it is.
- Every other eligible trip gets a *short check* when due: a pod that checks once and exits.
- A trip has at most one pod. Holding pods are never displaced.
- Caps: total pods, with `short_check_slots` kept free of warm pods; at most 2 starting; 30 starts an
  hour; a user's starts at least 20 s apart. Nothing is created that would wait as Pending.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from ferry_watch.models import PACIFIC, Trip
from ferry_watch.polling import PollPolicy, time_until
from ferry_watch.protocol import CommandReason, Mode

PodPhase = Literal["creating", "starting", "watching", "holding", "draining", "failed", "gone"]

MAX_STARTING = 2
MAX_STARTS_PER_HOUR = 30
USER_START_SPACING_S = 20.0


@dataclass(frozen=True, slots=True)
class UserView:
    id: str
    active: bool = True  # not suspended
    paused: bool = False  # the user paused everything
    max_active_browsers: int = 1


@dataclass(frozen=True, slots=True)
class TripView:
    trip: Trip
    user_id: str
    paused_reason: str | None = None  # e.g. after_checkout, until "Did you book it?" is answered
    archived: bool = False
    next_check_at: float | None = None  # a short check is due then (None: now)
    backoff_until: float | None = None
    needs_attention: bool = False
    queued_since: float | None = None

    @property
    def id(self) -> str:
        return self.trip.id


@dataclass(frozen=True, slots=True)
class PodView:
    name: str
    trip_id: str
    user_id: str
    mode: Mode
    phase: PodPhase
    created_at: float = 0.0

    @property
    def counts(self) -> bool:
        """Takes a browser slot (failed pods kept for post-mortems don't run anything)."""
        return self.phase not in ("failed", "gone")


@dataclass(frozen=True, slots=True)
class FleetView:
    max_browsers_total: int = 7
    short_check_slots: int = 1
    maintenance: bool = False
    starts_last_hour: int = 0
    last_start_by_user: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Start:
    trip_id: str
    user_id: str
    mode: Mode


@dataclass(frozen=True, slots=True)
class Drain:
    pod: str
    reason: CommandReason


@dataclass(slots=True)
class Plan:
    start: list[Start] = field(default_factory=list)
    drain: list[Drain] = field(default_factory=list)
    queued: dict[str, int] = field(default_factory=dict)  # warm trips waiting for a slot: position from 1
    overdue: list[str] = field(default_factory=list)  # short checks due but not started this pass
    warm: set[str] = field(default_factory=set)  # trips that have, or should have, the warm browser


def _ineligible(
    view: TripView, user: UserView | None, fleet: FleetView, now: float, at: datetime
) -> CommandReason | None:
    """Why this trip may not have a browser now (None: it may)."""
    reasons: list[tuple[bool, CommandReason]] = [
        (fleet.maintenance, "maintenance"),
        (user is None or not user.active, "suspended"),
        (user is not None and user.paused, "paused"),
        (not view.trip.enabled or view.paused_reason is not None, "paused"),
        (view.archived, "deleted"),
        (time_until(view.trip, at) is None, "past"),
        (view.needs_attention or (view.backoff_until is not None and view.backoff_until > now), "limit"),
    ]
    return next((reason for applies, reason in reasons if applies), None)


def plan(
    users: Iterable[UserView],
    trips: Iterable[TripView],
    pods: Iterable[PodView],
    fleet: FleetView,
    policy: PollPolicy,
    now: float,
) -> Plan:
    """What the fleet loop should do now."""
    return _Planner(users, trips, pods, fleet, policy, now).run()


class _Planner:
    def __init__(
        self,
        users: Iterable[UserView],
        trips: Iterable[TripView],
        pods: Iterable[PodView],
        fleet: FleetView,
        policy: PollPolicy,
        now: float,
    ) -> None:
        self.now = now
        self.at = datetime.fromtimestamp(now, PACIFIC)
        self.fleet = fleet
        self.policy = policy
        self.users = {u.id: u for u in users}
        self.trips = {t.id: t for t in trips}
        self.pods = [p for p in pods if p.counts]
        self.pod_by_trip = {p.trip_id: p for p in self.pods}
        self.eligible: dict[str, TripView] = {}
        self.handover: set[str] = set()  # warm next, as soon as the user's old warm pod is gone
        self.result = Plan()

    def run(self) -> Plan:
        self._sort_out_eligibility()
        want_warm = self._warm_browsers()
        self._admit(want_warm, self._due_short_checks())
        return self.result

    def _until(self, view: TripView) -> timedelta:
        return time_until(view.trip, self.at) or timedelta(0)

    def _sort_out_eligibility(self) -> None:
        for view in self.trips.values():
            reason = _ineligible(view, self.users.get(view.user_id), self.fleet, self.now, self.at)
            if reason is None:
                self.eligible[view.id] = view
                continue
            pod = self.pod_by_trip.get(view.id)
            if pod is not None and pod.phase not in ("holding", "draining"):
                self.result.drain.append(Drain(pod.name, reason))
        for pod in self.pods:  # pods for trips that no longer exist at all
            if pod.trip_id not in self.trips and pod.phase not in ("holding", "draining"):
                self.result.drain.append(Drain(pod.name, "deleted"))

    def _warm_browsers(self) -> list[TripView]:
        """Each user's nearest trips within the warm window; returns those that need a pod started."""
        by_user: dict[str, list[TripView]] = {}
        for view in self.eligible.values():
            by_user.setdefault(view.user_id, []).append(view)
        want: list[TripView] = []
        for user_id, views in by_user.items():
            user = self.users[user_id]
            in_window = [v for v in views if self._until(v) <= self.policy.warm_window]
            nearest = sorted(in_window, key=lambda v: (self._until(v), v.id))[: user.max_active_browsers]
            nearest_ids = {v.id for v in nearest}
            warm_pods = [p for p in self.pods if p.user_id == user_id and p.mode == "warm"]
            leaving = False  # one of the user's warm pods is on its way out
            for warm in warm_pods:
                if warm.trip_id in self.eligible and warm.trip_id not in nearest_ids and warm.phase == "watching":
                    self.result.drain.append(Drain(warm.name, "superseded"))  # a nearer trip takes the browser
                    leaving = True
                elif warm.phase == "draining" or warm.trip_id not in self.eligible:
                    leaving = True
            # Every warm pod counts until it's gone, so a user never has more than their warm browsers.
            room = max(user.max_active_browsers - len(warm_pods), 0)
            for view in nearest:
                current = self.pod_by_trip.get(view.id)
                if current is not None and current.mode == "warm":
                    self.result.warm.add(view.id)
                elif current is None and room > 0:
                    want.append(view)
                    self.result.warm.add(view.id)
                    room -= 1
                elif current is None and leaving:
                    self.handover.add(view.id)  # no short check meanwhile: it would hold up the handover
        return want

    def _due_short_checks(self) -> list[TripView]:
        """Eligible trips without a pod whose short check is due (a queued warm trip too, so a full
        cluster never leaves a trip unchecked), oldest first."""
        due = [
            v
            for v in self.eligible.values()
            if v.id not in self.pod_by_trip and v.id not in self.handover and (v.next_check_at or 0) <= self.now
        ]
        return sorted(due, key=lambda v: (v.next_check_at or 0, v.id))

    def _admit(self, want_warm: list[TripView], due: list[TripView]) -> None:
        fleet, result = self.fleet, self.result
        running = Counter(p.user_id for p in self.pods)
        free = fleet.max_browsers_total - len(self.pods)
        warm_room = fleet.max_browsers_total - fleet.short_check_slots - sum(1 for p in self.pods if p.mode == "warm")
        starts_left = min(
            MAX_STARTING - sum(1 for p in self.pods if p.phase in ("creating", "starting")),
            MAX_STARTS_PER_HOUR - fleet.starts_last_hour,
        )
        started_users: set[str] = set()

        def may_start(user_id: str) -> bool:
            last = fleet.last_start_by_user.get(user_id)
            recent = last is not None and self.now - last < USER_START_SPACING_S
            return free > 0 and starts_left > 0 and user_id not in started_users and not recent

        # Warm starts: the user with the fewest browsers, then the nearest sailing, then the longest wait.
        want_warm.sort(key=lambda v: (running[v.user_id], self._until(v), v.queued_since or self.now, v.id))
        for view in want_warm:
            if warm_room > 0 and may_start(view.user_id):
                result.start.append(Start(view.id, view.user_id, "warm"))
                started_users.add(view.user_id)
                free, warm_room, starts_left = free - 1, warm_room - 1, starts_left - 1
            else:
                result.queued[view.id] = len(result.queued) + 1
        started = {s.trip_id for s in result.start}
        for view in due:
            if view.id in started:
                continue
            if may_start(view.user_id):
                result.start.append(Start(view.id, view.user_id, "short_check"))
                started_users.add(view.user_id)
                free, starts_left = free - 1, starts_left - 1
            else:
                result.overdue.append(view.id)
