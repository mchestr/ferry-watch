"""Driving bcferries.com in a real browser.

The site sits behind a Queue-it waiting room and keeps the booking search in a server-side
session, so we drive Chromium with Playwright. One `SiteBrowser` owns the browser; each watched
trip gets its own `SiteSession` (a browser context with its own cookies, hence its own search and
cart). Interactions use the controls a person would use (labels, buttons by role), scoped to the
element they belong to, and every navigation is verified by URL.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import weakref
from datetime import UTC, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ferry_watch.models import Fare, FarePreference, Sailing, Trip, Vehicle, format_time

from .errors import (
    BookingError,
    CartMismatch,
    LoginFailed,
    MarkupChanged,
    NotBookable,
    QueueTimeout,
    RouteRejected,
    SessionExpired,
)
from .parse import parse_sailings, parse_time
from .terminals import Terminals
from .urls import BASE_URL, QUEUE_HOST, USER_AGENT, CheckoutView, Credentials, checkout_view, search_url

if TYPE_CHECKING:
    from ferry_watch.protocol import ReleaseCheck

log = logging.getLogger(__name__)

# Where /abandoned-cart/my-account ends up: the account page, or its login page for a guest.
_RELEASE_LANDINGS = ("/my-account", "/login")

ACTION_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000
RESULTS_TIMEOUT_MS = 20_000
QUEUE_TIMEOUT_MS = 10 * 60_000
DEBUG_SNAPSHOTS_KEPT = 20

# Passenger-type classes on BC Ferries' passenger form, in Trip.passengers order.
_PASSENGER_CLASSES = ("y_adult", "y_child", "y_infant", "y_senior")
_PASSENGER_NAMES = ("adult", "child", "infant", "senior")


class TerminalProvider(Protocol):
    """Where sessions get the terminal list from (terminals.TerminalDirectory)."""

    async def get(self) -> Terminals: ...


def choose_fare(fares: tuple[Fare, ...], preference: FarePreference) -> Fare:
    """The preferred fare if it's offered online, otherwise the cheapest one that is."""
    bookable = [f for f in fares if f.selectable]
    if not bookable:
        raise NotBookable("None of this sailing's fares can be booked online")
    if preference.fare_name:
        preferred = next((f for f in bookable if f.name == preference.fare_name), None)
        if preferred:
            return preferred
    return min(bookable, key=lambda f: f.amount)


class SiteBrowser:
    """The single Chromium instance (headed on a virtual display in production, so it can be watched)."""

    def __init__(
        self,
        terminals: TerminalProvider,
        *,
        headless: bool,
        window_size: tuple[int, int],
        debug_dir: Path | None,
        base_url: str = BASE_URL,
    ) -> None:
        self._terminals = terminals
        self._base_url = base_url
        self._headless = headless
        self._window_size = window_size
        self._debug_dir = debug_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: weakref.WeakSet[SiteSession] = weakref.WeakSet()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()

    async def stop(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except PlaywrightError:
                log.debug("Browser was already gone")
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _ensure_browser(self) -> Browser:
        if self._browser and self._browser.is_connected():
            return self._browser
        try:
            return await self._launch()
        except PlaywrightError:
            # The Playwright driver itself may have died; restart it once before giving up.
            log.warning("Couldn't launch Chromium; restarting Playwright", exc_info=True)
            await self.stop()
            await self.start()
            return await self._launch()

    async def _launch(self) -> Browser:
        if self._playwright is None:
            raise RuntimeError("SiteBrowser.start() hasn't been called")
        width, height = self._window_size
        log.info("Launching Chromium (%s)", "headless" if self._headless else "headed")
        browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[f"--window-size={width},{height}", "--window-position=0,0"],
        )
        browser.on("disconnected", lambda _: self._browser_died())
        self._browser = browser
        return browser

    def _browser_died(self) -> None:
        log.warning("Chromium disconnected")
        for session in list(self._sessions):
            session.mark_lost()

    async def new_session(self, name: str) -> SiteSession:
        browser = await self._ensure_browser()
        context = await browser.new_context(user_agent=USER_AGENT, no_viewport=True)
        page = await context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        session = SiteSession(name, context, page, self._terminals, self._debug_dir, base_url=self._base_url)
        await session.install_handlers()
        self._sessions.add(session)
        return session


class SiteSession:
    """One browser context: a BC Ferries search (and, once something's held, a cart)."""

    def __init__(
        self,
        name: str,
        context: BrowserContext,
        page: Page,
        terminals: TerminalProvider,
        debug_dir: Path | None,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self.opened_at = datetime.now(UTC).timestamp()
        self.logged_in = False
        self._context = context
        self._page = page
        self._terminals = terminals
        self._debug_dir = debug_dir
        self._closed = False
        self._lost = False
        # "close": the user closed the window (e.g. over noVNC) or the context was closed.
        # "crash": the tab's renderer died. Chromium dying altogether is reported by SiteBrowser.
        page.on("close", lambda _: self._mark_closed())
        page.on("crash", lambda _: self.mark_lost())

    def _mark_closed(self) -> None:
        self._closed = True

    def mark_lost(self) -> None:
        self._lost = True
        self._closed = True

    @property
    def closed(self) -> bool:
        """The page can't be used any more (closed by the user, by us, or crashed)."""
        return self._closed or self._page.is_closed()

    @property
    def lost(self) -> bool:
        """It went away unexpectedly (crash), as opposed to being closed on purpose."""
        return self._lost

    async def close(self) -> None:
        self._closed = True
        try:
            await self._context.close()
        except PlaywrightError:
            log.debug("Context for %s was already gone", self.name)

    # ---- navigation helpers

    async def _goto(self, url: str) -> None:
        try:
            await self._page.goto(url, wait_until="load")
        except PlaywrightTimeout as e:
            raise BookingError(f"Timed out loading {urllib.parse.urlsplit(url).path or '/'}") from e
        await self._wait_out_queue()

    async def _wait_out_queue(self) -> None:
        if QUEUE_HOST not in (urllib.parse.urlsplit(self._page.url).hostname or ""):
            return
        log.info("%s: in BC Ferries' waiting room", self.name)
        try:
            await self._page.wait_for_url(
                lambda url: QUEUE_HOST not in url, timeout=QUEUE_TIMEOUT_MS, wait_until="load"
            )
        except PlaywrightTimeout as e:
            raise QueueTimeout("Still in BC Ferries' waiting room after 10 minutes") from e

    async def _click_and_wait(self, control: Locator, path: str, failure: BookingError) -> None:
        """Click something that navigates, and check we land on `path`."""
        await control.click()
        try:
            await self._page.wait_for_url(re.compile(re.escape(path)), wait_until="load")
        except PlaywrightTimeout as e:
            raise failure from e
        await self._wait_out_queue()

    def _continue_button(self) -> Locator:
        return self._page.get_by_role("button", name="Continue", exact=True).filter(visible=True)

    async def install_handlers(self) -> None:
        """Dismiss the cookie banner whenever it gets in the way of an action."""
        accept = self._page.locator("#cAccept")
        await self._page.add_locator_handler(accept, lambda: accept.click(), times=None)

    # ---- the booking flow

    async def login(self, credentials: Credentials) -> None:
        """Log in to the owner's account (for saved payment cards at checkout).

        Playwright's error messages include an action's call log, which for fill() contains the
        value typed. So nothing from Playwright escapes this method: every failure becomes a
        LoginFailed with a message we wrote, and the original error is dropped (`from None`).
        """
        page = self._page
        await self._goto(f"{self._base_url}/login")
        form = page.locator("form").filter(has=page.locator("#j_password"))
        try:
            await form.locator("#email").fill(credentials.username)
            await form.locator("#j_password").fill(credentials.password)
            filled = bool(await form.locator("#email").input_value()) and bool(
                await form.locator("#j_password").input_value()
            )
        except PlaywrightError:
            raise LoginFailed("Couldn't fill in BC Ferries' login form") from None
        if not filled:
            # The site clears the form in some states (e.g. while a guest cart exists).
            raise LoginFailed("BC Ferries' login form wouldn't accept the credentials")
        try:
            await form.get_by_role("button", name="Log in", exact=True).click()
            await page.wait_for_url(lambda url: "/login" not in urllib.parse.urlsplit(url).path, wait_until="load")
        except PlaywrightError:
            alert = page.locator(".alert-danger, .alert-error, .fe-error, [role=alert]").filter(visible=True)
            try:
                reason = (await alert.first.inner_text(timeout=2_000)).strip() if await alert.count() else ""
            except PlaywrightError:
                reason = ""
            raise LoginFailed(f"BC Ferries login failed{': ' + reason if reason else ''}") from None
        self.logged_in = True

    async def search(self, trip: Trip) -> list[Sailing]:
        """Walk the booking form for `trip` and return the sailings on the results page."""
        page = self._page
        await self._goto(search_url(trip, await self._terminals.get(), self._base_url))

        submit = page.locator("#y_confirmaddpassenger")
        await submit.wait_for(state="visible")
        await page.locator("label[for='y_oneWayRadbtn']").click()
        if await page.locator("[name='routeInfoForm.departingDateTime']").input_value() != trip.date.strftime(
            "%m/%d/%Y"
        ):
            raise RouteRejected(f"BC Ferries didn't accept the date {trip.date:%b %-d, %Y}")
        await self._click_and_wait(
            submit, "/PassengerSelectionPage", RouteRejected("BC Ferries didn't accept this route and date")
        )

        for css_class, name, count in zip(_PASSENGER_CLASSES, _PASSENGER_NAMES, trip.passengers, strict=True):
            if not count:
                continue
            quantity = page.locator(f"input.y_outboundPassengerQuantity.{css_class}")
            if await quantity.count() != 1:
                raise RouteRejected(f"This route doesn't take {name} passengers online")
            plus = page.locator(".y_outboundPassengerQtySelector").filter(has=quantity).locator("button.btn-plus")
            for _ in range(count):
                await plus.click()
            if (await quantity.input_value()) != str(count):
                raise MarkupChanged(f"Couldn't set {name} passengers to {count}")

        walk_on = trip.vehicle is Vehicle.NONE
        travel_choice = "proceed_to_sailing_selection_btn" if walk_on else "proceed_to_vehicle_selection_btn"
        await page.locator("label.custom-radio-input", has=page.locator(f"#{travel_choice}")).click()
        if walk_on:
            await self._click_and_wait(
                self._continue_button(), "/fare-selection", MarkupChanged("Didn't reach the sailings")
            )
            return await self._read_results()

        await self._click_and_wait(
            self._continue_button(), "/VehicleSelectionPage", MarkupChanged("Didn't reach the vehicle step")
        )
        if trip.vehicle is Vehicle.TALL:
            await page.locator("label[for='over7Height_0']").click()
            height = page.locator("#Over7heightboxinft_0")
            await height.fill(f"{trip.height_ft:g}")
            await height.press("Tab")  # the site converts units on change
        else:
            await page.locator("label[for='under7Height_0']").click()
        await page.locator("label[for='under20Length_0']").click()
        await self._click_and_wait(
            self._continue_button(), "/fare-selection", MarkupChanged("Didn't reach the sailings")
        )
        return await self._read_results()

    async def refresh(self) -> list[Sailing]:
        """Reload the results page; BC Ferries re-runs the session's search with fresh availability."""
        await self._goto(f"{self._base_url}/fare-selection")
        return await self._read_results()

    async def _read_results(self) -> list[Sailing]:
        if "/fare-selection" not in urllib.parse.urlsplit(self._page.url).path:
            raise SessionExpired("The search session expired")
        try:
            await self._page.locator(".p-card").first.wait_for(timeout=RESULTS_TIMEOUT_MS)
        except PlaywrightTimeout as e:
            raise MarkupChanged("No sailings on the results page") from e
        sailings = parse_sailings(await self._page.content())
        if not sailings:
            raise MarkupChanged("No sailings on the results page")
        return sailings

    async def add_to_cart(self, depart: time, preference: FarePreference) -> Fare:
        """Select a fare for the sailing leaving at `depart`; BC Ferries puts it in this session's cart."""
        page = self._page
        sailings = parse_sailings(await page.content())
        index = next((i for i, s in enumerate(sailings) if s.depart == depart and s.available), None)
        if index is None:
            raise NotBookable("The sailing sold out again before it could be added to the cart")
        fare = choose_fare(sailings[index].fares, preference)

        card = page.locator(".p-card").nth(index)
        shown = parse_time(" ".join((await card.locator(".ferrytime").first.inner_text()).split()))
        if shown != depart:
            raise MarkupChanged("The sailings changed while selecting a fare")
        await card.get_by_role("button", name="View fares").click()
        option = card.locator(".vp-box").filter(has=page.locator(".fare-type-name").get_by_text(fare.name, exact=True))
        await self._click_and_wait(
            # Each fare also has a "Select this fare" label inside its collapsed price breakdown.
            option.locator("label.js-radio-select").filter(visible=True),
            "/fare-selection-review",
            NotBookable("BC Ferries didn't accept the fare selection"),
        )
        # Confirm the cart holds what we selected before telling anyone it's there.
        summary = " ".join((await page.locator("body").inner_text()).split()).lower()
        if format_time(depart) not in summary or fare.name.lower() not in summary:
            raise CartMismatch(f"The cart doesn't show the {format_time(depart)} {fare.name} sailing")
        return fare

    async def empty_cart(self) -> ReleaseCheck:
        """Abandon the cart, which gives the sailing back straight away.

        Returns how far the release could be confirmed: "landing_url" when BC Ferries sent us where an
        abandoned cart lands (the account page, or its login page for a guest), "none" otherwise. Checking
        that the cart is really empty needs captured cart pages (docs/multi-tenant §5.7).
        """
        await self._goto(f"{self._base_url}/abandoned-cart/my-account")
        landed = urllib.parse.urlsplit(self._page.url).path
        return "landing_url" if landed.startswith(_RELEASE_LANDINGS) else "none"

    def checkout_view(self) -> CheckoutView:
        """What the held cart's checkout is showing: every page and frame in this context, by URL only."""
        if self.closed:
            return CheckoutView()
        others = [
            frame.url for page in self._context.pages for frame in page.frames if frame is not self._page.main_frame
        ]
        return checkout_view(self._page.url, others, base_url=self._base_url)

    async def show(self) -> None:
        """Raise this session's window, so it's what's on screen over noVNC."""
        await self._page.bring_to_front()

    async def save_debug_snapshot(self, reason: str) -> Path | None:
        """Screenshot the page for troubleshooting (kept outside backed-up storage, oldest pruned)."""
        if self._debug_dir is None or self.closed:
            return None
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")[:40]
            path = self._debug_dir / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{self.name}-{slug}.png"
            # Viewport only: enough to see what went wrong without capturing the whole account page.
            await self._page.screenshot(path=path)
            for old in sorted(self._debug_dir.glob("*.png"))[:-DEBUG_SNAPSHOTS_KEPT]:
                old.unlink(missing_ok=True)
        except (PlaywrightError, OSError):
            log.warning("Couldn't save a debug screenshot", exc_info=True)
            return None
        return path
