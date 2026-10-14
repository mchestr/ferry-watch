from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any

from ferry_watch.control.planner import (
    MAX_STARTS_PER_HOUR,
    Drain,
    FleetView,
    PodView,
    Start,
    TripView,
    UserView,
    plan,
)
from ferry_watch.models import PACIFIC, Trip
from ferry_watch.polling import PollPolicy, QuietHours

NOW_AT = datetime(2026, 10, 11, 12, 0, tzinfo=PACIFIC)
NOW = NOW_AT.timestamp()
POLICY = PollPolicy()


def trip(n: int, days: int, *, user: str = "ann", **kw: Any) -> TripView:
    return TripView(
        Trip(
            id=f"{n:016x}",
            origin="SWB",
            destination="TSA",
            date=(NOW_AT + timedelta(days=days)).date(),
            enabled=kw.pop("enabled", True),
        ),
        user,
        **kw,
    )


def pod(view: TripView, phase: str = "watching", mode: str = "warm", user: str | None = None) -> PodView:
    return PodView(f"fw-{view.id}-g1", view.id, user or view.user_id, mode, phase)  # type: ignore[arg-type]


ANN = UserView("ann")
BOB = UserView("bob")
CAT = UserView("cat")


def run(trips: list[TripView], pods: list[PodView] = (), users: list[UserView] = (ANN,), **fleet: Any) -> Any:  # type: ignore[assignment]
    return plan(users, trips, pods, FleetView(**fleet), POLICY, NOW)


def test_the_nearest_trip_gets_a_warm_browser() -> None:
    a = trip(1, 2)
    result = run([a])
    assert result.start == [Start(a.id, "ann", "warm")]
    assert result.warm == {a.id}


def test_trips_further_out_get_short_checks_when_due() -> None:
    a, b = trip(1, 10), trip(2, 20, next_check_at=NOW + 3600)
    result = run([a, b])
    assert result.start == [Start(a.id, "ann", "short_check")]  # b isn't due for an hour
    assert result.warm == set()


def test_one_warm_browser_and_short_checks_for_the_rest() -> None:
    near, later = trip(1, 2), trip(2, 5)
    result = run([later, near])
    assert result.start == [Start(near.id, "ann", "warm")]  # one start per user per pass
    result = run([later, near], [pod(near, "starting")])
    assert result.start == [Start(later.id, "ann", "short_check")]


def test_a_nearer_trip_takes_the_browser_only_from_a_watching_pod() -> None:
    old, nearer = trip(1, 5), trip(2, 1)
    result = run([old, nearer], [pod(old, "watching")])
    assert result.drain == [Drain(f"fw-{old.id}-g1", "superseded")]
    assert result.start == []  # the new warm pod waits until the old one is gone
    for phase in ("starting", "holding", "draining"):
        result = run([old, nearer], [pod(old, phase)])
        assert result.drain == []
    result = run([old, nearer])  # the old pod is gone
    assert result.start == [Start(nearer.id, "ann", "warm")]


def test_a_held_warm_browser_leaves_the_nearer_trip_to_short_checks() -> None:
    old, nearer = trip(1, 5), trip(2, 1)
    result = run([old, nearer], [pod(old, "holding")])
    assert result.start == [Start(nearer.id, "ann", "short_check")]


def test_ineligible_trips_lose_their_browser_unless_it_holds_a_cart() -> None:
    off = trip(1, 2, enabled=False)
    asked = trip(2, 3, paused_reason="after_checkout")
    gone = trip(3, -2)
    result = run([off, asked, gone], [pod(off), pod(asked, "starting"), pod(gone)])
    assert sorted(result.drain, key=lambda d: d.pod) == [
        Drain(f"fw-{off.id}-g1", "paused"),
        Drain(f"fw-{asked.id}-g1", "paused"),
        Drain(f"fw-{gone.id}-g1", "past"),
    ]
    assert result.start == []
    result = run([off], [pod(off, "holding")])
    assert result.drain == []


def test_a_suspended_user_and_maintenance_stop_everything() -> None:
    a = trip(1, 2)
    result = run([a], [pod(a)], users=[UserView("ann", active=False)])
    assert result.drain == [Drain(f"fw-{a.id}-g1", "suspended")]
    result = run([a], [pod(a)], maintenance=True)
    assert result.drain == [Drain(f"fw-{a.id}-g1", "maintenance")]
    result = run([a], [pod(a)], users=[UserView("ann", paused=True)])  # an old "Pause all", for a release
    assert result.drain == [Drain(f"fw-{a.id}-g1", "paused")]
    assert run([a], maintenance=True).start == []


def test_quiet_hours_stop_a_users_trips_unless_lifted() -> None:
    """In a user's quiet hours their trips are treated as paused: nothing starts, and a browser that holds no
    cart is drained. A trip they lifted them for (a manual check, "Hold it for me") is checked as usual."""
    ann, bob = UserView("ann", quiet=QuietHours(time(23, 0), time(6, 0))), UserView("bob")
    near, later, bobs = trip(1, 2), trip(2, 10), trip(3, 2, user="bob")
    night = NOW + 11.5 * 3600  # 23:30 Pacific

    result = plan([ann, bob], [near, later, bobs], [], FleetView(), POLICY, night)
    assert result.start == [Start(bobs.id, "bob", "warm")]  # quiet hours are Ann's own
    result = plan([ann], [near, later], [pod(near)], FleetView(), POLICY, night)
    assert result.drain == [Drain(f"fw-{near.id}-g1", "paused")]
    assert plan([ann], [near], [pod(near, "holding")], FleetView(), POLICY, night).drain == []
    assert plan([ann], [near], [], FleetView(), POLICY, NOW).start == [Start(near.id, "ann", "warm")]  # noon

    lifted = replace(near, quiet_lifted_until=night + 600)
    assert plan([ann], [lifted, later], [], FleetView(), POLICY, night).start == [Start(near.id, "ann", "warm")]
    lapsed = replace(near, quiet_lifted_until=night - 1)
    assert plan([ann], [lapsed], [pod(near)], FleetView(), POLICY, night).drain == [Drain(f"fw-{near.id}-g1", "paused")]


def test_a_cart_held_for_a_quiet_trip_leaves_a_lifted_trip_to_short_checks() -> None:
    """The warm pod holds a cart for a trip that's now ineligible (quiet hours, paused): it stays until the
    cart is let go, so a trip the user lifted their quiet hours for gets a short check meanwhile instead of
    waiting for that pod to leave."""
    ann = UserView("ann", quiet=QuietHours(time(23, 0), time(6, 0)))
    held, lifted = trip(1, 2), trip(2, 1)
    night = NOW + 11.5 * 3600  # 23:30 Pacific
    lifted = replace(lifted, quiet_lifted_until=night + 600)
    result = plan([ann], [held, lifted], [pod(held, "holding")], FleetView(), POLICY, night)
    assert result.drain == []
    assert result.start == [Start(lifted.id, "ann", "short_check")]
    paused = replace(held, paused_reason="after_checkout")
    assert run([paused, trip(2, 1)], [pod(paused, "holding")]).start == [Start(trip(2, 1).id, "ann", "short_check")]


def test_a_pod_for_a_deleted_trip_is_drained() -> None:
    orphan = trip(9, 2)
    assert run([], [pod(orphan)]).drain == [Drain(f"fw-{orphan.id}-g1", "deleted")]


def test_removing_a_trip_drains_its_pod_even_while_it_holds_a_cart() -> None:
    """Nobody can check out of a removed trip's cart, so it isn't kept: draining lets it go."""
    removed, archived = trip(9, 2), trip(8, 3, archived=True)
    result = run([archived], [pod(removed, "holding"), pod(archived, "holding")])
    assert sorted(result.drain, key=lambda d: d.pod) == [
        Drain(f"fw-{archived.id}-g1", "deleted"),
        Drain(f"fw-{removed.id}-g1", "deleted"),
    ]
    assert run([], [pod(removed, "draining")]).drain == []  # already on its way out


def test_a_removed_trip_s_held_warm_browser_hands_over_to_the_next_trip() -> None:
    removed, next_trip = trip(9, 1), trip(2, 2)
    result = run([next_trip], [pod(removed, "holding")])
    assert result.drain == [Drain(f"fw-{removed.id}-g1", "deleted")]
    assert result.start == []  # no short check meanwhile: the warm browser comes once the old pod is gone


def test_backing_off_or_needing_attention_waits() -> None:
    a = trip(1, 2, backoff_until=NOW + 60)
    b = trip(2, 3, needs_attention=True, user="bob")
    assert run([a, b], users=[ANN, BOB]).start == []


def test_one_pod_per_trip_and_failed_pods_dont_count() -> None:
    a = trip(1, 2)
    assert run([a], [pod(a, "starting", mode="short_check")]).start == []
    assert run([a], [pod(a, "failed")]).start == [Start(a.id, "ann", "warm")]


def test_admins_can_give_a_user_more_warm_browsers() -> None:
    a, b, c = trip(1, 1), trip(2, 2), trip(3, 3)
    users = [UserView("ann", max_active_browsers=2)]
    result = run([a, b, c], [pod(a)], users=users)
    assert result.start == [Start(b.id, "ann", "warm")]
    assert result.warm == {a.id, b.id}


def test_a_spare_slot_is_kept_for_short_checks() -> None:
    ann, bob, cat = trip(1, 1), trip(2, 2, user="bob"), trip(3, 20, user="cat")
    busy = [pod(trip(7, 3, user="x"), user="x"), pod(trip(8, 4, user="y"), user="y")]
    result = run([ann, bob, cat], busy, users=[ANN, BOB, CAT, UserView("x"), UserView("y")], max_browsers_total=4)
    # 4 slots, 1 kept for short checks: one more warm browser, and a short check in the spare slot. Bob's
    # trip waits for a warm browser, but it's due a short check too, and it's the more urgent one.
    assert result.start == [Start(ann.id, "ann", "warm"), Start(bob.id, "bob", "short_check")]
    assert result.queued == {bob.id: 1}
    assert result.overdue == [cat.id]


def test_the_queue_favours_users_with_fewer_browsers() -> None:
    ann_near, bob_far = trip(1, 1), trip(2, 4, user="bob")
    ann_busy = pod(trip(3, 20), mode="short_check", phase="watching")
    result = run([ann_near, bob_far], [ann_busy], users=[ANN, BOB], max_browsers_total=2)
    assert result.start == [Start(bob_far.id, "bob", "warm")]  # bob has none running
    assert result.queued == {ann_near.id: 1}


def test_the_start_throttle() -> None:
    trips = [trip(n, 1 + n % 5, user=f"u{n}") for n in range(1, 6)]
    users = [UserView(f"u{n}") for n in range(1, 6)]
    result = run(trips, users=users, max_browsers_total=10)
    assert len(result.start) == 2  # at most two starting at once
    assert len(result.queued) == 3
    assert run(trips, users=users, max_browsers_total=10, starts_last_hour=MAX_STARTS_PER_HOUR).start == []


def test_a_users_starts_are_spaced() -> None:
    a, b = trip(1, 1), trip(2, 20)
    result = run([a, b], last_start_by_user={"ann": NOW - 5})
    assert result.start == []
    assert result.queued == {a.id: 1}
    assert result.overdue == [a.id, b.id]


def test_a_queued_warm_trip_still_gets_short_checks() -> None:
    a = trip(1, 2)
    full = [pod(trip(7, 3, user="x"), user="x")]
    result = run([a], full, users=[ANN, UserView("x")], max_browsers_total=2)
    # The one free slot is the short-check slot: no warm browser yet, but the trip is still checked.
    assert result.queued == {a.id: 1}
    assert result.start == [Start(a.id, "ann", "short_check")]
