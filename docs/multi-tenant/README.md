# Ferry Watch multi-tenant: plan for approval

**Implementation status:** [implementation.md](implementation.md) lists what's built, where the code departs
from this plan, and what's left.

This is the document to read and approve. Implementation detail lives in
**[design-reference.md](design-reference.md)** next to it; sections are cited as "design-reference.md §N".
Your decisions and the verified cluster facts are logged in [decisions.md](decisions.md), and the evidence
behind both documents is in [research/](research/). Code refs are `file:line` under `src/ferry_watch/` at 1cdfca9
(`site.py` is `bcferries/site.py`), except `polling.py`, which is new. Cluster refs are paths in
`mchestr/home-cluster` at 0bff2f8, under `kubernetes/apps/` unless stated.

**Revised for your round-3 answers** (design-reference.md Appendix D rows 61-75):
- **Polling:** trips at most 30 days ahead; your nearest trip within 7 days gets your one warm browser, which
  checks every 5 min a week out, down to every 30 s in the last 6 hours; other trips get a short check every
  6 hours from a pod that checks once and exits. Quiet hours 23:00-06:00 alert only. The numbers live in
  `src/ferry_watch/polling.py`.
- **Checkout:** public HTTPS egress opens while a cart is held, so any bank's 3-D Secure works; the 3DS
  allowlist and S-3DS are gone. After a checkout we can't confirm, the trip waits for "Did you book it?".
- **Sign-in:** `one_factor` for Ferry Watch, so no second-factor enrolment.
- **Settled:** the VNC race, no iOS takeover, 20 checks/min and the desktop UA, Talos releases, admin access,
  the Postgres Secret, the CI runner, the geo-block. The old M3 is merged into M2. Section 9 lists what is
  still open: three questions, none blocking W0 or M1.

---

## 1. What we're building

Today one container runs the dashboard, every trip's watch loop, one shared Chromium with no sandbox
(`site.py:154-157`), your BC Ferries password (`settings.py:40-42`) and a VNC view that is always open to
anyone who can reach `/vnc` (`entrypoint.sh:20-26`). The new system runs **disposable headed-browser pods, one
per trip being checked**, in their own namespace: a long-lived **warm** pod for each user's nearest trip
within 7 days, and a short-lived **short-check** pod for any other trip every 6 hours. A small **operator**
creates and deletes those pods and relays typed status. Alerts take the user to a dashboard page that opens a
VNC view of **their own** pod, and only while it holds a cart. Access is invite-only: the dashboard signs
people in with **OpenID Connect against your Authelia**, behind a replaceable interface. Nobody's BC Ferries
password is stored anywhere: checkout is as a guest, assumed to work, detected and switchable to alert-only.
The single-container mode is retired.

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
 |    fleet loop: warm/short-check pods, egress label --> kube-apiserver (VAP-pinned)          |
 |    hub endpoint: typed status in; commands, intervals, cart claims out          |           |
 +-----------^---------------------------------------------------------------------|-----------+
             | agent dials out (WS, projected token)       /t/<trip>/g/<gen>/ws -> |
 +- ns ferry-watch-browsers (PSA restricted, default-deny, no Secrets, not components/common) -+
 |  pod fw-<trip>-g<gen>: agent (one-trip watcher + VNC gate) | Chromium kiosk | Xvnc on unix  |
 +-------------------------------------------|-------------------------------------------------+
                                             | egress: bcferries.com + Queue-it (DNS + toFQDNs);
                                             | while a cart is held: any public HTTPS host
                       BC Ferries, Queue-it, payment/3DS  (one home WAN IP for all)
```

### Components

| Piece | What it does | What it can do | What it cannot do |
|---|---|---|---|
| **`ferry-control` (the operator)** | Decides which browser pods exist (fleet loop) and is the only endpoint pods talk to (hub endpoint). Warm-trip choice, short-check schedule, queue, timers, reaper, poll intervals, IP budget, one hold per user. | Create and delete pods of one admitted shape in `ferry-watch-browsers`; flip one label on them (`ferry-watch.io/egress`, open egress during a hold) and nothing else; TokenReview; read nodes; send `check_now`, `release`, `dismiss`, `drain`, `vnc_revoke`; its own DB tables. | Read any Secret; exec, attach, port-forward, read logs of or proxy to a pod; change a pod's image, env or anything but that label; see page content, screenshots, keystrokes, cookies or the cart session; steer the browser; mint a VNC ticket; be reached from the internet. |
| `ferry-web` | OIDC sign-in and sessions, dashboard, user and admin API, live updates, VNC tickets, alerts from a DB outbox. Holds the OIDC client secret, ticket key and VAPID key. | Read and edit tenants' trips; mint tickets for a cart its owner may open; send push and email; talk to Authelia's token endpoint and JWKS. | Anything in Kubernetes (no token, no RBAC); reach browser pods; join a VNC session the user already opened; see your Authelia cookie (removed at the route). |
| `vnc-edge` | Serves the unmodified KasmVNC client and proxies `/t/<trip>/g/<gen>/ws` to that pod. | Sees VNC traffic in plaintext (as do Envoy and Cloudflare). | Mint tickets; reach ferry-web, ferry-control or Postgres. No app code, no secrets. |
| Browser pod `fw-<trip>-g<gen>` | Today's watcher for one trip: search, cart, hold timers, the VNC gate. Chromium in kiosk mode, Xvnc on a unix socket. | Reach bcferries.com, Queue-it and ferry-control; during its own hold, any public HTTPS host. Hold a guest cart. Admit a viewer with a valid ticket. | Reach other pods, the API server, the LAN, nodes, metadata, smtp-relay or logging, ever. Use its token anywhere but ferry-control. |
| Postgres | All durable state; two login roles with separate grants. | Trips, limits, invitations, session hashes, cart summaries, audit, outbox. | Hold any BC Ferries credential or a usable session token. |
| GitOps guardrails | VAPs, PSA restricted, ResourceQuota, LimitRange, PriorityClass, two Cilium egress policies. | Bound what a buggy or compromised operator can create or change. | Be changed by any app identity. |

Three deployments (`ferry-web` and `ferry-control` from one image, plus `vnc-edge`), browser pods, the shared
Postgres. No CRDs. Authelia gains one OIDC client.

### Why the operator cannot get secrets out of browser pods

1. **There is no stored secret to take.** Guest mode: no BC Ferries password exists anywhere (the old login code
   stays behind a flag no image sets). A pod holds a guest cart cookie and what the user types during one
   hold, and is destroyed after any viewed hold.
2. **RBAC.** `pods` create/delete/get/list/watch/patch in one namespace, `tokenreviews` create, `nodes` read.
   No exec, attach, port-forward, logs, proxy or Secret verb. CI checks the negatives with `kubectl auth can-i`.
3. **Admission.** One ValidatingAdmissionPolicy admits exactly one digest-pinned pod shape (8 fixed env vars, no
   command override, no extra containers, no secret volumes, no debug knobs). A second (`fw-browser-pod-lock`,
   now in M1) lets the operator's patch change only the egress label between `allowlist` and `hold`, and
   denies exec, attach and port-forward. A CI matrix tries every leak vector (design-reference.md §7.5, §11.4).
4. **Protocol.** The agent dials out; its typed protocol has no navigate, URL, JS, screenshot, cookie or storage
   operation. The operator can revoke VNC, never grant it.
5. **Not in the VNC path.** ferry-web mints tickets and the pod verifies them; checkout traffic goes Cloudflare ->
   Envoy -> vnc-edge -> pod and never touches ferry-control.

What the operator *can* still do if compromised: forge any trip's status, release carts, start pinned pods up to
the quota, open hold egress on any pod, and read itineraries and emails (design-reference.md §4.5).

---

## 2. How the plan meets each of your requests

| You asked | How the plan meets it | What it costs |
|---|---|---|
| (1) operator, a headed browser per watched route, tenants isolated, no secrets out via the operator | One bare pod per trip being checked, created only by `ferry-control`, pinned by VAPs; the five points above. "Route" = trip (route + date + party). | ~1-1.5 GiB per running browser. Every pod start is a form walk and maybe a Queue-it wait. The VAPs and RBAC must stay correct forever (CI enforces it). |
| (1) browsers do the refresh flows and talk to the operator | The pod runs check/cart/hold; it dials `ferry-control` with a projected token checked by TokenReview. | A protocol to version (N and N-1). |
| (1) alerts link to the VNC | Cart alerts link to `https://ferry.chestr.dev/c/<trip>`: "Check out on this device" mints a ticket and opens VNC. | One extra tap; a bearer VNC link in a notification would hand VNC to whoever can read it. |
| (1) "scale better" | 6 warm browsers plus a short-check slot at the starting 12 GiB; 3-22 warm across 8-24 GiB; short checks for everyone else (section 5). | The home IP is the real ceiling; the curve keeps the fleet at 2-5 checks/min. |
| (2) admin limits on watched routes per user | `maxTrips` 3 saved, `maxActiveBrowsers` 1 warm browser, one hold at a time; per-user overrides, clamped by GitOps ceilings. | A small admin UI and a limits resolver. |
| (2) reap unused browsers; no misuse | No VNC outside a held cart; hold caps (60 min hard); pods recycled after every viewed hold and about every 24 h; short-check pods exit after one check; kiosk mode, policies, egress allowlist outside holds. | More pod starts, absorbed by the start throttle. |
| (3) VNC only while a cart is held | Enforced five times: in the pod, by ferry-web refusing to mint, by the operator's clock, by Xvnc `-MaxConnectionTime`, by `activeDeadlineSeconds`. | No "watch my browser" view; no pre-cart sign-in window (round 2). |
| (4, 6) Ko-fi link, no gating; Stripe out | `kofiUrl` value, validated `https://ko-fi.com/<name>`, a plain link. CSP unchanged. | None. |
| (5) identity easy to replace | Plain OIDC behind an `IdentityProvider` interface; issuer, client and claims are config; user id = `hash(iss, sub)` with GitOps aliases; swap runbook in design-reference.md §6.2.6. | GitHub, Apple or several providers need a broker (Dex, Pocket ID, authentik). |
| (7) no Pushover | Web Push to the PWA, email for cart alerts and as fallback, via smtp-relay. Alertmanager keeps Pushover for ops. | A service worker, VAPID keys, iOS 16.4+ "Add to Home Screen", SES reaching invitees. |
| (8) home-cluster is the source of truth | Every cluster fact verified against the repo (design-reference.md §2.2); cluster config lives there (section 7). | A home-cluster PR per milestone. |
| Invite-only | Authelia gives `ferry-watch` codes only to lldap groups `ferry-watch` and `admin`; the app admits only people you invited; suspension revokes every session at once; sessions re-checked with Authelia every 24 h. | lldap users made by hand on a LAN-only UI; removal in lldap takes up to 24 h to reach the app, so suspend in the app first (R20); never reuse a username (R21). |
| Operator only; Cilium, Envoy Gateway, k8s 1.36 | `entrypoint.sh` retired; compose for dev, kind for chart work. `toFQDNs` + DNS L7 allowlist, two static header-stripping HTTPRoutes, VAPs, TokenReview pod binding. | `toFQDNs` and label-driven policy are new on this cluster: S-CIL first. |
| (review) one browser per user; route each user to their own pod | `maxActiveBrowsers` 1, now chosen automatically (round 3). One host, one path per pod, nginx maps the path to that pod's DNS name, the pod admits only its owner's ticket. | None beyond vnc-edge. |
| (round 2) guest checkout "for now"; "Users can always sign-in on the browser?" | Guest-only, you included. Signing in to BC Ferries inside the checkout **probably won't work** (BC Ferries refuses a login while a guest cart exists, [top-level README](../../README.md); `site.py:288-290`) and is advised against: the password would take the card's unencrypted path. The copy says "Check out as a guest. Don't sign in to BC Ferries here." | Your own checkouts lose saved cards. One policy line can block `/login` if you prefer a rule. |
| (round 2) "Can we use oidc?" | Yes: ferry-web is an OIDC client of Authelia (confidential, PKCE), verifies the `id_token`, then gives the SPA its own bearer token instead of a cookie, so the route drops every Cookie header. | About 3-4 days; residuals R17-R21 (section 6). |
| (round 2) why the sandbox; no pre-cart window; no spike, no one-way doors | Sandbox explained in section 6; optional hardening is Q1. VNC strictly cart-gated. No spike tests guest checkout; fixtures from your use, production detection, an alert-only switch, the legacy app restorable, every guest choice a two-way door (section 3). | Your first real guest checkout is the end-to-end proof. |
| (round 2) checkout host name | `ferry-checkout.chestr.dev` everywhere; covered by the `*.chestr.dev` cert and tunnel rule. | None. |
| (round 3) "be good users of the api": poll less when far off, more when close; 30-day horizon; quiet hours alert only | The warm browser checks on your curve; other trips get 6-hourly short checks; trips ≤ 30 days; 23:00-06:00 alert only. Canonical: `src/ferry_watch/polling.py`. | A trip that isn't your nearest is seen every 6 h until it is. |
| (round 3) pause until "Did you book it?" is answered | The trip stays paused; "Yes" archives, "No" resumes; the viewed sailing is never re-carted. | A user who never answers stops that trip being watched. |
| (round 3) open public HTTPS egress while a cart is held | A second Cilium policy keyed on a pod label only ferry-control can change, and only that label (section 3). | A tenant can browse other sites from your IP during their own hold; code in a holding pod can reach any HTTPS host (R22). |
| (round 3) `one_factor` for Ferry Watch | The ferry client is password-only; other clients keep `two_factor` and still get group restrictions before anyone new joins lldap. | The generated password is an invitee's only factor. |
| (round 3) the rest | R3 race accepted; no iOS takeover; 20/min (40) with the desktop UA; Talos releases holds with an alert; no admin VNC, exec or screenshots; the Postgres Secret stays; the runner stays; the geo-block stays and onboarding says Canada/US. | Stated residuals (section 6). |

---

## 3. Key decisions

**Polling: one warm browser per user on a curve, short checks for the rest (your round-3 answer)**
(design-reference.md §6.6, §8.2, §9.3; canonical numbers in `src/ferry_watch/polling.py`).
- The warm browser (`maxActiveBrowsers`, default 1) goes automatically to your enabled trip with the nearest
  sailing within 7 days, and moves when a nearer trip appears (never during a hold). It checks by time to
  departure: 7-3 days every 5 min, 3 days-24 h every 2 min, 24-6 h every 60 s, under 6 h every 30 s (±15 %
  jitter; a 30 s floor compiled into the operator and the agent). This replaces "Pause *X* to watch this one"
  and the 30-day "Starts watching" wait.
- Every other saved trip (up to 3, within 30 days) gets a **short check every 6 hours** (admin-configurable
  up to daily), and one at once when added: a pod starts, checks once and exits. Short-check pods don't take
  your warm slot; they use cluster capacity (one slot is kept for them) and the start throttle. If one finds
  an opening and you hold no cart, it carts it and stays for checkout.
- **One hold per user:** before carting, a pod asks ferry-control for a cart claim, granted atomically only
  if you hold nothing, so a warm and a short-check pod can't both cart.
- **Quiet hours** 23:00-06:00 Pacific: checks continue, openings only alert (no cart overnight).
- Load on BC Ferries: typically 2-5 checks/min for the whole fleet, about 14 in the worst case (7 users all
  within 6 hours of a sailing), against today's 12/min for your trips alone. Above 20/min every interval
  stretches proportionally.

**Hold egress: the allowlist always, plus public HTTPS while a cart is held (your round-3 answer)**
(design-reference.md §7.6, C20, R16, R22).
- One Cilium policy lets every browser pod reach only bcferries.com and Queue-it. A second adds any public
  address on TCP 443, and DNS for any name, for pods labelled `ferry-watch.io/egress: hold`. ferry-control
  flips that label at READY and back at hold end; the pod-lock VAP lets it change that label and nothing
  else. Private, CGNAT, link-local (metadata), cluster, node, smtp-relay and observability destinations are
  denied in every state. Chromium's policy now allows any `https://` page and leaves hosts to the network.
- Not chosen: public HTTPS for every pod all the time (about 2 days less work). Most of a pod's life is
  unattended watching; that option would give an exploited pod the open internet for up to a day instead of
  only during a hold. It stays the fallback if S-CIL shows label changes are slow or unreliable.
- If the flip fails, the bank page doesn't load: detected, retried every 5 s, the admin alerted, and "Release
  and book it myself" offered.

**Identity: app-native OIDC with a cookie-less session (round 2)** (design-reference.md §6.2).
- Authorization Code + PKCE against `https://auth.chestr.dev`, the code returned in the URL fragment; ferry-web
  verifies the `id_token` (JWKS, `iss`, `aud`, `exp`, `nonce`, an `alg` allowlist), admits only invited users
  and returns an opaque bearer token (hash stored; 7 days idle, 30 absolute). Sessions are re-checked with
  Authelia every 24 h, silently while the Authelia session lives. The app sets no cookie, so the route drops
  every Cookie header. The client is **`one_factor`** (round 3).
- Not chosen: ext-auth + trusted headers, or Envoy Gateway's OIDC with claims in headers: both fail open if
  the gateway policy is detached; app-native OIDC fails closed.

**Guest checkout, assumed to work, every choice reversible (round 2)** (design-reference.md §5.7).
- Guest carting already works (only `watcher.py:459`/`:562` block it). Guest *checkout* gets no spike:
  fixtures come from your real use in M1; the agent reports forced login walls and expired carts; "Did you
  book it?" carries reasons; a `guest_checkout_failing` admin alert fires on the first forced wall.
- **If it fails:** "Pause carting" on the Fleet page (alert-only) at once, then `autoCart: false`, or
  alert-only per fare or vehicle type. The legacy app stays restorable until a real guest checkout has worked.
- **Two-way doors:** the login code kept behind a disabled flag with its tests; `checkout_mode` reserved in the
  DB and protocol; the VAP's env allowlist can admit a login-mode variable; the ticket names its purpose.

**One pod per trip being checked, and its memory** (design-reference.md §8.4). Measured ~0.52-0.57 GB on light
pages; we start at `podMemory` 1.5 GiB (tmpfs counts) and lower it after S-MEM. A shared Chromium with a context
per tenant (35-80 MB) is exactly the isolation failure you want gone.

**VNC gating: in-pod ticket check** (design-reference.md §5.5, §7.9). A 60 s single-use Ed25519 ticket bound to
owner, trip, pod and cart; the pod then locks the hold to that device's random nonce. Ticket and nonce travel in
the URL fragment and a WebSocket subprotocol, never in logs. Xvnc listens only on a unix socket.

**Postgres on the shared CNPG `postgres17`; no CRDs; Python + lightkube** (design-reference.md §6.4, D2, D4).
Quota checks need transactions; two login roles, RLS on the outbox, immutable trip owner. The standard
`components/cnpg` puts the superuser Secret in `ferry-watch`, kept permanently (round 3). One language shares
models, protocol and limits between web, operator and agent.

**Alerts: Web Push + email from an outbox** (design-reference.md §6.7). Only ferry-web sends, from its own
templates; pods emit typed kinds and can't inject text or links. Every cart alert also goes by email.

---

## 4. A user's journey

Detail: design-reference.md §3.5-3.6 (data flows), §6.2 (sign-in) and §8.1 (state machine).

1. **Invite.** You create them in lldap (group `ferry-watch`) with a generated password, then "Add person" on
   the Users page. The `invited` email covers signing in (password only, consent once, "Remember me"), the
   home-screen PWA, notifications, a test alert, how checking works, Canada/US only, and checking out as a
   guest.
2. **Sign in.** Authelia issues a code only to groups `ferry-watch` and `admin`; ferry-web verifies the
   `id_token`, binds the invitation and hands the app its session token.
3. **Add a trip** (at most 30 days ahead; `maxTrips` 3). Its first short check runs within minutes. If it is
   the user's nearest trip within 7 days it gets the warm browser ("Watching closely · every 2 min");
   otherwise "Checked every 6 h · next 2:05 pm". Nothing asks the user to pause another trip.
4. **Pod starts.** The operator creates `fw-<trip>-g1` (or queues it). The pod runs its lockdown self-tests,
   dials the operator, walks the booking form (maybe through Queue-it) and checks: on the curve if warm, once
   if a short check.
5. **Sailing opens.** The agent carts it as a guest if the user has holds left, holds no other cart (the cart
   claim), it isn't quiet hours and carting isn't paused; otherwise a "bookable" alert links to bcferries.com.
   ferry-control flips the pod's egress label to `hold`.
6. **Alert.** Web Push plus email: "Held for you: 7:00 am Tsawwassen -> Swartz Bay, Fri Aug 1. Check out as a
   guest by 3:42 pm." Reminder at 15 min; released at 30 min if nobody opens it (a warm pod keeps watching).
7. **Choose a device.** `/c/<trip>` offers *Check out on this device*, *I'll use my computer* or *Release and
   book it myself*, and says "Check out as a guest. Don't sign in to BC Ferries here."
8. **Checkout over VNC.** The ticket opens `ferry-checkout.chestr.dev/t/<trip>/g/<gen>/`; the pod locks the hold
   to that device. The user pays on the real bcferries.com page; the payment and bank 3DS pages load from
   wherever they live. Phone sleep or a switch to the bank app reconnects with the same nonce. Session and
   viewer-gone clocks pause on payment pages; the 60 min hard cap never pauses.
9. **Booked.** If the agent sees BC Ferries' confirmation page, the trip is archived and the user sees
   "Booked!" (with the Ko-fi link). Otherwise "Did you book it?" with optional reasons, and **the trip stays
   paused until they answer**: "Yes" archives, "No" resumes. The viewed sailing is never re-carted.

---

## 5. Admin controls, quotas and reaping

### Defaults you would tune (full table: design-reference.md §8.2)

Per-user values: effective = `min(override or default, GitOps ceiling)`; overrides on the Users page with a
mandatory reason. Polling values are fleet-wide, from `polling.py`, changed by values PR.

| Setting | Default | Ceiling / bound | Notes |
|---|---|---|---|
| `maxTrips` (saved trips per user) | 3 | 10 | replaces `MAX_TRIPS = 6` (`models.py:16`) |
| `maxActiveBrowsers` (warm browsers) | 1 | 3 | the nearest trip(s) within 7 days, automatically |
| holds at once, per user | 1 | 1 | cart claim; short-check pods can cart too |
| `tripHorizonDays` | 30 | your rule | `polling.py:41` |
| warm poll curve | 5 min / 2 min / 60 s / 30 s from 7 d / 3 d / 24 h / 6 h out | floor 30 s (compiled) | ±15 % jitter; `polling.py:34-39` |
| short check, other trips | every 6 h | up to 24 h | one check per pod; `polling.py:40` |
| quiet hours | 23:00-06:00 Pacific | fleet-wide | polling continues, alert only |
| `maxHoldsPerDay` / `maxCartAttemptsPerDay` | 4 / 6 | 10 / 12 | Pacific day |
| `vncMinutesPerDay` | 60 | 120 | checked before carting |
| hold: unclaimed / session / viewer gone / hard cap | 30 / 30 / 10 / 60 min | 45 / 45 / 15 / 90 (compiled) | tuned from telemetry |
| "Did you book it?" | trip paused until answered | — | no automatic resume |
| app session | 7 d idle / 30 d; re-checked every 24 h | re-check 1-24 h | silent while Authelia's session lives |
| `checksPerMinute` (whole fleet) | 20 | 40 | typically 2-5 used, ~14 worst case |
| `browserMemoryBudget` / `podMemory` | 12 GiB / 1.5 GiB | LimitRange 2 GiB | `podMemory` to measured p95 x 1.2 |
| `maxBrowsersTotal` / `shortCheckSlots` | 7 / 1 | `floor(budget/podMemory) - 1` | 6 warm browsers at the start |

**Capacity** (design-reference.md §8.4): warm browsers = people with a sailing within 7 days at once: 8 GiB ->
3-6, 12 GiB -> 6-10, 16 GiB -> 8-14, 24 GiB -> 14-22 (pods at 1.5 or 1.0 GiB). Everyone else costs a few
minutes of the short-check slot every 6 hours; one slot serves about 40 users whose trips are all further out.

### What the admin can do (v1; design-reference.md §9)

- **Fleet page:** memory vs budget, warm and short-check browsers, queue, short checks overdue, checks/min
  against the curve's demand, brake and breaker, live holds, hold outcomes by fare and vehicle type, an
  attention list (including hold-egress flips failing and `guest_checkout_failing`). Actions: maintenance,
  **pause carting** (before Talos upgrades; also the alert-only switch), close the breaker, release a cart,
  recycle a pod.
- **Users page:** state, invitations, effective limits with their source, usage, push/email health, sessions,
  unmatched sign-ins. Actions: add person, suspend (revokes sessions, drains pods), revoke sessions,
  overrides, make admin, delete user. Remove someone by suspending here first, then changing lldap
  (design-reference.md §12.5).
- Every mutation needs a reason and writes an INSERT-only audit row plus a log line.
- **Admins never get VNC, exec or screenshots** of a tenant's browser (round 3). Break-glass stays with you
  as cluster admin, out of band; tenants are told the cluster admin can technically see everything.
- Deferred to M4: Trips, Settings and Audit pages.

### What ends a browser (reaping)

- A short-check pod exits after its one check, unless it carted.
- After **any viewed hold** the pod exits, so nothing a viewer did persists.
- A warm pod is recycled after about 24 h (only while idle); hard `activeDeadlineSeconds` of 26 h.
- Trip disabled, paused, past or deleted, a nearer trip taking the warm slot, user suspended, or limit lowered:
  drained (never during a hold).
- Startup with no first check in 15 min; Watching pod silent 5 min; 6 consecutive failures; repeated failures
  back off (5-60 min) and then "Needs attention". Orphans swept every 60 s.

"Can't use it for other things" comes from: no VNC outside a hold, kiosk Chromium with managed policies (no
`file://`, `chrome://`, downloads, extensions), the egress allowlist outside holds, and the daily hold and
VNC-minute caps. During their own hold a tenant can reach other HTTPS sites through links (R22, accepted).

---

## 6. Security

### What is protected, from whom

Assets, highest first: card details typed during checkout; a live cart; your home IP's reputation with BC
Ferries and Queue-it; your cluster, LAN, shared Postgres and Authelia session; alert integrity; tenants'
itineraries.

| Attacker | Stopped from | By |
|---|---|---|
| Anyone on the internet without an invitation | Any data or action | Authelia's client policy; the app's invitations; 401 on every `/api/*` without a session; sockets must authenticate within 5 s; per-client sign-in limits |
| Someone who takes over ferry-web | Creating or altering pods, reading Secrets, reaching pods, joining a checkout already opened, replaying your Authelia session from the server | No Kubernetes token; no route to pods; the first-viewer lock; your cookie removed at the route. **Not stopped:** script it serves is same-site with your other `chestr.dev` apps and can make blind requests carrying your Authelia cookie (R12), as any app there can. |
| Someone who takes over vnc-edge | Minting tickets; reaching anything but a pod's `/ws` | No secrets, no app code. It *does* see and can inject into sessions passing through it, like Envoy and Cloudflare. |
| A malicious invited tenant | Other tenants' trips or VNC; exceeding limits; keeping a browser; reaching private addresses | User-scoped queries (foreign id = 404); tickets bound to owner and cart; ceilings; hold caps; egress denies private and cluster ranges always |
| Code running in one browser pod (bad page, Chromium exploit) | Other pods, the API server, LAN, nodes, metadata, smtp-relay, logs; the open internet outside a hold; another trip | Default-deny Cilium policies; no API token; TokenReview binding of each socket to one trip |
| That code escalating into the operator | Changing the pod shape, reading Secrets, exec, page content, minting VNC | VAPs (including the one-label patch rule), RBAC, no ticket key in the operator |

Who sees card data typed over VNC: the user's own pod, vnc-edge, Envoy, Cloudflare's edge and cloudflared (VNC
is plaintext once TLS ends). **Not** the operator, ferry-web, Postgres or logs.

### Why the sandbox and kernel question came up (round 2; still open as Q1)

- **The sandbox.** Chromium normally runs each page in a sandboxed renderer, so a bug that lets a page run code
  is trapped in a process that can do almost nothing. In containers that sandbox needs Linux user namespaces,
  which these pods don't get (Talos sets `user.max_user_namespaces=0`; containerd's seccomp profile blocks the
  calls), so Playwright starts Chromium with `--no-sandbox` (`site.py:154-157`), as today.
- **What that means.** A malicious script on bcferries.com, Queue-it or a payment/3DS page that exploits a
  Chromium bug runs code directly in that pod, at any point in its life, and can wait for the checkout to read
  the card. Per-pod isolation, no stored secrets, no API token and the egress policies keep it to that one
  tenant's pod **while two other layers hold**: ferry-control's message parser and the Linux kernel. Nothing
  like AppArmor confines the container, and all three nodes are control-plane nodes that also run workloads
  (`talos/controlplane.yaml:191`), so a kernel escape is the whole home cluster. Since round 3, a holding pod
  can also reach any public HTTPS host, which gives such code an easier way out during the hold (R22).
- **The kernel flags.** The nodes boot with `mitigations=off`, `apparmor=0` and `security=none`
  (`talos/schematic.yaml:6-12`); once an exploit gives native code in an unsandboxed pod, a CPU side channel
  can target kernel memory and every workload on that node.
- **Recommendation (Q1).** Neither is needed for the product to work. We recommend **optional hardening
  (about 4 days plus a per-node Talos change), done before inviting people you don't know well**, not a gate
  unless you make it one. The VAP params that can require the sandbox stay in place.

### Residual risks, plainly (full list R1-R22: design-reference.md §4.6)

- **You and anything with cluster-admin see everything**, including a live checkout: `openclaw` and
  `home-cluster-runner` (which also has Talos `os:admin` and runs a `pull_request` workflow). You leave the
  runner as is (round 3); tenants are told.
- **Chromium runs without its sandbox on `mitigations=off` nodes** unless you opt in (R2, Q1).
- **Checkout keystrokes cross Cloudflare, cloudflared, Envoy and vnc-edge in plaintext**, which can also inject
  input. PCI scope for relaying a cardholder's own keystrokes is a grey area. Tenants are told.
- **Open egress during holds** (R22, accepted): a tenant can browse other sites from your home IP during their
  own hold (at most 60 min, 4 holds a day), and code in a holding pod can reach any HTTPS host and tunnel
  through DNS; a compromised operator can open it on any pod.
- **The hold-egress flip can fail** (R16): the bank page then doesn't load; detected, retried, alerted.
- **A compromised ferry-web could win the race to a hold's first VNC connection** (visible, before card entry;
  no later takeover). Accepted for invite-only (round 3); S-VNCID stays in M4.
- **Guest checkout is assumed, not proven.** Production detection alerts you on the first forced login wall;
  "Pause carting" makes the fleet alert-only; the legacy app stays restorable.
- **The app session token is readable by script on the dashboard origin** (R17); the strict CSP makes
  injection hard but can't stop exfiltration by navigation; the 24 h re-check bounds a stolen token.
- **iOS home-screen sign-in may land in Safari or be lost when backgrounded** (R18): S-IOS on your iPhone in
  M1, with fallbacks.
- **Unauthenticated requests reach ferry-web** (R19), but only the static shell, `/auth/*` and 401s.
- **Removal at the IdP takes up to 24 h** (R20); **Authelia's `sub` follows the username** (R21): suspend in
  the app first, never reuse a username.
- **One home IP** (R13): a block hits everyone; the curve, budget, brake and breaker reduce this, but code in
  a pod can bypass them toward the hosts its policy allows.
- **Holds can be lost** on node crash, OOM, eviction, and Talos upgrades (released with an alert; accepted).
- **The shared Postgres superuser Secret sits in `ferry-watch`** (standard `components/cnpg`; accepted
  permanently). No app identity can read it.
- Also accepted: BC Ferries' own third-party scripts (which also load during holds); a policy gap the tests
  miss; a plaintext in-cluster agent channel (TLS in M4); a compromised control process can delete trips or
  release carts (audited, not prevented).

---

## 7. Changes needed in home-cluster

Ferry Watch's app-template HelmRelease (`kubernetes/apps/ferry-watch/`, copied here as `deploy/helmrelease.yaml`)
creates the browser namespace, VAPs, PriorityClass, quotas, both browser egress policies, ferry-web's egress to
`auth.chestr.dev` and both HTTPRoutes. home-cluster owns the rest (design-reference.md §2.3, §6.2.3, §7.10).
The W0 and M1 items up to the cutover are written on home-cluster's `claude/ferry-watch-multi-tenant-eyxfa0`
branch, except the 1Password fields, the release values (both listed in its
`kubernetes/apps/ferry-watch/ferry-watch/README.md`) and the GrafanaDashboard (nothing exports metrics yet).

**W0 (spikes)**
- [ ] Cilium values: `dnsProxy.dnsRejectResponseCode: nameError` (REFUSED stops glibc's search list; no DNS
      rules exist today, so nothing regresses).
- [ ] The Authelia OIDC client below, registered early for S-OIDC with only the `ferry-next.chestr.dev`
      redirect URI.

**M1: you on the new stack**
- [ ] `kubernetes/apps/ferry-watch/`: namespace with `components/common`; `ks.yaml` with `components/cnpg`
      (`APP=ferrywatch`), `dependsOn: cloudnative-pg-cluster`; an app-template HelmRelease
      (`upgrade.strategy: RetryOnFailure`); values for hosts, FQDN allowlist, `identity.oidc`,
      `bootstrapAdmins`, ceilings, memory budget, `kofiUrl`, `autoCart` and alert-only lists, ticket public keys
      (`polling` only to change your round-3 numbers).
- [ ] Authelia (`default/authelia/app/resources/configuration.yaml`; YAML in design-reference.md §6.2.3):
      `authorization_policies.ferry-watch` (default deny; **`one_factor`** for `group:ferry-watch`,
      `group:admin`) and a client `Ferry Watch` in the cluster's usual pattern (confidential, PKCE S256,
      consent 1y, `claims_policy: default`, `response_modes: [fragment]`), redirect URIs on `ferry.chestr.dev`
      and, until cutover, `ferry-next.chestr.dev`.
- [ ] Client id, secret and hash from the `default/authelia/app/README.md` script (`APP=FERRY_WATCH`) in the
      1Password item `authelia`; Authelia's ExternalSecret gains the id and hash.
- [ ] 1Password item `ferry-watch`: ticket Ed25519 key, VAPID key. Item `cloudnative-pg`: the `ferrywatch`
      owner, `ferrywatch_web` and `ferrywatch_control` passwords. ferry-watch's ExternalSecret reads these
      plus the client id and secret.
- [ ] CNPG managed roles `ferrywatch_web`, `ferrywatch_control` in
      `database/cloudnative-pg/cluster/cluster17.yaml`.
- [ ] smtp-relay NetworkPolicy: admit `ferry-watch/ferry-web` on 2525.
- [ ] Silences for `OomKilled`, `KubePodNotReady`, `KubePodCrashLooping`, `KubeQuota*` in `ferry-watch-browsers`;
      Fluent Bit `Exclude_Path` for `*_ferry-watch-browsers_*`; descheduler exclusion.
- [ ] Cloudflare: Rocket Loader and email obfuscation off for the ferry hosts. GrafanaDashboard; Renovate
      always opens a PR for `/ferry-watch/`.
- [ ] Cutover PR (after your first sign-in on `ferry-next`): legacy `default/ferry-watch` to `replicas: 0`, its
      route off, its PVC `prune: disabled`; `ferry.hosts.dashboard: ferry.chestr.dev`. The revert PR pins the
      legacy image by digest.
- [ ] Only once 14 days have passed, a real guest checkout has worked on the new stack and no forced login
      wall has been seen since: delete `default/ferry-watch`, its reference to the shared `pushover` item,
      `BCF_USER`/`BCF_PASS` and the `ferry-next` redirect URI.

**M2: before anyone new is added to lldap**
- [ ] Authelia `authorization_policies` restricting Immich, Windmill, Grafana, Autobrr and Paperless to their
      groups (they keep `two_factor`).
- [ ] lldap group `ferry-watch`.
- [ ] Each invitee's email deliverable through SES (invite and alert email): verify each address, then SES
      production access before the wider stage.
- [ ] Q2: soften Authelia `ban_time` from 1 day to 1 h (cluster-wide), if you agree.

**Optional, your call (backlog; Q1):**
- [ ] Chromium sandbox: Talos `user.max_user_namespaces` + a `machine.seccompProfiles` Localhost profile on
      every node, after S-SBX on one node; decide on `mitigations=off apparmor=0`. About 4 days.
- [ ] `policy.drainTimeout`/`timeout` on the TalosUpgrade if holds should ever survive upgrades.

Not needed any more: an ext-auth SecurityPolicy, a ReferenceGrant change, an Authelia `access_control` rule,
a gatus canary (round 2); restricting `home-cluster-runner` and a `components/cnpg` variant (round 3).

---

## 8. Delivery plan

### Spikes: what each one decides (experiments and fallbacks: design-reference.md §12.1)

No spike tests guest checkout and no booking is made only to test. S-MEM reads real BC Ferries pages; S-DRAIN
and the M1 VNC check briefly hold one real cart on a sailing with space and release it.

| Spike | When | Decides |
|---|---|---|
| S-KIOSK (local) | W0 | Kiosk lockdown, Xvnc on a unix socket, the gate, the stock client through the shim, SIGTERM drain. |
| S-VNC-lite (cluster, test page) | W0 | Phone reconnects through Cloudflare; whether iOS home-screen apps keep the device nonce (if not: Safari or a computer, no takeover). |
| S-CIL (cluster) | W0 | `toFQDNs` + DNS rule vs `toCIDRSet`; SNAT entity rules; **the hold-egress label flip** (speed, open connections survive, private ranges stay denied), else public HTTPS always. |
| S-OIDC (cluster) | W0 | Authelia accepts code + PKCE in fragment mode; outsiders denied; silent second sign-in; egress to the issuer; no Cookie reaches the app. |
| S-TR (cluster) | W0 | TokenReview pod binding, VAP CONNECT matching, and the pod-lock VAP's one-label UPDATE rule on 1.36.5. |
| S-MEM (cluster) | W0, M1 | `podMemory`, `cpuRequest`, `maxBrowsersTotal`, cold-start time (also the length of a short check). |
| S-DB | W0 | Managed roles, RLS and LISTEN/NOTIFY on `postgres17`. |
| Fixture capture | M1, your real use | Cart, confirmation, release, expired-cart, login-wall and block pages; the classifiers follow. |
| S-DRAIN | M1 | A real Talos drain with a brief hold: released in order, the reboot proceeds. |
| S-IOS (your iPhone) | M1 | Password sign-in, re-sign-in and the daily re-check stay in the home-screen app; else a fallback (up to +1 day). |
| S-PUSH | M2 | iOS push and re-sign-in from a notification; SES reaches an invitee. |
| S-SBX (one node) | optional (Q1) | Whether Chromium's sandbox runs on Talos with the sysctl + seccomp profile. |

Also S-VNCID (M4) and S-IDP (at an IdP change). Removed in round 3: S-3DS.

### Milestones (one developer, focused days; arithmetic in design-reference.md §12.3)

| Milestone | Size | Delivers | Exit criteria |
|---|---|---|---|
| **W0 De-risk** | 7-8.5 days | W0 spikes; Cilium `nameError`; frontend tolerates unknown enums; an opening-visibility metric on today's deployment. | Every spike passed or has a chosen fallback. No answers needed from you. |
| **M1 You on the new stack** | 66.5-81.5 days | Workspace split; one-trip agent (login code kept, disabled); supervisor, lockdown, gate; **poll curve, short checks and cart claims**; **hold egress** with the pod-lock VAP; guest-checkout detection; fixtures; ferry-control; ferry-web with OIDC, invitations, tickets, `/c`, email; vnc-edge; Postgres; chart; CI (VAP leak matrix incl. UPDATE cases, `can-i`, compose, kind-lite); home-cluster PR; cutover. | 0 carts lost across restarts and a drain (compose); a short-check pod carts while the warm pod watches, and a second opening is refused a claim; smoke green in both egress states; S-DRAIN; S-IOS or its fallback; a real hold opened over VNC on your iPhone with a control restart mid-hold; 7 days on the new stack only. |
| **M2 Invitees: household, then wider** | 16.5-21 days | Web Push and onboarding; Fleet and Users pages with invitations and session revocation; delete-my-data; persisted brake and breaker; cross-tenant suite; Authelia policies and the lldap group; tenant disclosure; restore rehearsal; then `fw-ns-objects` VAP and SES production access. | Household: cross-tenant suite green, quota and claim races pass, up to 3 invitees for 14 days with no Pending pods or overdue short checks, Q2 answered. Wider: the cut line below green, Q1 decided, 3-5 non-household invitees for 14 days. |
| M4 Hardening backlog | per item | See "Deferred", including the optional sandbox (about 4 days). | Each item ships alone. |

**The old M3 is merged into M2.** It held S-3DS (gone), the runner restriction (left as is), the database
variant (dropped) and the pod-lock VAP (moved to M1, which hold egress needs). What remains, the
`fw-ns-objects` VAP and SES production access, is about 2 days, too little for a milestone; the audience step
stays as M2's two stages.

Totals: **90-111 focused days (18-22 weeks)** to non-household invitees; you are on the new stack after W0 + M1,
**73.5-90 days (14.5-18 weeks)**; about **21-25 calendar weeks** with soaks. Versus 90-113 before: W0 +0.5 (the
flip and UPDATE-rule checks); M1 +3.5-4.5 (poll model +2-2.5, hold egress +2-2.5, OIDC −0.5 with no
enrolment); the old M3 (6.5-10) is gone and M2 grows by 2.5-3 for what was left of it. A failed S-IOS adds up
to 1 day; the optional sandbox is outside the total. Cheapest cuts that weaken no guarantee: email-only
household alerts (Web Push before the wider stage, -3 days to the household); household limits by values PR
(-3 days).

### Minimum secure v1 (all required before the first non-household invitee; design-reference.md §12.4)

- Guest-only, no BC Ferries credential anywhere (the login code present but disabled); production detection and
  the alert-only switch.
- One bare pod per trip being checked, `restartPolicy: Never`, `activeDeadlineSeconds`, the agent's compiled
  maxima.
- Only ferry-control creates pods, through the pinned-shape VAP; the pod-lock VAP limits its patch to the
  egress label; PSA restricted; quotas; no API token in pods; ferry-web without RBAC; RBAC negatives in CI and
  live; the namespace-objects VAP on.
- Typed agent protocol with no page operations; TokenReview binding; enum-only termination message.
- In-pod gate: 60 s single-use ticket bound to owner/trip/pod/cart, atomic first-viewer lock, single viewer,
  Origin check, revocation at hold end; hold caps in all five places.
- Xvnc on a unix socket only; fail-closed self-tests; kiosk + managed policies. (The sandbox is optional.)
- Static vnc-edge with the stock client and a strict CSP; every Cookie header stripped on both routes.
- Egress: the allowlist policy and the hold-egress policy with its label flip, both states verified by the
  post-deploy smoke; `nameError`; the blocked-frame detector; browser namespace outside `components/common`,
  excluded from Fluent Bit and the descheduler.
- Identity: app-native OIDC (`alg` allowlist), the cookie-less session re-checked every 24 h, codes and tokens
  out of URLs, per-client sign-in limits; invitations; Authelia policies on every client (`one_factor` on
  Ferry Watch); invitee email deliverable; the "Removing a person" runbook.
- User-scoped repository; cross-tenant suite green; limits (`maxTrips`, `maxActiveBrowsers = 1`, one hold per
  user, holds/day, VNC minutes/day, 30-day horizon, the curve's 30 s floor); Fleet and Users pages; Web Push +
  email; persisted brake.
- SES production access; Q1 decided; tenants told about cluster-admin visibility (the runner included), the
  plaintext keystroke path (naming Cloudflare), open egress during holds, and Canada/US only.

### Deferred (M4 and later; none is needed for the guarantees you asked for; design-reference.md §12.2, §15)

Optional sandbox and kernel-flag hardening (Q1; about 4 days); device handoff by QR code; S-VNCID (closes R3);
hold PDBs + Talos `drainTimeout`; breaker canaries and AIMD leases; TLS on the agent channel; a per-pod
rate-limiting egress proxy; cosign/SBOM with admission verification; admin Trips/Settings/Audit pages (the
poll curve editable there); a request-access queue; the `identityAliases` CLI; a warmer DaemonSet (shorter
short checks); a "+10 min" checkout extension; linked outbound/return pairs (Q3); the protocol skew job; a
shorter daily re-check if R17 needs it. Possible future option, needing your explicit OK: an attended BC
Ferries sign-in before a cart (the former M5, about 1.5 weeks; you said no for v1). Out of scope: Stripe,
Pushover for users, SMS in v1, CRDs, namespace per tenant, an HA control plane, a second egress IP.

---

## 9. Decisions

**Decided** (design-reference.md §14.1 has the full table):
- Round 2: guest checkout for now, you included, with no spike and only two-way doors; signing in to BC Ferries
  during checkout advised against; app-native OIDC with a cookie-less session; no pre-cart sign-in window;
  checkout host `ferry-checkout.chestr.dev`.
- Round 3: the VNC first-connection race accepted for invite-only; no iOS takeover ticket; a trip paused until
  "Did you book it?" is answered; the 30-day horizon, the warm-browser curve, 6-hourly short checks and quiet
  hours alert only (`polling.py`), with the other per-user defaults as proposed and one hold at a time; 20
  checks/min (ceiling 40) and the desktop UA; Talos upgrades release holds with an alert; `one_factor` for
  Ferry Watch, with the other clients restricted before anyone joins lldap; no admin VNC, exec or screenshots;
  open public HTTPS during holds; the Postgres Secret kept; the runner left as is; the geo-block unchanged.
- Ours, not yours: the hold-egress mechanism (the allowlist always plus a label-keyed hold policy, section 3);
  a separate domain stays unproposed (the app sets no cookie; R12 is shared by every `chestr.dev` app).

### Still open

Each has a recommended default; none is needed for W0 or M1.

1. **Q1 Sandbox and kernel flags** (was Q12). Having read section 6: accept Chromium without its sandbox on
   `mitigations=off` nodes for now, with the hardening (about 4 days plus a per-node Talos change) as an
   optional backlog item, or make it a gate before non-household invitees (M2's wider stage grows by about 4
   days)? Open egress during holds makes the hardening a little more valuable. **Default: accept for now; do
   the hardening before inviting people you don't know well.** Needed before the first non-household invitee.
2. **Q2 Authelia's lockout** (the unanswered half of Q6). Three wrong passwords in an hour ban an account for a
   day, cluster-wide. Soften `ban_time` to 1 h? The invitee's password is generated, so 72 guesses a day cost
   little, and a mistyped password won't lock someone out of a hold for a day. **Default: soften to 1 h.**
   Needed before the first invitee.
3. **Q3 Outbound and return pairs** (was Q3b). A return sailing gets 6-hourly short checks until the outbound
   has sailed. Should a pair share the warm browser? **Default: no pairing in v1**; raise that user's
   `maxActiveBrowsers` to 2 if needed. Any time.
