"""Buntzen Lake's vocabulary on the fetch wire (design.md §5.3-5.4): the query and the inventory.

Pure: imported by the client (which builds them from Yodel's answers), by `policy.evaluate` (which reads them)
and, through the provider, by waitlist-control and waitlist-web. Both are closed: the query can name only the
two places, and the inventory carries only enums and a sanitised status of at most 32 characters.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

import msgspec

from waitlist.providers.base import Detail

# The three passes, in the canonical order used everywhere (spec, options, alerts). Re-exported by spec.py.
Pass = Literal["full_day", "morning", "afternoon"]
PASSES: Final[tuple[Pass, ...]] = ("full_day", "morning", "afternoon")

FULL_DAY_PLACE: Final = 10671  # Yodel's place for the all-day pass
HALF_DAY_PLACE: Final = 10672  # Yodel's place for the morning and afternoon passes
PlaceId = Literal[10671, 10672]
PLACE_OF: Final[dict[Pass, PlaceId]] = {"full_day": 10671, "morning": 10672, "afternoon": 10672}

IsoDate = Annotated[str, msgspec.Meta(pattern=r"^\d{4}-\d{2}-\d{2}$")]

MAX_ITEMS: Final = 20


class BuntzenQuery(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    """One request to Yodel: one place on one date. Hashable, so control coalesces it across watches."""

    place_id: Literal[10671, 10672]  # the wire can't name any other place
    date: IsoDate


class PassItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    """One catalog item as the parser read it."""

    period: Literal["full_day", "morning", "afternoon", "unknown"]
    availability: Literal["open", "sold_out", "not_released", "unknown"]
    status: Detail  # the site's status, sanitised, at most 32 characters


class BuntzenInventory(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    """What one query found. `place_id` and `date` are the query's, never the response's."""

    place_id: Literal[10671, 10672]
    date: IsoDate
    items: Annotated[list[PassItem], msgspec.Meta(max_length=20)] = []
