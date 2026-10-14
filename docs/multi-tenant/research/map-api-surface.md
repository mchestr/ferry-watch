===== api-surface
The API is one aiohttp app (server.py:445-478) with 11 routes plus static files. Every route works on a single process-wide Services object (server.py:60-92): one ConfigStore/config.json, one RuntimeState, one Watcher, one AlertDispatcher tied to one Pushover user key, and one global checkout_url (BCF_VNC_URL). Requests carry no authentication or identity at all (server.py:3-6, docs/api.md:3-4). The only controls are anti-CSRF ones (writes must be JSON and same-origin, and the WebSocket checks Origin), a strict CSP built from script hashes, and nosniff, no-referrer and cache headers. The WebSocket hub builds ONE snapshot per coalesced burst of changes and sends the identical payload to every client (server.py:148-161). Scoping it per tenant therefore means restructuring the hub, not just filtering its output. Each endpoint and each snapshot field reads global state:
- the global enabled/interval settings
- MAX_TRIPS=6 per config
- one 80-entry event deque that also holds events not tied to any trip
- check_now, which resets back-off for every trip
- one global test-alert cooldown
- a /healthz based on a single heartbeat
- one checkout_url, used by the cart alert and by two dashboard links

Alerts are written in the watcher, and only cart_message carries the VNC URL (watcher.py:559-560, notify.py:229-246). For multiple tenants the app needs:
- An identity middleware fed by Authelia's Remote-User/Remote-Groups headers, trusted only on the ingress path. Browser pods must never be able to reach that port, because a tenant with headed Chromium and DevTools can send any header.
- Data access scoped per tenant, with a 404 for any trip the caller doesn't own.
- A per-tenant hub.
- Per-tenant Pushover user keys, with the shared app token kept in the operator.
- Per-trip VNC URLs served directly from the browser pods and never proxied through the operator.
- Quotas enforced atomically.
- A separate, authenticated internal port for pod reports and commands.

The CSP needs no change for links that open in a new tab. It needs frame-src (plus frame-ancestors on the VNC origin) only if the VNC view is put in an iframe.

## Endpoint inventory and the global state each one touches
Routes registered at server.py:454-465.

- GET /api/state (327-328) calls Services.snapshot (84-92). It reads store.config, the whole RuntimeState, the cached terminals, settings.checkout_url, `settings.credentials is not None` and alerts.configured.
- GET /api/terminals (331-334) reads the shared TerminalDirectory: BC Ferries' public route list, cached in terminals.json next to config (494). It is the same for everyone and can stay shared, but any caller can trigger an outbound fetch when the cache is cold (308-312).
- GET /api/ws (436-439) runs the Origin check, then Hub.serve.
- PATCH /api/settings (337-344) changes Config.enabled and interval_seconds (models.py:280-305). These gate EVERY trip (watcher.py:229, 265-266), and the handler writes a global 'Watching paused/resumed' event.
- POST /api/trips (347-356):
  - drops any client-supplied id (350); the id is 8 random hex characters (models.py:232)
  - checks the route against the terminals and that the date hasn't passed
  - store.add_trip enforces MAX_TRIPS=6 for the single config (store.py:265-271, models.py:16)
  - writes a global 'Trip added' event
- PATCH /api/trips/{id} (359-373) looks the trip up globally with store.require (store.py:283-287), replaces it, and resets runtime results when search_key changes (369-370).
- DELETE /api/trips/{id} (376-380) only removes the trip from config. The held cart is emptied later, in the watcher's next cycle (watcher.py:186-193). docs/api.md:130 promises 'releases any held cart'.
- POST /api/trips/{id}/cart/release (383-390) and /cart/dismiss (393-400) call store.require, then watcher.release_cart / dismiss_cart, which change the in-process TripRuntime (watcher.py:147-162). cart_id guards against stale requests (409 on mismatch).
- POST /api/check (403-407) calls watcher.check_now, which clears retry_at/next_due on EVERY trip and so skips back-off (watcher.py:141-145).
- POST /api/alerts/test (410-423) uses the global Services.last_test_alert (70) and the single dispatcher.
- GET /healthz (426-433) looks only at the single watcher_task and state.heartbeat_at.

Every mutation returns the full global snapshot via _state (304-305), as docs/api.md:12 requires.
refs: src/ferry_watch/server.py:327-439, src/ferry_watch/server.py:454-465, src/ferry_watch/server.py:304-305, src/ferry_watch/store.py:265-287, src/ferry_watch/watcher.py:141-162, src/ferry_watch/watcher.py:186-193, src/ferry_watch/models.py:16, src/ferry_watch/models.py:232

## WebSocket hub: coalescing, newest-only senders, pings, and why it can't be filtered per tenant as written
- Hub.notify only sets one global dirty Event (142-143). It is subscribed to both RuntimeState.changed and ConfigStore commits (73-75; state.py:110-113; store.py:289-294).
- Hub.run waits for that flag, sleeps COALESCE_SECONDS=0.1, builds ONE payload via the zero-argument snapshot callable (145-146, 150-156), and offers the identical string to every client (160-161).
- Each _Client keeps only the newest pending payload; offer() replaces anything unsent (110-112).
- After 15 s of silence (PING_SECONDS) the sender sends {type:'ping'} (117-119).
- A send that takes longer than 10 s (SEND_TIMEOUT_SECONDS) aborts the transport (126-131).
- aiohttp's WS heartbeat is set to 30 s (164).
- Clients send nothing; the read loop exists only to keep the socket open (171-172).
- Identity: _Client keeps `request` but nothing reads identity from it. The snapshot callable takes no arguments, so per-tenant payloads need (a) clients grouped by tenant, resolved once at upgrade, (b) a dirty set per tenant, and (c) a snapshot function that takes the tenant.
- Once the socket is upgraded, nothing re-checks the user's session. Pings keep it alive indefinitely, so a revoked or expired Authelia session keeps receiving data.
- There is no limit on concurrent sockets.
- Frontend: live.ts:42-46 always connects to /api/ws on the page host, and state.ts:26 drops a snapshot whose seq is older within the same boot_id. A per-tenant seq is compatible with this.
refs: src/ferry_watch/server.py:43-46, src/ferry_watch/server.py:101-183, src/ferry_watch/state.py:110-113, src/ferry_watch/store.py:289-294, web/src/lib/live.ts:42-46, web/src/lib/state.ts:26, tests/test_server.py:277-306, docs/api.md:137-144

## Current security controls (anti-CSRF, WS Origin, CSP, headers) and what they don't cover
- No authentication at all, by design (server.py:3-6; docs/api.md:3-4; README.md:80-84).
- Middleware order is [security_headers, cache, error, cross_site] (449-451), so ApiError raised by cross_site_middleware turns into a JSON error.
- cross_site_middleware (220-229) requires two things for POST/PUT/PATCH/DELETE:
  - _same_origin, which compares the Origin netloc to request.host (193-196). With no Origin header it accepts Sec-Fetch-Site of same-origin or none, and defaults to accepting when both headers are missing, so non-browser clients pass (197-198).
  - Content-Type: application/json (227-228).
- The WS upgrade uses the same _same_origin check (437-438). Tests: test_server.py:242-250, 293-296.
- The JSON parser rejects NaN and Infinity (290-301).
- content_security_policy (232-251):
  - default-src 'self'; script-src 'self' plus sha256 hashes of the inline scripts in index.html
  - style-src 'self' 'unsafe-inline'; img-src and font-src 'self' data:; connect-src 'self'
  - object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
- The CSP is only built when static_dir/index.html exists (448). security_headers_middleware (254-267) adds nosniff, Referrer-Policy: no-referrer and the CSP. cache_middleware (270-280) makes /assets immutable and everything else no-store.
- The frontend rejects non-http(s) checkout_url and booking_url values (web/src/lib/schema.ts:29, 88, 111).
- LinkButton adds rel=noopener noreferrer to target=_blank links (web/src/components/ui/button.tsx:43-52).
- Gaps for multi-tenancy:
  - no notion of a user or role anywhere
  - no rate limits apart from the test alert
  - no audit trail; the access log is disabled (__main__.py:251) and events don't record who acted
  - the 'network policy only lets the proxy in' assumption (entrypoint.sh:4-6) is the entire access control
refs: src/ferry_watch/server.py:3-6, src/ferry_watch/server.py:193-267, src/ferry_watch/server.py:290-301, src/ferry_watch/server.py:436-439, src/ferry_watch/server.py:448-451, src/ferry_watch/__main__.py:251, web/src/lib/schema.ts:29, web/src/lib/schema.ts:111

## Alert composition: which alerts exist, when they fire, and which URL they carry
All alerts are built in the watcher and sent through `Alerts.submit`.

| Alert | Fires when | Priority | URL |
|---|---|---|---|
| bookable_message (notify.py:216-226) | new in-window sailings were found and no cart was taken (watcher.py:561-564) | high | booking_url, the bcferries.com search link ('Book on bcferries.com') |
| cart_message (notify.py:229-246) | a fare was added to the cart (watcher.py:559-560) | high | checkout_url ('Open checkout', 'Tap to finish checkout'), falling back to booking_url when BCF_VNC_URL is unset (notify.py:243-244) |
| failing_message (notify.py:249-250) | exactly at the 3rd consecutive failure (watcher.py:421-422; FAILURE_ALERT_AFTER=3 at watcher.py:53) | normal | none |
| cart_lost_message (notify.py:253-259) | the browser holding the cart crashed (watcher.py:535) | high | none |
| cart_expired_message (notify.py:262-267) | cart_hold_minutes ran out (watcher.py:528) | normal | none |
| login_failed_message (notify.py:270-274) | a login failed (watcher.py:349) | normal | none; not tied to a trip |
| Test alert '⛴ Ferry Watch test' (server.py:419) | the test button | normal | none |

- cart_message is the ONLY alert that uses the VNC URL. checkout_url is a single global string from BCF_VNC_URL (settings.py:167, 206), passed into Watcher (server.py:520) and the snapshot (server.py:89).
- The same global URL feeds two dashboard links: the 'Live browser' button (WatcherCard.tsx:222-225) and 'Open checkout' on each trip's cart banner, wired from state.checkout_url (App.tsx:72; CartBanner.tsx:136-139).
- Message (notify.py:31-37) has no tenant or trip field.
- Error text in alerts and events goes through describe(error, secrets), which redacts BCF_USER/BCF_PASS (watcher.py:122, 133-134).
refs: src/ferry_watch/notify.py:31-37, src/ferry_watch/notify.py:216-274, src/ferry_watch/watcher.py:53, src/ferry_watch/watcher.py:122, src/ferry_watch/watcher.py:345-349, src/ferry_watch/watcher.py:421-422, src/ferry_watch/watcher.py:528, src/ferry_watch/watcher.py:535

## Pushover delivery: one notifier, background retries, failure events, redaction
- _lifecycle builds ONE PushoverNotifier(http, token, user_key) when both env vars are set, otherwise a LogNotifier (server.py:497-501). settings.py:186-188 requires the two vars as a pair.
- PushoverNotifier holds the user key as a constructor field (notify.py:54-58), so there is exactly one recipient.
- send() posts a form with title truncated to 250, message to 1024, url_title to 100, and priority 1 or 0 (notify.py:64-74). The url itself is not truncated; Pushover's documented url limit is 512 characters, which only matters if per-trip URLs ever get long.
- Network errors and non-200 replies raise NotifyError. _reason quotes Pushover's own `errors` text but replaces the token and user key with [redacted] (notify.py:85-96; test_notify.py:169-184).
- AlertDispatcher.submit sends in a background task (notify.py:148-151), retrying after (0,10,30,60,120,300) s, about 8.7 min in total (137, 157-169). When it gives up it calls on_failure. That is wired to state.event(ERROR, 'Couldn't deliver the alert …') with no trip_id (server.py:503-504), so the failure shows in everyone's activity feed.
- send_now has no retries; it is used by the test button (153-155).
- aclose cancels alerts still in flight on shutdown (171-174), so a pod restart can drop an alert that is still retrying.
- `configured` (144-146) is global and becomes state.alerts_configured (server.py:91).
refs: src/ferry_watch/notify.py:19-24, src/ferry_watch/notify.py:53-96, src/ferry_watch/notify.py:130-174, src/ferry_watch/server.py:497-506, src/ferry_watch/settings.py:174-175, src/ferry_watch/settings.py:186-188, tests/test_notify.py:91-111, tests/test_notify.py:169-184

## Test-alert rate limit and health endpoint are process-global
- test_alert (server.py:410-423):
  - returns 409 when the dispatcher isn't configured
  - returns 429 when time.monotonic() minus Services.last_test_alert (a single float, server.py:70) is under TEST_ALERT_COOLDOWN_SECONDS=30 (server.py:46)
  - otherwise send_now, mapping NotifyError to 502 with Pushover's redacted reason, then writes a global 'Test alert sent' event (422)
  - with several tenants, one tenant's test blocks everyone else for 30 s, and all tests go to the single user key
- healthz (426-433) returns 503 'watcher stopped' if the one watcher task has finished, or 'watcher stalled' if the global heartbeat is older than 15 min (views.py:17).
- If the operator kept this semantics while aggregating many browser pods, one tenant stuck in Queue-it or crashing would fail the operator's liveness probe and restart the control plane for everyone.
- The same heartbeat is exposed as state.watcher (views.py:142) and is shown as one global health indicator.
refs: src/ferry_watch/server.py:46, src/ferry_watch/server.py:70, src/ferry_watch/server.py:410-433, src/ferry_watch/views.py:17, src/ferry_watch/views.py:142, tests/test_server.py:260-274

## Complete list of single-user / global-state assumptions and what each needs
Each item gives the location, what it assumes today, and what multi-tenancy needs there.

1. Services is a singleton per app (server.py:60-95, 524). Needs a tenant-aware repository instead.
2. One ConfigStore over config.json (server.py:496; store.py:246-318). Needs a store per tenant, or CRs labelled by tenant.
3. Config.enabled and interval_seconds are global (models.py:280-305; PATCH /api/settings at server.py:337-344). Needs per-tenant settings, plus an admin floor on the interval: MIN_INTERVAL_SECONDS=15 (models.py:19) multiplied by N tenants is N times the load on BC Ferries.
4. MAX_TRIPS=6 per config (models.py:16; store.py:266-267; reported as max_trips at views.py:135). Needs a per-tenant limit plus a cluster browser cap.
5. store.require(trip_id) finds any trip by id (store.py:283-287). It's used by patch, delete, release and dismiss (server.py:361, 378, 386, 396). Needs a lookup scoped to the tenant that returns the same 404 'No such trip' for foreign trips (server.py:211-212).
6. Trip ids are unique only within one config (store.py:268-269; config_from_json at store.py:220-222), 32-bit random (models.py:232). Needs global uniqueness if they become k8s object names; _ID_RE (models.py:25) is already DNS-label safe.
7. RuntimeState holds a single seq, a single event-id counter and one events deque of 80 (state.py:27, 96-125); the view shows 40 (views.py:138). One noisy tenant evicts others' events, and the global ids and seq leak other tenants' activity rate. Needs per-tenant events and seq.
8. Events with no trip_id: 'Ferry Watch started' (server.py:530), 'Watching paused/resumed' (343), 'Test alert sent' (422), alert delivery failure (504), login failure (watcher.py:348). Each needs a tenant id or must be admin-only.
9. checkout_url is global (settings.py:167; views.py:139; server.py:89, 520). Needs a vnc_url on each trip.
10. login_configured is global, derived from env credentials (server.py:90; views.py:140), and also drives trip_defaults.auto_cart (views.py:98-115). Needs a per-tenant flag, reported by the pods as a boolean only.
11. alerts_configured is global (server.py:91). Needs to be per tenant: does this tenant have a Pushover key?
12. One notifier bound to one user key (server.py:497-501; notify.py:54-58). Needs a recipient per message.
13. Services.last_test_alert (server.py:70). Needs a cooldown per tenant.
14. check_now clears back-off on all trips (watcher.py:141-145). Needs to be scoped to the tenant and rate-limited.
15. Hub sends one payload to all clients (server.py:142-161). Needs a payload per tenant.
16. healthz and state.watcher rely on one heartbeat (server.py:426-433; views.py:142). Needs operator liveness separated from per-trip pod health.
17. Login coordination is process-global (_login_lock and _login_blocked_until at watcher.py:128-130, 300, 334-349), and so is the check concurrency semaphore of 3 (watcher.py:113, 127). Pods per trip lose both; they need back-off per tenant and a cluster-wide concurrency budget coordinated by the operator.
18. AlertMemory alerted.json is keyed only by trip id (server.py:518; state.py:159-167). Needs a key of (tenant, trip), held by the operator so restarting a pod doesn't re-alert.
19. BCF_USER/BCF_PASS and PUSHOVER_* come from process env (settings.py:183-188, 211-214), and startup logs whether they're present (__main__.py:242-250). BCF credentials must move to the browser pods only; the Pushover app token stays in the operator; user keys become tenant records.
20. cart_hold_minutes, headless, window_size and debug_dir are global settings (settings.py:168-173). They become pod-level or admin settings.
21. The frontend assumes one live browser: the WatcherCard 'Live browser' link (WatcherCard.tsx:222-225) and state.checkout_url passed to every CartBanner (App.tsx:72).
refs: src/ferry_watch/server.py:60-95, src/ferry_watch/server.py:337-344, src/ferry_watch/server.py:497-530, src/ferry_watch/store.py:220-222, src/ferry_watch/store.py:265-287, src/ferry_watch/state.py:27, src/ferry_watch/state.py:96-125, src/ferry_watch/views.py:98-143

## Identity from the proxy: nothing reads it today; how to trust it
- No handler or middleware reads any identity header; the only header logic is Origin and Sec-Fetch-Site (server.py:193-198).
- Authelia forward-auth passes the user to upstreams as Remote-User, Remote-Groups, Remote-Name and Remote-Email. oauth2-proxy uses X-Forwarded-User / X-Forwarded-Email. Header names should be configurable (for example BCF_USER_HEADER, BCF_GROUPS_HEADER, BCF_ADMIN_GROUP).
- Proposed identity_middleware, placed after error_middleware in the chain at server.py:449-451:
  - Require the user header on every /api/* route, including the WS upgrade. Return 401 when it's missing. /healthz is exempt for probes and must return nothing tenant-specific.
  - Normalise the user into a stable tenant id. Remote-User may contain characters that aren't legal in k8s names, so use a hash or slug for labels and keep the raw value in an annotation.
  - Derive is_admin from the groups header.
- The header is only trustworthy if every path to the listener goes through forward-auth, and the auth response overwrites any header the client sent. ingress-nginx auth-response-headers and Traefik forwardAuth authResponseHeaders both set or overwrite. The real danger is a path that bypasses auth: an Authelia `bypass` rule, a second Ingress or Service, or a pod in the cluster calling the Service directly.
- Defense in depth beyond NetworkPolicy, any of:
  - (a) check that the TCP peer (`request.transport.get_extra_info('peername')`) is in an allowlist of ingress-controller CIDRs; aiohttp doesn't parse X-Forwarded-For, so request.remote is the ingress pod
  - (b) require a shared-secret header that the ingress injects and clients can't know
  - (c) where the proxy supports it, verify a signed token instead of plain headers (e.g. an OIDC ID token from oauth2-proxy)
- WebSocket: resolve identity once at upgrade and store it on _Client. Close sockets after a maximum lifetime (e.g. 1 h) so they reconnect through Authelia and pick up revocation.
refs: src/ferry_watch/server.py:3-6, src/ferry_watch/server.py:193-198, src/ferry_watch/server.py:436-439, src/ferry_watch/server.py:449-451, src/ferry_watch/server.py:163-178, docs/api.md:3-4

## Browser pods are a hostile vantage point for the API: headed Chromium has DevTools and file:// and KasmVNC has no auth
- Chromium is launched with only window-size and position flags (bcferries/site.py:153-156). There is no managed policy blocking DevTools, file:// or chrome:// URLs.
- A tenant on VNC therefore controls a full browser inside the pod's network namespace and can:
  - (a) open DevTools on any reachable internal URL, e.g. the operator Service, and send same-origin fetches with arbitrary headers such as Remote-User: <victim> or Remote-Groups: admins; CORS and the _same_origin check don't help because the request is same-origin to that URL
  - (b) read files the browser process can see, e.g. file:///proc/self/environ or mounted Secret and service-account token files
- API consequences:
  - Browser pods must never reach the operator's user API port. Use NetworkPolicy plus the peer/secret checks above.
  - The pod→operator report/command API needs its own port and a per-pod credential that is bound to exactly one trip, and every report must be checked against that binding. Assume the tenant can read the credential and forge reports for their OWN trip, so validate schema and sizes and never let a report name another trip or tenant.
  - Nothing shared may be placed in browser pods: no Pushover app token, no operator credentials. That rules out pods calling Pushover directly.
  - Browser pods also need to be blocked from reaching each other's :6080.
- KasmVNC runs with -DisableBasicAuth -SecurityTypes None -AlwaysShared (entrypoint.sh:20-26) and serves the viewer under /vnc (Dockerfile:36-38). Access control for each pod's VNC therefore depends entirely on the routing and auth in front of it.
refs: src/ferry_watch/bcferries/site.py:149-160, entrypoint.sh:4-6, entrypoint.sh:20-26, Dockerfile:36-38, Dockerfile:66

## Per-trip VNC URLs: where they come from and why the operator must not proxy VNC
- Today one URL (BCF_VNC_URL) points at the single Xvnc/KasmVNC on :6080 (settings.py:206; entrypoint.sh:20-26). With a pod per trip, each trip needs its own URL. Two shapes:
  - a host per trip, e.g. https://<trip>.vnc.<domain>/vnc/vnc.html
  - a path per trip on a dedicated VNC host
- The operator can render the URL from a template such as BCF_VNC_URL_TEMPLATE='https://{trip}.vnc.example/vnc/vnc.html' and expose it as trip.vnc_url in trip_view (views.py:63-84). The cart alert then uses it instead of the global value (watcher.py:559-560; notify.py:243).
- Routing must go from the ingress straight to the pod's :6080. If VNC traffic passed through the operator, the operator would see every frame and keystroke: the tenant's logged-in BC Ferries pages and any payment details typed at checkout. That defeats the goal of 'no way to get secrets out from the operator'.
- Authorization for each VNC route can use forward-auth to an operator endpoint that only answers allow/deny (e.g. GET /internal/authz/vnc checking Remote-User owns the trip in X-Forwarded-Host). Even then, whoever controls that decision, and whoever can create Ingresses, can grant themselves access. The design should say so explicitly.
- Alert links must not carry tokens. Pushover is a third party and links stay in notification history, so the link should rely on the Authelia session alone.
- -AlwaysShared (entrypoint.sh:24) means an admin who can open a tenant's VNC sees that tenant's live session.
- The link must stay valid as long as the cart is held: cart_hold_minutes defaults to 60 (settings.py:173), and the pod must not be torn down while a cart is READY.
refs: src/ferry_watch/settings.py:173, src/ferry_watch/settings.py:206, src/ferry_watch/views.py:63-84, src/ferry_watch/notify.py:229-246, src/ferry_watch/watcher.py:555-564, entrypoint.sh:20-26

## How the CSP must change for per-trip VNC hosts or paths
- Opening VNC in a new tab via <a target=_blank> (CartBanner.tsx:136-139; WatcherCard.tsx:222-225) needs NO change: CSP doesn't restrict top-level navigation, and no-referrer plus noopener/noreferrer (server.py:261; button.tsx:49) already keep dashboard URLs out of the VNC host's logs.
- Embedding VNC in an iframe needs:
  - On a different host: `frame-src https://*.vnc.<domain>` added to content_security_policy (server.py:238-250). Today frame-src falls back to default-src 'self' and blocks it.
  - On the VNC origin's responses: frame-ancestors (or X-Frame-Options) allowing the dashboard origin. KasmVNC's built-in httpd (entrypoint.sh:22) probably doesn't set these, so the ingress should add them; verify.
  - An iframe allow= for clipboard-read, clipboard-write and fullscreen.
- Changes to connect-src and img-src are only needed if dashboard JS itself talks to the VNC host, e.g. to fetch thumbnails or probe pod liveness. That should be avoided: if the operator-served dashboard JS opened the VNC WebSocket itself (embedding noVNC as a component), the operator could ship code that reads the session.
- Serving VNC from the dashboard's own origin under a path (/vnc/<trip>/) puts pod-served JS in the API's origin. That JS could pass the _same_origin check (server.py:193-198) and call /api with the user's session. Separate origins keep the existing Origin check effective.
- The dashboard's frame-ancestors 'none' can stay.
- test_frontend_is_served_with_a_strict_csp (test_server.py:313-335) will need updating if frame-src is added.
refs: src/ferry_watch/server.py:232-267, src/ferry_watch/server.py:193-198, web/src/components/CartBanner.tsx:136-139, web/src/components/WatcherCard.tsx:222-225, web/src/components/ui/button.tsx:43-52, entrypoint.sh:22, tests/test_server.py:313-335

## Quotas: today only MAX_TRIPS=6, checked atomically because the store is synchronous
- The only limit is store.add_trip refusing a 7th trip with ValidationError('trips') → 400 (store.py:266-267; models.py:16). It is surfaced as state.max_trips (views.py:135; docs/api.md:128).
- The check plus insert is atomic only because ConfigStore is synchronous on one event loop (state.py:1-6; store.py:247). Once persistence means awaiting the k8s API (creating a CR or Pod), two concurrent POSTs can both pass the count.
- Fixes for that race: a per-tenant asyncio.Lock with a single operator replica, or native enforcement. A ResourceQuota `count/<plural>.<group>` per tenant namespace caps CRs per tenant. A ResourceQuota on pods in a shared browsers namespace caps cluster-wide browsers.
- Two limits are needed:
  - configured trips per tenant, which are cheap
  - running browser pods, cluster-wide and optionally per tenant
- Pods should only exist for trips that are active: enabled, date >= today, global enabled (watcher.py:229). Otherwise past or disabled trips hold browsers.
- patch_trip (359-373) and PATCH /api/settings (337-344) can activate trips without any quota check today. They need the browser-cap check too.
- Behaviour at the cluster cap is an API decision: reject with 503 or 429, or accept and show trip status 'queued for a browser'.
- Rate limits are also needed for POST /api/check, which bypasses back-off (watcher.py:141-145), and for POST/DELETE churn that would create and destroy pods.
refs: src/ferry_watch/store.py:247, src/ferry_watch/store.py:265-271, src/ferry_watch/models.py:16, src/ferry_watch/models.py:19-20, src/ferry_watch/views.py:135, src/ferry_watch/state.py:1-6, src/ferry_watch/server.py:337-373, src/ferry_watch/watcher.py:141-145

## Admin vs user roles: none exist; candidates for an admin surface
- Every caller is effectively the owner and admin.
- Candidate admin-only operations:
  - global pause, which is what PATCH /api/settings `enabled` is today (337-344)
  - floors and caps: minimum interval, trips per tenant, cluster browser cap, cart_hold_minutes (settings.py:173)
  - a cluster view: browser pods, their phase and health, capacity used, operator-level events such as start-up and terminal-list failures (server.py:530, 548-559)
  - force-deleting a tenant's trip to free capacity
- Everything else is user-scoped:
  - trips, carts, check-now and test alert on their own trips and key
  - their own Pushover user key (a new endpoint, e.g. PUT /api/me/pushover; Pushover offers a users/validate endpoint for checking a key on save)
- BC Ferries credentials should NOT have an operator API at all. Anything entered through it passes through the operator. Credentials would instead be entered into the browser pod, provisioned straight into a tenant Secret, or skipped in favour of a manual login over VNC. The state only reports login_configured or logged_in as booleans, as session.logged_in does today (views.py:77-81).
- If admins can see other tenants' trips (dates, party size, vehicle) or open their VNC, that is a privacy decision for the owner.
refs: src/ferry_watch/server.py:337-344, src/ferry_watch/server.py:530, src/ferry_watch/server.py:548-559, src/ferry_watch/settings.py:173, src/ferry_watch/views.py:77-81, src/ferry_watch/views.py:140

## Pods per trip break process-wide coordination that the API currently presents as one state
- The watcher serialises logins with one lock. After a LoginFailed it blocks auto-cart logins for ALL trips for 30 min and sends one login_failed alert (watcher.py:54, 128-130, 334-349).
- It also caps concurrent checks at 3 (watcher.py:113, 127).
- With one pod per trip, each pod would log in with the same tenant credentials independently. A wrong password then costs N failed attempts, which risks locking the account, and produces N duplicate login_failed alerts. Whether BC Ferries allows concurrent sessions on one account (and whether a new login drops another pod's cart) is unknown.
- The operator would need to:
  - keep back-off and login state per tenant, with pods reporting LoginFailed and asking before logging in
  - dedupe alerts per tenant
  - provide the cluster-wide concurrency budget for checks
- The API state would show this per tenant (e.g. a login_blocked_until for each tenant).
- Delete semantics also change. DELETE /api/trips/{id} relies on the in-process watcher emptying the cart on its next cycle (watcher.py:186-193). In k8s that becomes a finalizer, or a command to the pod before it terminates, so BC Ferries gets the space back.
refs: src/ferry_watch/watcher.py:54, src/ferry_watch/watcher.py:113, src/ferry_watch/watcher.py:127-130, src/ferry_watch/watcher.py:186-193, src/ferry_watch/watcher.py:334-349, src/ferry_watch/notify.py:270-274, docs/api.md:130

## Frontend/API contract changes implied
- The State type (docs/api.md:95-110) and test_state_shape (test_server.py:94-144) pin these top-level keys: checkout_url, login_configured, alerts_configured, watcher, max_trips. For multi-tenancy:
  - checkout_url moves to Trip.vnc_url, which the zod schema must also check as an http(s) URL (schema.ts:29, 88, 111)
  - watcher becomes per-trip pod health (phase: pending, queued, running, failed) plus operator health
  - max_trips becomes the tenant's limit
  - add a `me` block {user, is_admin, pushover_configured}
- The WatcherCard 'Live browser' button (WatcherCard.tsx:222-225) has no single target any more.
- CartBanner gets its URL from the trip rather than from App state (App.tsx:72).
- docs/api.md's error table (13-24) needs:
  - 401 when there is no identity
  - 403 for admin-only routes, distinct from today's cross-origin 403
  - 429 for check-now and quota
  - a 503 or 202 'queued' answer at browser capacity
- seq stays monotonic per tenant, which is compatible with state.ts:26.
refs: docs/api.md:13-24, docs/api.md:95-110, tests/test_server.py:94-144, web/src/lib/schema.ts:29, web/src/lib/schema.ts:88, web/src/lib/schema.ts:111, web/src/lib/state.ts:26, web/src/components/WatcherCard.tsx:222-225

IMPLICATIONS:
- Add an identity middleware that runs before every /api route and the WS upgrade. It reads configurable headers (Remote-User / Remote-Groups by default) and returns 401 when they are missing. It trusts them only from the ingress path, enforced by a peer-CIDR allowlist or an ingress-injected shared-secret header, with NetworkPolicy on top. It stores a Principal {tenant_id, display, is_admin} on the request. /healthz stays unauthenticated and returns nothing tenant-specific.
- Replace the Services singleton (server.py:60-95) with a repository scoped by tenant. Every lookup takes (tenant, trip_id), and a foreign trip returns the same 404 as a missing one (server.py:211-212). Filter at the data layer, not in views.py, so a new snapshot field can't leak across tenants.
- Rewrite the Hub (server.py:134-183):
- each _Client holds its tenant, captured at upgrade
- notify(tenant) marks only that tenant dirty
- after the 100 ms coalesce, build one payload per dirty tenant and send it to that tenant's clients
- cap sockets per tenant and close sockets after a maximum lifetime so revoked sessions fall off
Keep the newest-only sender, the 15 s pings and the 10 s send timeout as they are.
- Run three listeners or route groups:
- the public user API and dashboard behind Authelia
- admin routes gated on group membership
- a separate internal port for browser-pod reports and commands (status, events, alert intents, cart release/dismiss, check-now), never exposed via Ingress
The internal port uses per-pod credentials bound to one trip, and every report is checked against that trip and its schema. Browser pods must not be able to reach the public port.
- Move alert delivery into the operator. Pods send structured alert intents (kind, trip_id, sailings, fare, error text already passed through describe() in the pod). The operator writes the text with the existing notify.py functions and sends with its own Pushover app token plus the tenant's user key. PushoverNotifier.send takes the recipient per message (notify.py:54-74); the redaction covers both the token and that key; Message (notify.py:31-37) gets tenant_id/trip_id; alert-failure events (server.py:503-504) go to the owning tenant.
- Replace BCF_VNC_URL (settings.py:206) with a URL template that has a {trip} placeholder. Add trip.vnc_url to trip_view (views.py:63-84) and pass it to cart_message (watcher.py:559-560). Remove or deprecate the top-level checkout_url (views.py:139) and the global 'Live browser' button. Route VNC from the ingress straight to each pod's :6080, never through the operator, with an allow/deny forward-auth check that the caller owns the trip.
- CSP: no change while VNC opens in a new tab. If it is iframed, add `frame-src https://*.vnc.<domain>` (server.py:238-250), set frame-ancestors on the VNC origin at the ingress, and give the iframe allow="clipboard-read; clipboard-write; fullscreen". Keep VNC on a different origin from the dashboard so the existing Origin check (server.py:193-198) keeps protecting /api.
- Quotas:
- per-tenant configured trips: a ResourceQuota count/<crd> per tenant namespace, or a per-tenant lock around count-then-create once persistence is async
- cluster browser cap: a ResourceQuota on pods in the browser namespace, or operator-side accounting
Check the cap on POST /api/trips and on any PATCH that activates a trip (patch_trip, PATCH settings enabled). Create pods only for active trips (watcher.py:229). Decide between rejecting and queueing at the cap.
- Make PATCH /api/settings `enabled`/`interval_seconds` per tenant, with an admin floor on the interval. Add an admin-only global pause. Scope POST /api/check to the caller's trips and rate-limit it, since it bypasses back-off (watcher.py:141-145).
- Make the test-alert cooldown a dict keyed by tenant instead of Services.last_test_alert (server.py:70, 415-417). Return 409 when that tenant has no Pushover key. Write the 'Test alert sent' event to that tenant.
- Split health: the operator's /healthz reflects only its reconcile loop and hub. Each browser pod keeps the existing heartbeat-based /healthz (server.py:426-433; views.py:17) as its own liveness probe. Per-trip pod phase and health go into each trip's state rather than a global `watcher` block (views.py:142).
- Give each tenant its own event buffer and seq (state.py:96-125). Assign every event without a trip (server.py:343, 422, 504, 530; watcher.py:348) to a tenant or make it admin-only. Record the acting user in mutation events for an audit trail; the access log is currently disabled at __main__.py:251.
- DELETE a trip → finalizer/pre-stop command so the pod empties the cart before termination (today done by watcher.cycle at watcher.py:186-193); the operator must not tear down a pod holding a READY cart except by explicit release/expiry.
- Move login back-off and coordination to the operator, per tenant: pods report LoginFailed and ask before logging in. Dedupe login_failed alerts per tenant. Provide a cluster-wide budget for concurrent checks to replace the in-process Semaphore(3) (watcher.py:113, 127).
- Do not add any operator API that accepts BC Ferries credentials. login_configured and logged_in reach the operator only as booleans reported by the pod (views.py:77-81, 140).
- Tests to add alongside test_server.py:
- two Remote-User identities can't see each other's trips or events through GET /api/state, WS frames or mutation responses
- a foreign trip id gives 404 on PATCH, DELETE, release and dismiss
- requests without the identity header give 401
- spoofed identity headers from an untrusted peer are rejected
- per-tenant test-alert cooldown
- check-now affects only the caller's trips
- quota errors
- vnc_url appears per trip and in cart_message
- the CSP includes frame-src only when configured
- Update docs/api.md: add 401, admin-only 403 and 429/503 or queued to the error table, add trip.vnc_url and per-trip pod status, add a `me` block, and document the internal pod API separately. Update the zod schema to validate vnc_url as an http(s) URL (schema.ts:29).

RISKS:
- Identity headers spoofed via a path that skips forward-auth (an Authelia bypass rule, a second Ingress or Service, a pod calling the Service directly). The app trusts whatever header arrives, so one such path means full impersonation, including admin through Remote-Groups.
- A tenant with headed Chromium (site.py:153-156 sets no policies) can use DevTools to send arbitrary headers to any internal URL the pod can reach. They can also read mounted files and environment variables via file://, such as SA tokens, Secret files and /proc/self/environ. If browser pods can reach the operator's public port, or hold any shared secret like the Pushover app token, isolation is broken.
- Proxying VNC through the operator, or serving the VNC client JS from the operator's origin, would expose tenants' logged-in sessions and checkout keystrokes to the operator. That contradicts the stated goal.
- The real limit on 'the operator can't get secrets out' sits outside the API. Whoever can create Pods or Ingresses, or decide VNC authorization, can mount a tenant Secret into a pod of their own or grant themselves VNC access. API design alone can't meet the requirement; RBAC and admission policy have to.
- Long-lived WebSockets outlive Authelia sessions because pings keep them alive (server.py:117-119, 164). Without a maximum lifetime, a revoked user keeps receiving updates.
- With a pod per trip, each pod logs in with the same BC Ferries account independently. A wrong password means N failed logins (risk of lockout), and a new login may invalidate other pods' sessions or carts. The behaviour is unknown and not handled today, because coordination lives in one process (watcher.py:128-130, 334-349).
- Load on BC Ferries grows with tenants times trips: 15 s minimum interval (models.py:19), each pod polling on its own, no cluster-wide concurrency limit. All pods probably leave through the same node egress IP, so one tenant's aggressive settings or check-now abuse could get the shared IP throttled or queued by Queue-it for everyone.
- One Pushover application token shared across tenants: monthly message limits and rate limiting are per app, so one tenant with a noisy trip or an invalid key (6 attempts per alert, notify.py:137) consumes the shared budget.
- A race on the quota check once trip creation awaits the k8s API (today's atomicity relies on the synchronous store, store.py:265-271). Concurrent POSTs could exceed per-tenant or cluster caps.
- In-memory hub and RuntimeState assume a single replica (state.py:1-6). Running more than one operator replica without leader election or shared pub/sub would split WS clients across inconsistent state.
- Global event ids and seq (state.py:99, 104) leak other tenants' activity rate if kept shared. Minor, but easy to avoid.
- Alerts still being retried are cancelled when the process shuts down (notify.py:171-174). If delivery moves to the operator, an operator rollout during an open cart can drop the 'in your cart' alert unless intents are persisted.

OPEN_QUESTIONS:
- Should there be one browser pod per trip, as asked, or one per tenant with a context per trip? A pod per tenant still gives isolation between tenants, keeps the shared login lock and back-off in one process, and costs one Xvnc+Chromium per tenant instead of per trip. The owner said 'per route'; the code's unit is a Trip (route + date + party + vehicle).
- How do tenants' BC Ferries credentials get into their pods without passing through the operator? Options: entered in a form served by the browser pod itself, provisioned straight into a tenant Secret (e.g. External Secrets / 1Password), or no stored credentials at all, with a manual login over VNC and cookies kept on a per-trip volume.
- VNC addressing: a subdomain per trip (needs wildcard DNS/TLS; cleanest origin isolation and simplest Authelia rules) or a path per trip on a dedicated VNC host? Should the dashboard embed VNC in an iframe, which needs CSP frame-src, or only link to it?
- Who decides VNC access? An allow/deny forward-auth endpoint on the operator (simple, but the operator becomes the gatekeeper) or something independent of it (e.g. Authelia rules generated per tenant)? Should admins be able to open a tenant's VNC, given -AlwaysShared lets them watch the live session?
- What limits: maximum configured trips per tenant, maximum running browsers per tenant, cluster-wide browser cap, and the minimum allowed check interval? At the cap, should a new or re-enabled trip be rejected (503/429) or accepted and queued with a visible 'waiting for a browser' status?
- Roles: which Authelia group means admin? Should admins see other tenants' trip details (dates, party, vehicle) or only aggregate capacity and health?
- Pushover: one shared application token in the operator with per-tenant user keys (recommended), or each tenant brings their own app token? Is a Pushover key required before a tenant can add trips?
- Is namespace-per-tenant acceptable? It allows native ResourceQuota count/<crd> enforcement and per-namespace RBAC and NetworkPolicy. The alternative is one shared namespace with label-based scoping enforced only by the operator.
- Does the operator need multiple replicas (HA)? If so, the in-memory hub and event buffers need leader election or a shared pub/sub.
