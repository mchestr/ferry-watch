"""BC Ferries looks and sounds as it did (docs/waitlist/design.md §10, "BC Ferries behaviour unchanged", gates 3 and
4): a trip's dashboard entry and every one of its alerts equal what the contract base produced."""

from __future__ import annotations

import json
from typing import Any

from tests.golden_capture import GOLDEN, TRIP_ID, bcferries_entry_of, bcferries_messages, bcferries_state
from tests.postgres import Database


def golden(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text())


async def test_a_trips_dashboard_entry_is_as_captured(db: Database) -> None:
    state = await bcferries_state(db)
    entry = json.loads(json.dumps(bcferries_entry_of(state)))  # as the API sends it
    assert entry == golden("bcferries-entry.json")
    if "watches" in state:  # what every watch gained, beside 0.7.1's keys
        (watch,) = state["watches"]
        assert (watch["provider"], watch["id"], watch["enabled"]) == ("bcferries", TRIP_ID, True)
        assert watch["trip"]["id"] == watch["id"]
        assert watch["trip"]["enabled"] == watch["enabled"]


def test_every_bc_ferries_alert_is_as_captured() -> None:
    assert json.loads(json.dumps(bcferries_messages())) == golden("bcferries-messages.json")
