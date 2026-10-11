# Ferry Watch

Watches [BC Ferries](https://www.bcferries.com) for reservations on sold-out sailings. When
one opens it sends a Pushover alert, and can put the sailing in your BC Ferries cart straight
away. You finish checkout in that same browser, from any device, through a built-in remote viewer.

- Watch up to six trips at once (route, date, travellers, vehicle, departure window).
- Each trip re-checks every 30 seconds by default; a check takes about 3 seconds.
- Auto-cart: an opened sailing in your window goes into the cart within seconds, with the fare
  you prefer. The trip pauses until you release the cart.
- A web dashboard with live updates over a WebSocket, usable on a phone.

## How it works

bcferries.com sits behind a Queue-it waiting room and keeps each booking search in a
server-side session, so Ferry Watch drives a real Chromium with Playwright:

```
                    ┌──────────────── one asyncio event loop ────────────────┐
browser ── HTTP ──▶ │ aiohttp: REST API, WebSocket push, the React dashboard  │
          + WS      │    ▲ state changes                                       │
                    │ Watcher ── one Chromium, one browser context per trip ─┼──▶ bcferries.com
                    │    │                                                     │
                    │    └── alerts (retried in the background) ─────────────┼──▶ Pushover
                    └──────────────────────────────────────────────────────────┘
KasmVNC (:6080) shows that Chromium's screen in your browser, sized to your device.
```

- Each trip's session walks the booking form once. The date and route come pre-filled from the
  URL. After that, the watcher reloads the results page, which BC Ferries re-runs with fresh
  availability.
- When a sailing in the trip's window opens and auto-cart is on, the same page selects a fare.
  BC Ferries puts it in that session's cart, and the alert links to the remote viewer.
- The cart belongs to that browser session, not your account, which is why checkout happens in
  the same browser. Releasing a cart from the dashboard empties it, so the spot goes back
  straight away.

Code layout (`src/ferry_watch/`):

| Module | Responsibility |
|---|---|
| `models.py` | Trips, sailings, fares, and every validation rule |
| `store.py` | The configuration: in memory, validated, written atomically, versioned |
| `state.py` | Runtime state for the dashboard, the cart state machine, alert memory |
| `watcher.py` | The watch loop: sessions, checks, back-off, carts, alerts |
| `bcferries/` | Everything that knows bcferries.com: `site.py` (the browser flow), `parse.py` (pure HTML parsing), `terminals.py`, `errors.py` |
| `notify.py` | Alert wording and Pushover delivery with retries |
| `server.py`, `views.py` | The HTTP API, WebSocket hub and security headers; the JSON shapes ([docs/api.md](docs/api.md)) |
| `settings.py`, `__main__.py` | Environment configuration and the entry point |

The frontend (`web/`) is React, TypeScript, Vite, Tailwind, Radix UI and TanStack Query.

## Configuration

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `BCF_HOST`, `BCF_PORT` | `127.0.0.1`, `8765` | Where the web server listens |
| `BCF_CONFIG` | `config.json` | Where trips are stored. `alerted.json` and `terminals.json` go next to it |
| `BCF_STATIC` | (none) | The built frontend (`web/dist`); the image sets `/app/static` |
| `BCF_USER`, `BCF_PASS` | (none) | Your BC Ferries login. Needed for auto-cart, so saved cards are available at checkout |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER_KEY` | (none) | Pushover alerts. Without them, alerts only go to the log |
| `BCF_VNC_URL` | (none) | The remote viewer's address, linked from cart alerts |
| `BCF_HEADLESS` | (off) | `1` runs Chromium without a window (local development) |
| `BCF_DEBUG_DIR` | `$TMPDIR/ferry-watch` | Screenshots of failed checks, newest 20 kept. Empty disables them |
| `SCREEN_WIDTH`, `SCREEN_HEIGHT` | `1280`, `1000` | Initial size of the virtual screen (the viewer resizes it) |
| `LOG_LEVEL` | `INFO` | Python log level |

Everything else (trips, check frequency, the global on/off switch) is set in the dashboard.

## Running

The container (`ghcr.io/mchestr/ferry-watch`) runs KasmVNC (the virtual screen and its web
viewer on port 6080 under `/vnc`), a window manager, and the app on port 8765.

**Security model:**
- Ferry Watch has no login of its own, and the remote viewer has no password. **Only run it
  behind an authenticating proxy** (Authelia, oauth2-proxy, ...), and restrict network access so
  only that proxy can reach ports 8765 and 6080.
- The API refuses cross-origin and non-JSON writes, the WebSocket checks its Origin, and the
  page is served with a strict Content-Security-Policy.
- Credentials never reach logs, alerts or the dashboard. Failure screenshots are viewport-only
  and stay out of the config directory.
- The image runs as a non-root user and works with a read-only root filesystem, given a
  writable `/tmp` and `/config`.

To run locally with a real browser window (secrets from 1Password if the CLI is signed in):

```sh
scripts/run-local.sh        # http://localhost:8765
```

## Development

```sh
uv sync                     # Python 3.12 environment from uv.lock
uv run pytest               # unit, API and browser contract tests (no network access)
uv run pytest -m "not browser"
uv run ruff format && uv run ruff check && uv run mypy

cd web
npm ci
npm run dev                 # Vite on :5173, proxying /api (and the WebSocket) to :8765
npm run lint && npm run typecheck && npm test && npm run format:check && npm run build
```

Tests never touch bcferries.com:
- The parsers are tested against saved copies of its pages (`tests/fixtures`).
- The browser contract tests (`tests/browser`) run the real booking-flow code in Chromium against
  a local fake site serving those pages. Chromium can't resolve any real hostname there.

CI runs all of it, builds the image, smoke-tests the container, and only pushes from `main` and
version tags. The Playwright package in `uv.lock` must match the version of the
`mcr.microsoft.com/playwright/python` image (CI checks this). Bump them together.

## Things to know about BC Ferries

- Checking while logged in means your account is visibly refreshing the site. Keep the interval
  reasonable. A rejected login pauses auto-cart logins for 30 minutes instead of retrying.
- BC Ferries' login page won't accept a login while a guest cart exists, so the watcher logs in
  first and then adds to the cart.
- When BC Ferries changes its markup, the parsers fail loudly ("BC Ferries probably changed their
  site") rather than reporting nothing as bookable. Recapture the fixtures (see
  `tests/fixtures/README.md`) and update the selectors.
