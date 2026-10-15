"""The agent's one-trip watcher, against the fake site, a fake operator link and a hand-moved clock."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as clock_time
from typing import Any

import msgspec
import pytest

from ferry_watch.agent.cast_protocol import Clock as ClockMessage
from ferry_watch.agent.cast_protocol import CloseCode
from ferry_watch.agent.store import OneTripStore
from ferry_watch.agent.watch import (
    FAIL_SLOW_INTERVAL_S,
    MAX_CART_ATTEMPTS,
    RECYCLE_AFTER_FAILURES,
    Finish,
    TripWatcher,
)
from ferry_watch.bcferries.errors import MarkupChanged, NotBookable
from ferry_watch.bcferries.urls import CheckoutView
from ferry_watch.models import PACIFIC, Fare, Sailing, SailingStatus, Trip
from ferry_watch.protocol import (
    AlertKind,
    AlertParams,
    Brake,
    Configure,
    EventCode,
    EventParams,
    FarePreference,
    HoldLimits,
    Phase,
    TripSpec,
    decode_agent_message,
    encode,
)
from ferry_watch.state import CartStatus
from tests.fakes import FakeSite, sailing

TRIP = Trip(
    id="3f9c0a1b2c3d4e5f",
    origin="SWB",
    destination="TSA",
    date=date(2026, 10, 14),
    earliest=clock_time(7),
    latest=clock_time(20),
)
START = datetime(2026, 10, 12, 10, 0, tzinfo=PACIFIC).timestamp()
DEADLINE = START + 26 * 3600
HOLD = HoldLimits(unclaimed_s=1800, session_s=1800, viewer_gone_s=600, hard_s=3600)
SEARCH = ["search"]


def configure(rev: int = 1, *, trip: Trip = TRIP, **kw: Any) -> Configure:
    fields: dict[str, Any] = {"active": True, "interval_s": 300, "auto_cart": True, "hold": HOLD, "vnc_allowed": True}
    return Configure(
        seq=rev,
        trip=TRIP.id,
        boot="0" * 16,
        rev=rev,
        trip_spec=msgspec.convert(trip.to_json(), TripSpec),
        **(fields | kw),
    )


@dataclass
class FakeLink:
    grant: bool = True
    silent: float = 0.0
    events: list[tuple[EventCode, EventParams]] = field(default_factory=list)
    alerts: list[tuple[AlertKind, AlertParams]] = field(default_factory=list)
    claims: list[tuple[str, FarePreference]] = field(default_factory=list)
    unacked: set[int] = field(default_factory=set)
    hold_acks: bool = False

    def event(self, code: EventCode, params: EventParams | None = None) -> None:
        self.events.append((code, params or EventParams()))

    def alert(self, kind: AlertKind, params: AlertParams | None = None) -> int:
        self.alerts.append((kind, params or AlertParams()))
        seq = len(self.alerts)
        if self.hold_acks:
            self.unacked.add(seq)
        return seq

    def acked(self, seq: int) -> bool:
        return seq not in self.unacked

    async def claim(self, depart: str, fare: FarePreference) -> bool:
        self.claims.append((depart, fare))
        return self.grant

    def silent_for(self) -> float:
        return self.silent

    @property
    def kinds(self) -> list[AlertKind]:
        return [kind for kind, _ in self.alerts]

    def last(self) -> AlertKind:
        return self.alerts[-1][0]

    @property
    def codes(self) -> list[EventCode]:
        return [code for code, _ in self.events]


@dataclass
class Clock:
    now: float = START

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Rig:
    watcher: TripWatcher
    store: OneTripStore
    site: FakeSite
    link: FakeLink
    clock: Clock
    closed_viewers: list[str]
    lockdown: list[bool]
    close_codes: list[CloseCode]

    async def step(self) -> Finish | None:
        return await self.watcher.step()

    async def check(self) -> Finish | None:
        """Make a check due now and take one step."""
        self.watcher.next_due = self.watcher.retry_at = None
        self.watcher._bucket.tokens = 2  # checks in tests come faster than the bucket allows
        return await self.step()

    def calls(self) -> list[str]:
        return [c for s in self.site.sessions for c in s.calls]


@pytest.fixture
def rig() -> Rig:
    store = OneTripStore(TRIP.id)
    store.apply(configure())
    site = FakeSite(sailings=[sailing(9, available=False), sailing(11, available=False)])
    link = FakeLink()
    clock = Clock()
    closed: list[str] = []
    codes: list[CloseCode] = []
    lockdown = [True]

    async def close_viewer(reason: str, code: CloseCode) -> None:
        closed.append(reason)
        codes.append(code)

    watcher = TripWatcher(
        store,
        site,
        link,
        deadline_at=DEADLINE,
        lockdown_ok=lambda: lockdown[0],
        close_viewer=close_viewer,
        clock=clock,
    )
    return Rig(watcher, store, site, link, clock, closed, lockdown, codes)


def phase(rig: Rig) -> Phase:
    return rig.watcher.phase  # a call, so the type checker doesn't pin it between steps


def open_up(rig: Rig, *hours: int) -> None:
    rig.site.sailings = [sailing(h, available=h in hours) for h in (9, 11, 13)]


async def hold_one(rig: Rig, hour: int = 9) -> str:
    open_up(rig, hour)
    await rig.check()
    cart = rig.watcher.cart
    assert cart is not None
    assert cart.status is CartStatus.READY
    return cart.id


# ---- checking


async def test_checks_on_the_operators_interval(rig: Rig) -> None:
    assert phase(rig) == "starting"
    await rig.step()
    assert rig.calls() == SEARCH
    assert rig.link.codes[0] == "session_opened"
    next_due = rig.watcher.next_due
    assert next_due is not None
    assert 300 * 0.85 <= next_due - rig.clock.now <= 300 * 1.15
    await rig.step()
    assert rig.calls() == SEARCH  # not due yet
    rig.clock.now = next_due
    await rig.step()
    assert rig.calls() == ["search", "refresh"]
    assert phase(rig) == "watching"


async def test_slower_when_the_operator_is_unreachable_or_braking(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, interval_s=30))
    assert rig.watcher.effective_interval() == 30
    rig.store.apply(configure(rev=3, interval_s=30, brake=Brake(multiplier=4)))
    assert rig.watcher.effective_interval() == 120
    rig.link.silent = 11 * 60
    assert rig.watcher.effective_interval() == FAIL_SLOW_INTERVAL_S


@pytest.mark.parametrize(
    "change",
    [{"active": False}, {"brake": Brake(paused=True)}, {"breaker": "open"}],
)
async def test_no_checks_when_stopped(rig: Rig, change: dict[str, Any]) -> None:
    rig.store.apply(configure(rev=2, **change))
    await rig.check()
    assert rig.calls() == []


async def test_no_checks_after_the_last_departure(rig: Rig) -> None:
    rig.clock.now = datetime(2026, 10, 14, 20, 1, tzinfo=PACIFIC).timestamp()
    await rig.check()
    assert rig.calls() == []


async def test_failures_back_off_alert_and_recycle(rig: Rig) -> None:
    rig.site.error = MarkupChanged("no results table")
    for _ in range(3):
        await rig.check()
    assert rig.watcher.consecutive_errors == 3
    assert rig.link.kinds == ["failing"]
    assert rig.link.alerts[0][1].error_class == "markup_changed"
    assert rig.watcher.error == ("markup_changed", "no results table")
    assert rig.link.codes.count("check_failed") == 3
    for _ in range(RECYCLE_AFTER_FAILURES - 3):
        await rig.check()
    assert await rig.step() == Finish("recycle")


async def test_recycles_before_the_deadline_while_idle(rig: Rig) -> None:
    rig.clock.now = DEADLINE - 120 * 60
    assert await rig.step() == Finish("recycle")


# ---- alerting and carting


async def test_alerts_once_when_carting_is_off(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, auto_cart=False))
    open_up(rig, 9, 11)
    await rig.check()
    await rig.check()
    assert rig.link.alerts == [("bookable", AlertParams(departures=["09:00", "11:00"]))]
    assert rig.link.claims == []
    assert rig.watcher.offered == {clock_time(9), clock_time(11)}


async def test_carts_with_a_claim_and_holds(rig: Rig) -> None:
    open_up(rig, 9, 11)
    await rig.check()
    assert rig.link.claims == [("09:00", "cheapest")]
    assert rig.link.alerts == [
        ("cart_held", AlertParams(departures=["09:00", "11:00"], fare="Saver", price="$93.45")),
    ]
    assert phase(rig) == "holding"
    assert rig.watcher.alerted == {clock_time(9)}
    assert rig.watcher.offered == {clock_time(11)}
    assert "cart_added" in rig.link.codes


async def test_a_refused_claim_alerts_and_a_later_grant_carts_once(rig: Rig) -> None:
    rig.link.grant = False  # the user holds a cart in another pod
    open_up(rig, 9)
    await rig.check()
    assert rig.link.kinds == ["bookable"]
    assert rig.watcher.cart is None
    await rig.check()
    assert rig.link.kinds == ["bookable"]  # not again
    rig.link.grant = True  # the other hold ended
    await rig.check()
    assert rig.link.kinds == ["bookable", "cart_held"]  # the one re-alert
    assert rig.watcher.holding


async def test_a_failed_cart_alerts_and_can_be_dismissed(rig: Rig) -> None:
    rig.site.cart_error = MarkupChanged("no fare buttons")
    open_up(rig, 9)
    await rig.check()
    assert rig.link.alerts == [("cart_failed", AlertParams(departures=["09:00"], error_class="markup_changed"))]
    cart = rig.watcher.cart
    assert cart is not None
    assert cart.status is CartStatus.FAILED
    assert rig.watcher.dismiss(cart.id)
    assert rig.watcher.cart is None


async def test_a_failed_cart_is_left_alone_until_the_user_asks(rig: Rig) -> None:
    """Each try spends one of the user's daily cart attempts, so a failure isn't retried on every check."""
    rig.site.cart_error = MarkupChanged("no fare buttons")
    open_up(rig, 9, 11)
    for _ in range(4):
        await rig.check()
    assert rig.link.claims == [("09:00", "cheapest"), ("11:00", "cheapest")]  # each tried once
    assert rig.link.alerts == [
        ("cart_failed", AlertParams(departures=["09:00", "11:00"], error_class="markup_changed")),
        ("cart_failed", AlertParams(departures=["11:00"], error_class="markup_changed")),  # each frees its claim
    ]
    rig.watcher.check_now()  # "Check now" or "Hold it for me"
    await rig.check()
    assert rig.link.claims[-1] == ("09:00", "cheapest")
    rig.site.cart_error = None
    rig.watcher.check_now()
    await rig.check()
    assert rig.watcher.holding
    assert rig.link.kinds == ["cart_failed", "cart_failed", "cart_failed", "cart_held"]


async def test_a_hold_asked_for_after_a_bookable_alert_says_when_it_fails(rig: Rig) -> None:
    """ "Hold it for me" asks for a sailing already alerted as bookable: its failure still alerts, which is
    what frees the user's cart claim and ends the request."""
    rig.store.apply(configure(rev=2, auto_cart=False))  # quiet hours
    open_up(rig, 9)
    await rig.check()
    assert rig.link.kinds == ["bookable"]
    rig.store.apply(configure(rev=2, auto_cart=True))  # the request lifts them
    rig.site.cart_error = NotBookable("sold out while adding")
    rig.watcher.check_now()
    await rig.check()
    assert rig.link.alerts[-1] == ("cart_failed", AlertParams(departures=["09:00"], error_class="not_bookable"))


async def test_failures_are_forgotten_when_the_trip_changes(rig: Rig) -> None:
    rig.site.cart_error = MarkupChanged("no fare buttons")
    open_up(rig, 9)
    await rig.check()
    rig.site.cart_error = None
    edited = Trip.from_json(TRIP.to_json() | {"fare": "saver"})  # same search, another fare
    rig.store.apply(configure(rev=2, trip=edited))
    await rig.check()
    assert rig.link.claims == [("09:00", "cheapest"), ("09:00", "saver")]
    assert rig.watcher.holding


async def test_a_failed_sailing_is_tried_again_after_it_sells_out_and_reopens(rig: Rig) -> None:
    rig.site.cart_error = MarkupChanged("no fare buttons")
    open_up(rig, 9)
    await rig.check()
    open_up(rig)
    await rig.check()
    open_up(rig, 9)
    await rig.check()
    assert rig.link.claims == [("09:00", "cheapest"), ("09:00", "cheapest")]


async def test_cart_attempts_are_capped(rig: Rig) -> None:
    rig.site.cart_error = MarkupChanged("no fare buttons")
    open_up(rig, 9)
    for _ in range(MAX_CART_ATTEMPTS + 2):
        rig.watcher.check_now()
        await rig.check()
    assert len(rig.link.claims) == MAX_CART_ATTEMPTS


async def test_no_cart_near_the_deadline(rig: Rig) -> None:
    rig.clock.now = DEADLINE - 99 * 60
    rig.watcher._recycle_at = DEADLINE  # keep it from recycling first
    open_up(rig, 9)
    await rig.check()
    assert rig.link.claims == []
    assert rig.link.kinds == ["bookable"]


async def test_lockdown_failure_never_carts(rig: Rig) -> None:
    rig.lockdown[0] = False
    open_up(rig, 9)
    await rig.check()
    assert rig.link.claims == []


async def test_sold_out_again_alerts_again_on_reopening(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, auto_cart=False))
    open_up(rig, 9)
    await rig.check()
    open_up(rig)
    await rig.check()
    assert ("sold_out_again", EventParams(depart="09:00")) in rig.link.events
    open_up(rig, 9)
    await rig.check()
    assert rig.link.kinds == ["bookable", "bookable"]


async def test_the_operators_memory_seeds_the_pod(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, alerted=["09:00"], offered=["11:00"]))
    open_up(rig, 9, 11)
    await rig.check()
    assert rig.link.claims == [("11:00", "cheapest")]  # 09:00 was carted before; 11:00 may be once
    assert rig.link.alerts[0][1].departures == ["11:00"]


# ---- the hold


async def test_the_gate_opens_only_for_a_live_hold(rig: Rig) -> None:
    assert rig.watcher.checkout_cart() is None
    cart_id = await hold_one(rig)
    assert rig.watcher.checkout_cart() == cart_id
    rig.store.apply(configure(rev=2, vnc_allowed=False))
    assert rig.watcher.checkout_cart() is None
    rig.store.apply(configure(rev=3))
    rig.lockdown[0] = False
    assert rig.watcher.checkout_cart() is None
    rig.lockdown[0] = True
    rig.clock.advance(HOLD.unclaimed_s)
    assert rig.watcher.checkout_cart() is None  # the clock ran out before the loop noticed


async def test_an_unviewed_hold_expires_and_a_warm_pod_watches_on(rig: Rig) -> None:
    rig.link.hold_acks = True
    await hold_one(rig)
    rig.clock.advance(10 * 60)
    await rig.step()
    rig.link.unacked.clear()  # the operator has the alert (it was down for 10 minutes)
    await rig.step()  # so the unclaimed clock runs from now, not from the cart
    rig.clock.advance(15 * 60)
    await rig.step()
    assert rig.link.last() == "hold_reminder"
    rig.clock.advance(10 * 60)
    await rig.step()
    assert rig.watcher.holding  # 35 minutes since the cart, 25 since the user was told
    rig.clock.advance(5 * 60)
    assert await rig.step() is None
    assert rig.link.last() == "cart_expired"
    assert rig.site.sessions[0].calls[-2:] == ["empty cart", "close"]
    assert ("cart_released", EventParams(depart="09:00")) in rig.link.events
    assert rig.watcher.cart is None
    assert phase(rig) == "watching"
    await rig.check()
    assert rig.site.sessions[-1].calls == SEARCH  # watching again, from a fresh search
    assert rig.link.kinds.count("cart_held") == 1  # and that departure isn't carted again


async def test_a_viewed_hold_ends_the_pod(rig: Rig) -> None:
    await hold_one(rig)
    rig.clock.advance(60)
    rig.watcher.viewer_changed(connected=True)
    rig.clock.advance(HOLD.session_s)
    finish = await rig.step()
    assert finish == Finish(
        "hold_ended", "expired", cart_emptied=True, release_check="landing_url", viewer_was_connected=True
    )
    assert rig.closed_viewers  # the viewer was disconnected first
    assert rig.link.codes.count("viewer_connected") == 1


async def test_a_booking_ends_the_pod_without_emptying(rig: Rig) -> None:
    await hold_one(rig)
    rig.watcher.viewer_changed(connected=True)
    rig.site.view = CheckoutView(booked=True)
    assert await rig.step() == Finish("hold_ended", "booked", viewer_was_connected=True)
    assert "empty cart" not in rig.calls()
    assert "booking_confirmed" in rig.link.codes


async def test_payment_pages_stop_the_session_clock_but_not_the_hard_cap(rig: Rig) -> None:
    await hold_one(rig)
    rig.watcher.viewer_changed(connected=True)
    rig.clock.advance(20 * 60)
    rig.site.view = CheckoutView(payment=True)
    await rig.step()
    rig.clock.advance(20 * 60)  # 40 min into a 30 min session, but 20 of them paying
    assert await rig.step() is None
    state = rig.watcher.state(envelope())
    assert state.cart is not None
    assert state.cart.payment_in_progress
    rig.clock.advance(HOLD.hard_s)
    finish = await rig.step()
    assert finish == Finish("hold_ended", "payment_left_to_site", viewer_was_connected=True)
    assert "empty cart" not in rig.calls()


async def test_the_user_releases_the_cart(rig: Rig) -> None:
    cart_id = await hold_one(rig)
    assert not rig.watcher.release("not-the-cart")
    assert rig.watcher.release(cart_id)
    assert await rig.step() is None  # nobody viewed it: a warm pod carries on
    assert "empty cart" in rig.calls()
    assert rig.link.kinds == ["cart_held"]


async def test_an_unverified_release_is_reported(rig: Rig) -> None:
    cart_id = await hold_one(rig)
    rig.site.empty_check = "none"
    rig.watcher.release(cart_id)
    await rig.step()
    assert rig.link.last() == "release_unverified"


async def test_withdrawn_access_closes_the_viewer(rig: Rig) -> None:
    await hold_one(rig)
    rig.watcher.viewer_changed(connected=True)
    rig.store.apply(configure(rev=2, vnc_allowed=False))
    await rig.step()
    assert rig.closed_viewers == ["checkout access withdrawn"]


async def test_the_viewers_verdict(rig: Rig) -> None:
    """What a viewer may do, in order: draining, then revoked, then the streamed viewer withdrawn, then ended."""
    assert rig.watcher.viewer_verdict("nothing", "rfb") is CloseCode.ENDED  # no hold
    cart_id = await hold_one(rig)
    assert rig.watcher.viewer_verdict(cart_id, "rfb") is None
    assert rig.watcher.viewer_verdict(cart_id, "cast") is CloseCode.WITHDRAWN  # cast_allowed is false by default
    rig.store.apply(configure(rev=2, cast_allowed=True))
    assert rig.watcher.viewer_verdict(cart_id, "cast") is None
    assert rig.watcher.viewer_verdict("another cart", "cast") is CloseCode.ENDED
    rig.clock.advance(HOLD.unclaimed_s)
    assert rig.watcher.viewer_verdict(cart_id, "cast") is CloseCode.ENDED  # on the clock, before the loop runs
    rig.clock.advance(-HOLD.unclaimed_s)
    rig.store.apply(configure(rev=3, cast_allowed=False, vnc_allowed=True))
    assert rig.watcher.viewer_verdict(cart_id, "rfb") is None
    assert rig.watcher.viewer_verdict(cart_id, "cast") is CloseCode.WITHDRAWN
    rig.store.apply(configure(rev=4, cast_allowed=True, vnc_allowed=False))
    assert rig.watcher.viewer_verdict(cart_id, "cast") is CloseCode.REVOKED
    rig.store.apply(configure(rev=5, cast_allowed=True))
    rig.lockdown[0] = False
    assert rig.watcher.viewer_verdict(cart_id, "rfb") is CloseCode.REVOKED
    rig.watcher.drain()
    assert rig.watcher.viewer_verdict(cart_id, "rfb") is CloseCode.DRAINING
    assert rig.watcher.checkout_target() is None


async def test_the_clock_a_streamed_viewer_is_shown(rig: Rig) -> None:
    assert rig.watcher.clock_info() is None
    await hold_one(rig)
    started = rig.clock.now
    hard = started + HOLD.hard_s
    assert rig.watcher.clock_info() == ClockMessage(ends=started + HOLD.unclaimed_s, hard=hard, paused=False)
    rig.clock.advance(60)
    rig.watcher.viewer_changed(connected=True)
    assert rig.watcher.clock_info() == ClockMessage(ends=started + 60 + HOLD.session_s, hard=hard, paused=False)
    rig.site.view = CheckoutView(payment=True)
    await rig.step()
    assert rig.watcher.clock_info() == ClockMessage(ends=None, hard=hard, paused=True)  # the payment page stops it


async def test_the_checkout_target_is_the_held_carts_window(rig: Rig) -> None:
    assert rig.watcher.checkout_target() is None
    await hold_one(rig)
    assert rig.watcher.checkout_target() is rig.site.session
    rig.store.apply(configure(rev=2, vnc_allowed=False))
    assert rig.watcher.checkout_target() is None


async def test_the_kill_switch_closes_a_streamed_viewer(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, cast_allowed=True))
    await hold_one(rig)
    rig.watcher.viewer_changed(connected=True)
    await rig.step()
    assert rig.closed_viewers == []
    rig.store.apply(configure(rev=3, cast_allowed=False))
    await rig.step()
    assert list(zip(rig.closed_viewers, rig.close_codes, strict=True)) == [
        ("streamed viewer withdrawn", CloseCode.WITHDRAWN)
    ]
    await rig.step()  # once, when it turns off
    assert rig.close_codes == [CloseCode.WITHDRAWN]


async def test_each_way_a_hold_ends_has_its_close_code(rig: Rig) -> None:
    await hold_one(rig)
    await rig.watcher.revoke_viewer("revoked (suspended)")
    rig.watcher.viewer_changed(connected=True)
    rig.store.apply(configure(rev=2, vnc_allowed=False))
    await rig.step()
    rig.watcher.drain()
    await rig.step()
    assert rig.close_codes == [CloseCode.REVOKED, CloseCode.REVOKED, CloseCode.DRAINING]


async def test_a_hold_that_ends_closes_the_viewer_as_ended(rig: Rig) -> None:
    await hold_one(rig)
    rig.watcher.viewer_changed(connected=True)
    rig.watcher.release(rig.watcher.checkout_cart() or "")
    await rig.step()
    assert rig.close_codes == [CloseCode.ENDED]


@pytest.mark.parametrize("viewed", [False, True])
async def test_a_lost_browser_loses_the_cart(rig: Rig, viewed: bool) -> None:
    await hold_one(rig)
    if viewed:
        rig.watcher.viewer_changed(connected=True)
    rig.site.session.crash()
    assert await rig.step() == Finish("fatal", viewer_was_connected=viewed)
    assert rig.link.last() == "cart_lost"
    # A departure someone viewed in checkout is never carted again by the agent on its own.
    assert (clock_time(9) in rig.watcher.alerted) is viewed


async def test_the_sign_in_page_is_reported_once(rig: Rig) -> None:
    await hold_one(rig)
    rig.site.view = CheckoutView(sign_in=True)
    await rig.step()
    await rig.step()
    assert rig.link.codes.count("checkout_login_seen") == 1


# ---- short checks


async def test_a_short_check_checks_once(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, mode="short_check"))
    await rig.check()
    assert await rig.check() == Finish("short_check_done")
    assert rig.calls() == SEARCH


async def test_a_short_check_that_carts_stays_for_the_hold(rig: Rig) -> None:
    rig.store.apply(configure(rev=2, mode="short_check"))
    await hold_one(rig)
    await rig.step()  # the operator's ack is seen: the unclaimed clock starts
    rig.clock.advance(HOLD.unclaimed_s)
    finish = await rig.step()
    assert finish == Finish("hold_ended", "expired", cart_emptied=True, release_check="landing_url")


# ---- stopping


async def test_draining_empties_a_held_cart(rig: Rig) -> None:
    await hold_one(rig)
    rig.watcher.drain()
    assert rig.watcher.checkout_cart() is None
    finish = await rig.step()
    assert finish == Finish("drained", "released", cart_emptied=True, release_check="landing_url")
    assert rig.closed_viewers == ["draining"]


async def test_draining_leaves_a_payment_to_the_site(rig: Rig) -> None:
    await hold_one(rig)
    rig.site.view = CheckoutView(payment=True)
    await rig.step()
    rig.watcher.drain("deadline")
    assert await rig.step() == Finish("deadline", "payment_left_to_site")
    assert "empty cart" not in rig.calls()


async def test_draining_interrupts_a_check(rig: Rig) -> None:
    stuck = asyncio.Event()

    async def queue(_: Trip) -> None:
        stuck.set()
        await asyncio.Event().wait()  # BC Ferries' waiting room

    rig.site.search_hook = queue
    task = asyncio.create_task(rig.watcher.run())
    await asyncio.wait_for(stuck.wait(), 5)
    rig.watcher.drain()
    assert await asyncio.wait_for(task, 5) == Finish("drained")
    assert rig.site.session.closed


# ---- what the operator sees


def envelope() -> dict[str, Any]:
    return {"seq": 1, "trip": TRIP.id, "boot": "0" * 16, "rev": 1}


async def test_state_crosses_the_wire(rig: Rig) -> None:
    rig.site.sailings = [
        sailing(9),
        Sailing(
            clock_time(11),
            clock_time(12, 35),
            "x" * 200,
            SailingStatus.AVAILABLE,
            "",
            "<script>",
            (Fare("A fare name that is far too long to fit", "€93,45 <b>", selectable=True),),
        ),
    ]
    await rig.check()
    rig.watcher.viewer_changed(connected=True)
    rig.clock.advance(30)
    state = rig.watcher.state(envelope())
    decoded = decode_agent_message(encode(state))  # the operator would refuse anything out of bounds
    assert decoded == state
    assert state.phase == "holding"
    assert state.cart is not None
    assert state.cart.depart == "09:00"
    assert state.cart.hard_deadline == pytest.approx(rig.watcher.hold.ready_at + HOLD.hard_s)  # type: ignore[union-attr]
    assert state.viewer.connected
    assert state.viewer.seconds == 30
    assert state.sailings[1].vessel == "x" * 64
    assert state.sailings[1].fares[0].price is None  # page text that doesn't look like a price is left out
    assert state.navs.search == 1
    rig.watcher.navs_sent()
    assert rig.watcher.state(envelope()).navs.search == 0  # counts are since the last state sent


async def test_check_now_is_rate_limited(rig: Rig) -> None:
    await rig.step()
    rig.watcher.check_now()
    assert rig.watcher.next_due is not None  # ignored: a check started under 15 s ago
    rig.clock.advance(20)
    rig.watcher.check_now()
    assert rig.watcher.next_due is None
