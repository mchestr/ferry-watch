"""Capture Yodel's real answers for Buntzen Lake as parser fixtures (docs/waitlist/design.md §5.8, §11 Q1).

    uv run python scripts/capture_yodel.py --date 2027-05-20 [--place full|half|both] [--repeat N --every SECONDS]
        [--out tests/fixtures/buntzen] [--dry-run]

The owner runs it from a workstation on the home network, never in CI: once out of season (the envelope still
shows Yodel's shape) and again on the first day of the 2027 season around 07:00 (docs/providers/buntzen.md).
`--date` defaults to tomorrow (Pacific), the date whose passes go on sale at 07:00 today.

- `--dry-run` prints the exact request for each place (`build_request`: the URL, the three headers, the payload
  bytes) and sends nothing.
- Otherwise it refuses to run when CI or PYTEST_CURRENT_TEST is set. It sends Buntzen's request through
  waitlist-fetcher's own transport: https to api.yodelpass.com only, public addresses only, no redirects, no
  cookies, a 256 KiB cap. It waits at least 5 s between requests.
- For each response it writes `captured-<place>-<date>-<YYYYMMDDTHHMMSSZ>.json` (the body, pretty-printed if it
  is JSON, else raw as `.txt`) and, beside it, `….meta.json`: captured_at, status, content_type, the request (url
  and payload), and what the parser made of it (the inventory, or {"error": kind}). No response header is
  stored, so no cookie reaches the repo. It prints the parsed result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, TextIO

import msgspec

from waitlist.fetcher.app import upstream_session
from waitlist.fetcher.config import FetcherConfig
from waitlist.fetcher.transport import AiohttpTransport
from waitlist.models import PACIFIC
from waitlist.providers.base import FetchError, HttpResponse
from waitlist.providers.buntzen.client import build_request, parse_catalog
from waitlist.providers.buntzen.meta import HOSTS
from waitlist.providers.buntzen.model import FULL_DAY_PLACE, HALF_DAY_PLACE, BuntzenQuery

PlaceName = Literal["full", "half"]
PLACES: Final[dict[PlaceName, Literal[10671, 10672]]] = {"full": FULL_DAY_PLACE, "half": HALF_DAY_PLACE}
MAX_BYTES: Final = 256 * 1024
MIN_GAP_S: Final = 5.0
DEFAULT_OUT: Final = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "buntzen"


def _date(text: str) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or len(text) != 10:
        raise argparse.ArgumentTypeError(f"not a date like 2027-05-20: {text!r}")
    return day


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="capture_yodel", description=__doc__.split("\n\n")[0])
    tomorrow = datetime.now(PACIFIC).date() + timedelta(days=1)
    parser.add_argument("--date", type=_date, default=tomorrow, help="the day at the lake (default: tomorrow)")
    parser.add_argument("--place", choices=["full", "half", "both"], default="both")
    parser.add_argument("--repeat", type=int, default=1, help="rounds of requests (1-120)")
    parser.add_argument("--every", type=float, default=60.0, help="seconds between rounds (at least 5)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="print the requests and send nothing")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 120:
        parser.error("--repeat must be from 1 to 120")
    if args.every < MIN_GAP_S:
        parser.error(f"--every must be at least {MIN_GAP_S:g} seconds")
    return args


def queries(place: str, day: date) -> list[tuple[PlaceName, BuntzenQuery]]:
    names: list[PlaceName] = ["full", "half"] if place == "both" else [place]  # type: ignore[list-item]
    return [(name, BuntzenQuery(place_id=PLACES[name], date=day.isoformat())) for name in names]


def describe(query: BuntzenQuery) -> str:
    """The request exactly as the transport sends it."""
    url, payload, headers = build_request(query)
    lines = [f"POST {url}", *(f"{name}: {value}" for name, value in headers.items()), ""]
    return "\n".join(lines) + "\n" + msgspec.json.encode(payload).decode() + "\n"


def dry_run(args: argparse.Namespace, out: TextIO) -> None:
    for name, query in queries(args.place, args.date):
        out.write(f"# {name} day (placeId {query.place_id}) for {query.date}: dry run, nothing sent\n")
        out.write(describe(query) + "\n")


def refusal() -> str | None:
    """Why a real capture must not run here, or None."""
    for name in ("CI", "PYTEST_CURRENT_TEST"):
        if os.environ.get(name):
            return f"capture_yodel: refusing to call Yodel with {name} set (it runs on the owner's workstation only)"
    return None


def record(
    out_dir: Path, name: PlaceName, query: BuntzenQuery, response: HttpResponse, at: datetime
) -> tuple[Path, dict[str, Any]]:
    """Write the body and its .meta.json; return the meta."""
    url, payload, _headers = build_request(query)  # the headers are constants; no response header is kept
    try:
        parsed: object = msgspec.to_builtins(parse_catalog(response, query))
    except FetchError as e:
        parsed = {"error": e.kind}
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"captured-{name}-{query.date}-{at.strftime('%Y%m%dT%H%M%SZ')}"
    n = 1
    while (out_dir / f"{stem}.meta.json").exists():  # never overwrite an earlier capture
        n += 1
        stem = f"captured-{name}-{query.date}-{at.strftime('%Y%m%dT%H%M%SZ')}-{n}"
    try:
        pretty = json.dumps(json.loads(response.body), indent=2, ensure_ascii=False) + "\n"
        body_path = out_dir / f"{stem}.json"
        body_path.write_text(pretty)
    except ValueError:
        body_path = out_dir / f"{stem}.txt"
        body_path.write_bytes(response.body)
    meta = {
        "captured_at": at.isoformat(),
        "status": response.status,
        "content_type": response.content_type,
        "request": {"url": url, "payload": payload},
        "parsed": parsed,
    }
    (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return body_path, meta


async def capture(args: argparse.Namespace, out: TextIO) -> None:
    config = FetcherConfig(max_body_bytes=MAX_BYTES)
    async with upstream_session(config) as session:
        transport = AiohttpTransport(session, frozenset(HOSTS), max_bytes=MAX_BYTES)
        first = True
        for round_ in range(args.repeat):
            for i, (name, query) in enumerate(queries(args.place, args.date)):
                if not first:
                    await asyncio.sleep(args.every if i == 0 and round_ > 0 else MIN_GAP_S)
                first = False
                url, payload, headers = build_request(query)
                at = datetime.now(UTC).replace(microsecond=0)
                try:
                    response = await transport.post_json(url, payload, headers=headers)
                except FetchError as e:
                    out.write(f"{name} {query.date}: no response ({e.kind})\n")
                    continue
                path, meta = record(args.out, name, query, response, at)
                out.write(f"{name} {query.date}: HTTP {response.status} -> {path.name}\n")
                out.write(json.dumps(meta["parsed"], indent=2) + "\n")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.dry_run:
        dry_run(args, sys.stdout)
        return
    reason = refusal()
    if reason is not None:
        sys.exit(reason)
    asyncio.run(capture(args, sys.stdout))


if __name__ == "__main__":
    main()
