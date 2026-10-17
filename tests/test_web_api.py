"""Each user's API on waitlist-web, on real Postgres: scoping, limits, commands, answers and checkout tickets."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any

import aiohttp
import asyncpg
import pytest
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.postgres import ROLES, Database
from waitlist import __version__
from waitlist.control.configure import StreamViewer
from waitlist.control.limits import Limits
from waitlist.models import PACIFIC
from waitlist.polling import PollPolicy
from waitlist.tickets import verify
from waitlist.web.accounts import Accounts, IdentityPolicy
from waitlist.web.api import CheckoutSigner, checkout_url
from waitlist.web.app import build
from waitlist.web.identity import OidcClient, OidcSettings, Principal
from waitlist.web.repo import WebStore

ANN, BOB = "a" * 20, "b" * 20
ISSUER = "https://idp"
BOOTSTRAP = frozenset({(ISSUER, "boss")})  # whoever signs in as "boss" is an admin by config
KEY = Ed25519PrivateKey.generate()
ORIGIN = "https://waitlist-checkout.chestr.dev"
SITE = "https://waitlist.chestr.dev"  # the dashboard's own origin (baseUrl)


def soon(days: int = 3) -> str:
    return (datetime.now(PACIFIC).date() + timedelta(days=days)).isoformat()  # the server's "today" is Pacific


def today_at(hour: int, minute: int = 0) -> float:
    """A fixed time of day today, Pacific: for what depends on the hour, whenever the tests run."""
    return datetime.combine(datetime.now(PACIFIC).date(), dt_time(hour, minute), PACIFIC).timestamp()


def trips(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The state's BC Ferries trips: its watches on BC Ferries (docs/waitlist/design.md §6.3)."""
    return [w for w in state["watches"] if w["provider"] == "bcferries"]


@dataclass
class Rig:
    client: TestClient
    db: Database
    tokens: dict[str, str]
    clock: list[float]  # what waitlist-web takes as now; 0: the real time

    async def call(self, method: str, path: str, user: str = ANN, **kw: Any) -> aiohttp.ClientResponse:
        headers = {"Authorization": f"Bearer {self.tokens[user]}"} | kw.pop("headers", {})
        return await self.client.request(method, path, headers=headers, **kw)

    async def add(self, user: str = ANN, **trip: Any) -> str:
        body = {"from": "SWB", "to": "TSA", "date": soon()} | trip
        response = await self.call("POST", "/api/trips", user, json=body)
        assert response.status == 200, await response.text()
        return str(trips(await response.json())[-1]["trip"]["id"])

    async def sql(self, query: str, *args: Any) -> list[asyncpg.Record]:
        conn = await self.db.connect()
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()


@pytest.fixture
async def rig(db: Database, aiohttp_client: Callable[..., Any], request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    """waitlist-web for Ann and Bob. `checkout.streamViewer` is "off" unless a test passes it as the rig's param."""
    stream_viewer: StreamViewer = getattr(request, "param", "off")
    owner = await db.connect()
    try:
        for uid in (ANN, BOB):
            await owner.execute("INSERT INTO users (id, username) VALUES ($1, $1)", uid)
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(db.dsn(ROLES.web), min_size=1, max_size=4)
    accounts = Accounts(pool, IdentityPolicy(bootstrap_admins=BOOTSTRAP))
    tokens = {uid: await accounts.create_session(uid, Principal(ISSUER, uid), None) for uid in (ANN, BOB)}
    clock = [0.0]
    async with aiohttp.ClientSession() as http:
        oidc = OidcClient(http, OidcSettings(issuer="https://idp", client_id="c", client_secret="s", redirect_uri="x"))
        app = build(
            accounts,
            oidc,
            WebStore(pool, Limits(), PollPolicy(), clock=lambda: clock[0] or time.time()),
            CheckoutSigner(KEY, "k1", ORIGIN, stream_viewer=stream_viewer),
            pool=pool,
            limits=Limits(),
            public={"links": {"kofi": None}, "push": {"key": "BPushKey"}},
            origin=SITE,  # served over plain http here, as behind the gateway that ends TLS
        )
        client: TestClient = await aiohttp_client(app)
        try:
            yield Rig(client, db, tokens, clock)
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
    assert [t["trip"]["id"] for t in trips(state)] == [trip_id]
    assert trips(state)[0]["browser"]["mode"] == "off"
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
        ("POST", f"/api/trips/{trip_id}/hold", None),
        ("POST", f"/api/trips/{trip_id}/cart/release", {"cart_id": "abc"}),
        ("POST", f"/api/trips/{trip_id}/vnc", None),
    ]:
        response = await rig.call(method, path, json=body)
        assert response.status == 404, path
    state = await (await rig.call("GET", "/api/state")).json()
    assert trips(state) == []


async def test_editing_and_removing(rig: Rig) -> None:
    trip_id = await rig.add()
    await rig.sql("INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{09:00}')", trip_id)
    response = await rig.call("PATCH", f"/api/trips/{trip_id}", json={"adults": 2})
    assert response.status == 200
    (row,) = await rig.sql("SELECT rev, spec FROM trips")
    assert row["rev"] == 2
    assert json.loads(row["spec"])["adults"] == 2
    assert await rig.sql("SELECT * FROM alert_memory") == []  # a new search forgets what was alerted
    await rig.sql("UPDATE trips SET paused_reason = 'after_checkout'")
    assert (await rig.call("DELETE", f"/api/trips/{trip_id}")).status == 200
    archived = await rig.sql("SELECT archived_at IS NOT NULL AS archived, enabled, paused_reason, rev FROM trips")
    assert [tuple(r) for r in archived] == [(True, False, None, 3)]
    assert (await rig.call("DELETE", f"/api/trips/{trip_id}")).status == 404


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


def sailing(depart: str, status: str = "available") -> dict[str, Any]:
    return {"depart": depart, "arrive": "10:35", "vessel": "Queen of Oak Bay", "status": status, "fares": []}


async def removed_trip(rig: Rig, trip_id: str, user: str = ANN) -> None:
    """A BC Ferries trip of the user's that was removed: what an old pod or cart claim belongs to. (Only BC
    Ferries trips have pods and claims, so each one's trip is a row; docs/waitlist/design.md §3.1.)"""
    await rig.sql(
        "INSERT INTO trips (id, user_id, provider, spec, enabled, archived_at)"
        " VALUES ($1, $2, 'bcferries', '{}', false, now())",
        trip_id,
        user,
    )


async def seen(rig: Rig, trip_id: str, *sailings: dict[str, Any], cart: dict[str, Any] | None = None) -> None:
    """What the trip's pod last reported."""
    await rig.sql(
        "INSERT INTO trip_state (trip_id, pod, sailings, cart) VALUES ($1, 'p', $2, $3)",
        trip_id,
        json.dumps(list(sailings)),
        json.dumps(cart) if cart is not None else None,
    )


async def test_hold_it_for_me(rig: Rig) -> None:
    trip_id = await rig.add(earliest="08:00", latest="12:00")
    await seen(rig, trip_id, sailing("07:00"), sailing("09:00"))
    before = time.time()
    response = await rig.call("POST", f"/api/trips/{trip_id}/hold")
    assert response.status == 200, await response.text()
    (trip,) = trips(await response.json())
    assert trip["hold"]["can_request"] is True
    assert before + 590 < trip["hold"]["requested_until"] < time.time() + 610  # quiet hours are lifted for 10 min
    assert trip["hold"]["requested_until"] - trip["hold"]["requested_at"] == 600
    assert trip["booking_url"] is None  # no terminal list in this rig
    rows = await rig.sql("SELECT op, requested_by FROM agent_commands")
    assert [tuple(r) for r in rows] == [("check_now", ANN)]  # and it checks now, as a manual check


async def test_hold_it_for_me_needs_something_to_hold(rig: Rig) -> None:
    trip_id = await rig.add(earliest="08:00", latest="12:00")
    assert (await rig.call("POST", f"/api/trips/{trip_id}/hold")).status == 409  # not checked yet
    await seen(rig, trip_id, sailing("07:00"), sailing("09:00", "sold_out"))
    response = await rig.call("POST", f"/api/trips/{trip_id}/hold")
    assert (response.status, (await response.json())["error"]) == (409, "Nothing in your times is bookable right now")
    await rig.sql(
        "UPDATE trip_state SET sailings = $1, cart = $2", json.dumps([sailing("09:00")]), '{"status": "ready"}'
    )
    assert (await rig.call("POST", f"/api/trips/{trip_id}/hold")).status == 409  # already held
    assert await rig.sql("SELECT 1 FROM trip_runtime WHERE hold_requested_until IS NOT NULL") == []


async def test_hold_it_for_me_keeps_the_other_limits(rig: Rig) -> None:
    trip_id = await rig.add()
    await seen(rig, trip_id, sailing("09:00"))
    await removed_trip(rig, "f" * 16)
    await rig.sql(
        "INSERT INTO hold_claims (user_id, trip_id, pod, cart_id) VALUES ($1, 'ffffffffffffffff', 'x', 'c')", ANN
    )
    response = await rig.call("POST", f"/api/trips/{trip_id}/hold")
    assert (response.status, (await response.json())["error"]) == (
        409,
        "You're already holding a cart on another trip",
    )
    state = await (await rig.call("GET", "/api/state")).json()
    assert trips(state)[0]["hold"] == {
        "reason": "holding_elsewhere",
        "can_request": False,
        "holdable": ["09:00"],
        "requested_at": None,
        "requested_until": None,
    }
    await rig.sql("DELETE FROM hold_claims")
    await rig.sql(
        "INSERT INTO usage_ledger (user_id, day, kind, amount) VALUES ($1, $2, 'holds', 4)",
        ANN,
        datetime.now(PACIFIC).date(),
    )
    response = await rig.call("POST", f"/api/trips/{trip_id}/hold")
    assert (response.status, (await response.json())["error"]) == (409, "You've used today's holds")
    await rig.sql("DELETE FROM usage_ledger")
    for _ in range(6):
        assert (await rig.call("POST", f"/api/trips/{trip_id}/check")).status == 200
    assert (await rig.call("POST", f"/api/trips/{trip_id}/hold")).status == 429  # it's a manual check too
    assert await rig.sql("SELECT 1 FROM trip_runtime WHERE hold_requested_until IS NOT NULL") == []


async def test_no_hold_is_offered_for_a_sailing_already_held_once(rig: Rig) -> None:
    """Released, expired or viewed: the pod never carts that departure again, so asking would do nothing."""
    trip_id = await rig.add(earliest="08:00", latest="12:00")
    await seen(rig, trip_id, sailing("09:00"), sailing("11:00"))
    await rig.sql("INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{09:00}')", trip_id)
    state = await (await rig.call("GET", "/api/state")).json()
    assert trips(state)[0]["hold"]["holdable"] == ["11:00"]
    assert trips(state)[0]["hold"]["can_request"] is True
    await rig.sql("UPDATE alert_memory SET alerted = '{09:00,11:00}'")
    state = await (await rig.call("GET", "/api/state")).json()
    hold = trips(state)[0]["hold"]
    assert (hold["reason"], hold["can_request"], hold["holdable"]) == ("already_held", False, [])
    response = await rig.call("POST", f"/api/trips/{trip_id}/hold")
    assert (response.status, (await response.json())["error"]) == (
        409,
        "It was held for you once already; book it yourself on bcferries.com",
    )


async def test_a_paused_trip_offers_no_hold(rig: Rig) -> None:
    trip_id = await rig.add()
    await seen(rig, trip_id, sailing("09:00"))  # what the last check saw, before the pause
    response = await rig.call("PATCH", f"/api/trips/{trip_id}", json={"enabled": False})
    assert response.status == 200
    state = await response.json()
    assert trips(state)[0]["hold"] == {
        "reason": "paused",
        "can_request": False,
        "holdable": [],
        "requested_at": None,
        "requested_until": None,
    }
    response = await rig.call("POST", f"/api/trips/{trip_id}/hold")
    assert (response.status, (await response.json())["error"]) == (409, "This trip is paused")


async def test_a_lapsed_claim_isnt_a_hold_elsewhere(rig: Rig) -> None:
    """A claim whose cart never came (a failed try on an old pod, a claim that timed out) lapses after 2 min."""
    trip_id = await rig.add()
    await seen(rig, trip_id, sailing("09:00"))
    await removed_trip(rig, "f" * 16)
    await rig.sql(
        "INSERT INTO hold_claims (user_id, trip_id, pod, claimed_at) VALUES ($1, 'ffffffffffffffff', 'x',"
        " now() - interval '5 minutes')",
        ANN,
    )
    assert (await rig.call("POST", f"/api/trips/{trip_id}/hold")).status == 200


async def test_editing_a_trip_drops_its_hold_request(rig: Rig) -> None:
    trip_id = await rig.add()
    await seen(rig, trip_id, sailing("09:00"))
    assert (await rig.call("POST", f"/api/trips/{trip_id}/hold")).status == 200
    assert (await rig.call("PATCH", f"/api/trips/{trip_id}", json={"adults": 2})).status == 200
    assert await rig.sql("SELECT 1 FROM trip_runtime WHERE hold_requested_until IS NOT NULL") == []


async def test_a_sailing_the_state_cant_read_doesnt_break_it(rig: Rig) -> None:
    trip_id = await rig.add()
    await seen(rig, trip_id, {"depart": "9am", "status": "available"}, sailing("09:00"))
    state = await (await rig.call("GET", "/api/state")).json()
    assert trips(state)[0]["hold"]["holdable"] == ["09:00"]


async def test_the_state_says_why_a_trip_wouldnt_be_held(rig: Rig) -> None:
    trip_id = await rig.add(auto_cart=False)
    rig.clock[0] = today_at(12)  # outside the quiet hours everyone has
    state = await (await rig.call("GET", "/api/state")).json()
    hold = trips(state)[0]["hold"]
    assert hold == {
        "reason": "auto_cart_off",
        "can_request": True,
        "holdable": [],
        "requested_at": None,
        "requested_until": None,
    }
    await rig.sql("UPDATE fleet_state SET value = 'true' WHERE key = 'pause_carting'")
    state = await (await rig.call("GET", "/api/state")).json()
    assert trips(state)[0]["hold"]["reason"] == "carting_paused"
    assert trips(state)[0]["hold"]["can_request"] is False
    assert trip_id


async def after_checkout(rig: Rig, trip_id: str) -> None:
    """A viewed hold ended without a booking seen: its outcome is recorded, and the trip asks "Did you book it?"."""
    await rig.sql(
        "INSERT INTO hold_outcomes (user_id, trip_id, day, outcome) VALUES ($1, $2, now(), 'expired')", ANN, trip_id
    )
    await rig.sql("UPDATE trips SET paused_reason = 'after_checkout' WHERE id = $1", trip_id)


async def archived(rig: Rig, trip_id: str) -> tuple[Any, ...]:
    (trip,) = await rig.sql(
        "SELECT t.archived_at IS NOT NULL AS archived, t.enabled, t.paused_reason, t.rev,"
        " t.updated_at > t.created_at AS updated, rt.hold_requested_until FROM trips t"
        " JOIN trip_runtime rt ON rt.trip_id = t.id WHERE t.id = $1",
        trip_id,
    )
    return tuple(trip)


async def answers(rig: Rig) -> list[str | None]:
    return [r["answer_reason"] for r in await rig.sql("SELECT answer_reason FROM hold_outcomes ORDER BY id")]


async def test_did_you_book_it(rig: Rig) -> None:
    trip_id = await rig.add()
    path = f"/api/trips/{trip_id}/booked"
    assert (await rig.call("POST", path, json={"booked": False})).status == 409
    await after_checkout(rig, trip_id)
    response = await rig.call("POST", path, json={"booked": False, "reason": "needed_account"})
    assert response.status == 200
    assert (await rig.sql("SELECT paused_reason FROM trips"))[0]["paused_reason"] is None
    await after_checkout(rig, trip_id)
    await rig.sql("UPDATE trip_runtime SET hold_requested_until = now() + interval '5 minutes'")
    assert (await rig.call("POST", path, json={"booked": True})).status == 200
    assert await archived(rig, trip_id) == (True, False, None, 3, True, None)  # as a removed trip is
    assert await answers(rig) == ["needed_account", "booked"]


async def test_no_cancel_stops_watching_the_trip(rig: Rig) -> None:
    """ "Did you book it?" "No, cancel": not booked, and not wanted any more."""
    trip_id, other = await rig.add(), await rig.add()
    await after_checkout(rig, trip_id)
    await rig.sql("UPDATE trip_runtime SET hold_requested_until = now() + interval '5 minutes'")
    path = f"/api/trips/{trip_id}/booked"
    for body in ({"booked": True, "stop": True}, {"booked": False, "stop": "yes"}):
        response = await rig.call("POST", path, json=body)
        assert (response.status, (await response.json())["field"]) == (400, "stop")
    response = await rig.call("POST", path, json={"booked": False, "stop": True})
    assert response.status == 200
    assert [t["trip"]["id"] for t in trips(await response.json())] == [other]  # its slot is free again
    assert await archived(rig, trip_id) == (True, False, None, 2, True, None)
    assert await answers(rig) == ["cancelled"]
    assert (await rig.call("POST", path, json={"booked": False, "stop": True})).status == 409  # answered


async def test_pausing_and_resuming_one_trip(rig: Rig) -> None:
    """Trips are paused one at a time ("pause all" is gone); resuming one checks it soon."""
    trip_id, other = await rig.add(), await rig.add()
    state = await (await rig.call("PATCH", f"/api/trips/{trip_id}", json={"enabled": False})).json()
    assert [(t["trip"]["id"], t["trip"]["enabled"]) for t in trips(state)] == [(trip_id, False), (other, True)]
    assert state["me"]["paused"] is False  # kept a release for the old dashboard; always false
    await rig.sql("UPDATE trip_runtime SET next_check_at = now() + interval '6 hours'")
    assert (await rig.call("PATCH", f"/api/trips/{trip_id}", json={"enabled": True})).status == 200
    rows = await rig.sql("SELECT trip_id, next_check_at IS NULL AS now FROM trip_runtime ORDER BY trip_id = $1", other)
    assert [(r["trip_id"], r["now"]) for r in rows] == [(trip_id, True), (other, False)]


async def test_the_old_account_pause_moves_onto_each_trip(rig: Rig) -> None:
    """A "Pause all" from a waitlist-web before 0004 (users.paused) becomes each live trip paused, as 0004 did,
    the next time the user's state is read; the account itself is never shown paused."""
    trip_id, other = await rig.add(), await rig.add()
    await rig.call("PATCH", f"/api/trips/{other}", json={"enabled": False})
    await rig.sql("UPDATE users SET paused = true WHERE id = $1", ANN)  # as a waitlist-web before 0004 could
    before = {r["id"]: r["rev"] for r in await rig.sql("SELECT id, rev FROM trips")}
    state = await (await rig.call("GET", "/api/state")).json()
    assert state["me"]["paused"] is False
    assert [t["trip"]["enabled"] for t in trips(state)] == [False, False]
    rows = await rig.sql("SELECT id, enabled, spec->>'enabled' AS spec, rev FROM trips ORDER BY id = $1", trip_id)
    assert [(r["enabled"], r["spec"], r["rev"] - before[r["id"]]) for r in rows] == [
        (False, "false", 0),  # already paused: untouched
        (False, "false", 1),
    ]
    assert [r["paused"] for r in await rig.sql("SELECT paused FROM users")] == [False, False]
    assert (await rig.call("POST", f"/api/trips/{trip_id}/hold")).status == 409  # paused
    await make_admin(rig)
    people = await (await rig.call("GET", "/api/admin/users")).json()
    assert [u["paused"] for u in people["users"]] == [False, False]  # kept a release; always false


# ---- quiet hours


DEFAULT = {"start": "22:00", "end": "05:00"}  # everyone's, until they change them


async def test_quiet_hours_are_each_users_own(rig: Rig) -> None:
    rig.clock[0] = today_at(12)
    state = await (await rig.call("GET", "/api/state")).json()
    assert (state["settings"]["quiet_hours"], state["quiet_hours"]) == (DEFAULT, DEFAULT | {"now": False})
    assert await rig.sql("SELECT * FROM user_settings") == []  # the default needs no row
    night = {"start": "23:00", "end": "06:30"}
    response = await rig.call("PATCH", "/api/me", json={"quiet_hours": night})
    assert response.status == 200, await response.text()
    state = await response.json()
    assert (state["settings"]["quiet_hours"], state["quiet_hours"]) == (night, night | {"now": False})
    rows = await rig.sql("SELECT user_id, quiet_start, quiet_end FROM user_settings")
    assert [tuple(r) for r in rows] == [(ANN, dt_time(23, 0), dt_time(6, 30))]

    rig.clock[0] = today_at(22, 30)
    bobs = await rig.add(BOB)
    await rig.sql("UPDATE trip_runtime SET next_check_at = to_timestamp($1)", today_at(22, 45))
    bob = await (await rig.call("GET", "/api/state", BOB)).json()
    assert (bob["settings"]["quiet_hours"], bob["quiet_hours"]) == (DEFAULT, DEFAULT | {"now": True})
    (trip,) = trips(bob)
    five = datetime.combine(datetime.now(PACIFIC).date() + timedelta(days=1), dt_time(5), PACIFIC).timestamp()
    assert (trip["trip"]["id"], trip["quiet"], trip["browser"]["next_check_at"]) == (bobs, True, five)
    state = await (await rig.call("GET", "/api/state")).json()
    assert state["quiet_hours"]["now"] is False  # Ann's start at 11 pm


async def test_quiet_hours_a_063_waitlist_web_turned_off_are_the_default(rig: Rig) -> None:
    """During the rollout a 0.6.3 waitlist-web can still store "off" (no times): everyone has quiet hours now."""
    await rig.sql("INSERT INTO user_settings (user_id, quiet_start, quiet_end) VALUES ($1, NULL, NULL)", ANN)
    rig.clock[0] = today_at(23)
    state = await (await rig.call("GET", "/api/state")).json()
    assert (state["settings"]["quiet_hours"], state["quiet_hours"]) == (DEFAULT, DEFAULT | {"now": True})


async def test_quiet_hours_cant_be_turned_off(rig: Rig) -> None:
    for body in ({"quiet_hours": None}, {}):
        response = await rig.call("PATCH", "/api/me", json=body)
        assert (response.status, await response.json()) == (
            400,
            {"error": "Quiet hours need a start and an end", "field": "quiet_hours"},
        )
    assert await rig.sql("SELECT * FROM user_settings") == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"paused": True},  # pausing is per trip now
        {"quiet_hours": None, "paused": False},
        {"quiet_hours": {"start": "23:00", "end": "06:00"}, "paused": False},
        {"quiet_hours": "23:00-06:00"},
        {"quiet_hours": {"start": "23:00"}},
        {"quiet_hours": {"start": "23:00", "end": None}},
        {"quiet_hours": {"start": None, "end": None}},
        {"quiet_hours": {"start": "23:00", "end": "06:00", "days": "weekdays"}},
        {"quiet_hours": {"start": "24:00", "end": "06:00"}},
        {"quiet_hours": {"start": "7:00", "end": "09:00"}},
        {"quiet_hours": {"start": "23:00:00", "end": "06:00"}},
        {"quiet_hours": {"start": 2300, "end": "06:00"}},
        {"quiet_hours": {"start": "06:00", "end": "06:00"}},  # covers nothing
    ],
)
async def test_quiet_hours_are_two_different_times_of_day(rig: Rig, body: dict[str, Any]) -> None:
    response = await rig.call("PATCH", "/api/me", json=body)
    assert (response.status, (await response.json())["field"]) == (400, "quiet_hours")
    assert await rig.sql("SELECT * FROM user_settings") == []


async def test_in_quiet_hours_a_trip_isnt_checked_until_lifted(rig: Rig) -> None:
    checked, asked = await rig.add(), await rig.add()
    await seen(rig, checked, sailing("09:00"))
    await seen(rig, asked, sailing("09:00"))
    await rig.call("PATCH", "/api/me", json={"quiet_hours": {"start": "23:00", "end": "06:00"}})
    rig.clock[0] = today_at(23, 30)

    async def by_id() -> dict[str, dict[str, Any]]:
        state = await (await rig.call("GET", "/api/state")).json()
        return {t["trip"]["id"]: t for t in trips(state)}

    state = await (await rig.call("GET", "/api/state")).json()
    assert state["quiet_hours"] == {"start": "23:00", "end": "06:00", "now": True}
    for trip in (await by_id()).values():
        assert trip["quiet"] is True
        # Not held for the quiet hours, but "Hold it for me" lifts them.
        assert (trip["hold"]["reason"], trip["hold"]["can_request"]) == ("quiet_hours", True)

    assert (await rig.call("POST", f"/api/trips/{checked}/check")).status == 200  # a manual check lifts them
    (row,) = await rig.sql("SELECT quiet_lifted_until FROM trip_runtime WHERE trip_id = $1", checked)
    assert row["quiet_lifted_until"].timestamp() == rig.clock[0] + 600
    assert (await rig.call("POST", f"/api/trips/{asked}/hold")).status == 200  # and so does asking for a hold
    for trip in (await by_id()).values():
        assert (trip["quiet"], trip["hold"]["reason"]) == (False, None)  # checked, and it may hold a cart

    rig.clock[0] += 601
    assert [t["quiet"] for t in (await by_id()).values()] == [True, True]  # 10 minutes, then quiet again
    await rig.sql("UPDATE trip_runtime SET next_check_at = to_timestamp($1)", today_at(23, 45))
    morning = datetime.combine(datetime.now(PACIFIC).date() + timedelta(days=1), dt_time(6), PACIFIC).timestamp()
    assert [t["browser"]["next_check_at"] for t in (await by_id()).values()] == [morning, morning]
    rig.clock[0] = today_at(12)
    state = await (await rig.call("GET", "/api/state")).json()
    assert state["quiet_hours"]["now"] is False
    assert [(t["quiet"], t["hold"]["reason"]) for t in trips(state)] == [(False, None), (False, None)]


# ---- checkout


async def hold(rig: Rig, trip_id: str, *, gen: int = 2, agent_version: str | None = "0.6.4") -> None:
    """Ann's cart held by the trip's pod, whose agent said hello with `agent_version` (None: not yet)."""
    pod = f"wl-{trip_id}-g{gen}"
    await rig.sql(
        "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at, agent_version)"
        " VALUES ($1, $2, $3, $4, 'warm', now() + interval '1 day', $5)",
        pod,
        trip_id,
        ANN,
        gen,
        agent_version,
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
    assert (body["viewer"], body["nonce_key"]) == ("classic", f"{trip_id}:2")
    claims = verify(body["url"].removeprefix(prefix), {"k1": KEY.public_key()})
    assert (claims.sub, claims.trip, claims.pod, claims.cart_id) == (ANN, trip_id, f"wl-{trip_id}-g2", "abc123")
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


# ---- the checkout viewer (beta)


@pytest.mark.parametrize(
    ("rig", "admin", "offered"),
    [
        ("off", False, False),
        ("off", True, False),  # the kill switch: nobody, the admins included
        ("admins", False, False),
        ("admins", True, True),
        ("everyone", False, True),
        ("everyone", True, True),
    ],
    indirect=["rig"],
)
async def test_the_beta_is_offered_as_the_deployment_says(rig: Rig, admin: bool, offered: bool) -> None:
    if admin:
        await make_admin(rig)
    state = await (await rig.call("GET", "/api/state")).json()
    assert state["settings"] == {
        "quiet_hours": DEFAULT,
        "checkout_viewer": "classic",  # until they turn it on
        "stream_viewer_offered": offered,
    }
    response = await rig.call("PATCH", "/api/me", json={"checkout_viewer": "stream"})
    if not offered:
        assert (response.status, (await response.json())["field"]) == (403, "checkout_viewer")
        assert await rig.sql("SELECT * FROM user_settings") == []
    else:
        assert response.status == 200, await response.text()
        assert (await response.json())["settings"]["checkout_viewer"] == "stream"
    # Turning it off is always allowed.
    response = await rig.call("PATCH", "/api/me", json={"checkout_viewer": "classic"})
    assert response.status == 200
    assert (await response.json())["settings"] == state["settings"]
    ann = await live(rig)
    assert (await next_state(ann))["settings"] == state["settings"]  # the live socket says the same
    await ann.close()


@pytest.mark.parametrize("rig", ["everyone"], indirect=True)
async def test_the_viewer_and_quiet_hours_are_set_apart_or_together(rig: Rig) -> None:
    response = await rig.call("PATCH", "/api/me", json={"checkout_viewer": "stream"})
    assert response.status == 200
    rows = await rig.sql("SELECT quiet_start, quiet_end, checkout_viewer FROM user_settings")
    assert [tuple(r) for r in rows] == [(dt_time(22), dt_time(5), "stream")]  # the quiet hours stay the default
    night = {"start": "23:00", "end": "06:30"}
    assert (await rig.call("PATCH", "/api/me", json={"quiet_hours": night})).status == 200
    rows = await rig.sql("SELECT quiet_start, quiet_end, checkout_viewer FROM user_settings")
    assert [tuple(r) for r in rows] == [(dt_time(23), dt_time(6, 30), "stream")]  # and the viewer stays
    both = {"quiet_hours": DEFAULT, "checkout_viewer": "classic"}
    state = await (await rig.call("PATCH", "/api/me", json=both)).json()
    assert state["settings"] == both | {"stream_viewer_offered": True}


@pytest.mark.parametrize(
    "body",
    [
        {"checkout_viewer": "kasm"},
        {"checkout_viewer": "STREAM"},
        {"checkout_viewer": None},
        {"checkout_viewer": True},
        {"checkout_viewer": ["stream"]},
        {"checkout_viewer": {"viewer": "stream"}},
    ],
)
@pytest.mark.parametrize("rig", ["everyone"], indirect=True)
async def test_the_checkout_viewer_is_classic_or_stream(rig: Rig, body: dict[str, Any]) -> None:
    response = await rig.call("PATCH", "/api/me", json=body)
    assert (response.status, (await response.json())["field"]) == (400, "checkout_viewer")
    assert await rig.sql("SELECT * FROM user_settings") == []


@pytest.mark.parametrize("rig", ["admins"], indirect=True)
async def test_nothing_is_stored_when_one_setting_is_refused(rig: Rig) -> None:
    night = {"start": "23:00", "end": "06:30"}
    response = await rig.call("PATCH", "/api/me", json={"quiet_hours": night, "checkout_viewer": "stream"})
    assert response.status == 403  # Ann isn't an admin
    response = await rig.call(
        "PATCH", "/api/me", json={"quiet_hours": {"start": "23:00"}, "checkout_viewer": "classic"}
    )
    assert (response.status, (await response.json())["field"]) == (400, "quiet_hours")
    response = await rig.call("PATCH", "/api/me", json={"checkout_viewer": "classic", "paused": True})
    assert (response.status, (await response.json())["field"]) == (400, "quiet_hours")  # unknown keys, as before
    assert await rig.sql("SELECT * FROM user_settings") == []


async def opened(rig: Rig, trip_id: str) -> tuple[str, str]:
    """Open the trip's checkout: the response's viewer and its link's fragment, checked to carry a good ticket."""
    response = await rig.call("POST", f"/api/trips/{trip_id}/vnc")
    assert response.status == 200, await response.text()
    body = await response.json()
    prefix = f"{ORIGIN}/t/{trip_id}/g/2/#"
    assert body["url"].startswith(prefix)
    fragment = body["url"].removeprefix(prefix)
    ticket = fragment.removeprefix("v=s1&t=") if body["viewer"] == "stream" else fragment
    claims = verify(ticket, {"k1": KEY.public_key()})
    assert (claims.sub, claims.trip, claims.cart_id) == (ANN, trip_id, "abc123")  # the same ticket either way
    await rig.sql("DELETE FROM vnc_mints")  # not up against the 10 a minute
    return body["viewer"], fragment


@pytest.mark.parametrize("rig", ["everyone"], indirect=True)
async def test_the_stream_link_needs_the_opt_in_and_an_agent_that_streams(rig: Rig) -> None:
    trip_id = await rig.add()
    await hold(rig, trip_id, agent_version="0.7.0")
    viewer, fragment = await opened(rig, trip_id)
    assert viewer == "classic"  # offered, but not turned on: 0.6.4's link
    assert fragment.startswith("wl1.")
    assert "&" not in fragment
    assert (await rig.call("PATCH", "/api/me", json={"checkout_viewer": "stream"})).status == 200
    viewer, fragment = await opened(rig, trip_id)
    assert viewer == "stream"
    assert fragment.startswith("v=s1&t=wl1.")
    for newer in ("0.7.1", "0.8.0", "1.0.0", "0.7.0.post1", "0.7.1rc1", "0.7.0+local"):
        await rig.sql("UPDATE browser_pods SET agent_version = $1", newer)
        assert (await opened(rig, trip_id))[0] == "stream", newer
    # A pod from before the upgrade, or one whose version can't be read: KasmVNC, which every pod has.
    for older in ("0.6.4", "0.6.10", "0.7.0rc1", "0.7.0.dev3", "0.0.0", None, "", "garbage", "v0.7.0", "0.7.0 "):
        await rig.sql("UPDATE browser_pods SET agent_version = $1", older)
        assert (await opened(rig, trip_id))[0] == "classic", older


@pytest.mark.parametrize("rig", ["admins"], indirect=True)
async def test_a_withdrawn_beta_opens_classic_whatever_was_chosen(rig: Rig) -> None:
    """Turned on while it was offered (or stored by hand), then withdrawn: classic links until it's offered again."""
    trip_id = await rig.add()
    await hold(rig, trip_id, agent_version="0.7.0")
    await rig.sql("INSERT INTO user_settings (user_id, checkout_viewer) VALUES ($1, 'stream')", ANN)
    assert (await opened(rig, trip_id))[0] == "classic"  # Ann isn't an admin
    state = await (await rig.call("GET", "/api/state")).json()
    assert (state["settings"]["checkout_viewer"], state["settings"]["stream_viewer_offered"]) == ("stream", False)
    await make_admin(rig)
    assert (await opened(rig, trip_id))[0] == "stream"


async def test_the_classic_link_is_064s(rig: Rig) -> None:
    """Everyone not on the beta gets exactly the link 0.6.4 made: the ticket alone in the fragment."""
    trip_id = await rig.add()
    await hold(rig, trip_id, agent_version="0.7.0")
    await rig.sql("INSERT INTO user_settings (user_id, checkout_viewer) VALUES ($1, 'stream')", ANN)  # "off" wins
    response = await rig.call("POST", f"/api/trips/{trip_id}/vnc")
    body = await response.json()
    (row,) = await rig.sql("SELECT jti FROM vnc_mints")
    ticket = body["url"].rpartition("#")[2]
    assert verify(ticket, {"k1": KEY.public_key()}).jti == row["jti"]
    assert body == {
        "url": f"{ORIGIN}/t/{trip_id}/g/2/#{ticket}",
        "ttl": 60,
        "nonce_key": f"{trip_id}:2",
        "viewer": "classic",
    }
    assert checkout_url(ORIGIN, trip_id, 2, ticket, "classic") == f"{ORIGIN}/t/{trip_id}/g/2/#{ticket}"
    assert checkout_url(ORIGIN, trip_id, 2, ticket, "stream") == f"{ORIGIN}/t/{trip_id}/g/2/#v=s1&t={ticket}"


# ---- the plumbing


async def test_writes_must_be_same_site_json(rig: Rig) -> None:
    response = await rig.call("POST", "/api/trips", json={}, headers={"Origin": "https://evil.example"})
    assert response.status == 403
    response = await rig.call("POST", "/api/trips", data="from=SWB")
    assert response.status == 415


async def test_the_site_is_its_configured_origin_not_the_requests(rig: Rig) -> None:
    """The gateway ends TLS, so waitlist-web sees http://<host>; browsers on the site send https://<site>."""
    body = {"from": "SWB", "to": "TSA", "date": soon()}
    response = await rig.call("POST", "/api/trips", json=body, headers={"Origin": SITE})
    assert response.status == 200, await response.text()
    plain = f"http://{rig.client.host}:{rig.client.port}"  # what the request itself looks like
    response = await rig.call("POST", "/api/trips", json=body, headers={"Origin": plain})
    assert response.status == 403
    ws = await rig.client.ws_connect("/api/ws", headers={"Origin": SITE})
    await ws.close()
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await rig.client.ws_connect("/api/ws", headers={"Origin": "https://evil.example"})


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
    body = {"switch": "pause_carting", "on": True, "reason": "Node upgrade"}
    assert (await rig.call("POST", "/api/admin/switches", json=body)).status == 200
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert overview["switches"]["pause_carting"] is True
    body = {"switch": "brake", "on": True, "reason": "no"}
    assert (await rig.call("POST", "/api/admin/switches", json=body)).status == 400


async def test_the_overview_shows_the_fleet_and_what_needs_attention(rig: Rig) -> None:
    await make_admin(rig)
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert (overview["fleet"], overview["control_seen"], overview["attention"]) == (None, None, [])
    report = {"at": 1_000.0, "browsers": {"max": 7, "live": 1}, "breaker": "closed"}
    await rig.sql("INSERT INTO fleet_state (key, value) VALUES ('fleet_report', $1)", json.dumps(report))
    trip_id = await rig.add(BOB)
    await rig.sql("UPDATE trip_runtime SET needs_attention = true, failures = 5, updated_at = now() - interval '1 h'")
    for kind, sent, attempts, age in [
        ("cart_held", None, 3, "2 hours"),  # not delivered
        ("release_unverified", "now()", 1, "1 minute"),
        ("checkout_blocked", "now()", 1, "8 days"),  # too long ago
        ("cart_lost", None, 1, "1 minute"),  # still trying
    ]:
        await rig.sql(
            f"INSERT INTO notifications (user_id, trip_id, kind, params, sent_at, attempts, last_error, created_at)"  # noqa: S608
            f" VALUES ($1, $2, $3, $4, {sent or 'NULL'}, $5, 'Undelivered', now() - interval '{age}')",
            BOB,
            trip_id,
            kind,
            json.dumps({"departures": ["09:00"], "fare": "Saver"}),
            attempts,
        )
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert overview["fleet"] == report
    assert "fleet_report" not in overview["switches"]
    assert overview["control_seen"] == 1_000.0
    attention = overview["attention"]
    assert [(a["kind"], a["trip"], a["user"], a["username"]) for a in attention] == [
        ("release_unverified", trip_id, BOB, BOB),
        ("needs_attention", trip_id, BOB, BOB),
        ("delivery_failing", trip_id, BOB, BOB),
    ]
    assert attention[1]["detail"] == "Its browser failed 5 times; checks are stopped"
    assert attention[2]["detail"] == "Not delivered after 3 tries: cart held (Undelivered)"
    assert "09:00" not in json.dumps(attention)  # never a sailing
    await rig.sql("INSERT INTO fleet_state (key, value) VALUES ('control_alive_at', '2000')")
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert overview["control_seen"] == 2_000.0


async def test_the_overview_shows_what_versions_run(rig: Rig) -> None:
    await make_admin(rig)

    async def versions() -> dict[str, Any]:
        return dict((await (await rig.call("GET", "/api/admin/overview")).json())["versions"])

    assert await versions() == {"web": {"version": __version__}, "control": None, "browsers": []}
    report = {"at": 1_000.0, "browsers": {"max": 7, "live": 3}}  # from a waitlist-control too old to say
    await rig.sql("INSERT INTO fleet_state (key, value) VALUES ('fleet_report', $1)", json.dumps(report))
    assert (await versions())["control"] is None
    await rig.sql(
        "UPDATE fleet_state SET value = $1 WHERE key = 'fleet_report'", json.dumps(report | {"version": "9.9"})
    )
    image = "ghcr.io/mchestr/waitlist-browser:0.6.3@sha256:" + "b" * 64
    for trip_id in ("1" * 16, "2" * 16, "3" * 16):
        await removed_trip(rig, trip_id, BOB)
    for gen, version, ended in [(1, "0.6.2", True), (2, "0.6.3", False)]:  # the same trip's old pod and its new one
        await rig.sql(
            "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at, image_digest, agent_version,"
            " ended_at) VALUES ($1, $2, $3, $4, 'warm', now(), $5, $6, CASE WHEN $7 THEN now() END)",
            f"wl-{'1' * 16}-g{gen}",
            "1" * 16,
            BOB,
            gen,
            image,
            version,
            ended,
        )
    for trip_id, said in [("2" * 16, "0.6.3"), ("3" * 16, None)]:  # None: it hasn't said hello yet
        await rig.sql(
            "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at, image_digest, agent_version)"
            " VALUES ($1, $2, $3, 1, 'short_check', now(), $4, $5)",
            f"wl-{trip_id}-g1",
            trip_id,
            BOB,
            image,
            said,
        )
    assert await versions() == {
        "web": {"version": __version__},
        "control": {"version": "9.9", "at": 1_000.0},
        "browsers": [{"version": "0.6.3", "image": image, "pods": 2}, {"version": None, "image": image, "pods": 1}],
    }


async def held(rig: Rig, trip_id: str, user: str = BOB) -> str:
    """A live pod for the trip, holding cart abc123. Returns the pod's name."""
    pod = f"wl-{trip_id}-g1"
    await rig.sql(
        "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at)"
        " VALUES ($1, $2, $3, 1, 'warm', now() + interval '1 day')",
        pod,
        trip_id,
        user,
    )
    cart = {"id": "abc123", "status": "ready", "depart": "09:00", "hard_deadline": time.time() + 3000}
    await rig.sql("INSERT INTO trip_state (trip_id, pod, cart) VALUES ($1, $2, $3)", trip_id, pod, json.dumps(cart))
    await rig.sql(
        "INSERT INTO hold_claims (user_id, trip_id, pod, cart_id) VALUES ($1, $2, $3, 'abc123')", user, trip_id, pod
    )
    return pod


async def test_an_admin_releases_a_held_cart(rig: Rig) -> None:
    await make_admin(rig)
    trip_id = await rig.add(BOB)
    path = f"/api/admin/trips/{trip_id}/release"
    response = await rig.call("POST", path, json={"reason": "stuck"})
    assert (response.status, (await response.json())["error"]) == (404, "Nothing is held")
    pod = await held(rig, trip_id)
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert [(h["trip"], h["pod"]) for h in overview["holds"]] == [(trip_id, pod)]
    assert (await rig.call("POST", path, BOB, json={"reason": "mine"})).status == 403
    assert (await rig.call("POST", path, json={})).status == 400  # no reason
    response = await rig.call("POST", path, json={"reason": "they asked by email"})
    assert await response.json() == {"ok": True}
    rows = await rig.sql("SELECT trip_id, op, cart_id, requested_by FROM agent_commands")
    assert [tuple(r) for r in rows] == [(trip_id, "release", "abc123", f"admin:{ANN}")]
    audit = await rig.sql("SELECT action, target_id, after, reason FROM audit_log")
    assert [(a["action"], a["target_id"], json.loads(a["after"]), a["reason"]) for a in audit] == [
        ("release_cart", trip_id, {"cart_id": "abc123"}, "they asked by email")
    ]


@pytest.mark.parametrize(
    ("cart", "why"),
    [
        # Its pod would drop the release: the cart gets a new id once it's in.
        ({"status": "adding"}, "Still being added; try again in a moment"),
        # Pulling the cart mid-payment could charge them for nothing.
        ({"status": "ready", "payment_in_progress": True}, "They're paying for it now"),
    ],
)
async def test_an_admin_cant_release_a_cart_being_added_or_paid_for(
    rig: Rig, cart: dict[str, object], why: str
) -> None:
    await make_admin(rig)
    trip_id = await rig.add(BOB)
    await held(rig, trip_id)
    await rig.sql("UPDATE trip_state SET cart = cart || $2::jsonb WHERE trip_id = $1", trip_id, json.dumps(cart))
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert [h["cart"].get("payment_in_progress") for h in overview["holds"]] == [cart.get("payment_in_progress")]
    response = await rig.call("POST", f"/api/admin/trips/{trip_id}/release", json={"reason": "stuck"})
    assert (response.status, (await response.json())["error"]) == (409, why)
    assert await rig.sql("SELECT * FROM agent_commands") == []
    assert await rig.sql("SELECT * FROM audit_log") == []


async def test_an_admin_recycles_a_pod(rig: Rig) -> None:
    await make_admin(rig)
    trip_id = await rig.add(BOB)
    path = f"/api/admin/trips/{trip_id}/recycle"
    assert (await rig.call("POST", path, json={"reason": "wedged"})).status == 404  # no browser
    pod = await held(rig, trip_id)
    response = await rig.call("POST", path, json={"reason": "wedged"})
    assert await response.json() == {"ok": True}
    rows = await rig.sql("SELECT op, cart_id, requested_by FROM agent_commands")
    assert [tuple(r) for r in rows] == [("recycle", None, f"admin:{ANN}")]
    audit = await rig.sql("SELECT action, after FROM audit_log")
    assert [(a["action"], json.loads(a["after"])) for a in audit] == [("recycle_pod", {"pod": pod})]


async def test_only_waitlist_control_opens_the_breaker(rig: Rig) -> None:
    await make_admin(rig)
    await rig.sql("INSERT INTO fleet_state (key, value) VALUES ('breaker', '\"open\"')")
    response = await rig.call("POST", "/api/admin/switches", json={"switch": "breaker", "on": True, "reason": "x"})
    assert (response.status, (await response.json())["error"]) == (400, "Only waitlist-control opens the breaker")
    body = {"switch": "breaker", "on": False, "reason": "BC Ferries is back"}
    assert (await rig.call("POST", "/api/admin/switches", json=body)).status == 200
    overview = await (await rig.call("GET", "/api/admin/overview")).json()
    assert overview["switches"]["breaker"] == "closed"
    audit = await rig.sql("SELECT action, target_id, after FROM audit_log")
    assert [(a["action"], a["target_id"], json.loads(a["after"])) for a in audit] == [
        ("set_switch", "breaker", {"breaker": "closed"})
    ]


async def test_signing_someone_out_everywhere(rig: Rig) -> None:
    await make_admin(rig)
    path = f"/api/admin/users/{BOB}/revoke-sessions"
    response = await rig.call("POST", path, json={"reason": "lost phone"})
    assert await response.json() == {"revoked": 1}
    assert (await rig.call("GET", "/api/state", BOB)).status == 401
    assert (
        await rig.call("POST", "/api/admin/users/cccccccccccccccccccc/revoke-sessions", json={"reason": "x"})
    ).status == 404
    audit = await rig.sql("SELECT action, target_id, after FROM audit_log")
    assert [(a["action"], a["target_id"], json.loads(a["after"])) for a in audit] == [
        ("revoke_sessions", BOB, {"revoked": 1})
    ]


async def test_making_and_unmaking_admins(rig: Rig) -> None:
    await make_admin(rig)
    path = f"/api/admin/users/{BOB}"
    response = await rig.call("PATCH", f"/api/admin/users/{ANN}", json={"is_admin": False, "reason": "x"})
    assert (response.status, (await response.json())["error"]) == (409, "You can't change your own admin")
    assert (await rig.call("PATCH", path, json={"is_admin": "yes", "reason": "x"})).status == 400
    response = await rig.call("PATCH", path, json={"is_admin": True, "reason": "helps run it"})
    assert await response.json() == {"is_admin": True}
    assert (await rig.call("GET", "/api/admin/users", BOB)).status == 200  # at once
    response = await rig.call("PATCH", path, json={"is_admin": False, "reason": "done helping"})
    assert await response.json() == {"is_admin": False}
    assert (await rig.call("GET", "/api/admin/users", BOB)).status == 403
    audit = await rig.sql("SELECT action, before, after FROM audit_log ORDER BY id")
    assert [(a["action"], json.loads(a["before"]), json.loads(a["after"])) for a in audit] == [
        ("set_admin", {"is_admin": False}, {"is_admin": True}),
        ("set_admin", {"is_admin": True}, {"is_admin": False}),
    ]
    # Bob also signs in as "boss", whom the config makes an admin: only the config can undo that.
    await rig.sql("INSERT INTO identities (iss, sub, uid, username) VALUES ($1, 'boss-sub', $2, 'boss')", ISSUER, BOB)
    assert (await rig.call("PATCH", path, json={"is_admin": True, "reason": "bootstrap"})).status == 200
    response = await rig.call("PATCH", path, json={"is_admin": False, "reason": "x"})
    assert (response.status, (await response.json())["error"]) == (409, "Bootstrap admins are set in the config")
    people = await (await rig.call("GET", "/api/admin/users")).json()
    assert {u["id"]: u["bootstrap"] for u in people["users"]} == {ANN: False, BOB: True}


async def test_deleting_someone(rig: Rig) -> None:
    await make_admin(rig)
    trip_id = await rig.add(BOB)
    await rig.sql(
        "INSERT INTO events (user_id, trip_id, kind, code) VALUES ($1, $2, 'agent', 'cart_added')", BOB, trip_id
    )
    await rig.sql("INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES ($1, 'e', 'p', 'a')", BOB)
    await rig.sql(
        "INSERT INTO hold_outcomes (user_id, trip_id, day, outcome) VALUES ($1, $2, now(), 'booked')", BOB, trip_id
    )
    await rig.sql("INSERT INTO usage_ledger (user_id, day, kind, amount) VALUES ($1, now(), 'holds', 1)", BOB)
    path = f"/api/admin/users/{BOB}"
    response = await rig.call("DELETE", f"/api/admin/users/{ANN}", json={"reason": "x"})
    assert (response.status, (await response.json())["error"]) == (409, "You can't delete yourself")
    response = await rig.call("DELETE", path, json={"reason": "asked to leave"})
    assert (response.status, (await response.json())["error"]) == (409, "Suspend them first, so their browsers stop")
    body = {"state": "suspended", "reason": "asked to leave"}
    assert (await rig.call("POST", f"{path}/state", json=body)).status == 200
    pod = await held(rig, trip_id)
    assert (await rig.call("DELETE", path, json={"reason": "asked to leave"})).status == 409  # still running
    await rig.sql("UPDATE browser_pods SET ended_at = now(), phase = 'gone' WHERE name = $1", pod)
    assert (await rig.call("DELETE", path, json={})).status == 400  # no reason
    response = await rig.call("DELETE", path, json={"reason": "asked to leave"})
    assert await response.json() == {"ok": True}
    for table in ("trips", "trip_state", "hold_claims", "events", "push_subscriptions"):
        assert await rig.sql(f"SELECT 1 FROM {table}") == [], table  # noqa: S608 - fixed names
    assert [r["id"] for r in await rig.sql("SELECT id FROM users")] == [ANN]
    assert len(await rig.sql("SELECT 1 FROM hold_outcomes UNION ALL SELECT 1 FROM usage_ledger")) == 2  # kept
    audit = await rig.sql("SELECT action, target_id, before FROM audit_log WHERE action = 'delete_user'")
    assert [(a["target_id"], json.loads(a["before"])) for a in audit] == [(BOB, {"username": BOB})]
    assert (await rig.call("DELETE", path, json={"reason": "again"})).status == 404


async def test_suspending_releases_a_held_cart_only_if_asked(rig: Rig) -> None:
    await make_admin(rig)
    trip_id = await rig.add(BOB)
    await held(rig, trip_id)
    path = f"/api/admin/users/{BOB}/state"
    bad = {"state": "suspended", "holds": "drop", "reason": "x"}
    assert (await rig.call("POST", path, json=bad)).status == 400
    assert (await rig.call("POST", path, json={"state": "suspended", "reason": "let it finish"})).status == 200
    assert await rig.sql("SELECT * FROM agent_commands") == []
    assert (await rig.call("POST", path, json={"state": "active", "reason": "oops"})).status == 200
    body = {"state": "suspended", "holds": "release", "reason": "abuse"}
    assert (await rig.call("POST", path, json=body)).status == 200
    rows = await rig.sql("SELECT trip_id, op, cart_id, requested_by FROM agent_commands")
    assert [tuple(r) for r in rows] == [(trip_id, "release", "abc123", f"admin:{ANN}")]
    audit = await rig.sql("SELECT after FROM audit_log WHERE action = 'set_state' ORDER BY id")
    assert [json.loads(a["after"]) for a in audit] == [
        {"state": "suspended", "holds": "finish"},
        {"state": "active"},
        {"state": "suspended", "holds": "release"},
    ]


async def test_suspending_with_release_leaves_a_paid_for_cart_and_recycles_one_being_added(rig: Rig) -> None:
    await make_admin(rig)
    trip_id = await rig.add(BOB)
    await held(rig, trip_id)
    path = f"/api/admin/users/{BOB}/state"
    body = {"state": "suspended", "holds": "release", "reason": "abuse"}
    for cart in [{"payment_in_progress": True}, {"status": "adding", "id": "def456", "payment_in_progress": False}]:
        await rig.sql("UPDATE trip_state SET cart = cart || $2::jsonb WHERE trip_id = $1", trip_id, json.dumps(cart))
        assert (await rig.call("POST", path, json=body)).status == 200
        assert (await rig.call("POST", path, json={"state": "active", "reason": "oops"})).status == 200
    rows = await rig.sql("SELECT trip_id, op, cart_id FROM agent_commands")
    assert [tuple(r) for r in rows] == [(trip_id, "recycle", None)]  # its drain empties the cart once it's in
    audit = await rig.sql("SELECT after FROM audit_log WHERE action = 'set_state' ORDER BY id")
    assert [json.loads(a["after"]) for a in audit] == [
        {"state": "suspended", "holds": "release", "left_paying": [trip_id]},
        {"state": "active"},
        {"state": "suspended", "holds": "release"},
        {"state": "active"},
    ]


async def test_the_people_list_shows_sessions_devices_usage_and_emails(rig: Rig) -> None:
    await make_admin(rig)
    await rig.sql("UPDATE users SET email = 'bob@example.com' WHERE id = $1", BOB)
    for sub, email in [("s1", "BOB@example.com"), ("s2", "bob@elsewhere.example"), ("s3", None)]:
        await rig.sql(
            "INSERT INTO identities (iss, sub, uid, username, email) VALUES ($1, $2, $3, 'bob', $4)",
            ISSUER,
            sub,
            BOB,
            email,
        )
    await rig.sql("INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES ($1, 'e', 'p', 'a')", BOB)
    today = datetime.now(PACIFIC).date()
    await rig.sql(
        "INSERT INTO usage_ledger (user_id, day, kind, amount) VALUES ($1, $2, 'holds', 2), ($1, $3, 'mints', 9)",
        BOB,
        today,
        today - timedelta(days=1),  # yesterday's isn't today's
    )
    await rig.call("POST", "/auth/logout", BOB)  # one session ended, one expired, two live
    for ends in ("now() - interval '1 minute'", "now() + interval '1 day'", "now() + interval '1 day'"):
        await rig.sql(
            "INSERT INTO sessions (token_hash, user_id, iss, sub, idle_expires_at, expires_at)"  # noqa: S608
            f" VALUES ($1, $2, $3, $2, {ends}, {ends})",
            secrets.token_bytes(32),
            BOB,
            ISSUER,
        )
    people = {u["id"]: u for u in (await (await rig.call("GET", "/api/admin/users")).json())["users"]}
    bob = people[BOB]
    assert (bob["sessions"], bob["devices"], bob["usage_today"]) == (2, 1, {"holds": 2})
    assert bob["emails_seen"] == ["bob@elsewhere.example"]
    ann = people[ANN]
    assert (ann["sessions"], ann["devices"], ann["usage_today"], ann["bootstrap"], ann["emails_seen"]) == (
        1,
        0,
        {},
        False,
        [],
    )


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
    assert trips(await next_state(ann)) == []
    assert trips(await next_state(bob)) == []
    await rig.add()
    assert len(trips(await next_state(ann))) == 1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bob.receive(), 0.5)  # nothing of Ann's reaches Bob
    await ann.close()
    await bob.close()


async def test_the_live_socket_sends_the_same_state_as_the_api(rig: Rig) -> None:
    """The push key, the links and each trip's booking link too: without them the dashboard lost its "turn on
    alerts" button and its bcferries.com links at the first live update."""
    await rig.add()
    ann = await live(rig)
    pushed = await next_state(ann)
    fetched = await (await rig.call("GET", "/api/state")).json()
    assert pushed["push"] == fetched["push"] == {"key": "BPushKey"}
    assert pushed["links"] == fetched["links"]
    assert set(trips(pushed)[0]) == set(trips(fetched)[0]) >= {"booking_url", "hold"}
    await ann.close()


async def test_the_live_socket_needs_a_token_first(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = await rig.client.ws_connect("/api/ws")
    await ws.send_json({"type": "auth", "token": "nope"})
    await ws.receive()
    assert ws.close_code == 4401
    monkeypatch.setattr("waitlist.web.live.AUTH_DEADLINE_SECONDS", 0.1)
    ws = await rig.client.ws_connect("/api/ws")
    await ws.receive()  # said nothing: closed
    assert ws.close_code == 4401


async def test_a_revoked_session_closes_its_socket(rig: Rig) -> None:
    ws = await live(rig)
    await next_state(ws)
    await rig.call("POST", "/auth/logout")
    await rig.add(BOB)  # any change wakes the hub... but only Bob's sockets; so nudge Ann's
    await rig.sql("SELECT pg_notify('wl_user', $1)", ANN)
    frame = await asyncio.wait_for(ws.receive(), 5)
    assert frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    assert ws.close_code == 4401


# ---- alerts


async def test_devices_subscribe_only_to_real_push_services(rig: Rig) -> None:
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}
    assert (await rig.call("POST", "/api/me/push-subscriptions", json=good)).status == 200
    evil = good | {"endpoint": "https://169.254.169.254/latest"}
    assert (await rig.call("POST", "/api/me/push-subscriptions", json=evil)).status == 400
    state = await (await rig.call("GET", "/api/state")).json()
    assert state["devices"] == 1
    # The same device, signed in as Bob now, moves to Bob.
    assert (await rig.call("POST", "/api/me/push-subscriptions", BOB, json=good)).status == 200
    assert (await (await rig.call("GET", "/api/state")).json())["devices"] == 0
    response = await rig.call("DELETE", "/api/me/push-subscriptions", BOB, json={"endpoint": good["endpoint"]})
    assert response.status == 200
    assert await rig.sql("SELECT * FROM push_subscriptions") == []


async def test_test_alerts_are_limited(rig: Rig) -> None:
    for _ in range(6):
        assert (await rig.call("POST", "/api/alerts/test")).status == 200
    assert (await rig.call("POST", "/api/alerts/test")).status == 429
    assert [r["kind"] for r in await rig.sql("SELECT DISTINCT kind FROM notifications")] == ["test"]


async def test_an_invitation_with_an_email_is_sent(rig: Rig) -> None:
    await make_admin(rig)
    body = {"username": "cat", "email": "cat@example.com", "reason": "friend"}
    assert (await rig.call("POST", "/api/admin/invites", json=body)).status == 201
    rows = await rig.sql("SELECT kind, to_email, user_id FROM notifications")
    assert [tuple(r) for r in rows] == [("invited", "cat@example.com", None)]
