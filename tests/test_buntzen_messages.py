"""What Buntzen's notifications say (design.md §5.7)."""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any

import pytest

from waitlist.providers.base import FAILING, NOTICE_KINDS, OPENING, RELEASE_REMINDER, Notice
from waitlist.providers.buntzen import PROVIDER
from waitlist.providers.buntzen.messages import passes_phrase, render
from waitlist.providers.buntzen.meta import FULL_DAY_PAGE, HALF_DAY_PAGE, META
from waitlist.providers.buntzen.spec import PASSES, BuntzenSpec

URL = "https://waitlist.chestr.dev/w/0f3c9a1b2d4e5f60"
SPEC = BuntzenSpec(date=date(2027, 7, 1), passes=PASSES)
READY = "Book it on Yodel now: passes go fast. Have your Yodel account, licence plate and mobile number ready."


def opening(options: list[str], uncertain: list[str], spec: BuntzenSpec = SPEC) -> Notice:
    notice = render(OPENING, {"options": options, "uncertain": uncertain}, spec, watch_url=URL)
    assert notice is not None
    return notice


def test_pass_names() -> None:
    assert passes_phrase(["morning"]) == "morning pass"
    assert passes_phrase(["full_day"]) == "all-day pass"
    assert passes_phrase(["morning", "afternoon"]) == "morning and afternoon passes"
    assert passes_phrase(["full_day", "morning", "afternoon"]) == "all-day and morning and afternoon passes"


@pytest.mark.parametrize(
    ("options", "title", "also"),
    [
        (["full_day"], "Buntzen Lake: all-day pass open for Thu Jul 1", FULL_DAY_PAGE),
        (["morning"], "Buntzen Lake: morning pass open for Thu Jul 1", HALF_DAY_PAGE),
        (["afternoon", "morning"], "Buntzen Lake: morning and afternoon passes open for Thu Jul 1", HALF_DAY_PAGE),
        (
            ["full_day", "morning", "afternoon"],
            "Buntzen Lake: all-day and morning and afternoon passes open for Thu Jul 1",
            FULL_DAY_PAGE,
        ),
    ],
)
def test_an_opening_that_is_certain(options: list[str], title: str, also: str) -> None:
    notice = opening(options, [])
    assert notice == Notice(
        title=title, body=READY, url=URL, urgency="high", ttl=900, topic=None, also=("Book on Yodel", also)
    )


def test_a_mixed_opening_names_the_uncertain_ones_in_the_body() -> None:
    notice = opening(["morning", "full_day"], ["full_day"])
    assert notice.title == "Buntzen Lake: morning pass open for Thu Jul 1"
    assert notice.body == (
        READY + " Yodel also shows the all-day pass with a status we don't recognise; it may be bookable."
    )
    assert notice.also == ("Book on Yodel", HALF_DAY_PAGE)  # the first certain pass's page
    notice = opening(["full_day", "morning", "afternoon"], ["morning", "afternoon"])
    assert notice.title == "Buntzen Lake: all-day pass open for Thu Jul 1"
    assert notice.body == (
        READY
        + " Yodel also shows the morning and afternoon passes with a status we don't recognise; it may be bookable."
    )
    assert notice.also == ("Book on Yodel", FULL_DAY_PAGE)
    assert (notice.urgency, notice.ttl) == ("high", 900)


@pytest.mark.parametrize(
    ("uncertain", "title", "also"),
    [
        (["afternoon"], "Buntzen Lake: afternoon pass may be open for Thu Jul 1", HALF_DAY_PAGE),
        (
            ["morning", "afternoon"],
            "Buntzen Lake: morning and afternoon passes may be open for Thu Jul 1",
            HALF_DAY_PAGE,
        ),
        (PASSES, "Buntzen Lake: all-day and morning and afternoon passes may be open for Thu Jul 1", FULL_DAY_PAGE),
    ],
)
def test_an_opening_that_is_all_uncertain(uncertain: list[str], title: str, also: str) -> None:
    notice = opening(list(uncertain), list(uncertain))
    assert notice == Notice(
        title=title,
        body="Yodel shows a status we don't recognise; it may be bookable. Check on Yodel now.",
        url=URL,
        urgency="high",
        ttl=900,
        also=("Book on Yodel", also),
    )


def test_the_release_reminder() -> None:
    notice = render(RELEASE_REMINDER, {}, BuntzenSpec(date=date(2027, 7, 1), passes=("morning",)), watch_url=URL)
    assert notice == Notice(
        title="Buntzen Lake passes for Thu Jul 1 go on sale at 7:00 am",
        body=(
            "Popular passes sell out within seconds. Open Yodel and sign in now; we'll alert you if one opens up later."
        ),
        url=URL,
        urgency="high",
        ttl=600,
        also=("Open Yodel", HALF_DAY_PAGE),
    )
    full = render(RELEASE_REMINDER, {}, SPEC, watch_url=URL)
    assert full is not None
    assert full.also == ("Open Yodel", FULL_DAY_PAGE)


def test_failing() -> None:
    notice = render(FAILING, {"error_class": "site_changed"}, SPEC, watch_url=URL)
    assert notice == Notice(
        title="Having trouble checking Buntzen Lake",
        body="We'll keep trying. Until then, check Yodel yourself for Thu Jul 1.",
        url=URL,
        urgency="low",
        ttl=3600,
        also=("Open Yodel", FULL_DAY_PAGE),
    )


@pytest.mark.parametrize("kind", ["bookable", "cart_held", "site_changed", "checker_down", "test", "", "opening "])
def test_other_kinds_render_nothing(kind: str) -> None:
    assert render(kind, {"options": ["morning"], "uncertain": []}, SPEC, watch_url=URL) is None


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"options": ["morning"]},  # uncertain missing
        {"uncertain": []},
        {"options": [], "uncertain": []},
        {"options": "morning", "uncertain": []},
        {"options": ["morning", "morning"], "uncertain": []},
        {"options": ["evening"], "uncertain": []},
        {"options": [1], "uncertain": []},
        {"options": ["morning"], "uncertain": ["afternoon"]},  # not a subset
        {"options": ["morning"], "uncertain": ["morning", "morning"]},
        {"options": ["morning"], "uncertain": None},
        {"options": ["morning"] * 11, "uncertain": []},
    ],
)
def test_bad_opening_params_give_none(params: dict[str, Any]) -> None:
    assert render(OPENING, params, SPEC, watch_url=URL) is None


def test_params_that_arent_an_object_give_none() -> None:
    for kind in sorted(NOTICE_KINDS):
        assert render(kind, ["morning"], SPEC, watch_url=URL) is None  # type: ignore[arg-type]


def every_notice() -> list[Notice]:
    notices: list[Notice] = []
    specs = [
        BuntzenSpec(date=date(2027, 7, d), passes=p)
        for d in (1, 31)
        for n in (1, 2, 3)
        for p in itertools.combinations(PASSES, n)
    ]
    for spec in specs:
        notices.extend(
            opening(list(options), list(uncertain), spec)
            for n in (1, 2, 3)
            for options in itertools.permutations(PASSES, n)
            for k in range(n + 1)
            for uncertain in itertools.combinations(options, k)
        )
        for kind, params in ((RELEASE_REMINDER, {}), (FAILING, {"error_class": "timeout"})):
            notice = PROVIDER.render(kind, params, spec, watch_url=URL)
            assert notice is not None
            notices.append(notice)
    return notices


def test_every_notice_links_only_to_the_watch_and_a_booking_page() -> None:
    notices = every_notice()
    assert len(notices) > 500
    for notice in notices:
        assert notice.url == URL
        assert notice.topic is None  # the outbox sets it
        assert notice.also is not None
        assert META.allows_booking_url(notice.also[1])
        assert len(notice.title) <= 120
        assert len(notice.body) <= 300
        assert "http" not in notice.title + notice.body


def test_no_site_text_reaches_a_notice() -> None:
    # render never sees options or inventories: only pass ids from params, the spec and constants. A status a
    # site sent (an Option.detail) has no way in; a pass id that isn't one of ours is refused outright.
    marker = "Waitlist only"
    assert render(OPENING, {"options": ["morning"], "uncertain": [], "detail": marker}, SPEC, watch_url=URL) == opening(
        ["morning"], []
    )
    assert render(OPENING, {"options": [marker], "uncertain": [marker]}, SPEC, watch_url=URL) is None
    for notice in every_notice():
        assert marker not in notice.title + notice.body
