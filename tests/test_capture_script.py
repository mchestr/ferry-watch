"""scripts/capture_yodel.py (design.md §5.8) and the Buntzen fixtures' README. Nothing here reaches Yodel."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

from waitlist.providers.base import FetchError, HttpResponse
from waitlist.providers.buntzen.client import USER_AGENT, parse_catalog
from waitlist.providers.buntzen.model import BuntzenQuery

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "capture_yodel.py"
FIXTURES = ROOT / "tests" / "fixtures" / "buntzen"
README_FIRST_LINE = (
    "Synthesized from the Home Assistant automation's field names; not captured from Yodel. "
    "Replace with `captured-*` files from scripts/capture_yodel.py."
)


def payload(place: int, day: str) -> str:
    return (
        f'{{"userId":0,"placeId":"{place}","reservationDate":"{day}","catalogItemType":0,'
        '"scopeName":"buntzen lake","filterStartDate":null,"filterEndDate":null}'
    )


def request_text(place: int, day: str) -> str:
    return (
        "POST https://api.yodelpass.com/api/portal-catalog-items\n"
        f"user-agent: {USER_AGENT}\n"
        "x-api-version: 5.6\n"
        "content-type: application/json\n"
        "\n"
        f"{payload(place, day)}\n"
    )


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_yodel", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.usefixtures("socket_disabled")
def test_dry_run_prints_the_exact_requests_and_opens_no_socket(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    script.main(["--date", "2027-05-20", "--dry-run"])
    out = capsys.readouterr().out
    assert request_text(10671, "2027-05-20") in out
    assert request_text(10672, "2027-05-20") in out
    assert out.count("POST ") == 2
    assert "accept" not in out.lower().replace("content-type", "")  # exactly Home Assistant's three headers
    script.main(["--date", "2027-05-20", "--place", "half", "--dry-run"])
    out = capsys.readouterr().out
    assert out.count("POST ") == 1
    assert request_text(10672, "2027-05-20") in out


def test_dry_run_from_the_command_line_needs_no_date() -> None:
    # The environment is passed on as it is: PYTEST_CURRENT_TEST stays set, so a --dry-run that stopped working
    # would be refused by the script's own guard instead of reaching Yodel from outside pytest-socket.
    assert os.environ.get("PYTEST_CURRENT_TEST")
    done = subprocess.run(  # noqa: S603 - our own script, no shell
        [sys.executable, str(SCRIPT), "--dry-run"], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("POST https://api.yodelpass.com/api/portal-catalog-items") == 2
    assert re.search(r'"reservationDate":"\d{4}-\d{2}-\d{2}"', done.stdout)


def test_a_real_run_refuses_under_pytest_or_ci(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_session(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused capture must not open a session")

    monkeypatch.setattr(script, "upstream_session", no_session)
    monkeypatch.delenv("CI", raising=False)  # CI is checked first, and GitHub Actions sets it; it has its turn below
    assert os.environ.get("PYTEST_CURRENT_TEST")  # pytest sets it for every test
    with pytest.raises(SystemExit) as caught:
        script.main(["--date", "2027-05-20"])
    assert "refusing" in str(caught.value.code)
    assert "PYTEST_CURRENT_TEST" in str(caught.value.code)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(SystemExit) as caught:
        script.main(["--date", "2027-05-20", "--place", "full"])
    assert "CI" in str(caught.value.code)


@pytest.mark.parametrize(
    "argv",
    [
        ["--date", "2027-5-20"],
        ["--date", "2027-02-30"],
        ["--date", "tomorrow"],
        ["--place", "middle"],
        ["--every", "4"],
        ["--repeat", "0"],
        ["--repeat", "121"],
    ],
)
def test_bad_arguments_are_refused(script: ModuleType, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        script.main([*argv, "--dry-run"])
    assert caught.value.code == 2


def test_a_capture_is_written_as_a_body_and_its_meta(script: ModuleType, tmp_path: Path) -> None:
    query = BuntzenQuery(place_id=10672, date="2027-05-20")
    body = json.dumps(
        {"catalogItemExtendedResultLst": [{"catalogItemCategory": "Half Day Pass", "catalogItemStatus": "Morning"}]}
    ).encode()
    response = HttpResponse(status=200, content_type="application/json", body=body, retry_after_s=None)
    at = datetime(2027, 5, 19, 14, 0, 2, tzinfo=UTC)
    path, meta = script.record(tmp_path, "half", query, response, at)
    assert path.name == "captured-half-2027-05-20-20270519T140002Z.json"
    assert json.loads(path.read_text()) == json.loads(body)
    assert path.read_text().startswith("{\n")  # pretty-printed
    stored = json.loads((tmp_path / "captured-half-2027-05-20-20270519T140002Z.meta.json").read_text())
    assert stored == meta
    assert set(stored) == {"captured_at", "status", "content_type", "request", "parsed"}  # no response headers
    assert stored["request"] == {
        "url": "https://api.yodelpass.com/api/portal-catalog-items",
        "payload": json.loads(payload(10672, "2027-05-20")),
    }
    assert stored["parsed"] == {
        "place_id": 10672,
        "date": "2027-05-20",
        "items": [{"period": "morning", "availability": "open", "status": "Morning"}],
    }


def test_a_capture_that_isnt_json_is_kept_raw_with_its_error(script: ModuleType, tmp_path: Path) -> None:
    query = BuntzenQuery(place_id=10671, date="2027-05-20")
    response = HttpResponse(status=403, content_type="text/html", body=b"<html>blocked</html>")
    path, meta = script.record(tmp_path, "full", query, response, datetime(2027, 5, 19, 14, 0, 2, tzinfo=UTC))
    assert path.suffix == ".txt"
    assert path.read_bytes() == b"<html>blocked</html>"
    assert meta["parsed"] == {"error": "blocked"}
    assert meta["status"] == 403


def test_a_json_body_utf8_cant_hold_is_kept_raw_as_its_only_body_file(script: ModuleType, tmp_path: Path) -> None:
    query = BuntzenQuery(place_id=10672, date="2027-05-20")
    body = b'{"catalogItemExtendedResultLst": [], "note": "\\ud800"}'  # valid JSON; a lone surrogate once decoded
    response = HttpResponse(status=200, content_type="application/json", body=body)
    path, _meta = script.record(tmp_path, "half", query, response, datetime(2027, 5, 19, 14, 0, 2, tzinfo=UTC))
    assert path.suffix == ".txt"
    assert path.read_bytes() == body
    stem = "captured-half-2027-05-20-20270519T140002Z"
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{stem}.meta.json", f"{stem}.txt"]  # no empty .json


class FakeTransport:
    """Stands in for AiohttpTransport inside the script: canned answers, every request recorded."""

    made: ClassVar[list[FakeTransport]] = []

    def __init__(self, session: object, hosts: frozenset[str], *, max_bytes: int) -> None:  # noqa: ARG002
        self.hosts = hosts
        self.max_bytes = max_bytes
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        FakeTransport.made.append(self)

    async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse:
        self.requests.append((url, dict(payload), dict(headers)))
        if len(self.requests) == 2:
            raise FetchError("timeout")
        return HttpResponse(status=200, content_type="application/json", body=b'{"catalogItemExtendedResultLst": []}')


async def test_a_capture_run_waits_between_requests_and_records_each_answer(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(script, "AiohttpTransport", FakeTransport)
    monkeypatch.setattr(script.asyncio, "sleep", no_wait)
    FakeTransport.made.clear()
    args = script.parse_args(["--date", "2027-05-20", "--repeat", "2", "--every", "30", "--out", str(tmp_path)])
    out = io.StringIO()
    await script.capture(args, out)
    (transport,) = FakeTransport.made
    assert transport.hosts == frozenset({"api.yodelpass.com"})
    assert transport.max_bytes == 256 * 1024
    assert [p["placeId"] for _, p, _ in transport.requests] == ["10671", "10672", "10671", "10672"]
    assert sleeps == [5.0, 30.0, 5.0]  # never under 5 s between two requests
    assert "no response (timeout)" in out.getvalue()
    metas = sorted(tmp_path.glob("captured-*.meta.json"))
    assert len(metas) == 3  # the timed-out request left nothing to record
    for meta_path in metas:
        meta = json.loads(meta_path.read_text())
        stem = meta_path.name.removesuffix(".meta.json")
        query = BuntzenQuery(
            place_id=int(meta["request"]["payload"]["placeId"]),  # type: ignore[arg-type]
            date=meta["request"]["payload"]["reservationDate"],
        )
        body = (tmp_path / f"{stem}.json").read_bytes()
        reply = HttpResponse(status=meta["status"], content_type=meta["content_type"], body=body)
        assert meta["parsed"] == {"place_id": query.place_id, "date": "2027-05-20", "items": []}
        assert parse_catalog(reply, query).items == []


def test_every_fixture_is_synthesized_or_captured_and_named_in_the_readme() -> None:
    readme = (FIXTURES / "README.md").read_text()
    assert readme.splitlines()[0] == README_FIRST_LINE
    files = sorted(p.name for p in FIXTURES.iterdir() if p.name != "README.md")
    assert files
    for name in files:
        assert re.fullmatch(r"(synth-[a-z0-9-]+\.(json|html)|captured-[A-Za-z0-9-]+(\.meta)?\.(json|txt))", name), name
        assert f"`{name}`" in readme, f"{name} isn't listed in tests/fixtures/buntzen/README.md"
    for name in files:
        if name.startswith("captured-") and name.endswith(".meta.json"):
            stem = name.removesuffix(".meta.json")
            assert any(f in (f"{stem}.json", f"{stem}.txt") for f in files), f"{name} has no body"
