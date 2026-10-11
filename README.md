# ferry-watch

Watches bcferries.com for bookable sailings and sends a Pushover notification
with a link to the booking page.

```sh
(cd web && npm ci && npm run build)   # build the React frontend once
./run.sh            # web UI + watcher on http://localhost:8765 (creds from 1Password)
./run.sh --once     # single check, prints JSON
```

To work on the frontend, keep `./run.sh` running and start the Vite dev server
with `cd web && npm run dev`. It proxies `/api` to the watcher. The stack is
React, TypeScript, Vite, Tailwind, Radix UI and TanStack Query.

Edit the date, route, passengers, vehicle and time window in the web UI. The
settings are stored in `config.json`. You get one alert for each sailing that
becomes bookable. If that sailing sells out and reopens, you get alerted again.
After 3 failed checks in a row you also get a notification. The page from the
last failure is saved to `last-error.png`.

The site is behind Queue-it, so the watcher drives a real Chromium. It keeps one
browser open: it walks the booking form once, then polls by reloading the
`/fare-selection` page, about 3 seconds per check, every 30 seconds by default.
When a sailing in your window opens and auto-cart is on, that same page picks
the fare straight away. Polling pauses until you release the cart from the UI.
Releasing also empties the BC Ferries cart.

The link in the notification opens the booking form with the route and date
already filled in. The notification then lists the remaining taps for your
trip. BC Ferries needs a security (CSRF) token from your own browser for each
step, so a link can't skip ahead.

## Auto-cart

When auto-cart is on, the watcher's browser is logged in with your BC Ferries
account. A sailing that opens up goes straight into the cart with the fare you
chose, and the browser stops at the review page. The Pushover alert tells you
it's in the cart. Finish checkout in that browser window (or over noVNC in the
cluster). Nothing is booked until you pay.

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
