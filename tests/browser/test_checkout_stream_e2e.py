"""A checkout in the streamed viewer, end to end (docs/multi-tenant/streamed-viewer/DESIGN.md §11, final integration).

A ticket minted as waitlist-web mints it, in the link waitlist-web's checkout_url() makes for a user on the beta, opened
on a phone and on a computer: vnc-edge's landing page, the streamed viewer, the pod's gate and stream session, the
pod's real screen (test_stream_midpoint.py's rig). One whole checkout: a province from the native picker, the card
number typed into the payment provider's cross-site frame, the bank's 3-D Secure window with its one-time code, a
confirm dialog headed with its page's origin, the device coming back to the checkout, then "Use the classic viewer"
(KasmVNC by the device's nonce, the pod's own screen at 1x, the page as it was) until the hold ends. Nothing on the
way may break a page's Content-Security-Policy.

For everyone else the link is 0.6.4's, byte for byte, and it opens KasmVNC as 0.6.4 did.

Needs what test_stream_midpoint.py needs.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import pytest
from playwright.async_api import Browser, Locator, Page

from tests.browser import test_checkout_edge as edge
from tests.browser import test_checkout_stream as stream
from tests.browser import test_stream_midpoint as midpoint
from tests.browser import test_stream_viewer as viewers
from tests.browser.test_checkout_stream import Pod, frame_rect, middle
from tests.browser.test_stream_midpoint import (
    Checkout,
    device,
    drawn,
    field_for,
    focus_reaches,
    showing,
    until,
    value_reaches,
    wire,
)
from tests.browser.test_stream_viewer import Viewer
from waitlist.agent.cast_protocol import CloseCode
from waitlist.tickets import verify
from waitlist.web.api import checkout_url

pytestmark = [pytest.mark.browser, pytest.mark.timeout(240)]

site = edge.site
browser = edge.browser
pod = stream.pod
sites = stream.sites
checkout = midpoint.checkout

HEADER = (11, 79, 138)  # the shop's sticky header, #0b4f8a
CARD = "4242424242424242"


@dataclass
class Hands:
    """How a person uses the viewer on this device: a finger and the phone's keyboard, or a mouse and a keyboard."""

    viewer: Viewer
    touch: bool

    async def tap(self, point: tuple[float, float]) -> None:
        """A tap (or a click) at this point of the viewer's page, CSS px."""
        if self.touch:
            await viewers.tap(self.viewer, *point)
        else:
            await self.viewer.page.mouse.click(*point)

    async def press(self, selector: str) -> None:
        """One of the viewer's own buttons."""
        if self.touch:
            await self.viewer.page.tap(selector)
        else:
            await self.viewer.page.click(selector)

    async def type(self, text: str) -> None:
        """What the phone's on-screen keyboard sends, a character at a time; or real keys."""
        if not self.touch:
            await self.viewer.page.keyboard.type(text)
            return
        keyboard = await viewers.cdp(self.viewer)
        for c in text:
            await keyboard.send("Input.insertText", {"text": c})

    async def scroll_down(self) -> None:
        """A finger up the screen, let go while moving; or the mouse wheel."""
        if self.touch:
            await viewers.drag(self.viewer, [(195, 600 - 20 * i) for i in range(21)])
            await until(lambda: wire(self.viewer), midpoint.coasted)  # a touch now would only stop the coast
        else:
            await self.viewer.page.mouse.move(640, 400)
            await self.viewer.page.mouse.wheel(0, 500)


async def front(viewer: Viewer) -> dict[str, Any]:
    """The latest `pages` message of the viewer's connection."""
    pages = [m["msg"] for m in (await wire(viewer))["got"] if m["msg"]["t"] == "pages"]
    return pages[-1] if pages else {}


async def tab_with(pod: Pod, ending: str) -> Page:
    """The pod's page whose address ends so, once it has loaded."""

    async def found() -> Page | None:
        return next((p for p in pod.context.pages if p.url.endswith(ending)), None)

    page: Page | None = await until(found, bool)
    assert page is not None, f"no page at …{ending} opened in the pod"
    await page.wait_for_load_state("load")
    return page


async def dialog_answered(hands: Hands, checkout: Checkout, heading: str, message: str) -> None:
    """The dialog sheet shows this heading and message; the user presses OK, and the pod's dialog closes."""
    shown = await viewers.dialog_shown(hands.viewer)
    assert (shown["origin"], shown["message"]) == (heading, message)
    await hands.press("#dialog-ok")
    assert await until(lambda: hands.viewer.page.is_hidden("#dialog"), bool)
    await value_reaches(lambda: open_dialogs(checkout), [])


async def open_dialogs(checkout: Checkout) -> list[str]:
    return [d.message for d in checkout.rig.keeper.open_dialogs]


async def card_values(checkout: Checkout) -> list[str]:
    card = await stream.card(checkout.pod)
    return [await card.input_value("#cc"), await card.input_value("#exp")]


async def picker_over(viewer: Viewer, field: dict[str, Any]) -> Locator:
    """The native picker the viewer laid over this field of its map (the image is at the viewer's top left, k = 1)."""

    async def index() -> int | None:
        found: int = await viewer.evaluate(
            """([x, y]) => [...document.querySelectorAll("#overlays > *")].findIndex((e) => {
              const r = e.getBoundingClientRect();
              return Math.abs(r.x - x) <= 1 && Math.abs(r.y - y) <= 1;
            })""",
            field["r"][:2],
        )
        return found if found >= 0 else None

    at: int | None = await until(index, lambda i: i is not None)
    assert at is not None, "no picker over the field"
    return viewer.page.locator("#overlays > *").nth(at)


@dataclass
class Visit:
    """One device's checkout in the streamed viewer."""

    hands: Hands
    checkout: Checkout

    @property
    def viewer(self) -> Viewer:
        return self.hands.viewer

    @property
    def page(self) -> Page:
        return self.hands.viewer.page

    async def opens(self, link: str, ticket: str) -> None:
        """Waitlist's link: the landing page, then the streamed viewer, which draws the checkout."""
        await self.page.goto(link)
        assert await self.page.text_content("#beta") == "Opens in the new checkout viewer (beta)."
        await self.hands.press("#open")
        await self.page.wait_for_url(f"{self.checkout.url}{edge.STREAM_VIEWER}")
        image = await showing(self.viewer, self.checkout.pod)
        width = 390 if self.hands.touch else 1280
        assert (image["w"], image["dsf"]) == (width, 2 if self.hands.touch else 1)
        await until(lambda: viewers.status(self.viewer), lambda s: not s["shown"])
        assert viewers.close_to(await viewers.pixel(self.viewer, width / 2, 16), HEADER), "the checkout isn't drawn"
        nonce = (await viewers.device_record(self.viewer))["n"]
        assert sorted(self.checkout.offers[0]) == sorted(["wl.cast1", f"wl.n1.{nonce}", f"wl.t1.{ticket}"])

    async def picks_a_province(self) -> None:
        """From the native picker over the select."""
        picker = await picker_over(self.viewer, await field_for(self.viewer, self.checkout.pod, "#prov"))
        await picker.select_option(label="ON")
        await value_reaches(lambda: self.checkout.pod.tab.input_value("#prov"), "ON")

    async def pays_by_card(self) -> None:
        """Down to the card frame: the card number, then the expiry the card form moves on to by itself."""
        await self.hands.scroll_down()
        field = await field_for(self.viewer, self.checkout.pod, "#cc", "#card")
        await self.hands.tap(middle(field["r"]))
        await focus_reaches(self.checkout, "cc", "#card")
        await self.hands.type(CARD)
        await focus_reaches(self.checkout, "exp", "#card")
        await until(lambda: wire(self.viewer), lambda w: reported(w, "cc-exp"))
        await self.hands.type("1228")
        await value_reaches(lambda: card_values(self.checkout), [CARD, "1228"])

    async def approves_at_the_bank(self) -> None:
        """The bank's window is the front page, with its one-time code, until it closes itself."""
        pod = self.checkout.pod
        await showing(self.viewer, pod)
        await self.hands.tap(middle(await frame_rect(pod, "#pop3ds", "#card")))
        bank = dataclasses.replace(pod, tab=await tab_with(pod, "/popup.html"))
        # The popup is listed as soon as it opens, and its title once its page has loaded: wait for both.
        listed = await until(
            lambda: front(self.viewer),
            lambda p: p.get("front") == 2 and all(page["title"] for page in p.get("pages", [])),
        )
        assert [(p["id"], p["title"], p["origin"], p["main"]) for p in listed["pages"]] == [
            (1, "Checkout (stand-in for bcferries.com)", self.checkout.sites.shop, True),
            (2, "Your bank", self.checkout.sites.pay, False),
        ]
        badge = await until(lambda: self.page.text_content("#menu-badge"), lambda b: b == "2")
        assert badge == "2"
        await self.hands.tap(middle((await field_for(self.viewer, bank, "#otp"))["r"]))
        await value_reaches(lambda: bank.tab.evaluate("document.activeElement.id"), "otp")
        await self.hands.type("123456")
        await value_reaches(lambda: bank.tab.input_value("#otp"), "123456")
        await self.hands.tap(middle(await frame_rect(bank, "#ok")))
        listed = await until(lambda: front(self.viewer), lambda p: p.get("front") == 1)
        assert [p["id"] for p in listed["pages"]] == [1]
        await showing(self.viewer, pod)

    async def answers_dialogs(self) -> None:
        """A confirm, headed with its page's origin as Chrome heads it; the card frame's alert, as embedded."""
        pod, sites = self.checkout.pod, self.checkout.sites
        await self.hands.tap(middle(await frame_rect(pod, "#confirmbtn")))
        await dialog_answered(self.hands, self.checkout, f"{sites.shop} says:", "Leave checkout?")
        await value_reaches(lambda: self.checkout.tab("__dialogs"), [["confirm", True]])
        await showing(self.viewer, pod)
        await self.hands.tap(middle(await frame_rect(pod, "#cardalert", "#card")))
        await dialog_answered(
            self.hands, self.checkout, f"An embedded page at {sites.pay} says:", "Card declined: check the number"
        )
        await showing(self.viewer, pod)

    async def comes_back(self) -> None:
        """The device comes back to the checkout (the bank app, a lock screen): nothing the page can see changes."""
        sizes = await self.checkout.tab("__sizes")
        hello = (await wire(self.viewer))["sent"][0]["msg"]
        await self.page.reload()
        await until(lambda: self.page.evaluate("window.__wire.sockets"), lambda s: s == 1)
        again = await until(lambda: wire(self.viewer), lambda w: drawn(w) is not None)
        assert again["sent"][0]["msg"] == hello
        assert not [m for m in again["sent"] if m["msg"]["t"] == "resize"]
        assert await self.checkout.tab("__sizes") == sizes
        assert await card_values(self.checkout) == [CARD, "1228"]

    async def zooms_in(self) -> None:
        """Two fingers apart: the pod's page at twice its size."""
        await midpoint.pinch(self.viewer, [(145, 300), (245, 300)], [(95, 300), (295, 300)])
        await value_reaches(lambda: self.checkout.tab("Math.round(visualViewport.scale * 10) / 10"), 2)
        await showing(self.viewer, self.checkout.pod)

    async def goes_classic(self) -> None:
        """The menu's "Use the classic viewer": KasmVNC by the same nonce, on the pod's own screen at 1x and page
        scale 1, the page as it was."""
        checkout = self.checkout
        await checkout.tab("window.__classic = []; addEventListener('resize', () => __classic.push(devicePixelRatio))")
        nonce = (await viewers.device_record(self.viewer))["n"]
        await showing(self.viewer, checkout.pod)
        await self.hands.press("#menu-handle")
        await until(lambda: self.page.evaluate("document.getElementById('menu').classList.contains('open')"), bool)
        await self.hands.press("#menu-classic")
        await self.page.wait_for_url(f"{checkout.url}{edge.classic_url(touch=self.hands.touch)}")
        await self.page.wait_for_selector("html.noVNC_connected")
        assert sorted(checkout.offers[-1]) == sorted(["binary", f"wl.n1.{nonce}"])
        assert (await viewers.device_record(self.viewer))["viewer_fallback"] is True
        assert checkout.rig.screen.current is None
        assert await checkout.tab("[devicePixelRatio, visualViewport.scale]") == [1, 1], "not the pod's own screen"
        assert set(await checkout.tab("__classic")) == {1}
        assert await checkout.pod.tab.input_value("#prov") == "ON"
        assert await card_values(checkout) == [CARD, "1228"]
        assert await checkout.tab("__dialogs") == [["confirm", True]]


@pytest.mark.parametrize("name", ["phone", "desktop"])
async def test_a_whole_checkout_in_the_streamed_viewer_then_the_classic_one(
    browser: Browser, checkout: Checkout, name: str
) -> None:
    await checkout.pod.at(f"{checkout.sites.shop}/index.html")
    ticket = edge.ticket()  # tickets.mint, as waitlist-web mints it
    link = checkout_url(checkout.url, edge.TRIP, edge.GEN, ticket, "stream")
    assert link == f"{checkout.url}/t/{edge.TRIP}/g/{edge.GEN}/#v=s1&t={ticket}"
    hold = verify(ticket, {"k1": edge.KEY.public_key()}).disp
    assert hold is not None
    async with device(browser, name) as viewer:
        visit = Visit(Hands(viewer, touch=name == "phone"), checkout)
        await visit.opens(link, ticket)
        lock = checkout.rig.gate._lock
        await visit.picks_a_province()
        await visit.pays_by_card()
        await visit.approves_at_the_bank()
        await visit.answers_dialogs()
        await visit.comes_back()
        assert checkout.rig.gate._lock == lock
        if visit.hands.touch:
            await visit.zooms_in()
        await visit.goes_classic()
        assert checkout.rig.gate._lock == lock, "the classic viewer came in by another lock"

        # The hold ends: the gate closes the viewer, and the device's clock is past the hold's hard deadline too.
        await viewer.page.clock.set_system_time(hold.hard_deadline + 1)
        checkout.rig.cart[0] = None
        await checkout.rig.gate.close("the hold ended", CloseCode.ENDED)
        await viewer.page.wait_for_url(f"{checkout.url}/gone.html")
        assert await viewer.page.text_content("h1") == "No checkout is open for this trip"


def reported(seen: dict[str, Any], autocomplete: str) -> bool:
    """The agent reported focus on a field with this autocomplete."""
    return any(
        m["msg"]["t"] == "focus" and m["msg"]["f"] is not None and m["msg"]["f"]["ac"] == autocomplete
        for m in seen["got"]
    )


async def test_everyone_else_gets_0_6_4s_link_and_kasmvnc(browser: Browser, checkout: Checkout) -> None:
    """A user not on the beta: the link is exactly 0.6.4's, and the landing page opens KasmVNC with 0.6.4's settings
    on the pod's own screen, through the same gate the streamed viewer uses."""
    await checkout.pod.at(f"{checkout.sites.shop}/index.html")
    issued = edge.ticket()
    link = checkout_url(checkout.url, edge.TRIP, edge.GEN, issued, "classic")
    assert link == f"{checkout.url}/t/{edge.TRIP}/g/{edge.GEN}/#{issued}"  # 0.6.4's open_checkout, byte for byte
    async with device(browser, "phone") as viewer:
        await viewer.page.goto(link)
        assert not await viewer.page.is_visible("#beta")
        await viewer.page.click("#open")
        await viewer.page.wait_for_selector("html.noVNC_connected")
        assert viewer.page.url == f"{checkout.url}{edge.classic_url(touch=True)}"
        record = await viewers.device_record(viewer)
        assert (record["viewer"], record["viewer_fallback"]) == ("classic", False)
        assert sorted(checkout.offers[-1]) == sorted(["binary", f"wl.n1.{record['n']}", f"wl.t1.{issued}"])
        assert checkout.rig.screen.current is None
        assert await checkout.tab("devicePixelRatio") == 1
