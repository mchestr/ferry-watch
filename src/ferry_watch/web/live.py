"""The dashboard's live socket, `/api/ws` (docs/multi-tenant/design-reference.md §6.2.2 step 7, §6.3).

A browser can't put a header on a WebSocket upgrade, so the socket authenticates with its first message,
`{"type": "auth", "token": ...}`, within 5 s; until then nothing is sent, and a bad token closes it with
4401. The token never goes in a URL. Each socket then receives its own user's snapshot, newest only,
whenever Postgres says that user's rows changed (NOTIFY fw_user), and at least every 15 s, when its
session is checked again: a revoked or expired session closes it. At most 5 sockets a user, each for at
most an hour; messages from the browser are capped at 4 KiB.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from aiohttp import WSMsgType, web

from ferry_watch.control.store import USER_CHANNEL

from .accounts import Accounts, SignedIn
from .http import cross_site

log = logging.getLogger(__name__)

AUTH_DEADLINE_SECONDS = 5.0
PING_SECONDS = 15.0
LIFETIME_SECONDS = 60 * 60
MAX_PER_USER = 5
MAX_MESSAGE_BYTES = 4096
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_UNAUTHORIZED = 4401
CLOSE_TOO_MANY = 4429


@dataclass(eq=False)
class _Socket:
    ws: web.WebSocketResponse
    user: SignedIn
    opened: float = field(default_factory=time.monotonic)
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class LiveHub:
    def __init__(
        self,
        pool: asyncpg.Pool,
        accounts: Accounts,
        snapshot: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> None:
        self._pool = pool
        self._accounts = accounts
        self._snapshot = snapshot
        self._sockets: dict[str, list[_Socket]] = {}

    def changed(self, uid: str) -> None:
        for socket in self._sockets.get(uid, []):
            socket.wake.set()

    async def listen(self) -> None:
        """Hold a LISTEN connection for as long as the app runs, reconnecting if it drops."""
        while True:
            try:
                async with self._pool.acquire() as conn:

                    def on_change(
                        _conn: asyncpg.Connection[Any] | asyncpg.pool.PoolConnectionProxy[Any],
                        _pid: int,
                        _channel: str,
                        uid: object,
                        /,
                    ) -> None:
                        self.changed(str(uid))

                    await conn.add_listener(USER_CHANNEL, on_change)
                    try:
                        while not conn.is_closed():
                            await asyncio.sleep(PING_SECONDS)
                            await conn.execute("SELECT 1")
                    finally:
                        with contextlib.suppress(asyncpg.PostgresError, OSError):
                            await conn.remove_listener(USER_CHANNEL, on_change)
            except (asyncpg.PostgresError, OSError):
                log.warning("Lost the change feed; reconnecting", exc_info=True)
                await asyncio.sleep(2)

    async def serve(self, request: web.Request) -> web.StreamResponse:
        if cross_site(request):
            raise web.HTTPForbidden
        ws = web.WebSocketResponse(heartbeat=PING_SECONDS, max_msg_size=MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        user = await self._authenticate(ws)
        if user is None:
            await ws.close(code=CLOSE_UNAUTHORIZED, message=b"sign in")
            return ws
        socket = _Socket(ws, user)
        mine = self._sockets.setdefault(user.uid, [])
        mine.append(socket)
        while len(mine) > MAX_PER_USER:
            await mine.pop(0).ws.close(code=CLOSE_TOO_MANY, message=b"too many tabs")
        try:
            await self._feed(socket)
        finally:
            with contextlib.suppress(ValueError):
                mine.remove(socket)
            if not mine:
                self._sockets.pop(user.uid, None)
        return ws

    async def _authenticate(self, ws: web.WebSocketResponse) -> SignedIn | None:
        try:
            frame = await asyncio.wait_for(ws.receive(), AUTH_DEADLINE_SECONDS)
        except TimeoutError:
            return None
        if frame.type != WSMsgType.TEXT:
            return None
        try:
            message = json.loads(frame.data)
        except ValueError:
            return None
        if not isinstance(message, dict) or message.get("type") != "auth" or not isinstance(message.get("token"), str):
            return None
        result = await self._accounts.resolve(message["token"])
        return result if isinstance(result, SignedIn) else None

    async def _feed(self, socket: _Socket) -> None:
        ws = socket.ws
        reader = asyncio.create_task(self._drain_incoming(ws))
        socket.wake.set()  # the first snapshot at once
        code, why = 1000, b""
        try:
            while not ws.closed and not reader.done():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(socket.wake.wait(), PING_SECONDS)
                socket.wake.clear()
                if time.monotonic() - socket.opened > LIFETIME_SECONDS:
                    code, why = CLOSE_UNAUTHORIZED, b"reconnect"
                    break
                # Is the session still good? (revoked, expired or due for re-verification closes it)
                if not isinstance(await self._accounts.resolve_hash(socket.user.token_hash), SignedIn):
                    code, why = CLOSE_UNAUTHORIZED, b"sign in"
                    break
                payload = json.dumps({"type": "state", "state": await self._snapshot(socket.user.uid)})
                await asyncio.wait_for(ws.send_str(payload), SEND_TIMEOUT_SECONDS)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            # Stop reading before closing, so the close frame (and its code) goes out cleanly.
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await ws.close(code=code, message=why)

    @staticmethod
    async def _drain_incoming(ws: web.WebSocketResponse) -> None:
        async for _ in ws:  # nothing is expected after auth; reading notices the close
            pass
