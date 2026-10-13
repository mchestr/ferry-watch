===== quotas-admin
Quotas-admin design for multi-tenant Ferry Watch, where one headed-browser pod runs per watched trip.

Today there is one quota, a hard-coded process-global MAX_TRIPS=6. It counts every stored trip, including disabled and past ones. Several other knobs are really per-user quotas: a global check interval with a 15s floor, a cart hold where 0 means forever, a "check now" that skips back-off, a process-global test-alert cooldown, a global login back-off, and concurrency=3.

Proposed model:
1. **Identity and roles.** Identity comes from the proxy headers: Authelia Remote-User/Remote-Groups, or oauth2-proxy X-Forwarded-User/X-Forwarded-Groups, with configurable header names. Admin = username in a GitOps-owned bootstrapAdmins list, or a member of adminGroups. Eligibility is invite/allowlist by default, optionally anyone in allowedGroups.
2. **Per-user effective limits.** Each limit resolves as clamp(user override ?? tier ?? global default, cluster hard ceiling). The limits are maxTrips, maxRunningBrowsers, maxAutoCartTrips/maxHeldCarts, minIntervalSeconds, maxCartHoldMinutes (no "forever" for non-admins), maxVncMinutesPerDay/maxVncSessionMinutes/vncIdleTimeout, maxBrowserLifetimeHours, manual-check and test-alert rate limits, and guaranteedBrowsers.
3. **Cluster caps and queueing.** Cluster caps are maxBrowsersTotal, maxConcurrentSpawns, maxChecksPerMinuteTotal (one egress IP to bcferries.com) and maxVncSessionsTotal. The operator queues work itself (WaitingForCapacity / OverUserQuota) rather than creating Pending pods. Order is max-min fair share first, then date imminence, then wait-time aging. Preemption is limited: it never touches a held cart or a live VNC session.
4. **Two enforcement layers.** The operator does API admission inside one DB transaction, plus a continuous reconciler/reaper. A GitOps-owned Kubernetes backstop sits behind it, and the operator's service account cannot edit it:
   - PriorityClass-scoped ResourceQuota (pods/cpu/memory/ephemeral-storage);
   - a NotTerminating-scoped quota of pods: 0, which forces activeDeadlineSeconds on every browser pod; Kubernetes only lets that deadline be decreased, so it is a hard lifetime ceiling;
   - LimitRange;
   - a low, non-preempting PriorityClass;
   - a ValidatingAdmissionPolicy pinning image digest, priority class, deadline and labels;
   - deterministic pod names, so there is at most one pod per trip.

   Both layers are needed. Kubernetes cannot express per-user quotas in a shared namespace, or non-resource quotas such as hold time, intervals and VNC minutes, and its failures make for poor UX. The operator alone is a single point of failure, open to races, leaked pods and compromise.
5. **Admin API/UI.** Users, effective limits with where each value came from, browsers with age/last-activity/cart/VNC/CPU, queue, policy, stop/restart/release, suspend, and an append-only audit log covering admin and system actions.
6. **Quota changes.** Stored trips are kept; running resources and future actions follow the new limits straight away. Held carts are never silently destroyed: they finish a capped hold or are explicitly released with an alert.
7. **Storage.** Recommendation: SQLite on a PVC (WAL, single-replica operator, Litestream backup) as the record for users/limits/trips/usage ledger/audit. A GitOps ConfigMap holds bootstrap admins, header config, defaults and hard ceilings. Reasons:
   - quota admission is transactional (count + insert);
   - audit and usage ledgers fit a database, not etcd (1.5MiB request limit, watch churn, Events kept only 1h);
   - JupyterHub's Kubernetes deployment (z2jh) defaults to sqlite-pvc for the same hub-plus-per-user-pods shape;
   - the Kubernetes backstops don't need per-user data.

   A FerryUser CRD is only worth it if trips also become CRDs. Users and trips should live in the same store.

Network note: WebFetch and direct doc sites (kubernetes.io, authelia.com, oauth2-proxy.github.io) were blocked by egress policy. I checked external facts against the upstream source and docs repos on raw.githubusercontent.com (the kubernetes/website, kubernetes/kubernetes, authelia, oauth2-proxy, KasmVNC v1.5.0, jupyterhub and kueue repos), so those claims are high confidence. Resource-sizing numbers are estimates.

## Today's only quota is a process-global constant with no notion of a user, and it counts past and disabled trips
MAX_TRIPS=6 is a module constant. ConfigStore.add_trip enforces it by counting every stored trip, including disabled and past ones. post_trip only validates and then calls add_trip; nothing knows who the caller is (the module docstring says there is no authentication and the app relies on the proxy). The limit reaches the UI as State.max_trips, which the frontend compares against trips.length. A past trip stays visible as 'past' but still uses a slot. Trip ids are server-generated uuid4().hex[:8] and only checked for uniqueness within one config. With many tenants they need to be globally unique (longer, or prefixed with the user) before they can name pods. Migration: replace MAX_TRIPS with an EffectiveLimits object resolved per request. Replace State.max_trips with State.limits plus State.usage (keep max_trips as an alias so the existing UI keeps working). Count only non-past, non-archived trips toward maxTrips.
refs: src/ferry_watch/models.py:16, src/ferry_watch/store.py:89-95, src/ferry_watch/store.py:44-46, src/ferry_watch/server.py:1-7, src/ferry_watch/server.py:347-356, src/ferry_watch/views.py:70, src/ferry_watch/views.py:135, web/src/App.tsx:28

## Several existing global knobs are really per-user quotas and must become admin-capped
(a) The check interval is one global Config.interval_seconds, with a 15–3600s range in the model. It becomes a per-trip interval with an admin-set per-user floor (minIntervalSeconds), plus a cluster-wide checks-per-minute budget, because every pod polls bcferries.com from the same egress IP.
(b) Cart hold is a global env var, BCF_CART_HOLD_MINUTES (default 60). 0 means 'hold until released', and expires_at=None leaves the window open indefinitely. That is exactly the 'keep it around forever' vector. Non-admins must get a positive maxCartHoldMinutes cap and no indefinite hold.
(c) POST /api/check clears back-off and next_due for every trip. It needs a per-user rate limit (maxManualChecksPerHour).
(d) The test-alert cooldown is a single process-global timestamp; make it per user.
(e) Watcher concurrency=3 becomes the cluster-wide maxConcurrentSpawns / concurrent-check limit.
(f) Login uses one global lock and a 30-minute back-off after LoginFailed. With one pod per trip, each pod logs into the same user's BC Ferries account on its own and backs off on its own, so N trips can lock the account. The operator must coordinate a per-user login back-off and cap logged-in pods (maxAutoCartTrips).
(g) auto_cart defaults to on whenever a login is configured, so maxAutoCartTrips must also drive the add-trip form defaults.
refs: src/ferry_watch/models.py:19-20, src/ferry_watch/models.py:284, src/ferry_watch/models.py:290-305, src/ferry_watch/settings.py:30, src/ferry_watch/settings.py:50-54, src/ferry_watch/watcher.py:498, src/ferry_watch/watcher.py:510-542, src/ferry_watch/server.py:403-407

## Existing lifecycle semantics define what a 'running browser' is and what must never be reaped
A trip is active only when the global switch is on, the trip is enabled, and date >= today (Pacific). Inactive trips have their session closed. A trip holding a READY cart is not checked, keeps its window open, and resumes only on release, window close, or expiry. A crash loses the cart and triggers an alert. Mapped to quotas:
- a browser pod should exist only for an admitted, active trip;
- disabled or past trips hold no pod;
- the only state worth protecting from reaping or preemption is a held cart, and a live checkout VNC session.
Per-day accounting should use Pacific time, as the code already does for 'today'.
refs: src/ferry_watch/watcher.py:228-229, src/ferry_watch/watcher.py:199-218, src/ferry_watch/watcher.py:510-542, src/ferry_watch/state.py:91-93, src/ferry_watch/models.py:13-14, src/ferry_watch/models.py:94-96

## Identity and role sources: proxy headers plus a GitOps bootstrap allowlist
Authelia's trusted-header SSO returns Remote-User, Remote-Groups (comma-separated, e.g. 'admin,dev'), Remote-Name and Remote-Email. These are meant for the proxy-to-backend hop only. The backend must be reachable only via the proxy, and the proxy must replace any client-supplied copies. Authelia access_control can gate a domain or path with 'group:<name>' subjects.

oauth2-proxy's pass_user_headers (default true) sends X-Forwarded-User, X-Forwarded-Groups, X-Forwarded-Email and X-Forwarded-Preferred-Username. Its legacy header config sets PreserveRequestValue=false for X-Forwarded-Groups, so client values are overwritten. allowed_group restricts login.

Precedent: JupyterHub OAuthenticator derives admin from admin_groups and eligibility from allowed_groups.

Design:
- operator config has identity.userHeader/groupsHeader/emailHeader (defaults for both Authelia and oauth2-proxy), bootstrapAdmins (usernames; break-glass, can't be revoked from the UI), adminGroups, allowedGroups, and registration: invite | group | open;
- read groups on every request, so IdP changes apply at once, and snapshot last-seen groups into the DB for the admin UI;
- reject requests with no user header (401);
- add a second admin check at the proxy, an Authelia rule requiring group:ferry-admins on /api/admin/*.

Browser pods must report on a separate listener authenticated with a projected service-account token or mTLS, never via proxied header identity.
refs: https://raw.githubusercontent.com/authelia/authelia/master/docs/content/integration/trusted-header-sso/introduction.md (Response Headers table; Trusted Remote Networks), https://www.authelia.com/integration/trusted-header-sso/frequently-asked-questions/ (headers implicitly trusted; proxy must strip/override), https://raw.githubusercontent.com/authelia/authelia/master/docs/content/configuration/security/access-control.md (group: subject), https://raw.githubusercontent.com/oauth2-proxy/oauth2-proxy/master/docs/docs/configuration/overview.md (pass_user_headers, set_xauthrequest, allowed_group), https://raw.githubusercontent.com/oauth2-proxy/oauth2-proxy/master/pkg/apis/options/legacy_options.go (X-Forwarded-Groups PreserveRequestValue=false), https://raw.githubusercontent.com/jupyterhub/oauthenticator/main/oauthenticator/oauth2.py (allowed_groups/admin_groups)

## Proposed per-user limit catalogue, resolution rules and suggested defaults
Resolution: effective = clamp(userOverride ?? tier[user.tier] ?? policy.defaults, policy.hardCeilings). null means inherit. The admin UI shows each value and its source (override / tier / default / ceiling-clamped).

User state: invited | active | suspended | disabled. 'May watch at all' = active. Invite/allowlist is the default rather than 'anyone the proxy admits', because per-user limits mean nothing if people can mint identities.

Limits (suggested defaults; ceilings in brackets):
- maxTrips 6 [20]: stored, non-past trips; replaces MAX_TRIPS.
- maxRunningBrowsers 3 [cluster cap].
- maxAutoCartTrips 2: logged-in pods per BC Ferries account.
- maxHeldCarts 1.
- minIntervalSeconds 30 [15 admin-only].
- maxCartHoldMinutes 60: allowIndefiniteHold false, admin-only.
- maxVncSessionMinutes 30.
- maxVncMinutesPerDay 60 (Pacific day).
- vncIdleTimeoutSeconds 300.
- maxBrowserLifetimeHours 24: proactive recycle when not holding; hard activeDeadlineSeconds about 26h.
- maxTripHorizonDays: admin-set, so a far-future date can't be used to keep a pod alive.
- maxManualChecksPerHour 12.
- maxTestAlertsPerHour 6.
- guaranteedBrowsers 1: share under contention.
- weight 1: fair-share weight / priority tier.

Browser CPU/memory per pod stays a cluster setting, not a per-user one. Concurrent-spawn throttle mirrors today's concurrency=3.
refs: src/ferry_watch/models.py:16-20, src/ferry_watch/watcher.py:113, src/ferry_watch/settings.py:30

## Cluster-wide caps and behaviour when full: queue in the operator, not as Pending pods
Kubernetes facts:
- ResourceQuota is per namespace and independent of cluster capacity; there is no cluster-wide quota object.
- The 'pods' quota counts every non-terminal pod, so Pending pods consume quota.
- The kube-scheduler has no per-user fairness.
- Non-preempting pods wait in the scheduling queue with back-off.

So the operator owns admission and queueing. Cluster policy: maxBrowsersTotal (from the browser node pool's allocatable memory ÷ per-pod request, minus headroom), maxConcurrentSpawns, maxChecksPerMinuteTotal (shared egress budget to bcferries.com/Queue-it), maxVncSessionsTotal.

Trip states: Queued(reason = user_quota | cluster_capacity | spawn_throttle | poll_budget), with position and ETA.

Admission order on every tick and event:
1. users below guaranteedBrowsers, then max-min fair share by running/weight;
2. date imminence (trips within about 48h first);
3. wait-time aging, so nothing starves.

Operator preemption: only a running, non-holding browser with no live VNC session, which has run at least about 20 minutes and belongs to a user above fair share. Never a held cart.

If the poll budget is exceeded, stretch effective intervals proportionally before refusing.

Precedents: JupyterHub's active_server_limit, concurrent_spawn_limit and named_server_limit_per_user. Kueue supports plain Pods (queue-name label) with admission fair sharing, but it is heavy and can't express date imminence or held-cart protection directly. Not recommended for v1.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/policy/resource-quotas.md (Quota and Cluster Capacity; 'pods' = non-terminal pods), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/scheduling-eviction/pod-priority-preemption.md (Non-preempting PriorityClass), https://raw.githubusercontent.com/jupyterhub/jupyterhub/main/jupyterhub/app.py (named_server_limit_per_user, concurrent_spawn_limit, active_server_limit), https://raw.githubusercontent.com/kubernetes-sigs/kueue/main/site/content/en/docs/tasks/run/plain_pods.md, https://raw.githubusercontent.com/kubernetes-sigs/kueue/main/site/content/en/docs/concepts/fair_sharing.md

## Kubernetes backstop: primitives and the exact semantics that matter
- ResourceQuota rejects with 403 Forbidden. A cpu/memory quota forces every pod to declare requests/limits (pair it with a LimitRange for defaults). count/<resource>.<group> works for CRD-backed resources. scopeSelector can target a PriorityClass.
- NotTerminating scope = pods with activeDeadlineSeconds unset. A quota of pods: 0 in that scope therefore forbids any browser pod without a deadline.
- activeDeadlineSeconds updates may only go nil to positive, or positive to a smaller value; increasing it fails. That makes it a hard lifetime ceiling the operator can shorten (reaping) but never extend.
- priorityClassName is not among the updatable pod fields, so priority can't be raised when a cart is held. Cart protection must therefore be operator logic, optionally plus a PDB or safe-to-evict annotation during holds.
- A PriorityClass with a low value and preemptionPolicy: Never stops browser pods from ever evicting the owner's other workloads.
- LimitRange caps per-container cpu/memory/ephemeral-storage (limits downloads and stashes).
- ValidatingAdmissionPolicy (stable since 1.30) can enforce pod shape: pinned image digest, priorityClassName, activeDeadlineSeconds <= ceiling, required ferrywatch.io/user and /trip labels, no privileged/hostPath. paramKind lets it read a ceiling object, but CEL can't list or count objects, so per-user counts can't be enforced there.
- With the CNI bandwidth plugin, kubernetes.io/egress-bandwidth and ingress-bandwidth annotations throttle a pod, which makes it useless as a proxy or for streaming.
- Deterministic pod names (fw-<tripUid>) let the API server guarantee at most one browser per trip via AlreadyExists, even under operator races.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/policy/resource-quotas.md (How ResourceQuotas work; Quota on object count; Quota scopes; NotTerminating; PriorityClass scope), https://raw.githubusercontent.com/kubernetes/kubernetes/master/pkg/apis/core/validation/validation.go (updatablePodSpecFields ~L5861; activeDeadlineSeconds update rule ~L5904-5921), https://raw.githubusercontent.com/kubernetes/api/master/core/v1/types.go (ActiveDeadlineSeconds doc), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/policy/limit-range.md, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/scheduling-eviction/pod-priority-preemption.md, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/access-authn-authz/validating-admission-policy.md (stable v1.30; paramKind/paramRef), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins.md (bandwidth annotations)

## Why enforce in both the operator and Kubernetes
Operator admission is the real policy engine:
- it knows users, so it can enforce per-user limits in a shared namespace, which Kubernetes cannot;
- it handles non-resource quotas: intervals, hold minutes, VNC minutes, auto-cart, date horizon, rate limits;
- it gives friendly field-level errors and queueing instead of a 403 at pod-create after the trip was already accepted, or a pod stuck Pending;
- it applies fairness and date priority.

The Kubernetes backstop limits the damage from operator bugs:
- check-then-create races;
- a crash between pod create and DB commit, which leaks pods;
- reaper bugs that leave pods forever, which the NotTerminating=0 quota plus activeDeadlineSeconds prevents;
- operator compromise used for crypto-mining or mass spawning, stopped by the image-digest VAP, the pod-count/CPU quota and the LimitRange.

The backstop only works if the operator's ServiceAccount has no write verbs on ResourceQuota, LimitRange, PriorityClass or the VAP and its bindings. Those must be GitOps/admin owned. Set backstop values slightly above the operator caps (e.g. pods = maxBrowsersTotal + recycle surge). A hit then signals a bug, so alert when quota used is above the operator cap.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/policy/resource-quotas.md (restrict who can update/delete the quota, e.g. via ValidatingAdmissionPolicy), src/ferry_watch/store.py:113-118

## KasmVNC v1.5.0 has the knobs for VNC quotas, but some terminate the whole container
The image pins KasmVNC 1.5.0. Its Xvnc supports:
- -IdleTimeout (drop an idle connection);
- -MaxConnectionTime, -MaxDisconnectionTime and -MaxIdleTime (TERMINATE the server);
- -DLP_KeyRateLimit;
- -DLP_ClipSendMax / -DLP_ClipAcceptMax (clipboard caps, useful against using the browser as a data mule).

The entrypoint sets none of these, runs -AlwaysShared, and does 'wait -n; exit 1', so any Xvnc exit kills the pod and with it a held cart. Use -IdleTimeout for session idling. Enforce per-session and per-day VNC minutes in an operator VNC gateway that counts connected WebSocket time per user (Xvnc has no per-user ledger) and closes the connection or revokes the route. Use the terminating flags only when killing the pod is the intended reap. The per-day ledger needs the gateway, because one browser pod can be viewed by several connections at once (-AlwaysShared).
refs: Dockerfile:20, entrypoint.sh:20-26, entrypoint.sh:38-39, https://raw.githubusercontent.com/kasmtech/KasmVNC/v1.5.0/unix/xserver/hw/vnc/Xvnc.man (IdleTimeout, MaxDisconnectionTime, MaxConnectionTime, MaxIdleTime, DLP_KeyRateLimit, DLP_ClipSendMax)

## Storage comparison: FerryUser CRD vs SQLite-on-PVC vs ConfigMap
**CRD** (e.g. cluster-scoped FerryUser with spec.state/tier/limits and status.usage via the status subresource):
- Pros: kubectl/GitOps-able; watchable, so limit changes reconcile at once; RBAC-native; CEL bounds on fields; usable as a VAP param; count/<crd> quotas become possible if trips are CRDs too.
- Cons: no multi-object transactions, so count-and-create races need per-user locks or resourceVersion reservations; poor querying and aggregation; etcd is the wrong place for audit/usage ledgers or frequent status updates (1.5MiB default request limit, watch churn; Events are kept only 1h by default, so they aren't an audit log); two writers (GitOps and the UI) fight over spec. End users never use kubectl, so its main benefit mostly goes unused.

**SQLite on a PVC** (WAL; operator replicas=1, strategy Recreate):
- Pros: atomic BEGIN IMMEDIATE count+insert admission; relational usage ledger and append-only audit; easy migrations; matches the app's existing local-file persistence; proven by JupyterHub's z2jh default db.type sqlite-pvc for the same 'hub + per-user pods' shape; Litestream gives continuous backup.
- Cons: single writer and an RWO PVC tie the operator to one replica (fine for an operator); not readable by Kubernetes admission (the backstop doesn't need per-user data); needs backups.

**ConfigMap**:
- Good for static, GitOps-owned bootstrap (admins, header names, defaults, hard ceilings).
- Unsuitable for dynamic per-user writes: no validation, races, about 1MiB, no status.

Recommendation: SQLite as the record for users, limits, tiers, policy, trips, usage ledger and audit, plus one GitOps ConfigMap for bootstrap and ceilings that the DB policy can't exceed. Decide users and trips together. If the trips area insists on FerryTrip CRDs, then use FerryUser CRDs for limits, still keep the audit and usage ledgers in SQLite, and serialize admission per user inside the single-replica operator.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions.md (Validation rules / x-kubernetes-validations; Status subresource), https://raw.githubusercontent.com/etcd-io/website/main/content/en/docs/v3.5/dev-guide/limit.md (1.5 MiB request size), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/command-line-tools-reference/kube-apiserver.md (--event-ttl default 1h0m0s), https://raw.githubusercontent.com/jupyterhub/zero-to-jupyterhub-k8s/main/jupyterhub/values.yaml (hub.db.type: sqlite-pvc; cull.timeout/maxAge), https://raw.githubusercontent.com/benbjohnson/litestream/main/README.md, src/ferry_watch/store.py:70-142, src/ferry_watch/fileio.py

## Admin API/UI and audit log shape
Endpoints (all require an admin role; mutating calls require a 'reason'; If-Match/version for optimistic concurrency):
- GET /api/admin/users: user, last-seen groups, role and its source, state, tier, effective limits with provenance, usage (trips, running, queued, held carts, VNC minutes today), last seen.
- POST /api/admin/users: invite. PATCH /api/admin/users/{id}: overrides, tier, state, note. DELETE /api/admin/users/{id}: stop browsers, release carts with alert, delete trips; keep audit rows.
- GET /api/admin/browsers: pod, user, trip, node, phase, created/started, deadline, last heartbeat, last successful check, cart status/expires_at, logged-in flag, VNC viewers, VNC minutes today, CPU/memory from metrics-server, egress bytes if available.
- POST /api/admin/browsers/{trip}/stop (graceful: empty the cart first unless force), /restart, /release-cart.
- GET /api/admin/queue.
- GET/PATCH /api/admin/policy: defaults and cluster caps, clamped by the ConfigMap ceilings.
- GET /api/admin/audit?actor&target&action&since.
- An admin WebSocket snapshot like the existing Hub.

Audit row: id, ts, actor (username, groups, source IP, request id; or system:reaper / system:scheduler), action, target_type/id, before/after JSON diff, reason. Append-only: no update/delete code path, retention around 1 year, export. Reaper kills and automatic over-quota pauses are audited too, so users can be told why.

The admin UI must never show tenant secrets (BC Ferries password, cookies). Admin VNC into a tenant's browser is an open question (see below).
refs: src/ferry_watch/server.py:134-184, src/ferry_watch/views.py:118-143, src/ferry_watch/state.py:36-44

## How quota changes apply to existing trips
Rule: stored data is kept as-is; running resources and future actions follow the new limit right away; held carts are never silently destroyed.
- Lower maxTrips: keep existing trips and block adds until under the limit; the UI shows 'over quota'.
- Lower maxRunningBrowsers, or a suspension: stop the excess now in this order: keep holding trips, then the most imminent dates, then pause the rest as PausedOverQuota with auto-resume.
- Suspend: stop all non-holding pods and revoke VNC at once. The admin chooses whether a held cart is released now (empty_cart returns the spot) or allowed to finish its capped hold.
- Lower maxCartHoldMinutes: new holds use it; existing holds get expires_at = min(old, now + new).
- Raise minIntervalSeconds: push to pods at once.
- Disable auto-cart: applies from the next check; a current hold finishes.
- Lower VNC minutes: applies to today's ledger at once.
- Lower the lifetime ceiling: shorten activeDeadlineSeconds on live pods (allowed by Kubernetes) and recycle at the next idle moment.
refs: src/ferry_watch/watcher.py:375-382, src/ferry_watch/watcher.py:510-542, src/ferry_watch/watcher.py:189-196

RECOMMENDATIONS:
- 1. Identity first. Add an identity middleware to the operator API with configurable header names (Authelia Remote-User/Remote-Groups/Remote-Email; oauth2-proxy X-Forwarded-User/X-Forwarded-Groups/X-Forwarded-Email). Split groups on commas and handle repeated header values. Reject requests with no identity. Derive role = admin if the username is in the ConfigMap's bootstrapAdmins or any group is in adminGroups. Derive eligibility from registration mode: invite (default), group (allowedGroups), or open. Add Authelia/oauth2-proxy rules that require the user group for the app and the admin group for /api/admin/*. Use a NetworkPolicy so only the proxy reaches the API port, and give browser-pod reports a separate, token-authenticated port.
- 2. Storage. Create a SQLite schema on a PVC (WAL; operator replicas=1, Recreate strategy; Litestream backup) with these tables: users (id, username, email, state, tier, created/last_seen, last_groups), user_limits (nullable override columns), tiers, policy (defaults and cluster caps, versioned), trips (owner_id; globally unique id longer than today's 8 hex chars), usage_ledger (vnc_sessions, browser_runs, manual_checks, test_alerts), audit_log (append-only). Load ConfigMap ceilings at startup and clamp everything to them.
- 3. Replace MAX_TRIPS (models.py:16, store.py:90) with an EffectiveLimits resolver (override ?? tier ?? default, clamped to the ceiling). Enforce it at every mutating endpoint inside one BEGIN IMMEDIATE transaction: POST/PATCH trip (count non-past trips, check auto_cart count, interval floor, date horizon), enabling a trip, and cart-hold settings. Rate-limit /api/check and /api/alerts/test per user. Return 409/403 with field, limit and current usage. Replace State.max_trips with State.limits and State.usage, keeping max_trips as an alias for the current UI.
- 4. Build the operator scheduler. Admit trips into browser slots under maxRunningBrowsers, maxBrowsersTotal, maxConcurrentSpawns and maxChecksPerMinuteTotal. Order by max-min fair share (guaranteedBrowsers first, then weight), then date imminence, then aging. Expose Queued(reason, position) to the user. Operator preemption only of non-holding pods with no VNC session that have run at least 20 minutes and belong to over-share users. Never create a pod that would sit Pending.
- 5. Build the quota-backed reaper, which runs continuously:
   - stop pods for disabled, past or suspended trips;
   - recycle pods at maxBrowserLifetimeHours when not holding;
   - expire holds at the capped hold minutes;
   - close VNC on idle, per-session cap or per-day cap via a counting gateway;
   - kill pods whose heartbeat is lost, and orphan pods with no DB trip (adopt or delete by label);
   - flag or kill sustained high CPU while not checking (JS miners) and abnormal egress.
   Audit every reaper action as system:reaper and alert the user.
- 6. GitOps-owned Kubernetes backstop in a dedicated browser namespace:
   - PriorityClass ferry-browser (low value, preemptionPolicy: Never);
   - ResourceQuota scoped to that PriorityClass (pods, requests/limits cpu and memory, ephemeral-storage) set slightly above the operator caps;
   - ResourceQuota with scope NotTerminating and pods: 0, forcing activeDeadlineSeconds;
   - LimitRange (defaults and max per container, ephemeral-storage);
   - a VAP requiring the pinned image digest, priorityClassName, activeDeadlineSeconds <= ceiling, required user/trip labels and deterministic names, and no privileged/hostPath;
   - egress-bandwidth annotations if the CNI supports them;
   - operator RBAC with no write on ResourceQuota, LimitRange, PriorityClass or VAP;
   - an alert when quota used is above the operator cap.
- 7. Set browser pods' activeDeadlineSeconds to about lifetime ceiling + 2h at creation and shorten it when reaping. Name pods deterministically (fw-<tripUid>) so the API server enforces one browser per trip. Don't use KasmVNC -MaxIdleTime/-MaxConnectionTime as session limits (they terminate Xvnc and, via entrypoint.sh:38-39, the pod and its cart); use -IdleTimeout plus the gateway ledger, and consider -DLP_ClipSendMax/-DLP_ClipAcceptMax.
- 8. Ship the admin API/UI (users, effective limits with provenance, browsers, queue, policy, stop/restart/release, suspend, audit) and an append-only audit log of admin and system actions with before/after diffs and a required reason.
- 9. Implement the quota-change semantics above. Keep existing trips, apply limits on running resources immediately, cap existing holds at min(old expiry, now + new cap), and release carts only explicitly, with an alert.
- 10. Coordinate per-user BC Ferries login back-off in the operator. Today's back-off is in-process (watcher.py:333-349), so N pods would each retry. Broadcast a per-user 'logins paused until' to all of that user's pods, and cap logged-in pods with maxAutoCartTrips.
- 11. Tests:
   - concurrent POST /api/trips from the same user never exceeds maxTrips (transaction);
   - lowering limits pauses the right trips;
   - preemption never touches holding pods;
   - a pod create without activeDeadlineSeconds is rejected by the NotTerminating quota (kind/envtest);
   - the VAP rejects a foreign image.

RISKS:
- Identity spoofing: if anything other than the proxy can reach the operator API (including browser pods, whose Chromium a tenant controls over VNC), forged Remote-User/Remote-Groups headers give someone else's account or admin. Use a NetworkPolicy, strip headers at the proxy, and a separate authenticated channel for pod reports.
- Self-registration (registration: open) makes per-user limits pointless, because people can create many IdP identities. Invite or group is the safer default.
- Held carts are fragile. Pod priority is immutable, Kubernetes preemption or node drains can evict a holding pod, activeDeadlineSeconds can fire mid-checkout, and Xvnc's terminating flags kill the container. Recycle only when not holding, set the deadline well above the hold cap, and consider a PDB or safe-to-evict annotation during holds.
- With one pod per trip, each pod logs into the same BC Ferries account. Uncoordinated failures can lock the tenant's account, and many sessions per account may look abusive to BC Ferries.
- Every pod polls bcferries.com from one egress IP. Without a cluster-wide checks-per-minute budget, Queue-it or BC Ferries may throttle or block the whole service for all tenants. Holding inventory for many users also raises ToS and ethics questions.
- SQLite means a single-writer, single-replica operator on an RWO PVC. Node failure means downtime until reschedule, and losing the PVC without Litestream/backups loses users, trips and audit.
- The DB and the pods can drift (crash between pod create and DB commit, or the operator down during expiry). The reconciler must adopt or kill orphans by label, and the Kubernetes backstop must hold the line meanwhile.
- If the operator's ServiceAccount can edit ResourceQuota, LimitRange or the VAP, the backstop gives no protection against operator compromise.
- Admin 'view browser' or VNC access to a tenant's pod would expose the logged-in BC Ferries session, PII and saved cards. That conflicts with the requirement that the operator never gets tenant secrets.
- Fairness preemption can thrash (stop/start loops, repeated Queue-it waits). It needs a minimum run time and hysteresis.
- Per-browser CPU and memory numbers are estimates. Headed Chromium plus Xvnc must be measured before maxBrowsersTotal and the quotas are set, or the operator cap and the Kubernetes quota will disagree with real capacity.
- Trip ids are currently 8 hex chars (models.py:232) and unique only per config. Reusing them as global ids or pod names across tenants risks collisions.

OPEN_QUESTIONS:
- Who are the tenants: a handful of invited friends and family, or anyone the IdP admits? This sets the registration default and how strict the defaults should be.
- Are trips stored as CRDs (FerryTrip) or in the operator DB? Users and limits should live in the same store; I recommend the DB for both.
- Does the secrets-isolation design need a namespace per tenant? If so, the operator could sync a per-namespace ResourceQuota for a per-user Kubernetes backstop, but it would then need write on ResourceQuota (needs a VAP to cap the values).
- May an admin ever view a tenant's live browser over VNC (break-glass with consent and audit), or never?
- What is the browser node pool size, and the measured per-pod CPU/memory of headed Chromium plus KasmVNC? That gives maxBrowsersTotal and the quota values.
- Should past trips auto-archive or auto-delete after N days, and should disabled trips count toward maxTrips?
- Should time spent in an active checkout VNC session count against maxVncMinutesPerDay, or only time outside holds? Does the per-day reset use Pacific time?
- Is operator-level preemption acceptable to users? Or should a full cluster only queue, with no preemption beyond guaranteedBrowsers?
- Should users be able to choose their hold length up to the cap, and should admins be able to grant indefinite holds (today's BCF_CART_HOLD_MINUTES=0)?
- Is a JupyterHub-style shared pod per user (several trips as contexts in one Chromium) acceptable under capacity pressure? It roughly divides memory per trip at the cost of the one-pod-per-trip isolation.
