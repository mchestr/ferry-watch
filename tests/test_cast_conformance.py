"""The agent against the shared fw.cast1 conformance pack (PROTOCOL.md §10.1): every case meant for the agent.

What cast_protocol.py decides on its own (each frame's outcome, the image header, the text the viewer sends, the
scale factor, the outbound schema, `autocomplete` cleaning and message numbers), and the stream session's parts
the pack covers: the rate limiter and the inbound queue (stream.py), rectangles (cast_fields.frame_rect) and
input mapping (cast_input.InputMapper, with a stand-in CDP session).
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from ferry_watch.agent.cast_cdp import Cdp
from ferry_watch.agent.cast_fields import frame_rect
from ferry_watch.agent.cast_input import ImageInfo, ImageRing, InputMapper, choose_keys, date_chars
from ferry_watch.agent.cast_protocol import (
    MAX_INBOUND_BYTES,
    OVERALL_RATE,
    RATES,
    Answer,
    CloseCode,
    Hello,
    ImageHeader,
    Key,
    ProtocolViolation,
    Text,
    check,
    decode,
    decode_outbound,
    encode,
    message_type,
    numbered,
    scale_factor,
    strip_credential_tokens,
)
from ferry_watch.agent.stream import InboundQueue, RateLimiter
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


# ---- the stream session's own parts


def near(a: Any, b: Any) -> bool:
    """Equal, numbers within the pack's 1e-6."""
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return abs(a - b) <= 1e-6
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(near(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


@pytest.mark.parametrize("case", agent_cases("rects"))
def test_rectangles_in_frame_px(case: dict[str, Any]) -> None:
    origins = [tuple(o) for o in case["origins"]]
    r, c = frame_rect(case["s"], case["ox"], case["oy"], origins, tuple(case["clip"]), tuple(case["client"]))
    assert near(r, case["r"])
    assert (c is None) if case["c"] is None else near(c, case["c"])


@pytest.mark.parametrize("case", agent_cases("messages", "rates"))
def test_the_rate_limiter(case: dict[str, Any]) -> None:
    now = [0.0]
    limiter = RateLimiter(lambda: now[0])
    for step in case["steps"]:
        now[0] = step["at"] / 1000
        message = decode(json.dumps(step["msg"]))
        for _ in range(step["times"]):
            got: str
            if check(message, KNOWN) is not None:
                got = limiter.invalid()
            else:
                got = limiter.admit(message_type(message), len(message.s) if isinstance(message, Text) else 0)
            assert got == step["expect"], step


@pytest.mark.parametrize("case", agent_cases("messages", "coalesce"))
def test_the_inbound_queue(case: dict[str, Any]) -> None:
    queue = InboundQueue()
    for number, waiting in enumerate(case["queue"], 1):
        assert queue.put(decode(json.dumps(waiting)), number)
    arrives = decode(json.dumps(case["arrives"]))
    if message_type(arrives) in ("ack", "visible"):  # applied on arrival: never queued
        assert "after" in case
    else:
        accepted = queue.put(arrives, len(case["queue"]) + 1)
        assert accepted == ("close" not in case)
        if not accepted:
            assert case["close"] == CloseCode.RATE
            return
    assert queue.waiting == [decode(json.dumps(m)) for m in case["after"]]


class InputSession:
    """A Playwright CDPSession onto the front page: its layout metrics, and the Input calls it gets."""

    def __init__(self, live: dict[str, float], after: dict[str, float] | None = None) -> None:
        self.live = live
        self.after = after
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": self.live}
        self.calls.append((method, dict(params or {})))
        if method == "Input.synthesizePinchGesture" and self.after is not None:
            self.live = {**self.live, **self.after}
        return {}

    def on(self, event: str, handler: Any) -> None: ...


def mapper(case: dict[str, Any]) -> tuple[InputMapper, InputSession, list[float]]:
    images = ImageRing()
    for image in case.get("images", []):
        images.add(ImageInfo(**image))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    session = InputSession(case.get("live", {"pageX": 0, "pageY": 0, "scale": 1}), case.get("after"))
    input_mapper = InputMapper(images, sleep=sleep)
    input_mapper.aim(case.get("front", 1), Cdp(cast(Any, session)))
    return input_mapper, session, waits


def made(calls: list[tuple[str, dict[str, Any]]], expected: list[dict[str, Any]]) -> bool:
    """The calls are the expected ones: every listed param there with its value (a missing `modifiers` is 0)."""
    if len(calls) != len(expected):
        return False
    for (method, params), want in zip(calls, expected, strict=True):
        wanted = want.get("params", want)
        if want.get("method", method) != method or params.get("modifiers", 0) != wanted.get("modifiers", 0):
            return False
        if not all(near(params.get(k), v) for k, v in wanted.items()):
            return False
    return True


@pytest.mark.parametrize("case", agent_cases("mapping", "inputs"))
async def test_input_reaches_the_page_where_the_viewer_aimed_it(case: dict[str, Any]) -> None:
    input_mapper, session, waits = mapper(case)
    message = decode(json.dumps(case["msg"]))
    carry_out = {
        "tap": input_mapper.tap,
        "move": input_mapper.move,
        "wheel": input_mapper.wheel,
        "pinch": input_mapper.pinch,
        "text": input_mapper.text,
        "compose": input_mapper.compose,
    }[message_type(message)]
    error = await carry_out(message)  # type: ignore[operator]
    assert error == case.get("error")
    assert made(session.calls, case["calls"]), session.calls
    if "wait_ms" in case:
        assert waits == [case["wait_ms"] / 1000]


@pytest.mark.parametrize("case", agent_cases("mapping", "keys"))
async def test_keys(case: dict[str, Any]) -> None:
    input_mapper, session, _ = mapper(case)
    for message in case["msgs"]:
        key = decode(json.dumps(message))
        assert isinstance(key, Key)
        assert check(key) is None
        await input_mapper.key(key)
    if case.get("teardown"):
        await input_mapper.release()
    assert all(method == "Input.dispatchKeyEvent" for method, _ in session.calls)
    assert made(session.calls, case["events"]), session.calls


@pytest.mark.parametrize("case", agent_cases("mapping", "date_keys"))
def test_date_and_time_characters(case: dict[str, Any]) -> None:
    assert date_chars(case["k"], case["v"]) == case["chars"]


@pytest.mark.parametrize("case", agent_cases("mapping", "choose_keys"))
def test_choose_keys(case: dict[str, Any]) -> None:
    options = [tuple(o) for o in case["opts"]]
    keys = choose_keys(options, case["i"])
    assert keys == case.get("keys")
    assert (keys is None) == ("error" in case)
