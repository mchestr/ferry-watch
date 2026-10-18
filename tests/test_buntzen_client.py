"""Buntzen's client (design.md §5.3-5.4): the exact request, and how each HTTP answer is read. A fake Transport."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import msgspec
import pytest

from waitlist.providers import buntzen
from waitlist.providers.base import FetchError, HttpResponse
from waitlist.providers.buntzen.client import API_URL, FETCHER, USER_AGENT, Client, build_request, parse_catalog
from waitlist.providers.buntzen.meta import HOSTS
from waitlist.providers.buntzen.model import BuntzenInventory, BuntzenQuery, PassItem

QUERY = BuntzenQuery(place_id=10672, date="2027-07-01")
GOLDEN_PAYLOAD = (
    b'{"userId":0,"placeId":"10672","reservationDate":"2027-07-01","catalogItemType":0,'
    b'"scopeName":"buntzen lake","filterStartDate":null,"filterEndDate":null}'
)
SOLD_OUT = json.dumps(
    {"catalogItemExtendedResultLst": [{"catalogItemCategory": "Half Day Pass", "catalogItemStatus": "Sold Out"}]}
).encode()


class FakeTransport:
    """Records each request and answers with a fixed response (or raises)."""

    def __init__(self, response: HttpResponse | Exception) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.gets: list[str] = []

    async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse:
        self.posts.append((url, dict(payload), dict(headers)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse:  # noqa: ARG002
        self.gets.append(url)
        raise AssertionError("Buntzen never GETs")


def test_the_request_is_home_assistants() -> None:
    url, payload, headers = build_request(QUERY)
    assert url == API_URL == "https://api.yodelpass.com/api/portal-catalog-items"
    assert headers == {
        "user-agent": USER_AGENT,
        "x-api-version": "5.6",
        "content-type": "application/json",
    }  # exactly these three: no accept, no cookie, nothing else
    assert USER_AGENT == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0 Safari/537.36"
    )
    assert msgspec.json.encode(payload) == GOLDEN_PAYLOAD  # what the transport sends, key order and all
    assert isinstance(payload["placeId"], str)  # a string, as Home Assistant sent it


def test_the_full_day_place_and_another_date() -> None:
    _, payload, _ = build_request(BuntzenQuery(place_id=10671, date="2027-05-20"))
    assert (payload["placeId"], payload["reservationDate"]) == ("10671", "2027-05-20")


def test_the_client_is_buntzens_fetcher() -> None:
    assert isinstance(FETCHER, Client)
    assert FETCHER.provider_id == "buntzen"
    assert FETCHER.hosts == HOSTS == ("api.yodelpass.com",)


async def test_fetch_posts_the_request_and_parses_the_answer() -> None:
    transport = FakeTransport(HttpResponse(status=200, content_type="application/json", body=SOLD_OUT))
    inventory = await FETCHER.fetch(transport, QUERY)
    assert inventory == BuntzenInventory(
        place_id=10672,
        date="2027-07-01",
        items=[PassItem(period="unknown", availability="sold_out", status="Sold Out")],
    )
    assert transport.posts == [build_request(QUERY)]
    assert transport.gets == []


async def test_a_transports_failure_passes_through() -> None:
    transport = FakeTransport(FetchError("timeout"))
    with pytest.raises(FetchError, match="timeout"):
        await FETCHER.fetch(transport, QUERY)


@pytest.mark.parametrize("bad", ["2027-13-01", "2027-02-30", "0000-00-00"])
async def test_a_date_that_isnt_on_the_calendar_is_never_sent(bad: str) -> None:
    transport = FakeTransport(HttpResponse(status=200, content_type="application/json", body=SOLD_OUT))
    with pytest.raises(FetchError, match="unexpected"):
        await FETCHER.fetch(transport, BuntzenQuery(place_id=10672, date=bad))
    assert transport.posts == []


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, "rate_limited"),
        (401, "blocked"),
        (403, "blocked"),
        (400, "site_changed"),
        (404, "site_changed"),
        (405, "site_changed"),
        (406, "site_changed"),
        (410, "site_changed"),
        (415, "site_changed"),
        (422, "site_changed"),
        (426, "site_changed"),
        (301, "site_changed"),
        (302, "site_changed"),
        (307, "site_changed"),
        (500, "upstream_error"),
        (502, "upstream_error"),
        (503, "upstream_error"),
        (504, "upstream_error"),
        (201, "unexpected"),
        (204, "unexpected"),
        (409, "unexpected"),
        (418, "unexpected"),
    ],
)
async def test_every_status_mapping(status: int, kind: str) -> None:
    response = HttpResponse(status=status, content_type="application/json", body=SOLD_OUT, retry_after_s=120)
    with pytest.raises(FetchError) as caught:
        await FETCHER.fetch(FakeTransport(response), QUERY)
    assert caught.value.kind == kind
    assert caught.value.http_status == status
    # Retry-After is kept only where it means something: a 429 and a 503
    assert caught.value.retry_after_s == (120 if status in (429, 503) else None)


async def test_an_html_content_type_is_a_changed_site_even_with_a_json_body() -> None:
    response = HttpResponse(status=200, content_type="text/html", body=SOLD_OUT)
    with pytest.raises(FetchError) as caught:
        await FETCHER.fetch(FakeTransport(response), QUERY)
    assert (caught.value.kind, caught.value.http_status) == ("site_changed", 200)


def test_parse_catalog_is_what_fetch_runs() -> None:
    response = HttpResponse(status=200, content_type="application/json", body=SOLD_OUT)
    assert parse_catalog(response, QUERY).items[0].availability == "sold_out"


def test_the_provider_package_never_imports_its_client() -> None:
    """Control and web import waitlist.providers; only waitlist-fetcher may load the client (design.md §2.2)."""
    source = (Path(buntzen.__file__).parent / "__init__.py").read_text()
    imported = [node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)] + [
        alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Import) for alias in node.names
    ]
    assert not any(name == "client" or name.endswith(".client") for name in imported)
    script = (
        "import sys, waitlist.providers, waitlist.providers.buntzen; "
        "sys.exit('waitlist.providers.buntzen.client' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", script], check=False).returncode == 0  # noqa: S603
