"""waitlist-fetcher's HTTP client, as a provider's client sees it (`providers.base.Transport`; design.md §4.4).

A buggy or compromised provider client can reach nothing but https:443 at the hosts its fetcher declares, on
public addresses: the URL is checked before any I/O, the resolver drops every non-global answer (so DNS can't
steer it inward), redirects are never followed and cookies never kept (the session's DummyCookieJar). Bodies
are read to their end up to a byte cap. Nothing here logs a body, a header or a URL's path.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import ssl
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

import aiohttp
import aiohttp.abc
import msgspec

from waitlist.providers.base import FetchError, HttpResponse

log = logging.getLogger("waitlist.fetcher.transport")

MAX_RETRY_AFTER_S: Final = 3600
_DELAY_SECONDS = re.compile(r"[0-9]{1,10}")
_DEFAULT_PORTS: Final = {"https": 443, "http": 80}


async def _read_capped(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """The whole body, read to its end, or FetchError("too_large") once it passes max_bytes."""
    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > max_bytes:
            raise FetchError("too_large", http_status=resp.status)
    return bytes(body)


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """aiohttp's ThreadedResolver, keeping only global addresses: a DNS answer pointing at the LAN, a pod, a
    service, loopback, link-local or CGNAT space is refused, so toFQDNs can't be steered inward (DNS rebinding)."""

    def __init__(self, inner: aiohttp.abc.AbstractResolver | None = None) -> None:
        self._inner = inner if inner is not None else aiohttp.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        found = await self._inner.resolve(host, port, family)
        public = [r for r in found if ipaddress.ip_address(r["host"]).is_global]
        if not public:
            raise OSError(f"{host} resolved to no public address")
        return public

    async def close(self) -> None:
        await self._inner.close()


def retry_after(value: str | None) -> int | None:
    """Retry-After's delay-seconds, clamped to 0-3600; its HTTP-date form (or anything else) gives None."""
    if value is None or not _DELAY_SECONDS.fullmatch(value.strip()):
        return None
    return min(int(value.strip()), MAX_RETRY_AFTER_S)


class AiohttpTransport:
    """`providers.base.Transport` over one shared aiohttp session (built by `waitlist.fetcher.app.upstream_session`).

    `_scheme` and `_port` are a test seam only (tests/test_transport.py drives the real request path against a
    local http server through them); production code never passes them, and a test holds that.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hosts: frozenset[str],
        *,
        max_bytes: int,
        _scheme: str = "https",
        _port: int = 443,
    ) -> None:
        self._session = session
        self._hosts = frozenset(h.lower() for h in hosts)
        self._max_bytes = max_bytes
        self._scheme = _scheme
        self._port = _port

    def _allow(self, url: str) -> None:
        """Before any I/O: the scheme, the port, no userinfo, and a declared host. Else FetchError("unexpected")."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            log.error("Refused a request whose URL doesn't parse")  # noqa: TRY400 - nothing to trace back
            raise FetchError("unexpected") from None
        if port is None:
            port = _DEFAULT_PORTS.get(parts.scheme)
        if (
            parts.scheme != self._scheme
            or port != self._port
            or parts.username is not None
            or parts.password is not None
            or parts.hostname not in self._hosts
        ):
            log.error("Refused a request to scheme=%r host=%r port=%r", parts.scheme, parts.hostname, port)
            raise FetchError("unexpected")

    async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse:
        self._allow(url)
        body = msgspec.json.encode(dict(payload))
        return await self._request("POST", url, data=body, headers=dict(headers))

    async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse:
        self._allow(url)
        return await self._request("GET", url, data=None, headers=dict(headers))

    async def _request(self, method: str, url: str, *, data: bytes | None, headers: dict[str, str]) -> HttpResponse:
        try:
            async with self._session.request(method, url, data=data, headers=headers, allow_redirects=False) as resp:
                if resp.content_length is not None and resp.content_length > self._max_bytes:
                    raise FetchError("too_large", http_status=resp.status)
                content = await _read_capped(resp, self._max_bytes)
                return HttpResponse(
                    status=resp.status,
                    content_type=resp.content_type.lower(),
                    body=content,
                    retry_after_s=retry_after(resp.headers.get("Retry-After")),
                )
        except TimeoutError:
            raise FetchError("timeout") from None
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, ssl.SSLError, OSError):
            raise FetchError("network") from None
