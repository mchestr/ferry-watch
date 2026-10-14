from __future__ import annotations

import subprocess
import sys

import pytest

from ferry_watch.bcferries.urls import CheckoutView, checkout_view


def test_loads_without_playwright() -> None:
    """The web API and the control plane import these without a browser installed (docs/multi-tenant)."""
    code = (
        "import sys, ferry_watch.bcferries.urls, ferry_watch.polling, ferry_watch.protocol\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'playwright')\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603 - fixed argv, no shell


# ---- the checkout, by URL


@pytest.mark.parametrize(
    ("main", "others", "expected"),
    [
        ("https://www.bcferries.com/checkout/payment", [], CheckoutView()),
        (
            "https://www.bcferries.com/cart",
            ["about:blank", "data:text/html,x", "chrome-error://chromewebdata/"],
            CheckoutView(),
        ),
        ("https://bcferries.queue-it.net/?c=bcferries", [], CheckoutView()),
        ("https://www.bcferries.com/checkout", ["https://pay.example-bank.com/3ds"], CheckoutView(payment=True)),
        ("https://acs.example-bank.com/challenge", [], CheckoutView(payment=True)),
        ("https://www.bcferries.com/booking-confirmation?code=X", [], CheckoutView(booked=True)),
        ("https://www.bcferries.com/login?redirect=checkout", [], CheckoutView(sign_in=True)),
        ("https://evil.example/confirmation", [], CheckoutView(payment=True)),  # not booked: not BC Ferries
        ("https://bcferries.com.evil.example/login", [], CheckoutView(payment=True)),
    ],
)
def test_checkout_view(main: str, others: list[str], expected: CheckoutView) -> None:
    assert checkout_view(main, others) == expected


def test_checkout_view_follows_the_site_under_test() -> None:
    assert checkout_view("http://127.0.0.1:8123/checkout", base_url="http://127.0.0.1:8123") == CheckoutView()
