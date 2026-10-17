"""A Buntzen watch's spec (design.md §5.2): every rule, its message and its order."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from waitlist.models import PACIFIC
from waitlist.providers.base import SpecError
from waitlist.providers.buntzen import PROVIDER
from waitlist.providers.buntzen.spec import (
    PASSES,
    BuntzenSpec,
    dump_spec,
    load_spec,
    parse_spec,
    search_key,
    title,
    watch_date,
)

# 09:00 Pacific on Wed Jun 30 2027, given as UTC (as control and web pass it).
NOW = datetime(2027, 6, 30, 9, 0, tzinfo=PACIFIC).astimezone(UTC)
TODAY = date(2027, 6, 30)


def refused(data: object, *, base: BuntzenSpec | None = None, now: datetime = NOW) -> tuple[str, str]:
    with pytest.raises(SpecError) as caught:
        parse_spec(data, base=base, now=now)  # type: ignore[arg-type]
    return caught.value.field, caught.value.message


def test_a_full_spec_parses_and_dumps_with_all_three_keys() -> None:
    spec = parse_spec(
        {"date": "2027-07-01", "passes": ["morning", "afternoon"], "release_reminder": False}, base=None, now=NOW
    )
    assert spec == BuntzenSpec(date=date(2027, 7, 1), passes=("morning", "afternoon"), release_reminder=False)
    assert dump_spec(spec) == {"date": "2027-07-01", "passes": ["morning", "afternoon"], "release_reminder": False}


def test_release_reminder_defaults_to_true_on_create() -> None:
    spec = parse_spec({"date": "2027-07-01", "passes": ["full_day"]}, base=None, now=NOW)
    assert spec.release_reminder is True
    assert dump_spec(spec)["release_reminder"] is True


def test_duplicates_collapse_and_the_order_is_canonical() -> None:
    spec = parse_spec(
        {"date": "2027-07-01", "passes": ["afternoon", "full_day", "afternoon", "morning"]}, base=None, now=NOW
    )
    assert spec.passes == PASSES == ("full_day", "morning", "afternoon")
    assert parse_spec({"date": "2027-07-01", "passes": ["afternoon", "morning"]}, base=None, now=NOW).passes == (
        "morning",
        "afternoon",
    )


@pytest.mark.parametrize(
    ("data", "error"),
    [
        # in the order of the table: the first that applies wins
        ([], ("spec", "Expected a JSON object")),
        ("2027-07-01", ("spec", "Expected a JSON object")),
        (None, ("spec", "Expected a JSON object")),
        ({"date": "2027-07-01", "passes": ["morning"], "zzz": 1, "id": "x"}, ("id", "Unknown field")),
        ({"enabled": True, "date": "2027-07-01", "passes": ["morning"]}, ("enabled", "Unknown field")),
        ({"bogus": 1}, ("bogus", "Unknown field")),
        ({"passes": ["morning"]}, ("date", "Required")),
        ({}, ("date", "Required")),
        ({"date": "July 1", "passes": "x"}, ("date", "Use a date like 2027-07-01")),
        ({"date": 20270701, "passes": ["morning"]}, ("date", "Use a date like 2027-07-01")),
        ({"date": "2027-7-1", "passes": ["morning"]}, ("date", "Use a date like 2027-07-01")),
        ({"date": "2027-07-01T00:00", "passes": ["morning"]}, ("date", "Use a date like 2027-07-01")),
        ({"date": "٢٠٢٧-07-01", "passes": ["morning"]}, ("date", "Use a date like 2027-07-01")),
        ({"date": "2027-02-30", "passes": "x"}, ("date", "That date doesn't exist")),
        ({"date": "2027-13-01", "passes": ["morning"]}, ("date", "That date doesn't exist")),
        ({"date": "2027-07-01"}, ("passes", "Pick at least one pass")),
        ({"date": "2027-07-01", "passes": []}, ("passes", "Pick at least one pass")),
        ({"date": "2027-07-01", "passes": "morning"}, ("passes", "Pick at least one pass")),
        ({"date": "2027-07-01", "passes": {"morning": True}}, ("passes", "Pick at least one pass")),
        ({"date": "2027-07-01", "passes": None}, ("passes", "Pick at least one pass")),
        ({"date": "2027-07-01", "passes": ["evening"]}, ("passes", "Passes are full_day, morning or afternoon")),
        ({"date": "2027-07-01", "passes": ["morning", 1]}, ("passes", "Passes are full_day, morning or afternoon")),
        ({"date": "2027-07-01", "passes": ["Morning"]}, ("passes", "Passes are full_day, morning or afternoon")),
        (
            {"date": "2020-01-01", "passes": ["morning"], "release_reminder": "yes"},
            ("release_reminder", "Must be true or false"),
        ),
        (
            {"date": "2027-07-01", "passes": ["morning"], "release_reminder": 1},
            ("release_reminder", "Must be true or false"),
        ),
        (
            {"date": "2027-07-01", "passes": ["morning"], "release_reminder": None},
            ("release_reminder", "Must be true or false"),
        ),
        ({"date": "2027-06-29", "passes": ["morning"]}, ("date", "Pick today or a later date")),
        ({"date": "2027-07-31", "passes": ["morning"]}, ("date", "Pick a date from today to 30 days ahead")),
    ],
)
def test_each_rule_has_its_field_and_message(data: object, error: tuple[str, str]) -> None:
    assert refused(data) == error


def test_the_horizon_is_today_plus_thirty_days_inclusive() -> None:
    assert parse_spec({"date": "2027-07-30", "passes": ["morning"]}, base=None, now=NOW).date == date(2027, 7, 30)
    assert parse_spec({"date": "2027-06-30", "passes": ["afternoon"]}, base=None, now=NOW).date == TODAY


def test_today_is_refused_only_when_every_chosen_pass_is_over() -> None:
    at_1230 = datetime(2027, 6, 30, 12, 30, tzinfo=PACIFIC)
    assert refused({"date": "2027-06-30", "passes": ["morning"]}, now=at_1230) == (
        "passes",
        "Those passes are over for today",
    )
    assert parse_spec({"date": "2027-06-30", "passes": ["morning", "afternoon"]}, base=None, now=at_1230).date == TODAY
    at_1800 = datetime(2027, 6, 30, 18, 0, tzinfo=PACIFIC)
    assert refused({"date": "2027-06-30", "passes": ["full_day", "afternoon"]}, now=at_1800) == (
        "passes",
        "Those passes are over for today",
    )
    assert parse_spec({"date": "2027-07-01", "passes": ["morning"]}, base=None, now=at_1800).date == date(2027, 7, 1)


def test_today_is_pacific_whatever_zone_now_is_given_in() -> None:
    # 01:00 UTC on Jul 1 is 18:00 Pacific on Jun 30: Jun 30 is still today, Jun 29 is not.
    now = datetime(2027, 7, 1, 1, 0, tzinfo=UTC)
    assert refused({"date": "2027-06-29", "passes": ["morning"]}, now=now) == ("date", "Pick today or a later date")
    assert refused({"date": "2027-06-30", "passes": ["full_day"]}, now=now) == (
        "passes",
        "Those passes are over for today",
    )
    assert parse_spec({"date": "2027-07-30", "passes": ["morning"]}, base=None, now=now).date == date(2027, 7, 30)


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_spec({"date": "2027-07-01", "passes": ["morning"]}, base=None, now=datetime(2027, 6, 30, 9))  # noqa: DTZ001


def test_bools_and_ints_are_never_each_other() -> None:
    assert refused({"date": "2027-07-01", "passes": ["morning"], "release_reminder": 0}) == (
        "release_reminder",
        "Must be true or false",
    )
    assert refused({"date": True, "passes": ["morning"]}) == ("date", "Use a date like 2027-07-01")
    assert refused({"date": "2027-07-01", "passes": [True]}) == ("passes", "Passes are full_day, morning or afternoon")


BASE = BuntzenSpec(date=TODAY, passes=("morning",), release_reminder=True)


def test_a_partial_update_merges_over_the_base() -> None:
    assert parse_spec({"passes": ["full_day", "morning"]}, base=BASE, now=NOW) == BuntzenSpec(
        date=TODAY, passes=("full_day", "morning"), release_reminder=True
    )
    assert parse_spec({"release_reminder": False}, base=BASE, now=NOW) == BuntzenSpec(
        date=TODAY, passes=("morning",), release_reminder=False
    )
    assert parse_spec({}, base=BASE, now=NOW) == BASE
    assert parse_spec({"date": "2027-07-02"}, base=BASE, now=NOW).date == date(2027, 7, 2)


def test_a_partial_update_still_refuses_bad_values_and_unknown_keys() -> None:
    assert refused({"passes": []}, base=BASE) == ("passes", "Pick at least one pass")
    assert refused({"enabled": False}, base=BASE) == ("enabled", "Unknown field")
    assert refused({"release_reminder": "no"}, base=BASE) == ("release_reminder", "Must be true or false")
    assert refused({"date": None}, base=BASE) == ("date", "Use a date like 2027-07-01")


def test_date_rules_apply_only_when_the_date_changes() -> None:
    after_noon = datetime(2027, 6, 30, 13, 0, tzinfo=PACIFIC)  # the morning pass is over for today
    yesterday = BuntzenSpec(date=date(2027, 6, 29), passes=("morning",))
    # Toggling the reminder on a watch for today, or for yesterday, never fails.
    assert parse_spec({"release_reminder": False}, base=BASE, now=after_noon).release_reminder is False
    assert parse_spec({"passes": ["morning"]}, base=yesterday, now=after_noon).date == date(2027, 6, 29)
    assert parse_spec({"date": "2027-06-29"}, base=yesterday, now=after_noon).date == date(2027, 6, 29)  # same date
    # A new date is checked as on create.
    assert refused({"date": "2027-06-28"}, base=yesterday, now=after_noon) == ("date", "Pick today or a later date")
    assert refused({"date": "2027-06-30"}, base=yesterday, now=after_noon) == (
        "passes",
        "Those passes are over for today",
    )
    assert refused({"date": "2027-08-30"}, base=BASE) == ("date", "Pick a date from today to 30 days ahead")


@pytest.mark.parametrize(
    "spec",
    [
        BuntzenSpec(date=date(2027, 7, 1), passes=("full_day",)),
        BuntzenSpec(date=date(2027, 7, 1), passes=("morning", "afternoon"), release_reminder=False),
        BuntzenSpec(date=date(2027, 7, 1), passes=PASSES),
    ],
)
def test_load_spec_round_trips_dump_spec(spec: BuntzenSpec) -> None:
    assert load_spec(dump_spec(spec)) == spec
    assert PROVIDER.load_spec(PROVIDER.dump_spec(spec)) == spec


def test_load_spec_accepts_yesterdays_watch_and_any_date() -> None:
    stored = {"date": "2020-01-01", "passes": ["morning"], "release_reminder": True}
    assert load_spec(stored) == BuntzenSpec(date=date(2020, 1, 1), passes=("morning",))


@pytest.mark.parametrize(
    ("stored", "error"),
    [
        ([], ("spec", "Expected a JSON object")),
        ({"date": "2027-07-01", "passes": ["morning"]}, ("release_reminder", "Required")),
        ({"passes": ["morning"], "release_reminder": True}, ("date", "Required")),
        ({"date": "2027-07-01", "release_reminder": True}, ("passes", "Required")),
        (
            {"date": "2027-07-01", "passes": ["morning"], "release_reminder": True, "enabled": True},
            ("enabled", "Unknown field"),
        ),
        ({"date": "2027-02-30", "passes": ["morning"], "release_reminder": True}, ("date", "That date doesn't exist")),
        ({"date": "2027-07-01", "passes": [], "release_reminder": True}, ("passes", "Pick at least one pass")),
        (
            {"date": "2027-07-01", "passes": ["x"], "release_reminder": True},
            ("passes", "Passes are full_day, morning or afternoon"),
        ),
        (
            {"date": "2027-07-01", "passes": ["morning"], "release_reminder": 1},
            ("release_reminder", "Must be true or false"),
        ),
    ],
)
def test_load_spec_applies_the_shape_rules(stored: Any, error: tuple[str, str]) -> None:
    with pytest.raises(SpecError) as caught:
        load_spec(stored)
    assert (caught.value.field, caught.value.message) == error


def test_the_other_spec_methods() -> None:
    spec = BuntzenSpec(date=date(2027, 7, 1), passes=("morning", "afternoon"))
    assert search_key(spec) == (date(2027, 7, 1),)
    assert search_key(BuntzenSpec(date=date(2027, 7, 1), passes=("full_day",))) == search_key(spec)  # passes kept
    assert watch_date(spec) == date(2027, 7, 1)
    assert title(spec) == "Buntzen Lake, Thu Jul 1: morning or afternoon pass"
    assert title(BuntzenSpec(date=date(2027, 7, 1), passes=("full_day",))) == "Buntzen Lake, Thu Jul 1: all-day pass"
    assert title(BuntzenSpec(date=date(2027, 8, 14), passes=PASSES)) == (
        "Buntzen Lake, Sat Aug 14: all-day, morning or afternoon pass"
    )
    assert PROVIDER.title(spec) == title(spec)
    assert PROVIDER.watch_date(spec) == spec.date
    assert PROVIDER.search_key(spec) == search_key(spec)
