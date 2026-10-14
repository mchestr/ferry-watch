from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ferry_watch.control.config import ConfigError, duration, load
from ferry_watch.control.configure import Allowance, Switches, cart_gate
from ferry_watch.models import PACIFIC, Trip

MINIMAL: dict[str, Any] = {
    "pod": {
        "image": "ghcr.io/mchestr/ferry-watch-browser@sha256:" + "b" * 64,
        "hubUrl": "ws://ferry-control.ferry-watch.svc.cluster.local.:8766/agent/v1",
        "vncPubkeys": "k1:AAAA",
        "vncOrigin": "https://ferry-checkout.chestr.dev",
    }
}


def write(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_defaults_are_the_documented_ones(tmp_path: Path) -> None:
    config = load(write(tmp_path, MINIMAL))
    settings = config.hub_settings()
    assert settings.checks_per_minute == 20
    assert (settings.hold.unclaimed_s, settings.hold.hard_s) == (1800, 3600)
    assert settings.policy.warm_window == timedelta(days=7)
    assert settings.policy.short_check_interval == timedelta(hours=6)
    assert config.user_limits().value("maxTrips", {}) == 3
    assert config.fleet.max_browsers_total == 7
    assert config.pod_params().namespace == "ferry-watch-browsers"


def test_values_override(tmp_path: Path) -> None:
    config = load(
        write(
            tmp_path,
            MINIMAL
            | {
                "polling": {
                    "tiers": [{"within": "12h", "interval": "60s"}, {"within": "5d", "interval": "10m"}],
                    "shortCheckInterval": "12h",
                },
                "limits": {"defaults": {"maxTrips": 5}},
                "hold": {"hard": "90m"},
                "checksPerMinute": 40,
            },
        )
    )
    policy = config.policy()
    assert policy.warm_window == timedelta(days=5)
    assert policy.interval(timedelta(hours=1)) == timedelta(seconds=60)
    assert config.user_limits().value("maxTrips", {}) == 5
    assert config.hold_limits().hard_s == 5400


def test_the_old_fleet_wide_quiet_hours_still_parse(tmp_path: Path) -> None:
    """Quiet hours are each user's own now: a deployment that still sets polling.quietStart/quietEnd starts,
    and they change nothing (a user's quiet hours end at 5 am unless they change them)."""
    config = load(write(tmp_path, MINIMAL | {"polling": {"quietStart": "22:00", "quietEnd": "07:00"}}))
    trip = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 16))
    early = datetime(2026, 10, 15, 6, 30, tzinfo=PACIFIC)
    assert cart_gate(trip, Allowance(4, 6, 60), Switches(), config.hub_settings(), early) == "ok"


@pytest.mark.parametrize(
    "change",
    [
        {"hold": {"hard": "2h"}},  # past the compiled 90 min
        {"polling": {"tiers": [{"within": "6h", "interval": "10s"}]}},  # under the 30 s floor
        {"limits": {"defaults": {"maxTrips": 99}}},  # past its ceiling
        {"limits": {"defaults": {"maxEverything": 1}}},
        {"checksPerMinute": 41},
        {"surprise": True},
        {"pod": MINIMAL["pod"] | {"image": "ghcr.io/mchestr/ferry-watch-browser:latest"}},
    ],
)
def test_bad_values_stop_startup(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | change))


def test_durations() -> None:
    assert duration("30s") == timedelta(seconds=30)
    assert duration("7d") == timedelta(days=7)
    with pytest.raises(ConfigError):
        duration("soon")
