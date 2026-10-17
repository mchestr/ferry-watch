"""What the rest of the app needs to know about bcferries.com, without loading a browser.

Kept free of Playwright so the parts of Waitlist that never drive a browser (the web API, and the
control plane later) can import it. `site.py` builds on it.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass

from waitlist.models import Trip

from .errors import RouteRejected
from .terminals import Terminals

BASE_URL = "https://www.bcferries.com"
QUEUE_HOST = "queue-it.net"
# Where a checkout ends up. Provisional: confirmed against pages captured during M1 (design-reference §12.1).
CONFIRMATION_MARKERS = ("confirmation",)
SIGN_IN_PATHS = ("/login",)
# A desktop Chrome UA. Headless Chromium otherwise announces itself as "HeadlessChrome".
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)


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


@dataclass(frozen=True, slots=True)
class CheckoutView:
    """What a held cart's browser is showing, as far as its URLs tell."""

    payment: bool = False  # a page or frame is on a payment or bank host (3-D Secure, a card form)
    booked: bool = False  # BC Ferries' booking confirmation
    sign_in: bool = False  # BC Ferries' sign-in page


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def checkout_view(main: str, others: Iterable[str] = (), *, base_url: str = BASE_URL) -> CheckoutView:
    """Classify the checkout from the main page's URL and every other page's and frame's.

    Only hostnames and paths are looked at, never content, and nothing here leaves the pod: the agent
    turns it into the payment guard, an outcome and an event code.
    """
    site = (urllib.parse.urlsplit(base_url).hostname or "").removeprefix("www.")
    payment = False
    for url in (main, *others):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            continue  # about:blank, data:, chrome-error:// and the like
        if not (_matches(parts.hostname, site) or _matches(parts.hostname, QUEUE_HOST)):
            payment = True
    parts = urllib.parse.urlsplit(main)
    on_site = bool(parts.hostname) and _matches(parts.hostname or "", site)
    path = parts.path.lower()
    return CheckoutView(
        payment=payment,
        booked=on_site and any(marker in path for marker in CONFIRMATION_MARKERS),
        sign_in=on_site and path.startswith(SIGN_IN_PATHS),
    )
