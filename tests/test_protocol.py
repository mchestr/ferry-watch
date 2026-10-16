from __future__ import annotations

import json
from datetime import date
from typing import Any, get_args

import msgspec
import pytest

from ferry_watch import protocol
from ferry_watch.bcferries import errors
from ferry_watch.models import FarePreference, Trip, Vehicle
from ferry_watch.protocol import (
    MAX_FRAME_BYTES,
    Ack,
    Alert,
    AlertParams,
    Bye,
    CartClaim,
    CartClaimResult,
    CartInfo,
    Command,
    Configure,
    Event,
    Hello,
    HelloAck,
    HoldLimits,
    ProtocolError,
    SailingInfo,
    State,
    TripSpec,
    decode_agent_message,
    decode_control_message,
    encode,
    error_class,
)

ENVELOPE: dict[str, Any] = {"seq": 3, "trip": "3f9c0a1b2c3d4e5f", "boot": "a1b2c3d4e5f60718", "rev": 2}
TRIP = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 14))
HOLD = HoldLimits(unclaimed_s=1800, session_s=1800, viewer_gone_s=600, hard_s=3600)


def state(**kw: Any) -> State:
    fields: dict[str, Any] = {"phase": "watching", "lockdown_ok": True}
    return State(**ENVELOPE, **(fields | kw))


def configure(**kw: Any) -> Configure:
    fields: dict[str, Any] = {
        "trip_spec": msgspec.convert(TRIP.to_json(), TripSpec),
        "active": True,
        "interval_s": 300,
        "auto_cart": True,
        "hold": HOLD,
    }
    return Configure(**ENVELOPE, **(fields | kw))


def frame(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode()


# ---- round trips


def test_agent_messages_round_trip() -> None:
    held = state(
        phase="holding",
        sailings=[SailingInfo(depart="07:00", arrive="08:35", vessel="Spirit of BC", status="available")],
        cart=CartInfo(id="ab12cd34ef56", status="ready", depart="07:00", fare="Saver", price="$93.45"),
        alerted=["07:00"],
        offered=["07:00"],
    )
    messages: list[protocol.AgentMessage] = [
        held,
        Event(**ENVELOPE, code="cart_added"),
        Alert(**ENVELOPE, kind="cart_held", params=AlertParams(departures=["07:00"], fare="Saver")),
        Bye(**ENVELOPE, reason="hold_ended", outcome="booked", cart_emptied=True, release_check="cart_empty"),
        Bye(**ENVELOPE, reason="short_check_done"),
        CartClaim(**ENVELOPE, depart="07:00", fare="saver"),
        Hello(
            **ENVELOPE,
            proto=[1],
            agent_version="0.6.0",
            pod_name="fw-3f9c0a1b2c3d4e5f-g7",
            gen=7,
            applied_rev=2,
            snapshot=held,
            unacked=[Event(**ENVELOPE, code="viewer_connected")],
        ),
    ]
    for message in messages:
        assert decode_agent_message(encode(message)) == message


def test_control_messages_round_trip() -> None:
    messages: list[protocol.ControlMessage] = [
        HelloAck(**ENVELOPE, server_time=1.0, accepted_proto=1, resend_from=0),
        configure(),
        configure(mode="short_check"),
        CartClaimResult(**ENVELOPE, claim=4, granted=False, reason="holding_elsewhere"),
        Command(**ENVELOPE, id=1, op="release", cart_id="ab12cd34ef56"),
        Command(**ENVELOPE, id=2, op="drain", reason="deleted"),
        Command(**ENVELOPE, id=3, op="check_now"),
        Ack(**ENVELOPE, acked=9),
    ]
    for message in messages:
        assert decode_control_message(encode(message)) == message


def test_the_wire_names_match_the_documented_shape() -> None:
    wire = json.loads(encode(configure()))
    assert wire["type"] == "configure"
    assert wire["v"] == 1
    assert wire["trip_spec"]["from"] == "SWB"
    assert wire["mode"] == "warm"
    assert wire["checkout_mode"] == "guest"
    error = json.loads(encode(state(error=protocol.ErrorInfo(kind="queue_timeout", text="Still queued"))))
    assert error["error"] == {"class": "queue_timeout", "text": "Still queued"}


def test_a_trip_spec_round_trips_through_the_models() -> None:
    spec = configure().trip_spec
    assert Trip.from_json(msgspec.to_builtins(spec)) == TRIP


def test_literals_match_the_model_enums() -> None:
    assert set(get_args(protocol.Vehicle)) == {v.value for v in Vehicle}
    assert set(get_args(protocol.FarePreference)) == {f.value for f in FarePreference}


# ---- refusals


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://evil.example/"},  # unknown field
        {"interval_s": 15},  # below the compiled floor
        {"hold": {"unclaimed_s": 1800, "session_s": 1800, "viewer_gone_s": 600, "hard_s": 0}},  # "forever"
        {"hold": {"unclaimed_s": 1800, "session_s": 1800, "viewer_gone_s": 600, "hard_s": 99999}},
        {"checkout_mode": "account"},  # not defined in v1
        {"trip": "../etc"},
        {"v": 2},
    ],
)
def test_configure_outside_the_contract_is_refused(change: dict[str, Any]) -> None:
    good = json.loads(encode(configure()))
    with pytest.raises(ProtocolError):
        decode_control_message(frame(good | change))


def test_the_streamed_viewer_is_off_unless_the_operator_says_so() -> None:
    assert configure().cast_allowed is False
    shaped_064 = json.loads(encode(configure()))
    del shaped_064["cast_allowed"]  # what a 0.6.4 ferry-control sends
    decoded = decode_control_message(frame(shaped_064))
    assert isinstance(decoded, Configure)
    assert decoded.cast_allowed is False
    allowed = decode_control_message(encode(configure(cast_allowed=True)))
    assert isinstance(allowed, Configure)
    assert allowed.cast_allowed is True


def test_the_trip_spec_is_strict() -> None:
    good = json.loads(encode(configure()))
    good["trip_spec"]["base_url"] = "https://evil.example/"
    with pytest.raises(ProtocolError):
        decode_control_message(frame(good))


@pytest.mark.parametrize(
    "command",
    [
        {"op": "release"},  # which cart?
        {"op": "check_now", "cart_id": "ab12cd34ef56"},
        {"op": "drain"},  # why?
        {"op": "navigate", "reason": "deleted"},
        {"op": "eval"},
    ],
)
def test_commands_outside_the_contract_are_refused(command: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError):
        decode_control_message(frame({"type": "command", **ENVELOPE, "id": 1, **command}))


def test_bounded_lists_and_strings() -> None:
    sailing = {"depart": "07:00", "arrive": "08:35", "vessel": "x" * 65, "status": "available"}
    with pytest.raises(ProtocolError):
        decode_agent_message(frame({"type": "state", **ENVELOPE, "phase": "watching", "sailings": [sailing]}))
    too_many = [{"depart": "07:00", "arrive": "08:35", "vessel": "v", "status": "available"}] * 61
    with pytest.raises(ProtocolError):
        decode_agent_message(frame({"type": "state", **ENVELOPE, "phase": "watching", "sailings": too_many}))


def test_an_agent_cannot_send_operator_messages_or_vice_versa() -> None:
    with pytest.raises(ProtocolError):
        decode_agent_message(encode(configure()))
    with pytest.raises(ProtocolError):
        decode_control_message(encode(state()))


def test_oversized_and_malformed_frames_are_refused() -> None:
    with pytest.raises(ProtocolError, match="limit"):
        decode_agent_message(b"x" * (MAX_FRAME_BYTES + 1))
    with pytest.raises(ProtocolError):
        decode_agent_message(b"{not json")


# ---- the vocabulary stays closed


FORBIDDEN = {"url", "href", "base_url", "script", "js", "eval", "selector", "cookie", "cookies", "storage"}
FORBIDDEN |= {"screenshot", "html", "navigate", "credentials", "password", "headers", "proxy", "flags"}


def _property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names |= set(value)
            names |= _property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= _property_names(item)
    return names


def test_no_field_can_carry_page_control_or_secrets() -> None:
    (_, components) = msgspec.json.schema_components([protocol.AgentMessage, protocol.ControlMessage])
    names = _property_names(components)
    assert names, "the schema walk found nothing"
    assert not names & FORBIDDEN
    assert not [n for n in names if "url" in n or "cookie" in n]


# ---- error classes


def test_every_booking_error_has_a_class() -> None:
    for kind in (getattr(errors, name) for name in dir(errors)):
        if isinstance(kind, type) and issubclass(kind, errors.BookingError) and kind is not errors.BookingError:
            assert error_class(kind("x")) not in ("booking", "unexpected"), kind
    assert error_class(errors.BookingError("x")) == "booking"
    assert error_class(TimeoutError()) == "timeout"
    assert error_class(KeyError("x")) == "unexpected"
