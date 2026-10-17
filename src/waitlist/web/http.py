"""waitlist-web's HTTP plumbing: errors as JSON, security headers, and same-origin checks on writes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from waitlist.models import ValidationError

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
# The site's own origin (`baseUrl`). Behind a proxy that ends TLS the request itself looks like plain http,
# so the request's scheme and host can't say what a browser on this site sends as its Origin.
SITE_ORIGIN = web.AppKey("site_origin", str)
MAX_BODY_BYTES = 16 * 1024
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiError(Exception):
    def __init__(self, status: int, message: str, field: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field
        self.extra = extra


def error_response(status: int, message: str, field: str | None = None, **extra: Any) -> web.Response:
    body: dict[str, Any] = {"error": message} | extra
    if field is not None:
        body["field"] = field
    return web.json_response(body, status=status)


@web.middleware
async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as e:
        return error_response(e.status, e.message, e.field, **e.extra)
    except ValidationError as e:
        return error_response(400, e.message, e.field)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Unhandled error in %s %s", request.method, request.path)
        return error_response(500, "Something went wrong")


def site_origin(request: web.Request) -> str:
    """This site's origin: the configured one, else what the request says (tests, a local run)."""
    return request.config_dict.get(SITE_ORIGIN) or f"{request.scheme}://{request.host}"


def cross_site(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    return origin is not None and origin != site_origin(request)


@web.middleware
async def same_origin(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Writes must come from this site, as JSON (CSRF can't apply to a bearer header, but this stays)."""
    if request.method not in SAFE_METHODS and request.path.startswith("/api/"):
        if cross_site(request):
            return error_response(403, "Cross-site request refused")
        if request.can_read_body and request.content_type != "application/json":
            return error_response(415, "Send JSON")
    return await handler(request)


@web.middleware
async def security_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith(("/api/", "/auth/")):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.popall("Set-Cookie", None)  # never a cookie on this host
    return response


async def json_body(request: web.Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise ApiError(413, "Too large")
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "Not JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "Expected a JSON object")
    return body
