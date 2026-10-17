"""waitlist-control's client for waitlist-fetcher (docs/waitlist/design.md §4.3, §4.5).

One POST per query, over the closed wire of waitlist.fetchwire: a provider id and the provider's typed query go
out; the provider's typed inventory, or an error kind, comes back. The fetcher's replies are untrusted input,
bounded and strictly decoded. Every way a fetch can fail ends as a `CheckFailed` whose kind says whose trouble it
was: the site's (the fetcher's FetchFailed kinds) or ours (`fetcher_unavailable`, `bad_reply`), which never count
against a watch (I11).

Replies arrive as sent (auto_decompress=False on every request, whatever the session says): aiohttp would inflate
each read whole before any cap saw it, so a fetcher compromised by the content it parses (I6) could answer with a
small gzip body that inflates past waitlist-control's memory limit. The fetcher never compresses its replies, so
any Content-Encoding but identity is a bad reply, refused before its body is read.
"""

from __future__ import annotations

import secrets
from typing import Any

import aiohttp
import msgspec

from waitlist.fetchwire import MAX_REPLY_BYTES, PATH, FetchFailed, FetchRequest, WireError, decode_reply, encode
from waitlist.providers.base import CheckErrorKind, HttpProvider


class CheckFailed(Exception):
    """A query that didn't give an inventory. `replied`: the fetcher answered with a valid FetchFailed (the check
    loop's checker-down clock uses it)."""

    def __init__(self, kind: CheckErrorKind, *, retry_after_s: int | None = None, replied: bool = False) -> None:
        super().__init__(kind)
        self.kind: CheckErrorKind = kind
        self.retry_after_s = retry_after_s
        self.replied = replied


async def _read_capped(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """The whole body, read to its end, or CheckFailed("bad_reply") once it passes max_bytes."""
    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > max_bytes:
            raise CheckFailed("bad_reply")
    return bytes(body)


class FetchClient:
    def __init__(self, http: aiohttp.ClientSession, url: str, *, timeout_s: float = 20.0) -> None:
        self._http = http
        self._url = url.rstrip("/") + PATH
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def fetch(self, provider: HttpProvider[Any, Any, Any], query: msgspec.Struct) -> msgspec.Struct:
        """The query's inventory, as the provider's own type; or CheckFailed."""
        request_id = secrets.token_hex(8)
        request = FetchRequest(id=request_id, provider=provider.meta.id, query=msgspec.to_builtins(query))
        try:
            async with self._http.post(
                self._url,
                data=encode(request),
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                timeout=self._timeout,
                allow_redirects=False,
                auto_decompress=False,
            ) as resp:
                if resp.status != 200:
                    raise CheckFailed("bad_reply")
                if resp.headers.get("Content-Encoding", "").strip().lower() not in ("", "identity"):
                    raise CheckFailed("bad_reply")
                if resp.content_length is not None and resp.content_length > MAX_REPLY_BYTES:
                    raise CheckFailed("bad_reply")
                body = await _read_capped(resp, MAX_REPLY_BYTES)
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise CheckFailed("fetcher_unavailable") from None
        try:
            reply = decode_reply(body)
        except WireError:
            raise CheckFailed("bad_reply") from None
        if reply.id != request_id:
            raise CheckFailed("bad_reply")
        if isinstance(reply, FetchFailed):
            if reply.error == "rate_limited" and reply.http_status is None:
                # The fetcher's own cap, which never carries a status: our limit, not the site's (I11).
                raise CheckFailed("fetcher_unavailable", replied=True)
            raise CheckFailed(reply.error, retry_after_s=reply.retry_after_s, replied=True)
        try:
            inventory: msgspec.Struct = msgspec.convert(reply.inventory, provider.inventory_type, strict=True)
        except msgspec.ValidationError:
            raise CheckFailed("bad_reply") from None
        return inventory
