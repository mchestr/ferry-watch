"""The alert outbox against a fake push service and a fake mailer, on real Postgres."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import aiohttp
import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import ec

from ferry_watch.models import Trip
from ferry_watch.web.messages import Messages
from ferry_watch.web.outbox import RETRY_SECONDS, Outbox, allowed_endpoint, subscription_row
from ferry_watch.web.push import Vapid, _point, b64
from tests.postgres import ROLES, Database
from tests.test_push import AS_PRIVATE, decrypt

ANN = "a" * 20
TRIP = Trip(id="3f9c0a1b2c3d4e5f", origin="SWB", destination="TSA", date=date(2026, 10, 16))


@dataclass
class FakePushService:
    status: int = 201
    received: list[tuple[dict[str, str], bytes]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.received.append((dict(request.headers), await request.read()))
        return web.Response(status=self.status)


@dataclass
class FakeMailer:
    sent: list[tuple[str, str, str]] = field(default_factory=list)
    fail: bool = False

    async def send(self, to: str, subject: str, text: str) -> None:
        if self.fail:
            raise OSError("relay down")
        self.sent.append((to, subject, text))


@dataclass
class Rig:
    outbox: Outbox
    push: FakePushService
    mailer: FakeMailer
    db: Database
    browser: ec.EllipticCurvePrivateKey
    endpoint: str
    clock: list[float]

    async def sql(self, query: str, *args: Any, user: str = "ferrywatch") -> list[asyncpg.Record]:
        conn = await self.db.connect(user)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    async def queue(self, kind: str, params: dict[str, Any] | None = None) -> None:
        await self.sql(
            "INSERT INTO notifications (user_id, trip_id, kind, params) VALUES ($1, $2, $3, $4)",
            ANN,
            TRIP.id,
            kind,
            json.dumps(params or {}),
            user=ROLES.control,
        )

    def pushed(self) -> list[dict[str, Any]]:
        return [json.loads(decrypt(body, self.browser, b"0123456789abcdef")) for _, body in self.push.received]


@pytest.fixture
async def rig(db: Database, aiohttp_server: Callable[..., Any]) -> AsyncIterator[Rig]:
    service = FakePushService()
    app = web.Application()
    app.router.add_post("/push/{id}", service.handle)
    server: TestServer = await aiohttp_server(app)
    endpoint = str(server.make_url("/push/device-1"))
    browser = ec.generate_private_key(ec.SECP256R1())
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username, email) VALUES ($1, 'ann', 'ann@example.com')", ANN)
        await owner.execute(
            "INSERT INTO trips (id, user_id, spec) VALUES ($1, $2, $3)", TRIP.id, ANN, json.dumps(TRIP.to_json())
        )
        cart = {"id": "c1", "status": "ready", "depart": "09:00", "unclaimed_deadline": 1_790_000_000.0}
        await owner.execute("INSERT INTO trip_state (trip_id, cart) VALUES ($1, $2)", TRIP.id, json.dumps(cart))
        await owner.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES ($1, $2, $3, $4)",
            ANN,
            endpoint,
            b64(_point(browser.public_key())),
            b64(b"0123456789abcdef"),
        )
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(db.dsn(ROLES.web), min_size=1, max_size=4)
    clock = [0.0]  # an offset from now, to move time forward
    mailer = FakeMailer()
    async with aiohttp.ClientSession() as http:
        outbox = Outbox(
            pool,
            http,
            Messages("https://ferry.chestr.dev", lambda: None),
            vapid=Vapid.from_raw(AS_PRIVATE, "mailto:ferry-watch@chestr.dev"),
            mailer=mailer,  # type: ignore[arg-type]
            clock=lambda: time.time() + clock[0],
        )
        try:
            yield Rig(outbox, service, mailer, db, browser, endpoint, clock)
        finally:
            await pool.close()


async def test_a_held_cart_is_pushed_and_emailed(rig: Rig) -> None:
    await rig.queue("cart_held", {"departures": ["09:00", "11:00"], "fare": "Saver"})
    assert await rig.outbox.deliver_due() == 1
    (message,) = rig.pushed()
    assert message["title"] == "Held for you: 9:00 am, 11:00 am SWB → TSA, Fri Oct 16"
    assert message["url"] == f"https://ferry.chestr.dev/c/{TRIP.id}"
    headers = rig.push.received[0][0]
    assert headers["Urgency"] == "high"
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Authorization"].startswith("vapid t=")
    assert [(to, subject) for to, subject, _ in rig.mailer.sent] == [("ann@example.com", message["title"])]
    assert (await rig.sql("SELECT sent_at FROM notifications"))[0]["sent_at"] is not None
    assert await rig.outbox.deliver_due() == 0  # sent once


async def test_a_bookable_alert_is_pushed_only(rig: Rig) -> None:
    await rig.queue("bookable", {"departures": ["09:00"]})
    await rig.outbox.deliver_due()
    assert rig.pushed()[0]["title"].startswith("Bookable: 9:00 am")
    assert rig.mailer.sent == []


async def test_email_stands_in_when_no_device_works(rig: Rig) -> None:
    rig.push.status = 410  # the browser unsubscribed
    await rig.queue("bookable", {"departures": ["09:00"]})
    await rig.outbox.deliver_due()
    assert [to for to, _, _ in rig.mailer.sent] == ["ann@example.com"]
    assert await rig.sql("SELECT * FROM push_subscriptions") == []  # forgotten


async def test_failures_are_retried_later(rig: Rig) -> None:
    rig.push.status = 500
    rig.mailer.fail = True
    await rig.queue("bookable", {"departures": ["09:00"]})
    await rig.outbox.deliver_due()
    (row,) = await rig.sql("SELECT attempts, sent_at, last_error FROM notifications")
    assert (row["attempts"], row["sent_at"], row["last_error"]) == (1, None, "OSError")
    assert await rig.outbox.deliver_due() == 0  # not before its retry time
    rig.push.status, rig.mailer.fail = 201, False
    rig.clock[0] += RETRY_SECONDS[0] + 1
    assert await rig.outbox.deliver_due() == 1
    assert (await rig.sql("SELECT sent_at FROM notifications"))[0]["sent_at"] is not None


async def test_an_invitation_goes_by_email(rig: Rig) -> None:
    await rig.sql(
        "INSERT INTO notifications (to_email, kind, params) VALUES ('cat@example.com', 'invited', '{}')",
        user=ROLES.web,
    )
    await rig.outbox.deliver_due()
    assert [(to, subject) for to, subject, _ in rig.mailer.sent] == [
        ("cat@example.com", "You're invited to Ferry Watch")
    ]
    assert rig.push.received == []


async def test_did_you_book_it(rig: Rig) -> None:
    await rig.queue("booked_it", {"departures": ["09:00"]})
    await rig.outbox.deliver_due()
    assert rig.pushed()[0]["title"] == "Did you book it?"
    assert len(rig.mailer.sent) == 1


def test_only_real_push_services() -> None:
    assert allowed_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert allowed_endpoint("https://web.push.apple.com/QOt1")
    assert allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert not allowed_endpoint("http://fcm.googleapis.com/fcm/send/abc")
    assert not allowed_endpoint("https://fcm.googleapis.com.evil.example/x")
    assert not allowed_endpoint("https://169.254.169.254/latest/meta-data")
    assert not allowed_endpoint("https://fcm.googleapis.com:8443/x")
    with pytest.raises(ValueError, match="push service"):
        subscription_row({"endpoint": "https://evil.example/x", "keys": {"p256dh": "a", "auth": "b"}})
