"""A Buntzen Lake watch: one date and the passes wanted on it (design.md §5.2).

`parse_spec` validates what a user sends (every rule, in a fixed order, with fixed messages); `load_spec`
validates what is stored, without the date rules, so yesterday's watch still loads. Stored and sent as
`{"date": "2027-07-01", "passes": ["morning", "afternoon"], "release_reminder": true}`, always all three keys.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from waitlist.providers.base import SpecError

from .meta import META
from .model import PASSES, Pass
from .policy import LABELS, day_label, is_over
from .policy import today as _today

__all__ = [
    "KEYS",
    "PASSES",
    "BuntzenSpec",
    "Pass",
    "dump_spec",
    "load_spec",
    "parse_spec",
    "search_key",
    "title",
    "watch_date",
]

KEYS: Final = frozenset({"date", "passes", "release_reminder"})
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True, slots=True)
class BuntzenSpec:
    date: date  # the day at the lake (Pacific)
    passes: tuple[Pass, ...]  # non-empty, unique, in PASSES order
    release_reminder: bool = True  # a push at 06:55 the day before, when passes go on sale at 7:00


def _object(data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise SpecError("spec", "Expected a JSON object")
    unknown = sorted(str(k) for k in data if k not in KEYS)
    if unknown:
        raise SpecError(unknown[0], "Unknown field")
    return data


def _date(value: object) -> date:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise SpecError("date", "Use a date like 2027-07-01")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SpecError("date", "That date doesn't exist") from None


def _passes(value: object) -> tuple[Pass, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise SpecError("passes", "Pick at least one pass")
    if any(not isinstance(p, str) or p not in PASSES for p in value):
        raise SpecError("passes", "Passes are full_day, morning or afternoon")
    return tuple(p for p in PASSES if p in value)


def _reminder(value: object) -> bool:
    if not isinstance(value, bool):
        raise SpecError("release_reminder", "Must be true or false")
    return value


def parse_spec(data: Mapping[str, Any], *, base: BuntzenSpec | None, now: datetime) -> BuntzenSpec:
    """A spec from the user: `data` merged over `base` (a partial update), every rule of design.md §5.2."""
    given = _object(data)
    merged: dict[str, Any] = {**(dump_spec(base) if base is not None else {}), **given}
    if "date" not in merged:
        raise SpecError("date", "Required")
    day = _date(merged["date"])
    if "passes" not in merged:
        raise SpecError("passes", "Pick at least one pass")
    passes = _passes(merged["passes"])
    reminder = _reminder(merged["release_reminder"]) if "release_reminder" in merged else True
    if base is None or day != base.date:
        today = _today(now)
        if day < today:
            raise SpecError("date", "Pick today or a later date")
        if day > today + timedelta(days=META.horizon_days):
            raise SpecError("date", f"Pick a date from today to {META.horizon_days} days ahead")
        if day == today and all(is_over(day, p, now) for p in passes):
            raise SpecError("passes", "Those passes are over for today")
    return BuntzenSpec(date=day, passes=passes, release_reminder=reminder)


def load_spec(stored: Mapping[str, Any]) -> BuntzenSpec:
    """A stored spec, validated again for type and shape (never the date rules). All three keys are required."""
    data = _object(stored)
    for key in ("date", "passes", "release_reminder"):
        if key not in data:
            raise SpecError(key, "Required")
    return BuntzenSpec(
        date=_date(data["date"]), passes=_passes(data["passes"]), release_reminder=_reminder(data["release_reminder"])
    )


def dump_spec(spec: BuntzenSpec) -> dict[str, Any]:
    return {"date": spec.date.isoformat(), "passes": list(spec.passes), "release_reminder": spec.release_reminder}


def search_key(spec: BuntzenSpec) -> tuple[object, ...]:
    """Only the date: changing the passes keeps alert memory, which is keyed by pass."""
    return (spec.date,)


def watch_date(spec: BuntzenSpec) -> date:
    return spec.date


def title(spec: BuntzenSpec) -> str:
    """'Buntzen Lake, Thu Jul 1: morning or afternoon pass'."""
    names = [LABELS[p].lower().removesuffix(" pass") for p in spec.passes]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"
    return f"{META.name}, {day_label(spec.date)}: {joined} pass"
