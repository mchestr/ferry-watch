from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ferry_watch.bcferries.terminals import Terminals

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 11)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def terminals() -> Terminals:
    return Terminals.from_route_info(json.loads((FIXTURES / "route-info.json").read_text()))


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Pin "today" (Pacific) everywhere it's used, so date rules are deterministic."""
    for module in ("ferry_watch.models", "ferry_watch.watcher", "ferry_watch.views", "ferry_watch.server"):
        monkeypatch.setattr(f"{module}.today", lambda: TODAY)
    return TODAY


pytest_plugins = ["tests.postgres"]
