"""The goldens that hold BC Ferries' behaviour still (docs/waitlist/design.md §10, "BC Ferries behaviour unchanged").

`uv run python -m tests.golden_capture` writes tests/golden/:

- agent-protocol.json: msgspec's JSON schemas of AgentMessage and ControlMessage, the frozen agent protocol.
- bcferries-entry.json: the dashboard's entry for a user's one BC Ferries trip, with fixed rows and a fixed clock
  (0.7.1's `trip`, `rev`, `paused_reason`, `quiet`, `hold`, `browser` and `booking_url`).
- bcferries-messages.json: every BC Ferries alert kind rendered with fixed params (title, body, url, also,
  urgency, ttl, topic).

It was run once, on the contract base, before anything else changed. test_protocol_golden.py and
test_bcferries_golden.py build the same things with the code as it is now and compare; re-running this must
leave tests/golden unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, cast
from unittest import mock

import aiohttp
import asyncpg
import msgspec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.postgres import ROLES, TEMPLATE, Database, Server, _prepare, _private_cluster
from waitlist.bcferries.terminals import TerminalDirectory, Terminals
from waitlist.control.limits import Limits
from waitlist.models import PACIFIC, Trip
from waitlist.polling import PollPolicy
from waitlist.protocol import AgentMessage, ControlMessage
from waitlist.web import api
from waitlist.web.accounts import Accounts, IdentityPolicy
from waitlist.web.api import CheckoutSigner
from waitlist.web.app import build
from waitlist.web.identity import OidcClient, OidcSettings
from waitlist.web.messages import Messages
from waitlist.web.repo import WebStore

GOLDEN = Path(__file__).parent / "golden"
FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://waitlist.chestr.dev"

USER = "u0000000000000000001"
TRIP_ID = "a1b2c3d4e5f60718"
# 10:00 am Pacific on Thu Jul 1 2027: outside the default quiet hours, two days before the trip.
NOW = datetime(2027, 7, 1, 10, 0, tzinfo=PACIFIC).timestamp()
SPEC: dict[str, Any] = {
    "from": "TSA",
    "to": "SWB",
    "date": "2027-07-03",
    "adults": 2,
    "earliest": "07:00",
    "latest": "12:00",
}
TRIP = Trip(
    id=TRIP_ID,
    origin="TSA",
    destination="SWB",
    date=date(2027, 7, 3),
    adults=2,
    earliest=time(7, 0),
    latest=time(12, 0),
)
CART = {
    "id": "c1",
    "status": "ready",
    "depart": "09:00",
    "fare": "Saver",
    "unclaimed_deadline": NOW + 600,
    "session_deadline": NOW + 1500,
    "hard_deadline": NOW + 3600,
}
# Every BC Ferries kind (design.md §10, gate 4), with the params agents and waitlist-control send.
MESSAGE_CASES: list[tuple[str, str, dict[str, Any], dict[str, Any] | None]] = [
    ("bookable", "bookable", {"departures": ["09:00"]}, None),
    ("bookable-many", "bookable", {"departures": ["07:00", "09:00", "10:30", "11:45"]}, None),
    ("bookable-quiet", "bookable", {"departures": ["09:00"], "held_back": "quiet_hours"}, None),
    ("bookable-auto-cart-off", "bookable", {"departures": ["09:00"], "held_back": "auto_cart_off"}, None),
    ("bookable-elsewhere", "bookable", {"departures": ["09:00"], "held_back": "holding_elsewhere"}, None),
    ("bookable-no-holds", "bookable", {"departures": ["09:00"], "held_back": "no_holds_left"}, None),
    ("bookable-limit", "bookable", {"departures": ["09:00"], "held_back": "limit"}, None),
    ("bookable-paused", "bookable", {"departures": ["09:00"], "held_back": "carting_paused"}, None),
    ("bookable-unknown-reason", "bookable", {"departures": ["09:00"], "held_back": "teleported"}, None),
    ("bookable-no-departures", "bookable", {}, None),
    ("cart_held", "cart_held", {"departures": ["09:00"], "fare": "Saver"}, CART),
    ("cart_held-no-deadline", "cart_held", {"departures": ["09:00"]}, None),
    ("hold_reminder", "hold_reminder", {"departures": ["09:00"]}, CART),
    ("hold_reminder-unclaimed", "hold_reminder", {"departures": ["09:00"]}, {"unclaimed_deadline": NOW + 600}),
    ("hold_ending", "hold_ending", {"departures": ["09:00"]}, CART),
    ("hold_ending-no-cart", "hold_ending", {"departures": ["09:00"]}, None),
    ("cart_failed", "cart_failed", {"departures": ["09:00"], "error_class": "not_bookable"}, None),
    ("cart_failed-timeout", "cart_failed", {"departures": ["09:00"], "error_class": "timeout"}, None),
    ("cart_failed-queue", "cart_failed", {"departures": ["09:00"], "error_class": "queue_timeout"}, None),
    ("cart_failed-markup", "cart_failed", {"departures": ["09:00"], "error_class": "markup_changed"}, None),
    ("cart_failed-mismatch", "cart_failed", {"departures": ["09:00"], "error_class": "cart_mismatch"}, None),
    ("cart_failed-unknown", "cart_failed", {"departures": ["09:00"], "error_class": "gremlins"}, None),
    ("cart_lost", "cart_lost", {"departures": ["09:00"]}, None),
    ("cart_expired", "cart_expired", {"departures": ["09:00"], "minutes": 60}, None),
    ("cart_expired-no-minutes", "cart_expired", {"departures": ["09:00"]}, None),
    ("failing", "failing", {"error_class": "markup_changed"}, None),
    ("booked_it", "booked_it", {"departures": ["09:00"]}, None),
    ("booked", "booked", {"departures": ["09:00"]}, None),
    ("checkout_blocked", "checkout_blocked", {}, None),
]


def terminals() -> Terminals:
    return Terminals.from_route_info(json.loads((FIXTURES / "route-info.json").read_text()))


def protocol_schemas() -> dict[str, Any]:
    """The agent protocol, as msgspec describes it: any change to a message's shape shows here."""
    return {
        "AgentMessage": msgspec.json.schema(AgentMessage),
        "ControlMessage": msgspec.json.schema(ControlMessage),
    }


def bcferries_messages() -> dict[str, Any]:
    """Each case rendered twice: with the terminal list known (route names, booking links) and before it loads."""
    known = terminals()
    rendered: dict[str, Any] = {}
    for label, source in (("terminals", lambda: known), ("no-terminals", lambda: None)):
        messages = Messages(BASE, source)
        for name, kind, params, cart in MESSAGE_CASES:
            message = messages.render(kind, params, TRIP, cart)
            rendered[f"{name}/{label}"] = (
                None
                if message is None
                else {
                    "title": message.title,
                    "body": message.body,
                    "url": message.url,
                    "also": list(message.also) if message.also is not None else None,
                    "urgency": message.urgency,
                    "ttl": message.ttl,
                    "topic": message.topic,
                }
            )
    return rendered


class _Known:
    """A terminal directory that already holds the list (user_state reads only `cached`)."""

    def __init__(self, known: Terminals) -> None:
        self.cached = known


@contextlib.contextmanager
def _fixed_id(value: str) -> Iterator[None]:
    with mock.patch("secrets.token_hex", return_value=value):
        yield


async def bcferries_state(db: Database) -> dict[str, Any]:
    """The whole state, as GET /api/state builds it, for a user with one BC Ferries trip seen by a warm browser."""
    owner = await db.connect()
    try:
        await owner.execute(
            "INSERT INTO users (id, username, display, email) VALUES ($1, 'mike', 'Mike', 'mike@example.com')", USER
        )
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(db.dsn(ROLES.web), min_size=1, max_size=2)
    try:
        store = WebStore(pool, Limits(), PollPolicy(), clock=lambda: NOW)
        with _fixed_id(TRIP_ID):
            await store.add_trip(USER, dict(SPEC))
        owner = await db.connect()
        try:
            await owner.execute(
                "UPDATE trip_runtime SET phase = 'watching', mode = 'warm', last_check_at = to_timestamp($2),"
                " next_check_at = to_timestamp($3), quiet_lifted_until = to_timestamp($4) WHERE trip_id = $1",
                TRIP_ID,
                NOW - 120,
                NOW + 3600,
                NOW - 60,
            )
            sailings: list[dict[str, Any]] = [
                {"depart": d, "arrive": a, "vessel": v, "status": s, "fares": []}
                for d, a, v, s in (
                    ("09:00", "10:35", "Spirit of Vancouver Island", "available"),
                    ("11:00", "12:35", "Queen of New Westminster", "sold_out"),
                    ("13:00", "14:35", "Spirit of British Columbia", "available"),
                )
            ]
            await owner.execute(
                "INSERT INTO trip_state (trip_id, pod, agent_phase, checked_at, next_due, effective_interval,"
                " sailings, error_class, consecutive_errors, heartbeat_at)"
                " VALUES ($1, $2, 'watching', to_timestamp($3), to_timestamp($4), 300, $5, NULL, 0, to_timestamp($6))",
                TRIP_ID,
                f"wl-{TRIP_ID}-g1",
                NOW - 40,
                NOW + 260,
                json.dumps(sailings),
                NOW - 5,
            )
            await owner.execute(
                "INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{11:00}')", TRIP_ID
            )
        finally:
            await owner.close()
        accounts = Accounts(pool, IdentityPolicy())
        async with aiohttp.ClientSession() as http:
            settings = OidcSettings(issuer="https://idp", client_id="c", client_secret="s", redirect_uri="x")
            oidc = OidcClient(http, settings)
            app = build(
                accounts,
                oidc,
                store,
                CheckoutSigner(Ed25519PrivateKey.generate(), "k1", "https://waitlist-checkout.chestr.dev"),
                pool=pool,
                limits=Limits(),
                terminals=cast(TerminalDirectory, _Known(terminals())),
                public={"links": {"kofi": None}, "push": {"key": "BPushKey"}},
                origin=BASE,
            )
            return await api.user_state(app, USER)
    finally:
        await pool.close()


def bcferries_entry_of(state: dict[str, Any]) -> dict[str, Any]:
    """The BC Ferries trip's entry: `trips[0]` at the contract base; from Waitlist 1.0 on, the bcferries watch
    without the three keys every watch gained (`provider`, `id`, `enabled`)."""
    if "trips" in state:
        entry: dict[str, Any] = state["trips"][0]
        return entry
    (watch,) = (w for w in state["watches"] if w.get("provider") == "bcferries")
    return {k: v for k, v in watch.items() if k not in ("provider", "id", "enabled")}


async def bcferries_entry(db: Database) -> dict[str, Any]:
    return bcferries_entry_of(await bcferries_state(db))


async def _capture(server: Server) -> dict[str, Any]:
    await _prepare(server)
    name = f"wl_golden_{os.getpid()}"
    admin = await asyncpg.connect(server.dsn(server.admin, "postgres"))
    try:
        await admin.execute(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE} OWNER waitlist")
        try:
            entry = await bcferries_entry(Database(server, name))
        finally:
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    finally:
        await admin.close()
    return {
        "agent-protocol.json": protocol_schemas(),
        "bcferries-entry.json": entry,
        "bcferries-messages.json": bcferries_messages(),
    }


def dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    with _private_cluster() as server:
        files = asyncio.run(_capture(server))
    GOLDEN.mkdir(exist_ok=True)
    for name, data in files.items():
        (GOLDEN / name).write_text(dump(data))
        sys.stdout.write(f"wrote {GOLDEN / name}\n")


if __name__ == "__main__":
    main()
