"""Migration 0008 and the grants that go with it (docs/waitlist/design.md §3), each guarantee exercised as the role
it binds: waitlist_web, waitlist_control and the owner."""

from __future__ import annotations

import json
import re

import asyncpg
import pytest

from tests.postgres import ROLES, Database
from waitlist.control.db import grant, migrations
from waitlist.control.store import ControlStore
from waitlist.providers import PROVIDER_IDS

ANN = "a" * 20
FERRY = "f" * 16  # a BC Ferries trip
PASS = "b" * 16  # a Buntzen watch
BUNTZEN_SPEC = json.dumps({"date": "2027-07-01", "passes": ["morning"], "release_reminder": True})


@pytest.fixture
async def seeded(db: Database) -> Database:
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ($1, 'ann')", ANN)
        await owner.execute(
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'bcferries', '{}')", FERRY, ANN
        )
        await owner.execute(
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', $3)", PASS, ANN, BUNTZEN_SPEC
        )
    finally:
        await owner.close()
    return db


async def as_role(db: Database, role: str, sql: str, *args: object) -> str:
    conn = await db.connect(role)
    try:
        result: str = await conn.execute(sql, *args)
        return result
    finally:
        await conn.close()


def test_0008_is_the_last_migration() -> None:
    assert [v for v, _ in migrations()][-2:] == ["0007_waitlist_names", "0008_providers"]


async def test_the_provider_check_lists_exactly_the_registry(db: Database) -> None:
    owner = await db.connect()
    try:
        definition = await owner.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'trips_provider_check'"
        )
    finally:
        await owner.close()
    assert set(re.findall(r"'([a-z0-9_]+)'::text", definition)) == PROVIDER_IDS


async def test_every_insert_names_a_provider_from_the_list(db: Database) -> None:
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ($1, 'ann')", ANN)
        with pytest.raises(asyncpg.NotNullViolationError):  # no default: a forgotten provider fails
            await owner.execute("INSERT INTO trips (id, user_id, spec) VALUES ($1, $2, '{}')", FERRY, ANN)
        with pytest.raises(asyncpg.CheckViolationError):
            await owner.execute(
                "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'bcparks', '{}')", FERRY, ANN
            )
        for provider in sorted(PROVIDER_IDS):
            await owner.execute(
                "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, $3, '{}')",
                format(len(provider), "x") * 16,
                ANN,
                provider,
            )
    finally:
        await owner.close()
    web = await db.connect(ROLES.web)
    try:
        with pytest.raises(asyncpg.NotNullViolationError):
            await web.execute("INSERT INTO trips (id, user_id, spec) VALUES ($1, $2, '{}')", "c" * 16, ANN)
        await web.execute(
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', '{}')", "c" * 16, ANN
        )
    finally:
        await web.close()


async def test_a_watchs_provider_never_changes(seeded: Database) -> None:
    owner = await seeded.connect()
    try:
        with pytest.raises(asyncpg.RaiseError, match="provider never changes"):  # not even for the owner
            await owner.execute("UPDATE trips SET provider = 'buntzen' WHERE id = $1", FERRY)
        await owner.execute("UPDATE trips SET provider = 'bcferries' WHERE id = $1", FERRY)  # the same is no change
    finally:
        await owner.close()
    for role in (ROLES.web, ROLES.control):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await as_role(seeded, role, "UPDATE trips SET provider = 'buntzen' WHERE id = $1", FERRY)


async def test_a_watchs_owner_and_id_never_change(seeded: Database) -> None:
    owner = await seeded.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ($1, 'bob')", "c" * 20)
        with pytest.raises(asyncpg.RaiseError, match="owner never changes"):
            await owner.execute("UPDATE trips SET user_id = $2 WHERE id = $1", PASS, "c" * 20)
    finally:
        await owner.close()
    for column, value in (("user_id", "c" * 20), ("id", "d" * 16), ("checkout_mode", "guest"), ("created_at", None)):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await as_role(seeded, ROLES.web, f"UPDATE trips SET {column} = $2 WHERE id = $1", PASS, value)  # noqa: S608


async def test_web_writes_exactly_the_columns_it_needs(seeded: Database) -> None:
    web = await seeded.connect(ROLES.web)
    try:
        result = await web.execute(
            "UPDATE trips SET spec = $2, enabled = false, rev = rev + 1, paused_reason = NULL,"
            " archived_at = now(), updated_at = now() WHERE id = $1",
            PASS,
            BUNTZEN_SPEC,
        )
        assert result == "UPDATE 1"
        assert await web.execute("DELETE FROM trips WHERE id = $1", PASS) == "DELETE 1"
    finally:
        await web.close()


async def test_control_never_edits_a_spec(seeded: Database) -> None:
    for column in ("spec", "enabled", "rev"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await as_role(seeded, ROLES.control, f"UPDATE trips SET {column} = {column} WHERE id = $1", PASS)  # noqa: S608
    result = await as_role(
        seeded,
        ROLES.control,
        "UPDATE trips SET paused_reason = NULL, archived_at = now(), updated_at = now() WHERE id = $1",
        PASS,
    )
    assert result == "UPDATE 1"


async def test_control_can_lock_a_watch_to_compare_its_rev(seeded: Database) -> None:
    control = await seeded.connect(ROLES.control)
    try:
        async with control.transaction():
            rows = await control.fetch(
                "SELECT t.rev, t.enabled, t.archived_at, t.paused_reason FROM trips t"
                " WHERE t.id = $1 FOR KEY SHARE OF t",
                PASS,
            )
            assert [r["rev"] for r in rows] == [1]
            rev = await control.fetchval("SELECT rev FROM trips WHERE id = $1 FOR KEY SHARE", PASS)
            assert rev == 1
    finally:
        await control.close()


@pytest.mark.parametrize(
    ("table", "insert"),
    [
        (
            "browser_pods",
            "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at)"
            " VALUES ('wl-' || $1::text || '-g1', $1, $2, 1, 'warm', now())",
        ),
        ("hold_claims", "INSERT INTO hold_claims (user_id, trip_id, pod) VALUES ($2, $1, 'x')"),
    ],
)
async def test_browsers_and_carts_are_for_bc_ferries_trips_only(seeded: Database, table: str, insert: str) -> None:
    with pytest.raises(asyncpg.RaiseError, match=f"{table} rows are for BC Ferries trips only"):
        await as_role(seeded, ROLES.control, insert, PASS, ANN)
    with pytest.raises(asyncpg.RaiseError, match="for BC Ferries trips only"):  # nor a trip that doesn't exist
        await as_role(seeded, ROLES.control, insert, "e" * 16, ANN)
    assert await as_role(seeded, ROLES.control, insert, FERRY, ANN) == "INSERT 0 1"


async def test_a_cart_claim_cant_move_to_another_providers_watch(seeded: Database) -> None:
    await as_role(
        seeded, ROLES.control, "INSERT INTO hold_claims (user_id, trip_id, pod) VALUES ($1, $2, 'x')", ANN, FERRY
    )
    with pytest.raises(asyncpg.RaiseError, match="hold_claims rows are for BC Ferries trips only"):
        await as_role(seeded, ROLES.control, "UPDATE hold_claims SET trip_id = $1", PASS)


async def test_a_browser_pod_cant_move_to_another_providers_watch(seeded: Database) -> None:
    insert = (
        "INSERT INTO browser_pods (name, trip_id, user_id, gen, mode, deadline_at)"
        " VALUES ('wl-' || $1::text || '-g1', $1, $2, 1, 'warm', now())"
    )
    assert await as_role(seeded, ROLES.control, insert, FERRY, ANN) == "INSERT 0 1"
    with pytest.raises(asyncpg.RaiseError, match="browser_pods rows are for BC Ferries trips only"):
        await as_role(seeded, ROLES.control, "UPDATE browser_pods SET trip_id = $1", PASS)
    assert await as_role(seeded, ROLES.control, "UPDATE browser_pods SET trip_id = $1", FERRY) == "UPDATE 1"


async def test_a_checkout_ticket_cant_move_to_another_providers_watch(seeded: Database) -> None:
    mint = "INSERT INTO vnc_mints (jti, user_id, trip_id, pod, aud) VALUES ('j1', $1, $2, $3, 'wl-vnc')"
    assert await as_role(seeded, ROLES.web, mint, ANN, FERRY, f"wl-{FERRY}-g1") == "INSERT 0 1"
    owner = await seeded.connect()
    try:  # neither app may update a mint, and not even the owner can move one
        with pytest.raises(asyncpg.RaiseError, match="vnc_mints rows are for BC Ferries trips only"):
            await owner.execute("UPDATE vnc_mints SET trip_id = $1", PASS)
    finally:
        await owner.close()


async def test_a_check_result_cant_move_to_a_bc_ferries_trip(seeded: Database) -> None:
    insert = "INSERT INTO check_results (trip_id, search_hash) VALUES ($1, '0123456789abcdef')"
    assert await as_role(seeded, ROLES.control, insert, PASS) == "INSERT 0 1"
    with pytest.raises(asyncpg.RaiseError, match="check_results rows are for HTTP-checked watches only"):
        await as_role(seeded, ROLES.control, "UPDATE check_results SET trip_id = $1", FERRY)
    assert await as_role(seeded, ROLES.control, "UPDATE check_results SET trip_id = $1", PASS) == "UPDATE 1"


async def test_checkout_tickets_are_for_bc_ferries_trips_only(seeded: Database) -> None:
    mint = "INSERT INTO vnc_mints (jti, user_id, trip_id, pod, aud) VALUES ($1, $2, $3, $4, 'wl-vnc')"
    with pytest.raises(asyncpg.RaiseError, match="vnc_mints rows are for BC Ferries trips only"):
        await as_role(seeded, ROLES.web, mint, "j1", ANN, PASS, f"wl-{PASS}-g1")
    assert await as_role(seeded, ROLES.web, mint, "j2", ANN, FERRY, f"wl-{FERRY}-g1") == "INSERT 0 1"


async def test_check_results_are_for_http_watches_only_and_written_by_control(seeded: Database) -> None:
    insert = "INSERT INTO check_results (trip_id, search_hash) VALUES ($1, '0123456789abcdef')"
    with pytest.raises(asyncpg.RaiseError, match="check_results rows are for HTTP-checked watches only"):
        await as_role(seeded, ROLES.control, insert, FERRY)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await as_role(seeded, ROLES.web, insert, PASS)
    assert await as_role(seeded, ROLES.control, insert, PASS) == "INSERT 0 1"
    for statement in ("UPDATE check_results SET outcome = 'ok'", "DELETE FROM check_results"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await as_role(seeded, ROLES.web, statement)
    assert await as_role(seeded, ROLES.web, "SELECT * FROM check_results") == "SELECT 1"
    assert await as_role(seeded, ROLES.control, "UPDATE check_results SET outcome = 'ok'") == "UPDATE 1"
    owner = await seeded.connect()
    try:  # a watch's result goes with it
        await owner.execute("DELETE FROM trips WHERE id = $1", PASS)
        assert await owner.fetchval("SELECT count(*) FROM check_results") == 0
    finally:
        await owner.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("search_hash", "'XYZ'"),
        ("outcome", "'great'"),
        ("options", "'{}'"),
        ("options", "(SELECT jsonb_agg(i) FROM generate_series(1, 21) i)"),
        ("options", "jsonb_build_array(repeat('x', 16400))"),
        ("error_class", "'Site Changed'"),
        ("error_class", "'" + "a" * 33 + "'"),
        ("consecutive_errors", "-1"),
    ],
)
async def test_check_results_stay_bounded(seeded: Database, column: str, value: str) -> None:
    hashed = value if column == "search_hash" else "'0123456789abcdef'"
    extra = "" if column == "search_hash" else f", {column}"
    rest = "" if column == "search_hash" else f", {value}"
    sql = f"INSERT INTO check_results (trip_id, search_hash{extra}) VALUES ($1, {hashed}{rest})"  # noqa: S608
    with pytest.raises(asyncpg.CheckViolationError):
        await as_role(seeded, ROLES.control, sql, PASS)


async def test_check_results_hold_twenty_options(seeded: Database) -> None:
    options = json.dumps([{"key": f"o{i}"} for i in range(20)])
    insert = (
        "INSERT INTO check_results (trip_id, search_hash, options, error_class) VALUES ($1, $2, $3, 'site_changed')"
    )
    assert await as_role(seeded, ROLES.control, insert, PASS, "0123456789abcdef", options) == "INSERT 0 1"


@pytest.mark.parametrize("kind", ["opening", "release_reminder", "failing", "site_changed", "checker_down"])
async def test_web_cant_queue_what_control_alerts(seeded: Database, kind: str) -> None:
    insert = "INSERT INTO notifications (user_id, trip_id, kind) VALUES ($1, $2, $3)"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await as_role(seeded, ROLES.web, insert, ANN, PASS, kind)
    assert await as_role(seeded, ROLES.control, insert, ANN, PASS, kind) == "INSERT 0 1"


@pytest.mark.parametrize("kind", ["test", "account", "admin", "invited"])
async def test_web_still_queues_its_own_kinds(seeded: Database, kind: str) -> None:
    assert (
        await as_role(seeded, ROLES.web, "INSERT INTO notifications (user_id, kind) VALUES ($1, $2)", ANN, kind)
        == "INSERT 0 1"
    )


async def test_the_provider_pause_is_seeded_and_web_flips_it(db: Database) -> None:
    owner = await db.connect()
    try:
        assert await owner.fetchval("SELECT value FROM fleet_state WHERE key = 'paused_providers'") == "[]"
    finally:
        await owner.close()
    web = await db.connect(ROLES.web)
    try:
        assert (
            await web.execute("UPDATE fleet_state SET value = '[\"buntzen\"]' WHERE key = 'paused_providers'")
            == "UPDATE 1"
        )
        assert await web.execute("UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'") == "UPDATE 1"
        assert await web.execute("UPDATE fleet_state SET value = 'false' WHERE key = 'pause_carting'") == "UPDATE 1"
    finally:
        await web.close()


async def test_the_check_report_is_controls_alone(db: Database) -> None:
    report = json.dumps({"at": 1.0, "providers": {}})
    assert (
        await as_role(db, ROLES.control, "INSERT INTO fleet_state (key, value) VALUES ('check_report', $1)", report)
        == "INSERT 0 1"
    )
    web = await db.connect(ROLES.web)
    try:
        assert await web.execute("UPDATE fleet_state SET value = '{}' WHERE key = 'check_report'") == "UPDATE 0"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await web.execute("INSERT INTO fleet_state (key, value) VALUES ('brake', '{}')")
    finally:
        await web.close()
    with pytest.raises(asyncpg.CheckViolationError):
        await as_role(db, ROLES.control, "INSERT INTO fleet_state (key, value) VALUES ('mood', '{}')")
    for key in ("brake", "breaker", "control_alive_at", "fleet_report"):  # every key 0002 allowed still is
        await as_role(
            db, ROLES.control, "INSERT INTO fleet_state (key, value) VALUES ($1, '1') ON CONFLICT DO NOTHING", key
        )


async def test_grants_are_exact_after_every_migrate(seeded: Database) -> None:
    """A privilege an older release granted and this one doesn't is gone once grant() runs again."""
    owner = await seeded.connect()
    try:
        await owner.execute(f"GRANT UPDATE ON trips, check_results TO {ROLES.web}")
        await owner.execute(f"GRANT DELETE ON audit_log TO {ROLES.control}")
        await grant(owner, ROLES)
        await grant(owner, ROLES)  # idempotent
    finally:
        await owner.close()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await as_role(seeded, ROLES.web, "UPDATE trips SET user_id = user_id")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await as_role(seeded, ROLES.web, "UPDATE check_results SET outcome = 'ok'")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await as_role(seeded, ROLES.control, "DELETE FROM audit_log")
    assert await as_role(seeded, ROLES.web, "UPDATE trips SET rev = rev + 1 WHERE id = $1", PASS) == "UPDATE 1"


async def test_a_check_now_written_mid_fetch_survives_the_record(seeded: Database) -> None:
    """The compare-and-set upsert of design.md §4.5, as waitlist_control against a waitlist_web write."""
    control = await seeded.connect(ROLES.control)
    web = await seeded.connect(ROLES.web)
    try:
        await web.execute("INSERT INTO trip_runtime (trip_id) VALUES ($1)", PASS)
        planned = await control.fetchval("SELECT next_check_at FROM trip_runtime WHERE trip_id = $1", PASS)
        assert planned is None
        await web.execute("UPDATE trip_runtime SET next_check_at = to_timestamp(5000) WHERE trip_id = $1", PASS)
        upsert = (
            "INSERT INTO trip_runtime (trip_id, last_check_at, next_check_at, failures, backoff_until, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, now())"
            " ON CONFLICT (trip_id) DO UPDATE SET"
            " last_check_at = coalesce(EXCLUDED.last_check_at, trip_runtime.last_check_at),"
            " failures = EXCLUDED.failures, backoff_until = EXCLUDED.backoff_until, updated_at = now(),"
            " next_check_at = CASE WHEN trip_runtime.next_check_at IS NOT DISTINCT FROM $6"
            " THEN EXCLUDED.next_check_at ELSE trip_runtime.next_check_at END"
        )
        await control.execute(upsert, PASS, None, None, 0, None, planned)
        row = await control.fetchrow(
            "SELECT extract(epoch FROM next_check_at) AS at FROM trip_runtime WHERE trip_id = $1", PASS
        )
        assert row is not None
        assert float(row["at"]) == 5000.0  # web's "Check now" kept
    finally:
        await control.close()
        await web.close()


async def test_controls_trip_reads_never_see_another_providers_watch(seeded: Database) -> None:
    """The hub's trip lookup (ControlStore.trip_row) reads a Buntzen watch as no such trip (design.md §2.4)."""
    owner = await seeded.connect()
    try:
        spec = {"id": FERRY, "from": "TSA", "to": "SWB", "date": "2027-07-03"}  # as Trip.to_json() stores it
        await owner.execute("UPDATE trips SET spec = $2 WHERE id = $1", FERRY, json.dumps(spec))
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(seeded.dsn(ROLES.control), min_size=1, max_size=1)
    try:
        store = ControlStore(pool)
        assert await store.trip_row(PASS) is None
        found = await store.trip_row(FERRY)
        assert found is not None
        assert found[0].trip.id == FERRY
    finally:
        await pool.close()
