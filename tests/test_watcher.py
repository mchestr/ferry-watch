"""The watch loop's behaviour, driven one cycle at a time against a scripted fake site."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ferry_watch.bcferries.errors import BookingError, LoginFailed, MarkupChanged, NotBookable
from ferry_watch.bcferries.site import Credentials
from ferry_watch.bcferries.terminals import Terminals
from ferry_watch.models import Trip
from ferry_watch.state import AlertMemory, CartStatus, EventKind, RuntimeState, TripRuntime
from ferry_watch.store import ConfigStore
from ferry_watch.watcher import RESET_SESSION_AFTER, Watcher
from tests.fakes import PASSWORD, FakeSite, RecordingAlerts, sailing


@dataclass
class Env:
    store: ConfigStore
    state: RuntimeState
    site: FakeSite
    alerts: RecordingAlerts
    watcher: Watcher
    trip: Trip
    memory: AlertMemory

    def runtime(self) -> TripRuntime:
        """A fresh read each call (a property would let mypy narrow it across awaits)."""
        return self.state.trip(self.trip.id)

    def events(self, kind: EventKind | None = None) -> list[str]:
        return [e.text for e in self.state.events if kind is None or e.kind is kind]


def make_env(tmp_path: Path, terminals: Terminals, *, credentials: bool = True, **trip: Any) -> Env:
    store = ConfigStore(tmp_path / "config.json")
    added = store.add_trip(Trip.from_json({"from": "SWB", "to": "TSA", "date": "2026-10-12", **trip}))
    state = RuntimeState()
    site = FakeSite()
    alerts = RecordingAlerts()
    memory = AlertMemory(tmp_path / "alerted.json")
    watcher = Watcher(
        store,
        state,
        site,
        alerts,
        SimpleNamespace(cached=terminals),
        memory=memory,
        credentials=Credentials("me@example.com", PASSWORD) if credentials else None,
        checkout_url="https://ferry.example/vnc",
    )
    return Env(store, state, site, alerts, watcher, added, memory)


@pytest.fixture
def env(tmp_path: Path, terminals: Terminals) -> Env:
    return make_env(tmp_path, terminals)


async def test_first_check_opens_a_logged_in_session(env: Env) -> None:
    env.site.sailings = [sailing(7, available=False), sailing(9, available=False)]
    await env.watcher.cycle()

    assert env.site.session.calls == ["login", "search"]
    assert [s.depart for s in env.runtime().sailings] == [time(7), time(9)]
    assert env.runtime().checked_at is not None
    assert env.runtime().session_logged_in
    assert env.alerts.sent == []


async def test_later_checks_just_reload(env: Env) -> None:
    env.site.sailings = [sailing(7, available=False)]
    await env.watcher.cycle()
    await env.watcher.cycle()
    assert env.site.session.calls == ["login", "search", "refresh"]
    assert len(env.site.sessions) == 1


async def test_an_expired_session_searches_again_in_place(env: Env) -> None:
    env.site.sailings = [sailing(7, available=False)]
    await env.watcher.cycle()
    env.site.expire_next_refresh = True
    await env.watcher.cycle()
    assert env.site.session.calls == ["login", "search", "refresh", "search"]
    assert env.runtime().error is None


async def test_open_sailing_goes_into_the_cart_and_alerts(env: Env) -> None:
    env.site.sailings = [sailing(7, available=False), sailing(9), sailing(11)]
    await env.watcher.cycle()

    cart = env.runtime().cart
    assert cart is not None
    assert cart.status is CartStatus.READY
    assert cart.depart == time(9)
    assert "cart 09:00" in env.site.session.calls
    assert env.alerts.titles == ["🛒 9:00 am is in your cart"]
    assert env.alerts.sent[0].url == "https://ferry.example/vnc"
    assert env.runtime().alerted == {time(9), time(11)}

    # While the cart is held the trip isn't checked.
    calls = len(env.site.session.calls)
    await env.watcher.cycle()
    assert len(env.site.session.calls) == calls


async def test_outside_the_window_doesnt_alert(tmp_path: Path, terminals: Terminals) -> None:
    env = make_env(tmp_path, terminals, earliest="10:00", latest="12:00")
    env.site.sailings = [sailing(7), sailing(13)]
    await env.watcher.cycle()
    assert env.alerts.sent == []
    assert env.runtime().cart is None


async def test_without_auto_cart_it_just_alerts(tmp_path: Path, terminals: Terminals) -> None:
    env = make_env(tmp_path, terminals, auto_cart=False)
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    assert env.site.session.calls == ["search"]  # no login needed
    assert env.alerts.titles == ["⛴ Ferry space: Swartz Bay → Tsawwassen · Mon Oct 12"]
    assert "couldn't add" not in env.alerts.sent[0].body


async def test_without_credentials_there_is_no_cart(tmp_path: Path, terminals: Terminals) -> None:
    env = make_env(tmp_path, terminals, credentials=False)
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    assert "login" not in env.site.session.calls
    assert env.runtime().cart is None
    assert env.alerts.titles == ["⛴ Ferry space: Swartz Bay → Tsawwassen · Mon Oct 12"]


async def test_a_sailing_alerts_once_until_it_sells_out_again(tmp_path: Path, terminals: Terminals) -> None:
    env = make_env(tmp_path, terminals, auto_cart=False)
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    await env.watcher.cycle()
    assert len(env.alerts.sent) == 1

    env.site.sailings = [sailing(9, available=False)]
    await env.watcher.cycle()
    assert "Sold out again: 9:00 am" in env.events(EventKind.INFO)

    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    assert len(env.alerts.sent) == 2


async def test_release_needs_the_right_cart(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    cart = env.runtime().cart
    assert cart is not None

    assert not env.watcher.release_cart(env.trip.id, "some-other-cart")
    assert env.watcher.release_cart(env.trip.id, cart.id)
    await env.watcher.cycle()

    held_session = env.site.sessions[0]
    assert "empty cart" in held_session.calls
    assert held_session.closed
    assert env.runtime().cart is None
    assert "Cart released, watching resumed" in env.events()
    # A stale second release can't touch a later cart.
    assert not env.watcher.release_cart(env.trip.id, cart.id)


async def test_pausing_keeps_the_held_cart(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    env.store.replace_trip(Trip.from_json({"enabled": False}, base=env.trip))
    env.store.update_settings({"enabled": False})
    await env.watcher.cycle()

    assert not env.site.session.closed
    cart = env.runtime().cart
    assert cart is not None
    assert cart.status is CartStatus.READY


async def test_deleting_a_trip_hands_its_cart_back(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    env.store.delete_trip(env.trip.id)
    await env.watcher.cycle()

    assert env.site.session.calls[-2:] == ["empty cart", "close"]
    assert env.trip.id not in env.state.trips


async def test_closing_the_checkout_window_resumes_quietly(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    env.site.session.user_closes_window()
    await env.watcher.cycle()

    assert env.runtime().cart is None
    assert "Checkout window closed, watching resumed" in env.events()
    assert time(9) in env.runtime().alerted  # not re-alerted: the user was done with it
    assert len(env.alerts.sent) == 1


async def test_a_crash_loses_the_cart_and_retries_it(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    await env.watcher.cycle()
    env.site.session.crash()
    await env.watcher.cycle()  # notices the lost cart
    assert env.alerts.titles[-1] == "⛴ Lost the 9:00 am cart"
    assert time(9) not in env.runtime().alerted

    await env.watcher.cycle()  # still open: carted and alerted again in a new session
    assert len(env.site.sessions) == 2
    cart = env.runtime().cart
    assert cart is not None
    assert cart.status is CartStatus.READY


async def test_a_failed_cart_still_alerts(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    env.site.cart_error = NotBookable("sold out again")
    await env.watcher.cycle()

    cart = env.runtime().cart
    assert cart is not None
    assert cart.status is CartStatus.FAILED
    assert cart.error == "sold out again"
    assert env.site.session.closed
    assert "couldn't add it to the cart" in env.alerts.sent[0].body
    # Failed carts don't pause the trip, and can be dismissed by id.
    assert not env.watcher.dismiss_cart(env.trip.id, "wrong")
    assert env.watcher.dismiss_cart(env.trip.id, cart.id)
    assert env.runtime().cart is None


async def test_failures_back_off_and_keep_the_session_at_first(env: Env) -> None:
    env.site.sailings = [sailing(7, available=False)]
    await env.watcher.cycle()
    env.site.error = MarkupChanged("No sailings on the results page")

    await env.watcher.cycle()
    assert env.runtime().error == "No sailings on the results page"
    assert env.runtime().consecutive_errors == 1
    assert env.runtime().retry_at is None  # first retry is immediate
    assert not env.site.session.closed  # cookies and login kept
    assert "snapshot" in env.site.session.calls

    for _ in range(RESET_SESSION_AFTER - 1):
        env.watcher.check_now()  # skip the back-off
        await env.watcher.cycle()
    assert env.runtime().consecutive_errors == RESET_SESSION_AFTER
    assert env.runtime().retry_at is not None
    assert env.site.sessions[-1].closed  # started over
    assert env.alerts.titles == ["⛴ Watcher failing: Swartz Bay → Tsawwassen · Mon Oct 12"]

    env.site.error = None
    env.watcher.check_now()
    await env.watcher.cycle()
    assert env.runtime().error is None
    assert env.runtime().consecutive_errors == 0


async def test_backing_off_skips_checks(env: Env) -> None:
    env.site.error = BookingError("Timed out loading /")
    await env.watcher.cycle()
    await env.watcher.cycle()  # immediate retry (1st back-off step is 0)
    await env.watcher.cycle()  # now backing off: no new session, no call
    assert env.runtime().consecutive_errors == 2


async def test_a_failed_login_pauses_logins_but_keeps_checking(env: Env) -> None:
    env.site.login_error = LoginFailed("BC Ferries login failed: Password not valid")
    env.site.sailings = [sailing(7, available=False)]
    await env.watcher.cycle()

    assert env.site.session.calls == ["login", "search"]
    assert env.runtime().sailings  # alerts keep working
    assert env.alerts.titles == ["⛴ BC Ferries login failed"]
    await env.watcher.cycle()
    assert env.site.session.calls.count("login") == 1  # not retried every check


async def test_results_for_an_edited_trip_are_thrown_away(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    env.site.search_hook = lambda trip: env.store.replace_trip(Trip.from_json({"date": "2026-10-13"}, base=trip))
    await env.watcher.cycle()

    assert env.runtime().sailings == []
    assert env.alerts.sent == []
    assert env.runtime().cart is None


async def test_a_cart_for_an_edited_trip_is_handed_back(env: Env) -> None:
    env.site.sailings = [sailing(9)]
    env.site.cart_hook = lambda: env.store.replace_trip(Trip.from_json({"date": "2026-10-13"}, base=env.trip))
    await env.watcher.cycle()

    assert "empty cart" in env.site.sessions[0].calls
    assert env.runtime().cart is None
    assert env.alerts.sent == []


async def test_past_trips_arent_checked(tmp_path: Path, terminals: Terminals) -> None:
    env = make_env(tmp_path, terminals, date="2026-10-10")
    await env.watcher.cycle()
    assert env.site.sessions == []


async def test_alert_memory_survives_a_restart(tmp_path: Path, terminals: Terminals) -> None:
    env = make_env(tmp_path, terminals)
    env.site.sailings = [sailing(9), sailing(11)]
    await env.watcher.cycle()  # 9:00 carted, both alerted

    state = RuntimeState()
    restarted = Watcher(
        env.store,
        state,
        FakeSite(),
        RecordingAlerts(),
        SimpleNamespace(cached=terminals),
        memory=AlertMemory(tmp_path / "alerted.json"),
        credentials=None,
        checkout_url=None,
    )
    restarted.restore_alert_memory()
    runtime = state.trip(env.trip.id)
    # 11:00 was alerted; the held 9:00 cart died with the old browser, so it's forgotten.
    assert runtime.alerted == {time(11)}
    assert any("lost in a restart" in e.text for e in state.events)


async def test_secrets_never_reach_errors(env: Env) -> None:
    env.site.error = BookingError(f'Timeout 30000ms exceeded.\nCall log:\n  - fill("{PASSWORD}")')
    await env.watcher.cycle()
    assert env.runtime().error == "Timeout 30000ms exceeded."
    env.site.error = BookingError(f"weird {PASSWORD} message")
    env.watcher.check_now()
    await env.watcher.cycle()
    assert PASSWORD not in (env.runtime().error or "")
    assert all(PASSWORD not in e.text for e in env.state.events)


async def test_pausing_clears_an_old_failure(env: Env) -> None:
    env.site.error = BookingError("Timed out loading /")
    await env.watcher.cycle()
    assert env.runtime().error is not None
    env.store.replace_trip(Trip.from_json({"enabled": False}, base=env.trip))
    await env.watcher.cycle()
    assert env.runtime().error is None
    assert env.runtime().retry_at is None
