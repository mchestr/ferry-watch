"""What each kind of alert says, and where it links (docs/multi-tenant/design-reference.md §6.7).

Agents send only a kind and typed parameters; every word a user reads is written here. Links are built
from constants and the trip's id, never from anything an agent or a page said.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ferry_watch.bcferries.errors import RouteRejected
from ferry_watch.bcferries.terminals import Terminals
from ferry_watch.bcferries.urls import search_url
from ferry_watch.models import PACIFIC, Trip, format_time
from ferry_watch.web.push import Urgency

EMAIL_KINDS = frozenset({"cart_held", "cart_lost", "cart_expired", "booked_it", "booked", "invited"})
ADMIN_KINDS = frozenset({"release_unverified", "checkout_blocked"})
SHOWN_TIMES = 3
BOOK_IT_YOURSELF = "Or book it yourself on bcferries.com"
# Why a bookable sailing wasn't held (ferry-control's `held_back`), and what the user can do about it.
HELD_BACK = {
    "quiet_hours": "It's in your quiet hours, so we didn't hold it. Open Ferry Watch to hold it now.",
    "auto_cart_off": "Auto-hold is off for this trip. Open Ferry Watch to hold it now.",
    "holding_elsewhere": "You're holding a cart on another trip, so we didn't hold this one.",
    "no_holds_left": "You've used today's holds, so we didn't hold it.",
    "limit": "You've reached today's limit for holding carts, so we didn't hold it.",
    "carting_paused": "Holding carts is paused right now, so we didn't hold it.",
}
NOT_HELD = "We couldn't hold it this time. Open Ferry Watch to try again."
# Why BC Ferries didn't let a cart be held (the agent's error class).
CART_FAILED = {
    "not_bookable": "it may have just sold out",
    "timeout": "the site was too slow",
    "queue_timeout": "the site was too busy",
    "markup_changed": "the site's pages changed",
    "cart_mismatch": "the site's pages changed",
}


@dataclass(frozen=True, slots=True)
class Message:
    title: str
    body: str
    url: str  # absolute
    urgency: Urgency = "normal"
    ttl: int = 3600  # how long a push service keeps it for an offline device
    topic: str | None = None
    also: tuple[str, str] | None = None  # (label, url): a second link, for email (a push opens only one)

    def push(self) -> dict[str, Any]:
        return {"title": self.title, "body": self.body, "url": self.url}


def _times(departures: list[str]) -> str:
    times = [format_time(datetime.strptime(d, "%H:%M").time()) for d in departures]  # noqa: DTZ007 - a time of day
    if len(times) <= SHOWN_TIMES:
        return ", ".join(times)
    return f"{', '.join(times[:SHOWN_TIMES])} and {len(times) - SHOWN_TIMES} more"


def _date(trip: Trip) -> str:
    return trip.date.strftime("%a %b %-d")


def _clock(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return format_time(datetime.fromtimestamp(epoch, PACIFIC).time())


class Messages:
    def __init__(self, base_url: str, terminals: Callable[[], Terminals | None]) -> None:
        self._base = base_url.rstrip("/")
        self._terminals = terminals

    def _route(self, trip: Trip) -> str:
        terminals = self._terminals()
        if terminals is not None and trip.origin in terminals and trip.destination in terminals:
            return terminals.route_name(trip.origin, trip.destination)
        return f"{trip.origin} → {trip.destination}"

    def _search(self, trip: Trip) -> str:
        return self._booking(trip) or f"{self._base}/"  # an unknown terminal: the dashboard instead

    def _booking(self, trip: Trip) -> str | None:
        """BC Ferries' booking form for the trip, if the terminal list knows its route."""
        terminals = self._terminals()
        if terminals is not None:
            with contextlib.suppress(RouteRejected):
                return search_url(trip, terminals)
        return None

    def _book_it_yourself(self, trip: Trip) -> tuple[str, str] | None:
        url = self._booking(trip)
        return (BOOK_IT_YOURSELF, url) if url else None

    def render(  # noqa: C901, PLR0911, PLR0912 - one branch per kind
        self,
        kind: str,
        params: Mapping[str, Any],
        trip: Trip | None,
        cart: Mapping[str, Any] | None = None,
    ) -> Message | None:
        """The message for one notification, or None for a kind with nothing to say to its user."""
        checkout = f"{self._base}/c/{trip.id}" if trip else f"{self._base}/"
        if kind == "test":
            return Message("Test alert", "Alerts from Ferry Watch reach this device.", f"{self._base}/")
        if kind == "invited":
            return Message(
                "You're invited to Ferry Watch",
                "Sign in with your account to start watching BC Ferries sailings for you.",
                f"{self._base}/",
                ttl=86400,
            )
        if trip is None:
            return None
        route, date = self._route(trip), _date(trip)
        departures = list(params.get("departures") or [])
        when = _times(departures) if departures else "A sailing"
        topic = f"{kind[:8]}-{trip.id}"
        if kind == "cart_held":
            by = _clock((cart or {}).get("unclaimed_deadline"))
            deadline = f"Check out by {by}." if by else "Check out soon."
            return Message(
                f"Held for you: {when} {route}, {date}",
                f"{deadline} Have your card and phone ready; your bank may ask you to approve in its app. "
                "A computer is easiest.",
                checkout,
                urgency="high",
                ttl=1800,
                topic=topic,
            )
        if kind in ("hold_reminder", "hold_ending"):
            by = _clock((cart or {}).get("session_deadline") or (cart or {}).get("unclaimed_deadline"))
            lead = "Your cart is still held" if kind == "hold_reminder" else "Checkout closes soon"
            return Message(
                f"{lead}: {when} {route}",
                f"Check out by {by}." if by else "Check out soon.",
                checkout,
                urgency="high",
                ttl=600,
                topic=topic,
            )
        if kind == "bookable":
            body = HELD_BACK.get(str(params.get("held_back")), NOT_HELD)
            return Message(
                f"Bookable: {when} {route}, {date}",
                body,
                checkout,  # the trip's page: why nothing is held, "Hold it for me", and bcferries.com
                urgency="high",
                also=self._book_it_yourself(trip),
            )
        if kind == "cart_failed":
            why = CART_FAILED.get(str(params.get("error_class")))
            return Message(
                f"Bookable, but we couldn't hold it: {when} {route}, {date}",
                f"BC Ferries didn't let us hold it{f' ({why})' if why else ''}. Open Ferry Watch to try again.",
                checkout,
                urgency="high",
                also=self._book_it_yourself(trip),
            )
        if kind == "cart_lost":
            return Message(
                f"Lost the held {when} cart", f"{route}, {date}. We'll keep watching.", checkout, urgency="high"
            )
        if kind == "cart_expired":
            minutes = params.get("minutes")
            held = f" after {minutes} minutes" if minutes else ""
            return Message(f"Released the {when} cart{held}", f"{route}, {date}.", checkout)
        if kind == "failing":
            return Message(
                f"Having trouble checking {route}",
                "We'll keep trying. If BC Ferries changed their site, an update is on the way.",
                checkout,
                urgency="low",
            )
        if kind == "booked_it":
            return Message(
                "Did you book it?",
                f"You checked out the {when} {route} sailing. Tell us, so we know whether to keep watching.",
                checkout,
                ttl=86400,
            )
        if kind == "booked":
            return Message("Booked!", f"{when} {route}, {date}. Have a good trip.", checkout, ttl=86400)
        if kind == "checkout_blocked":
            return Message(
                "Checkout hit a blocked page",
                "Something in your checkout couldn't load. Try again, or book on bcferries.com.",
                checkout,
                urgency="high",
            )
        return None
