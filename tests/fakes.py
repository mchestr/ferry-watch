"""Test doubles for the browser layer, and an HTTP-checked provider for the check loop and the watch API."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import msgspec

from waitlist.bcferries.errors import SessionExpired
from waitlist.bcferries.urls import CheckoutView
from waitlist.models import PACIFIC, Fare, FarePreference, Sailing, SailingStatus, Trip
from waitlist.protocol import ReleaseCheck
from waitlist.providers import Registry
from waitlist.providers.base import (
    ALERTABLE,
    Budget,
    CheckWindow,
    HttpProvider,
    Notice,
    Option,
    OptionStatus,
    ProviderMeta,
    SpecError,
)
from waitlist.providers.bcferries import META as BCFERRIES
from waitlist.providers.buntzen.meta import BUDGET, FULL_DAY_PAGE, HALF_DAY_PAGE
from waitlist.providers.buntzen.meta import META as BUNTZEN_META


def sailing(hour: int, *, available: bool = True) -> Sailing:
    status = SailingStatus.AVAILABLE if available else SailingStatus.SOLD_OUT
    return Sailing(
        time(hour, 0),
        time(hour + 1, 35),
        "Spirit of Vancouver Island",
        status,
        "" if available else "Advance bookings sold out",
        "$93.45" if available else None,
        (Fare("Saver", "$93.45", selectable=True),),
    )


class FakeSession:
    def __init__(self, site: FakeSite, name: str) -> None:
        self.site = site
        self.name = name
        self.calls: list[str] = []
        self.preferences: list[FarePreference] = []
        self.snapshots: list[str] = []
        self._closed = False
        self._lost = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def lost(self) -> bool:
        return self._lost

    async def search(self, trip: Trip) -> list[Sailing]:
        self.calls.append("search")
        await self.site.on_search(trip)
        return self.site.results()

    async def refresh(self) -> list[Sailing]:
        self.calls.append("refresh")
        if self.site.expire_next_refresh:
            self.site.expire_next_refresh = False
            raise SessionExpired("expired")
        return self.site.results()

    async def add_to_cart(self, depart: time, preference: FarePreference) -> Fare:
        self.calls.append(f"cart {depart:%H:%M}")
        self.preferences.append(preference)
        await self.site.on_cart()
        if self.site.cart_error:
            raise self.site.cart_error
        return Fare("Saver", "$93.45", selectable=True)

    async def empty_cart(self) -> ReleaseCheck:
        self.calls.append("empty cart")
        if self.site.empty_error:
            raise self.site.empty_error
        return self.site.empty_check

    def checkout_view(self) -> CheckoutView:
        return self.site.view

    async def save_debug_snapshot(self, reason: str) -> Path | None:
        self.calls.append("snapshot")
        self.snapshots.append(reason)
        return None

    async def close(self) -> None:
        self.calls.append("close")
        self._closed = True

    # test controls
    def user_closes_window(self) -> None:
        self._closed = True

    def crash(self) -> None:
        self._closed = True
        self._lost = True


@dataclass
class FakeSite:
    sailings: list[Sailing] = field(default_factory=list)
    error: Exception | None = None
    cart_error: Exception | None = None
    expire_next_refresh: bool = False
    empty_check: ReleaseCheck = "landing_url"
    empty_error: Exception | None = None
    view: CheckoutView = field(default_factory=CheckoutView)  # what a held cart's checkout shows
    sessions: list[FakeSession] = field(default_factory=list)
    search_hook: Any = None
    cart_hook: Any = None

    async def new_session(self, name: str) -> FakeSession:
        session = FakeSession(self, name)
        self.sessions.append(session)
        return session

    def results(self) -> list[Sailing]:
        if self.error:
            raise self.error
        return list(self.sailings)

    async def on_search(self, trip: Trip) -> None:
        """Runs `search_hook` while "on the page"; an async hook can hold the search up (e.g. a queue)."""
        if self.search_hook:
            result = self.search_hook(trip)
            if inspect.isawaitable(result):
                await result

    async def on_cart(self) -> None:
        if self.cart_hook:
            result = self.cart_hook()
            if inspect.isawaitable(result):
                await result

    @property
    def session(self) -> FakeSession:
        return self.sessions[-1]


# ---- an HTTP-checked provider (docs/waitlist/design.md §9.1, "Interim")

Part = Literal["a", "b"]
PARTS: tuple[Part, ...] = ("a", "b")
FAKE_PAGES = {"a": FULL_DAY_PAGE, "b": HALF_DAY_PAGE}  # constants within buntzen's booking prefixes


@dataclass(frozen=True, slots=True)
class FakeSpec:
    date: date


class FakeQuery(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    date: str
    part: Part


class FakeInventory(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    date: str
    part: Part
    status: OptionStatus = "sold_out"  # what the site says of this part's one option


def _aware(now: datetime) -> None:
    assert now.tzinfo is not None, "a provider is always handed a timezone-aware datetime (design.md §2.2 rule 7)"


class FakeHttpProvider:
    """A complete HttpProvider with a one-field spec {"date": "YYYY-MM-DD"}. Each watch makes two queries, parts "a"
    and "b" (the shape of Buntzen's two places), and each part's inventory carries one option, keyed by the part.
    Checkable from 07:00 PT the day before until 18:00 PT on the day; every 60 s; a reminder at 06:55 PT the day
    before. Its meta is Buntzen's, so its rows pass trips_provider_check."""

    meta: ProviderMeta = BUNTZEN_META
    budget: Budget = BUDGET
    query_type: type[FakeQuery] = FakeQuery
    inventory_type: type[FakeInventory] = FakeInventory

    def parse_spec(self, data: Mapping[str, Any], *, base: FakeSpec | None, now: datetime) -> FakeSpec:
        _aware(now)
        if not isinstance(data, Mapping):
            raise SpecError("spec", "Expected a JSON object")
        unknown = sorted(set(data) - {"date"})
        if unknown:
            raise SpecError(unknown[0], "Unknown field")
        if "date" not in data:
            if base is None:
                raise SpecError("date", "Required")
            return base
        spec = self._date(data["date"])
        today = now.astimezone(PACIFIC).date()
        if base is None or spec.date != base.date:
            if spec.date < today:
                raise SpecError("date", "Pick today or a later date")
            if spec.date > today + timedelta(days=self.meta.horizon_days):
                raise SpecError("date", "Pick a date from today to 30 days ahead")
        return spec

    @staticmethod
    def _date(value: Any) -> FakeSpec:
        if not isinstance(value, str):
            raise SpecError("date", "Use a date like 2027-07-01")
        try:
            return FakeSpec(date.fromisoformat(value))
        except ValueError:
            raise SpecError("date", "That date doesn't exist") from None

    def load_spec(self, stored: Mapping[str, Any]) -> FakeSpec:
        if set(stored) != {"date"}:
            raise SpecError("spec", "Not a stored spec")
        return self._date(stored["date"])

    def dump_spec(self, spec: FakeSpec) -> dict[str, Any]:
        return {"date": spec.date.isoformat()}

    def search_key(self, spec: FakeSpec) -> tuple[object, ...]:
        return (spec.date,)

    def watch_date(self, spec: FakeSpec) -> date:
        return spec.date

    def title(self, spec: FakeSpec) -> str:
        return f"Fake watch, {spec.date:%a %b %-d}"

    def window(self, spec: FakeSpec) -> CheckWindow:
        return CheckWindow(
            opens_at=datetime.combine(spec.date - timedelta(days=1), time(7, 0), tzinfo=PACIFIC),
            closes_at=datetime.combine(spec.date, time(18, 0), tzinfo=PACIFIC),
        )

    def interval(self, spec: FakeSpec, now: datetime) -> timedelta:  # noqa: ARG002 - a flat curve
        _aware(now)
        return timedelta(seconds=60)

    def reminder_at(self, spec: FakeSpec) -> datetime | None:
        return datetime.combine(spec.date - timedelta(days=1), time(6, 55), tzinfo=PACIFIC)

    def queries(self, spec: FakeSpec) -> tuple[FakeQuery, ...]:
        return tuple(FakeQuery(date=spec.date.isoformat(), part=part) for part in PARTS)

    def evaluate(
        self, spec: FakeSpec, inventories: Mapping[FakeQuery, FakeInventory], now: datetime
    ) -> tuple[Option, ...]:
        _aware(now)
        window = self.window(spec)
        options = []
        for query in self.queries(spec):
            inventory = inventories.get(query)
            status: OptionStatus
            if now >= window.closes_at:
                status = "over"
            elif inventory is None:
                status = "not_released" if now < window.opens_at else "unchecked"
            else:
                status = inventory.status
            options.append(
                Option(
                    key=query.part,
                    label=f"Part {query.part}",
                    status=status,
                    wanted=True,
                    booking_url=FAKE_PAGES[query.part],
                    detail="Waitlist only" if status == "maybe" else None,
                )
            )
        return tuple(options)

    def booking_url(self, spec: FakeSpec, options: Sequence[Option]) -> str | None:  # noqa: ARG002 - one page each
        return next((o.booking_url for o in options if o.wanted and o.status in ALERTABLE), FULL_DAY_PAGE)

    def render(self, kind: str, params: Mapping[str, Any], spec: FakeSpec, *, watch_url: str) -> Notice | None:
        if kind == "opening":
            options = params.get("options")
            if not isinstance(options, list) or not options or not set(options) <= set(PARTS):
                return None
            return Notice(
                title=f"Fake: {', '.join(options)} open for {spec.date:%a %b %-d}",
                body="Book it now.",
                url=watch_url,
                urgency="high",
                ttl=900,
                also=("Book it", FAKE_PAGES[options[0]]),
            )
        if kind == "release_reminder":
            return Notice(title="Fake: on sale at 7:00 am", body="Get ready.", url=watch_url, urgency="high", ttl=600)
        if kind == "failing":
            return Notice(title="Fake: having trouble", body="We'll keep trying.", url=watch_url, urgency="low")
        return None


def fake_registry(provider: HttpProvider[Any, Any, Any] | None = None) -> Registry:
    """BC Ferries and the fake (as "buntzen"), as Registry.of builds the real one."""
    return Registry.of(browser=[BCFERRIES], http=[provider or FakeHttpProvider()])
