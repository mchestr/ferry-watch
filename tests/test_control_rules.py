"""The control plane's pure rules: limits, cart gating, the poll curve with the budget, and the API's answers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time

import pytest

from ferry_watch.control.configure import (
    Allowance,
    HubSettings,
    StreamViewer,
    Switches,
    TripRow,
    build_configure,
    cart_gate,
    curve_interval,
    interval_s,
    stretch,
    worth_pushing,
)
from ferry_watch.control.kube import AGENT_USERNAME, ObservedPod, pod_identity
from ferry_watch.control.limits import Effective, Limits
from ferry_watch.models import PACIFIC, FarePreference, Trip, Vehicle
from ferry_watch.polling import QuietHours
from ferry_watch.protocol import Brake, Configure

TRIP = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 16), earliest=time(7))
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=PACIFIC)
NIGHT = NOON.replace(hour=23, minute=30)
PLENTY = Allowance(holds_per_day=4, cart_attempts_per_day=6, vnc_minutes_per_day=60)  # quiet 10 pm to 5 am
QUIET = replace(PLENTY, quiet=QuietHours(time(23, 0), time(6, 0)))  # the user's own quiet hours
SETTINGS = HubSettings()

# ---- limits


def test_effective_limits_have_a_source() -> None:
    limits = Limits()
    assert limits.effective("maxTrips") == Effective(3, "default")
    assert limits.effective("maxTrips", 5) == Effective(5, "override")
    assert limits.effective("maxTrips", 50) == Effective(10, "ceiling")  # an override never passes the ceiling
    assert limits.value("maxActiveBrowsers", {"maxActiveBrowsers": 2}) == 2


def test_a_default_above_its_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="between 0 and its ceiling"):
        Limits(defaults={"maxTrips": 11})


# ---- carting


@pytest.mark.parametrize(
    ("allowance", "switches", "at", "expected"),
    [
        (PLENTY, Switches(), NOON, "ok"),
        (PLENTY, Switches(pause_carting=True), NOON, "carting_paused"),
        (PLENTY, Switches(maintenance=True), NOON, "carting_paused"),
        (PLENTY, Switches(), NOON.replace(hour=22), "quiet_hours"),  # everyone has quiet hours: 10 pm to 5 am
        (PLENTY, Switches(), NOON.replace(hour=4, minute=59), "quiet_hours"),
        (PLENTY, Switches(), NOON.replace(hour=5), "ok"),
        (PLENTY, Switches(), NOON.replace(hour=21, minute=59), "ok"),
        (QUIET, Switches(), NIGHT, "quiet_hours"),
        (QUIET, Switches(), NOON.replace(hour=5, minute=59), "quiet_hours"),
        (QUIET, Switches(), NOON.replace(hour=6), "ok"),
        (QUIET, Switches(), NOON.replace(hour=22, minute=30), "ok"),  # their own hours, not the default
        (Allowance(4, 6, 60, holds_today=4), Switches(), NOON, "no_holds_left"),
        (Allowance(4, 6, 60, cart_attempts_today=6), Switches(), NOON, "limit"),
        (Allowance(4, 6, 60, vnc_seconds_today=31 * 60), Switches(), NOON, "limit"),  # < a session's minutes left
    ],
)
def test_cart_gate(allowance: Allowance, switches: Switches, at: datetime, expected: str) -> None:
    assert cart_gate(TRIP, allowance, switches, SETTINGS, at) == expected


def test_lifted_quiet_hours_allow_a_hold_and_nothing_else() -> None:
    """A manual check or "Hold it for me" lifts the user's quiet hours for the trip: they're there, asking.
    Every other limit stays."""
    assert cart_gate(TRIP, QUIET, Switches(), SETTINGS, NIGHT, lifted=True) == "ok"
    assert cart_gate(TRIP, QUIET, Switches(pause_carting=True), SETTINGS, NIGHT, lifted=True) == "carting_paused"
    used = replace(QUIET, holds_today=4)
    assert cart_gate(TRIP, used, Switches(), SETTINGS, NIGHT, lifted=True) == "no_holds_left"
    assert cart_gate(TRIP, replace(QUIET, cart_attempts_today=6), Switches(), SETTINGS, NOON, lifted=True) == "limit"
    off = HubSettings(auto_cart=False)
    assert cart_gate(TRIP, QUIET, Switches(), off, NOON, lifted=True) == "carting_paused"


def test_alert_only_fares_and_vehicles() -> None:
    settings = HubSettings(alert_only_fares=frozenset({"prepaid"}), alert_only_vehicles=frozenset({"tall"}))
    prepaid = Trip(id=TRIP.id, origin="SWB", destination="TSA", date=TRIP.date, fare=FarePreference.PREPAID)
    tall = Trip(id=TRIP.id, origin="SWB", destination="TSA", date=TRIP.date, vehicle=Vehicle.TALL, height_ft=8)
    assert cart_gate(prepaid, PLENTY, Switches(), settings, NOON) == "limit"
    assert cart_gate(tall, PLENTY, Switches(), settings, NOON) == "limit"
    assert cart_gate(TRIP, PLENTY, Switches(), settings, NOON) == "ok"


# ---- the poll curve and the budget


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 10, 9, 12, tzinfo=PACIFIC), 300),  # 6.8 days out
        (datetime(2026, 10, 13, 12, tzinfo=PACIFIC), 120),  # 2.8 days
        (datetime(2026, 10, 15, 12, tzinfo=PACIFIC), 60),  # 19 h
        (datetime(2026, 10, 16, 3, tzinfo=PACIFIC), 30),  # 4 h
        (datetime(2026, 10, 16, 9, tzinfo=PACIFIC), 30),  # inside the window
    ],
)
def test_the_curve(at: datetime, expected: int) -> None:
    assert curve_interval(TRIP, at, SETTINGS.policy) == expected


def test_the_budget_stretches_only_beyond_it() -> None:
    assert stretch([30] * 7, 20) == 1.0  # 14 checks a minute: under 20
    assert stretch([30] * 22, 20) == pytest.approx(2.2)  # 44/min
    assert interval_s(30, 2.2, Brake()) == 66
    assert interval_s(300, 1.0, Brake(multiplier=8)) == 2400
    assert interval_s(3000, 1.0, Brake(multiplier=8)) == 3600  # never past the protocol's maximum


def test_small_interval_changes_aren_t_pushed() -> None:
    assert worth_pushing(None, 60)
    assert not worth_pushing(60, 64)
    assert worth_pushing(60, 120)


def test_configure() -> None:
    envelope: dict[str, object] = {"seq": 1, "trip": TRIP.id, "boot": "0" * 16, "rev": 3}
    row = TripRow(TRIP, rev=3, enabled=True, paused_reason=None, alerted=("09:00",))
    configure = build_configure(
        envelope,
        row,
        mode="warm",
        user_active=True,
        allowance=PLENTY,
        switches=Switches(),
        settings=SETTINGS,
        stretch_by=1.0,
        now=NOON.timestamp(),
    )
    assert (configure.active, configure.auto_cart, configure.vnc_allowed, configure.interval_s) == (
        True,
        True,
        True,
        60,
    )
    assert configure.alerted == ["09:00"]
    paused = build_configure(
        envelope,
        TripRow(TRIP, rev=3, enabled=True, paused_reason="after_checkout"),
        mode="warm",
        user_active=False,
        allowance=PLENTY,
        switches=Switches(brake=Brake(paused=True)),
        settings=SETTINGS,
        stretch_by=1.0,
        now=NOON.timestamp(),
    )
    assert (paused.active, paused.vnc_allowed) == (False, False)


def test_configure_for_a_requested_hold() -> None:
    """While the user's request lasts, the pod may cart: through quiet hours and the trip's own auto-cart off."""
    envelope: dict[str, object] = {"seq": 1, "trip": TRIP.id, "boot": "0" * 16, "rev": 3}
    night = NIGHT.timestamp()
    manual = Trip(id=TRIP.id, origin="SWB", destination="TSA", date=TRIP.date, earliest=time(7), auto_cart=False)

    def configure(trip: Trip, requested_until: float | None) -> Configure:
        return build_configure(
            envelope,
            TripRow(trip, rev=3, enabled=True, paused_reason=None, hold_requested_until=requested_until),
            mode="warm",
            user_active=True,
            allowance=QUIET,
            switches=Switches(),
            settings=SETTINGS,
            stretch_by=1.0,
            now=night,
        )

    assert configure(TRIP, None).auto_cart is False  # quiet hours
    asked = configure(manual, night + 600)
    assert (asked.active, asked.auto_cart, asked.trip_spec.auto_cart) == (True, True, True)
    lapsed = configure(manual, night - 1)
    assert (lapsed.active, lapsed.auto_cart, lapsed.trip_spec.auto_cart) == (False, False, False)


@pytest.mark.parametrize(
    ("stream_viewer", "is_admin", "user_active", "allowed"),
    [
        ("off", True, True, False),  # the kill switch
        ("admins", False, True, False),
        ("admins", True, True, True),
        ("everyone", False, True, True),
        ("everyone", True, False, False),  # a suspended owner may view nothing
    ],
)
def test_configure_says_whether_the_pod_may_stream(
    stream_viewer: StreamViewer, is_admin: bool, user_active: bool, allowed: bool
) -> None:
    envelope: dict[str, object] = {"seq": 1, "trip": TRIP.id, "boot": "0" * 16, "rev": 3}
    configure = build_configure(
        envelope,
        TripRow(TRIP, rev=3, enabled=True, paused_reason=None, owner_is_admin=is_admin),
        mode="warm",
        user_active=user_active,
        allowance=PLENTY,
        switches=Switches(),
        settings=HubSettings(stream_viewer=stream_viewer),
        stretch_by=1.0,
        now=NOON.timestamp(),
    )
    assert configure.cast_allowed is allowed


@pytest.mark.parametrize(
    ("allowance", "at", "lifted_until", "checking"),
    [
        (QUIET, NIGHT, None, False),  # the user's quiet hours: nothing is checked
        (QUIET, NIGHT, NIGHT.timestamp() + 600, True),  # lifted for this trip by a manual check
        (QUIET, NIGHT, NIGHT.timestamp() - 1, False),  # the lift has lapsed
        (QUIET, NOON, None, True),
        (PLENTY, NIGHT, None, False),  # the default quiet hours, 10 pm to 5 am
        (PLENTY, NOON, None, True),
    ],
)
def test_in_quiet_hours_a_trip_isnt_checked_unless_lifted(
    allowance: Allowance, at: datetime, lifted_until: float | None, checking: bool
) -> None:
    envelope: dict[str, object] = {"seq": 1, "trip": TRIP.id, "boot": "0" * 16, "rev": 3}
    configure = build_configure(
        envelope,
        TripRow(TRIP, rev=3, enabled=True, paused_reason=None, quiet_lifted_until=lifted_until),
        mode="warm",
        user_active=True,
        allowance=allowance,
        switches=Switches(),
        settings=SETTINGS,
        stretch_by=1.0,
        now=at.timestamp(),
    )
    assert (configure.active, configure.auto_cart) == (checking, checking)  # lifted: it may hold a cart too


# ---- the API server's answers


def review(**user: object) -> dict[str, object]:
    base = {
        "username": AGENT_USERNAME,
        "extra": {
            "authentication.kubernetes.io/pod-uid": ["uid-1"],
            "authentication.kubernetes.io/pod-name": ["fw-3f9c0a1b2c3d4e5f-g1"],
        },
    }
    return {"status": {"authenticated": True, "user": base | user}}


def test_a_token_review_names_a_browser_pod() -> None:
    identity = pod_identity(review())
    assert identity is not None
    assert (identity.pod_uid, identity.pod_name) == ("uid-1", "fw-3f9c0a1b2c3d4e5f-g1")
    assert pod_identity({"status": {"authenticated": False}}) is None
    assert pod_identity(review(username="system:serviceaccount:ferry-watch:ferry-control")) is None
    assert pod_identity(review(extra={})) is None  # a token not bound to a pod


def test_observed_pods() -> None:
    pod = ObservedPod.from_api(
        {
            "metadata": {"name": "fw-x", "uid": "u", "labels": {"a": "b"}, "deletionTimestamp": "2026-10-15T00:00:00Z"},
            "spec": {"nodeName": "n1"},
            "status": {
                "phase": "Failed",
                "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
                "containerStatuses": [{"state": {"terminated": {"message": "fatal markup_changed\n"}}}],
            },
        }
    )
    assert (pod.phase, pod.node, pod.deleting, pod.unschedulable) == ("Failed", "n1", True, True)
    assert pod.termination_message == "fatal markup_changed"
