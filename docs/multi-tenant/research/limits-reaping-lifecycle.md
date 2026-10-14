===== reaping-lifecycle
Lifecycle and reaping design for one browser pod per trip, based on watcher.py, state.py, site.py, entrypoint.sh, KasmVNC v1.5.0 source and Kubernetes docs and code. kasmweb.com and kubernetes.io were blocked by the egress proxy (403), so every external fact was checked against primary sources on GitHub (raw.githubusercontent.com), which were reachable.

Main conclusions:
1. Treat the pod as disposable. Use restartPolicy: Never. The operator recreates pods, and every new pod is a fresh browser. A pod is replaced after every checkout session a person touched, every 24 h of watching, and after repeated failures. It is never recycled while it holds a cart or is in the middle of a check.
2. Enforce in three layers. The in-pod agent is primary and keeps working when the operator is down. The operator is the backstop, using heartbeats and pod deletion. Kubernetes is the last resort (activeDeadlineSeconds, a PodDisruptionBudget while holding, graceful termination with a preStop drain).
3. Do not use KasmVNC's MaxIdleTime, MaxConnectionTime or MaxDisconnectionTime. All three call exit(0) on Xvnc. Because the entrypoint runs `wait -n; exit 1`, that kills the container, Chromium and the held cart without emptying it. Also, nobody is connected or typing while a pod is only watching, so they would fire then too. Use -IdleTimeout instead (it only drops idle viewers) plus a VNC gateway owned by the agent that counts viewers and enforces caps.
4. SIGTERM ordering kills the cart today. tini -g forwards SIGTERM to Xvnc and to the Playwright driver, and Playwright closes Chromium on SIGTERM by default. So the cart must be emptied in a preStop hook, which runs before any SIGTERM.
5. Turn scale-to-zero on by default, by tier. Trips more than 21 days out, or beyond a per-user always-on browser quota, sleep and wake every 30 min for about 5 min. They log in only when a sailing opens, because every pod start is another BC Ferries login for that user.
6. Login backoff and serialization must become per user across all of that user's pods. Today they are per process (watcher.py:128-130, 333-349).

## How the current single-process lifecycle maps onto per-trip pods
Each trip already has its own session (one Playwright context per trip). Pod-per-trip replaces the context with a pod, and the existing rules carry over:
- A removed trip stops its check, empties any non-failed cart and closes its session (watcher.py:189-195).
- A held cart is serviced BEFORE the active check (watcher.py:199-204). So disabling a trip, or the global switch, does NOT release a held cart.
- A trip is active when config.enabled and trip.enabled and trip.date >= today() (watcher.py:228-229). It keeps watching after the last in-window departure of that day.
- After 3 consecutive failures the context is reset (RESET_SESSION_AFTER, watcher.py:52, 414-417). Retries back off at 0/60/120/300/600 s (watcher.py:51, 405-406). The user is alerted at the 3rd failure (watcher.py:53, 421-422).
- A check is considered stuck after 15 min (watcher.py:48, 257-261). Fare selection gets 40 s (watcher.py:50, 480).
- A cart holds for cart_hold_minutes (default 60; 0 means forever) (settings.py:30, 50-54; watcher.py:498).
- _service_cart ends a hold in three cases: release requested or expired (empty the cart), window closed by the user (resume), or browser lost (alert, and forget the departure so it is re-carted) (watcher.py:510-542). In every case it closes the session, so later watching already happens in a new context.
- Alert memory persists alerted departures and forgets a departure that was held when the process died (state.py:128-176, watcher.py:568-584). This already matches 'the pod died while holding' semantics, but the memory has to move to the operator (it is not secret) because pods are ephemeral.
refs: src/ferry_watch/watcher.py:46-54, src/ferry_watch/watcher.py:189-218, src/ferry_watch/watcher.py:228-229, src/ferry_watch/watcher.py:401-422, src/ferry_watch/watcher.py:498, src/ferry_watch/watcher.py:510-542, src/ferry_watch/watcher.py:568-595, src/ferry_watch/state.py:36-44

## Shutdown today loses held carts, and SIGTERM kills Chromium before Python can empty the cart
_close_all stops checks and closes sessions but never empties held carts (watcher.py:368-373). The code itself says a held cart is 'lost with the browser' on restart (state.py:131-133).

The ordering problem: the container runs tini with -g (Dockerfile:67), so SIGTERM goes to the whole process group. That group includes Xvnc, started with & (entrypoint.sh:20-26), and the Playwright Node driver. Playwright's server launches browsers with handleSIGTERM=true by default (browserType.ts L199-201), and site.py does not override it (site.py:154-157). Chromium itself runs in its own process group (processLauncher.ts L135-138, detached: true), but the driver kills it on SIGTERM. When Xvnc dies, headed Chromium loses its display.

So a SIGTERM handler in Python cannot reliably run empty_cart(), which is a navigation to /abandoned-cart/my-account (site.py:400-401). A preStop hook is the right tool: it must finish before TERM is sent, and the grace-period countdown starts before preStop runs.

Confidence: high on the Playwright default (source read); medium-high that the Python-spawned driver shares the shell's process group (not run).
refs: src/ferry_watch/watcher.py:368-373, src/ferry_watch/state.py:131-133, Dockerfile:67, entrypoint.sh:20-26, entrypoint.sh:36-39, src/ferry_watch/bcferries/site.py:149-160, src/ferry_watch/bcferries/site.py:400-401, https://github.com/microsoft/playwright/blob/v1.56.0/packages/playwright-core/src/server/browserType.ts#L199-L201

## KasmVNC MaxIdleTime, MaxConnectionTime and MaxDisconnectionTime terminate Xvnc, which kills the pod; IdleTimeout only drops the viewer
In KasmVNC v1.5.0, VNCServerST::checkTimeouts() calls exit(0) and logs 'MaxDisconnectionTime reached, exiting', 'MaxConnectionTime reached, exiting' or 'MaxIdleTime reached, exiting'. The man page agrees ('Terminate when ...'). The kasmvnc.yaml keys server.auto_shutdown.{no_user_session_timeout, active_user_session_timeout, inactive_user_session_timeout} map to these three flags.

With entrypoint.sh's `wait -n; exit 1` (entrypoint.sh:38-39), Xvnc exiting ends the container. Chromium dies and the held cart is never emptied.

Worse, lastDisconnectTime and lastUserInputTime start at server start (VNCServerST.cxx ~L153), and Playwright's CDP input never reaches Xvnc. So MaxDisconnectionTime and MaxIdleTime would fire during normal unattended watching. MaxConnectionTime counts from the first of the current group of connections (lastConnectionTime is set only when clients was empty), which is a usable wall-clock cap but too blunt.

-IdleTimeout (yaml user_session.idle_timeout) is different. VNCSConnectionST::checkIdleTimeout() closes only that client connection, with the reason 'Idle timeout'. Its clock is lastEventTime, which is updated on pointer and key events and at auth/init. KasmVNC's keepalive keysym is excluded, so a viewer left open on a phone does not count as active. The server keeps running.
refs: entrypoint.sh:20-26, entrypoint.sh:38-39, https://raw.githubusercontent.com/kasmtech/KasmVNC/v1.5.0/unix/xserver/hw/vnc/Xvnc.man (IdleTimeout/MaxDisconnectionTime/MaxConnectionTime/MaxIdleTime, ~L489-506), https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCServerST.cxx#L380-L463, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCSConnectionST.cxx#L543-L575, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCSConnectionST.cxx#L776-L779, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCSConnectionST.cxx#L860-L869, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/unix/kasmvnc_defaults.yaml#L31-L36

## Detecting 'unused': what KasmVNC can and cannot report
Connected clients: KasmVNC's HTTP 'owner API' (/api/get_sessions, /api/get_users, /api/clear_clipboard, ...) can list sessions and their connection times. However, `owner` is only ever set by BasicAuth with an owner user. Under -DisableBasicAuth (entrypoint.sh:23), every /api/ request gets 401.

Idle time: Xvnc tracks lastUserInputTime internally but exposes it only through MaxIdleTime, which exits.

Logs: authSuccess logs 'User %s connected at %ld' at info level, but the current -Log '*:stderr:10' probably filters info. That is unverified, and log scraping is brittle anyway.

Practical signals:
- (a) An agent-owned WebSocket gateway in front of Xvnc. Xvnc moves to 127.0.0.1, and the agent proxies /vnc. It gives exact viewer count, first-connect and last-disconnect times, and the ability to close viewers and refuse new ones without touching Xvnc.
- (b) Xvnc -IdleTimeout for input idleness, enforced server-side.
- (c) Optionally, last input from an init script the agent installs in the held context while holding. The agent is not driving the page then, so any trusted keydown or pointerdown came from a person.
- (d) X server idle via the MIT-SCREEN-SAVER extension (xprintidle). This is medium confidence: it is likely reset by VNC-injected input and not by CDP input, but it needs a test.
- (e) Agent heartbeats: state, last successful check, consecutive errors, Chromium RSS, viewer count, hold deadlines.
refs: entrypoint.sh:23, entrypoint.sh:26, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/network/websocket.c#L1514-L1553, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/network/websocket.c#L1918-L1980, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/network/websocket.c#L2022-L2040, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCSConnectionST.cxx#L657-L661, src/ferry_watch/state.py:103, src/ferry_watch/server.py:426-433

## activeDeadlineSeconds is a good last-resort backstop and a bad primary control
It counts from the pod's StartTime. On an existing pod it can only be set (if it was nil) or decreased, never increased or cleared (validation.go ValidatePodUpdate, ~L5904-5921). When it is exceeded, the kubelet's active-deadline sync handler forces phase=Failed (reason DeadlineExceeded) (kubelet_pods.go ~L1966-1972). The pod then terminates through the normal pod-worker path, whose effective grace period falls back to spec.terminationGracePeriodSeconds (pod_workers.go calculateEffectiveGracePeriod ~L1009-1039), so preStop should run. That last part is medium-high confidence, from reading the code, not a test.

Why it is awkward as the main control:
- A watcher for a trip 60 days out would need a 60-day deadline.
- The deadline cannot be extended when a cart is caught near expiry.
- A Failed pod never restarts.

Why it is still worth setting: it guarantees pods die even if the operator is gone for good (uninstalled, crash-looping, lost RBAC). Value: maxPodAge + holdHardCap + slack (24 h + 1 h + 1 h = 26 h). For cold-wake pods: wake window + hold cap + slack (5 m + 60 m + 5 m = 70 m). The agent should stop catching new carts when less than holdHardCap + 10 min remains before the deadline. The operator passes the absolute deadline in an env var, because the Downward API cannot expose spec.activeDeadlineSeconds.
refs: https://github.com/kubernetes/kubernetes/blob/master/pkg/apis/core/validation/validation.go (ValidatePodUpdate activeDeadlineSeconds rules, ~L5904-5921), https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kubelet_pods.go (~L1966-1972), https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pod_workers.go (~L1009-1039), https://github.com/kubernetes/website/blob/main/content/en/docs/reference/kubernetes-api/workload-resources/pod-v1.md

## PDB, drain and eviction facts that shape cart protection
- PDBs for operator-owned or bare pods allow only an integer .spec.minAvailable, not maxUnavailable or percentages. Eviction of a pod covered by more than one PDB is refused, so selectors must not overlap.
- PDBs only constrain the Eviction API, which is what drain and autoscalers use. Involuntary disruptions are not covered. The kubelet's node-pressure eviction ignores PDBs, and hard thresholds use a 0 s grace period, so preStop gets no time.
- unhealthyPodEvictionPolicy: AlwaysAllow keeps a crash-looping or unready pod from blocking drains forever.
- kubectl drain refuses running pods that have no controller ownerReference unless --force (filters.go unreplicatedFilter). Any controller ownerRef satisfies it, including one to a Trip CR. It also refuses emptyDir pods without --delete-emptydir-data.
- Disruptions add a DisruptionTarget condition (reason EvictionByEvictionAPI, TerminationByKubelet, DeletionByTaintManager, ...). The agent and operator can use it to tell 'maintenance' from 'crash' in alerts.
- Graceful node shutdown gives pods only the kubelet's shutdownGracePeriod budget.
refs: https://github.com/kubernetes/website/blob/main/content/en/docs/tasks/run-application/configure-pdb.md (Arbitrary workloads ~L278-297; Unhealthy Pod Eviction Policy ~L242-275), https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/workloads/pods/disruptions.md (~L130, ~L236-275), https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/scheduling-eviction/node-pressure-eviction.md (~L22-26), https://github.com/kubernetes/kubectl/blob/master/pkg/drain/filters.go#L218-L250, https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/cluster-administration/node-shutdown.md

## ownerReferences and garbage collection: owner placement matters
- Cross-namespace owner references are not allowed. If browser pods run in a per-tenant namespace (needed so the tenant's BC Ferries secret is mounted there and not in the operator's namespace), the Trip CR must live in that same namespace for GC to work.
- Background cascading deletion is the default. GC deletes dependents through normal graceful pod deletion, so the preStop drain also runs when a Trip is deleted.
- Never make the operator Deployment/ReplicaSet the pods' owner. Reinstalling or deleting it would cascade into deleting every browser pod.
- Deleting the Trip CRD deletes every Trip and therefore every pod. Mark the CRD helm.sh/resource-policy: keep.
- Downward API volumes are refreshed live, but env vars are not. If the operator needs to signal the agent without opening a network path, an annotation in a Downward API volume is a fallback channel. Pulling desired state from the operator in the heartbeat response is faster.
refs: https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/overview/working-with-objects/owners-dependents.md (~L33-58), https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/architecture/garbage-collection.md (~L66-105), https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/workloads/pods/downward-api.md (~L105-106), https://github.com/kubernetes/website/blob/main/content/en/docs/tasks/inject-data-application/downward-api-volume-expose-pod-information.md (~L132-139)

## A 'fresh browser' requires a new pod, not a container restart
Container restarts under restartPolicy Always/OnFailure keep the pod's emptyDir volumes. That means /tmp/home, Chromium's profile, caches and ~/Downloads would survive into the next session (entrypoint.sh:12-17 puts HOME under /tmp when the root FS is read-only). The newer in-place RestartAllContainers action keeps all volumes including emptyDir and skips both the grace period and preStop.

So with restartPolicy: Never, each restart is a new pod, the operator controls backoff, and nothing a VNC user did can persist. Each trip's pod is now the security boundary, which the current single shared Chromium (site.py:102-176) cannot provide.
refs: entrypoint.sh:12-17, src/ferry_watch/bcferries/site.py:102-176, https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/workloads/pods/pod-lifecycle.md (restartPolicy ~L270-298; RestartAllContainers ~L492-515)

## With one pod per trip, logins multiply: recycling and scale-to-zero cost BC Ferries logins
Login serialization (_login_lock) and the 30-min LoginFailed backoff (_login_blocked_until) are per process (watcher.py:128-130, 333-349). A process can also log in again whenever a context resets (watcher.py:315-318). With one pod per trip, each pod start is a separate login to the same user's account. A bad password then fans out to N pods trying in parallel, which raises lockout risk. A 24 h max age costs about 1 login per trip per day; hourly cold wakes with eager login would cost about 24 per trip per day.

The README already warns that a logged-in watcher visibly refreshes the account.

When login is needed: only auto_cart trips with credentials log in (watcher.py:299-300). Login has to happen before carting, because BC Ferries rejects a login while a guest cart exists. Cold wakes can therefore search as a guest and log in only once something opens, at the cost of roughly one extra form walk at the critical moment.
refs: src/ferry_watch/watcher.py:128-130, src/ferry_watch/watcher.py:299-318, src/ferry_watch/watcher.py:333-349, README.md (Things to know about BC Ferries)

## Scale-to-zero trade-off for far-off trips
Checks: about 3 s each (results reload, site.py:356-359), at the user's interval (default 30 s, minimum 15 s; models.py:19, 284).

Warm-up of a new pod: scheduling, Xvnc, Chromium, Queue-it (up to 10 min, site.py:52), login, then a 3-5 navigation form walk at up to 60 s each (site.py:49-51, 303-354). This has never been measured. The code already records session_opened_at (watcher.py:311) and checked_at (watcher.py:434), so p50/p95 can be measured before tuning. Expect tens of seconds normally and minutes when queued.

Always-on pod: Chromium with one heavy site plus Xvnc plus the agent, likely 0.6-1 GiB RAM (estimate). Detection latency is at most the interval.

Cold tier waking every P minutes for a window W: duty cycle ≈ (warm-up + W)/P. Example: (60 s + 300 s)/1800 s ≈ 20% (≈5% if W is just 2-3 checks). Detection latency rises to about P + warm-up.

Popular cancellations get re-booked within minutes, so the cold tier lowers catch rate. It is only acceptable where availability churns slowly (far dates) or where a user has used up their always-on quota. A pod that stays up with Chromium closed between checks saves no scheduled capacity, because requests are static. In-place resize exists but adds complexity, so it is not worth it here.
refs: src/ferry_watch/bcferries/site.py:49-52, src/ferry_watch/bcferries/site.py:303-359, src/ferry_watch/models.py:19, src/ferry_watch/models.py:284, src/ferry_watch/watcher.py:311, src/ferry_watch/watcher.py:434, src/ferry_watch/state.py:67, src/ferry_watch/state.py:75

## Agent control endpoints inside the pod can be reached from the VNC user's Chromium
Chromium in the browser pod can load http://127.0.0.1:<port> and the pod IP, including KasmVNC's own :6080 and any agent HTTP control API. If 'drain', 'release' or (worse) 'extend hold' were on loopback TCP, a VNC user could reach them from the address bar.

The preStop hook and the local control channel should therefore use a Unix socket (preStop exec: `ferry-watch agent drain --timeout 60`). There should be no 'extend' operation at all. Chromium should also block loopback and pod-CIDR URLs (owned by the browser-lockdown/egress area).
refs: entrypoint.sh:20-26, src/ferry_watch/server.py:446-464

## Failure classes need different reaping responses, and the fleet needs a circuit breaker
- QueueTimeout, SessionExpired and transient navigation errors: retry inside the pod with the existing backoff.
- Browser lost (site.py:162-165, 204-212) or an unexpected exception: recycle the pod.
- RouteRejected: a configuration error. Recycling burns resources forever, so stop after 3 and mark the trip 'needs attention'.
- LoginFailed: never recycle, because recycling means another login.
- MarkupChanged (site.py:365-370): BC Ferries changed its site, and every pod fails at once. Recycling every pod would cause a login and Queue-it storm across all users. The operator must detect fleet-wide failure (for example, more than 50% of pods reporting MarkupChanged within 10 min), pause recycling and backoff resets, and alert the admin once.

Also, a trip date beyond the published schedule probably renders no sailings, which surfaces as MarkupChanged. That is unverified and needs a fixture. It should be classified as 'not yet bookable', which moves the trip to the cold tier, not into failure handling.
refs: src/ferry_watch/bcferries/site.py:162-165, src/ferry_watch/bcferries/site.py:204-222, src/ferry_watch/bcferries/site.py:361-371, src/ferry_watch/bcferries/errors.py, src/ferry_watch/watcher.py:401-422

## Today a hold can last forever, and the VNC view is always open
- cart_hold_minutes=0 holds a cart until it is released (settings.py:30, 53-54; watcher.py:498, 516). In a multi-tenant system, that is an unbounded VNC session.
- The VNC viewer is available any time, not just while holding: Xvnc has no auth and is shared (-AlwaysShared, entrypoint.sh:23-24), gated only by Authelia.
- Alerts link to a single static BCF_VNC_URL (settings.py:24, 63; notify.py:229-245).

Per-trip pods need three things instead: a per-trip, stable VNC route that survives pod replacement; a short-lived token tied to the cart_id (Cart.id, state.py:40); and a gate that refuses viewers unless the trip is holding.
refs: src/ferry_watch/settings.py:24, src/ferry_watch/settings.py:30, src/ferry_watch/settings.py:53-54, src/ferry_watch/settings.py:63, src/ferry_watch/notify.py:229-245, entrypoint.sh:23-24, src/ferry_watch/state.py:36-44

RECOMMENDATIONS:
- 1. Use bare Pods managed by the operator, not Deployments. Pod spec: restartPolicy: Never. One active pod per trip, named fw-<trip>-<n> and recorded in Trip.status.currentPod. Labels: app.kubernetes.io/managed-by=ferry-watch-operator, ferry-watch.io/trip=<id>, ferry-watch.io/user=<id>, ferry-watch.io/holding=false. Make the Trip CR (in the same tenant namespace) the controller ownerReference, with controller: true and blockOwnerDeletion: true. This also satisfies kubectl drain's controller check. terminationGracePeriodSeconds: 90. preStop exec: `ferry-watch agent drain --timeout 60` over a Unix socket. activeDeadlineSeconds: 93600 (26 h) for watchers and 4200 (70 min) for cold-wake pods. Lower the not-ready/unreachable tolerationSeconds to 60. Set memory requests equal to limits (for example 1Gi). emptyDir sizeLimit about 1Gi. A lower PriorityClass for cold-wake pods. Keep the CRD with helm.sh/resource-policy: keep, and never let the operator's own workload own pods.
- 2. States, kept in Trip.status.browser.phase:
- Off: no pod wanted.
- Sleeping: cold tier, with nextWakeAt.
- Queued: blocked by quota or capacity.
- Pending: Pod object exists, not running yet.
- Starting: Xvnc, Chromium, Queue-it, login and form walk, up to the first successful check.
- Watching: plus a `busy` flag while a check or fare selection (≤40 s) is in flight.
- Holding/Unclaimed: cart READY, VNC gate open, no viewer yet.
- Holding/InCheckout: a viewer has connected.
- Draining: emptying the cart and closing; the agent exits 0.
- Gone: the operator decides the next state.
- Failed: exit ≠ 0, OOMKilled, Evicted, liveness failure, Starting timeout, or lost heartbeat.
- BackingOff: too many failures (5, 10, 20, 40, then 60 min cap). Reset after 1 h of healthy Watching.
- Booked: terminal; trip disabled and kept.
- 3. Event → action:
- Trip created or enabled: Pending (always-on) or Sleeping (cold). Start a user's pods at least 20 s apart so logins stay serialized like watcher.py:128.
- Search-key edit: Draining → new pod. Other edits (window, fare, auto_cart): push to the agent live with no recycle. Edits during Holding apply after the hold ends.
- Trip disabled by the user: Watching → Draining → Off. A hold is allowed to finish, then Off (keeps the current watcher.py:199-204 semantics).
- Trip deleted: GC deletes the pod gracefully and preStop empties the cart. An optional Trip finalizer, bounded to 2 min, records 'cart returned'.
- Last in-window departure passed (Pacific): Draining → Off. A same-day hold is released at its departure time. This replaces date >= today (watcher.py:229).
- Global maintenance switch: no new pods, Watching pods drain, holds finish (up to the hard cap).
- Admin suspends a user: immediate Draining of all their pods, holds included (empty carts, revoke VNC tokens).
- Cart READY: flip label holding=true and issue a VNC token in the Pushover link.
- Viewer connects: Holding/InCheckout.
- Hold ends by user release, window closed, timeout, idle, session cap or hard cap: Draining → Gone → fresh pod, unless the trip was disabled or booked.
- Booking confirmation detected (needs a captured fixture for the URL) or the user taps 'I booked it': Booked → Off.
- Login failed: no recycle. Set a user-level loginBlockedUntil (+30 min) and send it to all of that user's pods. New pods start with login disabled until then.
- 4. Hold and VNC limits, enforced first by the agent (which extends _service_cart, watcher.py:510-542), then by the operator, then by Xvnc. Defaults (admin maximum in brackets):
- VNC gate closed unless Holding. Admin debug view is read-only.
- Unclaimed hold: 30 min [60], with a Pushover reminder at 15 min. This replaces the 60 min default in settings.py:30.
- Checkout session from first viewer connect: 20 min [45], with a warning at 17 min.
- No viewer connected for 10 min after the first connect: release. This also covers phone app-switching for 2FA or a card.
- Xvnc -IdleTimeout 600: drops input-idle viewers without killing Xvnc.
- Absolute hold cap: 60 min from READY [90].
- BCF_CART_HOLD_MINUTES=0 is forbidden for tenants.
- Every hold end runs empty_cart() and then exits the pod. Optimization: if zero viewers ever connected during the hold, the agent may resume in the same pod and save a login.
- Never pass -MaxIdleTime, -MaxConnectionTime or -MaxDisconnectionTime (or the matching kasmvnc.yaml auto_shutdown keys).
- 5. Build a VNC gateway inside the agent. Move Xvnc to -interface 127.0.0.1 on an internal port. The agent serves /vnc on :6080 and proxies WebSocket and HTTP to it. It checks the auth-proxy identity header against the trip owner, plus a signed token (trip, user, cart_id, exp of 15 min, verified with the operator's public key so it is not a tenant secret). It counts viewers, records first_connect_at and last_disconnect_at, can close every viewer, refuses viewers outside Holding, and meters bytes; a session above about 500 MB looks like streaming, so close it.

The operator is not in the VNC data path. The authenticating proxy routes /vnc/t/<trip> to the trip's current pod, so the link in an alert survives pod replacement. Keep -FrameRate low (15) as a cheap anti-streaming lever. Consider -DLP_KeyRateLimit.
- 6. Heartbeats and staleness. The agent sends to the operator every 15 s: phase, busy, last_check_ok_at, consecutive_errors, error class, cart {id, depart, ready_at, deadline}, viewers, first_connect_at, last_input_at, Chromium RSS and pod deadline. The response carries desired state (drain, loginBlockedUntil, live trip edits), so no ingress to browser pods is needed. Fallback channel: an annotation in a Downward API volume.
- Stale: no heartbeat for 90 s.
- Dead: 3 min when Watching, 5 min when Holding (enough for a queue wait during cart selection). Then delete the pod; preStop tries to empty the cart.
- After an operator start or leader change, wait 2 min before judging any pod stale.
- 7. Reaping rules:
- Max pod age 24 h with ±10% jitter, applied only when Watching and not busy.
- RSS recycle at 85% of the memory limit.
- Starting timeout 15 min, matching CHECK_TIMEOUT_SECONDS.
- Pending or unschedulable for more than 10 min: admin capacity alert; leave it Pending.
- Inactive user: no dashboard visit or alert action for 14 days pauses their trips, with a Pushover warning 48 h before.
- Orphan sweep every 5 min: list pods with the managed-by label. A pod whose Trip is missing, Off or not its currentPod gets drained and deleted. A Holding orphan gets 'drain' through desired state first.
- Delete Failed or Succeeded pods once their status has been recorded.
- 8. Scale-to-zero tiers (admin-tunable per user):
- Always-on: date within 21 days, or a user-flagged 'priority' trip, up to the user's quota maxAlwaysOnBrowsers (default 3), with the nearest dates first.
- Cold: everything else. Wake every 30 min with ±20% jitter, run about 5 min of checks, search as a guest and log in only once a sailing is in the window. If something is carted, the pod becomes Holding (its 70 min deadline covers the hold). Otherwise it exits 0 → Sleeping.
- Promote a trip to always-on automatically when its date comes within 21 days or a quota slot frees up.
- Measure warm-up p50/p95 from session_opened_at → first checked_at before finalizing P and W.
- 9. Disruptions. Create one static PDB per trip: selector trip=<id>, holding=true, minAvailable: 1, unhealthyPodEvictionPolicy: AlwaysAllow. The operator flips the pod label on entering or leaving Holding. Expect a few seconds of disruption-controller lag; never create overlapping PDBs.
- Watching pods can always be evicted; the replacement only costs a warm-up.
- Holding pods block drains for at most the hold hard cap (≤60-90 min). Admin knob drainPolicy: waitForHolds (default) | releaseNow. A later option, handoffUnclaimed, would bring up a replacement first, then empty the cart and re-cart.
- Node NotReady plus a lost heartbeat for 60 s: start a replacement immediately (new name); do not wait for taint-based deletion. For a held cart, send the cart_lost alert and forget the departure, as watcher.py:531-535 does today.
- Use DisruptionTarget reasons in alert text ('maintenance' vs 'crash').
- 10. Operator restarts and upgrades must be invisible to pods. Reconciliation is level-based and adopts pods by label and Trip.status. Hold deadlines are absolute timestamps set by the agent and mirrored to Trip.status. Leader election. No Helm hooks that delete CRs.

Pod template changes are rolled out only at natural recycles: max age, hold end, or failure. An admin 'force roll' still waits for holds to end. If the operator is unreachable, agents keep enforcing their own deadlines, and after a hold ends they exit; the trip resumes when the operator returns.
- 11. Failure handling:
- Inside the pod: context reset after 3 consecutive failures (existing).
- Pod recycle after 6 consecutive failures, or 2 resets with no success in between.
- BackingOff after 3 pod failures within 30 min.
- RouteRejected ×3: Off with 'needs attention'.
- LoginFailed: never recycles.
- Fleet circuit breaker: when more than 50% of pods fail with the same class (MarkupChanged) within 10 min, freeze recycling and backoff resets, alert the admin once, and probe with a single canary pod.
- 12. Per-user quotas the admin sets, all checked by the operator on Trip create/enable and on Holding transitions:
- maxTrips (replaces MAX_TRIPS=6, models.py:16 / store.py:89-91)
- maxAlwaysOnBrowsers: 3
- maxConcurrentHolds: 2
- maxHoldsPerDay: 10
- maxVncMinutesPerDay: 90
- holdMinutes caps (point 4)
- minIntervalSeconds floor: 30 (the current minimum is 15, models.py:19)
Also a global cap on browser pods and on BC Ferries checks per minute, because every pod egresses from the same cluster IP.
- 13. Code changes to prepare in the existing modules:
- Watcher runs for a single trip per pod.
- _close_all/drain empties held carts with a bounded wait. In drain mode, cap the Queue-it wait at about 45 s instead of 10 min (site.py:52).
- Login backoff and lock driven by the operator per user.
- Alert memory loaded from and saved to the operator.
- _active uses the last in-window departure time.
- Cart gets first_connect_at, viewer and idle deadlines.
- Remove the 'hold forever' 0 value for tenants.
- Control API on a Unix socket only.

RISKS:
- The preStop drain is best effort. Node-pressure hard evictions use a 0 s grace period, and node crashes, OOM kills and an empty_cart that ends up in Queue-it all skip it. The cart then stays locked until BC Ferries expires it, and the replacement pod cannot re-cart it until then. BC Ferries' own cart time-to-live is unknown.
- Logins multiply with pod churn (recycles, cold wakes, failures). Without per-user login coordination and lazy login, account lockouts or BC Ferries flagging accounts become likely.
- A PDB on holding pods can block cluster maintenance for up to the hold hard cap per pod. If there are many holds, drains stall. Admins need the drainPolicy knob and visibility.
- Without egress restrictions and a Chromium lockdown (owned by another area), a 20-60 min VNC session is still an open proxy or browser. Time caps limit how long, not what is done.
- The cold tier lowers the chance of catching quickly re-booked cancellations. Users may see it as 'the watcher missed it', so the tier must be visible in the dashboard and alerts.
- activeDeadlineSeconds cannot be extended. A clock or config mistake (deadline shorter than max age plus hold cap) kills pods mid-checkout.
- A fleet-wide BC Ferries markup change without a circuit breaker causes a storm of recycles, logins and Queue-it visits across every tenant, which could get the cluster IP blocked.
- Two pods can briefly run for the same trip during node partitions or handoff. VNC routing and cart ownership must follow Trip.status.currentPod to avoid double carts.
- The last-input signal is unverified. Both X idle via MIT-SCREEN-SAVER and the in-page listener need testing against KasmVNC 1.5.0 and Playwright CDP input.

OPEN_QUESTIONS:
- After a checkout session where the user connected, should the trip pause by default (to avoid double-booking another in-window sailing) or keep watching as it does today (watcher.py:529-530)? What URL or markup marks BC Ferries' booking confirmation (needs a captured fixture)?
- What are the real warm-up p50/p95 (pod start → first successful check) and Chromium RSS for one trip? These set the cold-tier interval and wake window, the memory request and the RSS recycle threshold.
- How long does BC Ferries itself hold a cart? Does a cart survive in the account (server side) when the session that created it dies? This decides whether to empty on drain or attempt a handoff.
- Should admins be able to give specific users longer holds or unlimited always-on browsers (owner/VIP), and should users be able to buy or choose priority per trip?
- Can the cluster's ingress or auth proxy route /vnc/t/<trip> to the current pod with WebSocket support and pass the identity header through to the agent gateway? Which proxy is used (Authelia in front of Traefik or nginx)?
- Is the Trip CR acceptable in the tenant namespace (needed for ownerReferences, since cross-namespace owners are not allowed), or must the operator keep trips in its own database and fall back to label sweeps plus activeDeadlineSeconds for garbage collection?
- Default drain policy for holds: wait up to the hold cap, or release immediately and re-cart? And does the cluster use graceful node shutdown (shutdownGracePeriod) or an autoscaler that drains nodes?
- Does KasmVNC 1.5.0's web client reconnect automatically after an 'Idle timeout' disconnect? If it does, the idle rule must live in the agent gateway rather than rely on Xvnc -IdleTimeout.
