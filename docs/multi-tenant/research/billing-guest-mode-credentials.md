===== guest-mode-credentials
Guest mode should be the default, as long as a short live spike confirms guest checkout works.

(1) The agent can already put a sailing in a guest cart without logging in to BC Ferries. Evidence: the code (search() and add_to_cart() never check for a login), fixtures captured without logging in (one page carries userType=guestUser), and PR #1, which says "Verified live against bcferries.com: a search, and a guest cart added then emptied." Only one gate blocks it: watcher.py:459 and :562 make auto-cart require stored credentials. Confidence: high.

(2) Whether a guest can then pay at bcferries.com is not proven. Indirect evidence says yes, with medium-low confidence:
- BC Ferries tells customers they can cancel "through your BC Ferries account or the link in your booking confirmation email", which implies bookings without an account exist.
- The site has a guest-only vehicle restriction, which implies guests can book other vehicles.
- BC Ferries' app does require an account.

Signing in after the guest cart exists is very likely blocked. The owner observed that the login page won't accept a login while a guest cart exists (README.md:124-125, site.py:288-290). Live research was limited: direct fetches of bcferries.com and cbc.ca were blocked from this sandbox (proxy CONNECT 403, DNS ENOTFOUND), so external claims rest on search snippets and need the spikes.

(3) In guest mode the platform stores no BC Ferries password and no account cookies. What remains is short-lived and exists only during the cart window:
- the cart session cookie
- guest contact details (name, email, phone, address)
- the card number, expiry and CVV the user types over VNC

The card is the real exposure. KasmVNC runs with -SecurityTypes None, so keystrokes travel in plain text after TLS ends, and the framebuffer shows the digits. -AlwaysShared also lets a second viewer watch silently. A compromised operator gets the card if it is in the VNC path, can exec or attach into browser pods, can change the browser pod's image, environment or base URL, or can re-point the VNC route to a pod it made.

(4) "User signs in during the cart window" is ruled out by the login-blocked-while-guest-cart behaviour. Recommended opt-in is an "attended sign-in": the user logs in themselves through a gated sign-in window opened BEFORE any cart, and the pod keeps only the in-memory session cookie, so no password is ever stored. If that session doesn't survive polling, the fallback is a password typed into a page served by the browser pod itself and held in pod memory only, re-entered each time the pod is recycled. Two options should be rejected:
- An operator-written create-only Secret: the operator sees the plaintext at intake, and anyone who can create pods in a namespace can read every Secret in it.
- An age/sealed blob: it only resists passive theft, because the operator serves the encrypting JavaScript and controls the specs of the pods that hold the key.

## Q1: Guest carting works in code and was verified live; only the credential gate in watcher.py blocks it
The booking flow never requires a login:
- SiteSession.search() walks route, passengers, vehicle and fare-selection with no login check (site.py:303-354).
- add_to_cart() selects the fare and reads back the review page with no login check (site.py:373-398).
- Browser contract tests cart without logging in (tests/browser/test_site_flow.py:248-251).
- The fixtures were 'captured 2026-10-11 without logging in' (tests/fixtures/README.md:3-4), and the vehicle page carries <input id="userType" value="guestUser"> (tests/fixtures/flow/vehicle.html:355).
- Live evidence: the PR #1 description says 'Verified live against bcferries.com: a search, and a guest cart added then emptied.' This is only in the PR body, not in the commit message from `git log --format=%B`.

Why guest auto-cart is off today: `_process` carts only when `trip.auto_cart and self._credentials is not None` (watcher.py:459). The alert's cart_failed flag uses the same condition (watcher.py:562), and the dashboard defaults auto_cart to login_configured (views.py:113). `_wants_login` already returns False without credentials (watcher.py:299-300), so the session stays a guest.

Caveats:
- The review fixture is synthetic, 'from a real one' (tests/fixtures/flow/review.html:4), so how the guest review page actually looks isn't captured.
- The vehicle type used in the live test isn't recorded.

Confidence: high.
refs: src/ferry_watch/bcferries/site.py:303-354, src/ferry_watch/bcferries/site.py:373-398, src/ferry_watch/watcher.py:299-300, src/ferry_watch/watcher.py:459, src/ferry_watch/watcher.py:562, src/ferry_watch/views.py:113, tests/browser/test_site_flow.py:248-251, tests/fixtures/README.md:3-4

## Q2: Guest checkout is probably supported but unproven; signing in on top of a guest cart is very likely blocked
Evidence that guest bookings exist on the website:
- BC Ferries' contact page says an online booking mistake can be cancelled at no cost within 30 minutes 'through your BC Ferries account or the link in your booking confirmation email'. That implies bookings without an account. Medium confidence, from a search snippet.
- A sign-in help page says 'if you do not have an account, refer to your booking confirmation email'. It may be a legacy /res/ page. Low-medium confidence.
- The vehicle page has a guest-only block, 'To book this type of vehicle, log in or create an account', for oversize vehicles (vehicle.html:584-587). Guests are therefore expected to book other vehicle types.

Evidence against:
- CBC reported at the app's launch that the app requires an account to book, and a user asked for guest checkout. That is the app, not the website.

Signing in at checkout:
- The owner observed that the login page won't accept a login while a guest cart exists (README.md:124-125).
- The code records that the site clears the login form while a guest cart exists (site.py:288-290).
- The captured login page uses the 'checkout-login' template (tests/fixtures/flow/login.html:61) and has a cart timer hook, cartTimerContinueUrl (login.html:45).
- Modals were stripped from the fixtures (tests/fixtures/README.md), so a 'your cart will be cleared' dialog may exist and not be captured.

Not confirmed:
- whether a checkout-step 'sign in' merges, keeps, or drops the guest cart
- the guest payment page itself (required fields, card-field host, 3-D Secure)
- BC Ferries' own cart timer length

Network: bcferries.com and cbc.ca could not be fetched (proxy CONNECT 403 and WebFetch ENOTFOUND). All external BC Ferries claims come from search snippets.
refs: https://www.bcferries.com/contact-us, https://www.bcferries.com/res/SigninCentre.html, https://www.cbc.ca/news/canada/british-columbia/bc-ferries-launches-app-1.6777563, README.md:124-125, src/ferry_watch/bcferries/site.py:288-290, tests/fixtures/flow/vehicle.html:584-587, tests/fixtures/flow/login.html:45, tests/fixtures/flow/login.html:61

## Today nothing is persisted to disk; guest mode leaves no long-lived BC Ferries secrets at all
The browser keeps nothing between runs:
- Chromium is launched with no user_data_dir (site.py:149-160).
- Each trip gets a non-persistent `browser.new_context(user_agent=..., no_viewport=True)` with no storage_state (site.py:167-176), so cookies live in memory only.
- HOME is an ephemeral /tmp/home on a read-only root filesystem (entrypoint.sh:13-17).

The only long-lived tenant secret is BCF_USER/BCF_PASS:
- read from the environment (settings.py:40-42, 68)
- held in the Watcher for the life of the process (watcher.py:110, 121-122)
- typed by login() (site.py:270-301)

In guest mode, credentials are None. No password exists, and no account session exists. The cart cookie lives only as long as the hold (≤ BCF_CART_HOLD_MINUTES, watcher.py:498), and the pod is recycled afterwards, as decided.

Polling is also anonymous, which removes 'your account is visibly refreshing the site' (README.md:122-123).
refs: src/ferry_watch/bcferries/site.py:149-176, src/ferry_watch/bcferries/site.py:270-301, src/ferry_watch/settings.py:40-42, src/ferry_watch/settings.py:68, src/ferry_watch/watcher.py:110, src/ferry_watch/watcher.py:121-122, src/ferry_watch/watcher.py:498, entrypoint.sh:13-17

## Q3: Compromised-operator threat model under guest mode: the live card entry over VNC is the remaining crown jewel
What remains, by value:

(a) Card number, expiry, CVV, and any 3-D Secure one-time code the user types at BC Ferries' payment page.
- They pass through KasmVNC as RFB KeyEvent keysyms. Xvnc runs with `-SecurityTypes None -sslOnly 0` (entrypoint.sh:23), so there is no RFB-level encryption, and anything that terminates TLS in front of the pod sees keystrokes in plain text (RFC 6143 §7.5.4).
- The framebuffer also renders the digits.
- `-AlwaysShared` (entrypoint.sh:24) lets a second viewer connect silently.
- KasmVNC's DLP logging is off by default but configurable, and whoever controls the pod spec controls it.

(b) Guest contact details typed at checkout: name, email, phone, address.

(c) The BC Ferries cart/session cookie (JSESSIONID-style, in memory, ≤ hold time). Its value is hijacking or abandoning the user's held spot, or paying for it with another card. It gives no account access.

(d) The booking reference on the confirmation page. Guest bookings are managed via the email link (see S9), so an exposed reference may allow changes.

How a compromised operator could reach these:
- It proxies or terminates the VNC WebSocket.
- Its ServiceAccount has pods/exec, pods/attach, pods/ephemeralcontainers or pods/portforward on browser pods.
- It can create browser pods with a modified image, environment or arguments. Examples: turn on KasmVNC DLP logging; override base_url so login() and checkout go to a look-alike host. base_url is a constructor parameter (site.py:112, used at 278 and 306).
- It can re-point the trip's VNC link or Service/route to a pod it created. The user cannot verify what a remote browser renders.

What it can no longer get in guest mode: the BC Ferries password, account cookies, saved cards, booking history, or the ability to cancel the user's account bookings.

Required controls:
- Route VNC ingress → auth proxy → browser pod directly, never through operator code.
- The browser pod enforces the 'cart held' gate itself and verifies the auth proxy's signed identity token, which the operator can't forge.
- Disable AlwaysShared, allowing a single viewer.
- An admission policy the operator cannot edit: pin the browser image digest; forbid env/args overrides of the base URL and KasmVNC logging; deny exec/attach/ephemeralcontainers/portforward to the operator.
- An egress allowlist (FQDN) to bcferries.com and queue-it.net.

Residual risk: the image and CI supply chain, plus cluster admins, remain the root of trust.
refs: entrypoint.sh:20-26, src/ferry_watch/bcferries/site.py:112, src/ferry_watch/bcferries/site.py:278, src/ferry_watch/bcferries/site.py:306, src/ferry_watch/watcher.py:498, README.md:36-38, README.md:81-83, https://www.rfc-editor.org/rfc/rfc6143

## Q4: Comparison of login options for users who want saved cards or account bookings
(A) The user signs in inside the gated cart window, after the agent carted as a guest.
- Likely not viable: the login page rejects or clears a login while a guest cart exists (README.md:124-125, site.py:288-290).
- It only works if spike S3 finds a checkout sign-in that keeps the cart.

(A') Attended sign-in BEFORE carting.
- The user opens a gated 'sign-in window' when the watch starts and logs in themselves.
- The pod keeps only the in-memory account cookie, and no password is stored anywhere.
- If the session lapses, the pod falls back to guest carting and alerts the user.
- Code impact: login state is set once (site.py:301) and never re-checked, so an is-logged-in probe is needed. The watcher logs in before searching (watcher.py:315-318).
- Exposure: the account cookie is in pod memory for the whole watch (days), so exec/attach must be denied as in Q3. The password is typed over VNC once, with the same keystroke exposure as the card.
- Requires widening the decided VNC gate (open question).

(B1) A Kubernetes Secret written create-only by the operator.
- Fails: the plaintext transits the operator at intake, and a compromised operator just logs new intakes.
- RBAC resourceNames cannot restrict create.
- Anyone who can create pods in a namespace can mount and read every Secret in it.

(B2) A credential intake page served by the browser pod, bypassing the operator.
- Acceptable if the password is held in pod memory or tmpfs only and re-entered whenever the pod is recycled.
- Writing it to a Secret reintroduces the pod-create-reads-secrets problem.
- The intake page needs the same direct, gated route as VNC.

(B3) An age/sealed blob whose private key only browser pods have.
- The key would have to be mounted into pods the operator creates, and the operator serves the dashboard JavaScript that encrypts.
- So it protects only against passive theft of the database or etcd, not an active operator compromise.
- Making it strong needs an external KMS bound to attested workload identity, plus admission control over pod specs. That is a lot of complexity for a nominal-fee tool.

Limit on the saved-card benefit: if BC Ferries asks for the CVV on saved cards (S8), login mode still puts card data over VNC.
refs: README.md:64, README.md:124-125, src/ferry_watch/bcferries/site.py:288-290, src/ferry_watch/bcferries/site.py:301, src/ferry_watch/watcher.py:315-318, src/ferry_watch/watcher.py:128-130, https://kubernetes.io/docs/concepts/configuration/secret/, https://kubernetes.io/docs/concepts/security/rbac-good-practices/

## empty_cart() does not verify that a guest cart was actually emptied
empty_cart() only navigates to /abandoned-cart/my-account (site.py:400-401). It never checks where it lands or whether the cart is empty.

The test fake simply redirects to /my-account (tests/browser/test_site_flow.py:100-101). For a guest, a real 'my-account' URL may redirect to /login, and `_goto` would not raise.

PR #1 says a guest cart was 'emptied', but the code cannot tell. Under guest mode with many tenants, unverified releases could keep sold-out inventory held until BC Ferries' own cart timer runs out. See spike S5.
refs: src/ferry_watch/bcferries/site.py:400-401, src/ferry_watch/bcferries/site.py:233-238, tests/browser/test_site_flow.py:100-101, src/ferry_watch/watcher.py:375-382

## Stripe fit: guest mode is independent of Stripe, and Stripe keeps the fee card off our servers
Stripe needs no BC Ferries account linkage:
- The paid entitlement (route watch per user) keys on the auth-proxy identity mapped to a Stripe Customer.
- Stripe-hosted Checkout sends card data to Stripe and keeps it off our servers, a low-risk integration for PCI purposes.

The contrast: the BC Ferries card typed over VNC does pass through our pods. Stripe's security guide says anyone involved in 'the processing, transmission, or storage of card data must comply with PCI DSS'.

Whether relaying a cardholder's own keystrokes to a third-party merchant puts the platform in scope is a grey area. Searches found no ruling; scoping guidance treats systems that transmit cardholder data as in scope.

Also a risk, not researched here: BC Ferries' terms on a paid service carting on behalf of others.
refs: https://docs.stripe.com/security/guide, https://docs.stripe.com/payments/checkout/how-checkout-works?payment-ui=stripe-hosted, https://www.securitymetrics.com/learn/pci-dss-scoping-updates

## Spike plan to settle BC Ferries behaviour (run from a workstation; this sandbox cannot reach bcferries.com)
Setup: a headed run (scripts/run-local.sh or a small Playwright script using SiteBrowser and SiteSession directly) with BCF_USER/BCF_PASS UNSET. Pick a sailing weeks out with plenty of space; never hold sold-out inventory. Save a HAR with credentials scrubbed.

S1 Guest cart.
- In a fresh context: new_session → search(trip) → add_to_cart(depart, pref).
- Record the final URL (/fare-selection-review), all cookie names, domains, expiry and HttpOnly flags, the userType value, and any visible cart countdown.
- Pass: the review page shows the sailing and fare.

S2 Guest checkout path.
- From the review page, click through manually.
- Log each URL and heading, whether a 'Continue as guest' or 'Sign in' choice appears, the required personal-info fields, the payment card-field iframe origin, and whether 3-D Secure triggers.
- Stop before submitting payment.
- S2b (owner approval needed): complete a cheap foot-passenger or Reservation Only booking as a guest with the owner's card, then cancel within 30 minutes through the email link. This proves end-to-end guest checkout and guest management at no cost, per the contact-us page.

S3 Sign-in with a guest cart.
- (a) With a guest cart held, open /login in the same context and type credentials by hand. Record whether the fields clear, any modal or error text, and the redirect. Then reopen the review/cart page and record whether the cart survived, merged, or was lost.
- (b) If S2 found a sign-in at checkout, repeat (a) through it.
- (c) Control: log in first, then cart (today's flow).

S4 Logged-in session longevity under polling (decides attended sign-in).
- Log in manually.
- Run refresh() every 30 seconds for 8 hours, then 24 hours.
- Every 15 minutes, probe login state (a 'Log out' link, or /my-account without a redirect to /login).
- Record the idle and absolute timeouts, and survival across a Queue-it detour.

S5 Release verification.
- After a guest add_to_cart, call empty_cart().
- Record the landing URL. Re-open the cart or review page to confirm it is empty, and if possible confirm the sailing's capacity returns.

S6 BC Ferries cart timer.
- After a guest add_to_cart, leave it idle.
- Re-check the cart every minute until BC Ferries drops it.
- Test whether navigation extends it, and compare with BCF_CART_HOLD_MINUTES (default 60).

S7 Vehicle and fare coverage as guest.
- Repeat S1 for TALL (over 7 ft, under 20 ft), walk-on, and each fare type (Saver, Prepaid, Reservation Only).
- Note whether 'log in or create an account' (vehicle.html:584-587) appears.

S8 Saved-card value.
- Logged in, reach payment.
- Record whether a saved card needs the CVV re-entered, or any other card data.

S9 Guest booking management.
- From the S2b confirmation email, record whether change/cancel works from the link alone, or by booking reference plus email or last name lookup.

S10 VNC exposure (internal; no BC Ferries needed).
- In a test cluster, put mitmproxy where TLS ends in front of KasmVNC.
- Type 4242 4242 4242 4242 in the remote Chromium and confirm the keysyms are readable.
- Connect a second viewer and confirm -AlwaysShared lets it watch silently.
- Then retest with sharing disabled and a direct (non-operator) route.
refs: scripts/run-local.sh, src/ferry_watch/bcferries/site.py:167-176, src/ferry_watch/bcferries/site.py:373-401, src/ferry_watch/watcher.py:498, tests/fixtures/flow/vehicle.html:584-587, entrypoint.sh:23-24, https://www.bcferries.com/contact-us, https://www.bcferries.com/terms-and-conditions/booking-cancellation-change-policies

RECOMMENDATIONS:
- Default to guest mode for all tenants, once spikes S1, S2 and S5 pass. Browser pods get no BC Ferries credentials and never persist storage_state. Remove the credential gate on auto-cart at watcher.py:459 and :562, make views.py:113 default auto_cart to true, and change the cart alert wording (notify.py:239-242) to say checkout is as a guest.
- Opt-in login, first choice: 'attended sign-in'. The user logs in themselves through a gated sign-in window opened before any cart exists; the pod keeps only the in-memory account cookie; no password is stored anywhere. Add an is-logged-in probe (login state is set once at site.py:301 and never re-checked), and fall back to guest carting with an alert when the session lapses. Adopt this only if S4 shows sessions survive polling for a useful time.
- Opt-in login, fallback if S4 fails: a credential page served by the browser pod itself, on the same direct gated route as VNC and never through the operator. Hold the password in pod memory or tmpfs only, and ask for it again whenever the pod is recycled.
- Reject the operator-written create-only Secret. Reject the age/sealed blob unless its key sits in an external KMS bound to attested workload identity and the encrypting UI is not served by the operator; without that, neither stops an operator that is actively compromised.
- Make the VNC path independent of the operator: ingress → auth proxy → browser pod. The pod enforces the 'cart held' gate itself, checks the auth proxy's signed identity token, and allows a single viewer (drop -AlwaysShared at entrypoint.sh:24). Never let operator code terminate or proxy the RFB WebSocket.
- Constrain browser pods with an admission policy the operator's ServiceAccount cannot edit: pin the image digest; forbid env/args overrides of the BC Ferries base URL (site.py:112) and KasmVNC logging/DLP settings; deny the operator pods/exec, attach, ephemeralcontainers and portforward on browser pods; add an FQDN egress allowlist (bcferries.com, queue-it.net).
- Make empty_cart() verify its result: check the landing URL and that the cart is empty (site.py:400-401). Its outcome should feed the cart state, so guest releases at multi-tenant scale really hand inventory back.
- Get advice before launch on whether relaying users' BC Ferries card keystrokes through our pods affects PCI scope, especially once Stripe makes the platform a merchant. Use Stripe-hosted Checkout for the fee so our own card-data footprint stays minimal.

RISKS:
- Guest checkout may not exist or may be restricted on the website (some vehicle types need login, per vehicle.html:584-587; the app needs an account). If S2 fails, there is no credential-free default, and the attended sign-in opt-in becomes mandatory.
- Card number, CVV and one-time codes typed over KasmVNC are plain text past TLS termination (-SecurityTypes None, entrypoint.sh:23) and visible in the framebuffer. Any component in the VNC path, or any party that can exec or attach into the pod, can capture them.
- A compromised operator that can create browser pods can phish: launch a modified image or base_url and point the user's 'cart held' link at it. Users cannot verify a remote browser. Only admission policy plus direct routing controls this.
- Attended sign-in leaves a long-lived BC Ferries account cookie in pod memory for the whole watch. Its value is account takeover (cancel or change bookings, see personal details), not just the cart.
- BC Ferries behaviour may change without notice (cart timer, login-with-cart rules, guest checkout), and all external BC Ferries evidence here is search snippets: direct fetches were blocked from this sandbox.
- Many anonymous guest sessions from one cluster egress IP may trip BC Ferries or Queue-it rate limiting or bot detection, which is more visible than one owner account.
- Unverified cart releases (site.py:400-401) could leave sold-out sailings held until BC Ferries' own timer expires, which reflects badly on the service and BC Ferries may act on it.
- Taking a fee for automated carting on someone else's site may conflict with BC Ferries' terms. Not researched in this area; flag for legal review.

OPEN_QUESTIONS:
- Should the decided 'VNC only while a cart is held' gate be widened to allow a pre-cart 'sign-in window' for the attended-login opt-in, or should login mode be dropped at launch?
- May spike S2b run: a real, cheap guest booking with the owner's card, cancelled within the 30-minute free window, to prove guest checkout end to end?
- Is it acceptable that users type their BC Ferries card details into a remote browser hosted on the platform (PCI and liability stance), given Stripe will make the platform a merchant for its own fee?
- Who owns admission policy and VNC ingress routing? Can the operator's ServiceAccount be denied pods/exec, attach and portforward and the ability to change browser pod specs and routes?
- Is a login opt-in worth building if S8 shows saved cards still need the CVV re-entered at BC Ferries checkout?
- If a fee-paying user's guest cart expires or the release fails, what is the refund or retry policy (this touches the Stripe design)?
