"""Watches on any provider: the generic API (docs/waitlist/design.md §6.1-6.2).

`POST /api/watches` starts a watch on any offered provider, and `PATCH`/`DELETE /api/watches/{id}`, `.../check` and
`.../done` act on one; `GET /api/providers` is the catalog. A watch's provider is read before its spec, which only
that provider parses. A BC Ferries watch goes through 0.7.1's trip code unchanged (`add_trip`, `update_trip`,
`check_now`), so its limits, horizon and wording are as they were; its holds, carts and checkout stay on
`/api/trips/{id}/...`. Every other provider's watch is the HTTP-checked kind: validated by its provider, checked by
waitlist-control, never given a browser.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from waitlist.providers import Registry
from waitlist.providers.base import HttpProvider

from .api import STORE, get_state, user_state
from .auth import signed_in
from .config import Offer
from .http import ApiError, json_body
from .repo import BCFERRIES, WebStore

REGISTRY_KEY = web.AppKey("registry", Registry)
OFFERS = web.AppKey("offers", dict[str, Offer])

NEW_WATCH_KEYS = frozenset({"provider", "spec", "enabled"})
NOT_HERE = "Not for this kind of watch"


def _watch_id(request: web.Request) -> str:
    return request.match_info["watch_id"]


def _enabled(body: dict[str, Any]) -> bool | None:
    if "enabled" not in body:
        return None
    if not isinstance(body["enabled"], bool):
        raise ApiError(400, "Must be true or false", "enabled")
    return body["enabled"]


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "Expected a JSON object", "spec")
    return value


def _trip_settings(spec: dict[str, Any], enabled: bool | None) -> dict[str, Any]:
    """A BC Ferries spec as 0.7.1's trip code takes it: `enabled` belongs at the top level, and the id is ours."""
    for key in ("id", "enabled"):
        if key in spec:
            raise ApiError(400, "Unknown field", key)
    return {**spec} | ({"enabled": enabled} if enabled is not None else {})


def _http(store: WebStore, provider_id: str) -> HttpProvider[Any, Any, Any]:
    provider = store.registry.http_provider(provider_id)
    if provider is None:  # a provider this build doesn't know: it can only be removed
        raise ApiError(409, NOT_HERE)
    return provider


async def get_providers(request: web.Request) -> web.Response:
    user = signed_in(request)
    store = request.app[STORE]
    return web.json_response({"providers": store.providers(is_admin=user.is_admin, switches=await store.switches())})


async def add_watch(request: web.Request) -> web.Response:
    """{provider, spec, enabled?}: 200 {watch_id, state}, so the SPA can link to the new watch."""
    user = signed_in(request)
    store = request.app[STORE]
    body = await json_body(request)
    unknown = sorted(set(body) - NEW_WATCH_KEYS)
    if unknown:
        raise ApiError(400, "Unknown field", unknown[0])
    provider_id = body.get("provider")
    meta = store.registry.meta(provider_id) if isinstance(provider_id, str) else None
    if meta is None:
        raise ApiError(400, "Not a provider you can watch", "provider")
    if not store.offered(meta, is_admin=user.is_admin):
        raise ApiError(403, f"{meta.name} isn't available to you", "provider")
    spec = _object(body.get("spec"))
    enabled = _enabled(body)
    if meta.id == BCFERRIES:
        watch_id = (await store.add_trip(user.uid, _trip_settings(spec, enabled))).id
    else:
        watch_id = await store.add_watch(user.uid, _http(store, meta.id), spec, enabled=enabled is not False)
    await store.notify(user.uid)
    return web.json_response({"watch_id": watch_id, "state": await user_state(request.app, user.uid)})


async def update_watch(request: web.Request) -> web.Response:
    """{spec?, enabled?}, at least one; `spec` is partial, over the current one."""
    user = signed_in(request)
    store = request.app[STORE]
    body = await json_body(request)
    if "provider" in body:
        raise ApiError(400, "A watch's provider can't change", "provider")
    unknown = sorted(set(body) - {"spec", "enabled"})
    if unknown:
        raise ApiError(400, "Unknown field", unknown[0])
    if "spec" not in body and "enabled" not in body:
        raise ApiError(400, "Nothing to change", "spec")
    spec = _object(body["spec"]) if "spec" in body else None
    enabled = _enabled(body)
    watch_id = _watch_id(request)
    provider_id = await store.watch_provider(user.uid, watch_id)
    if provider_id == BCFERRIES:
        await store.update_trip(user.uid, watch_id, _trip_settings(spec or {}, enabled))
    else:
        await store.update_watch(user.uid, watch_id, _http(store, provider_id), spec, enabled)
    await store.notify(user.uid)
    return await get_state(request)


async def delete_watch(request: web.Request) -> web.Response:
    """Stop watching, on any provider: a BC Ferries trip's pod drains and hands back any cart, as before."""
    user = signed_in(request)
    await request.app[STORE].archive_trip(user.uid, _watch_id(request), provider=None)
    await request.app[STORE].notify(user.uid)
    return await get_state(request)


async def check_watch(request: web.Request) -> web.Response:
    """A manual check, on any provider."""
    user = signed_in(request)
    store = request.app[STORE]
    watch_id = _watch_id(request)
    provider_id = await store.watch_provider(user.uid, watch_id)
    if provider_id == BCFERRIES:
        await store.check_now(user.uid, watch_id)
    else:
        await store.check_watch(user.uid, watch_id, _http(store, provider_id))
    await store.notify(user.uid)
    return await get_state(request)


async def done_watch(request: web.Request) -> web.Response:
    """ "I booked it", for an alert-only watch. BC Ferries asks "Did you book it?" instead (/api/trips/{id}/booked)."""
    user = signed_in(request)
    store = request.app[STORE]
    watch_id = _watch_id(request)
    meta = store.registry.meta(await store.watch_provider(user.uid, watch_id))
    if meta is None or not meta.capabilities.alert_only:
        raise ApiError(409, NOT_HERE)
    await store.done_watch(user.uid, watch_id)
    await store.notify(user.uid)
    return await get_state(request)


def routes() -> list[web.RouteDef]:
    watch = "/api/watches/{watch_id:[a-f0-9]{16}}"
    return [
        web.get("/api/providers", get_providers, allow_head=False),
        web.post("/api/watches", add_watch),
        web.patch(watch, update_watch),
        web.delete(watch, delete_watch),
        web.post(f"{watch}/check", check_watch),
        web.post(f"{watch}/done", done_watch),
    ]


def install(app: web.Application, store: WebStore) -> None:
    """The registry and the offers the store serves, under their app keys, and the routes."""
    app[REGISTRY_KEY] = store.registry
    app[OFFERS] = dict(store.offers)
    app.router.add_routes(routes())
