"""Per-user limits (docs/multi-tenant/design-reference.md §8.2, §9.1).

`effective = min(override ?? default, ceiling)`. Defaults and ceilings are deployment values (GitOps); an
admin's per-user override never goes past the ceiling, whatever the database says.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

LimitKey = Literal[
    "maxTrips",
    "maxActiveBrowsers",
    "maxHoldsPerDay",
    "maxCartAttemptsPerDay",
    "vncMinutesPerDay",
    "manualChecksPerHour",
    "testAlertsPerHour",
    "mintsPerMinute",
]
Source = Literal["default", "override", "ceiling"]

DEFAULTS: dict[LimitKey, int] = {
    "maxTrips": 3,
    "maxActiveBrowsers": 1,
    "maxHoldsPerDay": 4,
    "maxCartAttemptsPerDay": 6,
    "vncMinutesPerDay": 60,
    "manualChecksPerHour": 6,
    "testAlertsPerHour": 6,
    "mintsPerMinute": 10,
}
CEILINGS: dict[LimitKey, int] = {
    "maxTrips": 10,
    "maxActiveBrowsers": 3,
    "maxHoldsPerDay": 10,
    "maxCartAttemptsPerDay": 12,
    "vncMinutesPerDay": 120,
    "manualChecksPerHour": 20,
    "testAlertsPerHour": 6,
    "mintsPerMinute": 10,
}


@dataclass(frozen=True, slots=True)
class Effective:
    value: int
    source: Source


@dataclass(frozen=True, slots=True)
class Limits:
    defaults: Mapping[LimitKey, int] = field(default_factory=lambda: dict(DEFAULTS))
    ceilings: Mapping[LimitKey, int] = field(default_factory=lambda: dict(CEILINGS))

    def __post_init__(self) -> None:
        for key, value in self.defaults.items():
            if value < 0 or value > self.ceilings[key]:
                raise ValueError(f"The default for {key} must be between 0 and its ceiling")

    def effective(self, key: LimitKey, override: int | None = None) -> Effective:
        ceiling = self.ceilings[key]
        if override is not None:
            return Effective(ceiling, "ceiling") if override > ceiling else Effective(max(override, 0), "override")
        return Effective(min(self.defaults[key], ceiling), "default")

    def value(self, key: LimitKey, overrides: Mapping[str, int]) -> int:
        return self.effective(key, overrides.get(key)).value
