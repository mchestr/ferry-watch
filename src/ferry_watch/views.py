"""JSON the web UI consumes (GET /api/state and the WebSocket stream). See docs/api.md."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from .bcferries.terminals import Terminals
from .bcferries.urls import search_url
from .models import MAX_TRIPS, Config, Sailing, Trip, today
from .notify import trip_title
from .state import Cart, Event, RuntimeState, TripRuntime

# The watcher loop is considered stalled after this long without progress. Generous, because one
# trip's check can legitimately wait out BC Ferries' Queue-it room for up to 10 minutes.
HEARTBEAT_STALE_SECONDS = 15 * 60

_IDLE = TripRuntime()


def _hhmm(t: Any) -> str:
    return str(t.strftime("%H:%M"))


def sailing_view(sailing: Sailing, trip: Trip) -> dict[str, Any]:
    return {
        "depart": _hhmm(sailing.depart),
        "arrive": _hhmm(sailing.arrive),
        "vessel": sailing.vessel,
        "status": sailing.status.value,
        "note": sailing.note,
        "price": sailing.price,
        "in_window": trip.in_window(sailing.depart),
    }


def cart_view(cart: Cart | None) -> dict[str, Any] | None:
    if cart is None:
        return None
    return {
        "id": cart.id,
        "status": cart.status.value,
        "depart": _hhmm(cart.depart),
        "since": cart.since,
        "fare": cart.fare.name if cart.fare else None,
        "price": cart.fare.price if cart.fare else None,
        "error": cart.error,
        "expires_at": cart.expires_at,
    }


def _route_view(trip: Trip, terminals: Terminals | None) -> dict[str, str]:
    def names(code: str) -> tuple[str, str]:
        if terminals and code in terminals:
            return terminals[code].place, terminals[code].area
        return code, ""

    (from_place, from_area), (to_place, to_area) = names(trip.origin), names(trip.destination)
    return {"from_place": from_place, "from_area": from_area, "to_place": to_place, "to_area": to_area}


def trip_view(trip: Trip, runtime: TripRuntime, terminals: Terminals | None) -> dict[str, Any]:
    route = _route_view(trip, terminals)
    known = terminals is not None and trip.origin in terminals and trip.destination in terminals
    return {
        **trip.to_json(),
        "title": trip_title(trip, f"{route['from_place']} → {route['to_place']}"),
        "route": route,
        "past": trip.date < today(),
        "sailings": [sailing_view(s, trip) for s in runtime.sailings],
        "checked_at": runtime.checked_at,
        "checking": runtime.checking,
        "error": runtime.error,
        "error_at": runtime.error_at,
        "retry_at": runtime.retry_at,
        "session": (
            {"opened_at": runtime.session_opened_at, "logged_in": runtime.session_logged_in}
            if runtime.session_opened_at is not None
            else None
        ),
        "cart": cart_view(runtime.cart),
        "booking_url": search_url(trip, terminals) if known and terminals else None,
    }


def event_view(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "ts": event.ts,
        "kind": event.kind.value,
        "text": event.text,
        "trip_id": event.trip_id,
        "trip_title": event.trip_title,
    }


def trip_defaults(login_configured: bool) -> dict[str, Any]:
    tomorrow = today() + timedelta(days=1)
    return {
        "enabled": True,
        "from": "SWB",
        "to": "TSA",
        "date": tomorrow.isoformat(),
        "adults": 1,
        "children": 0,
        "infants": 0,
        "seniors": 0,
        "vehicle": "standard",
        "height_ft": 8.0,
        "earliest": "00:00",
        "latest": "23:59",
        "auto_cart": login_configured,
        "fare": "cheapest",
    }


def state_view(
    config: Config,
    state: RuntimeState,
    terminals: Terminals | None,
    *,
    checkout_url: str | None,
    login_configured: bool,
    alerts_configured: bool,
) -> dict[str, Any]:
    now = time.time()
    return {
        "boot_id": state.boot_id,
        "seq": state.seq,
        "server_time": now,
        "enabled": config.enabled,
        "interval_seconds": config.interval_seconds,
        "next_check_at": state.next_check_at,
        "max_trips": MAX_TRIPS,
        "trip_defaults": trip_defaults(login_configured),
        "trips": [trip_view(t, state.trips.get(t.id, _IDLE), terminals) for t in config.trips],
        "events": [event_view(e) for e in list(state.events)[:40]],
        "checkout_url": checkout_url,
        "login_configured": login_configured,
        "alerts_configured": alerts_configured,
        "watcher": {"heartbeat_at": state.heartbeat_at, "healthy": now - state.heartbeat_at < HEARTBEAT_STALE_SECONDS},
    }
