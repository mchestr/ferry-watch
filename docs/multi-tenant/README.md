# Ferry Watch multi-tenant: plan for approval

This is the document to read and approve. Implementation detail lives in
**[design-reference.md](design-reference.md)** next to it; sections are cited as "design-reference.md §N".
Your decisions and the verified cluster facts are logged in [decisions.md](decisions.md), and the evidence
behind both documents is in [research/](research/). Code refs are `file:line` under `src/ferry_watch/` at 1cdfca9
(`site.py` is `bcferries/site.py`). Cluster refs are paths in `mchestr/home-cluster` at 0bff2f8, under
`kubernetes/apps/` unless stated. Section 9 lists the decisions I need from you; everything else follows from
your messages and the verified cluster facts.

---

## 1. What we're building

Today one container runs the dashboard, every trip's watch loop, one shared Chromium with no sandbox
(`site.py:154-157`), your BC Ferries password (`settings.py:40-42`) and a VNC view that is always
open to anyone who can reach `/vnc` (`entrypoint.sh:20-26`: `-DisableBasicAuth -SecurityTypes None
-AlwaysShared`). The new system runs **one disposable headed-browser pod per actively watched trip** in its
own namespace. A small **operator** creates and deletes those pods and relays typed status. Alerts take the
user to a dashboard page that opens a VNC view of **their own** pod, and only while that pod holds a cart.
Access is invite-only through Authelia at the gateway (trusted headers behind a replaceable interface).
Per-user limits, an admin page, and reaping rules keep the fleet bounded. Nobody's BC Ferries password
is stored anywhere: checkout is as a guest. The single-container mode is retired.

### Architecture at a glance

```
 Internet -> Cloudflare edge + tunnel (CA/US/CN only) -> envoy-external (Envoy Gateway 1.9.2)
            |                                                       |
   ferry.chestr.dev                                       ferry-vnc.chestr.dev
   Authelia ext-auth -> Remote-User header;               no gateway auth (the pod checks a ticket);
   Cookie + Authorization stripped                        Cookie + Authorization stripped
            |                                                       |
 +- ns ferry-watch ---------------------------------------------------------------------------+
 |  ferry-web: dashboard, API, admin,                   vnc-edge: stock nginx + stock KasmVNC  |
 |  VNC ticket minter, Web Push + email.                client; forwards WS upgrades only      |
 |  NO Kubernetes rights.                                      |                               |
 |        | SQL (own role)                                     |                               |
 |  Postgres `ferrywatch` on the shared CNPG postgres17        |                               |
 |        | SQL (own role)                                     |                               |
 |  ferry-control = "THE OPERATOR"                             |                               |
 |    fleet loop: create/delete pods ----> kube-apiserver (VAP admits one pinned pod shape)   |
 |    hub endpoint: typed status in, typed commands out        |                               |
 +-----------^-------------------------------------------------|-------------------------------+
             | agent dials out (WS, projected token)            | /t/<trip>/g/<gen>/ws -> that pod
 +- ns ferry-watch-browsers (PSA restricted, default-deny, no Secrets, not components/common) -+
 |  pod fw-<trip>-g<gen>: agent (one-trip watcher + VNC gate) | Chromium kiosk | Xvnc on unix |
 +-------------------------------------------|-------------------------------------------------+
                                             | egress: DNS + toFQDNs allowlist only
                       bcferries.com, Queue-it, payment/3DS hosts  (one home WAN IP for all)
```

### Components

| Piece | What it does | What it can do | What it cannot do |
|---|---|---|---|
| **`ferry-control` (the operator)** | Decides which browser pods exist (fleet loop) and is the only endpoint pods talk to (hub endpoint). Queue, timers, reaper, IP budget. | Create and delete pods of one admitted shape in `ferry-watch-browsers`; TokenReview; read nodes; send `check_now`, `release`, `dismiss`, `drain`, `vnc_revoke`; read and write its own DB tables. | Read any Secret; exec, attach, port-forward, read logs of or proxy to a pod; see page content, screenshots, keystrokes, cookies or the cart session; steer the browser or run JS; mint a VNC ticket; be reached from the internet. |
| `ferry-web` | Dashboard, user and admin API, per-user live updates, mints VNC tickets, sends alerts from a DB outbox. | Read and edit tenants' trips; mint tickets for a cart its owner may open; send push and email. | Anything in Kubernetes (no token mounted, no RBAC); reach browser pods; join a VNC session the user already opened. |
| `vnc-edge` | Serves the unmodified KasmVNC web client and proxies the WebSocket for `/t/<trip>/g/<gen>/ws` to that pod. | Sees VNC traffic in plaintext (as do Envoy and Cloudflare). | Mint tickets; reach ferry-web, ferry-control or Postgres. No app code, no secrets. |
| Browser pod `fw-<trip>-g<gen>` | Today's watcher, run for one trip: search, cart, hold timers, the VNC gate. Chromium in kiosk mode, Xvnc on a unix socket. | Reach bcferries.com, Queue-it, the allowlisted payment/3DS hosts and ferry-control. Hold a guest cart. Admit a viewer with a valid ticket. | Reach other pods, the API server, the LAN, nodes, smtp-relay, logging or the open internet. Use its token anywhere but ferry-control. |
| Postgres | All durable state; two login roles (`ferrywatch_web`, `ferrywatch_control`) with separate grants. | Trips, limits, cart summaries, audit, alert outbox. | Hold any BC Ferries data or credential. |
| GitOps guardrails | VAP, PSA restricted, ResourceQuota, LimitRange, PriorityClass, Cilium policies, all rendered from values. | Bound what a buggy or compromised operator can create. | Be changed by any app identity. |

Three deployments (`ferry-web` and `ferry-control` from one image, plus `vnc-edge`), browser pods, the shared
Postgres. No CRDs.

### Why the operator cannot get secrets out of browser pods

1. **There is no stored secret to take.** Guest mode: no BC Ferries password exists anywhere. A pod holds a
   guest cart cookie and what the user types during one hold, and is destroyed after any viewed hold.
2. **RBAC.** `pods` create/delete/get/list/watch in one namespace, `tokenreviews` create, `nodes` read.
   No exec, attach, port-forward, logs, proxy or Secret verb. CI checks the negatives with `kubectl auth can-i`.
3. **Admission.** A ValidatingAdmissionPolicy admits exactly one digest-pinned pod shape: 8 fixed env vars,
   no command override, no extra containers, no secret volumes, no debug knobs such as `DEBUG=pw:*` or
   `SSLKEYLOGFILE`. A CI matrix tries every leak vector and must be denied (design-reference.md §7.5, §11.4).
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
| (3) VNC only while a cart is held | Enforced five times: in the pod (authoritative), by ferry-web refusing to mint, by the operator's own clock, by Xvnc `-MaxConnectionTime`, by `activeDeadlineSeconds`. | No "watch my browser" view. A pre-cart sign-in window would be an exception (Q1, default no). |
| (4, 6) Ko-fi donation link, no gating; Stripe out | `kofiUrl` Helm value, validated `https://ko-fi.com/<name>`, a plain link in the footer and on "Booked!". CSP unchanged. | None. |
| (5) Authelia + trusted headers, easy to replace, don't depend on Authelia | Authelia ext-auth SecurityPolicy on the dashboard route; app reads `Remote-User` through an `IdentityProvider` interface; user id = `hash(provider, subject)`; no Authelia-only feature is load-bearing. Swap runbook in design-reference.md §6.2.6. | Fails open if the gateway policy is ever detached; contained by rendering route and policy together plus a forged-header canary. Session length is Authelia's. |
| (7) no Pushover | Web Push to the existing PWA by default, email for cart alerts and as fallback, via the in-cluster smtp-relay. Your Alertmanager keeps Pushover for ops. | A service worker, VAPID keys, iOS 16.4+ "Add to Home Screen", SES able to reach invitees. |
| (8) home-cluster is the source of truth | Every cluster fact is verified against the repo (design-reference.md §2.2); all cluster-specific config lives in home-cluster (section 7). | A home-cluster PR per milestone. |
| Invite-only | Three gates: lldap group `ferry-watch` at Authelia, admin pre-registration in the app, instant suspension. | lldap users are created by hand on a LAN-only UI; no remote password reset. |
| Operator only | `entrypoint.sh` and the standalone image retired; docker compose for dev, kind for chart work. | One-time import of your `config.json`/`alerted.json`. |
| Cilium, Envoy Gateway, k8s 1.36 | `toFQDNs` + DNS L7 egress allowlist; HTTPRoute + SecurityPolicy; VAP, TokenReview pod binding, `hostUsers: false`. | `toFQDNs` and the DNS proxy have never been used on this cluster: spike S-CIL first. |
| (review) one browser per user at a time | `maxActiveBrowsers` default 1, admin-configurable per user; enabling a second trip returns "Pause *X* to watch this one". | Users choose which trip is live. |
| (review) route each user to their own pod | One host `ferry-vnc.chestr.dev`, one path per pod, nginx maps the path to that pod's own DNS name, the pod admits only a ticket bound to its owner, trip, pod and cart. | None beyond vnc-edge. |

---

## 3. Key decisions

**Identity: Authelia ext-auth + trusted headers (recommended) vs app-native OIDC** (design-reference.md §6.2).
- Recommended: a route-level SecurityPolicy on envoy-external sends each request to Authelia; Envoy
  overwrites `Remote-*` headers with Authelia's answer, then the route removes `Cookie` and `Authorization`
  so ferry-web never sees your Authelia session cookie (scoped to all of `chestr.dev`). About 1 day of app code.
- Alternative: app-native OIDC with its own session cookie (4-5 days). Because the app would then need cookies,
  stripping your `authelia-home-ops` cookie by name needs gateway-wide Lua (`enableLua`, off today) or a
  separate registrable domain. It fails closed and gives a signed token, which is its real advantage.
- The earlier draft chose OIDC on a wrong inference ("invitees are off-LAN"); the review moved back to your
  message 5. Later social login = swap the ext-auth backend for oauth2-proxy (design-reference.md §6.2.6).
  Q0 confirms.

**Guest mode only (recommended) vs stored credentials** (design-reference.md §5.7).
- Guest carting already works; only the credential check at `watcher.py:459`/`:562` blocks it. Guest
  *checkout* is probable but unproven: spikes S2/S2b must pass before carting ships, otherwise alert-only.
- Alternatives: per-user credentials in Secrets/1Password (rejected: puts plaintext in reach of the operator);
  an opt-in attended sign-in over VNC before a cart exists (deferred to M5, Q1).
- Cost to you: your own checkouts lose saved cards (`site.py:270-271` logs in today) and account-only fares
  (Q3b).

**One pod per actively watched trip, and its memory cost** (design-reference.md §8.4).
- Measured ~0.52-0.57 GB of process memory on light pages; with real pages and the in-memory `/tmp` and
  `/dev/shm` counted against the limit, we start at `podMemory` 1.5 GiB and lower it after S-MEM.
- Alternative: one shared Chromium with a context per tenant (35-80 MB each), which is cheap but is exactly the
  isolation failure you want gone. Sharing one browser per route across users is also rejected: one tenant's
  page would drive another's alerts.

**VNC gating: in-pod ticket check (recommended) vs gateway auth on the VNC host**
(design-reference.md §3.3, §5.5, §7.9).
- ferry-web mints a 60 s single-use Ed25519 ticket bound to owner, trip, pod and cart; the pod checks it,
  then locks the hold to that device's random nonce. The ticket travels in the URL fragment and then, with
  the nonce, in a WebSocket subprotocol header, so neither lands in access logs. Xvnc listens only on a unix
  socket, so page JS cannot open it.
- Alternative: Authelia on `ferry-vnc.chestr.dev`. Envoy only checks the upgrade, cross-node traffic is SNATed
  so a header is unprovable at the pod, and iOS home-screen apps keep a separate cookie jar. An IdP-signed
  principal the pod verifies itself is the M4 upgrade (S-VNCID).

**Storage: Postgres on the shared CNPG `postgres17` (recommended) vs SQLite on a PVC**
(design-reference.md §6.4).
- Quota checks need transactions; audit and usage must be queryable and backed up; it is your cluster
  convention. Two login roles via CNPG managed roles, row-level security on the outbox, immutable trip owner.
- Cost: `components/cnpg` puts the shared superuser Secret in `ferry-watch` (R14, Q14). Cluster PITR rolls back
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

Detail: design-reference.md §3.5-3.6 (data flows) and §8.1 (state machine).

1. **Invite.** You create them in lldap (group `ferry-watch`), then "Add person" on the Users page. They get
   an `invited` email: sign in, enrol a passkey or TOTP via Authelia's emailed code, tick "Remember me", add
   the PWA to the home screen, allow notifications, send a test alert.
2. **Add a trip.** The form checks `maxTrips` and `maxActiveBrowsers` in one transaction. A trip more than
   30 days out shows "Starts watching Jul 2"; a second live trip gets "Pause *X* to watch this one".
3. **Pod starts.** The operator creates `fw-<trip>-g1` (or queues it: "Waiting for a free browser (#2)").
   The pod runs its lockdown self-tests, dials the operator, walks the booking form (maybe through Queue-it)
   and starts checking every 30 s or more.
4. **Sailing opens.** The agent carts it as a guest if the user has holds left, it is outside quiet hours and
   the operator acked within 30 s; otherwise it sends a "bookable" alert with a bcferries.com link.
5. **Alert.** Web Push plus email: "Held for you: 7:00 am Tsawwassen -> Swartz Bay, Fri Aug 1. Check out as a
   guest by 3:42 pm." The link is `/c/<trip>`. Reminder at 15 min; released at 30 min if nobody opens it, and
   the pod keeps watching.
6. **Choose a device.** `/c/<trip>` offers *Check out on this device*, *I'll use my computer* (emails a fresh
   link) or *Release and book it myself*.
7. **Checkout over VNC.** The ticket opens `ferry-vnc.chestr.dev/t/<trip>/g/<gen>/`; the pod locks the hold to
   that device. The user pays on the real bcferries.com page. Phone sleep or a switch to the bank app
   reconnects with the same device nonce. Session and viewer-gone clocks pause while on a payment/3DS page; the
   60 min hard cap never pauses.
8. **Booked.** The agent sees BC Ferries' confirmation page, closes the viewer and the pod exits; the trip is
   archived and the user sees "Booked!" (with the Ko-fi link). With no confirmation seen, they get "Did you
   book it?"; no answer in 20 min resumes watching with a fresh pod, and the viewed sailing is never
   re-carted.

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
| hold: unclaimed / checkout session / viewer gone / hard cap | 30 / 30 / 10 / 60 min | 45 / 45 / 15 / 90 (compiled) | session default set from S2b's phone timing |
| `checksPerMinute` (whole fleet, one home IP) | 20 | 40 | 30 s each up to 10 pods, 60 s at 20 |
| `browserMemoryBudget` / `podMemory` | 12 GiB / 1.5 GiB | LimitRange 2 GiB | `podMemory` drops to measured p95 x 1.2 after S-MEM |
| `maxBrowsersTotal` | 7 | derived: `floor(budget/podMemory) - 1` | |

Capacity at one browser per user (design-reference.md §8.4): 8 GiB -> 4-7 users, 12 GiB -> 7-11, 16 GiB -> 9-15,
24 GiB -> 15-23, depending on whether pods measure 1.5 or 1.0 GiB.

### What the admin can do (v1; design-reference.md §9)

- **Fleet page:** memory used vs budget, browsers by phase, queue, checks/min, brake and breaker, live holds
  with countdowns, an attention list. Actions: maintenance on/off, **pause carting** (before Talos upgrades),
  close the breaker, release a cart, recycle a pod.
- **Users page:** state, effective limits with their source, usage today, push/email health. Actions: add
  person (sends the invite), suspend (drains pods at once), set overrides, make admin, delete user.
- Every mutation needs a reason and writes an INSERT-only audit row plus a log line.
- **Admins never get VNC** into a tenant's browser. Break-glass stays with you as cluster admin, out of band.
- Deferred to M4: Trips, Settings and Audit pages. Until then, defaults change by values PR and audit is
  read with `psql` or VictoriaLogs.

### What ends a browser (reaping)

- After **any viewed hold** the pod exits; watching resumes in a new pod, so nothing a viewer did persists.
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
| Someone who takes over ferry-web (internet-facing) | Creating or altering pods, reading Secrets, reaching pods, joining a checkout the user already opened | No Kubernetes token; no route to pods; the pod's first-viewer lock |
| Someone who takes over vnc-edge (internet-facing) | Minting tickets; reaching anything but a pod's `/ws` | No secrets, no app code. It *does* see and can inject into sessions passing through it, like Envoy and Cloudflare (residual below) |
| A malicious invited tenant | Seeing other tenants' trips or VNC; exceeding limits; keeping a browser | User-scoped queries (foreign id = 404); tickets bound to owner and cart; ceilings; hold caps |
| Code running in one browser pod (bad page, Chromium exploit) | Reaching other pods, the API server, LAN, nodes, smtp-relay, logs, the open internet; acting on another trip | Default-deny Cilium policy with FQDN allowlist; no API token; TokenReview binding of each socket to one trip |
| That code escalating into the operator | Changing the pod shape, reading Secrets, exec, getting page content, minting VNC | VAP, RBAC, no ticket key in the operator |

Who sees card data typed over VNC: the user's own pod, vnc-edge, Envoy, Cloudflare's edge and cloudflared
(VNC is plaintext once TLS ends). **Not** the operator, ferry-web, Postgres or logs. The browser namespace is
excluded from Fluent Bit and denied egress to observability.

### Residual risks, plainly

- **You and anything with cluster-admin see everything**, including a live checkout: `openclaw` and
  `home-cluster-runner`. The runner also has Talos `os:admin` and runs a `pull_request` workflow
  (`actions-runner-system/actions-runner-controller/runners/home-cluster/rbac.yaml`); restricting it is a hard
  gate before non-household invitees. Tenants are told.
- **Chromium runs without its sandbox until M3**, on nodes booted with `apparmor=0 security=none
  mitigations=off` (`talos/schematic.yaml:6-12`). A renderer exploit owns that pod; a kernel exploit reaches the
  node and every tenant on it. Non-household invitees wait for the sandbox (Q4).
- **Checkout keystrokes cross Cloudflare, cloudflared, Envoy and vnc-edge in plaintext**, which can also inject
  input. Whether relaying a cardholder's own keystrokes puts you in PCI scope is a grey area. Tenants are told.
- **A compromised ferry-web could win the race to a hold's first VNC connection** (visible to the user, before
  any card entry; it cannot take over afterwards). Accept for friends and family (Q5) or wait for M4.
- **One home IP.** A Queue-it or BC Ferries block hits every tenant and your household. The budget, brake and
  breaker reduce this; code running in a pod can bypass them toward the allowlisted hosts until a per-pod
  rate-limiting proxy (M4).
- **Trusted headers fail open** if the SecurityPolicy is ever detached; the chart refuses to render the route
  without it and a gatus canary sends a forged `Remote-User` every minute.
- **Holds can be lost** on node crash, OOM or eviction, and in v1 a Talos upgrade releases holds on the drained
  node (with an alert). Runbook: pause carting before merging a Talos bump.
- **A bank's 3DS host missing from the allowlist** breaks checkout at the last step; the pod detects it and
  offers "Release and book it myself". S-3DS shrinks the gap (Q13).
- **The shared Postgres superuser Secret sits in `ferry-watch`** (standard `components/cnpg`). No app identity can
  read it; removing it needs a component variant (Q14).
- Also accepted: BC Ferries' own third-party scripts; a policy gap the tests miss; a plaintext in-cluster agent
  channel (TLS in M4); a compromised control process can delete trips or release carts (audited, not prevented).
  The full list (R1-R16) is in design-reference.md §4.6.

---

## 7. Changes needed in home-cluster

The app chart (published from this repo) creates the browser namespace, VAPs, PriorityClass, quotas, network
policies and both HTTPRoutes. home-cluster owns the rest (design-reference.md §2.3, §7.10).

**W0 (spikes)**
- [ ] Cilium values: `dnsProxy.dnsRejectResponseCode: nameError` (the default REFUSED stops glibc's search list,
      so short service names never resolve; no DNS rules exist today, so nothing regresses).
- [ ] One node, temporarily: Talos `user.max_user_namespaces` sysctl + a `machine.seccompProfiles` Localhost
      profile, for S-SBX.

**M1: you on the new stack**
- [ ] `kubernetes/apps/ferry-watch/`: namespace with `components/common`; `ks.yaml` with `components/cnpg`
      (`APP=ferrywatch`), `dependsOn: cloudnative-pg-cluster`; OCIRepository + HelmRelease
      (`upgrade.strategy: RetryOnFailure`); values for hosts, FQDN allowlist, `identity.providerId`,
      `bootstrapAdmins`, ceilings, memory budget, `kofiUrl`, ticket public keys.
- [ ] 1Password item `ferry-watch`: VNC ticket Ed25519 private key, VAPID private key. Item `cloudnative-pg`:
      `ferrywatch_postgres_{username,password}`, `ferrywatch_web_password`, `ferrywatch_control_password`.
      ExternalSecrets for both items.
- [ ] CNPG managed roles `ferrywatch_web`, `ferrywatch_control` in
      `database/cloudnative-pg/cluster/cluster17.yaml`, with password ExternalSecrets in `database`.
- [ ] `default/authelia/app/referencegrant.yaml`: allow SecurityPolicies from namespace `ferry-watch`.
- [ ] Authelia `access_control` rule for `ferry.chestr.dev` and `ferry-next.chestr.dev` ->
      `group:ferry-watch`, `group:admin`, `two_factor`, placed before the `*.chestr.dev` rule at
      `default/authelia/app/resources/configuration.yaml:99`. **No bypass rule** for any ferry host.
- [ ] lldap group `ferry-watch`.
- [ ] smtp-relay NetworkPolicy: admit `ferry-watch/ferry-web` on 2525.
- [ ] Silences for `OomKilled`, `KubePodNotReady`, `KubePodCrashLooping`, `KubeQuota*` in `ferry-watch-browsers`.
- [ ] Fluent Bit `Exclude_Path` for `*_ferry-watch-browsers_*`; descheduler namespace exclusion.
- [ ] Cloudflare rule: Rocket Loader and email obfuscation off for the ferry hosts.
- [ ] gatus forged-header canary endpoint; GrafanaDashboard; Renovate always opens a PR for `/ferry-watch/`.
- [ ] Cutover PR: legacy `default/ferry-watch` to `replicas: 0`, its route off, its PVC `prune: disabled`;
      new `routes.dashboardHost: ferry.chestr.dev`. Never two routes for one host (UniFi DNS conflict).
- [ ] 14 days later: delete `default/ferry-watch`; remove only its reference to the shared `pushover` item
      (used by kube-prometheus-stack, gatus, sonarr, radarr, qbittorrent); delete `BCF_USER`/`BCF_PASS`.

**M2: before anyone new is added to lldap**
- [ ] Authelia OIDC `authorization_policies` restricting Immich, Windmill, Grafana, Autobrr and Paperless to their
      groups (today any lldap user passes `two_factor` on every client).
- [ ] Each invitee's email deliverable through SES (production access, or verify each address).
- [ ] Optional: soften Authelia `ban_time` from 1 day to 1 h (Q11, cluster-wide).

**M3: before non-household invitees**
- [ ] Talos sysctl + seccomp profile applied to every node (`task talos:apply-node`); decide on
      `mitigations=off apparmor=0` (Q4).
- [ ] `home-cluster-runner`: push/schedule or same-repo PRs only, approval for outside contributors, matrix
      values via `env:` (Q15).
- [ ] SES production access.
- [ ] If Q14 = yes: a CNPG `Database` + managed owner role, and a `components/cnpg` variant without the initdb
      superuser Secret.

**M4, optional:** `policy.drainTimeout`/`timeout` on the TalosUpgrade if holds should survive upgrades.

---

## 8. Delivery plan

### Phase 0 (W0) spikes: what each one decides (experiments and fallbacks: design-reference.md §12.1)

| Spike | Decides |
|---|---|
| S1 guest cart (live) | Whether auto-cart works without a login at all; stop and re-plan if not. |
| S2 guest checkout path (live, stop before paying) | Carting vs alert-only; collects the payment hosts for the allowlist. |
| S2b one real booking, cancelled (needs Q2) | Proves guest checkout end to end; sets the checkout-session default and the "booked" detector. |
| S5 release | Whether holds ship: `empty_cart` must be provably empty (today it never verifies). |
| S6 BC Ferries cart TTL | Unclaimed-hold length and the cart-liveness check. |
| S7 fare and vehicle coverage | Which trips (yours included) must be alert-only because they need an account. |
| S-FIX fixtures | Classifiers for blocked, not-yet-bookable, expired-cart and confirmation pages. |
| S-KIOSK (local) | Kiosk lockdown, Xvnc on a unix socket, the gate, the stock client through the shim, SIGTERM drain. Fallback: loopback Xvnc behind a per-boot password. |
| S-VNC-lite (cluster, test page only) | Phone reconnects through Cloudflare; whether iOS home-screen apps keep the device nonce (else Q5b). |
| S-CIL (cluster) | `toFQDNs` + DNS rule vs `toCIDRSet` fallback; SNAT entity rules; whether an egress proxy moves to M3. |
| S-AUTH (cluster) | Trusted headers behave as designed (forged headers overwritten, cookies stripped, WS passes); else Q0 option B. |
| S-TR (cluster) | TokenReview pod binding and VAP CONNECT matching on 1.36.5. |
| S-SBX (one node) | Whether Chromium's sandbox runs on Talos with the sysctl + seccomp profile (Q4). |
| S-MEM (cluster) | `podMemory`, `cpuRequest`, `maxBrowsersTotal`, cold-start time. |
| S-DB | Managed roles, RLS and LISTEN/NOTIFY on `postgres17`; else one login role. |

Later spikes: S-DRAIN (M1, real Talos drain with a hold), S-PUSH (M2, iOS push and re-login from a
notification, SES), S-3DS (M3, cards from 3+ issuers to learn 3DS hosts), S-VNCID (M4), S-IDP (at an IdP
change), S3/S4 (M5).

### Milestones (one developer, focused days; arithmetic in design-reference.md §12.3)

| Milestone | Size | Delivers | Exit criteria |
|---|---|---|---|
| **W0 De-risk** | 10.5-13 days | Spikes above; Cilium `nameError`; frontend tolerates unknown enum values; an opening-visibility metric on today's deployment. | Every spike passed or has a chosen fallback; Q0-Q4, Q13, Q14 answered. |
| **M1 You on the new stack** | 57-69 days | Workspace split; one-trip agent, supervisor, lockdown, gate; ferry-control; ferry-web with trusted headers, scoped API, tickets, `/c`, email outbox; vnc-edge; Postgres schema; chart with the pod VAP and quotas; CI (VAP leak matrix, `can-i`, compose, kind-lite); home-cluster PR; cutover. | 0 carts lost across web/control restarts and a drain during holds (compose); post-deploy smoke green; S-DRAIN done; one real guest checkout over VNC on your iPhone with a control restart mid-hold; 7 days on the new stack only. |
| **M2 Household invitees** | 14-18 days | Web Push + service worker + onboarding; admin Fleet and Users pages; invites; delete-my-data; persisted brake and breaker; cross-tenant test suite; Authelia client restrictions; tenant disclosure; restore rehearsal. | Cross-tenant suite green; quota race test passes; up to 3 household invitees for 14 days, no Pending pods, admin time < 10 min/day; Q5 answered; legacy app deleted. |
| **M3 Non-household gate** | 9.5-13 days | Chromium sandbox + `hostUsers: false`; S-3DS allowlist; the two second-fence VAPs; runner restricted; SES production; Q14 if chosen. | Everything in the cut line below is green; 3-5 non-household invitees for 14 days. |
| M4 Hardening backlog | per item | See "Deferred". | Each item ships alone. |
| M5 Attended sign-in (optional) | about 1.5 weeks | Only if Q1 = yes, S3/S4 pass and the sandbox is on. | |

Totals: **91-113 focused days (18-22.5 weeks)** to non-household invitees; you are on the new stack after
**13.5-16.5 weeks**; about **21-26 calendar weeks** to the end of M3 including soaks. The draft's 12 weeks
was below every source design. Cheapest cuts that weaken no guarantee: email-only alerts for the household
(Web Push to M3, -3 days); household limits by values PR instead of the Users page (-3 days).

### Minimum secure v1 (all required before the first non-household invitee; design-reference.md §12.4)

- Guest-only, no BC Ferries credential anywhere; alert-only if S2 fails.
- Only ferry-control creates pods, through the pinned-shape VAP; PSA restricted; quotas including "every pod
  has a deadline"; no API token in pods; ferry-web without RBAC; RBAC negatives checked in CI and live; the
  pod-lock and namespace-objects VAPs on.
- Typed agent protocol with no page operations; TokenReview binding with the pod-name check; enum-only
  termination message.
- In-pod gate: 60 s single-use ticket bound to owner/trip/pod/cart, atomic first-viewer lock, single viewer,
  Origin check, ping liveness, revocation at hold end; hold caps enforced in all five places.
- Xvnc on a unix socket only; fail-closed lockdown self-tests; kiosk + managed policies; **Chromium sandbox on**.
- Static vnc-edge with the stock client and a strict CSP; pods never serve HTML; cookies stripped on both routes.
- Egress allowlist + default deny verified by the post-deploy smoke; `nameError`; the S-3DS allowlist and the
  blocked-host detector.
- Browser namespace outside `components/common`, excluded from Fluent Bit and the descheduler.
- Identity coupling + canary; app pre-registration; Authelia client restrictions; invitee email deliverable.
- User-scoped repository (404 for foreign ids); cross-tenant suite green.
- Limits (`maxTrips`, `maxActiveBrowsers = 1`, holds/day, VNC minutes/day, 30 s floor); Fleet and Users pages.
- Web Push + email; persisted brake with the 30 s agent floor.
- Runner restricted; SES production; Q5 decided; tenants told about cluster-admin visibility and the
  plaintext keystroke path (naming Cloudflare).

### Deferred (M4 and later; none is needed for the guarantees you asked for; design-reference.md §12.2)

Device handoff by QR code; IdP-signed principal verified in the pod (S-VNCID, closes R3); hold PDBs + Talos
`drainTimeout`; breaker canaries and smarter fairness (AIMD leases, a cold Standby tier, near-sailing priority);
TLS on the agent channel; a per-pod rate-limiting egress proxy; cosign/SBOM with admission verification; admin
Trips/Settings/Audit pages and a DB policy table; a request-access queue (with a social IdP); the
`identityAliases` CLI; a warmer DaemonSet; a one-time "+10 min" checkout extension; linked outbound/return
pairs; the protocol skew CI job. Out of scope: Stripe, Pushover for users, SMS in v1, CRDs, namespace per
tenant, an HA control plane, a second egress IP, any pre-cart VNC unless Q1.

---

## 9. Decisions I need from you now

Ordered by how much the answer changes scope (full wording: design-reference.md §14). Each has a recommended
default. Q0-Q4, Q13 and Q14 are needed by the end of W0; the rest can wait until the milestone that uses
them.

1. **Q3 / Q3b: guest checkout and your own checkouts.** If S2 shows guest checkout doesn't work, ship
   alert-only until a login mode exists? And accept that *your* checkouts become guest checkouts too (no
   saved cards, no account-only fares)? **Default: yes to alert-only; accept guest for you**, unless S7 shows
   your usual fare types need an account, in which case those trips are alert-only or M5 moves forward for
   the admin only.
2. **Q0: identity.** Confirm Authelia ext-auth + trusted headers on the dashboard route, with app-native OIDC
   only as a later adapter. **Default: yes.** "No" adds 3-4 days of OIDC code plus gateway-wide Lua or a
   separate domain (Q6).
3. **Q4: sandbox and kernel.** Apply the Talos `user.max_user_namespaces` sysctl and a Localhost seccomp profile
   to every node before non-household invitees? Keep `mitigations=off apparmor=0` on nodes that run tenants'
   Chromium? **Default: apply both before M3; keep `mitigations=off` only while invitees are household, then
   reconsider.** "No" limits the service to your household.
4. **Q1: pre-cart sign-in window.** Allow an opt-in "use my BC Ferries account" mode with a <= 10 min VNC
   window before any cart (an exception to "VNC only while a cart is held")? **Default: no**; revisit after M3
   with the sandbox on. "Yes" adds M5 (~1.5 weeks).
5. **Q13: 3DS egress.** (a) a curated allowlist of payment and bank 3DS hosts from S-3DS plus the blocked-host
   detector, or (b) a wide-open egress during holds (lets a tenant browse anywhere from your IP while holding).
   **Default: (a).**
6. **Q2: one real booking.** May S2b make one cheap guest booking with your card and cancel it within BC
   Ferries' free 30 minutes? **Default: yes**; it is the only end-to-end proof and sets the checkout timer.
7. **Q14: Postgres superuser Secret in `ferry-watch`.** Keep the standard `components/cnpg`, or add a
   variant without the initdb Secret? **Default: standard for M1, variant before non-household invitees.**
8. **Q15: self-hosted runner.** Restrict `home-cluster-runner` as in section 7? **Default: yes, before M3.**
9. **Q5 / Q5b: VNC first-connection race.** Accept that a compromised ferry-web could win a hold's first
   connection (visible, before card entry)? **Default: accept for invite-only.** If iOS home-screen apps lose
   the device nonce, allow a "takeover" ticket after 60 s with no viewer? **Default: no**; tell iOS users to
   check out in Safari or on a computer.
10. **Q10: holds during Talos upgrades** are released (with an alert) in v1, and the runbook pauses carting
    first. **Default: accept**; hold PDBs + `drainTimeout` are an M4 option.
11. **Q8 / Q8b: per-user defaults.** Approve the values in section 5? Should an outbound + return pair count
    as one watched trip? **Default: approve; no pairing in v1**, raise that user's `maxActiveBrowsers` to 2.
12. **Q7: after a viewed checkout with no booking detected,** ask "Did you book it?" and resume after 20 min
    without an answer. **Default: yes.**
13. **Q9: BC Ferries etiquette.** 20 checks/min (ceiling 40) from your IP, and keep the desktop user agent
    (`site.py:45-47`)? **Default: yes; revisit if BC Ferries objects.**
14. **Q11: invitee login.** Keep `two_factor` (needs SES for enrolment) and soften the cluster-wide 1-day ban
    after 3 failures to 1 h? **Default: two_factor; soften to 1 h.**
15. **Q12: admin access.** Confirm admins never get in-app VNC or exec into tenant browsers, and tenants are
    told the cluster admin can technically see everything. **Default: confirm.**
16. **Q16: travelling invitees.** The Cloudflare geo-block (CA/US/CN) also covers `auth.chestr.dev`. Add a skip
    rule or tell invitees? **Default: tell invitees.**

Q6 (gateway Lua vs a separate domain) only matters if Q0 is "no" or the IdP later moves to a cookie on the
ferry host.
