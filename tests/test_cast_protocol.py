"""cast_protocol.py beyond the conformance pack: the decoder under mutated input, the text rules, and the caps and
cleaning that page-derived strings get before they leave the pod."""

from __future__ import annotations

import json
import logging
import random

import msgspec
import pytest

from tests.cast1 import load
from waitlist.agent.cast_protocol import (
    MAX_DIALOG_MESSAGE,
    MAX_OUTBOUND_BYTES,
    MAX_PROMPT_DEFAULT,
    Answer,
    Clock,
    Compose,
    Field,
    Fields,
    Key,
    PageInfo,
    Pages,
    ProtocolViolation,
    Ready,
    Text,
    check,
    clean,
    clean_text_ok,
    decode,
    dialog_message,
    encode,
    frame_origin,
    origin_of,
    strip_credential_tokens,
    typed_text_ok,
)

CASES = load("messages")["cases"]
CORPUS = [c["raw"] if "raw" in c else json.dumps(c["msg"]) for c in CASES if "hex" not in c and "pad_to" not in c]
ALPHABET = '{}[]:,"\\tsxyabc0123456789.-eEnulr \u00e9\u20ac\U0001f600'


def mutate(rng: random.Random, text: str) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, 6)):
        op, at = rng.random(), rng.randrange(len(chars) + 1)
        if op < 0.4:
            chars.insert(at, rng.choice(ALPHABET))
        elif chars and op < 0.8:
            chars.pop(min(at, len(chars) - 1))
        elif chars:
            chars[min(at, len(chars) - 1)] = rng.choice(ALPHABET)
    return "".join(chars)


def test_mutated_frames_only_ever_decode_or_violate_the_protocol() -> None:
    """50,000 mutations of every message in the pack: a message whose rules can be checked, or a 4400."""
    rng = random.Random(7)  # noqa: S311 - test data, not security
    decoded, codes = 0, set()
    for _ in range(50_000):
        data = mutate(rng, rng.choice(CORPUS))
        try:
            message = decode(data)
        except ProtocolViolation as e:
            codes.add(e.code)
            continue
        decoded += 1
        reason = check(message)
        assert reason is None or isinstance(reason, str)
    assert codes == {4400}
    assert decoded > 1000  # the mutations reach the value rules too


def test_structural_errors_name_nothing_the_viewer_sent() -> None:
    for frame in (
        '{"t": "text", "4242 4242 4242 4242": 1}',
        '{"t": "4242 4242 4242 4242"}',
        '{"t": "text", "s": 4242}',
    ):
        with pytest.raises(ProtocolViolation) as e:
            decode(frame)
        assert "4242" not in str(e.value)


def test_typed_and_clean_text() -> None:
    assert typed_text_ok("Émilie O'Brien\tline\nbreak \u00a0\u2028 😀")
    assert not typed_text_ok("a\rb")
    assert all(
        not typed_text_ok(f"a{chr(cp)}b")
        for cp in [*range(0x20), 0x7F, *range(0x80, 0xA0), 0xD800]
        if cp not in (9, 10)
    )
    assert clean_text_ok("Nguyễn 4242 😀")
    assert not clean_text_ok("a\tb")
    assert not clean_text_ok("a\nb")


def test_typed_text_never_shows_in_a_repr_or_a_log(caplog: pytest.LogCaptureFixture) -> None:
    secret = "4242 4242 4242 4242"
    messages = [
        decode(json.dumps({"t": "text", "s": secret})),
        decode(json.dumps({"t": "compose", "s": secret[:4]})),
        decode(json.dumps({"t": "key", "c": "4"})),
        decode(json.dumps({"t": "dialog", "id": 1, "ok": True, "s": secret})),
        decode(json.dumps({"t": "text", "s": "\n" + secret})),  # breaks no rule: just to be sure of the repr
    ]
    assert [type(m) for m in messages] == [Text, Compose, Key, Answer, Text]
    with caplog.at_level(logging.DEBUG):
        for message in messages:
            logging.getLogger("test").info("carried out %r (%s)", message, message)
    assert "4242" not in caplog.text
    assert "4" not in repr(messages[2])


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://www.bcferries.com/checkout?x=1#y", "https://www.bcferries.com"),
        ("http://pay.test:8002/card.html", "http://pay.test:8002"),
        ("https://pay.example:443/", "https://pay.example"),
        ("http://pay.example:80/", "http://pay.example"),
        ("https://user:secret@pay.example/", "https://pay.example"),
        ("https://[::1]:8443/x", "https://[::1]:8443"),
        ("https://XN--BCFRRIES-9ZA.example/", "https://xn--bcfrries-9za.example"),
        ("about:blank", ""),
        ("data:text/html,<b>hi</b>", ""),
        ("javascript:alert(1)", ""),
        ("blob:https://pay.example/0d2c", ""),
        ("https://pay.example:99999/", ""),
        ("", ""),
        ("https://www.bcferries.com." + "a" * 100 + ".example/", ""),  # too long: never cut into another origin
    ],
)
def test_origins(url: str, origin: str) -> None:
    assert origin_of(url) == origin


def test_page_text_is_cleaned_and_capped() -> None:
    assert clean("Pay\x00 now\x85\u202e!", 80) == "Pay now\u202e!"  # only controls go: what's left is text
    assert clean("a" * 100, 80) == "a" * 80
    assert clean("line 1\nline 2\r\n", 500, keep_lf=True) == "line 1\nline 2\n"
    assert clean("line 1\nline 2", 500) == "line 1line 2"
    assert clean("\x01" * 20_000_000 + "late", 80) == ""  # a huge string costs no more than a short one


def test_dialogs_carry_their_frames_origin_and_bounded_text() -> None:
    url = "http://pay.test:8002/card.html"
    message = dialog_message(3, 1, "prompt", url, False, "Code?\n" + "x" * 1000, "D" * 20_000)
    assert message.origin == "http://pay.test:8002"
    assert not message.main
    assert len(message.msg) == MAX_DIALOG_MESSAGE
    assert message.msg.startswith("Code?\n")
    assert message.default == "D" * MAX_PROMPT_DEFAULT
    assert json.loads(encode(message))["def"] == "D" * MAX_PROMPT_DEFAULT
    assert json.loads(encode(message))["main"] is False
    assert dialog_message(4, 1, "confirm", "about:blank", True, "Sure?", "ignored").default == ""


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://www.bcferries.com/checkout", "https://www.bcferries.com"),
        ("blob:https://pay.example/0d2c7d9e-7d44-4e3b-a4a8-31ad3b6f0f37", "https://pay.example"),  # who made it
        ("BLOB:http://pay.test:8002/0d2c", "http://pay.test:8002"),
        ("blob:null/0d2c", ""),  # made by an opaque origin
        ("blob:blob:https://pay.example/0d2c", ""),
        ("about:srcdoc", ""),  # its origin is its parent's, or opaque when sandboxed: the URL doesn't say
        ("about:blank", ""),
        ("data:text/html,<script>prompt(1)</script>", ""),
        ("https://www.bcferries.com." + "a" * 100 + ".example/", ""),
    ],
)
def test_a_dialogs_origin_is_its_frames_as_far_as_the_url_tells(url: str, origin: str) -> None:
    assert frame_origin(url) == origin


@pytest.mark.parametrize(
    ("autocomplete", "ac"),
    [
        ("cc-number", "cc-number"),
        ("shipping USERNAME email", "shipping email"),
        ("current-password new-password webauthn username", ""),
        ("user_name cc-exp", "cc-exp"),
        ("given-name\u3000family-name", ""),  # an ideographic space isn't ASCII whitespace
        ("a" * 70, "a" * 64),  # the script cut it at 64, and so does the agent
    ],
)
def test_credential_tokens_never_reach_the_viewer(autocomplete: str, ac: str) -> None:
    assert strip_credential_tokens(autocomplete) == ac


def test_the_agent_never_sends_what_the_protocol_doesnt_allow() -> None:
    assert json.loads(encode(Ready(w=390, h=664, dsf=2))) == {
        "t": "ready", "v": 1, "w": 390, "h": 664, "dsf": 2, "features": []
    }  # fmt: skip
    long_title = Pages(front=1, pages=[PageInfo(id=1, title="x" * 81, origin="", main=True)])
    with pytest.raises(ValueError, match="outside the protocol"):
        encode(long_title)
    with pytest.raises(ValueError, match="outside the protocol"):
        encode(Pages(front=1, pages=[]))
    with pytest.raises(ValueError, match="outside the protocol"):
        encode(Clock(ends=None, hard=float("nan"), paused=False))  # NaN goes as null
    field = Field(
        k="text", r=(0, 0, 10, 10), c=(0, 0, 10, 10), m=511, type="text", im="", ac="x" * 64, ekh="", cap="",
        max=None, ro=False,
    )  # fmt: skip
    many = Fields(page=1, rev=1, seq=1, n=1, sx=0, sy=0, scale=1, cw=1, ch=1, trunc=False, f=[field] * 700)
    with pytest.raises(ValueError, match="bytes"):
        encode(many)
    assert len(
        encode(Fields(page=1, rev=1, seq=1, n=1, sx=0, sy=0, scale=1, cw=1, ch=1, trunc=False, f=[field] * 300))
    ) < (MAX_OUTBOUND_BYTES)
    assert "id" not in json.loads(msgspec.json.encode(field))  # a field in `focus` has no id
