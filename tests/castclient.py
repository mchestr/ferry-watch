"""A streamed viewer for tests: a client that speaks wl.cast1 as PROTOCOL.md §8 says a viewer must."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from waitlist.agent.cast_protocol import ImageHeader

WAIT_S = 10.0


@dataclass
class Viewer:
    """A streamed viewer, as PROTOCOL §8 says one behaves: it numbers what it sends, and acks every image."""

    ws: aiohttp.ClientWebSocketResponse
    messages: list[dict[str, Any]] = field(default_factory=list)
    images: list[tuple[ImageHeader, bytes]] = field(default_factory=list)
    number: int = 0  # of its latest message
    ack: bool = True
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    _reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for frame in self.ws:
            if frame.type == aiohttp.WSMsgType.TEXT:
                self.messages.append(json.loads(frame.data))
            elif frame.type == aiohttp.WSMsgType.BINARY:
                header = ImageHeader.unpack(frame.data)
                self.images.append((header, frame.data[32:]))
                if self.ack:
                    await self.ws.send_str(json.dumps({"t": "ack", "seq": header.seq}))
            self._changed.set()
        self._changed.set()

    async def send(self, message: dict[str, Any]) -> int:
        """Send a message; its number (PROTOCOL §1)."""
        await self.ws.send_str(json.dumps(message))
        if message["t"] != "ack":
            self.number += 1
        return self.number

    async def until(self, found: Callable[[], Any], seconds: float = WAIT_S) -> Any:
        """Wait until `found()` returns something truthy, for up to `seconds`; that value."""
        deadline = time.monotonic() + seconds
        while not (value := found()):
            if self.ws.closed or time.monotonic() > deadline:
                recent = [m if m["t"] not in ("fields", "pages") else {"t": m["t"]} for m in self.messages[-8:]]
                closed = f" (closed {self.ws.close_code})" if self.ws.closed else ""
                raise AssertionError(f"the viewer didn't get what it waited for{closed}; it got {recent}")
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), 0.02)  # or look again: not all it waits for is a message
        return value

    async def message(
        self, t: str, where: Callable[[dict[str, Any]], bool] = lambda _: True, after: int = 0
    ) -> dict[str, Any]:
        """The first message of type `t` (from the `after`th on) that `where` likes."""
        found: dict[str, Any] = await self.until(
            lambda: next((m for m in self.messages[after:] if m["t"] == t and where(m)), None)
        )
        return found

    async def image(self, after: int = 0) -> tuple[ImageHeader, bytes]:
        """The first image with a seq greater than `after`."""
        found: tuple[ImageHeader, bytes] = await self.until(
            lambda: next((i for i in self.images if i[0].seq > after), None)
        )
        return found

    @property
    def latest(self) -> ImageHeader:
        return self.images[-1][0]

    async def closed(self) -> int | None:
        """Wait for the agent to close the connection: its code."""
        assert self._reader is not None
        await asyncio.wait_for(self._reader, WAIT_S)  # it reads until the socket closes
        return self.ws.close_code

    async def close(self) -> None:
        await self.ws.close()
        if self._reader is not None:
            await self._reader
