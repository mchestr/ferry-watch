from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from waitlist.control.config import ConfigError, duration, load
from waitlist.control.configure import Allowance, Switches, cart_gate
from waitlist.models import PACIFIC, Trip

MINIMAL: dict[str, Any] = {
    "pod": {
        "image": "ghcr.io/mchestr/waitlist-browser@sha256:" + "b" * 64,
        "hubUrl": "ws://waitlist-control.waitlist.svc.cluster.local.:8766/agent/v1",
        "vncPubkeys": "k1:AAAA",
        "vncOrigin": "https://waitlist-checkout.chestr.dev",
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
    assert config.pod_params().namespace == "waitlist-browsers"
    assert settings.stream_viewer == "off"  # the streamed viewer's kill switch is on until a deployment says


@pytest.mark.parametrize("value", ["off", "admins", "everyone"])
def test_who_may_use_the_streamed_viewer(tmp_path: Path, value: str) -> None:
    config = load(write(tmp_path, MINIMAL | {"checkout": {"streamViewer": value}}))
    assert config.hub_settings().stream_viewer == value


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
        # Only the three values; YAML reads a bare `off` as false, which must stop startup, not mean anything.
        *({"checkout": {"streamViewer": value}} for value in ("on", "Admins", False, None)),
        {"checkout": {"streamViewer": "off", "origin": "https://waitlist-checkout.chestr.dev"}},  # waitlist-web's key
        {"pod": MINIMAL["pod"] | {"image": "ghcr.io/mchestr/waitlist-browser:latest"}},
    ],
)
def test_bad_values_stop_startup(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | change))


FETCHER = {"url": "http://waitlist-fetcher.waitlist.svc.cluster.local.:8767", "timeoutSeconds": 20}


def test_http_checks_are_off_by_default(tmp_path: Path) -> None:
    config = load(write(tmp_path, MINIMAL))
    assert config.fetcher is None
    assert config.http_checks == {}
    assert config.http_rates() == {}  # the check loop doesn't start; BC Ferries runs as before


def test_http_checks_parse_as_rendered(tmp_path: Path) -> None:
    config = load(write(tmp_path, MINIMAL | {"fetcher": FETCHER, "httpChecks": {"buntzen": {"requestsPerMinute": 12}}}))
    assert config.fetcher is not None
    assert (config.fetcher.url, config.fetcher.timeout_seconds) == (FETCHER["url"], 20)
    assert config.http_rates() == {"buntzen": 12}


def test_a_provider_without_a_rate_gets_its_budgets_default(tmp_path: Path) -> None:
    config = load(write(tmp_path, MINIMAL | {"fetcher": {"url": "http://fetcher:8767"}, "httpChecks": {"buntzen": {}}}))
    assert config.http_rates() == {"buntzen": 12}  # buntzen.meta.BUDGET.default_per_minute
    assert config.fetcher is not None
    assert config.fetcher.timeout_seconds == 20


def test_a_fetcher_alone_checks_nothing(tmp_path: Path) -> None:
    assert load(write(tmp_path, MINIMAL | {"fetcher": FETCHER})).http_rates() == {}


@pytest.mark.parametrize(
    "change",
    [
        {"fetcher": FETCHER, "httpChecks": {"bcferries": {}}},  # browser-checked: not here
        {"fetcher": FETCHER, "httpChecks": {"bcparks": {}}},  # not a provider this build knows
        {"fetcher": FETCHER, "httpChecks": {"buntzen": {"requestsPerMinute": 31}}},  # past Buntzen's ceiling (30)
        {"fetcher": FETCHER, "httpChecks": {"buntzen": {"requestsPerMinute": 0}}},
        {"fetcher": FETCHER, "httpChecks": {"buntzen": {"requestsPerMinute": 61}}},
        {"httpChecks": {"buntzen": {}}},  # without a fetcher to run them
        {"fetcher": {"url": "http://fetcher:8767/v1"}},  # an origin only
        {"fetcher": {"url": "ftp://fetcher"}},
        {"fetcher": {"url": "http://Fetcher:8767"}},
        {"fetcher": FETCHER | {"timeoutSeconds": 61}},
        {"fetcher": FETCHER | {"surprise": 1}},
        {"fetcher": FETCHER, "httpChecks": {"buntzen": {"requestsPerMinute": 12, "maxPerMinute": 30}}},
    ],
)
def test_bad_http_checks_stop_startup(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | change))


def test_alert_only_watches_are_a_limit(tmp_path: Path) -> None:
    config = load(write(tmp_path, MINIMAL | {"limits": {"defaults": {"maxAlertOnlyWatches": 4}}}))
    assert config.user_limits().value("maxAlertOnlyWatches", {}) == 4
    assert load(write(tmp_path, MINIMAL)).user_limits().value("maxAlertOnlyWatches", {}) == 6
    assert load(write(tmp_path, MINIMAL)).user_limits().value("maxAlertOnlyWatches", {"maxAlertOnlyWatches": 99}) == 20
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | {"limits": {"defaults": {"maxAlertOnlyWatches": 21}}}))


def test_durations() -> None:
    assert duration("30s") == timedelta(seconds=30)
    assert duration("7d") == timedelta(days=7)
    with pytest.raises(ConfigError):
        duration("soon")
