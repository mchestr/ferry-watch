"""Buntzen Lake's fixed facts (design.md §5.1). Written verbatim from the design contract; W2 doesn't change it."""

from __future__ import annotations

from waitlist.providers.base import Budget, Capabilities, ProviderMeta

ID = "buntzen"

META = ProviderMeta(
    id=ID,
    name="Buntzen Lake",
    noun="pass",
    description="BC Hydro day-use parking passes, sold on Yodel. Alerts only: you book on Yodel.",
    checker="http",
    capabilities=Capabilities(auto_hold=False, checkout=False, alert_only=True),
    limit_key="maxAlertOnlyWatches",
    booking_prefixes=("https://yodelportal.com/buntzen-lake/",),
    horizon_days=30,
)

# Distinct queries a minute across every user's Buntzen watches (the owner's Home Assistant made 1-4).
BUDGET = Budget(default_per_minute=12, max_per_minute=30, concurrency=2)

# The only host waitlist-fetcher may call for Buntzen; home-cluster's fetcher egress policy lists the same.
API_HOST = "api.yodelpass.com"
HOSTS = (API_HOST,)

FULL_DAY_PAGE = "https://yodelportal.com/buntzen-lake/All-Day-Pass"
HALF_DAY_PAGE = "https://yodelportal.com/buntzen-lake/Half-Day-Pass"
