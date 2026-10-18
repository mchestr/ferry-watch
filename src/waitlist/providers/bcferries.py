"""BC Ferries as a provider: its catalog entry only (design.md §2.4). Written verbatim from the design contract.

BC Ferries is the "browser" checker: its trips are watched by browser pods (waitlist.agent), scheduled by the fleet
(waitlist.control.planner, .fleet), and stored, alerted and shown by the existing code paths, which this module
doesn't change. A BC Ferries watch's spec is waitlist.models.Trip.to_json(), as before.
"""

from __future__ import annotations

from .base import Capabilities, ProviderMeta

ID = "bcferries"

META = ProviderMeta(
    id=ID,
    name="BC Ferries",
    noun="trip",
    description="Sailing reservations. Can hold a sailing in a cart for you to check out.",
    checker="browser",
    capabilities=Capabilities(auto_hold=True, checkout=True, alert_only=False),
    limit_key="maxTrips",
    booking_prefixes=("https://www.bcferries.com/",),
    horizon_days=30,
)
