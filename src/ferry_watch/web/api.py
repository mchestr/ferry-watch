"""Each user's API (docs/multi-tenant/design-reference.md §6.3).

Every route acts for the signed-in user only, through `WebStore`, which scopes every query to them.
The checkout route mints the one-minute ticket that opens the user's own browser pod's screen, and
only while that pod holds their cart.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ferry_watch.bcferries.errors import BookingError, RouteRejected
from ferry_watch.bcferries.terminals import TerminalDirectory, Terminals
from ferry_watch.bcferries.urls import search_url
from ferry_watch.models import Trip, ValidationError
from ferry_watch.tickets import TTL_SECONDS, Display, mint

from .auth import signed_in
from .http import ApiError, json_body
from .outbox import subscription_row
from .repo import WebStore

STORE = web.AppKey("store", WebStore)
TERMINALS = web.AppKey("terminals", TerminalDirectory)
PUBLIC = web.AppKey("public", dict[str, Any])  # what every snapshot carries: links, the push key
MAX_CART_ID = 32


@dataclass(frozen=True, slots=True)
class CheckoutSigner:
    """Mints checkout tickets with the private half of the keys pinned in every browser pod."""

    key: Ed25519PrivateKey
    key_id: str
    origin: str  # https://ferry-checkout.chestr.dev
    route_name: Callable[[Trip], str] = lambda trip: f"{trip.origin} → {trip.destination}"


CHECKOUT = web.AppKey("checkout", CheckoutSigner)


def _trip_id(request: web.Request) -> str:
    return request.match_info["trip_id"]


def _cart_id(body: dict[str, Any]) -> str:
    cart_id = body.get("cart_id")
    if not isinstance(cart_id, str) or not 0 < len(cart_id) <= MAX_CART_ID:
        raise ApiError(400, "Which cart?", "cart_id")
    return cart_id


async def user_state(app: web.Application, uid: str) -> dict[str, Any]:
    """The user's whole state, as both GET /api/state and the live socket send it."""
    state = await app[STORE].snapshot(uid)
    directory = app.get(TERMINALS)
    terminals = directory.cached if directory is not None else None
    for trip in state["trips"]:
        trip["booking_url"] = _booking_url(trip["trip"], terminals)
    return state | app[PUBLIC]


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(await user_state(request.app, signed_in(request).uid))


def _booking_url(spec: dict[str, Any], terminals: Terminals | None) -> str | None:
    """BC Ferries' booking form for the trip's route and date, to book it yourself."""
    if terminals is None:
        return None
    try:
        return search_url(Trip.from_json(spec), terminals)
    except (RouteRejected, ValidationError):
        return None


async def get_terminals(request: web.Request) -> web.Response:
    directory = request.app[TERMINALS]
    try:
        terminals = await directory.get()
    except BookingError:
        raise ApiError(503, "BC Ferries' terminal list is unavailable right now; try again shortly") from None
    return web.json_response({"terminals": sorted((t.to_json() for t in terminals.values()), key=lambda t: t["label"])})


async def add_trip(request: web.Request) -> web.Response:
    user = signed_in(request)
    await request.app[STORE].add_trip(user.uid, await json_body(request))
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def update_trip(request: web.Request) -> web.Response:
    user = signed_in(request)
    await request.app[STORE].update_trip(user.uid, _trip_id(request), await json_body(request))
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def delete_trip(request: web.Request) -> web.Response:
    await request.app[STORE].archive_trip(signed_in(request).uid, _trip_id(request))
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def check_now(request: web.Request) -> web.Response:
    await request.app[STORE].check_now(signed_in(request).uid, _trip_id(request))
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def request_hold(request: web.Request) -> web.Response:
    await request.app[STORE].request_hold(signed_in(request).uid, _trip_id(request))
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def release_cart(request: web.Request) -> web.Response:
    cart_id = _cart_id(await json_body(request))
    await request.app[STORE].command(signed_in(request).uid, _trip_id(request), "release", cart_id)
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def dismiss_cart(request: web.Request) -> web.Response:
    cart_id = _cart_id(await json_body(request))
    await request.app[STORE].command(signed_in(request).uid, _trip_id(request), "dismiss", cart_id)
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def answer_booked(request: web.Request) -> web.Response:
    body = await json_body(request)
    booked, reason = body.get("booked"), body.get("reason")
    if not isinstance(booked, bool):
        raise ApiError(400, "Did you book it: yes or no?", "booked")
    if reason is not None and not isinstance(reason, str):
        raise ApiError(400, "Not a reason", "reason")
    await request.app[STORE].answer_booked(signed_in(request).uid, _trip_id(request), booked, reason)
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def update_me(request: web.Request) -> web.Response:
    body = await json_body(request)
    paused = body.get("paused")
    if set(body) != {"paused"} or not isinstance(paused, bool):
        raise ApiError(400, "Only paused can be changed", "paused")
    await request.app[STORE].set_paused(signed_in(request).uid, paused)
    await request.app[STORE].notify(signed_in(request).uid)
    return await get_state(request)


async def open_checkout(request: web.Request) -> web.Response:
    """A one-minute ticket for the user's own browser pod's screen, while it holds their cart."""
    user = signed_in(request)
    store, signer = request.app[STORE], request.app[CHECKOUT]
    held = await store.held_cart(user.uid, _trip_id(request))
    display = Display(
        route=signer.route_name(held.trip)[:64],
        date=held.trip.date.isoformat(),
        depart=held.depart,
        fare=(held.fare or "")[:32],
        session_s=max(held.session_s, 0),
        hard_deadline=int(held.hard_deadline),
    )
    jti = secrets.token_urlsafe(16)
    await store.record_mint(user.uid, held, jti)  # counted (and limited) before it's handed out
    ticket = mint(
        signer.key,
        signer.key_id,
        sub=user.uid,
        trip=held.trip.id,
        pod=held.pod,
        cart_id=held.cart_id,
        disp=display,
        jti=jti,
    )
    # The ticket rides in the fragment, so it never reaches a server log.
    url = f"{signer.origin}/t/{held.trip.id}/g/{held.gen}/#{ticket}"
    return web.json_response({"url": url, "ttl": TTL_SECONDS, "nonce_key": f"{held.trip.id}:{held.gen}"})


async def subscribe(request: web.Request) -> web.Response:
    try:
        subscription = subscription_row(await json_body(request))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    await request.app[STORE].subscribe(signed_in(request).uid, subscription)
    return web.json_response({"ok": True})


async def unsubscribe(request: web.Request) -> web.Response:
    endpoint = (await json_body(request)).get("endpoint")
    if not isinstance(endpoint, str):
        raise ApiError(400, "Which device?", "endpoint")
    await request.app[STORE].unsubscribe(signed_in(request).uid, endpoint)
    return web.json_response({"ok": True})


async def test_alert(request: web.Request) -> web.Response:
    await request.app[STORE].test_alert(signed_in(request).uid)
    await request.app[STORE].notify(signed_in(request).uid)
    return web.json_response({"ok": True})


def routes() -> list[web.RouteDef]:
    return [
        web.get("/api/state", get_state, allow_head=False),
        web.get("/api/terminals", get_terminals, allow_head=False),
        web.post("/api/trips", add_trip),
        web.patch("/api/trips/{trip_id:[a-f0-9]{16}}", update_trip),
        web.delete("/api/trips/{trip_id:[a-f0-9]{16}}", delete_trip),
        web.post("/api/trips/{trip_id:[a-f0-9]{16}}/check", check_now),
        web.post("/api/trips/{trip_id:[a-f0-9]{16}}/hold", request_hold),
        web.post("/api/trips/{trip_id:[a-f0-9]{16}}/cart/release", release_cart),
        web.post("/api/trips/{trip_id:[a-f0-9]{16}}/cart/dismiss", dismiss_cart),
        web.post("/api/trips/{trip_id:[a-f0-9]{16}}/vnc", open_checkout),
        web.post("/api/trips/{trip_id:[a-f0-9]{16}}/booked", answer_booked),
        web.patch("/api/me", update_me),
        web.post("/api/me/push-subscriptions", subscribe),
        web.delete("/api/me/push-subscriptions", unsubscribe),
        web.post("/api/alerts/test", test_alert),
    ]
