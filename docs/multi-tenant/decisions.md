# Ferry Watch multi-tenant: owner decisions and constraints

The decision log collected while planning (October 2026): the owner's requests and answers, plus the
cluster facts verified in `mchestr/home-cluster`. The plan ([README.md](README.md)) and the design
reference ([design-reference.md](design-reference.md)) treat these as fixed. Items marked
"(Recommendation)" are the planner's, not the owner's.

Owner's goal, in their words: "One of the problems with the headed browser is isolation, we should use
k8s and an operator to spin up a headed browser for each route we are watching, so multiple tenants are
isolated to their own container and no way to get secrets out of them from the main operator pod. The
operator will have access to spin up browsers to do the refresh flows and communicate to the operator,
we can link alerts to users that direct them to the vnc on the headed browser. I think this will scale
better."

## Decided by the owner
1. Kubernetes operator + ONE headed-browser pod PER watched route/trip. Tenants isolated in their own
   containers. The operator (control plane) must not be able to get secrets out of browser pods.
   Browser pods do the BC Ferries refresh/poll/cart flows and report to the operator.
2. Alerts link users to the VNC view of their trip's headed browser.
3. VNC is GATED: reachable only while that trip holds a cart. (Any exception, e.g. a pre-cart sign-in
   window for an opt-in "use my BC Ferries account" mode, must be called out as an open question.)
4. Audience: INVITE-ONLY (friends/family and referrals; an admin approves each account).
5. OPERATOR-ONLY: the single-container personal mode does NOT need to keep working. Local dev may use
   kind/k3d.
6. NO payment gating. A Ko-fi donation link in the dashboard (plain outbound link, URL from config; no
   Ko-fi widget script, so the strict CSP stays). Stripe is out of scope.
7. Identity: start with an authenticating proxy + trusted headers (Authelia today), but DO NOT assume
   Authelia long term (it may become social login, e.g. oauth2-proxy/OIDC). Identity must sit behind an
   abstraction that is easy to replace. Do not make Authelia-only features (e.g. domain_regex named
   groups) a load-bearing security control.
8. Admin controls: per-user limits on the number of watched routes (and other per-user quotas).
9. Reap browsers that are unused; limits so people cannot use the browser to do other things or keep it
   around forever.

## Facts established by research (see research/ files for evidence)
- The watcher only talks to browsers via Session/Browser Protocols (watcher.py:57-78); Session methods
  never take a URL or JS, except login(credentials) which passes the password across the seam.
- Guest carting works without a BC Ferries login (code + PR #1 live test). Auto-cart is gated on
  credentials only by watcher.py:459/562. Guest CHECKOUT on bcferries.com is probable but unverified;
  BC Ferries refuses a login while a guest cart exists. => a "no stored BC Ferries credentials" default
  is likely possible, pending a live spike. (Superseded in round 2: no live spike; guest checkout is
  assumed and detected in production.)
- Measured: ~0.52-0.57 GB RAM per headed pod (Xvnc+matchbox+Chromium+Playwright driver) vs ~35-80 MB per
  extra context in one shared Chromium. Real BC Ferries pages are heavier.
- All pods share one home egress IP behind Queue-it; a global check budget is needed.
- Playwright launches Chromium with --no-sandbox by default (chromium_sandbox not set in site.py).
- KasmVNC runs -DisableBasicAuth -SecurityTypes None -AlwaysShared on 0.0.0.0:6080, viewer served
  same-origin under /vnc today. Xvnc -MaxIdleTime/-MaxConnectionTime/-MaxDisconnectionTime terminate
  the server (=> pod exits via entrypoint `wait -n`); -IdleTimeout only drops a viewer.
- 'create pods' in a namespace ~= read every Secret there; admission (VAP, GA 1.30) must pin pod specs.
- Gateway ext-auth/forward-auth only checks the WebSocket upgrade; open sockets are not re-checked.
- Pushover: account-wide 10k msgs/month since May 2026; each recipient needs a licence. Web Push to the
  existing PWA is the natural default per-user channel.
- BC Ferries now runs free sailing alerts and an auto-booking waitlist pilot (2026); fairness limits
  (hold length, held carts per user, interval floor) matter.
- Unknown cluster facts: k8s version, CNI (Cilium?), gateway (Envoy Gateway/Traefik; ingress-nginx is
  retired), node sizes. The plan must list prerequisites and fallbacks.

## Research notes (in [research/](research/))
- research/map-core-runtime.md      state inventory, cross-trip coupling, agent<->operator protocol, fat vs thin agent
- research/map-browser-flow.md      site.py flow, what is reusable in-pod, cart/close/lost semantics
- research/map-api-surface.md       server.py/views.py/notify.py; multi-tenancy changes to API + WS hub
- research/map-frontend.md          web/ changes, sizes
- research/map-packaging-ci.md      images split, uv workspace, CI matrix, kind/envtest testing, Helm chart
- research/map-k8s-research.md      kopf vs Go, VAP sketch, RBAC leak paths, TokenReview, gateways, KasmVNC, footprint, egress
- research/map-threat-model.md      threat model, API/reconciler split, VNC origin, credential intake ranking
- research/limits-quotas-admin.md   identity/roles, per-user limits, cluster caps, queueing, storage (SQLite vs CRD)
- research/limits-reaping-lifecycle.md  pod states, reaping rules, hold/VNC limits, PDBs, cold tier, failure handling
- research/limits-browser-lockdown.md   egress proxy allowlist, Chromium policies, kiosk, KasmVNC flags, VNC gating/revocation
- research/billing-guest-mode-credentials.md  guest mode, attended sign-in opt-in, spike plan S1-S10
- research/billing-paid-service-implications.md  ToS findings, alert channels (Pushover/Web Push), identity options, isolation
- (The Stripe billing report was dropped when payments were replaced by a Ko-fi link.)

## Cluster facts (answered by the owner after the designs started; authoritative)
- CNI: Cilium  => CiliumNetworkPolicy toFQDNs (with a DNS L7 rule) is available for browser egress
  allowlisting; an egress proxy becomes optional defence in depth, not required.
- Ingress: Envoy Gateway => Gateway API HTTPRoute + SecurityPolicy extAuth per route; WebSockets on by
  default. Target this; no ingress-nginx. (Superseded in round 2 for the ferry routes: no SecurityPolicy on
  them; the app does its own OIDC.)
- Kubernetes: 1.36 or newer => ValidatingAdmissionPolicy and MutatingAdmissionPolicy GA, user-namespace
  pods (hostUsers: false) GA, WebSocket exec 'create' check, fine-grained kubelet authz GA. No Kyverno
  fallback needed.
- Capacity for browser pods: 8-24 GB => roughly 15-40 headed browsers cluster-wide; default per-user
  route limit around 3; maxBrowsersTotal must be a config value, set after measuring real BC Ferries pages.

## Alerts (owner decision, added later; authoritative)
- Do NOT use Pushover: most users won't have it installed. Remove it as the alert channel.
- (Recommendation) Alerts go out from the control plane through a pluggable notifier interface (today's Notifier protocol,
  notify.py:40-50, is the seam). Default channel: Web Push to the existing PWA (needs a service worker,
  VAPID keys held only by the control plane, per-user push subscriptions; iOS needs Add to Home Screen,
  16.4+). Email alongside it for cart-held alerts and as fallback (SMTP or a transactional provider).
  SMS is a possible later add-on, not in the first version.
- (Recommendation, not an owner decision) Alert links point at the dashboard deep link for the trip
  (which then opens the gated VNC), not at a bearer-token URL.

## Owner's cluster = github.com/mchestr/home-cluster (Flux IaC, local clone github.com/mchestr/home-cluster)
Owner instruction: use this repo for ALL information about the cluster. Survey reports:
research/cluster-deploy-conventions.md, research/cluster-networking.md, research/cluster-identity-security.md,
research/cluster-platform-services.md. Verified facts that the plan MUST reflect:
- Talos v1.13.10, Kubernetes v1.36.5, Cilium 1.20.2 (kubeProxyReplacement, native routing, netkit, bandwidth
  manager on, Hubble on, envoy.enabled false; toFQDNs/DNS proxy never used here yet -> verify live), Envoy
  Gateway 1.9.2, Gateway API v1.6.3. 3 nodes, all control-plane, 96 GB each, workloads scheduled on them.
- Gateways: envoy-internal (LAN/WireGuard only; gateway-wide Authelia ext-auth -> Remote-User/Groups/Email/Name)
  and envoy-external (internet via Cloudflare tunnel; NO auth; repo convention: external apps do their own auth,
  e.g. OIDC via Authelia). Invitees are off-LAN => dashboard on envoy-external with app-native OIDC (Authelia is
  already an OIDC provider; clients use PKCE; groups claim available). Trusted headers only as an optional
  LAN mode. This is how the IdP stays replaceable: plain OIDC (issuer/client/claims are config).
- Authelia session cookie is scoped to chestr.dev (and mikeflix.lol): any VNC host under chestr.dev receives it
  -> strip Cookie/Authorization on VNC routes (RequestHeaderModifier) or use a separate registrable domain.
- Certs/tunnel/DNS: only single-label *.chestr.dev is covered; external-dns and gatus-sidecar react to every
  HTTPRoute -> prefer ONE fixed VNC host (e.g. ferry-checkout.chestr.dev) with per-trip PATHS; per-trip routes must
  carry external-dns 'controller: none' and gatus 'enabled: false' annotations. Gateway https listeners accept
  routes from ALL namespaces for ANY hostname -> an admission policy must constrain operator-created HTTPRoutes.
- Cross-node Envoy->pod traffic is SNATed to node IPs which Cilium labels 'kube-apiserver'; existing policies
  allow fromEntities kube-apiserver. NetworkPolicy cannot prove a request came from the gateway; API-server
  pods/proxy also arrives that way -> no pods/proxy/services/proxy RBAC, and an in-pod identity/authorization
  check on VNC is load-bearing.
- Namespaces: new 'ferry-watch' namespace for the control plane (standard components/common). Browser pods in
  a SEPARATE namespace (e.g. 'ferry-watch-browsers') created WITHOUT components/common (which injects Flux
  GitHub token, registry pull creds and cluster-secrets) and labelled PSA restricted. Never 'default' (Authelia's
  Dragonfly session store is reachable from any pod there; privileged pods run there).
- No admission policy, PSA labels, ResourceQuota, LimitRange, PriorityClass, default-deny NetworkPolicy,
  RuntimeClass/gVisor, or user-namespace sysctls exist today: all new. Talos kernel args: apparmor=0,
  security=none, mitigations=off (owner must accept or change for multi-tenant Chromium). User namespaces /
  gVisor need Talos machine-config or schematic changes applied per node (not Flux).
- Storage: Postgres via components/cnpg on the shared postgres17 CNPG cluster (30-day PITR to R2 + pg_dumps)
  rather than SQLite. The cnpg component puts the shared superuser password Secret in the app namespace ->
  the reconciler must NOT be able to create pods in the control-plane namespace.
- Secrets: ClusterSecretStore 'onepassword' is unrestricted and writable -> no external-secrets.io RBAC for the
  operator; app secrets (OIDC client secret, VAPID keys, session key) in 1Password item 'ferry-watch' via a
  Flux-managed ExternalSecret.
- Logs: Fluent Bit ships every pod's logs to VictoriaLogs (unauthenticated in-cluster) -> exclude the browser
  namespace (Exclude_Path) and deny egress from both ferry namespaces to observability.
- Alerts/ops: Alertmanager routes everything to Pushover for the OWNER (ops alerts; separate from user alerts);
  browser churn would trip OomKilled/KubePodNotReady etc. -> silence/blackhole for the browser namespace;
  descheduler can evict browser pods -> exclude the browser namespace. Gatus/homepage annotations off on
  per-trip routes.
- Email: in-cluster smtp-relay (maddy -> AWS SES) at smtp-relay.default:2525, @chestr.dev senders, its
  NetworkPolicy must be extended for the control plane; browser pods must never reach it. SES sandbox status
  unknown (needed to email invitees).
- Identity/invites: Authelia 4.39 + lldap (users created by hand in an internal-only UI). OIDC clients have no
  group-restricted authorization_policies: before inviting outsiders into lldap, restrict existing clients
  (Immich, Windmill are internet-facing). Invitees have no remote self-service password path (lldap reset link
  is LAN-only, Authelia reset disabled). Never give the app lldap admin credentials.
- Cloudflare: Rocket Loader + email obfuscation zone-wide (disable for ferry hosts); geo-block except CA/US/CN.
- Packaging convention: owner consumes third-party operators as OCI Helm charts (OCIRepository + HelmRelease);
  image refs as tag@digest with Renovate; Flux patches HelmReleases (CRDs CreateReplace). The app should
  publish its own OCI chart; cluster-specific bits (Cilium FQDN policy, hostnames, quotas, Authelia client)
  live in home-cluster.
- Current deployment: kubernetes/apps/default/ferry-watch (app-template, ferry.chestr.dev on envoy-internal,
  VNC same-origin /vnc, 1Password items 'ferry-watch' + 'pushover', VolSync PVC). Migration must cut over.

## Per-user browser limit (owner decision, added during review; authoritative)
- Owner: "We should limit (should be configurable by admin) users to 1 browser/trip lookup at a time."
  => Default per-user maxActiveBrowsers = 1 (one running browser pod / one actively watched trip at a time),
  admin-configurable as a global default and per-user override (still clamped by a GitOps ceiling).
  Interpretation adopted (state it in the plan): a user may store more than one trip (default stored limit
  ~3) but only `maxActiveBrowsers` of them are watched at once; enabling another trip asks the user to pause
  the current one (or the admin raises the limit). Capacity maths change accordingly: with 1 browser per
  user, 8-24 GB supports roughly 7-23 concurrent users, not 2-7.
- Owner: "VNC only available once it's checked out seems fine but how will routing work if multiple users
  have a checkout at the same time? We need to route each user to their own pod."
  => The plan must explain per-user routing explicitly and simply: one static host, a per-trip/per-pod path,
  vnc-edge proxying that path to that pod's own DNS name (headless Service + pod hostname/subdomain), and
  the pod admitting only a ticket bound to its own owner/trip/cart. Concurrent checkouts are independent
  paths to independent pods; guessing another path yields nothing without that user's ticket.

## Owner answers to the plan's open questions (round 2; authoritative)

- **Guest checkout (Q3/Q3b): accepted "for now".** Guest checkout is the launch mode, including for the
  owner's own trips. Owner asked: "Users can always sign-in on the browser?" Answer recorded in the plan
  (corrected after review): probably not during checkout, and advised against. bcferries.com, including
  `/login`, is on the allowlist, so a user can try, but VNC exists only while a guest cart is held and the
  owner has observed that BC Ferries' login page won't accept a login while a guest cart exists
  ([top-level README](../../README.md) "Things to know about BC Ferries"; `site.py:288-290`), so a sign-in
  will most likely fail; whether trying affects the cart is unverified. A password typed there would also
  cross the plaintext VNC path into an unsandboxed Chromium. The plan does not rely on it. Nothing persists
  in the pod after it exits, and the password manager stays off. (Superseded after 0.6.3; see "Signing in
  to BC Ferries at checkout" below.)
- **No live-site spike for guest checkout (Q2 = no).** "No spike for now, assume it works and if not we
  will adjust later, don't make any one way door decisions on this." => W0 is no longer gated on S1/S2/S2b/
  S5/S6/S7. Guest checkout is assumed to work. The plan must stay reversible: keep `SiteSession.login()` and
  `Credentials` behind a disabled flag rather than deleting them; keep the data model and protocol able to
  express an account/login mode later; detect checkout failure in production; keep the fleet-wide
  `autoCart` kill switch (alert-only) as the adjustment. Fixtures (cart, review, confirmation, release
  pages) are captured from the owner's real use during M1, as the existing fixtures were.
- **Identity (Q0): "Can we use oidc?" => use OIDC.** This supersedes trusted headers. (Recommendation:
  app-native OIDC against Authelia's existing OIDC provider, confidential client + PKCE, with a cookie-less
  app session (bearer token held by the SPA) so the gateway strips every Cookie header and the owner's
  `chestr.dev`-scoped Authelia cookie never reaches the app, with no gateway-wide Lua and no new domain.)
  The IdP stays replaceable: issuer, client and claim names are config.
- **Sandbox and kernel (Q4): owner asked why it is needed.** Explained in the plan. (Recommendation: make
  the Chromium sandbox / user-namespace Talos change and the `mitigations=off` question optional hardening,
  not a gate on invitees; the owner can make it a gate later.)
- **Pre-cart sign-in window (Q1): no.** VNC stays strictly gated to a held cart. Reversible later.
- **Checkout host name: `ferry-checkout.chestr.dev`** (owner, round 2), replacing `ferry-vnc.chestr.dev`.
  Still a single label under `chestr.dev`, so the existing `*.chestr.dev` certificate and the cloudflared
  `*.chestr.dev` rule cover it. Internal component names (`vnc-edge`, `FW_VNC_*`) may stay as they are.

## Owner answers to the remaining open questions (round 3; authoritative)

1. **VNC first-connection race (R3):** accept for invite-only; the IdP-signed in-pod check stays in the
   hardening backlog.
2. **iOS device lock:** no takeover ticket. iPhone users who lose the device lock check out in Safari or on a
   computer; revisit with data from the owner's iPhone during M1.
3. **After a viewed checkout without a detected confirmation: pause the trip until the user answers "Did you
   book it?"** (no automatic resume). "Yes" archives it, "No" resumes. The viewed sailing is never re-carted.
4. **Trip horizon, polling and quiet hours (owner, replacing the proposed defaults):**
   - Trips may be at most **30 days** ahead (`tripHorizonDays` = 30).
   - "We shouldn't just hammer the BC Ferries website ... the longer away it is the less frequent we poll,
     but closer it is more frequent. We want to be good users of the api."
   - **Warm browser** (the user's one `maxActiveBrowsers` slot) goes to the user's trip with the **nearest
     sailing within 7 days**. It polls on a curve by time until the trip's earliest departure:
     7-3 days out every 5 min; 3 days-24 h every 2 min; 24 h-6 h every 60 s; under 6 h every 30 s
     (±15% jitter; tiers admin-tunable within GitOps ceilings).
   - **Every other saved trip** (up to `maxTrips` = 3, within 30 days) gets a **short check every 6 hours**
     (admin-configurable, up to daily) by a short-lived pod that starts, checks once and exits. These pods
     don't take the user's warm-browser slot; they count against cluster capacity and the start throttle. If a
     short check finds an opening and the user holds no cart, it carts it and that pod stays for checkout.
   - **Quiet hours 23:00-06:00 Pacific: normal polling, alert only** (no cart held overnight).
   - Other per-user defaults as proposed: 4 holds/day, 6 cart attempts/day, 60 VNC min/day; hold clocks
     30 min to open / 30 min session (paused on payment/3DS pages) / 10 min viewer-gone / 60 min hard cap;
     one hold at a time.
5. **Fleet budget 20 checks/min** (ceiling 40), brake on Queue-it detours or 403/429; **keep the desktop UA**.
6. **Talos upgrades:** accept that holds on a drained node are released (with an alert); the runbook pauses
   carting before a Talos bump. Hold protection stays in the backlog.
7. **Invitee sign-in: `one_factor` for the ferry-watch OIDC client** (password only, no 2FA enrolment).
   Other clients keep their policies; the authorization policies restricting the other OIDC clients to their
   own groups still come before anyone new joins lldap.
8. **Admin access:** confirmed. No admin VNC, exec or screenshots in the app; tenants are told the cluster
   admin can technically see everything.
9. **Payment/3DS egress: open public HTTPS egress while a cart is held**, so any bank's 3-D Secure works.
   Private, cluster, node and metadata ranges stay denied. Accepted: a user can browse other sites from the
   owner's IP during their own hold (VNC exists only during holds).
10. **Postgres superuser Secret:** keep the standard `components/cnpg` permanently; accepted residual.
11. **Self-hosted CI runner:** leave as is; stated as a residual to tenants.
12. **Cloudflare geo-block:** unchanged; onboarding tells invitees it only works from Canada/US.

## Signing in to BC Ferries at checkout (owner feedback after 0.6.3; authoritative)

- Owner: "Why do we say checkout as guest? BC ferries has a sign in and checkout option that probably works
  just fine if they want to use it."
  => Signing in to BC Ferries during checkout is the user's choice. The app no longer tells anyone to check
  out as a guest or not to sign in: not in the cart alerts, `/c/<trip>`, the trip card, the checkout landing
  page or the invite email. Carts are still added as a guest, and nothing stores a BC Ferries password.
- The old "it doesn't accept a sign-in while a cart is held" came from the single-tenant watcher's automated
  login, which BC Ferries refused while a guest cart already existed (so that watcher logs in first and carts
  after). It never covered a person signing in at BC Ferries' own checkout step.
- The security note stays: a password typed during checkout travels the same plaintext path as the card
  number (Cloudflare, cloudflared, Envoy, vnc-edge) into an unsandboxed Chromium, and the BC Ferries session
  it creates outlives the pod. A sign-in the user chooses is recorded as `checkout_login_seen` with
  `forced: false` and alerts nobody.
