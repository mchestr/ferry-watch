# Ferry Watch

Watches [BC Ferries](https://www.bcferries.com) for reservations on sold-out sailings. Each person signs in and
watches their own trips. When a sailing opens it sends an alert (Web Push, and email for what matters most), and
can put the sailing in a BC Ferries cart straight away, in a browser kept for that trip. You finish checkout in
that same browser, from any device, through the checkout viewer.

- Each trip is watched by its own browser pod. A trip that's soon is re-checked every couple of minutes; one
  further out gets a short check every few hours. A check takes a few seconds.
- Auto-cart: an opened sailing in your window goes into the cart within seconds, with the fare you prefer. The
  hold ends when you check out, release it, or its clocks run out, and the spot is handed back.
- Quiet hours (each person's own, 10 pm to 5 am until they change them): no checks or holds overnight.
- A web dashboard with live updates, usable on a phone, and an admin page for invitations, limits and the fleet.

## How it works

bcferries.com sits behind a Queue-it waiting room and keeps each booking search in a server-side session, so
each trip's checks run in a real Chromium, driven with Playwright. The design is in
[docs/multi-tenant/](docs/multi-tenant/README.md); what's built is in
[implementation.md](docs/multi-tenant/implementation.md).

```
 phone / laptop ──▶ ferry-web ──── Postgres ──── ferry-control ──▶ Kubernetes API (browser pods)
   (dashboard,      sign-in (OIDC), the API,        the hub the pods dial, the fleet loop
    Web Push)       the alert outbox                     ▲
        │                                                │ one WebSocket per pod (closed vocabulary)
        │                                                │
        └── checkout ──▶ vnc-edge ──▶ browser pod (one per trip): the agent, Xvnc, Chromium ──▶ bcferries.com
             (a one-time ticket)
```

- **ferry-web** signs people in (OIDC, invite-only), serves the dashboard and each person's API, mints checkout
  tickets, and sends alerts (Web Push and email) from an outbox. It has no Kubernetes access.
- **ferry-control** decides which trips get a browser, starts and reaps the browser pods, and runs the hub they
  dial. It's the only part that talks to the API server.
- **A browser pod** runs the agent: one trip's watcher, its Chromium on a private Xvnc, and the gate that admits
  a checkout viewer with a ticket.
- **vnc-edge** serves the checkout page (KasmVNC's web client, or the streamed viewer) and proxies its socket to
  the pod holding the cart. It holds no secrets.

Code layout (`src/ferry_watch/`):

| Package or module | Responsibility |
|---|---|
| `models.py`, `polling.py`, `holds.py`, `protocol.py`, `tickets.py`, `versions.py` | The shared core: trips and their validation, the poll curve and quiet hours, the hold clocks, the agent ↔ ferry-control messages, checkout tickets |
| `bcferries/` | Everything that knows bcferries.com: `site.py` (the browser flow), `parse.py` (pure HTML parsing), `urls.py`, `terminals.py`, `errors.py` |
| `agent/` | The browser pod: the one-trip watcher, the hub link, the VNC gate, the streamed viewer, lockdown self-tests |
| `control/` | ferry-control: migrations and grants, the hub, the fleet planner, the pod template |
| `web/` | ferry-web: sign-in, the API, admin, the live socket, the alert outbox |

The frontend (`web/`) is React, TypeScript, Vite, Tailwind, Radix UI and TanStack Query; the live UI is
`web/src/mt/`. The trip form's route map is drawn from data bundled with the app (no map service sees where
anyone is going): see [the route map](#the-route-map). The checkout host is `vnc-edge/`.

## Deployment

CI builds three images and pushes them from `main` and version tags:

| Image | What |
|---|---|
| `ghcr.io/mchestr/ferry-watch-control` (`Dockerfile` target `control`, the default) | `ferry-web` and `ferry-control`, with the dashboard; no browser libraries |
| `ghcr.io/mchestr/ferry-watch-browser` (target `browser`) | One browser pod: `ferry-watch-agent`, KasmVNC's Xvnc, matchbox and Chromium |
| `ghcr.io/mchestr/ferry-watch-vnc-edge` (`vnc-edge/Dockerfile`) | The checkout host: stock nginx with KasmVNC's web client |

They're deployed with app-template as one HelmRelease in home-cluster. `deploy/helmrelease.yaml` and
`deploy/externalsecret.yaml` are copies of it: CI renders the copy, runs kubeconform on it, and proves its
admission policies and RBAC against a real API server (`tests/apiserver`), so change both copies together.

- `ferry-web run` and `ferry-control run` read their `config.json` (rendered from the HelmRelease's `ferry.*`
  values) and secrets from files. `ferry-control migrate` creates and migrates the database (the db hook Job).
- `ferry-web keys` prints a fresh checkout key pair and Web Push key, for setting up.

**Security model:** ferry-web is the only part on the internet that people sign in to; the checkout host's only
credential is a one-minute ticket. Browser pods run in their own namespace behind default-deny network policies,
and admission policies admit only the exact pod ferry-control builds; nobody can change or enter one afterwards.
The protocol between the pods and ferry-control can't carry a URL, a script, a cookie or a screenshot.

## Development

```sh
uv sync                     # Python 3.12 environment from uv.lock
uv run pytest -m "not browser and not apiserver"
uv run ruff format && uv run ruff check && uv run mypy && uv run lint-imports

cd web
npm ci
npm run dev                 # Vite on :5173, proxying /api (and the WebSocket) to :8765
npm run lint && npm run typecheck && npm test && npm run format:check && npm run build
```

- The control plane's tests start a throwaway PostgreSQL if one is installed (or use `FW_TEST_PGHOST`); without
  either they skip.
- `pytest -m browser` runs the browser contract tests in Chromium; the checkout tests also need KasmVNC
  (`FW_KASMVNC_WWW` pointing at its `www` directory, and `Xvnc`, `matchbox-window-manager`, `xrandr` and
  `xsettingsd` on the path). `pytest -m apiserver` needs envtest's binaries (`FW_ENVTEST_BIN`) and helm
  (`FW_HELM`). CI's workflow shows how it installs each.
- `node --test vnc-edge/tests/*.test.mjs` runs the streamed viewer's DOM-free modules.

Tests never touch bcferries.com:
- The parsers are tested against saved copies of its pages (`tests/fixtures`).
- The browser contract tests (`tests/browser`) run the real booking-flow code in Chromium against
  a local fake site serving those pages. Chromium can't resolve any real hostname there.

CI runs all of it, builds the images, smoke-tests each one read-only and unprivileged, and only pushes from
`main` and version tags. The Playwright package in `uv.lock` must match the version of the
`mcr.microsoft.com/playwright/python` image (CI checks this). Bump them together.

### The route map

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
