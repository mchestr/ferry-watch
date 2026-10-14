from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ferry_watch.agent.gate import MAX_FAILURES_PER_MINUTE, Gate, GateSettings
from ferry_watch.tickets import mint

NOW = 1_790_000_000.0
KEY = Ed25519PrivateKey.generate()
ORIGIN = "https://ferry-checkout.chestr.dev"
OWNER = "user-7"
TRIP = "3f9c0a1b2c3d4e5f"
POD = f"fw-{TRIP}-g1"
NONCE = "n" * 32
GREETING = b"RFB 003.008\n"


def ticket(**kw: Any) -> str:
    binding = {"sub": OWNER, "trip": TRIP, "pod": POD, "cart_id": "cart1"} | kw
    return mint(KEY, "k1", now=NOW, **binding)


def protocols(nonce: str | None = NONCE, tkt: str | None = None) -> list[str]:
    offered = ["binary"]
    if tkt is not None:
        offered.append(f"fw.t1.{tkt}")
    if nonce is not None:
        offered.append(f"fw.n1.{nonce}")
    return offered


@dataclass
class FakeXvnc:
    """Greets each connection like an RFB server, then echoes what it's sent."""

    path: Path
    connections: int = 0
    open: int = 0
    server: asyncio.Server | None = None
    writers: list[asyncio.StreamWriter] = field(default_factory=list)

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=self.path)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.open += 1
        self.writers.append(writer)
        try:
            writer.write(GREETING)
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.open -= 1
            writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            for writer in self.writers:
                writer.close()
            await self.server.wait_closed()


@dataclass
class Rig:
    gate: Gate
    client: TestClient
    xvnc: FakeXvnc
    viewers: list[bool]
    cart: list[str | None]

    async def connect(self, offered: list[str], *, origin: str = ORIGIN) -> aiohttp.ClientWebSocketResponse:
        return await self.client.ws_connect("/ws", protocols=offered, origin=origin)

    async def refused(self, offered: list[str], *, origin: str = ORIGIN) -> int:
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await self.connect(offered, origin=origin)
        return e.value.status


@pytest.fixture
async def rig(aiohttp_client: Callable[..., Any], tmp_path: Path) -> AsyncIterator[Rig]:
    xvnc = FakeXvnc(tmp_path / "rfb.sock")
    await xvnc.start()
    viewers: list[bool] = []
    cart: list[str | None] = ["cart1"]
    gate = Gate(
        GateSettings(
            origin=ORIGIN,
            owner=OWNER,
            trip_id=TRIP,
            pod_name=POD,
            keys={"k1": KEY.public_key()},
            rfb_socket=xvnc.path,
        ),
        checkout_cart=lambda: cart[0],
        viewer_changed=viewers.append,
        clock=lambda: NOW + 1,
    )
    client: TestClient = await aiohttp_client(gate.app())
    try:
        yield Rig(gate, client, xvnc, viewers, cart)
    finally:
        await gate.close("test over")
        await client.close()
        await xvnc.stop()


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110 - polls state that has nothing to await
            await asyncio.sleep(0.01)


async def greeted(ws: aiohttp.ClientWebSocketResponse) -> bytes:
    data = b""
    while len(data) < len(GREETING):
        frame = await asyncio.wait_for(ws.receive(), 5)
        assert frame.type == aiohttp.WSMsgType.BINARY
        data += frame.data
    return data


# ---- getting in


async def test_the_owner_gets_the_screen(rig: Rig) -> None:
    ws = await rig.connect(protocols(tkt=ticket()))
    assert ws.protocol == "binary"
    assert await greeted(ws) == GREETING
    await ws.send_bytes(b"\x03\x00")
    frame = await asyncio.wait_for(ws.receive(), 5)
    assert frame.data == b"\x03\x00"
    assert rig.viewers == [True]
    assert rig.gate.viewer_connected
    await ws.close()
    await until(lambda: rig.viewers == [True, False])
    assert rig.gate.viewer_seconds() > 0
    await until(lambda: rig.xvnc.open == 0)


async def test_page_script_inside_the_pod_cannot_get_in(rig: Rig) -> None:
    assert await rig.refused(protocols(tkt=ticket()), origin="https://www.bcferries.com") == 403
    assert rig.xvnc.connections == 0


async def test_nothing_without_a_held_cart(rig: Rig) -> None:
    rig.cart[0] = None
    assert await rig.refused(protocols(tkt=ticket())) == 409


@pytest.mark.parametrize(
    "binding",
    [{"sub": "user-8"}, {"trip": "ffffffffffffffff"}, {"pod": f"fw-{TRIP}-g2"}, {"cart_id": "cart2"}],
)
async def test_a_ticket_for_anything_else_is_refused(rig: Rig, binding: dict[str, str]) -> None:
    assert await rig.refused(protocols(tkt=ticket(**binding))) == 403
    assert rig.xvnc.connections == 0


async def test_an_expired_or_forged_ticket_is_refused(rig: Rig) -> None:
    expired = mint(KEY, "k1", now=NOW - 120, sub=OWNER, trip=TRIP, pod=POD, cart_id="cart1")
    forged = mint(Ed25519PrivateKey.generate(), "k1", now=NOW, sub=OWNER, trip=TRIP, pod=POD, cart_id="cart1")
    assert await rig.refused(protocols(tkt=expired)) == 403
    assert await rig.refused(protocols(tkt=forged)) == 403
    assert await rig.refused(protocols()) == 403  # no ticket at all
    assert await rig.refused(protocols(nonce=None, tkt=ticket())) == 403  # no nonce
    assert await rig.refused(protocols(nonce="short", tkt=ticket())) == 403


async def test_only_websocket_upgrades_on_one_path(rig: Rig) -> None:
    assert (await rig.client.get("/")).status == 404
    assert (await rig.client.get("/vnc.html")).status == 404
    response = await rig.client.get("/ws", headers={"Origin": ORIGIN})
    assert response.status == 400
    assert await response.text() == "400\n"  # no reason is ever sent


async def test_the_binary_subprotocol_is_required(rig: Rig) -> None:
    offered = [p for p in protocols(tkt=ticket()) if p != "binary"]
    assert await rig.refused(offered) == 400


# ---- one viewer, one device


async def test_a_second_ticket_cannot_join_a_claimed_checkout(rig: Rig) -> None:
    ws = await rig.connect(protocols(tkt=ticket()))
    await greeted(ws)
    assert await rig.refused(protocols(nonce="m" * 32, tkt=ticket())) == 403
    assert rig.viewers == [True]
    assert not ws.closed


async def test_the_same_device_reconnects_by_its_nonce(rig: Rig) -> None:
    first = await rig.connect(protocols(tkt=ticket()))
    await greeted(first)
    # A network change: the old socket looks open, the device comes back without a ticket.
    second = await rig.connect(protocols())
    assert await greeted(second) == GREETING
    frame = await asyncio.wait_for(first.receive(), 5)
    assert frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
    assert rig.viewers == [True, False, True]
    await until(lambda: rig.xvnc.open == 1)  # the stale Xvnc connection is gone too
    await second.close()


async def test_a_new_cart_needs_a_new_ticket(rig: Rig) -> None:
    ws = await rig.connect(protocols(tkt=ticket()))
    await greeted(ws)
    await ws.close()
    await until(lambda: rig.viewers == [True, False])
    rig.cart[0] = "cart2"
    assert await rig.refused(protocols()) == 403  # the old nonce means nothing for this cart
    ws = await rig.connect(protocols(tkt=ticket(cart_id="cart2")))
    assert await greeted(ws) == GREETING
    await ws.close()


async def test_failed_attempts_are_limited_but_never_lock_out_the_owner(rig: Rig) -> None:
    ws = await rig.connect(protocols(tkt=ticket()))
    await greeted(ws)
    for _ in range(MAX_FAILURES_PER_MINUTE):
        assert await rig.refused(protocols(nonce="m" * 32)) == 403
    rig.cart[0] = "cart2"  # with the limit reached, even a good first ticket waits
    assert await rig.refused(protocols(tkt=ticket(cart_id="cart2"))) == 429
    rig.cart[0] = "cart1"
    again = await rig.connect(protocols())  # but the device that holds the checkout gets back in
    assert await greeted(again) == GREETING
    await again.close()


# ---- closing


async def test_the_agent_closes_the_checkout(rig: Rig) -> None:
    ws = await rig.connect(protocols(tkt=ticket()))
    await greeted(ws)
    rig.cart[0] = None  # released
    await rig.gate.close("hold ended")
    frame = await asyncio.wait_for(ws.receive(), 5)
    assert frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    assert rig.viewers == [True, False]
    assert not rig.gate.viewer_connected
    assert await rig.refused(protocols()) == 409


async def test_text_frames_end_the_session(rig: Rig) -> None:
    ws = await rig.connect(protocols(tkt=ticket()))
    await greeted(ws)
    await ws.send_str("hello")
    await until(lambda: rig.viewers == [True, False])
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ws.receive(), 5)


async def test_xvnc_unreachable(rig: Rig) -> None:
    await rig.xvnc.stop()
    rig.xvnc.path.unlink()
    assert await rig.refused(protocols(tkt=ticket())) == 502
    assert rig.viewers == []
    again = await rig.refused(protocols())  # the claim stands: the same device can retry
    assert again == 502
