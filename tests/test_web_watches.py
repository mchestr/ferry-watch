"""The generic watch API on real Postgres (docs/waitlist/design.md §6.1-6.2): POST/PATCH/DELETE /api/watches, a manual
check, "I booked it" and the catalog, for BC Ferries and an HTTP-checked provider (FakeHttpProvider, registered as
Buntzen Lake, until the real one lands); and BC Ferries' own routes, which another provider's watch never reaches."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import aiohttp
import asyncpg
import msgspec
import pytest
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.fakes import FAKE_PAGES, FakeHttpProvider, FakeSpec, fake_registry
from tests.golden_capture import _Known as Known
from tests.golden_capture import terminals as golden_terminals
from tests.postgres import ROLES, Database
from waitlist.bcferries.terminals import TerminalDirectory
from waitlist.control.limits import Limits
from waitlist.models import PACIFIC, Trip
from waitlist.polling import PollPolicy
from waitlist.providers import REGISTRY, Registry
from waitlist.providers.base import Option, search_hash
from waitlist.providers.buntzen import PROVIDER as BUNTZEN
from waitlist.web.accounts import Accounts, IdentityPolicy
from waitlist.web.api import CheckoutSigner
from waitlist.web.app import build
from waitlist.web.identity import OidcClient, OidcSettings, Principal
from waitlist.web.repo import UNREADABLE, WebStore, remark
from waitlist.web.watches import OFFERS, REGISTRY_KEY

ANN, BOB = "a" * 20, "b" * 20
DAY = date(2027, 7, 1)
NOW = datetime.combine(DAY - timedelta(days=1), time(12, 0), tzinfo=PACIFIC).timestamp()  # noon PT, Wed Jun 30
POLICY = PollPolicy(horizon=timedelta(days=45))  # a deployment's polling.horizonDays: 45


@dataclass
class Rig:
    client: TestClient
    db: Database
    tokens: dict[str, str]
    clock: list[float]
    app: Any

    async def call(self, method: str, path: str, user: str = ANN, **kw: Any) -> aiohttp.ClientResponse:
        return await self.client.request(method, path, headers={"Authorization": f"Bearer {self.tokens[user]}"}, **kw)

    async def ok(self, method: str, path: str, user: str = ANN, **kw: Any) -> dict[str, Any]:
        response = await self.call(method, path, user, **kw)
        assert response.status == 200, await response.text()
        body: dict[str, Any] = await response.json()
        return body

    async def error(self, method: str, path: str, user: str = ANN, **kw: Any) -> tuple[int, dict[str, Any]]:
        response = await self.call(method, path, user, **kw)
        return response.status, await response.json()

    async def watch(self, day: date = DAY, user: str = ANN, **body: Any) -> str:
        created = await self.ok(
            "POST", "/api/watches", user, json={"provider": "buntzen", "spec": {"date": day.isoformat()}} | body
        )
        return str(created["watch_id"])

    async def trip(self, user: str = ANN, **spec: Any) -> str:
        body = {
            "provider": "bcferries",
            "spec": {"from": "SWB", "to": "TSA", "date": (DAY + timedelta(days=2)).isoformat()} | spec,
        }
        return str((await self.ok("POST", "/api/watches", user, json=body))["watch_id"])

    async def sql(self, query: str, *args: Any, user: str = "waitlist") -> list[asyncpg.Record]:
        conn = await self.db.connect(user)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    async def report(self, at: float | None = None, **entry: Any) -> None:
        """waitlist-control's check report: checks running, unless the entry says otherwise."""
        value = {"at": at if at is not None else self.clock[0], "providers": {"buntzen": {"fetcher": "ok"} | entry}}
        await self.sql(
            "INSERT INTO fleet_state (key, value) VALUES ('check_report', $1)"
            " ON CONFLICT (key) DO UPDATE SET value = $1",
            json.dumps(value),
            user=ROLES.control,
        )

    async def state(self, user: str = ANN) -> dict[str, Any]:
        return await self.ok("GET", "/api/state", user)


async def make_rig(
    db: Database,
    aiohttp_client: Callable[..., Any],
    *,
    offer: str = "everyone",
    registry: Registry | None = None,
    terminals: bool = False,
    policy: PollPolicy = POLICY,
) -> AsyncIterator[Rig]:
    """`terminals`: the terminal list is known (from the saved route info), so BC Ferries trips get booking links."""
    owner = await db.connect()
    try:
        for uid in (ANN, BOB):
            await owner.execute("INSERT INTO users (id, username) VALUES ($1, $1)", uid)
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(db.dsn(ROLES.web), min_size=1, max_size=4)
    accounts = Accounts(pool, IdentityPolicy())
    tokens = {uid: await accounts.create_session(uid, Principal("https://idp", uid), None) for uid in (ANN, BOB)}
    clock = [NOW]
    store = WebStore(
        pool,
        Limits(),
        policy,
        clock=lambda: clock[0],
        registry=registry or fake_registry(),
        offers={"buntzen": offer},  # type: ignore[dict-item]
    )
    async with aiohttp.ClientSession() as http:
        oidc = OidcClient(http, OidcSettings(issuer="https://idp", client_id="c", client_secret="s", redirect_uri="x"))
        app = build(
            accounts,
            oidc,
            store,
            CheckoutSigner(Ed25519PrivateKey.generate(), "k1", "https://waitlist-checkout.chestr.dev"),
            pool=pool,
            limits=Limits(),
            public={"links": {"kofi": None}, "push": {"key": "BPushKey"}},
            origin="https://waitlist.chestr.dev",
            terminals=cast(TerminalDirectory, Known(golden_terminals())) if terminals else None,
        )
        client: TestClient = await aiohttp_client(app)
        try:
            yield Rig(client, db, tokens, clock, app)
        finally:
            await client.close()
            await pool.close()


@pytest.fixture
async def rig(db: Database, aiohttp_client: Callable[..., Any], request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    """waitlist-web for Ann and Bob, with Buntzen offered to everyone unless a test passes another offer."""
    async for r in make_rig(db, aiohttp_client, offer=getattr(request, "param", "everyone")):
        yield r


def watches(state: dict[str, Any], provider: str | None = None) -> list[dict[str, Any]]:
    return [w for w in state["watches"] if provider is None or w["provider"] == provider]


# ---- creating


async def test_a_new_watch_is_due_at_once_and_comes_back_with_the_state(rig: Rig) -> None:
    body = {"provider": "buntzen", "spec": {"date": DAY.isoformat()}}
    created = await rig.ok("POST", "/api/watches", json=body)
    watch_id = created["watch_id"]
    assert len(watch_id) == 16
    (watch,) = watches(created["state"])
    assert (watch["provider"], watch["id"], watch["rev"], watch["enabled"]) == ("buntzen", watch_id, 1, True)
    assert watch["spec"] == {"date": DAY.isoformat()}
    assert watch["check"]["state"] == "unavailable"  # no check report yet: waitlist-control's loop isn't running
    (row,) = await rig.sql(
        "SELECT t.provider, t.spec, rt.next_check_at FROM trips t JOIN trip_runtime rt ON rt.trip_id = t.id"
    )
    assert (row["provider"], json.loads(row["spec"]), row["next_check_at"]) == ("buntzen", {"date": "2027-07-01"}, None)


async def test_a_new_watch_may_start_paused(rig: Rig) -> None:
    created = await rig.ok(
        "POST", "/api/watches", json={"provider": "buntzen", "spec": {"date": "2027-07-01"}, "enabled": False}
    )
    assert watches(created["state"])[0]["enabled"] is False
    assert watches(created["state"])[0]["check"]["state"] == "paused"


@pytest.mark.parametrize(
    ("body", "status", "field", "message"),
    [
        ({"provider": "buntzen", "spec": {}, "colour": "blue"}, 400, "colour", "Unknown field"),
        ({"spec": {"date": "2027-07-01"}}, 400, "provider", "Not a provider you can watch"),
        ({"provider": 7, "spec": {}}, 400, "provider", "Not a provider you can watch"),
        ({"provider": "bcparks", "spec": {}}, 400, "provider", "Not a provider you can watch"),
        ({"provider": "constructor", "spec": {}}, 400, "provider", "Not a provider you can watch"),
        ({"provider": "buntzen"}, 400, "spec", "Expected a JSON object"),
        ({"provider": "buntzen", "spec": ["2027-07-01"]}, 400, "spec", "Expected a JSON object"),
        (
            {"provider": "buntzen", "spec": {"date": "2027-07-01"}, "enabled": "yes"},
            400,
            "enabled",
            "Must be true or false",
        ),
        (
            {"provider": "buntzen", "spec": {"date": "2027-06-29"}},
            400,
            "date",
            "Pick today or a later date",
        ),  # the provider's
        ({"provider": "buntzen", "spec": {"date": "2027-07-01", "id": "x"}}, 400, "id", "Unknown field"),
        (
            {"provider": "bcferries", "spec": {"from": "SWB", "to": "TSA", "date": "2027-07-03", "id": "1" * 16}},
            400,
            "id",
            "Unknown field",
        ),
        (
            {"provider": "bcferries", "spec": {"from": "SWB", "to": "TSA", "date": "2027-07-03", "enabled": True}},
            400,
            "enabled",
            "Unknown field",
        ),
        (
            {"provider": "bcferries", "spec": {"from": "SWB", "to": "SWB", "date": "2027-07-03"}},
            400,
            "to",
            "Pick a different destination",
        ),
        (
            {"provider": "bcferries", "spec": {"from": "SWB", "to": "TSA", "date": "2027-09-01"}},
            400,
            "date",
            "Pick a date from today to 45 days ahead",
        ),
    ],
)
async def test_what_a_new_watch_refuses(rig: Rig, body: dict[str, Any], status: int, field: str, message: str) -> None:
    assert await rig.error("POST", "/api/watches", json=body) == (status, {"error": message, "field": field})
    assert await rig.sql("SELECT 1 FROM trips") == []


@pytest.mark.parametrize("rig", ["admins"], indirect=True)
async def test_an_admins_only_provider_refuses_everyone_else(rig: Rig) -> None:
    body = {"provider": "buntzen", "spec": {"date": "2027-07-01"}}
    assert await rig.error("POST", "/api/watches", json=body) == (
        403,
        {"error": "Buntzen Lake isn't available to you", "field": "provider"},
    )
    await rig.sql("UPDATE users SET is_admin = true WHERE id = $1", ANN)
    assert (await rig.call("POST", "/api/watches", json=body)).status == 200


@pytest.mark.parametrize("rig", ["off"], indirect=True)
async def test_a_provider_thats_off_refuses_even_admins_but_bc_ferries_is_always_offered(rig: Rig) -> None:
    await rig.sql("UPDATE users SET is_admin = true WHERE id = $1", ANN)
    status, _ = await rig.error("POST", "/api/watches", json={"provider": "buntzen", "spec": {"date": "2027-07-01"}})
    assert status == 403
    await rig.trip()
    catalog = (await rig.ok("GET", "/api/providers"))["providers"]
    assert [(p["id"], p["offered"]) for p in catalog] == [("bcferries", True), ("buntzen", False)]


async def test_a_bc_ferries_watch_is_0_7_1s_trip(rig: Rig) -> None:
    created = await rig.ok(
        "POST",
        "/api/watches",
        json={
            "provider": "bcferries",
            "spec": {"from": "SWB", "to": "TSA", "date": "2027-07-03", "adults": 2},
            "enabled": False,
        },
    )
    (watch,) = watches(created["state"], "bcferries")
    assert watch["id"] == created["watch_id"] == watch["trip"]["id"]
    assert (watch["enabled"], watch["trip"]["enabled"], watch["trip"]["adults"]) == (False, False, 2)
    assert "spec" not in watch  # `trip` is the one source
    (row,) = await rig.sql("SELECT provider FROM trips")
    assert row["provider"] == "bcferries"


async def test_alert_only_watches_are_limited_together_and_apart_from_trips(rig: Rig) -> None:
    for _ in range(3):
        await rig.trip()
    status, body = await rig.error(
        "POST",
        "/api/watches",
        json={"provider": "bcferries", "spec": {"from": "SWB", "to": "TSA", "date": "2027-07-03"}},
    )
    assert (status, body["error"], body["limit"]) == (409, "You can watch up to 3 trips", 3)
    for i in range(6):  # BC Ferries trips don't count against these
        await rig.watch(DAY + timedelta(days=i % 2))
    status, body = await rig.error("POST", "/api/watches", json={"provider": "buntzen", "spec": {"date": "2027-07-01"}})
    assert (status, body) == (
        409,
        {"error": "You can have up to 6 alert-only watches", "field": "provider", "limit": 6, "usage": 6},
    )
    await rig.watch(user=BOB)  # Bob has his own
    await rig.sql(
        "INSERT INTO user_limits (user_id, key, value) VALUES ($1, 'maxAlertOnlyWatches', 7)", ANN, user=ROLES.web
    )
    await rig.watch()
    capacity = (await rig.state())["capacity"]
    assert capacity == {
        "can_add": False,
        "reason": "You're watching as many as you can",
        "providers": {
            "bcferries": {"can_add": False, "reason": "You can watch up to 3 trips", "limit": 3, "count": 3},
            "buntzen": {"can_add": False, "reason": "You can have up to 7 alert-only watches", "limit": 7, "count": 7},
        },
    }


# ---- editing


async def test_editing_an_http_watch_never_reaches_bc_ferries_code(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_id = await rig.watch()

    def refuse(*_: Any, **__: Any) -> Trip:
        raise AssertionError("Trip.from_json was given another provider's watch")

    monkeypatch.setattr(Trip, "from_json", refuse)
    await rig.ok("PATCH", f"/api/watches/{watch_id}", json={"enabled": False})
    await rig.ok("DELETE", f"/api/watches/{watch_id}")


async def test_editing_the_date_forgets_what_was_alerted_and_hides_the_old_result(rig: Rig) -> None:
    watch_id = await rig.watch()
    await rig.report()
    options = [
        {
            "key": "a",
            "label": "Part a",
            "status": "open",
            "wanted": True,
            "booking_url": FAKE_PAGES["a"],
            "detail": None,
        },
        {
            "key": "b",
            "label": "Part b",
            "status": "sold_out",
            "wanted": True,
            "booking_url": FAKE_PAGES["b"],
            "detail": None,
        },
    ]
    await rig.sql(
        "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at)"
        " VALUES ($1, $2, 'ok', $3, to_timestamp($4), to_timestamp($4))",
        watch_id,
        search_hash((DAY,)),
        json.dumps(options),
        NOW - 30,
        user=ROLES.control,
    )
    await rig.sql(
        "INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{a}')", watch_id, user=ROLES.control
    )
    await rig.sql("UPDATE trip_runtime SET next_check_at = to_timestamp($2) WHERE trip_id = $1", watch_id, NOW + 50)
    (watch,) = watches(await rig.state())
    assert [(o["key"], o["status"]) for o in watch["options"]] == [("a", "open"), ("b", "sold_out")]
    assert watch["booking_url"] == FAKE_PAGES["a"]
    assert (watch["check"]["outcome"], watch["check"]["last_check_at"]) == ("ok", NOW - 30)

    state = await rig.ok("PATCH", f"/api/watches/{watch_id}", json={"spec": {"date": "2027-07-02"}})
    (watch,) = watches(state)
    assert watch["rev"] == 2
    assert watch["spec"] == {"date": "2027-07-02"}
    assert [(o["key"], o["status"]) for o in watch["options"]] == [
        ("a", "not_released"),  # placeholders: Jul 2's passes go on sale tomorrow
        ("b", "not_released"),
    ]  # placeholders
    assert watch["check"]["outcome"] == "pending"
    assert await rig.sql("SELECT * FROM alert_memory") == []
    (runtime,) = await rig.sql("SELECT next_check_at FROM trip_runtime")
    assert runtime["next_check_at"] is None  # checked again within a tick


async def test_turning_a_watch_back_on_makes_it_due(rig: Rig) -> None:
    watch_id = await rig.watch(enabled=False)
    await rig.sql("UPDATE trip_runtime SET next_check_at = to_timestamp($2) WHERE trip_id = $1", watch_id, NOW + 600)
    await rig.sql(
        "INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{a}')", watch_id, user=ROLES.control
    )
    state = await rig.ok("PATCH", f"/api/watches/{watch_id}", json={"enabled": True})
    assert watches(state)[0]["enabled"] is True
    (runtime,) = await rig.sql("SELECT next_check_at FROM trip_runtime")
    assert runtime["next_check_at"] is None
    assert len(await rig.sql("SELECT * FROM alert_memory")) == 1  # the same search remembers


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"provider": "bcferries"}, "provider", "A watch's provider can't change"),
        ({"spec": {}, "provider": "buntzen"}, "provider", "A watch's provider can't change"),
        ({"rev": 3}, "rev", "Unknown field"),
        ({}, "spec", "Nothing to change"),
        ({"spec": "2027-07-02"}, "spec", "Expected a JSON object"),
        ({"enabled": 1}, "enabled", "Must be true or false"),
        ({"spec": {"date": "2027-06-01"}}, "date", "Pick today or a later date"),
        ({"spec": {"colour": "blue"}}, "colour", "Unknown field"),
    ],
)
async def test_what_an_edit_refuses(rig: Rig, body: dict[str, Any], field: str, message: str) -> None:
    watch_id = await rig.watch()
    assert await rig.error("PATCH", f"/api/watches/{watch_id}", json=body) == (400, {"error": message, "field": field})
    (row,) = await rig.sql("SELECT rev, provider FROM trips")
    assert (row["rev"], row["provider"]) == (1, "buntzen")


async def test_editing_a_bc_ferries_watch_is_0_7_1s_edit(rig: Rig) -> None:
    trip_id = await rig.trip()
    state = await rig.ok("PATCH", f"/api/watches/{trip_id}", json={"spec": {"adults": 2}, "enabled": False})
    (watch,) = watches(state, "bcferries")
    assert (watch["trip"]["adults"], watch["enabled"], watch["rev"]) == (2, False, 2)
    for key in ("id", "enabled"):
        assert (await rig.call("PATCH", f"/api/watches/{trip_id}", json={"spec": {key: True}})).status == 400


async def test_another_users_watch_looks_missing(rig: Rig) -> None:
    watch_id = await rig.watch(user=BOB)
    for method, path, body in [
        ("PATCH", f"/api/watches/{watch_id}", {"enabled": False}),
        ("DELETE", f"/api/watches/{watch_id}", None),
        ("POST", f"/api/watches/{watch_id}/check", {}),
        ("POST", f"/api/watches/{watch_id}/done", {}),
    ]:
        assert await rig.error(method, path, json=body) == (404, {"error": "No such watch"}), path
    assert (await rig.call("PATCH", "/api/watches/not-a-watch", json={"enabled": False})).status == 404


# ---- removing, and "I booked it"


async def test_removing_any_providers_watch(rig: Rig) -> None:
    watch_id, trip_id = await rig.watch(), await rig.trip()
    for removed in (watch_id, trip_id):
        state = await rig.ok("DELETE", f"/api/watches/{removed}")
        assert removed not in [w["id"] for w in watches(state)]
        assert await rig.error("DELETE", f"/api/watches/{removed}") == (404, {"error": "No such watch"})
    rows = await rig.sql("SELECT archived_at IS NOT NULL AS archived, enabled, rev FROM trips ORDER BY provider")
    assert [tuple(r) for r in rows] == [(True, False, 2), (True, False, 2)]


async def test_i_booked_it_archives_an_alert_only_watch(rig: Rig) -> None:
    watch_id = await rig.watch()
    state = await rig.ok("POST", f"/api/watches/{watch_id}/done", json={})
    assert watches(state) == []
    (event,) = await rig.sql("SELECT user_id, trip_id, kind, code, params FROM events")
    assert tuple(event) == (ANN, watch_id, "watch", "booked", "{}")
    trip_id = await rig.trip()
    assert await rig.error("POST", f"/api/watches/{trip_id}/done", json={}) == (
        409,
        {"error": "Not for this kind of watch"},
    )


# ---- a manual check


async def test_a_manual_check_of_an_http_watch(rig: Rig) -> None:
    watch_id = await rig.watch()
    await rig.report()
    await rig.ok("POST", f"/api/watches/{watch_id}/check", json={})
    (runtime,) = await rig.sql("SELECT next_check_at, quiet_lifted_until FROM trip_runtime")
    assert runtime["next_check_at"].timestamp() == NOW
    assert runtime["quiet_lifted_until"].timestamp() == NOW + 600
    (command,) = await rig.sql("SELECT trip_id, op, requested_by, consumed_at FROM agent_commands")
    assert (command["trip_id"], command["op"], command["requested_by"]) == (watch_id, "check_now", ANN)
    assert command["consumed_at"].timestamp() == NOW  # no warm pod: recorded for the limit only


async def test_manual_checks_are_one_limit_across_providers(rig: Rig) -> None:
    watch_id, trip_id = await rig.watch(), await rig.trip()
    await rig.report()
    for i in range(6):
        await rig.ok("POST", f"/api/watches/{watch_id if i % 2 else trip_id}/check", json={})
    # Made at waitlist-web's "now" (the database stamps its own clock, which this test's isn't).
    await rig.sql("UPDATE agent_commands SET created_at = to_timestamp($1)", NOW - 60)
    status, body = await rig.error("POST", f"/api/watches/{watch_id}/check", json={})
    assert (status, body["error"]) == (429, "Up to 6 checks an hour")


QUIET_AT_NOON = {"quiet_hours": {"start": "11:00", "end": "13:00"}}


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"at": NOW - 6 * 3600}, "Checks start Wed Jun 30 at 7:00 am"),  # 6 am, before the release
        ({"at": datetime.combine(DAY, time(18, 0), tzinfo=PACIFIC).timestamp()}, "It's too late for this watch"),
        ({"paused_providers": ["buntzen"]}, "Checks for Buntzen Lake are paused right now"),
        ({"maintenance": True}, "Checks for Buntzen Lake are paused right now"),
        ({"report": None}, "Checks for Buntzen Lake aren't running right now"),
        ({"report": {"at": NOW - 121}}, "Checks for Buntzen Lake aren't running right now"),  # stale
        ({"report": {"fetcher": "down"}}, "Checks for Buntzen Lake aren't running right now"),
        ({"enabled": False}, "This watch is paused"),
        ({"backoff_until": NOW + 15 * 60}, "Checks resume at 12:15 pm"),
        ({"report": {"cooldown_until": NOW + 30 * 60}}, "Checks resume at 12:30 pm"),
        ({"quiet": True, "report": {"cooldown_until": NOW + 30 * 60}}, "Checks resume at 12:30 pm"),
    ],
)
async def test_a_check_that_couldnt_happen_is_refused_and_costs_nothing(
    rig: Rig, setup: dict[str, Any], message: str
) -> None:
    watch_id = await rig.watch()
    report = setup.get("report", {})
    if report is not None:
        await rig.report(**report)
    if "at" in setup:
        rig.clock[0] = setup["at"]
        await rig.report(**report)
    if "paused_providers" in setup:
        await rig.sql(
            "UPDATE fleet_state SET value = $1 WHERE key = 'paused_providers'", json.dumps(setup["paused_providers"])
        )
    if setup.get("maintenance"):
        await rig.sql("UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'")
    if setup.get("enabled") is False:
        await rig.ok("PATCH", f"/api/watches/{watch_id}", json={"enabled": False})
    if "backoff_until" in setup:
        await rig.sql(
            "UPDATE trip_runtime SET backoff_until = to_timestamp($1)", setup["backoff_until"], user=ROLES.control
        )
    if setup.get("quiet"):
        await rig.ok("PATCH", "/api/me", json=QUIET_AT_NOON)
    assert await rig.error("POST", f"/api/watches/{watch_id}/check", json={}) == (409, {"error": message})
    assert await rig.sql("SELECT * FROM agent_commands") == []  # no manualChecksPerHour unit spent


async def test_a_check_in_quiet_hours_lifts_them(rig: Rig) -> None:
    watch_id = await rig.watch()
    await rig.report()
    await rig.ok("PATCH", "/api/me", json=QUIET_AT_NOON)
    (watch,) = watches(await rig.state())
    assert (watch["check"]["state"], watch["quiet"]) == ("quiet", True)
    await rig.ok("POST", f"/api/watches/{watch_id}/check", json={})
    (watch,) = watches(await rig.state())
    assert (watch["check"]["state"], watch["quiet"]) == ("pending", False)


async def test_a_bc_ferries_check_is_0_7_1s(rig: Rig) -> None:
    trip_id = await rig.trip()
    await rig.ok("POST", f"/api/watches/{trip_id}/check", json={})
    (runtime,) = await rig.sql("SELECT next_check_at FROM trip_runtime")
    assert runtime["next_check_at"].timestamp() == NOW


# ---- BC Ferries' own routes, and another provider's watch


async def test_bc_ferries_routes_never_reach_another_providers_watch(rig: Rig) -> None:
    watch_id = await rig.watch()
    trip = f"/api/trips/{watch_id}"
    for method, path, body in [
        ("POST", f"{trip}/hold", {}),
        ("POST", f"{trip}/vnc", {}),
        ("POST", f"{trip}/cart/release", {"cart_id": "c1"}),
        ("POST", f"{trip}/cart/dismiss", {"cart_id": "c1"}),
        ("POST", f"{trip}/check", {}),
        ("PATCH", trip, {"adults": 2}),
        ("DELETE", trip, None),
    ]:
        assert await rig.error(method, path, json=body) == (404, {"error": "No such trip"}), (method, path)
    assert await rig.error("POST", f"{trip}/booked", json={"booked": True}) == (
        409,
        {"error": "There's no checkout to answer for"},
    )
    assert (await rig.call("POST", f"{trip}/cart/release", json={})).status == 400  # "Which cart?" comes first
    (row,) = await rig.sql("SELECT rev, archived_at, enabled FROM trips")
    assert (row["rev"], row["archived_at"], row["enabled"]) == (1, None, True)
    assert await rig.sql("SELECT * FROM agent_commands") == []


async def test_the_trip_aliases_still_work_for_bc_ferries(rig: Rig) -> None:
    body = {"from": "SWB", "to": "TSA", "date": "2027-07-03"}
    state = await rig.ok("POST", "/api/trips", json=body)
    (watch,) = watches(state, "bcferries")
    trip_id = watch["id"]
    assert "trips" not in state
    await rig.ok("PATCH", f"/api/trips/{trip_id}", json={"adults": 3})
    await rig.ok("POST", f"/api/trips/{trip_id}/check")
    assert watches(await rig.ok("DELETE", f"/api/trips/{trip_id}")) == []


# ---- the catalog


async def test_the_catalog(rig: Rig) -> None:
    body = await rig.ok("GET", "/api/providers")
    (bcferries, buntzen) = body["providers"]
    assert bcferries == REGISTRY.metas[0].to_json() | {"horizon_days": 45, "offered": True, "paused": False}
    assert buntzen == REGISTRY.metas[1].to_json() | {"offered": True, "paused": False}
    await rig.sql("UPDATE fleet_state SET value = '[\"buntzen\"]' WHERE key = 'paused_providers'")
    providers = (await rig.state())["providers"]
    assert [(p["id"], p["paused"]) for p in providers] == [("bcferries", False), ("buntzen", True)]
    assert providers == (await rig.ok("GET", "/api/providers"))["providers"]
    await rig.sql("UPDATE fleet_state SET value = '[]' WHERE key = 'paused_providers'")
    await rig.sql("UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'")
    assert [p["paused"] for p in (await rig.ok("GET", "/api/providers"))["providers"]] == [False, True]


async def test_a_paused_provider_still_takes_new_watches(rig: Rig) -> None:
    await rig.sql("UPDATE fleet_state SET value = '[\"buntzen\"]' WHERE key = 'paused_providers'")
    watch_id = await rig.watch()
    state = await rig.state()
    assert state["capacity"]["providers"]["buntzen"]["can_add"] is True
    assert watches(state)[0]["id"] == watch_id
    assert watches(state)[0]["check"]["state"] == "provider_paused"


async def test_the_app_keys_hold_the_stores_registry_and_offers(rig: Rig) -> None:
    assert set(rig.app[REGISTRY_KEY].http) == {"buntzen"}
    assert rig.app[OFFERS] == {"buntzen": "everyone"}


def test_stored_options_are_re_marked_against_the_spec() -> None:
    def option(key: str, status: str = "unchecked", *, wanted: bool = True) -> Option:
        return Option(key=key, label=key, status=status, wanted=wanted)  # type: ignore[arg-type]

    placeholders = [option("full_day", wanted=False), option("morning"), option("afternoon")]
    stored = [option("full_day", "open"), option("morning", "sold_out", wanted=False), option("evening", "open")]
    assert remark(stored, placeholders) == [
        option("full_day", "open", wanted=False),  # no longer watched: never a Book link
        option("morning", "sold_out"),  # newly wanted: its last-seen status until the next check
        option("afternoon"),  # never stored: the placeholder
    ]
    assert remark(placeholders, placeholders) == placeholders  # an unchanged spec: the identity


# ---- a stored spec this build can't load


class _ReadsNothing(FakeHttpProvider):
    """A provider's bug, not a SpecError: it fails on every stored spec."""

    def load_spec(self, stored: Mapping[str, Any]) -> FakeSpec:  # noqa: ARG002 - whatever it's given
        raise KeyError("date")


@pytest.mark.parametrize("failure", ["spec_error", "provider_bug"])
async def test_a_stored_spec_that_doesnt_load_answers_like_a_degraded_watch(
    db: Database, aiohttp_client: Callable[..., Any], failure: str
) -> None:
    """Whatever the provider raises on a watch's stored spec, the state shows the watch degraded, and editing or
    checking it answers 409: never a 500, nor a SpecError's 400 naming a field the request didn't send. Nothing
    changes, no manual check is spent, and the watch can still be removed."""
    registry = fake_registry(_ReadsNothing()) if failure == "provider_bug" else fake_registry()
    async for rig in make_rig(db, aiohttp_client, registry=registry):
        watch_id = "b" * 16
        stored = {"date": DAY.isoformat(), "from_a_newer_build": True}  # the fake's load_spec refuses the extra key
        await rig.sql(
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', $3)",
            watch_id,
            ANN,
            json.dumps(stored),
        )
        await rig.sql("INSERT INTO trip_runtime (trip_id) VALUES ($1)", watch_id)
        await rig.report()
        (watch,) = watches(await rig.state())
        assert set(watch) == {"provider", "id", "rev", "enabled", "paused_reason", "quiet"}
        (before,) = await rig.sql("SELECT spec, enabled, rev FROM trips")
        refused = (409, {"error": UNREADABLE})
        path = f"/api/watches/{watch_id}"
        assert await rig.error("PATCH", path, json={"spec": {"date": (DAY + timedelta(days=1)).isoformat()}}) == refused
        assert await rig.error("PATCH", path, json={"enabled": False}) == refused
        assert await rig.error("POST", f"{path}/check", json={}) == refused
        assert await rig.sql("SELECT spec, enabled, rev FROM trips") == [before]
        assert await rig.sql("SELECT 1 FROM agent_commands") == []
        assert watches(await rig.ok("DELETE", path)) == []


# ---- with the real Buntzen provider (W2)


@pytest.fixture
async def buntzen(db: Database, aiohttp_client: Callable[..., Any]) -> AsyncIterator[Rig]:
    async for r in make_rig(db, aiohttp_client, registry=REGISTRY):
        yield r


async def test_buntzen_watches_end_to_end(buntzen: Rig) -> None:
    rig = buntzen
    spec = {"date": DAY.isoformat(), "passes": ["morning", "afternoon"], "release_reminder": True}
    created = await rig.ok("POST", "/api/watches", json={"provider": "buntzen", "spec": spec})
    watch_id = created["watch_id"]
    (watch,) = watches(created["state"])
    assert watch["spec"] == spec
    await rig.report()
    await rig.ok("POST", f"/api/watches/{watch_id}/check", json={})  # 200
    status, body = await rig.error("POST", "/api/watches", json={"provider": "buntzen", "spec": spec | {"passes": []}})
    assert (status, body["field"]) == (400, "passes")


async def test_changing_only_the_passes_keeps_alert_memory_and_re_marks_the_options(buntzen: Rig) -> None:
    rig = buntzen
    spec = {"date": DAY.isoformat(), "passes": ["morning"], "release_reminder": True}
    watch_id = (await rig.ok("POST", "/api/watches", json={"provider": "buntzen", "spec": spec}))["watch_id"]
    await rig.report()
    loaded = BUNTZEN.load_spec(spec)
    at = datetime.fromtimestamp(NOW, UTC)
    stored = [
        {**o, "status": "open"} if o["key"] in ("morning", "afternoon") else o
        for o in msgspec.to_builtins(list(BUNTZEN.evaluate(loaded, {}, at)))
    ]
    await rig.sql(
        "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at)"
        " VALUES ($1, $2, 'ok', $3, now(), now())",
        watch_id,
        search_hash(BUNTZEN.search_key(loaded)),
        json.dumps(stored),
        user=ROLES.control,
    )
    await rig.sql(
        "INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{morning}')",
        watch_id,
        user=ROLES.control,
    )
    state = await rig.ok("PATCH", f"/api/watches/{watch_id}", json={"spec": {"passes": ["afternoon"]}})
    (watch,) = watches(state)
    by_key = {o["key"]: o for o in watch["options"]}
    assert by_key["morning"]["wanted"] is False  # no longer watched: no Book link
    assert by_key["afternoon"]["wanted"] is True
    assert watch["booking_url"] is not None
    (runtime,) = await rig.sql("SELECT next_check_at FROM trip_runtime")
    assert runtime["next_check_at"] is None
    assert [r["alerted"] for r in await rig.sql("SELECT alerted FROM alert_memory")] == [["morning"]]
