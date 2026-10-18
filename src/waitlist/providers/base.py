"""What every Waitlist provider is (docs/waitlist/design.md §2.3). Written verbatim from the design contract.

A *provider* is a reservation system Waitlist can watch: BC Ferries, Buntzen Lake. A *watch* (a `trips` row) is
one thing a user wants on one provider: a sailing on a date, a parking pass on a date. Each provider's *checker*
says how its watches are checked:

- "browser": a disposable browser pod per watch, driven by the agent (BC Ferries only). Its scheduling, results,
  carts and checkout are the existing BC Ferries path; it registers only its `ProviderMeta` here.
- "http": waitlist-control decides when a watch is due and asks waitlist-fetcher (no database, no secrets, no
  Kubernetes) to make the provider's request. The fetcher parses the response into the provider's typed
  *inventory*; control evaluates it into *options* and alerts. Everything below is that contract.

This module is pure: no I/O, no aiohttp, no database. A provider's client (its request builder and response
parser) implements `Fetcher` against `Transport`, which the fetcher supplies, so a provider never opens a socket
itself and can only reach the hosts it declares.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, TypeVar

import msgspec

from waitlist.models import ValidationError

# ---- vocabulary

PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
ProviderId = Annotated[str, msgspec.Meta(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
OptionKey = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,31}$")]
Label = Annotated[str, msgspec.Meta(min_length=1, max_length=64)]
# Short, plain text. Never HTML, never a URL; React escapes it anyway. Never placed in a notification.
Detail = Annotated[str, msgspec.Meta(max_length=64, pattern=r"^[A-Za-z0-9 .,:;'()/&+-]*$")]
HttpsUrl = Annotated[str, msgspec.Meta(max_length=300, pattern=r"^https://[a-z0-9.-]+/[A-Za-z0-9._~%/+-]*$")]

Checker = Literal["browser", "http"]
Urgency = Literal["very-low", "low", "normal", "high"]  # Web Push's, as waitlist.web.push has it

# What one option (a pass, a sailing time...) looks like at the last check.
OptionStatus = Literal[
    "open",  # bookable now: alert
    "maybe",  # the site said something we don't recognise that isn't "sold out": alert, worded as "may be"
    "sold_out",
    "not_released",  # not on sale yet (before the release time, or the site says "coming soon")
    "not_offered",  # the site offers no such option for this date (out of season, say)
    "over",  # too late to be useful (the pass period has ended)
    "unchecked",  # no result yet
]
ALERTABLE: frozenset[str] = frozenset({"open", "maybe"})

# Where an HTTP-checked watch's checking is (waitlist.control.checkplan.check_state; the SPA shows it).
CheckState = Literal[
    "pending",  # checkable, never checked yet
    "waiting_release",  # before window.opens_at
    "checking",
    "error",  # checked, but the last BACKOFF_AFTER or more checks failed
    "backoff",  # failing, or the provider is cooling down after a 429/403: next check later
    "quiet",  # in the owner's quiet hours, not lifted
    "paused",  # the watch is paused, or its owner suspended
    "provider_paused",  # maintenance, or an admin paused the provider
    "unavailable",  # checks for the provider aren't running (no fresh check report, or waitlist-fetcher is down)
    "over",  # after window.closes_at
]
# What the last check of a watch found (check_results.outcome).
CheckOutcome = Literal["pending", "ok", "no_inventory", "error"]

# Why a fetch failed. Closed on purpose: nothing a site says crosses to control as text.
FetchErrorKind = Literal[
    "site_changed",  # a response we can't read: a new shape, HTML where JSON was, a 4xx meaning the API moved
    "rate_limited",  # 429, or the fetcher's own per-provider cap
    "blocked",  # 401/403: a WAF or geo-block
    "upstream_error",  # 5xx
    "timeout",
    "network",  # includes a host that resolved to a non-public address
    "too_large",
    "unexpected",
]
# A check's error as control records it: the fetch's, or control's own trouble reaching the fetcher.
CheckErrorKind = Literal[
    "site_changed",
    "rate_limited",
    "blocked",
    "upstream_error",
    "timeout",
    "network",
    "too_large",
    "unexpected",
    "fetcher_unavailable",  # ours: waitlist-fetcher didn't answer
    "bad_reply",  # ours: waitlist-fetcher answered with something that isn't a valid reply
]

# Notification kinds control queues for http providers, rendered by the watch's provider (params as control
# writes them; the provider re-checks them):
OPENING = "opening"  # {"options": [OptionKey, ...], "uncertain": [OptionKey, ...]}; uncertain ⊆ options, ≤ 10 each
RELEASE_REMINDER = "release_reminder"  # {}
FAILING = "failing"  # {"error_class": CheckErrorKind}
NOTICE_KINDS = frozenset({OPENING, RELEASE_REMINDER, FAILING})
# Admin-only kinds control queues, rendered by waitlist-web itself (never by a provider):
SITE_CHANGED = "site_changed"  # {"provider": ProviderId}
CHECKER_DOWN = "checker_down"  # {"provider": ProviderId, "since": epoch seconds}
ADMIN_NOTICE_KINDS = frozenset({SITE_CHANGED, CHECKER_DOWN})


class SpecError(ValidationError):
    """A watch spec that can't be accepted. `field` names the spec key; waitlist-web answers 400 with it."""


class FetchError(Exception):
    """A fetch that didn't produce an inventory. Raised by a Transport or a Fetcher; crosses the wire as `kind`."""

    def __init__(self, kind: FetchErrorKind, *, http_status: int | None = None, retry_after_s: int | None = None):
        super().__init__(kind)
        self.kind: FetchErrorKind = kind
        self.http_status = http_status
        self.retry_after_s = retry_after_s


# ---- what a provider is


@dataclass(frozen=True, slots=True)
class Capabilities:
    auto_hold: bool  # can hold a booking in a cart for the user (BC Ferries' carts)
    checkout: bool  # has a checkout viewer (VNC or the streamed viewer)
    alert_only: bool  # only alerts; the user books on the provider's site

    def __post_init__(self) -> None:
        if self.alert_only and (self.auto_hold or self.checkout):
            raise ValueError("An alert-only provider can't hold or check out")

    def to_json(self) -> dict[str, bool]:
        return {"auto_hold": self.auto_hold, "checkout": self.checkout, "alert_only": self.alert_only}


@dataclass(frozen=True, slots=True)
class ProviderMeta:
    """What the picker, the catalog and the limits need to know about a provider."""

    id: str  # "bcferries", "buntzen": trips.provider
    name: str  # "Buntzen Lake"
    noun: str  # what one watch is on its card: "trip", "pass"
    description: str  # one line, for the provider picker
    checker: Checker
    capabilities: Capabilities
    limit_key: str  # the waitlist.control.limits.LimitKey that caps this provider's watches per user
    booking_prefixes: tuple[str, ...]  # every booking link this provider makes starts with one of these
    horizon_days: int = 30  # how far ahead a watch's date may be

    def __post_init__(self) -> None:
        if not PROVIDER_ID.fullmatch(self.id):
            raise ValueError(f"Not a provider id: {self.id!r}")
        if not self.booking_prefixes:
            raise ValueError("A provider needs at least one booking prefix")
        for prefix in self.booking_prefixes:
            if not re.fullmatch(r"https://[a-z0-9.-]+/([A-Za-z0-9._~%+-]+/)*", prefix):
                raise ValueError(f"A booking prefix must be an https origin and a path ending in /: {prefix!r}")
        if not 1 <= self.horizon_days <= 120:
            raise ValueError("horizon_days must be from 1 to 120")

    def allows_booking_url(self, url: str) -> bool:
        return any(url.startswith(prefix) for prefix in self.booking_prefixes)

    def to_json(self) -> dict[str, Any]:
        """The catalog entry (GET /api/providers); waitlist-web adds `offered` and `paused`."""
        return {
            "id": self.id,
            "name": self.name,
            "noun": self.noun,
            "description": self.description,
            "checker": self.checker,
            "capabilities": self.capabilities.to_json(),
            "horizon_days": self.horizon_days,
        }


@dataclass(frozen=True, slots=True)
class Budget:
    """How hard control may lean on a provider's site, in requests (distinct queries) a minute."""

    default_per_minute: int
    max_per_minute: int  # the ceiling a deployment's config can't pass
    concurrency: int = 2  # fetches in flight at once

    def __post_init__(self) -> None:
        if not 1 <= self.default_per_minute <= self.max_per_minute <= 60 or not 1 <= self.concurrency <= 4:
            raise ValueError("A budget is 1-60 a minute (default within the ceiling) and 1-4 at once")


@dataclass(frozen=True, slots=True)
class CheckWindow:
    """When checking a watch is useful: from `opens_at` (e.g. the release time) until `closes_at`."""

    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        if self.opens_at.tzinfo is None or self.closes_at.tzinfo is None:
            raise ValueError("A check window's times must be timezone-aware")
        if self.closes_at <= self.opens_at:
            raise ValueError("A check window must close after it opens")

    def phase(self, now: datetime) -> Literal["before", "open", "after"]:
        if now < self.opens_at:
            return "before"
        return "open" if now < self.closes_at else "after"


class Option(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    """One bookable thing as the last check saw it. Stored in check_results.options and shown as is."""

    key: OptionKey  # stable across checks: what alert memory remembers ("morning", "08:30")
    label: Label  # "Morning pass": the provider's own words, never the site's
    status: OptionStatus
    wanted: bool  # the user asked for it (only wanted options alert)
    booking_url: HttpsUrl | None = None  # a constant of the provider's, never from a response
    detail: Detail | None = None  # e.g. the site's unrecognised status, sanitised; shown on the card only


MAX_OPTIONS = 20


@dataclass(frozen=True, slots=True)
class Notice:
    """What a notification says. The outbox turns it into a push and, for some kinds, an email."""

    title: str  # at most 120 characters
    body: str  # at most 300 characters
    url: str  # what a push opens: always the watch's own page on the site (`watch_url`)
    urgency: Urgency = "normal"
    ttl: int = 3600  # seconds a push service keeps it for an offline device
    topic: str | None = None  # replaces an earlier push with the same topic; [A-Za-z0-9_-]{1,32}
    also: tuple[str, str] | None = None  # (label, url) for email only: a booking link, within booking_prefixes

    def __post_init__(self) -> None:
        if not self.title or len(self.title) > 120 or len(self.body) > 300:
            raise ValueError("A notice's title is 1-120 characters and its body at most 300")
        if self.topic is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.topic):
            raise ValueError("A topic is 1-32 letters, digits, - or _")


# ---- the HTTP checker

SpecT = TypeVar("SpecT")
QueryT = TypeVar("QueryT", bound=msgspec.Struct)
InventoryT = TypeVar("InventoryT", bound=msgspec.Struct)
QueryT_contra = TypeVar("QueryT_contra", bound=msgspec.Struct, contravariant=True)
InventoryT_co = TypeVar("InventoryT_co", bound=msgspec.Struct, covariant=True)


class HttpProvider(Protocol[SpecT, QueryT, InventoryT]):
    """A provider checked over HTTP. Pure: imported by waitlist-web, waitlist-control and waitlist-fetcher alike.

    `SpecT` is the provider's own watch spec (frozen). `QueryT` is one upstream request's inputs: a frozen
    msgspec Struct (hashable, so control can coalesce identical queries across watches and users).
    `InventoryT` is what the fetcher parsed from one response: a msgspec Struct, bounded, closed vocabulary.

    Every `now` passed in is timezone-aware (callers pass `datetime.fromtimestamp(t, UTC)`); convert it with
    `.astimezone(PACIFIC)`, and never compare it with a naive datetime.
    """

    @property
    def meta(self) -> ProviderMeta: ...
    @property
    def budget(self) -> Budget: ...
    @property
    def query_type(self) -> type[QueryT]: ...
    @property
    def inventory_type(self) -> type[InventoryT]: ...

    # -- the watch

    def parse_spec(self, data: Mapping[str, Any], *, base: SpecT | None, now: datetime) -> SpecT:
        """A spec from the user (POST/PATCH /api/watches), every field validated, unknown keys refused.

        `base`: a partial update on top of the current spec. `now`: for the date rules (no past dates, within
        the horizon, not already over). Raises SpecError(field, message). Never sees `id` or `enabled`.
        """
        ...

    def load_spec(self, stored: Mapping[str, Any]) -> SpecT:
        """A spec as stored in trips.spec (dump_spec's output), validated again but without the date rules,
        so yesterday's watch still loads. Raises SpecError."""
        ...

    def dump_spec(self, spec: SpecT) -> dict[str, Any]:
        """The JSON object stored in trips.spec and sent as the watch's `spec`. load_spec(dump_spec(s)) == s."""
        ...

    def search_key(self, spec: SpecT) -> tuple[object, ...]:
        """What defines the search: an edit that changes it forgets what was alerted (alert memory) and hides the
        last result (search_hash). Parts are str, int, float, bool, None or dates."""
        ...

    def watch_date(self, spec: SpecT) -> date:
        """The date the user wants (Pacific): the dashboard sorts by it."""
        ...

    def title(self, spec: SpecT) -> str:
        """Plain words for admin pages and logs, e.g. "Buntzen Lake, Thu Jul 1: morning pass"."""
        ...

    # -- scheduling (control's check loop)

    def window(self, spec: SpecT) -> CheckWindow:
        """When checks are useful. Control checks nothing before `opens_at` and stops at `closes_at`."""
        ...

    def interval(self, spec: SpecT, now: datetime) -> timedelta:
        """The poll curve: how long after a check at `now` the next is due. Control never goes below 30 s,
        adds ±10 % jitter, and stretches it to stay within the budget."""
        ...

    def reminder_at(self, spec: SpecT) -> datetime | None:
        """When to send RELEASE_REMINDER for this watch (once), or None."""
        ...

    # -- checking

    def queries(self, spec: SpecT) -> tuple[QueryT, ...]:
        """The upstream requests this watch needs, 1-4 of them, deduplicated."""
        ...

    def evaluate(self, spec: SpecT, inventories: Mapping[QueryT, InventoryT], now: datetime) -> tuple[Option, ...]:
        """The watch's options from what was fetched. Total: an option whose query has no inventory here is
        "unchecked" (or "not_released"/"over" by time); control reads "unchecked" as "not seen this round" and
        keeps its alert memory. Wanted options first; at most MAX_OPTIONS."""
        ...

    def booking_url(self, spec: SpecT, options: Sequence[Option]) -> str | None:
        """Where to book it yourself, within meta.booking_prefixes: the first open option's link, else the
        watch's default page."""
        ...

    # -- what users read

    def render(self, kind: str, params: Mapping[str, Any], spec: SpecT, *, watch_url: str) -> Notice | None:
        """The notice for one notification of NOTICE_KINDS (params as control writes them, re-checked here), or
        None for a kind with nothing to say to the user. Every link is `watch_url` or a booking constant."""
        ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What a Transport hands back: the status, the media type (lowercased, no parameters) and at most the
    transport's byte limit of body. Redirects are never followed; a 3xx arrives as itself."""

    status: int
    content_type: str
    body: bytes
    retry_after_s: int | None = None  # a 429's or 503's Retry-After in seconds, if it gave one (0-3600)


class Transport(Protocol):
    """The fetcher's HTTP client, as a provider sees it. It refuses (FetchError "unexpected") any URL that isn't
    https on port 443 at one of the fetcher's declared hosts, connects only to public addresses (FetchError
    "network" otherwise), follows no redirects, sends no cookies, reads the whole body up to its byte limit
    (FetchError "too_large" past it), enforces its timeout (FetchError "timeout"), and never logs bodies."""

    async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse: ...

    async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse: ...


class Fetcher(Protocol[QueryT_contra, InventoryT_co]):
    """A provider's client: builds the request for a query, makes it through the transport, and parses the
    response defensively into an inventory. Imported only by waitlist-fetcher (an import-linter contract)."""

    @property
    def provider_id(self) -> str: ...
    @property
    def hosts(self) -> tuple[str, ...]: ...  # the only hosts its requests go to (the fetcher's egress allowlist)

    async def fetch(self, transport: Transport, query: QueryT_contra) -> InventoryT_co:
        """One query's inventory, or FetchError. A response it can't read is "site_changed", never an empty
        inventory: "nothing open" must only ever mean the site said so."""
        ...


def search_hash(key: tuple[object, ...]) -> str:
    """A short, stable digest of a search_key (check_results.search_hash), so a result made for an earlier search
    is never shown for the current one. A search_key's parts are str, int, float, bool, None or dates: never sets,
    whose order isn't stable between processes."""
    text = json.dumps(list(key), default=str, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def check_status(response: HttpResponse) -> None:
    """The same reading of HTTP statuses for every provider. Returns for 200; raises FetchError otherwise."""
    status = response.status
    if status == 200:
        return
    if status == 429:
        raise FetchError("rate_limited", http_status=status, retry_after_s=response.retry_after_s)
    if status in (401, 403):
        raise FetchError("blocked", http_status=status)
    if status in (400, 404, 405, 406, 410, 415, 422, 426) or 300 <= status < 400:
        raise FetchError("site_changed", http_status=status)
    if status == 503:
        raise FetchError("upstream_error", http_status=status, retry_after_s=response.retry_after_s)
    if status >= 500:
        raise FetchError("upstream_error", http_status=status)
    raise FetchError("unexpected", http_status=status)
