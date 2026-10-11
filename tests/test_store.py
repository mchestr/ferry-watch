from __future__ import annotations

import json
from pathlib import Path

import pytest

from ferry_watch.models import MAX_TRIPS, Trip, ValidationError
from ferry_watch.store import SCHEMA_VERSION, ConfigStore, TripNotFound


def trip(**overrides: object) -> Trip:
    return Trip.from_json({"from": "SWB", "to": "TSA", "date": "2026-10-12", **overrides})


def test_creates_an_empty_config(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    assert store.config.trips == ()
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved == {"version": SCHEMA_VERSION, "enabled": True, "interval_seconds": 30, "trips": []}


def test_changes_are_persisted_and_announced(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    calls: list[int] = []
    store.subscribe(lambda: calls.append(1))

    added = store.add_trip(trip())
    store.replace_trip(Trip.from_json({"adults": 2}, base=added))
    store.update_settings({"interval_seconds": 60})

    reloaded = ConfigStore(path).config
    assert reloaded.interval_seconds == 60
    assert reloaded.trips == (Trip.from_json({"adults": 2}, base=added),)
    assert len(calls) == 3

    store.delete_trip(added.id)
    assert ConfigStore(path).config.trips == ()
    assert not list(tmp_path.glob(".config.json.*.tmp"))  # no temp files left behind


def test_trip_limit(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    for _ in range(MAX_TRIPS):
        store.add_trip(trip())
    with pytest.raises(ValidationError):
        store.add_trip(trip())


def test_unknown_trip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(TripNotFound):
        store.delete_trip("nope")
    with pytest.raises(TripNotFound):
        store.replace_trip(trip())


def test_migrates_the_single_trip_format(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "date": "2026-10-12",
                "from": "SWB",
                "to": "TSA",
                "adults": 2,
                "children": 0,
                "infants": 0,
                "seniors": 0,
                "vehicle": "standard",
                "height_ft": 8.0,
                "earliest": "09:00",
                "latest": "17:00",
                "interval_minutes": 2,
                "hold": True,
                "fare": "reservation",
            }
        )
    )
    config = ConfigStore(path).config
    assert len(config.trips) == 1
    migrated = config.trips[0]
    assert (migrated.adults, migrated.auto_cart, migrated.fare.value) == (2, True, "reservation")
    assert json.loads(path.read_text())["version"] == SCHEMA_VERSION


def test_migrates_the_0_4_trips_format(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "interval_seconds": 45,
                "trips": [
                    {
                        "id": "374d7e06",
                        "enabled": True,
                        "date": "2026-10-12",
                        "from": "SWB",
                        "to": "TSA",
                        "adults": 2,
                        "children": 0,
                        "infants": 0,
                        "seniors": 0,
                        "vehicle": "standard",
                        "height_ft": 8.0,
                        "earliest": "09:00",
                        "latest": "14:00",
                        "hold": False,
                        "fare": "cheapest",
                    }
                ],
            }
        )
    )
    config = ConfigStore(path).config
    assert config.interval_seconds == 45
    assert config.trips[0].id == "374d7e06"
    assert config.trips[0].auto_cart is False


def test_an_unreadable_config_is_set_aside(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")
    store = ConfigStore(path)
    assert store.config.trips == ()
    aside = list(tmp_path.glob("config.json.invalid-*"))
    assert len(aside) == 1
    assert aside[0].read_text() == "{not json"
    assert "moved it to" in caplog.text
