"""Alerts: what they say (pure functions) and how they're delivered (Pushover)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import time
from typing import Protocol

import aiohttp

from .bcferries.terminals import Terminals
from .bcferries.urls import BASE_URL, search_url
from .models import Fare, Sailing, Trip, Vehicle, format_time

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# Pushover's field limits (https://pushover.net/api#limits).
_MAX_TITLE = 250
_MAX_MESSAGE = 1024
_MAX_URL_TITLE = 100
_WORD_SLACK = 32  # how far back from a cut to look for a space


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
    """Where written alerts go for delivery (AlertDispatcher, or a recorder in tests)."""

    def submit(self, message: Message) -> None: ...


class AlertSink(Protocol):
    """What the watcher tells the user about.

    The watcher reports what happened; a sink picks the words and the links. TextAlerts writes today's
    messages. In a browser pod the agent sends typed alerts to the operator, which writes them.
    """

    def bookable(self, trip: Trip, sailings: Sequence[Sailing], *, cart_failed: bool) -> None: ...
    def cart_held(self, trip: Trip, held: Sailing, fare: Fare, others: Sequence[Sailing]) -> None: ...
    def failing(self, trip: Trip, error: str) -> None: ...
    def cart_expired(self, trip: Trip, depart: time, minutes: int) -> None: ...
    def cart_lost(self, trip: Trip, depart: time) -> None: ...
    def login_failed(self, reason: str) -> None: ...


class RouteNames(Protocol):
    """The terminal list, if it's been loaded (bcferries.terminals.TerminalDirectory)."""

    @property
    def cached(self) -> Terminals | None: ...


def route_name(trip: Trip, terminals: Terminals | None) -> str:
    """'Swartz Bay → Tsawwassen', or the codes before the terminal list has loaded."""
    if terminals is None:
        return f"{trip.origin} → {trip.destination}"
    return terminals.route_name(trip.origin, trip.destination)


class TextAlerts:
    """Writes the watcher's alerts as messages and submits them for delivery."""

    def __init__(self, alerts: Alerts, terminals: RouteNames, checkout_url: str | None) -> None:
        self._alerts = alerts
        self._terminals = terminals
        self._checkout_url = checkout_url

    def _route(self, trip: Trip) -> str:
        return route_name(trip, self._terminals.cached)

    def _booking_url(self, trip: Trip) -> str:
        terminals = self._terminals.cached
        return search_url(trip, terminals) if terminals else f"{BASE_URL}/"

    def bookable(self, trip: Trip, sailings: Sequence[Sailing], *, cart_failed: bool) -> None:
        message = bookable_message(trip, self._route(trip), sailings, self._booking_url(trip), cart_failed=cart_failed)
        self._alerts.submit(message)

    def cart_held(self, trip: Trip, held: Sailing, fare: Fare, others: Sequence[Sailing]) -> None:
        route, url = self._route(trip), self._booking_url(trip)
        self._alerts.submit(cart_message(trip, route, held, fare, others, self._checkout_url, url))

    def failing(self, trip: Trip, error: str) -> None:
        self._alerts.submit(failing_message(trip, self._route(trip), error))

    def cart_expired(self, trip: Trip, depart: time, minutes: int) -> None:
        self._alerts.submit(cart_expired_message(trip, self._route(trip), depart, minutes))

    def cart_lost(self, trip: Trip, depart: time) -> None:
        self._alerts.submit(cart_lost_message(trip, self._route(trip), depart))

    def login_failed(self, reason: str) -> None:
        self._alerts.submit(login_failed_message(reason))


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
            "title": truncate(message.title, _MAX_TITLE),
            "message": truncate(message.body, _MAX_MESSAGE),
            "priority": "1" if message.high_priority else "0",
        }
        if message.url:
            form["url"] = message.url
            form["url_title"] = truncate(message.url_title or "Open", _MAX_URL_TITLE)
        try:
            async with self._http.post(self._url, data=form, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                status = resp.status
                detail = "" if status == 200 else await resp.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise NotifyError(f"Couldn't reach Pushover: {one_line(str(e)) or type(e).__name__}") from e
        if status != 200:
            raise NotifyError(f"Pushover returned {status}: {self._reason(detail)}")
        log.info("Sent alert: %s", message.title)

    def _reason(self, body: str) -> str:
        """Pushover's own explanation when it gives one ("user identifier is invalid"), never our secrets."""
        try:
            reply = json.loads(body)
        except ValueError:
            reply = None
        errors = reply.get("errors") if isinstance(reply, dict) else None
        reasons = [e for e in errors if isinstance(e, str)] if isinstance(errors, list) else []
        text = "; ".join(reasons) or body
        for secret in (self._token, self._user_key):
            text = text.replace(secret, "[redacted]")
        return one_line(text) or "(empty response)"


def truncate(text: str, limit: int) -> str:
    """`text` if it fits in `limit` characters, otherwise its start plus "…", cut between words when one is close.

    Pushover refuses a message with an over-long field outright, so an alert would be lost rather than shortened.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if not text[limit - 1].isspace():
        boundary = max(cut.rfind(" "), cut.rfind("\n"))
        if boundary > 0 and len(cut) - boundary <= _WORD_SLACK:
            cut = cut[:boundary]
    return cut.rstrip() + "…"


def one_line(text: str, limit: int = 200) -> str:
    """Whitespace collapsed and cut to `limit`: error messages go in log lines and a small UI notice."""
    return truncate(" ".join(text.split()), limit)


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


def cart_expired_message(trip: Trip, route: str, depart: time, minutes: int) -> Message:
    return Message(
        title=f"⛴ Released the {format_time(depart)} cart",
        body=f"{trip_title(trip, route)}\nIt was held for {minutes} minutes without checking out, so it went back. "
        "Watching resumed.",
    )


def login_failed_message(error: str) -> Message:
    return Message(
        title="⛴ BC Ferries login failed",
        body=f"{error}\n\nAuto-cart is paused for 30 minutes so the account doesn't get locked. Alerts continue.",
    )
