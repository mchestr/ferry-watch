"""ferry-control's hub endpoint, end to end: the agent's own client and store against it, on real Postgres."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg
import pytest
from aiohttp.test_utils import TestServer

from ferry_watch.agent.hub import HubClient, Identity
from ferry_watch.agent.store import OneTripStore
from ferry_watch.control.configure import HubSettings
from ferry_watch.control.hub import CLOSE_NOT_YET_RECORDED, CLOSE_VIOLATION, PATH, HubEndpoint
from ferry_watch.control.kube import ObservedPod, PodIdentity
from ferry_watch.control.store import ControlStore
from ferry_watch.models import PACIFIC, Trip
from ferry_watch.protocol import (
    Alert,
    AlertParams,
    Bye,
    CartInfo,
    Command,
    Configure,
    ControlMessage,
    Event,
    Hello,
    State,
    ViewerInfo,
    encode,
)
from tests.postgres import ROLES, Database

USER = "a" * 20
OTHER_USER = "b" * 20
TRIP = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 16), earliest=time(7))
POD = f"fw-{TRIP.id}-g1"
UID = "11111111-2222-3333-4444-555555555555"
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=PACIFIC).timestamp()  # 19 h before the trip: the 60 s tier


@dataclass
class FakeCluster:
    tokens: dict[str, PodIdentity] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)

    async def review_token(self, token: str, audience: str) -> PodIdentity | None:
        assert audience == "ferry-watch-agent"
        return self.tokens.get(token)

    async def list_pods(self) -> list[ObservedPod]:
        return []

    async def create_pod(self, spec: dict[str, Any]) -> ObservedPod:
        raise NotImplementedError

    async def delete_pod(self, name: str) -> None:
        self.deleted.append(name)

    async def set_egress(self, name: str, value: str) -> None:
        raise NotImplementedError


async def seed(db: Database, *, uid: str | None = UID, trip: Trip = TRIP, user: str = USER, pod: str = POD) -> None:
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ($1, $1) ON CONFLICT DO NOTHING", user)
        await owner.execute(
            "INSERT INTO trips (id, user_id, spec, rev) VALUES ($1, $2, $3, 3)",
            trip.id,
            user,
            json.dumps(trip.to_json()),
        )
        await owner.execute("INSERT INTO trip_runtime (trip_id) VALUES ($1)", trip.id)
        await owner.execute(
            "INSERT INTO browser_pods (name, uid, trip_id, user_id, gen, mode, deadline_at)"
            " VALUES ($1, $2, $3, $4, 1, 'warm', now() + interval '26 hours')",
            pod,
            uid,
            trip.id,
            user,
        )
    finally:
        await owner.close()


@dataclass
class Rig:
    hub: HubEndpoint
    server: TestServer
    cluster: FakeCluster
    db: Database
    clock: list[float]

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        conn = await self.db.connect()
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()


@pytest.fixture
async def rig(db: Database, aiohttp_server: Callable[..., Any]) -> AsyncIterator[Rig]:
    await seed(db)
    pool = await asyncpg.create_pool(db.dsn(ROLES.control), min_size=1, max_size=4)
    cluster = FakeCluster({"token-1": PodIdentity(UID, POD)})
    clock = [NOON]
    hub = HubEndpoint(ControlStore(pool), cluster, HubSettings(), clock=lambda: clock[0])
    server: TestServer = await aiohttp_server(hub.app())
    try:
        yield Rig(hub, server, cluster, db, clock)
    finally:
        await server.close()
        await pool.close()


@dataclass
class Agent:
    client: HubClient
    store: OneTripStore
    received: list[ControlMessage]


@pytest.fixture
async def agent(rig: Rig, tmp_path: Path) -> AsyncIterator[Agent]:
    (tmp_path / "token").write_text("token-1")
    store = OneTripStore(TRIP.id)
    received: list[ControlMessage] = []

    async def handle(message: ControlMessage) -> None:
        received.append(message)
        if isinstance(message, Configure):
            store.apply(message)

    async with aiohttp.ClientSession() as http:
        client = HubClient(
            http,
            str(rig.server.make_url(PATH)).replace("http", "ws", 1),
            tmp_path / "token",
            Identity(TRIP.id, POD, 1, "0.6.0"),
            handle,
            rev=lambda: max(store.rev, 0),
            backoff=(0.05,),
        )
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(client.connected.wait(), 5)
            yield Agent(client, store, received)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110 - polls state that has nothing to await
            await asyncio.sleep(0.02)


async def eventually(rig: Rig, sql: str, *args: Any) -> list[asyncpg.Record]:
    async with asyncio.timeout(5):
        while not (rows := await rig.fetch(sql, *args)):  # noqa: ASYNC110 - polls the database
            await asyncio.sleep(0.02)
    return rows


# ---- binding


async def test_the_agent_is_bound_and_configured(rig: Rig, agent: Agent) -> None:
    await until(lambda: agent.store.rev == 3)
    latest = agent.store.latest
    assert latest is not None
    assert latest.mode == "warm"
    assert latest.interval_s == 60  # 19 h out: the 60 s tier
    assert latest.vnc_allowed
    assert latest.auto_cart
    assert agent.store.config.trip(TRIP.id) == TRIP
    assert rig.hub.connected(POD)


async def test_an_unknown_token_is_refused(rig: Rig) -> None:
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await http.ws_connect(rig.server.make_url(PATH), headers={"Authorization": "Bearer nope"})
    assert e.value.status == 401


async def test_a_pod_not_yet_recorded_is_asked_to_retry(rig: Rig) -> None:
    other = Trip(id="ffffffffffffffff", origin="SWB", destination="TSA", date=date(2026, 10, 17))
    await seed(rig.db, uid=None, trip=other, pod=f"fw-{other.id}-g1")
    rig.cluster.tokens["token-2"] = PodIdentity("9999", f"fw-{other.id}-g1")
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(rig.server.make_url(PATH), headers={"Authorization": "Bearer token-2"})
        await ws.receive()
        assert ws.close_code == CLOSE_NOT_YET_RECORDED


async def test_a_hello_for_another_trip_is_refused(rig: Rig) -> None:
    hello = Hello(
        seq=1,
        trip="ffffffffffffffff",
        boot="0" * 16,
        rev=0,
        proto=[1],
        agent_version="0.6.0",
        pod_name=POD,
        gen=1,
        applied_rev=0,
        snapshot=State(seq=1, trip="ffffffffffffffff", boot="0" * 16, rev=0, phase="starting"),
    )
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(rig.server.make_url(PATH), headers={"Authorization": "Bearer token-1"})
        await ws.send_bytes(encode(hello))
        await ws.receive()
        assert ws.close_code == CLOSE_VIOLATION
    assert not rig.hub.connected(POD)


async def test_lying_about_the_trip_after_hello_deletes_the_pod(rig: Rig, agent: Agent) -> None:
    lie = Event(seq=99, trip="ffffffffffffffff", boot=agent.client.boot, rev=0, code="cart_added")
    agent.client.send(lie)
    await until(lambda: rig.cluster.deleted == [POD])


# ---- what the agent reports


async def test_state_is_recorded(rig: Rig, agent: Agent) -> None:
    state = State(
        **agent.client.envelope(),
        phase="holding",
        checked_at=NOON,
        cart=CartInfo(id="abc123", status="ready", depart="09:00", ready_at=NOON),
        viewer=ViewerInfo(connected=True, first_at=NOON, seconds=90),
        alerted=["09:00"],
    )
    agent.client.publish(state)
    rows = await eventually(rig, "SELECT * FROM trip_state WHERE agent_phase = 'holding'")
    assert rows[0]["viewer_connected"]
    assert json.loads(rows[0]["cart"])["id"] == "abc123"
    assert (await rig.fetch("SELECT phase FROM browser_pods"))[0]["phase"] == "holding"
    assert (await rig.fetch("SELECT alerted FROM alert_memory"))[0]["alerted"] == ["09:00"]
    usage = await eventually(rig, "SELECT amount FROM usage_ledger WHERE kind = 'vnc_seconds'")
    assert usage[0]["amount"] == 90


async def test_alerts_are_queued_once_even_when_replayed(rig: Rig, agent: Agent) -> None:
    alert = Alert(**agent.client.envelope(), kind="cart_held", params=AlertParams(departures=["09:00"], fare="Saver"))
    agent.client.send(alert)
    await until(lambda: agent.client.acked(alert.seq))
    agent.client.send(alert)  # the same message again, as after a reconnect
    await until(lambda: agent.client.acked(alert.seq))
    rows = await rig.fetch("SELECT kind, params, dedup_key FROM notifications")
    assert [(r["kind"], json.loads(r["params"])["departures"]) for r in rows] == [("cart_held", ["09:00"])]
    assert rows[0]["dedup_key"] == f"{POD}:{agent.client.boot}:{alert.seq}"
    usage = await rig.fetch("SELECT amount FROM usage_ledger WHERE kind = 'holds'")
    assert usage[0]["amount"] == 1  # counted once


async def test_one_hold_per_user(rig: Rig, agent: Agent) -> None:
    assert await agent.client.claim("09:00", "saver") is True
    assert await agent.client.claim("09:00", "saver") is False  # this user already holds a claim
    rows = await rig.fetch("SELECT pod FROM hold_claims")
    assert [r["pod"] for r in rows] == [POD]
    assert (await rig.fetch("SELECT amount FROM usage_ledger WHERE kind = 'cart_attempts'"))[0]["amount"] == 1


async def test_no_carts_in_quiet_hours(rig: Rig, agent: Agent) -> None:
    rig.clock[0] = datetime(2026, 10, 15, 23, 30, tzinfo=PACIFIC).timestamp()
    assert await agent.client.claim("09:00", "saver") is False
    assert await rig.fetch("SELECT * FROM hold_claims") == []


async def test_a_viewed_hold_without_a_booking_pauses_the_trip(rig: Rig, agent: Agent) -> None:
    bye = Bye(**agent.client.envelope(), reason="hold_ended", outcome="expired", viewer_was_connected=True)
    agent.client.send(bye)
    await until(lambda: agent.client.acked(bye.seq))
    trip = (await rig.fetch("SELECT paused_reason, archived_at FROM trips"))[0]
    assert (trip["paused_reason"], trip["archived_at"]) == ("after_checkout", None)
    assert [r["kind"] for r in await rig.fetch("SELECT kind FROM notifications")] == ["booked_it"]
    assert [r["outcome"] for r in await rig.fetch("SELECT outcome FROM hold_outcomes")] == ["expired"]


async def test_a_booking_archives_the_trip(rig: Rig, agent: Agent) -> None:
    bye = Bye(**agent.client.envelope(), reason="hold_ended", outcome="booked", viewer_was_connected=True)
    agent.client.send(bye)
    await until(lambda: agent.client.acked(bye.seq))
    trip = (await rig.fetch("SELECT archived_at FROM trips"))[0]
    assert trip["archived_at"] is not None
    assert [r["kind"] for r in await rig.fetch("SELECT kind FROM notifications")] == ["booked"]


# ---- what the agent is told


async def test_commands_and_configuration_changes_reach_the_pod(rig: Rig, agent: Agent) -> None:
    await until(lambda: agent.store.rev == 3)
    owner = await rig.db.connect()
    try:
        await owner.execute(
            "INSERT INTO agent_commands (trip_id, op, requested_by) VALUES ($1, 'check_now', $2)", TRIP.id, USER
        )
        await owner.execute("UPDATE trips SET rev = 4, enabled = false")
    finally:
        await owner.close()
    await rig.hub.refresh()
    await until(lambda: any(isinstance(m, Command) and m.op == "check_now" for m in agent.received))
    await until(lambda: agent.store.rev == 4)
    latest = agent.store.latest
    assert latest is not None
    assert latest.active is False
    assert (await rig.fetch("SELECT consumed_at FROM agent_commands"))[0]["consumed_at"] is not None


async def test_an_admins_recycle_drains_the_pod(rig: Rig, agent: Agent) -> None:
    owner = await rig.db.connect()
    try:
        await owner.execute(  # asked for before this pod existed: meant for an earlier generation
            "INSERT INTO agent_commands (trip_id, op, requested_by, created_at)"
            " VALUES ($1, 'recycle', 'admin:x', now() - interval '1 day')",
            TRIP.id,
        )
        recycle = await owner.fetchval(
            "INSERT INTO agent_commands (trip_id, op, requested_by) VALUES ($1, 'recycle', 'admin:x') RETURNING id",
            TRIP.id,
        )
    finally:
        await owner.close()
    await rig.hub.refresh()
    await until(lambda: any(isinstance(m, Command) and m.op == "drain" for m in agent.received))
    drains = [m for m in agent.received if isinstance(m, Command) and m.op == "drain"]
    assert [(m.id, m.reason) for m in drains] == [(recycle, "recycle")]
    assert [r["consumed_at"] is not None for r in await rig.fetch("SELECT consumed_at FROM agent_commands")] == [
        True,
        True,
    ]


async def test_the_fleet_loop_can_drain_a_pod(rig: Rig, agent: Agent) -> None:
    assert await rig.hub.command(POD, "drain", "superseded")
    await until(lambda: any(isinstance(m, Command) and m.op == "drain" for m in agent.received))
    assert not await rig.hub.command("fw-ffffffffffffffff-g1", "drain", "past")
