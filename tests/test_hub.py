"""waitlist-control's hub endpoint, end to end: the agent's own client and store against it, on real Postgres."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg
import msgspec
import pytest
from aiohttp.test_utils import TestServer

from tests.postgres import ROLES, Database
from waitlist.agent.hub import HubClient, Identity
from waitlist.agent.store import OneTripStore
from waitlist.control.configure import HubSettings, StreamViewer
from waitlist.control.hub import CLOSE_NOT_YET_RECORDED, CLOSE_VIOLATION, PATH, HubEndpoint
from waitlist.control.kube import ObservedPod, PodIdentity
from waitlist.control.store import CLAIM_LAPSE_SECONDS, ControlStore
from waitlist.models import PACIFIC, Trip
from waitlist.protocol import (
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
    decode_control_message,
    encode,
)

USER = "a" * 20
OTHER_USER = "b" * 20
TRIP = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 16), earliest=time(7))
POD = f"wl-{TRIP.id}-g1"
UID = "11111111-2222-3333-4444-555555555555"
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=PACIFIC).timestamp()  # 19 h before the trip: the 60 s tier
NIGHT = datetime(2026, 10, 15, 23, 30, tzinfo=PACIFIC).timestamp()


@dataclass
class FakeCluster:
    tokens: dict[str, PodIdentity] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)

    async def review_token(self, token: str, audience: str) -> PodIdentity | None:
        assert audience == "waitlist-agent"
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
            "INSERT INTO trips (id, user_id, provider, spec, rev) VALUES ($1, $2, 'bcferries', $3, 3)",
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
async def rig(db: Database, aiohttp_server: Callable[..., Any], request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    """The hub, with `checkout.streamViewer` "off" unless a test passes it as the rig's param."""
    stream_viewer: StreamViewer = getattr(request, "param", "off")
    await seed(db)
    pool = await asyncpg.create_pool(db.dsn(ROLES.control), min_size=1, max_size=4)
    cluster = FakeCluster({"token-1": PodIdentity(UID, POD)})
    clock = [NOON]
    hub = HubEndpoint(ControlStore(pool), cluster, HubSettings(stream_viewer=stream_viewer), clock=lambda: clock[0])
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
    assert [r["agent_version"] for r in await rig.fetch("SELECT agent_version FROM browser_pods")] == ["0.6.0"]


async def test_an_unknown_token_is_refused(rig: Rig) -> None:
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await http.ws_connect(rig.server.make_url(PATH), headers={"Authorization": "Bearer nope"})
    assert e.value.status == 401


async def test_a_pod_not_yet_recorded_is_asked_to_retry(rig: Rig) -> None:
    other = Trip(id="ffffffffffffffff", origin="SWB", destination="TSA", date=date(2026, 10, 17))
    await seed(rig.db, uid=None, trip=other, pod=f"wl-{other.id}-g1")
    rig.cluster.tokens["token-2"] = PodIdentity("9999", f"wl-{other.id}-g1")
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


async def test_checkout_seconds_are_counted_once_across_reconnects(rig: Rig, agent: Agent) -> None:
    def watched(seconds: int) -> State:
        return State(**agent.client.envelope(), phase="holding", viewer=ViewerInfo(connected=True, seconds=seconds))

    async def counted() -> int:
        rows = await rig.fetch("SELECT amount FROM usage_ledger WHERE kind = 'vnc_seconds'")
        return rows[0]["amount"] if rows else 0

    agent.client.publish(watched(90))
    await eventually(rig, "SELECT 1 FROM usage_ledger WHERE kind = 'vnc_seconds' AND amount = 90")
    await rig.hub._peers[
        POD
    ].ws.close()  # a dropped socket, or a restarted waitlist-control: the agent says hello again
    await until(lambda: rig.hub.connected(POD) and rig.hub._peers[POD].vnc_seconds == 90)
    assert await counted() == 90  # the hello's snapshot repeats the running total; it isn't counted again
    agent.client.publish(watched(150))
    await eventually(rig, "SELECT 1 FROM usage_ledger WHERE kind = 'vnc_seconds' AND amount = 150")


async def test_checkout_seconds_already_counted_before_0003_are_not_counted_again(rig: Rig, agent: Agent) -> None:
    """A pod live when 0003 ran: the old waitlist-control counted its seconds, so its next total is the baseline."""
    owner = await rig.db.connect()
    try:
        await owner.execute("UPDATE browser_pods SET vnc_boot = 'pre-0003'")
    finally:
        await owner.close()

    def watched(seconds: int) -> State:
        return State(**agent.client.envelope(), phase="holding", viewer=ViewerInfo(connected=True, seconds=seconds))

    agent.client.publish(watched(900))
    await eventually(rig, "SELECT 1 FROM browser_pods WHERE vnc_seconds = 900")  # the baseline, counted by nobody
    assert await rig.fetch("SELECT 1 FROM usage_ledger WHERE kind = 'vnc_seconds'") == []
    agent.client.publish(watched(960))
    await eventually(rig, "SELECT 1 FROM usage_ledger WHERE kind = 'vnc_seconds' AND amount = 60")


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


async def test_a_hold_that_ends_without_an_alert_frees_the_claim(rig: Rig, agent: Agent) -> None:
    """Released on the dashboard, nobody viewing: the pod watches on, and the user may hold again."""
    assert await agent.client.claim("09:00", "saver") is True
    ready = CartInfo(id="abc123", status="ready", depart="09:00", ready_at=NOON)
    agent.client.publish(State(**agent.client.envelope(), phase="holding", cart=ready))
    await eventually(rig, "SELECT 1 FROM hold_claims WHERE cart_id = 'abc123'")
    agent.client.publish(State(**agent.client.envelope(), phase="watching"))
    async with asyncio.timeout(5):
        while await rig.fetch("SELECT 1 FROM hold_claims"):  # noqa: ASYNC110 - polls the database
            await asyncio.sleep(0.02)
    assert await agent.client.claim("11:00", "saver") is True


async def test_a_claim_waiting_for_its_cart_is_kept(rig: Rig, agent: Agent) -> None:
    assert await agent.client.claim("09:00", "saver") is True
    agent.client.publish(State(**agent.client.envelope(), phase="watching", checked_at=NOON))
    await eventually(rig, "SELECT 1 FROM trip_state WHERE checked_at IS NOT NULL")
    assert len(await rig.fetch("SELECT 1 FROM hold_claims")) == 1


async def quiet_hours(rig: Rig, start: time = time(23, 0), end: time = time(6, 0)) -> None:
    """Quiet hours the user chose (without any, they have the default: 10 pm to 5 am)."""
    owner = await rig.db.connect()
    try:
        await owner.execute(
            "INSERT INTO user_settings (user_id, quiet_start, quiet_end) VALUES ($1, $2, $3)", USER, start, end
        )
    finally:
        await owner.close()


async def lift(rig: Rig, column: str, minutes: int = 10) -> None:
    """Lift the user's quiet hours for the trip: "Hold it for me" (hold_requested_until) or a manual check."""
    owner = await rig.db.connect()
    try:
        await owner.execute(f"UPDATE trip_runtime SET {column} = to_timestamp($1)", rig.clock[0] + minutes * 60)  # noqa: S608
    finally:
        await owner.close()


async def test_no_carts_in_the_users_quiet_hours(rig: Rig, agent: Agent) -> None:
    await quiet_hours(rig)
    rig.clock[0] = NIGHT
    assert await agent.client.claim("09:00", "saver") is False
    assert await rig.fetch("SELECT * FROM hold_claims") == []


async def test_a_user_who_never_chose_quiet_hours_has_the_default(rig: Rig, agent: Agent) -> None:
    rig.clock[0] = datetime(2026, 10, 15, 22, 0, tzinfo=PACIFIC).timestamp()  # 10 pm to 5 am
    assert await agent.client.claim("09:00", "saver") is False
    rig.clock[0] = datetime(2026, 10, 16, 5, 0, tzinfo=PACIFIC).timestamp()
    assert await agent.client.claim("09:00", "saver") is True


async def test_a_hold_the_user_asks_for_is_claimed_in_quiet_hours(rig: Rig, agent: Agent) -> None:
    await quiet_hours(rig)
    rig.clock[0] = NIGHT
    await lift(rig, "hold_requested_until")
    assert await agent.client.claim("09:00", "saver") is True
    held = Alert(**agent.client.envelope(), kind="cart_held", params=AlertParams(departures=["09:00"]))
    agent.client.send(held)
    await until(lambda: agent.client.acked(held.seq))
    rows = await rig.fetch("SELECT hold_requested_until FROM trip_runtime")
    assert rows[0]["hold_requested_until"] is None  # tried: quiet hours apply again


async def test_a_manual_check_lifts_quiet_hours_too(rig: Rig, agent: Agent) -> None:
    await quiet_hours(rig)
    rig.clock[0] = NIGHT
    await lift(rig, "quiet_lifted_until")
    assert await agent.client.claim("09:00", "saver") is True


@pytest.mark.parametrize("column", ["hold_requested_until", "quiet_lifted_until"])
async def test_a_claim_keeps_the_lift_while_the_cart_is_added(rig: Rig, agent: Agent, column: str) -> None:
    """A lift that would lapse while the pod adds the cart is kept until the claim lapses, so the fleet loop
    doesn't drain the pod for quiet hours halfway through."""
    await quiet_hours(rig)
    rig.clock[0] = NIGHT
    await lift(rig, column, minutes=1)
    assert await agent.client.claim("09:00", "saver") is True
    rows = await rig.fetch("SELECT extract(epoch FROM quiet_lifted_until) AS until FROM trip_runtime")
    assert float(rows[0]["until"]) == NIGHT + CLAIM_LAPSE_SECONDS


@pytest.mark.parametrize("column", ["hold_requested_until", "quiet_lifted_until"])
async def test_a_lapsed_lift_is_quiet_hours_again(rig: Rig, agent: Agent, column: str) -> None:
    await quiet_hours(rig)
    rig.clock[0] = NIGHT
    await lift(rig, column, minutes=-1)
    assert await agent.client.claim("09:00", "saver") is False


async def test_in_quiet_hours_the_pod_stops_checking_until_lifted(rig: Rig, agent: Agent) -> None:
    await until(lambda: agent.store.config.enabled)
    await quiet_hours(rig)
    rig.clock[0] = NIGHT
    await rig.hub.refresh()  # within 5 s of quiet hours starting
    await until(lambda: not agent.store.config.enabled)
    await lift(rig, "quiet_lifted_until")  # a manual check
    await rig.hub.refresh()
    await until(lambda: agent.store.config.enabled)
    latest = agent.store.latest
    assert latest is not None
    assert latest.auto_cart  # and it may hold a cart
    rig.clock[0] = NIGHT + 601
    await rig.hub.refresh()
    await until(lambda: not agent.store.config.enabled)
    rig.clock[0] = datetime(2026, 10, 16, 6, 0, tzinfo=PACIFIC).timestamp()  # morning
    await rig.hub.refresh()
    await until(lambda: agent.store.config.enabled)


@pytest.mark.parametrize(
    ("setup", "held_back"),
    [
        ("quiet", "quiet_hours"),
        ("manual", "auto_cart_off"),
        ("elsewhere", "holding_elsewhere"),
        ("nothing", "unknown"),
    ],
)
async def test_a_bookable_alert_says_why_nothing_was_held(rig: Rig, agent: Agent, setup: str, held_back: str) -> None:
    owner = await rig.db.connect()
    try:
        if setup == "quiet":
            await quiet_hours(rig)
            rig.clock[0] = NIGHT
        elif setup == "manual":
            spec = TRIP.to_json() | {"auto_cart": False}
            await owner.execute("UPDATE trips SET spec = $1", json.dumps(spec))
        elif setup == "elsewhere":
            # The user's other BC Ferries trip (only those have cart claims), holding the one cart.
            await owner.execute(
                "INSERT INTO trips (id, user_id, provider, spec) VALUES ('ffffffffffffffff', $1, 'bcferries', '{}')",
                USER,
            )
            await owner.execute(
                "INSERT INTO hold_claims (user_id, trip_id, pod, cart_id) VALUES ($1, 'ffffffffffffffff', 'x', 'c1')",
                USER,
            )
    finally:
        await owner.close()
    alert = Alert(**agent.client.envelope(), kind="bookable", params=AlertParams(departures=["09:00"]))
    agent.client.send(alert)
    await until(lambda: agent.client.acked(alert.seq))
    rows = await rig.fetch("SELECT params FROM notifications")
    assert json.loads(rows[0]["params"]) == {
        "departures": ["09:00"],
        "fare": None,
        "price": None,
        "minutes": None,
        "error_class": None,
        "by_site": None,
        "held_back": held_back,
    }


async def test_a_viewed_hold_without_a_booking_pauses_the_trip(rig: Rig, agent: Agent) -> None:
    bye = Bye(**agent.client.envelope(), reason="hold_ended", outcome="expired", viewer_was_connected=True)
    agent.client.send(bye)
    await until(lambda: agent.client.acked(bye.seq))
    trip = (await rig.fetch("SELECT paused_reason, archived_at FROM trips"))[0]
    assert (trip["paused_reason"], trip["archived_at"]) == ("after_checkout", None)
    assert [r["kind"] for r in await rig.fetch("SELECT kind FROM notifications")] == ["booked_it"]
    assert [r["outcome"] for r in await rig.fetch("SELECT outcome FROM hold_outcomes")] == ["expired"]


async def test_a_release_the_user_asked_for_asks_nothing(rig: Rig, agent: Agent) -> None:
    """They pressed Release (or an admin did): they know they didn't book it. The outcome is still recorded."""
    bye = Bye(**agent.client.envelope(), reason="hold_ended", outcome="released", viewer_was_connected=True)
    agent.client.send(bye)
    await until(lambda: agent.client.acked(bye.seq))
    trip = (await rig.fetch("SELECT paused_reason, archived_at FROM trips"))[0]
    assert (trip["paused_reason"], trip["archived_at"]) == (None, None)
    assert await rig.fetch("SELECT kind FROM notifications") == []
    assert [r["outcome"] for r in await rig.fetch("SELECT outcome FROM hold_outcomes")] == ["released"]


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [("drained", "released"), ("deadline", "released"), ("hold_ended", "payment_left_to_site")],
)
async def test_a_viewed_hold_that_ended_otherwise_still_asks(rig: Rig, agent: Agent, reason: str, outcome: str) -> None:
    bye = Bye(**agent.client.envelope(), reason=reason, outcome=outcome, viewer_was_connected=True)  # type: ignore[arg-type]
    agent.client.send(bye)
    await until(lambda: agent.client.acked(bye.seq))
    assert (await rig.fetch("SELECT paused_reason FROM trips"))[0]["paused_reason"] == "after_checkout"
    assert [r["kind"] for r in await rig.fetch("SELECT kind FROM notifications")] == ["booked_it"]


async def test_a_hold_drained_because_the_trip_was_removed_asks_nothing(rig: Rig, agent: Agent) -> None:
    """Removing a trip drains its pod, which lets the cart go: no "Did you book it?" about a trip that's gone."""
    owner = await rig.db.connect()
    try:
        await owner.execute("UPDATE trips SET archived_at = now(), enabled = false")
    finally:
        await owner.close()
    bye = Bye(**agent.client.envelope(), reason="drained", outcome="released", viewer_was_connected=True)
    agent.client.send(bye)
    await until(lambda: agent.client.acked(bye.seq))
    assert (await rig.fetch("SELECT paused_reason FROM trips"))[0]["paused_reason"] is None
    assert await rig.fetch("SELECT kind FROM notifications") == []
    assert [r["outcome"] for r in await rig.fetch("SELECT outcome FROM hold_outcomes")] == ["released"]


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
    assert not await rig.hub.command("wl-ffffffffffffffff-g1", "drain", "past")


# ---- the streamed checkout viewer's kill switch (cast_allowed)

# Configure as a 0.6.4 agent decodes it: these fields and no others (it refuses unknown ones, as every agent does).
CONFIGURE_064 = frozenset(
    {"v", "seq", "trip", "boot", "rev", "trip_spec", "active", "mode", "interval_s", "auto_cart", "checkout_mode"}
    | {"hold", "alerted", "offered", "vnc_allowed", "brake", "breaker"}
)


def _as_064() -> msgspec.json.Decoder[Any]:
    def field(info: msgspec.structs.FieldInfo) -> tuple[Any, ...]:
        if info.default is not msgspec.NODEFAULT:
            return (info.name, info.type, info.default)
        if info.default_factory is not msgspec.NODEFAULT:
            return (info.name, info.type, msgspec.field(default_factory=info.default_factory))
        return (info.name, info.type)

    fields = [field(f) for f in msgspec.structs.fields(Configure) if f.name in CONFIGURE_064]
    old = msgspec.defstruct(
        "Configure064", fields, kw_only=True, forbid_unknown_fields=True, tag_field="type", tag="configure"
    )
    return msgspec.json.Decoder(old)


AS_064 = _as_064()


@dataclass
class RawAgent:
    """An agent of any version, seen frame by frame: what the hub sends it, before any decoding."""

    ws: aiohttp.ClientWebSocketResponse

    async def configure(self) -> bytes:
        async with asyncio.timeout(5):
            while True:
                frame = await self.ws.receive()
                assert frame.type == aiohttp.WSMsgType.BINARY, frame
                if json.loads(frame.data)["type"] == "configure":
                    return bytes(frame.data)

    async def nothing_more(self) -> None:
        with pytest.raises(TimeoutError):
            await self.ws.receive(timeout=0.3)


@contextlib.asynccontextmanager
async def raw_agent(server: TestServer, version: str) -> AsyncIterator[RawAgent]:
    boot = "0" * 16
    hello = Hello(
        seq=1,
        trip=TRIP.id,
        boot=boot,
        rev=0,
        proto=[1],
        agent_version=version,
        pod_name=POD,
        gen=1,
        applied_rev=0,
        snapshot=State(seq=1, trip=TRIP.id, boot=boot, rev=0, phase="watching"),
    )
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(server.make_url(PATH), headers={"Authorization": "Bearer token-1"}) as ws,
    ):
        await ws.send_bytes(encode(hello))
        yield RawAgent(ws)


async def owner(rig: Rig, sql: str, *args: Any) -> None:
    conn = await rig.db.connect()
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


def cast_allowed(frame: bytes) -> bool:
    configure = decode_control_message(frame)
    assert isinstance(configure, Configure)
    return configure.cast_allowed


def test_the_064_schema_is_todays_without_cast_allowed() -> None:
    """When a release adds another configure field, older agents need it left out too (see _encode_for)."""
    assert {f.name for f in msgspec.structs.fields(Configure)} == CONFIGURE_064 | {"cast_allowed"}


@pytest.mark.parametrize(
    ("rig", "admin", "allowed"),
    [
        ("off", True, False),
        ("admins", False, False),
        ("admins", True, True),
        ("everyone", False, True),
    ],
    indirect=["rig"],
)
async def test_a_070_agent_may_stream_as_the_deployment_says(rig: Rig, admin: bool, allowed: bool) -> None:
    await owner(rig, "UPDATE users SET is_admin = $1", admin)
    async with raw_agent(rig.server, "0.7.0") as agent:
        frame = await agent.configure()
    assert json.loads(frame)["cast_allowed"] is allowed  # always sent to 0.7.0, so false withdraws a live stream
    assert cast_allowed(frame) is allowed


@pytest.mark.parametrize("rig", ["everyone"], indirect=True)
async def test_a_suspended_owner_may_not_stream(rig: Rig) -> None:
    await owner(rig, "UPDATE users SET state = 'suspended'")
    async with raw_agent(rig.server, "0.7.0") as agent:
        configure = decode_control_message(await agent.configure())
    assert isinstance(configure, Configure)
    assert (configure.vnc_allowed, configure.cast_allowed) == (False, False)


@pytest.mark.parametrize("version", ["0.6.4", "0.6.0", "0.7.0rc1", "garbage"])
@pytest.mark.parametrize("rig", ["everyone"], indirect=True)
async def test_an_older_agent_is_never_sent_cast_allowed(rig: Rig, version: str) -> None:
    """0.6.x agents refuse a configure with a field they don't know (and close the socket): they get 0.6.4's."""
    await owner(rig, "UPDATE users SET is_admin = true")
    async with raw_agent(rig.server, version) as agent:
        frame = await agent.configure()
        assert "cast_allowed" not in json.loads(frame)
        old = AS_064.decode(frame)
        assert (old.vnc_allowed, old.active, old.interval_s) == (True, True, 60)
        # A change only in what it's never sent isn't pushed to it.
        await owner(rig, "UPDATE users SET is_admin = false")
        await rig.hub.refresh()
        await agent.nothing_more()
    with pytest.raises(msgspec.ValidationError, match="cast_allowed"):  # what the hub must never send it
        AS_064.decode(encode(msgspec.convert(json.loads(frame) | {"cast_allowed": False}, Configure)))


@pytest.mark.parametrize("rig", ["admins"], indirect=True)
async def test_a_change_in_who_may_stream_is_pushed(rig: Rig) -> None:
    async with raw_agent(rig.server, "0.7.0") as agent:
        assert cast_allowed(await agent.configure()) is False
        await owner(rig, "UPDATE users SET is_admin = true")
        await rig.hub.refresh()
        assert cast_allowed(await agent.configure()) is True
        await rig.hub.refresh()
        await agent.nothing_more()  # nothing else changed


@pytest.mark.parametrize("rig", ["everyone"], indirect=True)
async def test_turning_the_beta_off_withdraws_it_from_running_pods(
    rig: Rig, aiohttp_server: Callable[..., Any]
) -> None:
    """Changing checkout.streamViewer rolls waitlist-control; each agent dials the new one, whose first configure
    withdraws the streamed viewer (the pod's gate then closes a live stream with 4005)."""
    async with raw_agent(rig.server, "0.7.0") as agent:
        assert cast_allowed(await agent.configure()) is True
    pool = await asyncpg.create_pool(rig.db.dsn(ROLES.control), min_size=1, max_size=2)
    try:
        hub = HubEndpoint(ControlStore(pool), rig.cluster, HubSettings(stream_viewer="off"), clock=lambda: NOON)
        server: TestServer = await aiohttp_server(hub.app())
        async with raw_agent(server, "0.7.0") as agent:
            assert cast_allowed(await agent.configure()) is False
        await server.close()
    finally:
        await pool.close()
