"""Contract tests: the booking-flow code against saved copies of BC Ferries' real pages.

A fake BC Ferries (a local aiohttp server) serves the pages in tests/fixtures/flow and answers
form posts the way the real site does, with redirects. The few things the flow relies on the
site's own JavaScript for (passenger counters, expanding fare options, selecting a fare,
dismissing the cookie banner) are stood in for by a small init script. Chromium can't resolve any
real hostname here, so nothing can reach the live site.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import NoReturn

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import MultiDictProxy
from playwright.async_api import Browser, BrowserContext, async_playwright

from ferry_watch.bcferries.errors import CartMismatch, LoginFailed, NotBookable, RouteRejected, SessionExpired
from ferry_watch.bcferries.site import SiteSession
from ferry_watch.bcferries.terminals import Terminals
from ferry_watch.bcferries.urls import Credentials
from ferry_watch.models import FarePreference, Trip

pytestmark = pytest.mark.browser

FLOW = Path(__file__).parent.parent / "fixtures" / "flow"
CREDENTIALS = Credentials("me@example.com", "correct-horse")
# Any hostname under .localhost is loopback in Chromium; this one contains "queue-it.net", which is
# how the session recognises BC Ferries' waiting room.
QUEUE_HOST = "bcferries.queue-it.net.localhost"

SITE_SCRIPT = """
document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const plus = target.closest(".btn-plus");
  if (plus) {
    const input = plus.closest(".input-group").querySelector("input");
    input.value = String(Number(input.value || 0) + 1);
    return;
  }
  const viewFares = target.closest(".view-fare-btn");
  if (viewFares) {
    viewFares.closest(".p-card").querySelector(".viewfare-box").classList.add("in");
    return;
  }
  if (target.closest("label.js-radio-select")) {
    location.href = "/fare-selection-review";
    return;
  }
  if (target.closest("#cAccept")) {
    document.getElementById("note").remove();
  }
}, true);
"""

PAGES = {
    "/PassengerSelectionPage": "passengers.html",
    "/VehicleSelectionPage": "vehicle.html",
    "/fare-selection": "fare-selection.html",
    "/fare-selection-review": "review.html",
    "/login": "login.html",
    "/my-account": "my-account.html",
}
DATE_INPUT = (
    'name="routeInfoForm.departingDateTime" type="text" '
    'class=" bc-dropdown depart datepicker-input form-control" value="'
)


@dataclass
class FakeBCFerries:
    """The parts of bcferries.com the booking flow touches."""

    posts: list[tuple[str, MultiDictProxy[str]]] = field(default_factory=list)
    accept_date: bool = True
    session_expired: bool = False
    queue_first_visit: bool = False

    def post(self, path: str) -> MultiDictProxy[str]:
        return next(form for p, form in self.posts if p.endswith(path))

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.home)
        app.router.add_get("/queue", self.queue)
        app.router.add_get("/abandoned-cart/my-account", self.empty_cart)
        app.router.add_get("/{page:.*}", self.page)
        app.router.add_post("/{action:.*}", self.submit)
        return app

    def redirect(self, location: str) -> NoReturn:
        raise web.HTTPFound(location)

    async def empty_cart(self, _request: web.Request) -> web.Response:
        self.redirect("/my-account")

    def html(self, name: str, replace: tuple[str, str] | None = None) -> web.Response:
        body = (FLOW / name).read_text()
        if replace:
            body = body.replace(*replace)
        return web.Response(text=body, content_type="text/html")

    async def home(self, request: web.Request) -> web.Response:
        if self.queue_first_visit:
            self.queue_first_visit = False
            back = str(request.url)
            raise web.HTTPFound(request.url.with_host(QUEUE_HOST).with_path("/queue").with_query({"t": back}))
        date = request.query.get("departingDateTime", "") if self.accept_date else ""
        return self.html("route.html", (DATE_INPUT + '"', DATE_INPUT + date + '"'))

    async def queue(self, request: web.Request) -> web.Response:
        back = request.query["t"]
        script = f"<script>setTimeout(() => location.replace({back!r}), 300)</script>"
        return web.Response(text=f"<p>You're in line</p>{script}", content_type="text/html")

    async def page(self, request: web.Request) -> web.Response:
        path = request.path
        if path == "/fare-selection" and self.session_expired:
            self.redirect("/")
        if path not in PAGES:
            raise web.HTTPNotFound
        return self.html(PAGES[path])

    async def submit(self, request: web.Request) -> web.Response:
        form = await request.post()
        path = request.path
        self.posts.append((path, form))  # type: ignore[arg-type]
        if path.endswith("/PassengerInfo"):
            self.redirect("/PassengerSelectionPage")
        if path.endswith("/next-step"):
            walk_on = form.get("passengerInfoForm.travellingAsWalkOn") == "true"
            self.redirect("/fare-selection" if walk_on else "/VehicleSelectionPage")
        if path.endswith("/search"):
            self.redirect("/fare-selection")
        if path == "/j_spring_security_check":
            ok = form.get("j_username") == CREDENTIALS.username and form.get("j_password") == CREDENTIALS.password
            self.redirect("/my-account" if ok else "/login?error=true")
        raise web.HTTPNotFound


class FixedTerminals:
    def __init__(self, terminals: Terminals) -> None:
        self._terminals = terminals

    async def get(self) -> Terminals:
        return self._terminals


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            # No real hostname resolves: a request that escapes the fake site fails instead of
            # reaching bcferries.com.
            args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE *.localhost"]
        )
        yield browser
        await browser.close()


@pytest.fixture
def site() -> FakeBCFerries:
    return FakeBCFerries()


@pytest.fixture
async def site_url(site: FakeBCFerries) -> AsyncIterator[str]:
    server = TestServer(site.app(), host="127.0.0.1")
    await server.start_server()
    yield str(server.make_url("")).rstrip("/")
    await server.close()


@pytest.fixture
async def context(browser: Browser) -> AsyncIterator[BrowserContext]:
    context = await browser.new_context()
    await context.add_init_script(SITE_SCRIPT)
    yield context
    await context.close()


@pytest.fixture
async def session(context: BrowserContext, terminals: Terminals, site_url: str) -> SiteSession:
    page = await context.new_page()
    session = SiteSession("test", context, page, FixedTerminals(terminals), debug_dir=None, base_url=site_url)
    await session.install_handlers()
    return session


def trip(**overrides: object) -> Trip:
    return Trip.from_json({"from": "SWB", "to": "TSA", "date": "2026-10-14", **overrides})


async def test_search_walks_the_booking_form(session: SiteSession, site: FakeBCFerries) -> None:
    sailings = await session.search(trip(adults=2, seniors=1))

    assert [s.depart for s in sailings] == [time(7), time(9), time(11)]
    route_form = site.post("/PassengerInfo")
    assert route_form["routeInfoForm.tripType"] == "SINGLE"
    assert route_form["routeInfoForm.departingDateTime"] == "10/14/2026"
    assert route_form["routeInfoForm.departureLocation"] == "SWB"
    passengers = site.post("/next-step")
    assert passengers["passengerInfoForm.passengerTypeQuantityList[0].quantity"] == "2"  # adults
    assert passengers["passengerInfoForm.passengerTypeQuantityList[3].quantity"] == "1"  # seniors
    assert passengers["passengerInfoForm.travellingAsWalkOn"] == "false"
    vehicle = site.post("/search")
    assert vehicle["vehicleInfoForm.vehicleInfo[0].vehicleType.code"] == "UH_standard"
    assert vehicle["vehicleInfoForm.vehicleInfo[0].standardOversizeLengthOpted"] == "underLength"


async def test_search_as_a_walk_on_skips_the_vehicle_step(session: SiteSession, site: FakeBCFerries) -> None:
    sailings = await session.search(trip(vehicle="none"))
    assert len(sailings) == 3
    assert site.post("/next-step")["passengerInfoForm.travellingAsWalkOn"] == "true"
    assert not any(path.endswith("/search") for path, _ in site.posts)


async def test_search_with_a_tall_vehicle(session: SiteSession, site: FakeBCFerries) -> None:
    await session.search(trip(vehicle="tall", height_ft=8.5))
    assert site.post("/search")["vehicleInfoForm.vehicleInfo[0].vehicleType.code"] == "OS_standard"


async def test_a_date_the_site_wont_take_is_rejected(session: SiteSession, site: FakeBCFerries) -> None:
    site.accept_date = False
    with pytest.raises(RouteRejected):
        await session.search(trip())


async def test_waits_out_the_queue(session: SiteSession, site: FakeBCFerries) -> None:
    site.queue_first_visit = True
    assert len(await session.search(trip())) == 3


async def test_refresh_and_expiry(session: SiteSession, site: FakeBCFerries) -> None:
    await session.search(trip())
    assert len(await session.refresh()) == 3
    site.session_expired = True
    with pytest.raises(SessionExpired):
        await session.refresh()


async def test_add_to_cart(session: SiteSession) -> None:
    await session.search(trip())
    fare = await session.add_to_cart(time(7), FarePreference.SAVER)
    assert (fare.name, fare.price) == ("Saver", "$93.45")


async def test_add_to_cart_checks_the_review_page(session: SiteSession) -> None:
    await session.search(trip())
    with pytest.raises(CartMismatch):
        await session.add_to_cart(time(9), FarePreference.SAVER)  # the review page shows 7:00 am


async def test_add_to_cart_for_a_sailing_thats_gone(session: SiteSession) -> None:
    await session.search(trip())
    with pytest.raises(NotBookable):
        await session.add_to_cart(time(6), FarePreference.SAVER)


async def test_login(session: SiteSession) -> None:
    await session.login(CREDENTIALS)
    assert session.logged_in


async def test_a_failed_login_never_mentions_the_password(session: SiteSession) -> None:
    wrong = Credentials(CREDENTIALS.username, "wrong-password-123")
    with pytest.raises(LoginFailed) as exc:
        await session.login(wrong)
    assert "wrong-password-123" not in str(exc.value)
    assert exc.value.__suppress_context__  # the Playwright error (with its call log) is dropped
    assert not session.logged_in


async def test_empty_cart(session: SiteSession) -> None:
    await session.empty_cart()  # lands on my-account without error


async def test_closing_the_window_is_noticed(session: SiteSession, context: BrowserContext) -> None:
    open_before = not session.closed
    await context.pages[0].close()  # the user closing the window
    assert open_before
    assert session.closed
    assert not session.lost
