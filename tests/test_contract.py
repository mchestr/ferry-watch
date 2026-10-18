"""The design contract's shared pieces agree with each other (docs/waitlist/design.md §9.0). Verbatim."""

from __future__ import annotations

import json
from pathlib import Path

import msgspec
import pytest

from waitlist.fetchwire import FetchFailed, FetchOk, FetchRequest, decode_reply, decode_request, encode
from waitlist.providers import PROVIDER_IDS, REGISTRY
from waitlist.providers.base import ADMIN_NOTICE_KINDS, NOTICE_KINDS, Option, ProviderMeta, search_hash
from waitlist.providers.buntzen.meta import HOSTS, META

FIXTURE = Path(__file__).parent.parent / "web" / "src" / "test" / "fixtures" / "api-state.json"


def _fixture() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text())
    return data


def test_the_registry_has_exactly_the_providers_the_contract_names() -> None:
    assert {"bcferries", "buntzen"} == PROVIDER_IDS
    assert [m.id for m in REGISTRY.metas] == ["bcferries", "buntzen"]
    assert set(REGISTRY.http) == {"buntzen"}
    assert REGISTRY.meta("buntzen") is META
    assert HOSTS == ("api.yodelpass.com",)
    assert not NOTICE_KINDS & ADMIN_NOTICE_KINDS


def test_the_catalog_in_the_shared_fixture_is_the_metas() -> None:
    providers = _fixture()["providers"]
    assert isinstance(providers, list)
    entries = {p["id"]: {k: v for k, v in p.items() if k not in ("offered", "paused")} for p in providers}
    assert entries == {m.id: m.to_json() for m in REGISTRY.metas}


def test_the_fixtures_buntzen_options_are_options() -> None:
    watches = _fixture()["watches"]
    assert isinstance(watches, list)
    for watch in (w for w in watches if w["provider"] == "buntzen"):
        options = msgspec.convert(watch["options"], list[Option])
        assert all(META.allows_booking_url(o.booking_url or "") for o in options)
        assert META.allows_booking_url(watch["booking_url"])


def test_a_booking_prefix_must_be_https_and_a_directory() -> None:
    meta = REGISTRY.metas[0]
    with pytest.raises(ValueError, match="booking prefix"):
        ProviderMeta(**{**{f: getattr(meta, f) for f in meta.__slots__}, "booking_prefixes": ("http://x.example/",)})


def test_the_fetch_wire_round_trips_and_refuses_extras() -> None:
    request = FetchRequest(id="5be0d1c27f6a9e31", provider="buntzen", query={"place_id": 10672, "date": "2027-07-01"})
    assert decode_request(encode(request)) == request
    with pytest.raises(ValueError, match="unknown field"):
        decode_request(encode(request)[:-1] + b',"url":"https://evil.example"}')
    ok = FetchOk(id="5be0d1c27f6a9e31", inventory={"place_id": 10672}, elapsed_ms=3)
    failed = FetchFailed(id="5be0d1c27f6a9e31", error="site_changed", http_status=404)
    assert decode_reply(encode(ok)) == ok
    assert decode_reply(encode(failed)) == failed


def test_search_hash_is_stable() -> None:
    assert search_hash(("2027-07-01",)) == search_hash(("2027-07-01",))
    assert len(search_hash(("x", 1, None, True))) == 16
