"""Parse BC Ferries pages into domain objects.

Pure functions over HTML (no browser), so they're tested against saved copies of the real pages
in tests/fixtures. When BC Ferries changes its markup these fail loudly with MarkupChanged rather
than silently reporting nothing as bookable.
"""

from __future__ import annotations

import re
from datetime import time

from selectolax.parser import HTMLParser, Node

from waitlist.models import Fare, Sailing, SailingStatus

from .errors import MarkupChanged

_TIME_RE = re.compile(r"^(1[0-2]|0?[1-9]):([0-5]\d)\s*([ap])\.?m\.?$", re.IGNORECASE)


def _text(node: Node | None) -> str:
    """The node's text as rendered: text nodes concatenated (`<span>$</span>93.45` is "$93.45"),
    whitespace collapsed, non-breaking spaces included."""
    if node is None:
        return ""
    return " ".join(node.text(deep=True, separator="").replace("\xa0", " ").split())


def parse_time(text: str) -> time:
    """'6:00 AM' / '12:30 pm' -> time."""
    m = _TIME_RE.match(text.strip())
    if not m:
        raise MarkupChanged(f"Unrecognised sailing time {text!r}")
    hour, minute = int(m[1]) % 12, int(m[2])
    if m[3].lower() == "p":
        hour += 12
    return time(hour, minute)


def parse_card(card: Node) -> Sailing:
    """One `.p-card` from the fare-selection page."""
    times = card.css(".ferrytime")
    if len(times) < 2:
        raise MarkupChanged("Sailing card without departure/arrival times")
    depart, arrive = parse_time(_text(times[0])), parse_time(_text(times[1]))
    vessel = _text(card.css_first(".transport-vehicle-info"))

    fares = tuple(
        Fare(
            name=_text(box.css_first(".fare-type-name")),
            price=_text(box.css_first(".price-desc")),
            selectable=box.css_first("label.js-radio-select") is not None,
        )
        for box in card.css(".viewfare-box .vp-box")
        if box.css_first(".fare-type-name") is not None
    )

    if card.css_first("button.view-fare-btn") is not None:
        price = _text(card.css_first(".pc-2")) or None
        return Sailing(depart, arrive, vessel, SailingStatus.AVAILABLE, "", price, fares)

    note = _text(card.css_first(".p-cash"))
    status = SailingStatus.SOLD_OUT if "sold out" in note.lower() else SailingStatus.UNAVAILABLE
    return Sailing(depart, arrive, vessel, status, note, None, fares)


def parse_sailings(html: str) -> list[Sailing]:
    """All sailings on a fare-selection page, in the order shown."""
    return [parse_card(card) for card in HTMLParser(html).css(".p-card")]
