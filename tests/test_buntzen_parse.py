"""Buntzen's defensive parser (design.md §5.4) against every fixture of §5.8, and against random input."""

from __future__ import annotations

import json
import random
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import msgspec
import pytest

from waitlist.models import PACIFIC
from waitlist.providers.base import FetchError, HttpResponse
from waitlist.providers.buntzen import PROVIDER
from waitlist.providers.buntzen.client import parse_catalog
from waitlist.providers.buntzen.model import BuntzenInventory, BuntzenQuery, PlaceId
from waitlist.providers.buntzen.spec import BuntzenSpec

FIXTURES = Path(__file__).parent / "fixtures" / "buntzen"
DATE = "2027-07-01"
FULL: Final = 10671
HALF: Final = 10672
ALL_PASSES = BuntzenSpec(date=date(2027, 7, 1), passes=("full_day", "morning", "afternoon"))
IN_WINDOW = datetime(2027, 6, 30, 8, 0, tzinfo=PACIFIC)  # after the 07:00 release, before every stop time
LIST = "catalogItemExtendedResultLst"
DETAIL = re.compile(r"[A-Za-z0-9 .,:;'()/&+-]*")

SC = "SC"
Items = list[tuple[str, str, str]]
HALF_OPEN: Items = [("morning", "open", "Available"), ("afternoon", "sold_out", "Sold Out")]
HALF_OPEN_OPTIONS = {"morning": "open", "afternoon": "sold_out"}

# file -> (places it is read for, items or SC, the options on that place for a watch of all three passes)
EXPECTED: dict[str, tuple[tuple[PlaceId, ...], Items | str, dict[str, str]]] = {
    "synth-fullday-available.json": ((FULL,), [("full_day", "open", "Available")], {"full_day": "open"}),
    "synth-fullday-soldout.json": ((FULL,), [("full_day", "sold_out", "Sold Out")], {"full_day": "sold_out"}),
    "synth-bare-array.json": ((FULL, HALF), SC, {}),
    "synth-halfday-morning-open.json": ((HALF,), HALF_OPEN, HALF_OPEN_OPTIONS),
    "synth-halfday-ha-quirk.json": (
        (HALF,),
        [("morning", "open", "Morning"), ("unknown", "sold_out", "Sold Out")],
        {"morning": "open", "afternoon": "sold_out"},
    ),
    "synth-halfday-ha-quirk-both.json": (
        (HALF,),
        [("morning", "open", "Morning"), ("afternoon", "open", "Afternoon")],
        {"morning": "open", "afternoon": "open"},
    ),
    "synth-halfday-ha-quirk-soldout.json": (
        (HALF,),
        [("unknown", "sold_out", "Sold Out"), ("unknown", "sold_out", "Sold Out")],
        {"morning": "sold_out", "afternoon": "sold_out"},
    ),
    "synth-halfday-both-soldout.json": (
        (HALF,),
        [("morning", "sold_out", "Sold Out"), ("afternoon", "sold_out", "Sold Out")],
        {"morning": "sold_out", "afternoon": "sold_out"},
    ),
    "synth-wrapped-content.json": ((HALF,), HALF_OPEN, HALF_OPEN_OPTIONS),
    "synth-wrapped-data.json": ((HALF,), HALF_OPEN, HALF_OPEN_OPTIONS),
    "synth-empty.json": (
        (FULL, HALF),
        [],
        {"full_day": "not_offered", "morning": "not_offered", "afternoon": "not_offered"},
    ),
    "synth-null-list.json": (
        (FULL, HALF),
        [],
        {"full_day": "not_offered", "morning": "not_offered", "afternoon": "not_offered"},
    ),
    "synth-unknown-status.json": (
        (HALF,),
        [("morning", "unknown", "Waitlist only")],
        {"morning": "maybe", "afternoon": "not_offered"},
    ),
    "synth-quantity-only.json": (
        (HALF,),
        [("morning", "open", "Ready")],
        {"morning": "open", "afternoon": "not_offered"},
    ),
    "synth-not-released.json": ((FULL,), [("full_day", "not_released", "Coming Soon")], {"full_day": "not_released"}),
    "synth-changed-shape.json": ((FULL, HALF), SC, {}),
    "synth-not-success.json": ((FULL, HALF), SC, {}),
    "synth-halfday-unplaceable-open.json": ((HALF,), SC, {}),
    "synth-status-not-string.json": ((FULL, HALF), SC, {}),
    "synth-error-page.html": ((FULL, HALF), SC, {}),
    "synth-huge.json": ((FULL, HALF), SC, {}),
}
PLACE_PASSES = {FULL: ("full_day",), HALF: ("morning", "afternoon")}


def response(body: bytes, *, content_type: str = "application/json", status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type=content_type, body=body)


def parse(body: object, place: PlaceId = HALF, **kw: Any) -> BuntzenInventory:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return parse_catalog(response(raw, **kw), BuntzenQuery(place_id=place, date=DATE))


def items_of(inventory: BuntzenInventory) -> Items:
    return [(i.period, i.availability, i.status) for i in inventory.items]


def fixture_response(name: str) -> HttpResponse:
    content_type = "text/html" if name.endswith(".html") else "application/json"
    return response((FIXTURES / name).read_bytes(), content_type=content_type)


def test_every_synthesized_fixture_has_an_expectation() -> None:
    assert {p.name for p in FIXTURES.glob("synth-*")} == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_fixture_gives_exactly_its_expected_result(name: str) -> None:
    places, expected, options = EXPECTED[name]
    for place in places:
        query = BuntzenQuery(place_id=place, date=DATE)
        if expected == SC:
            with pytest.raises(FetchError) as caught:
                parse_catalog(fixture_response(name), query)
            assert caught.value.kind == "site_changed"
            assert caught.value.http_status == 200
            continue
        inventory = parse_catalog(fixture_response(name), query)
        assert items_of(inventory) == expected
        assert (inventory.place_id, inventory.date) == (place, DATE)
        found = PROVIDER.evaluate(ALL_PASSES, {query: inventory}, IN_WINDOW)
        on_place = {o.key: o.status for o in found if o.key in PLACE_PASSES[place]}
        assert on_place == {k: v for k, v in options.items() if k in PLACE_PASSES[place]}


def test_the_ha_quirk_all_sold_out_is_both_half_day_passes_sold_out_not_a_changed_site() -> None:
    query = BuntzenQuery(place_id=HALF, date=DATE)
    inventory = parse_catalog(fixture_response("synth-halfday-ha-quirk-soldout.json"), query)
    spec = BuntzenSpec(date=ALL_PASSES.date, passes=("morning", "afternoon"))
    options = PROVIDER.evaluate(spec, {query: inventory}, IN_WINDOW)
    assert [(o.key, o.status, o.wanted) for o in options] == [
        ("morning", "sold_out", True),
        ("afternoon", "sold_out", True),
    ]


def test_the_unknown_status_becomes_a_maybe_with_its_sanitised_status_as_detail() -> None:
    query = BuntzenQuery(place_id=HALF, date=DATE)
    inventory = parse_catalog(fixture_response("synth-unknown-status.json"), query)
    morning = PROVIDER.evaluate(ALL_PASSES, {query: inventory}, IN_WINDOW)[1]
    assert (morning.key, morning.status, morning.detail) == ("morning", "maybe", "Waitlist only")


@pytest.mark.parametrize("wrapper", ["content", "data"])
def test_the_list_is_found_under_content_or_data(wrapper: str) -> None:
    unwrapped = parse(json.loads((FIXTURES / "synth-halfday-morning-open.json").read_text()))
    assert parse({wrapper: json.loads((FIXTURES / "synth-halfday-morning-open.json").read_text())}) == unwrapped


def test_the_top_level_list_wins_and_other_wrappers_are_not_searched() -> None:
    top = {"catalogItemCategory": "Morning", "catalogItemStatus": "Sold Out"}
    inner = {"catalogItemCategory": "Morning", "catalogItemStatus": "Available"}
    assert items_of(parse({LIST: [top], "content": {LIST: [inner]}})) == [("morning", "sold_out", "Sold Out")]
    assert items_of(parse({"content": {LIST: [top]}, "data": {LIST: [inner]}})) == [("morning", "sold_out", "Sold Out")]
    with pytest.raises(FetchError, match="site_changed"):
        parse({"result": {LIST: [inner]}})
    with pytest.raises(FetchError, match="site_changed"):
        parse({"content": [inner]})  # content isn't an object, so it holds no list key


@pytest.mark.parametrize("flag", ["isSuccess", "success", "succeeded"])
def test_a_refusal_flag_is_a_changed_site(flag: str) -> None:
    with pytest.raises(FetchError, match="site_changed"):
        parse({flag: False, LIST: []})
    with pytest.raises(FetchError, match="site_changed"):
        parse({"data": {flag: False, LIST: []}})  # in the object holding the list, too
    assert parse({flag: True, LIST: []}).items == []
    assert parse({flag: 0, LIST: []}).items == []  # only JSON false means refused


@pytest.mark.parametrize("value", ["{}", '"items"', "3", "true"])
def test_a_list_that_isnt_a_list_is_a_changed_site(value: str) -> None:
    with pytest.raises(FetchError, match="site_changed"):
        parse(f'{{"{LIST}": {value}}}'.encode())


def test_fifty_entries_are_read_and_twenty_kept() -> None:
    entry = {"catalogItemCategory": "All Day Pass", "catalogItemStatus": "Sold Out"}
    inventory = parse({LIST: [entry] * 50}, FULL)
    assert len(inventory.items) == 20
    with pytest.raises(FetchError, match="site_changed"):
        parse({LIST: [entry] * 51}, FULL)


@pytest.mark.parametrize("entry", [None, 3, "Sold Out", ["Sold Out"], {"catalogItemCategory": "Morning"}])
def test_every_entry_must_be_an_object_with_a_string_status(entry: object) -> None:
    with pytest.raises(FetchError, match="site_changed"):
        parse({LIST: [{"catalogItemStatus": "Sold Out"}, entry]})


@pytest.mark.parametrize(
    "body",
    [b"", b"{", b"\xff\xfe", b"<html>", b"null", b"NaN", b'{"a": 1}{"b": 2}', b"[" * 100_000 + b"]" * 100_000],
)
def test_undecodable_bodies_are_a_changed_site(body: bytes) -> None:
    with pytest.raises(FetchError, match="site_changed"):
        parse(body, content_type="text/plain")


def test_json_is_read_whatever_the_content_type_says_except_html() -> None:
    body = json.dumps({LIST: [{"catalogItemCategory": "All Day Pass", "catalogItemStatus": "Available"}]}).encode()
    for content_type in ("application/json", "text/plain", "application/octet-stream", ""):
        assert items_of(parse(body, FULL, content_type=content_type)) == [("full_day", "open", "Available")]
    with pytest.raises(FetchError, match="site_changed"):
        parse(body, FULL, content_type="text/html")


@pytest.mark.parametrize(
    ("entry", "period"),
    [
        ({"catalogItemCategory": "Morning Pass"}, "morning"),
        ({"catalogItemCategory": "half-day_AFTERNOON  pass"}, "afternoon"),
        ({"catalogItemCategory": "Half Day Pass", "catalogItemName": "Afternoon"}, "afternoon"),
        ({"name": "Morning (8 am - 1 pm)"}, "morning"),
        ({"title": "8 am to 1 pm"}, "unknown"),  # times are never period words
        ({"catalogItemCategory": "Morning and Afternoon", "catalogItemName": "Afternoon"}, "afternoon"),
        ({"catalogItemCategory": "Morning or afternoon"}, "unknown"),  # two periods: skipped
        ({"catalogItemCategory": "Mornings"}, "unknown"),  # whole words only
        ({"catalogItemCategory": 3, "description": "the morning slot"}, "morning"),  # only strings count
        ({"catalogItemCategory": "Half Day Pass"}, "unknown"),
    ],
)
def test_the_period_is_the_first_field_that_names_exactly_one(entry: dict[str, Any], period: str) -> None:
    inventory = parse({LIST: [{**entry, "catalogItemStatus": "Sold Out"}]})
    assert inventory.items[0].period == period


@pytest.mark.parametrize("words", ["All Day Pass", "Full-Day", "allday", "Daily parking"])
def test_full_day_words_are_full_day_on_10671_and_unknown_on_10672(words: str) -> None:
    entry = {"catalogItemCategory": words, "catalogItemStatus": "Sold Out"}
    assert parse({LIST: [entry]}, FULL).items[0].period == "full_day"
    assert parse({LIST: [entry]}, HALF).items[0].period == "unknown"
    with pytest.raises(FetchError, match="site_changed"):  # unknown and open on the half-day place
        parse({LIST: [{**entry, "catalogItemStatus": "Available"}]}, HALF)


def test_every_item_on_10671_is_full_day_whatever_it_says() -> None:
    entries = [
        {"catalogItemCategory": "Morning", "catalogItemStatus": "Available"},
        {"catalogItemStatus": "Totally new"},
    ]
    assert items_of(parse({LIST: entries}, FULL)) == [
        ("full_day", "open", "Available"),
        ("full_day", "unknown", "Totally new"),
    ]


@pytest.mark.parametrize(
    ("status", "availability"),
    [
        *((s, "sold_out") for s in ["Sold Out", "SOLDOUT", "Full", "Unavailable", "Not Available"]),
        *((s, "sold_out") for s in ["No availability", "Closed", "Sold out - check back", "sold_out"]),
        *((s, "not_released") for s in ["Coming Soon", "Not yet available", "Not Released", "Upcoming"]),
        ("Opens soon", "not_released"),
        *((s, "open") for s in ["Available", "Book Now", "Open", "Limited", "Limited Availability"]),
        *((s, "open") for s in ["Few left", "Almost Full", "Selling fast", "Morning", "All-Day", "Full Day"]),
        *((s, "unknown") for s in ["", "Waitlist only", "Available!", "Ready", "Sold"]),
    ],
)
def test_the_status_table(status: str, availability: str) -> None:
    assert parse({LIST: [{"catalogItemCategory": "Morning", "catalogItemStatus": status}]}).items[0].availability == (
        availability
    )


@pytest.mark.parametrize(
    ("extra", "availability"),
    [
        ({"availableQuantity": 3}, "open"),
        ({"availableQuantity": 0}, "sold_out"),
        ({"quantityAvailable": 1}, "open"),
        ({"remainingQuantity": 0}, "sold_out"),
        ({"availableCount": 2}, "open"),
        ({"remaining": 0}, "sold_out"),
        ({"availableQuantity": True, "remaining": 0}, "sold_out"),  # a bool is never a number
        ({"availableQuantity": -1, "remaining": 4}, "open"),  # the first int >= 0
        ({"availableQuantity": "3"}, "unknown"),
        ({"availableQuantity": 2.5}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_the_quantity_fallback(extra: dict[str, Any], availability: str) -> None:
    entry = {"catalogItemCategory": "Morning", "catalogItemStatus": "Ready", **extra}
    assert parse({LIST: [entry]}).items[0].availability == availability


def test_a_recognised_status_word_always_wins_over_a_number() -> None:
    entry = {"catalogItemCategory": "Morning", "catalogItemStatus": "Sold Out", "availableQuantity": 5}
    assert parse({LIST: [entry]}).items[0].availability == "sold_out"
    entry = {"catalogItemCategory": "Morning", "catalogItemStatus": "Available", "availableQuantity": 0}
    assert parse({LIST: [entry]}).items[0].availability == "open"


def test_an_unplaced_item_that_is_sold_out_or_not_released_stands_for_the_passes_without_their_own() -> None:
    query = BuntzenQuery(place_id=HALF, date=DATE)
    entries = [
        {"catalogItemCategory": "Morning", "catalogItemStatus": "Available"},
        {"catalogItemCategory": "Half Day Pass", "catalogItemStatus": "Coming soon"},
    ]
    inventory = parse({LIST: entries})
    assert items_of(inventory) == [("morning", "open", "Available"), ("unknown", "not_released", "Coming soon")]
    statuses = {o.key: o.status for o in PROVIDER.evaluate(ALL_PASSES, {query: inventory}, IN_WINDOW)}
    assert (statuses["morning"], statuses["afternoon"]) == ("open", "not_released")


@pytest.mark.parametrize(
    ("status", "sanitised"),
    [
        ("Sold Out", "Sold Out"),
        ("  Sold\t\n Out  ", "Sold Out"),
        ("<b>Sold</b> Out", "bSold/b Out"),
        ("Ré-ouvert bientôt", "R-ouvert bientt"),
        ("Available " + "x" * 60, "Available " + "x" * 22),
        ("javascript:alert(1)", "javascript:alert(1)"),
        ('"quoted"; DROP', "quoted; DROP"),
        ("\u200bMorning\u00a0Pass", "Morning Pass"),
    ],
)
def test_statuses_are_sanitised_to_the_detail_vocabulary(status: str, sanitised: str) -> None:
    item = parse({LIST: [{"catalogItemStatus": status}]}, FULL).items[0]
    assert item.status == sanitised
    assert DETAIL.fullmatch(item.status)
    assert len(item.status) <= 32


def test_place_and_date_come_from_the_query_never_the_body() -> None:
    body = {
        "placeId": "99999",
        "reservationDate": "2001-01-01",
        "date": "2001-01-01",
        LIST: [
            {"placeId": 10671, "reservationDate": "2001-01-01", "catalogItemStatus": "Sold Out", "date": "1999-12-31"}
        ],
    }
    inventory = parse(body, HALF)
    assert (inventory.place_id, inventory.date) == (HALF, DATE)


def _random_value(rng: random.Random, depth: int) -> object:  # noqa: PLR0911 - one return per kind of value
    choice = rng.randrange(9 if depth < 4 else 6)
    keys = [LIST, "content", "data", "catalogItemStatus", "catalogItemCategory", "isSuccess", "availableQuantity"]
    words = ["Sold Out", "Morning", "Afternoon", "All Day", "Available", "", "Coming Soon", "é\x00<", "x" * 80]
    if choice == 0:
        return None
    if choice == 1:
        return rng.choice([True, False])
    if choice == 2:
        return rng.randint(-5, 10**20)
    if choice == 3:
        return rng.random() * 1000
    if choice in (4, 5):
        return rng.choice(words)
    if choice == 6:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 60 if depth == 1 else 4))]
    return {rng.choice([*keys, "x", "y"]): _random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))}


def _has_list_key(root: dict[str, Any]) -> bool:
    return LIST in root or any(isinstance(root.get(w), dict) and LIST in root[w] for w in ("content", "data"))


def test_random_objects_raise_only_fetch_errors_and_never_invent_an_inventory() -> None:
    rng = random.Random(20270701)  # noqa: S311 - seeded test data: the same cases every run
    outcomes = {"ok": 0, "changed": 0}
    kinds: set[str] = set()
    for _ in range(3000):
        root = _random_value(rng, 0)
        if not isinstance(root, dict) or rng.random() < 0.5:
            root = {LIST: _random_value(rng, 1), **({} if rng.random() < 0.5 else {"content": _random_value(rng, 1)})}
        places: tuple[PlaceId, ...] = (FULL, HALF)
        for place in places:
            try:
                inventory = parse(root, place)
            except FetchError as e:
                kinds.add(e.kind)
                outcomes["changed"] += 1
                continue
            outcomes["ok"] += 1
            assert len(inventory.items) <= 20
            assert (inventory.place_id, inventory.date) == (place, DATE)
            if inventory.items:
                assert _has_list_key(root)
            for item in inventory.items:
                assert DETAIL.fullmatch(item.status)
                assert len(item.status) <= 32
            msgspec.convert(msgspec.to_builtins(inventory), BuntzenInventory, strict=True)  # the wire accepts it
    assert kinds == {"site_changed"}
    assert outcomes["ok"] > 100
    assert outcomes["changed"] > 100


def test_captured_responses_still_parse_as_they_did() -> None:
    """Each `captured-*` body from scripts/capture_yodel.py is a regression test: parsed as its .meta.json says."""
    for meta_path in sorted(FIXTURES.glob("captured-*.meta.json")):
        meta = json.loads(meta_path.read_text())
        stem = meta_path.name.removesuffix(".meta.json")
        body_path = next(p for p in FIXTURES.glob(f"{stem}.*") if p != meta_path)
        payload = meta["request"]["payload"]
        query = BuntzenQuery(place_id=int(payload["placeId"]), date=payload["reservationDate"])  # type: ignore[arg-type]
        body = body_path.read_bytes()
        if body_path.suffix == ".json":  # stored pretty-printed; the parser reads JSON the same either way
            body = json.dumps(json.loads(body)).encode()
        reply = HttpResponse(status=meta["status"], content_type=meta["content_type"], body=body)
        try:
            parsed: object = msgspec.to_builtins(parse_catalog(reply, query))
        except FetchError as e:
            parsed = {"error": e.kind}
        assert parsed == meta["parsed"], meta_path.name
