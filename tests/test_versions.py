"""Reading an agent's version: what ferry-web and ferry-control offer a pod depends on it."""

from __future__ import annotations

import pytest

from ferry_watch.versions import STREAM_VIEWER, at_least


@pytest.mark.parametrize(
    "version",
    [
        *("0.7.0", "0.7.1", "0.8.0", "1.0.0", "0.10.0", "1", "0.7", "0.7.0.0", "0.7.0.1"),
        *("0.7.0.post1", "0.7.0.post1.dev2", "0.7.0+local", "0.7.1rc1", "0.8.0a1", "0.8.0.dev3+g1a2b3c4"),
    ],
)
def test_releases_from_070_on_stream(version: str) -> None:
    assert at_least(version, STREAM_VIEWER)


@pytest.mark.parametrize(
    "version",
    [
        *("0.6.4", "0.6.10", "0.6.99", "0.1.0", "0.0.0", "0.7.0rc1", "0.7.0a1", "0.7.0b2", "0.7.0.dev3"),
        "0.7.0rc1.post1",
        # Not a version as the package metadata has it: treated as too old, so the pod gets what every agent takes.
        *("", "garbage", "v0.7.0", "0.7.0 ", " 0.7.0", "0.7.0-rc1", "0.7.x", "0.7.0+", "0." * 10 + "7", "1" * 7),
    ],
)
def test_older_or_unreadable_versions_dont(version: str) -> None:
    assert not at_least(version, STREAM_VIEWER)


def test_no_version_doesnt() -> None:
    assert not at_least(None, STREAM_VIEWER)  # a pod that hasn't said hello yet
    assert at_least("0.6.4", (0, 6, 4))
    assert not at_least("0.6.3", (0, 6, 4))
