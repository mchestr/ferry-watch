"""Each user's API on ferry-web, on real Postgres: scoping, limits, commands, answers and checkout tickets."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp
import asyncpg
import pytest
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ferry_watch.control.limits import Limits
from ferry_watch.models import PACIFIC
from ferry_watch.polling import PollPolicy
from ferry_watch.tickets import verify
from ferry_watch.web.accounts import Accounts, IdentityPolicy
from ferry_watch.web.api import CheckoutSigner
from ferry_watch.web.app import build
from ferry_watch.web.identity import OidcClient, OidcSettings, Principal
from ferry_watch.web.repo import WebStore
from tests.postgres import ROLES, Database

ANN, BOB = "a" * 20, "b" * 20
KEY = Ed25519PrivateKey.generate()
ORIGIN = "https://ferry-checkout.chestr.dev"


def soon(days: int = 3) -> str:
    return (datetime.now(PACIFIC).date() + timedelta(days=days)).isoformat()  # the server's "today" is Pacific


@dataclass
class Rig:
    client: TestClient
    db: Database
    tokens: dict[str, str]

    async def call(self, method: str, path: str, user: str = ANN, **kw: Any) -> aiohttp.ClientResponse:
        headers = {"Authorization": f"Bearer {self.tokens[user]}"} | kw.pop("headers", {})
        return await self.client.request(method, path, headers=headers, **kw)

    async def add(self, user: str = ANN, **trip: Any) -> str:
        body = {"from": "SWB", "to": "TSA", "date": soon()} | trip
        response = await self.call("POST", "/api/trips", user, json=body)
        assert response.status == 200, await response.text()
        return str((await response.json())["trips"][-1]["trip"]["id"])

    async def sql(self, query: str, *args: Any) -> list[asyncpg.Record]:
        conn = await self.db.connect()
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()


@pytest.fixture
async def rig(db: Database, aiohttp_client: Callable[..., Any]) -> AsyncIterator[Rig]:
    owner = await db.connect()
    try:
        for uid in (ANN, BOB):
            await owner.execute("INSERT INTO users (id, username) VALUES ($1, $1)", uid)
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(db.dsn(ROLES.web), min_size=1, max_size=4)
    accounts = Accounts(pool, IdentityPolicy())
    tokens = {uid: await accounts.create_session(uid, Principal("https://idp", uid), None) for uid in (ANN, BOB)}
    async with aiohttp.ClientSession() as http:
        oidc = OidcClient(http, OidcSettings(issuer="https://idp", client_id="c", client_secret="s", redirect_uri="x"))
        app = build(
            accounts,
            oidc,
            WebStore(pool, Limits(), PollPolicy(), clock=time.time),
            CheckoutSigner(KEY, "k1", ORIGIN),
            pool=pool,
            limits=Limits(),
        )
        client: TestClient = await aiohttp_client(app)
        try:
            yield Rig(client, db, tokens)
        finally:
            await client.close()  # stops the change feed, which holds a pool connection
            await pool.close()


# ---- trips


async def test_signed_out_calls_are_refused(rig: Rig) -> None:
    response = await rig.client.get("/api/state")
    assert response.status == 401
    assert "Set-Cookie" not in response.headers


async def test_adding_a_trip(rig: Rig) -> None:
    trip_id = await rig.add()
    assert len(trip_id) == 16
    state = await (await rig.call("GET", "/api/state")).json()
    assert [t["trip"]["id"] for t in state["trips"]] == [trip_id]
    assert state["trips"][0]["browser"]["mode"] == "off"
    assert state["limits"]["maxTrips"] == {"value": 3, "source": "default"}
    assert (await rig.sql("SELECT next_check_at FROM trip_runtime"))[0]["next_check_at"] is None  # checked at once


async def test_the_trip_limit_is_per_user(rig: Rig) -> None:
    for _ in range(3):
        await rig.add()
    response = await rig.call("POST", "/api/trips", json={"from": "SWB", "to": "TSA", "date": soon()})
    assert response.status == 409
    assert (await response.json())["limit"] == 3
    await rig.add(BOB)  # Bob has his own


@pytest.mark.parametrize("days", [-1, 31])
async def test_trips_are_within_30_days(rig: Rig, days: int) -> None:
    response = await rig.call("POST", "/api/trips", json={"from": "SWB", "to": "TSA", "date": soon(days)})
    assert response.status == 400
    assert (await response.json())["field"] == "date"


async def test_another_users_trip_looks_missing(rig: Rig) -> None:
    trip_id = await rig.add(BOB)
    for method, path, body in [
        ("PATCH", f"/api/trips/{trip_id}", {"adults": 2}),
        ("DELETE", f"/api/trips/{trip_id}", None),
        ("POST", f"/api/trips/{trip_id}/check", None),
        ("POST", f"/api/trips/{trip_id}/cart/release", {"cart_id": "abc"}),
        ("POST", f"/api/trips/{trip_id}/vnc", None),
    ]:
        response = await rig.call(method, path, json=body)
        assert response.status == 404, path
    state = await (await rig.call("GET", "/api/state")).json()
    assert state["trips"] == []


async def test_editing_and_removing(rig: Rig) -> None:
    trip_id = await rig.add()
    await rig.sql("INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{09:00}')", trip_id)
    response = await rig.call("PATCH", f"/api/trips/{trip_id}", json={"adults": 2})
    assert response.status == 200
    (row,) = await rig.sql("SELECT rev, spec FROM trips")
    assert row["rev"] == 2
    assert json.loads(row["spec"])["adults"] == 2
    assert await rig.sql("SELECT * FROM alert_memory") == []  # a new search forgets what was alerted
    assert (await rig.call("DELETE", f"/api/trips/{trip_id}")).status == 200
    assert (await rig.sql("SELECT archived_at FROM trips"))[0]["archived_at"] is not None


# ---- what users ask of their browser


async def test_manual_checks_are_limited(rig: Rig) -> None:
    trip_id = await rig.add()
    for _ in range(6):
        assert (await rig.call("POST", f"/api/trips/{trip_id}/check")).status == 200
    response = await rig.call("POST", f"/api/trips/{trip_id}/check")
    assert response.status == 429


async def test_a_release_reaches_the_pod_as_a_command(rig: Rig) -> None:
    trip_id = await rig.add()
    assert (await rig.call("POST", f"/api/trips/{trip_id}/cart/release", json={"cart_id": "abc123"})).status == 200
    rows = await rig.sql("SELECT op, cart_id, requested_by FROM agent_commands")
    assert [tuple(r) for r in rows] == [("release", "abc123", ANN)]
    assert (await rig.call("POST", f"/api/trips/{trip_id}/cart/release", json={})).status == 400


async def test_did_you_book_it(rig: Rig) -> None:
    trip_id = await rig.add()
    assert (await rig.call("POST", f"/api/trips/{trip_id}/booked", json={"booked": False})).status == 409
    await rig.sql("UPDATE trips SET paused_reason = 'after_checkout'")
    response = await rig.call(
        "POST", f"/api/trips/{trip_id}/booked", json={"booked": False, "reason": "needed_account"}
    )
    assert response.status == 200
    assert (await rig.sql("SELECT paused_reason FROM trips"))[0]["paused_reason"] is None
    await rig.sql("UPDATE trips SET paused_reason = 'after_checkout'")
    await rig.call("POST", f"/api/trips/{trip_id}/booked", json={"booked": True})
    assert (await rig.sql("SELECT archived_at FROM trips"))[0]["archived_at"] is not None


async def test_pausing_everything(rig: Rig) -> None:
    response = await rig.call("PATCH", "/api/me", json={"paused": True})
    assert (await response.json())["me"]["paused"] is True
    assert (await rig.call("PATCH", "/api/me", json={"is_admin": True})).status == 400


# ---- checkout


async def hold(rig: Rig, trip_id: str, *, gen: int = 2) -> None:
    pod = f"fw-{trip_id}-g{gen}"
    await rig.sql(
        "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at) VALUES ($1, $2, $3, $4, 'warm',"
        " now() + interval '1 day')",
        pod,
        trip_id,
        ANN,
        gen,
    )
    cart = {
        "id": "abc123",
        "status": "ready",
        "depart": "09:00",
        "fare": "Saver",
        "hard_deadline": time.time() + 3000,
        "session_deadline": time.time() + 1500,
    }
    await rig.sql("INSERT INTO trip_state (trip_id, pod, cart) VALUES ($1, $2, $3)", trip_id, pod, json.dumps(cart))


async def test_a_checkout_ticket_opens_only_the_owners_pod(rig: Rig) -> None:
    trip_id = await rig.add()
    assert (await rig.call("POST", f"/api/trips/{trip_id}/vnc")).status == 409  # nothing held
    await hold(rig, trip_id)
    response = await rig.call("POST", f"/api/trips/{trip_id}/vnc")
    assert response.status == 200
    body = await response.json()
    prefix = f"{ORIGIN}/t/{trip_id}/g/2/#"
    assert body["url"].startswith(prefix)
    claims = verify(body["url"].removeprefix(prefix), {"k1": KEY.public_key()})
    assert (claims.sub, claims.trip, claims.pod, claims.cart_id) == (ANN, trip_id, f"fw-{trip_id}-g2", "abc123")
    assert claims.disp is not None
    assert claims.disp.depart == "09:00"
    assert [r["jti"] for r in await rig.sql("SELECT jti FROM vnc_mints")] == [claims.jti]


async def test_checkout_tickets_are_limited(rig: Rig) -> None:
    trip_id = await rig.add()
    await hold(rig, trip_id)
    for _ in range(10):
        assert (await rig.call("POST", f"/api/trips/{trip_id}/vnc")).status == 200
    assert (await rig.call("POST", f"/api/trips/{trip_id}/vnc")).status == 429


async def test_no_ticket_once_todays_checkout_time_is_used(rig: Rig) -> None:
    trip_id = await rig.add()
    await hold(rig, trip_id)
    await rig.sql(
        "INSERT INTO usage_ledger (user_id, day, kind, amount)"
        " VALUES ($1, (now() AT TIME ZONE 'America/Vancouver')::date, 'vnc_seconds', 3600)",
        ANN,
    )
    assert (await rig.call("POST", f"/api/trips/{trip_id}/vnc")).status == 409


# ---- the plumbing


async def test_writes_must_be_same_site_json(rig: Rig) -> None:
    response = await rig.call("POST", "/api/trips", json={}, headers={"Origin": "https://evil.example"})
    assert response.status == 403
    response = await rig.call("POST", "/api/trips", data="from=SWB")
    assert response.status == 415


# ---- admin


async def make_admin(rig: Rig) -> None:
    await rig.sql("UPDATE users SET is_admin = true WHERE id = $1", ANN)


async def test_admin_routes_are_for_admins(rig: Rig) -> None:
    assert (await rig.call("GET", "/api/admin/users", BOB)).status == 403
    assert (await rig.call("POST", "/api/admin/invites", BOB, json={"username": "x", "reason": "r"})).status == 403


async def test_inviting_someone(rig: Rig) -> None:
    await make_admin(rig)
    assert (await rig.call("POST", "/api/admin/invites", json={"username": "cat"})).status == 400  # no reason
    bad = {"username": "cat", "reason": "friend", "limits": {"maxTrips": 99}}
    assert (await rig.call("POST", "/api/admin/invites", json=bad)).status == 400  # past the ceiling
    good = {"username": "cat", "email": "cat@example.com", "reason": "friend", "limits": {"maxTrips": 5}}
    response = await rig.call("POST", "/api/admin/invites", json=good)
    assert response.status == 201
    listing = await (await rig.call("GET", "/api/admin/users")).json()
    assert [i["username"] for i in listing["invites"]] == ["cat"]
    audit = await rig.sql("SELECT actor, action, reason FROM audit_log")
    assert [tuple(r) for r in audit] == [(ANN, "invite", "friend")]


async def test_suspending_revokes_every_session(rig: Rig) -> None:
    await make_admin(rig)
    assert (await rig.call("GET", "/api/state", BOB)).status == 200
    response = await rig.call("POST", f"/api/admin/users/{BOB}/state", json={"state": "suspended", "reason": "abuse"})
    assert response.status == 200
    assert (await rig.call("GET", "/api/state", BOB)).status == 401
    assert (await rig.sql("SELECT state FROM users WHERE id = $1", BOB))[0]["state"] == "suspended"
    response = await rig.call("POST", f"/api/admin/users/{ANN}/state", json={"state": "suspended", "reason": "x"})
    assert response.status == 409  # not yourself


async def test_limit_overrides_stop_at_the_ceiling(rig: Rig) -> None:
    await make_admin(rig)
    path = f"/api/admin/users/{BOB}/limits"
    response = await rig.call("POST", path, json={"key": "maxTrips", "value": 7, "reason": "planning a summer"})
    assert await response.json() == {"value": 7, "source": "override"}
    response = await rig.call("POST", path, json={"key": "maxTrips", "value": 50, "reason": "lots"})
    assert await response.json() == {"value": 10, "source": "ceiling"}
    response = await rig.call("POST", path, json={"key": "maxTrips", "value": None, "reason": "back to normal"})
    assert await response.json() == {"value": 3, "source": "default"}


async def test_the_fleet_switches(rig: Rig) -> None:
    await make_admin(rig)
    body = {"switch": "pause_carting", "on": True, "reason": "Talos upgrade"}
    assert (await rig.call("POST", "/api/admin/switches", json=body)).status == 200
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert overview["switches"]["pause_carting"] is True
    body = {"switch": "brake", "on": True, "reason": "no"}
    assert (await rig.call("POST", "/api/admin/switches", json=body)).status == 400


# ---- the live socket


async def live(rig: Rig, user: str = ANN) -> aiohttp.ClientWebSocketResponse:
    ws = await rig.client.ws_connect("/api/ws")
    await ws.send_json({"type": "auth", "token": rig.tokens[user]})
    return ws


async def next_state(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    frame = await asyncio.wait_for(ws.receive(), 5)
    assert frame.type == aiohttp.WSMsgType.TEXT, frame
    message = json.loads(frame.data)
    assert message["type"] == "state"
    return dict(message["state"])


async def test_the_live_socket_follows_the_users_own_changes(rig: Rig) -> None:
    ann, bob = await live(rig), await live(rig, BOB)
    assert (await next_state(ann))["trips"] == []
    assert (await next_state(bob))["trips"] == []
    await rig.add()
    assert len((await next_state(ann))["trips"]) == 1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bob.receive(), 0.5)  # nothing of Ann's reaches Bob
    await ann.close()
    await bob.close()


async def test_the_live_socket_needs_a_token_first(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = await rig.client.ws_connect("/api/ws")
    await ws.send_json({"type": "auth", "token": "nope"})
    await ws.receive()
    assert ws.close_code == 4401
    monkeypatch.setattr("ferry_watch.web.live.AUTH_DEADLINE_SECONDS", 0.1)
    ws = await rig.client.ws_connect("/api/ws")
    await ws.receive()  # said nothing: closed
    assert ws.close_code == 4401


async def test_a_revoked_session_closes_its_socket(rig: Rig) -> None:
    ws = await live(rig)
    await next_state(ws)
    await rig.call("POST", "/auth/logout")
    await rig.add(BOB)  # any change wakes the hub... but only Bob's sockets; so nudge Ann's
    await rig.sql("SELECT pg_notify('fw_user', $1)", ANN)
    frame = await asyncio.wait_for(ws.receive(), 5)
    assert frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    assert ws.close_code == 4401
