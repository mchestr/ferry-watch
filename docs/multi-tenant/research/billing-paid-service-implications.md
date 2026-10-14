===== paid-service-implications
Turning Ferry Watch into a paid service for strangers changes four things. It moves it from a personal tool to a commercial bot sold against BC Ferries. Each user's Pushover licence becomes a cost they have to bear. Authelia, the current login, can't let the public sign up. And browser pods now need real isolation. On BC Ferries: I couldn't read bcferries.com directly (the egress proxy blocks it with a 403 and WebFetch can't resolve DNS). Search excerpts from its Terms & Conditions and Conditions of Carriage confirm two clauses. Bookings must come "from the Carrier, or from an authorized source expressly authorized by the Carrier", and "unauthorized resale ... is prohibited". I found no website clause on bots, scraping or account sharing, so a lawyer has to read those terms directly. Since spring 2026 BC Ferries runs its own free "first-come, first-served" sailing alerts and a waitlist pilot that books automatically. A paid service that holds carts within seconds therefore looks like paying to jump that line, even without resale. On Pushover: since May 1, 2026 the free limit is 10,000 messages a month per account, shared by every app on it. Every recipient needs a licence ($4.99 per platform, one-time) unless the operator buys licence credits for them. On identity: Authelia has no self-registration, and Google-style social login is only a "needs-design" item on its roadmap. On isolation: Chromium currently runs with its own sandbox off (Playwright's default), the VNC viewer has no authentication, and the app has no notion of users. Fixing per-trip authorization, sandboxing and egress control matters more than picking gVisor or Kata. Sources: GitHub source clones (Authelia, oauth2-proxy, Pocket ID, Playwright, Kubernetes docs, KasmVNC, gVisor): high confidence. Stripe docs (via the read-only Stripe documentation search): high confidence. WebSearch excerpts: medium confidence, exact wording should be re-checked on the live pages. Nothing in the repo, Stripe or any other system was changed.

## Research access and confidence
Through the egress proxy only GitHub and the Stripe documentation tool were reachable. bcferries.com, pushover.net, docs.stripe.com, webkit.org, gvisor.dev, authelia.com, kubernetes.io and queue-it.com all got a 403 on CONNECT. WebFetch failed with 'getaddrinfo ENOTFOUND' for every host. Confidence by source: (a) project docs read from sparse GitHub clones: HIGH. (b) Stripe docs via the read-only search_stripe_documentation: HIGH. (c) everything else via WebSearch result excerpts: MEDIUM for wording, LOW where flagged. I did not call stripe_implementation_planner because it needs a Stripe account context. Nothing in Stripe or any other external system was created or changed.
refs: curl $HTTPS_PROXY/__agentproxy/status (recentRelayFailures: kubernetes.io, www.authelia.com, www.bcferries.com 403)

## (a) BC Ferries terms found: an authorized-source rule and a resale ban. No bot or automation clause found.
Search excerpts of bcferries.com (MEDIUM confidence on exact wording):
- Conditions of Carriage: "Bookings and tickets for carriage must be obtained from the Carrier, or from an authorized source expressly authorized by the Carrier."
- Terms & Conditions: "The unauthorized resale of BC Ferries products, including bookings, reservations, and tickets, is prohibited."
- Booking policies: "The non-authorized resale of a booking/reservation for commercial purposes is prohibited."
- Conditions of Carriage: "The Carrier has the right, but not the obligation, to make inquiries regarding the source of a passenger's booking, reservation, or ticket." If it suspects an unauthorized source, it may cancel the booking or refuse carriage.
- The terms are governed by British Columbia law and Canadian federal law.

Several searches found NO website terms-of-use clause on robots, spiders, scrapers, automated access, commercial use of the site, or sharing account credentials. The Terms & Conditions page returned by search covers carriage and the Experience Card. This is a gap that has to be closed by reading the live site and its account terms. It does not show the clauses are absent.

On positioning: if users book on their own BC Ferries accounts and pay BC Ferries directly, the service is not reselling. But a fee charged per booking obtained could be read as brokering, or as the booking coming from an 'unauthorized source'.
refs: https://www.bcferries.com/terms-and-conditions, https://www.bcferries.com/terms-and-conditions/conditions-of-carriage, https://www.bcferries.com/terms-and-conditions/booking-cancellation-change-policies

## BC Ferries now runs its own free alerts and an auto-booking waitlist, both framed as fair queues
News coverage and BC Ferries pages (MEDIUM):
- Sailing alerts are free emails sent when space opens on a sold-out sailing, "on a first-come, first-served basis". They started on Horseshoe Bay–Nanaimo, Horseshoe Bay–Langdale and Powell River–Comox, and extended to Tsawwassen–Swartz Bay and Tsawwassen–Duke Point on April 2, 2026.
- A waitlist pilot on Powell River–Comox, from April 2, 2026, books and charges customers automatically, based on their position in line.
- Free third-party alert services already exist: NextSailing.ca (free SMS, donations only), FerryHawk, and the 'Ferry Alert' app.

What this means: alerts alone are now a free commodity, so the paid value is the auto-cart. Auto-carting is exactly what cuts against BC Ferries' fairness model. Paying users would systematically beat everyone else, including people on BC Ferries' own alert list. That raises the chance BC Ferries enforces against the service, independent of the resale clause.
refs: https://604now.com/bc-ferries-sailing-alerts-waitlist-pilot-long-weekend-april-2026/, https://www.bcferries.com/waitlists, https://bc.ctvnews.ca/free-website-alerts-bc-ferries-passengers-when-spots-open-up-on-full-sailings-1.7067108, https://ferryhawk.ca/guide

## Code behaviours that look like scalping once the service is sold to others
Each of these is fine for one owner but becomes an issue when sold:
- Auto-cart puts an opened sailing in the cart "within seconds" (README.md:10).
- A held cart is kept 60 minutes by default (settings.py:30; README.md:11-12). Across many tenants, that means holding inventory others can't book.
- Checks run every 30 s by default, with a 15 s floor, for up to 6 trips per user (models.py:16, 19, 284). Each check is a full page load.
- It logs in with the owner's stored BC Ferries username and password (settings.py:40-42, 68; site.py:270-301; watcher.py:299-300, 459). For other users, that means collecting their passwords, which is credential sharing.
- It sends a desktop Mac Chrome user agent so the bot isn't seen as Chromium (site.py:44-47). Counsel may read this as concealment.

In its favour: it waits in the Queue-it waiting room rather than bypassing it (site.py:240-249).

Queue-it (vendor pages, MEDIUM) lets its customers set Traffic Access Rules by IP address, ASN and classifier. It can also tie queue access to a unique identifier and "limit each validated customer to one queue spot". Data-centre egress IPs plus several pods per user is the profile those controls are built to catch.
refs: README.md:10-12, src/ferry_watch/settings.py:30, src/ferry_watch/settings.py:40-42, src/ferry_watch/models.py:16, src/ferry_watch/models.py:19, src/ferry_watch/models.py:284, src/ferry_watch/bcferries/site.py:44-47, src/ferry_watch/bcferries/site.py:240-249

## BC Ticket Sales Act bans ticket bots, probably only for live events
BC's Ticket Sales Act (introduced 2019) "bans the use of automated software, such as bots, to purchase tickets". Coverage describes it as aimed at live events and resale businesses. Ferry carriage is probably out of scope, but I couldn't confirm the Act's commencement or its definition of 'event' (LOW-MEDIUM). Counsel should confirm.
refs: https://www.straight.com/living/bc-rolls-out-new-ticket-buying-rules-for-live-events, https://biv.com/article/2019/04/bcs-ticket-sales-act-aims-crack-down-online-scalping

## (b) Pushover: an account-wide 10k/month limit, and each recipient needs a licence
Facts from search excerpts of pushover.net and the Pushover blog and support pages (MEDIUM-HIGH):
- Since May 1, 2026, the API limit is per account, not per application. Every app on the account shares 10,000 free messages a month (25,000 for Pushover for Teams).
- One message is one successful delivery to one user. The limit resets on the 1st at 00:00 Central Time. Once it's used up, every app on the account gets HTTP 429.
- Extra capacity (support page, possibly older pricing): 10k for $25, 25k for $57.50, 50k for $100, 100k for $150, 500k for $500. It is non-refundable and non-transferable.
- Recipients pay $4.99 USD once per platform (iOS, Android, Desktop) after a 30-day trial. Alternatively the operator can buy licence credits and assign them through the Licensing API (api.pushover.net/1/licenses/assign.json). Credits are non-refundable and permanently bound to that user.
- Pushover for Teams costs $5 per user per month and is built for administering an organization's own members, not a public customer base.
- One app token can send to many user keys. The Web-based Subscription flow collects each user's key and redirects back to your site with pushover_user_key, so users never paste keys.

At scale: roughly 500 tenants × 20 alerts/month uses the whole free pool. Cart, failure, expiry and login alerts all count.

In the code, the app token and one user key come from env (settings.py:43-45) and live in the process (notify.py:53-71). In a pod-per-trip design that shared token would sit in every browser pod. Because the limit is account-wide, one compromised or buggy pod could burn every tenant's alerts.

Not verified: that priority 1, which cart alerts use (notify.py:70, 225, 245), bypasses quiet hours. That comes from memory (LOW).
refs: https://blog.pushover.net/posts/2026/4/app-limits, https://support.pushover.net/i12-message-size-and-frequency-limitations, https://support.pushover.net/i13-purchasing-additional-capacity-to-send-more-messages-per-month, https://pushover.net/pricing, https://pushover.net/teams, https://pushover.net/api/licensing/, https://pushover.net/api/subscriptions/, src/ferry_watch/notify.py:53-71

## Per-user alert channels: Web Push fits the existing PWA, but the service worker is missing
Web Push (MEDIUM-HIGH):
- iOS/iPadOS 16.4+ supports it only for web apps added to the Home Screen. The permission request must come from a user gesture such as a Subscribe tap.
- Declarative Web Push, which needs no service worker, needs iOS 18.4+.
- A third-party report says iOS subscriptions sometimes stop after inactivity (LOW).
- Costs: VAPID keys held by the operator, and payloads encrypted to the browser. No per-user licence, no per-message fee.
- The repo has the manifest and Apple meta tags (web/public/manifest.webmanifest; web/index.html:9-15) but no service worker or PushManager code (grep found none).

ntfy (MEDIUM): a self-hosted server only gets instant iOS notifications through upstream-base-url=https://ntfy.sh, which relays a content-free poll request via APNs. That needs a public server, makes ntfy.sh a dependency, and means every user installs the ntfy app. Topic-based access control is a poor fit for paying strangers.

SMS through Twilio Canada (MEDIUM): about US$0.0079–0.0083 per outbound SMS, plus carrier fees and about $1.15/month per long-code number. Competitors (NextSailing, FerryHawk) use SMS.

Email through a transactional provider: cheap, but too slow and noisy for a cart that expires. Good for receipts, account mail and fallback.
refs: https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/, https://webkit.org/?p=16535, https://pushpad.xyz/blog/ios-special-requirements-for-web-push-notifications, https://github.com/binwiederhier/ntfy/issues/331, https://www.twilio.com/en-us/sms/pricing/ca, web/public/manifest.webmanifest, web/index.html:9-15

## (c) Authelia can't do public self-serve sign-up
From the Authelia docs source (HIGH):
- Its features list only "Self-service reset of user passwords" (docs/content/overview/prologue/introduction.md:27). There's no self-registration.
- Users live in a file or LDAP backend. GitHub discussion #7797 complains about hand-editing YAML with password hashes.
- Delegating login to providers like Google (OIDC Relying Party) is a roadmap item in 'planning' with stage "needs-design" (docs/content/roadmap/planning/openid-connect-1.0-relying-party.md).
- Its trusted-header SSO sends Remote-User, Remote-Groups, Remote-Name and Remote-Email. The docs warn: "your proxy must ensure only Authelia is setting these headers, and any other headers are never forwarded to the backend". They also recommend configuring trusted source IPs (docs/content/integration/trusted-header-sso/introduction.md:52-55, 63-73).
refs: https://github.com/authelia/authelia/blob/master/docs/content/roadmap/planning/openid-connect-1.0-relying-party.md, https://github.com/authelia/authelia/blob/master/docs/content/overview/prologue/introduction.md, https://github.com/authelia/authelia/blob/master/docs/content/integration/trusted-header-sso/introduction.md, https://github.com/authelia/authelia/discussions/7797

## Self-serve identity options, verified
oauth2-proxy (docs source, HIGH):
- Google, GitHub and generic OIDC providers. There's no dedicated Apple provider page.
- `--email-domain=*` admits anyone.
- Identity reaches the app as X-Forwarded-User/Email (pass_user_headers) or X-Auth-Request-* (set_xauthrequest).
- With `--reverse-proxy`, it trusts every source IP unless `--trusted-proxy-ip` is set, "which means a client that can reach OAuth2 Proxy directly may be able to spoof forwarded headers" (docs/docs/configuration/overview.md:197, 249).
- `--skip-auth-strip-headers` defaults to true.

authentik (MEDIUM): ships enrollment blueprints, including flows-enrollment-email-verification.yaml (users are created inactive and activated after email verification), plus invitation-based enrollment.

Zitadel (MEDIUM):
- Hosted self-registration, social login (Google, Apple, GitHub) and passkeys.
- Before 2.64.0, turning registration off only hid the button (CVE-2024-49757).
- Login V2 had a policy bypass in 4.0.0–4.12.0, fixed in 4.12.1 (CVE-2026-29193).

Pocket ID (source, HIGH): passkey-only OIDC provider. ALLOW_USER_SIGNUPS defaults to "disabled". "open" allows anyone to sign up; otherwise a signup token is required (backend/internal/appconfig/model.go:25, 131; backend/internal/usersignup/service.go:52).

Keycloak: its realm 'User registration' setting and identity brokering are from memory, not verified here (MEDIUM).
refs: https://github.com/oauth2-proxy/oauth2-proxy/blob/master/docs/docs/configuration/overview.md, https://docs.goauthentik.io/add-secure-apps/flows-stages/flow/examples/flows/, https://zitadel.com/docs/guides/integrate/onboarding/end-users, https://tenable.com/cve/CVE-2024-49757, https://db.gcve.eu/vuln/CVE-2026-29193, https://github.com/pocket-id/pocket-id/blob/main/backend/internal/appconfig/model.go, https://github.com/pocket-id/pocket-id/blob/main/backend/internal/usersignup/service.go

## The app has no notion of users, and the VNC viewer has no auth: authorization is the real gap
The server says "There is no authentication here" (server.py:3). README says to put it behind a proxy (README.md:81-83). No code reads Remote-User, X-Forwarded-User or X-Auth-Request-* (grep found nothing).

KasmVNC runs with `-DisableBasicAuth -SecurityTypes None` on 0.0.0.0:6080 (entrypoint.sh:22-23).

With strangers as tenants, logging in isn't enough. Every API call, every WebSocket (server.py Hub.serve) and every per-pod VNC WebSocket must check that this user owns this trip or pod.

The same-origin check passes when there is no Origin header and no Sec-Fetch-Site (server.py:193-198). That's acceptable only behind the proxy.

A Stripe webhook endpoint has to skip the login proxy (Stripe can't log in). It must instead verify the Stripe-Signature HMAC over the raw body, and can additionally allowlist Stripe's IPs.
refs: src/ferry_watch/server.py:1-7, src/ferry_watch/server.py:193-198, README.md:81-83, entrypoint.sh:22-23, https://docs.stripe.com/webhooks

## (d) Canadian privacy: PIPEDA is the baseline, and replacement Bill C-36 is pending
MEDIUM confidence:
- BC PIPA is designated substantially similar to PIPEDA and covers private-sector activity within BC. PIPEDA covers interprovincial and international commercial activity. A BC service whose data goes to US processors (Stripe, Apple and Google push services, Pushover) will likely be under PIPEDA as well.
- PIPEDA has required breach reporting since Nov 1, 2018. Breaches with a "real risk of significant harm" go to the OPC and the affected individuals "as soon as feasible". Every breach must be recorded and the record kept 24 months. Sources differ on when the 24 months start; check SOR/2018-64.
- Whether BC PIPA has its own mandatory breach notification is contested between sources and unverified.
- Bill C-36, the Protecting Privacy and Consumer Data Act, was introduced June 15, 2026. It would repeal PIPEDA's privacy part and add a right to request disposal of personal information and a private right of action. Its stage as of October 2026 is unverified.
refs: https://www.bennettjones.com/Insights/Blogs/2026/06/Bill-C-36-and-Canadian-Privacy-Reform, https://www.osler.com/?p=121632, https://gazette.gc.ca/rp-pr/p2/2018/2018-04-18/html/sor-dors64-eng.html, https://northstarit.ca/learn/bc-pipa-vs-pipeda/

## What tenant data the code would keep, and the risky pieces
Ordinary data:
- Trips: route, date, passenger counts including infants and seniors, vehicle type and height (models.py:165-196). Kept in config.json, with alerted.json next to it (README.md:62). Dated itineraries are personal information: they reveal when someone is away.
- Alert identifiers: Pushover keys (settings.py:31-32, 43-45).

Risky pieces:
1. BC Ferries username and password held in process memory and env (settings.py:40-42, 68).
2. Failure screenshots of the live BC Ferries page, newest 20 kept (site.py:407-422; README.md:69). In a multi-tenant setup these can capture a stranger's name, booking or account page.
3. The VNC checkout: users type card details into Chromium inside our pod (README.md:36-37; the cart belongs to the browser session). Cardholder data then passes through Xvnc and our Chromium. That may bring PCI DSS scope as a service provider even though Stripe never sees BC Ferries payments. A QSA should answer this.

KasmVNC's DLP logging defaults to off. Its config warns that verbose logging "LOGS YOUR PRIVATE INFORMATION. Keypresses and clipboard content" (kasmtech/KasmVNC unix/kasmvnc_defaults.yaml).

For our own fee, Stripe Checkout keeps card data off our servers. Stripe says card brand, last 4 digits and expiry are outside PCI scope and can be stored.
refs: src/ferry_watch/models.py:165-196, README.md:62, README.md:69, README.md:36-37, src/ferry_watch/bcferries/site.py:407-422, src/ferry_watch/settings.py:40-42, https://github.com/kasmtech/KasmVNC/blob/master/unix/kasmvnc_defaults.yaml, https://docs.stripe.com/security/guide

## Stripe pricing, tax and underwriting for a small fee
Card fees (MEDIUM, third-party sources): Canadian domestic cards cost 2.9% + C$0.30. A C$2 fee loses about C$0.36 (18%), so the fixed $0.30 should be spread across a larger purchase.

Tax (Stripe docs, HIGH):
- GST/HST registration is required once taxable sales pass C$30,000 over four consecutive quarters. Canadian businesses use the normal regime.
- BC PST is administered separately. Stripe lists its registration threshold as C$10,000 per rolling 12 months, and Stripe can't register you for it.
- Whether BC PST applies to this service is a question for an accountant.

Plumbing (Stripe docs, HIGH): Checkout plus the Customer Portal can cover quantity or seat changes and cancellation. Access should follow customer.subscription.created, .updated and .deleted (or checkout.session.completed for one-off payments). Handlers must be idempotent by event ID, because Stripe retries delivery for up to 3 days.

Underwriting (MEDIUM): Stripe's FAQ lists "travel reservation services and clubs" among the travel businesses it treats as high risk and does not support. The business should be described accurately as alert/monitoring software that takes no fare payments.
refs: https://docs.stripe.com/tax/supported-countries/canada/collect-tax?tax-jurisdiction-canada=federal, https://docs.stripe.com/tax/supported-countries/canada/collect-tax?tax-jurisdiction-canada=british-columbia, https://docs.stripe.com/billing/subscriptions/build-subscriptions?payment-ui=checkout&ui=embedded-page, https://docs.stripe.com/customer-management/configure-portal, https://docs.stripe.com/events/manage-webhook-endpoints, https://support.stripe.com/questions/prohibited-and-restricted-business-list-faqs

## (e) Chromium's own sandbox is off today, and Pod Security 'restricted' can still run it with it on
Playwright's `chromiumSandbox` "Defaults to `false`" (microsoft/playwright docs/src/api/params.md:1201-1204). The launch call passes only window args and never enables it (site.py:154-157). So pages from bcferries.com and its third-party scripts render with no renderer sandbox.

The image already runs as non-root pwuser (Dockerfile:65). Playwright's guidance for untrusted sites is a non-root user plus seccomp_profile.json: Docker's default profile with clone, setns and unshare allowed (docs/src/docker.md:62-87).

Pod Security 'restricted' requires seccompProfile type RuntimeDefault or Localhost (kubernetes/website pod-security-standards.md:435-449), so a Localhost profile is allowed.

Another route is pod user namespaces (`hostUsers: false`). The UserNamespacesSupport feature was beta and on by default in 1.33–1.35 and is stable and locked on from 1.36.

The root filesystem can already be read-only, with writable /tmp and /config (README.md:88-89).
refs: https://github.com/microsoft/playwright/blob/main/docs/src/api/params.md, https://github.com/microsoft/playwright/blob/main/docs/src/docker.md, src/ferry_watch/bcferries/site.py:154-157, Dockerfile:65, README.md:88-89, https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/security/pod-security-standards.md, https://github.com/kubernetes/website/blob/main/content/en/docs/reference/command-line-tools-reference/feature-gates/UserNamespacesSupport.md

## gVisor and Kata compatibility with Chromium and Xvnc is unverified
gVisor: I found no authoritative statement that Chromium's namespace/seccomp sandbox works under runsc. gVisor's compatibility guide says it implements a subset of the syscall ABI. A related report shows Chromium failing to set up its sandbox under hardened namespaces (NVIDIA/OpenShell #4098; not gVisor). Treat this as unknown (LOW) until tested. Xvnc and KasmVNC's WebSocket need only ordinary sockets and are probably fine, also untested.

Kata (MEDIUM): each pod is a real VM, so Chromium behaves as on a normal VM. But nodes need /dev/kvm: bare metal or instance types with nested virtualization. AWS's EKS walkthrough uses bare metal for this reason. VM memory is reserved per pod, which costs a lot at one headed browser per trip per user.
refs: https://github.com/google/gvisor/blob/master/g3doc/user_guide/compatibility.md, https://github.com/NVIDIA/OpenShell/issues/4098, https://aws.amazon.com/blogs/containers/enhancing-kubernetes-workload-isolation-and-security-using-kata-containers/, https://cloudfleet.ai/docs/workload-isolation/kata-containers/

## Abuse paths created by renting strangers a browser on our IP
1. Card testing at BC Ferries checkout through the VNC browser. Our egress IP takes the fraud signals, and BC Ferries, Queue-it or Akamai may block it for every tenant. Gating VNC to held-cart windows (decided) narrows this but doesn't remove it.
2. Load-based blocking. N users × 6 trips × a reload every 15–30 s (models.py:16, 19, 284) comes from one data-centre ASN, which Queue-it can rule against.
3. Card testing against our own Checkout page. A small fee is an attractive card-testing target. Stripe recommends hosted Checkout (built-in rate limits, CAPTCHA), requiring login before checkout, and Radar velocity rules.
4. Arbitrary browsing. The headed Chromium has an address bar, and outbound traffic isn't restricted beyond the KasmVNC viewer, so a user could browse anything from our IP.

KasmVNC can help: DLP settings can turn clipboard directions off, rate-limit the keyboard, and add a per-user watermark (unix/kasmvnc_defaults.yaml).
refs: src/ferry_watch/models.py:16, src/ferry_watch/models.py:19, https://docs.stripe.com/disputes/prevention/card-testing, https://queue-it.com/developers/bots-abuse-management/, https://github.com/kasmtech/KasmVNC/blob/master/unix/kasmvnc_defaults.yaml

RECOMMENDATIONS:
- Before taking any money, have a Canadian lawyer read the full bcferries.com website terms of use, account terms, Conditions of Carriage and robots.txt. Have them check: (i) automated access, scraping and commercial-use clauses; (ii) whether auto-carting for paying users is 'procuring' bookings from an 'unauthorized source' or brokering; (iii) using customers' BC Ferries accounts on their behalf; (iv) BC Ticket Sales Act scope; (v) trademark use of 'BC Ferries'; (vi) your liability if BC Ferries cancels a booking. Consider asking BC Ferries for permission or a partnership: they now run competing alerts and an auto-booking waitlist. This is not legal advice.
- Price it as monitoring software, never per booking. No success fees, no markup on fares. Users use their own BC Ferries accounts and pay BC Ferries directly. Add a prominent 'not affiliated with BC Ferries' disclaimer. Offer an alert-only tier, and make auto-hold an opt-in with hard limits: one held cart per user at a time, a much shorter hold than the 60-minute default in settings.py:30, and a daily cap on carts.
- Be a polite client at scale. Raise the interval floor for tenants well above MIN_INTERVAL_SECONDS=15 (models.py:19). Enforce a global request budget per egress IP and per route in the operator, and collapse identical searches where possible. Reconsider the spoofed Mac user agent (site.py:44-47). Never rotate or residential-proxy IPs to get around a block; that turns a terms dispute into circumvention.
- Don't collect BC Ferries passwords. Default to a guest cart that the user checks out themselves. If a login is needed for saved cards, the user types it into the pod's own browser during a short VNC window. Keep the Chromium profile on tmpfs, never let credentials or cookies pass through the operator, and wipe them when the pod is recycled. This needs a design change, because the login has to happen before the cart exists (README.md:124-125).
- Move alert delivery into the operator as a multi-channel dispatcher, so browser pods only report events and never hold the Pushover app token (notify.py:53-71). Channels: Web Push as the default (add a service worker, VAPID keys, and a Home Screen install hint for iOS 16.4+); email for account mail and as fallback; Pushover as an optional bring-your-own via the Web-based Subscription flow, with users paying their own $4.99 licence; SMS as a later paid add-on. Watch Pushover's limit response headers, since the 10k/month pool is account-wide.
- Identity: keep Authelia for admins if you like, but put customers behind something that supports self-sign-up. The simplest is oauth2-proxy with Google and GitHub; alternatives are Zitadel (passwords, passkeys, Apple; pin ≥4.12.1) or Pocket ID with ALLOW_USER_SIGNUPS=open (passkeys only). Key each tenant by issuer plus subject, not email.
- Trust identity only from the proxy. Use a NetworkPolicy so only proxy pods can reach the operator, configure oauth2-proxy's --trusted-proxy-ip, and strip or overwrite any client-supplied Remote-* or X-Forwarded-* headers. Better still, have the operator verify a signed ID token or JWT (issuer and audience), or use mTLS between proxy and operator. Enforce ownership of each trip and pod on every API call, WebSocket and VNC WebSocket.
- Stripe: use hosted Checkout plus the Customer Portal, and set client_reference_id or metadata to the tenant ID. Grant and revoke watch slots only from verified webhooks (signature over the raw body, idempotent by event ID). The webhook path should bypass the login proxy but nothing else. Price so the C$0.30 fixed fee is spread out: a monthly plan with N slots, or a multi-week pass. Enable Stripe Tax and track the C$30k GST/HST and BC PST thresholds with an accountant. Describe the business to Stripe as alert software.
- Privacy: write a privacy policy that names the processors and cross-border transfers (Stripe, Apple and Google push services, Pushover, email provider). Name an accountable person and keep a breach log, following PIPEDA's process. Delete trips automatically N days after the sailing date. Turn off failure screenshots for tenants, or keep them per tenant with a short time-to-live (site.py:407-422). Ship account deletion (tear down pods; remove trips, alert keys and events; delete or anonymise the Stripe Customer while keeping invoices as tax law requires) and a JSON data export. Building these now also prepares for Bill C-36's disposal right.
- Isolation: turn on Chromium's sandbox (chromium_sandbox=True). Run Pod Security 'restricted' with a Localhost seccomp profile derived from Playwright's (adds clone, setns, unshare), and/or hostUsers:false on Kubernetes 1.33+. Also: automountServiceAccountToken off; read-only root filesystem; memory and CPU limits; a separate tainted node pool for browser pods. Run a gVisor spike that tests the Chromium sandbox, Xvnc and the KasmVNC WebSocket before committing; use Kata only if your nodes have nested virtualization.
- Network and abuse controls: deny all ingress and egress by default. Allow only proxy → pod :6080 and pod → operator, and route egress through an allowlisting proxy (bcferries.com, queue-it.net, plus the CDNs and payment hosts you discover). Block the cloud metadata address. Lock Chromium to bcferries.com with URLBlocklist/URLAllowlist policies. Keep KasmVNC logging off, rate-limit its keyboard and watermark sessions with the tenant ID. Watch for repeated payment failures at checkout and end the session. Require a paid, logged-in account before any pod starts. Keep a per-tenant kill switch.

RISKS:
- BC Ferries could treat paid auto-carting as a terms violation or 'unauthorized source' booking, then cancel users' bookings, block the egress IP or ASN, or send a legal demand. That risk is higher now that it runs its own fair-queue alerts and waitlist. The clause on automated access is still unverified.
- Holding carts for an hour across many tenants looks like inventory hoarding and scalping even without resale, which is reputational and enforcement risk.
- Collecting customers' BC Ferries passwords creates credential-sharing liability and a high-value breach target, and contradicts the rule that the operator never sees tenant secrets.
- Cardholder data typed through our VNC-hosted Chromium may put the operator in PCI DSS scope (unverified; ask a QSA). Verbose KasmVNC logging would record keystrokes.
- Without per-trip authorization on the API and VNC, one tenant can reach another tenant's browser or cart, because KasmVNC itself has no auth (entrypoint.sh:22-23).
- Identity headers can be spoofed if the operator is reachable by anything other than the proxy, or if oauth2-proxy's --trusted-proxy-ip is left at its default of trusting all IPs.
- Chromium runs with its renderer sandbox off (Playwright default), so a browser exploit lands straight in the pod. Combined with open egress or metadata access, that could pivot across tenants.
- Pushover's 10k/month account-wide pool and per-user licences make it a poor default. One misbehaving pod holding the token could exhaust alerts for everyone.
- iOS Web Push only works after a Home Screen install and has reported reliability gaps, so time-critical cart alerts may be missed without a second channel.
- Stripe may classify the business as a travel reservation service (high-risk or unsupported) and freeze the account. Small fees also attract card testing.
- Tax: GST/HST and possibly BC PST obligations once thresholds are crossed. Collecting tax before registration, or missing registration, is a compliance problem.
- gVisor or Kata may not run headed Chromium with its sandbox plus Xvnc without tuning. Kata needs nested-virtualization or bare-metal nodes and reserves VM memory per pod, raising cost per watched trip.

OPEN_QUESTIONS:
- Will you offer auto-cart to paying users at all, or launch alert-only and add auto-hold only after legal review or contact with BC Ferries?
- What should the paid unit be: per watched trip (one-time), a monthly plan with N watch slots, or prepaid credits? And what happens if nothing opens: refund or not?
- Are you willing to approach BC Ferries for permission or a partnership before launch?
- Which checkout model: guest carts only (no BC Ferries login, card typed via VNC, PCI question), or a user-performed login in the pod for saved cards (needs a VNC window before any cart exists)?
- What is the maximum cart hold and the per-user cart limits (per day, concurrent) you'll accept for fairness?
- Which identity provider for customers (oauth2-proxy with Google/GitHub, Zitadel, Pocket ID, authentik), and should admins stay on Authelia?
- Which alert channels at launch: Web Push plus email only, or also bring-your-own Pushover and paid SMS?
- Retention: how long are trips, events, payment references and failure screenshots kept after a sailing date, and do tenants get screenshots at all?
- Which legal entity and province is the seller, and who is the accountable privacy contact? This affects PIPA vs PIPEDA, tax registration and Stripe underwriting.
- Budget and hosting: can you get nodes with nested virtualization for Kata, or should the plan be sandboxed Chromium plus gVisor (after a spike) on a dedicated node pool?
