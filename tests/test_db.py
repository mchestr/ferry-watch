"""The schema, and what each login role may and may not do."""

from __future__ import annotations

import asyncpg
import pytest

from ferry_watch.control.config import ControlConfig, PodConfig
from ferry_watch.control.db import migrate, migrations
from ferry_watch.control.main import run_migrate
from tests.postgres import ROLES, Database, Server

USER = "u5yq2kz7m4c3x8a1b2d3e"[:20]
TRIP = "3f9c0a1b2c3d4e5f"


async def seed(db: Database) -> None:
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ($1, 'ann')", USER)
        await owner.execute("INSERT INTO trips (id, user_id, spec) VALUES ($1, $2, '{}')", TRIP, USER)
    finally:
        await owner.close()


async def test_migrations_apply_once(db: Database) -> None:
    owner = await db.connect()
    try:
        assert await migrate(owner) == []  # the template already has them
        versions = [r["version"] for r in await owner.fetch("SELECT version FROM schema_version ORDER BY 1")]
        assert versions == [v for v, _ in migrations()]
    finally:
        await owner.close()


async def test_a_trips_owner_never_changes(db: Database) -> None:
    await seed(db)
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ('bbbbbbbbbbbbbbbbbbbb', 'bob')")
        with pytest.raises(asyncpg.RaiseError, match="owner never changes"):
            await owner.execute("UPDATE trips SET user_id = 'bbbbbbbbbbbbbbbbbbbb'")
    finally:
        await owner.close()


async def test_one_live_pod_per_trip(db: Database) -> None:
    await seed(db)
    control = await db.connect(ROLES.control)
    try:
        insert = (
            "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at)"
            " VALUES ($1, $2, $3, $4, 'warm', now() + interval '26 hours')"
        )
        await control.execute(insert, f"fw-{TRIP}-g1", TRIP, USER, 1)
        with pytest.raises(asyncpg.UniqueViolationError):
            await control.execute(insert, f"fw-{TRIP}-g2", TRIP, USER, 2)
        await control.execute("UPDATE browser_pods SET ended_at = now(), phase = 'gone'")
        await control.execute(insert, f"fw-{TRIP}-g2", TRIP, USER, 2)
    finally:
        await control.close()


async def test_one_hold_per_user(db: Database) -> None:
    await seed(db)
    control = await db.connect(ROLES.control)
    try:
        claim = (
            "INSERT INTO hold_claims (user_id, trip_id, pod) VALUES ($1, $2, $3)"
            " ON CONFLICT (user_id) DO NOTHING RETURNING user_id"
        )
        assert await control.fetchval(claim, USER, TRIP, f"fw-{TRIP}-g1") == USER
        assert await control.fetchval(claim, USER, "ffffffffffffffff", "fw-ffffffffffffffff-g1") is None
    finally:
        await control.close()


async def test_the_audit_log_is_insert_only(db: Database) -> None:
    for role in (ROLES.web, ROLES.control):
        conn = await db.connect(role)
        try:
            await conn.execute("INSERT INTO audit_log (actor_kind, actor, action) VALUES ('system', 'x', 'y')")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE audit_log SET action = 'z'")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM audit_log")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetch("SELECT * FROM audit_log")
        finally:
            await conn.close()


async def test_web_queues_only_its_own_notifications(db: Database) -> None:
    await seed(db)
    web = await db.connect(ROLES.web)
    try:
        await web.execute("INSERT INTO notifications (user_id, kind) VALUES ($1, 'test')", USER)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # an agent-kind alert must come from control
            await web.execute("INSERT INTO notifications (user_id, kind) VALUES ($1, 'cart_held')", USER)
        await web.execute("UPDATE notifications SET sent_at = now()")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await web.execute("UPDATE notifications SET kind = 'cart_held'")
    finally:
        await web.close()
    control = await db.connect(ROLES.control)
    try:
        await control.execute("INSERT INTO notifications (user_id, kind) VALUES ($1, 'cart_held')", USER)
    finally:
        await control.close()


async def test_web_cannot_write_what_agents_report(db: Database) -> None:
    await seed(db)
    web = await db.connect(ROLES.web)
    try:
        for statement in (
            "INSERT INTO trip_state (trip_id) VALUES ('3f9c0a1b2c3d4e5f')",
            "INSERT INTO hold_claims (user_id, trip_id, pod) VALUES ('u5yq2kz7m4c3x8a1b2d3', '3f9c0a1b2c3d4e5f', 'x')",
            "UPDATE browser_pods SET phase = 'gone'",
            "UPDATE browser_pods SET vnc_seconds = 0",
            "UPDATE trip_runtime SET failures = 0",
            "INSERT INTO hold_outcomes (user_id, trip_id, day, outcome) VALUES ('x', 'y', now(), 'booked')",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await web.execute(statement)
        # What it may do to the runtime: make a check due now, and ask for a hold.
        await web.execute("UPDATE trip_runtime SET next_check_at = now(), hold_requested_until = now()")
    finally:
        await web.close()


async def test_web_flips_only_the_maintenance_switches(db: Database) -> None:
    control = await db.connect(ROLES.control)
    try:
        await control.execute("INSERT INTO fleet_state (key, value) VALUES ('brake', '{}'), ('breaker', '\"open\"')")
    finally:
        await control.close()
    web = await db.connect(ROLES.web)
    try:
        assert await web.execute("UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'") == "UPDATE 1"
        assert await web.execute("UPDATE fleet_state SET value = '{}' WHERE key = 'brake'") == "UPDATE 0"
        assert await web.execute("UPDATE fleet_state SET value = '\"closed\"' WHERE key = 'breaker'") == "UPDATE 1"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # only ferry-control opens it
            await web.execute("UPDATE fleet_state SET value = '\"open\"' WHERE key = 'breaker'")
    finally:
        await web.close()


async def test_web_deletes_a_user_and_everything_theirs(db: Database) -> None:
    await seed(db)
    control = await db.connect(ROLES.control)
    try:
        await control.execute("INSERT INTO trip_state (trip_id) VALUES ($1)", TRIP)
        await control.execute("INSERT INTO hold_claims (user_id, trip_id, pod) VALUES ($1, $2, 'x')", USER, TRIP)
        await control.execute("INSERT INTO notifications (user_id, kind) VALUES ($1, 'cart_held')", USER)
        await control.execute(
            "INSERT INTO hold_outcomes (user_id, trip_id, day, outcome) VALUES ($1, $2, now(), 'booked')", USER, TRIP
        )
    finally:
        await control.close()
    web = await db.connect(ROLES.web)
    try:
        await web.execute("INSERT INTO events (user_id, kind, code) VALUES ($1, 'agent', 'cart_added')", USER)
        await web.execute(
            "INSERT INTO sessions (token_hash, user_id, iss, sub, idle_expires_at, expires_at)"
            " VALUES ($1, $2, 'i', 's', now(), now())",
            bytes(32),
            USER,
        )
        await web.execute("DELETE FROM events WHERE user_id = $1", USER)
        await web.execute("DELETE FROM users WHERE id = $1", USER)  # the rest follows, whatever web may touch
    finally:
        await web.close()
    owner = await db.connect()
    try:
        for table in ("trips", "trip_state", "hold_claims", "notifications", "sessions", "events"):
            assert await owner.fetchval(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608 - fixed names
        assert await owner.fetchval("SELECT count(*) FROM hold_outcomes") == 1  # the record stays
    finally:
        await owner.close()


async def test_control_cannot_touch_people_or_sessions(db: Database) -> None:
    await seed(db)
    control = await db.connect(ROLES.control)
    try:
        for statement in (
            "SELECT * FROM sessions",
            "SELECT * FROM identities",
            "SELECT * FROM push_subscriptions",
            "UPDATE users SET is_admin = true",
            "UPDATE trips SET spec = '{}'",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await control.execute(statement)
        await control.execute("UPDATE trips SET paused_reason = 'after_checkout'")  # the one thing it may change
    finally:
        await control.close()


async def test_the_migrate_command_builds_an_empty_database(pg_server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = await asyncpg.connect(pg_server.dsn(pg_server.admin, "postgres"))
    try:
        await admin.execute("DROP DATABASE IF EXISTS fw_empty")
        await admin.execute("CREATE DATABASE fw_empty OWNER ferrywatch")
        for name, value in {
            "PGHOST": pg_server.host,
            "PGPORT": str(pg_server.port),
            "PGUSER": "ferrywatch",
            "PGDATABASE": "fw_empty",
        }.items():
            monkeypatch.setenv(name, value)
        await run_migrate(ControlConfig(pod=PodConfig(image="x", hub_url="x", vnc_pubkeys="x", vnc_origin="x")))
        await run_migrate(ControlConfig(pod=PodConfig(image="x", hub_url="x", vnc_pubkeys="x", vnc_origin="x")))
        web = await asyncpg.connect(pg_server.dsn(ROLES.web, "fw_empty"))
        try:
            assert await web.fetchval("SELECT count(*) FROM trips") == 0
        finally:
            await web.close()
    finally:
        await admin.execute("DROP DATABASE IF EXISTS fw_empty WITH (FORCE)")
        await admin.close()
