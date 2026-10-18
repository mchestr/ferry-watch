"""The provider clients waitlist-fetcher runs (design.md §4.4), one per HTTP-checked provider.

A client parses what a site sends, so only this package imports one (an import-linter contract). The query and
inventory types come from the pure provider in `waitlist.providers.REGISTRY`, which control and web share, so
both ends of the wire convert with the same Structs.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from waitlist.providers import REGISTRY
from waitlist.providers.base import Fetcher
from waitlist.providers.buntzen.client import FETCHER as BUNTZEN

FETCHERS: Mapping[str, Fetcher[Any, Any]] = MappingProxyType({"buntzen": BUNTZEN})


def _check(fetchers: Mapping[str, Fetcher[Any, Any]]) -> None:
    unknown = set(fetchers) - set(REGISTRY.http)
    if unknown:
        raise RuntimeError(f"Fetchers for providers that aren't registered as http: {sorted(unknown)}")
    for provider_id, fetcher in fetchers.items():
        if fetcher.provider_id != provider_id:
            raise RuntimeError(f"{provider_id}: its fetcher says it is {fetcher.provider_id!r}")


_check(FETCHERS)
