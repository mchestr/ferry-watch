"""The watch loop: keep a BC Ferries session open per trip, re-check it, alert and fill the cart.

Runs as a task on the same event loop as the web server, so it shares state with the HTTP
handlers without locks. Each trip's session walks the booking form once and is then re-checked by
reloading its results page (~3s). When a sailing in the trip's window opens, the same page selects
a fare (it lands in that session's cart) and an alert goes out. A trip holding a cart isn't checked
until the user releases the cart or closes the checkout window; the cart survives pausing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass
from datetime import time as clock_time
from pathlib import Path
from typing import Protocol

from .bcferries.errors import BookingError, LoginFailed, MarkupChanged, SessionExpired, describe
from .bcferries.site import Credentials, search_url
from .bcferries.terminals import Terminals
from .models import Fare, FarePreference, Sailing, Trip, format_time, today
from .notify import (
    Alerts,
    bookable_message,
    cart_lost_message,
    cart_message,
    failing_message,
    login_failed_message,
    summarize,
    trip_title,
)
from .state import AlertMemory, Cart, CartStatus, EventKind, RuntimeState, TripRuntime
from .store import ConfigStore

log = logging.getLogger(__name__)

JITTER = 0.15  # spread checks so they don't land on an exact beat
CART_POLL_SECONDS = 5  # how often a held cart is checked for release or a closed window
CART_BUDGET_SECONDS = 40  # give up selecting a fare after this long and just alert
ERROR_BACKOFF_SECONDS = (0, 60, 120, 300, 600)  # wait before retrying, by consecutive failures
RESET_SESSION_AFTER = 3  # consecutive failures before starting over with a fresh browser context
FAILURE_ALERT_AFTER = 3
LOGIN_BACKOFF_SECONDS = 30 * 60


class Session(Protocol):
    """What the watcher needs from a browser session (bcferries.site.SiteSession, or a fake in tests)."""

    opened_at: float
    logged_in: bool

    @property
    def closed(self) -> bool: ...
    @property
    def lost(self) -> bool: ...
    async def login(self, credentials: Credentials) -> None: ...
    async def search(self, trip: Trip) -> list[Sailing]: ...
    async def refresh(self) -> list[Sailing]: ...
    async def add_to_cart(self, depart: clock_time, preference: FarePreference) -> Fare: ...
    async def empty_cart(self) -> None: ...
    async def show(self) -> None: ...
    async def save_debug_snapshot(self, reason: str) -> Path | None: ...
    async def close(self) -> None: ...


class Browser(Protocol):
    async def new_session(self, name: str) -> Session: ...


class TerminalSource(Protocol):
    """The terminal list, if it's been loaded (bcferries.terminals.TerminalDirectory)."""

    @property
    def cached(self) -> Terminals | None: ...


@dataclass(slots=True)
class _OpenSession:
    session: Session
    search_key: tuple[object, ...]
    needs_search: bool = True  # walk the booking form (again) before reading results
    announced: bool = False  # "Opened a booking session" logged


def _memory_key(trip: Trip) -> str:
    return repr(trip.search_key())


class Watcher:
    def __init__(
        self,
        store: ConfigStore,
        state: RuntimeState,
        browser: Browser,
        alerts: Alerts,
        terminals: TerminalSource,
        *,
        memory: AlertMemory | None,
        credentials: Credentials | None,
        checkout_url: str | None,
    ) -> None:
        self._store = store
        self._state = state
        self._browser = browser
        self._alerts = alerts
        self._terminals = terminals
        self._memory = memory
        self._credentials = credentials
        self._secrets = (credentials.username, credentials.password) if credentials else ()
        self._checkout_url = checkout_url
        self._sessions: dict[str, _OpenSession] = {}
        self._wake = asyncio.Event()
        self._login_blocked_until = 0.0
        store.subscribe(self.wake)

    def _describe(self, error: BaseException) -> str:
        return describe(error, self._secrets)

    # ---- controls (called from HTTP handlers, on the same event loop)

    def wake(self) -> None:
        self._wake.set()

    def check_now(self) -> None:
        for runtime in self._state.trips.values():
            runtime.retry_at = None
        self.wake()

    def release_cart(self, trip_id: str, cart_id: str) -> bool:
        runtime = self._state.trips.get(trip_id)
        if runtime is None or not runtime.holding or runtime.cart is None or runtime.cart.id != cart_id:
            return False
        runtime.release_requested = True
        self.wake()
        return True

    def dismiss_cart(self, trip_id: str, cart_id: str) -> bool:
        runtime = self._state.trips.get(trip_id)
        cart = runtime.cart if runtime else None
        if runtime is None or cart is None or cart.status is not CartStatus.FAILED or cart.id != cart_id:
            return False
        runtime.cart = None
        self._state.changed()
        return True

    # ---- loop

    async def run(self) -> None:
        log.info("Watcher started")
        self.restore_alert_memory()
        try:
            while True:
                try:
                    await self.cycle()
                except Exception:
                    # A bug in one cycle mustn't stop watching; it's logged and the next cycle starts fresh.
                    log.exception("Watch cycle failed")
                await self._sleep()
        finally:
            await self._close_all()

    async def cycle(self) -> None:
        """One pass over every trip: check it, or look after its held cart. `run()` repeats this."""
        config = self._store.config
        trip_ids = [t.id for t in config.trips]

        # Trips removed while their cart was held: hand the spot back.
        for trip_id in set(self._sessions) - set(trip_ids):
            runtime = self._state.trips.get(trip_id)
            if runtime and runtime.holding:
                await self._empty_cart(trip_id)
            await self._close_session(trip_id)
        self._state.prune(trip_ids)

        for trip_id in trip_ids:
            self._state.heartbeat_at = time.time()
            # Re-read: the config may have changed while earlier trips were being checked.
            config = self._store.config
            trip = config.trip(trip_id)
            if trip is None:
                continue
            runtime = self._state.trip(trip_id)
            if runtime.holding:
                await self._service_cart(trip, runtime)
            elif config.enabled and trip.enabled and trip.date >= today():
                await self._check(trip, runtime)
            else:
                await self._close_session(trip_id)
                if runtime.error or runtime.retry_at:
                    # Not being checked any more, so an old failure would only mislead.
                    runtime.error = runtime.error_at = runtime.retry_at = None
                    runtime.consecutive_errors = 0
                    self._state.changed()

        self._state.heartbeat_at = time.time()
        self._save_alerts()

    async def _sleep(self) -> None:
        config = self._store.config
        delay = config.interval_seconds * random.uniform(1 - JITTER, 1 + JITTER)  # noqa: S311 (not crypto)
        deadline = time.monotonic() + delay
        checking = config.enabled and any(
            t.enabled and t.date >= today() and not self._holding(t.id) for t in config.trips
        )
        self._state.next_check_at = time.time() + delay if checking else None
        self._state.changed()

        while (remaining := deadline - time.monotonic()) > 0:
            held = [t for t in self._store.config.trips if self._holding(t.id)]
            try:
                await asyncio.wait_for(self._wake.wait(), min(remaining, CART_POLL_SECONDS) if held else remaining)
            except TimeoutError:
                self._state.heartbeat_at = time.time()
                for trip in held:
                    await self._service_cart(trip, self._state.trip(trip.id))
                continue
            self._wake.clear()
            return

    def _holding(self, trip_id: str) -> bool:
        runtime = self._state.trips.get(trip_id)
        return bool(runtime and runtime.holding)

    # ---- sessions

    def _wants_login(self, trip: Trip) -> bool:
        return trip.auto_cart and self._credentials is not None and time.time() >= self._login_blocked_until

    async def _session_with_results(self, trip: Trip, runtime: TripRuntime) -> tuple[Session, list[Sailing]]:
        """The trip's session and fresh results: reuse the open search, or (re)walk the booking form."""
        current = self._sessions.get(trip.id)
        if current and (current.search_key != trip.search_key() or current.session.closed):
            await self._close_session(trip.id)
            current = None
        if current is None:
            session = await self._browser.new_session(trip.id)
            current = self._sessions[trip.id] = _OpenSession(session, trip.search_key())
            runtime.session_opened_at = session.opened_at
            await self._raise_held_windows()
        session = current.session

        if self._wants_login(trip) and not session.logged_in:
            await self._login(session)
            current.needs_search = True  # logging in navigates away from the results
            runtime.session_logged_in = session.logged_in

        if not current.needs_search:
            try:
                return session, await session.refresh()
            except SessionExpired:
                log.info("%s: search session expired, searching again", trip.id)
        sailings = await session.search(trip)
        current.needs_search = False
        if not current.announced:
            # Logged after the first search, by which point the terminal names (for the title) are loaded.
            current.announced = True
            self._state.event(EventKind.INFO, "Opened a booking session", trip.id, self._title(trip))
        return session, sailings

    async def _login(self, session: Session) -> None:
        if self._credentials is None:
            return
        try:
            await session.login(self._credentials)
        except LoginFailed as e:
            # Don't retry every check and get the account locked; carry on without logging in.
            self._login_blocked_until = time.time() + LOGIN_BACKOFF_SECONDS
            reason = self._describe(e)
            log.warning("%s; not logging in again for %d minutes", reason, LOGIN_BACKOFF_SECONDS // 60)
            self._state.event(EventKind.ERROR, f"{reason}. Auto-cart logins paused for 30 minutes.")
            self._alerts.submit(login_failed_message(reason))

    async def _raise_held_windows(self) -> None:
        """New windows open on top; keep any checkout in progress visible over noVNC."""
        for trip_id, current in self._sessions.items():
            if self._holding(trip_id) and not current.session.closed:
                with contextlib.suppress(Exception):
                    await current.session.show()

    async def _close_session(self, trip_id: str) -> None:
        current = self._sessions.pop(trip_id, None)
        if current:
            await current.session.close()
        runtime = self._state.trips.get(trip_id)
        if runtime and runtime.session_opened_at is not None:
            runtime.session_opened_at = None
            runtime.session_logged_in = False
            self._state.changed()

    async def _close_all(self) -> None:
        for trip_id in list(self._sessions):
            with contextlib.suppress(Exception):
                await self._close_session(trip_id)

    async def _empty_cart(self, trip_id: str) -> None:
        current = self._sessions.get(trip_id)
        if current is None or current.session.closed:
            return
        try:
            await current.session.empty_cart()  # gives the spot back straight away
        except BookingError:
            log.warning("%s: couldn't empty the cart; it will expire on its own", trip_id, exc_info=True)

    # ---- checking

    async def _check(self, trip: Trip, runtime: TripRuntime) -> None:
        if runtime.retry_at and time.time() < runtime.retry_at:
            return
        runtime.checking = True
        self._state.changed()
        try:
            session, sailings = await self._session_with_results(trip, runtime)
        except Exception as e:
            if not isinstance(e, BookingError):
                log.exception("%s: unexpected error while checking", trip.id)
            await self._failed(trip, runtime, e)
            return
        finally:
            runtime.checking = False
            self._state.changed()
        await self._process(trip, runtime, session, sailings)

    async def _failed(self, trip: Trip, runtime: TripRuntime, error: Exception) -> None:
        runtime.consecutive_errors += 1
        runtime.error = self._describe(error)
        runtime.error_at = time.time()
        wait = ERROR_BACKOFF_SECONDS[min(runtime.consecutive_errors, len(ERROR_BACKOFF_SECONDS)) - 1]
        runtime.retry_at = runtime.error_at + wait if wait else None

        current = self._sessions.get(trip.id)
        if current:
            if isinstance(error, MarkupChanged) or not isinstance(error, BookingError):
                path = await current.session.save_debug_snapshot(type(error).__name__)
                if path:
                    log.info("Saved a screenshot of the failure to %s", path)
            if current.session.closed or runtime.consecutive_errors >= RESET_SESSION_AFTER:
                await self._close_session(trip.id)
            else:
                current.needs_search = True  # keep cookies (and the login); just search again

        log.warning("%s: check failed (%d in a row): %s", trip.id, runtime.consecutive_errors, runtime.error)
        self._state.event(EventKind.ERROR, f"Check failed: {runtime.error}", trip.id, self._title(trip))
        if runtime.consecutive_errors == FAILURE_ALERT_AFTER:
            self._alerts.submit(failing_message(trip, self._route(trip), runtime.error))

    async def _process(self, trip: Trip, runtime: TripRuntime, session: Session, sailings: list[Sailing]) -> None:
        # The trip may have been edited or removed while we were on the page.
        current = self._store.config.trip(trip.id)
        if current is None or current.search_key() != trip.search_key():
            return
        trip = current

        previously_available = {s.depart for s in runtime.sailings if s.available}
        available_now = {s.depart for s in sailings if s.available}
        runtime.sailings = sailings
        runtime.checked_at = time.time()
        runtime.error = None
        runtime.error_at = None
        runtime.consecutive_errors = 0
        runtime.retry_at = None
        # Forget sailings that sold out again, so they alert again if they reopen.
        runtime.alerted &= available_now
        new = [s for s in sailings if s.available and trip.in_window(s.depart) and s.depart not in runtime.alerted]

        gone = sorted(previously_available - available_now)
        if gone:
            self._state.event(
                EventKind.INFO, f"Sold out again: {', '.join(format_time(d) for d in gone)}", trip.id, self._title(trip)
            )
        log.info(
            "%s: %d sailings, %d bookable in window",
            trip.id,
            len(sailings),
            sum(1 for s in sailings if s.available and trip.in_window(s.depart)),
        )
        self._state.changed()
        if not new:
            return

        fare = None
        if trip.auto_cart and self._credentials is not None:
            fare = await self._add_to_cart(trip, runtime, session, new[0])
            latest = self._store.config.trip(trip.id)
            if fare and (latest is None or latest.search_key() != trip.search_key()):
                # Edited or removed while the fare was being selected: that cart is for the old search.
                await self._empty_cart(trip.id)
                await self._close_session(trip.id)
                runtime.cart = None
                self._state.changed()
                return

        runtime.alerted |= {s.depart for s in new}
        self._save_alerts()
        self._alert(trip, new, fare)
        self._state.event(EventKind.ALERT, f"Bookable: {summarize(new)}", trip.id, self._title(trip))

    async def _add_to_cart(self, trip: Trip, runtime: TripRuntime, session: Session, sailing: Sailing) -> Fare | None:
        runtime.cart = Cart(CartStatus.ADDING, sailing.depart)
        runtime.release_requested = False
        self._state.changed()
        try:
            async with asyncio.timeout(CART_BUDGET_SECONDS):
                fare = await session.add_to_cart(sailing.depart, trip.fare)
        except Exception as e:
            if isinstance(e, TimeoutError):
                e = BookingError(f"Timed out after {CART_BUDGET_SECONDS}s selecting a fare")
            elif not isinstance(e, BookingError):
                log.exception("%s: unexpected error adding to the cart", trip.id)
            await session.save_debug_snapshot("cart failed")
            await self._close_session(trip.id)  # the page is in an unknown state; search again next time
            runtime.cart = Cart(CartStatus.FAILED, sailing.depart, error=self._describe(e))
            self._state.event(
                EventKind.ERROR,
                f"Couldn't add {format_time(sailing.depart)} to the cart: {runtime.cart.error}",
                trip.id,
                self._title(trip),
            )
            return None

        runtime.cart = Cart(CartStatus.READY, sailing.depart, fare=fare)
        self._state.event(
            EventKind.CART,
            f"{format_time(sailing.depart)} added to the cart ({fare.name} {fare.price})",
            trip.id,
            self._title(trip),
        )
        with contextlib.suppress(Exception):
            await session.show()
        return fare

    async def _service_cart(self, trip: Trip, runtime: TripRuntime) -> None:
        """A held cart: carry on once the user releases it or the checkout window goes away."""
        cart = runtime.cart
        current = self._sessions.get(trip.id)
        session = current.session if current else None
        open_window = session is not None and not session.closed
        if cart is None or (open_window and not runtime.release_requested):
            return

        if runtime.release_requested:
            if open_window:
                await self._empty_cart(trip.id)
            kind, text = EventKind.INFO, "Cart released, watching resumed"
        elif session is not None and not session.lost:
            kind, text = EventKind.INFO, "Checkout window closed, watching resumed"
        else:
            # Crashed: the cart is gone. Forget the alert so it's alerted (and carted) again if still open.
            runtime.alerted.discard(cart.depart)
            kind, text = EventKind.ERROR, f"Lost the {format_time(cart.depart)} cart (the browser closed unexpectedly)"
            self._alerts.submit(cart_lost_message(trip, self._route(trip), cart.depart))

        runtime.cart = None
        runtime.release_requested = False
        await self._close_session(trip.id)
        self._state.event(kind, text, trip.id, self._title(trip))
        self._save_alerts()

    # ---- alerts and labels

    def _route(self, trip: Trip) -> str:
        terminals = self._terminals.cached
        return (
            terminals.route_name(trip.origin, trip.destination) if terminals else f"{trip.origin} → {trip.destination}"
        )

    def _title(self, trip: Trip) -> str:
        return trip_title(trip, self._route(trip))

    def _alert(self, trip: Trip, new: list[Sailing], fare: Fare | None) -> None:
        terminals = self._terminals.cached
        booking_url = search_url(trip, terminals) if terminals else "https://www.bcferries.com/"
        route = self._route(trip)
        if fare is not None:
            message = cart_message(trip, route, new[0], fare, new[1:], self._checkout_url, booking_url)
        else:
            cart_failed = trip.auto_cart and self._credentials is not None
            message = bookable_message(trip, route, new, booking_url, cart_failed=cart_failed)
        self._alerts.submit(message)

    # ---- alert memory (survives restarts)

    def restore_alert_memory(self) -> None:
        """Reload which departures were already alerted before a restart (`run()` calls this first)."""
        if self._memory is None:
            return
        for trip_id, (key, alerted, held) in self._memory.load().items():
            trip = self._store.config.trip(trip_id)
            if trip is None or key != _memory_key(trip):
                continue
            runtime = self._state.trip(trip_id)
            runtime.alerted = alerted - ({held} if held else set())
            if held:
                self._state.event(
                    EventKind.INFO,
                    f"The {format_time(held)} cart was lost in a restart; it'll be added again if it's still open",
                    trip_id,
                    self._title(trip),
                )

    def _save_alerts(self) -> None:
        if self._memory is None:
            return
        entries = {}
        for trip in self._store.config.trips:
            runtime = self._state.trips.get(trip.id)
            if runtime and (runtime.alerted or runtime.holding):
                held = runtime.cart.depart if runtime.holding and runtime.cart else None
                entries[trip.id] = (_memory_key(trip), runtime.alerted, held)
        self._memory.save(entries)
