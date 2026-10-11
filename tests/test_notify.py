from __future__ import annotations

import asyncio
from datetime import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ferry_watch.models import Fare, Sailing, SailingStatus, Trip
from ferry_watch.notify import (
    AlertDispatcher,
    Message,
    NotifyError,
    PushoverNotifier,
    bookable_message,
    booking_steps,
    cart_message,
    summarize,
)


def sailing(hour: int, price: str | None = "$93.45") -> Sailing:
    return Sailing(time(hour, 0), time(hour + 1, 35), "Spirit of Vancouver Island", SailingStatus.AVAILABLE, "", price)


TRIP = Trip.from_json({"from": "SWB", "to": "TSA", "date": "2026-10-12", "adults": 2})


def test_summarize() -> None:
    assert summarize([sailing(7)]) == "7:00 am ($93.45)"
    assert (
        summarize([sailing(h) for h in (7, 9, 11, 13, 15)])
        == "7:00 am ($93.45), 9:00 am ($93.45), 11:00 am ($93.45) +2 more"
    )
    assert summarize([sailing(7, price=None)]) == "7:00 am"


def test_booking_steps() -> None:
    assert booking_steps(TRIP, time(15, 0)) == (
        "1. One-way → Continue\n"
        "2. 2 adults, with a vehicle → Continue\n"
        "3. 7 ft & under, under 20 ft → Continue\n"
        "4. 3:00 pm → View fares"
    )
    walk_on = Trip.from_json({"vehicle": "none", "adults": 1, "children": 1}, base=TRIP)
    assert "1 adult, 1 child, without a vehicle" in booking_steps(walk_on, time(9, 0))
    assert "Over 7 ft (8.5 ft)" in booking_steps(
        Trip.from_json({"vehicle": "tall", "height_ft": 8.5}, base=TRIP), time(9, 0)
    )


def test_messages() -> None:
    bookable = bookable_message(TRIP, "Swartz Bay → Tsawwassen", [sailing(9)], "https://book", cart_failed=True)
    assert bookable.title == "⛴ Ferry space: Swartz Bay → Tsawwassen · Mon Oct 12"
    assert "couldn't add it to the cart" in bookable.body
    assert bookable.url == "https://book"
    assert bookable.high_priority

    fare = Fare("Saver", "$93.45", selectable=True)
    cart = cart_message(TRIP, "Swartz Bay → Tsawwassen", sailing(9), fare, [sailing(11)], "https://vnc", "https://book")
    assert cart.title == "🛒 9:00 am is in your cart"
    assert "Also bookable: 11:00 am ($93.45)" in cart.body
    assert (cart.url, cart.url_title) == ("https://vnc", "Open checkout")
    assert cart_message(TRIP, "r", sailing(9), fare, [], None, "https://book").url == "https://book"


class FlakyNotifier:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.sent: list[Message] = []

    @property
    def configured(self) -> bool:
        return True

    async def send(self, message: Message) -> None:
        if self.failures:
            self.failures -= 1
            raise NotifyError("Pushover returned 500")
        self.sent.append(message)


@pytest.fixture
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AlertDispatcher, "RETRY_DELAYS", (0, 0, 0))


@pytest.mark.usefixtures("_no_retry_delay")
async def test_dispatcher_retries_until_delivered() -> None:
    notifier = FlakyNotifier(failures=2)
    failures: list[str] = []
    dispatcher = AlertDispatcher(notifier, lambda _m, err: failures.append(err))
    dispatcher.submit(Message("t", "b"))
    await asyncio.sleep(0.05)
    assert [m.title for m in notifier.sent] == ["t"]
    assert failures == []


@pytest.mark.usefixtures("_no_retry_delay")
async def test_dispatcher_reports_giving_up() -> None:
    notifier = FlakyNotifier(failures=10)
    failures: list[str] = []
    dispatcher = AlertDispatcher(notifier, lambda _m, err: failures.append(err))
    dispatcher.submit(Message("t", "b"))
    await asyncio.sleep(0.05)
    assert notifier.sent == []
    assert failures == ["Pushover returned 500"]
    await dispatcher.aclose()


async def pushover_server(status: int, received: list[dict[str, str]]) -> TestServer:
    async def handler(request: web.Request) -> web.Response:
        received.append(dict(await request.post()))  # type: ignore[arg-type]
        return web.json_response({"status": 1 if status == 200 else 0}, status=status)

    app = web.Application()
    app.router.add_post("/1/messages.json", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_pushover_sends_the_message() -> None:
    received: list[dict[str, str]] = []
    server = await pushover_server(200, received)
    async with aiohttp.ClientSession() as http:
        notifier = PushoverNotifier(http, "tok", "user", url=str(server.make_url("/1/messages.json")))
        await notifier.send(Message("Title", "Body", url="https://book", url_title="Book", high_priority=True))
    await server.close()
    assert received == [
        {
            "token": "tok",
            "user": "user",
            "title": "Title",
            "message": "Body",
            "priority": "1",
            "url": "https://book",
            "url_title": "Book",
        }
    ]


async def test_pushover_errors_are_reported() -> None:
    server = await pushover_server(400, [])
    async with aiohttp.ClientSession() as http:
        notifier = PushoverNotifier(http, "tok", "user", url=str(server.make_url("/1/messages.json")))
        with pytest.raises(NotifyError, match="400"):
            await notifier.send(Message("Title", "Body"))
    await server.close()
