"""Buntzen Lake: STUB from the contract commit. W2 replaces this file with the real provider (design.md §5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import msgspec

from waitlist.providers.base import Budget, CheckWindow, HttpProvider, Notice, Option, ProviderMeta

from .meta import BUDGET, META


class _Query(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class _Inventory(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class _NotYet:
    meta: ProviderMeta = META
    budget: Budget = BUDGET
    query_type: type[_Query] = _Query
    inventory_type: type[_Inventory] = _Inventory

    def parse_spec(self, data: Mapping[str, Any], *, base: object | None, now: datetime) -> object:
        raise NotImplementedError

    def load_spec(self, stored: Mapping[str, Any]) -> object:
        raise NotImplementedError

    def dump_spec(self, spec: object) -> dict[str, Any]:
        raise NotImplementedError

    def search_key(self, spec: object) -> tuple[object, ...]:
        raise NotImplementedError

    def watch_date(self, spec: object) -> date:
        raise NotImplementedError

    def title(self, spec: object) -> str:
        raise NotImplementedError

    def window(self, spec: object) -> CheckWindow:
        raise NotImplementedError

    def interval(self, spec: object, now: datetime) -> timedelta:
        raise NotImplementedError

    def reminder_at(self, spec: object) -> datetime | None:
        raise NotImplementedError

    def queries(self, spec: object) -> tuple[_Query, ...]:
        raise NotImplementedError

    def evaluate(self, spec: object, inventories: Mapping[_Query, _Inventory], now: datetime) -> tuple[Option, ...]:
        raise NotImplementedError

    def booking_url(self, spec: object, options: Sequence[Option]) -> str | None:
        raise NotImplementedError

    def render(self, kind: str, params: Mapping[str, Any], spec: object, *, watch_url: str) -> Notice | None:
        raise NotImplementedError


PROVIDER: HttpProvider[Any, Any, Any] = _NotYet()
