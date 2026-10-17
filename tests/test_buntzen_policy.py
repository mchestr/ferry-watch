"""When a Buntzen watch is checked and what its options are (design.md §5.5-5.6)."""

from __future__ import annotations

import itertools
import json
import random
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from waitlist.models import PACIFIC
from waitlist.providers.base import ALERTABLE, MAX_OPTIONS, Option
from waitlist.providers.buntzen import PROVIDER
from waitlist.providers.buntzen.meta import FULL_DAY_PAGE, HALF_DAY_PAGE, META
from waitlist.providers.buntzen.model import BuntzenInventory, BuntzenQuery, PassItem
from waitlist.providers.buntzen.policy import (
    BURST,
    ESTIMATED_SEASON,
    KNOWN_SEASONS,
    LABELS,
    OFF_SEASON,
    PAGES,
    RELEASE_AT,
    REMINDER_AT,
    STEADY,
    STOP_AT,
    booking_url,
    evaluate,
    in_season,
    interval,
    queries,
    reminder_at,
    window,
)
from waitlist.providers.buntzen.spec import PASSES, BuntzenSpec, Pass

DAY = date(2027, 7, 1)
ALL = BuntzenSpec(date=DAY, passes=PASSES)
FULL_Q = BuntzenQuery(place_id=10671, date="2027-07-01")
HALF_Q = BuntzenQuery(place_id=10672, date="2027-07-01")
RELEASE = datetime(2027, 6, 30, 7, 0, tzinfo=PACIFIC)
IN_WINDOW = datetime(2027, 6, 30, 9, 0, tzinfo=PACIFIC)
API_STATE = Path(__file__).parent.parent / "web" / "src" / "test" / "fixtures" / "api-state.json"


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=PACIFIC)


def inventory(query: BuntzenQuery, *items: tuple[str, str]) -> BuntzenInventory:
    return BuntzenInventory(
        place_id=query.place_id,
        date=query.date,
        items=[PassItem(period=p, availability=a, status=f"{p} {a}"[:32]) for p, a in items],  # type: ignore[arg-type]
    )


def statuses(spec: BuntzenSpec, inventories: dict[BuntzenQuery, BuntzenInventory], now: datetime) -> dict[str, str]:
    return {o.key: o.status for o in evaluate(spec, inventories, now)}


def test_the_binding_constants() -> None:
    assert LABELS == {"full_day": "All-day pass", "morning": "Morning pass", "afternoon": "Afternoon pass"}
    assert (time(7, 0), time(6, 55)) == (RELEASE_AT, REMINDER_AT)
    assert (timedelta(seconds=30), timedelta(seconds=60), timedelta(minutes=30)) == (BURST, STEADY, OFF_SEASON)
    assert STOP_AT == {"full_day": time(18, 0), "morning": time(12, 0), "afternoon": time(18, 0)}
    assert PAGES == {"full_day": FULL_DAY_PAGE, "morning": HALF_DAY_PAGE, "afternoon": HALF_DAY_PAGE}
    assert KNOWN_SEASONS == {2026: (date(2026, 5, 14), date(2026, 9, 7))}
    assert ESTIMATED_SEASON == ((5, 1), (9, 30))


@pytest.mark.parametrize("passes", [("morning",), ("morning", "afternoon"), ("full_day",), PASSES])
def test_the_window_opens_at_the_release_and_closes_at_the_last_stop_time(passes: tuple[Pass, ...]) -> None:
    w = window(BuntzenSpec(date=DAY, passes=passes))
    assert w.opens_at == RELEASE
    assert w.closes_at == max(at(DAY, STOP_AT[p].hour) for p in passes)
    assert window(BuntzenSpec(date=DAY, passes=("morning",))).closes_at == at(DAY, 12)


def utc_offset_hours(at_: datetime) -> float:
    offset = at_.utcoffset()
    assert offset is not None
    return offset.total_seconds() / 3600


@pytest.mark.parametrize(
    "day",
    [
        # The design's DST dates. The tz database this runs on (2026c) has British Columbia on permanent UTC-7
        # from 2026-11-01, so these no longer shift; 07:00 local holds either way.
        date(2027, 3, 14),
        date(2027, 3, 15),
        date(2027, 11, 7),
        date(2027, 11, 8),
        # 2026's spring change, which every tz database has: the offset really moves under the release.
        date(2026, 3, 8),
        date(2026, 3, 9),
    ],
)
def test_the_window_is_07_00_local_across_dst(day: date) -> None:
    w = window(BuntzenSpec(date=day, passes=PASSES))
    opens = w.opens_at.astimezone(PACIFIC)
    closes = w.closes_at.astimezone(PACIFIC)
    assert (opens.date(), opens.time()) == (day - timedelta(days=1), time(7, 0))
    assert (closes.date(), closes.time()) == (day, time(18, 0))
    # The instants are the zone's own: 07:00 local in UTC is 07:00 minus that day's offset.
    assert w.opens_at.astimezone(UTC).hour == (7 - utc_offset_hours(opens)) % 24
    assert w.closes_at.astimezone(UTC).hour == (18 - utc_offset_hours(closes)) % 24


def test_the_release_moves_with_the_clocks_in_utc() -> None:
    before = window(BuntzenSpec(date=date(2026, 3, 8), passes=PASSES)).opens_at  # released Sat Mar 7, PST
    after = window(BuntzenSpec(date=date(2026, 3, 9), passes=PASSES)).opens_at  # released Sun Mar 8, PDT
    assert (before.astimezone(UTC).hour, after.astimezone(UTC).hour) == (15, 14)
    assert after.astimezone(UTC) - before.astimezone(UTC) == timedelta(hours=23)  # a 23-hour Saturday night


def test_reminders_are_06_55_local_and_none_out_of_season() -> None:
    assert reminder_at(BuntzenSpec(date=date(2027, 3, 15), passes=PASSES)) is None  # March: out of season
    assert reminder_at(BuntzenSpec(date=date(2027, 7, 1), passes=PASSES)) == at(date(2027, 6, 30), 6, 55)


def test_the_windows_match_the_shared_api_fixture() -> None:
    state = json.loads(API_STATE.read_text())
    for watch in (w for w in state["watches"] if w["provider"] == "buntzen"):
        spec = PROVIDER.load_spec(watch["spec"])
        w = PROVIDER.window(spec)
        assert (w.opens_at.timestamp(), w.closes_at.timestamp()) == (
            watch["check"]["opens_at"],
            watch["check"]["closes_at"],
        )
        placeholders = PROVIDER.evaluate(spec, {}, datetime.fromtimestamp(watch["check"]["opens_at"] - 60, UTC))
        assert [o.key for o in placeholders] == [o["key"] for o in watch["options"]]
        assert [o.label for o in placeholders] == [o["label"] for o in watch["options"]]
        assert [o.booking_url for o in placeholders] == [o["booking_url"] for o in watch["options"]]


def test_interval_bursts_for_twenty_minutes_after_the_release_then_steadies() -> None:
    assert interval(ALL, RELEASE - timedelta(minutes=5)) == BURST
    assert interval(ALL, RELEASE + timedelta(seconds=2)) == BURST
    assert interval(ALL, RELEASE + timedelta(minutes=19, seconds=59)) == BURST
    assert interval(ALL, RELEASE + timedelta(minutes=20)) == STEADY
    assert interval(ALL, at(DAY, 11)) == STEADY
    assert interval(ALL, (RELEASE + timedelta(minutes=10)).astimezone(UTC)) == BURST  # any aware zone


def test_interval_is_thirty_minutes_out_of_season() -> None:
    late = BuntzenSpec(date=date(2027, 10, 15), passes=PASSES)
    assert interval(late, at(date(2027, 10, 14), 7, 1)) == OFF_SEASON
    assert interval(late, at(date(2027, 10, 15), 11)) == OFF_SEASON


def test_reminder_at_is_06_55_the_day_before_in_season_only() -> None:
    assert reminder_at(ALL) == at(date(2027, 6, 30), 6, 55)
    assert reminder_at(BuntzenSpec(date=DAY, passes=PASSES, release_reminder=False)) is None
    assert reminder_at(BuntzenSpec(date=date(2027, 4, 30), passes=PASSES)) is None
    assert reminder_at(BuntzenSpec(date=date(2027, 5, 1), passes=PASSES)) == at(date(2027, 4, 30), 6, 55)


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2026, 5, 13), False),
        (date(2026, 5, 14), True),
        (date(2026, 9, 7), True),
        (date(2026, 9, 8), False),
        (date(2026, 9, 20), False),  # a known season replaces the estimate for its year
        (date(2027, 4, 30), False),
        (date(2027, 5, 1), True),
        (date(2027, 7, 1), True),
        (date(2027, 9, 30), True),
        (date(2027, 10, 1), False),
        (date(2028, 1, 15), False),
    ],
)
def test_in_season(d: date, expected: bool) -> None:
    assert in_season(d) is expected


@pytest.mark.parametrize("n", [1, 2, 3])
def test_queries_for_every_pass_subset(n: int) -> None:
    for passes in itertools.combinations(PASSES, n):
        got = queries(BuntzenSpec(date=DAY, passes=passes))
        expected = []
        if "full_day" in passes:
            expected.append(FULL_Q)
        if "morning" in passes or "afternoon" in passes:
            expected.append(HALF_Q)
        assert got == tuple(expected)
        assert len(set(got)) == len(got)
        assert PROVIDER.queries(BuntzenSpec(date=DAY, passes=passes)) == got


def test_options_list_the_wanted_first_then_the_unwanted_on_a_queried_place() -> None:
    keys = {
        passes: [(o.key, o.wanted) for o in evaluate(BuntzenSpec(date=DAY, passes=passes), {}, IN_WINDOW)]
        for n in (1, 2, 3)
        for passes in itertools.combinations(PASSES, n)
    }
    assert keys[("morning",)] == [("morning", True), ("afternoon", False)]
    assert keys[("afternoon",)] == [("afternoon", True), ("morning", False)]
    assert keys[("full_day",)] == [("full_day", True)]
    assert keys[("full_day", "afternoon")] == [("full_day", True), ("afternoon", True), ("morning", False)]
    assert keys[("morning", "afternoon")] == [("morning", True), ("afternoon", True)]
    assert keys[PASSES] == [("full_day", True), ("morning", True), ("afternoon", True)]
    assert all(len(v) <= MAX_OPTIONS for v in keys.values())


def test_the_keys_depend_on_the_spec_alone() -> None:
    full = {
        FULL_Q: inventory(FULL_Q, ("full_day", "open")),
        HALF_Q: inventory(HALF_Q, ("morning", "sold_out")),
    }
    for n in (1, 2, 3):
        for passes in itertools.combinations(PASSES, n):
            spec = BuntzenSpec(date=DAY, passes=passes)
            for now in (RELEASE - timedelta(hours=1), IN_WINDOW, at(DAY, 13), at(DAY, 19)):
                expected = [o.key for o in evaluate(spec, {}, now)]
                assert [o.key for o in evaluate(spec, full, now)] == expected
                assert [o.key for o in evaluate(spec, {FULL_Q: full[FULL_Q]}, now)] == expected


def test_each_options_fixed_fields() -> None:
    for option in evaluate(BuntzenSpec(date=DAY, passes=("morning",)), {}, IN_WINDOW):
        assert option.label == LABELS[option.key]  # type: ignore[index]
        assert option.booking_url == PAGES[option.key]  # type: ignore[index]
        assert option.detail is None


def test_over_by_stop_time_wins_over_everything() -> None:
    inv = {FULL_Q: inventory(FULL_Q, ("full_day", "open")), HALF_Q: inventory(HALF_Q, ("morning", "open"))}
    assert statuses(ALL, inv, at(DAY, 11, 59)) == {"full_day": "open", "morning": "open", "afternoon": "not_offered"}
    assert statuses(ALL, inv, at(DAY, 12)) == {"full_day": "open", "morning": "over", "afternoon": "not_offered"}
    assert statuses(ALL, inv, at(DAY, 18)) == {"full_day": "over", "morning": "over", "afternoon": "over"}
    assert statuses(ALL, {}, at(DAY, 18)) == {"full_day": "over", "morning": "over", "afternoon": "over"}


def test_without_an_inventory_an_option_is_not_released_before_the_release_else_unchecked() -> None:
    assert set(statuses(ALL, {}, RELEASE - timedelta(seconds=1)).values()) == {"not_released"}
    assert set(statuses(ALL, {}, RELEASE).values()) == {"unchecked"}
    # one query failed this round: its options are unchecked, the other's are read
    partial = statuses(ALL, {FULL_Q: inventory(FULL_Q, ("full_day", "open"))}, IN_WINDOW)
    assert partial == {"full_day": "open", "morning": "unchecked", "afternoon": "unchecked"}


@pytest.mark.parametrize(
    ("items", "morning"),
    [
        ((("morning", "open"), ("morning", "sold_out"), ("morning", "unknown")), "open"),
        ((("morning", "unknown"), ("morning", "sold_out"), ("morning", "not_released")), "maybe"),
        ((("morning", "not_released"), ("morning", "sold_out")), "not_released"),
        ((("morning", "sold_out"),), "sold_out"),
        ((("morning", "sold_out"), ("unknown", "not_released")), "sold_out"),  # its own item decides
        ((("unknown", "not_released"), ("unknown", "sold_out")), "not_released"),
        ((("unknown", "sold_out"),), "sold_out"),
        ((("afternoon", "open"),), "not_offered"),
        ((), "not_offered"),
    ],
)
def test_the_status_rules_in_order(items: tuple[tuple[str, str], ...], morning: str) -> None:
    got = statuses(BuntzenSpec(date=DAY, passes=("morning",)), {HALF_Q: inventory(HALF_Q, *items)}, IN_WINDOW)
    assert got["morning"] == morning


def test_maybe_carries_the_first_unknown_items_status_as_detail() -> None:
    inv = BuntzenInventory(
        place_id=10672,
        date="2027-07-01",
        items=[
            PassItem(period="morning", availability="sold_out", status="Sold Out"),
            PassItem(period="morning", availability="unknown", status="Waitlist only"),
            PassItem(period="afternoon", availability="unknown", status=""),
        ],
    )
    options = {
        o.key: o for o in evaluate(BuntzenSpec(date=DAY, passes=("morning", "afternoon")), {HALF_Q: inv}, IN_WINDOW)
    }
    assert (options["morning"].status, options["morning"].detail) == ("maybe", "Waitlist only")
    assert (options["afternoon"].status, options["afternoon"].detail) == ("maybe", None)  # empty: no detail


def test_unwanted_options_are_shown_but_never_alert() -> None:
    inv = {HALF_Q: inventory(HALF_Q, ("morning", "sold_out"), ("afternoon", "open"))}
    options = evaluate(BuntzenSpec(date=DAY, passes=("morning",)), inv, IN_WINDOW)
    afternoon = next(o for o in options if o.key == "afternoon")
    assert (afternoon.status, afternoon.wanted) == ("open", False)
    alertable = [o.key for o in options if o.wanted and o.status in ALERTABLE]  # what control alerts on
    assert alertable == []


def test_an_inventory_for_another_query_is_no_answer() -> None:
    wrong = BuntzenInventory(
        place_id=10671, date="2027-07-01", items=[PassItem(period="full_day", availability="open", status="x")]
    )
    assert statuses(ALL, {HALF_Q: wrong}, IN_WINDOW)["morning"] == "unchecked"
    other_day = BuntzenInventory(place_id=10672, date="2027-07-02", items=[])
    assert statuses(ALL, {HALF_Q: other_day}, IN_WINDOW)["morning"] == "unchecked"


def test_a_naive_now_is_refused() -> None:
    naive = datetime(2027, 6, 30, 9)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(ALL, {}, naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        interval(ALL, naive)


def option(key: str, status: str, wanted: bool = True) -> Option:
    return Option(key=key, label="x", status=status, wanted=wanted, booking_url=None)  # type: ignore[arg-type]


def test_booking_url_is_the_first_wanted_alertable_options_page_else_the_default() -> None:
    assert booking_url(ALL, (option("full_day", "sold_out"), option("afternoon", "maybe"))) == HALF_DAY_PAGE
    assert booking_url(ALL, (option("morning", "open"), option("full_day", "open"))) == HALF_DAY_PAGE
    assert booking_url(ALL, (option("morning", "open", wanted=False),)) == FULL_DAY_PAGE
    assert booking_url(ALL, ()) == FULL_DAY_PAGE
    assert booking_url(BuntzenSpec(date=DAY, passes=("afternoon",)), ()) == HALF_DAY_PAGE
    # a stored option's own link is never used, only our constant for its key
    evil = Option(key="morning", label="x", status="open", wanted=True, booking_url="https://evil.example/x")
    assert booking_url(ALL, (evil,)) == HALF_DAY_PAGE
    assert booking_url(BuntzenSpec(date=DAY, passes=("morning",)), (option("nope", "open"),)) == HALF_DAY_PAGE


def test_every_booking_url_lies_within_the_prefixes() -> None:
    rng = random.Random(7)  # noqa: S311 - test data, not security
    keys = [*PASSES, "other"]
    kinds = ["open", "maybe", "sold_out", "not_released", "not_offered", "over", "unchecked"]
    for _ in range(500):
        passes = tuple(p for p in PASSES if rng.random() < 0.6) or ("morning",)
        spec = BuntzenSpec(date=DAY, passes=passes)
        options = tuple(
            option(rng.choice(keys), rng.choice(kinds), rng.random() < 0.7) for _ in range(rng.randrange(4))
        )
        url = PROVIDER.booking_url(spec, options)
        assert url is not None
        assert META.allows_booking_url(url)
    for spec_option in evaluate(ALL, {}, IN_WINDOW):
        assert META.allows_booking_url(spec_option.booking_url or "")
