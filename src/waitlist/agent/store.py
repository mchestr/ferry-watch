"""The agent's one trip, as the operator last configured it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import msgspec

from waitlist.models import Config, Trip, ValidationError
from waitlist.protocol import Configure, ProtocolError


class OneTripStore:
    """What the watcher reads in a pod: a `.config` with its one trip, and `.subscribe` for changes.

    Each `configure` replaces the trip and its settings. The trip is validated again here, with the same
    rules as the dashboard, and must be the trip this pod was started for.
    """

    def __init__(self, trip_id: str) -> None:
        self._trip_id = trip_id
        self._rev = -1
        self._config = Config(enabled=False, trips=())
        self._latest: Configure | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def config(self) -> Config:
        return self._config

    @property
    def latest(self) -> Configure | None:
        """The last configure applied: the limits and flags beyond the trip (None before the first)."""
        return self._latest

    @property
    def rev(self) -> int:
        """The revision last applied (-1 before the first configure)."""
        return self._rev

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def apply(self, message: Configure) -> bool:
        """Take a configure from the operator. Returns False for a stale one (an older revision)."""
        if message.rev < self._rev:
            return False
        try:
            trip = Trip.from_json(msgspec.to_builtins(message.trip_spec))
        except ValidationError as e:
            raise ProtocolError(f"Invalid trip: {e}") from None
        if trip.id != self._trip_id:
            raise ProtocolError(f"This pod watches trip {self._trip_id}, not {trip.id}")
        # The operator can turn carting off (quiet hours, hold limits) but never on against the user's wish.
        trip = replace(trip, auto_cart=trip.auto_cart and message.auto_cart)
        self._config = Config(enabled=message.active, interval_seconds=message.interval_s, trips=(trip,))
        self._rev = message.rev
        self._latest = message
        for listener in self._listeners:
            listener()
        return True
