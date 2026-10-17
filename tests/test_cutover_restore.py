"""The cutover rehearsal (docs/waitlist/design.md §3.5, §8.7 C4-C8), end to end on a local PostgreSQL.

A Ferry Watch 0.7.1 database (migrations 0001-0006, owned by ferrywatch, with 0.7.1's grants for the
ferrywatch_* roles) is copied into a new `waitlist` database with the runbook's exact C5 pipeline, cleaned with
C6's SQL, then migrated and granted as waitlist-control's db hook does. Afterwards nothing names the old roles,
every row is a BC Ferries trip, and the new roles have exactly their grants.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from tests.postgres import OWNER, ROLES, Server, _binaries
from waitlist.control.db import Roles, grant, migrate, migrations

OLD_OWNER, OLD_WEB, OLD_CONTROL = "ferrywatch", "ferrywatch_web", "ferrywatch_control"
USER = "u0000000000000000001"
TRIP = "3f9c0a1b2c3d4e5f"
POD = f"fw-{TRIP}-g1"

# 0.7.1's grants (the foundation's `_grants`, before migration 0008), frozen as it ran for ferrywatch_*.
GRANTS_071 = [
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON users, identities, unmatched_identities, invites, sessions, "
        "user_limits, user_settings, push_subscriptions, trips TO ferrywatch_web"
    ),
    "GRANT SELECT ON trip_runtime, browser_pods, hold_claims, trip_state, alert_memory TO ferrywatch_web",
    (
        "GRANT INSERT (trip_id), UPDATE (next_check_at, hold_requested_until, quiet_lifted_until) ON trip_runtime "
        "TO ferrywatch_web"
    ),
    "GRANT DELETE ON alert_memory TO ferrywatch_web",
    "GRANT SELECT, INSERT ON agent_commands, events, vnc_mints, notifications TO ferrywatch_web",
    "GRANT DELETE ON events TO ferrywatch_web",
    "GRANT UPDATE (sent_at, attempts, last_error, next_attempt_at) ON notifications TO ferrywatch_web",
    "GRANT SELECT ON hold_outcomes TO ferrywatch_web",
    "GRANT UPDATE (answer_reason) ON hold_outcomes TO ferrywatch_web",
    "GRANT SELECT, INSERT, UPDATE ON usage_ledger TO ferrywatch_web",
    "GRANT SELECT ON fleet_state TO ferrywatch_web",
    "GRANT UPDATE (value) ON fleet_state TO ferrywatch_web",
    "GRANT INSERT ON audit_log TO ferrywatch_web",
    (
        "GRANT USAGE ON SEQUENCE agent_commands_id_seq, events_id_seq, notifications_id_seq, "
        "push_subscriptions_id_seq, audit_log_id_seq TO ferrywatch_web"
    ),
    "GRANT SELECT ON users, user_limits, user_settings, trips TO ferrywatch_control",
    "GRANT UPDATE (paused_reason, archived_at, updated_at) ON trips TO ferrywatch_control",
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON trip_runtime, browser_pods, hold_claims, trip_state, alert_memory "
        "TO ferrywatch_control"
    ),
    "GRANT SELECT ON agent_commands TO ferrywatch_control",
    "GRANT UPDATE (consumed_at) ON agent_commands TO ferrywatch_control",
    "GRANT SELECT, INSERT ON events, notifications, hold_outcomes TO ferrywatch_control",
    "GRANT SELECT, INSERT, UPDATE ON usage_ledger, fleet_state TO ferrywatch_control",
    "GRANT INSERT ON audit_log TO ferrywatch_control",
    (
        "GRANT USAGE ON SEQUENCE events_id_seq, notifications_id_seq, hold_outcomes_id_seq, audit_log_id_seq TO "
        "ferrywatch_control"
    ),
    "ALTER TABLE notifications ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS web_reads ON notifications",
    "CREATE POLICY web_reads ON notifications FOR SELECT TO ferrywatch_web USING (true)",
    "DROP POLICY IF EXISTS web_queues_its_own_kinds ON notifications",
    (
        "CREATE POLICY web_queues_its_own_kinds ON notifications FOR INSERT TO ferrywatch_web WITH CHECK (kind IN "
        "('test', 'account', 'admin', 'invited'))"
    ),
    "DROP POLICY IF EXISTS web_sends ON notifications",
    "CREATE POLICY web_sends ON notifications FOR UPDATE TO ferrywatch_web USING (true) WITH CHECK (true)",
    "DROP POLICY IF EXISTS control_notifies ON notifications",
    "CREATE POLICY control_notifies ON notifications FOR ALL TO ferrywatch_control USING (true) WITH CHECK (true)",
    "ALTER TABLE fleet_state ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS web_reads ON fleet_state",
    "CREATE POLICY web_reads ON fleet_state FOR SELECT TO ferrywatch_web USING (true)",
    "DROP POLICY IF EXISTS web_switches ON fleet_state",
    (
        "CREATE POLICY web_switches ON fleet_state FOR UPDATE TO ferrywatch_web USING (key IN ('maintenance', "
        "'pause_carting', 'breaker')) WITH CHECK (key IN ('maintenance', 'pause_carting') OR (key = 'breaker' AND "
        "value = '\"closed\"'::jsonb))"
    ),
    "DROP POLICY IF EXISTS control_runs_the_fleet ON fleet_state",
    (
        "CREATE POLICY control_runs_the_fleet ON fleet_state FOR ALL TO ferrywatch_control USING (true) WITH CHECK "
        "(true)"
    ),
]

# The runbook's C5 (§8.7), run here as the cluster's superuser like `pg bash -c` in the primary pod: the libpq
# environment points it at the test's server, and the databases are this test's.
C5 = (
    'set -euo pipefail; { echo "SET ROLE waitlist;"; pg_dump --no-owner --no-privileges -d $OLD_DB; }'
    " | psql -v ON_ERROR_STOP=1 --single-transaction -q -d $NEW_DB"
)
# C6, as the superuser, before 0008 adds its triggers.
C6 = [
    "UPDATE sessions SET revoked_at = now(), revoked_reason = 'cutover' WHERE revoked_at IS NULL",
    "UPDATE browser_pods SET ended_at = now(), phase = 'gone', end_reason = 'cutover' WHERE ended_at IS NULL",
    "DELETE FROM hold_claims",
    "UPDATE trip_state SET cart = NULL, viewer_connected = false WHERE cart IS NOT NULL OR viewer_connected",
    "UPDATE trip_runtime SET hold_requested_until = NULL WHERE hold_requested_until IS NOT NULL",
    "UPDATE notifications SET sent_at = now(), last_error = 'cutover' WHERE sent_at IS NULL",
    "UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'",
]


@contextlib.asynccontextmanager
async def databases(server: Server) -> AsyncIterator[tuple[str, str]]:
    """An empty `ferrywatch` copy and an empty `waitlist`, each owned by its own owner (C4)."""
    old, new = f"ferrywatch_{os.getpid()}", f"waitlist_{os.getpid()}"
    admin = await asyncpg.connect(server.dsn(server.admin, "postgres"))
    try:
        for role in (OLD_OWNER, OLD_WEB, OLD_CONTROL):
            with contextlib.suppress(asyncpg.DuplicateObjectError):
                await admin.execute(f"CREATE ROLE {role} LOGIN")
        for name, owner in ((old, OLD_OWNER), (new, OWNER)):
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {name} OWNER {owner}")
        yield old, new
    finally:
        for name in (old, new):
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()


async def ferry_watch_071(server: Server, name: str) -> None:
    """Ferry Watch 0.7.1's database, with a little of everything in it."""
    owner = await asyncpg.connect(server.dsn(OLD_OWNER, name))
    try:
        earlier = [m for m in migrations() if m[0] < "0007"]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("waitlist.control.db.migrations", lambda: earlier)
            assert await migrate(owner) == [v for v, _ in earlier]
        async with owner.transaction():
            for statement in GRANTS_071:
                await owner.execute(statement)
        await owner.execute("INSERT INTO users (id, username, email) VALUES ($1, 'mike', 'mike@example.com')", USER)
        spec = {"from": "TSA", "to": "SWB", "date": "2027-07-03", "enabled": True}
        await owner.execute("INSERT INTO trips (id, user_id, spec) VALUES ($1, $2, $3)", TRIP, USER, json.dumps(spec))
        await owner.execute(
            "INSERT INTO trip_runtime (trip_id, phase, hold_requested_until) VALUES ($1, 'off', now())", TRIP
        )
        await owner.execute(
            'INSERT INTO trip_state (trip_id, pod, cart, viewer_connected) VALUES ($1, $2, \'{"id": "c1"}\', true)',
            TRIP,
            POD,
        )
        await owner.execute("INSERT INTO alert_memory (trip_id, key_hash, alerted) VALUES ($1, '', '{09:00}')", TRIP)
        await owner.execute(
            "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at, ended_at, phase)"
            " VALUES ($1, $2, $3, 1, 'warm', now(), now(), 'gone')",
            POD,
            TRIP,
            USER,
        )
        await owner.execute(
            "INSERT INTO sessions (token_hash, user_id, iss, sub, idle_expires_at, expires_at)"
            " VALUES ($1, $2, 'https://auth', 'sub', now() + interval '1 day', now() + interval '30 days')",
            bytes(32),
            USER,
        )
        await owner.execute(
            "INSERT INTO notifications (user_id, trip_id, kind) VALUES ($1, $2, 'bookable')", USER, TRIP
        )
    finally:
        await owner.close()


def copy(server: Server, binaries: Path, old: str, new: str) -> None:
    env = {
        "PATH": f"{binaries}:/usr/bin:/bin",
        "PGHOST": server.host,
        "PGPORT": str(server.port),
        "PGUSER": server.admin,
        "OLD_DB": old,
        "NEW_DB": new,
    }
    subprocess.run(["/bin/bash", "-c", C5], check=True, env=env, capture_output=True, timeout=120)  # noqa: S603


async def test_a_ferry_watch_database_becomes_waitlists(pg_server: Server) -> None:
    binaries = _binaries()
    if binaries is None or not (binaries / "pg_dump").exists() or not (binaries / "psql").exists():
        pytest.skip("pg_dump and psql aren't installed")
    async with databases(pg_server) as (old, new):
        await ferry_watch_071(pg_server, old)
        copy(pg_server, binaries, old, new)
        admin = await asyncpg.connect(pg_server.dsn(pg_server.admin, new))
        try:
            async with admin.transaction():
                for statement in C6:
                    await admin.execute(statement)
        finally:
            await admin.close()

        owner = await asyncpg.connect(pg_server.dsn(OWNER, new))
        try:
            assert await migrate(owner) == ["0007_waitlist_names", "0008_providers"]
            await grant(owner, Roles())
            assert [tuple(r) for r in await owner.fetch("SELECT provider, count(*) FROM trips GROUP BY 1")] == [
                ("bcferries", 1)
            ]
            roles = await owner.fetch(
                "SELECT DISTINCT r::regrole::text AS name FROM pg_policy p, unnest(p.polroles) r ORDER BY 1"
            )
            assert [r["name"] for r in roles] == [ROLES.control, ROLES.web]
            owners = await owner.fetch("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
            assert [r["tableowner"] for r in owners] == [OWNER]
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee LIKE 'ferrywatch%'"
                )
                == 0
            )
            assert await owner.fetchval("SELECT value FROM fleet_state WHERE key = 'maintenance'") == "true"
            assert await owner.fetchval("SELECT value FROM fleet_state WHERE key = 'paused_providers'") == "[]"
            # C6 left nothing that would mislead the new deployment.
            assert await owner.fetchval("SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == 0
            assert await owner.fetchval("SELECT count(*) FROM notifications WHERE sent_at IS NULL") == 0
            assert await owner.fetchval("SELECT cart FROM trip_state") is None
            assert await owner.fetchval("SELECT alerted FROM alert_memory") == ["09:00"]
        finally:
            await owner.close()
        await holds_as_each_role(pg_server, new)


async def holds_as_each_role(server: Server, name: str) -> None:
    """The guarantees of design.md §3.3 on the copied database, as the roles that now log in."""
    web = await asyncpg.connect(server.dsn(ROLES.web, name))
    try:
        assert await web.fetchval("SELECT count(*) FROM trips") == 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await web.execute("UPDATE trips SET provider = 'buntzen'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await web.execute("UPDATE trips SET user_id = user_id")
        assert await web.execute("UPDATE trips SET rev = rev + 1, updated_at = now()") == "UPDATE 1"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await web.execute("INSERT INTO notifications (user_id, kind) VALUES ($1, 'opening')", USER)
        await web.execute(
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', '{}')", "b" * 16, USER
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await web.execute(
                "INSERT INTO check_results (trip_id, search_hash) VALUES ($1, '0123456789abcdef')", "b" * 16
            )
        assert await web.execute("UPDATE fleet_state SET value = 'false' WHERE key = 'maintenance'") == "UPDATE 1"
        assert await web.execute("UPDATE fleet_state SET value = '[]' WHERE key = 'paused_providers'") == "UPDATE 1"
    finally:
        await web.close()
    control = await asyncpg.connect(server.dsn(ROLES.control, name))
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await control.execute("UPDATE trips SET spec = spec")
        insert = "INSERT INTO check_results (trip_id, search_hash) VALUES ($1, '0123456789abcdef')"
        with pytest.raises(asyncpg.RaiseError):
            await control.execute(insert, TRIP)
        assert await control.execute(insert, "b" * 16) == "INSERT 0 1"
        with pytest.raises(asyncpg.RaiseError):  # browser pods are for BC Ferries trips only
            await control.execute(
                "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at)"
                " VALUES ($1, $2, $3, 1, 'warm', now())",
                f"wl-{'b' * 16}-g1",
                "b" * 16,
                USER,
            )
        await control.execute(
            "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at)"
            " VALUES ($1, $2, $3, 2, 'warm', now())",
            f"wl-{TRIP}-g2",
            TRIP,
            USER,
        )
        async with control.transaction():
            assert await control.fetchval("SELECT rev FROM trips t WHERE id = $1 FOR KEY SHARE OF t", TRIP) == 2
    finally:
        await control.close()
    old = await asyncpg.connect(server.dsn(OLD_WEB, name))
    try:  # the old roles still log in during the cutover, but hold nothing here
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await old.fetch("SELECT * FROM trips")
    finally:
        await old.close()
