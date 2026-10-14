"""The alert outbox: ferry-web is the only sender (docs/multi-tenant/design-reference.md §6.7).

ferry-control and ferry-web queue rows in `notifications`; this worker sends them, by Web Push to each of
the user's devices and by email for the kinds that matter most (or when a user has no working device).
Several replicas can run: each claims rows with SKIP LOCKED. Failures are retried at 10, 30, 60, 120 and
300 s, then given up; a device the push service says is gone is forgotten.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import smtplib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import asyncpg

from ferry_watch.control.store import USER_CHANNEL
from ferry_watch.models import Trip, ValidationError

from .messages import ADMIN_KINDS, EMAIL_KINDS, Message, Messages
from .push import PushGone, Subscription, Vapid, send

log = logging.getLogger(__name__)

RETRY_SECONDS = (10, 30, 60, 120, 300)
SWEEP_SECONDS = 30.0
BATCH = 20
LEASE_SECONDS = 120  # a claimed row is someone else's to retry after this
GIVE_UP_AFTER = timedelta(days=1)
MAX_PUSH_FAILURES = 5
MAX_ENDPOINT = 1024
MAX_KEY = 200


class Undelivered(Exception):
    """No channel took the message (no working device and no email): try again later."""


@dataclass(frozen=True, slots=True)
class Mailer:
    """Plain SMTP to the cluster's relay, which does the authenticated sending."""

    host: str
    port: int
    sender: str

    async def send(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(text)

        def deliver() -> None:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                smtp.send_message(message)

        await asyncio.to_thread(deliver)


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class Outbox:
    def __init__(
        self,
        pool: asyncpg.Pool,
        http: aiohttp.ClientSession,
        messages: Messages,
        *,
        vapid: Vapid | None,
        mailer: Mailer | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._pool = pool
        self._http = http
        self._messages = messages
        self._vapid = vapid
        self._mailer = mailer
        self._clock = clock
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        """Send what's due, whenever something is queued (NOTIFY) and every 30 s anyway."""
        async with self._pool.acquire() as conn:

            def queued(*_: object) -> None:
                self._wake.set()

            await conn.add_listener(USER_CHANNEL, queued)
            try:
                while True:
                    try:
                        await self.deliver_due()
                    except (asyncpg.PostgresError, OSError):
                        log.exception("Outbox pass failed")
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), SWEEP_SECONDS)
                    self._wake.clear()
            finally:
                with contextlib.suppress(asyncpg.PostgresError, OSError):
                    await conn.remove_listener(USER_CHANNEL, queued)

    async def deliver_due(self) -> int:
        """Send every notification that's due now. Returns how many were attempted."""
        now = self._clock()
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT n.id, n.user_id, n.to_email, n.trip_id, n.kind, n.params, n.attempts, u.email,"
                " t.spec, s.cart FROM notifications n LEFT JOIN users u ON u.id = n.user_id"
                " LEFT JOIN trips t ON t.id = n.trip_id LEFT JOIN trip_state s ON s.trip_id = n.trip_id"
                " WHERE n.sent_at IS NULL AND n.next_attempt_at <= $1 AND n.attempts < $2 AND n.created_at > $3"
                " ORDER BY n.created_at LIMIT $4 FOR UPDATE OF n SKIP LOCKED",
                _ts(now),
                len(RETRY_SECONDS) + 1,
                _ts(now - GIVE_UP_AFTER.total_seconds()),
                BATCH,
            )
            if rows:
                await conn.execute(
                    "UPDATE notifications SET next_attempt_at = $2 WHERE id = ANY($1::bigint[])",
                    [r["id"] for r in rows],
                    _ts(now + LEASE_SECONDS),
                )
        for row in rows:
            await self._deliver(row)
        return len(rows)

    async def _deliver(self, row: asyncpg.Record) -> None:
        trip = None
        if row["spec"]:
            with contextlib.suppress(ValidationError, ValueError):
                trip = Trip.from_json(json.loads(row["spec"]))
        cart = json.loads(row["cart"]) if row["cart"] else None
        message = self._messages.render(row["kind"], json.loads(row["params"]), trip, cart)
        try:
            if message is not None:  # (an admin-only kind has nothing for its user; the admins get it below)
                await self._send(row, message)
            if row["kind"] in ADMIN_KINDS:
                await self._to_admins(row["kind"], trip)
        except Exception as e:  # noqa: BLE001 - recorded and retried, whatever went wrong
            attempts = row["attempts"] + 1
            delay = RETRY_SECONDS[min(attempts, len(RETRY_SECONDS)) - 1]
            log.warning("Notification %d (%s) failed, attempt %d: %s", row["id"], row["kind"], attempts, e)
            await self._pool.execute(
                "UPDATE notifications SET attempts = $2, last_error = $3, next_attempt_at = $4 WHERE id = $1",
                row["id"],
                attempts,
                type(e).__name__,
                _ts(self._clock() + delay),
            )
            return
        await self._pool.execute("UPDATE notifications SET sent_at = $2 WHERE id = $1", row["id"], _ts(self._clock()))

    async def _send(self, row: asyncpg.Record, message: Message) -> None:
        if row["kind"] == "invited":
            delivered = await self._email(row["to_email"], message)
        else:
            delivered = await self._push(row["user_id"], message)
            if row["kind"] in EMAIL_KINDS or not delivered:
                delivered = await self._email(row["email"], message) or delivered
        if not delivered:
            raise Undelivered

    async def _push(self, user_id: str | None, message: Message) -> bool:
        """Push to every device the user subscribed. True if at least one took it."""
        if user_id is None or self._vapid is None:
            return False
        rows = await self._pool.fetch(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = $1 AND failures < $2",
            user_id,
            MAX_PUSH_FAILURES,
        )
        delivered = False
        for r in rows:
            try:
                await send(
                    self._http,
                    self._vapid,
                    Subscription(r["endpoint"], r["p256dh"], r["auth"]),
                    message.push(),
                    ttl=message.ttl,
                    urgency=message.urgency,
                    topic=message.topic,
                )
            except PushGone:
                await self._pool.execute("DELETE FROM push_subscriptions WHERE id = $1", r["id"])
                continue
            except (aiohttp.ClientError, TimeoutError) as e:
                log.info("Push to one device failed: %s", e)
                await self._pool.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = $1", r["id"])
                continue
            delivered = True
            await self._pool.execute(
                "UPDATE push_subscriptions SET last_ok = $2, failures = 0 WHERE id = $1", r["id"], _ts(self._clock())
            )
        return delivered

    async def _email(self, to: str | None, message: Message) -> bool:
        if to is None or self._mailer is None:
            return False
        text = f"{message.body}\n\n{message.url}\n"
        if message.also is not None:
            label, url = message.also
            text += f"\n{label}:\n{url}\n"
        await self._mailer.send(to, message.title, text)
        return True

    async def _to_admins(self, kind: str, trip: Trip | None) -> None:
        admins = await self._pool.fetch("SELECT id FROM users WHERE is_admin AND state = 'active'")
        note = Message(f"Admin: {kind.replace('_', ' ')}", f"Trip {trip.id if trip else '?'}.", "/admin")
        for admin in admins:
            await self._push(admin["id"], note)


def subscription_row(body: dict[str, Any]) -> Subscription:
    """A browser's PushSubscription JSON, checked (see `allowed_endpoint`)."""
    keys = body.get("keys")
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not isinstance(keys, dict):
        raise ValueError("Not a push subscription")  # noqa: TRY004 - bad input, answered with 400
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str) or len(p256dh) > MAX_KEY or len(auth) > MAX_KEY:
        raise ValueError("Not a push subscription")
    if not allowed_endpoint(endpoint):
        raise ValueError("Not a push service we send to")
    return Subscription(endpoint, p256dh, auth)


PUSH_SERVICES = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
    "notify.windows.com",
)


def allowed_endpoint(endpoint: str) -> bool:
    """Only the big push services: ferry-web never posts to an address a user made up."""
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and parts.port in (None, 443)
        and len(endpoint) <= MAX_ENDPOINT
        and any(host == s or host.endswith("." + s) for s in PUSH_SERVICES)
    )
