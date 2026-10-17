"""The state waitlist-web sends has the shared fixture's shape (web/src/test/fixtures/api-state.json, docs/waitlist/
design.md §6.3): the keys and value types of every entry, the catalog and the capacity. The fixture is what the SPA's
zod contract is tested against, so a snapshot that drifts from it breaks the SPA."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.fakes import FakeHttpProvider, FakeSpec, fake_registry
from tests.postgres import ROLES, Database
from tests.test_web_watches import ANN, Rig, make_rig, watches
from waitlist.control.limits import Limits
from waitlist.polling import PollPolicy
from waitlist.providers import REGISTRY
from waitlist.providers.base import search_hash
from waitlist.providers.buntzen import PROVIDER as BUNTZEN
from waitlist.web.accounts import Accounts, IdentityPolicy
from waitlist.web.api import CheckoutSigner
from waitlist.web.app import build
from waitlist.web.identity import OidcClient, OidcSettings
from waitlist.web.repo import WebStore

FIXTURE = Path(__file__).parent.parent / "web" / "src" / "test" / "fixtures" / "api-state.json"
# The fixture's moment: 7:31:50 am PT on Wed Jun 30 2027, half an hour after Buntzen's release for Jul 1.
NOW = 1814365910.0


def fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def shape(value: Any) -> Any:
    """A value's JSON shape: every key, and each value's type (any number is a number)."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v) for v in value]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    return "null" if value is None else type(value).__name__


def entry(provider: str, index: int = 0) -> dict[str, Any]:
    found: dict[str, Any] = [w for w in fixture()["watches"] if w["provider"] == provider][index]
    return found


@pytest.fixture
async def rig(db: Database, aiohttp_client: Callable[..., Any]) -> AsyncIterator[Rig]:
    async for r in make_rig(db, aiohttp_client, terminals=True, policy=PollPolicy()):
        r.clock[0] = NOW
        await r.sql("UPDATE users SET is_admin = true WHERE id = $1", ANN)  # the fixture's user is an admin
        yield r


async def seen_trip(rig: Rig) -> str:
    """A BC Ferries trip like the fixture's: watched by a warm browser that saw one sold-out sailing."""
    spec = {"from": "TSA", "to": "SWB", "date": "2027-07-03", "adults": 2, "earliest": "07:00", "latest": "12:00"}
    created = await rig.ok("POST", "/api/watches", json={"provider": "bcferries", "spec": spec})
    trip_id = str(created["watch_id"])
    for _ in range(4):  # rev 5, as the fixture's
        await rig.ok("PATCH", f"/api/watches/{trip_id}", json={"spec": {}})
    await rig.sql(
        "UPDATE trip_runtime SET phase = 'watching', mode = 'warm', last_check_at = to_timestamp($2)"
        " WHERE trip_id = $1",
        trip_id,
        NOW - 45,
    )
    sailings = [
        {
            "depart": "09:00",
            "arrive": "10:35",
            "vessel": "Spirit of Vancouver Island",
            "status": "sold_out",
            "fares": [],
        }
    ]
    await rig.sql(
        "INSERT INTO trip_state (trip_id, agent_phase, checked_at, next_due, effective_interval, sailings,"
        " heartbeat_at) VALUES ($1, 'watching', to_timestamp($2), to_timestamp($3), 300, $4, to_timestamp($5))",
        trip_id,
        NOW - 45,
        NOW + 255,
        json.dumps(sailings),
        NOW - 5,
        user=ROLES.control,
    )
    return trip_id


async def test_the_state_has_the_fixtures_top_level_keys_catalog_and_capacity(rig: Rig) -> None:
    trip_id = await seen_trip(rig)
    await rig.sql(
        "INSERT INTO events (user_id, trip_id, kind, code, params)"
        " VALUES ($1, $2, 'check', 'opening_seen', '{\"option\": \"x\"}')",
        ANN,
        trip_id,
        user=ROLES.control,
    )
    state = await rig.state()
    expected = fixture()
    assert list(state) == list(expected)
    assert shape(state["providers"]) == shape(expected["providers"])
    assert shape(state["capacity"]) == shape(expected["capacity"])
    assert list(state["limits"]) == list(expected["limits"])
    assert isinstance(state["usage"], dict)  # counters, by kind, for today
    for key in ("me", "settings", "quiet_hours", "devices", "links", "push"):
        assert set(state[key] if isinstance(state[key], dict) else {}) >= set(
            expected[key] if isinstance(expected[key], dict) else {}
        ), key
    assert shape(state["events"]) == shape(expected["events"])


async def test_a_bc_ferries_entry_has_the_fixtures_shape(rig: Rig) -> None:
    trip_id = await seen_trip(rig)
    (watch,) = watches(await rig.state(), "bcferries")
    assert shape(watch) == shape(entry("bcferries"))
    assert list(watch) == list(entry("bcferries"))
    assert (watch["id"], watch["trip"]["id"]) == (trip_id, trip_id)


async def test_public_holds_only_links_and_push(db: Database) -> None:
    pool = None
    async with aiohttp.ClientSession() as http:
        oidc = OidcClient(http, OidcSettings(issuer="https://idp", client_id="c", client_secret="s", redirect_uri="x"))
        store = WebStore(pool, Limits(), PollPolicy(), clock=lambda: NOW)  # type: ignore[arg-type]
        signer = CheckoutSigner(Ed25519PrivateKey.generate(), "k1", "https://waitlist-checkout.chestr.dev")
        for extra in ("providers", "capacity", "watches"):
            with pytest.raises(ValueError, match="only"):
                build(
                    Accounts(pool, IdentityPolicy()),  # type: ignore[arg-type]
                    oidc,
                    store,
                    signer,
                    pool=pool,  # type: ignore[arg-type]
                    limits=Limits(),
                    public={"links": {}, extra: []},
                )
    assert db.name  # (the database itself isn't needed: build() refuses before using it)


async def test_an_admins_pause_shows_in_the_next_snapshot(rig: Rig) -> None:
    await rig.watch(date(2027, 7, 1))
    assert [p["paused"] for p in (await rig.state())["providers"]] == [False, False]
    await rig.sql("UPDATE fleet_state SET value = '[\"buntzen\"]' WHERE key = 'paused_providers'", user=ROLES.web)
    state = await rig.state()
    assert [(p["id"], p["paused"]) for p in state["providers"]] == [("bcferries", False), ("buntzen", True)]
    assert watches(state)[0]["check"]["state"] == "provider_paused"


async def test_a_watch_that_wont_load_degrades_and_the_state_stays_whole(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    trip_id = await seen_trip(rig)
    watch_id = await rig.watch(date(2027, 7, 1))
    await rig.sql('UPDATE trips SET spec = \'{"park": "garibaldi"}\' WHERE id = $1', watch_id)
    with caplog.at_level(logging.WARNING, logger="waitlist.web.repo"):
        state = await rig.state()
        await rig.state()
    unknown = next(w for w in state["watches"] if w["id"] == watch_id)
    assert unknown == {
        "provider": "buntzen",
        "id": watch_id,
        "rev": 1,
        "enabled": True,
        "paused_reason": None,
        "quiet": False,
    }
    assert shape(unknown) == shape({k: v for k, v in entry("bcparks").items() if k != "spec"})
    assert [w["id"] for w in watches(state, "bcferries")] == [trip_id]
    assert "hold" in watches(state, "bcferries")[0]
    assert state["capacity"]["providers"]["buntzen"]["count"] == 1  # it still counts against the limit
    assert len([r for r in caplog.records if watch_id in r.getMessage()]) == 1  # once per (id, rev)


async def test_a_bc_ferries_trip_that_wont_load_degrades_too(rig: Rig) -> None:
    trip_id = await seen_trip(rig)
    await rig.sql('UPDATE trips SET spec = \'{"from": "TSA"}\' WHERE id = $1', trip_id)
    (watch,) = watches(await rig.state())
    assert set(watch) == {"provider", "id", "rev", "enabled", "paused_reason", "quiet"}
    assert watch["provider"] == "bcferries"


# ---- with the real Buntzen provider (W2)


@pytest.fixture
async def buntzen(db: Database, aiohttp_client: Callable[..., Any]) -> AsyncIterator[Rig]:
    async for r in make_rig(db, aiohttp_client, registry=REGISTRY, terminals=True, policy=PollPolicy()):
        r.clock[0] = NOW
        await r.sql("UPDATE users SET is_admin = true WHERE id = $1", ANN)
        yield r


async def test_buntzen_entries_have_the_fixtures_shape(buntzen: Rig) -> None:
    rig = buntzen
    await rig.report()
    open_spec = entry("buntzen", 0)["spec"]
    waiting_spec = entry("buntzen", 1)["spec"]
    first = (await rig.ok("POST", "/api/watches", json={"provider": "buntzen", "spec": open_spec}))["watch_id"]
    await rig.ok("PATCH", f"/api/watches/{first}", json={"spec": open_spec})  # rev 2, as the fixture's
    await rig.ok("POST", "/api/watches", json={"provider": "buntzen", "spec": waiting_spec})
    await seen_trip(rig)
    options = entry("buntzen", 0)["options"]
    await rig.sql(
        "INSERT INTO check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at)"
        " VALUES ($1, $2, 'ok', $3, to_timestamp($4), to_timestamp($4))",
        first,
        search_hash(BUNTZEN.search_key(BUNTZEN.load_spec(open_spec))),
        json.dumps(options),
        NOW - 3,
        user=ROLES.control,
    )
    await rig.sql("UPDATE trip_runtime SET next_check_at = to_timestamp($2) WHERE trip_id = $1", first, NOW + 57)
    state = await rig.state()
    built = state["watches"]
    expected = fixture()["watches"][:3]
    assert [w["provider"] for w in built] == [w["provider"] for w in expected]
    for got, want in zip(built, expected, strict=True):
        assert shape(got) == shape(want), want["id"]
        assert list(got) == list(want)
    assert built[0]["check"]["state"] == "checking"
    assert built[0]["options"] == options
    assert built[0]["booking_url"] == entry("buntzen", 0)["booking_url"]
    assert built[1]["check"] == entry("buntzen", 1)["check"]
    assert built[1]["options"] == entry("buntzen", 1)["options"]
    assert state["capacity"] == fixture()["capacity"]
    assert state["providers"] == fixture()["providers"]


async def test_a_providers_bug_degrades_its_watch_not_the_state(
    db: Database, aiohttp_client: Callable[..., Any]
) -> None:
    """A provider that fails on a stored spec with something other than a SpecError: the state still comes back."""

    class Broken(FakeHttpProvider):
        def load_spec(self, stored: Mapping[str, Any]) -> FakeSpec:
            raise NotImplementedError  # as the contract commit's stub did, for everything

    async for rig in make_rig(db, aiohttp_client, registry=fake_registry(Broken()), policy=PollPolicy()):
        await rig.sql("INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', '{}')", "b" * 16, ANN)
        state = await rig.state()
        (watch,) = watches(state)
        assert set(watch) == {"provider", "id", "rev", "enabled", "paused_reason", "quiet"}
        assert state["capacity"]["providers"]["buntzen"]["count"] == 1
