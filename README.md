# Waitlist

Waitlist watches reservation systems ("providers") for openings on things that are booked up, and alerts you
when one opens: one dashboard for every watch, whatever system it's on.

- **BC Ferries**: sailings, with auto-cart and checkout through the remote viewer.
- **Buntzen Lake**: BC Hydro's day-use parking passes, sold on Yodel. Alert-only: you book on Yodel with your
  own account. Each deployment chooses who is offered it (`waitlist.providers.buntzen.offer`).

Each person signs in and watches their own things. Alerts arrive by Web Push, and by email for what matters
most. The provider framework's design contract is [docs/waitlist/design.md](docs/waitlist/design.md) (its
amendments come first); running the Buntzen Lake provider is
[docs/providers/buntzen.md](docs/providers/buntzen.md).
Waitlist was called Ferry Watch until October 2026; the design record in
[docs/multi-tenant/](docs/multi-tenant/README.md) still uses the old names, and each of its files starts with
the mapping.

## BC Ferries

Waitlist watches [BC Ferries](https://www.bcferries.com) for reservations on sold-out sailings. When one
opens it can put the sailing in a BC Ferries cart straight away, in a browser kept for that trip. You finish
checkout in that same browser, from any device, through the checkout viewer.

- Each trip is watched by its own browser pod. A trip that's soon is re-checked every couple of minutes; one
  further out gets a short check every few hours. A check takes a few seconds.
- Auto-cart: an opened sailing in your window goes into the cart within seconds, with the fare you prefer. The
  hold ends when you check out, release it, or its clocks run out, and the spot is handed back.
- Quiet hours (each person's own, 10 pm to 5 am until they change them): no checks or holds overnight.
- A web dashboard with live updates, usable on a phone, and an admin page for invitations, limits and the fleet.

## Buntzen Lake

Passes for a day go on sale at 7:00 am Pacific the day before, and popular ones sell out in seconds. A Buntzen
watch is a date and the passes that would do (all day, morning, afternoon). Waitlist reminds you at 6:55, checks
from 7:00:02 (every 30 s for 20 minutes, then every minute, every 30 minutes out of season), and alerts once a
wanted pass is open; the alert opens the watch's page, whose first button books it on Yodel. Waitlist holds no
Yodel account. How it's checked, how an answer is read and what still has to be confirmed with real captures
are in [docs/providers/buntzen.md](docs/providers/buntzen.md).

## How it works

bcferries.com sits behind a Queue-it waiting room and keeps each booking search in a server-side session, so
each BC Ferries trip's checks run in a real Chromium, driven with Playwright. The design is in
[docs/multi-tenant/](docs/multi-tenant/README.md); what's built is in
[implementation.md](docs/multi-tenant/implementation.md). A provider with a plain HTTP API, like Buntzen
Lake's, is checked by waitlist-control's HTTP check loop through **waitlist-fetcher** instead
([docs/waitlist/design.md §4](docs/waitlist/design.md)).

```
 phone / laptop ──▶ waitlist-web ──── Postgres ──── waitlist-control ──▶ Kubernetes API (browser pods)
   (dashboard,      sign-in (OIDC), the API,        the hub the pods dial, the fleet loop
    Web Push)       the alert outbox                     ▲
        │                                                │ one WebSocket per pod (closed vocabulary)
        │                                                │
        └── checkout ──▶ vnc-edge ──▶ browser pod (one per trip): the agent, Xvnc, Chromium ──▶ bcferries.com
             (a one-time ticket)

 waitlist-control's HTTP check loop ──(closed wire)──▶ waitlist-fetcher ──▶ api.yodelpass.com (Buntzen Lake)
                                                       (no database, no secrets, no Kubernetes)
```

- **waitlist-web** signs people in (OIDC, invite-only), serves the dashboard and each person's API, mints
  checkout tickets, and sends alerts (Web Push and email) from an outbox. It has no Kubernetes access.
- **waitlist-control** decides which trips get a browser, starts and reaps the browser pods, and runs the hub
  they dial. It's the only part that talks to the API server. Its HTTP check loop schedules every HTTP-checked
  watch (coalescing identical queries across people, within each provider's request budget), records what was
  found and queues the alerts.
- **waitlist-fetcher** makes those checks' requests, to the providers' declared hosts only, and parses the
  answers into typed inventories. It holds no secret, no token and no database credential.
- **A browser pod** runs the agent: one trip's watcher, its Chromium on a private Xvnc, and the gate that admits
  a checkout viewer with a ticket.
- **vnc-edge** serves the checkout page (KasmVNC's web client, or the streamed viewer) and proxies its socket to
  the pod holding the cart. It holds no secrets.

Code layout (`src/waitlist/`):

| Package or module | Responsibility |
|---|---|
| `models.py`, `polling.py`, `holds.py`, `protocol.py`, `tickets.py`, `versions.py` | The shared core: trips and their validation, the poll curve and quiet hours, the hold clocks, the agent ↔ waitlist-control messages, checkout tickets |
| `providers/` | Every provider's catalog entry, and the pure HTTP-checked providers (`buntzen/`): specs, queries, check windows, evaluation, alert wording |
| `fetchwire.py`, `fetcher/` | The closed wire between waitlist-control and waitlist-fetcher, and the fetcher itself |
| `bcferries/` | Everything that knows bcferries.com: `site.py` (the browser flow), `parse.py` (pure HTML parsing), `urls.py`, `terminals.py`, `errors.py` |
| `agent/` | The browser pod: the one-trip watcher, the hub link, the VNC gate, the streamed viewer, lockdown self-tests |
| `control/` | waitlist-control: migrations and grants, the hub, the fleet planner, the pod template, the HTTP check loop (`checkplan.py`, `checks.py`, `fetch.py`) |
| `web/` | waitlist-web: sign-in, the API (`watches.py` for every provider), admin, the live socket, the alert outbox |

`scripts/capture_yodel.py` captures Yodel's real answers as parser fixtures (the owner runs it, never CI), and
`scripts/build_route_map.py` builds the BC Ferries route map's data.

The frontend (`web/`) is React, TypeScript, Vite, Tailwind, Radix UI and TanStack Query; the live UI is
`web/src/mt/`. The trip form's route map is drawn from data bundled with the app (no map service sees where
anyone is going): see [the BC Ferries route map](#the-bc-ferries-route-map). The checkout host is `vnc-edge/`.

## The API

Every route acts for the signed-in user (`Authorization: Bearer <session token>`); every write answers with the
user's whole state, which the live socket (`/api/ws`) pushes too. Errors are `{"error", "field"?, "limit"?,
"usage"?}`.

| Route | What |
|---|---|
| `GET /api/state` | `me`, `limits`, `usage`, `watches` (every live watch, any provider), `providers`, `capacity`, `settings`, `quiet_hours`, `devices`, `events`, `links`, `push` |
| `GET /api/providers` | The catalog: each provider, whether it's `offered` to you and whether its checks are `paused` |
| `POST /api/watches` | `{provider, spec, enabled?}`: a new watch; answers `{watch_id, state}` |
| `PATCH /api/watches/{id}` | `{spec?, enabled?}`: `spec` is partial, over the current one |
| `DELETE /api/watches/{id}` | Stop watching |
| `POST /api/watches/{id}/check` | Check now (`manualChecksPerHour`) |
| `POST /api/watches/{id}/done` | "I booked it", for an alert-only watch |
| `POST /api/trips/{id}/hold`, `/cart/release`, `/cart/dismiss`, `/vnc`, `/booked` | BC Ferries' holds, carts and checkout |
| `PATCH /api/me`, `POST`/`DELETE /api/me/push-subscriptions`, `POST /api/alerts/test`, `GET /api/terminals` | Settings, devices, a test alert, BC Ferries' terminals |
| `/api/admin/*` | Invitations, people, limits, the fleet's switches (including pausing a provider's checks) |

`POST /api/trips`, `PATCH`/`DELETE /api/trips/{id}` and `POST /api/trips/{id}/check` remain as BC Ferries-only
aliases until 1.1; the dashboard doesn't use them.

## Deployment

CI builds three images and pushes them from `main` and version tags:

| Image | What |
|---|---|
| `ghcr.io/mchestr/waitlist-control` (`Dockerfile` target `control`, the default) | `waitlist-web`, `waitlist-control` and `waitlist-fetcher`, with the dashboard; no browser libraries |
| `ghcr.io/mchestr/waitlist-browser` (target `browser`) | One browser pod: `waitlist-agent`, KasmVNC's Xvnc, matchbox and Chromium |
| `ghcr.io/mchestr/waitlist-vnc-edge` (`vnc-edge/Dockerfile`) | The checkout host: stock nginx with KasmVNC's web client |

They're deployed with app-template as one HelmRelease in home-cluster
(`kubernetes/apps/waitlist/waitlist/`, whose README is the runbook). That HelmRelease is the only copy: this
repo has no Helm or Kubernetes files, and home-cluster's CI renders and checks it.
The release runs waitlist-web, waitlist-control, waitlist-fetcher and vnc-edge as Deployments, and
`waitlist-control migrate` as the db hook Job, which creates and migrates the `waitlist` database.

## Configuration

Each process reads one `config.json`, rendered from the HelmRelease's `waitlist.*` values into a ConfigMap, and
takes its secrets from mounted files. A setting shared by several processes (the providers, the streamed viewer)
is set once, at the top of `waitlist.*`; the render refuses it written into one process's own section.

| Process | Config | Secrets and other environment |
|---|---|---|
| `waitlist-web run` | `WL_WEB_CONFIG` (default `/etc/waitlist-web/config.json`; `src/waitlist/web/config.py`) | `WL_OIDC_CLIENT_ID_FILE`, `WL_OIDC_CLIENT_SECRET_FILE`, `WL_CHECKOUT_KEY_FILE`, `WL_VAPID_KEY_FILE`; the database from the libpq variables |
| `waitlist-control run`, `migrate` | `WL_CONTROL_CONFIG` (default `/etc/waitlist-control/config.json`; `src/waitlist/control/config.py`) | The database from the libpq variables; the API server from its ServiceAccount |
| `waitlist-fetcher run` | `WL_FETCHER_CONFIG` (default `/etc/waitlist-fetcher/config.json`; `src/waitlist/fetcher/config.py`) | None: no database, no token, no secret |

All three take `WL_LOG_LEVEL`. A browser pod's environment (`WL_TRIP_ID`, `WL_OWNER`, `WL_HUB_URL`, ...) is
set by waitlist-control and pinned by the admission policy, never by hand.

The values a deployment chooses (home-cluster's `app/helmrelease.yaml` documents each):

- `waitlist.hosts`: the dashboard (`waitlist.chestr.dev`) and checkout (`waitlist-checkout.chestr.dev`) hosts.
- `waitlist.browserImage` and each controller's image: every tag pins its digest (`<version>@sha256:<digest>`).
- `waitlist.checkout.streamViewer`: who is offered the streamed checkout viewer, `"off"`, `"admins"` or
  `"everyone"`.
- `waitlist.providers.buntzen`: `offer` (`"off"`, the default, `"admins"` or `"everyone"`), `requestsPerMinute`
  (waitlist-control's budget, 1-30) and `fetcherPerMinute` (waitlist-fetcher's own hard cap). With `"off"` the
  configs carry nothing about the provider. BC Ferries is always on.
- `waitlist.control.*` (the browser pods, the fleet, hold clocks, polling, limits) and `waitlist.web.*` (sign-in,
  invitations, links, email).

The secrets come from 1Password through home-cluster's `app/externalsecret.yaml`: `WAITLIST_CHECKOUT_KEY` and
`WAITLIST_VAPID_KEY` from the `waitlist` item, `WAITLIST_OAUTH_CLIENT_ID` and `WAITLIST_OAUTH_CLIENT_SECRET`
from `authelia`, and the two database passwords from `cloudnative-pg`. `waitlist-web keys` prints a fresh
`WAITLIST_CHECKOUT_KEY=` and `WAITLIST_VAPID_KEY=` for setting up a deployment; `waitlist.control.pod.vncPubkeys`
carries the checkout key's public half.

**Security model:** waitlist-web is the only part on the internet that people sign in to; the checkout host's
only credential is a one-minute ticket. Browser pods run in their own namespace behind default-deny network
policies, and admission policies admit only the exact pod waitlist-control builds; nobody can change or enter one
afterwards. The protocol between the pods and waitlist-control can't carry a URL, a script, a cookie or a
screenshot.

## Development

```sh
uv sync --locked            # Python 3.12 environment from uv.lock
uv run ruff format --check && uv run ruff check && uv run mypy && uv run lint-imports
uv run pytest -m "not browser"
uv run pytest -m browser    # Chromium (and KasmVNC for the checkout tests; see below)
uv run python scripts/capture_yodel.py --dry-run  # the exact Yodel request, sent nowhere
node --test vnc-edge/tests/*.test.mjs             # the streamed viewer's DOM-free modules

cd web
npm ci
npm run dev                 # Vite on :5173, proxying /api (and the WebSocket) to :8765
npm run lint && npm run typecheck && npm test && npm run format:check && npm run build
```

- The control plane's tests start a throwaway PostgreSQL if one is installed (or use `WL_TEST_PGHOST`); without
  either they skip.
- `pytest -m browser` runs the browser contract tests in Chromium; the checkout tests also need KasmVNC
  (`WL_KASMVNC_WWW` pointing at its `www` directory, and `Xvnc`, `matchbox-window-manager`, `xrandr` and
  `xsettingsd` on the path).

Tests never touch bcferries.com or Yodel:
- The parsers are tested against saved copies of their pages and answers (`tests/fixtures`).
- The browser contract tests (`tests/browser`) run the real booking-flow code in Chromium against
  a local fake site serving those pages. Chromium can't resolve any real hostname there.
- waitlist-fetcher's transport is tested against a local server, and the HTTP check loop against fakes.

CI runs all of it, builds the images, smoke-tests each one read-only and unprivileged, and only pushes from
`main` and version tags. The Playwright package in `uv.lock` must match the version of the
`mcr.microsoft.com/playwright/python` image (CI checks this). Bump them together.

### The BC Ferries route map

The trip form shows a small map of the route. It's an SVG drawn from
`web/src/lib/route-map-data.json`: Natural Earth's public-domain coastline, each terminal's
berth, and a path through the water for every route BC Ferries books. The app loads it (about
35 KB gzipped) the first time it shows a map. `scripts/build_route_map.py` writes it, and its
docstring says where to download the coastline and how to run it. When BC Ferries adds a
terminal or a route, add it to the script's `TERMINALS` table and rebuild. Until then there's no
map for a trip to or from a new terminal, and a new route between known ones gets its two ends
but no line. The script checks each berth against the coast and each path against the land. The
coastline leaves out most small bays, so a berth that falls inside the land is drawn just off the
nearest shore.

## Things to know about BC Ferries

- Carts are guest carts: the cart belongs to the browser session, not anyone's account, which is why checkout
  happens in the same browser. Releasing a cart empties it, so the spot goes back straight away.
- When BC Ferries changes its markup, the parsers fail loudly ("BC Ferries probably changed their
  site") rather than reporting nothing as bookable. Recapture the fixtures (see
  `tests/fixtures/README.md`) and update the selectors.
