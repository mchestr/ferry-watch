"""Runtime state the UI shows: per-trip results, carts, the activity feed.

Everything here is mutated from the event loop thread only (the watcher and the HTTP handlers are
both coroutines on that loop), so there's no locking. `changed()` bumps `seq` and tells
subscribers (chiefly the WebSocket hub) that a new snapshot is worth sending.
"""

from __future__ import annotations

import enum
import itertools
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import time as clock_time
from pathlib import Path

from .fileio import write_json_atomic
from .models import Fare, Sailing

log = logging.getLogger(__name__)

MAX_EVENTS = 80


class CartStatus(enum.StrEnum):
    ADDING = "adding"  # selecting the fare
    READY = "ready"  # in the cart; the trip is paused until it's released
    FAILED = "failed"  # couldn't add it; shown until dismissed


@dataclass(slots=True)
class Cart:
    status: CartStatus
    depart: clock_time
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    since: float = field(default_factory=time.time)
    fare: Fare | None = None
    error: str | None = None
    expires_at: float | None = None  # a ready cart is handed back at this time


class EventKind(enum.StrEnum):
    INFO = "info"
    ALERT = "alert"
    CART = "cart"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    kind: EventKind
    text: str
    trip_id: str | None
    trip_title: str | None
    ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class TripRuntime:
    sailings: list[Sailing] = field(default_factory=list)
    checked_at: float | None = None
    checking: bool = False
    error: str | None = None
    error_at: float | None = None
    consecutive_errors: int = 0
    retry_at: float | None = None  # backing off after errors until this time
    next_due: float | None = None  # when it's checked next (None: as soon as possible)
    alerted: set[clock_time] = field(default_factory=set)  # departures already alerted
    session_opened_at: float | None = None
    session_logged_in: bool = False
    cart: Cart | None = None
    release_requested: bool = False

    def reset_results(self) -> None:
        """The search changed: old results, errors and alerts no longer apply."""
        self.sailings = []
        self.checked_at = None
        self.error = None
        self.error_at = None
        self.consecutive_errors = 0
        self.retry_at = None
        self.next_due = None
        self.alerted = set()

    @property
    def holding(self) -> bool:
        return self.cart is not None and self.cart.status is CartStatus.READY


class RuntimeState:
    def __init__(self) -> None:
        self.boot_id = uuid.uuid4().hex[:12]
        self.seq = 0
        self.trips: dict[str, TripRuntime] = {}
        self.events: deque[Event] = deque(maxlen=MAX_EVENTS)
        self.next_check_at: float | None = None
        self.heartbeat_at: float = time.time()  # last time the watcher loop made progress
        self._event_ids = itertools.count(1)
        self._listeners: list[Callable[[], None]] = []

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def changed(self) -> None:
        self.seq += 1
        for listener in self._listeners:
            listener()

    def trip(self, trip_id: str) -> TripRuntime:
        return self.trips.setdefault(trip_id, TripRuntime())

    def prune(self, keep: Iterable[str]) -> None:
        keep = set(keep)
        for trip_id in set(self.trips) - keep:
            del self.trips[trip_id]

    def event(self, kind: EventKind, text: str, trip_id: str | None = None, trip_title: str | None = None) -> None:
        self.events.appendleft(Event(next(self._event_ids), kind, text, trip_id, trip_title))
        self.changed()


class AlertMemory:
    """Which departures each trip has already alerted, kept across restarts.

    Without it every deploy would re-alert every open sailing. Entries are tied to the trip's
    search, so editing a trip forgets them. A cart held when the service stopped is lost with the
    browser, so its departure is forgotten too (it'll be alerted and carted again if still open).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._saved: str | None = None

    def load(self) -> dict[str, tuple[str, set[clock_time], clock_time | None]]:
        """{trip_id: (search key, alerted departures, departure held in a cart)}"""
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("Ignoring unreadable %s", self._path, exc_info=True)
            return {}
        result: dict[str, tuple[str, set[clock_time], clock_time | None]] = {}
        try:
            for trip_id, entry in raw.items():
                held = clock_time.fromisoformat(entry["held"]) if entry.get("held") else None
                result[trip_id] = (str(entry["key"]), {clock_time.fromisoformat(t) for t in entry["alerted"]}, held)
        except (AttributeError, KeyError, TypeError, ValueError):
            log.warning("Ignoring malformed %s", self._path, exc_info=True)
            return {}
        return result

    def save(self, entries: dict[str, tuple[str, set[clock_time], clock_time | None]]) -> None:
        data = {
            trip_id: {
                "key": key,
                "alerted": sorted(t.strftime("%H:%M") for t in alerted),
                "held": held.strftime("%H:%M") if held else None,
            }
            for trip_id, (key, alerted, held) in sorted(entries.items())
        }
        serialized = json.dumps(data, sort_keys=True)
        if serialized == self._saved:
            return
        try:
            write_json_atomic(self._path, data)
        except OSError:
            log.warning("Couldn't save %s", self._path, exc_info=True)
            return
        self._saved = serialized
