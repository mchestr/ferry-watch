# Ferry Watch multi-tenant: plan for approval

This is the document to read and approve. Implementation detail lives in
**[design-reference.md](design-reference.md)** next to it; sections are cited as "design-reference.md §N".
Your decisions and the verified cluster facts are logged in [decisions.md](decisions.md), and the evidence
behind both documents is in [research/](research/). Code refs are `file:line` under `src/ferry_watch/` at 1cdfca9
(`site.py` is `bcferries/site.py`). Cluster refs are paths in `mchestr/home-cluster` at 0bff2f8, under
`kubernetes/apps/` unless stated.

**Revised for your round-2 answers** (design-reference.md Appendix D rows 36-47), then corrected after three
independent reviews of that revision (rows 48-60): app-native OIDC sign-in; guest checkout assumed, with no
spike that tests it and only reversible choices; the sandbox and kernel flags proposed as optional hardening,
not a gate (your call: Q12); no pre-cart sign-in window in v1; checkout host `ferry-checkout.chestr.dev`.
Section 9 lists what was decided and what is still open (nothing blocks W0).

---

## 1. What we're building

Today one container runs the dashboard, every trip's watch loop, one shared Chromium with no sandbox
(`site.py:154-157`), your BC Ferries password (`settings.py:40-42`) and a VNC view that is always
open to anyone who can reach `/vnc` (`entrypoint.sh:20-26`: `-DisableBasicAuth -SecurityTypes None
-AlwaysShared`). The new system runs **one disposable headed-browser pod per actively watched trip** in its
own namespace. A small **operator** creates and deletes those pods and relays typed status. Alerts take the
user to a dashboard page that opens a VNC view of **their own** pod, and only while that pod holds a cart.
Access is invite-only: the dashboard signs people in itself with **OpenID Connect against your Authelia**,
behind a replaceable interface. Per-user limits, an admin page, and reaping rules keep the fleet bounded.
Nobody's BC Ferries password is stored anywhere: checkout is as a guest, assumed to work, detected and
switchable to alert-only if it doesn't. The single-container mode is retired.

### Architecture at a glance

```
 Internet -> Cloudflare edge + tunnel (CA/US/CN only) -> envoy-external (Envoy Gateway 1.9.2)
        |                                    |                                   |
 ferry.chestr.dev                    auth.chestr.dev                 ferry-checkout.chestr.dev
 no gateway auth: the app signs      Authelia, the OIDC issuer       no gateway auth: the pod
 people in with OIDC itself;         (already runs here)             checks a ticket;
 Cookie stripped, Authorization              ^                       Cookie + Authorization
 kept (the app's own bearer token)           | ferry-web calls its   stripped
        |                                    | token endpoint + JWKS             |
 +- ns ferry-watch ----------------------------------------------------------------------------+
 |  ferry-web: OIDC sign-in + bearer sessions,                  vnc-edge: stock nginx + stock  |
 |  dashboard, API, admin, VNC ticket minter,                   KasmVNC client; forwards WS    |
 |  Web Push + email. NO Kubernetes rights.                     upgrades only      |           |
 |        | SQL (own role)                                                         |           |
 |  Postgres `ferrywatch` on the shared CNPG postgres17                            |           |
 |        | SQL (own role)                                                         |           |
 |  ferry-control = "THE OPERATOR"                                                 |           |
 |    fleet loop: create/delete pods ----> kube-apiserver (VAP admits one pinned pod shape)    |
 |    hub endpoint: typed status in, typed commands out                            |           |
 +-----------^---------------------------------------------------------------------|-----------+
             | agent dials out (WS, projected token)       /t/<trip>/g/<gen>/ws -> |
 +- ns ferry-watch-browsers (PSA restricted, default-deny, no Secrets, not components/common) -+
 |  pod fw-<trip>-g<gen>: agent (one-trip watcher + VNC gate) | Chromium kiosk | Xvnc on unix  |
 +-------------------------------------------|-------------------------------------------------+
                                             | egress: DNS + toFQDNs allowlist only
                       bcferries.com, Queue-it, payment/3DS hosts  (one home WAN IP for all)
```

### Components

| Piece | What it does | What it can do | What it cannot do |
|---|---|---|---|
| **`ferry-control` (the operator)** | Decides which browser pods exist (fleet loop) and is the only endpoint pods talk to (hub endpoint). Queue, timers, reaper, IP budget. | Create and delete pods of one admitted shape in `ferry-watch-browsers`; TokenReview; read nodes; send `check_now`, `release`, `dismiss`, `drain`, `vnc_revoke`; read and write its own DB tables. | Read any Secret; exec, attach, port-forward, read logs of or proxy to a pod; see page content, screenshots, keystrokes, cookies or the cart session; steer the browser or run JS; mint a VNC ticket; be reached from the internet. |
| `ferry-web` | OIDC sign-in and the app's own sessions, dashboard, user and admin API, per-user live updates, mints VNC tickets, sends alerts from a DB outbox. Holds the OIDC client secret, ticket key and VAPID key. | Read and edit tenants' trips; mint tickets for a cart its owner may open; send push and email; exchange sign-in codes with Authelia (discovery, token endpoint and JWKS only). | Anything in Kubernetes (no token mounted, no RBAC); reach browser pods; join a VNC session the user already opened; see your Authelia cookie (removed at the route); get tokens for any other Authelia client. |
| `vnc-edge` | Serves the unmodified KasmVNC web client and proxies the WebSocket for `/t/<trip>/g/<gen>/ws` to that pod. | Sees VNC traffic in plaintext (as do Envoy and Cloudflare). | Mint tickets; reach ferry-web, ferry-control or Postgres. No app code, no secrets. |
| Browser pod `fw-<trip>-g<gen>` | Today's watcher, run for one trip: search, cart, hold timers, the VNC gate. Chromium in kiosk mode, Xvnc on a unix socket. | Reach bcferries.com, Queue-it, the allowlisted payment/3DS hosts and ferry-control. Hold a guest cart. Admit a viewer with a valid ticket. | Reach other pods, the API server, the LAN, nodes, smtp-relay, logging or the open internet. Use its token anywhere but ferry-control. |
| Postgres | All durable state; two login roles (`ferrywatch_web`, `ferrywatch_control`) with separate grants. | Trips, limits, invitations, session hashes, cart summaries, audit, alert outbox. | Hold any BC Ferries data or credential, or a usable session token (hashes only). |
| GitOps guardrails | VAP, PSA restricted, ResourceQuota, LimitRange, PriorityClass, Cilium policies, all rendered from values. | Bound what a buggy or compromised operator can create. | Be changed by any app identity. |

Three deployments (`ferry-web` and `ferry-control` from one image, plus `vnc-edge`), browser pods, the shared
Postgres. No CRDs. Authelia gains one OIDC client; nothing else changes on the gateway.

### Why the operator cannot get secrets out of browser pods

1. **There is no stored secret to take.** Guest mode: no BC Ferries password exists anywhere (the old login
   code stays in the agent behind a flag no image sets). A pod holds a guest cart cookie and what the user
   types during one hold, and is destroyed after any viewed hold.
2. **RBAC.** `pods` create/delete/get/list/watch in one namespace, `tokenreviews` create, `nodes` read.
   No exec, attach, port-forward, logs, proxy or Secret verb. CI checks the negatives with `kubectl auth can-i`.
3. **Admission.** A ValidatingAdmissionPolicy admits exactly one digest-pinned pod shape: 8 fixed env vars
   (plus an optional list from GitOps params, empty in v1), no command override, no extra containers, no
   secret volumes, no debug knobs such as `DEBUG=pw:*` or `SSLKEYLOGFILE`. A CI matrix tries every leak
   vector and must be denied (design-reference.md §7.5, §11.4).
4. **Protocol.** The agent dials out. Its typed protocol has no navigate, URL, JS, screenshot, cookie or
   storage operation; the pod's termination message is an enum. The operator can revoke VNC, never grant it.
5. **Not in the VNC path.** ferry-web mints tickets and the pod verifies them; checkout traffic goes
   Cloudflare -> Envoy -> vnc-edge -> pod and never touches ferry-control.

What the operator *can* still do if compromised: forge any trip's status, release carts, start pinned pods up
to the quota, and read itineraries and emails. See design-reference.md §4.5 for the full blast-radius table.

---

## 2. How the plan meets each of your requests

| You asked | How the plan meets it | What it costs |
|---|---|---|
| (1) k8s operator, a headed browser per watched route, tenants isolated, no way to get secrets out from the operator pod | One bare pod per actively watched trip, created only by `ferry-control`, pinned by a VAP; the five points above. "Route" = trip (route + date + party), because each has its own search, cart and owner. | ~1-1.5 GiB per watched trip vs 35-80 MB per extra context today. Every pod start is a fresh form walk and possibly a Queue-it wait. The VAP and RBAC must stay correct forever (CI enforces it). |
| (1) browsers do the refresh flows and talk to the operator | The browser pod runs the whole check/cart/hold flow; it dials `ferry-control` with a projected token checked by TokenReview. | A new agent<->operator protocol to version (N and N-1 supported). |
| (1) alerts link to the VNC | Cart alerts link to `https://ferry.chestr.dev/c/<trip>`: one tap on "Check out on this device" mints a ticket and opens VNC. | One extra tap. A bearer VNC link inside a notification would hand VNC to whoever can send or read it. |
| (1) "scale better" | 7 concurrent browsers at the starting 12 GiB budget; 4-23 across your 8-24 GiB range (section 5). | The real ceiling is the one home IP: a fleet-wide 20 checks/min budget stretches intervals beyond 10 pods. |
| (2) admin limits on watched routes per user | `maxTrips` 3 stored, `maxActiveBrowsers` 1 watched at once (your review decision); per-user overrides on the Users page, clamped by GitOps ceilings. | A small admin UI and a limits resolver. |
| (2) reap unused browsers; no using the browser for other things or keeping it forever | No VNC outside a held cart; hold caps (60 min hard); pods recycled after every viewed hold and about every 24 h; 26 h hard deadline; kiosk mode, managed policies and an egress allowlist. | More pod churn, absorbed by the start throttle and budget. |
| (3) VNC only while a cart is held | Enforced five times: in the pod (authoritative), by ferry-web refusing to mint, by the operator's own clock, by Xvnc `-MaxConnectionTime`, by `activeDeadlineSeconds`. | No "watch my browser" view, and no pre-cart sign-in window (your round-2 answer). |
| (4, 6) Ko-fi donation link, no gating; Stripe out | `kofiUrl` Helm value, validated `https://ko-fi.com/<name>`, a plain link in the footer and on "Booked!". CSP unchanged. | None. |
| (5) identity easy to replace; don't depend on Authelia | Sign-in is plain OIDC (round 2, below) behind an `IdentityProvider` interface: issuer, client and claim names are config; user id = `hash(iss, sub)` with GitOps aliases for a migration; no Authelia-only feature is load-bearing. Trusted headers stay only as a LAN/dev adapter, off in production. Swap runbook: design-reference.md §6.2.6. | Google works directly; GitHub, Apple or several providers at once need a broker (Dex, Pocket ID, authentik) or several provider entries. |
| (7) no Pushover | Web Push to the existing PWA by default, email for cart alerts and as fallback, via the in-cluster smtp-relay. Your Alertmanager keeps Pushover for ops. | A service worker, VAPID keys, iOS 16.4+ "Add to Home Screen", SES able to reach invitees. |
| (8) home-cluster is the source of truth | Every cluster fact is verified against the repo (design-reference.md §2.2); all cluster-specific config lives in home-cluster (section 7). | A home-cluster PR per milestone. |
| Invite-only | Three gates: Authelia issues sign-in codes for the `ferry-watch` client only to lldap groups `ferry-watch` and `admin` (an OIDC authorization policy, default deny); the app admits only people you invited on the Users page; suspension revokes every session at once. The app re-checks each session with Authelia every 24 h. | lldap users are created by hand on a LAN-only UI; no remote password reset. Removing someone in lldap takes up to 24 h to reach the app, so you suspend them in the app first (R20). Never reuse an lldap username (R21). |
| Operator only | `entrypoint.sh` and the standalone image retired; docker compose for dev, kind for chart work. | One-time import of your `config.json`/`alerted.json`. |
| Cilium, Envoy Gateway, k8s 1.36 | `toFQDNs` + DNS L7 egress allowlist; two static HTTPRoutes that remove headers, with no SecurityPolicy; VAP, TokenReview pod binding; `hostUsers: false` ready as optional hardening. | `toFQDNs` and the DNS proxy have never been used on this cluster: spike S-CIL first. |
| (review) one browser per user at a time | `maxActiveBrowsers` default 1, admin-configurable per user; enabling a second trip returns "Pause *X* to watch this one". | Users choose which trip is live. |
| (review) route each user to their own pod | One host `ferry-checkout.chestr.dev`, one path per pod, nginx maps the path to that pod's own DNS name, the pod admits only a ticket bound to its owner, trip, pod and cart. | None beyond vnc-edge. |
| (round 2) "We can leave as guest checkout for now" | Guest-only at launch, you included. Carting is on by default; cart alerts say "check out as a guest". | Your own checkouts lose saved cards (`site.py:270-271` logs in today) and any account-only fares. |
| (round 2) "Users can always sign-in on the browser?" | **Probably not during checkout, and we advise against it.** VNC exists only while a guest cart is held, and you've seen BC Ferries refuse a login while a guest cart exists ([top-level README](../../README.md) "Things to know about BC Ferries"; `site.py:288-290`), so a sign-in will most likely fail; whether trying affects the cart is unverified. The page isn't blocked (bcferries.com, including `/login`, is allowlisted), but a password typed there would take the same unencrypted path as card numbers into a Chromium without its sandbox, and the BC Ferries account session it creates outlives the pod. `/c/<trip>`, the checkout page, the cart banner and the invite email say "Check out as a guest. Don't sign in to BC Ferries here." | Users who want their saved cards can't use them. If you'd rather make the advice a rule, one browser-policy line blocks `/login`. Proper sign-in is the deferred attended-sign-in option, which needs the sandbox first. |
| (round 2) "Can we use oidc?" | Yes. ferry-web is an OIDC client of your Authelia (confidential, PKCE), verifies the signed `id_token` itself, then gives the SPA its own bearer token instead of a cookie, re-checked with Authelia every 24 h. So the route drops every Cookie header and your `chestr.dev` Authelia cookie never reaches the app's server, with no gateway Lua and no new domain. | About 3-4 more focused days. New residuals: token in localStorage (R17), the iOS home-screen redirect (R18), unauthenticated requests reach ferry-web (R19), removal at the IdP takes up to 24 h (R20), Authelia's `sub` follows the username (R21). |
| (round 2) "I don't understand this limitation why is it needed" | Explained in plain terms in section 6. It is a risk-acceptance choice, not something the product needs to work, so we recommend optional hardening in the backlog rather than a gate. That's a recommendation: you decide in Q12. | Chromium keeps running `--no-sandbox` unless you opt in (about 4 days plus a per-node Talos change). |
| (round 2) "No" to a pre-cart sign-in window | VNC stays strictly gated to a held cart in v1. The attended-sign-in milestone (M5) is gone and listed as a possible future option that needs your explicit OK. | None now. Nothing designs it out, but adding it later is real work (about 1.5 weeks; section 8, "Deferred"). |
| (round 2) "No spike for now … don't make any one way door decisions" | No spike tests guest checkout and no booking is made only to test. Fixtures come from your real use in M1; production detection and an alert-only switch cover failure; the legacy app stays restorable until a real guest checkout has worked; every guest-related choice is a two-way door (section 3). | Your first real guest checkout on the new stack is the end-to-end proof. A few spikes briefly hold a real cart and release it (section 8). |
| (round 2) checkout host name | `ferry-checkout.chestr.dev` everywhere. A single label, so the `*.chestr.dev` certificate and tunnel rule cover it. | None. |

---

## 3. Key decisions

**Identity: app-native OIDC with a cookie-less session (your round-2 answer)** (design-reference.md §6.2).
- The SPA sends the browser to Authelia (`https://auth.chestr.dev`) with Authorization Code + PKCE S256
  (scopes `openid profile email groups`; the code comes back in the URL fragment, so no access log holds it).
  ferry-web exchanges it with its client secret, verifies the `id_token` (JWKS signature, `iss`, `aud`, `exp`,
  `nonce`, and an allowlisted `alg`), computes the uid from `(iss, sub)` (a stored row can't change it),
  admits only invited users, and returns an opaque 256-bit session token (only its hash stored; 7 days idle,
  30 absolute; revoked on logout, "sign out everywhere", suspension).
- Every 24 h the session is re-checked with Authelia by the same silent redirect, because Authelia applies the
  group policy only when it issues a code: someone you remove in lldap loses access within 24 h (suspend them
  in the app for immediate effect), and a stolen token stops working within 24 h.
- The SPA keeps the token in localStorage and sends `Authorization: Bearer`; the dashboard WebSocket sends it
  as its first message within 5 s, never in a URL, and is closed when that session ends. The app sets no
  cookie, so the route removes the whole Cookie header and has no gateway auth. Bearer headers aren't sent
  automatically, so classic CSRF doesn't apply (the Origin and JSON checks stay). When the session lapses, a
  redirect to Authelia comes straight back with no prompt while the Authelia session lives (consent is
  remembered for a year); after an explicit sign-out the app waits for a "Sign in" tap.
- Not chosen (§6.2.5): ext-auth + trusted headers (the previous choice, about 1 day of code) and Envoy
  Gateway's own OIDC with claims copied into headers. Both are header trust that fails open if the gateway
  policy is detached; app-native OIDC fails closed (no session = 401) and is testable without the gateway.
- Cost: about 3-4 more focused days; R17-R21 (section 6); S-OIDC in W0, S-IOS on your iPhone in M1.

**Guest checkout, assumed to work, every choice reversible (your round-2 answers)** (design-reference.md §5.7).
- Guest carting already works; only the credential check at `watcher.py:459`/`:562` blocks it. Guest
  *checkout* is unproven and, per your answer, gets no spike. Instead: fixtures captured from your own real
  use in M1 (as `tests/fixtures/README.md` describes); production detection (the agent reports a login wall
  or account-required page during checkout, telling a forced wall from a user's own visit, and a cart the
  site expired; "Did you book it?" answers carry a reason; `ferrywatch_holds_total{outcome}` and a per-hold
  outcome record by fare and vehicle type, kept 400 days; a `guest_checkout_failing` admin alert on the first
  forced login wall or "the site wanted me to sign in" answer); hold clocks kept at 30/30/10/60 min and tuned
  from telemetry.
- **If it fails:** "Pause carting" on the Fleet page at once (alert-only), then `autoCart: false` in values, or
  alert-only for just the affected fare or vehicle types. Your own logged-in checkout is still there in the
  legacy app, which stays restorable until a real guest checkout has worked on the new stack (section 7).
  Then decide on an account mode: with stored passwords ruled out, the candidate is an attended sign-in
  before a cart, which needs your explicit OK (you said no for v1).
- **Two-way doors:** `SiteSession.login()` and `Credentials` stay in the agent behind a disabled flag, not
  deleted; a per-trip `checkout_mode` (default `guest`) is reserved in the data model and protocol; the
  fleet-wide `autoCart` kill switch stays, with alert-only per fare or vehicle type; the VAP's env allowlist
  can admit a future login-mode variable by a params change; the legacy app, its data and your BC Ferries
  password stay until guest checkout has worked; the VNC ticket names its purpose, so a later sign-in window
  is an addition. Nothing in the DB, protocol or pod shape assumes guest forever.
- **Releasing a cart:** `empty_cart` checks the landing URL now and the empty cart once the M1 fixtures exist;
  until then a release is best effort (BC Ferries' cart timer is the backstop) and counted in metrics.
- Rejected: per-user credentials in Secrets or 1Password (plaintext within the operator's reach). Not now
  (you said no): a pre-cart sign-in window.

**One pod per actively watched trip, and its memory cost** (design-reference.md §8.4).
- Measured ~0.52-0.57 GB of process memory on light pages; with real pages and the in-memory `/tmp` and
  `/dev/shm` counted against the limit, we start at `podMemory` 1.5 GiB and lower it after S-MEM.
- Alternative: one shared Chromium with a context per tenant (35-80 MB each), which is cheap but is exactly the
  isolation failure you want gone. Sharing one browser per route across users is also rejected: one tenant's
  page would drive another's alerts.

**VNC gating: in-pod ticket check (recommended) vs gateway auth on the checkout host**
(design-reference.md §3.3, §5.5, §7.9).
- ferry-web mints a 60 s single-use Ed25519 ticket bound to owner, trip, pod and cart; the pod checks it,
  then locks the hold to that device's random nonce. The ticket travels in the URL fragment and then, with
  the nonce, in a WebSocket subprotocol header, so neither lands in access logs. Xvnc listens only on a unix
  socket, so page JS cannot open it.
- Alternative: Authelia on `ferry-checkout.chestr.dev`. Envoy only checks the upgrade, cross-node traffic is
  SNATed so a header is unprovable at the pod, and iOS home-screen apps keep a separate cookie jar. An
  IdP-signed principal the pod verifies itself is the M4 upgrade (S-VNCID).

**Storage: Postgres on the shared CNPG `postgres17` (recommended) vs SQLite on a PVC**
(design-reference.md §6.4).
- Quota checks need transactions; audit and usage must be queryable and backed up; it is your cluster
  convention. Two login roles via CNPG managed roles, row-level security on the outbox, immutable trip owner.
- Cost: `components/cnpg` puts the shared superuser Secret in `ferry-watch` (R14, Q9). Cluster PITR rolls back
  Authelia, lldap and Immich too, so the per-app restore path is the 4-hourly pg_dump.

**No CRDs (recommended) vs a Trip CRD with a controller** (design-reference.md §7.1, D2).
- Desired state is user data that needs per-user scoping and transactional limits; pods are derived and
  disposable. A CRD would mean giving the internet-facing ferry-web Kubernetes write rights or adding a sync
  path. The admin Fleet page replaces `kubectl get trips`.

**Python + lightkube (recommended) vs Go controller-runtime or kopf** (design-reference.md §6.6, D4).
- One language shares the models, protocol schema and limits resolver between web, operator and agent, so
  security checks cannot drift between two implementations. A level-triggered loop (pod/node watch, 10 s list,
  Postgres LISTEN) tested with a `FakeCluster` and envtest.

**Alerts: Web Push + email from an outbox (recommended) vs Pushover or SMS** (design-reference.md §6.7).
- The `Notifier` seam (`notify.py:40-44`) gains a recipient; an outbox in Postgres survives restarts. Only
  ferry-web sends, from its own templates; pods emit typed alert kinds and can't inject text or links.
- SMS is a later add-on. Risk: iOS push lapses or Focus silences it, so every cart alert also goes by email.

---

## 4. A user's journey

Detail: design-reference.md §3.5-3.6 (data flows), §6.2 (sign-in) and §8.1 (state machine).

1. **Invite.** You create them in lldap (group `ferry-watch`), then "Add person" on the Users page. The
   `invited` email covers Authelia (consent once, a passkey or TOTP, "Remember me"; the first sign-in is
   easiest in Safari or on a computer), the home-screen PWA, notifications, a test alert and checking out as a
   guest without signing in to BC Ferries.
2. **Sign in.** Authelia issues a code only to groups `ferry-watch` and `admin`; back on `/auth/callback`,
   ferry-web verifies the `id_token`, binds the invitation and hands the app its session token. Later
   sign-ins, and the daily re-check, are a quick redirect with no prompt while their Authelia session
   lives.
3. **Add a trip.** The form checks `maxTrips` and `maxActiveBrowsers` in one transaction. A trip more than
   30 days out shows "Starts watching Jul 2"; a second live trip gets "Pause *X* to watch this one".
4. **Pod starts.** The operator creates `fw-<trip>-g1` (or queues it: "Waiting for a free browser (#2)").
   The pod runs its lockdown self-tests, dials the operator, walks the booking form (maybe through Queue-it)
   and starts checking every 30 s or more.
5. **Sailing opens.** The agent carts it as a guest if the user has holds left, it is outside quiet hours,
   carting isn't paused and the operator acked within 30 s; otherwise it sends a "bookable" alert with a
   bcferries.com link.
6. **Alert.** Web Push plus email: "Held for you: 7:00 am Tsawwassen -> Swartz Bay, Fri Aug 1. Check out as a
   guest by 3:42 pm." The link is `/c/<trip>`. Reminder at 15 min; released at 30 min if nobody opens it, and
   the pod keeps watching.
7. **Choose a device.** (A lapsed app session redirects through Authelia and back.) `/c/<trip>` offers *Check
   out on this device*, *I'll use my computer* (emails a fresh link) or *Release and book it myself*, and says
   "Check out as a guest. Don't sign in to BC Ferries here: it doesn't accept a sign-in while a cart is
   held."
8. **Checkout over VNC.** The ticket opens `ferry-checkout.chestr.dev/t/<trip>/g/<gen>/`; the pod locks the
   hold to that device. The user pays as a guest on the real bcferries.com page. Phone sleep or a switch to the
   bank app reconnects with the same device nonce. Session and viewer-gone clocks pause while on a payment/3DS
   page; the 60 min hard cap never pauses. If BC Ferries sends the checkout to a login wall, the agent
   reports it as forced (a user's own visit to the login page is reported too, but alerts nobody).
9. **Booked.** When the agent sees BC Ferries' confirmation page (detector built from the page you capture in
   M1), the pod exits, the trip is archived and the user sees "Booked!" (with the Ko-fi link). Otherwise they
   get "Did you book it?" with optional reasons ("the site wanted me to sign in", "I tried to sign in", …);
   no answer in 20 min resumes watching with a fresh pod, and the viewed sailing is never re-carted.

---

## 5. Admin controls, quotas and reaping

### Defaults you would tune (full table: design-reference.md §8.2)

Effective value = `min(per-user override or default, GitOps ceiling)`. Defaults and ceilings are Helm values in
home-cluster; overrides are set on the Users page with a mandatory reason.

| Setting | Default | Ceiling | Notes |
|---|---|---|---|
| `maxTrips` (stored trips per user) | 3 | 10 | replaces `MAX_TRIPS = 6` (`models.py:16`) |
| `maxActiveBrowsers` (watched at once) | 1 | 3 | holds at once = this |
| `startWindowDays` (closer than this -> gets a pod) | 30 | 120 | |
| `tripHorizonDays` (latest date allowed) | 120 | 180 | |
| check interval | 30 s | floor 30 s (compiled) | stretched by the fleet budget |
| `maxHoldsPerDay` / `maxCartAttemptsPerDay` | 4 / 6 | 10 / 12 | Pacific day |
| `vncMinutesPerDay` | 60 | 120 | checked before carting, never cuts an open session |
| quiet hours (alert only, no carting) | 23:00-07:00 on | user may turn off | |
| hold: unclaimed / checkout session / viewer gone / hard cap | 30 / 30 / 10 / 60 min | 45 / 45 / 15 / 90 (compiled) | tuned from production telemetry (checkout duration, site cart TTL) |
| app session (sign-in) | 7 d idle / 30 d absolute; re-checked with Authelia every 24 h | re-check 1-24 h | then a silent redirect signs in again while the Authelia session lives |
| `checksPerMinute` (whole fleet, one home IP) | 20 | 40 | 30 s each up to 10 pods, 60 s at 20 |
| `browserMemoryBudget` / `podMemory` | 12 GiB / 1.5 GiB | LimitRange 2 GiB | `podMemory` drops to measured p95 x 1.2 after S-MEM |
| `maxBrowsersTotal` | 7 | derived: `floor(budget/podMemory) - 1` | |

Capacity at one browser per user (design-reference.md §8.4): 8 GiB -> 4-7 users, 12 GiB -> 7-11, 16 GiB -> 9-15,
24 GiB -> 15-23, depending on whether pods measure 1.5 or 1.0 GiB.

### What the admin can do (v1; design-reference.md §9)

- **Fleet page:** memory vs budget, browsers by phase, queue, checks/min, brake and breaker, live holds with
  countdowns, hold outcomes by week and by fare and vehicle type (forced login walls and "Did you book it?"
  reasons included), an attention list (including `guest_checkout_failing`). Actions: maintenance on/off,
  **pause carting** (before Talos upgrades; also the immediate alert-only switch), close the breaker, release
  a cart, recycle a pod.
- **Users page:** state, open invitations, effective limits with their source, usage today, push/email health,
  active sessions, unmatched sign-ins (last 30 days) and sign-ins whose email changed. Actions: add person
  (creates the invitation, sends the invite), suspend (revokes sessions, closes sockets, drains pods at once),
  revoke sessions, set overrides, make admin, delete user.
- **Removing someone:** suspend on the Users page first, then change lldap; delete the user here before
  deleting or renaming them in lldap, and never reuse a username (design-reference.md §12.5).
- Every mutation needs a reason and writes an INSERT-only audit row plus a log line.
- **Admins never get VNC** into a tenant's browser. Break-glass stays with you as cluster admin, out of band.
- Deferred to M4: Trips, Settings and Audit pages. Until then, defaults change by values PR and audit is
  read with `psql` or VictoriaLogs.

### What ends a browser (reaping)

- After **any viewed hold** the pod exits; watching resumes in a new pod, so nothing a viewer did persists
  (including a BC Ferries sign-in).
- After about 24 h (recycled only while idle); hard `activeDeadlineSeconds` of 26 h regardless.
- Trip disabled, paused, past or deleted, user suspended, or limit lowered: drained at once (a held cart keeps
  `min(old deadline, now + new cap)`).
- Startup with no first check in 15 min; Watching pod silent 5 min; 6 consecutive failures; repeated failures
  back off (5-60 min) and then "Needs attention". Orphans swept every 60 s.
- Removed from the draft: a 14-day inactivity pause. It stopped exactly the users who add a trip and wait,
  and a watching pod without VNC cannot be used for anything else.

"Can't use it for other things" comes from: no VNC outside a hold, kiosk Chromium with managed policies
(no other sites, `file://`, `chrome://`, downloads, extensions), the DNS/FQDN egress allowlist, and the daily
hold and VNC-minute caps.

---

## 6. Security

### What is protected, from whom

Assets, highest first: card details typed during checkout; a live cart; the reputation of your home IP with
BC Ferries and Queue-it; your cluster, LAN, shared Postgres and Authelia session; alert integrity (phishing);
tenants' itineraries (they show when someone is away).

| Attacker | Stopped from | By |
|---|---|---|
| Anyone on the internet without an invitation | Any data or action | Authelia's client policy (groups `ferry-watch`, `admin`); the app's invitations; 401 on every `/api/*` without a valid session; the dashboard WebSocket must authenticate within 5 s; sign-in attempts limited per client |
| Someone who takes over ferry-web (internet-facing) | Creating or altering pods, reading Secrets, reaching pods, joining a checkout the user already opened, replaying your Authelia session from the server | No Kubernetes token; no route to pods; the pod's first-viewer lock; your Authelia cookie is removed at the route and its client secret is for `ferry-watch` only. **Not stopped:** script it serves is same-site with your other `chestr.dev` apps, so your browser attaches your Authelia cookie (`SameSite=Lax`) to blind requests that script makes to them (R12). Every other app on `chestr.dev` has the same exposure; a separate domain would close it. |
| Someone who takes over vnc-edge (internet-facing) | Minting tickets; reaching anything but a pod's `/ws` | No secrets, no app code. It *does* see and can inject into sessions passing through it, like Envoy and Cloudflare (residual below) |
| A malicious invited tenant | Seeing other tenants' trips or VNC; exceeding limits; keeping a browser | User-scoped queries (foreign id = 404); tickets bound to owner and cart; ceilings; hold caps |
| Code running in one browser pod (bad page, Chromium exploit) | Reaching other pods, the API server, LAN, nodes, smtp-relay, logs, the open internet; acting on another trip | Default-deny Cilium policy with FQDN allowlist; no API token; TokenReview binding of each socket to one trip |
| That code escalating into the operator | Changing the pod shape, reading Secrets, exec, getting page content, minting VNC | VAP, RBAC, no ticket key in the operator |

Who sees card data typed over VNC: the user's own pod, vnc-edge, Envoy, Cloudflare's edge and cloudflared
(VNC is plaintext once TLS ends). **Not** the operator, ferry-web, Postgres or logs. The browser namespace is
excluded from Fluent Bit and denied egress to observability.

### Why the sandbox and kernel question came up (your round-2 question 3)

- **The sandbox, and why these pods lack it.** Chromium normally runs each web page in a sandboxed
  "renderer" process, so a bug that lets a page run code is trapped in a process that can do almost nothing.
  In containers that sandbox needs Linux user namespaces, which these pods don't get by default (Talos sets
  `user.max_user_namespaces=0`; containerd's default seccomp profile blocks the calls). So Playwright starts
  Chromium with `--no-sandbox` (`site.py:154-157`), as in today's container.
- **What that means.** A malicious script on bcferries.com, Queue-it or a payment/3DS page that exploits a
  Chromium bug runs code directly in that pod, as the same user as the agent. It can land at any time in the
  pod's life (a pod reads BC Ferries pages for up to a day before any hold) and wait for the checkout, where
  it can read the card as it is typed. Per-pod isolation (destroyed after any viewed hold), no stored
  secrets, no API token and the egress allowlist keep it to that one tenant's pod, **as long as two other
  layers hold**: ferry-control's message parser (break that, and it can read every tenant's trips and start
  or stop pods for anyone) and the Linux kernel. Nothing like AppArmor confines the container, and all three
  of your nodes are control-plane nodes that also run workloads (`talos/controlplane.yaml:191`), so a kernel
  escape from a browser pod is the whole home cluster: etcd, the API server, the 1Password store, Authelia.
  Enabling user namespaces on Talos (a sysctl plus a seccomp profile, applied per node) would put the sandbox
  back, so an exploit would need a second bug to get out of the renderer.
- **The kernel flags.** Separately, the nodes boot with `mitigations=off` (the kernel's defences against
  Spectre-style CPU side channels are off for speed), `apparmor=0` and `security=none`
  (`talos/schematic.yaml:6-12`). Page JavaScript alone is limited by Chromium's site isolation, but once an
  exploit gives native code in an unsandboxed pod, a side channel can target kernel memory and every
  workload on that node, not just other tenants' browsers.
- **Recommendation (yours to decide: Q12).** Neither is needed for the product to work; both are
  risk-acceptance choices. We recommend treating the sandbox and the flags as **optional hardening (backlog,
  about 4 days plus a per-node Talos change), done before inviting people you don't know well**, not a gate
  unless you make it one. Accepting that means accepting a Chromium exploit as a risk to one tenant's
  checkout, and, chained with a kernel or parser bug, to your whole cluster. The VAP params that can require
  the sandbox stay in place.

### Residual risks, plainly

- **You and anything with cluster-admin see everything**, including a live checkout: `openclaw` and
  `home-cluster-runner`. The runner also has Talos `os:admin` and runs a `pull_request` workflow
  (`actions-runner-system/actions-runner-controller/runners/home-cluster/rbac.yaml`); restricting it is a hard
  gate before non-household invitees (Q10). Tenants are told.
- **Chromium runs without its sandbox on `mitigations=off` nodes** unless you opt into the hardening above
  (R2). Recommended to accept for now (Q12): an exploit reaches one tenant's checkout, and your whole cluster
  only if chained with a kernel or parser bug.
- **Checkout keystrokes cross Cloudflare, cloudflared, Envoy and vnc-edge in plaintext**, which can also inject
  input; so would a BC Ferries password, if a user signed in during checkout despite the advice. Whether
  relaying a cardholder's own keystrokes puts you in PCI scope is a grey area. Tenants are told.
- **A compromised ferry-web could win the race to a hold's first VNC connection** (visible to the user, before
  any card entry; it cannot take over afterwards). Accept for friends and family (Q1) or wait for M4.
- **Guest checkout is assumed, not proven.** If BC Ferries demands an account, early holds may end at a login
  wall; production detection alerts you on the first one, "Pause carting" turns the fleet alert-only at once,
  and your logged-in legacy app stays restorable until a guest checkout has worked.
- **The app session token is readable by script on the dashboard origin** (R17): XSS could copy it from
  localStorage and use it elsewhere until revocation or the next daily re-check (at most 24 h; a thief can't
  pass the re-check). The strict hash-based CSP (`server.py:232-251`) makes injection hard and blocks
  fetch and form exfiltration, but not a script that leaks the token by navigating away. VNC still needs the
  pod's ticket and first-viewer lock.
- **iOS home-screen sign-in may land in Safari, or be lost when iOS backgrounds the app** (R18): during
  first sign-in the user switches to Mail for Authelia's code. S-IOS checks both on your iPhone in M1; the
  fallbacks are a pending sign-in kept for 10 minutes, first sign-in in Safari, or a one-time handoff code
  (~1 day).
- **Unauthenticated requests now reach ferry-web** (R19), but only the static shell, `/auth/*` (bodies capped,
  rate-limited per client) and 401s; unauthenticated sockets are capped per client and closed after 5 s.
- **Removing someone at the IdP takes up to 24 h to reach the app** (R20): Authelia checks the group only when
  the app sends the browser back to it. Suspend in the app first (instant), then change lldap.
- **Authelia's `sub` follows the username** (R21): a new person given an old username would inherit the old
  account. Never reuse an lldap username; delete the user in Ferry Watch before deleting them in lldap.
- **One home IP.** A Queue-it or BC Ferries block hits every tenant and your household. The budget, brake and
  breaker reduce this; code running in a pod can bypass them toward the allowlisted hosts until a per-pod
  rate-limiting proxy (M4).
- **Holds can be lost** on node crash, OOM or eviction, and in v1 a Talos upgrade releases holds on the drained
  node (with an alert). Runbook: pause carting before merging a Talos bump.
- **A bank's 3DS host missing from the allowlist** breaks checkout at the last step; the pod detects it and
  offers "Release and book it myself". The list is seeded with BC Ferries' processor (Moneris), completed from
  your M1 checkouts and widened by S-3DS (Q8).
- **The shared Postgres superuser Secret sits in `ferry-watch`** (standard `components/cnpg`). No app identity can
  read it; removing it needs a component variant (Q9).
- Also accepted: BC Ferries' own third-party scripts; a policy gap the tests miss; a plaintext in-cluster agent
  channel (TLS in M4); a compromised control process can delete trips or release carts (audited, not prevented).
- **Gone in round 2:** "trusted headers fail open if the SecurityPolicy is detached" (R15; there is no gateway
  policy to detach) and its forged-header canary; the Authelia cookie reaching the app's server (R12; the app
  sets no cookie; script on the ferry host can still ride it, see the table above). Full list (R1-R21) in
  design-reference.md §4.6.

---

## 7. Changes needed in home-cluster

The app chart (published from this repo) creates the browser namespace, VAPs, PriorityClass, quotas, network
policies (including ferry-web's egress to `auth.chestr.dev`) and both HTTPRoutes. home-cluster owns the rest
(design-reference.md §2.3, §6.2.3, §7.10).

**W0 (spikes)**
- [ ] Cilium values: `dnsProxy.dnsRejectResponseCode: nameError` (the default REFUSED stops glibc's search list,
      so short service names never resolve; no DNS rules exist today, so nothing regresses).
- [ ] The Authelia OIDC client below, registered early for S-OIDC with only the `ferry-next.chestr.dev`
      redirect URI.
- No Talos change: the sandbox is optional hardening (below).

**M1: you on the new stack**
- [ ] `kubernetes/apps/ferry-watch/`: namespace with `components/common`; `ks.yaml` with `components/cnpg`
      (`APP=ferrywatch`), `dependsOn: cloudnative-pg-cluster`; OCIRepository + HelmRelease
      (`upgrade.strategy: RetryOnFailure`); values for hosts, FQDN allowlist, `identity.oidc` (issuer
      `https://auth.chestr.dev`, client id, scopes, claim names, invite-match claim), `bootstrapAdmins`
      (issuer + your username), ceilings, memory budget, `kofiUrl`, `autoCart` and the alert-only fare/vehicle
      lists, ticket public keys.
- [ ] Authelia (`default/authelia/app/resources/configuration.yaml`, `identity_providers.oidc`; YAML in
      design-reference.md §6.2.3): `authorization_policies.ferry-watch` (default deny; `two_factor` for
      `group:ferry-watch`, `group:admin`) and a client `Ferry Watch` in the cluster's usual pattern
      (confidential, PKCE S256, pre-configured consent 1y, `claims_policy: default`), scopes `openid profile
      email groups`, `response_modes: [fragment]`, redirect URIs `https://ferry.chestr.dev/auth/callback` and,
      until cutover, `https://ferry-next.chestr.dev/auth/callback`.
- [ ] Client id, secret and hash from the `default/authelia/app/README.md` script (`APP=FERRY_WATCH`) in the
      1Password item `authelia`; Authelia's ExternalSecret gains the id and the hash, like every client.
- [ ] 1Password item `ferry-watch`: VNC ticket Ed25519 private key, VAPID private key. Item `cloudnative-pg`:
      `ferrywatch_postgres_{username,password}`, `ferrywatch_web_password`, `ferrywatch_control_password`.
      ferry-watch's ExternalSecret reads those plus the client id and secret from `authelia` (as
      `observability/grafana/instance/externalsecret.yaml` does).
- [ ] CNPG managed roles `ferrywatch_web`, `ferrywatch_control` in
      `database/cloudnative-pg/cluster/cluster17.yaml`, with password ExternalSecrets in `database`.
- [ ] smtp-relay NetworkPolicy: admit `ferry-watch/ferry-web` on 2525.
- [ ] Silences for `OomKilled`, `KubePodNotReady`, `KubePodCrashLooping`, `KubeQuota*` in `ferry-watch-browsers`;
      Fluent Bit `Exclude_Path` for `*_ferry-watch-browsers_*`; descheduler namespace exclusion.
- [ ] Cloudflare rule: Rocket Loader and email obfuscation off for the ferry hosts. GrafanaDashboard; Renovate
      always opens a PR for `/ferry-watch/`.
- [ ] Cutover PR (after your first sign-in on `ferry-next`): legacy `default/ferry-watch` to `replicas: 0`, its
      route off, its PVC `prune: disabled`; new `routes.dashboardHost: ferry.chestr.dev`. Never two routes for
      one host (UniFi DNS conflict). The prepared revert PR pins the legacy image by digest (today it is
      tag-only, `0.5.1`).
- [ ] Only once **all** hold: 14 days since cutover, at least one real guest checkout completed on the new
      stack, and no forced login wall since: delete `default/ferry-watch`; remove only its reference to the
      shared `pushover` item (used by kube-prometheus-stack, gatus, sonarr, radarr, qbittorrent); delete
      `BCF_USER`/`BCF_PASS`; remove the `ferry-next` redirect URI from the Authelia client. Until then the
      legacy app stays at zero replicas with its 1 Gi PVC and the two password fields: your logged-in
      fallback, which can also run beside the new stack on an envoy-internal host if guest checkout fails.
- Not needed any more: an ext-auth SecurityPolicy, a ReferenceGrant change in `default/authelia`, an Authelia
  `access_control` rule for the ferry hosts, a gatus forged-header canary.

**M2: before anyone new is added to lldap**
- [ ] Authelia OIDC `authorization_policies` restricting Immich, Windmill, Grafana, Autobrr and Paperless to their
      groups (today any lldap user passes `two_factor` on every client).
- [ ] lldap group `ferry-watch`.
- [ ] Each invitee's email deliverable through SES (production access, or verify each address).
- [ ] Optional: soften Authelia `ban_time` from 1 day to 1 h (Q6, cluster-wide).

**M3: before non-household invitees**
- [ ] `home-cluster-runner`: push/schedule or same-repo PRs only, approval for outside contributors, matrix
      values via `env:` (Q10).
- [ ] SES production access.
- [ ] If Q9 = yes: a CNPG `Database` + managed owner role, and a `components/cnpg` variant without the initdb
      superuser Secret.
- [ ] Q12 decided; if you make the sandbox a gate, the optional items below move here.

**Optional, your call (backlog; Q12):**
- [ ] Chromium sandbox: Talos `user.max_user_namespaces` sysctl + a `machine.seccompProfiles` Localhost profile
      on every node (`task talos:apply-node`), after S-SBX on one node; decide on `mitigations=off
      apparmor=0`. About 4 days in all. Recommended before inviting people you don't know well.
- [ ] `policy.drainTimeout`/`timeout` on the TalosUpgrade if holds should survive upgrades (Q5).

---

## 8. Delivery plan

### Spikes: what each one decides (experiments and fallbacks: design-reference.md §12.1)

No spike tests guest checkout and no booking is made only to test (your round-2 answer). Most spikes below run
locally or on the cluster against test pages. S-MEM reads real BC Ferries pages the way watching does; S-DRAIN
and the M1 VNC exit check briefly hold one real cart on a far-out sailing with space and release it, as the
product itself does; S-3DS walks a card to the 3DS step only if you agree (Q8).

| Spike | When | Decides |
|---|---|---|
| S-KIOSK (local) | W0 | Kiosk lockdown, Xvnc on a unix socket, the gate, the stock client through the shim, SIGTERM drain. Fallback: loopback Xvnc behind a per-boot password. |
| S-VNC-lite (cluster, test page only) | W0 | Phone reconnects through Cloudflare; whether iOS home-screen apps keep the device nonce (else Q1b). |
| S-CIL (cluster) | W0 | `toFQDNs` + DNS rule vs `toCIDRSet` fallback; SNAT entity rules; whether an egress proxy moves to M3. |
| S-OIDC (cluster; replaces S-AUTH) | W0 | Authelia 4.39 accepts code + PKCE with the code in the fragment (else query mode, with the single-use code in the access log as a stated exception); outsiders are denied, and removing someone from the group makes their next daily re-check fail; a remembered consent makes the second sign-in silent; ferry-web reaches the token endpoint and JWKS; an echo shows no Cookie (several headers, HTTP/2) and `Authorization` intact. |
| S-TR (cluster) | W0 | TokenReview pod binding and VAP CONNECT matching on 1.36.5. |
| S-MEM (cluster) | W0 (watching), M1 (checkout, from your real holds) | `podMemory`, `cpuRequest`, `maxBrowsersTotal`, cold-start time. |
| S-DB | W0 | Managed roles, RLS and LISTEN/NOTIFY on `postgres17`; else one login role. |
| Fixture capture (not a spike, not a gate) | M1, your real use | Cart and review, confirmation, after-release and empty cart, expired cart, any login wall, beyond-schedule and block pages; the classifiers, booking detector and full empty-cart check follow. Unseen pages stay on the safe path. |
| S-DRAIN | M1 | A real Talos drain with a brief real hold on a far-out sailing: the hold is released in order and the reboot proceeds. |
| S-IOS (your iPhone) | M1 | Sign-in, re-sign-in and the daily re-check stay inside the home-screen app, including a 1-3 min switch to Mail or an authenticator mid-sign-in; else the matching fallback (up to +1 day). |
| S-PUSH | M2 | iOS push and re-sign-in from a notification; SES reaches an invitee. |
| S-3DS | M3 | 3DS hosts from 3+ issuers, from the household's real checkouts; walks to the 3DS step that stop there (card entered, maybe a bank prompt, no booking) only if you agree (Q8). |
| S-SBX (one node, about 1 day) | optional backlog (Q12) | Whether Chromium's sandbox runs on Talos with the sysctl + seccomp profile. Not a gate unless you make it one. |

Also S-VNCID (M4, closes R3) and S-IDP (at an IdP change). Removed in round 2: S1, S2, S2b, S5, S6, S7 and the
live part of S-FIX (replaced by fixture capture and production detection), S3/S4 (M5's), S-AUTH (now S-OIDC).

### Milestones (one developer, focused days; arithmetic in design-reference.md §12.3)

| Milestone | Size | Delivers | Exit criteria |
|---|---|---|---|
| **W0 De-risk** | 6.5-8 days | W0 spikes above; Cilium `nameError`; frontend tolerates unknown enum values; an opening-visibility metric on today's deployment. | Every W0 spike passed or has a chosen fallback. No answers needed from you. |
| **M1 You on the new stack** | 63-77 days | Workspace split; one-trip agent (login code kept, disabled, its tests still running), supervisor, lockdown, gate; guest-checkout production detection (forced vs voluntary login, hold-outcome record), `checkout_mode`, alert-only by fare/vehicle type; fixture capture and S-MEM's checkout numbers from your real holds; ferry-control; ferry-web with OIDC sign-in, sessions re-checked every 24 h, session-bound sockets, invitations, scoped API, tickets, `/c`, email outbox; vnc-edge; Postgres schema; chart with the pod VAP and quotas; CI (VAP leak matrix, `can-i`, compose with a fake issuer, kind-lite); home-cluster PR; cutover. | 0 carts lost across web/control restarts and a drain during holds (compose); post-deploy smoke green (including the Cookie echo and an unauthenticated 401); S-DRAIN done; S-IOS passed or its fallback shipped; at least one real hold opened over VNC on your iPhone (your own trip, or a brief hold on a far-out sailing released without booking) with a control restart mid-hold; 7 days on the new stack only. Your first real guest checkout is the end-to-end proof, with the alert-only switch ready and the legacy app still restorable. |
| **M2 Household invitees** | 14-18 days | Web Push + service worker + onboarding; admin Fleet and Users pages with invitations and session revocation; delete-my-data; persisted brake and breaker; cross-tenant suite; Authelia policies for the other clients and the lldap group; tenant disclosure; restore rehearsal. | Cross-tenant suite green (including a revoked session token refused); quota race test passes; up to 3 household invitees for 14 days, no Pending pods, admin time < 10 min/day; Q1 answered. The legacy app is deleted only once a real guest checkout has worked (section 7), not on a date. |
| **M3 Wider invitees** | 6.5-10 days | S-3DS allowlist (Q8); the two second-fence VAPs; runner restricted (Q10); SES production; Q9 if chosen. | Everything in the cut line below is green; Q12 decided; 3-5 non-household invitees for 14 days. |
| M4 Hardening backlog | per item | See "Deferred", including the optional sandbox and kernel hardening (about 4 days, S-SBX included). | Each item ships alone. |

**M3 stays its own milestone** rather than merging into M2: it lost the sandbox implementation (about 3 days),
but S-3DS needs the household's real checkouts from M2, and the runner restriction and SES production access
matter for strangers, not the household; merging would hold household invitees back. **M5 (attended sign-in) is
removed** (your "No" to a pre-cart window for v1); it is a possible future option below, about 1.5 weeks if you
ever want it.

Totals: **90-113 focused days (18-22.5 weeks)** to non-household invitees; you are on the new stack after W0 +
M1, **69.5-85 days (14-17 weeks)**; about **21-26 calendar weeks** to the end of M3 with soaks. Versus 91-113
before: W0 lost the live spikes (3-4 days) and S-SBX (1); M1 gained OIDC (+3-4), detection plus fixtures (+2-3)
and the review's fixes (+1); M3 lost the sandbox implementation (3). A failed S-IOS adds up to 1 day; the
optional sandbox (about 4 days) is outside the total. Cheapest cuts that weaken no guarantee: email-only
household alerts (Web Push to M3, -3 days); household limits by values PR instead of the Users page (-3 days).

### Minimum secure v1 (all required before the first non-household invitee; design-reference.md §12.4)

- Guest-only, no BC Ferries credential anywhere (the login code present but disabled); guest checkout assumed,
  with production detection and the alert-only switch.
- One bare pod per actively watched trip with `restartPolicy: Never`, `activeDeadlineSeconds` and the agent's
  compiled maxima.
- Only ferry-control creates pods, through the pinned-shape VAP; PSA restricted; quotas including "every pod
  has a deadline"; no API token in pods; ferry-web without RBAC; RBAC negatives checked in CI and live; the
  pod-lock and namespace-objects VAPs on.
- Typed agent protocol with no page operations; TokenReview binding with the pod-name check; enum-only
  termination message.
- In-pod gate: 60 s single-use ticket bound to owner/trip/pod/cart, atomic first-viewer lock, single viewer,
  Origin check, ping liveness, revocation at hold end; hold caps enforced in all five places (agent, web
  refusing to mint, the operator's clock, Xvnc `-MaxConnectionTime`, `activeDeadlineSeconds`).
- Xvnc on a unix socket only; fail-closed lockdown self-tests; kiosk + managed policies. (The Chromium sandbox
  is optional hardening, not part of this line.)
- Static vnc-edge with the stock client and a strict CSP; pods never serve HTML; every Cookie header stripped
  on both routes, and Authorization on the checkout route.
- Egress allowlist + default deny verified by the post-deploy smoke; `nameError`; the S-3DS allowlist and the
  blocked-host detector; browser namespace outside `components/common`, excluded from Fluent Bit and the
  descheduler.
- Identity: app-native OIDC with `id_token` verification (`alg` allowlist) and the cookie-less bearer session
  re-checked with Authelia every 24 h, sockets bound to their session, codes and tokens kept out of URLs,
  per-client sign-in limits; the app's invitations; Authelia authorization policies on every client; invitee
  email deliverable; the "Removing a person" runbook.
- User-scoped repository (404 for foreign ids); cross-tenant suite green; limits (`maxTrips`,
  `maxActiveBrowsers = 1`, holds/day, VNC minutes/day, 30 s floor); Fleet and Users pages; Web Push + email;
  persisted brake with the 30 s agent floor.
- Runner restricted; SES production; Q1 and Q12 decided; tenants told about cluster-admin visibility and the
  plaintext keystroke path (naming Cloudflare).

### Deferred (M4 and later; none is needed for the guarantees you asked for; design-reference.md §12.2, §15)

Optional Chromium sandbox and kernel-flag hardening (your call, Q12; about 4 days); device handoff by QR code;
IdP-signed principal verified in the pod (S-VNCID, closes R3); hold PDBs + Talos `drainTimeout`; breaker
canaries and smarter fairness (AIMD leases, a cold Standby tier, near-sailing priority); TLS on the agent
channel; a per-pod rate-limiting egress proxy; cosign/SBOM with admission verification; admin
Trips/Settings/Audit pages and a DB policy table; a request-access queue (with a social IdP); the
`identityAliases` CLI; a warmer DaemonSet; a one-time "+10 min" checkout extension; linked outbound/return
pairs; the protocol skew CI job; a shorter daily re-check or device-bound session tokens if R17 needs a smaller
window.

Possible future options, none designed out, each needing your explicit OK: **any pre-cart VNC, as an attended
BC Ferries sign-in before a cart** (the former M5; you said no for v1). Adding it undoes nothing, but it is
about 1.5 weeks: an exception to "VNC only while a cart is held", a new kind of ticket at the pod's gate, a
"signed in, keep watching" branch, a sign-in-minutes budget in the five "cart held" checks, a check that the
pod is still logged in, and the sandbox hardening first. An account checkout mode: with stored passwords
ruled out, that attended sign-in is its only route.
Out of scope: Stripe, Pushover for users, SMS in v1, CRDs, namespace per tenant, an HA control plane, a second
egress IP, a "watch my browser" view outside holds.

---

## 9. Decisions

**Decided in round 2 (your answers):** guest checkout accepted for now, you included; signing in to BC
Ferries inside the checkout probably won't work while a cart is held and is advised against, not relied on
(old Q3/Q3b). Identity is app-native OIDC with a cookie-less app session (old Q0). No pre-cart sign-in window
in v1; M5 removed (old Q1). No spike that tests guest checkout; every related choice reversible (old Q2).
Checkout host: `ferry-checkout.chestr.dev`.

**Our conclusions, not your answers:** gateway Lua vs a separate domain is mostly moot, as the app sets no
cookie (old Q6); a separate domain would still stop script on the ferry host from riding your Authelia cookie
to your other `chestr.dev` apps (R12), which every other app there shares today, so we don't propose it. You
asked why the sandbox was needed (old Q4); it's explained in section 6, and whether to make it optional is
Q12 below.

### Still open

Numbered as in design-reference.md §14.2, old numbers in brackets. Each has a recommended default. Q1, Q8, Q9
and Q12 change scope; the rest tune numbers or wording. None is needed for W0: Q1 by the end of M2, Q8-Q10 and
Q12 before M3, the rest when the milestone that uses them starts.

1. **Q1 [Q5] VNC first-connection race.** Accept that a compromised ferry-web could win a hold's first
   connection (visible, before card entry; no later takeover)? **Default: accept for invite-only.**
   **Q1b [Q5b]:** if iOS home-screen apps lose the device nonce (S-VNC-lite), allow a "takeover" ticket after
   60 s with no viewer? **Default: no**; tell iOS users to check out in Safari or on a computer.
2. **Q2 [Q7] After a viewed checkout with no booking detected,** ask "Did you book it?" and resume after 20 min
   without an answer (the viewed sailing is never re-carted)? **Default: yes.**
3. **Q3 [Q8] Per-user defaults.** Approve the values in section 5 (hold clocks tuned later from telemetry)?
   **Q3b [Q8b]:** should an outbound + return pair count as one watched trip? **Default: approve; no pairing
   in v1**, raise that user's `maxActiveBrowsers` to 2.
4. **Q4 [Q9] BC Ferries etiquette.** 20 checks/min (ceiling 40) from your IP, and keep the desktop user agent
   (`site.py:45-47`)? **Default: yes; revisit if BC Ferries objects.**
5. **Q5 [Q10] Holds during Talos upgrades** are released (with an alert) in v1, and the runbook pauses carting
   first. **Default: accept**; hold PDBs + `drainTimeout` are an M4 option.
6. **Q6 [Q11] Invitee login.** Keep `two_factor` on the `ferry-watch` client (needs SES for enrolment) and
   soften the cluster-wide 1-day ban after 3 failures to 1 h? **Default: two_factor; soften to 1 h.**
7. **Q7 [Q12] Admin access.** Confirm admins never get in-app VNC or exec into tenant browsers, and tenants are
   told the cluster admin can technically see everything. **Default: confirm.**
8. **Q8 [Q13] 3DS egress.** (a) a curated allowlist of payment and bank 3DS hosts from your checkouts and
   S-3DS, plus the blocked-host detector, or (b) a wide-open egress during holds (lets a tenant browse anywhere
   from your IP while holding). With (a), may S-3DS walk a household card to the 3DS step and stop (a brief
   real hold, maybe a bank prompt, no booking)? **Default: (a); walks only if you agree, otherwise only real
   checkouts count.**
9. **Q9 [Q14] Postgres superuser Secret in `ferry-watch`.** Keep the standard `components/cnpg`, or add a
   variant without the initdb Secret? **Default: standard for M1, variant before non-household invitees.**
10. **Q10 [Q15] Self-hosted runner.** Restrict `home-cluster-runner` as in section 7? **Default: yes, before M3.**
11. **Q11 [Q16] Travelling invitees.** The Cloudflare geo-block (CA/US/CN) also covers `auth.chestr.dev`. Add a
    skip rule or tell invitees? **Default: tell invitees.**
12. **Q12 [Q4] Sandbox and kernel flags.** Having read section 6: accept Chromium without its sandbox on
    `mitigations=off` nodes for now, with the hardening (about 4 days plus a per-node Talos change) as an
    optional backlog item, or make it a gate before non-household invitees (M3 grows by about 4 days)?
    **Default: accept for now; do the hardening before inviting people you don't know well.**
