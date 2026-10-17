"""The providers Waitlist can watch, and the one registry everything looks them up in (design.md §2.3).

Written verbatim from the design contract. Adding a provider is: its package under providers/, a line here, its
id in migration CHECK trips_provider_check (an expand-only migration), its client in waitlist.fetcher.registry,
and its card and form in web/src/providers/.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import HttpProvider, ProviderMeta
from .bcferries import META as BCFERRIES
from .buntzen import PROVIDER as BUNTZEN

__all__ = ["PROVIDER_IDS", "REGISTRY", "Registry"]


@dataclass(frozen=True, slots=True)
class Registry:
    """Every provider's meta, in picker order, and the HTTP-checked ones' implementations."""

    metas: tuple[ProviderMeta, ...]
    http: Mapping[str, HttpProvider[Any, Any, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [m.id for m in self.metas]
        if len(set(ids)) != len(ids):
            raise ValueError("Provider ids must be unique")
        for provider_id, provider in self.http.items():
            if provider.meta.id != provider_id or provider.meta not in self.metas:
                raise ValueError(f"{provider_id}: an http provider must be registered under its own meta")
            if provider.meta.checker != "http":
                raise ValueError(f"{provider_id}: only http providers have an implementation here")
        for meta in self.metas:
            if meta.checker == "http" and meta.id not in self.http:
                raise ValueError(f"{meta.id}: an http provider needs its implementation")

    @classmethod
    def of(cls, *, browser: Iterable[ProviderMeta], http: Iterable[HttpProvider[Any, Any, Any]]) -> Registry:
        providers = list(http)
        return cls(metas=(*browser, *(p.meta for p in providers)), http={p.meta.id: p for p in providers})

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(m.id for m in self.metas)

    def meta(self, provider_id: str) -> ProviderMeta | None:
        return next((m for m in self.metas if m.id == provider_id), None)

    def http_provider(self, provider_id: str) -> HttpProvider[Any, Any, Any] | None:
        return self.http.get(provider_id)


REGISTRY = Registry.of(browser=[BCFERRIES], http=[BUNTZEN])
# Exactly the ids migration 0008's CHECK (trips_provider_check) allows; a test holds the two together.
PROVIDER_IDS = REGISTRY.ids
