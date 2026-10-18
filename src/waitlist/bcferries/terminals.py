"""BC Ferries terminals and the routes between them (from the site's public route-info JSON)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from waitlist.fileio import write_json_atomic

from .errors import BookingError

log = logging.getLogger(__name__)

ROUTE_INFO_URL = "https://www.bcferries.com/route-info"
_REFRESH_SECONDS = 24 * 3600


@dataclass(frozen=True, slots=True)
class Terminal:
    code: str  # "SWB"
    name: str  # "Victoria (Swartz Bay)"
    city: str  # "Sidney"
    destinations: tuple[str, ...]  # codes of terminals with a bookable route from here

    @property
    def label(self) -> str:
        """The location name BC Ferries' booking form uses, e.g. 'Sidney - Victoria (Swartz Bay)'."""
        return f"{self.city} - {self.name}"

    @property
    def place(self) -> str:
        """The terminal itself: 'Swartz Bay' from 'Victoria (Swartz Bay)'."""
        m = re.fullmatch(r".*\((.+)\)", self.name)
        return m[1] if m else self.name

    @property
    def area(self) -> str:
        """The area it serves: 'Victoria' from 'Victoria (Swartz Bay)'."""
        m = re.fullmatch(r"(.+?)\s*\(.+\)", self.name)
        return m[1] if m else self.city

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "place": self.place,
            "area": self.area,
            "dests": list(self.destinations),
        }


class Terminals(Mapping[str, Terminal]):
    def __init__(self, terminals: dict[str, Terminal]) -> None:
        self._terminals = terminals

    def __getitem__(self, code: str) -> Terminal:
        return self._terminals[code]

    def __iter__(self) -> Iterator[str]:
        return iter(self._terminals)

    def __len__(self) -> int:
        return len(self._terminals)

    def has_route(self, origin: str, destination: str) -> bool:
        return origin in self and destination in self[origin].destinations

    def route_name(self, origin: str, destination: str) -> str:
        """'Swartz Bay → Tsawwassen' (falls back to codes for unknown terminals)."""
        o = self[origin].place if origin in self else origin
        d = self[destination].place if destination in self else destination
        return f"{o} → {d}"

    @classmethod
    def from_route_info(cls, data: Any) -> Terminals:
        try:
            return cls(
                {
                    t["code"]: Terminal(
                        code=t["code"],
                        name=t["name"],
                        city=t["city"]["name"],
                        destinations=tuple(r["code"] for r in t.get("destinationRoutes", []) if r.get("isBookable")),
                    )
                    for t in data
                }
            )
        except (KeyError, TypeError) as e:
            raise BookingError(f"Unexpected route-info format: {e!r}") from e


class TerminalDirectory:
    """Fetches the terminal list once and refreshes it daily; concurrent callers share one fetch.

    The last good list is cached on disk, so a restart while bcferries.com is unreachable still
    knows the terminals.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        user_agent: str,
        *,
        url: str = ROUTE_INFO_URL,
        cache_path: Path | None = None,
    ) -> None:
        self._http = http
        self._user_agent = user_agent
        self._url = url
        self._cache_path = cache_path
        self._terminals: Terminals | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> Terminals | None:
        return self._terminals

    async def get(self) -> Terminals:
        if self._terminals is not None and time.monotonic() - self._fetched_at < _REFRESH_SECONDS:
            return self._terminals
        async with self._lock:
            if self._terminals is None or time.monotonic() - self._fetched_at >= _REFRESH_SECONDS:
                try:
                    self._terminals = await self._fetch()
                    self._fetched_at = time.monotonic()
                except BookingError:
                    if self._terminals is None:
                        self._terminals = self._load_cache()
                        if self._terminals is None:
                            raise
                        log.warning("Couldn't load terminals from BC Ferries; using the cached copy")
                    else:
                        log.warning("Couldn't refresh terminals; keeping the current list")
            return self._terminals

    def _load_cache(self) -> Terminals | None:
        if self._cache_path is None:
            return None
        try:
            return Terminals.from_route_info(json.loads(self._cache_path.read_text()))
        except (OSError, ValueError, BookingError):
            return None

    async def _fetch(self) -> Terminals:
        try:
            async with self._http.get(
                self._url,
                headers={"User-Agent": self._user_agent, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise BookingError(f"Couldn't load BC Ferries terminals: {e}") from e
        terminals = Terminals.from_route_info(data)
        log.info("Loaded %d BC Ferries terminals", len(terminals))
        if self._cache_path is not None:
            try:
                write_json_atomic(self._cache_path, data)
            except OSError:
                log.warning("Couldn't cache the terminal list", exc_info=True)
        return terminals
