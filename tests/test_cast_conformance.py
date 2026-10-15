"""The agent against the shared fw.cast1 conformance pack (PROTOCOL.md §10.1): every case meant for the agent.

The stream session's own parts (rates, the inbound queue, rectangles, input mapping) are tested where they're
built; this file covers what cast_protocol.py decides on its own: each frame's outcome, the image header, the
text the viewer sends, the scale factor, the outbound schema, `autocomplete` cleaning and message numbers.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ferry_watch.agent.cast_protocol import (
    MAX_INBOUND_BYTES,
    OVERALL_RATE,
    RATES,
    Answer,
    CloseCode,
    Hello,
    ImageHeader,
    ProtocolViolation,
    check,
    decode,
    decode_outbound,
    encode,
    message_type,
    numbered,
    scale_factor,
    strip_credential_tokens,
)
from tests.cast1 import PackKnown, agent_cases, load

MESSAGES = load("messages")
KNOWN = PackKnown.from_context(MESSAGES["context"])


def frame(case: dict[str, Any]) -> str | bytes:
    """The frame a case describes: text as a str, binary as bytes."""
    if "hex" in case:
        return bytes.fromhex(case["hex"])
    if "raw" in case:
        text: str = case["raw"]
        if "pad_to" in case:
            text += " " * (case["pad_to"] - len(text.encode("utf-8", "surrogatepass")))
        return text
    return json.dumps(case["msg"])


def outcome(data: str | bytes, state: str) -> tuple[str, int | None]:
    """What the agent does with one frame arriving in a state (§3.2, §6): the session's rules, in order."""
    if isinstance(data, bytes):
        return "close", CloseCode.PROTOCOL  # text frames only
    if len(data.encode("utf-8", "surrogatepass")) > MAX_INBOUND_BYTES:
        return "close", 1009  # aiohttp's limit on the gate's socket
    try:
        message = decode(data)
    except ProtocolViolation as e:
        return "close", e.code
    if state == "open":  # the first message must be a good hello
        good = isinstance(message, Hello) and check(message) is None
        return ("ok", None) if good else ("close", CloseCode.PROTOCOL)
    if state == "hello" or isinstance(message, Hello):  # nothing before ready; no second hello
        return "close", CloseCode.PROTOCOL
    return ("ok", None) if check(message, KNOWN) is None else ("invalid", None)


@pytest.mark.parametrize("case", agent_cases("messages"))
def test_each_frame_gets_its_outcome(case: dict[str, Any]) -> None:
    assert outcome(frame(case), case.get("state", "ready")) == (case["outcome"], case.get("code"))


@pytest.mark.parametrize("case", agent_cases("headers"))
def test_image_headers_pack_and_unpack(case: dict[str, Any]) -> None:
    header = ImageHeader(**case["fields"])
    assert header.pack().hex() == case["header"]
    assert ImageHeader.unpack(bytes.fromhex(case["header"] + case["jpeg"])) == header
    assert header.first == case["first"]


@pytest.mark.parametrize("case", agent_cases("text"))
def test_what_the_viewer_sends_for_any_text_passes_the_value_rules(case: dict[str, Any]) -> None:
    sent = case.get("messages", [])
    if case["as"] == "dialog":
        sent = [{"t": "dialog", "id": 1, "ok": True, "s": case["s"]}]
        assert isinstance(decode(json.dumps(sent[0])), Answer)
    for message in sent:
        assert check(decode(json.dumps(message))) is None, message


@pytest.mark.parametrize("case", agent_cases("messages", "dsf"))
def test_the_scale_factor(case: dict[str, Any]) -> None:
    hello = decode(json.dumps(case["hello"]))
    assert isinstance(hello, Hello)
    assert check(hello) is None
    assert scale_factor(hello.w, hello.h, hello.dpr) == case["dsf"]


@pytest.mark.parametrize("case", agent_cases("messages", "outbound"))
def test_the_outbound_schema_takes_every_well_formed_message_as_it_is(case: dict[str, Any]) -> None:
    message = decode_outbound(json.dumps(case["msg"]))
    assert json.loads(encode(message)) == case["msg"]


@pytest.mark.parametrize("case", agent_cases("messages", "autocomplete"))
def test_autocomplete_cleaning(case: dict[str, Any]) -> None:
    assert strip_credential_tokens(case["input"]) == case["ac"]


@pytest.mark.parametrize("case", agent_cases("usable", "numbering"))
def test_message_numbers(case: dict[str, Any]) -> None:
    numbers: list[int | None] = []
    n = 0
    for t in case["sent"]:
        if numbered(t):
            n += 1
            numbers.append(n)
        else:
            numbers.append(None)
    assert numbers == case["numbers"]


def test_the_rate_table() -> None:
    table = dict(MESSAGES["rate_table"])
    overall = table.pop("*")
    assert (OVERALL_RATE.rate, OVERALL_RATE.burst) == (overall["rate"], overall["burst"])
    assert set(RATES) == set(table)
    for t, rate in RATES.items():
        expected = table[t]
        assert (rate.rate, rate.burst) == (expected["rate"], expected["burst"]), t
        assert (rate.cp_rate, rate.cp_burst) == (expected.get("cp_rate", 0), expected.get("cp_burst", 0)), t


def test_every_inbound_type_is_in_the_rate_table() -> None:
    seen = {message_type(decode(frame(c))) for c in MESSAGES["cases"] if c["outcome"] == "ok"}
    assert seen - {"hello"} == set(RATES)
