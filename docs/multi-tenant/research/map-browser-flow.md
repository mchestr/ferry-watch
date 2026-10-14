===== browser-flow
The whole bcferries.com flow lives in src/ferry_watch/bcferries/site.py. `SiteBrowser` owns one Chromium. `SiteSession` wraps one BrowserContext and Page per trip and exposes only domain-typed methods: login, search, refresh, add_to_cart, empty_cart, show, save_debug_snapshot, close, plus the closed/lost flags. Page objects never leave site.py. The watcher depends only on the `Session` protocol (watcher.py:57-78), so a clean seam for a per-trip agent pod already exists. parse.py, errors.py, Terminals, search_url/choose_fare and all of SiteSession can be reused unchanged in a pod, and the browser contract tests already build a SiteSession on its own. Things that don't survive the split:
- The password is held by the server process and passed into login() (watcher.py:121-122, 338-342).
- Error masking relies on the server knowing the secret (watcher.py:122, 133-134).
- Login serialisation and the 30-minute login backoff are process-wide (watcher.py:128-130, 333-349). With per-trip pods, N pods for one account would each log in and each retry a bad password.
- closed/lost are synchronous flags fed by in-process Playwright events (site.py:204-205, 158-165).
- Screenshots are returned as a local Path (watcher.py:411-413).
- show()/_raise_held_windows exist only because many windows share one X display.
- Terminals come from a shared in-process TerminalDirectory (server.py:494, 507-509).
What touches secrets: the login fill of BCF_USER/BCF_PASS; the logged-in context's cookies, which are a bearer credential for the account, including saved payment cards at checkout; and the VNC framebuffer and input channel, which gives full control of that logged-in browser. So the flow code and the per-trip state machine have to run inside the pod. The operator must never get a Playwright/CDP handle, exec, or port-forward into the pod, and the VNC path should not go through the operator. Two behaviours need live verification before committing to the design. First, whether BC Ferries ties a logged-in cart to the account rather than the session; if it does, one pod's empty_cart could clear another pod's cart for the same account. Second, whether closing the only headed window kills Chromium; if it does, a user's 'I'm done' would be read as a crash, re-carting the sailing and sending a 'Lost the cart' alert.

## URLs visited and how navigation is verified
The site is reached in these places:
- Search: search_url builds `https://www.bcferries.com/?sailing=true&departureLocation=..&departureLocationName=..&arrivalLocation=..&arrivalLocationName=..&departingDateTime=MM/DD/YYYY`, so route and date are filled in server-side.
- Login page: `/login`.
- Results reload: `/fare-selection`.
- After choosing a fare: `/fare-selection-review`.
- Emptying the cart: `/abandoned-cart/my-account`.
- Terminal list: `/route-info` JSON, fetched over aiohttp, not the browser.
The form posts the flow causes (from the contract-test fake) are `/PassengerInfo`, `/next-step`, `/search` and `/j_spring_security_check`.
Every navigation goes through `_goto` (goto, wait for load, then `_wait_out_queue`) or `_click_and_wait`, which waits for an expected path regex and then runs `_wait_out_queue`. A timeout becomes the caller-supplied BookingError subclass.
The UA is spoofed to a fixed desktop Chrome 141 string. The context is created with `no_viewport=True` so the page follows the window size (`--window-size`, `--window-position=0,0`). A locator handler auto-clicks the cookie banner `#cAccept`.
refs: src/ferry_watch/bcferries/site.py:42-53, src/ferry_watch/bcferries/site.py:72-87, src/ferry_watch/bcferries/site.py:233-266, src/ferry_watch/bcferries/site.py:154-157, src/ferry_watch/bcferries/site.py:169, src/ferry_watch/bcferries/terminals.py:23, tests/browser/test_site_flow.py:130-144

## Booking form walk (search)
`search(trip)` runs these steps:
1. `_goto(search_url)`, then wait for `#y_confirmaddpassenger`.
2. Click the one-way label `label[for='y_oneWayRadbtn']`.
3. Check that `routeInfoForm.departingDateTime` equals the trip date; if not, raise RouteRejected.
4. Submit and expect `/PassengerSelectionPage`.
5. For each non-zero passenger type (`y_adult`/`y_child`/`y_infant`/`y_senior`), click `.btn-plus` N times and check the input value. Too many or too few inputs raise RouteRejected; a wrong count raises MarkupChanged.
6. Pick the walk-on radio (`proceed_to_sailing_selection_btn`) or the vehicle radio (`proceed_to_vehicle_selection_btn`), then press Continue.
7. Walk-on goes straight to `/fare-selection`. With a vehicle it goes to `/VehicleSelectionPage`: tall uses `over7Height_0` plus height `#Over7heightboxinft_0` and a Tab so the site converts units; otherwise `under7Height_0`. Always `under20Length_0`, then Continue to `/fare-selection`.
8. `_read_results()`.
The only external input besides the Trip is the terminal list, read via `await self._terminals.get()` at site.py:306 to build search_url.
refs: src/ferry_watch/bcferries/site.py:303-354, src/ferry_watch/bcferries/site.py:55-57, tests/browser/test_site_flow.py:200-232

## Queue-it handling, and a gap in login
`_wait_out_queue` does nothing unless the page hostname contains `queue-it.net`. If it does, it waits up to 10 minutes (QUEUE_TIMEOUT_MS) for a URL without that host, else raises QueueTimeout. The watcher wraps each whole check in a 15-minute timeout (CHECK_TIMEOUT_SECONDS) and runs at most 3 checks at once (`asyncio.Semaphore(concurrency=3)`), so a trip stuck in the queue doesn't block the others.
Queue position lives in each context's cookies, so every context (and every future pod) queues on its own.
Gap: after clicking 'Log in', `login()` waits only for `'/login' not in path` and never calls `_wait_out_queue`. A login that lands in the waiting room would satisfy that check, and the session would be marked `logged_in=True` without actually being logged in.
refs: src/ferry_watch/bcferries/site.py:240-249, src/ferry_watch/bcferries/site.py:292-301, src/ferry_watch/watcher.py:48, src/ferry_watch/watcher.py:113, src/ferry_watch/watcher.py:127, src/ferry_watch/watcher.py:249-261, tests/browser/test_site_flow.py:235-237

## Fare-selection reload loop and session-reset policy
The booking form is walked once. After that, `refresh()` just reloads `/fare-selection` and BC Ferries re-runs the session's stored search. `_read_results` raises SessionExpired if the URL is no longer `/fare-selection`, and MarkupChanged if no `.p-card` appears within 20s or the parse is empty. Parsing is pure selectolax over `page.content()` (parse.py).
How the watcher reacts:
- SessionExpired: re-walk the form in the same context, keeping cookies and login.
- Any other failure: set `needs_search=True`.
- After 3 consecutive failures (RESET_SESSION_AFTER), or if the page is closed: close the context, which also drops the login.
- Backoff between retries: 0/60/120/300/600s.
- A change to the trip's search_key (route, date, party, vehicle, height) closes the old session and opens a new one.
- Interval: `config.interval_seconds` (default 30, min 15) with ±15% jitter.
refs: src/ferry_watch/bcferries/site.py:356-371, src/ferry_watch/bcferries/parse.py:41-70, src/ferry_watch/watcher.py:302-331, src/ferry_watch/watcher.py:401-422, src/ferry_watch/watcher.py:51-52, src/ferry_watch/watcher.py:265-266, src/ferry_watch/models.py:177-179, src/ferry_watch/models.py:19

## Login flow: where BCF_USER/BCF_PASS are typed and how leaks are prevented
Credentials come from the BCF_USER/BCF_PASS env vars in Settings.from_env, into a frozen `Credentials` dataclass whose password field is hidden from repr (the username is not). They are held in the server process, and the Watcher passes them into `session.login(credentials)`.
`login()` does the following:
1. `_goto('/login')`.
2. Scope to the form that has `#j_password`.
3. `fill()` `#email` (posted as `j_username`) and `#j_password`.
4. Read both values back. Empty values mean the site cleared the form 'while a guest cart exists', which raises LoginFailed.
5. Click 'Log in' and wait to leave `/login`.
6. On failure, scrape a visible alert for the reason.
Leak protection: Playwright call logs include the value passed to fill(), so every PlaywrightError inside login() becomes a LoginFailed carrying a message the code wrote, raised `from None`. A test asserts the password is absent and `__suppress_context__` is set.
Second layer: errors.describe() keeps only the first line of any error, masks known secrets and truncates to 300 characters. The watcher passes (username, password) as the secrets, so the email address is treated as a secret too.
Limitation: `logged_in` is set once and never re-checked. If BC Ferries' login session times out while the search cookies survive, later carts are made as a guest.
refs: src/ferry_watch/settings.py:80-82, src/ferry_watch/settings.py:108, src/ferry_watch/bcferries/site.py:66-69, src/ferry_watch/bcferries/site.py:270-301, src/ferry_watch/bcferries/errors.py:40-50, src/ferry_watch/watcher.py:121-122, src/ferry_watch/watcher.py:133-134, tests/browser/test_site_flow.py:266-277

## Login ordering and cross-trip login coordination are process-wide
Ordering: BC Ferries won't accept a login while a guest cart exists, so the watcher logs in before the first search of a new session, not lazily at cart time. Logging in navigates away from the results, so the search runs again afterwards. Login is only attempted for trips with auto_cart on when credentials exist and no backoff is active.
Coordination:
- `_login_lock` makes logins happen one at a time across all trips.
- `_login_blocked_until` is one process-wide 30-minute pause, set on LoginFailed, so a bad password produces exactly one failed login no matter how many trips are starting. It's re-checked inside the lock.
- Tests pin both: three sessions log in with `most_logging_in == 1`, and with a failing login only one attempt is made while every trip is still searched.
Note: today's code already runs N logged-in contexts of the same account at once. It only serialises the login submission.
refs: src/ferry_watch/watcher.py:128-130, src/ferry_watch/watcher.py:299-300, src/ferry_watch/watcher.py:315-318, src/ferry_watch/watcher.py:333-349, src/ferry_watch/watcher.py:54, README.md:122-125, tests/test_watcher.py:83-87, tests/test_watcher.py:473-486

## Adding to cart and reading the cart back
`add_to_cart(depart, preference)` works on whatever results page is currently loaded, so it must run straight after refresh/search on the same page:
1. Re-parse `page.content()` and find the card index for an available sailing at `depart`; if none, raise NotBookable.
2. `choose_fare`: the preferred fare if it is selectable, else the cheapest selectable one.
3. Check the card's displayed `.ferrytime` matches; if not, raise MarkupChanged.
4. Click 'View fares', then the visible `label.js-radio-select` in the `.vp-box` whose `.fare-type-name` matches, and expect `/fare-selection-review`; on timeout, raise NotBookable.
5. Read the cart back with a substring check of the body text for the formatted time and fare name; if either is missing, raise CartMismatch.
The watcher gives this a 40s budget. On any failure it takes a snapshot and closes the whole context, which is what drops a half-made cart. On success it sets `Cart(READY, expires_at=now+hold)` and pauses the trip until release, window close or expiry.
The cart is never read again after this one check. Expiry is the app's own timer. The site's own cart timer (`cartTimerContinueUrl`) and the review page's 'Order ID' are not used.
refs: src/ferry_watch/bcferries/site.py:90-99, src/ferry_watch/bcferries/site.py:373-398, src/ferry_watch/watcher.py:50, src/ferry_watch/watcher.py:475-508, tests/browser/test_site_flow.py:248-263, tests/fixtures/flow/review.html, tests/fixtures/flow/login.html:45-46

## Releasing / emptying a cart
`empty_cart()` is just `_goto('/abandoned-cart/my-account')`. It does not check that the cart is actually empty, and the test only asserts it doesn't raise.
The watcher calls it when:
- a trip is deleted while holding a cart;
- a trip is edited while its fare is being selected;
- the user presses release, or the hold time expires with the window still open.
The dashboard's release endpoint only sets `release_requested`; the watcher acts on it on its next 5s cart poll (CART_POLL_SECONDS).
`_empty_cart` swallows only BookingError. A raw PlaywrightError, for example 'target closed', would propagate up to cycle().
On shutdown, `_close_all` closes contexts without emptying carts, so held spots are left to expire on the site. A held cart also survives pausing: the holding check runs before the 'is this trip active?' check.
refs: src/ferry_watch/bcferries/site.py:400-401, src/ferry_watch/watcher.py:147-153, src/ferry_watch/watcher.py:188-196, src/ferry_watch/watcher.py:199-204, src/ferry_watch/watcher.py:368-382, src/ferry_watch/watcher.py:461-468, src/ferry_watch/watcher.py:510-542, tests/browser/test_site_flow.py:280-281

## How 'user closed the checkout window' is told apart from a crash
The signals are event-driven and in-process:
- `page.on('close')` sets `_closed`.
- `page.on('crash')` calls `mark_lost()`, setting `_lost` and `_closed`.
- `browser.on('disconnected')` calls `SiteBrowser._browser_died()`, which marks every session in a WeakSet as lost.
- `closed` also checks `page.is_closed()`.
While a cart is held, `_service_cart` polls these flags every 5s:
- Closed but not lost means 'Checkout window closed, watching resumed'. The departure stays in `alerted`, so it is not re-alerted.
- Lost means 'Lost the cart', a high-priority alert, and the departure is removed from `alerted` so it is re-carted in a new session.
This replaced an earlier version that polled `page.title()` and wrongly treated a navigating page as closed (commit 6016172).
Coverage gaps: the contract test closes the page through Playwright's API, headless, on a SiteSession built without a SiteBrowser. It does not cover a headed user closing the last window in the browser, nor the disconnected handler.
refs: src/ferry_watch/bcferries/site.py:158, src/ferry_watch/bcferries/site.py:162-165, src/ferry_watch/bcferries/site.py:202-222, src/ferry_watch/watcher.py:510-535, tests/browser/test_site_flow.py:284-289, tests/test_watcher.py:215-237

## Failure screenshots
`save_debug_snapshot(reason)` saves a viewport-only PNG (deliberately not full-page, to avoid capturing the whole account page) to `debug_dir`, named `{UTC ts}-{trip id}-{slug}.png`. Only the newest 20 are kept, and errors are swallowed. `debug_dir` defaults to `$TMPDIR/ferry-watch`, outside the backed-up `/config`.
It is called for MarkupChanged or non-BookingError check failures, and for every add_to_cart failure. LoginFailed does not trigger a screenshot.
The screenshots are logged-in pages (results or review page, account header). Password fields render masked, but the images are tenant data. The method returns a local Path that the watcher only logs, which assumes a filesystem shared with the server.
refs: src/ferry_watch/bcferries/site.py:53, src/ferry_watch/bcferries/site.py:407-422, src/ferry_watch/settings.py:67, src/ferry_watch/settings.py:96, src/ferry_watch/watcher.py:408-413, src/ferry_watch/watcher.py:487, README.md:86-87

## Terminal list dependency
SiteSession reaches `TerminalProvider.get()` exactly once, in search() at site.py:306, to resolve four strings: origin code and label, destination code and label. `TerminalDirectory` fetches `/route-info` over aiohttp at most once every 24h, has single-flight locking, and keeps a disk cache next to config.json. The server also warms it at startup.
The watcher itself only uses `terminals.cached` for route names in alerts and for `booking_url`. The contract tests already inject a `FixedTerminals` stub.
refs: src/ferry_watch/bcferries/site.py:60-63, src/ferry_watch/bcferries/site.py:306, src/ferry_watch/bcferries/terminals.py:101-172, src/ferry_watch/server.py:494, src/ferry_watch/server.py:529, src/ferry_watch/watcher.py:546-564, tests/browser/test_site_flow.py:147-152

## In-process coupling: no Page objects leak, but several in-process assumptions do
Page, BrowserContext and Browser are created and kept privately in site.py. The watcher's `Session`/`Browser` protocols use only domain types (Trip, Sailing, Fare, FarePreference, time, Credentials, Path). There is no `storage_state`, `context.cookies()`, `connect_over_cdp` or `launch_server` anywhere in src. Chromium is driven by Playwright's in-process driver launched via `async_playwright().start()`.
Assumptions that break over RPC:
1. `login(credentials)` takes the secret from the caller.
2. `closed`, `lost`, `logged_in` and `opened_at` are read synchronously as attributes or properties, and are fed by Playwright callbacks.
3. `save_debug_snapshot` returns a local Path.
4. Masking via describe() needs the secret on the caller's side.
5. asyncio.timeout/cancel around add_to_cart relies on closing the context locally to drop a half-made cart.
6. `show()`/`bring_to_front()` and `_raise_held_windows` exist only because all trips share one X display and one VNC view.
7. One `_browser_died` fans out to every session.
8. Terminals come from a shared object built on the server's aiohttp session.
9. `_process` re-reads the live config right after page work, to discard results or carts for trips edited mid-check.
refs: src/ferry_watch/watcher.py:57-78, src/ferry_watch/watcher.py:309-318, src/ferry_watch/watcher.py:351-356, src/ferry_watch/watcher.py:424-429, src/ferry_watch/watcher.py:459-468, src/ferry_watch/watcher.py:480-488, src/ferry_watch/watcher.py:506-507, src/ferry_watch/server.py:507-522

## Inventory of secret-touching interactions
Things that must never be reachable from the operator:
(a) The `fill()` of `#email`/`#j_password` in login(), and any Playwright error or call log produced around it.
(b) The logged-in BrowserContext cookies, a bearer credential for the account, plus the Queue-it pass cookies. Today they live only in Chromium memory under an ephemeral profile in `HOME=/tmp/home`.
(c) Checkout. The code never automates payment; the human finishes it over VNC. README says credentials are needed 'so saved cards are available at checkout'. So the VNC session is full interactive control of a logged-in account with saved payment methods. KasmVNC runs with `-SecurityTypes None -DisableBasicAuth -interface 0.0.0.0`.
(d) Failure screenshots and the review page's Order ID: tenant data, not credentials.
(e) The `Settings` repr shows the username, which is not hidden like the password.
The Pushover token and user key are hidden from repr and are the operator's own secrets, not the tenant's BC Ferries secrets.
refs: src/ferry_watch/bcferries/site.py:280-287, src/ferry_watch/bcferries/site.py:169, README.md:36-38, README.md:64, entrypoint.sh:14-16, entrypoint.sh:20-23, src/ferry_watch/settings.py:69-72, tests/fixtures/flow/review.html

## Reusable unchanged inside a per-trip agent pod
Reusable as-is:
- `parse.py`: pure, no browser.
- `errors.py`: the exception taxonomy and describe().
- `terminals.Terminal`/`Terminals`.
- `search_url`, `choose_fare`, `USER_AGENT`, `Credentials`.
- All of `SiteSession`: login, search, refresh, add_to_cart, empty_cart, save_debug_snapshot, close, closed/lost.
- `SiteBrowser`, used with a single new_session. Its relaunch-on-death logic is fine in a pod.
- The browser contract tests and fixtures, which already build SiteSession directly against a fake site.
- The per-trip parts of Watcher: `_session_with_results`, `_check`, `_failed`, `_process`, `_add_to_cart`, `_service_cart`. They only need a per-trip view of config and an outbound event sink in place of RuntimeState/Alerts.
Not needed in a pod: `show()` and `_raise_held_windows`.
Must move to the operator or be reworked into an account-level service: `_login_lock`, `_login_blocked_until` and the concurrency semaphore.
refs: src/ferry_watch/bcferries/parse.py:1-70, src/ferry_watch/bcferries/errors.py:1-50, src/ferry_watch/bcferries/site.py:179-422, tests/browser/test_site_flow.py:188-193, src/ferry_watch/watcher.py:302-542

## Process supervision in the current container
`entrypoint.sh` starts Xvnc, matchbox and `ferry-watch` in the background, then `wait -n; exit 1`. Chromium is a child of the Python process through the Playwright driver, not of the entrypoint. A Chromium crash therefore doesn't end the container; it surfaces only as `disconnected`, which marks sessions lost, and SiteBrowser relaunches on the next `new_session`. matchbox runs with `-use_titlebar no`, so a user closes the checkout window with Chromium's own tab or window controls. Today all trip windows share one display and one `BCF_VNC_URL` (`checkout_url`), which is linked from every cart alert.
refs: entrypoint.sh:34-39, src/ferry_watch/bcferries/site.py:137-160, src/ferry_watch/settings.py:64, src/ferry_watch/settings.py:103, src/ferry_watch/notify.py:229-246

IMPLICATIONS:
- Run the flow code inside the pod: the agent should own SiteSession plus the per-trip check, cart and hold state machine. The operator talks to it over a narrow RPC carrying only domain data: Sailing lists, the Fare/Cart status, typed error codes with text already masked by the agent, and closed/lost/logged_in events. Never give the operator a Playwright WebSocket endpoint (launch_server), a CDP port, pods/exec, pods/attach or pods/portforward. Any of those allows context.cookies(), storage_state() or input_value('#j_password').
- Change the Session contract so `login()` takes no Credentials. The agent reads BCF_USER/BCF_PASS from a Secret mounted only into its own pod (preferably as a file, not env), and describe()-style masking happens in the pod before anything is sent. The operator can no longer mask (watcher.py:122), so it must treat agent error text as already sanitised and keep it to one line of at most 300 characters.
- Keep the decision to cart in the pod, or make refresh-then-add_to_cart a single agent operation. add_to_cart re-parses whatever page is currently loaded (site.py:376), so an operator round-trip between 'saw a sailing' and 'select fare' must not let the page navigate. Push live trip settings (earliest/latest window, fare preference, auto_cart, enabled) to the agent. A search_key change (models.py:177-179) should recreate the context or the pod, matching watcher.py:305-307.
- Replace the process-wide `_login_lock` and `_login_blocked_until` (watcher.py:128-130, 333-349) with operator-held account-level coordination keyed by an opaque account id. The agent asks for a login lease, the operator grants one at a time per account, and a LoginFailed result sets an account-wide 30-minute block that all of that account's pods respect. This needs no secret on the operator side.
- Replace the 3-way check semaphore (watcher.py:113, 127, 249) with an operator-issued poll budget or token per egress IP. Otherwise N independent pods each reloading /fare-selection every 15-30s with the same UA from one home-cluster IP removes today's throttle (MAX_TRIPS=6, models.py:16).
- Have the operator resolve terminals and pass origin/destination code and label in the pod's trip spec. The agent then uses a FixedTerminals-style provider (as tests do at test_site_flow.py:147-152) and never fetches /route-info. TerminalDirectory stays in the operator for validation and route names.
- Turn closed/lost into events pushed by the agent: page close, page crash and browser disconnected. Keep the three outcomes and their semantics: user closed (resume quietly, don't re-alert), lost (alert, then re-cart), released or expired (empty_cart, then resume). Before relying on this, verify what happens in the pod when a headed user closes the only window.
- Cancellation must reach the pod. Today a timed-out or failed add_to_cart is cleaned up by closing the context locally (watcher.py:487-488). In the pod model, an operator-side timeout must send 'abort and close context', or the pod may keep a cart the operator thinks failed.
- Pod lifecycle carries cart semantics. Deleting or rescheduling a pod that holds a cart loses the cart, so trip deletion and release should run empty_cart in the agent before teardown, for example a graceful shutdown hook (current shutdown at watcher.py:368-373 doesn't empty carts). An operator restart no longer has to lose carts, which today's AlertMemory logic assumes (state.py:131-134, watcher.py:572-584). The operator instead needs to reconcile with live pods on startup.
- Drop show(), bring_to_front and _raise_held_windows (site.py:403-405, watcher.py:351-356, 506-507). Make checkout_url per pod or trip rather than one global BCF_VNC_URL (settings.py:103). Route each pod's KasmVNC (:6080, no auth per entrypoint.sh:23) through the ingress/Authelia layer with a per-tenant authorization rule, not through the operator. The VNC stream shows and controls a logged-in account at checkout.
- Failure screenshots stay in the pod's emptyDir by default (save_debug_snapshot writes locally). Exporting them to the operator or UI is an explicit choice to share tenant PII such as account header and order details, though not credentials.
- Contract tests carry over unchanged. Add tests for what the split introduces: SiteBrowser with a single headed window (close vs disconnect), the login-then-queue redirect, and an RPC fake that replaces FakeSite/FakeSession (tests/fakes.py:33-149).

RISKS:
- Account-bound carts across concurrent sessions (unverified). The site looks like a Spring Security / commerce-platform login: the form posts to `/j_spring_security_check` with a CSRFToken (login.html:73-93), and there is an `/abandoned-cart/my-account` endpoint. If a logged-in account has one cart shared across sessions, or BC Ferries limits concurrent sessions per account, then N pods logged into one tenant's account could see or clear each other's carts. For example, empty_cart in one pod could release another pod's held sailing, or a new login could invalidate an older pod's session. Today's design already runs N logged-in contexts at once, so the owner's experience is the only evidence. README:36 asserts the cart belongs to the session, not the account.
- Possible misclassification when the user closes the only window (unverified). If Chromium exits when a headed user closes its last window, Playwright reports `disconnected`. That runs `_browser_died`, then mark_lost (site.py:162-165), so the watcher treats it as a crash: a high-priority 'Lost the cart' alert and an automatic re-cart of the sailing the user may have just bought or abandoned (watcher.py:531-535). Today other trips' windows usually keep the browser alive. In one-window-per-pod this becomes the normal case.
- Login backoff becomes per pod. Without operator coordination, a wrong or rotated password produces one failed login per pod at once, and again every 30 minutes per pod (watcher.py:345). That risks the account lockout that LoginFailed's docstring warns about (errors.py:28-29).
- The operator's pod-create permission is effectively secret-read permission. Anything allowed to create pods in a namespace can mount any Secret there and print it. To truly keep tenant BC Ferries secrets from the operator, its pod creation must be constrained by admission policy (pinned image digest and command, no extra volumes or env), and the operator must lack get/list on Secrets and pods/exec, pods/attach, pods/portforward and ephemeralcontainers.
- Whoever brokers VNC can read the screen and inject input into a logged-in session with saved payment methods. Routing the VNC WebSocket through the operator puts the operator back in the trust boundary the design is trying to remove it from.
- Login completion isn't verified. `logged_in=True` is set when the URL merely leaves /login, with no Queue-it check (site.py:292-301). It is never re-validated over a long-lived pod, so later carts can silently be guest carts without saved cards at checkout.
- Bot detection. N per-trip Chromium processes with the same fixed UA (site.py:45-47) polling from one egress IP, without today's 3-concurrent cap, raises the odds of Queue-it throttling or BC Ferries blocking. README:122-123 already notes that logged-in polling is visible on the account.
- empty_cart is not verified and is only partially error-handled (site.py:400-401, watcher.py:379-382). A release that silently fails leaves the spot held until the site's own timer runs out. Across pods, the operator has no read-back to confirm it worked.
- Resource cost scales linearly: one Chromium plus Xvnc plus matchbox per watched trip, versus one Chromium with N contexts today. A trip that is disabled or past its date closes its session today (watcher.py:207-208). Keeping its pod alive would waste resources unless the pod lifecycle mirrors that.

OPEN_QUESTIONS:
- Does each tenant have their own BC Ferries account, and will one tenant watch several trips at once, meaning several pods logged into the same account? Do you know whether BC Ferries shares a logged-in cart across sessions or allows only one session per account? A quick check: log in from two contexts, cart a sailing in one, and look at the other.
- How do tenant credentials get into the cluster? If tenants enter BC Ferries passwords through the dashboard, the operator handles them in transit. That breaks the 'no way for the operator to get secrets' requirement unless a separate intake path writes the Secret directly. Are out-of-band Secrets (kubectl, External Secrets, 1Password) acceptable instead?
- Should the decision to cart and the hold timer live in the agent pod (autonomous per-trip watcher, operator only configures and observes), or in the operator driving a remote Session over RPC? The first keeps more state off the operator and survives operator restarts. The second keeps the agent thin.
- Is the operator allowed to see non-credential tenant data: sailings, fares, order IDs, failure screenshots of logged-in pages? This decides whether screenshots and order IDs are exported or stay in the pod.
- Should VNC traffic go straight from the ingress/Authelia layer to each browser pod, with per-tenant authorization, so the operator never brokers it? Or is the operator trusted to proxy VNC?
- Should pods run headless for polling and only become headed (Xvnc plus window manager) when a cart is held, by moving the context's storage state to a headed browser inside the pod? That would cut cost but adds a step to verify against BC Ferries and Queue-it.
- What should the pod lifecycle be for paused, disabled or expired trips? Today a paused trip's session is closed but a held cart survives pausing (watcher.py:199-214). Should a paused trip keep its pod (and login) warm, or be torn down?
- What overall poll rate are you willing to send from the home cluster's single egress IP once each trip has its own pod? This sets the global budget the operator would enforce.
