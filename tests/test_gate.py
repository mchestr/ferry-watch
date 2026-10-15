from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ferry_watch.agent.cast_protocol import CloseCode
from ferry_watch.agent.gate import MAX_FAILURES_PER_MINUTE, Gate, GateSettings, ViewerKind
from ferry_watch.tickets import mint
from tests.cast1 import load as load_pack

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


# ---- two kinds of viewer: the classic (binary) one and the streamed one (fw.cast1)

PACK_OFFERS = load_pack("messages")["offers"]
PACK_NONCE = "Q2hlY2tvdXROb25jZUZvclRlc3Rz"  # the pack's offers use it


def cast_protocols(nonce: str | None = NONCE, tkt: str | None = None) -> list[str]:
    return ["fw.cast1", *protocols(nonce, tkt)[1:]]


@dataclass
class FakeSession:
    """A stream session: it notes what happens to it, echoes text, and answers "verdict" with its verdict."""

    ws: web.WebSocketResponse
    verdict: Callable[[], CloseCode | None]
    log: list[str]
    name: str
    teardown_s: float = 0.0
    received: list[str] = field(default_factory=list)

    async def run(self) -> None:
        self.log.append(f"{self.name} runs")
        async for frame in self.ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                break
            if frame.data == "fail":
                raise RuntimeError("the page said something")
            self.received.append(frame.data)
            await self.ws.send_str(str(self.verdict()) if frame.data == "verdict" else frame.data)

    async def close(self) -> None:
        self.log.append(f"{self.name} tears down")
        await asyncio.sleep(self.teardown_s)
        self.log.append(f"{self.name} torn down")


@dataclass
class Stand:
    """The screen and the dialog keeper, as the gate sees them: they note when they're used."""

    log: list[str]

    async def restore(self) -> None:
        self.log.append("screen restored")

    async def keep(self) -> None:
        self.log.append("dialogs kept")

    async def release(self) -> None:
        self.log.append("dialogs released")


@dataclass
class CastRig:
    gate: Gate
    client: TestClient
    xvnc: FakeXvnc
    viewers: list[bool]
    cart: list[str | None]
    log: list[str]
    sessions: list[FakeSession]
    allowed: list[bool]
    teardown_s: list[float]

    async def connect(self, offered: list[str]) -> aiohttp.ClientWebSocketResponse:
        return await self.client.ws_connect("/ws", protocols=offered, origin=ORIGIN)

    async def refused(self, offered: list[str]) -> int:
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await self.connect(offered)
        return e.value.status

    async def stream(self, offered: list[str]) -> aiohttp.ClientWebSocketResponse:
        """A stream that has started its session."""
        count = len(self.sessions)
        ws = await self.connect(offered)
        assert ws.protocol == "fw.cast1"
        await until(lambda: len(self.sessions) > count and f"{self.sessions[-1].name} runs" in self.log)
        return ws

    def clear(self) -> None:
        self.log.clear()


def make_cast_rig(aiohttp_client: Callable[..., Any], tmp_path: Path, **gate_kw: Any) -> Callable[[], Any]:
    @contextlib.asynccontextmanager
    async def rig() -> AsyncIterator[CastRig]:
        xvnc = FakeXvnc(tmp_path / "rfb.sock")
        await xvnc.start()
        viewers: list[bool] = []
        cart: list[str | None] = ["cart1"]
        log: list[str] = []
        sessions: list[FakeSession] = []
        allowed = [True]
        teardown_s = [0.0]

        def verdict(cart_id: str, kind: ViewerKind) -> CloseCode | None:
            if kind == "cast" and not allowed[0]:
                return CloseCode.WITHDRAWN
            return None if cart[0] == cart_id else CloseCode.ENDED

        def cast(ws: web.WebSocketResponse, session_verdict: Callable[[], CloseCode | None]) -> FakeSession:
            session = FakeSession(ws, session_verdict, log, f"session {len(sessions) + 1}", teardown_s[0])
            sessions.append(session)
            return session

        stand = Stand(log)
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
            cast=cast,
            verdict=verdict,
            screen=stand,
            dialogs=stand,
            **gate_kw,
        )
        client: TestClient = await aiohttp_client(gate.app())
        try:
            yield CastRig(gate, client, xvnc, viewers, cart, log, sessions, allowed, teardown_s)
        finally:
            await gate.close("test over")
            await client.close()
            await xvnc.stop()

    return rig


@pytest.fixture
async def cast_rig(aiohttp_client: Callable[..., Any], tmp_path: Path) -> AsyncIterator[CastRig]:
    async with make_cast_rig(aiohttp_client, tmp_path)() as rig:
        yield rig


async def closed_with(ws: aiohttp.ClientWebSocketResponse) -> int | None:
    """Read until the agent closes the socket; its close code."""
    async with asyncio.timeout(5):
        while True:
            frame = await ws.receive()
            if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                return ws.close_code


@pytest.mark.parametrize("case", [pytest.param(c, id=c["id"]) for c in PACK_OFFERS])
async def test_the_gate_admits_exactly_one_kind(cast_rig: CastRig, case: dict[str, Any]) -> None:
    """The conformance pack's offers (PROTOCOL §2.2): with the device's nonce already holding the lock."""
    first = await cast_rig.connect(protocols(nonce=PACK_NONCE, tkt=ticket()))
    await greeted(first)
    await first.close()
    await until(lambda: cast_rig.viewers == [True, False])
    cast_rig.allowed[0] = case["cast_allowed"]
    if isinstance(case["result"], int):
        assert await cast_rig.refused(case["offered"]) == case["result"]
        return
    ws = await cast_rig.connect(case["offered"])
    assert ws.protocol == case["result"]
    await ws.close()


async def test_a_withdrawn_stream_is_refused_but_never_counts_as_a_failure(cast_rig: CastRig) -> None:
    cast_rig.allowed[0] = False
    for _ in range(MAX_FAILURES_PER_MINUTE + 2):
        assert await cast_rig.refused(cast_protocols(tkt=ticket())) == 400
    ws = await cast_rig.connect(protocols(tkt=ticket()))  # the classic viewer's first connect isn't held up
    assert await greeted(ws) == GREETING
    await ws.close()


async def test_without_a_stream_factory_fw_cast1_is_refused(rig: Rig) -> None:
    assert await rig.refused(cast_protocols(tkt=ticket())) == 400
    ws = await rig.connect(protocols(tkt=ticket()))  # and it wasn't counted
    assert await greeted(ws) == GREETING
    await ws.close()


async def test_a_stream_connects_reconnects_and_hands_over_to_the_classic_viewer(cast_rig: CastRig) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    await stream.send_str("hello")
    assert (await asyncio.wait_for(stream.receive(), 5)).data == "hello"
    assert cast_rig.viewers == [True]

    again = await cast_rig.stream(cast_protocols())  # the same device, by its nonce: no ticket
    assert await closed_with(stream) == CloseCode.REPLACED
    classic = await cast_rig.connect(protocols())  # "Use the classic viewer": the same device again
    assert await greeted(classic) == GREETING
    assert await closed_with(again) == CloseCode.REPLACED
    assert cast_rig.viewers == [True, False, True, False, True]
    back = await cast_rig.stream(cast_protocols())  # and the reverse
    assert await closed_with(classic) == 1000
    await back.close()
    await until(lambda: cast_rig.viewers[-1] is False)
    assert cast_rig.viewers == [True, False, True, False, True, False, True, False]


async def test_whichever_kind_took_the_lock_no_ticket_gets_in_after_it(cast_rig: CastRig) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    assert await cast_rig.refused(protocols(nonce="m" * 32, tkt=ticket())) == 403
    assert await cast_rig.refused(cast_protocols(nonce="m" * 32, tkt=ticket())) == 403
    await stream.close()


async def test_a_stream_message_over_4096_bytes_closes_the_socket(cast_rig: CastRig) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    await stream.send_str(" " * 4096)
    assert (await asyncio.wait_for(stream.receive(), 5)).data == " " * 4096
    await stream.send_str(" " * 4097)
    assert await closed_with(stream) == 1009
    await until(lambda: cast_rig.viewers == [True, False])


@pytest.mark.parametrize(
    "code", [CloseCode.ENDED, CloseCode.REVOKED, CloseCode.DRAINING, CloseCode.WITHDRAWN], ids=lambda c: str(c.value)
)
async def test_the_agent_closes_a_stream_with_its_reason(cast_rig: CastRig, code: CloseCode) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    cast_rig.clear()
    await cast_rig.gate.close("why", code)
    assert await closed_with(stream) == code
    released = ["dialogs released"] if code is not CloseCode.WITHDRAWN else []  # a withdrawn stream goes classic
    assert cast_rig.log == ["session 1 tears down", "session 1 torn down", *released]
    assert not cast_rig.gate.viewer_connected


async def test_a_withdrawal_leaves_a_classic_viewer_alone(cast_rig: CastRig) -> None:
    classic = await cast_rig.connect(protocols(tkt=ticket()))
    await greeted(classic)
    await cast_rig.gate.close("streamed viewer withdrawn", CloseCode.WITHDRAWN)
    assert cast_rig.gate.viewer_connected
    await classic.send_bytes(b"\x03\x00")
    assert (await asyncio.wait_for(classic.receive(), 5)).data == b"\x03\x00"
    await cast_rig.gate.close("hold ended")
    assert await closed_with(classic) == 1000  # a classic viewer gets 0.6.4's close, whatever the reason


async def test_the_next_viewer_waits_for_a_slow_teardown(cast_rig: CastRig) -> None:
    cast_rig.teardown_s[0] = 0.5
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    cast_rig.clear()
    started = asyncio.get_running_loop().time()
    classic = await cast_rig.connect(protocols())
    assert await greeted(classic) == GREETING
    assert asyncio.get_running_loop().time() - started >= 0.5
    assert cast_rig.log[:3] == ["session 1 tears down", "session 1 torn down", "dialogs kept"]
    assert await closed_with(stream) == CloseCode.REPLACED
    await classic.close()


async def test_a_teardown_finishes_even_when_its_task_is_cancelled_again(cast_rig: CastRig) -> None:
    cast_rig.teardown_s[0] = 0.5
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    closing = asyncio.create_task(cast_rig.gate.close("hold ended"))
    await until(lambda: "session 1 tears down" in cast_rig.log)
    viewer = cast_rig.gate._viewer
    assert viewer is not None
    assert viewer.bridge is not None
    for _ in range(3):
        viewer.bridge.cancel()  # e.g. the server shutting down meanwhile
        await asyncio.sleep(0.05)
    still_the_viewer = cast_rig.gate.viewer_connected
    assert still_the_viewer  # until its teardown is done
    await closing
    assert cast_rig.log[-3:] == ["session 1 tears down", "session 1 torn down", "dialogs released"]
    assert not cast_rig.gate.viewer_connected
    assert await closed_with(stream) == CloseCode.ENDED


async def test_the_connection_cap(aiohttp_client: Callable[..., Any], tmp_path: Path) -> None:
    async with make_cast_rig(aiohttp_client, tmp_path, max_connection_seconds=0.3)() as rig:
        stream = await rig.stream(cast_protocols(tkt=ticket()))
        assert await closed_with(stream) == CloseCode.TIME_CAP
        assert rig.log[-2:] == ["session 1 tears down", "session 1 torn down"]
        classic = await rig.connect(protocols())
        await greeted(classic)
        assert await closed_with(classic) == 1000  # the cap holds for the classic viewer too
        await until(lambda: rig.viewers == [True, False, True, False])


async def test_a_failing_session_closes_4500_and_logs_no_page_text(
    cast_rig: CastRig, caplog: pytest.LogCaptureFixture
) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    await stream.send_str("fail")
    assert await closed_with(stream) == CloseCode.INTERNAL
    assert "RuntimeError" in caplog.text
    assert "the page said something" not in caplog.text
    assert cast_rig.log[-1] == "session 1 torn down"


async def test_the_session_asks_the_operators_verdict(cast_rig: CastRig) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    await stream.send_str("verdict")
    assert (await asyncio.wait_for(stream.receive(), 5)).data == "None"
    cast_rig.allowed[0] = False
    await stream.send_str("verdict")
    assert (await asyncio.wait_for(stream.receive(), 5)).data == str(CloseCode.WITHDRAWN)
    cast_rig.allowed[0] = True
    cast_rig.cart[0] = "cart2"
    await stream.send_str("verdict")
    assert (await asyncio.wait_for(stream.receive(), 5)).data == str(CloseCode.ENDED)
    await stream.close()


async def test_stream_sessions_start_at_most_once_a_second(cast_rig: CastRig) -> None:
    loop = asyncio.get_running_loop()
    first = await cast_rig.stream(cast_protocols(tkt=ticket()))
    started = loop.time()
    await asyncio.sleep(0.2)
    second = await cast_rig.stream(cast_protocols())  # a reconnect loop: it waits, nothing is refused
    assert loop.time() - started >= 1.0
    assert await closed_with(first) == CloseCode.REPLACED
    await second.close()


async def test_the_screen_is_restored_only_before_a_classic_viewer(cast_rig: CastRig) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    again = await cast_rig.stream(cast_protocols())
    assert await closed_with(stream) == CloseCode.REPLACED
    await again.close()
    await until(lambda: cast_rig.viewers[-1] is False)
    await cast_rig.gate.close("streamed viewer withdrawn", CloseCode.WITHDRAWN)
    assert "screen restored" not in cast_rig.log  # not at a stream's teardown, a reconnect or a close
    classic = await cast_rig.connect(protocols())
    await greeted(classic)
    assert cast_rig.log[-2:] == ["dialogs kept", "screen restored"]
    assert cast_rig.log.count("dialogs kept") == 3  # every viewer's admission
    await cast_rig.gate.close("hold ended")
    assert cast_rig.log[-1] == "dialogs released"
    assert cast_rig.log.count("screen restored") == 1


async def test_a_stream_admitted_while_the_hold_ends_is_turned_away(cast_rig: CastRig) -> None:
    stream = await cast_rig.stream(cast_protocols(tkt=ticket()))
    waiting = asyncio.create_task(cast_rig.refused(cast_protocols()))
    await asyncio.sleep(0.1)  # the reconnect waits for its turn (pacing) ...
    cast_rig.cart[0] = None  # ... and the hold ends meanwhile
    assert await waiting == 409
    await stream.close()
