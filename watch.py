# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.56.0", "aiohttp==3.10.10"]
# ///
"""BC Ferries reservation watcher.

Watches one or more trips on bcferries.com (the site sits behind Queue-it, so it
drives a real Chromium), sends a Pushover alert when a sailing becomes bookable,
and can add it to the cart so checkout is a few taps away.

A web UI on http://localhost:8765 manages the trips and shows results.

    PUSHOVER_TOKEN=... PUSHOVER_USER_KEY=... uv run watch.py
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import mimetypes
import threading
from collections import deque
import time
import traceback
import uuid
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BCF_CONFIG", HERE / "config.json"))
PORT = int(os.environ.get("BCF_PORT", "8765"))
HOST = os.environ.get("BCF_HOST", "127.0.0.1")
# Where the cart browser can be watched/controlled (noVNC when running in the cluster).
VNC_URL = os.environ.get("BCF_VNC_URL", "")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)

TRIP_DEFAULTS = {
    "enabled": True,
    "date": "2026-10-12",
    "from": "SWB",
    "to": "TSA",
    "adults": 1,
    "children": 0,
    "infants": 0,
    "seniors": 0,
    "vehicle": "standard",  # none | standard | tall
    "height_ft": 8,  # only used when vehicle == "tall"
    "earliest": "00:00",
    "latest": "23:59",
    "hold": True,  # add an open sailing to the cart
    "fare": "cheapest",  # cheapest | saver | prepaid | reservation
}
TRIP_TYPES = {
    "enabled": bool, "date": str, "from": str, "to": str, "vehicle": str, "earliest": str, "latest": str,
    "adults": int, "children": int, "infants": int, "seniors": int, "height_ft": float, "hold": bool, "fare": str,
}
# Changing any of these means a new search, so the trip's browser session is reopened.
SEARCH_KEYS = ("date", "from", "to", "adults", "children", "infants", "seniors", "vehicle", "height_ft")
MAX_TRIPS = 6

FARE_NAMES = {"saver": "Saver", "prepaid": "Prepaid", "reservation": "Reservation Only"}

# Passenger counter order on PassengerSelectionPage.
PASSENGER_FIELDS = ["adults", "children", "infants", "seniors"]

state_lock = threading.Lock()
state: dict = {
    "trips": {},  # trip id -> runtime state, see trip_state()
    "checking": None,  # id of the trip being checked right now
    "next_check": None,  # epoch seconds
    "events": deque(maxlen=80),  # activity feed: {"ts", "kind", "text", "trip"}
}
wake = threading.Event()


def trip_state(tid: str) -> dict:
    """Runtime state for one trip (call with state_lock held)."""
    return state["trips"].setdefault(tid, {
        "sailings": [],
        "last_check": None,
        "last_error": None,
        "consecutive_errors": 0,
        "notified": set(),  # departures already alerted
        "session": None,  # {"since", "logged_in"}
        "hold": None,  # {"status": "adding"|"ready"|"failed"|"closed", "depart", "fare", "price", "at", "error"}
    })


# --------------------------------------------------------------------------
# config / terminals


def new_trip(**kw) -> dict:
    trip = {"id": uuid.uuid4().hex[:8], **TRIP_DEFAULTS}
    trip.update({k: TRIP_TYPES[k](v) for k, v in kw.items() if k in TRIP_TYPES})
    return trip


def load_config() -> dict:
    raw = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if "trips" not in raw:
        # Pre-0.4 config held a single trip at the top level.
        raw = {"enabled": raw.get("enabled", True), "interval_seconds": raw.get("interval_seconds", 30),
               "trips": [new_trip(**{k: v for k, v in raw.items() if k != "enabled"})] if raw else [new_trip()]}
    cfg = {"enabled": bool(raw.get("enabled", True)), "interval_seconds": int(raw.get("interval_seconds", 30))}
    cfg["trips"] = [{**TRIP_DEFAULTS, **t} for t in raw["trips"]]
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def trip_label(t: dict) -> str:
    """'SWB→TSA Mon Oct 12' for logs, alerts and the activity feed."""
    day = datetime.strptime(t["date"], "%Y-%m-%d").strftime("%a %b %-d")
    return f'{terminals()[t["from"]]["short"].split("(")[-1].rstrip(")")} → {terminals()[t["to"]]["short"].split("(")[-1].rstrip(")")}, {day}'


_terminals: dict | None = None


def terminals() -> dict:
    """{code: {"label": "Delta - Vancouver (Tsawwassen)", "short": ..., "dests": [codes]}}"""
    global _terminals
    if _terminals is None:
        req = urllib.request.Request("https://www.bcferries.com/route-info", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        _terminals = {
            t["code"]: {
                "label": f'{t["city"]["name"]} - {t["name"]}',
                "short": t["name"],
                "dests": [d["code"] for d in t.get("destinationRoutes", []) if d.get("isBookable")],
            }
            for t in data
        }
    return _terminals


def booking_link(cfg: dict) -> str:
    t = terminals()
    y, m, d = cfg["date"].split("-")
    q = {
        "sailing": "true",
        "departureLocation": cfg["from"],
        "departureLocationName": t[cfg["from"]]["label"],
        "arrivalLocation": cfg["to"],
        "arrivalLocationName": t[cfg["to"]]["label"],
        "departingDateTime": f"{m}/{d}/{y}",
    }
    return "https://www.bcferries.com/?" + urllib.parse.urlencode(q)


def booking_steps(cfg: dict, sailing: dict) -> str:
    """The taps left on bcferries.com after the prefilled link (it can't skip them: CSRF)."""
    pax = [f"{n} {one if n == 1 else many}" for k, one, many in
           (("adults", "adult", "adults"), ("children", "child", "children"), ("infants", "infant", "infants"), ("seniors", "senior", "seniors"))
           if (n := int(cfg.get(k, 0)))]
    steps = ["One-way → Continue", f'{", ".join(pax)}, {"Without" if cfg["vehicle"] == "none" else "With"} a vehicle → Continue']
    if cfg["vehicle"] == "standard":
        steps.append("7ft & under, Under 20ft → Continue")
    elif cfg["vehicle"] == "tall":
        steps.append(f'Over 7ft ({cfg.get("height_ft", 8)}ft), Under 20ft → Continue')
    steps.append(f'{sailing["depart"]} → View fares')
    return "\n".join(f"{i}. {x}" for i, x in enumerate(steps, 1))


# --------------------------------------------------------------------------
# scraping


def to_minutes(s: str) -> int:
    """'3:00 pm' or '15:00' -> minutes since midnight."""
    s = s.strip().lower()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if not m:
        raise ValueError(s)
    h, mi, ap = int(m[1]), int(m[2]), m[3]
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mi


def start_url(cfg: dict) -> str:
    t = terminals()
    return "https://www.bcferries.com/?" + urllib.parse.urlencode(
        {
            "sailing": "true",
            "departureLocation": cfg["from"],
            "departureLocationName": t[cfg["from"]]["label"],
            "arrivalLocation": cfg["to"],
            "arrivalLocationName": t[cfg["to"]]["label"],
        }
    )


def dismiss_cookies(pg) -> None:
    accept = pg.locator("button:visible:has-text('Accept')")
    if accept.count():
        accept.first.click()


def goto_sailings(pg, cfg: dict) -> None:
    """Walk the booking form (route/date → passengers → vehicle) to the fare-selection page."""
    y, mo, d = (int(x) for x in cfg["date"].split("-"))

    # Step 1: route + date
    pg.goto(start_url(cfg), wait_until="load", timeout=120_000)
    pg.wait_for_selector("#y_confirmaddpassenger", state="visible")
    dismiss_cookies(pg)
    pg.click("#y_oneWayRadbtn", force=True)
    pg.locator("text=Departure date").locator("xpath=..").click()
    cal = "#check-in-datepicker"
    day = f'{cal} td[data-month="{mo - 1}"][data-year="{y}"] a:text-is("{d}")'
    for _ in range(13):
        if pg.locator(day).count():
            break
        pg.click(f"{cal} .ui-datepicker-next")
    else:
        raise RuntimeError(f"{cfg['date']} is not selectable in the BC Ferries calendar")
    pg.click(day)
    with pg.expect_navigation(url="**/PassengerSelectionPage**"):
        pg.click("#y_confirmaddpassenger")

    # Step 2: passengers
    for i, key in enumerate(PASSENGER_FIELDS):
        for _ in range(int(cfg.get(key, 0))):
            pg.evaluate(
                "id => document.getElementById(id).parentElement.querySelectorAll('button')[1].click()",
                f"passengerInfoForm.passengerTypeQuantityList{i}.quantity",
            )
    walk_on = cfg["vehicle"] == "none"
    pg.click("#proceed_to_sailing_selection_btn" if walk_on else "#proceed_to_vehicle_selection_btn", force=True)
    with pg.expect_navigation():
        pg.click("button[type=submit]:visible:has-text('Continue')")

    # Step 3: vehicle
    if not walk_on:
        pg.wait_for_url("**/VehicleSelectionPage**")
        if cfg["vehicle"] == "tall":
            pg.click("#over7Height_0", force=True)
            pg.fill("#Over7heightboxinft_0", str(cfg.get("height_ft", 8)))
            pg.dispatch_event("#Over7heightboxinft_0", "change")
        else:
            pg.click("#under7Height_0", force=True)
        pg.click("#under20Length_0", force=True)
        with pg.expect_navigation():
            pg.click("button[type=submit]:visible:has-text('Continue')")

    # Step 4: sailings
    pg.wait_for_url("**/fare-selection**")
    pg.wait_for_selector(".p-card", timeout=60_000)


def parse_card(card) -> dict | None:
    text = card.inner_text()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    try:
        depart = lines[lines.index("DEPART") + 1]
        ai = lines.index("ARRIVE")
        arrive = lines[ai + 1]
    except (ValueError, IndexError):
        return None
    price = re.search(r"\$\d[\d,]*\.\d\d", text)
    if card.locator("button:has-text('View fares')").count():
        status = "available"
    elif "sold out" in text.lower():
        status = "sold out"
    else:
        status = next((l for l in lines[ai + 3 :] if l != "Notify me"), "unavailable")
    return {
        "depart": depart,
        "arrive": arrive,
        "vessel": lines[ai + 2] if len(lines) > ai + 2 else "",
        "status": status,
        "price": price[0] if price and status == "available" else "",
    }


def save_failure(pg) -> None:
    try:
        path = CONFIG_PATH.with_name("last-error.png")
        pg.screenshot(path=str(path), full_page=True)
        log(f"saved screenshot of failure to {path} ({pg.url})")
    except Exception:  # noqa: BLE001
        pass


def browser_session(p):
    browser = p.chromium.launch(headless=True)
    pg = browser.new_page(user_agent=UA, viewport={"width": 1280, "height": 1400})
    pg.set_default_timeout(60_000)
    return browser, pg


def check(cfg: dict) -> list[dict]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, pg = browser_session(p)
        try:
            goto_sailings(pg, cfg)
            sailings = [s for c in pg.locator(".p-card").all() if (s := parse_card(c))]
            if not sailings:
                raise RuntimeError("Reached the sailings page but found no sailings")
            return sailings
        except Exception:
            save_failure(pg)
            raise
        finally:
            browser.close()


def login(pg) -> None:
    pg.goto("https://www.bcferries.com/login", wait_until="load", timeout=120_000)
    dismiss_cookies(pg)
    pg.fill("#email", os.environ["BCF_USER"])
    pg.fill("#j_password", os.environ["BCF_PASS"])
    with pg.expect_navigation():
        pg.click("button[type=submit]:has-text('Log in')")
    if "/login" in pg.url:
        raise RuntimeError("BC Ferries login failed (check the Bcferries item in 1Password)")


def choose_fare(pg, preference: str) -> tuple[str, str]:
    """Click the Select button for the preferred fare (else the cheapest). Returns (fare, price)."""
    pg.wait_for_selector("label.js-radio-select:visible")
    fares = pg.evaluate(
        """() => [...document.querySelectorAll('label.js-radio-select')].filter(l => l.offsetParent).map((l, i) => {
            let e = l;
            while (e && !/(Saver|Prepaid|Reservation Only)/.test(e.innerText)) e = e.parentElement;
            const name = e ? e.innerText.match(/(Saver|Prepaid|Reservation Only)/)[1] : '?';
            const price = (l.closest('.standard-sailing-wrapper')?.querySelector('.price-desc')?.innerText || '').trim();
            return {i, name, price};
        })"""
    )
    if not fares:
        raise RuntimeError("No fares offered for this sailing")
    amount = lambda f: float(re.sub(r"[^\d.]", "", f["price"]) or "inf")
    want = FARE_NAMES.get(preference)
    pick = next((f for f in fares if f["name"] == want), None) or min(fares, key=amount)
    with pg.expect_navigation(url="**/fare-selection-review**"):
        pg.locator("label.js-radio-select:visible").nth(pick["i"]).click()
    return pick["name"], pick["price"]


FARE_SELECTION = "https://www.bcferries.com/fare-selection"


def summarize(sailings: list[dict], limit: int = 3) -> str:
    """'7:00 am ($93.45), 9:00 am ($93.45), 11:00 am ($93.45) +7 more'"""
    parts = [f'{s["depart"]} ({s["price"]})' if s["price"] else s["depart"] for s in sailings[:limit]]
    extra = len(sailings) - limit
    return ", ".join(parts) + (f" +{extra} more" if extra > 0 else "")


def have_login() -> bool:
    return bool(os.environ.get("BCF_USER") and os.environ.get("BCF_PASS"))


def in_window(t: dict, s: dict) -> bool:
    return to_minutes(t["earliest"]) <= to_minutes(s["depart"]) <= to_minutes(t["latest"])


def set_hold(tid: str, **kw) -> None:
    with state_lock:
        ts = trip_state(tid)
        ts["hold"] = {**(ts["hold"] or {}), **kw}


class Session:
    """One trip's browser context: BC Ferries keeps the search in the session, so each trip needs its own."""

    def __init__(self, ctx, pg, key):
        self.ctx, self.pg, self.key = ctx, pg, key

    def alive(self) -> bool:
        # is_closed() is set by Playwright's "close" event and never talks to the page, so a page
        # in the middle of navigating (e.g. the user submitting payment) isn't mistaken for a closed
        # window. Asking the page for its title did exactly that, and the watcher then closed the
        # checkout window itself.
        return not self.pg.is_closed()

    def close(self) -> None:
        try:
            self.ctx.close()
        except Exception:  # noqa: BLE001
            pass


class Watcher:
    """Owns one long-lived (headed) browser with a session per trip.

    Each session walks the booking form once, then is polled by reloading its
    fare-selection page (~3s). A sailing that opens is added to the cart on that
    same page, and that trip pauses until its cart is released.
    """

    def __init__(self) -> None:
        self.browser = None
        self.sessions: dict[str, Session] = {}
        self.release: set[str] = set()
        self.release_lock = threading.Lock()

    # ---- browser/session lifecycle

    def ensure_browser(self) -> None:
        if self.browser and self.browser.is_connected():
            return
        for tid in list(self.sessions):
            self.close_session(tid)
        size = os.environ.get("BCF_WINDOW_SIZE", "1280,1000")
        self.browser = self.p.chromium.launch(
            headless=os.environ.get("BCF_HEADLESS") == "1",
            args=[f"--window-size={size}", "--window-position=0,0"],
        )

    def close_session(self, tid: str) -> None:
        if sess := self.sessions.pop(tid, None):
            sess.close()
        with state_lock:
            if tid in state["trips"]:
                state["trips"][tid]["session"] = None

    def open_session(self, t: dict, key) -> Session:
        self.close_session(t["id"])
        ctx = self.browser.new_context(user_agent=UA, no_viewport=True)
        pg = ctx.new_page()
        pg.set_default_timeout(60_000)
        sess = self.sessions[t["id"]] = Session(ctx, pg, key)
        logged_in = bool(t["hold"] and have_login())
        if logged_in:
            login(pg)
        goto_sailings(pg, t)
        with state_lock:
            trip_state(t["id"])["session"] = {"since": time.time(), "logged_in": logged_in}
        event("info", "Opened a booking session" + (" (logged in)" if logged_in else ""), t)
        return sess

    # ---- main loop

    def run(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            self.p = p
            while True:
                cfg = load_config()
                active = [t for t in cfg["trips"] if t["enabled"]] if cfg["enabled"] else []
                for tid in set(self.sessions) - {t["id"] for t in active}:
                    self.close_session(tid)  # trip removed, paused, or watching off
                for t in active:
                    if self.holding(t["id"]):
                        self.check_hold(t)
                    else:
                        self.poll(t)
                with state_lock:
                    for tid in set(state["trips"]) - {t["id"] for t in cfg["trips"]}:
                        del state["trips"][tid]
                polling = [t for t in active if not self.holding(t["id"])]
                delay = max(15, cfg["interval_seconds"]) * random.uniform(0.85, 1.15)
                deadline = time.time() + delay
                with state_lock:
                    state["next_check"] = deadline if polling else None
                # Sleep until the next check, but look at held carts every few seconds so a release
                # or a closed checkout window is noticed quickly. Any UI action wakes us immediately.
                while (left := deadline - time.time()) > 0:
                    held = [t for t in active if self.holding(t["id"])]
                    if wake.wait(min(left, 5) if held else left):
                        wake.clear()
                        break
                    for t in held:
                        self.check_hold(t)

    def holding(self, tid: str) -> bool:
        with state_lock:
            h = trip_state(tid)["hold"]
        return bool(h and h["status"] == "ready")

    def check_hold(self, t: dict) -> None:
        tid = t["id"]
        sess = self.sessions.get(tid)
        with self.release_lock:
            released = tid in self.release
            self.release.discard(tid)
        alive = bool(sess and sess.alive())
        if not released and alive:
            return
        if released and alive:
            # Released from the UI: empty the cart so the spot goes back right away.
            try:
                sess.pg.goto("https://www.bcferries.com/abandoned-cart/my-account", timeout=30_000)
            except Exception:  # noqa: BLE001
                pass
        set_hold(tid, status="closed")
        event("info", "Cart released, watching resumed", t)
        self.close_session(tid)  # the cart lives in this session; a fresh one starts clean

    def poll(self, t: dict) -> None:
        tid = t["id"]
        key = tuple(t[k] for k in SEARCH_KEYS) + (bool(t["hold"] and have_login()),)
        with state_lock:
            state["checking"] = tid
        sess = None
        try:
            self.ensure_browser()
            sess = self.sessions.get(tid)
            if not sess or sess.key != key or not sess.alive():
                sess = self.open_session(t, key)
            else:
                sess.pg.goto(FARE_SELECTION, wait_until="load", timeout=60_000)
                try:
                    sess.pg.wait_for_selector(".p-card", timeout=20_000)
                except Exception:  # noqa: BLE001
                    # Session expired, Queue-it, or redirected home: walk the form again.
                    sess = self.open_session(t, key)
            sailings = [s for c in sess.pg.locator(".p-card").all() if (s := parse_card(c))]
            if not sailings:
                raise RuntimeError("Reached the sailings page but found no sailings")
            self.process(t, sess, sailings)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if sess:
                save_failure(sess.pg)
            self.close_session(tid)
            with state_lock:
                ts = trip_state(tid)
                ts["last_error"] = f"{datetime.now():%H:%M:%S} {type(e).__name__}: {e}"
                ts["consecutive_errors"] += 1
                n = ts["consecutive_errors"]
            event("error", f"Check failed: {type(e).__name__}: {e}"[:200], t)
            if n == 3:
                pushover(f"⛴ Ferry watcher failing: {trip_label(t)}", f"3 checks in a row failed:\n{e}"[:1000])
        finally:
            with state_lock:
                state["checking"] = None

    # ---- results

    def process(self, t: dict, sess: Session, sailings: list[dict]) -> None:
        tid = t["id"]
        for s in sailings:
            s["in_window"] = in_window(t, s)
        with state_lock:
            ts = trip_state(tid)
            notified = set(ts["notified"])
            prev = {x["depart"]: x["status"] for x in ts["sailings"]}
        new = [s for s in sailings if s["status"] == "available" and s["in_window"] and s["depart"] not in notified]

        held = self.hold(t, sess, new[0]) if new and t["hold"] else None
        if new:
            self.alert(t, new, held)

        gone = [s["depart"] for s in sailings if prev.get(s["depart"]) == "available" and s["status"] != "available"]
        with state_lock:
            ts = trip_state(tid)
            ts["notified"] |= {s["depart"] for s in new}
            # Forget sailings that sold out again so we re-alert if they reopen.
            ts["notified"] -= {s["depart"] for s in sailings if s["status"] != "available"}
            ts.update(sailings=sailings, last_check=datetime.now(), last_error=None, consecutive_errors=0)
        if gone:
            event("info", f"Sold out again: {', '.join(gone)}", t)
        avail = [s["depart"] for s in sailings if s["status"] == "available" and s["in_window"]]
        log(f"{trip_label(t)}: {len(sailings)} sailings, bookable in window: {avail or 'none'}")

    def hold(self, t: dict, sess: Session, sailing: dict) -> tuple[str, str] | None:
        """Add `sailing` to the cart on the trip's already-open fare-selection page."""
        tid = t["id"]
        set_hold(tid, status="adding", depart=sailing["depart"], date=t["date"], fare=None, price=None, at=time.time(), error=None)
        try:
            card = next(
                (c for c in sess.pg.locator(".p-card").all()
                 if (s := parse_card(c)) and s["depart"] == sailing["depart"] and s["status"] == "available"),
                None,
            )
            if card is None:
                raise RuntimeError(f'{sailing["depart"]} is no longer bookable')
            card.locator("button:has-text('View fares')").click()
            fare, price = choose_fare(sess.pg, t.get("fare", "cheapest"))
            set_hold(tid, status="ready", fare=fare, price=price, at=time.time())
            event("cart", f"{sailing['depart']} added to the cart ({fare} {price})", t)
            log(f"holding {trip_label(t)} {sailing['depart']} ({fare} {price}) in cart")
            try:
                sess.pg.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            mac_notify("Ferry in your cart", f"{sailing['depart']}: finish checkout in the browser window")
            return fare, price
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            save_failure(sess.pg)
            set_hold(tid, status="failed", error=f"{type(e).__name__}: {e}"[:300])
            event("error", f"Couldn't add {sailing['depart']} to the cart: {e}"[:200], t)
            self.close_session(tid)
            return None

    def alert(self, t: dict, new: list[dict], held: tuple[str, str] | None) -> None:
        times = summarize(new, limit=6)
        if held:
            fare, price = held
            others = ", ".join(s["depart"] for s in new[1:])
            where = "Tap to finish checkout" if VNC_URL else "Finish checkout in the browser window"
            pushover(
                f"🛒 {new[0]['depart']} is in your cart",
                f"{trip_label(t)}\n{fare} {price}" + (f"\nAlso bookable: {others}" if others else "")
                + f"\n\n{where}. Nothing is booked until you pay.",
                url=VNC_URL or booking_link(t),
                url_title="Open checkout" if VNC_URL else "Book on bcferries.com",
                priority=1,
            )
        else:
            failed = " (couldn't add it to the cart)" if t["hold"] else ""
            pushover(
                f"⛴ Ferry space: {trip_label(t)}",
                f"Bookable: {times}{failed}\n\n{booking_steps(t, new[0])}",
                url=booking_link(t),
                priority=1,
            )
        event("alert", f"Bookable: {summarize(new)} · alert sent", t)


watcher = Watcher()


def mac_notify(title: str, message: str) -> None:
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], check=False)


# --------------------------------------------------------------------------
# notifications


def pushover(title: str, message: str, url: str | None = None, priority: int = 0, url_title: str = "Book on bcferries.com") -> None:
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER_KEY")
    if not token or not user:
        log(f"[no pushover creds] {title}: {message}")
        return
    data = {"token": token, "user": user, "title": title, "message": message, "priority": priority}
    if url:
        data |= {"url": url, "url_title": url_title}
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    log(f"pushover sent: {title}")


def event(kind: str, text: str, trip: dict | None = None) -> None:
    """Record something worth showing in the UI's activity feed (kind: info|alert|cart|error)."""
    with state_lock:
        state["events"].appendleft({"ts": time.time(), "kind": kind, "text": text, "trip": trip["id"] if trip else None})


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --------------------------------------------------------------------------
# web UI (static page + JSON API)

# Built React app (web/dist locally, /app/static in the image).
STATIC_DIR = Path(os.environ.get("BCF_STATIC", HERE / "web" / "dist")).resolve()
config_lock = threading.Lock()


def api_state() -> dict:
    cfg = load_config()
    with state_lock:
        trips = []
        for t in cfg["trips"]:
            ts = trip_state(t["id"])
            trips.append({
                **t,
                "sailings": ts["sailings"],
                "last_check": ts["last_check"].isoformat() if ts["last_check"] else None,
                "last_error": ts["last_error"],
                "session": ts["session"],
                "cart": ts["hold"],  # runtime cart state; "hold" is the trip's auto-cart setting
                "checking": state["checking"] == t["id"],
                "booking_link": booking_link(t),
            })
        events = list(state["events"])[:40]
        checking, next_check = state["checking"], state["next_check"]
    return {
        "enabled": cfg["enabled"],
        "interval_seconds": cfg["interval_seconds"],
        "trips": trips,
        "checking": checking is not None,
        "next_check": next_check,
        "now": time.time(),
        "events": events,
        "vnc_url": VNC_URL,
        "login": have_login(),
        "pushover": bool(os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER_KEY")),
        "max_trips": MAX_TRIPS,
    }


def api_terminals() -> dict:
    return {c: {"label": v["label"], "short": v["short"], "dests": v["dests"]} for c, v in terminals().items()}


def clean_trip(trip: dict, body: dict) -> dict:
    for k, typ in TRIP_TYPES.items():
        if k in body:
            trip[k] = typ(body[k])
    for k in PASSENGER_FIELDS:
        trip[k] = max(0, min(9, trip[k]))
    trip["adults"] = max(trip["adults"], 1) if not (trip["children"] or trip["seniors"]) else trip["adults"]
    dests = terminals().get(trip["from"], {}).get("dests", [])
    if trip["to"] not in dests and dests:
        trip["to"] = dests[0]
    return trip


class ApiError(Exception):
    pass


def handle_post(parts: list[str], body: dict) -> None:
    """Apply a UI action. Runs in a worker thread (it may block on config/state locks or Pushover)."""
    if parts == ["api", "settings"]:
        with config_lock:
            cfg = load_config()
            if "enabled" in body:
                cfg["enabled"] = bool(body["enabled"])
                event("info", "Watching resumed" if cfg["enabled"] else "Watching paused")
            if "interval_seconds" in body:
                cfg["interval_seconds"] = max(15, int(body["interval_seconds"]))
            save_config(cfg)
        wake.set()
    elif parts == ["api", "trips"]:
        with config_lock:
            cfg = load_config()
            if len(cfg["trips"]) >= MAX_TRIPS:
                raise ApiError(f"Up to {MAX_TRIPS} trips can be watched at once")
            trip = clean_trip(new_trip(), body)
            cfg["trips"].append(trip)
            save_config(cfg)
        event("info", "Trip added", trip)
        wake.set()
    elif parts[:2] == ["api", "trips"] and len(parts) in (3, 4):
        tid, action = parts[2], (parts[3] if len(parts) == 4 else "update")
        with config_lock:
            cfg = load_config()
            trip = next((t for t in cfg["trips"] if t["id"] == tid), None)
            if trip is None:
                raise ApiError("No such trip")
            if action == "update":
                before = {k: trip[k] for k in SEARCH_KEYS + ("earliest", "latest")}
                clean_trip(trip, body)
                save_config(cfg)
                if before != {k: trip[k] for k in SEARCH_KEYS + ("earliest", "latest")}:
                    with state_lock:
                        ts = trip_state(tid)
                        ts["sailings"], ts["notified"] = [], set()
                if set(body) != {"enabled"}:
                    event("info", "Trip updated", trip)
            elif action == "delete":
                cfg["trips"] = [t for t in cfg["trips"] if t["id"] != tid]
                save_config(cfg)
                event("info", f"Trip removed: {trip_label(trip)}")
            elif action == "release":
                with watcher.release_lock:
                    watcher.release.add(tid)
            elif action == "dismiss":
                with state_lock:
                    ts = trip_state(tid)
                    if ts["hold"] and ts["hold"]["status"] in ("failed", "closed"):
                        ts["hold"] = None
            else:
                raise ApiError(f"Unknown action {action}")
        wake.set()
    elif parts == ["api", "check"]:
        wake.set()
    elif parts == ["api", "test"]:
        pushover("⛴ Ferry watch test", "Notifications are working.")
        event("info", "Test notification sent")
    else:
        raise ApiError("Not found")


def make_app():
    from aiohttp import WSMsgType, web

    clients: set = set()

    async def get_state(_req):
        return web.json_response(await asyncio.to_thread(api_state), headers={"Cache-Control": "no-store"})

    async def get_terminals(_req):
        return web.json_response(await asyncio.to_thread(api_terminals))

    async def healthz(_req):
        return web.Response(text="ok")

    async def post(req):
        body = await req.json() if req.can_read_body else {}
        try:
            await asyncio.to_thread(handle_post, req.path.strip("/").split("/"), body)
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)}, status=502)
        return web.json_response(await asyncio.to_thread(api_state))

    async def ws(req):
        sock = web.WebSocketResponse(heartbeat=25)
        await sock.prepare(req)
        clients.add(sock)
        try:
            await sock.send_str(json.dumps(await asyncio.to_thread(api_state)))
            async for msg in sock:  # nothing to receive; keeps the connection open
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            clients.discard(sock)
        return sock

    async def broadcaster(_app):
        """Push state to every connected browser whenever it changes (checked twice a second)."""
        last = None

        async def loop():
            nonlocal last
            while True:
                await asyncio.sleep(0.5)
                if not clients:
                    last = None
                    continue
                st = await asyncio.to_thread(api_state)
                sig = json.dumps({k: v for k, v in st.items() if k != "now"}, sort_keys=True)
                if sig == last:
                    continue
                last = sig
                payload = json.dumps(st)
                for c in list(clients):
                    try:
                        await c.send_str(payload)
                    except Exception:  # noqa: BLE001
                        clients.discard(c)

        task = asyncio.create_task(loop())
        yield
        task.cancel()

    async def static(req):
        """Serve the built frontend; unknown paths fall back to index.html."""
        rel = req.match_info.get("path", "") or "index.html"
        f = (STATIC_DIR / rel).resolve()
        if not f.is_file() or STATIC_DIR not in f.parents:
            f = STATIC_DIR / "index.html"
        if not f.is_file():
            return web.Response(status=503, text="frontend not built (cd web && npm run build)")
        # Vite fingerprints assets, so they can be cached forever; index.html must not be.
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-store"
        return web.FileResponse(f, headers={"Cache-Control": cache})

    app = web.Application()
    app.cleanup_ctx.append(broadcaster)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/terminals", get_terminals)
    app.router.add_get("/api/ws", ws)
    app.router.add_post("/api/{tail:.*}", post)
    app.router.add_get("/{path:.*}", static)
    return app


def main() -> None:
    if not CONFIG_PATH.exists() or "trips" not in json.loads(CONFIG_PATH.read_text() or "{}"):
        save_config(load_config())  # create, or migrate a single-trip config
    if "--once" in sys.argv:
        print(json.dumps(check(load_config()["trips"][0]), indent=2))
        return
    event("info", "Watcher started")
    threading.Thread(target=watcher.run, daemon=True).start()
    from aiohttp import web

    log(f"web UI on http://localhost:{PORT}")
    web.run_app(make_app(), host=HOST, port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
