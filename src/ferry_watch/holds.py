"""When a held cart must be handed back (docs/multi-tenant/design-reference.md §5.4, §8.2).

Four clocks run while a cart is held, and the first to run out releases it:
- unclaimed: nobody has opened the checkout since the user was told (from the operator's ack);
- session: a checkout has been open this long (from the first viewer);
- viewer gone: the viewer left and hasn't come back;
- hard cap: the cart has been held this long, whatever is happening (from READY).

While the checkout is on a payment or 3-D Secure page, the session and viewer-gone clocks stop, so a
bank-app switch doesn't lose a booking mid-payment. The hard cap never stops. Each limit is capped by a
compiled maximum, so no configuration can hold a cart for ever.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .protocol import MAX_HARD_S, MAX_SESSION_S, MAX_UNCLAIMED_S, MAX_VIEWER_GONE_S

REMIND_UNCLAIMED_AFTER_S = 15 * 60  # "your cart is waiting" once, if nobody has opened it
WARN_BEFORE_SESSION_END_S = 5 * 60  # "checkout closes soon" once


@dataclass(frozen=True, slots=True)
class HoldLimits:
    unclaimed_s: int = 30 * 60
    session_s: int = 30 * 60
    viewer_gone_s: int = 10 * 60
    hard_s: int = 60 * 60

    def clamped(self) -> HoldLimits:
        """These limits within the compiled maxima (and at least a second each)."""
        return HoldLimits(
            unclaimed_s=_clamp(self.unclaimed_s, MAX_UNCLAIMED_S),
            session_s=_clamp(self.session_s, MAX_SESSION_S),
            viewer_gone_s=_clamp(self.viewer_gone_s, MAX_VIEWER_GONE_S),
            hard_s=_clamp(self.hard_s, MAX_HARD_S),
        )


def _clamp(value: int, maximum: int) -> int:
    return min(max(value, 1), maximum)


@dataclass(slots=True)
class HoldState:
    """What has happened to one held cart. Times are epoch seconds."""

    ready_at: float  # the cart became READY
    acked_at: float | None = None  # the operator acknowledged the cart alert (the user was told)
    first_viewer_at: float | None = None
    viewer_connected: bool = False
    last_disconnect_at: float | None = None
    payment_since: float | None = None  # on a payment/3DS page since then
    payment_paused_s: float = 0.0  # time spent on payment pages before the current visit
    reminded: bool = False
    warned: bool = False

    def payment_started(self, now: float) -> None:
        if self.payment_since is None:
            self.payment_since = now

    def payment_ended(self, now: float) -> None:
        if self.payment_since is None:
            return
        self.payment_paused_s += now - self.payment_since
        if not self.viewer_connected and self.last_disconnect_at is not None:
            # The viewer-gone clock stood still too (e.g. the user was in their bank's app).
            self.last_disconnect_at += now - max(self.payment_since, self.last_disconnect_at)
        self.payment_since = None

    def viewer_joined(self, now: float) -> None:
        if self.first_viewer_at is None:
            self.first_viewer_at = now
        self.viewer_connected = True

    def viewer_left(self, now: float) -> None:
        self.viewer_connected = False
        self.last_disconnect_at = now

    def paused_s(self, now: float) -> float:
        """Time the session and viewer-gone clocks have stood still for payment pages."""
        current = now - self.payment_since if self.payment_since is not None else 0.0
        return self.payment_paused_s + current


class Release(enum.StrEnum):
    UNCLAIMED = "unclaimed"
    SESSION = "session"
    VIEWER_GONE = "viewer_gone"
    HARD_CAP = "hard_cap"


class Notice(enum.StrEnum):
    REMIND_UNCLAIMED = "hold_reminder"  # protocol alert kinds
    SESSION_ENDING = "hold_ending"


@dataclass(frozen=True, slots=True)
class Verdict:
    release: Release | None  # hand the cart back now, and why
    notice: Notice | None  # tell the user something (once each)
    unclaimed_deadline: float | None  # for the dashboard's countdowns
    session_deadline: float | None
    hard_deadline: float


def release_due(hold: HoldState, limits: HoldLimits, now: float) -> Release | None:
    """Whether a clock has run out at `now`, and which. Changes nothing (the gate asks this too)."""
    return peek(hold, limits, now).release


def peek(hold: HoldState, limits: HoldLimits, now: float) -> Verdict:
    """Where this hold stands at `now`, without giving any notice (for status reports)."""
    release, unclaimed_deadline, session_deadline, hard_deadline = _check(hold, limits.clamped(), now)
    return Verdict(release, None, unclaimed_deadline, session_deadline, hard_deadline)


def _check(hold: HoldState, limits: HoldLimits, now: float) -> tuple[Release | None, float | None, float | None, float]:
    hard_deadline = hold.ready_at + limits.hard_s
    unclaimed_deadline = None
    if hold.first_viewer_at is None:
        started = hold.acked_at if hold.acked_at is not None else hold.ready_at
        unclaimed_deadline = started + limits.unclaimed_s
    session_deadline = None
    if hold.first_viewer_at is not None:
        session_deadline = hold.first_viewer_at + limits.session_s + hold.paused_s(now)  # payment pages don't count

    release: Release | None = None
    if now >= hard_deadline:
        release = Release.HARD_CAP
    elif unclaimed_deadline is not None and now >= unclaimed_deadline:
        release = Release.UNCLAIMED
    elif session_deadline is not None and now >= session_deadline:
        release = Release.SESSION
    elif (
        not hold.viewer_connected
        and hold.last_disconnect_at is not None
        and hold.payment_since is None
        and now - hold.last_disconnect_at >= limits.viewer_gone_s
    ):
        release = Release.VIEWER_GONE
    return release, unclaimed_deadline, session_deadline, hard_deadline


def evaluate(hold: HoldState, limits: HoldLimits, now: float) -> Verdict:
    """Where this hold stands at `now`. Marks a notice as given, so each is returned once."""
    limits = limits.clamped()
    release, unclaimed_deadline, session_deadline, hard_deadline = _check(hold, limits, now)

    notice: Notice | None = None
    if release is None:
        if (
            unclaimed_deadline is not None
            and not hold.reminded
            and now - (unclaimed_deadline - limits.unclaimed_s) >= REMIND_UNCLAIMED_AFTER_S
        ):
            hold.reminded = True
            notice = Notice.REMIND_UNCLAIMED
        elif (
            session_deadline is not None
            and not hold.warned
            and min(session_deadline, hard_deadline) - now <= WARN_BEFORE_SESSION_END_S
        ):
            hold.warned = True
            notice = Notice.SESSION_ENDING

    return Verdict(release, notice, unclaimed_deadline, session_deadline, hard_deadline)
