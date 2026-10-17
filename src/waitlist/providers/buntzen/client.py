"""Buntzen Lake's client: the request to Yodel and the defensive parser of its answer (design.md §5.3-5.4).

Only waitlist-fetcher imports this module (an import-linter contract): it reads what a site sends. It makes no
I/O of its own; the fetcher's `Transport` does, to `api.yodelpass.com` only. The parser fails loudly: an answer
it can't read is `site_changed`, never an empty inventory, so "nothing open" only ever means Yodel said so.

The request is the one the owner's Home Assistant automation made for years (placeId as a string, exactly its
three headers). Yodel's response shape beyond the two fields that automation read (`catalogItemStatus`,
`catalogItemCategory` in `catalogItemExtendedResultLst`) is unverified; tests/fixtures/buntzen/ says which
fixtures are synthesized and how to capture real ones (scripts/capture_yodel.py).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Final, Literal

import msgspec

from waitlist.providers.base import Fetcher, FetchError, HttpResponse, Transport, check_status

from .meta import API_HOST, HOSTS, ID
from .model import FULL_DAY_PLACE, MAX_ITEMS, BuntzenInventory, BuntzenQuery, PassItem

USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
API_URL: Final = f"https://{API_HOST}/api/portal-catalog-items"
API_VERSION: Final = "5.6"

LIST_KEY: Final = "catalogItemExtendedResultLst"
STATUS_KEY: Final = "catalogItemStatus"
MAX_ENTRIES: Final = 50
STATUS_CHARS: Final = 32

Period = Literal["full_day", "morning", "afternoon", "unknown"]
Availability = Literal["open", "sold_out", "not_released", "unknown"]

# Where the period may be named, in the order they are read.
PERIOD_FIELDS: Final = ("catalogItemCategory", "catalogItemName", "name", "title", "description", STATUS_KEY)


def _words(*words: str) -> re.Pattern[str]:
    """Whole words or phrases in normalised text ("8 am" or "half day" are never period words)."""
    return re.compile(rf"(?<![a-z0-9])(?:{'|'.join(words)})(?![a-z0-9])")


_PERIOD_TOKENS: Final[tuple[tuple[Literal["full_day", "morning", "afternoon"], re.Pattern[str]], ...]] = (
    ("full_day", _words("all day", "full day", "allday", "daily")),
    ("morning", _words("morning")),
    ("afternoon", _words("afternoon")),
)

_SOLD_OUT: Final = frozenset(
    {"sold out", "soldout", "full", "unavailable", "not available", "no availability", "closed"}
)
_NOT_RELEASED: Final = frozenset({"coming soon", "not yet available", "not released", "upcoming", "opens soon"})
_OPEN: Final = frozenset(
    {
        "available",
        "book now",
        "open",
        "limited",
        "limited availability",
        "few left",
        "almost full",
        "selling fast",
        # The Home Assistant quirk: while a pass is on sale its status is its period.
        "morning",
        "afternoon",
        "all day",
        "full day",
    }
)
QUANTITY_FIELDS: Final = ("availableQuantity", "quantityAvailable", "remainingQuantity", "availableCount", "remaining")
SUCCESS_FLAGS: Final = ("isSuccess", "success", "succeeded")

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WHITESPACE = re.compile(r"\s+")
_NOT_DETAIL = re.compile(r"[^A-Za-z0-9 .,:;'()/&+-]")


def build_request(query: BuntzenQuery) -> tuple[str, dict[str, Any], dict[str, str]]:
    """(url, payload, headers) for one query: Home Assistant's request, byte for byte in the payload."""
    payload: dict[str, Any] = {
        "userId": 0,
        "placeId": str(query.place_id),
        "reservationDate": query.date,
        "catalogItemType": 0,
        "scopeName": "buntzen lake",
        "filterStartDate": None,
        "filterEndDate": None,
    }
    headers = {"user-agent": USER_AGENT, "x-api-version": API_VERSION, "content-type": "application/json"}
    return API_URL, payload, headers


def normalise(text: str) -> str:
    """Lowercase, '-' and '_' as spaces, whitespace collapsed."""
    return _WHITESPACE.sub(" ", text.lower().replace("-", " ").replace("_", " ")).strip()


def sanitise_status(text: str) -> str:
    """The site's status as base.Detail allows it: safe characters only, whitespace collapsed, 32 at most."""
    kept = _NOT_DETAIL.sub("", _WHITESPACE.sub(" ", text))
    return _WHITESPACE.sub(" ", kept).strip()[:STATUS_CHARS].strip()


def period_of(entry: dict[str, Any]) -> Period:
    """The first field naming exactly one period decides; a field naming two is skipped; none is 'unknown'."""
    for field in PERIOD_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str):
            continue
        text = normalise(value)
        named = {period for period, pattern in _PERIOD_TOKENS if pattern.search(text)}
        if len(named) == 1:
            return named.pop()
    return "unknown"


def availability_of(entry: dict[str, Any]) -> Availability:
    """From the status words; a quantity only when the words say nothing we know."""
    status = normalise(entry[STATUS_KEY])
    if status in _SOLD_OUT or status.startswith("sold out"):
        return "sold_out"
    if status in _NOT_RELEASED:
        return "not_released"
    if status in _OPEN:
        return "open"
    for field in QUANTITY_FIELDS:
        value = entry.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return "open" if value > 0 else "sold_out"
    return "unknown"


def _catalog(root: dict[str, Any], changed: FetchError) -> list[Any]:
    """The catalog list: at the top level, under `content`, or under `data` (the envelope is unverified)."""
    holder: dict[str, Any] | None = root if LIST_KEY in root else None
    if holder is None:
        for wrapper in ("content", "data"):
            inner = root.get(wrapper)
            if isinstance(inner, dict) and LIST_KEY in inner:
                holder = inner
                break
    if holder is None:
        raise changed
    if holder is not root and any(holder.get(flag) is False for flag in SUCCESS_FLAGS):
        raise changed
    entries = holder[LIST_KEY]
    if entries is None:
        return []
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise changed
    return entries


def parse_catalog(response: HttpResponse, query: BuntzenQuery) -> BuntzenInventory:
    """One response read defensively (design.md §5.4); FetchError for anything it can't vouch for."""
    check_status(response)
    changed = FetchError("site_changed", http_status=response.status)
    if response.content_type == "text/html":
        raise changed
    try:
        root = msgspec.json.decode(response.body)
    except (msgspec.DecodeError, ValueError, RecursionError):
        raise changed from None
    if not isinstance(root, dict):
        raise changed
    if any(root.get(flag) is False for flag in SUCCESS_FLAGS):
        raise changed  # the API refused the request (an obsolete x-api-version, say)
    items: list[PassItem] = []
    for entry in _catalog(root, changed):
        if not isinstance(entry, dict) or not isinstance(entry.get(STATUS_KEY), str):
            raise changed
        period = period_of(entry)
        if query.place_id == FULL_DAY_PLACE:
            period = "full_day"
        elif period == "full_day":
            period = "unknown"
        availability = availability_of(entry)
        if query.place_id != FULL_DAY_PLACE and period == "unknown" and availability in ("open", "unknown"):
            raise changed  # something may be bookable, and we can't say which pass
        items.append(PassItem(period=period, availability=availability, status=sanitise_status(entry[STATUS_KEY])))
    inventory = {
        "place_id": query.place_id,
        "date": query.date,
        "items": [msgspec.to_builtins(i) for i in items[:MAX_ITEMS]],
    }
    try:  # built through convert, so every bound of the Structs is checked, not just assumed
        return msgspec.convert(inventory, BuntzenInventory, strict=True)
    except msgspec.ValidationError:
        raise changed from None


class Client:
    """Buntzen's `Fetcher`: build_request, then transport.post_json, then parse_catalog."""

    @property
    def provider_id(self) -> str:
        return ID

    @property
    def hosts(self) -> tuple[str, ...]:
        return HOSTS

    async def fetch(self, transport: Transport, query: BuntzenQuery) -> BuntzenInventory:
        if not _ISO_DATE.fullmatch(query.date):
            raise FetchError("unexpected")
        try:
            date.fromisoformat(query.date)
        except ValueError:
            raise FetchError("unexpected") from None
        url, payload, headers = build_request(query)
        response = await transport.post_json(url, payload, headers=headers)
        return parse_catalog(response, query)


FETCHER: Fetcher[BuntzenQuery, BuntzenInventory] = Client()
