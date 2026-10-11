from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest

from ferry_watch.models import Config, FarePreference, Trip, ValidationError, Vehicle, format_time

BASE: dict[str, Any] = {"from": "SWB", "to": "TSA", "date": "2026-10-12"}


def test_defaults() -> None:
    trip = Trip.from_json(BASE)
    assert (trip.origin, trip.destination, trip.date) == ("SWB", "TSA", date(2026, 10, 12))
    assert trip.passengers == (1, 0, 0, 0)
    assert trip.vehicle is Vehicle.STANDARD
    assert (trip.earliest, trip.latest) == (time(0, 0), time(23, 59))
    assert trip.auto_cart is True
    assert trip.fare is FarePreference.CHEAPEST
    assert trip.enabled is True
    assert len(trip.id) == 8


def test_json_round_trip() -> None:
    trip = Trip.from_json(
        {
            **BASE,
            "adults": 2,
            "seniors": 1,
            "vehicle": "tall",
            "height_ft": 8.5,
            "earliest": "09:00",
            "latest": "14:30",
            "auto_cart": False,
            "fare": "reservation",
            "enabled": False,
        }
    )
    again = Trip.from_json(trip.to_json(), base=trip)
    assert again == trip
    assert trip.to_json()["earliest"] == "09:00"


def test_partial_update_keeps_id_and_other_fields() -> None:
    trip = Trip.from_json({**BASE, "adults": 2})
    updated = Trip.from_json({"latest": "17:00"}, base=trip)
    assert updated.id == trip.id
    assert updated.adults == 2
    assert updated.latest == time(17, 0)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"date": "tomorrow"}, "date"),
        ({"date": "2026-02-30"}, "date"),
        ({"date": "20266-10-12"}, "date"),
        ({"from": "swb"}, "from"),
        ({"from": "XYZ1"}, "from"),
        ({"to": "SWB"}, "to"),
        ({"earliest": ""}, "earliest"),
        ({"earliest": "25:99"}, "earliest"),
        ({"earliest": "9:00"}, "earliest"),
        ({"earliest": "15:00", "latest": "11:00"}, "latest"),
        ({"adults": -1}, "adults"),
        ({"adults": 10}, "adults"),
        ({"adults": 1.5}, "adults"),
        ({"adults": True}, "adults"),
        ({"adults": "2"}, "adults"),
        ({"adults": 5, "children": 5}, "adults"),
        ({"adults": 0}, "adults"),
        ({"vehicle": "boat"}, "vehicle"),
        ({"height_ft": 7}, "height_ft"),
        ({"height_ft": 81}, "height_ft"),
        ({"height_ft": float("nan")}, "height_ft"),
        ({"height_ft": float("inf")}, "height_ft"),
        ({"auto_cart": "false"}, "auto_cart"),
        ({"fare": "cheap"}, "fare"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_validation(change: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Trip.from_json({**BASE, **change})
    assert exc.value.field == field


@pytest.mark.parametrize("missing", ["from", "to", "date"])
def test_required_fields(missing: str) -> None:
    data = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ValidationError) as exc:
        Trip.from_json(data)
    assert exc.value.field == missing


def test_seniors_without_adults_is_fine() -> None:
    assert Trip.from_json({**BASE, "adults": 0, "seniors": 2}).passengers == (0, 0, 0, 2)


def test_search_key_ignores_window_and_preferences() -> None:
    trip = Trip.from_json(BASE)
    other = Trip.from_json({"earliest": "10:00", "fare": "saver", "auto_cart": False, "enabled": False}, base=trip)
    assert other.search_key() == trip.search_key()
    assert Trip.from_json({"adults": 2}, base=trip).search_key() != trip.search_key()


def test_in_window_is_inclusive() -> None:
    trip = Trip.from_json({**BASE, "earliest": "09:00", "latest": "17:00"})
    assert trip.in_window(time(9, 0))
    assert trip.in_window(time(17, 0))
    assert not trip.in_window(time(8, 59))
    assert not trip.in_window(time(17, 1))


def test_settings_validation() -> None:
    config = Config()
    assert config.with_settings({"interval_seconds": 60}).interval_seconds == 60
    assert config.with_settings({"enabled": False}).enabled is False
    for bad in (
        {"interval_seconds": 5},
        {"interval_seconds": 10**10},
        {"interval_seconds": "30"},
        {"enabled": 1},
        {"x": 1},
    ):
        with pytest.raises(ValidationError):
            config.with_settings(bad)


@pytest.mark.parametrize(
    ("t", "text"),
    [(time(0, 5), "12:05 am"), (time(9, 0), "9:00 am"), (time(12, 0), "12:00 pm"), (time(23, 59), "11:59 pm")],
)
def test_format_time(t: time, text: str) -> None:
    assert format_time(t) == text


def test_stored_ids_are_kept_and_validated() -> None:
    assert Trip.from_json({**BASE, "id": "374d7e06"}).id == "374d7e06"
    with pytest.raises(ValidationError) as exc:
        Trip.from_json({**BASE, "id": "../etc"})
    assert exc.value.field == "id"
    trip = Trip.from_json({**BASE, "id": "374d7e06"})
    assert Trip.from_json({"id": "other"}, base=trip).id == "374d7e06"
