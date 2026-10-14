"""The control plane's Postgres: migrations, and what each login role may touch (design-reference §6.4).

`db-migrate` runs `migrate()` then `grant()` as the database owner. ferry-web and ferry-control log in as
their own roles, own nothing, and get exactly the grants below: the audit log is insert-only for both;
ferry-web may queue only its own kinds of notification, flip only the maintenance switches and close (never
open) the breaker; only ferry-control writes what the agents report.
"""

from __future__ import annotations

import importlib.resources
import logging
import re
from dataclasses import dataclass

import asyncpg

log = logging.getLogger(__name__)

MIGRATIONS_PACKAGE = "ferry_watch.control.migrations"
LOCK_KEY = 0x6677_6D69  # "fwmi": one migrator at a time
_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")

WEB_NOTIFICATION_KINDS = ("test", "account", "admin", "invited")
WEB_FLEET_KEYS = ("maintenance", "pause_carting")


@dataclass(frozen=True, slots=True)
class Roles:
    web: str = "ferrywatch_web"
    control: str = "ferrywatch_control"

    def __post_init__(self) -> None:
        for role in (self.web, self.control):
            if not _ROLE.fullmatch(role):
                raise ValueError(f"Not a role name: {role!r}")


def migrations() -> list[tuple[str, str]]:
    """(version, SQL) for every migration, in order."""
    files = importlib.resources.files(MIGRATIONS_PACKAGE)
    found = sorted((f for f in files.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name.removesuffix(".sql"), f.read_text()) for f in found]


async def migrate(conn: asyncpg.Connection) -> list[str]:
    """Apply what hasn't been applied, each migration in its own transaction. Returns what ran."""
    applied_now = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version"
            " (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row["version"] for row in await conn.fetch("SELECT version FROM schema_version")}
        for version, sql in migrations():
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_version (version) VALUES ($1)", version)
            log.info("Applied migration %s", version)
            applied_now.append(version)
    return applied_now


def _grants(web: str, control: str) -> list[str]:
    kinds = ", ".join(f"'{k}'" for k in WEB_NOTIFICATION_KINDS)
    keys = ", ".join(f"'{k}'" for k in WEB_FLEET_KEYS)
    return [
        # ---- ferry-web: people, trips, the API's reads, the outbox's sending
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON users, identities, unmatched_identities, invites, sessions,"
        f" user_limits, push_subscriptions, trips TO {web}",
        f"GRANT SELECT ON trip_runtime, browser_pods, hold_claims, trip_state, alert_memory TO {web}",
        # ... and may make a short check due now (a new trip, a re-enable, a manual check), nothing more.
        f"GRANT INSERT (trip_id), UPDATE (next_check_at) ON trip_runtime TO {web}",
        f"GRANT DELETE ON alert_memory TO {web}",  # an edited search forgets what the old one alerted
        f"GRANT SELECT, INSERT ON agent_commands, events, vnc_mints, notifications TO {web}",
        f"GRANT DELETE ON events TO {web}",  # deleting a user takes their history with them
        f"GRANT UPDATE (sent_at, attempts, last_error, next_attempt_at) ON notifications TO {web}",
        f"GRANT SELECT ON hold_outcomes TO {web}",
        f"GRANT UPDATE (answer_reason) ON hold_outcomes TO {web}",
        f"GRANT SELECT, INSERT, UPDATE ON usage_ledger TO {web}",
        f"GRANT SELECT ON fleet_state TO {web}",
        f"GRANT UPDATE (value) ON fleet_state TO {web}",
        f"GRANT INSERT ON audit_log TO {web}",
        f"GRANT USAGE ON SEQUENCE agent_commands_id_seq, events_id_seq, notifications_id_seq,"
        f" push_subscriptions_id_seq, audit_log_id_seq TO {web}",
        # ---- ferry-control: pods, what agents report, and the trips' pause and archive
        f"GRANT SELECT ON users, user_limits, trips TO {control}",
        f"GRANT UPDATE (paused_reason, archived_at, updated_at) ON trips TO {control}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON trip_runtime, browser_pods, hold_claims, trip_state,"
        f" alert_memory TO {control}",
        f"GRANT SELECT ON agent_commands TO {control}",
        f"GRANT UPDATE (consumed_at) ON agent_commands TO {control}",
        f"GRANT SELECT, INSERT ON events, notifications, hold_outcomes TO {control}",
        f"GRANT SELECT, INSERT, UPDATE ON usage_ledger, fleet_state TO {control}",
        f"GRANT INSERT ON audit_log TO {control}",
        f"GRANT USAGE ON SEQUENCE events_id_seq, notifications_id_seq, hold_outcomes_id_seq, audit_log_id_seq"
        f" TO {control}",
        # ---- row-level limits
        "ALTER TABLE notifications ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS web_reads ON notifications",
        f"CREATE POLICY web_reads ON notifications FOR SELECT TO {web} USING (true)",
        "DROP POLICY IF EXISTS web_queues_its_own_kinds ON notifications",
        f"CREATE POLICY web_queues_its_own_kinds ON notifications FOR INSERT TO {web} WITH CHECK (kind IN ({kinds}))",
        "DROP POLICY IF EXISTS web_sends ON notifications",
        f"CREATE POLICY web_sends ON notifications FOR UPDATE TO {web} USING (true) WITH CHECK (true)",
        "DROP POLICY IF EXISTS control_notifies ON notifications",
        f"CREATE POLICY control_notifies ON notifications FOR ALL TO {control} USING (true) WITH CHECK (true)",
        "ALTER TABLE fleet_state ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS web_reads ON fleet_state",
        f"CREATE POLICY web_reads ON fleet_state FOR SELECT TO {web} USING (true)",
        # ferry-web flips the maintenance switches, and may close the breaker (only ferry-control opens it).
        "DROP POLICY IF EXISTS web_switches ON fleet_state",
        f"CREATE POLICY web_switches ON fleet_state FOR UPDATE TO {web} USING (key IN ({keys}, 'breaker'))"
        f" WITH CHECK (key IN ({keys}) OR (key = 'breaker' AND value = '\"closed\"'::jsonb))",
        "DROP POLICY IF EXISTS control_runs_the_fleet ON fleet_state",
        f"CREATE POLICY control_runs_the_fleet ON fleet_state FOR ALL TO {control} USING (true) WITH CHECK (true)",
    ]


async def grant(conn: asyncpg.Connection, roles: Roles) -> None:
    """Give each login role exactly its access. Idempotent; run after every migration."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        for statement in _grants(roles.web, roles.control):
            await conn.execute(statement)
