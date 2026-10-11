# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.56.0"]
# ///
"""BC Ferries reservation watcher.

Drives the bcferries.com booking flow in headless Chromium (the site sits
behind Queue-it, so plain HTTP doesn't work), finds sailings that can be
booked, and sends a Pushover notification with a link to the booking page.

A small web UI on http://localhost:8765 edits the config and shows results.

    PUSHOVER_TOKEN=... PUSHOVER_USER_KEY=... uv run watch.py
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

DEFAULT_CONFIG = {
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
    "interval_minutes": 5,
    "hold": True,  # add the sailing to the cart in a visible browser on this Mac
    "fare": "cheapest",  # cheapest | saver | prepaid | reservation
}

FARE_NAMES = {"saver": "Saver", "prepaid": "Prepaid", "reservation": "Reservation Only"}

# Passenger counter order on PassengerSelectionPage.
PASSENGER_FIELDS = ["adults", "children", "infants", "seniors"]

state_lock = threading.Lock()
state: dict = {
    "last_check": None,
    "last_error": None,
    "sailings": [],
    "notified": set(),  # (date, from, to, depart) already pushed
    "consecutive_errors": 0,
    "checking": False,
    "next_check": None,  # epoch seconds
    "hold": None,  # {"status": "adding"|"ready"|"failed"|"closed", "depart", "fare", "price", "at", "error"}
}
wake = threading.Event()


# --------------------------------------------------------------------------
# config / terminals


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)  # fills in keys added since the file was written
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


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


def hold_worker(cfg: dict, sailing: dict) -> None:
    """Add `sailing` to the cart in a visible browser, then leave it open for checkout."""
    from playwright.sync_api import sync_playwright

    def set_hold(**kw):
        with state_lock:
            state["hold"] = {**(state["hold"] or {}), **kw}

    set_hold(status="adding", depart=sailing["depart"], date=cfg["date"], fare=None, price=None, at=time.time(), error=None)
    with sync_playwright() as p:
        size = os.environ.get("BCF_WINDOW_SIZE", "1280,1000")
        browser = p.chromium.launch(headless=False, args=[f"--window-size={size}", "--window-position=0,0"])
        pg = browser.new_page(user_agent=UA, no_viewport=True)
        pg.set_default_timeout(60_000)
        try:
            if os.environ.get("BCF_USER") and os.environ.get("BCF_PASS"):
                login(pg)
            goto_sailings(pg, cfg)
            card = next(
                (c for c in pg.locator(".p-card").all()
                 if (s := parse_card(c)) and s["depart"] == sailing["depart"] and s["status"] == "available"),
                None,
            )
            if card is None:
                raise RuntimeError(f'{sailing["depart"]} is no longer bookable')
            card.locator("button:has-text('View fares')").click()
            fare, price = choose_fare(pg, cfg.get("fare", "cheapest"))
            set_hold(status="ready", fare=fare, price=price, at=time.time())
            pg.bring_to_front()
            day = datetime.strptime(cfg["date"], "%Y-%m-%d").strftime("%a %b %-d")
            log(f"holding {day} {sailing['depart']} ({fare} {price}) in cart")
            mac_notify("Ferry in your cart", f"{day} {sailing['depart']}: finish checkout in the browser window")
            where = "Tap to open the checkout browser" if VNC_URL else "Finish checkout in the browser window on your Mac"
            pushover(
                f"🛒 In your cart: {sailing['depart']}",
                f"{day} {sailing['depart']}, {fare} {price}.\n{where}. Booking isn't confirmed until you pay.",
                url=VNC_URL or None,
                url_title="Open checkout",
                priority=1,
            )
            # Keep the window (and the cart's session) alive until it's closed, at most 3 hours.
            pg.wait_for_event("close", timeout=3 * 3600 * 1000)
            set_hold(status="closed")
        except Exception as e:  # noqa: BLE001
            if browser.is_connected() and not pg.is_closed():
                save_failure(pg)
                traceback.print_exc()
                set_hold(status="failed", error=f"{type(e).__name__}: {e}"[:300])
                pushover("⛴ Couldn't add to cart", f"{sailing['depart']}: {e}"[:500] + "\nBook it on your phone instead.")
            else:
                set_hold(status="closed")
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def maybe_hold(cfg: dict, sailing: dict) -> None:
    if not cfg.get("hold"):
        return
    with state_lock:
        h = state["hold"]
        if h and h["status"] in ("adding", "ready"):
            return  # one cart window at a time
    threading.Thread(target=hold_worker, args=(cfg, sailing), daemon=True).start()


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


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --------------------------------------------------------------------------
# loop


def in_window(cfg: dict, s: dict) -> bool:
    m = to_minutes(s["depart"])
    return to_minutes(cfg["earliest"]) <= m <= to_minutes(cfg["latest"])


def run_once() -> None:
    cfg = load_config()
    with state_lock:
        state["checking"] = True
    try:
        sailings = check(cfg)
        for s in sailings:
            s["in_window"] = in_window(cfg, s)
        t = terminals()
        new = [
            s
            for s in sailings
            if s["status"] == "available"
            and s["in_window"]
            and (cfg["date"], cfg["from"], cfg["to"], s["depart"]) not in state["notified"]
        ]
        if new:
            day = datetime.strptime(cfg["date"], "%Y-%m-%d").strftime("%a %b %-d")
            times = ", ".join(f'{s["depart"]} ({s["price"]})' if s["price"] else s["depart"] for s in new)
            pushover(
                f"⛴ Ferry space: {day}",
                f'{t[cfg["from"]]["short"]} → {t[cfg["to"]]["short"]}\nBookable: {times}\n\n{booking_steps(cfg, new[0])}',
                url=booking_link(cfg),
                priority=1,
            )
            maybe_hold(cfg, new[0])
        with state_lock:
            for s in new:
                state["notified"].add((cfg["date"], cfg["from"], cfg["to"], s["depart"]))
            # Forget sailings that sold out again so we re-alert if they reopen.
            for s in sailings:
                if s["status"] != "available":
                    state["notified"].discard((cfg["date"], cfg["from"], cfg["to"], s["depart"]))
            state.update(sailings=sailings, last_check=datetime.now(), last_error=None, consecutive_errors=0)
        avail = [s["depart"] for s in sailings if s["status"] == "available" and s["in_window"]]
        log(f"{cfg['date']} {cfg['from']}->{cfg['to']}: {len(sailings)} sailings, bookable in window: {avail or 'none'}")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with state_lock:
            state["last_error"] = f"{datetime.now():%H:%M:%S} {type(e).__name__}: {e}"
            state["consecutive_errors"] += 1
            n = state["consecutive_errors"]
        if n == 3:
            pushover("⛴ Ferry watcher failing", f"3 checks in a row failed:\n{e}"[:1000])
    finally:
        with state_lock:
            state["checking"] = False


def loop() -> None:
    while True:
        cfg = load_config()
        if cfg["enabled"]:
            run_once()
        delay = max(1.0, float(cfg["interval_minutes"])) * 60 * random.uniform(0.85, 1.15)
        with state_lock:
            state["next_check"] = time.time() + delay if cfg["enabled"] else None
        wake.wait(delay)
        wake.clear()


# --------------------------------------------------------------------------
# web UI (static page + JSON API)

UI_PATH = HERE / "ui.html"
CONFIG_KEYS = {
    "date": str, "from": str, "to": str, "vehicle": str, "earliest": str, "latest": str,
    "adults": int, "children": int, "infants": int, "seniors": int,
    "interval_minutes": int, "height_ft": float, "enabled": bool, "hold": bool, "fare": str,
}


def api_state() -> dict:
    cfg = load_config()
    with state_lock:
        st = {k: v for k, v in state.items() if k != "notified"}
    return {
        "config": cfg,
        "checking": st["checking"],
        "last_check": st["last_check"].isoformat() if st["last_check"] else None,
        "last_error": st["last_error"],
        "next_check": st["next_check"],
        "now": time.time(),
        "sailings": st["sailings"],
        "booking_link": booking_link(cfg),
        "hold": st["hold"],
        "vnc_url": VNC_URL,
        "login": bool(os.environ.get("BCF_USER") and os.environ.get("BCF_PASS")),
        "pushover": bool(os.environ.get("PUSHOVER_TOKEN") and os.environ.get("PUSHOVER_USER_KEY")),
    }


def api_terminals() -> dict:
    return {c: {"label": v["label"], "short": v["short"], "dests": v["dests"]} for c, v in terminals().items()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
        elif self.path == "/api/state":
            self._json(api_state())
        elif self.path == "/api/terminals":
            self._json(api_terminals())
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/config":
            cfg = load_config()
            for k, typ in CONFIG_KEYS.items():
                if k in body:
                    cfg[k] = typ(body[k])
            for k in ("adults", "children", "infants", "seniors"):
                cfg[k] = max(0, min(9, cfg[k]))
            cfg["interval_minutes"] = max(1, cfg["interval_minutes"])
            dests = terminals().get(cfg["from"], {}).get("dests", [])
            if cfg["to"] not in dests and dests:
                cfg["to"] = dests[0]
            trip_changed = any(cfg[k] != load_config()[k] for k in CONFIG_KEYS if k not in ("enabled", "interval_minutes", "hold", "fare"))
            save_config(cfg)
            if trip_changed:
                with state_lock:
                    state["sailings"] = []
            wake.set()
        elif self.path == "/api/hold/dismiss":
            with state_lock:
                if state["hold"] and state["hold"]["status"] in ("failed", "closed"):
                    state["hold"] = None
        elif self.path == "/api/check":
            wake.set()
        elif self.path == "/api/test":
            cfg = load_config()
            try:
                pushover("⛴ Ferry watch test", "Notifications are working. Tap to open the booking page.", url=booking_link(cfg))
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 502)
                return
        else:
            self.send_error(404)
            return
        self._json(api_state())


def main() -> None:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
    if "--once" in sys.argv:
        run_once()
        print(json.dumps(state["sailings"], indent=2))
        return
    threading.Thread(target=loop, daemon=True).start()
    log(f"web UI on http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
