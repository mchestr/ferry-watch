"""The user's configuration: held in memory, validated on every change, persisted atomically."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .fileio import write_json_atomic
from .models import MAX_TRIPS, Config, Trip, ValidationError

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


class TripNotFound(LookupError):
    pass


def config_to_json(config: Config) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "enabled": config.enabled,
        "interval_seconds": config.interval_seconds,
        "trips": [t.to_json() for t in config.trips],
    }


def config_from_json(data: dict[str, Any]) -> Config:
    """Parse (and migrate) a stored config. Raises ValidationError if it can't be used."""
    if not isinstance(data, dict):
        raise ValidationError("config", "Expected a JSON object")
    version = data.get("version", 1 if "trips" not in data else 2)
    if version == 1:
        data = _migrate_v1(data)
    elif version != SCHEMA_VERSION:
        raise ValidationError("version", f"Unsupported config version {version}")

    trips = tuple(Trip.from_json(_migrate_trip(t)) for t in data.get("trips", []))
    if len({t.id for t in trips}) != len(trips):
        raise ValidationError("trips", "Duplicate trip ids")
    base = Config(trips=trips)
    settings = {k: data[k] for k in ("enabled", "interval_seconds") if k in data}
    return base.with_settings(settings)


def _migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    """v1 (0.1 to 0.3) stored one trip at the top level."""
    trip = {k: v for k, v in data.items() if k not in ("enabled", "interval_seconds", "interval_minutes")}
    return {
        "enabled": data.get("enabled", True),
        "interval_seconds": data.get("interval_seconds", 30),
        "trips": [trip] if trip else [],
    }


def _migrate_trip(trip: dict[str, Any]) -> dict[str, Any]:
    """Trip fields renamed since 0.4: `hold` became `auto_cart`."""
    trip = dict(trip)
    if "hold" in trip:
        trip.setdefault("auto_cart", trip.pop("hold"))
    return trip


class ConfigStore:
    """Owns config.json. Only touched from the event loop thread, so it needs no locking."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._listeners: list[Callable[[], None]] = []
        self._config = self._load()

    @property
    def config(self) -> Config:
        return self._config

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Call `listener` after every change."""
        self._listeners.append(listener)

    def update_settings(self, data: dict[str, Any]) -> Config:
        return self._commit(self._config.with_settings(data))

    def add_trip(self, trip: Trip) -> Trip:
        if len(self._config.trips) >= MAX_TRIPS:
            raise ValidationError("trips", f"Up to {MAX_TRIPS} trips can be watched at once")
        if self._config.trip(trip.id) is not None:
            raise ValidationError("id", "A trip with this id already exists")
        self._commit(replace(self._config, trips=(*self._config.trips, trip)))
        return trip

    def replace_trip(self, trip: Trip) -> Trip:
        self.require(trip.id)
        self._commit(replace(self._config, trips=tuple(trip if t.id == trip.id else t for t in self._config.trips)))
        return trip

    def delete_trip(self, trip_id: str) -> Trip:
        trip = self.require(trip_id)
        self._commit(replace(self._config, trips=tuple(t for t in self._config.trips if t.id != trip_id)))
        return trip

    def require(self, trip_id: str) -> Trip:
        trip = self._config.trip(trip_id)
        if trip is None:
            raise TripNotFound(trip_id)
        return trip

    def _commit(self, config: Config) -> Config:
        self._write(config)
        self._config = config
        for listener in self._listeners:
            listener()
        return config

    def _load(self) -> Config:
        if not self._path.exists():
            config = Config()
            self._write(config)
            return config
        try:
            raw = json.loads(self._path.read_text())
            config = config_from_json(raw)
        except (ValueError, ValidationError):
            # Don't crash-loop over a bad file: keep it for inspection and start empty.
            aside = self._path.with_name(f"{self._path.name}.invalid-{int(time.time())}")
            self._path.replace(aside)
            log.exception("Couldn't read %s; moved it to %s and started with no trips", self._path, aside)
            config = Config()
            self._write(config)
            return config
        if raw.get("version") != SCHEMA_VERSION:
            log.info("Migrated %s to config version %d", self._path, SCHEMA_VERSION)
            self._write(config)
        return config

    def _write(self, config: Config) -> None:
        write_json_atomic(self._path, config_to_json(config))
