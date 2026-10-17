from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from waitlist.bcferries.errors import MarkupChanged, NotBookable
from waitlist.bcferries.parse import parse_card, parse_sailings, parse_time
from waitlist.bcferries.site import choose_fare
from waitlist.models import Fare, FarePreference, SailingStatus


def test_sold_out_sailings(fixtures: Path) -> None:
    sailings = parse_sailings((fixtures / "fare-selection-sold-out.html").read_text())

    assert [(s.depart, s.arrive) for s in sailings] == [
        (time(6, 0), time(7, 35)),
        (time(7, 0), time(8, 35)),
        (time(8, 0), time(9, 35)),
    ]
    assert [s.vessel for s in sailings] == [
        "Coastal Celebration",
        "Spirit of Vancouver Island",
        "Queen of New Westminster",
    ]
    assert all(s.status is SailingStatus.SOLD_OUT and not s.available for s in sailings)
    assert all(s.note == "Advance bookings sold out" and s.price is None for s in sailings)


def test_available_sailings_with_fares(fixtures: Path) -> None:
    sailings = parse_sailings((fixtures / "fare-selection-available.html").read_text())

    assert [s.depart for s in sailings] == [time(7, 0), time(9, 0), time(11, 0)]
    assert all(s.status is SailingStatus.AVAILABLE and s.available for s in sailings)
    first = sailings[0]
    assert first.vessel == "Spirit of Vancouver Island"
    assert first.price == "$93.45"
    assert first.note == ""
    assert first.fares == (
        Fare("Saver", "$93.45", selectable=True),
        Fare("Prepaid", "$115.50", selectable=True),
        Fare("Reservation Only", "$135.50", selectable=True),
        Fare("At terminal", "", selectable=False),
    )


def test_page_without_cards_has_no_sailings() -> None:
    assert parse_sailings("<html><body><p>No results found</p></body></html>") == []


def test_card_without_times_is_a_markup_change() -> None:
    card = HTMLParser('<div class="p-card"><div class="ferrytime"><b>6:00 AM</b></div></div>').css_first(".p-card")
    assert card is not None
    with pytest.raises(MarkupChanged):
        parse_card(card)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6:00 AM", time(6, 0)),
        ("6:00 am", time(6, 0)),
        ("12:00 PM", time(12, 0)),
        ("12:30 AM", time(0, 30)),
        ("11:59 p.m.", time(23, 59)),
        (" 9:05 PM ", time(21, 5)),
    ],
)
def test_parse_time(text: str, expected: time) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["", "noon", "25:00 AM", "6:00", "6 PM"])
def test_parse_time_rejects_unknown_formats(text: str) -> None:
    with pytest.raises(MarkupChanged):
        parse_time(text)


FARES = (
    Fare("Saver", "$93.45", selectable=True),
    Fare("Prepaid", "$115.50", selectable=True),
    Fare("Reservation Only", "$135.50", selectable=True),
    Fare("At terminal", "", selectable=False),
)


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        (FarePreference.CHEAPEST, "Saver"),
        (FarePreference.SAVER, "Saver"),
        (FarePreference.PREPAID, "Prepaid"),
        (FarePreference.RESERVATION, "Reservation Only"),
    ],
)
def test_choose_fare(preference: FarePreference, expected: str) -> None:
    assert choose_fare(FARES, preference).name == expected


def test_choose_fare_falls_back_to_cheapest_when_preference_not_offered() -> None:
    offered = (Fare("Prepaid", "$137.55", selectable=True), Fare("Reservation Only", "$157.55", selectable=True))
    assert choose_fare(offered, FarePreference.SAVER).name == "Prepaid"


def test_choose_fare_needs_a_bookable_fare() -> None:
    with pytest.raises(NotBookable):
        choose_fare((Fare("At terminal", "", selectable=False),), FarePreference.CHEAPEST)
