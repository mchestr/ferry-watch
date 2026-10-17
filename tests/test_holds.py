from __future__ import annotations

from waitlist.holds import HoldLimits, HoldState, Notice, Release, evaluate
from waitlist.protocol import MAX_HARD_S, MAX_SESSION_S, MAX_UNCLAIMED_S, MAX_VIEWER_GONE_S

MIN = 60.0
LIMITS = HoldLimits()  # 30 min unclaimed, 30 min session, 10 min viewer gone, 60 min hard cap


def test_an_unopened_cart_is_released_after_the_unclaimed_time() -> None:
    hold = HoldState(ready_at=0, acked_at=0)
    assert evaluate(hold, LIMITS, 29 * MIN).release is None
    verdict = evaluate(hold, LIMITS, 30 * MIN)
    assert verdict.release is Release.UNCLAIMED


def test_the_unclaimed_clock_starts_when_the_user_is_told() -> None:
    hold = HoldState(ready_at=0, acked_at=5 * MIN)  # the alert went out 5 min after carting
    assert evaluate(hold, LIMITS, 31 * MIN).release is None
    assert evaluate(hold, LIMITS, 35 * MIN).release is Release.UNCLAIMED


def test_one_reminder_while_unclaimed() -> None:
    hold = HoldState(ready_at=0, acked_at=0)
    assert evaluate(hold, LIMITS, 14 * MIN).notice is None
    assert evaluate(hold, LIMITS, 15 * MIN).notice is Notice.REMIND_UNCLAIMED
    assert evaluate(hold, LIMITS, 16 * MIN).notice is None


def test_a_checkout_session_ends_after_the_session_time() -> None:
    hold = HoldState(ready_at=0, acked_at=0)
    hold.viewer_joined(10 * MIN)
    verdict = evaluate(hold, LIMITS, 20 * MIN)
    assert verdict.release is None
    assert verdict.unclaimed_deadline is None  # claimed
    assert verdict.session_deadline == 40 * MIN
    assert evaluate(hold, LIMITS, 34 * MIN).notice is None
    assert evaluate(hold, LIMITS, 35 * MIN).notice is Notice.SESSION_ENDING  # once, 5 min before
    assert evaluate(hold, LIMITS, 36 * MIN).notice is None
    assert evaluate(hold, LIMITS, 40 * MIN).release is Release.SESSION


def test_payment_pages_stop_the_session_clock_but_not_the_hard_cap() -> None:
    hold = HoldState(ready_at=0, acked_at=0)
    hold.viewer_joined(10 * MIN)
    hold.payment_started(35 * MIN)
    assert evaluate(hold, LIMITS, 45 * MIN).release is None  # past the session time, but paying
    hold.payment_ended(50 * MIN)
    assert evaluate(hold, LIMITS, 50 * MIN).session_deadline == 55 * MIN  # 15 min paused
    assert evaluate(hold, LIMITS, 54 * MIN).release is None
    hold.payment_started(56 * MIN)
    assert evaluate(hold, LIMITS, 60 * MIN).release is Release.HARD_CAP


def test_a_viewer_who_leaves_and_doesnt_return_releases_the_cart() -> None:
    hold = HoldState(ready_at=0, acked_at=0)
    hold.viewer_joined(5 * MIN)
    hold.viewer_left(8 * MIN)
    assert evaluate(hold, LIMITS, 17 * MIN).release is None
    hold.viewer_joined(17 * MIN)  # came back in time
    hold.viewer_left(18 * MIN)
    assert evaluate(hold, LIMITS, 27 * MIN).release is None
    assert evaluate(hold, LIMITS, 28 * MIN).release is Release.VIEWER_GONE  # before the session ends at 35


def test_a_bank_app_switch_mid_payment_doesnt_count_as_leaving() -> None:
    hold = HoldState(ready_at=0, acked_at=0)
    hold.viewer_joined(5 * MIN)
    hold.payment_started(10 * MIN)
    hold.viewer_left(11 * MIN)  # off to the bank's app
    assert evaluate(hold, LIMITS, 25 * MIN).release is None  # 14 min away, but paying
    hold.payment_ended(25 * MIN)
    assert evaluate(hold, LIMITS, 34 * MIN).release is None  # the gone clock restarted at 25
    assert evaluate(hold, LIMITS, 35 * MIN).release is Release.VIEWER_GONE


def test_the_hard_cap_wins_whatever_else_is_happening() -> None:
    hold = HoldState(ready_at=0, acked_at=40 * MIN)
    assert evaluate(hold, LIMITS, 60 * MIN).release is Release.HARD_CAP
    assert evaluate(hold, LIMITS, 60 * MIN).hard_deadline == 60 * MIN


def test_limits_cant_exceed_the_compiled_maxima() -> None:
    huge = HoldLimits(unclaimed_s=10**9, session_s=10**9, viewer_gone_s=10**9, hard_s=10**9).clamped()
    assert (huge.unclaimed_s, huge.session_s, huge.viewer_gone_s, huge.hard_s) == (
        MAX_UNCLAIMED_S,
        MAX_SESSION_S,
        MAX_VIEWER_GONE_S,
        MAX_HARD_S,
    )
    forever = HoldLimits(hard_s=0).clamped()
    assert forever.hard_s == 1  # "0 = hold for ever" doesn't exist
    hold = HoldState(ready_at=0, acked_at=0)
    hold.viewer_joined(1)
    assert evaluate(hold, HoldLimits(hard_s=10**9), MAX_HARD_S).release is Release.HARD_CAP
