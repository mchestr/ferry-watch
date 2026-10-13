"""The agent's alert sink: typed alerts for the operator, which writes the words and the links."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import time

from ferry_watch.models import Fare, Sailing, Trip
from ferry_watch.protocol import AlertKind, AlertParams, error_class

log = logging.getLogger(__name__)

MAX_DEPARTURES = 10


def _hhmm(depart: time) -> str:
    return depart.strftime("%H:%M")


class AgentAlerts:
    """Implements the watcher's AlertSink by queueing typed alerts for the operator (never text or URLs)."""

    def __init__(self, send: Callable[[AlertKind, AlertParams], None]) -> None:
        self._send = send

    def bookable(self, trip: Trip, sailings: Sequence[Sailing], *, cart_failed: bool) -> None:  # noqa: ARG002
        departures = [_hhmm(s.depart) for s in sailings[:MAX_DEPARTURES]]
        self._send("cart_failed" if cart_failed else "bookable", AlertParams(departures=departures))

    def cart_held(self, trip: Trip, held: Sailing, fare: Fare, others: Sequence[Sailing]) -> None:  # noqa: ARG002
        departures = [_hhmm(s.depart) for s in [held, *others][:MAX_DEPARTURES]]
        self._send("cart_held", AlertParams(departures=departures, fare=fare.name[:32], price=fare.price[:24]))

    def failing(self, trip: Trip, error: str, cause: BaseException) -> None:  # noqa: ARG002
        self._send("failing", AlertParams(error_class=error_class(cause)))

    def cart_expired(self, trip: Trip, depart: time, minutes: int) -> None:  # noqa: ARG002
        self._send("cart_expired", AlertParams(departures=[_hhmm(depart)], minutes=minutes))

    def cart_lost(self, trip: Trip, depart: time) -> None:  # noqa: ARG002
        self._send("cart_lost", AlertParams(departures=[_hhmm(depart)]))

    def login_failed(self, reason: str) -> None:  # noqa: ARG002 - may quote BC Ferries' page
        # Agents run in guest mode: the login path is kept but disabled (design-reference.md §5.7).
        log.warning("A BC Ferries login failed in a pod that shouldn't log in")
