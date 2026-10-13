"""What the rest of the app needs to know about bcferries.com, without loading a browser.

Kept free of Playwright so the parts of Ferry Watch that never drive a browser (the web API, and the
control plane later) can import it. `site.py` builds on it.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from ferry_watch.models import Trip

from .errors import RouteRejected
from .terminals import Terminals

BASE_URL = "https://www.bcferries.com"
# A desktop Chrome UA. Headless Chromium otherwise announces itself as "HeadlessChrome".
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class Credentials:
    username: str
    password: str = field(repr=False)


def search_url(trip: Trip, terminals: Terminals, base_url: str = BASE_URL) -> str:
    """The booking form with route and date filled in server-side (the date picker isn't needed)."""
    try:
        origin, destination = terminals[trip.origin], terminals[trip.destination]
    except KeyError as e:
        raise RouteRejected(f"Unknown terminal {e.args[0]}") from None
    return f"{base_url}/?" + urllib.parse.urlencode(
        {
            "sailing": "true",
            "departureLocation": origin.code,
            "departureLocationName": origin.label,
            "arrivalLocation": destination.code,
            "arrivalLocationName": destination.label,
            "departingDateTime": trip.date.strftime("%m/%d/%Y"),
        }
    )
