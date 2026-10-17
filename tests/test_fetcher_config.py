from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from waitlist.fetcher.config import ConfigError, FetcherConfig, load

# As the deployment renders it (design.md §4.4, §8.2).
RENDERED: dict[str, Any] = {
    "listenPort": 8767,
    "upstreamTimeoutSeconds": 10,
    "maxBodyBytes": 262144,
    "providers": {"buntzen": {"maxPerMinute": 30}},
}


def write(tmp_path: Path, config: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_the_rendered_config_parses_in_camel_case(tmp_path: Path) -> None:
    config = load(write(tmp_path, RENDERED))
    assert config.listen_port == 8767
    assert config.upstream_timeout_seconds == 10
    assert config.max_body_bytes == 262_144
    assert config.providers["buntzen"].max_per_minute == 30


def test_defaults_and_providers_off(tmp_path: Path) -> None:
    config = load(write(tmp_path, {}))
    assert config == FetcherConfig()
    assert (config.listen_port, config.upstream_timeout_seconds, config.max_body_bytes) == (8767, 10, 262_144)
    assert config.providers == {}  # "off": every request is refused as unexpected
    assert load(write(tmp_path, {**RENDERED, "providers": {}})).providers == {}


@pytest.mark.parametrize(
    "change",
    [
        {"upstreamTimeoutSeconds": 0},
        {"upstreamTimeoutSeconds": 31},
        {"upstreamTimeoutSeconds": "10"},
        {"maxBodyBytes": 1023},
        {"maxBodyBytes": 1_048_577},
        {"listenPort": 0},
        {"listenPort": 65536},
        {"listenPort": True},
        {"providers": {"buntzen": {"maxPerMinute": 0}}},
        {"providers": {"buntzen": {"maxPerMinute": 61}}},
        {"providers": {"buntzen": {}}},
        {"providers": {"buntzen": {"maxPerMinute": 10, "burst": 3}}},
        {"providers": []},
        {"listen_port": 8767},  # snake case is an unknown key
        {"secret": "x"},
    ],
)
def test_bad_values_are_refused(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, {**RENDERED, **change}))


@pytest.mark.parametrize("provider", ["bcferries", "nope", "BUNTZEN"])
def test_a_provider_the_fetcher_cant_check_is_refused(tmp_path: Path, provider: str) -> None:
    with pytest.raises(ConfigError, match="not a provider waitlist-fetcher can check"):
        load(write(tmp_path, {"providers": {provider: {"maxPerMinute": 10}}}))


def test_a_cap_above_the_providers_ceiling_is_refused(tmp_path: Path) -> None:
    # Buntzen's budget.max_per_minute is 30; the struct alone would allow up to 60.
    assert (
        load(write(tmp_path, {"providers": {"buntzen": {"maxPerMinute": 30}}})).providers["buntzen"].max_per_minute
        == 30
    )
    with pytest.raises(ConfigError, match="ceiling is 30"):
        load(write(tmp_path, {"providers": {"buntzen": {"maxPerMinute": 31}}}))


def test_unreadable_files_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing.json"):
        load(tmp_path / "missing.json")
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        load(path)
    with pytest.raises(ConfigError):
        load(write(tmp_path, [RENDERED]))
