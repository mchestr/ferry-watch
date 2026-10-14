"""The schema, and what each login role may and may not do."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import time

import asyncpg
import pytest

from ferry_watch.control.config import ControlConfig, PodConfig
from ferry_watch.control.db import migrate, migrations
from ferry_watch.control.main import run_migrate
from tests.postgres import OWNER, ROLES, Database, Server

USER = "u5yq2kz7m4c3x8a1b2d3e"[:20]
TRIP = "3f9c0a1b2c3d4e5f"


@contextlib.asynccontextmanager
async def migrated_up_to(pg_server: Server, version: str) -> AsyncIterator[asyncpg.Connection]:
    """A new database with the migrations before `version`, as its owner: `migrate()` applies the rest."""
    name = f"fw_before_{version}"
    admin = await asyncpg.connect(pg_server.dsn(pg_server.admin, "postgres"))
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name}")
        await admin.execute(f"CREATE DATABASE {name} OWNER {OWNER}")
        owner = await asyncpg.connect(pg_server.dsn(OWNER, name))
        try:
            earlier = [m for m in migrations() if m[0] < version]
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr("ferry_watch.control.db.migrations", lambda: earlier)
                await migrate(owner)
            yield owner
        finally:
            await owner.close()
    finally:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()


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
            "UPDATE browser_pods SET agent_version = '0.6.3'",
            "UPDATE trip_runtime SET failures = 0",
            "INSERT INTO hold_outcomes (user_id, trip_id, day, outcome) VALUES ('x', 'y', now(), 'booked')",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await web.execute(statement)
        # What it may do to the runtime: make a check due now, ask for a hold, and lift quiet hours.
        await web.execute(
            "UPDATE trip_runtime SET next_check_at = now(), hold_requested_until = now(), quiet_lifted_until = now()"
        )
    finally:
        await web.close()


async def test_web_keeps_each_users_settings_and_control_only_reads_them(db: Database) -> None:
    await seed(db)
    web = await db.connect(ROLES.web)
    try:
        await web.execute(
            "INSERT INTO user_settings (user_id, quiet_start, quiet_end) VALUES ($1, '23:00', '06:00')", USER
        )
        with pytest.raises(asyncpg.CheckViolationError):  # a start needs an end
            await web.execute("UPDATE user_settings SET quiet_end = NULL")
        with pytest.raises(asyncpg.CheckViolationError):  # hours that start and end together cover nothing
            await web.execute("UPDATE user_settings SET quiet_start = '06:00'")
        await web.execute("UPDATE user_settings SET quiet_start = '22:30'")
    finally:
        await web.close()
    control = await db.connect(ROLES.control)
    try:
        assert await control.fetchval("SELECT quiet_start FROM user_settings") == time(22, 30)
        for statement in (
            "INSERT INTO user_settings (user_id) VALUES ('bbbbbbbbbbbbbbbbbbbb')",
            "UPDATE user_settings SET quiet_start = '22:00', quiet_end = '07:00'",
            "DELETE FROM user_settings",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await control.execute(statement)
    finally:
        await control.close()
    web = await db.connect(ROLES.web)
    try:
        assert await web.execute("DELETE FROM user_settings") == "DELETE 1"
    finally:
        await web.close()


async def test_0004_pauses_each_trip_of_whoever_paused_everything(pg_server: Server) -> None:
    """ "Pause all my trips" is gone: an account paused before 0004 keeps every live trip paused, one by one."""
    async with migrated_up_to(pg_server, "0004") as owner:
        await owner.execute(
            "INSERT INTO users (id, username, paused) VALUES ('aaaaaaaaaaaaaaaaaaaa', 'ann', true),"
            " ('bbbbbbbbbbbbbbbbbbbb', 'bob', false)"
        )
        for trip_id, user, spec, enabled, archived in [
            ("1111111111111111", "a" * 20, {"adults": 1}, True, False),  # Ann's, live
            ("2222222222222222", "a" * 20, {"enabled": False}, False, False),  # already paused
            ("3333333333333333", "a" * 20, {}, True, True),  # archived
            ("4444444444444444", "b" * 20, {}, True, False),  # Bob didn't pause
        ]:
            await owner.execute(
                "INSERT INTO trips (id, user_id, spec, enabled, archived_at)"
                " VALUES ($1, $2, $3, $4, CASE WHEN $5 THEN now() END)",
                trip_id,
                user,
                json.dumps(spec),
                enabled,
                archived,
            )
        assert await migrate(owner) == [v for v, _ in migrations() if v >= "0004"]
        rows = await owner.fetch("SELECT enabled, spec, rev FROM trips ORDER BY id")
        assert [(r["enabled"], json.loads(r["spec"]), r["rev"]) for r in rows] == [
            (False, {"adults": 1, "enabled": False}, 2),  # paused, and a new rev so its pod hears at once
            (False, {"enabled": False}, 1),
            (True, {}, 1),
            (True, {}, 1),
        ]
        assert await owner.fetchval("SELECT count(*) FROM users WHERE paused") == 0


async def test_0005_gives_everyone_quiet_hours(pg_server: Server) -> None:
    """Quiet hours off, or left at 0.6.3's 11 pm to 6 am, become the default, 10 pm to 5 am; times someone chose
    stay."""
    async with migrated_up_to(pg_server, "0005") as owner:
        ann, bob, cat, dan = "a" * 20, "b" * 20, "c" * 20, "d" * 20
        await owner.execute(
            "INSERT INTO users (id, username) VALUES ($1, 'ann'), ($2, 'bob'), ($3, 'cat'), ($4, 'dan')",
            ann,
            bob,
            cat,
            dan,
        )
        await owner.execute(
            "INSERT INTO user_settings (user_id, quiet_start, quiet_end)"
            " VALUES ($1, NULL, NULL), ($2, '23:30', '06:15'), ($3, '23:00', '06:00')",
            ann,
            bob,
            dan,
        )
        assert await migrate(owner) == [v for v, _ in migrations() if v >= "0005"]
        await owner.execute("INSERT INTO user_settings (user_id) VALUES ($1)", cat)  # a row starts at the default
        rows = await owner.fetch("SELECT user_id, quiet_start, quiet_end FROM user_settings ORDER BY user_id")
        assert [tuple(r) for r in rows] == [
            (ann, time(22), time(5)),  # had turned them off
            (bob, time(23, 30), time(6, 15)),
            (cat, time(22), time(5)),
            (dan, time(22), time(5)),  # turned them on and kept the times 0.6.3 filled in
        ]
        # A 0.6.3 ferry-web still running during the rollout can store "off" (it reads as the default): the times
        # become NOT NULL a release later.
        await owner.execute(
            "INSERT INTO user_settings (user_id, quiet_start, quiet_end) VALUES ($1, NULL, NULL) ON CONFLICT (user_id)"
            " DO UPDATE SET quiet_start = NULL, quiet_end = NULL, updated_at = now()",
            bob,
        )


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
        await web.execute("INSERT INTO user_settings (user_id) VALUES ($1)", USER)
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
        for table in ("trips", "trip_state", "hold_claims", "notifications", "sessions", "events", "user_settings"):
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
