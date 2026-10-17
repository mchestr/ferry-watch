"""The agent protocol is frozen (docs/waitlist/design.md §10, "BC Ferries behaviour unchanged", gate 1): agents N-1
must keep working, so its schemas equal the ones captured at the contract base."""

from __future__ import annotations

import json

from tests.golden_capture import GOLDEN, protocol_schemas


def test_the_agent_protocol_is_as_captured() -> None:
    assert json.loads((GOLDEN / "agent-protocol.json").read_text()) == protocol_schemas()
