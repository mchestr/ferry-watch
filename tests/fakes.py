"""Test doubles for the browser layer and alerts."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from ferry_watch.bcferries.errors import SessionExpired
from ferry_watch.bcferries.site import Credentials
from ferry_watch.models import Fare, FarePreference, Sailing, SailingStatus, Trip
from ferry_watch.notify import Message

PASSWORD = "hunter2-secret"


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
        self.opened_at = 1_000.0
        self.logged_in = False
        self.calls: list[str] = []
        self.logged_in_as: str | None = None
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

    async def login(self, credentials: Credentials) -> None:
        self.calls.append("login")
        self.site.logging_in += 1
        self.site.most_logging_in = max(self.site.most_logging_in, self.site.logging_in)
        try:
            await asyncio.sleep(0.01)  # a login takes a moment; long enough for another to overlap it
            if self.site.login_error:
                raise self.site.login_error
        finally:
            self.site.logging_in -= 1
        self.logged_in = True
        self.logged_in_as = credentials.username

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

    async def empty_cart(self) -> None:
        self.calls.append("empty cart")

    async def show(self) -> None:
        self.calls.append("show")

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
    login_error: Exception | None = None
    cart_error: Exception | None = None
    expire_next_refresh: bool = False
    sessions: list[FakeSession] = field(default_factory=list)
    search_hook: Any = None
    cart_hook: Any = None
    logging_in: int = 0  # logins in progress right now
    most_logging_in: int = 0

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


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    def submit(self, message: Message) -> None:
        self.sent.append(message)

    @property
    def titles(self) -> list[str]:
        return [m.title for m in self.sent]
