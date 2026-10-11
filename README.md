# ferry-watch

Watches bcferries.com for bookable sailings and sends a Pushover notification
with a link to the booking page.

```sh
./run.sh            # web UI + watcher on http://localhost:8765 (creds from 1Password)
./run.sh --once     # single check, prints JSON
```

Edit the date, route, passengers, vehicle and time window in the web UI. The
settings are stored in `config.json`. You get one alert for each sailing that
becomes bookable. If that sailing sells out and reopens, you get alerted again.
After 3 failed checks in a row you also get a notification. The page from the
last failure is saved to `last-error.png`.

The site is behind Queue-it, so each check drives the booking flow in headless
Chromium. That takes about 10 seconds.

The link in the notification opens the booking form with the route and date
already filled in. The notification then lists the remaining taps for your
trip. BC Ferries needs a security (CSRF) token from your own browser for each
step, so a link can't skip ahead.

## Cart on this Mac

When "Add to cart on this Mac" is on (Advanced), a sailing that opens up also
gets added to the cart in a visible Chromium window. The window logs in with
the `Bcferries` item from 1Password, picks the fare you chose, and stops at
the review page. A second Pushover alert tells you when it's ready. Finish
checkout in that window. Nothing is booked until you pay.

The cart stays with that browser window, not your account. You can't pick it
up on your phone or in another browser, and closing the window abandons it.

## Running in Kubernetes

The image (`ghcr.io/mchestr/ferry-watch`) runs the watcher next to a virtual
display. Xvfb provides the display, x11vnc serves it, and noVNC shows it in a
web browser on port 6080 at `/vnc/vnc.html`. The cart browser opens on that
display, so you can finish checkout from any device that can reach the page.

| Env | Purpose |
|---|---|
| `PUSHOVER_TOKEN`, `PUSHOVER_USER_KEY` | Notifications |
| `BCF_USER`, `BCF_PASS` | BC Ferries login for the cart |
| `BCF_VNC_URL` | Link in the "in your cart" alert, e.g. `https://ferry.example/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/websockify` |
| `SCREEN_WIDTH`, `SCREEN_HEIGHT` | Virtual display size (default 1280x1000) |

VNC has no password. Put both ports behind an authenticating proxy.
