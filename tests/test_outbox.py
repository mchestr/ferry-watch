"""The alert outbox against a fake push service and a fake mailer, on real Postgres."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import aiohttp
import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import ec

from tests.fakes import FakeHttpProvider, FakeSpec, fake_registry
from tests.postgres import ROLES, Database
from tests.test_push import AS_PRIVATE, decrypt
from waitlist.bcferries.terminals import Terminals
from waitlist.models import Trip
from waitlist.providers import Registry
from waitlist.providers.bcferries import META as BCFERRIES
from waitlist.web import admin
from waitlist.web.messages import Messages
from waitlist.web.outbox import RETRY_SECONDS, Outbox, allowed_endpoint, subscription_row
from waitlist.web.push import Vapid, _point, b64

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

    async def sql(self, query: str, *args: Any, user: str = "waitlist") -> list[asyncpg.Record]:
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
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'bcferries', $3)",
            TRIP.id,
            ANN,
            json.dumps(TRIP.to_json()),
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
            Messages("https://waitlist.chestr.dev", lambda: None),
            vapid=Vapid.from_raw(AS_PRIVATE, "mailto:waitlist@chestr.dev"),
            mailer=mailer,  # type: ignore[arg-type]
            clock=lambda: time.time() + clock[0],
            registry=fake_registry(),  # Buntzen's meta, FakeHttpProvider's notices
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
    assert message["body"].startswith("Check out by ")  # not "as a guest": signing in is up to them
    assert message["url"] == f"https://waitlist.chestr.dev/c/{TRIP.id}"
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
    await rig.queue("bookable", {"departures": ["09:00"], "held_back": "quiet_hours"})
    await rig.outbox.deliver_due()
    assert [to for to, _, _ in rig.mailer.sent] == ["ann@example.com"]
    (_, _, text) = rig.mailer.sent[0]
    assert text.startswith("It's in your quiet hours, so we didn't hold it. Open Waitlist to hold it now.\n\n")
    assert f"https://waitlist.chestr.dev/c/{TRIP.id}\n" in text  # the trip's page
    assert "bcferries.com" not in text  # no terminal list here: no second link, rather than a wrong one
    assert await rig.sql("SELECT * FROM push_subscriptions") == []  # forgotten


def test_unheld_alerts_say_why_and_link_to_the_trip(terminals: Terminals) -> None:
    messages = Messages("https://waitlist.chestr.dev", lambda: terminals)
    trip = Trip(id=TRIP.id, origin="NAN", destination="HSB", date=date(2026, 10, 16))
    page = f"https://waitlist.chestr.dev/c/{TRIP.id}"
    quiet = messages.render("bookable", {"departures": ["09:00"], "held_back": "quiet_hours"}, trip)
    assert quiet is not None
    assert (quiet.url, quiet.body) == (
        page,
        "It's in your quiet hours, so we didn't hold it. Open Waitlist to hold it now.",
    )
    assert quiet.also is not None
    assert quiet.also[1].startswith("https://www.bcferries.com/?sailing=true&departureLocation=NAN")
    older = messages.render("bookable", {"departures": ["09:00"]}, trip)  # queued before held_back existed
    assert older is not None
    assert older.body == "We couldn't hold it this time. Open Waitlist to try again."
    failed = messages.render("cart_failed", {"departures": ["09:00"], "error_class": "not_bookable"}, trip)
    assert failed is not None
    assert failed.url == page
    assert failed.body == "BC Ferries didn't let us hold it (it may have just sold out). Open Waitlist to try again."
    assert failed.push() == {"title": failed.title, "body": failed.body, "url": page}  # a push opens one link


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
    assert [(to, subject) for to, subject, _ in rig.mailer.sent] == [("cat@example.com", "You're invited to Waitlist")]
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


# ---- other providers (docs/waitlist/design.md §6.5)

WATCH = "b" * 16


async def http_watch(rig: Rig, spec: dict[str, Any] | None = None) -> None:
    await rig.sql(
        "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', $3)",
        WATCH,
        ANN,
        json.dumps(spec if spec is not None else {"date": "2027-07-01"}),
    )


async def queue_for(rig: Rig, kind: str, params: dict[str, Any], trip_id: str | None = WATCH) -> None:
    await rig.sql(
        "INSERT INTO notifications (user_id, trip_id, kind, params) VALUES ($1, $2, $3, $4)",
        ANN,
        trip_id,
        kind,
        json.dumps(params),
        user=ROLES.control,
    )


async def unrenderable(rig: Rig) -> list[tuple[Any, ...]]:
    rows = await rig.sql("SELECT kind, sent_at, attempts, last_error FROM notifications")
    return [tuple(r) for r in rows]


async def test_an_opening_is_the_providers_notice_and_opens_the_watchs_page(rig: Rig) -> None:
    await http_watch(rig)
    await queue_for(rig, "opening", {"options": ["a"], "uncertain": []})
    assert await rig.outbox.deliver_due() == 1
    (message,) = rig.pushed()
    assert message == {
        "title": "Fake: a open for Thu Jul 1",
        "body": "Book it now.",
        "url": f"https://waitlist.chestr.dev/w/{WATCH}",
    }
    headers = rig.push.received[0][0]
    assert (headers["Urgency"], headers["TTL"], headers["Topic"]) == ("high", "900", f"opening-{WATCH}")
    assert rig.mailer.sent == []  # a device took it: no email


async def test_an_openings_email_carries_the_booking_link(rig: Rig) -> None:
    await http_watch(rig)
    await rig.sql("DELETE FROM push_subscriptions")
    await queue_for(rig, "opening", {"options": ["b"], "uncertain": []})
    await rig.outbox.deliver_due()
    ((to, subject, text),) = rig.mailer.sent
    assert (to, subject) == ("ann@example.com", "Fake: b open for Thu Jul 1")
    assert f"https://waitlist.chestr.dev/w/{WATCH}" in text
    assert "Book it:\nhttps://yodelportal.com/buntzen-lake/Half-Day-Pass" in text


class BadLinks(FakeHttpProvider):
    def __init__(self, *, url: str | None = None, also: str | None = None, fail: bool = False) -> None:
        self._url, self._also, self._fail = url, also, fail

    def render(self, kind: str, params: Any, spec: FakeSpec, *, watch_url: str) -> Any:
        if self._fail:
            raise RuntimeError("a bug in a provider")
        notice = super().render(kind, params, spec, watch_url=watch_url)
        assert notice is not None
        return replace(notice, url=self._url or notice.url, also=("Book", self._also) if self._also else notice.also)


@pytest.mark.parametrize(
    ("setup", "registry"),
    [
        ("unknown", Registry(metas=(BCFERRIES,))),  # an older build: no buntzen
        ("bad-spec", None),
        ("nothing-to-say", None),
        ("bad-url", fake_registry(BadLinks(url="https://evil.example/"))),
        ("bad-also", fake_registry(BadLinks(also="https://evil.example/buntzen-lake/"))),
        ("render-raises", fake_registry(BadLinks(fail=True))),
    ],
)
async def test_an_unrenderable_notification_is_never_silent(rig: Rig, setup: str, registry: Registry | None) -> None:
    await http_watch(rig, {"date": "2027-07-01", "colour": "blue"} if setup == "bad-spec" else None)
    params = {"options": [], "uncertain": []} if setup == "nothing-to-say" else {"options": ["a"], "uncertain": []}
    await queue_for(rig, "opening", params)
    if registry is not None:
        rig.outbox._registry = registry
    await rig.outbox.deliver_due()
    assert rig.pushed() == []
    assert rig.mailer.sent == []
    assert await unrenderable(rig) == [("opening", None, 6, "Unrenderable")]
    assert await rig.outbox.deliver_due() == 0  # never tried again
    attention = await admin._attention(rig.outbox._pool)
    assert [(a["kind"], a["detail"]) for a in attention] == [
        ("delivery_failing", "Not delivered after 6 tries: opening (Unrenderable)")
    ]


async def test_a_bc_ferries_trip_that_wont_load_is_unrenderable_too(rig: Rig) -> None:
    await rig.sql('UPDATE trips SET spec = \'{"from": "SWB"}\' WHERE id = $1', TRIP.id)
    await rig.queue("bookable", {"departures": ["09:00"]})
    await rig.outbox.deliver_due()
    assert rig.pushed() == []
    assert await unrenderable(rig) == [("bookable", None, 6, "Unrenderable")]


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("site_changed", "Buntzen Lake's answers can't be read; its checks are failing."),
        ("checker_down", "waitlist-fetcher isn't answering; Buntzen Lake checks are on hold."),
    ],
)
async def test_a_providers_trouble_goes_to_admins_only(rig: Rig, kind: str, body: str) -> None:
    await queue_for(rig, kind, {"provider": "buntzen", "since": 1.0}, trip_id=None)
    await rig.outbox.deliver_due()
    assert rig.pushed() == []  # Ann isn't an admin: nothing for her, though the row names her
    (row,) = await rig.sql("SELECT sent_at FROM notifications")
    assert row["sent_at"] is not None
    await rig.sql("UPDATE users SET is_admin = true WHERE id = $1", ANN)
    await queue_for(rig, kind, {"provider": "buntzen", "since": 1.0}, trip_id=None)
    await rig.outbox.deliver_due()
    assert rig.pushed() == [{"title": f"Admin: {kind.replace('_', ' ')}", "body": body, "url": "/admin"}]
    assert rig.mailer.sent == []


def test_an_invitation_says_waitlist_watches_reservations() -> None:
    message = Messages("https://waitlist.chestr.dev", lambda: None).render("invited", {}, None)
    assert message is not None
    assert (message.title, message.body) == (
        "You're invited to Waitlist",
        "Sign in with your account to start watching reservations for you.",
    )
