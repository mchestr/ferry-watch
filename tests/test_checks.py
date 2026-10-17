"""The HTTP check loop on real Postgres, with FakeHttpProvider and a fake waitlist-fetcher (docs/waitlist/design.md
§4.5): what each kind of record writes, who hears about it, and what never counts against a watch."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import asyncpg
import msgspec
import pytest

from tests.fakes import FakeHttpProvider, FakeInventory, FakeQuery, FakeSpec, fake_registry
from tests.postgres import ROLES, Database
from waitlist.control.checkplan import (
    ARCHIVE_AFTER_CLOSE_S,
    BLOCKED_COOLDOWN_S,
    CHECKER_DOWN_ALERT_AFTER_S,
    OUR_SIDE_RETRY_S,
    RATE_LIMITED_COOLDOWN_S,
)
from waitlist.control.checks import HttpChecks
from waitlist.control.fetch import CheckFailed, FetchClient
from waitlist.models import PACIFIC
from waitlist.providers import REGISTRY, Registry
from waitlist.providers.base import HttpProvider, Option, OptionStatus, search_hash
from waitlist.providers.buntzen import PROVIDER as BUNTZEN

ANN, BOB, ADMIN = "a" * 20, "b" * 20, "c" * 20
W1, W2, W3 = "1" * 16, "2" * 16, "3" * 16
DAY = date(2027, 7, 1)
NOON = datetime.combine(DAY, time(12, 0), tzinfo=PACIFIC).timestamp()
CLOSES = datetime.combine(DAY, time(18, 0), tzinfo=PACIFIC).timestamp()
REMIND = datetime.combine(DAY - timedelta(days=1), time(6, 55), tzinfo=PACIFIC).timestamp()
STUB = type(BUNTZEN).__name__ == "_NotYet"


class FakeClient(FetchClient):
    """waitlist-fetcher's answers, per part: an inventory's status, or a failure."""

    def __init__(self) -> None:  # no session: nothing is ever sent
        self.answers: dict[str, OptionStatus | CheckFailed] = {"a": "sold_out", "b": "sold_out"}
        self.calls: list[msgspec.Struct] = []
        self.during: Callable[[], Awaitable[None]] | None = None  # runs once, mid-fetch
        self.inventories: Callable[[msgspec.Struct], msgspec.Struct] | None = None

    async def fetch(self, provider: HttpProvider[Any, Any, Any], query: msgspec.Struct) -> msgspec.Struct:  # noqa: ARG002
        self.calls.append(query)
        if self.during is not None:
            during, self.during = self.during, None
            await during()
        if self.inventories is not None:
            return self.inventories(query)
        assert isinstance(query, FakeQuery)
        answer = self.answers[query.part]
        if isinstance(answer, CheckFailed):
            raise answer
        return FakeInventory(date=query.date, part=query.part, status=answer)


@dataclass
class Rig:
    db: Database
    pool: asyncpg.Pool
    client: FakeClient
    clock: list[float]
    checks: HttpChecks
    registry: Registry

    def loop(self, registry: Registry | None = None) -> HttpChecks:
        """Another waitlist-control, as after a restart."""
        return HttpChecks(
            self.pool,
            self.client,
            rates={"buntzen": 12},
            registry=registry or self.registry,
            clock=lambda: self.clock[0],
            rng=random.Random(7),  # noqa: S311 - jitter, seeded,
        )

    async def sql(self, query: str, *args: Any, user: str = "waitlist") -> list[asyncpg.Record]:
        conn = await self.db.connect(user)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    async def add(self, watch_id: str = W1, user: str = ANN, day: date = DAY, spec: Any = None) -> None:
        """A watch as waitlist-web adds one: its row, and a runtime row due at once."""
        spec = spec if spec is not None else {"date": day.isoformat()}
        await self.sql(
            "INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'buntzen', $3)",
            watch_id,
            user,
            json.dumps(spec),
            user=ROLES.web,
        )
        await self.sql("INSERT INTO trip_runtime (trip_id) VALUES ($1)", watch_id, user=ROLES.web)

    async def tick(self, at: float | None = None) -> Any:
        if at is not None:
            self.clock[0] = at
        return await self.checks.tick()

    async def result(self, watch_id: str = W1) -> dict[str, Any] | None:
        rows = await self.sql("SELECT * FROM check_results WHERE trip_id = $1", watch_id)
        return dict(rows[0]) if rows else None

    async def runtime(self, watch_id: str = W1) -> dict[str, Any]:
        (row,) = await self.sql("SELECT * FROM trip_runtime WHERE trip_id = $1", watch_id)
        return dict(row)

    async def due_at(self, watch_id: str = W1) -> float:
        when = (await self.runtime(watch_id))["next_check_at"]
        assert when is not None
        return float(when.timestamp())

    async def memory(self, watch_id: str = W1) -> list[str]:
        rows = await self.sql("SELECT alerted FROM alert_memory WHERE trip_id = $1", watch_id)
        return list(rows[0]["alerted"]) if rows else []

    async def notifications(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = await self.sql(
            "SELECT user_id, trip_id, kind, params, dedup_key FROM notifications"
            " WHERE $1::text IS NULL OR kind = $1 ORDER BY id",
            kind,
        )
        return [dict(r) | {"params": json.loads(r["params"])} for r in rows]

    async def events(self, code: str | None = None) -> list[tuple[str, dict[str, Any], str | None]]:
        rows = await self.sql(
            "SELECT code, params, dedup_key FROM events WHERE $1::text IS NULL OR code = $1 ORDER BY id", code
        )
        return [(r["code"], json.loads(r["params"]), r["dedup_key"]) for r in rows]


@pytest.fixture
async def rig(db: Database) -> AsyncIterator[Rig]:
    owner = await db.connect()
    try:
        await owner.execute("INSERT INTO users (id, username) VALUES ($1, 'ann'), ($2, 'bob')", ANN, BOB)
        await owner.execute("INSERT INTO users (id, username, is_admin) VALUES ($1, 'boss', true)", ADMIN)
    finally:
        await owner.close()
    pool = await asyncpg.create_pool(db.dsn(ROLES.control), min_size=1, max_size=4)
    client = FakeClient()
    clock = [NOON]
    registry = fake_registry()
    checks = HttpChecks(
        pool,
        client,
        rates={"buntzen": 12},
        registry=registry,
        clock=lambda: clock[0],
        rng=random.Random(7),  # noqa: S311 - jitter, seeded
    )
    try:
        yield Rig(db, pool, client, clock, checks, registry)
    finally:
        await pool.close()


def options(result: dict[str, Any] | None) -> list[tuple[str, str]]:
    assert result is not None
    return [(o["key"], o["status"]) for o in json.loads(result["options"])]


# ---- success, and what alerts


async def test_a_success_writes_the_result_the_runtime_and_the_alert_memory(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": "open", "b": "sold_out"}
    plan = await rig.tick()
    assert [j.watch_ids for j in plan.jobs] == [(W1,), (W1,)]
    result = await rig.result()
    assert result is not None
    assert options(result) == [("a", "open"), ("b", "sold_out")]
    assert (result["outcome"], result["error_class"], result["consecutive_errors"], result["error_since"]) == (
        "ok",
        None,
        0,
        None,
    )
    assert result["attempted_at"].timestamp() == result["checked_at"].timestamp() == NOON
    assert result["search_hash"] == search_hash((DAY,))
    runtime = await rig.runtime()
    assert runtime["last_check_at"].timestamp() == NOON
    assert 54 <= runtime["next_check_at"].timestamp() - NOON <= 66  # 60 s ± 10 %
    assert (runtime["failures"], runtime["backoff_until"], runtime["mode"], runtime["phase"]) == (0, None, None, "off")
    assert await rig.memory() == ["a"]
    (opening,) = await rig.notifications("opening")
    assert (opening["user_id"], opening["trip_id"], opening["params"]) == (
        ANN,
        W1,
        {"options": ["a"], "uncertain": []},
    )
    assert opening["dedup_key"] == f"open:{W1}:{int(NOON * 1000)}"
    assert await rig.events() == [("opening_seen", {"option": "a"}, None)]


async def test_an_opening_alerts_once_while_it_stays_open(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": "open", "b": "maybe"}
    await rig.tick()
    (opening,) = await rig.notifications("opening")
    assert opening["params"] == {"options": ["a", "b"], "uncertain": ["b"]}
    for _ in range(3):
        await rig.tick(await rig.due_at())
    assert len(await rig.notifications("opening")) == 1
    assert await rig.memory() == ["a", "b"]


async def test_a_reopening_alerts_again_but_not_within_two_minutes(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": "open", "b": "sold_out"}
    await rig.tick()  # T: alerted
    rig.client.answers["a"] = "sold_out"
    await rig.tick(NOON + 70)  # closed within 120 s of the alert: still remembered
    assert await rig.memory() == ["a"]
    assert await rig.events("opening_gone") == []
    rig.client.answers["a"] = "open"
    await rig.tick(NOON + 140)  # back: flapping, not a new opening
    assert len(await rig.notifications("opening")) == 1
    rig.client.answers["a"] = "sold_out"
    await rig.tick(NOON + 210)  # closed, long after the alert: forgotten
    assert await rig.memory() == []
    assert await rig.events("opening_gone") == [("opening_gone", {"option": "a"}, None)]
    rig.client.answers["a"] = "open"
    await rig.tick(NOON + 280)
    assert [n["params"]["options"] for n in await rig.notifications("opening")] == [["a"], ["a"]]


async def test_more_than_six_openings_an_hour_are_held_back(rig: Rig) -> None:
    await rig.add()
    for i in range(6):
        await rig.sql(
            "INSERT INTO notifications (user_id, trip_id, kind, created_at) VALUES ($1, $2, 'opening', $3)",
            ANN,
            W1,
            datetime.fromtimestamp(NOON - 600 + i, UTC),
        )
    rig.client.answers = {"a": "open", "b": "sold_out"}
    await rig.tick()
    await rig.tick(await rig.due_at())
    hour = datetime.fromtimestamp(NOON, UTC).strftime("%Y%m%d%H")
    assert await rig.events() == [("alert_held_back", {"count": 6}, f"held:{W1}:{hour}")]  # one an hour
    assert len(await rig.notifications("opening")) == 6
    assert await rig.memory() == []  # not remembered: it alerts once the hour allows
    await rig.tick(NOON + 3600)
    assert len(await rig.notifications("opening")) == 7


async def test_out_of_season_is_no_inventory_not_an_error(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": "not_offered", "b": "not_offered"}
    await rig.tick()
    result = await rig.result()
    assert result is not None
    assert (result["outcome"], result["consecutive_errors"]) == ("no_inventory", 0)
    assert await rig.notifications() == []


# ---- what a record never does


async def test_an_edit_while_fetching_drops_the_result(rig: Rig) -> None:
    await rig.add()

    async def edit() -> None:
        await rig.sql("UPDATE trips SET rev = rev + 1, updated_at = now() WHERE id = $1", W1, user=ROLES.web)

    rig.client.answers = {"a": "open", "b": "open"}
    rig.client.during = edit
    await rig.tick()
    assert await rig.result() is None
    assert await rig.notifications() == []
    assert (await rig.runtime())["next_check_at"] is None  # due again: the next tick replans


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE trips SET enabled = false",
        "UPDATE trips SET paused_reason = 'admin'",
        "UPDATE trips SET archived_at = now()",
        "DELETE FROM trips",
        "DELETE FROM users WHERE id = 'aaaaaaaaaaaaaaaaaaaa'",
    ],
)
async def test_a_watch_changed_or_gone_mid_fetch_is_dropped_silently(rig: Rig, change: str) -> None:
    await rig.add()

    async def mid_fetch() -> None:
        await rig.sql(change)

    rig.client.answers = {"a": "open", "b": "open"}
    rig.client.during = mid_fetch
    await rig.tick()
    assert await rig.result() is None
    assert await rig.notifications("opening") == []


async def test_a_failing_record_doesnt_stop_the_others(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    bad_day = DAY + timedelta(days=1)

    class Overflowing(FakeHttpProvider):
        def evaluate(
            self, spec: FakeSpec, inventories: Mapping[FakeQuery, FakeInventory], now: datetime
        ) -> tuple[Option, ...]:
            if spec.date != bad_day:
                return super().evaluate(spec, inventories, now)
            return tuple(Option(key=f"o{i}", label="Too many", status="sold_out", wanted=True) for i in range(21))

    rig.checks = rig.loop(fake_registry(Overflowing()))
    await rig.add(W1, day=bad_day)  # its record breaks check_results' bound
    await rig.add(W2, BOB)
    with caplog.at_level(logging.ERROR, logger="waitlist.control.checks"):
        await rig.tick()
    assert await rig.result(W1) is None
    assert await rig.result(W2) is not None
    assert any(W1 in r.getMessage() for r in caplog.records)


# ---- failures: ours, the site's, and part of a watch's


def ours(kind: str = "fetcher_unavailable", *, replied: bool = False) -> CheckFailed:
    return CheckFailed(kind, replied=replied)  # type: ignore[arg-type]


def theirs(kind: str, retry_after_s: int | None = None) -> CheckFailed:
    return CheckFailed(kind, retry_after_s=retry_after_s, replied=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("failure", [ours(), ours("bad_reply"), ours(replied=True)])
async def test_our_own_trouble_charges_no_watch(rig: Rig, failure: CheckFailed) -> None:
    await rig.add()
    rig.client.answers = {"a": "sold_out", "b": "sold_out"}
    await rig.tick()
    before = await rig.result()
    rig.client.answers = {"a": failure, "b": failure}
    later = await rig.due_at()
    heard: list[str] = []
    conn = await rig.db.connect()
    try:
        await conn.add_listener("wl_user", lambda *args: heard.append(args[3]))
        await rig.tick(later)
        for _ in range(50):
            if heard:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    finally:
        await conn.close()
    assert heard == [ANN]  # the state's next check moved
    assert await rig.result() == before  # no counter, no outcome, no attempt
    runtime = await rig.runtime()
    assert (runtime["failures"], runtime["backoff_until"]) == (0, None)
    assert runtime["next_check_at"].timestamp() == later + OUR_SIDE_RETRY_S
    assert runtime["last_check_at"].timestamp() == NOON
    assert [code for code, _, _ in await rig.events()] == []
    assert await rig.notifications() == []
    report = json.loads((await rig.sql("SELECT value FROM fleet_state WHERE key = 'check_report'"))[0]["value"])
    assert report["providers"]["buntzen"]["cooldown_until"] is None  # the fetcher's own cap included


@pytest.mark.parametrize(
    ("failure", "cooldown"),
    [
        (theirs("rate_limited"), RATE_LIMITED_COOLDOWN_S),
        (theirs("rate_limited", 900), 900),
        (theirs("blocked"), BLOCKED_COOLDOWN_S),
    ],
)
async def test_a_sites_429_or_403_cools_the_provider_down_not_the_watch(
    rig: Rig, failure: CheckFailed, cooldown: int
) -> None:
    await rig.add()
    await rig.add(W2, BOB, DAY + timedelta(days=1))
    rig.client.answers = {"a": failure, "b": failure}
    plan = await rig.tick()
    assert len(plan.jobs) == 4
    for watch_id in (W1, W2):
        result = await rig.result(watch_id)
        assert result is not None
        assert (result["outcome"], result["error_class"], result["consecutive_errors"]) == (
            "error",
            failure.kind,
            1,
        )
        runtime = await rig.runtime(watch_id)
        assert runtime["backoff_until"] is None
        assert runtime["next_check_at"].timestamp() == NOON + cooldown
    report = json.loads((await rig.sql("SELECT value FROM fleet_state WHERE key = 'check_report'"))[0]["value"])
    assert report["providers"]["buntzen"]["cooldown_until"] == NOON + cooldown
    calls = len(rig.client.calls)
    plan = await rig.tick(NOON + 60)
    assert plan.states == {W1: "backoff", W2: "backoff"}
    assert len(rig.client.calls) == calls
    # A restart doesn't forget it.
    again = rig.loop()
    await again.restore()
    rig.checks = again
    assert (await rig.tick(NOON + 120)).states == {W1: "backoff", W2: "backoff"}


async def test_a_failing_watch_backs_off_at_three_and_tells_its_owner_once_at_five(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": theirs("upstream_error"), "b": theirs("timeout")}
    started = NOON
    seen: list[tuple[int, float | None]] = []
    for _ in range(6):
        await rig.tick()
        result = await rig.result()
        assert result is not None
        runtime = await rig.runtime()
        backoff = runtime["backoff_until"]
        seen.append(
            (result["consecutive_errors"], round(backoff.timestamp() - rig.clock[0]) if backoff is not None else None)
        )
        assert result["error_class"] == "upstream_error"  # the first of the site's, in query order
        assert result["error_since"].timestamp() == started
        rig.clock[0] = await rig.due_at()
    assert seen == [(1, None), (2, None), (3, 300), (4, 600), (5, 1200), (6, 2400)]
    assert [(code, params) for code, params, _ in await rig.events()] == [
        ("check_failed", {"class": "upstream_error"}),
        ("check_failed", {"class": "upstream_error"}),
    ]  # at 1 and at 5
    (failing,) = await rig.notifications("failing")
    assert (failing["params"], failing["dedup_key"]) == (
        {"error_class": "upstream_error"},
        f"failing:{W1}:{int(started)}",
    )
    # Recovery clears the streak and the backoff.
    rig.client.answers = {"a": "sold_out", "b": "sold_out"}
    await rig.tick()
    result = await rig.result()
    assert result is not None
    assert (result["consecutive_errors"], result["error_since"], result["outcome"]) == (0, None, "ok")
    assert (await rig.runtime())["backoff_until"] is None


async def test_a_failing_query_never_stops_an_opening_on_the_other(rig: Rig) -> None:
    """Partial records: part b's site changed while part a is open."""
    await rig.add()
    rig.client.answers = {"a": "open", "b": "open"}
    await rig.tick()
    assert await rig.memory() == ["a", "b"]
    rig.client.answers = {"a": "sold_out", "b": theirs("site_changed")}
    await rig.tick(NOON + 200)  # past the re-alert cooldown: a is gone, b wasn't seen this round, so never "gone"
    assert await rig.memory() == ["b"]
    assert [params for _, params, _ in await rig.events("opening_gone")] == [{"option": "a"}]
    rig.client.answers["a"] = "open"
    for _ in range(4):
        await rig.tick(max(await rig.due_at(), rig.clock[0] + 200))
        runtime = await rig.runtime()
        assert runtime["backoff_until"] is None  # even at 3 or more
        assert 54 <= runtime["next_check_at"].timestamp() - rig.clock[0] <= 66  # the healthy part keeps pace
    result = await rig.result()
    assert result is not None
    assert result["outcome"] == "error"
    assert result["error_class"] == "site_changed"
    assert result["consecutive_errors"] == 5
    assert options(result) == [("a", "open"), ("b", "unchecked")]
    assert [n["params"]["options"] for n in await rig.notifications("opening")] == [["a", "b"], ["a"]]
    assert ("opening_gone", {"option": "b"}, None) not in await rig.events()
    assert await rig.memory() == ["a", "b"]


async def test_part_of_a_watch_failing_on_our_side_charges_nothing(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": "open", "b": ours()}
    await rig.tick()
    (opening,) = await rig.notifications("opening")
    assert opening["params"]["options"] == ["a"]
    result = await rig.result()
    assert result is not None
    assert (result["outcome"], result["consecutive_errors"], result["error_class"]) == ("ok", 0, None)
    assert options(result) == [("a", "open"), ("b", "unchecked")]
    runtime = await rig.runtime()
    assert (runtime["failures"], runtime["backoff_until"]) == (0, None)
    assert runtime["next_check_at"].timestamp() == NOON + OUR_SIDE_RETRY_S
    assert [code for code, _, _ in await rig.events()] == ["opening_seen"]


# ---- the provider's trouble, for admins


async def test_a_changed_site_tells_an_admin_once_a_day(rig: Rig) -> None:
    await rig.add()
    await rig.add(W2, BOB, DAY + timedelta(days=1))
    rig.client.answers = {"a": theirs("site_changed"), "b": theirs("site_changed")}
    await rig.tick()
    await rig.tick(NOON + 3600)
    (note,) = await rig.notifications("site_changed")
    day = datetime.fromtimestamp(NOON, PACIFIC).strftime("%Y-%m-%d")
    assert (note["user_id"], note["trip_id"], note["params"], note["dedup_key"]) == (
        ADMIN,
        None,
        {"provider": "buntzen"},
        f"site_changed:buntzen:{day}",
    )


async def test_a_fetcher_down_three_minutes_tells_an_admin_once_an_hour(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": ours(), "b": ours()}
    down = NOON
    await rig.tick()
    while rig.clock[0] < down + 600:  # ten minutes of outage
        await rig.tick(await rig.due_at())
    (note,) = await rig.notifications("checker_down")
    first_note_at = down + CHECKER_DOWN_ALERT_AFTER_S
    assert note["params"] == {"provider": "buntzen", "since": down}
    hour = datetime.fromtimestamp(first_note_at, UTC).strftime("%Y%m%d%H")
    assert note["dedup_key"] == f"checker_down:buntzen:{hour}"
    result = await rig.result()
    assert result is None  # nothing was ever charged to the watch
    assert [n["kind"] for n in await rig.notifications()] == ["checker_down"]  # no user alert
    report = json.loads((await rig.sql("SELECT value FROM fleet_state WHERE key = 'check_report'"))[0]["value"])
    assert report["providers"]["buntzen"]["fetcher"] == "down"
    assert report["providers"]["buntzen"]["fetcher_down_since"] == down


async def test_the_fetchers_own_cap_means_its_up(rig: Rig) -> None:
    await rig.add()
    rig.client.answers = {"a": ours(replied=True), "b": ours(replied=True)}
    for _ in range(8):
        await rig.tick(await rig.due_at() if rig.clock[0] > NOON else NOON)
        rig.clock[0] += 1
    assert await rig.notifications() == []
    assert await rig.result() is None


# ---- compare-and-set, reminders, archives, notifies, the report


async def test_a_check_now_made_while_fetching_survives_the_record(rig: Rig) -> None:
    await rig.add()
    asked = NOON + 3

    async def check_now() -> None:
        await rig.sql(
            "UPDATE trip_runtime SET next_check_at = $2 WHERE trip_id = $1",
            W1,
            datetime.fromtimestamp(asked, UTC),
            user=ROLES.web,
        )

    rig.client.during = check_now
    await rig.tick()
    assert await rig.due_at() == asked
    assert (await rig.runtime())["last_check_at"].timestamp() == NOON  # the record itself still landed


async def test_a_check_now_survives_our_own_failure_too(rig: Rig) -> None:
    await rig.add()
    asked = NOON + 3

    async def check_now() -> None:
        await rig.sql(
            "UPDATE trip_runtime SET next_check_at = $2 WHERE trip_id = $1", W1, datetime.fromtimestamp(asked, UTC)
        )

    rig.client.answers = {"a": ours(), "b": ours()}
    rig.client.during = check_now
    await rig.tick()
    assert await rig.due_at() == asked


async def test_a_reminder_is_queued_once(rig: Rig) -> None:
    await rig.add()
    await rig.add(W2, day=DAY)  # the same user and date
    await rig.add(W3, BOB)
    await rig.tick(REMIND)
    await rig.tick(REMIND + 5)
    rig.checks = rig.loop()  # a restart
    await rig.tick(REMIND + 10)
    reminders = await rig.notifications("release_reminder")
    assert [(n["user_id"], n["trip_id"], n["params"], n["dedup_key"]) for n in reminders] == [
        (ANN, W1, {}, f"remind:{ANN}:{DAY.isoformat()}"),
        (BOB, W3, {}, f"remind:{BOB}:{DAY.isoformat()}"),
    ]
    assert rig.client.calls == []  # before the release: nothing to find


async def test_an_ended_watch_is_archived_12_hours_after_its_window(rig: Rig) -> None:
    await rig.add()
    await rig.add(W2, BOB, DAY + timedelta(days=1))
    await rig.tick(CLOSES + ARCHIVE_AFTER_CLOSE_S - 1)
    assert [r["archived_at"] for r in await rig.sql("SELECT archived_at FROM trips WHERE id = $1", W1)] == [None]
    await rig.tick(CLOSES + ARCHIVE_AFTER_CLOSE_S)
    rows = await rig.sql("SELECT id, archived_at IS NOT NULL AS archived FROM trips ORDER BY id")
    assert [(r["id"], r["archived"]) for r in rows] == [(W1, True), (W2, False)]
    assert await rig.events() == [("watch_ended", {}, f"ended:{W1}")]
    assert await rig.notifications() == []  # an ended watch tells nobody anything
    # Brought back and ended again: still one event.
    await rig.sql("UPDATE trips SET archived_at = NULL WHERE id = $1", W1)
    await rig.tick(CLOSES + ARCHIVE_AFTER_CLOSE_S + 10)
    assert await rig.events() == [("watch_ended", {}, f"ended:{W1}")]


async def test_an_archive_that_changes_no_row_writes_no_event(rig: Rig) -> None:
    """waitlist-web removed the watch between the plan and the archive: its UPDATE changes nothing, so no event."""
    await rig.add()
    execute = asyncpg.connection.Connection.execute

    async def raced(self: asyncpg.Connection, query: str, *args: Any, **kw: Any) -> str:
        if query.startswith("UPDATE trips SET archived_at"):
            await rig.sql("UPDATE trips SET archived_at = now() WHERE id = $1", W1)
        return await execute(self, query, *args, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncpg.connection.Connection, "execute", raced)
        await rig.tick(CLOSES + ARCHIVE_AFTER_CLOSE_S)
    assert await rig.events() == []


async def test_each_affected_user_hears_once(rig: Rig) -> None:
    await rig.add()
    await rig.add(W2)
    await rig.add(W3, BOB, DAY + timedelta(days=1))
    heard: list[str] = []
    conn = await rig.db.connect()
    try:
        await conn.add_listener("wl_user", lambda *args: heard.append(args[3]))
        rig.client.answers = {"a": "open", "b": "sold_out"}
        await rig.tick()
        for _ in range(50):
            if len(heard) >= 2:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    finally:
        await conn.close()
    assert sorted(heard) == [ANN, BOB]


async def test_the_check_report(rig: Rig) -> None:
    await rig.add()
    await rig.add(W2, BOB)
    await rig.add(W3, BOB, DAY + timedelta(days=2))
    await rig.sql("UPDATE trips SET enabled = false WHERE id = $1", W3)
    rig.client.answers = {"a": "open", "b": theirs("site_changed")}
    await rig.tick()
    report = json.loads((await rig.sql("SELECT value FROM fleet_state WHERE key = 'check_report'"))[0]["value"])
    assert report["at"] == NOON
    assert isinstance(report["version"], str)
    assert report["providers"]["buntzen"] == {
        "running": True,
        "paused": False,
        "fetcher": "ok",
        "fetcher_down_since": None,
        "budget_per_minute": 12,
        "demand_per_minute": 2.0,  # two distinct queries, every 60 s
        "fetches_last_hour": 2,
        "errors_last_hour": 1,
        "last_ok_at": NOON,
        "last_error": "site_changed",
        "last_error_at": NOON,
        "cooldown_until": None,
        "watches": 3,
        "checking": 2,
        "deferred": 0,
    }
    await rig.tick(NOON + 10)  # not yet due again
    report = json.loads((await rig.sql("SELECT value FROM fleet_state WHERE key = 'check_report'"))[0]["value"])
    assert report["at"] == NOON  # every 30 s


async def test_maintenance_and_an_admins_pause_stop_the_checks(rig: Rig) -> None:
    await rig.add()
    await rig.sql("UPDATE fleet_state SET value = '[\"buntzen\"]' WHERE key = 'paused_providers'")
    plan = await rig.tick()
    assert (plan.jobs, plan.states) == ([], {W1: "provider_paused"})
    await rig.sql("UPDATE fleet_state SET value = '[]' WHERE key = 'paused_providers'")
    await rig.sql("UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'")
    rig.checks = rig.loop()
    plan = await rig.tick(NOON + 30)
    assert (plan.jobs, plan.states) == ([], {W1: "provider_paused"})
    report = json.loads((await rig.sql("SELECT value FROM fleet_state WHERE key = 'check_report'"))[0]["value"])
    assert (report["providers"]["buntzen"]["paused"], report["providers"]["buntzen"]["running"]) == (True, False)


async def test_a_spec_that_wont_load_is_skipped_and_logged_once(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    await rig.add(W1, spec={"date": "2027-07-01", "colour": "blue"})
    await rig.add(W2, BOB)
    with caplog.at_level(logging.WARNING, logger="waitlist.control.checks"):
        plan = await rig.tick()
        await rig.tick(NOON + 1)
    assert W1 not in plan.states
    assert [j.watch_ids for j in plan.jobs] == [(W2,), (W2,)]
    assert len([r for r in caplog.records if W1 in r.getMessage()]) == 1


async def test_the_budget_holds_across_ticks(rig: Rig) -> None:
    rig.checks = HttpChecks(
        rig.pool,
        rig.client,
        rates={"buntzen": 3},  # three requests a minute: one watch's two queries, and change
        registry=rig.registry,
        clock=lambda: rig.clock[0],
        rng=random.Random(7),  # noqa: S311 - jitter, seeded
    )
    await rig.add(W1)
    await rig.add(W2, BOB, DAY + timedelta(days=1))  # its own two queries
    first = await rig.tick()
    assert {j.watch_ids for j in first.jobs} == {(W1,)}  # the bucket starts full: 3
    assert first.deferred == {"buntzen": 1}
    second = await rig.tick(NOON + 5)  # 1.25 tokens: not enough for W2's two
    assert second.jobs == []
    assert second.deferred == {"buntzen": 1}
    third = await rig.tick(NOON + 25)  # 2.25
    assert {j.watch_ids for j in third.jobs} == {(W2,)}
    assert third.deferred == {}


async def test_archived_and_bc_ferries_rows_are_never_read(rig: Rig) -> None:
    await rig.add()
    await rig.add(W2, BOB)
    await rig.sql("UPDATE trips SET archived_at = now() WHERE id = $1", W2)
    await rig.sql("INSERT INTO trips (id, user_id, provider, spec) VALUES ($1, $2, 'bcferries', '{}')", W3, BOB)
    plan = await rig.tick()
    assert plan.states == {W1: "pending"}
    assert {j.watch_ids for j in plan.jobs} == {(W1,)}


async def test_the_loop_wakes_when_something_is_due_survives_a_bad_tick_and_stops(rig: Rig) -> None:
    await rig.add()
    # 07:00 PT the day before, less 0.3 s: its first check (the release, plus 2 s) is 2.3 s off.
    rig.clock[0] = datetime.combine(DAY - timedelta(days=1), time(7, 0), tzinfo=PACIFIC).timestamp() - 0.3
    ticks: list[float] = []
    tick = rig.checks.tick

    async def counted() -> Any:
        ticks.append(asyncio.get_running_loop().time())
        if len(ticks) == 1:
            raise asyncpg.PostgresError("the database went away for a moment")
        return await tick()

    rig.checks.tick = counted  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(rig.checks.run(stop))
    for _ in range(200):
        if len(ticks) >= 3:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(ticks) >= 3
    assert ticks[1] - ticks[0] >= 4.9  # a failed tick: the loop sleeps TICK_SECONDS
    assert ticks[2] - ticks[1] < 4.0  # then it wakes for the release, 2.3 s off, not the full tick


# ---- with the real Buntzen provider (W2)


@pytest.mark.skipif(STUB, reason="buntzen.PROVIDER is still the contract stub (W2 replaces it)")
async def test_a_half_day_answer_failing_never_stops_an_all_day_opening(rig: Rig) -> None:
    rig.checks = rig.loop(REGISTRY)
    spec = {"date": DAY.isoformat(), "passes": ["full_day", "morning", "afternoon"], "release_reminder": True}
    await rig.add(spec=spec)

    def answer(query: msgspec.Struct) -> msgspec.Struct:
        place = msgspec.to_builtins(query)["place_id"]
        if place != 10671:
            raise theirs("site_changed")
        items = [{"period": "full_day", "availability": "open", "status": "Available"}]
        inventory: msgspec.Struct = msgspec.convert(
            {"place_id": place, "date": DAY.isoformat(), "items": items}, BUNTZEN.inventory_type
        )
        return inventory

    rig.client.inventories = answer
    for _ in range(4):
        await rig.tick(max(await rig.due_at(), rig.clock[0] + 100) if rig.clock[0] > NOON else NOON)
    (opening,) = await rig.notifications("opening")
    assert opening["params"] == {"options": ["full_day"], "uncertain": []}
    assert (await rig.runtime())["backoff_until"] is None
