===== browser-lockdown
Browser lockdown for per-trip headed-browser pods. Recommended layers, strongest first:

1. **Network egress allowlist.** Default-deny egress on the browser pod. Its only routes out are a shared forward proxy with a domain allowlist and the operator's agent port. This is the only control that stops proxy/VPN use, streaming, crypto-mining pools and general browsing no matter how the user gets out of the UI.
2. **Time-gated VNC.** Access is allowed only while the trip holds a cart or needs a manual login. Revoking it closes live streams with `kasmvncconfig -disconnect`, and the pod is deleted and recreated after every cart.
3. **Hard caps the operator enforces itself.** Admin-set maximum cart hold (0 is not allowed), VNC minutes per cart and per day, KasmVNC IdleTimeout and MaxConnectionTime, and the pod's activeDeadlineSeconds.
4. **Chromium managed policies** (URL allowlist and the feature-off policies).
5. **Kiosk UI plus an agent watchdog** that closes pages the agent didn't open.
6. **KasmVNC hardening flags** (clipboard limits, client-settings lock, no sharing).
7. **Abuse signals** that trigger automatic revocation.

**Gotchas found in the source code:**
- **Policy directory depends on the Playwright version.** Playwright 1.56 (pinned) ships a Chromium-branded build, which reads /etc/chromium/policies. Playwright 1.57+ switched to Chrome for Testing, which reads /etc/opt/chrome_for_testing/policies. Linux arm64 followed in 1.63. A Playwright bump would silently drop the lockdown unless policies are installed in both directories and checked at startup.
- **Two policies break Playwright.** `RemoteDebuggingAllowed=false` and `DeveloperToolsAvailability=2` both block Playwright's CDP pipe. Use `--kiosk` instead: it blocks the DevTools window but leaves the CDP pipe working.
- **URLBlocklist only controls navigations** (top-level pages and iframes), not subresources or fetch/XHR, so it is no substitute for egress filtering. From Chrome 147 the `*` wildcard no longer covers chrome:// pages; they must be listed explicitly.
- **Kiosk mode leaves shortcuts working on Linux:** Ctrl+T, Ctrl+N and Ctrl+O still work. Policies plus the watchdog have to catch them.
- **Today Chromium inherits the app's whole environment, including `BCF_PASS`.** It also runs without the sandbox and with popup blocking off (Playwright defaults).

**Network limits during research:** kasmweb.com, bcferries.com, chromeenterprise.google, playwright.dev, docs.cilium.io and kubernetes.io were all refused by this session's egress proxy. Facts come from upstream source and docs read on GitHub instead: KasmVNC v1.5.0 tag, chromium/chromium main and 141.0.7390.37, microsoft/playwright v1.56.0 and main, cilium/cilium, kubernetes/website, squid-cache. Web search was used for BC Ferries' payment provider and Calico. Confidence is marked on each claim. The full list of third-party domains BC Ferries checkout needs could NOT be verified, so a recorded checkout run is required before turning on enforcement.

## Baseline today: nothing stops the VNC user browsing anywhere
[verified-source]

**How Chromium is launched now:**
- `_launch` passes only window-size and position args (site.py:154-157).
- Every trip is a context in ONE shared browser (site.py:102-103, 167-176).
- The full Chromium UI (omnibox, tabs, menus) is drawn inside the matchbox fullscreen window (entrypoint.sh:33-34). A VNC user can type any URL, open tabs, open DevTools, or open file:// pages.
- Playwright's defaults add `--disable-popup-blocking` (chromiumSwitches.ts:67) and `--no-sandbox` (chromium.ts:324-325; `chromiumSandbox` defaults to false, params.md:1034).
- Chromium inherits the whole process environment (params.md:246-252 "Defaults to process.env"). That includes BCF_USER/BCF_PASS/PUSHOVER_* read from the environment (settings.py:35-42), so they are probably readable at file:///proc/<pid>/environ unless file:// is blocked (inferred, not tested).

**How KasmVNC is run now:**
- No password, no auth, shared sessions: `-DisableBasicAuth -SecurityTypes None ... -AlwaysShared` (entrypoint.sh:20-26).
- The VNC link is one static URL (settings.py:24,63) put into every cart alert (notify.py:239-244).
- A cart hold of 0 means held until released (settings.py:30,53-54; watcher.py:498), so a VNC session can stay open indefinitely.

**Already in the codebase:**
- The tests already use an in-browser DNS fence: `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1` (tests/browser/test_site_flow.py:157-162).
refs: src/ferry_watch/bcferries/site.py:102-103, src/ferry_watch/bcferries/site.py:149-160, src/ferry_watch/bcferries/site.py:167-176, entrypoint.sh:20-26, entrypoint.sh:33-34, src/ferry_watch/settings.py:24, src/ferry_watch/settings.py:30, src/ferry_watch/settings.py:35-42

## (a) Egress: plain NetworkPolicy can't filter by domain, so use default-deny plus a forward proxy
[doc] Kubernetes NetworkPolicy cannot:
- match FQDNs or anything TLS-related;
- log what it blocks;
- reliably cut connections that already exist when a policy changes ("implementation defined", network-policies.md L444-478).

**Recommended pattern:**
- Browser pods get default-deny egress. They may reach only:
  - a shared egress proxy Service (Squid or Envoy, in its own namespace);
  - the operator's agent listener (NetworkPolicy works at L4, so put the agent API on its own port, separate from the dashboard/admin API);
  - kube-dns, if Service names are used.
- Nothing else is reachable, so cluster pods, the Kubernetes API Service IP and 169.254.169.254 are blocked by default.
- Squid: use `acl allowed dstdomain -n .bcferries.com .queue-it.net ...`. The `-n` stops reverse-DNS matching of IP-literal URLs (cf.data.pre L1217-1226).
- The proxy itself must deny RFC1918, 100.64/10, 169.254/16, 127/8 and the cluster/Service CIDRs. Otherwise a browser → proxy → internal hop (SSRF or DNS rebinding) gets around the pod policy.
- Proxy pods get egress to the world minus those ranges.
- Squid access logs give per-pod host and byte counts. `delay_pools` can cap bandwidth per client (cf.data.pre L7745).
- TLS is not intercepted, so the proxy sees hostnames and byte counts only, never cookies or passwords. This fits the 'operator never sees secrets' rule.

**Pointing Chromium at the proxy** (two options):
- Playwright's `proxy=` launch option, which becomes `--proxy-server` plus a `<-loopback>` bypass (chromium.ts L331-345).
- The `ProxySettings` policy, which locks the setting and "ignore[s] all proxy-related options specified from the command line" (ProxySettings.yaml). Set one, or keep both identical.

**Sidecar proxy vs shared proxy:** a sidecar proxy shares the pod's network namespace, so per-pod NetworkPolicy can't tell Chromium's traffic from the sidecar's. That would need iptables owner-match plus NET_ADMIN. The shared gateway is cleaner.

**Cilium option** (strong second fence if Cilium is the CNI) [doc]:
- `toFQDNs` `matchName`/`matchPattern` allow traffic to IPs learned through Cilium's DNS proxy. They need `--enable-l7-proxy` and a `rules.dns` block (layer3.rst L500-560).
- TLS SNI allowlisting via `serverNames` also needs the L7 proxy (layer4.rst L158-180).
- Caveat: allowing by IP lets through other sites on the same CDN IPs. SNI rules close most of that gap.

**Calico option** [medium-low confidence]: domain-based egress policy is documented for Calico Cloud/Enterprise. I found no evidence it exists in Calico OSS.

**Domains checkout needs** [low confidence; bcferries.com was blocked from this sandbox]:
- Known from code: www.bcferries.com (site.py:42); Queue-it via queue-it.net redirects (site.py:43, 240-249).
- BC Ferries says payments go through Moneris with 3-D Secure 2.0. The issuer's ACS challenge pages (each bank's own domain) load in iframes/navigations, so they need both proxy allowlisting AND URLAllowlist entries. The set of issuer domains is open-ended.
- So: run a learning phase (log-only proxy, real checkouts with Visa/Mastercard/Amex from several Canadian issuers), then enforce. Alert on hosts denied while a cart is held, and keep the allowlist in a hot-reloadable ConfigMap.
- Too tight an allowlist breaks the 3DS challenge, and the booking with it.
refs: https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/services-networking/network-policies.md, https://github.com/squid-cache/squid/blob/master/src/cf.data.pre, https://github.com/cilium/cilium/blob/main/Documentation/security/policy/layer3.rst, https://github.com/cilium/cilium/blob/main/Documentation/security/policy/layer4.rst, https://docs.tigera.io/calico-cloud/network-policy/domain-based-policy, https://www.bcferries.com/payment-protection, https://developer.moneris.com/hosted-solutions/docs/hosted-tokenization-introduction, https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Miscellaneous/ProxySettings.yaml

## (b) Which policy directory Playwright's Chromium reads depends on the Playwright version and CPU architecture
[verified-source] `policy_paths.cc` (identical in 141.0.7390.37 and main) sets `kPolicyPath` by build branding:
- Google Chrome: /etc/opt/chrome/policies
- Chrome for Testing: /etc/opt/chrome_for_testing/policies
- otherwise (Chromium): /etc/chromium/policies

Managed policies go in the `managed/` subdirectory.

**Which build each Playwright version ships:**
- 1.56 (pinned: Dockerfile:14, uv.lock:585-586) runs the `chromium` executable when headed (chromium.ts `getExecutableName`). That is a Chromium-branded build, so it reads **/etc/chromium/policies/managed**.
- 1.57 release notes: "Playwright now runs on Chrome for Testing builds rather than Chromium" (headed uses `chrome`), so it reads **/etc/opt/chrome_for_testing/policies/managed**. Linux arm64 stayed on Chromium until 1.63 moved it to Chrome for Testing as well.

The image builds both amd64 and arm64 (Dockerfile:19-26). A Renovate bump (renovate.json5 exists) would silently drop every policy.

**Fix:**
- Install the same JSON (root-owned, read-only) into all three `managed/` directories.
- At startup, have the agent check enforcement: navigate to a non-allowlisted URL and require `net::ERR_BLOCKED_BY_ADMINISTRATOR` (navigation throttle L79-98).
- Refuse to open VNC if the check fails.
- Policies are `dynamic_refresh: true`, so a ConfigMap mount can update them without a restart.
refs: https://github.com/chromium/chromium/blob/141.0.7390.37/components/policy/core/common/policy_paths.cc, https://github.com/chromium/chromium/blob/main/components/policy/core/common/policy_paths.cc#L15-L23, https://github.com/microsoft/playwright/blob/main/docs/src/release-notes-python.md, https://github.com/microsoft/playwright/blob/v1.56.0/packages/playwright-core/src/server/chromium/chromium.ts#L353-L358, Dockerfile:12-14, Dockerfile:19-26, uv.lock:585-586

## (b) Two policies would break Playwright; kiosk mode blocks the DevTools window instead
[verified-source + doc]

**`RemoteDebuggingAllowed=false` breaks launch.** The policy description says it controls `--remote-debugging-port` and `--remote-debugging-pipe`, and Playwright always uses `--remote-debugging-pipe` (chromium.ts L286-289).

**`DeveloperToolsAvailability=2` breaks automation.** Its description now says it "affects ... direct CDP access (... --remote-debugging-pipe)". In main, `ChromeDevToolsManagerDelegate::AllowInspectingTarget` → `IsInspectionAllowed` → `kPolicyDisallowed`. In 141, `AllowInspectingRenderFrameHost` also routes through `IsInspectionAllowed`. Expect Playwright to stop being able to drive pages (high confidence from source; not run here because the Playwright Chromium download was blocked).

**DevTools allow/block lists don't help either.** They are URL-based, so they would also block Playwright on bcferries.com.

**Use these instead:**
- **`--kiosk` (preferred).** `DevToolsWindow` refuses the DevTools UI in kiosk mode: 141 `AllowDevToolsFor` L1224-1231; main `GetProfileBlockReason` returns `kKioskMode`, L1340-1345. CDP over the pipe is untouched.
- **URLBlocklist containing devtools://** also blocks only the UI (main L1347-1352). Google's policy text discourages this ("can lead to unexpected errors"); with kiosk it is optional.

**Also needed:** `view-source:*` in URLBlocklist. The DevTools policy text says it's needed, and the navigation throttle checks view-source separately (L62-76, L102-112).
refs: https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Miscellaneous/RemoteDebuggingAllowed.yaml, https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Miscellaneous/DeveloperToolsAvailability.yaml, https://github.com/chromium/chromium/blob/main/chrome/browser/devtools/chrome_devtools_manager_delegate.cc#L407-L425, https://github.com/chromium/chromium/blob/main/chrome/browser/devtools/devtools_availability_checker.cc#L381-L390, https://github.com/chromium/chromium/blob/main/chrome/browser/devtools/devtools_window.cc#L1340-L1372, https://github.com/chromium/chromium/blob/141.0.7390.37/chrome/browser/devtools/devtools_window.cc#L1224-L1232, https://github.com/chromium/chromium/blob/141.0.7390.37/chrome/browser/devtools/chrome_devtools_manager_delegate.cc#L307-L333, https://github.com/chromium/chromium/blob/main/components/policy/content/policy_blocklist_navigation_throttle.cc#L62-L120

## (b) What URLBlocklist/URLAllowlist actually cover, and the Chrome 147 change
[verified-source + doc] What the URL blocklist does:
- It blocks navigations, including redirects and iframes (`WillStartOrRedirectRequest`, throttle L79-120).
- It does NOT block subresources or XHR/fetch. The policy says so: "does not apply to in-page JavaScript ... XMLHTTPRequest". So it is a UI fence, not an egress control.
- `blob:` is always exempt (blocklist manager L402-407; throttle L84-86).
- The bare `*` doesn't cover chrome-extension, chrome-native or chrome-search (L58-67).
- **Chrome 147 change:** "the wildcard * on its own does not apply to internal chrome:// URLs" (also a feature flag in the code, L119-123). Chromium 141 (Playwright 1.56) still blocks chrome:// with `*`, but list `chrome://*` explicitly for future builds. That blocks chrome://flags (whose Relaunch would cut Playwright's pipe), settings, inspect and net-export.
- `javascript://*` only affects the omnibox and bookmarklets.

**Compatibility with Playwright:**
- The agent only goes to BASE_URL pages (site.py:72-87, 278, 358, 401) and the Queue-it redirect (site.py:240-249). An allowlist of bcferries.com, queue-it.net, the payment/3DS hosts and `about:blank` (Playwright's initial page) fits.
- A missing Queue-it or 3DS host shows up as `ERR_BLOCKED_BY_ADMINISTRATOR`. Today the flow would surface that as BookingError/QueueTimeout.

**CI caveat:** the browser contract tests run against 127.0.0.1 (tests/browser/test_site_flow.py:157-162), and CI smoke-tests the image (README.md:116). Mount the policies at runtime (ConfigMap) rather than baking them into the image, or allowlist 127.0.0.1 only in test.
refs: https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Miscellaneous/URLBlocklist.yaml, https://github.com/chromium/chromium/blob/main/components/policy/core/browser/url_list/url_blocklist_manager.cc#L58-L123, https://github.com/chromium/chromium/blob/main/components/policy/core/browser/url_list/url_blocklist_manager.cc#L270-L291, https://github.com/chromium/chromium/blob/main/components/policy/content/policy_blocklist_navigation_throttle.cc#L79-L120, https://support.google.com/chrome/a/answer/9942583, src/ferry_watch/bcferries/site.py:72-87, src/ferry_watch/bcferries/site.py:240-249, tests/browser/test_site_flow.py:157-162

## (b) Recommended managed-policy JSON (feature lockdown)
[doc; policy names verified against chromium policy_definitions YAML]

```
{"URLBlocklist":["*","chrome://*","chrome-untrusted://*","view-source:*","file://*","javascript://*"],
"URLAllowlist":["about:blank","bcferries.com","queue-it.net",<moneris/3DS/ACS hosts from learning run>],
"IncognitoModeAvailability":1,"BrowserGuestModeEnabled":false,"BrowserAddPersonEnabled":false,"BrowserSignin":0,"SyncDisabled":true,
"DownloadRestrictions":3,"AllowFileSelectionDialogs":false,"PrintingEnabled":false,
"PasswordManagerEnabled":false,"AutofillCreditCardEnabled":false,"AutofillAddressEnabled":false,
"ExtensionInstallBlocklist":["*"],"DefaultFileSystemReadGuardSetting":2,"DefaultFileSystemWriteGuardSetting":2,"DefaultWebUsbGuardSetting":2,"DefaultNotificationsSetting":2,"DefaultClipboardSetting":2,
"AudioCaptureAllowed":false,"VideoCaptureAllowed":false,"ScreenCaptureAllowed":false,
"WebRtcIPHandling":"disable_non_proxied_udp","QuicAllowed":false,"DnsOverHttpsMode":"off",
"DefaultSearchProviderEnabled":false,"SearchSuggestEnabled":false,"NewTabPageLocation":"about:blank","BookmarkBarEnabled":false,"EditBookmarksEnabled":false,"TranslateEnabled":false,
"DefaultPopupsSetting":2,"PopupsAllowedForUrls":["[*.]bcferries.com"],
"ProxySettings":{"ProxyMode":"fixed_servers","ProxyServer":"http://<egress-proxy>:3128","ProxyBypassList":"<-loopback>"}}
```

**Do NOT set** `RemoteDebuggingAllowed` or `DeveloperToolsAvailability` (see the previous finding).

**Popup settings:** `DefaultPopupsSetting` only works if Playwright's `--disable-popup-blocking` is dropped with `ignore_default_args`.

**Why the password and autofill settings:**
- PasswordManager and Autofill off keep Chromium from saving the BC Ferries password or card numbers in the profile.
- `DefaultClipboardSetting=2` only blocks the async clipboard API for web pages, not the user's own copy and paste.
- `AllowFileSelectionDialogs=false` stops uploads from the pod filesystem. BC Ferries checkout shouldn't need any (unverified).
refs: https://github.com/chromium/chromium/tree/main/components/policy/resources/templates/policy_definitions, https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/ContentSettings/DefaultClipboardSetting.yaml, https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Miscellaneous/AllowFileSelectionDialogs.yaml, https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Miscellaneous/DownloadRestrictions.yaml, https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/WebRtc/WebRtcIPHandling.yaml

## (b) Playwright route() is a weak second fence; observing traffic is better
[doc]

**What `context.route` covers:**
- Every page in that context, including popups ("the popup will belong to the parent page's browser context", class-browsercontext.md L6, L193-197).
- But: "Enabling routing disables http cache" (L1170). Every BC Ferries asset would be refetched on each 30 s poll.
- It also doesn't see requests made by Service Workers (L1024) unless `service_workers='block'`.

**What it doesn't cover:** pages outside Playwright's contexts. That means Incognito, Guest and new-profile windows, or anything in the default browser context when using non-persistent launch with `--no-startup-window` (chromium.ts L290-293).

**Recommendation:**
- One trip per pod → use `launch_persistent_context` (user-data-dir on a memory emptyDir), so every tab or window the user opens lands in the one observed context.
- Disable Incognito, Guest and Add-person by policy.
- Use passive `context.on('request')`, `context.on('page')` and `framenavigated` for detection, and leave enforcement to the proxy and policy.
refs: https://github.com/microsoft/playwright/blob/v1.56.0/docs/src/api/class-browsercontext.md, https://github.com/microsoft/playwright/blob/v1.56.0/packages/playwright-core/src/server/chromium/chromium.ts#L283-L294

## (c) UI lockdown: --kiosk, which shortcuts still work, and --force-app-mode
[verified-source; how it behaves with Playwright on Linux/matchbox is untested]

**`--app=URL` doesn't fit Playwright.** Playwright opens pages over CDP in normal tabbed windows, and positional URL args are rejected (chromium.ts L305-307). So `--app` wouldn't apply to the agent's pages.

**What `--kiosk` does:**
- `IsRunningInAppMode` (app_mode_utils.cc L102-106) keeps the window permanently fullscreen; `ExitFullscreenModeInternal` returns early (fullscreen_controller.cc L653-657).
- It hides the toolbar and tab strip. `IDC_FOCUS_LOCATION` (Ctrl+L) is enabled only when the location bar is showing (browser_command_controller.cc L2431, L2453).
- It blocks the DevTools window.

**What `--kiosk` doesn't do on Linux:** it doesn't remove other shortcuts. ChromeOS-only locked fullscreen is what prunes commands (L2557+). Still working:
- Ctrl+T / Ctrl+N (new tab/window at the new-tab page)
- Ctrl+Shift+N (Incognito)
- Ctrl+O (file chooser → file://)
- Ctrl+P / Ctrl+S
- Ctrl+U (view-source)
- the right-click menu ("Open link in new tab/window", "Search <engine> for ...")

**How each escape is closed:**

| Escape | Closed by |
|---|---|
| Ctrl+L | kiosk |
| Ctrl+T / Ctrl+N | URLBlocklist incl. `chrome://*`, NewTabPageLocation=about:blank, agent closes unexpected pages |
| Ctrl+Shift+N | IncognitoModeAvailability=1 |
| Ctrl+O / file:// | AllowFileSelectionDialogs=false + `file://*` |
| Ctrl+U | `view-source:*` |
| javascript: | `javascript://*` |
| chrome://flags relaunch | `chrome://*` |
| F12 / Ctrl+Shift+I/J | kiosk |
| Search-for context item | DefaultSearchProviderEnabled=false |
| Popups | DefaultPopupsSetting + agent watchdog |

**`--force-app-mode`** is defined on all platforms (chrome_switches.h L385-388). When set, `BrowserView::LoadAccelerators` drops every keyboard shortcut except a fixed list: back/forward/reload/stop/cut/copy/paste/zoom/caret browsing/DevTools (browser_view.cc L5647-5660; app_mode_utils.cc L72-101). That would kill Ctrl+T/N/O/P/S/U at the source. But it is a ChromeOS-kiosk feature, so treat it as an experiment.

**Fallback if kiosk misbehaves:** matchbox already keeps one fullscreen window with no title bar (entrypoint.sh:33-34). KasmVNC's DLP_Region can black out and block clicks on a strip of the screen, such as the tab strip and omnibox (ServerCore.cxx L185-198). This is fragile with resizing, and keyboard shortcuts still go through.
refs: https://github.com/chromium/chromium/blob/main/chrome/browser/app_mode/app_mode_utils.cc#L72-L111, https://github.com/chromium/chromium/blob/main/chrome/browser/ui/views/frame/browser_view.cc#L5641-L5660, https://github.com/chromium/chromium/blob/main/chrome/browser/ui/browser_command_controller.cc#L2424-L2512, https://github.com/chromium/chromium/blob/main/chrome/browser/ui/exclusive_access/fullscreen_controller.cc#L650-L657, https://github.com/chromium/chromium/blob/main/chrome/common/chrome_switches.h#L385-L391, https://github.com/microsoft/playwright/blob/v1.56.0/packages/playwright-core/src/server/chromium/chromium.ts#L297-L308, entrypoint.sh:33-34, src/ferry_watch/watcher.py:351-356

## (d) KasmVNC v1.5.0 controls and exactly how they behave
[verified-source, KasmVNC v1.5.0 = the version pinned at Dockerfile:20]

The entrypoint runs Xvnc directly, so the YAML config is never read; settings must be passed as command-line flags. The `vncserver` script shows which YAML key maps to which flag (vncserver L2279-2307).

**Timeouts** (ServerCore.cxx L28-43, VNCServerST.cxx L395-455, VNCSConnectionST.cxx L543-575):
- **IdleTimeout:** drops ONE connection after N s without key or pointer input. The client keepalive keysym doesn't count. Minimum 15 s while authenticating.
- **MaxConnectionTime:** calls `exit(0)` on the whole Xvnc process once a client has been connected for N s. The clock starts when the first client connects to an empty server and resets only after everyone disconnects.
- **MaxDisconnectionTime:** exits after N s with no client. The clock counts from server start.
- **MaxIdleTime:** exits after N s without any user input. Also counts from server start.

All three Max* settings kill Xvnc, and `wait -n; exit 1` (entrypoint.sh:38-39) then takes the container down. So they are blunt reaping switches. MaxIdleTime and MaxDisconnectionTime would kill pods during normal unattended watching. Use only MaxConnectionTime, as a backstop.

**Clipboard:**
- `-AcceptCutText` / `-SendCutText` turn each direction on or off.
- `DLP_ClipAcceptMax`, `DLP_ClipSendMax`, `DLP_ClipDelay` and `DLP_ClipTypes` set size, rate and type limits (L93-99, L164-191).
- `DLP_KeyRateLimit` caps key presses per second.
- `DLP_Log=verbose` logs keystrokes and clipboard content (defaults yaml L108-110). That would include card numbers, so never enable it.

**File transfer:**
- Xvnc has no upload channel. Unix relays need `-UnixRelay`, which isn't used.
- `/api/downloads` lists ~/Downloads but is owner-only (websocket.c L1666-1680, L2024-2040).
- The built-in web server serves only the `-httpd` directory. Never link a Downloads folder into /opt/vnc-web.

**Client-side settings:** `-IgnoreClientSettingsKasm` stops viewers raising quality or max video resolution (SMsgReader.cxx L176; ConnParams.cxx L164-165). Pair it with `-FrameRate` and `-MaxVideoResolution` to cap bandwidth.

**Sharing:** `-NeverShared` plus `-DisconnectClients` means a new connection kicks the old one; with `DisconnectClients=0` a second connection is refused instead (ServerCore.cxx L64-78). Either way nobody can co-view silently. Today `-AlwaysShared` is set.

**View-only:**
- With BasicAuth on, kasmpasswd users carry read/write/owner permissions.
- With `-DisableBasicAuth`, every client is read+write (VNCSConnectionST.cxx L1241-1249).
- Without it, a client that isn't a kasmpasswd user gets no rights at all: no frames and no input (VNCSConnectionST.cxx L91-108, L1006). A unix-socket client has no user, so the pod's Xvnc (`-rfbunixpath`, no WebSocket listener) needs `-DisableBasicAuth` for its one viewer to see or use the screen.
- Server-wide view-only is possible at runtime: `kasmvncconfig -set AcceptPointerEvents=0` (and `AcceptKeyEvents=0`, after adding it to `-AllowOverride`). The default AllowOverride list is "desktop,AcceptPointerEvents,SendCutText,AcceptCutText,SendPrimary,SetPrimary" (vncExtInit.cc L109-111, L572-575).

**Kicking viewers:**
- `kasmvncconfig -display :99 -disconnect` closes every client immediately (vncconfig.cxx L245-247; XserverDesktop.cc L465-468; the Debian package installs vncconfig as kasmvncconfig).
- With BasicAuth, kasmpasswd is watched with inotify (VNCServerST.cxx L239-248). Deleting a user closes their live sessions with "User was deleted"; downgrading their permissions applies live (VNCSConnectionST.cxx L1357-1371).

**Per-session passwords:** the owner-only HTTP API (create/remove/update user, get_screenshot) works only with BasicAuth and an owner user (websocket.c L2024-2040, L1349-1500). Never give owner credentials to the operator: `get_screenshot` would hand it the checkout screen.
refs: https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/ServerCore.cxx#L28-L198, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCServerST.cxx#L395-L455, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCSConnectionST.cxx#L543-L575, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCSConnectionST.cxx#L1241-L1371, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/VNCServerST.cxx#L232-L250, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/unix/vncconfig/vncconfig.cxx#L245-L280, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/unix/xserver/hw/vnc/XserverDesktop.cc#L465-L468, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/unix/xserver/hw/vnc/vncExtInit.cc#L109-L111

## (e) Gating VNC in time and cutting streams that are already open
[design; verified mechanisms]

**How access is checked:** an authenticating proxy (Authelia/oauth2-proxy) plus a forward-auth call to the operator. The operator allows the WebSocket upgrade only if:
- the identity owns the trip, and
- the trip is in CART_HELD or NEEDS_LOGIN, and
- a short-lived, single-use ticket bound to (user, trip, cart id) is valid.

This replaces the static BCF_VNC_URL (settings.py:63; notify.py:243).

**Gateways only check at the upgrade.** Changing a NetworkPolicy may not cut an established flow (network-policies.md L470-478). So revoking access means:
1. Deny new upgrades.
2. The agent runs `kasmvncconfig -disconnect` (plus `-set AcceptKeyEvents=0 AcceptPointerEvents=0`).
3. Backstop: the operator deletes the pod.

The agent lives in a pod the tenant drives, so it must not be the only enforcement point.

**Hook points in today's code:**
- Open: after a successful add-to-cart (watcher.py:498-507).
- Revoke: in `_service_cart` on release, expiry, window closed or lost (watcher.py:510-542).

**Recycle the pod after every cart:** a fresh pod with memory emptyDir for HOME/profile (entrypoint.sh:14-17 already supports a read-only root filesystem). Nothing from the user's VNC session survives.

**Hold caps:**
- Make cart hold an admin maximum; remove the "0 = until released" option (settings.py:30, 53-54).
- Add an operator-side cap on VNC minutes per cart and per day.
- KasmVNC `-IdleTimeout` (~300 s) and `-MaxConnectionTime` (hold cap + margin) as in-pod backstops.

**Keep the VNC stream out of the operator process:** keystrokes (card number, BC Ferries password during a manual login) flow through whatever proxies the WebSocket. Terminate TLS at the ingress (or pod), and let the operator answer only the authorization question.
refs: src/ferry_watch/watcher.py:498-507, src/ferry_watch/watcher.py:510-542, src/ferry_watch/settings.py:30, src/ferry_watch/settings.py:53-54, src/ferry_watch/settings.py:63, src/ferry_watch/notify.py:235-244, entrypoint.sh:14-17, https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/services-networking/network-policies.md

## (f) Abuse signals and where to collect them
[design]

**Proxy:**
- denied hosts per pod (any denial outside a held cart is suspicious; during a cart it may be a missing 3DS host);
- allowed bytes per pod;
- connection rate.

NetworkPolicy can't log, so Squid or Cilium Hubble is the source (network-policies.md L464).

**Agent (Playwright):**
- `context.on('page')` for pages the agent didn't create → close any not on the allowlist, cap extra pages at about 2, count them;
- `framenavigated` on the main frame leaving the allowlist;
- hosts seen in `context.on('request')`;
- `page.on('close')` and `page.on('crash')`, already wired (site.py:204-205).
- A browser-level CDP session (`browser.new_browser_cdp_session()` → `Target.setDiscoverTargets`) can spot devtools:// or other-context targets and close them (needs a test).

**KasmVNC:**
- connect/disconnect/idle lines in its log;
- `DLP_Log=info` for clipboard and keyboard counts (never verbose);
- the VNC connection length versus whether a cart is held.

**Kubernetes:**
- container CPU near its limit for a long time (mining);
- container_network_transmit_bytes (streaming over VNC);
- restarts (MaxConnectionTime exits).

**Responses:** revoke VNC, recycle the pod, notify an admin, and count strikes against the user's quota.
refs: src/ferry_watch/bcferries/site.py:204-205, src/ferry_watch/watcher.py:351-356, https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/services-networking/network-policies.md, https://github.com/kasmtech/KasmVNC/blob/v1.5.0/common/rfb/ServerCore.cxx#L181-L183

## Pod-level hardening that the lockdown relies on
[doc + inferred]

**Anything in the browser pod should be assumed readable by the tenant.** If the renderer is compromised (`--no-sandbox` by default) or file:// isn't blocked, they can read it. Therefore:
- `launch(env={...minimal...})` so Chromium doesn't inherit BCF_PASS or tokens (params.md L246-252).
- No platform-wide secrets (the Pushover app token, operator credentials) in browser pods.
- `automountServiceAccountToken: false`.
- The agent→operator token scoped to one trip. The operator must not trust what the agent reports when deciding VNC access or quotas: a tenant could forge "cart held" to keep VNC open.

**Sandbox:** turn on `chromium_sandbox=True` with Playwright's seccomp profile that allows user namespaces (docker.md L62-80), or run browser pods under a gVisor RuntimeClass.

**Resource and bandwidth caps:**
- CPU, memory and ephemeral-storage limits.
- `kubernetes.io/egress-bandwidth` to cap VNC/stream bandwidth (network-plugins.md L148-157; Cilium bandwidth-manager.rst L34-36).

**Screenshots:** debug screenshots (site.py:407-422) could capture the checkout page. Keep them in-pod, or disable them while a cart is held.
refs: src/ferry_watch/settings.py:35-42, src/ferry_watch/bcferries/site.py:407-422, https://github.com/microsoft/playwright/blob/v1.56.0/docs/src/api/params.md, https://github.com/microsoft/playwright/blob/v1.56.0/docs/src/docker.md, https://github.com/kubernetes/website/blob/main/content/en/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins.md, https://github.com/cilium/cilium/blob/main/Documentation/network/kubernetes/bandwidth-manager.rst

## Controls ranked by strength vs effort
1. **Egress default-deny + domain-allowlist proxy.** Strength: very high (defeats VPN/proxy use, streaming, mining pools and browsing whatever happens in the UI). Effort: medium. Main risk: breaking 3DS.
2. **Time-gated VNC + `kasmvncconfig -disconnect` + recycle the pod per cart.** Strength: high against 'keeping it forever'. Effort: medium.
3. **Operator-enforced caps** (maximum hold, VNC minutes, activeDeadlineSeconds, IdleTimeout/MaxConnectionTime). Strength: high. Effort: low.
4. **Chromium managed policies.** Strength: medium-high. Effort: low; the gotchas are the directory path, the policies that break Playwright, and the Chrome 147 change.
5. **`--kiosk` + persistent single context + agent page watchdog.** Strength: medium. Effort: low-medium.
6. **KasmVNC flags** (clipboard, NeverShared, IgnoreClientSettingsKasm, FrameRate). Strength: low-medium. Effort: trivial.
7. **Pod hardening** (env scrubbing, no SA token, sandbox/gVisor, limits, bandwidth annotation). Strength: medium. Effort: low-medium.
8. **Abuse telemetry and automatic response.** Detective control. Effort: medium.
9. **Playwright route() enforcement.** Low marginal value, and it costs the HTTP cache. Defer.
10. **Cilium toFQDNs/SNI** as a second fence. High value if Cilium is already the CNI, otherwise skip.
refs: 

RECOMMENDATIONS:
- 1. Egress fence first. Give browser pods default-deny egress. Allow only: the shared egress proxy Service port, a dedicated operator agent port (a different listener from the dashboard/admin API), and kube-dns if needed. Run Squid or Envoy in its own namespace with an allowlist (`dstdomain -n`). Explicitly deny private, link-local, metadata (169.254.169.254) and cluster CIDRs; give the proxy pod's own egress the same exclusions. Keep per-pod access logs and bandwidth limits (delay pools).
- 2. Before enforcing, run a learning phase: a log-only proxy and a few real checkouts through Moneris 3DS 2.0 with cards from several Canadian issuers, to collect every bcferries/Queue-it/CDN/Moneris/ACS host. Keep the allowlist in a ConfigMap that reloads without a restart. Alert the admin on any host denied while a cart is held, and keep the 'Book on bcferries.com' fallback link (notify.py:243) in cart alerts.
- 3. If the cluster runs Cilium, add a second fence with CiliumNetworkPolicy: toFQDNs (with a DNS rules block), TLS serverNames, and the kube-apiserver entity not allowed. Don't plan on Calico OSS DNS policy without confirming it exists in OSS.
- 4. Time-gate VNC. Replace BCF_VNC_URL with a per-trip URL and a short-lived ticket bound to (user, trip, cart id). At the WebSocket upgrade, ingress forward-auth asks the operator, which allows it only in CART_HELD or NEEDS_LOGIN. To revoke: deny new upgrades, have the agent run `kasmvncconfig -display :99 -disconnect` and set AcceptKeyEvents/AcceptPointerEvents to 0, and have the operator delete the pod as the authoritative backstop. Delete and recreate the browser pod after every cart release, expiry or checkout; keep the profile and HOME on a memory emptyDir.
- 5. Admin caps for reaping. Set a maximum cart hold (forbid 0, which today holds forever: settings.py:30, 53-54), maximum VNC minutes per cart and per user per day, and activeDeadlineSeconds on browser pods (e.g. 24 h, after which the operator recreates them). The operator enforces all of these from its own clock; it never relies on what the agent reports.
- 6. Chromium policies. Ship the managed-policy JSON from the findings, mounted read-only and root-owned, into /etc/chromium/policies/managed, /etc/opt/chrome_for_testing/policies/managed and /etc/opt/chrome/policies/managed, so a Playwright upgrade can't silently drop it. Do NOT set RemoteDebuggingAllowed=false or DeveloperToolsAvailability=2: both break Playwright's CDP pipe. Add `chrome://*` explicitly for Chrome 147+. Add an agent startup self-test (a non-allowlisted URL must fail with ERR_BLOCKED_BY_ADMINISTRATOR) and keep VNC closed if it fails.
- 7. Launch changes in `SiteBrowser._launch` (site.py:149-160): add `--kiosk` (hides the omnibox and tab strip, blocks the DevTools window); use `ignore_default_args=['--disable-popup-blocking']`, `env=` a minimal set (no BCF_PASS or tokens), `proxy=` or the ProxySettings policy, and `chromium_sandbox=True` (with Playwright's seccomp profile) or a gVisor RuntimeClass. With one trip per pod, switch to `launch_persistent_context` so every user-opened tab or window is in the observed context. Spike `--force-app-mode` as an experimental way to disable Ctrl+T/N/O/P/S/U.
- 8. Agent watchdog. Close any page the agent didn't create unless its URL is on the allowlist (cap extra pages at about 2). Close devtools:// and other-context targets found through a browser CDP session. Log main-frame navigations that leave the allowlist. Use passive request observation, not `context.route` (route disables the HTTP cache and misses Service Worker requests).
- 9. KasmVNC flags. Replace `-AlwaysShared` with `-NeverShared -DisconnectClients`. Add `-SendCutText=0 -DLP_ClipAcceptMax=1024 -IgnoreClientSettingsKasm -FrameRate 15 -IdleTimeout 300 -MaxConnectionTime <hold cap + margin> -AllowOverride AcceptPointerEvents,AcceptKeyEvents,AcceptCutText,SendCutText -DLP_Log info` (never verbose). Add a NetworkPolicy ingress rule so only the ingress controller reaches :6080. Never create an owner kasmpasswd user that the operator can read; per-session kasmpasswd users with inotify revocation are an optional later step.
- 10. Pod hardening. automountServiceAccountToken: false; restricted PodSecurity; readOnlyRootFilesystem; CPU, memory and ephemeral-storage limits; a kubernetes.io/egress-bandwidth annotation; no platform-wide secrets (Pushover app token, operator credentials) in browser pods; a trip-scoped agent token; debug screenshots off while a cart is held.
- 11. Abuse telemetry. Export from the proxy (denied hosts and bytes per pod), the agent (unexpected pages and navigations), KasmVNC (session length versus cart state) and cAdvisor (CPU, network). Auto-revoke, recycle the pod and notify an admin when thresholds trip; count strikes toward per-user quotas.

RISKS:
- Too tight an egress or URL allowlist breaks the Moneris 3-D Secure challenge (issuer ACS domains vary and aren't known in advance), so bookings fail at the last step. Mitigate with a learning run, alerts on hosts denied while a cart is held, a fast allowlist update path, and the direct bcferries.com fallback link.
- Policies silently stop applying after a Playwright bump: 1.57 moved amd64 to Chrome for Testing (/etc/opt/chrome_for_testing/policies) and 1.63 moved arm64. Renovate is configured. Mitigation: install into every policy directory and fail-closed on the startup self-test.
- Some lockdown policies break automation: RemoteDebuggingAllowed=false stops Playwright launching, and DeveloperToolsAvailability=2 (per source and policy text) blocks CDP inspection of page targets.
- From Chrome 147, URLBlocklist '*' no longer covers chrome:// URLs. Forgetting to list chrome://* reopens chrome://flags (whose Relaunch cuts Playwright's pipe), settings and inspect.
- On Linux, --kiosk does not disable Ctrl+T/N/O/P/S/U or the right-click menu. --force-app-mode is a ChromeOS-kiosk feature and may misbehave on desktop Linux; untested here because the Playwright Chromium download was blocked from this sandbox.
- KasmVNC MaxConnectionTime, MaxIdleTime and MaxDisconnectionTime exit Xvnc, and through entrypoint.sh's wait -n the whole container. A cap set too tight kills an in-progress checkout and loses the cart; MaxIdleTime and MaxDisconnectionTime would kill unattended watching pods.
- Gateways and NetworkPolicy generally don't cut WebSockets that are already open. Revocation depends on kasmvncconfig -disconnect run by an agent that sits in a tenant-reachable pod, so operator-side pod deletion must remain the authoritative kill switch.
- Whatever proxies the VNC WebSocket sees keystrokes (card numbers, the BC Ferries password during manual login). Routing VNC through operator code would break the 'operator never gets tenant secrets' requirement. An operator that can mint VNC tickets could also connect itself; NeverShared at least makes co-viewing visible to the user.
- Chromium runs with --no-sandbox by default and inherits the app's whole environment. Anything in the browser pod (agent token, BCF_PASS, any platform secret) should be treated as tenant-readable, and the agent's reports as forgeable (e.g. claiming 'cart held' to keep VNC open).
- Baking policies into the image would break the CI container smoke test and any in-image browser tests against 127.0.0.1 unless they're mounted only at runtime or 127.0.0.1 is allowlisted in test.
- The shared egress proxy becomes a single point of failure for every tenant's polling. Run replicas with a PodDisruptionBudget; the Squid access logs are also per-tenant browsing metadata (allowed hosts only).
- DLP_Log=verbose in KasmVNC would write keystrokes and clipboard contents, including card numbers, to logs.

OPEN_QUESTIONS:
- Which third-party hosts does BC Ferries checkout actually load: Moneris variant (Checkout vs Hosted Tokenization vs MPI), 3DS ACS domains for common Canadian issuers, CDNs, Queue-it asset hosts, required analytics? bcferries.com could not be reached from this sandbox, so this needs a recorded checkout session.
- Does BC Ferries open popups or new windows anywhere in booking or checkout (terms, 3DS, receipts)? This decides the DefaultPopupsSetting/PopupsAllowedForUrls entries and the watchdog's page cap.
- Does --kiosk (and optionally --force-app-mode) work with Playwright-created contexts under matchbox on Linux, and does URLBlocklist '*' block the DevTools window and the new-tab page in Chromium 141 / Chrome for Testing 14x as the source suggests? Needs a short spike in the real image.
- Is Cilium (or another FQDN-capable CNI) the cluster's network plugin, or must enforcement rely only on the forward proxy?
- Should users be able to watch the polling browser read-only outside the cart window (KasmVNC AcceptKeyEvents/AcceptPointerEvents=0), or is VNC closed entirely unless a cart is held or a manual login is needed?
- Where is TLS terminated for the VNC WebSocket (ingress vs pod), and is the ingress/auth proxy inside or outside the 'must never see tenant secrets' boundary? This needs agreement with the secrets-isolation design.
- Does the 'needs manual login' state (login failures, MFA or captcha) need interactive VNC, and how long should that window be capped at?
- Which Pushover credentials live where: per-tenant user keys in the operator and the app token only in the operator, never in browser pods?
