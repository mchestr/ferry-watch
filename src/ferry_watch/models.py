"""Domain types: trips (what the user watches), sailings (what BC Ferries offers), and their validation."""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Any, Self
from zoneinfo import ZoneInfo

# BC Ferries runs on Pacific time; "today" and "has this date passed" are judged there.
PACIFIC = ZoneInfo("America/Vancouver")

MAX_TRIPS = 6
# BC Ferries books at most nine passengers online.
MAX_PASSENGERS = 9
MIN_INTERVAL_SECONDS = 15
MAX_INTERVAL_SECONDS = 3600

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID_RE = re.compile(r"^[a-z0-9]{1,32}$")


class ValidationError(ValueError):
    """User input that can't be accepted. `field` names the offending JSON key."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class Vehicle(enum.StrEnum):
    STANDARD = "standard"  # 7 ft and under, under 20 ft long
    TALL = "tall"  # over 7 ft, under 20 ft long
    NONE = "none"  # walk-on


class FarePreference(enum.StrEnum):
    CHEAPEST = "cheapest"
    SAVER = "saver"
    PREPAID = "prepaid"
    RESERVATION = "reservation"

    @property
    def fare_name(self) -> str | None:
        """The fare's name as BC Ferries shows it, or None for "whichever is cheapest"."""
        return _FARE_NAMES.get(self)


_FARE_NAMES = {
    FarePreference.SAVER: "Saver",
    FarePreference.PREPAID: "Prepaid",
    FarePreference.RESERVATION: "Reservation Only",
}


class SailingStatus(enum.StrEnum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    UNAVAILABLE = "unavailable"  # departed, cancelled, not bookable online...


@dataclass(frozen=True, slots=True)
class Fare:
    name: str  # "Saver", "Prepaid", "Reservation Only", "At terminal"
    price: str  # as displayed, e.g. "$93.45"
    selectable: bool  # bookable online (the "At terminal" option isn't)

    @property
    def amount(self) -> float:
        digits = re.sub(r"[^\d.]", "", self.price)
        return float(digits) if digits else float("inf")


@dataclass(frozen=True, slots=True)
class Sailing:
    depart: time
    arrive: time
    vessel: str
    status: SailingStatus
    note: str  # BC Ferries' wording for anything but available, e.g. "Advance bookings sold out"
    price: str | None  # lowest fare, only shown for available sailings
    fares: tuple[Fare, ...] = ()

    @property
    def available(self) -> bool:
        return self.status is SailingStatus.AVAILABLE


def today() -> date:
    """Today's date in BC (whatever timezone the server runs in)."""
    return datetime.now(PACIFIC).date()


def format_time(t: time) -> str:
    """time(15, 0) -> '3:00 pm' (BC Ferries' style, used in alerts and logs)."""
    return f"{(t.hour + 11) % 12 + 1}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def _parse_hhmm(name: str, value: Any) -> time:
    if not isinstance(value, str) or not (m := _HHMM_RE.match(value)):
        raise ValidationError(name, "Use a 24-hour time like 09:30")
    return time(int(m[1]), int(m[2]))


def _parse_date(name: str, value: Any) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(name, "Use a date like 2026-10-12")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError(name, "That date doesn't exist") from None


def _parse_int(name: str, value: Any, lo: int, hi: int) -> int:
    # bool is an int subclass; reject it so `true` isn't read as 1.
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValidationError(name, f"Must be a whole number from {lo} to {hi}")
    return value


def _parse_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(name, "Must be true or false")
    return value


def _parse_enum[E: enum.StrEnum](name: str, value: Any, kind: type[E]) -> E:
    try:
        return kind(value)
    except ValueError:
        choices = ", ".join(e.value for e in kind)
        raise ValidationError(name, f"Must be one of: {choices}") from None


def _parse_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError("id", "Trip ids are 1-32 lowercase letters and digits")
    return value


def _parse_code(name: str, value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValidationError(name, "Must be a BC Ferries terminal code like SWB")
    return value


@dataclass(frozen=True, slots=True)
class Trip:
    """One route + date + party the user wants a reservation for."""

    id: str
    origin: str  # terminal code, e.g. "SWB"
    destination: str  # terminal code, e.g. "TSA"
    date: date
    adults: int = 1
    children: int = 0
    infants: int = 0
    seniors: int = 0
    vehicle: Vehicle = Vehicle.STANDARD
    height_ft: float = 8.0  # only used for Vehicle.TALL
    earliest: time = time(0, 0)
    latest: time = time(23, 59)
    auto_cart: bool = True  # add an opened sailing to the cart
    fare: FarePreference = FarePreference.CHEAPEST
    enabled: bool = True

    @property
    def passengers(self) -> tuple[int, int, int, int]:
        """Counts in BC Ferries' passenger-form order: adults, children, infants, seniors."""
        return (self.adults, self.children, self.infants, self.seniors)

    def search_key(self) -> tuple[object, ...]:
        """Everything that defines the BC Ferries search; a change means a new search session."""
        return (self.origin, self.destination, self.date, self.passengers, self.vehicle, self.height_ft)

    def in_window(self, departure: time) -> bool:
        return self.earliest <= departure <= self.latest

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "from": self.origin,
            "to": self.destination,
            "date": self.date.isoformat(),
            "adults": self.adults,
            "children": self.children,
            "infants": self.infants,
            "seniors": self.seniors,
            "vehicle": self.vehicle.value,
            "height_ft": self.height_ft,
            "earliest": self.earliest.strftime("%H:%M"),
            "latest": self.latest.strftime("%H:%M"),
            "auto_cart": self.auto_cart,
            "fare": self.fare.value,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], *, base: Trip | None = None) -> Self:
        """Build a trip from JSON, validating every field.

        With `base`, `data` is a partial update applied on top of it (the id can't change).
        Without it, `from`, `to` and `date` are required, optional fields take defaults, and the
        trip keeps `data["id"]` if given (stored trips) or gets a new one.
        """
        if not isinstance(data, dict):
            raise ValidationError("trip", "Expected a JSON object")
        unknown = set(data) - set(_FIELDS) - {"id"}
        if unknown:
            raise ValidationError(sorted(unknown)[0], "Unknown field")

        current: dict[str, Any] = base.to_json() if base else {}
        merged = {**current, **data}
        for required in ("from", "to", "date"):
            if required not in merged:
                raise ValidationError(required, "Required")

        height = merged.get("height_ft", 8.0)
        if isinstance(height, bool) or not isinstance(height, int | float) or not 7.0 < height <= 14.0:
            raise ValidationError("height_ft", "Must be over 7 and at most 14 feet")

        if base is not None:
            trip_id = base.id
        elif "id" in data:
            trip_id = _parse_id(data["id"])
        else:
            trip_id = uuid.uuid4().hex[:8]
        trip = cls(
            id=trip_id,
            origin=_parse_code("from", merged["from"]),
            destination=_parse_code("to", merged["to"]),
            date=_parse_date("date", merged["date"]),
            adults=_parse_int("adults", merged.get("adults", 1), 0, MAX_PASSENGERS),
            children=_parse_int("children", merged.get("children", 0), 0, MAX_PASSENGERS),
            infants=_parse_int("infants", merged.get("infants", 0), 0, MAX_PASSENGERS),
            seniors=_parse_int("seniors", merged.get("seniors", 0), 0, MAX_PASSENGERS),
            vehicle=_parse_enum("vehicle", merged.get("vehicle", "standard"), Vehicle),
            height_ft=round(float(height), 1),
            earliest=_parse_hhmm("earliest", merged.get("earliest", "00:00")),
            latest=_parse_hhmm("latest", merged.get("latest", "23:59")),
            auto_cart=_parse_bool("auto_cart", merged.get("auto_cart", True)),
            fare=_parse_enum("fare", merged.get("fare", "cheapest"), FarePreference),
            enabled=_parse_bool("enabled", merged.get("enabled", True)),
        )
        if trip.origin == trip.destination:
            raise ValidationError("to", "Pick a different destination")
        if sum(trip.passengers) > MAX_PASSENGERS:
            raise ValidationError("adults", f"BC Ferries books at most {MAX_PASSENGERS} passengers online")
        if trip.adults + trip.seniors < 1:
            raise ValidationError("adults", "At least one adult or senior has to travel")
        if trip.latest < trip.earliest:
            raise ValidationError("latest", "Must be after the earliest departure")
        return trip


_FIELDS = (
    "enabled",
    "from",
    "to",
    "date",
    "adults",
    "children",
    "infants",
    "seniors",
    "vehicle",
    "height_ft",
    "earliest",
    "latest",
    "auto_cart",
    "fare",
)


@dataclass(frozen=True, slots=True)
class Config:
    """Everything the user configures; persisted to config.json."""

    enabled: bool = True
    interval_seconds: int = 30
    trips: tuple[Trip, ...] = field(default_factory=tuple)

    def trip(self, trip_id: str) -> Trip | None:
        return next((t for t in self.trips if t.id == trip_id), None)

    def with_settings(self, data: dict[str, Any]) -> Config:
        if not isinstance(data, dict):
            raise ValidationError("settings", "Expected a JSON object")
        unknown = set(data) - {"enabled", "interval_seconds"}
        if unknown:
            raise ValidationError(sorted(unknown)[0], "Unknown field")
        return replace(
            self,
            enabled=_parse_bool("enabled", data.get("enabled", self.enabled)),
            interval_seconds=_parse_int(
                "interval_seconds",
                data.get("interval_seconds", self.interval_seconds),
                MIN_INTERVAL_SECONDS,
                MAX_INTERVAL_SECONDS,
            ),
        )
