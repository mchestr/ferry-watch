"""Failures talking to bcferries.com, classified by how the watcher should react."""

from __future__ import annotations

from collections.abc import Iterable


class BookingError(Exception):
    """Something went wrong on bcferries.com. Transient unless a subclass says otherwise."""


class MarkupChanged(BookingError):
    """The page didn't look like we expect; BC Ferries probably changed their site."""


class SessionExpired(BookingError):
    """The search session is gone (redirected home, cookies expired); search again."""


class QueueTimeout(BookingError):
    """Stuck in BC Ferries' Queue-it waiting room for too long."""


class RouteRejected(BookingError):
    """BC Ferries didn't accept the route/date (not bookable, outside the schedule...)."""


class LoginFailed(BookingError):
    """BC Ferries rejected the login. Retrying immediately risks locking the account."""


class NotBookable(BookingError):
    """The sailing sold out again before it could be added to the cart."""


class CartMismatch(BookingError):
    """The cart page doesn't show the sailing/fare we selected."""


def describe(error: BaseException, secrets: Iterable[str] = ()) -> str:
    """A one-line, secret-free description of an error, fit for logs and the UI.

    Playwright errors carry a multi-line call log that can include values typed into fields, so
    only the first line is kept and any known secret is masked.
    """
    text = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    for secret in secrets:
        if secret:
            text = text.replace(secret, "•••")
    return text[:300]
