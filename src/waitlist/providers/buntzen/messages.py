"""What Buntzen Lake's notifications say (design.md §5.7).

A push always opens the watch's own page (`watch_url`); an email's second link (`also`) is one of Buntzen's
booking pages, a constant. Nothing a site sent (no status, no `Option.detail`) is ever placed in a notice.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from waitlist.models import format_time
from waitlist.providers.base import FAILING, OPENING, RELEASE_REMINDER, Notice

from .model import PASSES, Pass
from .policy import LABELS, PAGES, RELEASE_AT, booking_url, day_label
from .spec import BuntzenSpec

log = logging.getLogger("waitlist.providers.buntzen")

MAX_PARAM_OPTIONS: Final = 10

_BOOK: Final = "Book on Yodel"
_OPEN: Final = "Open Yodel"


def passes_phrase(passes: Sequence[Pass]) -> str:
    """'morning pass'; 'morning and afternoon passes'; 'all-day and morning and afternoon passes'."""
    names = [LABELS[p].lower() for p in passes]
    if len(names) == 1:
        return names[0]
    return " and ".join(n.removesuffix(" pass") for n in names) + " passes"


def _pass_ids(value: object) -> list[Pass] | None:
    """A list of distinct pass ids, or None."""
    if not isinstance(value, list) or len(value) > MAX_PARAM_OPTIONS:
        return None
    if any(not isinstance(p, str) or p not in PASSES for p in value) or len(set(value)) != len(value):
        return None
    return [p for p in PASSES if p in value]


def _opening(params: Mapping[str, Any], spec: BuntzenSpec, watch_url: str) -> Notice | None:
    options = _pass_ids(params.get("options"))
    uncertain = _pass_ids(params.get("uncertain"))
    if not options or uncertain is None or not set(uncertain) <= set(options):
        log.warning("Buntzen: an opening notification with unusable params; not rendered")
        return None
    day = day_label(spec.date)
    certain = [p for p in options if p not in uncertain]
    if certain:
        body = "Book it on Yodel now: passes go fast. Have your Yodel account, licence plate and mobile number ready."
        if uncertain:
            body += (
                f" Yodel also shows the {passes_phrase(uncertain)} with a status we don't recognise;"
                " it may be bookable."
            )
        return Notice(
            title=f"Buntzen Lake: {passes_phrase(certain)} open for {day}",
            body=body,
            url=watch_url,
            urgency="high",
            ttl=900,
            also=(_BOOK, PAGES[certain[0]]),
        )
    return Notice(
        title=f"Buntzen Lake: {passes_phrase(uncertain)} may be open for {day}",
        body="Yodel shows a status we don't recognise; it may be bookable. Check on Yodel now.",
        url=watch_url,
        urgency="high",
        ttl=900,
        also=(_BOOK, PAGES[uncertain[0]]),
    )


def render(kind: str, params: Mapping[str, Any], spec: BuntzenSpec, *, watch_url: str) -> Notice | None:
    """The notice for one notification of base.NOTICE_KINDS, or None (the outbox marks that row Unrenderable)."""
    if not isinstance(cast(object, params), Mapping):  # stored JSON: re-checked, as everything control writes
        log.warning("Buntzen: a %s notification whose params aren't an object; not rendered", kind[:32])
        return None
    day = day_label(spec.date)
    if kind == OPENING:
        return _opening(params, spec, watch_url)
    if kind == RELEASE_REMINDER:
        return Notice(
            title=f"Buntzen Lake passes for {day} go on sale at {format_time(RELEASE_AT)}",
            body=(
                "Popular passes sell out within seconds. Open Yodel and sign in now; we'll alert you if one opens up"
                " later."
            ),
            url=watch_url,
            urgency="high",
            ttl=600,
            also=(_OPEN, booking_url(spec, ())),
        )
    if kind == FAILING:
        return Notice(
            title="Having trouble checking Buntzen Lake",
            body=f"We'll keep trying. Until then, check Yodel yourself for {day}.",
            url=watch_url,
            urgency="low",
            ttl=3600,
            also=(_OPEN, booking_url(spec, ())),
        )
    return None
