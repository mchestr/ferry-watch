"""The fw.cast1 conformance pack is well formed (tests/fixtures/cast1/README.md).

Only the files' shape is checked here: the agent's and the viewer's own tests run the cases.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PACK = Path(__file__).parent / "fixtures" / "cast1"
PROTOCOL = Path(__file__).parents[1] / "docs" / "multi-tenant" / "streamed-viewer" / "PROTOCOL.md"

Check = Callable[[Any], bool]


def integer(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def text(v: Any) -> bool:
    return isinstance(v, str)


def boolean(v: Any) -> bool:
    return isinstance(v, bool)


def obj(v: Any) -> bool:
    return isinstance(v, dict)


def anything(v: Any) -> bool:
    return True


def message(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("t"), str)


def hexes(v: Any) -> bool:
    return isinstance(v, str) and re.fullmatch(r"(?:[0-9a-f]{2})*", v) is not None


def one_of(*values: Any) -> Check:
    return lambda v: any(v == x and type(v) is type(x) for x in values)


def nullable(check: Check) -> Check:
    return lambda v: v is None or check(v)


def list_of(check: Check, size: int | None = None) -> Check:
    return lambda v: isinstance(v, list) and (size is None or len(v) == size) and all(check(x) for x in v)


def shaped(**fields: Check) -> Check:
    """An object with exactly these fields (a trailing ? marks an optional one)."""
    return lambda v: isinstance(v, dict) and not _problems(v, fields)


def _problems(value: dict[str, Any], fields: dict[str, Check]) -> list[str]:
    known = {name.rstrip("?"): (check, name.endswith("?")) for name, check in fields.items()}
    out = [f"unknown field {k!r}" for k in value if k not in known]
    for k, (check, optional) in known.items():
        if k not in value:
            out += [] if optional else [f"missing {k!r}"]
        elif not check(value[k]):
            out.append(f"bad {k!r}: {value[k]!r}")
    return out


rect = list_of(num, 4)
point = list_of(num, 2)
messages = list_of(message)
image = shaped(page=integer, seq=integer, w=integer, h=integer, scale=num, sx=num, sy=num)
call = shaped(method=text, params=obj)
step = shaped(
    at=integer, msg=message, times=integer, expect=one_of("ok", "rate", "invalid", "close"), **{"code?": one_of(4429)}
)
map_case = {"map": message, "oks": messages, "image": image, "last_input": integer, "shifted": boolean}
EVENTS = {  # each gesture event type and its fields besides `at` and `type`
    "touchstart": {"touches"},
    "touchmove": {"touches"},
    "touchend": {"touches"},
    "pointerdown": {"x", "y"},
    "pointermove": {"x", "y"},
    "pointerup": {"x", "y"},
    "wheel": {"x", "y", "dx", "dy", "mode"},
    "image": {"image"},
}
event_fields = {
    "touches": list_of(point),
    "x": num,
    "y": num,
    "dx": num,
    "dy": num,
    "mode": one_of(0, 1),
    "image": image,
}
event = shaped(at=integer, type=one_of(*EVENTS), **{f"{k}?": v for k, v in event_fields.items()})
wheels = shaped(
    page=integer, x=num, y=num, dx=num, **{f"{k}?": num for k in ("dy", "dy_more_than", "coast_after", "none_after")}
)
expect = shaped(
    **{"taps?": messages, "pinches?": messages, "moves?": shaped(page=integer, last=point), "wheels?": wheels}
)

SCHEMAS: dict[str, dict[str, dict[str, Check]]] = {
    "messages": {
        "cases": {
            "msg?": anything,
            "raw?": text,
            "hex?": hexes,
            "pad_to?": integer,
            "state?": one_of("open", "hello", "ready"),
            "outcome": one_of("ok", "invalid", "close"),
            "code?": one_of(4400, 1009),
        },
        "offers": {"offered": list_of(text), "cast_allowed": boolean, "result": one_of("binary", "fw.cast1", 400, 403)},
        "viewer_offers": {
            "record": nullable(obj),
            "ticket": nullable(obj),
            "now": num,
            "offer": nullable(list_of(text)),
        },
        "hello_env": {"inner": point, "dpr": num, "hello": message},
        "dsf": {"hello": message, "dsf": one_of(1, 2)},
        "rates": {"steps": list_of(step)},
        "coalesce": {"queue": messages, "arrives": message, "after?": messages, "close?": one_of(4429)},
        "outbound": {"msg": message, "outcome": one_of("accept", "extra", "unknown")},
        "autocomplete": {"input?": text, "ac": text, "proxy": text},
    },
    "headers": {
        "cases": {"fields": obj, "header": hexes, "jpeg": hexes, "first": boolean},
        "malformed": {"hex": hexes},
    },
    "rects": {
        "cases": {
            "s": num,
            "ox": num,
            "oy": num,
            "origins": list_of(point),
            "clip": rect,
            "client": rect,
            "r": rect,
            "c": nullable(rect),
        },
    },
    "hits": {
        "cases": map_case
        | {
            "taps": list_of(shaped(x=num, y=num, field=nullable(integer), **{"note?": text})),
            "overlays": list_of(shaped(field=integer, overlay=one_of(None, "select", "date", "time"))),
        },
    },
    "usable": {
        "numbering": {"sent": list_of(text), "numbers": list_of(nullable(integer)), "last_input": integer},
        "cases": map_case | {"usable": boolean},
    },
    "text": {
        "cases": {
            "as": one_of("text", "compose", "dialog"),
            "via?": one_of("paste", "insertText"),
            "input": text,
            "messages?": messages,
            "s?": text,
        },
    },
    "gestures": {
        "cases": {"viewport": point, "image": image, "events": list_of(event), "end": integer, "expect": expect}
    },
    "mapping": {
        "inputs": {
            "front": integer,
            "images": list_of(image),
            "live": shaped(pageX=num, pageY=num, scale=num),
            "after?": shaped(scale=num),
            "msg": message,
            "calls": list_of(call),
            "wait_ms?": integer,
            "error?": one_of("stale"),
        },
        "keys": {"msgs": messages, "teardown?": boolean, "events": list_of(obj)},
        "date_keys": {"k": one_of("date", "time"), "v": text, "chars": text},
        "choose_keys": {
            "opts": list_of(list_of(anything, 2)),
            "i": integer,
            "keys?": list_of(text),
            "error?": one_of("failed"),
        },
    },
    "close_codes": {
        "cases": {
            "steps": list_of(
                shaped(
                    opened=boolean,
                    ready=boolean,
                    code=integer,
                    action=obj,
                    prominent=boolean,
                    **{"deadline_passed?": boolean},
                )
            )
        },
    },
}
SHARED = {"messages": {"context", "rate_table", "drop_limit"}, "close_codes": {"delays"}}
COMMON: dict[str, Check] = {"id": text, "sec": text, "note?": text, "only?": one_of("agent", "viewer")}


def load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((PACK / f"{name}.json").read_text("utf-8"))
    return doc


def sections() -> set[str]:
    return set(re.findall(r"^#{2,4} (\d+(?:\.\d+)*)\.? ", PROTOCOL.read_text("utf-8"), re.MULTILINE))


def test_the_pack_has_exactly_the_described_files() -> None:
    readme = (PACK / "README.md").read_text("utf-8")
    assert {p.stem for p in PACK.glob("*.json")} == set(SCHEMAS)
    for name, arrays in SCHEMAS.items():
        assert f"## {name}.json" in readme
        for array in arrays:
            assert f"`{array}`" in readme, (name, array)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_cases_match_the_readme(name: str) -> None:
    doc = load(name)
    assert doc.pop("protocol") == "fw.cast1"
    assert set(doc) == set(SCHEMAS[name]) | SHARED.get(name, set())
    known, ids = sections(), set()
    for array, fields in SCHEMAS[name].items():
        assert doc[array], f"{name}.{array} is empty"
        for case in doc[array]:
            assert not _problems(case, COMMON | fields), (name, array, case.get("id"), _problems(case, COMMON | fields))
            assert case["id"] not in ids, f"{name}: id {case['id']} twice"
            ids.add(case["id"])
            assert case["sec"] in known, f"{name}: {case['id']} names §{case['sec']}, which PROTOCOL.md doesn't have"


def test_message_cases_are_consistent() -> None:
    doc = load("messages")
    for case in doc["cases"]:
        assert len({"msg", "raw", "hex"} & case.keys()) == 1, case["id"]
        assert ("pad_to" in case) <= ("raw" in case), case["id"]
        assert ("code" in case) == (case["outcome"] == "close"), case["id"]
    for case in doc["coalesce"]:
        assert len({"after", "close"} & case.keys()) == 1, case["id"]
    for case in doc["rates"]:
        assert all(("code" in s) == (s["expect"] == "close") for s in case["steps"]), case["id"]
    assert {k for k in doc["rate_table"] if k != "*"} == {
        "ack",
        "tap",
        "move",
        "wheel",
        "pinch",
        "key",
        "text",
        "compose",
        "choose",
        "date",
        "dialog",
        "page",
        "resize",
        "visible",
    }


def test_other_cases_are_consistent() -> None:
    for case in load("headers")["cases"]:
        assert len(case["header"]) == 64, case["id"]
        assert (case["jpeg"] == "") == (case["fields"]["kind"] == 2), case["id"]
    for case in load("hits")["cases"]:
        assert sorted(o["field"] for o in case["overlays"]) == sorted(f["id"] for f in case["map"]["f"]), case["id"]
    for case in load("text")["cases"]:
        assert ("s" in case) == (case["as"] == "dialog") != ("messages" in case), case["id"]
        assert ("via" in case) == (case["as"] == "text"), case["id"]
    mapping = load("mapping")
    for case in mapping["inputs"]:
        assert "error" not in case or case["calls"] == [], case["id"]
        assert ("wait_ms" in case) == (case["msg"]["t"] == "tap" and "error" not in case), case["id"]
    for case in mapping["choose_keys"]:
        assert len({"keys", "error"} & case.keys()) == 1, case["id"]
    for case in load("gestures")["cases"]:
        for e in case["events"]:
            assert e.keys() - {"at", "type"} == EVENTS[e["type"]], (case["id"], e)
        times = [e["at"] for e in case["events"]]
        assert times == sorted(times), case["id"]
        assert times[-1] <= case["end"], case["id"]
        if "wheels" in case["expect"]:
            w = case["expect"]["wheels"]
            assert ("dy" in w) != ("dy_more_than" in w), case["id"]
            assert ("dy_more_than" in w) == ("coast_after" in w), case["id"]
    for case in load("usable")["numbering"]:
        assert len(case["sent"]) == len(case["numbers"]), case["id"]
