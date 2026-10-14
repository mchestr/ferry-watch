from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import msgspec
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ferry_watch.agent.hub import HubClient, Identity
from ferry_watch.agent.store import OneTripStore
from ferry_watch.models import Trip
from ferry_watch.protocol import (
    Ack,
    Alert,
    AlertParams,
    Bye,
    CartClaim,
    CartClaimResult,
    ClaimReason,
    Configure,
    ControlMessage,
    Event,
    Hello,
    HelloAck,
    HoldLimits,
    ProtocolError,
    TripSpec,
    decode_agent_message,
    encode,
)

TRIP = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 14))
IDENTITY = Identity(
    trip_id=TRIP.id, pod_name=f"fw-{TRIP.id}-g1", gen=1, image_digest="sha256:" + "a" * 64, agent_version="0.6.0"
)
HOLD = HoldLimits(unclaimed_s=1800, session_s=1800, viewer_gone_s=600, hard_s=3600)


def configure(rev: int = 1, *, trip: Trip = TRIP, **kw: Any) -> Configure:
    fields: dict[str, Any] = {"active": True, "interval_s": 300, "auto_cart": True, "hold": HOLD}
    return Configure(
        seq=rev,
        trip=TRIP.id,
        boot="0" * 16,
        rev=rev,
        trip_spec=msgspec.convert(trip.to_json(), TripSpec),
        **(fields | kw),
    )


# ---- a fake operator


@dataclass
class FakeOperator:
    token: str = "token-1"
    ack: bool = True
    grant: bool | None = True  # None: never answer a cart claim
    hellos: list[Hello] = field(default_factory=list)
    received: list[Any] = field(default_factory=list)
    connections: int = 0
    after_hello: list[ControlMessage | bytes] = field(default_factory=list)
    drop: asyncio.Event = field(default_factory=asyncio.Event)
    got: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def handler(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            raise web.HTTPUnauthorized
        self.connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = decode_agent_message((await ws.receive()).data)
        assert isinstance(hello, Hello)
        self.hellos.append(hello)
        await ws.send_bytes(encode(HelloAck(**_envelope(), server_time=1.0, accepted_proto=1, resend_from=0)))
        for message in self.after_hello:
            await ws.send_bytes(message if isinstance(message, bytes) else encode(message))
        reader = asyncio.create_task(self._read(ws))
        dropper = asyncio.create_task(self.drop.wait())
        try:
            await asyncio.wait([reader, dropper], return_when=asyncio.FIRST_COMPLETED)
        finally:
            reader.cancel()
            dropper.cancel()
            await asyncio.gather(reader, dropper, return_exceptions=True)
            self.drop.clear()
            await ws.close()
        return ws

    async def _read(self, ws: web.WebSocketResponse) -> None:
        async for frame in ws:
            message = decode_agent_message(frame.data)
            async with self.got:
                self.received.append(message)
                self.got.notify_all()
            if self.ack and isinstance(message, Event | Alert | Bye):
                await ws.send_bytes(encode(Ack(**_envelope(), acked=message.seq)))
            if isinstance(message, CartClaim) and self.grant is not None:
                reason: ClaimReason = "ok" if self.grant else "holding_elsewhere"
                answer = CartClaimResult(**_envelope(), claim=message.seq, granted=self.grant, reason=reason)
                await ws.send_bytes(encode(answer))

    async def wait_for(self, check: Callable[[], bool]) -> None:
        async with asyncio.timeout(5):
            async with self.got:
                await self.got.wait_for(check)


def _envelope() -> dict[str, Any]:
    return {"seq": 1, "trip": TRIP.id, "boot": "0" * 16, "rev": 0}


@pytest.fixture
async def operator(aiohttp_server: Callable[..., Any]) -> tuple[FakeOperator, TestServer]:
    fake = FakeOperator()
    app = web.Application()
    app.router.add_get("/agent/v1", fake.handler)
    server: TestServer = await aiohttp_server(app)
    return fake, server


@dataclass
class Agent:
    client: HubClient
    store: OneTripStore
    handled: list[ControlMessage]
    task: asyncio.Task[None]


@pytest.fixture
async def agent(operator: tuple[FakeOperator, TestServer], tmp_path: Path) -> AsyncIterator[Agent]:
    fake, server = operator
    token = tmp_path / "token"
    token.write_text(fake.token + "\n")
    store = OneTripStore(TRIP.id)
    handled: list[ControlMessage] = []

    async def handle(message: ControlMessage) -> None:
        handled.append(message)
        if isinstance(message, Configure):
            store.apply(message)

    async with aiohttp.ClientSession() as http:
        client = HubClient(
            http,
            str(server.make_url("/agent/v1")),
            token,
            IDENTITY,
            handle,
            rev=lambda: store.rev,
            backoff=(0.01,),
            claim_timeout=0.2,
        )

        task = asyncio.create_task(client.run())
        try:
            yield Agent(client, store, handled, task)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110 - polls state that has nothing to await
            await asyncio.sleep(0.01)


# ---- the connection


async def test_hello_then_configure(operator: tuple[FakeOperator, TestServer], agent: Agent) -> None:
    fake, _ = operator
    fake.after_hello = [configure()]
    await until(lambda: bool(fake.hellos))
    hello = fake.hellos[0]
    assert (hello.pod_name, hello.gen, hello.trip) == (IDENTITY.pod_name, 1, TRIP.id)
    assert hello.snapshot.phase == "starting"
    assert hello.proto == [1]
    await asyncio.wait_for(agent.client.connected.wait(), 5)


async def test_configure_reaches_the_store(operator: tuple[FakeOperator, TestServer], tmp_path: Path) -> None:
    fake, server = operator
    fake.after_hello = [configure(rev=4, interval_s=120)]
    store = OneTripStore(TRIP.id)

    async def handle(message: ControlMessage) -> None:
        if isinstance(message, Configure):
            store.apply(message)

    token = tmp_path / "token"
    token.write_text(fake.token)
    async with aiohttp.ClientSession() as http:
        client = HubClient(http, str(server.make_url("/agent/v1")), token, IDENTITY, handle, backoff=(0.01,))
        task = asyncio.create_task(client.run())
        await until(lambda: store.rev == 4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert store.config.interval_seconds == 120
    assert store.config.trip(TRIP.id) == TRIP


async def test_alerts_are_kept_until_acknowledged(operator: tuple[FakeOperator, TestServer], agent: Agent) -> None:
    fake, _ = operator
    await asyncio.wait_for(agent.client.connected.wait(), 5)
    seq = agent.client.alert("cart_held", AlertParams(departures=["09:00", "11:00"], fare="Saver", price="$93.45"))
    await fake.wait_for(lambda: any(isinstance(m, Alert) for m in fake.received))
    await until(lambda: agent.client.acked(seq))
    alert = next(m for m in fake.received if isinstance(m, Alert))
    assert alert.kind == "cart_held"
    assert alert.params == AlertParams(departures=["09:00", "11:00"], fare="Saver", price="$93.45")


async def test_unacknowledged_messages_ride_along_after_a_reconnect(
    operator: tuple[FakeOperator, TestServer], agent: Agent, tmp_path: Path
) -> None:
    fake, _ = operator
    fake.ack = False
    await asyncio.wait_for(agent.client.connected.wait(), 5)
    agent.client.alert("cart_lost", AlertParams(departures=["09:00"]))
    agent.client.event("viewer_disconnected")
    await fake.wait_for(lambda: len(fake.received) == 2)

    # The kubelet rotates the token; the operator drops the connection.
    fake.token = "token-2"
    (tmp_path / "token").write_text("token-2")
    fake.drop.set()
    await until(lambda: len(fake.hellos) == 2)

    replayed = fake.hellos[1].unacked
    assert [type(m).__name__ for m in replayed] == ["Alert", "Event"]
    assert fake.hellos[1].boot == fake.hellos[0].boot  # same agent boot: the operator dedupes by (boot, seq)


async def test_a_bad_operator_message_drops_the_connection(
    operator: tuple[FakeOperator, TestServer], agent: Agent
) -> None:
    fake, _ = operator
    fake.after_hello = [b'{"type": "command", "op": "navigate", "url": "https://evil.example/"}']
    await until(lambda: fake.connections >= 2)  # it reconnected rather than act on it
    assert not [m for m in agent.handled if not isinstance(m, HelloAck)]


async def test_cart_claims(operator: tuple[FakeOperator, TestServer], agent: Agent) -> None:
    fake, _ = operator
    await asyncio.wait_for(agent.client.connected.wait(), 5)
    assert await agent.client.claim("09:00", "saver") is True
    fake.grant = False
    assert await agent.client.claim("09:00", "saver") is False
    fake.grant = None
    assert await agent.client.claim("09:00", "saver") is False  # no answer in time is a no
    claims = [m for m in fake.received if isinstance(m, CartClaim)]
    assert [(c.depart, c.fare) for c in claims] == [("09:00", "saver")] * 3
    assert not agent.client.unacked  # claims are never buffered for a later connection


async def test_no_connection_means_no_cart(tmp_path: Path) -> None:
    async def handle(message: ControlMessage) -> None:
        pass

    async with aiohttp.ClientSession() as http:
        client = HubClient(http, "http://127.0.0.1:9/agent/v1", tmp_path / "token", IDENTITY, handle)
        assert await client.claim("09:00", "saver") is False


async def test_acks_are_visible(operator: tuple[FakeOperator, TestServer], agent: Agent) -> None:
    fake, _ = operator
    fake.ack = False
    await asyncio.wait_for(agent.client.connected.wait(), 5)
    alert = Alert(**agent.client.envelope(), kind="cart_held")
    agent.client.send(alert)
    await fake.wait_for(lambda: any(isinstance(m, Alert) for m in fake.received))
    assert not agent.client.acked(alert.seq)
    fake.ack = True
    agent.client.send(Event(**agent.client.envelope(), code="cart_added"))
    await fake.wait_for(lambda: any(isinstance(m, Event) for m in fake.received))
    await until(lambda: len(agent.client.unacked) == 1)
    assert not agent.client.acked(alert.seq)


async def test_silence_is_measured(operator: tuple[FakeOperator, TestServer], agent: Agent) -> None:
    await asyncio.wait_for(agent.client.connected.wait(), 5)
    assert agent.client.silent_for() < 5


# ---- the store


def test_the_store_takes_only_its_own_trip() -> None:
    store = OneTripStore(TRIP.id)
    other = Trip(id="ffffffffffffffff", origin="SWB", destination="TSA", date=date(2026, 10, 14))
    with pytest.raises(ProtocolError, match="watches trip"):
        store.apply(configure(trip=other))


def test_stale_configures_are_ignored() -> None:
    store = OneTripStore(TRIP.id)
    assert store.apply(configure(rev=3, interval_s=120))
    assert not store.apply(configure(rev=2, interval_s=600))
    assert store.config.interval_seconds == 120


def test_the_operator_can_turn_carting_off_but_not_on() -> None:
    store = OneTripStore(TRIP.id)
    store.apply(configure(auto_cart=False))  # quiet hours, or the user's hold limit
    trip = store.config.trip(TRIP.id)
    assert trip is not None
    assert trip.auto_cart is False
    no_cart = Trip(id=TRIP.id, origin="SWB", destination="TSA", date=date(2026, 10, 14), auto_cart=False)
    store.apply(configure(rev=2, trip=no_cart, auto_cart=True))
    trip = store.config.trip(TRIP.id)
    assert trip is not None
    assert trip.auto_cart is False  # the user's choice wins


def test_the_store_notifies_the_watcher() -> None:
    store = OneTripStore(TRIP.id)
    calls: list[int] = []
    store.subscribe(lambda: calls.append(store.rev))
    store.apply(configure(rev=5))
    assert calls == [5]
    assert store.config.enabled is True
