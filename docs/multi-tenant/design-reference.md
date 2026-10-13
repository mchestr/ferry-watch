# Ferry Watch multi-tenant: design reference

Status: revised design reference for the owner, after five adversarial reviews of the draft (security red
team, cluster feasibility, product/UX, delivery/scope, completeness). Appendix D lists every change and why.
The owner's decisions in [decisions.md](decisions.md) are fixed; this document states what each one costs.
Code refs are `file:line` in this repo at commit 1cdfca9. Cluster refs are `path:line` in
`mchestr/home-cluster` at commit 0bff2f8 (paths under `kubernetes/apps/` unless stated). Research refs are
`[R:name]` for [research/&lt;name&gt;.md](research/). Upstream facts checked during the review are cited by file.

---

## 1. Summary

### What we are building and why

Ferry Watch today is one container (`Dockerfile`, `entrypoint.sh`). In it, one Python process runs the
dashboard, the watch loop for every trip, a single shared Chromium and the alerting, and KasmVNC shows that
Chromium to whoever reaches `/vnc`. That works for one person. It does not isolate tenants. Every trip shares
one unsandboxed Chromium (`site.py:154-157`, `site.py:167-176`), the BC Ferries password sits in the same
process (`settings.py:40-42`), and the VNC view is always open and shared (`entrypoint.sh:20-26`).

We will run one headed-browser pod per actively watched trip on the owner's cluster (Talos 1.13.10,
Kubernetes 1.36.5, Cilium 1.20.2, Envoy Gateway 1.9.2). A small operator decides which pods exist and relays
typed status between pods and users. Alerts link users to a gated VNC view of their own trip's browser, and
only while that browser holds a cart. Access is invite-only, through Authelia at the gateway with trusted
headers (owner decision 7), with per-user limits and an admin page. The single-container mode is retired
(owner decision 5).

### Terms, and which part is "the operator"

The owner asked for "an operator to spin up a headed browser for each route we are watching … and no way to
get secrets out of them from the main operator pod". This is how those words map onto the design:

| Owner's words | In this plan | What it is |
|---|---|---|
| "the operator", "the main operator pod" | **`ferry-control`** | One Deployment. Its *fleet loop* creates and deletes browser pods; its *hub endpoint* is the only thing browser pods talk to. |
| "headed browser for each route" | **browser pod** `fw-<trip>-g<gen>` | One pod per *actively watched trip*: Chromium, Xvnc, and the **agent** (today's watcher, run for one trip). |
| "watched route" | **trip** | Route + date + party: today's `Trip` (`models.py:204-258`). A round trip is two trips. The owner's "number of watched routes per user" is `maxTrips` (stored) and `maxActiveBrowsers` (watched at once, default 1), §8.2. |
| "link alerts to … the vnc" | **`/c/<trip>`** on the dashboard, then **`vnc-edge`** | The dashboard deep link opens checkout; vnc-edge is a static nginx that serves the KasmVNC client and forwards WebSocket upgrades to the pod. |
| "the dashboard" | **`ferry-web`** | Internet-facing UI and API. Mints VNC tickets. Has no Kubernetes rights and no route to pods. |

**What the operator (`ferry-control`) can do:** create and delete browser pods of exactly one admitted shape
in one namespace; check pod identities with TokenReview; read the nodes list; receive typed status from
agents (phase, sailings, cart summary, error *classes*); send typed commands (`check_now`, `release`,
`dismiss`, `drain`, `vnc_revoke`); read and write its own database tables.

**What it cannot do:** read any Secret; exec, attach, port-forward, read logs of, or proxy to any pod; see
page content, screenshots, keystrokes, cookies or the guest cart session; steer the browser to a URL or run
JavaScript in it; mint a VNC ticket; be reached from the internet. These are enforced by RBAC (§7.3), the
agent protocol (§5.3) and admission (§7.5), each tested in CI (§11.5).

### How each user reaches their own pod

See §3.3. In one line: one static host `ferry-vnc.chestr.dev`, one path per pod
(`/t/<trip>/g/<gen>/`), vnc-edge proxies that path to that pod's own DNS name, and the pod admits only a
ticket bound to its own owner, trip, pod and cart. Two people checking out at once use two different paths to
two different pods.

### The load-bearing ideas

1. **No BC Ferries secret exists anywhere by default.** Guest carting already works; only the credential
   gate at `watcher.py:459`/`:562` blocks it. Guest checkout is probable but **must be confirmed** by spikes
   S2 and S2b before carting ships. A pod holds only a guest cart cookie, plus what the user types over VNC
   during one hold, and is destroyed after any viewed hold.
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
6. **Egress is an allowlist enforced below the browser.** A Cilium `toFQDNs` policy with a DNS L7 rule lets
   browser pods reach only bcferries.com, Queue-it, the learned payment and 3-D Secure (3DS) hosts, and
   `ferry-control`. Chromium runs in kiosk mode with managed policies. Startup self-tests fail closed.
7. **The shared home IP is budgeted where neither the control plane nor an honest agent can bypass it.**
   `ferry-control` stretches intervals fairly across pods; each agent enforces them locally with a compiled
   30 s floor and slows down (never speeds up) when it loses the operator. A persisted brake and a markup
   breaker protect the IP. A pod running attacker code (A3) can bypass all of this toward the allowlisted
   hosts; that residual is R13 (§4.6).
8. **Isolation is proven before anyone outside the household is invited.** CI blocks on a VAP leak-vector
   denial matrix and `kubectl auth can-i` negatives against a real apiserver. The egress matrix runs as a
   post-deploy smoke test on the home cluster, the only place Cilium's FQDN and SNAT behaviour can be proven;
   the operator stays in maintenance mode until it passes.
9. **Identity follows owner decision 7: Authelia plus trusted headers, behind a replaceable interface.** The
   dashboard route on envoy-external gets an Authelia ext-auth SecurityPolicy; the app trusts only the
   gateway-set `Remote-User`. Replacing the IdP later is a documented runbook (§6.2.6).

### What it costs

- **Deployed components: 3** (`ferry-web` and `ferry-control` from one `ferry-watch-control` image, plus
  `vnc-edge`) plus the browser pods. Postgres comes from the shared CNPG `postgres17`. No CRDs.
- **Memory:** about 1-1.5 GiB per watched trip including the in-memory `/tmp` and `/dev/shm`, against 35-80 MB
  per extra context today [R:map-k8s-research §g]. At one browser per user, the owner's 8-24 GiB budget
  serves 4-23 concurrent users depending on measured pod size (§8.4).
- **Effort:** about 18-22 focused weeks for one developer to reach invitees outside the household, of which
  the owner is on the new stack after about 13.5-16.5. The arithmetic is in §12.3; the draft's 12 weeks was
  below both source designs.
- **What the owner gives up:** logged-in checkout with saved cards and account-only fares (guest only, Q3b);
  `--no-sandbox` Chromium until M3; holds released by Talos upgrades in v1 (§7.8).

### Which design we started from, and why

We started from **ops-scale-fairness**, the only one of the four designs written against the verified
cluster facts (shared CNPG Postgres, envoy-external, one single-label VNC host, Cookie stripping, no Pushover
for users, the browser namespace outside `components/common`, Fluent Bit/descheduler/Alertmanager
integration), cut to **lean-home-cluster's scope** (static VNC router, static interval stretching, no Standby
tier, SIGTERM drain, alert-only fallback), with security controls grafted from security-maximal and
k8s-native. The review then:
- merged hub and fleet into one operator process (as the owner described it) and moved identity to
  Authelia trusted headers, per owner decision 7;
- dropped the anchor ConfigMap and hold PDBs, which do not fit Talos's drain;
- set one active browser per user (owner decision during review);
- fixed DNS, seccomp, memory accounting, the VNC reconnect path and several checkout-losing UX defaults;
- re-baselined the effort.

---

## 2. Owner decisions and assumptions

### 2.1 Owner decisions: how each is honoured and what it costs

| # | Decision | How this plan honours it | Honest cost |
|---|---|---|---|
| 1 | Operator plus one headed pod per watched route; the operator can't get secrets out | One pinned bare pod per actively watched trip, created only by `ferry-control`, admitted by a VAP that forbids every secret-bearing field. The agent dials out, and its typed protocol has no page-content operations. No control-plane process is in the VNC data path. The VNC data path does cross Cloudflare, Envoy and vnc-edge in plaintext; see §4.3. | About 1-1.5 GiB per trip (§8.4). Every pod start is a fresh form walk and a possible Queue-it wait. The VAP and RBAC must stay correct forever; CI enforces this. |
| 2 | Alerts link to the trip's VNC | Cart alerts link to `https://ferry.chestr.dev/c/<trip>`. That page shows the held sailing and two choices: "Check out on this device" (mints a ticket and opens VNC) or "I'll use my computer". | One extra tap, because a bearer VNC URL in a notification would hand VNC power to whoever sends the notification, and an automatic open would lock checkout to whichever device opened the link first. |
| 3 | VNC only while a cart is held | Enforced in the pod (authoritative), by ferry-web (won't mint a ticket), by ferry-control (deletes the pod at hold cap + 5 min on its own clock), by Xvnc `-MaxConnectionTime`, and by `activeDeadlineSeconds`. | No "watch my browser" view. An opt-in BC Ferries sign-in window would be an exception (open question Q1). |
| 4 | Invite-only; an admin approves each account | Three gates: Authelia lets only lldap group `ferry-watch` reach the dashboard; the app admits only people the admin added on the Users page (§6.2.7); suspension is immediate. | lldap users are still created by hand on a LAN-only UI, with no remote password reset [R:cluster-identity-security "lldap"]. Each invitee's email must be deliverable through SES before they can enrol a second factor (§2.3). |
| 5 | Operator-only | `entrypoint.sh` and the standalone image are retired. Local dev uses docker compose; chart work uses kind. | The owner's `config.json`/`alerted.json` need a one-time import (§12.5). |
| 6 | Ko-fi link, no payments | A `kofiUrl` Helm value validated as `https://ko-fi.com/<name>`, rendered as a plain `<a>`. The CSP (`server.py:232-251`) is unchanged. | None worth noting. |
| 7 | Authelia and trusted headers to keep it simple; easy to replace; don't depend on Authelia | Authelia ext-auth SecurityPolicy on the dashboard route (envoy-external); the app reads `Remote-User` through a `TrustedHeaderProvider` behind an `IdentityProvider` interface. User ids are `hash(provider id, subject)`. No Authelia-only feature is load-bearing. §6.2 compares this with app-native OIDC and gives the replacement runbook. | A missing gateway policy would fail open (mitigated by C29). Session length is Authelia's (3 days idle, or 6 months with "Remember me"). The VNC host cannot use the IdP, so ownership is checked cryptographically in the pod (D7). |
| 8 | Per-user limits on watched routes | `maxTrips` 3 stored, `maxActiveBrowsers` 1 watched at once (owner, during review), both admin-configurable per user and clamped by GitOps ceilings, enforced in five layers (§9). | A small admin Users page and a limits resolver. |
| 9 | Reap unused browsers; no misuse | Disposable pods (`restartPolicy: Never`), recycled after every viewed hold and at about 24 h, plus hold caps, kiosk mode, policies and the egress allowlist. | More pod churn means more Queue-it visits, which the budget (D12) absorbs. |
| Alerts | No Pushover; Web Push plus email | ferry-web sends from a Postgres outbox. Web Push is the default; email carries cart alerts and is the fallback. | A service worker, VAPID handling and SES production access (§2.3). |
| Routing (review) | "Route each user to their own pod" | §3.3: one host, one path per pod, one ticket per owner, trip, pod and cart. | None beyond vnc-edge. |

### 2.2 Cluster facts this plan relies on (verified in home-cluster at 0bff2f8 unless marked upstream)

| Fact | Consequence in this plan |
|---|---|
| Kubernetes 1.36.5: VAP GA (CONNECT matching to be confirmed in S-TR), TokenReview returns pod-name/pod-uid extras, `hostUsers` GA. | VAPs are the admission layer and TokenReview binds agents. If VAP CONNECT doesn't work, exec/attach/portforward rely on RBAC alone (noted). |
| Cilium 1.20.2, native routing, netkit, BPF masquerade, kube-proxy replacement (`kube-system/cilium/app/resources/values.yaml:1-50`). `toFQDNs` and the DNS proxy are unused here. | Egress uses `toFQDNs` plus a DNS L7 rule, verified live in S-CIL. Fallback: today's `toCIDRSet`. |
| Cilium's DNS proxy answers **REFUSED** for names a policy does not allow (upstream `Documentation/security/policy/layer7.rst`, "DNS Refused"); no override here. glibc stops walking the search list on REFUSED and then tries the name as typed (upstream `resolv/res_query.c` search loop). | A short name such as `ferry-hub.ferry-watch.svc` never resolves. Fix: home-cluster sets `dnsProxy.dnsRejectResponseCode: nameError` (no DNS rules exist today, so nothing regresses), and every in-cluster name in config is fully qualified (§7.6). |
| Hubble is on through the HelmRelease values (`kube-system/cilium/app/helmrelease.yaml:28-49`), overriding `values.yaml:33`. | Hubble-based checks in S-CIL and the post-deploy smoke are feasible. The control plane cannot read Hubble (no egress to observability). |
| Cross-node Envoy→pod traffic is SNATed to the `kube-apiserver` entity [R:cluster-networking "Cross-node SNAT quirk"]. | NetworkPolicy cannot prove a request came from the gateway. In-pod checks are cryptographic. No app identity holds `pods/proxy` or `services/proxy`. |
| envoy-external has no auth. envoy-internal has a gateway-wide `SecurityPolicy internal-secure` using Authelia ext-authz with `headersToBackend: Remote-User/Groups/Name/Email` (`networking/envoy-gateway/app/envoy.yaml:122-151`). Authelia's ReferenceGrant admits SecurityPolicies only from `networking` (`default/authelia/app/referencegrant.yaml:5-14`). | Trusted headers on envoy-external need a route-level SecurityPolicy in `ferry-watch` and one more ReferenceGrant entry (§6.2.2). |
| Envoy Gateway runs Lua only if `extensionApis.enableLua: true` (upstream EG v1.9 `tasks/extensibility/lua.md`); it is not set here (`networking/envoy-gateway/app/helmrelease.yaml:14-20`). | The draft's Lua cookie filter would need a gateway-wide change. Under trusted headers it is not needed (§6.2). |
| The EnvoyProxy sets no access log (`envoy.yaml:19-43`), so Envoy Gateway's default stdout access log applies and records request paths. | No secret ever appears in a path or query string (C15). |
| Authelia: session cookie `authelia-home-ops` on `chestr.dev`, 3 d inactivity, 7 d expiry, 6 months with remember-me (`default/authelia/app/resources/configuration.yaml:32-41`); `default_policy: two_factor`, last rule denies `*.chestr.dev` (`:45-104`); 3 failures in 1 h ban for 1 day (`:27-30`); password reset disabled (`:23-24`); every OIDC client is `two_factor` with no `authorization_policies` (`:131-214`). | The ferry rule must precede `:99`. Invitees enrol a second factor through an emailed code (SES). Existing clients are restricted before anyone new joins lldap (hard gate). |
| `*.chestr.dev` reaches envoy-external through a Cloudflare tunnel (`networking/cloudflared/app/resources/config.yaml:5-11`). | Cloudflare's edge and cloudflared see plaintext HTTP and WebSocket frames, including VNC keystrokes (§4.3, R4). |
| Only single-label `*.chestr.dev` is covered by the cert and the tunnel. external-dns (Cloudflare: `--gateway-name=envoy-external`, `networking/external-dns/cloudflare/helmrelease.yaml:31-38`; UniFi: all HTTPRoutes, `unifi/helmrelease.yaml:48`) and gatus (`--auto-httproute`, `observability/gatus/app/helmrelease.yaml:25`) react to every HTTPRoute. HTTPS listeners accept routes from any namespace. | Two static hosts, `ferry.chestr.dev` and `ferry-vnc.chestr.dev`. No app identity writes HTTPRoutes. Before cutover the dashboard runs on `ferry-next.chestr.dev` (§12.5). |
| `components/common` injects the Flux GitHub token, registry credentials and cluster-secrets. `default` has privileged pods and Authelia's Dragonfly. | The browser namespace is created by the chart without `components/common`, and is never `default`. |
| CNPG `postgres17` with 30-day PITR to R2 (`database/cloudnative-pg/cluster/cluster17.yaml`). `components/cnpg` copies the shared superuser password into `${APP}-initdb-secret` in the app namespace (`components/cnpg/externalsecret.yaml:21-23`) and adds a 4-hourly pg_dump CronJob (`components/cnpg/cronjob.yaml`). 15 apps use it; none uses CNPG managed roles. | DB `ferrywatch`. The superuser is used only by the stock `postgres-init` image, never by our image (§6.4). Per-process login roles come from CNPG managed roles. The resident superuser Secret is residual R14 (Q14). |
| talosctl 1.13's drain uses `Force: true` and a 5-minute default timeout; on timeout the upgrade fails without rebooting and leaves the node cordoned (upstream `cmd/talosctl/pkg/talos/nodedrain/nodedrain.go:30,86`; `cmd/talosctl/cmd/talos/upgrade.go:148-162`). tuppr passes `--drain-timeout` only if `policy.drainTimeout` is set (upstream tuppr `internal/controller/talosupgrade/jobs.go:600-601`); here it is not (`system-upgrade/tuppr/upgrades/talos.yaml:10-11`). | Bare pods drain without any controller ownerReference, so there is no anchor ConfigMap. A hold PDB would fail the upgrade rather than delay it, so v1 has none (§7.8). |
| containerd's RuntimeDefault seccomp profile allows `clone` with namespace flags, `unshare` and `setns` only with `CAP_SYS_ADMIN` (upstream `contrib/seccomp/seccomp_default.go:692-718,820-860`); the kubelet applies it by default (`talos/controlplane.yaml:24`); Talos KSPP sets `user.max_user_namespaces=0`. | Chromium's namespace sandbox needs a Localhost seccomp profile **and** the sysctl, both Talos machine-config changes (S-SBX, Q4). |
| descheduler runs LowNodeUtilization with `evictLocalStoragePods: true` (`kube-system/descheduler/app/helmrelease.yaml:30,51`); its default evictor skips pods without ownerReferences (upstream v0.37 `defaultevictor.go:239-240`). | Bare browser pods are skipped by design; the namespace exclusion is a second layer. |
| Fluent Bit ships every pod's logs to VictoriaLogs, unauthenticated in-cluster. Stock alerts page the owner on Pushover. | The browser namespace is excluded from Fluent Bit and its stock alerts are silenced. |
| smtp-relay (maddy to SES) at `smtp-relay.default:2525` admits only Authelia and lldap. | One home-cluster NetworkPolicy rule admits `ferry-web` only. |
| Multus (macvlan `multus-iot`) is installed. | The VAP denies the `k8s.v1.cni.cncf.io/networks` annotation. |
| Talos kernel args are `apparmor=0 security=none mitigations=off`. | Unsandboxed Chromium on these nodes gates non-household invitees (R2, Q4). |
| A self-hosted GitHub runner `home-cluster-runner` has `cluster-admin` and Talos `os:admin` (`actions-runner-system/actions-runner-controller/runners/home-cluster/rbac.yaml:9-25`, `talos/controlplane.yaml:85-88`) and runs a `pull_request` workflow that interpolates PR-derived values into a shell command (`.github/workflows/image-pull.yaml:5-7,95-109`). | A hard gate before non-household invitees (§2.3, R1). |
| The ClientTrafficPolicy trusts XFF from 10.42/16, but pods are on 10.244/16. | Nothing here keys on client IP. Rate limits are per user. |
| Cloudflare geo-blocks every country except CA/US/CN; Rocket Loader and email obfuscation are zone-wide. | Invitees must be in CA/US/CN, including when travelling (Q16). A Cloudflare rule turns both features off for the ferry hosts. |
| The current deployment `default/ferry-watch` reads `BCF_USER`/`BCF_PASS` from 1Password item `ferry-watch` and the `pushover` item (`default/ferry-watch/app/externalsecret.yaml:14-23`); `pushover` is also used by kube-prometheus-stack, gatus, sonarr, radarr and qbittorrent. The Kustomization prunes (`default/ferry-watch/ks.yaml:26`) and includes the VolSync component. | The cutover keeps the legacy app scaled to zero with its PVC until the soak ends, removes only ferry-watch's *reference* to `pushover`, and then deletes the BC Ferries fields (§12.5). |

### 2.3 Prerequisites, with fallbacks if a feature is missing

| Prerequisite | Who | Needed by | Fallback if missing |
|---|---|---|---|
| Cilium `dnsProxy.dnsRejectResponseCode: nameError` | home-cluster | W0 (S-CIL) | Add `*.svc.cluster.local`-style patterns to every DNS rule and keep fully qualified names. |
| Cilium DNS proxy works for `toFQDNs` (S-CIL) | spike | M1 | `toCIDRSet` 0.0.0.0/0 minus private, CGNAT and link-local ranges (today's rule), plus the Chromium URL allowlist. |
| Authelia ext-auth for the dashboard: chart-rendered SecurityPolicy, a ReferenceGrant entry for `ferry-watch`, an Authelia `access_control` rule for `ferry.chestr.dev`/`ferry-next.chestr.dev` → `group:ferry-watch` placed before `configuration.yaml:99` | chart + home-cluster | M1 | App-native OIDC (Q0 alternative), which then needs `enableLua` or a separate domain (§6.2.3). |
| Authelia OIDC `authorization_policies` restricting Immich, Windmill, Grafana, Autobrr and Paperless to their groups; lldap group `ferry-watch` | home-cluster | **before any new person is added to lldap** | none (hard gate on M2) |
| Each invitee's email deliverable through SES (production access, or the invitee verified as an SES identity first) | owner | before each invitee is created | none: Authelia's second-factor enrolment emails a one-time code [R:cluster-identity-security]. |
| CNPG managed roles `ferrywatch_web` and `ferrywatch_control` on `postgres17`, with password ExternalSecrets in `database` | home-cluster | M1 | One login role for both processes, as a stated weakening of C24 until fixed. |
| 1Password fields. Item `ferry-watch`: VNC ticket Ed25519 private key, VAPID private key. Item `cloudnative-pg`: `ferrywatch_postgres_{username,password}`, `ferrywatch_web_password`, `ferrywatch_control_password`. | owner | M1 | none |
| Talos `user.max_user_namespaces` sysctl **and** a `machine.seccompProfiles` Localhost profile, applied per node (S-SBX) | owner (`task talos:apply-node`) | M3 | `--no-sandbox` stays; invitees limited to the household (Q4). |
| `home-cluster-runner` restricted: push/schedule events or same-repo PRs only, approval for outside contributors, matrix values via `env:` | owner (home-cluster) | M3 | none (hard gate before non-household invitees) |
| Measured browser memory (S-MEM) | spike | M1 | Start at `browserMemoryBudget: 12Gi`, `podMemory: 1.5Gi`, so `maxBrowsersTotal = 7` (§8.4). |
| Multi-issuer 3DS learning run (S-3DS) | owner + household | M3 | Non-household invitees wait; or Q13 option (b). |

---
## 3. Architecture

### 3.1 Components

| Component | Kind / image | Reachable from | Kubernetes RBAC | Secrets it holds | Job |
|---|---|---|---|---|---|
| `ferry-web` | Deployment, 1 replica, Recreate; `ferry-watch-control web` | envoy-external → `ferry.chestr.dev` on :8765 (behind Authelia ext-auth); Prometheus on :9090 | **none** (no SA token mounted) | VNC ticket signing key, VAPID key, `ferrywatch_web` DB password | dashboard, user and admin API, per-user WebSocket hub, VNC ticket minting, notification outbox sender (Web Push, email) |
| `ferry-control` (**the operator**) | Deployment, 1 replica, Recreate; `ferry-watch-control control` | browser pods only, on :8766; Prometheus on :9090 | ClusterRole `tokenreviews` create; Role in `ferry-watch-browsers`: `pods` create/delete/get/list/watch; ClusterRole `nodes` get/list/watch | `ferrywatch_control` DB password | **hub endpoint:** agent WebSocket, TokenReview binding, agent state to Postgres, commands and `configure` to agents, budget and brake. **fleet loop:** admission and queue, reconcile pods, timers, reaper, cordon handling, maintenance gate |
| `vnc-edge` | Deployment, 2 replicas; `ferry-watch-vnc-edge` (nginx-unprivileged, the stock KasmVNC web client, a landing page and a small WebSocket shim) | envoy-external → `ferry-vnc.chestr.dev` on :8080 | none | none | serves the static client; proxies only WS upgrades on `/t/<trip>/g/<gen>/ws` to that pod |
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
  ferry.chestr.dev (static HTTPRoute)                    ferry-vnc.chestr.dev (static HTTPRoute, no gateway auth)
  SecurityPolicy ext-auth -> Authelia (cookie checked)   RequestHeaderModifier removes Cookie, Authorization
  route then removes Cookie, Authorization;
  backend gets Remote-User/Groups/Email/Name
          |                                                            |
 +- ns ferry-watch (components/common, components/cnpg) ------------------------------------+
 |        v                                                            v                    |
 |  ferry-web :8765  ---- no k8s RBAC ----                     vnc-edge :8080 (nginx)       |
 |   TrustedHeaderProvider | per-user API + WS | admin          stock KasmVNC client + shim  |
 |   ticket minter (Ed25519) | outbox -> Web Push, email        proxies WS upgrades only     |
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
 |  Pod fw-<trip>-g<gen>  (one per actively watched trip; restartPolicy Never; deadline 26 h)|
 |   tini -- ferry-watch-agent (supervisor)                                                  |
 |     |- Watcher(1 trip) + SiteSession + hub client + local budget bucket                   |
 |     |- VNC gate :6080 (ticket, owner, cart, Origin, first-viewer lock) == unix socket ==> |
 |     |- Xvnc (RFB on /tmp/kasm/rfb.sock only, mode 0600; no TCP), matchbox,                |
 |     |  Chromium (kiosk, managed policies)                                                 |
 |     `- probes :8080 (/livez, /readyz)                                                     |
 |  Service browsers (headless), params ConfigMap, quotas, LimitRange                        |
 +-------------------------------+-----------------------------------------------------------+
                                 | egress: kube-dns (L7 DNS allowlist), ferry-control:8766,
                                 v toFQDNs bcferries.com / *.bcferries.com / *.queue-it.net / 3DS hosts :443
                      BC Ferries, Queue-it, payment/3DS  <-- one home WAN IP for everything
```

### 3.3 How each user reaches their own pod

Five facts make per-user routing work, including when several people check out at once:

1. **One host.** Every checkout uses `https://ferry-vnc.chestr.dev`. There is one static HTTPRoute, created by
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
| TB1 | Internet → ferry-web | Authelia ext-auth at Envoy (cookie session, `two_factor`, group `ferry-watch`); the route removes Cookie and Authorization; the app trusts only `Remote-User` (C29). The existing Origin and JSON checks on mutations (`server.py:220-229`). |
| TB2 | Internet → vnc-edge → pod | Static routing. nginx forwards only upgrades matching the path regex. The in-pod gate verifies the ticket and the first-viewer lock. Origin is pinned. Cookie and Authorization are removed. |
| TB3 | Browser pod → ferry-control | Projected token (audience `ferry-watch-agent`, 600 s) checked by TokenReview and bound to the `browser_pods` row written before the pod was created. A strict schema with size caps. Treated as forgeable for its own trip only. |
| TB4 | ferry-web ↔ ferry-control | Only Postgres rows and NOTIFY, under per-role GRANTs, row-level security (RLS) and triggers. No RPC. |
| TB5 | ferry-control → kube-apiserver | Role scoped to `pods` in one namespace. A VAP pins the pod shape. PSA `restricted`. |
| TB6 | Browser → world | CiliumNetworkPolicy (FQDN plus DNS L7), Chromium policies, kiosk mode, a page watchdog. |
| — | Cluster admin and anything with cluster-admin (the owner; `openclaw`; `home-cluster-runner`), Cloudflare | Out of scope and fully trusted. Tenants are told so on the invite email and the dashboard's About page. |

### 3.5 Data flow (a): adding and enabling a trip

1. The user submits the trip form. `POST /api/trips` passes Authelia at Envoy, then the same-origin and JSON
   checks (`server.py:220-229`). ferry-web validates with `Trip.from_json` (`models.py:204-258`).
2. Inside one transaction ferry-web:
   - runs `SELECT … FROM users WHERE id=$me FOR UPDATE`;
   - counts the user's trips that are neither past nor archived, against `maxTrips`;
   - if the new trip is enabled and inside `startWindowDays`, counts the user's trips that currently want a
     pod against `maxActiveBrowsers`;
   - checks the date horizon;
   - `INSERT trips(id = 16 random hex, user_id = $me, rev = 1, spec)`, an event row and an audit row;
   - runs `NOTIFY fleet_wake`.

   A limit returns 409 `{field, limit, usage}`; for `maxActiveBrowsers` it also names the trip to pause
   ("Pause *Tsawwassen → Swartz Bay, Aug 1* to watch this one").
3. The fleet loop wakes and recomputes the desired state (§6.6):
   - beyond `startWindowDays` the trip is `Dormant` (badge "Starts watching Jul 2") and gets no pod;
   - a Dormant trip whose window opens while the user is at `maxActiveBrowsers` shows "Waiting: your
     1-trip limit";
   - otherwise the cluster cap and the start throttle apply, and a trip that doesn't fit is
     `Queued(position)` ("Waiting for a free browser (#2)");
   - if admitted, the loop runs `INSERT browser_pods(name='fw-<trip>-g<n>', …)`, creates the Pod (checked
     by the VAP), records the returned uid, and sets the phase to `Starting`.
4. In the pod, tini starts the agent supervisor, which:
   - starts Xvnc on a unix socket only, then matchbox;
   - launches Chromium in kiosk mode under policy;
   - runs the fail-closed self-tests (§5.6);
   - dials `ferry-control` with its projected token.
5. ferry-control runs TokenReview and binds the socket to `(owner, trip, gen)` from `browser_pods`. It sends
   `configure` with the spec, interval, cart and hold policy, `alerted[]`/`offered[]` from `alert_memory`,
   and `vnc_allowed`.
6. The agent walks the booking form, possibly through Queue-it, and sends `state{phase: watching, sailings}`.
   ferry-control upserts `trip_state` and runs `NOTIFY trip_changed`. ferry-web pushes a new per-user
   snapshot to that user's dashboard sockets.
7. If no first check arrives within 15 min (`CHECK_TIMEOUT_SECONDS`, `watcher.py:48`), the fleet loop marks
   the pod Failed and recycles it with back-off.

### 3.6 Data flow (b): a sailing opens, through to recycle

Numbers below are the defaults from the canonical table in §8.2.

1. **Opening.** A refresh finds a new departure in the trip's window (`watcher.py:441`). The agent carts it
   only if all hold: `configure.auto_cart` (daily holds and attempts left, VNC minutes left ≥ session length,
   outside the user's quiet hours), the compiled per-pod limits, and **a control ack within the last 30 s**
   (so nobody is carted while alerts can't be delivered). Otherwise it records the departure as *offered*
   and sends a `bookable` alert; an offered departure may still be carted later, once, if it is still open
   when the limit clears (§5.2). When carting, the agent:
   - runs `add_to_cart` (≤40 s, `CART_BUDGET_SECONDS`, `watcher.py:50`);
   - on READY, starts its local hard-cap clock;
   - sends `alert{cart_held, depart, fare, price}` and `state`, buffered until acked.
2. **Persist and alert.** ferry-control validates the message and upserts `trip_state.cart`. If
   `hold_ready_at` is unset for this `cart_id`, it sets it from its own clock. In one transaction it inserts
   `notifications(kind=cart_held, dedup_key=trip:cart_id:cart_held)` and increments `usage_ledger`, then
   acks. The agent starts its unclaimed-hold clock at the ack. While unclaimed, the agent checks every few
   minutes that the cart is still alive on the site (S6 decides whether that check extends BC Ferries' timer;
   if it would extend it past policy, the check is skipped). A cart the site has already dropped sends
   `cart_expired{by_site}` at once and never counts as viewed.
3. **Deliver.** ferry-web's outbox worker gets the NOTIFY and renders the typed kind from its own templates
   (§6.7): "Held for you: 7:00 am Tsawwassen → Swartz Bay, Fri Aug 1. Check out as a guest by 3:42 pm …".
   The link is built from its own template: `https://ferry.chestr.dev/c/<trip>`. It sends Web Push (urgency
   high, TTL 1800 s) and email.
4. **Choose a device.** The user taps the notification. The service worker builds the URL from the trip id
   against a constant origin. If the Authelia session has lapsed, Envoy redirects through
   `auth.chestr.dev` and back. `/c/<trip>` shows the sailing, the deadline, and three choices, with the copy
   "Checkout will be locked to the device you choose":
   - **Check out on this device** POSTs `/api/trips/<trip>/vnc`. ferry-web checks that the user owns the
     trip, that `trip_state` shows a READY cart with this `cart_id` in the current pod, and that the user
     has made fewer than 10 mints this minute. It mints a ticket (§5.5), writes `vnc_mints` and audit rows,
     and returns `https://ferry-vnc.chestr.dev/t/<trip>/g/<gen>/#t=<ticket>`. The page navigates there.
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
6. **Checkout.** The user pays on the real bcferries.com page through the allowlisted payment and 3DS hosts.
   - The gate pings every 20 s, which also keeps Cloudflare's idle timer alive. A missed pong drops the
     viewer and starts the viewer-gone clock (10 min).
   - When the phone sleeps or the user switches to their bank app, the client reconnects with only
     `fw.n1.<N>`; a reconnect with the locked `N` replaces any stale socket.
   - While any frame is on a payment or 3DS host, or the payment step has been submitted, the session and
     viewer-gone clocks pause; only the hard cap runs.
   - If a frame is blocked (an issuer's 3DS host missing from the allowlist), the agent sends
     `event{checkout_host_blocked}` (no hostname); the user sees "Your bank's verification page was blocked;
     use Release and book it myself" and the admin is alerted.
7. **End.** The hold ends on whichever comes first: BC Ferries' confirmation page appearing (a booking);
   the user's "Done" or "Release" in the dashboard
   (web → `agent_commands` → control → `command{release}`); checkout-session expiry or viewer-gone; the hard
   cap (60 min from READY); the window closing. The agent closes the socket and empties the cart, then
   verifies it is empty, **except** when payment is in progress: then it closes the socket without
   navigating and leaves BC Ferries' own timer to clear the cart. It sends
   `bye{viewer_was_connected, outcome, empty_verified}` and exits 0. `outcome` is `booked` when the agent saw
   BC Ferries' confirmation step (selector from S2b; no booking reference crosses the protocol).
8. **Recycle.** The fleet loop records the pod Succeeded and deletes it.
   - `outcome = booked`: ferry-control archives the trip and inserts a "Booked!" notification (with the Ko-fi
     link on the screen it opens).
   - Otherwise ferry-control sets `paused_reason='after_checkout'` and inserts a `booked_it` notification
     ("Did you book it?"). "Yes" archives the trip; "No, keep watching" or **no answer within 20 min**
     resumes watching with a new pod `fw-<trip>-g<n+1>`. The trip keeps its slot at the head of the queue for
     30 min.
   - The viewed departure stays in `alerted`, so it is never re-carted (C22).
9. **Unviewed expiry.** If no viewer ever connected, the cart is emptied and verified after 30 min
   (reminder at 15). The user gets `cart_expired`, with "Still bookable: book on bcferries.com" when the last
   check saw it open, and the pod **resumes watching in place**, which saves a Queue-it pass.

### 3.7 Who is authoritative for what

| Fact | Authority | Mirror / consumer |
|---|---|---|
| Trip spec, rev, enabled, paused reason | Postgres `trips` (ferry-web; ferry-control may set `paused_reason` and archive only) | agent via `configure` |
| Whether a pod should exist; queue position | ferry-control fleet loop (`trip_runtime`, `browser_pods`) | dashboard |
| Cart state, hold timers, viewer, VNC gate | the agent | `trip_state` (ferry-control) |
| Hold backstop clock | ferry-control writes `hold_ready_at` with its own clock and enforces it | — |
| Interval and brake | ferry-control (computes; brake persisted in `fleet_state`) | agent local bucket |
| Alert memory (`alerted`, `offered`) | the agent decides; ferry-control persists it in `alert_memory` | `configure` on reconnect |
| Who may see the VNC view | **the pod**: ticket, `FW_OWNER`, cart, lock | ferry-web only decides whether to mint |
| Who the user is | Authelia via Envoy (`Remote-User`) | ferry-web maps it to a uid (§6.2.5) |

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
  (`--no-sandbox` until S-SBX), or A2 working through VNC.
- **A4** controls ferry-control through its only input, the agent protocol (that is, A3 escalating).

Out of scope: the cluster admin, node root, GitOps, backups, the registry signer, `openclaw` and
`home-cluster-runner` (cluster-admin), Cloudflare, and BC Ferries' own page supply chain.

### 4.3 Who can see each secret

"Yes" means the component handles the value in plaintext during normal operation. The cluster admin can see
everything (R1).

| Value | Browser pod | ferry-control (operator) | ferry-web | vnc-edge | Envoy | Cloudflare edge + cloudflared | Postgres | Logs (VictoriaLogs) |
|---|---|---|---|---|---|---|---|---|
| Card number, CVV, 3DS codes, guest contact details (typed over VNC) | yes (own tenant only) | **no** | **no** | yes (RFB is plaintext) | yes | yes | no | no |
| BC Ferries guest cart cookie | yes (own) | no | no | no | no | no | no | no |
| VNC ticket (60 s, single use) | verifies it | no | mints it | yes (subprotocol header) | yes (header, not logged) | yes | `jti` only | no |
| Resume nonce `N` | stores `sha256(N)` | no | **no** | yes | yes | yes | no | no |
| Authelia session cookie | no | no | no (removed at the route) | no (removed) | yes | yes | no (in Dragonfly) | no |
| Identity headers `Remote-*` | no | no | yes | no | yes (sets them) | no | uid only | no |
| Itineraries, emails | own trip only | yes | yes | no | yes | yes | yes | no |
| Ticket signing key, VAPID key | public key only | no | yes | no | no | no | no | no |

Two rows matter most. The operator sees no card data and no checkout screen, which is the owner's
requirement. Cloudflare, cloudflared, Envoy and vnc-edge do see checkout keystrokes, because RFB is plaintext
once TLS ends; tenants are told (R4).

### 4.4 Controls

"From" is the milestone where the control first ships (§12.2).

| # | Control | Threat it stops | Where enforced | From |
|---|---|---|---|---|
| C1 | Guest mode only. No API accepts BC Ferries credentials. The VAP forbids secret volumes, `envFrom` and `valueFrom`. | A password or account cookie leaking from anywhere | agent build (`credentials=None`), API, VAP `fw-browser-pod` | M1 |
| C2 | ferry-web has no Kubernetes token and no RBAC | A1 shaping pods, reading Secrets, exec | chart: `automountServiceAccountToken: false`; helm-unittest | M1 |
| C3 | Only ferry-control may create pods in the browser namespace; it has no internet ingress and its only input is the strict agent protocol from browser pods | Internet-reachable code holding `pods/create`. **Changed from the draft:** a parser exploit by A3/A4 now reaches `pods/create` too (still VAP-pinned; see §4.5). | VAP `fw-browser-pod` (creator check); NetworkPolicy; RBAC | M1 |
| C4 | Exact pod template: digest-pinned image, a fixed env list with literal values, fixed volumes, no command/args/hooks/exec probes/extra containers/hostAliases/dnsConfig/nodeName/runtimeClass/Multus annotation/ownerReferences | "create pods = read Secrets"; `DEBUG=pw:*`, `SSLKEYLOGFILE`, `NODE_OPTIONS`, base-URL redirection, sidecars | VAP `fw-browser-pod` with GitOps params; PSA `restricted` | M1 |
| C5 | Pods are immutable: no UPDATE, ephemeral containers, exec, attach or port-forward by any app identity | Image swap or interactive access to a live pod | RBAC + CI `can-i` (M1); VAP `fw-browser-pod-lock` as a second fence (M3) | M1/M3 |
| C6 | No app identity holds `pods/proxy`, `services/proxy`, `nodes/proxy`, `pods/log`, any Secret verb, any workload-controller create, or `external-secrets.io/*` | API-server proxy paths that arrive SNATed as `kube-apiserver`; reading the 1Password vault through ESO | RBAC; helm-unittest forbidden list; `can-i` negatives in CI (M1); VAP `fw-ns-objects` as a second fence (M3) | M1/M3 |
| C7 | The agent dials out with a typed protocol that has no navigate, URL, JS, screenshot, cookie, storage, base_url or log-level operation | The operator extracting page content or steering the browser | `ferry_watch_core.protocol` (strict msgspec, unknown fields rejected); a CI test walks the schema | M1 |
| C8 | Projected token (audience `ferry-watch-agent`, 600 s), TokenReview, binding to the `browser_pods` row the fleet loop wrote before creating the pod; every message's trip must match | A pod impersonating another pod or trip; a token replayed against the API server | ferry-control | M1 |
| C9 | Agent output is forgeable for its own trip only: schema caps, typed alert kinds, no free text into alerts, links only from web templates | A3 phishing through alerts or poisoning other tenants | ferry-control (validation) + ferry-web (rendering) | M1 |
| C10 | VNC ticket: Ed25519, 60 s, single-use `jti`, bound to `sub == FW_OWNER`, trip, pod and `cart_id`; public key pinned by the VAP | Cross-tenant VNC, ticket replay, a stale cart | in-pod gate | M1 |
| C11 | First-viewer lock: after the first accept, only the holder of nonce `N` can connect; tickets are ignored for that cart; the check-and-set is one synchronous step | A compromised **ferry-web** taking over a session after the user connected. It does **not** constrain a compromised vnc-edge, Envoy or Cloudflare, which see `N` (R4). | in-pod gate | M1 |
| C12 | Single viewer: one socket at the gate (a reconnect with `N` replaces it) and Xvnc `-NeverShared -DisconnectClients=0` | Silent co-viewing | gate + Xvnc | M1 |
| C13 | Xvnc serves RFB only on a unix socket (`-rfbunixpath`, mode 0600) with no TCP or WebSocket listener and no `-httpd` | Page JS opening a loopback socket and reading the framebuffer past the payment iframe's isolation. Browsers have no AF_UNIX API. (A3, as uid 1001, can open the socket; it already owns that session.) | agent supervisor; self-test asserts no extra listeners | M1 |
| C14 | Pods never serve HTML. vnc-edge serves the stock client with a build-computed CSP (`connect-src 'self' wss://ferry-vnc.chestr.dev`, hashed inline script, `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'none'`), `Referrer-Policy: no-referrer` and `no-store`. nginx proxies only WS upgrades. vnc-edge stays a stock, logic-free nginx plus static files. | Tenant-compromisable script on a chestr.dev origin; a page pointing the client at a keystroke relay | vnc-edge image and config | M1 |
| C15 | Ticket only in the URL fragment, resume nonce only in `Sec-WebSocket-Protocol`; neither appears in a path or query that Envoy, nginx or VictoriaLogs records | Tickets and nonces leaking into access logs, which VictoriaLogs and `openclaw` can read | landing page + shim + gate | M1 |
| C16 | Cookie and Authorization removed on both routes (on the dashboard route after ext-auth has read the cookie) | A pod or a compromised ferry-web replaying the owner's Authelia session into Immich, Windmill or internal apps | HTTPRoute RequestHeaderModifier; post-deploy echo test | M1 |
| C17 | "Cart held" enforced five times: agent (authoritative); web won't mint; ferry-control deletes at `hold_ready_at + hard cap + 5 min` by its own clock; Xvnc `-MaxConnectionTime 6000`; `activeDeadlineSeconds 93600` | VNC kept open outside a hold, by a buggy or lying agent | agent, web, control, Xvnc, kubelet | M1 |
| C18 | Compiled agent maxima: interval ≥ 30 s, unclaimed ≤ 45 min, session ≤ 45 min, hard ≤ 90 min, viewer-gone ≤ 15 min, ≤ 6 cart attempts per pod; no "0 = forever" | A compromised control plane making holds endless or polling unbounded (not A3, see R13) | agent code (not configurable) | M1 |
| C19 | Gate revocation: the socket closes on release, expiry, session cap, viewer-gone, `vnc_revoke`, drain or suspend; a viewed hold always ends with the pod exiting | Gateways check only the upgrade [R:map-k8s-research §e] | agent gate; pod delete as the final kill | M1 |
| C20 | Egress allowlist: Cilium `toFQDNs` on TCP 443 plus a DNS L7 rule that resolves only allowlisted names; deny world, LAN, nodes, kube-apiserver, other pods, smtp-relay and observability | The browser used as a proxy; DNS exfiltration; lateral movement | CiliumNetworkPolicy (GitOps values) | M1 |
| C21 | Chromium lockdown: `--kiosk`, managed policies in all 3 directories, watchdog, minimal env; self-test requires `ERR_BLOCKED_BY_ADMINISTRATOR` | Escaping to other sites, file://, chrome://, view-source, downloads, extensions | browser image + agent | M1 |
| C22 | A pod that ever had a viewer never watches again (exit, fresh pod). A departure viewed in checkout is never re-carted | Anything a viewer did persisting into the next session; double bookings | agent (`watcher.py:510-542` changes) + fleet loop | M1 |
| C23 | Deterministic user id `u = hash(provider id, subject)`; `trips.user_id` immutable by trigger; `FW_OWNER` set at pod create and checked by the VAP against the owner label | A DB row re-owning a live or future browser | Postgres trigger + VAP + gate | M1 |
| C24 | Per-process DB roles (`ferrywatch_web`, `ferrywatch_control`); `audit_log` INSERT-only for both; RLS lets web insert only `test`, `account`, `admin` and `invited` notifications | A1 rewriting audit, writing pod bindings or trip state, or making forged agent-kind rows appear agent-originated in the outbox (attribution; a compromised web can still message tenants directly, §4.5) | db-migrate grants + RLS | M1 |
| C25 | Browser namespace without `components/common`; quota `secrets: 0` | Mountable registry/GitHub/Cloudflare secrets | chart; ResourceQuota | M1 |
| C26 | Logs: browser namespace excluded from Fluent Bit; both ferry namespaces denied egress to observability; debug screenshots stay in tmpfs and none are taken while holding | Checkout screens or tokens reaching VictoriaLogs (unauthenticated) or `openclaw` | home-cluster Fluent Bit + NetworkPolicy + agent | M1 |
| C27 | Quotas the app can't edit: a PriorityClass-scoped ResourceQuota, `NotTerminating pods: 0`, LimitRange, PriorityClass `preemptionPolicy: Never` | A runaway or compromised operator exhausting the cluster | GitOps objects; no app verbs on them | M1 |
| C28 | *Withdrawn.* The draft's anchor ConfigMap guard; the anchor no longer exists (§7.8). | — | — | — |
| C29 | Identity headers are trusted only from the gateway: the chart renders the dashboard HTTPRoute only together with its ext-auth SecurityPolicy (`failOpen: false`); no Authelia `bypass` rule for ferry hosts; ferry-web ingress only from envoy-external pods and the SNAT entity; a gatus canary sends a forged `Remote-User` and alerts unless it gets Authelia's redirect | A client-supplied `Remote-User` reaching ferry-web (fail-open) | chart template guard + helm-unittest; home-cluster gatus; NetworkPolicy | M1 |
| C30 | The termination message (`/dev/termination-log`, readable by the operator through `pods get`) carries only a fixed enum reason and error class | A pod-to-operator channel for tenant data | agent + image test | M1 |
| C31 | The operator holds no PDB, ConfigMap, Service, route or workload-controller verbs; holds are protected by the agent's drain, not by objects the operator writes | Operator-written objects becoming a lever (the draft's anchor GC hazard) | RBAC; `can-i` negatives | M1 |

### 4.5 Blast radius if each component is compromised

| Compromised | Can | Cannot |
|---|---|---|
| ferry-web (A1) | Phish from the dashboard origin and its service worker. Read every tenant's itineraries and email. Create, edit or delete trips. Release carts. Raise limits up to the GitOps ceilings. Send any text to tenants' push and email (it holds the VAPID key and SMTP access). **Mint VNC tickets for holding carts**, so it can win the race to a hold's *first* connection (the user then sees "opened on another device", before any card entry), and can pass a ticket out through a push message it sends. | Create, alter or exec into pods. Reach pods. Learn `N`, so it cannot join a session the user already opened (C11) or co-view (C12). Erase audit. Write `trip_state`, pod bindings or alert memory (role grants). |
| vnc-edge (A1) | **Fully inside the VNC confidentiality and integrity boundary**: read keystrokes and screens of sessions passing through it, inject key and pointer events (`-AcceptCutText=1` included), observe tickets and `N`, and use an observed `N` to reconnect (and, with preemption, displace the user's socket) for the rest of that hold. Hold sockets open. | Mint tickets. Reach any pod path other than `:6080/ws`. Reach ferry-web, ferry-control or Postgres. Mitigation: vnc-edge is a stock nginx image with static files and a short config, no secrets and no app code. Envoy and Cloudflare sit in the same boundary (R4). |
| ferry-control (A4, through A3) | Forge any trip's status. Send `release`, `drain` or `vnc_revoke` to any agent. Write typed agent alerts for any user. **Create and delete pinned browser pods** for any trip and owner, up to the ResourceQuota (availability, and IP-budget abuse through the pods it starts). Read tenants' itineraries and emails (its DB grants include them). | Mint VNC tickets (no key). Change image, env, volumes or commands (VAP). Read Secrets, exec, read logs, proxy. Get page content. Touch any other namespace. Write `users`, limits or audit updates (role grants). |
| A browser pod (A3) | Its own tenant's current session and card entry. Lie about its own trip, within the operator's clock and the deadline. Attack ferry-control's parser. **Send unbounded requests from the home IP to the allowlisted hosts** (bcferries.com, Queue-it, 3DS hosts), bypassing the agent's budget; bounded only by the egress bandwidth annotation, if S-CIL shows it works (R13). | Reach other pods, the API server, the LAN, smtp-relay, observability or the internet beyond the allowlist. Use its token anywhere but ferry-control. Affect another tenant's trip. |
| A malicious tenant (A2) | Browse allowlisted hosts during their own holds. Card-test at BC Ferries checkout from the home IP (bounded by holds per day). | Get another tenant's trips or VNC (404s, ticket binding). Exceed the ceilings. Persist anything across pods. |

### 4.6 Residual risks, stated plainly

- **R1 Cluster admin.** The owner and anything with cluster-admin can see everything, including a live VNC
  session: `openclaw` (which also has a VictoriaLogs skill wired to hosted LLMs) and `home-cluster-runner`.
  The runner also holds Talos `os:admin` and runs a `pull_request` workflow on what appears to be a public
  repo; depending on GitHub's approval settings, a fork PR may run code there. Restricting it is a hard gate
  before non-household invitees (§2.3). Tenants are told the cluster admin can see everything.
- **R2 Sandbox and kernel.** Until S-SBX lands, Chromium runs `--no-sandbox` (`site.py:154-157`), so a
  renderer exploit owns that pod. Agent and Chromium share uid 1001. Nodes run `mitigations=off apparmor=0
  security=none`, so a kernel exploit reaches the node and every tenant on it. Non-household invitees wait
  for the sandbox (Q4).
- **R3 A compromised ferry-web can race a hold's first VNC connection** (visible, before card entry), and can
  phish through the dashboard and alerts. Closing the race needs an IdP-asserted principal verified by the
  pod (S-VNCID), deferred to M4. Accepting R3 for invite-only friends and family is Q5.
- **R4 Plaintext keystrokes on infrastructure.** Cloudflare's edge, cloudflared, Envoy, vnc-edge and the pod
  see checkout keystrokes (RFB is plaintext after TLS ends). vnc-edge, Envoy and Cloudflare can also inject
  input and reuse `N`. Whether relaying a cardholder's own keystrokes puts the owner in PCI scope is a grey
  area. Tenants are told, naming Cloudflare.
- **R5 BC Ferries' own page supply chain.** Third-party scripts on bcferries.com or the payment pages can skim
  cards to allowlisted hosts, exactly as in a normal browser.
- **R6 One residential IP.** A Queue-it or BC Ferries block hits every tenant and the owner's household. The
  budget and brake reduce the risk but cannot remove it. There is also ToS and fairness exposure, given BC
  Ferries' 2026 alert and waitlist programmes.
- **R7 Policy gaps.** One missed VAP field, or a Cilium behaviour different from what was tested, silently
  reopens a path. Mitigated by allowlist-style VAPs, independent second checks (gate crypto, no SA token, no
  Secrets) and the CI and post-deploy matrices. Not eliminated.
- **R8 Disruption loses carts.** A node crash, OOM or node-pressure eviction (0 s grace) skips the drain;
  BC Ferries keeps the cart until its own timer runs out (S6). In v1 a Talos upgrade also releases any hold
  on the drained node (orderly, with an alert), because there is no hold PDB (§7.8).
- **R9 A tenant who owns their own pod** can keep its hold open until the operator's clock (hard cap + 5 min)
  and can lie about their own trip.
- **R10 Plaintext in-cluster agent channel.** A node-level sniffer can replay one pod's token for up to
  10 min, as that pod only. TLS or WireGuard on :8766 is M4.
- **R11 Integrity and availability of the control plane.** Any compromised control process can delete trips,
  release carts or stop pods. Audit makes this visible; it does not prevent it.
- **R12 The Authelia cookie.** Resolved in v1: the dashboard route removes the whole Cookie header after
  ext-auth (C16). It returns if the IdP later moves to a session cookie on the ferry host itself (app-native
  OIDC or a cookie-based proxy) while Authelia still serves the owner's apps (§6.2.6).
- **R13 A3 can spend the home IP's budget.** The budget, brake and 30 s floor live in agent code. Code
  execution in a pod (after a Chromium exploit under `--no-sandbox`, R2) bypasses them toward the allowlisted
  hosts. The only technical bound is `kubernetes.io/egress-bandwidth: 20M`, a bytes-per-second cap (many
  small requests fit) that is itself unverified with netkit. The request-level fix is a per-pod rate-limiting
  egress proxy (smokescreen-style, M4). Until then the owner accepts this, with the sandbox (M3) raising the
  bar.
- **R14 The shared Postgres superuser is resident in `ferry-watch`.** `components/cnpg` copies it into
  `ferrywatch-initdb-secret`. No app identity can read Secrets or create pods there, and only the stock
  `postgres-init` image uses it, but anything that gains pod-create or secret-read in `ferry-watch` reaches
  Authelia, lldap and Immich data. Eliminating it (a CNPG `Database` plus managed owner role in home-cluster,
  and a component variant without the initdb secret) is Q14.
- **R15 Trusted headers fail open if the gateway policy is missing.** If the SecurityPolicy were detached
  from the route, a client-supplied `Remote-User` would be believed. C29 couples them in the chart and a
  canary watches. A hostNetwork process on a node (privileged, admin-controlled) can also send forged headers
  to ferry-web through the SNAT entity; that is R1 territory.
- **R16 A tenant's bank uses a 3DS host the allowlist lacks.** Checkout breaks at the last step. The agent
  detects the block, the user is offered "Release and book it myself", and the admin is alerted (§3.6
  step 6). S-3DS shrinks but cannot close the gap (Q13).

---
## 5. Browser pod (agent)

### 5.1 Process layout and shutdown

`tini --` (not `-g`; `Dockerfile:67` today uses `-g`) runs `ferry-watch-agent`, which is the supervisor. It
replaces `entrypoint.sh` and its `wait -n; exit 1` (`entrypoint.sh:38-39`):

1. It creates `HOME=/tmp/home` (as `entrypoint.sh:14-17` does) and `/tmp/kasm` (mode 0700).
2. It starts Xvnc with RFB on the unix socket `/tmp/kasm/rfb.sock` only (§5.6), then matchbox, each in its
   own session (`setsid`) so a TERM from tini reaches only the agent.
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
| `watcher.py:54` `LOGIN_BACKOFF_SECONDS`, `:110` credentials, `:122` secrets, `:128` `_login_lock`, `:130` `_login_blocked_until`, `:299-300`, `:315-318`, `:333-349` login and back-off | Not wired in guest mode (`credentials=None`, `_wants_login` always False). Kept behind a build flag for the M5 login mode. |
| `watcher.py:113`, `:127` `concurrency=3`, `_slots` | Concurrency 1; the semaphore is removed. |
| `watcher.py:141-145` `check_now` for all trips | `command{check_now}` for this trip; ignored if a check ran in the last 15 s. |
| `watcher.py:147-162` `release_cart`/`dismiss_cart` with the `cart_id` token | Unchanged, driven by `command{release, cart_id}` and `command{dismiss, cart_id}`. |
| `watcher.py:228-229` `_active` uses `date >= today` | Active until the last in-window departure has passed (Pacific, `models.py:13-14`). |
| `watcher.py:265-266` interval from `store.config` | Interval from `configure.interval_s`, clamped to ≥ 30 s (compiled), keeping ±15 % jitter (`:46`). Local token bucket (burst 2). Fail-slow: if ferry-control has been silent for 10 min, `max(interval, 300 s)`. |
| `watcher.py:72` `Session.show()`, `:312`, `:351-356`, `:506-507`, `site.py:403-405` `_raise_held_windows` and `show()` | Deleted, including the protocol method. One window per pod. |
| `watcher.py:401-422` `_failed` | Kept. Error classes cross as enums (`errors.py:8-38`), plus new `Blocked` (403/429/challenge page) and `NotYetBookable` (date beyond the schedule), both from S-FIX fixtures. Pod recycle after 6 consecutive failures (context reset at 3 stays, `:52`). |
| `watcher.py:441`, `:470` alert memory: every alerted departure, carted or not, joins `alerted` | Two sets. `alerted` (alerted and carted, or viewed) is never re-carted. `offered` (alerted as bookable because a limit suppressed the cart) may be carted once later if still open when the limit clears, with one re-alert. |
| `watcher.py:459`, `:562` `trip.auto_cart and self._credentials is not None` | `trip.auto_cart and self._cart_allowed`. `_cart_allowed` = `configure.auto_cart` (daily holds and attempts, VNC minutes, quiet hours) AND compiled limits (≤ 6 attempts per pod; no new cart within 100 min of `FW_DEADLINE_AT`) AND a control ack within the last 30 s. |
| `watcher.py:112`, `:124`, `:498`, `:516`, `:526`, `settings.py:30`, `:50-54` `cart_hold_minutes` (0 = forever) | A `HoldPolicy` with four clocks: unclaimed (from control ack), session (from first connect), viewer-gone (from last disconnect), hard (from READY). All clamped to compiled maxima; "forever" no longer exists. A **payment guard** pauses session and viewer-gone while a frame is on a payment/3DS host or the payment step was submitted. |
| `watcher.py:510-542` `_service_cart` | Gains the timers, viewer state, verified release, cart-liveness checks while unclaimed, BC Ferries confirmation detection (`outcome=booked`), and C22. After a **viewed** hold: empty (unless payment is in progress), verify, `bye`, exit 0 (instead of resuming at `:536-541`). After an unviewed expiry: resume in place. |
| `watcher.py:529-530` window closed, watching resumed | Not reachable: the window is the pod; viewer disconnects are handled by the gate clocks. |
| `watcher.py:531-535` lost cart: forget the alert and re-cart | Re-cart only if no viewer ever connected. If a viewer had connected, keep the departure in `alerted`. |
| `watcher.py:368-373` `_close_all` | Empties and verifies held carts before closing (drain path). |
| `watcher.py:555-564` `_alert` builds a `Message` with a URL | Emits `alert{kind, params}` (typed, no text, no URL). |
| `watcher.py:96-97`, `:568-595` alert memory keyed by `repr(search_key)` | `alerted[]` and `offered[]` arrive in `configure`, persisted by ferry-control, keyed by `sha256(canonical JSON of search_key)`. A held departure is removed on boot, as today (`:577`). |
| `site.py:149-160` `_launch` | `launch_persistent_context("/tmp/profile")` with `--kiosk`, `--disk-cache-size=33554432` and `ignore_default_args=["--disable-popup-blocking"]`. `env={DISPLAY, HOME, TMPDIR, TZ}`. `handle_sig*=False`. `chromium_sandbox=(FW_SANDBOX=="1")`. |
| `site.py:167-176` `new_session` | Pages come from the single persistent context; the user agent is unchanged (Q9). |
| `site.py:400-401` `empty_cart` | Navigates, then verifies both the landing URL and an empty cart (selectors from S5). Raises `ReleaseUnverified` otherwise; the admin is alerted. |
| `site.py:407-422` debug snapshots | tmpfs only, none while holding, never fetchable. |
| `site.py:42-47`, `:66-87` `BASE_URL`, `USER_AGENT`, `Credentials`, `search_url` | Move to `ferry_watch_core/bcferries/urls.py` (Playwright-free). |
| `site.py:112` `base_url` parameter | Overridable only in the `agent-e2e` image, enabled by a baked marker file. |
| `state.py` `RuntimeState`, `Cart` (`:36-44`, `cart.id` at `:40`) | Reused in the agent. `cart.id` is the `cart_id` tickets bind to. |
| `entrypoint.sh:20-26` Xvnc flags | §5.6. `-AlwaysShared`, `-interface 0.0.0.0`, `-websocketPort`, `-httpd` and `-DisableBasicAuth` are removed; `:99`, `-geometry` and `-depth 24` stay. |
| `Dockerfile:36-38` `/vnc` symlink, `:65` `USER pwuser`, `:67` `tini -g` | Symlink removed. `USER 1001:1001` (numeric). `ENTRYPOINT ["/usr/bin/tini","--","ferry-watch-agent"]`. Firefox and WebKit removed from the image. |

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
| A→C | `hello` | `proto: [1]`, `agent_version`, `image_digest`, `pod_name`, `gen`, `applied_rev`, `snapshot` (a `state`), `unacked[]` |
| A→C | `state` | `phase` (starting/watching/holding/draining), `checking`, `checked_at`, `next_due`, `retry_at`, `effective_interval`, `sailings[≤60]{depart, arrive, vessel≤64, status enum, fares[≤6]{name≤32, price, selectable}}`, `error{class enum, text≤300 masked by describe()}`, `consecutive_errors`, `cart{id, status, depart, fare, price, ready_at, unclaimed_deadline, session_deadline, hard_deadline, payment_in_progress}\|null`, `viewer{connected, first_at, seconds}`, `alerted[HH:MM ≤100]`, `offered[HH:MM ≤100]`, `navs{refresh, search, cart, empty, route_info}` since the last message, `queue_detours`, `working_set_mb`, `lockdown_ok`, `bucket`. Sent on change (coalesced over 250 ms) and at least every 15 s. |
| A→C | `event` | `code` enum (session_opened, sold_out_again, check_failed, cart_added, cart_released, viewer_connected, viewer_disconnected, booking_confirmed, checkout_host_blocked, cart_expired_by_site, opening_seen, opening_gone, …), `params{depart?, fare?, minutes?, class?, seconds?}`. No free text, no hostnames. |
| A→C | `alert` | `kind` enum (bookable, cart_held, cart_failed, cart_lost, cart_expired, failing, hold_reminder, hold_ending, release_unverified, checkout_blocked), `params{departures[≤10], fare?, price?, minutes?, error_class?, by_site?}` |
| A→C | `bye` | `reason` enum (hold_ended, drained, deadline, recycle, startup_failed, lockdown_failed, fatal, smoke_done), `outcome` enum (booked, released, expired, expired_by_site, payment_left_to_site, none), `cart_emptied`, `empty_verified`, `viewer_was_connected` |
| C→A | `hello_ack` | `server_time`, `accepted_proto`, `resend_from` |
| C→A | `configure` | `rev`, `trip` (`Trip.to_json`; the agent re-validates with `Trip.from_json` and requires `id == FW_TRIP_ID`), `active`, `interval_s`, `auto_cart`, `hold{unclaimed_s, session_s, viewer_gone_s, hard_s}`, `alerted[]`, `offered[]`, `vnc_allowed`, `brake{multiplier, paused}`, `breaker` (closed/open) |
| C→A | `command` | `id`, `op`: `check_now` \| `release{cart_id}` \| `dismiss{cart_id}` \| `drain{reason}` \| `vnc_revoke{reason}` |
| C→A | `ack` | `seq` |

**Deliberately absent:** navigate, URL, base_url, eval, selector, screenshot, cookies, storage_state,
credentials, log level, tracing, Chromium flags, file transfer, "extend hold", and anything that grants VNC.
ferry-control can only *revoke* VNC (`vnc_allowed=false`, `vnc_revoke`). The agent fetches the terminal list
itself (`terminals.py`), so labels from the control plane never feed navigation. The draft's `reauth`
message and 5-minute re-review are dropped: the token is checked at connect, and in one process the fleet
loop closes a pod's socket itself when it deletes the pod.

**Versioning:** ferry-control accepts protocols N and N-1. Every pod is gone within 26 h, so releases at
least 26 h apart are safe. A CI skew job (the N-1 agent image against the new control image) starts with the
first protocol change after v1; before that there is no N-1.

### 5.4 Agent-side invariants (compiled, not configurable)

- `interval_s ≥ 30`. Hold clocks: unclaimed ≤ 45 min, session ≤ 45 min, viewer-gone ≤ 15 min, hard ≤ 90 min.
  No zero or "forever". The payment guard pauses only session and viewer-gone, never the hard cap.
- At most 6 cart attempts per pod lifetime. No new cart within 100 min of `FW_DEADLINE_AT`. No new cart
  without a control ack in the last 30 s.
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

`disp` is decoded but not trusted by the landing page, which shows it with `textContent` only. At first
connect the landing page stores `session deadline = now + session_s` with `N` and shows "Checkout closes at
hh:mm (paused while you're on the payment page)" and the hard deadline.

**Upgrade checks, all required, in order:**
1. `Origin == FW_VNC_ORIGIN` (`https://ferry-vnc.chestr.dev`). Page JS in the pod's own Chromium cannot pass
   this (its Origin is bcferries.com).
2. `lockdown_ok`, not draining, `vnc_allowed`, a local cart READY, and every hold clock still running.
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
- `N` lives in localStorage on the `ferry-vnc.chestr.dev` origin, keyed by `(trip, gen)`, so a reload or a
  return from the dashboard with a fresh ticket reuses it. ferry-web never sees it.
- A device that loses `N` (cleared storage, a different browser context) cannot rejoin by design (C11), and
  the hold ends at viewer-gone. Whether the iOS home-screen app path loses `N` is tested in S-VNC-lite; if it
  does, Q5b (an owner-gated takeover) decides.
- Moving checkout to another device ("continue on another device" QR code) is M4.

**Revocation:** the gate closes the socket on release, any clock expiring, `vnc_revoke`, suspend or drain.
After a viewed hold the pod exits.

**Rate and usage:** viewer seconds are reported in `state` and charged to `vncMinutesPerDay`. The gate
refuses more than 5 *failed* upgrade attempts per minute; successful reconnects are not counted.

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
  plus `URLAllowlist: ["about:blank","bcferries.com","queue-it.net", <payment and 3DS hosts from S2 and S-3DS>]`.
  The blocklist covers navigations including subframes, not fetch or WebSocket, which is why C13 does not
  rely on it. S-KIOSK confirms the subframe behaviour, because it decides whether an unknown 3DS iframe is
  blocked (R16).
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
Queue-it, with at most 2 extra pages. It counts main-frame navigations that leave the allowlist. During a
hold it watches for `ERR_BLOCKED_BY_ADMINISTRATOR` or `ERR_NAME_NOT_RESOLVED` on any frame and reports
`checkout_host_blocked`.

**Self-tests** run at startup and after any Chromium relaunch, and fail closed (`lockdown_ok=false`):
1. `https://example.com/`, `chrome://version`, `file:///etc/passwd` and `view-source:https://www.bcferries.com/`
   must each fail with `ERR_BLOCKED_BY_ADMINISTRATOR`.
2. Listeners: TCP exactly `0.0.0.0:6080` and `0.0.0.0:8080`; unix socket `/tmp/kasm/rfb.sock` with mode
   0600; nothing else (`ss -ltnx`).
3. From the first bcferries page, `page.evaluate` tries `fetch("http://127.0.0.1:6081/")`,
   `new WebSocket("ws://127.0.0.1:6081/websockify")`, `new WebSocket("ws://127.0.0.1:6080/ws")` and
   `fetch("https://example.com/")`. None may complete an RFB handshake or load content. The LNA check only
   warns; the listener and egress checks are fatal.

**Xvnc flags** replace `entrypoint.sh:20-26`:
`:99 -geometry 1280x1000 -depth 24 -rfbunixpath /tmp/kasm/rfb.sock -rfbunixmode 0600 -SecurityTypes None
-NeverShared -DisconnectClients=0 -SendCutText=0 -AcceptCutText=1 -DLP_ClipAcceptMax 256
-IgnoreClientSettingsKasm -FrameRate 15 -MaxVideoResolution 1920x1080 -MaxConnectionTime 6000
-AcceptSetDesktopSize -publicIP 127.0.0.1 -Log *:stderr:10`.

- With `-rfbunixpath` set, KasmVNC creates only the unix listener and no WebSocket or TCP listener
  (upstream `unix/xserver/hw/vnc/vncExtInit.cc:255-290`). That is what makes C13 hold by construction.
- **Removed:** `-AlwaysShared`, `-interface`, `-websocketPort`, `-httpd`, `-DisableBasicAuth`, `-sslOnly`,
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
- Seccomp `RuntimeDefault` until S-SBX; then `Localhost` profile `profiles/fw-chromium.json` (containerd's
  default plus `clone`/`unshare` with `CLONE_NEWUSER`/`NEWPID`/`NEWNET`, and `chroot`, the exact set fixed by
  S-SBX), together with `chromium_sandbox=True`. Both are VAP-param driven.
- `hostUsers: false` once the Talos `user.max_user_namespaces` change is applied. It is independent
  hardening (the pod's root maps to an unprivileged host uid) and needs the same sysctl.
- `fluentbit.io/exclude: "true"`.
- `kubernetes.io/egress-bandwidth: 20M`, only if S-CIL shows the bandwidth manager works with netkit.

### 5.7 Credential modes

- **Guest, the only mode at launch, for everyone including the owner.** Remove the auto-cart credential gate
  (`watcher.py:459`, `:562`) and default `auto_cart` to true (`views.py:113`). Drop `BCF_USER`/`BCF_PASS`
  (`settings.py:40-42`) and pass `credentials=None`. Cart alerts say "check out as a guest". The guest's
  contact details and card exist only in the pod's memory during one hold, and the pod is destroyed after any
  viewed hold. **Owner cost:** today the owner's checkout signs in for saved payment cards
  (`site.py:270-271`) and possibly account-only fares; under this plan the owner also checks out as a guest.
  S1, S2 and S7 run against the owner's actual current trips before cutover; trips whose fare types need an
  account become alert-only (Q3b).
- **If S2 (guest checkout) fails:** ship **alert-only**. Turn auto-cart off fleet-wide (`autoCart: false`
  in values), and bookable alerts link to `search_url` on bcferries.com (`site.py:72-87`). **No** pod-served
  password page and no pre-cart VNC window under any fallback.
- **Attended sign-in** (deferred, M5) is open question Q1 and an exception to decision 3. The user would type
  their BC Ferries password into the real bcferries.com page in their own pod, during a VNC window of at most
  10 min, before any cart exists. Cookies would live in tmpfs only. It requires the Chromium sandbox first,
  spikes S3/S4 (§12.1), and an origin assertion before typing into `/login` (`site.py:278-301`).
- **Never:** a dashboard password form, an operator-written Secret, per-user credentials in 1Password or ESO,
  or a pod-served credential page. Each puts the plaintext through a component the operator controls
  [R:map-threat-model, R:billing-guest-mode-credentials Q4].

---
## 6. Control plane

### 6.1 Processes and configuration

- One image, `ferry-watch-control` (python:3.12-slim by digest, tzdata, uid 65532, the web build), with
  three entrypoints: `web`, `control` and `migrate`. An all-in-one `dev` entrypoint (web + control in one
  process, with a dev identity proxy) exists for docker compose only.
- Config comes from a chart ConfigMap and the ExternalSecret. Secrets are mounted only into the process that
  uses them:
  - **web:** ticket private key, VAPID private key, `ferrywatch_web` password.
  - **control:** `ferrywatch_control` password.
  - **migrate (Job):** the `ferrywatch` owner password (from `ferrywatch-pguser-secret`).
- ferry-control mounts `ferry-control-config`, rendered from the same Helm values as the VAP params
  ConfigMap, so the two cannot drift; a CI check asserts they are equal.
- Probes are `/livez` and `/readyz` on each process's metrics port. web's `readyz` needs the DB; control's
  needs the DB and the apiserver. A control-plane outage never touches pods, because pod readiness is local.
- Each process holds a `pg_try_advisory_lock` (distinct keys), so a second replica idles.

### 6.2 Identity (owner message 5)

#### 6.2.1 The question and the answer

The owner said: "Do not assume authelia will be the thing we use later, auth may be a social login for
example. To keep it simple we can use authelia and trusted headers perhaps to make it multi tenant, but it
should be easy to replace later." Decision 7 records: start with an authenticating proxy plus trusted
headers; keep identity behind an abstraction; no Authelia-only feature may be load-bearing.

The draft overrode this with app-native OIDC on the reasoning "invitees are off-LAN, so OIDC". That
inference was wrong: trusted headers exist only on envoy-internal today because of configuration, and one
route-level SecurityPolicy brings them to envoy-external. **This plan adopts trusted headers through Authelia
ext-auth on the dashboard route**, behind an `IdentityProvider` interface, and keeps app-native OIDC as a
documented adapter for a later IdP change. Q0 asks the owner to confirm.

#### 6.2.2 What trusted headers look like on this cluster (chosen)

Objects (the first two rendered by the chart, so they can't be deployed apart; C29):

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: ferry-web
  namespace: ferry-watch
  annotations: {gatus.home-operations.com/enabled: "false"}   # a probe would only see Authelia's redirect
spec:
  hostnames: [ferry.chestr.dev]                                 # ferry-next.chestr.dev before cutover
  parentRefs: [{name: envoy-external, namespace: networking, sectionName: https}]
  rules:
  - filters:
    - type: RequestHeaderModifier
      requestHeaderModifier: {remove: [Cookie, Authorization]} # applied by the router, after ext-auth
    backendRefs: [{name: ferry-web, port: 8765}]
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata: {name: ferry-web-auth, namespace: ferry-watch}
spec:
  targetRefs: [{group: gateway.networking.k8s.io, kind: HTTPRoute, name: ferry-web}]
  extAuth:
    failOpen: false
    headersToExtAuth: [X-Forwarded-Proto, accept, cookie]       # not authorization: cookie sessions only
    http:
      backendRefs: [{group: "", kind: Service, name: authelia, namespace: default, port: 80}]
      path: /api/authz/ext-authz/
      headersToBackend: [Remote-User, Remote-Groups, Remote-Email, Remote-Name]
```

home-cluster adds:
- a `SecurityPolicy` entry for namespace `ferry-watch` in `default/authelia/app/referencegrant.yaml`;
- an Authelia `access_control` rule, placed before the `*.chestr.dev` admin rule at `configuration.yaml:99`:
  `{domain: [ferry.chestr.dev, ferry-next.chestr.dev], subject: ["group:ferry-watch", "group:admin"], policy: two_factor}`;
- **no `bypass` rule** for any ferry host: a bypassed request carries no `Remote-User` from Authelia, so a
  client-supplied one would reach the app;
- the lldap group `ferry-watch`.

How a request flows:
1. The browser sends its `authelia-home-ops` cookie (domain `chestr.dev`) to `ferry.chestr.dev`.
2. Envoy's ext-authz filter (filter order 6 in EG, `eg-httpfilters.go:117-118`) asks Authelia. Without a
   valid two-factor session in group `ferry-watch`, Authelia redirects to `auth.chestr.dev` and back.
3. On success Envoy copies `Remote-User/Groups/Email/Name` from Authelia's answer onto the request,
   overriding any client-supplied copies (EG `headersToBackend`).
4. The route's `RequestHeaderModifier` runs in the router, after every HTTP filter (`eg-route.go:59-60`), and
   removes `Cookie` and `Authorization`. ferry-web never sees the Authelia cookie (C16), and the app sets no
   cookie of its own.

What the app does:
- `TrustedHeaderProvider` reads `Remote-User` (regex `^[A-Za-z0-9._@-]{1,64}$`, lowercased) as the subject, and
  `Remote-Email`/`Remote-Name` for display. `Remote-Groups` is advisory (used only for optional
  auto-approval, §6.2.7). Header names are config.
- Every `/api/*` route and the dashboard WebSocket require a user row in state `active`; otherwise 401/403.
  Suspension is immediate. Dashboard WebSockets close after at most 60 min, so a lapsed Authelia session is
  noticed at the next upgrade.
- The SPA treats a 401, or a redirect to `auth.chestr.dev` on a fetch, as "reload the page" (Envoy returns
  Authelia's redirect to fetches too). The manifest link gains `crossorigin="use-credentials"`
  (`web/index.html:10`), so the PWA manifest loads behind ext-auth; S-PUSH checks `sw.js` too.

Session lifetime is Authelia's: 3 days idle and 7 days total, or 6 months with "Remember me"
(`configuration.yaml:32-36`). These are cluster-wide settings. The onboarding email tells invitees to tick
"Remember me", which gives a longer session than the draft's app session (7 d idle, 30 d absolute).

Forgery analysis:
- Through Envoy, a client cannot set `Remote-User`: Authelia always returns it on success, and Envoy
  overrides the client's copy. A request that Authelia rejects never reaches the app.
- The fail-open case is a missing or detached SecurityPolicy (R15). The chart renders the route only with
  its SecurityPolicy (a template `fail` otherwise, asserted by helm-unittest). A gatus endpoint (home-cluster)
  sends `GET https://ferry.chestr.dev/api/state` with a forged `Remote-User` every minute and alerts unless
  the status is Authelia's 302/401.
- Directly to the pod: ferry-web's ingress admits only envoy-external pods and the `kube-apiserver` entity
  (SNAT). The latter includes hostNetwork processes on nodes and the API-server proxy, both admin-only
  (R1, R15). Browser pods and vnc-edge cannot reach ferry-web.

#### 6.2.3 What app-native OIDC would need instead (not chosen for v1)

- OIDC client code in ferry-web: login, callback, `state`/`nonce`, PKCE, JWKS verification, a `sessions`
  table and a `__Host-fw_session` cookie, and an Authelia OIDC client `ferry-watch` (about 4-5 focused days).
- Because the app then has its own cookie, the route can't drop the whole Cookie header, so the
  `authelia-home-ops` cookie must be removed by name. Envoy Gateway can do that only with a Lua
  EnvoyExtensionPolicy, and Lua is off unless `config.envoyGateway.extensionApis.enableLua: true` is set
  gateway-wide (upstream EG v1.9 Lua task; weakly sandboxed, affects the Envoy fleet serving envoy-internal
  and Authelia). The alternative is a separate registrable domain for Ferry Watch (new zone, cert, tunnel
  entries).
- ferry-web would need egress to the issuer. `auth.chestr.dev` resolves to the envoy-external LB IP, but
  under kube-proxy replacement the policy sees the envoy-external pods on port 10443 (EG shifts privileged
  ports by 10000), so the rule is `toEndpoints` for `networking` pods labelled
  `gateway.envoyproxy.io/owning-gateway-name: envoy-external` on TCP 10443.
- Advantages it would bring: the app verifies a signed `id_token` itself (no fail-open mode); app-controlled
  session length; a signed token that S-VNCID could later forward to the pod.

#### 6.2.4 Comparison and recommendation

| | A: Authelia ext-auth + trusted headers (chosen) | B: app-native OIDC |
|---|---|---|
| Owner decision 7 | Matches it | Departs from it |
| App code | A header adapter (about 1 day) | OIDC client, sessions, cookie (about 4-5 days) |
| Authelia cookie at the backend | Removed with the whole Cookie header; no Lua | Needs gateway-wide Lua or a separate domain |
| Failure mode if the gateway is misconfigured | Fails open (C29 guards, R15) | Fails closed |
| Session length | Authelia's (3 d, or 6 months with remember-me) | App's own |
| Signed token for a later in-pod check (S-VNCID) | None | `id_token` available |
| Social login later | Swap the ext-auth backend (§6.2.6) | Change issuer config; the cookie problem persists while Authelia serves the owner's apps |

**Recommendation: A.** It is what the owner asked for, it is less code, and it is the only option on
`chestr.dev` that keeps the owner's Authelia cookie away from the app without a gateway-wide Lua change. Its
one real weakness, fail-open on misconfiguration, is contained by rendering the route and policy together
plus a continuous canary.

#### 6.2.5 The abstraction

```python
@dataclass(frozen=True)
class Principal: provider: str; subject: str; email: str | None; name: str | None; groups: frozenset[str]
class IdentityProvider(Protocol):
    async def authenticate(self, request: web.Request) -> Principal | None: ...
    def routes(self) -> list[web.RouteDef]: ...     # empty for headers; /auth/* for an OIDC adapter
```

- `TrustedHeaderProvider` (v1): `provider` is the configured `identity.providerId` (for example
  `authelia-home`), `subject` is `Remote-User`.
- `OidcProvider` (later, if Q0 or an IdP change needs it): `provider` is `iss`, `subject` is `sub`.
- `DevHeaderProvider`: compose only, behind a dev proxy that injects headers.
- **Stable id:** `uid = "u" + base32lower(sha256("fw/v1|" + provider + "|" + subject))[:19]` (20 characters,
  DNS-label safe). It is deterministic, so no DB row can map a principal onto another user's browsers (C23),
  and the admin can compute a uid from a username before the person first logs in.
- `identityAliases` (`new principal hash → existing uid`) in the GitOps ConfigMap carry users across an IdP
  change. The admin CLI that generates them by email ships when the first migration happens.
- Email is for display and invitations only, never a key.

#### 6.2.6 Replacing the IdP later

**Path 1 (preferred): keep the ext-auth pattern, swap the authenticating proxy.** For social login, deploy
oauth2-proxy (or another forward-auth proxy) in `ferry-watch`, configured for the new IdP:
1. Run it as an ext-auth backend that returns 2xx with identity headers for signed-in users and a redirect to
   sign-in otherwise (oauth2-proxy with `--upstream=static://202 --set-xauthrequest`, which emits
   `X-Auth-Request-User/Email/Groups`). Its `/oauth2/*` paths need their own HTTPRoute rule without the
   SecurityPolicy. Prove this shape in a spike (S-IDP) before relying on it: oauth2-proxy's `/oauth2/auth`
   endpoint answers 401, not a redirect (upstream `oauthproxy.go:1040-1057`).
2. Change the chart's SecurityPolicy backend and `headersToBackend`, and the app's header names and
   `identity.providerId`.
3. Generate `identityAliases` for existing users (by email) as a GitOps PR. Running pods keep working:
   `FW_OWNER` is the old uid, which the alias maps to.
4. Admission: social IdPs have no `ferry-watch` group, so turn off group auto-approval and rely on the app's
   pre-registered people (§6.2.7) matched by verified email, or by one-time invite codes where emails differ
   (Apple's private relay).
5. GitHub has no OIDC `id_token`; oauth2-proxy supports it natively, which is another reason to keep a proxy
   in front rather than the app.
6. The route still removes all cookies after ext-auth, so R12 stays closed, as long as the new proxy's own
   cookie is checked at ext-auth and not needed by the app.

**Path 2: switch the app to `OidcProvider`.** Implement §6.2.3, then solve the Authelia cookie by name (Lua)
or by moving Ferry Watch to its own registrable domain, because the owner's `authelia-home-ops` cookie would
still reach `ferry.chestr.dev` while Authelia serves the owner's other apps. Then steps 3-4 as above.

Either way, nothing in the browser pods, the VNC gate, the VAPs or the operator changes, because they key on
the uid, not on the IdP.

#### 6.2.7 Invite-only

- v1 admission has three layers: Authelia admits only lldap group `ferry-watch` (gateway); the app admits only
  people the admin pre-registered on the Users page ("Add person": lldap username, email, optional limit
  overrides, mandatory reason); suspension blocks immediately.
- "Add person" computes the uid from the username, creates the user row in state `invited`, and sends an
  `invited` email with the onboarding steps (§12.5). The first request from that `Remote-User` makes the row
  `active`.
- Optional `autoApproveGroups` (default empty) lets membership of a listed group stand in for "Add person"
  while the IdP is lldap, saving one admin step. Groups are generic, not Authelia-specific, but they are
  load-bearing only if the owner turns this on.
- The draft's "request access" queue is deferred: under group-restricted ext-auth nobody outside the group
  can reach the app. It returns with a social IdP.

#### 6.2.8 Admins

`bootstrapAdmins` (a list of uids) in GitOps cannot be revoked from the UI. `users.is_admin` is the DB flag.
The `groups` header never grants admin by itself. **Admins never get VNC** into tenant browsers.

#### 6.2.9 Hard gates before anyone new joins lldap

- Authelia `identity_providers.oidc.authorization_policies` restrict Immich, Windmill, Grafana, Autobrr and
  Paperless to their own groups; today every client accepts any lldap user at `two_factor`
  (`configuration.yaml:131-214`).
- Never give the app lldap admin credentials.

### 6.3 API changes (`server.py`)

**Middleware and plumbing:**
- The identity middleware goes **inside** `error_middleware`, so its 401/403 `ApiError` is JSON-formatted:
  `[security_headers, cache, error, identity, cross_site]` (today `server.py:449-451`; aiohttp runs the list
  outermost first, and `error_middleware` at `server.py:201-217` formats `ApiError`). `/livez` and `/readyz`
  replace `/healthz` (`server.py:426-433`), are exempt from identity, and return nothing tenant-specific.
- `Services` (`server.py:60-95`) becomes a **repository scoped by user**. Every lookup takes
  `(uid, trip_id)`, and a foreign trip returns the same 404 as a missing one. Filtering happens in SQL, not in
  `views.py`, so a new snapshot field cannot leak across tenants.
- The Hub (`server.py:134-184`) becomes **per user**: each client records its uid at upgrade;
  `trip_changed` marks only that user dirty; one payload is built per dirty user, coalesced over 100 ms; at
  most 5 sockets per user, each living at most 60 min. The newest-only sender, the 15 s ping and the 10 s send
  timeout are kept.

**Route changes:**

| Route | Change |
|---|---|
| `GET /api/state` | Per-user snapshot: `me`, `limits` (effective value plus source), `usage`, `trips[].browser{phase, reason, queue_position, starts_on, effective_interval, next_due, heartbeat_at, cart, viewer, lockdown_ok}`, `capacity{can_add, reason}`, `links{kofi}`, events. Drops the global `checkout_url`, `login_configured`, `alerts_configured` and `max_trips` (`views.py:118-143`). |
| `POST/PATCH/DELETE /api/trips[/{id}]` | Scoped to the user, with transactional limits (§9.2). Enabling a trip at `maxActiveBrowsers` returns 409 naming the trip to pause. DELETE sends `drain`; the cart is returned before the row is archived. |
| `POST /api/trips/{id}/check` | Replaces `POST /api/check` (`server.py:403-407`). 6/h per user. |
| `POST /api/trips/{id}/cart/release` and `/dismiss` | Scoped. Inserts an `agent_commands` row. |
| `POST /api/trips/{id}/cart/self-book` | **New.** Release, then return `search_url` for the user to book directly. |
| `POST /api/trips/{id}/vnc` | **New.** Ticket mint (§3.6 step 4). Same-origin JSON POST, so the existing CSRF rules apply. |
| `POST /api/trips/{id}/booked` `{booked: bool}` | **New.** The "Booked it?" answer. `true` archives; `false` resumes. |
| `PATCH /api/me` | `{paused, interval_seconds, quiet_hours}` per user, replacing the global `PATCH /api/settings` `enabled` that let one viewer pause everyone. |
| `DELETE /api/me` | **New.** Delete my data: drain pods, delete trips, push subscriptions and alert memory; redact audit payloads to ids. |
| `POST/DELETE /api/me/push-subscriptions` | **New.** Web Push. |
| `POST /api/alerts/test` | Per-user cooldown (6/h) instead of the global 30 s (`server.py:46`, `:410-423`). |
| `GET /c/{trip}`, `/admin`, `/settings` | Serve `index.html` (explicit routes, no wildcard). |
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
| `users` | `id text pk (uid)`, state (invited/active/suspended), username, email, display, is_admin, interval_pref, quiet_hours, paused, created_at, last_seen_at | web |
| `identities` | (provider, subject) pk, uid, last_groups, first/last_seen | web |
| `user_limits` | uid, key, value, set_by, reason, set_at | web (clamped on read) |
| `trips` | `id char(16) pk`, `user_id` (immutable), rev, spec jsonb (validated by `Trip.from_json`), enabled, `paused_reason`, created/updated/archived | web; control (column grants on `paused_reason`, `archived_at` only) |
| `trip_runtime` | trip_id pk, phase, reason, queue_position, reserved_until, gen, failures, backoff_until, updated_at | control |
| `browser_pods` | name pk, trip_id, uid (pod), user_id, gen, node, image_digest, created_at, deadline_at, ended_at, end_reason | control |
| `trip_state` | trip_id pk, pod_uid, boot, seq, agent_phase, checked_at, next_due, effective_interval, sailings jsonb, error_class, consecutive_errors, cart jsonb, **hold_ready_at**, first_viewer_at, viewer_connected, heartbeat_at (coalesced to 30 s), lockdown_ok, working_set_mb | control |
| `agent_commands` | id, trip_id, op, cart_id, requested_by, created_at, consumed_at | web inserts; control sets consumed |
| `alert_memory` | trip_id pk, key_hash, alerted text[], offered text[], held text | control |
| `events` | id, user_id, trip_id, ts, kind, code, params jsonb (typed, bounded) | web, control (insert) |
| `notifications` (outbox) | id, user_id, trip_id, kind, params jsonb, dedup_key unique, created_at, sent_at, attempts, last_error | control inserts; web inserts (RLS-limited) and updates sent |
| `push_subscriptions` | id, user_id, endpoint, p256dh, auth, created, last_ok, failures | web |
| `vnc_mints` | jti pk, user_id, trip_id, pod, cart_id, created_at | web |
| `usage_ledger` | (user_id, day Pacific, kind) pk, amount (holds, cart_attempts, vnc_seconds, manual_checks, test_alerts, mints, pod_starts) | control, web |
| `fleet_state` | key pk, value jsonb (brake, breaker, maintenance, pause_carting, control_alive_at) | control; web (maintenance and pause toggles only) |
| `audit_log` | id, ts, actor_kind (user/admin/system), actor, action, target_kind, target_id, before, after, reason, request_id | both (INSERT only) |

- Policy defaults live in Helm values (ConfigMap), not in a DB `policy` table; per-user overrides live in
  `user_limits`.
- **Retention:** events 30 d, notifications 14 d, `browser_pods` 30 d, audit and usage 400 d. Archived trips
  are deleted 90 d after the sailing. `DELETE /api/me` and the admin "delete user" act at once and redact
  audit `before/after` to ids.
- **No BC Ferries data** is ever stored.
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
< `maxCartAttemptsPerDay`, VNC minutes left ≥ `session_s`, outside the user's quiet hours, global
`pause_carting` off. With one active browser per user there is no cross-pod hold coordination; if an admin
raises a user's `maxActiveBrowsers`, holds at once are limited to the same number.

**Notifications it inserts:** every agent-originated kind, plus `booked_it` or `booked` on `bye`, plus
admin alerts (`release_unverified`, `checkout_blocked`, binding violations).

### 6.6 ferry-control: fleet loop

**Loop:** level-triggered. A lightkube `AsyncClient` watches pods with
`app.kubernetes.io/managed-by=ferry-watch-control` and nodes, lists every 10 s, and wakes on Postgres
`LISTEN fleet_wake`. On each pass it computes the desired pods from rows, diffs them against the observed
pods, and creates or deletes. Pod names are deterministic, so AlreadyExists is idempotent. A `Cluster`
Protocol (with `FakeCluster` in tests) isolates lightkube.

**Desired set:** a trip wants a pod when the user is active and not paused; the trip is enabled, has no
`paused_reason`, and is not past; the date is within `startWindowDays` (otherwise `Dormant`); the trip is not
`BackingOff` or `NeedsAttention`; global `maintenance` is off.

**Admission and fairness:**
- Caps: the user has fewer than `maxActiveBrowsers` running, the total is below `maxBrowsersTotal`, at most 2
  pods are Starting, at most 30 starts in the last hour, and the user's last start was at least 20 s ago.
- A trip blocked only by its user's own `maxActiveBrowsers` is `Waiting(limit)`; one blocked by the cluster cap
  or the start throttle is `Queued(position)`. **No Pending pods** are created.
- Queue order: a trip with a reserved slot (30 min after a viewed hold), then the user with the fewest running
  browsers, then the nearest sailing, then the oldest queued (aging, so nothing starves).
- No preemption. Holding pods are never displaced.
- A successor pod for a trip is created only after its predecessor is gone, so at most one pod per trip
  exists and the quota needs only one pod of headroom (§8.4).

**Pod build:** one function, `browser_pod(trip, gen, owner, cfg) -> dict` (§7.4), snapshot-tested and applied
through the real VAP in CI.

**Timers** (values in §8.2): startup timeout; Pending/unschedulable (delete, Queued, admin alert); Watching
silence (only when the hub endpoint has been up at least 2 min); Holding is never reaped on silence, only by
the operator's own clock (`hold_ready_at + hard cap + 5 min`) or kubelet status; back-off; orphan sweep
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
| `bookable` (no cart: auto-cart off, alert-only, limit, quiet hours) | push | route, date, sailings | `search_url(trip)` on the constant `BASE_URL` (`site.py:72-87`) |
| `cart_lost`, `cart_expired` (with "Still bookable: book on bcferries.com" when the last check saw it open), `cart_failed`, `failing` (3rd error; suppressed while the breaker is open), `checkout_blocked` | push (+ email for lost, expired) | typed | `/c/<trip>` (or `search_url`) |
| `booked_it` ("Did you book it?"), `booked` ("Booked!") | push + email | typed; email links open `/c/<trip>?answer=…`, which asks to confirm (no state change on GET) | `/c/<trip>` |
| `invited` | email | onboarding steps (§12.5) | `https://ferry.chestr.dev/` |
| admin: breaker, brake, capacity, delivery failures, `release_unverified`, `checkout_blocked` | admin push + PrometheusRules | typed | `/admin` |

`BCF_VNC_URL`/`checkout_url` (`settings.py:24`, `:63`; `notify.py:229-246`) are removed.

**Ko-fi:** the Helm value `kofiUrl` is validated against `^https://ko-fi\.com/[A-Za-z0-9_]{1,64}$`, exposed as
`state.links.kofi`, and rendered as `<a href target="_blank" rel="noopener noreferrer">` in the footer and
on the "Booked!" screen. It never appears on `ferry-vnc` or in alerts. There is no widget script, so the CSP
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
- {apiGroups: [""], resources: [pods], verbs: [create, delete, get, list, watch]}
---
kind: ClusterRole            # ferry-watch-control-nodes
rules: [{apiGroups: [""], resources: [nodes], verbs: [get, list, watch]}]
# ferry-browser (browser ns): no bindings; only the projected control-audience token
```

**Granted to no app identity**, asserted by helm-unittest and by `kubectl auth can-i --as` in envtest and in
the post-deploy smoke, for both namespaces:
- `secrets` (any verb);
- `pods` update, patch or deletecollection; `pods` create in `ferry-watch`; every `pods/*` subresource (exec,
  attach, portforward, proxy, log, ephemeralcontainers, eviction, binding, resize);
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
  annotations: {fluentbit.io/exclude: "true"}  # + kubernetes.io/egress-bandwidth if S-CIL ok
  # no ownerReferences: a bare pod (talosctl drains bare pods; the descheduler skips them)
spec:
  hostname: fw-3f9c0a1b2c3d4e5f-g7               # DNS: <hostname>.browsers.ferry-watch-browsers.svc.cluster.local
  subdomain: browsers
  serviceAccountName: ferry-browser
  automountServiceAccountToken: false
  enableServiceLinks: false
  hostUsers: false                               # only once params.requireUserNamespace == "true" (Talos sysctl applied)
  restartPolicy: Never
  priorityClassName: ferry-browser
  activeDeadlineSeconds: 93600
  terminationGracePeriodSeconds: 90
  dnsPolicy: ClusterFirst
  securityContext:
    runAsNonRoot: true
    runAsUser: 1001
    runAsGroup: 1001
    seccompProfile: {type: RuntimeDefault}       # {type: Localhost, localhostProfile: profiles/fw-chromium.json} once params.sandbox == "true"
  tolerations:
  - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 60}
  - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 60}
  containers:
  - name: browser
    image: ghcr.io/mchestr/ferry-watch-browser@sha256:<params.allowedImages[0]>
    env:                                          # literal values only, exactly these 8
    - {name: FW_TRIP_ID, value: 3f9c0a1b2c3d4e5f}
    - {name: FW_OWNER, value: u5yq2kz7m4c3x8a1b2d3e}
    - {name: FW_POD_NAME, value: fw-3f9c0a1b2c3d4e5f-g7}
    - {name: FW_DEADLINE_AT, value: "1797000000"}  # epoch s = create + 93600
    - {name: FW_HUB_URL, value: "ws://ferry-control.ferry-watch.svc.cluster.local.:8766/agent/v1"}   # == params
    - {name: FW_VNC_PUBKEYS, value: "k1:ed25519:…"}                                                    # == params
    - {name: FW_VNC_ORIGIN, value: https://ferry-vnc.chestr.dev}                                        # == params
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
`parameterNotFoundAction: Deny`. Keys: `allowedImages` (at most 2 digests, so one rollout can overlap);
`hubUrl`, `vncPubkeys`, `vncOrigin`, `podMemory`, `cpuRequest`, `maxDeadline` (93600); `requireUserNamespace`,
`sandbox`, `seccompProfile`; `controlSA`, `breakGlassGroup`.

| VAP | Matches | Enforces | From |
|---|---|---|---|
| `fw-browser-pod` | pods CREATE in ns `ferry-watch-browsers` (all requesters) | Requester is `params.controlSA` or in `params.breakGlassGroup`, and the pod is exactly §7.4 (abridged CEL below) | M1 |
| `fw-browser-pod-lock` | pods UPDATE; `pods/ephemeralcontainers` UPDATE; `pods/exec`, `pods/attach`, `pods/portforward` CONNECT, in that namespace | Deny unless break-glass. UPDATE is also allowed for `system:node:*` and the named kube-system controllers (`generic-garbage-collector`, `node-controller`, `pod-garbage-collector`; Talos runs kube-controller-manager with `--use-service-account-credentials`). **Not** a blanket `system:` exemption. Status writes go through `pods/status` and are unaffected. Positive cases are proven in kind-lite and the post-deploy smoke, not envtest (no kubelet or GC there). | M3 |
| `fw-ns-objects` | CREATE/UPDATE/DELETE of Secrets, ConfigMaps, Services, Endpoints, EndpointSlices, NetworkPolicies, CiliumNetworkPolicies, Roles, RoleBindings, ServiceAccounts, HTTPRoutes, SecurityPolicies, ExternalSecrets, PushSecrets, ResourceQuotas, LimitRanges, PodDisruptionBudgets, `apps/*` (Deployments, ReplicaSets, StatefulSets, DaemonSets) and `batch/*` (Jobs, CronJobs) in both ferry namespaces, plus pods CREATE in `ferry-watch` | Deny for every `system:serviceaccount:ferry-watch:*` and `system:serviceaccount:ferry-watch-browsers:*` identity. System controllers and Flux are unaffected. This is defence in depth; the guarantee is "no ferry SA has any verb except those in §7.3", proven by the `can-i` matrix (§11.4). | M3 |

`fw-browser-pod-lock` and `fw-ns-objects` back up RBAC that already denies the same actions (C5, C6), so they
move from the draft's first milestone to M3, the milestone before non-household invitees. The draft's
`fw-browser-pod-creator` is folded into `fw-browser-pod`; `fw-hold-pdb` and `fw-anchor-guard` are gone with
the PDBs and the anchor (§7.8).

Abridged CEL for `fw-browser-pod`. The real policy uses `has()` guards, and every line has an envtest denial
case:
- `request.userInfo.username == params.controlSA || params.breakGlassGroup in request.userInfo.groups`.
- `object.metadata.name.matches('^fw-[a-f0-9]{16}-g[0-9]{1,6}$')`, and each label equals its derived value;
  `ferry-watch.io/owner.matches('^u[a-z2-7]{19}$')`.
- Annotation keys are a subset of `{fluentbit.io/exclude, kubernetes.io/egress-bandwidth}`; this denies
  `k8s.v1.cni.cncf.io/networks` (Multus), among others.
- No `ownerReferences`, no `finalizers`.
- One container. No init or ephemeral containers. Image is in `params.allowedImages`, which must contain
  `@sha256:`.
- No `command`, `args`, `workingDir`, `lifecycle`, `envFrom` or `securityContext.capabilities.add`. Probes are
  `httpGet` on `probe` only.
- Env names are exactly the 8, with no `valueFrom`. `FW_TRIP_ID` and `FW_POD_NAME` equal the label and the
  name; `FW_OWNER` equals the owner label; `FW_HUB_URL`, `FW_VNC_PUBKEYS`, `FW_VNC_ORIGIN` and `FW_SANDBOX`
  equal their params; `FW_DEADLINE_AT` matches `^[0-9]{10}$`.
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
| browser pods (CNP) | :6080 from `ferry-watch/vnc-edge` (add the `kube-apiserver` entity only if S-CIL shows vnc-edge→pod is SNATed cross-node; the gate's crypto is load-bearing either way). :8080 from entity `host` (kubelet probes). | kube-dns :53 with `rules.dns` matchName/matchPattern for `bcferries.com`, `*.bcferries.com`, `queue-it.net`, `*.queue-it.net`, the payment and 3DS names, and `ferry-control.ferry-watch.svc.cluster.local`. `toFQDNs` (same external names) on TCP 443. `ferry-watch/ferry-control` :8766. **Nothing else**: no world, LAN, nodes, kube-apiserver, other pods, smtp-relay or observability. |
| ferry-web | :8765 from envoy-external Envoy pods plus the `kube-apiserver` entity (SNAT). :9090 from Prometheus. | Postgres (`database/postgres17-rw` :5432). smtp-relay :2525. kube-dns with a DNS rule for the Postgres and smtp-relay names, the Web Push endpoints (`fcm.googleapis.com`, `*.push.apple.com`, `updates.push.services.mozilla.com`, `*.notify.windows.com`) and `www.bcferries.com` (terminal names); `toFQDNs` for those external names on 443. **Denied:** observability, both browser ports, envoy-external (no OIDC issuer needed under trusted headers). |
| ferry-control | :8766 from browser pods (`ferry-watch-browsers` + label); add the `kube-apiserver` entity only if S-CIL demands it. :9090 from Prometheus. | kube-apiserver entity :6443. Postgres. kube-dns. |
| vnc-edge | :8080 from envoy-external Envoy pods plus the `kube-apiserver` entity | kube-dns. Browser pods :6080. |
| db-init, db-migrate Jobs; cnpg `pg-backups` CronJob | none | Postgres. kube-dns. The NFS backup target (pg-backups only). |

home-cluster adds one rule to smtp-relay's NetworkPolicy admitting `ferry-watch/ferry-web` on 2525.

### 7.7 PriorityClass, ResourceQuota, LimitRange

GitOps-owned; no app identity has verbs on them. Sizes come from the capacity table (§8.4).

- `PriorityClass ferry-browser`: `value: 0`, `preemptionPolicy: Never`. Browsers never preempt the owner's
  workloads, and default-priority pods can't preempt a holding browser (a negative value would let them).
- `ResourceQuota browsers` (scopeSelector PriorityClass `ferry-browser`), rendered from values:
  - `pods = maxBrowsersTotal + 1` (one terminating predecessor);
  - `requests.memory = limits.memory = (maxBrowsersTotal + 1) × podMemory`, which helm-unittest asserts is
    ≤ `browserMemoryBudget`;
  - `requests.cpu = (maxBrowsersTotal + 1) × cpuRequest` (the draft's fixed `2` capped the fleet at 20).

  A quota hit means an accounting bug, and it pages the admin.
- `ResourceQuota require-deadline` (scope `NotTerminating`): `pods: 0`. Every pod must carry
  `activeDeadlineSeconds`.
- `ResourceQuota objects`: `services: 1` (the headless `browsers`), `configmaps: 2` (params and the
  auto-created `kube-root-ca.crt`), `secrets: 0`, `count/poddisruptionbudgets.policy: 0`.
- `LimitRange`: container max memory 2Gi, max cpu 1500m, max ephemeral-storage 256Mi.

### 7.8 Node drains, without PDBs or an anchor

**What Talos does.** A tuppr `TalosUpgrade` runs `talosctl upgrade`, which cordons and drains the node with
the kubectl drain library (`Force: true`, so bare pods are evicted; `GracePeriodSeconds: -1`, so our 90 s
grace applies) and a 5-minute default timeout. If eviction is still blocked at the timeout, the upgrade fails
without rebooting and the node stays cordoned; tuppr retries twice and marks the upgrade Failed (§2.2).

**What v1 does.**
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
matching `^wss://ferry-vnc\.chestr\.dev/t/[a-f0-9]{16}/g/[0-9]{1,6}/ws$` only and sets the protocols to
`["binary", "fw.n1.<N>"]`, plus `fw.t1.<ticket>` while a ticket is pending; it drops the ticket after the
first successful open. If the client gives up reconnecting before the hold deadline it knows from `disp`, the
shim reloads `vnc.html` with back-off (1, 2, 5, 10 s), so retries are effectively uncapped until the deadline.
The landing page sets `#path=t/<trip>/g/<gen>/ws&autoconnect=1&reconnect=1&reconnect_delay=1000&idle_disconnect=120`
and shows the sailing and deadlines with `textContent`. If the fragment's ticket has already expired (a slow
load) and this device holds no lock yet, it sends the user back to `https://ferry.chestr.dev/c/<trip>` to mint
again; with no ticket and no stored `N` it shows only a link back to the dashboard. A CI test drives the stock client in headless Chromium
against a fake gate and asserts the path and subprotocols, so a KasmVNC bump that changes this fails CI.

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
log_format noargs '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent $request_time';
server {
  listen 8080; access_log /dev/stdout noargs; server_tokens off;
  resolver kube-dns.kube-system.svc.cluster.local valid=5s ipv6=off;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'sha256-<build>' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://ferry-vnc.chestr.dev; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" always;
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
- `connect-src` names `wss://ferry-vnc.chestr.dev` explicitly because some Safari versions do not match
  `wss:` to `'self'`.
- The HTTPRoute is static and lives in the chart: host `ferry-vnc.chestr.dev`; parentRef
  `networking/envoy-external`, `sectionName: https`; `RequestHeaderModifier remove [Cookie, Authorization]`;
  backend `vnc-edge:8080`; annotation `gatus.home-operations.com/enabled: "false"`. Envoy's route idle timeout
  is already disabled cluster-wide (`BackendTrafficPolicy requestTimeout: 0s`, `envoy.yaml:169`).
- vnc-edge stays stock and logic-free: no app code, no secrets, no Lua, so its attack surface is nginx and a
  static directory (§4.5).

### 7.10 Helm chart layout and the home-cluster split

The chart is published from this repo as a **public** OCI chart, `oci://ghcr.io/mchestr/charts/ferry-watch`,
because flux-local renders it anonymously. Images are referenced as `X.Y.Z@sha256:…` and bumped by Renovate.

```
charts/ferry-watch/
  Chart.yaml  values.yaml  values.schema.json
  templates/
    control/   web.yaml control.yaml services.yaml serviceaccounts.yaml rbac.yaml
               config-configmap.yaml control-config-configmap.yaml db-init-job.yaml db-migrate-job.yaml
               networkpolicies.yaml ciliumnetworkpolicies.yaml prometheusrule.yaml podmonitors.yaml
    edge/      vnc-edge.yaml vnc-edge-configmap.yaml
    routes/    dashboard-httproute.yaml dashboard-securitypolicy.yaml vnc-httproute.yaml   # routes.enabled
    browsers/  namespace.yaml serviceaccount.yaml headless-service.yaml policy-params-configmap.yaml
               resourcequotas.yaml limitrange.yaml networkpolicies.yaml ciliumnetworkpolicy.yaml
    cluster/   priorityclass.yaml vap-browser-pod.yaml vap-browser-pod-lock.yaml vap-ns-objects.yaml
               vapbindings.yaml clusterroles.yaml
  tests/       helm-unittest suites (rbac-forbidden, vap-params == control-config, quota ≤ budget,
               route-without-securitypolicy fails, values combinations)
```

Values `routes.enabled` (default false) and `routes.dashboardHost` let M1 run on `ferry-next.chestr.dev`
without touching the live `ferry.chestr.dev` route on envoy-internal (two routes for one host would collide in
UniFi DNS; §12.5). `vaps.lock` and `vaps.nsObjects` default to false until M3.

**home-cluster owns:**
- `kubernetes/apps/ferry-watch/`: namespace directory with `components/common`; `ks.yaml` with
  `components/cnpg` (`APP=ferrywatch`) and `dependsOn: cloudnative-pg-cluster`; the OCIRepository plus
  HelmRelease (`upgrade.strategy: RetryOnFailure`, `wait: true`) and values: hosts, the FQDN list,
  `identity.providerId`, `bootstrapAdmins`, ceilings, `browserMemoryBudget`, `podMemory`, `kofiUrl`, the
  public keys.
- `ExternalSecret ferry-watch` from 1Password item `ferry-watch` (ticket key, VAPID key) and the two role
  passwords from item `cloudnative-pg`.
- CNPG managed roles `ferrywatch_web` and `ferrywatch_control` on `postgres17`, with password ExternalSecrets
  in `database`.
- The ReferenceGrant entry, the Authelia `access_control` rule and client `authorization_policies`, the lldap
  group (§6.2).
- Cilium `dnsProxy.dnsRejectResponseCode: nameError`.
- The smtp-relay NetworkPolicy rule.
- Silence CRs for `OomKilled`, `KubePodNotReady`, `KubePodCrashLooping` and `KubeQuota*` in
  `ferry-watch-browsers`.
- Fluent Bit `Exclude_Path /var/log/containers/*_ferry-watch-browsers_*.log`.
- A descheduler namespace exclusion.
- A Cloudflare Terraform rule turning Rocket Loader and email obfuscation off for the ferry hosts.
- A gatus endpoint for the forged-header canary (C29).
- A GrafanaDashboard for the ferry metrics (as `networking/envoy-gateway/app/grafanadashboard.yaml` does).
- Renovate: `/ferry-watch/` always opens a PR.
- M3: the Talos `user.max_user_namespaces` sysctl and the `machine.seccompProfiles` entry, applied per node;
  `home-cluster-runner` restrictions.

**Dropped from the draft:** the EnvoyExtensionPolicy Lua filter (not needed under trusted headers), the
ClusterSecretStore `spec.conditions` (over-built: a mistake there cuts every namespace off from its secrets,
and nothing in `ferry-watch-browsers` can create an ExternalSecret anyway), the warmer DaemonSet (its
long-running container referenced only `pause`, so the browser image could still be garbage-collected; cold
starts are measured in S-MEM instead), the anchor ConfigMap and the PDBs.

---
## 8. Lifecycle and reaping

### 8.1 State machine

```
 Trip phases (shown to users; owned by the fleet loop except where noted)

   Off <-------- disabled / paused (user, after_checkout, suspended, maintenance)
    |  enable/resume (409 if at maxActiveBrowsers)
    v
 Dormant "Starts watching Jul 2" --window opens--> Waiting(limit) "Waiting: your 1-trip limit"
    |                                                  | user pauses another trip
    +------------------------------------------------->+--> Queued(position) "Waiting for a free browser (#2)"
                                                                     | slot + start throttle
                                                                     v
 BackingOff(until) <--3 pod failures/30 min-- Starting --first check--> Watching <--+
    |  timer                                    | 15 min timeout              |      | unviewed hold ended, or
    +------------------> Queued                 v                             v      | cart expired on the site
                                             Failed -> recycle        Holding.Unclaimed --viewer--> Holding.InCheckout
                                                                              |                         |
 NeedsAttention <-- RouteRejected x3 / 5 startup failures                     +---- hold ends ----------+
                                                                              v
                                                         Draining (empty + verify unless paying, bye, exit 0)
                                                                              |
                                  viewed? --yes, BC Ferries confirmation seen--> archived, "Booked!"
                                          --yes, no confirmation--> Off(after_checkout) "Did you book it?"
                                                                    (answer, or auto-resume after 20 min,
                                                                     slot reserved 30 min)
                                          --no----> resume in place (see above)
 Past (last in-window departure passed, Pacific) -> archived after 7 d
```

**Pod phases** (`browser_pods`, `trip_runtime`): Creating → Starting → Watching → Holding → Draining → Gone,
plus Failed. At most one pod per trip exists at a time. Every start is a new pod `fw-<trip>-g<n+1>`, so
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
| `maxActiveBrowsers`: trips watched at once (Waiting, Queued, Starting, Watching, Holding), owner decision | 1 | 3 | web (409 at enable), fleet loop |
| holds at once | = `maxActiveBrowsers` (one cart per pod) | — | agent |
| `startWindowDays` (later → Dormant) | 30 | 120 | fleet loop |
| `tripHorizonDays` (latest allowed date) | 120 | 180 | web |
| check interval, user preference (today min 15 s, default 30 s: `models.py:19`, `:284`) | 30 s | floor 30 s = compiled floor; the budget stretches it (§9.3) | web, control, agent |
| `maxHoldsPerDay` (Pacific) | 4 | 10 | control (`auto_cart`) |
| `maxCartAttemptsPerDay` | 6 | 12 | control; agent ≤ 6 per pod |
| `vncMinutesPerDay`: checked before carting (no cart when minutes left < `session_s`); never closes an open session | 60 | 120 | control (`auto_cart`), web (mint) |
| quiet hours: alert only, no carting | 23:00-07:00 Pacific, on | user may turn off | control (`auto_cart`) |
| VNC ticket mints | 10/min | — | web |
| manual checks (ignored if a check ran < 15 s ago) | 6/h | 20/h | web, agent |
| test alerts | 6/h | 6/h | web |
| **Hold clocks** | | | |
| unclaimed, from control ack (reminder at 15 min) | 30 min, and ≤ BC Ferries' cart TTL − 5 min once S6 measures it | compiled 45 | agent; operator backstop |
| checkout session, from first connect (warning at T−5) | 30 min (set to S2b's measured phone-checkout p90 + 5 min) | compiled 45 | agent gate |
| viewer gone, then release | 10 min | compiled 15 | agent |
| payment guard: session and viewer-gone paused while a frame is on a payment/3DS host or payment was submitted | on | hard cap still applies | agent |
| hard hold cap, from READY (replaces `BCF_CART_HOLD_MINUTES=60`/0, `settings.py:30`) | 60 min | compiled 90 | agent; operator at +5 min; Xvnc `-MaxConnectionTime 6000` |
| cart liveness check while unclaimed | every 4 min (only if S6 shows it does not extend the site TTL past policy) | — | agent |
| "Did you book it?" auto-resume / slot reservation | 20 min / 30 min | — | control / fleet loop |
| **Cluster** (§8.4) | | | |
| `browserMemoryBudget` | 12 GiB (owner range 8-24 GiB) | — | chart (quota) |
| `podMemory` (request = limit) | 1.5 GiB → `ceil(p95 working set incl. tmpfs × 1.2)` after S-MEM | LimitRange 2 GiB | VAP, quota |
| `cpuRequest` | 200m | limit 1500m | VAP, quota |
| `maxBrowsersTotal` | `floor(budget / podMemory) − 1` = 7 | quota pods = cap + 1 | fleet loop, quota |
| concurrent starts / starts per hour / per-user spacing | 2 / 30 / 20 s | — | fleet loop |
| `checksPerMinute` (budget) | 20 | 40 | control |
| **Pod** | | | |
| heartbeat (`state`) / UI stale | ≤ 15 s / 60 s | — | agent / web |
| Watching dead after silence | 5 min (only when the hub endpoint has been up ≥ 2 min) | — | fleet loop |
| Holding dead after silence | never; only the operator's clock or kubelet | — | fleet loop |
| startup timeout (create → first check; `CHECK_TIMEOUT_SECONDS`, `watcher.py:48`) | 15 min | — | fleet loop |
| Pending / unschedulable | 5 min → delete, Queued, admin alert | — | fleet loop |
| max age (only Watching and idle) | about 24 h −10 % | — | agent (`FW_DEADLINE_AT`) |
| `activeDeadlineSeconds` | 93600 (26 h) | VAP ≤ 93600 | kubelet, quota |
| no new cart within … of the deadline | 100 min | compiled | agent |
| no new cart without a control ack within | 30 s | compiled | agent |
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
| dashboard WebSocket lifetime | 60 min | — | web |
| sign-in session | Authelia's: 3 d idle / 7 d, or 6 months with remember-me (cluster-wide) | — | Authelia |
| **Retention** | events 30 d; outbox 14 d; pod history 30 d; audit and usage 400 d; archived trips deleted 90 d after the sailing; delete-my-data immediate | | db jobs (control) |

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
  change only at natural recycles (max age, after a hold, failure), never during a hold. `allowedImages` holds
  2 digests during the overlap.
- **Ticket key rotation:**
  1. Add the new public key next to the old one (pods created from then on carry both).
  2. After 26 h, switch web to the new private key.
  3. After another 26 h, drop the old public key.
- **DB:** CNPG failover makes both control processes reconnect with back-off. Agents are unaffected.

### 8.4 Capacity

One browser per actively watched trip, and by default one actively watched trip per user, so the number of
browsers is the number of people being served at once.

`maxBrowsersTotal = floor(browserMemoryBudget / podMemory) − 1`, and the quota is `(maxBrowsersTotal + 1) ×
podMemory ≤ browserMemoryBudget` (one terminating predecessor of headroom; a trip's successor pod is created
only after its predecessor is gone).

| Budget | podMemory 1.0 GiB | 1.25 GiB | 1.5 GiB (start) |
|---|---|---|---|
| 8 GiB | 7 | 5 | 4 |
| 12 GiB (start) | 11 | 8 | 7 |
| 16 GiB | 15 | 11 | 9 |
| 24 GiB | 23 | 18 | 15 |

- `podMemory` must cover the processes **and** the memory-backed `/tmp` and `/dev/shm`, because Kubernetes
  charges tmpfs pages to the container's memory cgroup. The research measured 0.52-0.57 GB of process memory
  per pod on light pages [R:map-k8s-research §g]; real BC Ferries pages, the in-memory profile and cache
  (capped at 32 MiB) and `/dev/shm` add to that. S-MEM measures the cgroup working set
  (`container_memory_working_set_bytes`, `memory.peak`) while watching and during a checkout with a viewer.
  The start value of 1.5 GiB is deliberately conservative; it drops to the measured value after S-MEM.
- If the owner's "8-24 GB" meant decimal gigabytes, use 7.45-22.35 GiB in the formula (one or two fewer
  browsers per row). decisions.md's "roughly 7-23 users" is the 1.0 GiB column.
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
   `{field, limit, usage}`. Covers trips, active browsers, horizon, interval, manual checks, test alerts and
   mints.
2. **ferry-control fleet loop, continuous.** Caps, the queue, start throttling, the hold backstop clock and the
   reaper.
3. **ferry-control hub endpoint.** Holds per day, cart attempts per day, VNC minutes, quiet hours, the budget.
4. **The agent.** Compiled maxima (§5.4) and local timers. Works with the control plane down.
5. **Kubernetes backstop, GitOps-owned.** ResourceQuota, `NotTerminating pods: 0`, LimitRange, PriorityClass
   and the VAP (§7.5-7.7).

### 9.3 Queueing and fairness

- **Memory slots.** No Pending pods and no preemption. Queue order (§6.6): reserved slot, then fewest running
  browsers, then nearest sailing, then oldest queued.
- **BC Ferries budget (D12).** ferry-control computes, for every Starting or Watching pod:

  ```
  n        = pods Starting or Watching
  base     = ceil(60 × n / checksPerMinute)          # checksPerMinute default 20, ceiling 40
  interval = min(3600, max(user_pref, 30, base) × brake.multiplier)
  ```

  - At 20 checks/min everyone gets 30 s up to 10 pods, and 60 s at 20 pods. The draft's separate 60 s user
    floor is removed: it halved detection against today's 30 s while most of the budget sat idle, and the
    stretch already slows everyone as the fleet grows. If an opening stays bookable for T seconds and checks
    are I seconds apart, the chance of seeing it is about min(1, T/I).
  - New intervals are pushed in `configure` only when they change by more than 10 %. The agent keeps ±15 %
    jitter and a token bucket (burst 2). The dashboard shows "30 s requested · 45 s now".
  - Pod starts cost about 4-5 navigations each [R:limits-reaping-lifecycle] and are throttled separately.
  - Cart flows (add-to-cart, liveness, empty, drain) bypass the bucket and are capped per day.
  - Today the owner runs up to 6 trips at 30 s, about 12 refreshes/min. The agent reports `opening_seen` and
    `opening_gone` events, which give `ferrywatch_opening_visible_seconds`; a cheap version of that metric is
    added to the current deployment in W0, so the 30 s default can be checked against real data.
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
- **M4, only if data warrants it:** weighted max-min water-filling with AIMD leases; a Standby (cold) tier if
  `Queued` becomes common; a static priority that favours trips sailing within 7 days.
- **Powell River–Comox:** BC Ferries' waitlist auto-books on this route
  [R:billing-paid-service-implications], so openings may never be public. The dashboard shows users of that
  route a note suggesting they also join the waitlist.

### 9.4 Admin UI and API (v1)

All admin routes require `is_admin` (or `bootstrapAdmins`). Every mutation requires `reason` and writes
`audit_log` plus a JSON audit log line from ferry-web, so VictoriaLogs keeps a copy the DB can't rewrite.
**Admins never get VNC** into tenant browsers, and see no tenant sailings beyond the cart summary.

| Page | Shows | Actions |
|---|---|---|
| Fleet (admin home) | capacity (memory used/budget, browsers by phase, queued), budget (navigations/min, brake, breaker), holds now (user, route, countdown), attention list (NeedsAttention, image skew, delivery failures, `release_unverified`, `checkout_blocked`) | maintenance on/off, **pause carting** (for Talos upgrades), close the breaker, release a cart, recycle a pod |
| Users | state, effective limits with provenance, usage today, last seen, push/email status | add person (sends `invited`), suspend (drains pods; holds released or allowed to finish, by choice), set overrides, make admin (bootstrap admins are locked), delete user |

**API:** `GET /api/admin/overview`; `GET/POST/PATCH/DELETE /api/admin/users[/{id}]`;
`POST /api/admin/trips/{id}/{release|recycle}`; `POST /api/admin/fleet/{maintenance|pause-carting|breaker}`.

The draft's Trips, Settings and Audit pages, the DB `policy` table and the request-access queue are deferred
to M4 (§12.2). Until then audit is read with `psql` or from VictoriaLogs, and defaults change through a values
PR.

### 9.5 Audit and ops signals

- **Audited:** every admin mutation; user trip create, edit and delete; every VNC mint; and system actions as
  `system:control` (start, recycle, reap with reason, queue, brake/breaker transitions, binding violations).
- **Metrics** (no user, trip or pod labels; Prometheus is capped at 2 GiB):
  - `ferrywatch_browsers{phase}`, `ferrywatch_queue_depth`, `ferrywatch_memory_{requested,budget}_bytes`;
  - `ferrywatch_bcf_navigations_total{kind}`, `ferrywatch_queueit_detours_total`, `ferrywatch_brake_multiplier`,
    `ferrywatch_breaker_state`;
  - `ferrywatch_holds{state}`, `ferrywatch_holds_total{outcome}`, `ferrywatch_checkout_host_blocked_total`;
  - `ferrywatch_alerts_total{channel,result}`, `ferrywatch_reaper_actions_total{reason}`,
    `ferrywatch_vap_denials_total`;
  - histograms for check seconds, pod-ready seconds, agent working set and `ferrywatch_opening_visible_seconds`.
- **PrometheusRules** go to the owner's Alertmanager: BreakerOpen, Blocked, PodsPending (> 5 m),
  PodCreateDenied (VAP or quota), AgentsSilent (> 50 % for 5 m), WebDown/ControlDown, HoldsLost (≥ 2 in 1 h),
  HoldingPodMemoryHigh (> 85 %), CheckoutHostBlocked, AlertDeliveryFailing (> 10 % for 15 m), QuotaBackstop
  (≥ 95 %), ReleaseUnverified, IdentityCanaryFailing (from gatus).
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
   - Add `me{id, name, email, is_admin, paused, interval_pref, quiet_hours, push_enabled}`,
     `limits{value, source}`, `usage`, `capacity{can_add, reason}`, `links{kofi}`, and per-trip
     `browser{phase, reason, queue_position, starts_on, effective_interval, next_due, heartbeat_at, lockdown_ok}`.
   - `cart` gains `unclaimed_deadline`, `session_deadline`, `hard_deadline`, `payment_in_progress` and
     `viewer{connected}`.
   - Remove the global `checkout_url`, `login_configured`, `alerts_configured` and `max_trips`.
3. **Routes:** path routes `/` (my trips), `/c/:trip` (checkout chooser), `/settings` and `/admin`, with a
   small router. The server serves `index.html` on those exact paths.
4. **Identity plumbing:** no sign-in screen of our own (Authelia's page does it). A 401/403 or an opaque
   redirect on any fetch reloads the page; a 403 for a non-active user shows "Your access is being set up" or
   "Your account is suspended". `<link rel="manifest" crossorigin="use-credentials">` (`web/index.html:10`).
5. **Components:**
   - Replace `WatcherCard` (`WatcherCard.tsx:159-238`) with an **Account card**: push subscribe and status,
     a test alert, "pause all my trips", interval preference (choices from 30 s), quiet hours, "Signed in
     through Authelia; tick Remember me", delete my data, and the Ko-fi link.
   - Per trip: a "next check in m:ss" chip, the effective interval, and a phase badge with plain wording:
     "Starts watching Jul 2" (Dormant), "Waiting: your 1-trip limit", "Waiting for a free browser (#2)",
     "Starting", "Watching", "Holding a cart", "Needs attention". The trip form shows the same reason at
     create time.
   - `/c/:trip`: the held sailing, the deadlines, and **Check out on this device** / **I'll use my computer**
     / **Release and book it myself**, with "Checkout will be locked to the device you choose". No automatic
     POST.
   - `CartBanner` (`CartBanner.tsx:44`, `:112-113`, `:136-140`): per-trip "Open checkout" (goes to `/c`),
     the unclaimed and session countdowns, "paused while paying", the guest wording, and "Release".
   - "Did you book it?" and "Booked!" screens; the latter carries the Ko-fi link.
   - `TripSheet` (`TripSheet.tsx:317`, `:470-500`): auto-cart is always available (guest). Remove the
     `BCF_USER` copy. Show 409 limit errors, including "Pause *X* to watch this one".
   - `Header` (`Header.tsx:59-86`): the global "Watch all trips" switch becomes a user menu (pause mine,
     settings, sign out via Authelia's logout, admin).
   - The "watcher stalled" banner (`App.tsx:34-36`, `Banners.tsx:119-125`) becomes per trip, with a separate
     admin-only control-plane health banner.
   - A Powell River–Comox note pointing to BC Ferries' waitlist.
6. **Service worker** (new, `web/public/sw.js`, M2): Web Push display, and `notificationclick` building the URL
   from `data.trip` against `self.location.origin + '/c/'`. CSP is unchanged (`worker-src` falls back to
   `script-src 'self'`).
7. **Onboarding page** (`/settings#start`, linked from the `invited` email): add to Home Screen, enable
   notifications from inside the app, allow Ferry Watch in Focus/Sleep, send a test alert.
8. **Errors:** Envoy replaces 502-504 bodies with HTML [R:cluster-networking], so 5xx bodies are never parsed
   as JSON.
9. **Tests:** update the fixtures every suite uses; rewrite the `WatcherCard` and `watcher.ts` tests; add tests
   for the router, the `/c` chooser, the Account card and 401 handling.
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

New dependencies are `msgspec`, `asyncpg`, `lightkube`, `PyJWT[crypto]` (Ed25519) and `pywebpush` (or
`http-ece` plus `py-vapid` on aiohttp). Each is checked against `filterwarnings = ["error"]`
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
- **CI-only:** `ferry-watch-agent-e2e` carries the marker file that alone enables `FW_E2E_BASE_URL` and the
  127.0.0.1 allow policy; `fake-bcferries` is extracted from `tests/browser/test_site_flow.py` into a runnable
  server (with confirmation, expired-cart and 3DS-iframe pages).

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
  dev identity proxy. It runs ticket mint, gate, RFB banner and the refusals; a full guest hold; restarts of
  web and control mid-hold with zero carts lost and every cart alert delivered once; the SIGTERM drain.
- `kind-lite` (main, tags, and any PR touching `charts/`, the agent or the browser image): kind with its
  default CNI, the chart, one pinned pod; the VAP positive cases that need a real kubelet and GC (orphan and
  foreground deletion, eviction, NoExecute taint eviction, GC of Failed pods); eviction-driven SIGTERM drain.
- `skew`: from the first protocol change after v1.
- The Playwright pin check runs against every FROM line that carries it.

The draft's kind e2e with Cilium and Envoy Gateway is dropped: kind cannot reproduce netkit, native routing,
kube-proxy replacement, cross-node SNAT or real DNS for `toFQDNs`, which are exactly the behaviours that
matter. They are proven on the home cluster (§11.4 tier 6).

### 11.4 Test tiers

1. **Unit:**
   - the one-trip watcher with `tests/fakes.py`: HoldPolicy clocks and the payment guard, compiled maxima,
     `alerted` versus `offered`, the lost-after-viewer rule, exit after a viewed hold, `_close_all` emptying,
     verified `empty_cart`, booking detection, the control-ack gate on carting;
   - protocol codec plus hypothesis fuzzing (unknown types and fields, oversize, wrong trip, stale rev);
   - identity: `TrustedHeaderProvider` parsing, uid derivation, invited→active, suspended, missing header;
   - repository scoping: a foreign trip gives 404 on every route; per-user Hub payloads;
   - the gate against a fake Xvnc unix socket: owner, cart, Origin, jti replay, lock, two simultaneous first
     viewers (exactly one wins), reconnect with `N` replacing a stale socket, ticket refused after the lock,
     revoke closes within 1 s, missed pong drops the viewer, failed-upgrade limit, `/api` and non-upgrade
     requests give 404;
   - a `browser_pod()` snapshot;
   - the fleet loop with `FakeCluster`: admission order, reserved slots, throttle, adoption, silence rules
     frozen after a restart, cordon handling, failed-pod retention;
   - budget and brake properties: the sum of rates stays ≤ budget, the brake persists, fail-slow.
2. **Postgres-backed:** a concurrent-POST quota race (trips and active browsers); RLS (web can't insert a
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
   - pod CREATE by a non-control SA, and by ferry-control in `ferry-watch`;
   - (M3) pod UPDATE, ephemeralcontainers, exec/attach/portforward CONNECT; Secret, Service, ConfigMap,
     HTTPRoute, Deployment and Job creation by app SAs.

   Plus **`can-i` negatives** for every forbidden verb in §7.3, for each app SA, in both namespaces.
4. **compose integration:** §11.3.
5. **kind-lite:** §11.3.
6. **Home-cluster post-deploy smoke** (after every deploy; the fleet loop stays in maintenance until it passes):
   ferry-control starts one pod with the reserved smoke trip id, which runs the egress matrix from inside the
   real CNP: *positive* bcferries.com and Queue-it through `toFQDNs` (with the Python resolver and with
   Chromium), the control endpoint by its FQDN; *denials* for the kube-apiserver, kubelet 10250, a node IP,
   169.254.169.254, another browser pod, ferry-web :8765, smtp-relay and `example.com`; and the lockdown
   self-tests. Hubble shows the denials. An echo check confirms no ferry backend receives Cookie or
   Authorization. The `can-i` matrix runs against the live cluster.

### 11.5 Security invariants that block CI

- The VAP leak matrix and `can-i` negatives. A pod-template change without a matching matrix update fails.
- A helm-unittest forbidden-RBAC list for every SA; the dashboard route never renders without its
  SecurityPolicy; quota ≤ budget.
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
  a local container), agent-e2e against fake-bcferries, vnc-edge, and a dev proxy that injects `Remote-User`.
  It needs about 4 GiB RAM and 2 CPUs. Each browser container adds about 1.5 GiB.
- **Chart and admission work:** kind (default CNI) plus envtest; about 6 GiB RAM.
- **Reproducing a live failure:** run the browser image locally against the live site with `FW_TRIP_ID` set and
  a local fake control endpoint; debug snapshots stay in the container's tmpfs.

---
## 12. Delivery plan

### 12.1 Spikes

Live-site spikes run from the owner's workstation with `BCF_USER`/`BCF_PASS` unset, on sailings weeks out with
plenty of space; they never hold sold-out inventory. Spike days are counted in the calendar (§12.3), not run
"in parallel" with code.

| Spike | When | Exact experiment | Pass | If it fails |
|---|---|---|---|---|
| **S1** guest cart (live) | W0 | `new_session → search → add_to_cart` in a fresh context, including for each of the owner's current trips. Record the final URL, cookie names, domains, expiry and HttpOnly flags, `userType`, and any visible cart countdown. | The review page shows the sailing and fare with no login. | Stop: re-plan auto-cart. |
| **S2** guest checkout path (live, stop before paying) | W0 | Click through from review. Log every URL, heading, "Continue as guest"/"Sign in" choice, required field, the card iframe origin, and whether 3DS triggers. Capture a HAR (credentials scrubbed) and every host for the allowlist. | The payment page is reachable without signing in. | Ship **alert-only** (Q3); M5 becomes the carting path. |
| **S2b** one real booking (owner's card, needs Q2) | W0 | A cheap foot-passenger guest booking, cancelled within the free 30 min through the email link. Done on the owner's phone through today's VNC (over WireGuard), timed end to end. Record the confirmation page and a selector for it. | Confirmation, then cancellation; the phone-checkout time sets the session default (§8.2). | As S2. |
| **S5** release | W0 | After a guest cart, run `empty_cart()`. Record the landing URL, reopen the cart and confirm it is empty; check that the sailing's capacity returns if possible. Write the verification selectors. | Provably empty, with selectors that detect it. | Holds stay off until release is verifiable. |
| **S6** BC Ferries cart TTL | W0 | Leave a guest cart idle and re-check every minute. Record the TTL; whether a reload or the site's "continue" action (`cartTimerContinueUrl`) extends it; and what an expired cart looks like (selectors). | TTL recorded, liveness check designed. | Unclaimed ≤ TTL − 5 min; unclaimed + session ≤ TTL if it can't be extended. |
| **S7** coverage | W0 | Repeat S1 for tall vehicle, walk-on and each fare type (Saver, Prepaid, Reservation Only), and for the fare types on the owner's current trips. | Note which need an account (`vehicle.html:584-587`). | Those trips are alert-only (Q3b). |
| **S-FIX** fixtures | W0 | Capture a "date beyond schedule" page, a block or challenge page, an expired cart and the confirmation page. | Fixtures exist; `NotYetBookable`, `Blocked`, expired-cart and confirmation classifiers are written. | Classify conservatively as MarkupChanged. |
| **S-KIOSK** (local, Playwright 1.56 under matchbox) | W0 | Policies in 3 dirs plus `--kiosk` plus a persistent context. Try escapes (Ctrl+T/N/O/U/Shift+I, closing the last window). Run the self-tests (§5.6) including the LNA policy names. Xvnc with `-rfbunixpath` and no WebSocket listener; a prototype gate bridging WS ↔ unix socket; the stock KasmVNC client through the shim. Does `URLBlocklist ["*"]` block a cross-origin subframe? Check `handle_sig*=False` plus the supervisor drain ordering. Type into a 6-field form with phone keyboards' burst patterns (no `DLP_KeyRateLimit`). | The CDP pipe works under the policies. Every escape is blocked. No TCP listeners but the gate's and the probe server's. The stock client connects through the shim. TERM reaches only the agent and the cart is emptied before Chromium stops. Typed values are byte-exact. | No pod-served VNC until it passes; fall back to Xvnc WebSocket on 127.0.0.1:6081 behind a per-boot BasicAuth credential, with self-tests 2-3 blocking. |
| **S-VNC-lite** (cluster) | W0 | A throwaway pod running the S-KIOSK gate prototype and showing a static test page (never today's unauthenticated container, never a BC Ferries session) behind a static HTTPRoute on envoy-external under a random path, through the Cloudflare tunnel. From iPhone Safari, the iOS home-screen app (opened from a notification) and Android Chrome: background the browser for 90 s (bank-app approval), switch Wi-Fi to LTE, leave a static page for 3 min; with 20 s pings. Where does `N` live on the iOS home-screen path, and does a return via the dashboard find it? Exact CSP (QOI on/off). Rocket Loader off. Gate CPU at 15 fps. | Reconnects within 10 s in every case; `N` survives the return on all three clients; gate ≤ 0.3 CPU. | If iOS loses `N`, Q5b; if Cloudflare cuts sockets despite pings, shorten the ping. |
| **S-CIL** (cluster) | W0 | Apply `nameError` (home-cluster). Check `cilium config view` for the DNS proxy. Apply the browser CNP to a test pod. Then: `toFQDNs` reach bcferries.com with the Python resolver and in Chromium; the control endpoint resolves by its FQDN (with and without the trailing dot); DNS for other names fails; the deny matrix holds (apiserver, LAN, nodes, pods, smtp-relay, observability). With Hubble, check whether vnc-edge→pod and pod→control traffic is SNATed cross-node. Check the bandwidth annotation with netkit. | All of these work. | `toCIDRSet` fallback plus `kube-apiserver`-entity ingress where SNATed; a per-pod egress proxy moves from M4 to M3. |
| **S-AUTH** (cluster; replaces S-COOKIE) | W0 | The chart's dashboard route plus SecurityPolicy on `ferry-next.chestr.dev`, pointed at an echo backend. Unauthenticated → Authelia redirect; a user outside `ferry-watch` → 403; a valid session → echo shows `Remote-*` and no Cookie/Authorization; a forged `Remote-User` without a session → redirect; a forged `Remote-User` with a valid session → overwritten; the WebSocket upgrade passes; the manifest loads with `crossorigin="use-credentials"`; requests with several Cookie headers and from an HTTP/2 client reach the echo with no Cookie at all. | All as stated. | Q0 option B (app-native OIDC). |
| **S-TR** (cluster) | W0 | TokenReview with audience returns `extra` pod-uid and pod-name. VAP with ConfigMap params. VAP CONNECT denies exec (needed in M3). | All work on 1.36.5. | RBAC-only for CONNECT; note it. |
| **S-SBX** (one node) | W0 | Apply the Talos `user.max_user_namespaces` change and a `machine.seccompProfiles` Localhost profile (containerd default plus `clone`/`unshare` with namespace flags and `chroot`). Run `chromium_sandbox=True` in a PSA-restricted pod under that profile; separately, `hostUsers: false`. | `chrome://sandbox` shows the namespace sandbox and seccomp-BPF; the exact syscall additions are recorded. | Q4: household only until solved. |
| **S-MEM** (cluster) | W0 | Cgroup working set (`memory.peak`, `container_memory_working_set_bytes`, including tmpfs) on real pages while Watching and during checkout with a viewer; CPU; pod create → first check p50/p95 with a cold and a warm image. | Numbers recorded. | Sets `podMemory`, `cpuRequest` and `maxBrowsersTotal` (§8.4). |
| **S-DB** | W0 | The cnpg component with `APP=ferrywatch`; the stock `postgres-init` creates the DB; CNPG managed roles appear; `db-migrate` grants, RLS and trigger work; LISTEN/NOTIFY through `postgres17-rw`. | Works. | One login role for both processes, as a stated residual. |
| **S-DRAIN** | M1 | `talosctl reboot --drain` (or a TalosUpgrade) on one node in a quiet window, with one Watching pod and one Holding pod on a far-out sailing. | Watching pod moved; the hold released in order with an alert; the reboot proceeds. | Fix the agent drain before M1 exit. |
| **S-PUSH** | M2 | Web Push on an iOS 16.4+ home-screen app and on Android; notification click → `/c/<trip>`. With the Authelia session expired, tap a notification in the iOS home-screen app, complete login with a second factor, and check the session lands in the app's own cookie jar (no loop) and `/c` works. `sw.js` loads behind ext-auth. SES: Authelia's one-time code reaches an invitee address. | All as stated. | Email-first onboarding; SES identity per invitee. |
| **S-3DS** | M3 | Household cards from at least 3 issuers and the 3 networks: walk checkout to the 3DS step (completing only where a real, cancellable booking is acceptable), with Hubble DNS visibility on the pod. | ACS hosts recorded; Q13 decided. | Non-household invitees wait, or Q13 option (b). |
| **S-VNCID** | M4 | §12.2 M4. | — | Q5 stands. |
| **S-IDP** | at an IdP change | oauth2-proxy (or the chosen proxy) as an EG ext-auth backend with `--upstream=static://202 --set-xauthrequest`, `/oauth2/*` on its own route, redirect on sign-in (§6.2.6). | Works with the new IdP. | Path 2 (app-native OIDC). |
| **S3, S4** | M5 | Sign-in over a guest cart; account-session longevity [R:billing-guest-mode-credentials "spike plan"]. | As there. | M5 stays out. |

### 12.2 Milestones (each one shippable)

**W0: De-risk (about 2 weeks).** The W0 spikes above, plus three small PRs: Cilium `nameError` (home-cluster);
the frontend `.catch('unknown')`; an opening-visibility metric on the current deployment (background data for
the interval default, not a gate). Exit: every W0 spike has a pass or a chosen fallback; Q0-Q4, Q13 and Q14
are answered.

**M1: The owner on the new stack (about 11.5-14 weeks).**
- Deliverables:
  - **Code split:** uv workspace, `urls.py` out of `site.py`, import-linter.
  - **Agent and pod runtime:** OneTripStore, HoldPolicy with the payment guard, compiled maxima, guest
    auto-cart, `alerted`/`offered`, verified `empty_cart`, `_close_all` emptying, liveness checks, booking
    detection, the lost-after-viewer rule, exit after a viewed hold, typed alerts and events, the new error
    classes; the supervisor (Xvnc on a unix socket, `tini --`, SIGTERM drain, termination message), kiosk
    policies and self-tests; the gate.
  - **ferry-control:** hub endpoint (binding, protocol, `trip_state`, commands, budget, brake) and fleet loop
    (reconcile, queue, timers, reaper, cordon handling, adoption, failed-pod retention, smoke pod).
  - **ferry-web:** `TrustedHeaderProvider`, user-scoped repository and API, per-user hub, ticket minting,
    `/c/<trip>` chooser, outbox with email, the frontend (§10 items 1-5, 7-9).
  - **vnc-edge** (stock client, shim, landing page, CSP).
  - **Postgres:** schema, roles, RLS, trigger; the import CLI.
  - **Chart:** one VAP (`fw-browser-pod`), quotas, LimitRange, PriorityClass, policies, db Jobs, routes on
    `ferry-next.chestr.dev`.
  - **CI:** python, apiserver (leak matrix, `can-i`), chart, image, compose, kind-lite.
  - **home-cluster PR 1:** the `ferry-watch` namespace with components, the OCIRepository and HelmRelease,
    ExternalSecrets, CNPG managed roles, the ReferenceGrant entry, the Authelia rule, the smtp-relay rule,
    silences, the Fluent Bit and descheduler exclusions, the Cloudflare rule, the gatus canary, the
    GrafanaDashboard.
  - **Cutover** (§12.5), with the legacy app scaled to zero, not deleted.
- Exit criteria:
  - The compose tier passes restarts of web and control during holds and a drain during a hold, against
    fake-bcferries, with 0 carts lost and every cart alert delivered once.
  - The post-deploy smoke is green on the home cluster; S-DRAIN done.
  - At least 1 real guest checkout through VNC on the owner's iPhone (owner's card, cancelled in the free
    window), with one ferry-control restart during that hold.
  - The owner runs only the new stack for 7 days.

**M2: Household invitees (about 3-3.5 weeks; the owner's soak overlaps).**
- Deliverables: Web Push, the service worker and the onboarding page (S-PUSH); admin Fleet and Users pages,
  "Add person" and the `invited` email, limits with provenance, suspend, delete user; delete-my-data; brake and
  the simplified breaker; the cross-tenant suite; home-cluster: Authelia client `authorization_policies` and
  the lldap group **before anyone new is added to lldap**; each invitee's email deliverable; the tenant
  disclosure (R1, R4 naming Cloudflare) in the invite email and an About page; one restore rehearsal.
- Exit criteria: the cross-tenant suite is green (API 404s, WS payloads, VNC IDOR, Origin, replay, lock, second
  viewer, after release); the quota race test passes; up to 3 household invitees for 14 days with no Pending
  pods and admin time under 10 min/day; Q5 answered. The legacy app is deleted 14 days after cutover (§12.5
  step 7).

**M3: Non-household gate (about 2-2.5 weeks; the household period overlaps).**
- Deliverables: the Chromium sandbox (Talos sysctl and seccomp profile applied per node by the owner; VAP
  params `sandbox`, `seccompProfile`, `requireUserNamespace`; `hostUsers: false`); S-3DS and the resulting
  allowlist (Q13); the `fw-browser-pod-lock` and `fw-ns-objects` VAPs with kind-lite positives and the live
  `can-i` matrix; `home-cluster-runner` restricted; SES production access; Q14 applied if chosen.
- Exit criteria: every item in §12.4 is green; 3-5 non-household invitees for 14 days.

**M4: Hardening backlog** (each item ships alone, ordered by observed need):
- Device handoff ("continue on another device" QR code).
- S-VNCID: an EG OIDC SecurityPolicy on the VNC route with the token carried correctly — remove only Cookie,
  have Envoy set a dedicated header (for example `X-FW-IdP-Token`) after deleting any client copy, let nginx
  forward it only on `/ws`, verify it in the pod against a JWKS in VAP params, test forged-header overwrite
  and token refresh at the upgrade (a 302 there breaks the socket).
- Hold PDBs plus TalosUpgrade `drainTimeout`/`timeout`, or a tuppr health check (§7.8).
- Breaker canaries; weighted water-filling with AIMD leases and a fleet simulator; a Standby tier; a
  near-sailing priority.
- TLS or Cilium WireGuard on :8766 (R10); a per-pod rate-limiting egress proxy (R13).
- cosign signatures, SBOMs and admission-time verification.
- Admin Trips, Settings and Audit pages and a DB policy table; the request-access queue (with a social IdP);
  the `identityAliases` CLI.
- A warmer DaemonSet if cold starts hurt (long-running container on the browser image, automount false, no
  secrets).
- A one-time "+10 min" session extension if data shows sessions ending early; linked outbound/return pairs.
- The protocol skew CI job (from the first protocol change); arm64 if ever needed.

**M5: Optional attended sign-in (about 1.5 weeks).** Only if Q1 is yes, S3/S4 pass, and the M3 sandbox is on.

### 12.3 Effort and calendar (one developer, focused days)

| Milestone | Workstream | Days |
|---|---|---|
| W0 | Live spikes S1, S2, S2b, S5, S6, S7, S-FIX | 3-4 |
| | S-KIOSK (with the gate and bridge prototype) | 2 |
| | S-CIL, S-VNC-lite, S-AUTH, S-TR, S-SBX, S-MEM, S-DB | 5-6 |
| | Three small PRs | 0.5-1 |
| | **W0 total** | **10.5-13** |
| M1 | Workspace split, `urls.py`, import-linter | 2 |
| | One-trip agent | 9-11 |
| | Supervisor, Xvnc unix socket, kiosk policies, self-tests, SIGTERM drain | 4-5 |
| | Gate | 3-4 |
| | Protocol, hub client, fake hub | 3 |
| | Images (browser, vnc-edge, agent-e2e, fake-bcferries) | 3-4 |
| | ferry-control hub endpoint | 4-5 |
| | ferry-control fleet loop | 5-6 |
| | Postgres schema, migrations, roles, RLS, trigger, import CLI | 3 |
| | ferry-web (identity adapter, scoped API, per-user hub, tickets, `/c`, outbox and email) | 5-6 |
| | Frontend (§10 items 1-5, 7-9) | 5-6 |
| | Chart, VAP, envtest matrix, `can-i`, helm-unittest, network policies | 5-6 |
| | CI compose and kind-lite jobs | 2-3 |
| | home-cluster PR, deploy, post-deploy smoke, S-DRAIN, cutover | 4-5 |
| | **M1 total** | **57-69** |
| M2 | Web Push, service worker, onboarding, S-PUSH | 3-4 |
| | Admin Fleet and Users, Add person, limits with provenance, suspend, delete | 4-5 |
| | Delete-my-data, retention jobs | 1-1.5 |
| | Brake persistence, breaker | 1.5 |
| | Cross-tenant suite | 2 |
| | home-cluster Authelia policies, disclosure, restore rehearsal | 1.5-2 |
| | Soak fixes | 1-2 |
| | **M2 total** | **14-18** |
| M3 | Sandbox (profile, params, `hostUsers`, tests) | 3 |
| | S-3DS and allowlist changes | 2-3 |
| | Lock and ns-objects VAPs, kind-lite positives, live `can-i` | 2-2.5 |
| | Runner hardening, SES, Q14 if chosen | 1.5-3 |
| | Soak fixes | 1-1.5 |
| | **M3 total** | **9.5-13** |
| | **Total to non-household invitees** | **91-113 days ≈ 18-22.5 weeks** |

- The owner is on the new stack after W0 + M1: 67.5-82 days, about 13.5-16.5 focused weeks.
- Calendar time adds the owner's 7-day soak (overlapping M2 work), the 14-day household period (overlapping M3
  work) and the 14-day non-household period: about 21-26 calendar weeks to the end of M3.
- For calibration: ops-scale-fairness estimated 15-17 weeks plus spikes, security-maximal about 20 weeks, and
  lean-home-cluster 35-45 focused days for a much smaller scope (SQLite, one process, k3d). The draft's 12
  weeks double-counted spike time and understated the control plane.
- If the owner wants the household sooner, the cheapest further cuts are: email-only alerts for the household
  (Web Push moves to M3, −3 days) and managing household limits by values PR instead of the Users page
  (−3 days). Neither weakens a security guarantee.

### 12.4 Minimum secure v1 (all required before the first non-household invitee)

- Guest-only, no BC Ferries credentials anywhere; alert-only if S2 fails.
- One bare pod per actively watched trip with `restartPolicy: Never`, `activeDeadlineSeconds` and the agent's
  compiled maxima.
- Only ferry-control creates pods, through `fw-browser-pod`, PSA restricted and the ResourceQuotas (including
  `NotTerminating pods: 0`). No SA token in pods. ferry-web has no Kubernetes RBAC. The RBAC forbidden list and
  `can-i` negatives in CI and on the cluster. `fw-browser-pod-lock` and `fw-ns-objects` on.
- The typed agent protocol with no page operations; TokenReview binding with the pod-name check; the enum-only
  termination message.
- The in-pod gate: Ed25519 ticket bound to owner, trip, pod and cart_id; 60 s; single use; nonce lock with an
  atomic check-and-set; single viewer; Origin check; ping liveness; revocation at hold end. Hold caps
  enforced by the agent, by web refusing to mint, by the operator's clock and by `activeDeadlineSeconds`.
- Xvnc on a unix socket only, with fail-closed listener and lockdown self-tests; kiosk mode with managed
  policies; the Chromium sandbox (`hostUsers: false` plus the Localhost seccomp profile).
- Static vnc-edge with the stock client and build-computed CSP; pods never serve HTML; Cookie and Authorization
  stripped on both routes.
- Egress CNP (`toFQDNs` or the `toCIDRSet` fallback) plus default deny, verified by the post-deploy smoke;
  `nameError` set; the S-3DS allowlist and the blocked-host detector.
- The browser namespace without `components/common`, excluded from Fluent Bit and the descheduler.
- Identity: Authelia ext-auth with the route/policy coupling and the gatus canary (C29); the app's
  pre-registration; Authelia client `authorization_policies`; each invitee's email deliverable.
- The user-scoped repository returning 404 for foreign ids; the cross-tenant suite green.
- Limits: `maxTrips`, `maxActiveBrowsers = 1`, holds/day, VNC minutes/day, interval floor; admin Users and
  Fleet pages.
- Web Push plus email; the persisted brake with the 30 s agent floor.
- `home-cluster-runner` restricted; SES production access; Q5 decided; tenants told about R1 and R4.

Everything in M4 is post-v1. None of it is needed for the guarantees the owner asked for.

### 12.5 Runbooks

**Cutover and rollback (M1).**
1. Before: the new stack has run on `ferry-next.chestr.dev` with the owner as its only user; S1, S2 and S7
   passed against the owner's current trips; a revert PR is prepared.
2. In the legacy dashboard, turn off "Watch all trips" (`PATCH /api/settings` `enabled=false`) and release any
   held carts.
3. Copy `config.json` and `alerted.json` out of the legacy pod (`kubectl cp`, cluster admin).
4. Compute the owner's uid from the Authelia username (`ferry-watch-control admin uid --user <name>`) and run
   `ferry-watch-control admin import --user <uid> config.json alerted.json` as a one-off Job. No login is
   needed first, because the uid is deterministic.
5. One home-cluster PR: the legacy HelmRelease to `replicas: 0` with its HTTPRoute disabled; the legacy PVC
   annotated `kustomize.toolkit.fluxcd.io/prune: disabled`; the new values `routes.dashboardHost:
   ferry.chestr.dev`. Never two routes for one host at once (UniFi DNS would conflict).
6. Go/no-go within 30 min: post-deploy smoke green; imported trips Watching; a test alert arrives; the gatus
   canary passes.
7. Rollback, any time in the next 14 days: merge the revert PR (legacy replicas 1 and its route back; the new
   route back to `ferry-next`), then set legacy `enabled=true`. The legacy PVC still holds its state.
8. After 14 days: delete `kubernetes/apps/default/ferry-watch` (its PVC is pruned; VolSync snapshots stay in
   R2 per their retention); remove only that app's reference to the `pushover` 1Password item (the item itself
   serves kube-prometheus-stack, gatus, sonarr, radarr and qbittorrent); delete `BCF_USER` and `BCF_PASS` from
   the 1Password item `ferry-watch`.

**Inviting a person (M2+).**
1. Make sure their email is deliverable: SES production access, or add the address as a verified SES identity
   and wait for them to confirm.
2. In the lldap UI (LAN only): create the user (username, email, display name) and add them to group
   `ferry-watch`.
3. Send a one-time password out of band. There is no remote password reset; a reset needs the admin.
4. On the Ferry Watch Users page: **Add person** (username, email, optional overrides, reason). The app sends
   the `invited` email:
   - sign in at the link with the password you were sent;
   - Authelia will email you a code to set up a passkey (best on phones) or an authenticator app;
   - tick "Remember me", or you will be asked to sign in again after 3 idle days;
   - three wrong passwords in an hour lock you out for a day (Q11);
   - on iPhone: Share → Add to Home Screen, open it from the icon, allow notifications, and allow Ferry Watch
     during Sleep/Focus;
   - send yourself a test alert;
   - you must be in Canada or the US to sign in or check out (Q16);
   - what we can see: the cluster admin can technically see everything; checkout keystrokes pass through
     Cloudflare and our servers unencrypted for a moment; your card is never stored; checkout is as a guest.
5. Check the Users page shows the person as active with push enabled.

**Talos upgrade.** §7.8: turn on "Pause carting", wait until the Holds panel is empty (at most the hard cap),
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
| Guest checkout doesn't work, or not for some fare or vehicle types | Medium / high | S2, S2b and S7 run in W0, against the owner's own trips too. Alert-only ships on the same platform. M5 if Q1 is approved. |
| A BC Ferries or Queue-it block of the home IP (household too) | Medium / high | 20/min budget, start throttle, persisted brake (pause on any 403/429/challenge), breaker, fail-slow agents, maintenance switch. A compromised pod can bypass these (R13). |
| VAP or RBAC drifts from the template | Medium / critical | Template and VAP are written together; leak matrix, `can-i` and helm-unittest block CI; allowlist-style CEL; PSA as a second fence; second-fence VAPs from M3. |
| Cilium `toFQDNs` or the DNS proxy misbehave (never used here) | Medium / medium | `nameError` plus fully qualified names; S-CIL in W0 with both resolvers; `toCIDRSet` fallback; the post-deploy smoke after every deploy. |
| A tenant's bank uses a 3DS host the allowlist lacks (R16) | Medium / high | S-3DS across issuers before non-household invitees; the agent's blocked-host detector with a user message and admin alert in v1; "Release and book it myself"; Q13. |
| A checkout is cut off mid-payment | Medium / high | Payment guard pauses session clocks; no navigation away while paying; session default from S2b's phone timing; `hold_ending` warning at T−5. |
| VNC reconnects fail on phones (bank-app switch, network change, Cloudflare idle) | Medium / high | 20 s pings; reconnect with `N` replaces stale sockets; shim reload until the deadline; only failed upgrades count; S-VNC-lite on three clients. |
| The device that opened checkout is lost (iOS home-screen storage) | Medium / medium | No automatic open; device choice on `/c`; `N` reused across returns; S-VNC-lite checks the iOS path; Q5b if it fails. |
| The stock KasmVNC client changes on upgrade | Low / medium | No bundle patch; a CI test drives the client through the shim against a fake gate. |
| Memory is higher than measured (heavy pages, tmpfs, checkout) | Medium / medium | S-MEM measures the cgroup working set including tmpfs; podMemory starts at 1.5 GiB; disk cache capped; recycle at 85 % while Watching, alert while Holding; quota backstop. |
| A Talos upgrade releases a hold (R8) | Medium / low | "Pause carting" runbook; orderly release with an alert; hold PDB plus `drainTimeout` in M4 if it bites. |
| Trusted headers fail open (R15) | Low / critical | Route and SecurityPolicy rendered together; `failOpen: false`; no bypass rules; gatus forged-header canary; ingress limited to Envoy and the SNAT entity. |
| Invitees can't log in (2FA enrolment email, 1-day ban, geo-block) | Medium / medium | SES deliverability per invitee is a prerequisite; onboarding email; Q11 on the ban; Q16 on travel. |
| iOS Web Push lapses, the app isn't installed, or Focus silences it | High / medium | Email for every cart alert; push health on the Account card; reminder at 15 min; quiet hours on by default; onboarding asks to allow Ferry Watch in Focus. |
| PCI and ToS exposure (keystroke relay, automated carting for several people) | Unknown / medium | Disclose to tenants (naming Cloudflare). No payments taken. Short holds, per-user caps, no account automation. The UA question (Q9). |
| A compromised web races a first VNC connection (R3) | Low / medium | First-viewer lock (visible, before card entry); Q5; S-VNCID in M4. |
| The self-hosted runner is reachable from fork PRs (R1) | Unknown / critical | Restricted before non-household invitees (M3 hard gate). |
| The effort runs over | Medium / medium | Bottom-up estimate with ranges; explicit cut line (§12.4); M4 holds everything not needed for the owner's guarantees. |

---

## 14. Open questions for the owner

Each has a recommended default. Q0-Q5, Q13 and Q14 change scope; the rest tune numbers or wording.

0. **Q0 Identity.** Confirm decision 7 as designed: Authelia ext-auth on the dashboard route with trusted
   headers, app-native OIDC only as a later adapter (§6.2). **Default: yes.**
1. **Q1 Pre-cart sign-in window (an exception to decision 3).** Allow an opt-in "use my BC Ferries account"
   mode with a ≤ 10-minute VNC window before any cart, at most once per pod? **Default: no.** Revisit after M3
   with the sandbox on and S3/S4 data.
2. **Q2 Spike S2b.** May we make one cheap real guest booking with your card and cancel it within the free
   30 minutes? **Default: yes.** It is the only way to prove guest checkout end to end and to time it.
3. **Q3 If guest checkout fails,** is alert-only acceptable until a login mode exists? **Default: yes.**
   **Q3b Your own checkouts** become guest checkouts too: no saved cards, no account-only fares. Accept, or
   pull M5 forward for the admin only? **Default: accept**, unless S7 shows your usual fare types need an
   account.
4. **Q4 Sandbox and kernel.** Will you apply the Talos `user.max_user_namespaces` change **and** a
   `machine.seccompProfiles` Localhost profile (both needed for Chromium's sandbox; the sysctl also enables
   `hostUsers: false`) before non-household invitees? Do you accept `mitigations=off apparmor=0` on nodes that
   run tenants' Chromium? **Default:** apply both before M3; keep `mitigations=off` only while invitees are
   household, then reconsider.
5. **Q5 Residual R3.** Accept that a compromised ferry-web could win the race to a hold's *first* VNC
   connection (visible, before card entry; later takeover impossible)? **Default: accept for invite-only
   friends and family.**
   **Q5b Takeover.** If S-VNC-lite shows the iOS home-screen path loses the resume nonce, allow ferry-web to
   mint a "takeover" ticket when no viewer has been connected for ≥ 60 s? This lets a compromised ferry-web
   take over a session whose phone went to sleep, weakening C11. **Default: no**; tell iOS users to check out
   in Safari or on a computer instead.
6. **Q6 Separate domain.** Only relevant if Q0 is "no" or the IdP later moves to a cookie on the ferry host:
   then choose gateway-wide Lua (`enableLua`) or a separate registrable domain. **Default: n/a for v1.**
7. **Q7 After a viewed checkout without a detected booking,** ask "Did you book it?" and resume automatically
   after 20 minutes without an answer (the viewed departure is never re-carted)? **Default: yes.** A detected
   booking archives the trip.
8. **Q8 Per-user defaults:** 3 stored trips; 1 watched at once; 30 s interval; 4 holds and 6 cart attempts a
   day; holds of 30 min unclaimed, 30 min checkout (to be set from S2b) and 60 min hard; 60 VNC minutes a day;
   quiet hours 23:00-07:00 on. **Default: as listed** (§8.2).
   **Q8b** Should an outbound and return pair count as one watched route? **Default: no** in v1; raise a
   user's `maxActiveBrowsers` to 2 if needed.
9. **Q9 BC Ferries etiquette.** Is 20 checks/min (ceiling 40) from the home IP acceptable? Should a
   multi-user service keep the spoofed desktop user agent (`site.py:45-47`)? **Default:** 20/min; keep the UA
   and revisit if BC Ferries objects.
10. **Q10 Holds during Talos upgrades** are released in v1 (with an alert), and the runbook pauses carting
    first. Acceptable, with hold PDBs plus `drainTimeout` as an M4 option? **Default: yes.**
11. **Q11 Invitee login.** Keep `two_factor` for the ferry rule (needs SES for enrolment), and keep the
    cluster-wide 3-failures/1-day ban, or soften `ban_time` to 1 h now that people log in against a hold
    clock? **Default:** `two_factor`; soften `ban_time` to 1 h (cluster-wide, your call).
12. **Q12 Admin access.** Confirm admins never get in-app VNC or exec into tenant browsers (break-glass stays
    with the cluster admin, out of band), and that tenants are told the cluster admin can technically see
    everything. **Default: confirm.**
13. **Q13 3DS egress posture.** (a) A curated allowlist of payment and ACS hosts from S-3DS, plus the
    blocked-host detector and "Release and book it myself"; or (b) a broader posture during holds (DNS
    `matchPattern: "*"` and public `toCIDRSet`), accepting that a tenant could then browse anywhere from the
    home IP during their own holds (it weakens decision 9). **Default: (a).**
14. **Q14 Shared Postgres superuser (R14).** Keep the standard `components/cnpg` (superuser Secret resident in
    `ferry-watch`, used only by the stock `postgres-init`), or add a CNPG `Database` plus managed owner role in
    home-cluster and a component variant without the initdb secret? **Default:** standard component for M1;
    the variant before non-household invitees.
15. **Q15 Self-hosted runner.** Restrict `home-cluster-runner` to push/schedule events or same-repo PRs,
    require approval for outside contributors, and move matrix values to `env:`? **Default: yes, before M3.**
16. **Q16 Travelling invitees.** The Cloudflare geo-block (CA/US/CN only) also covers `auth.chestr.dev`. Add a
    skip rule for the ferry hosts and `auth.chestr.dev`, or tell invitees? **Default: tell invitees.**

---

## 15. Out of scope

- Stripe or any payment; the Ko-fi link is a plain URL.
- Storing BC Ferries credentials anywhere (Secrets, 1Password/ESO, Vault, the DB, a PVC), a dashboard
  credential form, or a pod-served credential page.
- Pushover as a user channel; SMS in v1.
- CRDs and a kubectl-native trip view; a Go controller; kopf.
- Namespace-per-tenant. It is revisited only before shipping any mode that persists a per-tenant credential or
  browser profile.
- Admin or break-glass VNC in the app; tenants seeing any other tenant's data.
- Cross-tenant search deduplication.
- An HA multi-replica control plane; an egress gateway or a second egress IP.
- A "watch my browser" view outside holds, and any pre-cart VNC (unless Q1).
- Public sign-up; users outside CA/US/CN (Cloudflare geo-block).
- The single-container personal mode (owner decision 5); arm64 images (not built today).
- One browser per *route* shared across dates or users: the unit of watching is a trip (route + date + party),
  because each trip has its own search, cart and owner; sharing one browser across tenants would make one
  tenant's page drive another's alerts (the dedupe problem above).
- An inactivity pause of trips (removed, §8.1).

---

## Appendix A: Decision record (D1-D16)

| Id | Decision | Why |
|---|---|---|
| D1 | Two single-replica Recreate Deployments from one control image: `ferry-web` (internet-facing, no k8s RBAC) and `ferry-control` (the operator: reachable only from browser pods, `tokenreviews create`, `pods` in one namespace, nodes read), plus a static `vnc-edge`. They coordinate only through Postgres rows and NOTIFY under separate roles; each is fenced by an advisory lock. | It keeps internet input away from `pods/create` and from the pod-facing parser [R:map-threat-model]. Merging hub and fleet removes four DB-mediated protocols and their chaos tests, and matches the owner's description of the operator. The cost (a parser exploit reaches VAP-pinned pod create) is stated in C3 and §4.5. |
| D2 | Postgres on CNPG `postgres17` (`ferrywatch`) through the standard `components/cnpg`; superuser used only by the stock `postgres-init` Job; per-process login roles as CNPG managed roles; migrations as the owner role in a separate Job; RLS on the outbox, immutable `trips.user_id`, INSERT-only audit. Defaults and ceilings in Helm values. No CRDs. | Quota admission needs transactions; audit and usage must be queryable and backed up; the cluster convention requires it. Our image never holds the superuser. Q14 can remove the superuser Secret from the namespace. |
| D3 | One shared `ferry-watch-browsers` namespace, chart-made without `components/common`, PSA restricted, default-deny. | In guest mode there is no tenant Secret or PVC; ownership is enforced cryptographically in the pod. Per-tenant namespaces would need GitOps toil per invite or namespace-create rights for the operator. Revisit before any stored-credential mode. |
| D4 | Python 3.12 with lightkube `AsyncClient`. A level-triggered loop (pod and node watch, a 10 s list, Postgres LISTEN), deterministic names, `Cluster` Protocol plus `FakeCluster`, envtest from pytest. No kopf, no Go. | One language shares the models, protocol and limits resolver, so there are no drifting duplicate security checks. |
| D5 | Fat agent: today's Watcher for one trip, owning cart, timers (with the payment guard), back-off, gate and a local bucket, clamped to compiled maxima. It dials out over WS. Protocol §5.3, with no page-control operations. | Holds and VNC gating must survive control-plane outages. A thin RPC agent would put carts on the network path and keep a remote caller one step from page control. |
| D6 | Projected SA token (audience `ferry-watch-agent`, 600 s), TokenReview with pod-uid and pod-name at connect, binding to the `browser_pods` row written before the pod existed, one socket per uid, every message's trip checked. | Bound tokens die with the pod and are useless against the API server. The binding anchors in a row written by the operator, not in anything the agent asserts. The draft's 5-minute re-review is unnecessary once the fleet loop closes a deleted pod's socket itself. |
| D7 | Static `ferry-vnc.chestr.dev` route (Cookie and Authorization stripped) → stock-client `vnc-edge` (upgrades only) → pod gate → Xvnc unix socket. web mints a 60 s Ed25519 ticket bound to `sub == FW_OWNER`, trip, pod and `cart_id`, carried in the URL fragment and a subprotocol (via a shim), never in a URL a server sees. First-viewer lock via a client nonce, atomic, with preemption by the lock holder and ping liveness. The alert link is `/c/<trip>`, a device chooser. | The in-pod check must be cryptographic (SNAT). Pods must not serve HTML on a chestr.dev origin. The lock stops a compromised web taking over after the user connects. A unix socket closes the page-JS path by construction. No bundle patch to maintain. |
| D8 | Guest-only at launch, the owner included; alert-only if S2 fails; attended sign-in only via Q1, after the sandbox. Never a dashboard form, a Secret, ESO or a pod-served page. | With no stored credential, no component can leak one. |
| D9 | Cilium `toFQDNs` plus DNS L7 allowlist with `nameError` and fully qualified names (`toCIDRSet` fallback). Kiosk mode and managed policies in 3 directories with fail-closed self-tests. Xvnc on a unix socket only, NeverShared/DisconnectClients=0, `-MaxConnectionTime 6000`, never MaxIdle, MaxDisconnection, IdleTimeout or DLP_KeyRateLimit. PSA restricted, read-only, uid 1001. Localhost seccomp profile plus the sysctl for the sandbox, and `hostUsers: false`, by M3. | Cilium makes a forward proxy optional. REFUSED breaks search lists. containerd's default seccomp blocks Chromium's namespace sandbox. Unsandboxed Chromium on `mitigations=off` nodes is the largest residual, so it gates outsiders. |
| D10 | `min(override ?? default, GitOps ceiling)`. Five enforcement layers. Queued, not Pending. Admin Fleet and Users pages with mandatory reasons and INSERT-only audit plus a log line. No admin VNC. One active browser per user by default. | Ceilings plus compiled maxima mean neither a compromised web nor the operator can make holds or honest polling unbounded. |
| D11 | Phases Off, Dormant, Waiting, Queued, Starting, Watching, Holding, Draining, BackingOff, NeedsAttention, Past. `restartPolicy: Never`, 26 h deadline, about 24 h recycle. A viewed hold always ends in pod exit, then archive (booked) or "Did you book it?" with auto-resume. SIGTERM drain, no hooks. Bare pods with no anchor and no hold PDB in v1. No inactivity pause. | Fixes today's lost-cart shutdown and "forever" holds. Talos drains bare pods with `Force` and fails upgrades on blocked evictions, so a PDB would fail upgrades and an anchor adds a GC hazard. |
| D12 | ferry-control's static fair interval stretch (20/min default, 40 ceiling) enforced by the agent bucket with a compiled 30 s floor. Start throttle. Persisted brake and a simple breaker with manual close. Fail-slow. No cross-tenant dedupe. | One residential IP is shared with the household. Agent-side enforcement means no outage or control-plane compromise can speed an honest agent up; a compromised pod can (R13). |
| D13 | Only web sends, from a Postgres outbox: Web Push by default plus email via smtp-relay; no Pushover. Typed kinds, templates on the server, links only `/c/<trip>` or `search_url`. Ko-fi as a validated plain link. | Owner decisions. Server-side templates stop pods injecting phishing links; the outbox survives restarts. |
| D14 | Authelia ext-auth SecurityPolicy on the dashboard route (envoy-external) with trusted headers, behind an `IdentityProvider` interface; deterministic `uid = hash(provider, subject)` with GitOps aliases; admin pre-registration; `bootstrapAdmins` plus a DB flag; app-native OIDC as a later adapter. | Owner decision 7. Less code than OIDC, and the only option on `chestr.dev` that keeps the Authelia cookie from the app without gateway-wide Lua. Fail-open is contained by C29. A deterministic uid lets the pod verify ownership without trusting DB mappings, and survives an IdP swap through aliases. |
| D15 | A uv workspace (core, control, agent) with import-linter. Images: control, browser, vnc-edge, plus CI-only agent-e2e and fake-bcferries. A public OCI Helm chart from this repo; cluster-specific bits in home-cluster. Test tiers: unit, Postgres, envtest (VAP matrix, `can-i`), compose integration, kind-lite, home-cluster post-deploy smoke. | Matches the owner's OCI Helm and digest conventions and keeps Playwright out of the control plane. The VAP and RBAC are the isolation, so their negative tests block CI; network behaviour can only be proven on the real cluster, so it gates deploys. |
| D16 | W0 de-risk spikes, then M1 owner on the new stack, M2 household invitees, M3 non-household gate, M4 hardening backlog, M5 optional sign-in. | Each live unknown decides a design branch before code depends on it. Controls that back up RBAC land just before non-household invitees. |

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
   pod (S-VNCID) moves to M4; Q5 decides R3 before outsiders. Header-based principal binding is rejected
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
11. **Standby tier and AIMD.** M4, behind data.
12. **User id.** Deterministic `hash(provider, subject)` with GitOps aliases.
13. **PriorityClass value.** 0 with `preemptionPolicy: Never`.
14. **Namespace-per-tenant.** Rejected for v1 (D3).
15. **Go controller and CRDs.** Rejected (D2, D4).
16. **Egress forward proxy.** M4 (for R13), or M3 if S-CIL fails.
17. **Pushover bring-your-own.** Removed for users.
18. **Credential-page fallback.** Rejected; alert-only, and Q1 for the pre-cart window.
19. **Cookie on the dashboard host.** The draft used a Lua filter. **Review: trusted headers let the route drop
    the whole Cookie header after ext-auth**; Lua is off by default and gateway-wide.
20. **Cross-tenant dedupe.** Rejected (integrity channel for A3).
21. **Default trip limits.** **3 stored, 1 watched at once** (owner decision during review), replacing the
    draft's 4 and 3. With one active browser per user, cross-pod hold coordination and `maxConcurrentHolds` go.
22. **Ticket hand-off.** A client-generated nonce registered on first connect gives the "only the first device
    can reconnect" guarantee with no pod HTTP endpoint and no stream change.
23. **Identity.** The draft chose app-native OIDC; the completeness review showed decision 7 was overridden on
    a wrong inference. **Trusted headers via Authelia ext-auth** (§6.2), with Q0.
24. **Lost-device recovery versus C11.** The product review proposed a takeover ticket; the security review
    kept C11 intact. **C11 kept by default**; the device chooser and nonce reuse reduce the risk; Q5b decides a
    takeover only if S-VNC-lite shows the iOS path loses the nonce.
25. **VNC minutes per day.** The product review proposed dropping it; the delivery review listed it in the
    minimum v1. **Kept, enforced only before carting**, never closing an open session.
26. **Termination message.** The completeness review proposed a masked last-error summary there; the security
    review asked for fixed enums only. **Enums only** (reason and error class); post-mortem uses retained pods.
27. **Second-fence VAPs.** The delivery review proposed one VAP; the security review kept the allowlist VAP
    approach. **One VAP in M1; the lock and namespace-objects VAPs in M3**, before non-household invitees, with
    `apps/*` and `batch/*` added.
28. **Kind with Cilium and EG in CI.** Dropped for compose plus kind-lite plus the real-cluster smoke; kind
    cannot reproduce the behaviours that matter.

## Appendix C: Must-fix traceability (from the judges' reviews of the four designs)

| Must-fix (judge) | Where addressed |
|---|---|
| Authelia cookie reaches every chestr.dev backend (A) | C16, §6.2.2 (route drops Cookie after ext-auth), S-AUTH, the echo check in §11.4, R12 |
| Pods serve the KasmVNC client (A) | C14, §7.9 (vnc-edge serves the client; pods only do `/ws`) |
| Loopback Xvnc reachable by page JS (A, B) | C13 (unix socket only), §5.6 self-tests 2-3, LNA policy, S-KIOSK |
| Bearer ticket, undefined reconnect (A) | §5.5: `sub == FW_OWNER`, nonce lock with atomic set and preemption, no URL reuse; Q5, Q5b |
| Hub inside the internet-facing process (A) | D1: the hub endpoint lives in `ferry-control`, not `ferry-web` |
| Kernel args and `--no-sandbox` (A) | R2, S-SBX (seccomp profile + sysctl), Q4; sandbox gates M3 |
| SNAT makes NetworkPolicy unprovable (A, B) | §2.2, C6, cryptographic gate, vnc-edge hop, S-CIL decides entity rules |
| Hold-cap clock starts when the operator first sees it (A) | §3.6 step 2: `hold_ready_at` at ack; compiled 90-min cap; `-MaxConnectionTime` |
| VAP `system:` exemption (A) | §7.5 `fw-browser-pod-lock` (named exemptions, positives in kind-lite), M3 |
| Credential-page fallback (A, B) | §5.7: alert-only; Q1 |
| One DB role for every process (A) | §6.4 two login roles via CNPG managed roles, RLS, trigger; S-DB |
| Authelia client restrictions before outsiders (A) | §6.2.9, §2.3, M2 hard gate |
| Tickets in URLs reach logs (A, B) | C15 fragment plus subprotocol via shim; nginx `noargs`; Appendix B #6 |
| `empty_cart` unverified; guest checkout unverified (A) | §5.2, S1/S2/S2b/S5 gate carting |
| Align with cluster facts (B) | §2.2 |
| No pre-cart VNC under any fallback (B) | §5.7 |
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
