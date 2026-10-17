"""When a Buntzen watch is checked, and what its options are (design.md §5.5-5.6).

The constants are binding: the SPA mirrors the ones it shows (labels, stop times, 06:55, 07:00, the seasons).
Every `now` is timezone-aware; it is compared as an instant and read as a Pacific date. No naive datetime is
built or compared here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, Literal

from waitlist.models import PACIFIC
from waitlist.providers.base import ALERTABLE, CheckWindow, Option, OptionStatus

from .meta import FULL_DAY_PAGE, HALF_DAY_PAGE
from .model import PASSES, PLACE_OF, BuntzenInventory, BuntzenQuery, Pass, PassItem

if TYPE_CHECKING:
    from .spec import BuntzenSpec

LABELS: Final[dict[Pass, str]] = {"full_day": "All-day pass", "morning": "Morning pass", "afternoon": "Afternoon pass"}
RELEASE_AT: Final = time(7, 0)  # Pacific, on date - 1
REMINDER_AT: Final = time(6, 55)  # Pacific, on date - 1 (release_reminder)
BURST: Final = timedelta(seconds=30)  # how often, for BURST_FOR after the release
BURST_FOR: Final = timedelta(minutes=20)
STEADY: Final = timedelta(seconds=60)
OFF_SEASON: Final = timedelta(minutes=30)  # dates outside the season: checked, slowly
# When a pass stops being worth finding (Pacific, on the day). Unverified: confirm with the May 2027 capture.
STOP_AT: Final[dict[Pass, time]] = {"full_day": time(18, 0), "morning": time(12, 0), "afternoon": time(18, 0)}
PAGES: Final[dict[Pass, str]] = {"full_day": FULL_DAY_PAGE, "morning": HALF_DAY_PAGE, "afternoon": HALF_DAY_PAGE}
# Reservation seasons, both ends inclusive. Other years use the estimate, which is generous on purpose.
KNOWN_SEASONS: Final[dict[int, tuple[date, date]]] = {2026: (date(2026, 5, 14), date(2026, 9, 7))}
ESTIMATED_SEASON: Final = ((5, 1), (9, 30))  # (month, day) to (month, day)

_PAGE_OF_KEY: Final[dict[str, str]] = {str(p): page for p, page in PAGES.items()}

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def instant(now: datetime) -> datetime:
    """`now` in UTC, for comparisons. A naive datetime is a caller's bug (design.md §2.2 rule 7), so it is refused.

    Comparing in UTC compares instants: two datetimes in the same ZoneInfo compare by wall clock instead.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Buntzen needs a timezone-aware now")
    return now.astimezone(UTC)


def today(now: datetime) -> date:
    """The date in Pacific at `now`."""
    return instant(now).astimezone(PACIFIC).date()


def day_label(d: date) -> str:
    """date(2027, 7, 1) -> 'Thu Jul 1' (strftime's '%a %b %-d', without depending on the locale)."""
    return f"{_DAYS[d.weekday()]} {_MONTHS[d.month - 1]} {d.day}"


def in_season(d: date) -> bool:
    """Inside KNOWN_SEASONS[d.year], else inside ESTIMATED_SEASON of d.year (both ends inclusive)."""
    known = KNOWN_SEASONS.get(d.year)
    if known is not None:
        start, end = known
    else:
        (start_month, start_day), (end_month, end_day) = ESTIMATED_SEASON
        start, end = date(d.year, start_month, start_day), date(d.year, end_month, end_day)
    return start <= d <= end


def release_at(d: date) -> datetime:
    """When passes for `d` go on sale: 07:00 Pacific the day before."""
    return datetime.combine(d - timedelta(days=1), RELEASE_AT, tzinfo=PACIFIC)


def stop_at(d: date, p: Pass) -> datetime:
    """When pass `p` on `d` stops being worth finding."""
    return datetime.combine(d, STOP_AT[p], tzinfo=PACIFIC)


def is_over(d: date, p: Pass, now: datetime) -> bool:
    return instant(now) >= stop_at(d, p)


def window(spec: BuntzenSpec) -> CheckWindow:
    return CheckWindow(opens_at=release_at(spec.date), closes_at=max(stop_at(spec.date, p) for p in spec.passes))


def interval(spec: BuntzenSpec, now: datetime) -> timedelta:
    if not in_season(spec.date):
        return OFF_SEASON
    return BURST if instant(now) < release_at(spec.date) + BURST_FOR else STEADY


def reminder_at(spec: BuntzenSpec) -> datetime | None:
    if not (spec.release_reminder and in_season(spec.date)):
        return None
    return datetime.combine(spec.date - timedelta(days=1), REMINDER_AT, tzinfo=PACIFIC)


def queries(spec: BuntzenSpec) -> tuple[BuntzenQuery, ...]:
    """All-day first, then half-day; one query per place."""
    places = sorted({PLACE_OF[p] for p in spec.passes})
    return tuple(BuntzenQuery(place_id=place, date=spec.date.isoformat()) for place in places)


def _status(p: Pass, inventory: BuntzenInventory) -> tuple[OptionStatus, str | None]:
    """An option's status from its place's inventory: the rows of design.md §5.5 after the time rules."""
    own = [i for i in inventory.items if i.period == p]
    if own:
        availability = _first_availability(own)
        if availability == "unknown":
            detail = next(i.status for i in own if i.availability == "unknown")
            return "maybe", detail or None
        return availability, None
    # No item of its own: an unlabelled item stands for every half-day pass that has none (the HA quirk).
    unlabelled = {i.availability for i in inventory.items if i.period == "unknown"}
    if "not_released" in unlabelled:
        return "not_released", None
    if "sold_out" in unlabelled:
        return "sold_out", None
    return "not_offered", None


def _first_availability(items: Sequence[PassItem]) -> Literal["open", "unknown", "not_released", "sold_out"]:
    found = {i.availability for i in items}
    if "open" in found:
        return "open"
    if "unknown" in found:
        return "unknown"
    return "not_released" if "not_released" in found else "sold_out"


def evaluate(
    spec: BuntzenSpec, inventories: Mapping[BuntzenQuery, BuntzenInventory], now: datetime
) -> tuple[Option, ...]:
    """The watch's options, wanted first, then the unwanted ones on a place it queries (for context).

    The keys and their order depend on the spec alone, never on which inventories are present.
    """
    at = instant(now)
    places = {q.place_id for q in queries(spec)}
    wanted = [p for p in PASSES if p in spec.passes]
    context = [p for p in PASSES if p not in spec.passes and PLACE_OF[p] in places]
    options = []
    for p in (*wanted, *context):
        status: OptionStatus
        detail: str | None = None
        query = BuntzenQuery(place_id=PLACE_OF[p], date=spec.date.isoformat())
        inventory = inventories.get(query)
        if inventory is not None and (inventory.place_id, inventory.date) != (query.place_id, query.date):
            inventory = None  # an inventory filed under the wrong query is no answer for this one
        if at >= stop_at(spec.date, p):
            status = "over"
        elif inventory is None:
            status = "not_released" if at < release_at(spec.date) else "unchecked"
        else:
            status, detail = _status(p, inventory)
        options.append(
            Option(key=p, label=LABELS[p], status=status, wanted=p in spec.passes, booking_url=PAGES[p], detail=detail)
        )
    return tuple(options)


def booking_url(spec: BuntzenSpec, options: Sequence[Option]) -> str:
    """The first wanted open (or maybe open) option's page, else the page of the watch's main pass."""
    for option in options:
        page = _PAGE_OF_KEY.get(option.key)  # a constant of ours, never the option's own link
        if option.wanted and option.status in ALERTABLE and page is not None:
            return page
    return FULL_DAY_PAGE if "full_day" in spec.passes else HALF_DAY_PAGE
