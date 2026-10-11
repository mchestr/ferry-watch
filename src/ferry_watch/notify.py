"""Alerts: what they say (pure functions) and how they're delivered (Pushover)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import time
from typing import Protocol

import aiohttp

from .models import Fare, Sailing, Trip, Vehicle, format_time

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class NotifyError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Message:
    title: str
    body: str
    url: str | None = None
    url_title: str | None = None
    high_priority: bool = False


class Notifier(Protocol):
    @property
    def configured(self) -> bool: ...

    async def send(self, message: Message) -> None: ...


class Alerts(Protocol):
    """Where the watcher sends alerts (AlertDispatcher, or a recorder in tests)."""

    def submit(self, message: Message) -> None: ...


class PushoverNotifier:
    def __init__(self, http: aiohttp.ClientSession, token: str, user_key: str, *, url: str = PUSHOVER_URL) -> None:
        self._http = http
        self._token = token
        self._user_key = user_key
        self._url = url

    @property
    def configured(self) -> bool:
        return True

    async def send(self, message: Message) -> None:
        form = {
            "token": self._token,
            "user": self._user_key,
            "title": message.title,
            "message": message.body,
            "priority": "1" if message.high_priority else "0",
        }
        if message.url:
            form["url"] = message.url
            form["url_title"] = message.url_title or "Open"
        try:
            async with self._http.post(self._url, data=form, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    raise NotifyError(f"Pushover returned {resp.status}: {detail[:200]}")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise NotifyError(f"Couldn't reach Pushover: {e}") from e
        log.info("Sent alert: %s", message.title)


class LogNotifier:
    """Used when Pushover isn't configured: alerts only go to the log."""

    @property
    def configured(self) -> bool:
        return False

    async def send(self, message: Message) -> None:
        log.info("Alert (Pushover not configured): %s | %s", message.title, message.body.replace("\n", " / "))


class AlertDispatcher:
    """Delivers alerts in the background, retrying failures.

    Watching never waits on Pushover, and a Pushover outage can't break a check or lose a cart;
    the alert just arrives late (or `on_failure` is told it never did).
    """

    RETRY_DELAYS = (10, 30, 60, 120, 300)

    def __init__(self, notifier: Notifier, on_failure: Callable[[Message, str], None]) -> None:
        self._notifier = notifier
        self._on_failure = on_failure
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def configured(self) -> bool:
        return self._notifier.configured

    def submit(self, message: Message) -> None:
        task = asyncio.create_task(self._deliver(message), name=f"alert: {message.title}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send_now(self, message: Message) -> None:
        """Send once, raising NotifyError (for the UI's test button)."""
        await self._notifier.send(message)

    async def _deliver(self, message: Message) -> None:
        error = ""
        for delay in (0, *self.RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._notifier.send(message)
            except NotifyError as e:
                error = str(e)
                log.warning("Alert %r not delivered yet: %s", message.title, e)
            else:
                return
        self._on_failure(message, error)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


# ---- wording


def trip_title(trip: Trip, route: str) -> str:
    """'Swartz Bay → Tsawwassen · Mon Oct 12'"""
    return f"{route} · {trip.date:%a %b} {trip.date.day}"


def summarize(sailings: Sequence[Sailing], limit: int = 3) -> str:
    """'7:00 am ($93.45), 9:00 am ($93.45), 11:00 am ($93.45) +7 more'"""
    parts = [f"{format_time(s.depart)} ({s.price})" if s.price else format_time(s.depart) for s in sailings[:limit]]
    extra = len(sailings) - limit
    return ", ".join(parts) + (f" +{extra} more" if extra > 0 else "")


def booking_steps(trip: Trip, depart: time) -> str:
    """The taps left on bcferries.com after the prefilled link (it can't carry the whole booking)."""
    party = [
        f"{n} {one if n == 1 else many}"
        for n, one, many in zip(
            trip.passengers,
            ("adult", "child", "infant", "senior"),
            ("adults", "children", "infants", "seniors"),
            strict=True,
        )
        if n
    ]
    steps = [
        "One-way → Continue",
        f"{', '.join(party)}, {'without' if trip.vehicle is Vehicle.NONE else 'with'} a vehicle → Continue",
    ]
    if trip.vehicle is Vehicle.STANDARD:
        steps.append("7 ft & under, under 20 ft → Continue")
    elif trip.vehicle is Vehicle.TALL:
        steps.append(f"Over 7 ft ({trip.height_ft:g} ft), under 20 ft → Continue")
    steps.append(f"{format_time(depart)} → View fares")
    return "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))


def bookable_message(
    trip: Trip, route: str, sailings: Sequence[Sailing], booking_url: str, *, cart_failed: bool
) -> Message:
    note = " (couldn't add it to the cart)" if cart_failed else ""
    return Message(
        title=f"⛴ Ferry space: {trip_title(trip, route)}",
        body=f"Bookable: {summarize(sailings, limit=6)}{note}\n\n{booking_steps(trip, sailings[0].depart)}",
        url=booking_url,
        url_title="Book on bcferries.com",
        high_priority=True,
    )


def cart_message(
    trip: Trip,
    route: str,
    held: Sailing,
    fare: Fare,
    others: Sequence[Sailing],
    checkout_url: str | None,
    booking_url: str,
) -> Message:
    also = f"\nAlso bookable: {summarize(others)}" if others else ""
    where = "Tap to finish checkout" if checkout_url else "Finish checkout in the browser window"
    return Message(
        title=f"🛒 {format_time(held.depart)} is in your cart",
        body=f"{trip_title(trip, route)}\n{fare.name} {fare.price}{also}\n\n{where}. Nothing is booked until you pay.",
        url=checkout_url or booking_url,
        url_title="Open checkout" if checkout_url else "Book on bcferries.com",
        high_priority=True,
    )


def failing_message(trip: Trip, route: str, error: str) -> Message:
    return Message(title=f"⛴ Watcher failing: {trip_title(trip, route)}", body=f"Checks keep failing:\n{error}"[:1000])


def cart_lost_message(trip: Trip, route: str, depart: time) -> Message:
    return Message(
        title=f"⛴ Lost the {format_time(depart)} cart",
        body=f"{trip_title(trip, route)}\nThe browser holding it closed unexpectedly. "
        "It'll be added again if it's still open.",
        high_priority=True,
    )


def login_failed_message(error: str) -> Message:
    return Message(
        title="⛴ BC Ferries login failed",
        body=f"{error}\n\nAuto-cart is paused for 30 minutes so the account doesn't get locked. Alerts continue.",
    )
