"""The agent's watch loop: one trip, one browser window, at most one held cart.

Today's watcher cut down to the one trip a pod is started for (docs/multi-tenant/design-reference.md
§5.2, §5.4), plus what a pod needs that a shared process didn't:

- Checks follow the operator's interval (the poll curve), never more often than every 30 s, and slow
  to every 5 minutes if the operator has gone quiet. A local token bucket stops bursts.
- Before carting, the agent asks the operator for a cart claim, so a user holds one cart at a time.
- A held cart lives under four clocks (holds.py). The gate opens the checkout only while they run.
- A hold someone viewed ends the pod: the cart is emptied (unless a payment is under way) and the
  agent exits, so nothing a viewer did carries into the next session. A pod nobody viewed resumes
  watching after the hold, unless it is a short check.
- A short-check pod checks once and exits, unless it carts; then it stays for that hold.

`run()` returns how the pod should say goodbye (the `bye` message and its exit).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Protocol, cast

from ferry_watch.bcferries.errors import BookingError, SessionExpired, describe
from ferry_watch.bcferries.urls import CheckoutView
from ferry_watch.holds import HoldLimits, HoldState, Notice, evaluate, peek, release_due
from ferry_watch.models import PACIFIC, Fare, FarePreference, Sailing, Trip, format_time
from ferry_watch.polling import time_until
from ferry_watch.protocol import (
    MIN_INTERVAL_S,
    AlertKind,
    AlertParams,
    ByeReason,
    CartInfo,
    ErrorClass,
    ErrorInfo,
    EventCode,
    EventParams,
    FareInfo,
    Mode,
    Navigations,
    Outcome,
    Phase,
    ReleaseCheck,
    SailingInfo,
    SailingStatus,
    State,
    ViewerInfo,
    error_class,
)
from ferry_watch.protocol import CartStatus as WireCartStatus
from ferry_watch.protocol import FarePreference as WireFarePreference
from ferry_watch.state import Cart, CartStatus

from .store import OneTripStore

log = logging.getLogger(__name__)

# Compiled limits: whatever the operator says (§5.4).
MAX_CART_ATTEMPTS = 6  # per pod
NO_CART_BEFORE_DEADLINE_S = 100 * 60  # a hold must be able to finish before the pod's deadline
RECYCLE_BEFORE_DEADLINE_S = 120 * 60
RECYCLE_JITTER_S = 144 * 60  # recycle at roughly 22-24 h, spread so pods don't all go at once
RECYCLE_AFTER_FAILURES = 6  # a fresh pod rather than a seventh failure in a row
RESET_SESSION_AFTER = 3  # failures in a row before a fresh browser context
FAILURE_ALERT_AFTER = 3
FAIL_SLOW_AFTER_S = 10 * 60  # the operator has been unreachable this long ...
FAIL_SLOW_INTERVAL_S = 300  # ... so check at most this often
CHECK_NOW_MIN_GAP_S = 15
BUCKET_SIZE = 2  # checks that may run back to back
BUCKET_REFILL_S = MIN_INTERVAL_S  # one more every 30 s

JITTER = 0.15
HOLD_POLL_SECONDS = 5
IDLE_WAKE_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 15 * 60  # longer than BC Ferries' queue ever takes
CART_BUDGET_SECONDS = 40
EMPTY_CART_SECONDS = 45  # emptying a cart, Queue-it included, before giving up (the drain budget)
ERROR_BACKOFF_SECONDS = (0, 60, 120, 300, 600)

MAX_PRICE_LENGTH = 24
_PRICE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$.,- ")


_NOTICE_ALERTS: dict[Notice, AlertKind] = {
    Notice.REMIND_UNCLAIMED: "hold_reminder",
    Notice.SESSION_ENDING: "hold_ending",
}


class Page(Protocol):
    """One browser window on bcferries.com (SiteSession in the pod, a fake in tests)."""

    @property
    def closed(self) -> bool: ...
    @property
    def lost(self) -> bool: ...
    async def search(self, trip: Trip) -> list[Sailing]: ...
    async def refresh(self) -> list[Sailing]: ...
    async def add_to_cart(self, depart: clock_time, preference: FarePreference) -> Fare: ...
    async def empty_cart(self) -> ReleaseCheck: ...
    def checkout_view(self) -> CheckoutView: ...
    async def save_debug_snapshot(self, reason: str) -> Path | None: ...
    async def close(self) -> None: ...


class Browser(Protocol):
    async def new_session(self, name: str) -> Page: ...


class Link(Protocol):
    """The operator, as the watcher sees it (HubClient in the pod)."""

    def event(self, code: EventCode, params: EventParams | None = None) -> None: ...
    def alert(self, kind: AlertKind, params: AlertParams | None = None) -> int: ...
    def acked(self, seq: int) -> bool: ...
    async def claim(self, depart: str, fare: WireFarePreference) -> bool: ...
    def silent_for(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Finish:
    """Why the agent is stopping, for its `bye`."""

    reason: ByeReason
    outcome: Outcome = "none"
    cart_emptied: bool = False
    release_check: ReleaseCheck = "none"
    viewer_was_connected: bool = False


@dataclass(slots=True)
class _Navs:
    refresh: int = 0
    search: int = 0
    cart: int = 0
    empty: int = 0


@dataclass(slots=True)
class _Viewer:
    connected_since: float | None = None
    seconds: float = 0.0
    first_at: float | None = None


@dataclass(slots=True)
class _Bucket:
    tokens: float = BUCKET_SIZE
    at: float = field(default_factory=time.time)

    def level(self, now: float) -> float:
        return min(BUCKET_SIZE, self.tokens + (now - self.at) / BUCKET_REFILL_S)

    def take(self, now: float) -> float:
        """Take a token; returns 0, or how long until one is available (taking nothing)."""
        level = self.level(now)
        if level < 1:
            return (1 - level) * BUCKET_REFILL_S
        self.tokens, self.at = level - 1, now
        return 0.0


class TripWatcher:
    def __init__(
        self,
        store: OneTripStore,
        browser: Browser,
        link: Link,
        *,
        deadline_at: float,
        lockdown_ok: Callable[[], bool],
        close_viewer: Callable[[str], Awaitable[None]],
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self._browser = browser
        self._link = link
        self._deadline_at = deadline_at
        self._lockdown_ok = lockdown_ok
        self._close_viewer = close_viewer
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._recycle_at = deadline_at - RECYCLE_BEFORE_DEADLINE_S - self._rng.uniform(0, RECYCLE_JITTER_S)
        self._wake = asyncio.Event()
        store.subscribe(self._wake.set)

        self._page: Page | None = None
        self._search_key: tuple[object, ...] | None = None
        self._needs_search = True
        self._check_task: asyncio.Task[None] | None = None
        self._checks_done = 0
        self._check_started_at: float | None = None
        self._bucket = _Bucket(at=clock())
        self._navs = _Navs()

        self.sailings: list[Sailing] = []
        self.checked_at: float | None = None
        self.next_due: float | None = None  # None: as soon as possible
        self.retry_at: float | None = None
        self.checking = False
        self.error: tuple[ErrorClass, str] | None = None
        self.consecutive_errors = 0
        self.alerted: set[clock_time] = set()  # alerted and carted, or viewed: never carted again
        self.offered: set[clock_time] = set()  # alerted while carting was held back: may be carted once
        self._seeded_key: tuple[object, ...] | None = None

        self.cart: Cart | None = None
        self.hold: HoldState | None = None
        self._cart_key: tuple[object, ...] | None = None
        self._held_alert: int | None = None  # seq of the cart_held alert: its ack starts the unclaimed clock
        self._attempts = 0
        self._release_requested = False
        self._sign_in_reported = False
        self._viewer = _Viewer()
        self._viewer_ever = False
        self._drain_reason: ByeReason | None = None

    # ---- what the rest of the agent reads

    @property
    def mode(self) -> Mode:
        latest = self._store.latest
        return latest.mode if latest is not None else "warm"

    @property
    def holding(self) -> bool:
        return self.cart is not None and self.cart.status is CartStatus.READY

    @property
    def phase(self) -> Phase:
        if self._drain_reason is not None:
            return "draining"
        if self.holding:
            return "holding"
        if self.checked_at is None and self.error is None:
            return "starting"
        return "watching"

    def checkout_cart(self) -> str | None:
        """The cart a checkout viewer may open now, or None (the gate's step 2)."""
        latest = self._store.latest
        if self._drain_reason is not None or not self._lockdown_ok() or latest is None or not latest.vnc_allowed:
            return None
        if not self.holding or self.cart is None or self.hold is None:
            return None
        if release_due(self.hold, self._limits(), self._clock()) is not None:
            return None
        return self.cart.id

    def viewer_changed(self, connected: bool) -> None:
        """The gate: a checkout viewer connected or left."""
        now = self._clock()
        viewer = self._viewer
        if connected:
            viewer.connected_since = now
            viewer.first_at = viewer.first_at or now
            self._viewer_ever = True
            if self.hold is not None:
                self.hold.viewer_joined(now)
            self._link.event("viewer_connected")
        else:
            if viewer.connected_since is not None:
                viewer.seconds += now - viewer.connected_since
                viewer.connected_since = None
            if self.hold is not None:
                self.hold.viewer_left(now)
            self._link.event("viewer_disconnected")
        self._wake.set()

    def state(self, envelope: dict[str, Any]) -> State:
        """Everything the dashboard shows, as a `state` message (page-derived text trimmed to fit).

        Page loads are counted since `navs_sent()` was last called.
        """
        error = ErrorInfo(kind=self.error[0], text=self.error[1][:300]) if self.error else None
        navs = self._navs
        viewer = self._viewer
        seconds = viewer.seconds + (self._clock() - viewer.connected_since if viewer.connected_since else 0.0)
        return State(
            **envelope,
            phase=self.phase,
            checking=self.checking,
            checked_at=self.checked_at,
            next_due=self.next_due,
            retry_at=self.retry_at,
            effective_interval=self.effective_interval(),
            sailings=[_sailing_info(s) for s in self.sailings[:60]],
            error=error,
            consecutive_errors=self.consecutive_errors,
            cart=self._cart_info(),
            viewer=ViewerInfo(
                connected=viewer.connected_since is not None,
                first_at=viewer.first_at,
                seconds=int(seconds),
            ),
            alerted=sorted(_hhmm(d) for d in self.alerted)[:100],
            offered=sorted(_hhmm(d) for d in self.offered)[:100],
            navs=Navigations(refresh=navs.refresh, search=navs.search, cart=navs.cart, empty=navs.empty),
            lockdown_ok=self._lockdown_ok(),
            bucket=round(self._bucket.level(self._clock()), 2),
        )

    def navs_sent(self) -> None:
        """The page-load counts went out in a state message; count afresh."""
        self._navs = _Navs()

    def effective_interval(self) -> int:
        latest = self._store.latest
        interval = latest.interval_s * latest.brake.multiplier if latest is not None else FAIL_SLOW_INTERVAL_S
        if self._link.silent_for() >= FAIL_SLOW_AFTER_S:
            interval = max(interval, FAIL_SLOW_INTERVAL_S)
        return max(MIN_INTERVAL_S, round(interval))

    # ---- what the operator asks for

    def check_now(self) -> None:
        started = self._check_started_at
        if self.holding or self.checking or (started is not None and self._clock() - started < CHECK_NOW_MIN_GAP_S):
            return
        self.next_due = self.retry_at = None
        self._wake.set()

    def release(self, cart_id: str) -> bool:
        if not self.holding or self.cart is None or self.cart.id != cart_id:
            return False
        self._release_requested = True
        self._wake.set()
        return True

    def dismiss(self, cart_id: str) -> bool:
        if self.cart is None or self.cart.status is not CartStatus.FAILED or self.cart.id != cart_id:
            return False
        self.cart = None
        return True

    def drain(self, reason: ByeReason = "drained") -> None:
        """Stop: SIGTERM, the operator's drain, or the deadline. `run()` empties any cart and returns."""
        if self._drain_reason is None:
            self._drain_reason = reason
        if self._check_task is not None:
            self._check_task.cancel()
        self._wake.set()

    async def revoke_viewer(self, reason: str) -> None:
        await self._close_viewer(reason)

    # ---- the loop

    async def run(self) -> Finish:
        try:
            while True:
                finish = await self.step()
                if finish is not None:
                    return finish
                await self._sleep()
        finally:
            await self._close_session()

    async def step(self) -> Finish | None:
        """One turn of the loop: drain, look after the hold, or check if due. `run()` repeats this."""
        self._seed_memory()
        if self._drain_reason is not None:
            return await self._drain()
        if self.holding:
            return await self._service_hold()
        if self.mode == "short_check" and self._checks_done:
            return Finish("short_check_done")
        if self.consecutive_errors >= RECYCLE_AFTER_FAILURES:
            return Finish("recycle")
        if self._clock() >= self._recycle_at:
            return Finish("recycle")
        if self._due():
            await self._run_check()
        return None

    async def _sleep(self) -> None:
        now = self._clock()
        if self.holding or self._drain_reason is not None:
            timeout = float(HOLD_POLL_SECONDS)
        else:
            due = max(self.next_due or now, self.retry_at or now)
            timeout = min(due - now, IDLE_WAKE_SECONDS)
        try:
            await asyncio.wait_for(self._wake.wait(), max(timeout, 0.05))
        except TimeoutError:
            return
        self._wake.clear()

    def _trip(self) -> Trip | None:
        config = self._store.config
        return config.trips[0] if config.trips else None

    def _due(self) -> bool:
        config, latest, trip = self._store.config, self._store.latest, self._trip()
        if trip is None or latest is None or not config.enabled or not trip.enabled:
            return False
        if latest.brake.paused or latest.breaker == "open":
            return False
        now = self._clock()
        if time_until(trip, datetime.fromtimestamp(now, PACIFIC)) is None:
            return False  # its last departure has gone
        if (self.next_due or now) > now or (self.retry_at or now) > now:
            return False
        wait = self._bucket.take(now)
        if wait:
            self.next_due = now + wait
            return False
        return True

    def _seed_memory(self) -> None:
        """Take the operator's record of alerted departures when the pod starts, or the search changes."""
        trip, latest = self._trip(), self._store.latest
        if trip is None or latest is None or self._seeded_key == trip.search_key():
            return
        if self._seeded_key is not None:  # the trip was edited: old results no longer apply
            self.sailings, self.checked_at, self.error, self.consecutive_errors = [], None, None, 0
            self.next_due = self.retry_at = None
        self._seeded_key = trip.search_key()
        self.alerted = {clock_time.fromisoformat(t) for t in latest.alerted}
        self.offered = {clock_time.fromisoformat(t) for t in latest.offered}

    def _limits(self) -> HoldLimits:
        latest = self._store.latest
        if latest is None:
            return HoldLimits()
        hold = latest.hold
        return HoldLimits(hold.unclaimed_s, hold.session_s, hold.viewer_gone_s, hold.hard_s)

    # ---- checking

    async def _run_check(self) -> None:
        trip = self._trip()
        if trip is None:
            return
        self._check_started_at = self._clock()
        self._check_task = asyncio.create_task(self._timed_check(trip), name=f"check {trip.id}")
        try:
            await asyncio.wait([self._check_task])
        finally:
            self._check_task = None
            self._checks_done += 1
            interval = self.effective_interval() * self._rng.uniform(1 - JITTER, 1 + JITTER)
            self.next_due = self._clock() + max(MIN_INTERVAL_S, interval)

    async def _timed_check(self, trip: Trip) -> None:
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                await self._check(trip)
        except TimeoutError:
            await self._failed(BookingError(f"The check was stuck for {CHECK_TIMEOUT_SECONDS // 60} minutes"))

    async def _check(self, trip: Trip) -> None:
        self.checking = True
        try:
            page, sailings = await self._results(trip)
        except Exception as e:
            if not isinstance(e, BookingError):
                log.exception("Unexpected error while checking")
            await self._failed(e)
            return
        finally:
            self.checking = False
        await self._process(trip, page, sailings)

    async def _results(self, trip: Trip) -> tuple[Page, list[Sailing]]:
        """Fresh results: reload the open search, or walk the booking form again."""
        if self._page is not None and (self._search_key != trip.search_key() or self._page.closed):
            await self._close_session()
        if self._page is None:
            self._page = await self._browser.new_session(trip.id)
            self._search_key = trip.search_key()
            self._needs_search = True
            self._link.event("session_opened")
        page = self._page
        if not self._needs_search:
            try:
                self._navs.refresh += 1
                return page, await page.refresh()
            except SessionExpired:
                log.info("Search session expired, searching again")
        self._navs.search += 1
        sailings = await page.search(trip)
        self._needs_search = False
        return page, sailings

    async def _failed(self, error: Exception) -> None:
        self.consecutive_errors += 1
        kind = error_class(error)
        self.error = (kind, describe(error))
        wait = ERROR_BACKOFF_SECONDS[min(self.consecutive_errors, len(ERROR_BACKOFF_SECONDS)) - 1]
        self.retry_at = self._clock() + wait if wait else None
        page = self._page
        if page is not None:
            if kind == "markup_changed" or not isinstance(error, BookingError):
                await page.save_debug_snapshot(type(error).__name__)
            if page.closed or self.consecutive_errors >= RESET_SESSION_AFTER:
                await self._close_session()
            else:
                self._needs_search = True
        log.warning("Check failed (%d in a row): %s", self.consecutive_errors, self.error[1])
        self._link.event("check_failed", EventParams(kind=kind))
        if self.consecutive_errors == FAILURE_ALERT_AFTER:
            self._link.alert("failing", AlertParams(error_class=kind))

    async def _process(self, trip: Trip, page: Page, sailings: list[Sailing]) -> None:
        current = self._trip()
        if current is None or current.search_key() != trip.search_key():
            return  # edited while we were on the page
        trip = current
        before = {s.depart for s in self.sailings if s.available}
        now_open = {s.depart for s in sailings if s.available}
        self.sailings = sailings
        self.checked_at = self._clock()
        self.error, self.consecutive_errors, self.retry_at = None, 0, None
        # Sold out again: forget them, so they alert again if they reopen.
        self.alerted &= now_open
        self.offered &= now_open
        for depart in sorted(before - now_open):
            self._link.event("sold_out_again", EventParams(depart=_hhmm(depart)))

        open_in_window = [s for s in sailings if s.available and trip.in_window(s.depart)]
        candidates = [s for s in open_in_window if s.depart not in self.alerted]
        fresh = [s for s in candidates if s.depart not in self.offered]  # never alerted at all
        log.info("%d sailings, %d bookable in window", len(sailings), len(open_in_window))
        if not candidates:
            return

        target = candidates[0]
        fare: Fare | None = None
        tried = False
        if self._may_cart(trip):
            if await self._link.claim(_hhmm(target.depart), cast(WireFarePreference, trip.fare.value)):
                tried = True
                fare = await self._add_to_cart(trip, page, target)
            else:
                log.info("The operator refused a cart claim for %s", format_time(target.depart))

        if fare is not None:
            self.alerted.add(target.depart)
            self.offered.discard(target.depart)
            others = [s for s in fresh if s.depart != target.depart]
            self.offered |= {s.depart for s in others}
            departures = [_hhmm(s.depart) for s in [target, *others]][:10]
            params = AlertParams(departures=departures, fare=fare.name[:32], price=_price(fare.price))
            self._held_alert = self._link.alert("cart_held", params)
            self._link.event("cart_added", EventParams(depart=departures[0], fare=fare.name[:32]))
        elif fresh:
            self.offered |= {s.depart for s in fresh}
            kind: AlertKind = "cart_failed" if tried else "bookable"
            self._link.alert(kind, AlertParams(departures=[_hhmm(s.depart) for s in fresh][:10]))

    def _may_cart(self, trip: Trip) -> bool:
        return (
            trip.auto_cart  # the user's choice, already ANDed with the operator's (OneTripStore)
            and self._drain_reason is None
            and not self._viewer_ever
            and self._lockdown_ok()
            and self._attempts < MAX_CART_ATTEMPTS
            and self._clock() < self._deadline_at - NO_CART_BEFORE_DEADLINE_S
        )

    async def _add_to_cart(self, trip: Trip, page: Page, sailing: Sailing) -> Fare | None:
        self._attempts += 1
        self.cart = Cart(CartStatus.ADDING, sailing.depart)
        self._release_requested = False
        try:
            async with asyncio.timeout(CART_BUDGET_SECONDS):
                self._navs.cart += 1
                fare = await page.add_to_cart(sailing.depart, trip.fare)
        except Exception as e:
            if isinstance(e, TimeoutError):
                e = BookingError(f"Timed out after {CART_BUDGET_SECONDS}s selecting a fare")
            elif not isinstance(e, BookingError):
                log.exception("Unexpected error adding to the cart")
            await page.save_debug_snapshot("cart failed")
            await self._close_session()  # the page is in an unknown state; search again next time
            self.cart = Cart(CartStatus.FAILED, sailing.depart, error=describe(e))
            return None
        now = self._clock()
        self.cart = Cart(CartStatus.READY, sailing.depart, fare=fare, since=now)
        self.hold = HoldState(ready_at=now)
        self._cart_key = trip.search_key()
        self._sign_in_reported = False
        log.info("Holding %s (%s)", format_time(sailing.depart), fare.name)
        return fare

    # ---- a held cart

    async def _service_hold(self) -> Finish | None:
        cart, hold, page = self.cart, self.hold, self._page
        if cart is None or hold is None:
            return None
        if page is None or page.closed:
            return await self._lost()
        now = self._clock()
        depart = _hhmm(cart.depart)
        if self._follow_checkout(page.checkout_view(), hold, depart, now):
            return await self._end_hold("booked", empty=False)

        if hold.acked_at is None and self._held_alert is not None and self._link.acked(self._held_alert):
            hold.acked_at = now
        trip = self._trip()
        if self._release_requested or trip is None or trip.search_key() != self._cart_key:
            return await self._end_hold("released", empty=True)
        latest = self._store.latest
        if self._viewer.connected_since is not None and (latest is None or not latest.vnc_allowed):
            await self._close_viewer("checkout access withdrawn")

        verdict = evaluate(hold, self._limits(), now)
        if verdict.notice is not None:
            self._link.alert(_NOTICE_ALERTS[verdict.notice], AlertParams(departures=[depart]))
        if verdict.release is None:
            return None
        log.info("Hold on %s ended: %s", depart, verdict.release)
        if hold.payment_since is not None:
            # Only the hard cap ends a hold mid-payment. Pulling the cart now could charge for nothing.
            outcome: Outcome = "payment_left_to_site"
        else:
            outcome = "expired"
            minutes = round((now - hold.ready_at) / 60)
            self._link.alert("cart_expired", AlertParams(departures=[depart], minutes=minutes))
        return await self._end_hold(outcome, empty=outcome == "expired")

    def _follow_checkout(self, view: CheckoutView, hold: HoldState, depart: str, now: float) -> bool:
        """Track the payment guard and report what the checkout shows. True once it's booked."""
        if view.payment:
            hold.payment_started(now)
        else:
            hold.payment_ended(now)
        if view.sign_in and not self._sign_in_reported:
            self._sign_in_reported = True
            self._link.event("checkout_login_seen", EventParams(forced=False))
        if view.booked:
            self._link.event("booking_confirmed", EventParams(depart=depart))
        return view.booked

    async def _end_hold(self, outcome: Outcome, *, empty: bool) -> Finish | None:
        cart, hold = self.cart, self.hold
        if cart is None or hold is None:
            return None
        await self._close_viewer(f"hold ended ({outcome})")
        emptied, check = False, cast(ReleaseCheck, "none")
        if empty:
            emptied, check = await self._empty(cart)
        viewed = hold.first_viewer_at is not None
        self.cart, self.hold, self._held_alert, self._release_requested = None, None, None, False
        if viewed or outcome == "booked" or self.mode == "short_check":
            return Finish("hold_ended", outcome, emptied, check, viewer_was_connected=viewed)
        # Nobody looked: a warm pod carries on watching, from a fresh search.
        await self._close_session()
        self.next_due = None
        return None

    async def _empty(self, cart: Cart) -> tuple[bool, ReleaseCheck]:
        page = self._page
        depart = _hhmm(cart.depart)
        if page is None or page.closed:
            return False, "none"
        try:
            async with asyncio.timeout(EMPTY_CART_SECONDS):
                self._navs.empty += 1
                check = await page.empty_cart()
        except (BookingError, TimeoutError) as e:
            log.warning("Couldn't empty the cart; BC Ferries' own timer will release it: %s", describe(e))
            self._link.alert("release_unverified", AlertParams(departures=[depart]))
            return False, "none"
        if check == "none":
            self._link.alert("release_unverified", AlertParams(departures=[depart]))
        self._link.event("cart_released", EventParams(depart=depart))
        return True, check

    async def _lost(self) -> Finish:
        """The browser went away with the cart in it."""
        cart, hold = self.cart, self.hold
        viewed = hold is not None and hold.first_viewer_at is not None
        if cart is None:
            return Finish("fatal", viewer_was_connected=viewed)
        self._link.alert("cart_lost", AlertParams(departures=[_hhmm(cart.depart)]))
        if not viewed:
            self.alerted.discard(cart.depart)  # a fresh pod may cart it again; one someone viewed, never
        self.cart, self.hold = None, None
        await self._close_viewer("browser lost")
        return Finish("fatal", viewer_was_connected=viewed)

    # ---- stopping

    async def _drain(self) -> Finish:
        reason = self._drain_reason or "drained"
        await self._close_viewer("draining")
        cart, hold = self.cart, self.hold
        viewed = hold is not None and hold.first_viewer_at is not None
        if cart is None or cart.status is CartStatus.FAILED:
            return Finish(reason, viewer_was_connected=viewed)
        if hold is not None and hold.payment_since is not None:
            return Finish(reason, "payment_left_to_site", viewer_was_connected=viewed)
        emptied, check = await self._empty(cart)  # an ADDING cart may be in BC Ferries' cart too
        self.cart, self.hold = None, None
        return Finish(reason, "released" if emptied else "none", emptied, check, viewer_was_connected=viewed)

    async def _close_session(self) -> None:
        page, self._page = self._page, None
        self._search_key = None
        if page is not None:
            with contextlib.suppress(Exception):
                await page.close()

    def _cart_info(self) -> CartInfo | None:
        cart = self.cart
        if cart is None:
            return None
        verdict = peek(self.hold, self._limits(), self._clock()) if self.hold is not None else None
        return CartInfo(
            id=cart.id,
            status=cast(WireCartStatus, cart.status.value),
            depart=_hhmm(cart.depart),
            fare=cart.fare.name[:32] if cart.fare else None,
            price=_price(cart.fare.price) if cart.fare else None,
            ready_at=cart.since if cart.status is CartStatus.READY else None,
            unclaimed_deadline=verdict.unclaimed_deadline if verdict else None,
            session_deadline=verdict.session_deadline if verdict else None,
            hard_deadline=verdict.hard_deadline if verdict else None,
            payment_in_progress=self.hold is not None and self.hold.payment_since is not None,
        )


def _hhmm(t: clock_time) -> str:
    """A departure as the protocol writes it: 24-hour HH:MM."""
    return t.strftime("%H:%M")


def _price(text: str | None) -> str | None:
    """A price as the page showed it, if it fits the protocol's pattern (else it's left out)."""
    if text is None or len(text) > MAX_PRICE_LENGTH or not set(text) <= _PRICE_CHARS:
        return None
    return text


def _sailing_info(sailing: Sailing) -> SailingInfo:
    return SailingInfo(
        depart=_hhmm(sailing.depart),
        arrive=_hhmm(sailing.arrive),
        vessel=sailing.vessel[:64],
        status=cast(SailingStatus, sailing.status.value),
        fares=[FareInfo(name=f.name[:32], price=_price(f.price), selectable=f.selectable) for f in sailing.fares[:6]],
    )
