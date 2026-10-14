"""The fleet loop against real Postgres and a cluster that keeps its pods in memory."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from typing import Any

import asyncpg
import pytest

from ferry_watch.control.configure import HubSettings, curve_interval
from ferry_watch.control.fleet import (
    EGRESS_GIVE_UP_S,
    FAILED_RETENTION_S,
    ORPHAN_GRACE_S,
    SILENCE_S,
    STARTUP_TIMEOUT_S,
    Fleet,
)
from ferry_watch.control.kube import KubeError, ObservedPod
from ferry_watch.control.pods import LABEL_EGRESS, PodParams
from ferry_watch.models import PACIFIC, Trip
from ferry_watch.polling import PollPolicy
from ferry_watch.protocol import CommandOp, CommandReason
from tests.postgres import ROLES, Database

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=PACIFIC).timestamp()
ANN = "a" * 20
NEAR = Trip(id="1" * 16, origin="SWB", destination="TSA", date=date(2026, 10, 14), earliest=time(7))
FAR = Trip(id="2" * 16, origin="SWB", destination="TSA", date=date(2026, 10, 30))
PARAMS = PodParams(
    image="ghcr.io/mchestr/ferry-watch-browser@sha256:" + "b" * 64,
    hub_url="ws://ferry-control.ferry-watch.svc.cluster.local.:8766/agent/v1",
    vnc_pubkeys="k1:AAAA",
    vnc_origin="https://ferry-checkout.chestr.dev",
)


@dataclass
class FakeCluster:
    clock: list[float]
    pods: dict[str, ObservedPod] = field(default_factory=dict)
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    egress_fails: bool = False
    uids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def review_token(self, token: str, audience: str) -> None:  # noqa: ARG002 - not used here
        return None

    async def list_pods(self) -> list[ObservedPod]:
        return list(self.pods.values())

    async def create_pod(self, spec: dict[str, Any]) -> ObservedPod:
        name = spec["metadata"]["name"]
        pod = ObservedPod(name, f"uid-{next(self.uids)}", dict(spec["metadata"]["labels"]), created=self.clock[0])
        self.pods[name], self.specs[name] = pod, spec
        return pod

    async def delete_pod(self, name: str) -> None:
        self.pods.pop(name, None)

    async def set_egress(self, name: str, value: str) -> None:
        if self.egress_fails:
            raise KubeError(500, "nope")
        pod = self.pods[name]
        self.pods[name] = replace(pod, labels=pod.labels | {LABEL_EGRESS: value})

    def set_phase(self, name: str, phase: str, message: str | None = None) -> None:
        self.pods[name] = replace(self.pods[name], phase=phase, termination_message=message)


@dataclass
class FakeHub:
    started_at: float
    connected: set[str] = field(default_factory=set)
    heard: dict[str, float] = field(default_factory=dict)
    commands: list[tuple[str, CommandOp, CommandReason]] = field(default_factory=list)

    def last_heard(self, pod: str) -> float | None:
        return self.heard.get(pod)

    async def command(self, pod: str, op: CommandOp, reason: CommandReason) -> bool:
        if pod not in self.connected:
            return False
        self.commands.append((pod, op, reason))
        return True


@dataclass
class Rig:
    fleet: Fleet
    cluster: FakeCluster
    hub: FakeHub
    db: Database
    clock: list[float]

    async def run(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        conn = await self.db.connect()
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    async def add_trip(self, trip: Trip, *, user: str = ANN) -> None:
        await self.run("INSERT INTO users (id, username) VALUES ($1, $1) ON CONFLICT DO NOTHING", user)
        await self.run(
            "INSERT INTO trips (id, user_id, spec) VALUES ($1, $2, $3)", trip.id, user, json.dumps(trip.to_json())
        )

    async def pods(self) -> list[asyncpg.Record]:
        return await self.run("SELECT * FROM browser_pods ORDER BY created_at, name")

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds


@pytest.fixture
async def rig(db: Database) -> AsyncIterator[Rig]:
    pool = await asyncpg.create_pool(db.dsn(ROLES.control), min_size=1, max_size=4)
    clock = [NOW]
    cluster = FakeCluster(clock)
    hub = FakeHub(started_at=NOW - 3600)
    fleet = Fleet(pool, cluster, hub, PARAMS, HubSettings(), clock=lambda: clock[0])
    try:
        yield Rig(fleet, cluster, hub, db, clock)
    finally:
        await pool.close()


# ---- starting


async def test_a_trip_within_a_week_gets_a_warm_pod(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    report = await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    assert report.started == [name]
    (row,) = await rig.pods()
    assert (row["name"], row["mode"], row["phase"], row["uid"]) == (name, "warm", "starting", "uid-1")
    assert rig.cluster.specs[name]["spec"]["containers"][0]["env"][1] == {"name": "FW_OWNER", "value": ANN}
    assert (await rig.fleet.reconcile()).started == []  # one pod per trip


async def test_a_trip_further_out_gets_a_short_check_every_6_hours(rig: Rig) -> None:
    await rig.add_trip(FAR)
    assert (await rig.fleet.reconcile()).started == [f"fw-{FAR.id}-g1"]
    (runtime,) = await rig.run("SELECT mode, next_check_at FROM trip_runtime")
    assert runtime["mode"] == "short_check"
    assert 6 * 3600 * 0.85 <= runtime["next_check_at"].timestamp() - NOW <= 6 * 3600 * 1.15
    rig.cluster.set_phase(f"fw-{FAR.id}-g1", "Succeeded", "short_check_done -")  # it checked and left
    report = await rig.fleet.reconcile()
    assert report.deleted == [f"fw-{FAR.id}-g1"]
    assert report.started == []  # not due again for 6 hours
    assert [(r["phase"], r["end_reason"]) for r in await rig.pods()] == [("gone", "short_check_done")]


# ---- reaping


async def test_a_pod_that_never_starts_is_replaced(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    rig.advance(STARTUP_TIMEOUT_S + 1)
    report = await rig.fleet.reconcile()
    assert report.deleted == [f"fw-{NEAR.id}-g1"]
    assert report.started == [f"fw-{NEAR.id}-g2"]  # a fresh generation
    assert (await rig.run("SELECT failures FROM trip_runtime"))[0]["failures"] == 1


async def test_a_silent_watching_pod_is_replaced(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    await rig.run("UPDATE browser_pods SET phase = 'watching'")
    rig.hub.heard[name] = NOW
    rig.advance(SILENCE_S - 1)
    assert (await rig.fleet.reconcile()).deleted == []
    rig.advance(2)
    assert (await rig.fleet.reconcile()).deleted == [name]


async def test_failed_pods_are_kept_for_a_post_mortem_then_swept(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    rig.cluster.set_phase(name, "Failed", "fatal markup_changed")
    rig.advance(30)  # a user's starts are 20 s apart
    report = await rig.fleet.reconcile()
    assert name in rig.cluster.pods  # kept: it never held a cart or had a viewer
    assert report.started == [f"fw-{NEAR.id}-g2"]
    rig.advance(FAILED_RETENTION_S + 1)
    assert name in (await rig.fleet.reconcile()).deleted


async def test_three_failures_back_off(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    for gen in (1, 2, 3):
        await rig.fleet.reconcile()
        rig.cluster.set_phase(f"fw-{NEAR.id}-g{gen}", "Failed", "fatal -")
        rig.advance(30)
    report = await rig.fleet.reconcile()
    assert report.started == []
    (runtime,) = await rig.run("SELECT failures, backoff_until FROM trip_runtime")
    assert runtime["failures"] == 3
    assert runtime["backoff_until"] is not None


async def test_strangers_are_swept_after_a_grace(rig: Rig) -> None:
    rig.cluster.pods["fw-ffffffffffffffff-g1"] = ObservedPod("fw-ffffffffffffffff-g1", "u", created=NOW)
    assert (await rig.fleet.reconcile()).deleted == []
    rig.advance(ORPHAN_GRACE_S + 1)
    assert (await rig.fleet.reconcile()).deleted == ["fw-ffffffffffffffff-g1"]


# ---- holds


async def hold(rig: Rig, name: str) -> None:
    await rig.run("UPDATE browser_pods SET phase = 'holding' WHERE name = $1", name)
    await rig.run(
        "INSERT INTO trip_state (trip_id, pod, hold_ready_at) VALUES ($1, $2, $3)"
        " ON CONFLICT (trip_id) DO UPDATE SET pod = $2, hold_ready_at = $3",
        NEAR.id,
        name,
        datetime.fromtimestamp(rig.clock[0], UTC),
    )


async def test_a_held_cart_opens_egress_and_closes_it_after(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    rig.hub.heard[name] = NOW
    await hold(rig, name)
    report = await rig.fleet.reconcile()
    assert report.egress == [(name, "hold")]
    assert rig.cluster.pods[name].labels[LABEL_EGRESS] == "hold"
    await rig.run("UPDATE browser_pods SET phase = 'watching'")  # an unviewed hold ended; the pod watches on
    assert (await rig.fleet.reconcile()).egress == [(name, "allowlist")]


async def test_a_pod_whose_egress_won_t_close_is_deleted(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    await hold(rig, name)
    await rig.fleet.reconcile()
    await rig.run("UPDATE browser_pods SET phase = 'watching'")
    rig.hub.heard[name] = NOW
    rig.cluster.egress_fails = True
    assert name not in (await rig.fleet.reconcile()).deleted
    rig.advance(EGRESS_GIVE_UP_S + 1)
    rig.hub.heard[name] = rig.clock[0]
    assert name in (await rig.fleet.reconcile()).deleted


async def test_a_holding_pod_is_never_reaped_for_silence_only_by_the_backstop(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    await hold(rig, name)
    rig.advance(3600)  # an hour of silence: fine while holding
    assert name not in (await rig.fleet.reconcile()).deleted
    rig.advance(6 * 60)  # past the hard cap plus 5 minutes
    assert name in (await rig.fleet.reconcile()).deleted


# ---- draining


async def test_a_paused_trip_s_pod_is_drained(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    await rig.run("UPDATE browser_pods SET phase = 'watching'")
    rig.hub.connected.add(name)
    rig.hub.heard[name] = NOW
    await rig.run("UPDATE trips SET enabled = false")
    report = await rig.fleet.reconcile()
    assert report.drained == [name]
    assert rig.hub.commands == [(name, "drain", "paused")]


async def test_the_old_account_pause_is_ignored(rig: Rig) -> None:
    """Trips are paused one at a time: users.paused (moved onto each trip by 0004) no longer stops anything."""
    await rig.add_trip(NEAR)
    await rig.run("UPDATE users SET paused = true")  # as a ferry-web from before 0004 could still set it
    assert (await rig.fleet.reconcile()).started == [f"fw-{NEAR.id}-g1"]


async def test_quiet_hours_stop_a_users_browsers_until_lifted(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.run("INSERT INTO user_settings (user_id, quiet_start, quiet_end) VALUES ($1, '23:00', '06:00')", ANN)
    await rig.run("INSERT INTO trip_runtime (trip_id) VALUES ($1)", NEAR.id)
    rig.clock[0] = datetime(2026, 10, 12, 23, 30, tzinfo=PACIFIC).timestamp()

    async def lift(column: str) -> None:
        await rig.run(f"UPDATE trip_runtime SET {column} = $1", datetime.fromtimestamp(rig.clock[0] + 600, UTC))  # noqa: S608

    assert (await rig.fleet.reconcile()).started == []  # Ann's quiet hours: nothing is checked
    await lift("hold_requested_until")  # "Hold it for me"
    name = f"fw-{NEAR.id}-g1"
    assert (await rig.fleet.reconcile()).started == [name]
    await rig.run("UPDATE browser_pods SET phase = 'watching'")
    rig.hub.connected.add(name)
    rig.advance(601)  # the request lapsed
    rig.hub.heard[name] = rig.clock[0]
    assert (await rig.fleet.reconcile()).drained == [name]
    assert rig.hub.commands == [(name, "drain", "paused")]
    rig.cluster.set_phase(name, "Succeeded", "drained -")
    await lift("quiet_lifted_until")  # a manual check
    assert (await rig.fleet.reconcile()).started == [f"fw-{NEAR.id}-g2"]


async def test_a_disconnected_pod_is_deleted_instead(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.fleet.reconcile()
    name = f"fw-{NEAR.id}-g1"
    await rig.run("UPDATE trips SET enabled = false")
    report = await rig.fleet.reconcile()
    assert name in report.deleted  # its own SIGTERM drain empties any cart


async def test_a_nearer_trip_takes_the_warm_browser(rig: Rig) -> None:
    later = Trip(id="3" * 16, origin="SWB", destination="TSA", date=date(2026, 10, 17))
    await rig.add_trip(later)
    await rig.fleet.reconcile()
    old = f"fw-{later.id}-g1"
    await rig.run("UPDATE browser_pods SET phase = 'watching'")
    rig.hub.connected.add(old)
    rig.hub.heard[old] = NOW
    await rig.add_trip(NEAR)
    rig.advance(30)
    report = await rig.fleet.reconcile()
    assert report.drained == [old]
    assert rig.hub.commands == [(old, "drain", "superseded")]
    (runtime,) = await rig.run("SELECT mode FROM trip_runtime WHERE trip_id = $1", later.id)
    assert runtime["mode"] == "short_check"


# ---- what the admin sees


async def test_each_pass_reports_what_it_knew(rig: Rig) -> None:
    await rig.add_trip(NEAR)
    await rig.add_trip(FAR, user="b" * 20)
    await rig.fleet.reconcile()
    report = await rig.fleet.reconcile()  # the warm browser started last pass now counts towards demand
    (row,) = await rig.run("SELECT value FROM fleet_state WHERE key = 'fleet_report'")
    assert json.loads(row["value"]) == report.summary
    interval = curve_interval(NEAR, datetime.fromtimestamp(NOW, PACIFIC), PollPolicy())
    assert report.summary == {
        "at": NOW,
        "browsers": {"max": 7, "live": 2},
        "memory": {"pod": "1536Mi", "pod_mb": 1536, "requested_mb": 3072, "budget_mb": 7 * 1536},
        "queued": 0,
        "short_checks": {"due": 0, "overdue": 0},
        "budget": {"checks_per_minute": 20, "demand_per_minute": round(60 / interval, 2), "stretch": 1.0},
        "brake": {"multiplier": 1.0, "paused": False},
        "breaker": "closed",
    }
    await rig.run(
        "INSERT INTO fleet_state (key, value) VALUES ('breaker', '\"open\"'), ('brake', $1)",
        json.dumps({"multiplier": 2, "paused": True}),
    )
    rig.advance(60)
    report = await rig.fleet.reconcile()
    assert report.summary["at"] == NOW + 60
    assert (report.summary["breaker"], report.summary["brake"]) == ("open", {"multiplier": 2.0, "paused": True})
    assert report.summary["browsers"]["live"] == 0  # an open breaker stops every browser


async def test_short_checks_waiting_for_room_are_overdue(rig: Rig) -> None:
    for n, user in ((4, "c" * 20), (5, "d" * 20), (6, "e" * 20)):
        far = Trip(id=str(n) * 16, origin="SWB", destination="TSA", date=date(2026, 10, 30))
        await rig.add_trip(far, user=user)
    report = await rig.fleet.reconcile()
    assert len(report.started) == 2  # at most two starting at once
    assert report.summary["short_checks"] == {"due": 3, "overdue": 1}
