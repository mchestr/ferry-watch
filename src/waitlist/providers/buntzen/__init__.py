"""Buntzen Lake: BC Hydro's day-use parking passes, sold on Yodel (design.md §5). Alert-only.

A watch is one date and any of the all-day, morning and afternoon passes. It is checked over HTTP: from 07:00
Pacific the day before (when passes go on sale) until each pass's stop time, by waitlist-control through
waitlist-fetcher, which runs this provider's client (`.client`). This module never imports `.client`: control
and web import `waitlist.providers`, and the client is the fetcher's alone (design.md §2.2 rule 2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from waitlist.providers.base import Budget, CheckWindow, HttpProvider, Notice, Option, ProviderMeta

from . import messages as _messages
from . import policy as _policy
from . import spec as _spec
from .meta import BUDGET, META
from .model import BuntzenInventory, BuntzenQuery
from .spec import BuntzenSpec

__all__ = ["PROVIDER", "BuntzenInventory", "BuntzenProvider", "BuntzenQuery", "BuntzenSpec"]


class BuntzenProvider:
    """`HttpProvider[BuntzenSpec, BuntzenQuery, BuntzenInventory]`: pure, every method a plain function."""

    @property
    def meta(self) -> ProviderMeta:
        return META

    @property
    def budget(self) -> Budget:
        return BUDGET

    @property
    def query_type(self) -> type[BuntzenQuery]:
        return BuntzenQuery

    @property
    def inventory_type(self) -> type[BuntzenInventory]:
        return BuntzenInventory

    # -- the watch

    def parse_spec(self, data: Mapping[str, Any], *, base: BuntzenSpec | None, now: datetime) -> BuntzenSpec:
        return _spec.parse_spec(data, base=base, now=now)

    def load_spec(self, stored: Mapping[str, Any]) -> BuntzenSpec:
        return _spec.load_spec(stored)

    def dump_spec(self, spec: BuntzenSpec) -> dict[str, Any]:
        return _spec.dump_spec(spec)

    def search_key(self, spec: BuntzenSpec) -> tuple[object, ...]:
        return _spec.search_key(spec)

    def watch_date(self, spec: BuntzenSpec) -> date:
        return _spec.watch_date(spec)

    def title(self, spec: BuntzenSpec) -> str:
        return _spec.title(spec)

    # -- scheduling

    def window(self, spec: BuntzenSpec) -> CheckWindow:
        return _policy.window(spec)

    def interval(self, spec: BuntzenSpec, now: datetime) -> timedelta:
        return _policy.interval(spec, now)

    def reminder_at(self, spec: BuntzenSpec) -> datetime | None:
        return _policy.reminder_at(spec)

    # -- checking

    def queries(self, spec: BuntzenSpec) -> tuple[BuntzenQuery, ...]:
        return _policy.queries(spec)

    def evaluate(
        self, spec: BuntzenSpec, inventories: Mapping[BuntzenQuery, BuntzenInventory], now: datetime
    ) -> tuple[Option, ...]:
        return _policy.evaluate(spec, inventories, now)

    def booking_url(self, spec: BuntzenSpec, options: Sequence[Option]) -> str | None:
        return _policy.booking_url(spec, options)

    # -- what users read

    def render(self, kind: str, params: Mapping[str, Any], spec: BuntzenSpec, *, watch_url: str) -> Notice | None:
        return _messages.render(kind, params, spec, watch_url=watch_url)


PROVIDER: HttpProvider[BuntzenSpec, BuntzenQuery, BuntzenInventory] = BuntzenProvider()
