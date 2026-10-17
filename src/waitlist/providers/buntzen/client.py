"""Buntzen Lake's client: STUB from the contract commit. W2 replaces this file (design.md §5.3-5.4)."""

from __future__ import annotations

from typing import Any

import msgspec

from waitlist.providers.base import Fetcher, Transport

from .meta import HOSTS, ID


class _NotYet:
    provider_id: str = ID
    hosts: tuple[str, ...] = HOSTS

    async def fetch(self, transport: Transport, query: msgspec.Struct) -> msgspec.Struct:
        raise NotImplementedError


FETCHER: Fetcher[Any, Any] = _NotYet()
