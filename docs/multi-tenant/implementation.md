# Multi-tenant Ferry Watch: what's built

This tracks the implementation against the plan ([README.md](README.md),
[design-reference.md](design-reference.md), [decisions.md](decisions.md)). It lists what exists,
where it departs from the plan's wording and why, and what's still to do.

Everything is in one distribution, `ferry-watch`, split into packages whose boundaries
import-linter holds (`lint-imports` in CI). Three entrypoints: `ferry-watch-agent` (a browser pod),
`ferry-control` (the operator) and `ferry-web` (the internet-facing app). Today's single-container
app (`ferry-watch`, `server.py`, `watcher.py`) is untouched apart from shared groundwork, and keeps
running until cutover.

## Built

### Shared core (no browser, no database)

| Module | What |
|---|---|
| `protocol.py` | Agent ↔ ferry-control messages, v1 plus the round-3 additions (`mode`, `cart_claim`, `short_check_done`). Closed vocabulary; a schema test proves no field can carry a URL, cookie, script or selector. |
| `polling.py` | The poll curve, the warm window, short checks, the 30-day horizon and quiet hours. |
| `holds.py` | The four hold clocks and the payment guard. |
| `tickets.py` | Ed25519 checkout tickets (`fw1.<kid>.<payload>.<sig>`), with the signed display fields. |
| `bcferries/urls.py` | `BASE_URL`, the user agent, `search_url`, and `checkout_view()` (what a held cart's checkout shows, from URLs only). |

### The browser pod (`ferry_watch.agent`)

| Module | What |
|---|---|
| `watch.py` | `TripWatcher`, the one-trip watcher: the operator's interval with a 30 s floor, a token bucket, fail-slow; cart claims and compiled limits; the hold clocks; a viewed hold, a booking or a short-check hold ends the pod; drains empty the cart unless a payment is under way. |
| `gate.py` | The VNC gate on :6080: Origin, the agent's go-ahead, then a ticket (first connect, locks the cart to the device's nonce) or the nonce (reconnect). |
| `hub.py` | The link to ferry-control: dials out with the projected token, buffers events, alerts and bye until acknowledged, answers claims. |
| `lockdown.py` | Self-tests after every Chromium launch, failing closed. |
| `processes.py`, `main.py`, `settings.py` | Xvnc (unix socket only) and matchbox; the supervisor; the eight pinned env vars. |

### ferry-control (`ferry_watch.control`)

| Module | What |
|---|---|
| `migrations/0001_init.sql`, `db.py` | The schema, and per-role grants with row-level security (§6.4). |
| `hub.py`, `store.py` | The hub endpoint: TokenReview binding, idempotent recording, cart claims (one hold per user), configure pushes, commands, NOTIFY on change. |
| `planner.py`, `fleet.py`, `pods.py`, `kube.py` | The fleet loop: warm and short-check pods, admission and fairness, reaping timers, hold egress, the pod template. |
| `configure.py`, `limits.py`, `config.py`, `main.py` | Cart gating, the curve with the budget stretch, per-user limits, the deployment's config, `migrate` and `run`. |

### ferry-web (`ferry_watch.web`)

| Module | What |
|---|---|
| `identity.py`, `accounts.py`, `auth.py` | OIDC (code + PKCE), id_token verification, computed user ids, invite-only admission, cookie-less bearer sessions re-verified every 24 h. |
| `api.py`, `repo.py` | Each user's API, scoped in SQL; checkout ticket minting. |
| `admin.py` | Invitations, suspension, limit overrides, the fleet switches, the overview. |
| `live.py` | The dashboard socket (first-message auth, NOTIFY-driven). |
| `outbox.py`, `push.py`, `messages.py` | The alert outbox: Web Push (RFC 8291/8292) and email. |
| `config.py`, `main.py`, `http.py`, `app.py` | Config and secrets, middleware, the SPA shell. |

### The web app (`web/src/mt/`)

| Module | What |
|---|---|
| `MtApp.tsx`, `auth.ts`, `router.ts` | Sign-in (one button; the code leaves the address bar at once), quiet re-verification on a 401, the signed-out screen, `/`, `/c/<trip>` and `/auth/callback`. |
| `MyTripCard.tsx`, `trips.ts` | Each trip in plain words (watching closely, checked every 6 h, waiting for a free browser, holding a cart, "Did you book it?"), the held cart, release and dismiss, check now, the shared trip editor. |
| `CheckoutPage.tsx` | `/c/<trip>`: never opens the checkout by itself; "Check out on this device" mints the one-minute link. |
| `AccountCard.tsx`, `push.ts`, `public/sw.js` | Web Push for the device, a test alert, pause everything, sign out everywhere, the Ko-fi link. |
| `live.ts`, `api.ts`, `schema.ts` | The per-user socket (first-message auth) and the bearer API. |
| `AdminPage.tsx`, `admin.ts`, `ReasonDialog.tsx` | `/admin`, for admins. Fleet: the switches and the breaker, capacity and budget from ferry-control's last pass, holds now (cart summary only) with release and recycle, the attention list, the week's outcomes. People: limits and where each comes from, sessions, devices, today's usage, sign out everywhere, make or remove admin, suspend (releasing a held cart or letting it finish), reinstate, delete, invitations, sign-ins turned away. Every change asks why. |

### vnc-edge (`vnc-edge/`)

| File | What |
|---|---|
| `Dockerfile`, `build.py` | Stock nginx-unprivileged plus KasmVNC 1.5.0's web client from the sha256-checked package, with one line inserted in `vnc.html`. `build.py` computes the CSP from the shipped page and fails the build if the page no longer has exactly one inline script and one module script. |
| `default.conf.in` | §7.9's nginx config: the landing page, the client's files, and one WebSocket proxy per pod, nothing else. |
| `site/fw/shim.js` | Offers the device's nonce, and the ticket until the first connect succeeds, on this checkout's socket only; reloads with back-off if the client gives up; stops at the hard deadline. |
| `site/fw/landing.html`, `landing.js`, `site/gone.html` | The ticket in, the nonce made and kept, the sailing and deadlines shown as text; an expired ticket goes back to Ferry Watch to open it again. |
| `smoke-test.sh` | Run by CI against the image, read-only and unprivileged. |

`tests/browser/test_checkout_edge.py` drives KasmVNC's stock client in Chromium through the shim into the
real gate (with a minimal RFB server for Xvnc): first connect with ticket and nonce, a reload by nonce alone,
an expired ticket, no key at all, and the end of the hold.

### Images

| Image | What |
|---|---|
| `ferry-watch-control` (`Dockerfile` target `control`) | python:3.12-slim, uid 65532, `ferry-web` and `ferry-control` with the SPA; no Playwright, Chromium or selectolax. |
| `ferry-watch-browser` (target `browser`) | Playwright's noble image with KasmVNC, matchbox and tini; Chromium only (Firefox, WebKit and ffmpeg removed); the managed policy (`docker/browser/chromium-policy.json`) in all three directories; uid 1001; `tini -- ferry-watch-agent`. |
| `ferry-watch-vnc-edge` (`vnc-edge/Dockerfile`) | Above. |

CI builds each, smoke-tests it read-only and unprivileged, and pushes it from `main` and tags. The browser
image's smoke test runs the agent with no network and the lockdown self-test against a real Chromium.

### The deployment (`deploy/helmrelease.yaml`, home-cluster's copy)

There is no chart: home-cluster deploys Ferry Watch with app-template, as one HelmRelease
(`kubernetes/apps/ferry-watch/ferry-watch/app/helmrelease.yaml`; design §7.10). This repo keeps a copy.

| Part | What |
|---|---|
| `ferry.*` values | The hosts, the browser image (`tag@digest`), and ferry-control's and ferry-web's configs. Each `config.json`, the policy parameters, the quota and the LimitRange are rendered from them. The render refuses a missing digest or checkout key. |
| `controllers`, `service`, `route`, `networkpolicies` | ferry-web, ferry-control (Recreate: one holds the lock), vnc-edge (two, rolling), the db hook Job (postgres-init, then `ferry-control migrate`), their Services, the two HTTPRoutes, default deny and per-workload policies. |
| `rawResources` | The browser namespace (PSA restricted, kept on uninstall), `ferry-browser`, the headless `browsers` Service, the policy params, the three quotas, the LimitRange, default deny and the two Cilium policies; ferry-control's Role and ClusterRoles; PriorityClass `ferry-browser`; `fw-browser-pod`, `fw-browser-pod-lock` and `fw-ns-objects`. |

`tests/deploy.py` renders the copy through app-template 5.3.0 as helm-controller would (and refuses anything
Flux's substitution would change). CI's `deploy` job runs kubeconform on the render. `tests/apiserver/`
(marker `apiserver`) checks what the render refuses, then starts envtest's etcd and kube-apiserver 1.36 and
applies the render. It checks that:
- the pod `pods.py` builds from the rendered config is admitted;
- 39 different changes to it are refused;
- only ferry-control moves the egress label, and nobody gets a shell;
- no ferry identity holds a verb outside §7.3.

## Where the build departs from the plan's wording

Each is a two-way door.

- **One distribution, not a uv workspace** (§11.1). The same boundaries are packages held by
  import-linter. Playwright and selectolax are a `browser` extra and asyncpg and PyJWT a `control` extra,
  so the control-plane image installs neither browser library.
- **The one-trip watcher is a new module** (`agent/watch.py`) rather than edits to `watcher.py`, which
  serves the single-container app until cutover (§5.2). The disabled login path isn't ported yet.
- **A small Kubernetes client on aiohttp** instead of lightkube (§6.6): ferry-control makes five calls
  (TokenReview, and list, create, delete and label-patch of pods), behind a `Cluster` protocol.
- **Protocol field names**: `configure.trip_spec` and `ack.acked` (the envelope already has `trip` and
  `seq`). `hello` carries no image digest: the pod's eight env vars don't include it, and ferry-control
  reads it from the pod. `cart_claim.fare` is the trip's fare preference. A `paused` drain reason was added.
- **The gate's rate limit** counts only ticket and nonce failures and never refuses a correct reconnect,
  so a burst of bad attempts can't lock the owner out (§5.5).
- **Lockdown self-tests**: the unix-socket check requires Xvnc's socket at mode 0600 and logs the
  others; the page probes run from `about:blank` (§5.6).
- **Fleet planner**: a queued warm trip still gets its short checks, so a full cluster never leaves a
  trip unchecked; a nearer trip waiting only for the old warm pod to drain doesn't start a short check.
- **Schema details**: `events` and `hold_outcomes` carry `dedup_key` (pod:boot:seq); `notifications`
  gains `next_attempt_at`, and `to_email` for invitations (which have no user yet); the two admin
  switches are seeded. ferry-web may insert a `trip_runtime` row and set `next_check_at` (a short check
  now), and delete a trip's alert memory, beyond §6.4's grants.
- **ferry-web loads BC Ferries' public terminal list** for route names in alerts and the trip editor:
  one more egress host for ferry-web (`www.bcferries.com:443`), not in §7.6 yet.
- **The SPA picks its shell at runtime**: `/auth/config` answers on ferry-web and not on the single-container
  app, so one build serves both until cutover.
- **No chart; app-template in home-cluster** (§7.10, owner's call): the guard rails are `rawResources` in
  one HelmRelease, and the planned helm-unittest suites are the render and envtest tests above. Two app
  changes came with it: the browser image may carry a tag before its digest, so Renovate can bump it, and
  ferry-web may read the OIDC client id from a file (`FW_OIDC_CLIENT_ID_FILE`). `ferry-web keys` prints a
  fresh checkout key pair and VAPID key for setting up.
- **vnc-edge has its own Dockerfile** (`vnc-edge/`) rather than a target in one shared Dockerfile (§11.2):
  its build context then holds nothing but the edge's own files. The checkout host and the dashboard origin
  are build arguments.
- **vnc-edge details** (§7.9): KasmVNC's client puts the default port in its socket URL
  (`wss://ferry-checkout.chestr.dev:443/...`), so the shim matches the parsed URL (same host, the socket path)
  rather than the literal pattern. The shim returns a real `WebSocket`, not a subclass, because the client
  checks the object's own prototype. `connect-src` also allows `data:`: the client fetches one to probe the
  browser's video decoders. The landing page opens the viewer after two seconds (or at once on "Open
  checkout"), and keeps the pending ticket in the tab's sessionStorage, out of the address bar.
- **Web Push without pywebpush**: encryption and VAPID are built on `cryptography` (pywebpush pulls in
  `requests`); they match RFC 8291's worked example. Devices may subscribe only with endpoints at the
  big push services, so ferry-web never posts to an address a user supplied.

## Still to do

- **Frontend** (§10): each person's own session list.
- **Deployment and CI follow-ups** (§7, §11): release v0.6.0 and fill in home-cluster's image digests and
  `vncPubkeys` (its README lists the 1Password fields to create first). Then: the PrometheusRule, PodMonitors
  and GrafanaDashboard once there are metrics; kind-lite (the positive pod-lock cases that need a kubelet,
  and the egress flip on a running pod); the `agent-e2e` and `fake-bcferries` images and the compose job.
- **Agent follow-ups**: the smoke trip, the page watchdog (`checkout_host_blocked`), porting the
  disabled login path; `/readyz` reporting the display, Chromium, the loop and lockdown (it answers 200 as
  soon as the probe server is up).
- **ferry-web follow-ups**: `DELETE /api/me` (delete my data), the session list, pre-auth rate limits
  on `/auth/*`, "self-book" (release, then the search link), and the `failing` suppression while the
  breaker is open.
- **ferry-control follow-ups**: the brake and breaker computations (the switches are honoured, but
  nothing sets them yet), node-cordon handling, retention jobs, metrics.
