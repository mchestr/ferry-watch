<!-- Design for the streamed checkout viewer (beta, 0.7.0). The spikes behind it were run outside the repo; paths to their scripts and results are kept as written but are not part of the repo. -->

# Streamed checkout viewer (beta): design for Ferry Watch 0.7.0

Round 2: revised after the security and feasibility reviews. Every finding was checked, most with a spike of
our own on the pod's real screen. §15 lists each finding, the verdict, the evidence and where it is fixed. §16
lists the decisions that are the owner's.

The wire protocol is in [`PROTOCOL.md`](PROTOCOL.md) (normative). This document gives:
- the decisions and the evidence behind each;
- the security analysis;
- the plumbing;
- the tests;
- a build plan for three parallel workstreams, with a shared conformance pack and a midpoint integration
  checkpoint.

Every number comes from a spike on the pod's real screen (§3). The scripts and raw results are listed in §14.
Round-2 scripts live in `round2/`.

## 1. Summary

**What ships.**
- A per-user "New checkout viewer (beta)" switch in Settings, off by default.
- When an opted-in user opens a checkout (phone or computer), vnc-edge serves a small viewer of our own
  (`/fw/viewer.html`) instead of KasmVNC's client.
- The pod's agent streams the checkout page as JPEG images over the existing gate WebSocket (new subprotocol
  `fw.cast1`) and accepts a narrow, validated input protocol.
- The viewer always offers "Use the classic viewer", which opens KasmVNC for the same checkout on the same
  device by the same nonce.
- Everyone else gets KasmVNC exactly as in 0.6.4, but for JavaScript dialogs: 0.7.0 leaves them on the pod's screen
  for the classic viewer to answer instead of dismissing them unseen (§16 decision 2, decided: fix it now).

**A sharp, phone-shaped page without touching the pod at launch.** For a streamed session only, the agent:
- sets Xvnc's X screen to the viewer's CSS size times a scale factor, with one RandR mode (`xrandr`);
- starts `xsettingsd` with `Xft/DPI` = 192, which Chromium follows live (`devicePixelRatio` 1 → 2, no relaunch).

So the page lays out at the phone's width in a real window and renders at 2x. Pop-ups and new tabs get the same
size and scale with no per-page work.

**Across connections.**
- The pod keeps the phone's screen when a stream disconnects, so a reconnect from the same device changes
  nothing the page can see. Phones reconnect constantly (3-D Secure in the bank app, screen locks).
- The pod's own 1280x1000 at 1x is restored only just before a classic (KasmVNC) viewer is bridged. KasmVNC
  therefore always finds the pod as 0.6.4 left it, and the classic path is byte-for-byte 0.6.4's.
- Nothing else depends on the screen during a hold: the watcher reads URLs, and a viewed pod empties its cart by
  URL and exits (C22).

**Input: no touch emulation, no Emulation domain, desktop user agent kept.** Every input is mouse, wheel or
keyboard, as from a Mac with a trackpad:
- taps are mouse clicks (with the real press duration), refused if the page has moved since the image the user
  tapped;
- scrolling is wheel deltas with viewer-computed momentum, at most 30 a second, coalesced in the agent;
- pinch is a trackpad pinch (`synthesizePinchGesture`, mouse source) about the start centroid, then a pan to the
  end centroid;
- text is `insertText`, IME composition and named keys;
- computers also send pointer movement and real key down/up timing.

All of it works into a cross-site (out-of-process) card iframe.

**The phone keyboard.** The agent keeps the viewer supplied with a map of the visible editable fields: type,
`inputmode`, credential-free `autocomplete`, rectangle and a 3×3 hit mask, including fields in cross-site
iframes. It reads them in isolated worlds that page script can't see, with every bound applied inside the page
before anything reaches the agent.
- With the map, the viewer focuses a proxy `<input>` with the right attributes inside the tap, which is what lets
  iOS open the keyboard. The proxy is never a password field and never carries credential tokens.
- Native pickers stand in for `<select>` and date/time fields. The agent sets their values with trusted input,
  only by clicking where the field itself would get the click, and only after confirming the field has focus.

**Same security model.**
- Admission, tickets, the nonce device lock, one viewer at a time, viewer seconds and the hold clocks are
  unchanged.
- Every inbound message *and every outbound image or message* is re-authorized against the locked cart, and
  checked every second on the clock.
- A 6000 s connection cap replaces Xvnc's `-MaxConnectionTime` for streams.
- A pod-side kill switch (`cast_allowed`) closes live streams and refuses new ones within one configure push.
- No raw CDP is reachable by the viewer: the agent calls an allowlisted set of methods with fixed scripts.
- Typed text is never logged or stored, and page-derived strings are rendered as text under a strict CSP.

**Cost.**

| Measure | Value |
|---|---|
| First image after the socket opens, first connect (screen switch included; six runs) | 173-575 ms (round 1). A same-device reconnect needs no switch; the seeded screenshot takes 25-42 ms |
| Image size, phone at 2x (780x1328 to 780x1688 px, JPEG q50) | about 80 kB |
| Bandwidth while scrolling continuously (cap 15 images/s) | 8.6 Mbit/s |
| Bandwidth on a static page / blinking caret | 0 / about 0 (repeats) |
| An animated page (an 18 px spinner) with no input | 37 kB/s in still mode (was 218 kB/s) |
| Input to image inside the pod, isolated wheels, p50 | 47 ms |
| Input to image inside the pod, continuous 60-120 Hz gesture (coalesced), p50 (p90) | 114-137 ms (173-217) |
| Chromium memory while streaming at 2x | +54 MB (423 vs 369 MB PSS) |
| Chromium CPU while scrolling continuously (`everyNthFrame` 1; 2 measured 100-106% but loses a change's last frame, §13) | about 1.25 cores (125-128%) |
| Chromium CPU on an animated page, no input | 19% in still mode (143% with the screencast running) |
| Field map per evaluation (main page + card iframe), at most 4 a second | 14.7 ms p50 agent wall time; ≤ 48 KiB per frame |
| The agent's own CPU while streaming | 9% |

**Effort.**

| Workstream | Days |
|---|---|
| Step 0: shared conformance pack (A and B together) | 1 |
| A: agent, gate, pod image | 10-12 |
| B: viewer and vnc-edge | 10-12 |
| C: ferry-web, ferry-control, DB, frontend | 3-4 |
| Midpoint integration checkpoint | 1 |
| Final integration and device tuning | 3-4 |

### Decisions at a glance

| # | Question | Decision | Evidence (§) |
|---|---|---|---|
| 1 | Sharp frames | Per-session `xsettingsd` Xft/DPI + RandR screen size; scale factor 2 for high-DPI viewers within a 1.6 MP budget, else 1 | §4.1 |
| 2 | The screen across connections | Apply is a no-op when nothing changed; on a change, DPI then mode, back to back (the page's CSS width moves monotonically, 96/96 transitions); restore only before a classic viewer is bridged | §4.1 |
| 3 | Phone layout | The real window at the viewer's CSS size, not `Emulation.setDeviceMetricsOverride`; nothing per page | §4.2 |
| 4 | Input | Mouse taps with `seq` (refused when stale) and press duration; `move` and real key down/up for computers; wheel + viewer momentum, ≤ 1 per 33 ms, coalesced in the agent; pinch about the start centroid then a pan; `insertText` (LF/TAB allowed, native paste rules) / keys / IME | §4.3 |
| 5 | Selects and dates | Native pickers in the viewer; the agent clicks only a point where the field itself gets the click (hit-tested through every ancestor frame), and presses keys only once the field has focus | §4.3, §4.4 |
| 6 | Keyboard awareness | Agent-pushed field map from isolated worlds with 3×3 hit masks, bounded inside the page, on one rate-limited schedule; usable only for the image and the inputs it was taken after; proxy focused inside `touchend` with `pointer-events: none`; never `type=password`, no credential tokens | §4.4 |
| 7 | Frame pipeline | Live (screencast, ≤ 15/s), still (screenshots, ≤ 2/s, for animated pages without input) and paused (hidden viewer); every session seeded with a screenshot; ≤ 2 un-acked, newest wins; repeat cache; scroll prediction only once the images confirm the document moves | §4.5 |
| 8 | Wire protocol | `fw.cast1`; structural errors close 4400, value errors answer `error invalid`; close codes 4000-4500 + 4005 | PROTOCOL |
| 9 | Kill switch | `checkout.streamViewer` in ferry-web (URLs, the Settings switch) and in ferry-control (`cast_allowed` in each pod's configure; the gate refuses and closes streams) | §8.3 |
| 10 | Plumbing | `user_settings.checkout_viewer` (migration 0006), `PATCH /api/me`, `GET /api/state`; the `#v=s1&t=<ticket>` URL; landing routing; sticky per-checkout classic fallback; shared `fw/session.js` | §8 |
| 11 | vnc-edge | `viewer.html` + ES modules under their own CSP; vnc.html's patch and CSP byte-identical to 0.6.4; the escape hatch in static markup | §9 |

## 2. Constraints (owner decisions) and how each is met

| Owner decision | How it is met |
|---|---|
| Per-user beta switch, default off; KasmVNC for everyone else, unchanged but for dialogs | `checkout_viewer` defaults to `classic`. ferry-web emits the 0.6.4 URL byte-for-byte for classic users. vnc.html, its CSP and the shim's behaviour are unchanged (§9). Who can see the switch at launch is §16 decision 1. The one deliberate change to the classic path is dialogs (§13, §16 decision 2) |
| Streamed viewer on any device, with a classic escape hatch on the same device that a broken beta can't strand | The escape hatch is in viewer.html's static markup and is shown by `fw/session.js` (a classic script) 3 s after load unless the viewer module reports ready, and on any uncaught error (PROTOCOL §8.10). The menu, status screens and close code 4005 also lead there. It reconnects by the device's nonce (verified round 1: no ticket needed) |
| Nothing the KasmVNC path depends on changes at launch; per-session changes undone or irrelevant afterwards | No change to `processes.py`, Xvnc flags, matchbox, Chromium's launch arguments or scale factor. A stream's screen is left in place while it can only matter to another stream from the same device. It is undone before any `binary` viewer is bridged: DPR 1, 1280x1000, modes removed, xsettingsd stopped (§4.1; measured 44 ms) |
| Same security model; an equivalent connection cap; the admin never views | §5 |
| Keep the desktop UA; minimise fingerprint changes; avoid touch emulation unless unavoidable | No touch emulation and no Emulation domain at all. On a phone the page sees the same as with 0.6.4's classic viewer except DPR 2 (a Mac Retina's value). On a computer it sees pointer movement, hover and real click and key timings, as with classic (§4.2). Nothing needs touch emulation; no feature was found that requires it (§4.3) |
| Version 0.7.0 | `pyproject.toml`, `web/package.json` and the images |

## 3. How the evidence was gathered

The pod's real screen, reproduced as `tests/browser/test_checkout_screen.py` does:
- the repo's own `pod_processes()`: KasmVNC 1.5.0's Xvnc with the pod's flags, and matchbox;
- Chromium 141 headed, launched with exactly PodBrowser's arguments (`--kiosk`, 1280x1000, the Mac UA,
  `no_viewport`, the same ignored defaults), plus `--host-resolver-rules` for the test origins;
- the repo's `Gate`, extended in a scratch subclass (`proto/gate_cast.py`);
- vnc-edge's real `build.py` output with KasmVNC's stock client and the 0.6.4 shim, for the classic handoff.

**Test sites.**
- `shop.test:8001` stands in for BC Ferries: sticky header, contact fields, a select, date and time, dialogs,
  pop-ups, 70 sections.
- `pay.test:8002` is a cross-site card iframe (its own CDP target, i.e. out-of-process), plus a bank pop-up.

Round 2 added:
- `round2/site/shop2/occl.html` with `pay.test:8012/card3.html`: a `position: fixed` "Pay now" footer (type
  submit) over selects, a date field and the lower part of a cross-site card iframe; a custom select whose
  mousedown is cancelled.
- From the feasibility review, `review-feasibility/site/extra/`: an app-shell page whose `<main>` scrolls, a page
  with a spinner and a countdown, and a page that swaps its component tree at a breakpoint in script.

**Viewers.** The viewer was Playwright Chromium in iPhone-13-like emulation (390x664 CSS at DPR 3, touch, mobile)
and as a 1280x800 desktop at DPR 2. WebKit isn't available here, so nothing ran on iOS Safari or Android Chrome
(§12).

## 4. Decisions with evidence

### 4.1 Sharp frames, and the screen across connections

| Option | What it does | Measured | Verdict |
|---|---|---|---|
| **(a) Xft/DPI via xsettingsd + RandR** | The agent starts `xsettingsd -c <conf>` with `Xft/DPI 196608` (192 dpi = 2x) and sets the X screen to `w·2 × h·2` with a RandR mode | Chromium follows live: DPR 2 in 4-33 ms, no relaunch, no reload. Screencast and screenshot images are 780x1688 for a 390x844 viewport. Stopping xsettingsd gives DPR 1 in 16 ms and a pixel-identical screenshot | **Chosen** |
| (b) `Page.captureScreenshot` polling at an emulated DPR | | 17.8 fps max, 53 ms p50 per capture, no change notifications | Rejected as the main source; used for still mode and seeding (§4.5) |
| (c) 1x | | 33 kB images, but visibly soft on a 3x phone (`shots/s1c-legibility-1x-vs-2x-full.png`) | Used when the viewer's DPR < 1.5 or the frame would exceed the budget (desktops) |

**Building (a).**
- Add `xsettingsd` and `x11-xserver-utils` (for `xrandr`) to the Dockerfile's `display` stage.
- The config lives in a 0700 directory under the pod's memory-backed `/tmp` (`/tmp/xsettings/xsettingsd.conf`,
  mode 0600) and holds one line, `Xft/DPI <n>`.
- xsettingsd opens no listener (it is an X client), and a crash of it fails safe (back to 1x).
- A RandR mode change resizes the kiosk window (matchbox) in 7-38 ms:
  `xrandr --newmode fw780x1328 60 780 780 780 780 1328 1328 1328 1328`, `--addmode VNC-0 …`,
  `--output VNC-0 --mode …`. At most 16 of our modes are on the X server at once: a new size first removes the
  one used least recently (never the one shown), since a pod's life can see any number of viewer sizes (a
  window resized again and again, browser zoom, rotation, reconnects from other devices). Found in review: a
  limit of 16 per pod life closed every stream at a 17th size with 4500, reconnect after reconnect.

**Round 2: what the page sees, and when** (`round2/s4_screen.py`, `round2/s4b_monotone.py`; reviews: security 4,
feasibility 6).
- **Round 1 problem.** Round 1 restored 1280x1000 at 1x at every stream teardown and applied the phone size again
  on the next hello. A phone that reconnected put the page through 390@2 → 780@1 → 1280@1 → 780@1 → 390@2: five
  resizes per connection. The reviewers measured typed values wiped on a page that swaps its component tree at a
  breakpoint.
- **`Screen.apply(w, h, dsf)` is a no-op when nothing changed.** Measured: a same-phone reconnect produced
  `apply_did_something: false` and **0** resize events.
- **A real change sets DPI first, then the mode, without waiting in between.** Same scale factor (rotation,
  another phone, a window resize): one resize. A scale-factor change makes the page see one or two resize events.
  - Over 96 measured transitions (first connect 1280@1 → 390@2, phone → 1280x800@1 desktop, desktop → phone,
    restore), across three orderings, every sequence of `innerWidth` values was **monotone** from the old width
    to the new one.
  - About half had one transitional width (640 or 780) between them. So a page crosses each of its breakpoints at
    most once, as with any single resize, and nothing can be typed in the 10-30 ms in between.
  - DPI-first is chosen because its transitional width is the old pixel width over the new scale factor. That is
    monotone whenever the 2x side is at most half the 1x side's width, which holds for every phone ↔
    desktop/pod pairing. The one exception is a 2x window wider than 640 CSS px meeting a 1x one less than twice
    as wide (a small desktop window at DPR 2, which the pixel budget makes rare), where it can overshoot once.
- **Restore** (`Screen.restore()`): runs only in the gate, after the previous viewer's teardown and before a
  `binary` viewer is bridged (PROTOCOL §2.3).
  - Sets DPI 1 and mode 1280x1000 together, then deletes the added modes.
  - Xvnc's own 1280x1000 mode is gone once a classic viewer has resized the screen: Xvnc replaces its mode with
    one of the viewer's size (measured). The restore then makes one of ours of the pod's size and keeps it while
    it's shown, and it deletes our other modes even when switching failed. Found in review: classic, then a
    stream, then classic again left the stream's 780x1328 for KasmVNC to resize from, and every later classic
    viewer repeated the failing restore.
  - Bounded to 3 s in total, with 2 s per `xrandr` and a timed-out `xrandr` killed. Measured 44 ms; afterwards
    xsettingsd was stopped and no modes were left.
  - It never runs at hold end or drain, because the pod exits anyway (C22).
  - As built, the gate's restore is `stream.ClassicScreen`: when a stream has changed the screen since the last
    classic viewer, it first pinches every zoomed page of the checkout back to page scale 1 (bounded to 2 s; a page
    with an open dialog is left as it is), then calls `Screen.restore()`. Found in integration: after a stream's pinch, KasmVNC found the page at scale 2, which
    a classic viewer on a phone can't pinch back.
- **The escape hatch on the same phone** restores 1280x1000@1 and then KasmVNC resizes to its own size, so the page
  goes 390@2 → 1280@1 → 390@1 once. That is the price of KasmVNC finding exactly 0.6.4's state. It happens at most
  once per checkout, and only when the user leaves the beta.
- **A reconnect loop is paced.** The gate starts at most one cast session per second; extra upgrades wait (PROTOCOL
  §2.2 step 7). With the no-op apply, a loop from the same device causes no screen work at all.

**The scale factor rule.** 2 when `dpr ≥ 1.5` and `w·h·4 ≤ 1,600,000`, else 1:
- iPhone 13: 390x664 → 2x, 780x1328;
- Pixel 7: 412x839 → 2x, 1.38 MP;
- a 1280x800 desktop → 1x.

It is only ever 1 or 2, a Mac's two scale factors.

**What the KasmVNC path sees** (round 1 `proto/e2e.py`, phone and desktop):
- after a stream session, the classic viewer connected by the same nonce;
- the pod tab was at DPR 1;
- KasmVNC resized the screen to its own size as in 0.6.4;
- xsettingsd was not running.

Round 2 moves the restore from teardown to the classic admission. The state KasmVNC finds is the same; the
end-to-end check is repeated in A6 and in the integration tests.

### 4.2 Phone layout and what page script sees

- **Real window, not emulation.** The page's window becomes the viewer's CSS viewport, at 2x. `innerWidth`,
  `outerWidth`, `screen.*` and `orientation` all come from the real X screen and window.
- **Pop-ups and new tabs.**
  - `window.open` with features, `target=_blank` and `window.open` from inside the cross-site iframe all opened at
    390 CSS wide, DPR 2.
  - Each appeared in 289-378 ms.
  - `window.close()` returned to the checkout in 21-22 ms.
- **What doesn't follow the phone.** Without `mobile: true`, the viewport meta tag is ignored (the page lays out at
  the window width), there is no text autosizing, and scrollbars are classic (15 CSS px), as in the classic viewer
  today.

**Fingerprint, measured** (round 1 `out/s1-dpi.json`, `out/s2a-pointer.json`; round 2 `round2/out/s4*.json`,
`round2/out/s8-desktop.json`; the security review's `reconnect_flap.json`):

| What page script sees | 0.6.4 pod, no viewer | 0.6.4 classic, iPhone | **0.7.0 stream, iPhone** | 0.6.4 classic, desktop | **0.7.0 stream, desktop** |
|---|---|---|---|---|---|
| `innerWidth`×`innerHeight` | 1280×1000 | 390×664 | 390×664 | 1280×800 | 1280×800 |
| `outerWidth` | 1280 | 500 | 500 | 1280 | 1280 |
| `devicePixelRatio` | 1 | 1 | **2** | 1 | 1 |
| `maxTouchPoints`, `ontouchstart`, touch events | 0, no, never | same | same | same | same |
| Pinch seen as | n/a | ctrl+wheel | ctrl+wheel (trackpad pinch) | ctrl+wheel | ctrl+wheel |
| Pointer path before a click | n/a | none (taps) | none (taps) | continuous | continuous (`move`, ≤ 30/s) |
| Hover | n/a | no | no | yes | yes (mouseenter seen) |
| Press duration of a click | n/a | back to back | the touch's duration, ≤ 300 ms | real | real, ≤ 300 ms (99 ms measured for a 95 ms press) |
| Key events | n/a | down and up together | down and up together | real down/up, Shift as a key | real down/up (`ph`), Shift as a key, rollover (89-100 ms dwells measured) |
| A reconnect from the same device | n/a | nothing | **nothing** (round 1: 5 resizes) | nothing | nothing |
| The first connect | n/a | one resize, 1280 → 390 | 1-2 resizes, width monotone 1280 → (640) → 390, DPR 1 → 2 | one resize | one resize |
| Leaving for the classic viewer | n/a | n/a | 390@2 → 1280@1 → 390@1, once | n/a | 1280@1 → 1280x1000@1 → 1280x800@1, once |

`outerWidth` 500 with `innerWidth` 390 is Chromium's 500-DIP minimum window width. 0.6.4's classic viewer on a
phone shows it too. `navigator.platform` is "Linux x86_64" with the Mac user agent in every column: an existing
inconsistency, out of scope.

### 4.3 Input without touch emulation

Every row ran on the pod's real screen at 2x, with the cross-site card iframe present, no touch emulation and no
Emulation domain.

| Gesture | Viewer → protocol | Agent → CDP | Result |
|---|---|---|---|
| Tap | `tap {page, seq, x, y, ms}` on `touchend` (under 8 px of movement, under 500 ms), or on `pointerup` for a mouse | live state from `Page.getLayoutMetrics`; if image `seq` no longer matches the page (scroll/scale moved by more than 1 px / 0.01): `error stale`, nothing sent. Else `mouseMoved`, `mousePressed`, wait `ms`, `mouseReleased` at (x/s, y/s) | Clicks in 3 ms + `ms`, `pointerType: mouse`. **11/11** into the cross-site iframe at 11 scroll offsets (round 1) |
| Mouse movement (computers) | `move {page, x, y}`, ≤ 1 per 33 ms | `mouseMoved`, coalesced | 15 moves → 15 `mousemove` and a `mouseenter` before the click (`round2/s8`) |
| Scroll, finger-following | `wheel {dx,dy}`, ≤ 1 per 33 ms (summed) | `mouseWheel`; queued wheels merged | 1:1 at DPR 2. Round 2 (`round2/s5`, 60 and 120 Hz, drag + coast): peak queue 1 (was 43 and 136, which would have closed 4429), finished 25-47 ms after the last send (was 1.3-3.7 s), every pixel dispatched, exact final scroll |
| Flick / momentum | the viewer keeps sending decaying `wheel` | `mouseWheel` | 320 px finger + 1,114 px coast (round 1). Also starting over the iframe |
| Pinch | local zoom following the fingers, then one `pinch {seq, x0, y0, x1, y1, scale}` | stale check; `synthesizePinchGesture` (mouse source) at (x0/s, y0/s), then `mouseWheel` of (x0 − x1, y0 − y1) at (x1/s′, y1/s′) | The content under the start centroid ends exactly under the end centroid (`round2/s7`: (300, 400) → (250, 300) measured). The page sees ctrl+wheel and no touch events |
| Pinch alternatives | | `dispatchTouchEvent` two-point | Rejected: touch events while `maxTouchPoints` is 0 |
| Long press | nothing sent | none | No right-click; paste is in the menu (§4.4) |
| Text | single typed character → `key {c}`; multi-character → `text {s}` (LF and TAB allowed) | `dispatchKeyEvent keyDown` / `Input.insertText` | Unicode, card numbers into the iframe. Round 2 (`round2/s9`): `insertText` applies Chromium's native paste rules in an input (trailing LF dropped, inner LF → space) and keeps LFs in a textarea, identically to a real paste; a LF never submits a form (`round2/s9b`). The reviewers' 4400 on a pasted newline is gone |
| IME | `compose {s}` per update, `text {s}` to commit | `imeSetComposition`, `insertText` | "Mike " + "Ch" → "Ches" → "Chester" = "Mike Chester" (KasmVNC dropped letters here) |
| Named keys | `key {k}` (+ `shift`, + `word`) | `rawKeyDown`/`keyDown` + `keyUp` | Backspace, Tab (cc → expiry inside the iframe), Enter, arrows, Home/End |
| Physical keyboards (computers) | `key {k|c, ph: down}` at `keydown`, `{ph: up}` at `keyup`; `Shift` as a key | down and up halves; ≤ 6 held; Shift held → modifiers 8; all released at teardown | `round2/s8`: keydown Shift, keydown M (shiftKey), input M, keyup M, keyup Shift; rollover i/k; trusted |
| Browser shortcuts | impossible: only Shift (and Ctrl as `word`) exist | — | Ctrl+T/N/W/L/P/O/U, F12, Ctrl+Shift+I, Alt+F4, Alt+Left, Shift+Escape, Ctrl+Shift+Delete did nothing via CDP |
| `<select>` | native picker in the viewer → `choose {rev, f, i}` | PROTOCOL §6.11 (below) | One trusted `input` + `change` |
| Date / time | native picker → `date {rev, f, v}` | PROTOCOL §6.12 | 2026-10-16, 2028-02-29; 09:30, 00:05; trusted events |
| JS dialogs | dialog sheet with the frame's origin → `dialog {id, ok, s}` | `context.on("dialog")`, kept across reconnects | confirm → true, prompt → "FERRY10" (round 1); iframe dialogs carry the iframe's origin (`round2/s2b`) |
| Pop-ups and tabs | `pages`, `page show/close` | follow `context.on("page")`; seed a screenshot | §4.2 |

**`choose` and `date` never click what the user didn't pick** (`round2/s1_choose.py`; security review 1).
- **The reviewer's failure.** Round 1 clicked the select's centre. Under a fixed "Pay now" footer that clicked Pay
  twice and submitted the form twice (reproduced: `prototype_on_s1`).
- **The fix: hit-test, then click, then check focus.** In the field's own isolated world the agent picks the first
  point of the element (centre, then 9 samples) where `elementFromPoint` is the element itself. It then confirms
  that every ancestor frame would deliver that point to the owning iframe. Only then does it click. After the
  click it checks `activeElement` in the field's frame and each ancestor before any key.
- **Measured on the occlusion page**, with the footer's click log empty in every case:

| Case | Result |
|---|---|
| Fully covered select | `error stale`, nothing sent |
| Half-covered select | Picked through its visible half |
| Clear select | Picked |
| Custom select whose list never opens | `error failed`, no Enter sent |
| Select in the cross-site iframe | Picked |
| Iframe select under the parent page's footer | `error stale`, nothing sent |
| Covered date field | `error stale` |
| Clear date field | Set |
| Enter on a focused, closed select inside a form | Doesn't submit |

### 4.4 Field awareness for the phone keyboard

iOS opens the on-screen keyboard only for an element focused synchronously in a user gesture. The viewer can't ask
the pod "is this a field?" during `touchend`, so it must already know.

**The field map** (PROTOCOL §5.3; reference scripts `round2/cast_scripts.py`, `round2/worlds.py`).
- **Isolated worlds.** Fixed scripts run in an isolated world in each frame of the front page: the main frame, and
  each iframe whose owner box intersects the visual viewport (at most 8 frames, 3 levels). Page script can't see
  these worlds (round 1: a page hooking `querySelectorAll` saw 0 calls from them).
- **Hit masks.** Each field carries a 3×3 hit mask. Bit *j* is set when a click at the centre of cell *j* of the
  field's visible box would reach the field and nothing else, in its own frame (`elementFromPoint`) and through
  every ancestor (`OWNER_HIT` on the owner iframe resolved into the parent's world). Covered fields are not sent.
  Measured on the occlusion page:

| Field | Mask |
|---|---|
| Fully covered select | Dropped |
| Half-covered select | `000111111` (bottom row covered) |
| Iframe select partly under a main-page element | `110110111` |
| Iframe select under the parent's footer | Dropped |
| Covered date field | Dropped |

- **Rectangles in frame px** (PROTOCOL §5.3.3; feasibility review 3).
  - A main-frame field is at `(client − visualViewport offset) × scale`. An iframe field adds the chain of owner
    content-box origins, read with `getBoundingClientRect` in each parent's world.
  - `DOM.getBoxModel` is no longer used for offsets. It is visual-viewport-relative and unscaled, so mixing it with
    `getBoundingClientRect` is what put round 1's main-page fields 300 px off when zoomed.
  - Measured at scale 2 with offsets (150, 150) and (175, 250): main and iframe rectangles exact (`postal` =
    (−276, 285, 702, 77), as the reviewer's ground truth). A CDP click at every mapped centre focused that field
    (`round2/s7*.json`). The rectangles are in frame px of the drawn image, used by the viewer as they are.
- **Bounds inside the page** (security review 2; `round2/s2*`).
  - Every cap is applied by the script before it returns: 500 candidates, 30 ms, 100 fields, labels and
    `autocomplete` cut to 64, 400 options, a 48 KiB budget. Scripts run with `Runtime.evaluate` and
    `timeout: 100`.
  - `Runtime.callFunctionOn` silently ignores `timeout` (measured: 2,003 ms for a 2 s loop).
    `Runtime.terminateExecution` isn't used either, because it can kill the page's own next script.
  - On the reviewer's pages:

| Page | Round 1 | Round 2 |
|---|---|---|
| 200 options × 100k-character labels | 20 MB in 5.6 s | 15.8 kB in 8 ms |
| 5,000 overlapping inputs × 20 kB `autocomplete` | 100 MB in 124 s | 4.9 kB in 35 ms |
| 200,000 overlapping inputs (each `elementFromPoint` there costs about 6 ms) | 28.7 s (without the time budget) | 85-101 ms, `trunc` |

    Agent RSS growth: 0. After a truncated or timed-out evaluation, the next waits 1 s.
  - Titles come from `Target.getTargetInfo` (Chromium caps them at 4,096; measured), not `page.title()` (20 M
    characters measured).
- **Cost.** Main page + card iframe: 14.7 ms p50 (max 20.6) of agent wall time per evaluation with the hit tests.
  The first evaluation, including world setup, takes 56 ms. The reference implementation makes four sequential CDP
  round trips; two can run in parallel.
- **When it is sent** (PROTOCOL §5.3.4; feasibility review 1). There is one schedule: an evaluation is due after
  any newer image, navigation or DOM-affecting input; it starts at most every 250 ms and at least 50 ms after its
  trigger, with no debounce. An unchanged result is sent as a small `fields_ok`.
  - Round 1's debounce never fired on a page with a spinner (0 maps in a session).
  - Round 1's "only when sx/sy changed" missed an inner scroller that moved 516 px.
- **When the viewer may use it** (PROTOCOL §8.3), all of:
  - same page and visual viewport;
  - within 4 images of the drawn one;
  - taken after the agent had carried out the user's latest scroll, tap or keystroke (the message counter `n`);
  - no prediction or local zoom drawn.

  An inner scroller that moves because the user scrolled it invalidates the map at once. One that moves by itself
  is caught by the next evaluation (≤ 250 ms). A covered field has no hit cells. Overlays are shown only for fully
  visible fields of a usable map.

**In `touchend`** (viewer):
- If the tap hits a usable map's `text`/`area` field that isn't read-only, the viewer sets the proxy's attributes
  and position and calls `focus()` in the same task, then sends `tap` so the pod's field gets focus too.
- Round 1's e2e checked the proxy was `document.activeElement` within the `touchend` task, with
  `autocomplete="name"`, then `cc-number` and `inputmode="numeric"` for the iframe field.

**The proxy's attributes** (PROTOCOL §8.5; security review 3).
- `type` is `email`, `tel`, `url` or `text`, **never `password`**.
  - Browsers ignore `autocomplete=off` on password inputs, so a password-typed proxy on
    `ferry-checkout.chestr.dev` would offer the user's saved `auth.chestr.dev` (Authelia) login: Chrome's
    same-site matching, iCloud Keychain's registrable-domain matching.
  - The proxy for a password field is `type=text`, `autocomplete=off`, autocorrect/autocapitalize off,
    `spellcheck=false`. That is exactly KasmVNC's own hidden `textarea` in 0.6.4 (`autocapitalize="off"
    autocomplete="off" spellcheck="false"`), so it is no worse than classic.
- The credential tokens `username`, `current-password`, `new-password` and `webauthn` are stripped by the agent and
  again by the viewer. Contact, address, `cc-*` and `one-time-code` are kept, so AutoFill can offer contacts and
  cards.
- `autocorrect=off` and `spellcheck=false` always; `font-size: 16px` (no iOS zoom on focus); `opacity: 0`;
  `caret-color: transparent`.
- **`pointer-events: none`** (feasibility review 7). Round 1's proxy sat over the focused field and swallowed taps
  and drags there: the pod's caret didn't move, and a drag from the field didn't scroll.
  - Now a full-screen gesture layer above the canvas and the proxy takes every touch, click and wheel, and the
    proxy is focused programmatically inside `touchend`.
  - iOS's long-press paste on the proxy is gone with it, so the menu sheet gains "Paste", which reads
    `navigator.clipboard.readText()` inside its own tap. iOS shows its own "Paste" confirmation; Android asks once.

**Focus reports** (PROTOCOL §5.4).
- After each tap, Tab/Enter/Escape, choose/date, dialog answer and page show, and while typing, the agent reads
  `document.activeElement` in each frame's world.
- It sends `focus` (innermost frame) when it changes. This handles a card field auto-advancing to the expiry: the
  viewer retargets the already-focused proxy.
- On a computer the viewer focuses the proxy from `focus` reports alone.
- When the map wasn't usable at the tap, the tap still goes through, and the `focus` report makes the viewer show a
  "Tap to type" chip whose tap focuses the proxy inside a gesture.

**Selects and dates.**
- While a map is usable, a transparent native `<select>` or `<input type=date|time>` overlays each fully visible,
  hit-clear field (`m` = 511). The user's own tap opens the platform picker; on `change` the viewer sends
  `choose`/`date`.
- If a tap reached a select in the pod (no usable map, or a partly covered select), the `focus` report says
  `popup: true`, and the viewer shows its own option sheet. The agent then picks with keys only, after the focus
  check.
- Selects with more than 400 options, or cut by the budget, have `trunc`: no overlay. The sheet shows what it has
  and says the rest needs the classic viewer. A 249-entry country list fits.

**Dialogs** (security review 3).
- The dialog message carries the opening frame's origin (`Page.javascriptDialogOpening.url` on the page's session
  with the Page domain enabled; Playwright's `Dialog` has only the page). The iframe's alert carried
  `http://pay.test:8002/card.html` (`round2/s2b`).
- The sheet's heading is "`<origin>` says:", or "An embedded page at `<origin>` says:", as Chrome's.
- The sheet is styled as page content, never as Ferry Watch's own controls, and its prompt input follows the
  password rules. A cross-site frame therefore can't pass a "re-enter your Ferry Watch password" prompt off as ours.

### 4.5 Frame pipeline

**Live mode parameters.**
- `Page.startScreencast {format: jpeg, quality: 50, everyNthFrame: 1}`, every Chromium frame acked at once.
- At most 15 images a second forwarded, at most 2 un-acked by the viewer (newest wins), and a 2-slot repeat cache
  keyed by SHA-256.
- The screencast needs no `Page.enable` (`round2/s2_bounds.py`). Page is enabled only by the dialog keeper, on its
  own sessions, for dialog origins and closings (§6.1).

**Measured on the pod screen** (390x844 at 2x; links simulated in-process; round 1):

| Link | Cap | Window | Shown fps | Mbit/s while scrolling | kB/image | Input → shown p50 (max), isolated wheels |
|---|---|---|---|---|---|---|
| LAN | 15 | 2 | 13.1 | 8.6 | 80 | 47-49 ms (52-69) |
| LTE 10 Mbit/s, 60 ms RTT | 15 | 1 | 6.8 | 4.7 | 83 | 138 ms |
| **LTE** | **15** | **2** | **13.0** | **8.7** | 82 | **142 ms** (148) |
| LTE | 15 | 3 | 13.0 | 8.9 | 84 | 142 ms (177) |
| 3 Mbit/s, 120 ms RTT | 15 | 2 | 4.3 | 2.9 | 82 | 329 ms |

`everyNthFrame` 2 would save about a quarter of a core against 1 (100-106% vs 125-128% while scrolling), at no
typical latency cost, but it can drop the last frame of a change, which a still page then never replaces (§13). The
agent uses 1.

**Round 2 correction** (feasibility review 4).
- Round 1's 47 ms came from awaited, isolated wheels.
- Under a continuous drag + coast at 60 and 120 Hz, with coalescing, the pod shows each input after 114-137 ms p50
  (p90 173-217) in-pod (`round2/s5`). Each awaited `mouseWheel` costs 26 ms p50 with the screencast running.
- Over LTE add about 60 ms RTT and 60 ms transfer. Scroll prediction hides this for document scrolls.

**Seeding** (found while verifying the lazy restore).
- `Page.startScreencast` sends nothing until the page changes: 0 frames in 1 s on a static page, 5 of 5 runs
  (`round2/s6b`).
- With the no-op apply, a reconnect to a static page would never get an image. So every session start, page
  switch and return from hidden begins with one `Page.captureScreenshot` image (25-42 ms; 780x1328 at 2x;
  `round2/s6`).

**Still mode for animated pages** (feasibility review 5; `round2/s6_idle.py`).
- With a 18 px CSS spinner and a countdown, the screencast alone costs 143% of a core of Chromium CPU at 12 images
  a second while the user does nothing. `everyNthFrame` 4/8 and slower acks barely help (review: 107-133%).
- Polling screenshots costs a constant 8-10% even on a static page, where the screencast costs 1%. So still mode
  is entered only when the page keeps producing frames without input:

| Page (5 s, no input) | Screencast (live) | Screenshots every 500 ms (still) |
|---|---|---|
| Spinner + 1 Hz countdown | 143% CPU, 12.2 images/s, 218 kB/s | **19%**, 2.0 images/s, 37 kB/s |
| Countdown only | 4%, 0.6 images/s, 12 kB/s | 10%, 1.0 images/s, 20 kB/s |
| Static | 1%, 0 | 9%, 0.2 images/s |

- **The rule** (PROTOCOL §4.3): live → still after 1 s without input while Chromium delivered ≥ 8 frames in that
  second. Still → live on any input (the screencast restarts before the input is carried out: first frame 49-72 ms
  later) or after 4 unchanged captures. Hidden viewer → paused (nothing).
- Page changes during still mode appear within 500 ms. `captureScreenshot` with a `<select>`'s list open took 46 ms
  on this headed screen (no hang), and each is bounded to 1 s anyway.
- **Idle budget:** an animated page costs at most about 20% of a core and about 40 kB/s while the user isn't
  touching anything.

**Local scroll prediction (viewer), confirmed only** (feasibility review 2).
- Round 1 shifted the last image by every wheel delta not yet reflected in an image's `sy`. When an inner element
  (an app-shell `<main>`, a modal, a terms box) scrolled instead, `sy` never moved. The image slid away by the
  whole finger travel and stayed blank, and a tap on the blank area clicked "Pay now" (review `r3_inner`).
- Round 2:
  - The viewer shifts only after an image whose `sy` moved in the wheel's direction since the gesture began.
  - The shift is capped at the deltas sent since that confirming image, clamped to the document (`ch` from the
    map) and to one viewport height.
  - It is dropped on a timer 250 ms after the last wheel, not only on the next image.
  - No tap is sent while a shift is drawn (PROTOCOL §8.2).
- An inner scroller therefore never gets a prediction. It follows at the stream's rate, as the classic viewer
  does.

## 5. Security

### 5.1 Unchanged

The gate's admission is the same code for both kinds:
- the Origin check;
- a READY cart with running clocks, lockdown passed, not draining, `vnc_allowed`;
- a ticket with `aud`, `sub`, trip, pod, cart and an unused `jti`;
- the nonce lock, set in one synchronous block;
- the failure rate limit that never locks the owner out.

The rest is also unchanged:
- one viewer at a time;
- viewer seconds and the hold clocks (`viewer_changed`);
- revocation on release, expiry, `vnc_revoke`, suspension and drain;
- the pod exits after any viewed hold (C22);
- ferry-web mints only for the cart's owner, so the admin never views;
- ferry-control never connects to the gate;
- C13: the classic path's RFB socket is unchanged, and the stream doesn't use it;
- lockdown self-tests: no new listener.

### 5.2 New controls (proposed numbering C33-C42 in design-reference §4.4)

| # | Control | Stops | Where |
|---|---|---|---|
| C33 | Exactly one of `binary`/`fw.cast1` per upgrade; 4 KiB inbound limit; two-step decoding: structural errors close 4400, value errors are dropped with `error invalid` and count toward the drop limit; typed-text rules; enumerated keys with no Ctrl/Alt/Meta | Parser abuse; smuggled shortcuts; control characters into fields; a viewer bug turning into reconnect loops | gate, `cast_protocol.py` |
| C34 | **CDP allowlist** (below); no viewer string is ever part of a CDP method name, a script or a script argument | Raw CDP reaching the viewer; script injection | `cast_cdp.py` |
| C35 | **Every input and every output authorized**: `verdict()` before carrying out any message but `ack`/`visible`, before sending any image or JSON, and every second; it evaluates `release_due` on the clock, `vnc_allowed`, `cast_allowed`, lockdown and drain directly, not through the watch loop | Input or images after a release, an expiry, a revocation or the kill switch, even if the watch loop is stuck (C17's "buggy or lying agent" case) | `stream.py`, `watch.py` |
| C36 | **Connection cap** 6000 s (4004) for both kinds (Xvnc's `-MaxConnectionTime 6000` stays for classic); **session pacing**: at most one cast session start per second | A stream outliving any hold; reconnect loops driving screen and CDP work | gate |
| C37 | **Typed text is never logged or stored**: decode errors log only the message type and length; structs holding text have no `__repr__` in any log path; nothing typed goes into `state`, events, alerts or the termination log; the viewer never stores the proxy's value | Card data in logs (C26's intent) | agent, viewer; caplog test |
| C38 | **Viewer page hardening**: CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src wss://<checkout host>; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'`; page-derived strings via `textContent` only; `Referrer-Policy: no-referrer`, `nosniff`, `no-store` | XSS from page-derived strings; exfiltration | vnc-edge |
| C39 | **Page data bounded in the page**: every isolated-world script caps counts, string lengths, time (30 ms) and serialized size before returning; `Runtime.evaluate` with `timeout: 100`; at most 8 frames per map; worlds and sessions of detached frames dropped; titles from `Target.getTargetInfo` | A page (or any third party in it) pushing hundreds of MB into the agent and OOM-killing the pod mid-hold (R8: an OOM skips the drain) | `cast_scripts.py`, `cast_cdp.py`, `cast_fields.py`, `cast_pages.py` |
| C40 | **Click safety for `choose`/`date`**: click only a point hit-tested to the element through every ancestor frame; no key until the element has focus | Clicks and Enter reaching a covering "Pay" button the user never touched | `cast_input.py` (PROTOCOL §6.11-6.12) |
| C41 | **Credential isolation**: the proxy and the prompt input are never `type=password` and never carry credential `autocomplete` tokens; dialogs carry and show their frame's origin; the escape hatch can't be hidden by page content | The user's saved chestr.dev (Authelia) login offered into a pod page; a page impersonating Ferry Watch's UI | agent (token stripping), viewer |
| C42 | **Pod-side kill switch**: `cast_allowed` in the operator's `configure` (default false); the gate refuses `fw.cast1` without it and closes a live stream with 4005 when it turns false | A bug in the stream surface staying reachable on running pods for up to 26 h after the switch is flipped or the release rolled back | gate, `watch.py`, ferry-control |

**C34's allowlist.** The session's CDP wrapper refuses any other method or parameter shape:
- `Page.{startScreencast, stopScreencast, screencastFrameAck, captureScreenshot, getLayoutMetrics, getFrameTree,
  createIsolatedWorld}`. No `Page.enable`: dialog origins come from the dialog keeper's own sessions (§6.1),
  which no viewer message reaches.
- `Input.{dispatchMouseEvent, dispatchKeyEvent, insertText, imeSetComposition, synthesizePinchGesture}`.
- `Runtime.evaluate`, only with:
  - `contextId` of a world the session created;
  - `returnByValue: true` and `timeout: 100`;
  - an expression that is one of the fixed scripts applied to arguments formatted from Python `int`s and finite
    `float`s.
- `Runtime.callFunctionOn`, only the fixed `STORE_OWNER` function on an object from `DOM.resolveNode`.
- `DOM.{enable, getFrameOwner, resolveNode}`.
- `Target.getTargetInfo` (no arguments).

C17 ("cart held" enforced five times) becomes:
- the agent;
- ferry-web won't mint;
- ferry-control's deletion;
- **Xvnc `-MaxConnectionTime` (classic) and the gate's 6000 s cap (both)**;
- `activeDeadlineSeconds`;
- plus C35, which now covers images as well as input.

C12 (single viewer) holds across kinds: one slot, and the replaced viewer is closed with 4001.

### 5.3 New surfaces, analysed

- **The msgspec parser instead of KasmVNC's C RFB parser.**
  - Python and memory-safe.
  - 200,000 randomly mutated messages produced only msgspec's own errors (round 1 `out/fuzz-protocol.txt`).
  - Round 2 splits decoding into structure (msgspec) and values (Python), so a wrong value is a dropped message,
    never a crash or a disconnect.
- **The isolated-world scripts.**
  - Seven constant strings: `FIELDS`, `OWNER_HIT`, `STORE_OWNER`, `CLICK_POINTS`, `IS_FOCUSED`, `FOCUS`,
    `VIEWPORT`.
  - Arguments are numbers the agent computed.
  - They only read the DOM (`STORE_OWNER` writes only to the world's own global) and run where page script can't
    reach.
  - Everything they return is bounded before it leaves the renderer (C39).
- **`xrandr` and `xsettingsd` subprocesses.** Arguments are built from validated integers only. The config file is
  written atomically, mode 0600, in a 0700 directory. They are started without a shell.
- **Page-derived strings to the viewer.**
  - What crosses: titles (80), origins (100), option labels (64), dialog messages (500), prompt defaults (256),
    `autocomplete` tokens (64, credential tokens removed), enum-checked attributes.
  - No field values cross, except a select's selected index and a date/time value.
- **What a compromised vnc-edge, Envoy or Cloudflare can do (R4).**
  - Unchanged in substance: they see the images, including the card number as the page renders it, and the typed
    text as JSON strings instead of RFB key events.
  - They can inject input messages, and they see `N`.
  - R4's wording should say "screen images and typed text".
- **A malicious viewer (the tenant themself).**
  - It can only do what the protocol allows, on their own checkout, inside the rate limits.
  - The tap staleness check and `choose`'s hit test apply to it too; they protect the user, not the pod.
- **Pre-existing, found in passing.** A page's `prompt(msg, default)` default text is unbounded: 20,000,000
  characters measured.
  - In 0.6.4 it reaches Playwright's Node driver (whose CDP session receives every dialog event) but not the Python
    agent, because no listener is registered.
  - From a pod's first viewer on (of either kind), the dialog keeper's listener means the Python agent receives it
    too, then cuts it to 256.
  - Neither process can bound it before receipt; a page could push one huge string per dialog, as it already can
    into the driver. Recorded in §13 with the classic dialog finding.

## 6. Agent design (workstream A)

### 6.1 Modules

| File | What |
|---|---|
| `src/ferry_watch/agent/cast_protocol.py` (new) | msgspec inbound structs with types only; `check(msg) -> str | None` for the value rules; `typed_text_ok()`; the header `struct.Struct("<BBHIHHffffBBH")`; outbound builders with caps and cleaning (`clean()`, `origin_of()`, `strip_credential_tokens()`); `CloseCode` IntEnum; the rate table; the conformance-pack loader for tests |
| `src/ferry_watch/agent/cast_scripts.py` (new) | The fixed isolated-world scripts (reference: `round2/cast_scripts.py`) and `call(script, *args) -> str`, which formats the expression from ints/finite floats only |
| `src/ferry_watch/agent/screen.py` (new) | `Screen.apply(w, h, dsf) -> bool` (no-op when unchanged; DPI then mode), `Screen.restore()` (bounded 3 s, never raises), `Screen.current`, `Screen.changed` (reference: `round2/screen2.py`); `Screen.widen()`/`narrow()`, room for a dialog on a classic viewer's phone-sized screen (§13) |
| `src/ferry_watch/agent/stream.py` (new) | `CastSession` (opening, inbound, the worker, the ticker, outbound with the verdict, teardown); `InboundQueue` (coalescing, bound 64); `RateLimiter`; `CastPod` and `factory()` for the gate; `ClassicScreen`, the gate's restore before a classic viewer (pages back to scale 1, then `Screen.restore()`), and `fit_dialogs()` while one is bridged (§13) |
| `src/ferry_watch/agent/cast_cdp.py` (new) | `Cdp` (the allowlist wrapper, C34; every call bounded to 2 s); `Worlds` (per-frame worlds, out-of-process frames' own sessions, the owner chain, invalidation on `framenavigated`/`framedetached`); `open_session()`, the one way the agent opens a CDP session: Playwright makes a session even when nobody waits for it any more (a cancelled task, a bound run out), so such a late one is detached as soon as it's there (found in review: each stream session cancelled while opening left one attached for the page's life) |
| `src/ferry_watch/agent/cast_frames.py` (new) | `FramePump` (live/still/paused, seeding, window, repeat cache) |
| `src/ferry_watch/agent/cast_pages.py` (new) | `PageList` (ids, at most 8 listed, one CDP session each, titles from `Target.getTargetInfo`) |
| `src/ferry_watch/agent/cast_fields.py` (new) | `frame_rect()` (PROTOCOL §5.3.3); `FieldMapper` (schedule, assembly, `fields`/`fields_ok`, the 2 revisions held); `FocusReporter` |
| `src/ferry_watch/agent/cast_input.py` (new) | `InputMapper` (tap staleness, move, wheel, pinch, keys with held-key tracking, text, compose, `choose`/`date`); `ImageRing` (the metadata of the last 16 images) |
| `src/ferry_watch/agent/dialogs.py` (new) | `DialogKeeper`: `keep()` registers Playwright's context dialog listener when the gate admits the first viewer of either kind, and opens a CDP session with `Page.enable` on each page of the context (in the background: `Page.enable` doesn't return while a dialog is open on that page, measured), which gives each dialog its frame's URL (`Page.javascriptDialogOpening`, matched to Playwright's dialog by page, type and message) and says when one closes without us (`Page.javascriptDialogClosed`: answered on the screen, or navigated away); keeps open dialogs across sessions and kinds; `attach(sink)` returns the open ones with the session's ids and reports later ones (`dialog_opened`, `dialog_closed`); `detach(sink)`; `answer(id, ok, text)`; `dismiss_all()`; `release()` (unregister, dismiss all, back to Playwright's auto-dismiss); `on_change(listener)` (a dialog opened or went) |
| `src/ferry_watch/agent/gate.py` | Subprotocol routing; `cast_allowed` admission; session pacing; `_Viewer.kind`/`close_code`; `close(reason, code)`; the `cast` factory; the connection cap; uncancellable teardown before `_gone`; `dialogs.keep()` at every admission; before a `binary` bridge, `screen.restore()`; while it's bridged, `screen.fit_dialogs()`, stopped and finished before the viewer counts as gone |
| `src/ferry_watch/agent/watch.py` | `checkout_target()`; `clock_info()`; `viewer_verdict(cart_id, kind) -> CloseCode | None` (§6.2); `close_viewer(reason, code)`; the hold-service step closes a cast viewer with 4005 when `cast_allowed` turns false |
| `src/ferry_watch/protocol.py` | `Configure.cast_allowed: bool = False` (the operator sends it only to agents ≥ 0.7.0, §8.3) |
| `src/ferry_watch/bcferries/site.py` | `SiteSession.context` and `.page` read-only properties (nothing else changes) |
| `src/ferry_watch/agent/main.py` | Build `Screen(...)` and `DialogKeeper(...)`; pass `cast=`, `screen=`, `dialogs=`, `verdict=` to `Gate` |
| `src/ferry_watch/agent/settings.py` | `xsettings_dir` property (`tmp / "xsettings"`) |
| `Dockerfile` (display stage) | `apt-get install … xsettingsd x11-xserver-utils` |
| `pyproject.toml` | version 0.7.0 |

Unchanged: `processes.py`, `lockdown.py`, `docker/browser/chromium-policy.json` and Chromium's launch arguments.

### 6.2 Gate changes (exact)

```python
CAST_SUBPROTOCOL = "fw.cast1"
MAX_CAST_FRAME_BYTES = 4096
MAX_CONNECTION_SECONDS = 6000
CAST_START_SPACING_SECONDS = 1.0

class CloseCode(IntEnum):  # in cast_protocol.py
    ENDED = 4000; REPLACED = 4001; REVOKED = 4002; DRAINING = 4003; TIME_CAP = 4004; WITHDRAWN = 4005
    PROTOCOL = 4400; NO_HELLO = 4408; RATE = 4429; INTERNAL = 4500
```

- **`Gate(…, cast=None, screen=None, dialogs=None, verdict=None)`.**
  - `cast` is a factory `(ws, verdict: Callable[[], CloseCode | None]) -> CastSession`.
  - `verdict` is `watcher.viewer_verdict`.
  - With `cast=None`, `fw.cast1` is refused with 400 (unit tests, and any build without the stream).
- **`_admit`.**
  - After the Origin and cart checks: `kinds = [p for p in offered if p in ("binary", "fw.cast1")]`;
    `len(kinds) != 1` → `Refused(400)`.
  - For `fw.cast1` with `verdict(cart_id, "cast") == WITHDRAWN` → `Refused(400)`, not counted in
    `self._failures`.
  - Build the `WebSocketResponse` with `protocols=(kind,)` and `max_msg_size` 4096 for cast, else 256 KiB.
  - Return `kind` too. Everything else is as built (prototype `proto/gate_cast.py`).
- **`_upgrade`, cast branch.**
  - `await asyncio.sleep(max(0, self._last_cast_start + 1.0 - monotonic()))` before the swap.
  - Inside the swap: `_disconnect(old)`, `prepare`, `self._last_cast_start = monotonic()`,
    `_serve_cast(viewer, cart_id)`.
- **`_upgrade`, rfb branch.** Inside the swap, after `_disconnect(old)`: `await self._dialogs.keep()`, then
  `await self._screen.restore()` (both bounded; the restore is a no-op when no stream ever ran; as built it also
  pinches zoomed pages back to scale 1, §4.1), then today's code. While the bridge runs, `screen.fit_dialogs()`
  makes room for dialogs on a phone-sized screen; when the bridge ends it is cancelled and waited for (it puts the
  viewer's own screen back, bounded) before `_gone`, so the next viewer finds the screen as the classic one had it.
  The cast branch calls `keep()` too: dialogs are kept from the first viewer of either kind (§13).
- **`_serve_cast`.**
  - `asyncio.wait_for(session.run(), MAX_CONNECTION_SECONDS)`. A timeout closes 4004; an exception closes 4500,
    logged with the exception type only.
  - In `finally`: start `session.close()` as a task, and await it under `shield` in a loop until it is done, even
    across cancellations. Only then call `_gone(viewer)`. This ordering was a real bug in the first prototype.
- **`close(reason, code=CloseCode.ENDED)`.** Sets `viewer.close_code`, then disconnects; 4005 disconnects only a
  stream viewer (the kill switch doesn't concern a classic one). For codes 4000, 4002 and 4003 it then calls
  `dialogs.release()`, whether or not a viewer was connected: nobody will answer a dialog now, and the agent's own
  navigation afterwards (emptying the cart) mustn't wait on a kept `beforeunload`. So that no viewer is let in
  again meanwhile (KasmVNC's client reconnects a second after its socket closes, and a release is on no clock), the
  watcher's `checkout_cart()` is None from the moment a hold starts ending, before it closes the viewer and while it
  empties the cart (found in review: a classic reconnect kept the dialogs again and the emptying waited on a
  `beforeunload`). 4005 leaves dialogs open for the classic viewer the stream moves to. The replacing path sets `REPLACED`. `_upgrade`'s `finally` closes a
  stream's socket with `viewer.close_code or 1000`, and a classic viewer's with 1000 whatever the reason, as in
  0.6.4 (KasmVNC's client does look at how a socket closed).
- **`watch.py` `viewer_verdict(cart_id, kind)`**, in this order:

  ```python
  latest = self._store.latest
  if self._drain_reason is not None: return DRAINING
  if latest is None or not latest.vnc_allowed or not self._lockdown_ok(): return REVOKED
  if kind == "cast" and not latest.cast_allowed: return WITHDRAWN
  if self.checkout_cart() != cart_id: return ENDED   # includes release_due(...) on the clock
  return None
  ```

  It reads only in-memory state, so it costs microseconds.
- The heartbeat (20 s) and `CLOSE_TIMEOUT_SECONDS` are unchanged.

### 6.3 `CastSession` lifecycle

1. **Hello.** Wait up to 5 s for `hello` (else 4408). A wrong first message or a bad hello closes 4400.
2. **Target.** `target = watcher.checkout_target()`; if None, close with `verdict()` or 4000.
3. **Screen.**
   - `dsf = 2 if dpr ≥ 1.5 and w·h·4 ≤ 1.6 MP else 1`.
   - `changed = await screen.apply(w, h, dsf)`. If changed, poll `VIEWPORT` in the main world until
     `[innerWidth, innerHeight, devicePixelRatio]` match, up to 1 s.
4. **Pages.** Track `context.pages` and `context.on("page")`, ids from 1, at most 8 listed. For each tracked page,
   a CDP session (no `Page.enable`: the dialog keeper has its own; `Page.enable` doesn't return while a dialog is
   open on the page, which a reconnect often finds), titles via `Target.getTargetInfo`. The front page is the
   newest.
5. **Dialogs.** `opened = dialogs.attach(session)`: the gate's admission already started keeping; `opened` (with this
   session's ids, oldest first) is announced after `clock`, and later ones arrive as `dialog_opened`/`dialog_closed`
   callbacks. Each `dialog` message is `cast_protocol.dialog_message(id, page id, kind, url, message, default)`.
6. **Front page.**
   - `bring_to_front()`.
   - `DOM.enable` on its session.
   - The seeded screenshot image (flag bit 0), then `startScreencast`, dropping images whose
     `deviceWidth/deviceHeight` don't match the applied size for up to 1 s after a change. While a dialog is open on
     the front page, none of this can happen (the page answers no CDP, §13): it waits for the dialog's answer.
7. **Messages out.** `ready`, `pages`, `clock`, `dialog`s, the seeded image, then `FramePump`. Every outbound
   message passes `verdict()` first. The field map follows PROTOCOL §5.3.4, and focus reports PROTOCOL §5.4.
8. **Inbound loop.**
   - Decode: structure, then values (`error invalid`).
   - Rate-limit, then `verdict()`, then enqueue (coalescing) into the bounded queue of 64.
   - A worker task carries messages out in order and advances `n`.
   - A 1 s ticker calls `verdict()` and sends `clock` when it changed.
9. **Teardown** (bounded, every step `wait_for(…, 2)`):
   - cancel the pump, the worker, the ticker and timers;
   - release held keys and a mid-press mouse button;
   - `stopScreencast`;
   - `dialogs.detach(session)` (dialogs stay open);
   - remove listeners;
   - detach every CDP session it opened.

   No screen restore. Round 1's prototype took 10-17 ms for CDP.

### 6.4 The isolated-world scripts

The reference is `round2/cast_scripts.py`, exercised by `round2/s1_choose.py`, `s1b_mapcost.py`, `s2*`, `s7*`.
The production module keeps the same names, bounds and return shapes. Two differences from the reference:
- The reference calls the scripts with `Runtime.callFunctionOn`, and `OWNER_HIT` on the resolved owner object.
  Production runs every script with `Runtime.evaluate` and `timeout: 100`, because `callFunctionOn` ignores
  timeouts. `OWNER_HIT` therefore reads the owner that `STORE_OWNER` kept under index `k`.
- The reference makes four sequential round trips per map. Production evaluates the main frame and the child frames
  in parallel, and folds `VIEWPORT` and the owners' visibility into the main frame's `FIELDS` call.

| Script | Runs in | Arguments | Returns |
|---|---|---|---|
| `FIELDS(ev, keep1, keep2)` | each evaluated frame's world | the evaluation's number and the two the agent still holds for map revisions | `{f: [...], trunc, slow, clip, vv: [pageLeft, pageTop, offsetLeft, offsetTop, scale] (null but in the main frame), cw, ch, own: [[k, visible] …]}`, each field with `i, tag, type, im, ac, ekh, cap, max, ro, r, m` (+ `n, opts, ot, sel` / `v`); bounds per PROTOCOL §5.3.2; keeps the elements of `ev`, `keep1` and `keep2` in the world's global and drops the rest; `own` says which kept owner iframes intersect the clip |
| `STORE_OWNER(k)` | `callFunctionOn` on the owner iframe resolved into the parent's world | `k` | stores `this` as owner `k` (at most 64 a world) |
| `OWNER_HIT(k, points)` | the parent's world | `k`, ≤ 2048 numbers | `{o: [x, y], h: [0/1 …], focused, vis, slow}`: the content-box origin, per-point delivery to the iframe (inside the visual viewport for the main frame), whether the iframe is focused, and whether its box intersects the clip |
| `CLICK_POINTS(ev, i, mode)` | the field's frame world | ints | `{pts: [...], sel, v}` or `{why: "gone" | "changed"}` |
| `IS_FOCUSED(ev, i)` | the field's frame world | ints | bool |
| `FOCUS()` | the main frame's world, then each focused owner's child | — | `{f, clip, vv}` (the focused element as a field), `{frame: k, vv}` when it is owner iframe `k`, or null |
| `VIEWPORT()` | the main frame's world | — | `[innerWidth, innerHeight, devicePixelRatio, pageLeft, pageTop, offsetLeft, offsetTop, scale, navigator.language]` |

Evaluations are numbered by the agent (`ev`), apart from map revisions: an evaluation that finds the same map
gives `fields_ok`, and `choose`/`date` reach the elements through the `ev` behind the revision they name.

### 6.5 Input mapping tables (normative for the agent)

**Named keys** (`key`, `code`, Windows VK):

| Key | `code` | VK |
|---|---|---|
| Backspace | Backspace | 8 |
| Delete | Delete | 46 |
| Enter (`keyDown` with `text: "\r"`) | Enter | 13 |
| Tab | Tab | 9 |
| Escape | Escape | 27 |
| ArrowLeft | ArrowLeft | 37 |
| ArrowRight | ArrowRight | 39 |
| ArrowUp | ArrowUp | 38 |
| ArrowDown | ArrowDown | 40 |
| Home | Home | 36 |
| End | End | 35 |
| PageUp | PageUp | 33 |
| PageDown | PageDown | 34 |
| Shift (`rawKeyDown`/`keyUp`, `ph` only) | ShiftLeft | 16 |

Modifiers: `shift` or a held Shift → 8; `word` → 2.

**Characters** (`key {c}`):
- `keyDown` with `key = c`, `text = c`, `unmodifiedText = c.lower()`.
- `code` = `Key<A-Z>` / `Digit<0-9>` for ASCII letters and digits, else "".
- VK = the uppercase ASCII code for letters and digits, else 0.
- Then `keyUp`, unless `ph: down`.

**`choose` and `date`**: PROTOCOL §6.11-6.12 as written, with the scripts above. **Locale**: `navigator.language`
read once per session in the main world must start with `en-US`, else `date` answers `unsupported`.

## 7. Viewer design (workstream B)

### 7.1 Files

`vnc-edge/site/fw/`:
- `viewer.html`: no inline script or style. Its static markup holds the canvas, the gesture layer, the proxy, the
  status overlay and the hidden escape bar (`#fw-escape`).
- `viewer.css`.
- `viewer/core/*.js`: **DOM-free** ES modules, testable in Node.
  - `protocol.js`: encode/decode, header parse.
  - `text.js`: `normalise()`, `split()`.
  - `fields.js`: `mapUsable()`, `fieldAt()`, `overlayFor()`.
  - `gestures.js`: the touch/mouse state machine with an injected clock.
  - `predict.js`: confirmed scroll prediction.
  - `layout.js`: the §8.1 box and the coordinate transforms.
  - `reconnect.js`: `nextAction()` for close codes.
- `viewer/main.js`, `socket.js`, `frames.js`, `input.js`, `keyboard.js`, `overlays.js`, `sheets.js`: the DOM glue.
- `session.js`: shared with the shim (§8.4), plus the escape-hatch watchdog.

### 7.2 Connection (`socket.js`)

- Read `#t=<trip>&g=<gen>`; the nonce and the pending ticket come from storage via `FwSession`.
- Open `fw.cast1`; send `hello` clamped (PROTOCOL §8.1); `FwSession.viewerReady()` after the first `ready`.
- Close codes and backoff via `core/reconnect.js` (PROTOCOL §3.6-3.7); on 4005, the escape hatch.
- `resize` on `resize`/`orientationchange` (not when only `visualViewport` changed: the keyboard); `visible` on
  `visibilitychange`.
- A reconnect resets all session state (message numbers from 1).

### 7.3 Rendering (`frames.js`, `core/layout.js`)

- One full-viewport `<canvas>` (backing store = CSS size × `devicePixelRatio`). viewer.html doesn't ask for
  `viewport-fit=cover`: a phone's browser keeps the page, and so the image and where taps land, clear of the notch
  and the rounded corners in landscape, as for any page (found in review: with it, the image was drawn under them
  while the menu handle moved inside the safe area).
- Binary messages go through one promise chain: `createImageBitmap(new Blob([jpeg]))` into the slot, closing the
  old bitmap, then draw, then `ack`.
- Each image is drawn into the box of PROTOCOL §8.1 (letterboxed, `k ≤ 1`). A 2560x1440 desktop window sends
  1920x1440 and sees the 1920-wide page centred, crisp. The reviewers' 4400 on wide windows is gone.
- The prediction offset and the local pinch zoom are drawing transforms. Taps are blocked while either is drawn.

### 7.4 Gestures (`core/gestures.js`, `core/predict.js`, `input.js`)

- **The gesture layer.** A full-screen element above the canvas and the proxy receives all pointer, touch and wheel
  input (`touch-action: none`, `preventDefault`). The viewer page itself never scrolls or zooms.
- **Touch.**
  - A tap is under 8 px of movement and under 500 ms. It is sent on `touchend` with `seq` and `ms`; if it hits a
    usable map's text field, the proxy is focused first in the same task.
  - 8 px or more starts a scroll: `wheel` at most every 33 ms with summed deltas. The velocity is filtered
    (0.8 new / 0.2 old).
  - A release within 80 ms of the last move above 0.3 px/ms coasts with exponential decay (0.997/ms default;
    tuned on devices). Any touch stops a coast, and a touch that stops a coast is not a tap.
  - Two fingers start a local pinch: the content under the start centroid follows the current centroid, zoomed by
    the distance ratio. At the end, one `pinch {seq, x0, y0, x1, y1, scale}`. The local zoom is kept until an image
    arrives with a different `scale` (or for 600 ms).
- **Prediction** (§4.5): shift only after an image confirms the document is moving, cap at deltas since that
  image, drop 250 ms after the last wheel.
- **Mouse.**
  - `move` from `pointermove` (≤ 1 per 33 ms, latest position, only over the image).
  - A click is a `tap` on `pointerup` with `ms` = the press duration.
  - `wheel` from `wheel` events (line mode × 40), summed per 33 ms.

### 7.5 Keyboard (`keyboard.js`, `core/text.js`)

**The proxy.**
- Attributes per PROTOCOL §8.5; `pointer-events: none`.
- `beforeinput`:
  - `insertText` with one code point → `key {c}`; with more → `text` (normalised, split);
  - `insertReplacementText`, `insertFromPaste`, `insertFromDrop` → `text` from `dataTransfer`;
  - `deleteContentBackward`/`Forward` → `key Backspace`/`Delete`;
  - `deleteWordBackward`/`Forward` → the same with `word`;
  - `insertLineBreak`/`insertParagraph` → `key Enter`.

  Each is `preventDefault()`ed, so the proxy stays empty. As built, with one exception: where the proxy's
  `autocapitalize` is `words` or `sentences` (names, addresses, free text), it keeps the last 64 code points typed
  since the field was tapped, because a phone's keyboard decides capitals from the text before the caret (an
  always-empty field would capitalise every letter). It is emptied whenever the pod's caret may have moved (a
  tap, a focus change, a navigation key), and an `insertReplacementText` on that text is left to the browser and
  sent as the difference (`Backspace`s, then the new text).
- Compositions: `compositionupdate` → `compose`; `compositionend` → `text` (or `compose ""`), then the proxy is
  cleared. An `input` with no preceding `beforeinput` (some autofill paths) sends the proxy's value as `text` and
  clears it.

**Physical keyboards (computers).**
- `keydown`/`keyup` → `key` with `ph` per PROTOCOL §8.4, whether or not the proxy is focused (type-ahead and
  keyboard navigation work). A held key's auto-repeats are further `down`s (at most one per 25 ms), which the agent
  dispatches with `autoRepeat` (found in review: holding Backspace or an arrow acted once, as CDP never repeats).
- Dead keys and IME fall through to the composition path.
- On window `blur`, every key sent down is sent up.
- Cmd/Ctrl combinations are never sent; paste works through `beforeinput insertFromPaste`.

**Paste on touch devices.** The menu sheet's "Paste" reads `navigator.clipboard.readText()` inside its own tap and
sends it as `text` (normalised) to the focused field.

### 7.6 Pickers, dialogs, tabs, chrome (`overlays.js`, `sheets.js`)

- **Overlays.**
  - Native `<select>`/`<input type=date|time>` per PROTOCOL §8.3: only on a usable map, for `m` = 511 fields fully
    in the image, not `trunc`.
  - Options are built with `new Option(label)` and `disabled` flags.
  - Overlays are hidden while scrolling or zooming, and always sit below the viewer's own controls.
  - A pick the agent couldn't carry out (`error failed`/`stale`/`unsupported` for `choose` or `date`) takes that
    field's overlay away for the rest of the map revision, and a short notice says "That didn't take. Tap the field
    to try again." The next tap there reaches the page's own control (found in review: on a custom select whose
    mousedown is cancelled, the picker came back after every failed pick and the page's control never got a tap).
- **The option sheet** for `focus.popup`.
- **The dialog sheet**: the origin heading per PROTOCOL §8.8, the message in a `<p>` via `textContent`, OK/Cancel,
  and a prompt input with the password rules. It is styled as page content, not as viewer chrome.
- **The menu.**
  - A slim edge handle (right edge, vertically centred, 24×56 px, safe-area aware) opens a bottom sheet with: time
    left (from `clock`), the page list (title and origin, a close button for non-main pages), "Paste", "Use the
    classic viewer", "Back to Ferry Watch".
  - The handle shows a count badge when more than one page is open.
- **Status overlay.** "Connecting…", "Reconnecting…" (with the classic button), "This checkout has ended", "Opened
  elsewhere on this device" ("Show it here").
- **Accessibility.** Real `<button>`s; the canvas has `role="img"` and an `aria-label`.

### 7.7 The escape hatch

**"Use the classic viewer"** (PROTOCOL §8.10):
1. `FwSession.useClassic(trip, gen)` sets `record.viewer_fallback = true`.
2. Close the socket (1000).
3. `location.replace(FwSession.classicUrl(trip, gen))`: exactly the 0.6.4 vnc.html fragment, built by the same
   function the landing page uses.

The shim then offers `binary` with the nonce. The gate replaces the stream viewer after its teardown and restores
the screen, then bridges KasmVNC; a dialog left open is on the screen for the classic viewer to answer. Verified in round 1: the KasmVNC client connected 4 s after
the click, with no ticket and the same lock.

**The watchdog in `session.js`.** On `viewer.html` it un-hides `#fw-escape` 3 s after load unless
`FwSession.viewerReady()` was called, and on any `error`/`unhandledrejection`. The bar's button is wired by
`session.js` itself, so it works when every module failed to load. Modules are served with `Cache-Control:
no-store` (the server-level header already covers `/fw/`), so a deploy can't mix old modules with a new page.

## 8. Plumbing (workstream C, with the landing page in B)

### 8.1 The setting

Migration **`0006_checkout_viewer.sql`** (expand only):

```sql
-- Ferry Watch 0.7.0: each user's checkout viewer. 'classic' is KasmVNC (everyone, by default); 'stream' is the
-- streamed viewer beta. A 0.6.4 ferry-web ignores the column; its inserts get the default.
ALTER TABLE user_settings ADD COLUMN checkout_viewer text NOT NULL DEFAULT 'classic'
    CHECK (checkout_viewer IN ('classic', 'stream'));
```

`ferrywatch_web` already has the table's grants. A user without a row is `classic`.

### 8.2 API

**`PATCH /api/me`**
- Body: `{"quiet_hours"?: {...}, "checkout_viewer"?: "classic" | "stream"}`, with at least one key.
- `{}` and unknown keys stay 400 with field `quiet_hours`, as today. A bad viewer value is 400 with field
  `checkout_viewer`. Setting `stream` when the user may not see the beta (§8.3) is 403.
- `WebStore.set_settings(uid, *, quiet=None, viewer=None)` upserts only what's given.

**`GET /api/state`**: `settings: {quiet_hours, checkout_viewer, stream_viewer_offered: bool}`.

**`POST /api/trips/{id}/vnc`**: unchanged request. The response adds `"viewer": "classic" | "stream"`.

### 8.3 Choosing the viewer, and the kill switch

**The values key** `checkout.streamViewer`: `off` | `admins` | `everyone` (the launch value is §16 decision 1).
It is rendered into both configs, unless it is `off`, which both default to:
- ferry-web: `WebConfig.checkout.stream_viewer`;
- ferry-control: `ControlConfig.checkout.stream_viewer` → `HubSettings.stream_viewer`.

Both configs are `forbid_unknown_fields`, so the key ships with the code that reads it. With `off` the rendered
configs are 0.6.4's, so 0.6.4's images start with the 0.7.0 values (found in review: rendering `off` too made
ferry-web, ferry-control and the db hook refuse their configs after an image-only rollback). **`offered(user)`** =
`everyone`, or `admins` and `users.is_admin`.

**ferry-web** sets `viewer = "stream"` only when all three hold:
1. the user's `checkout_viewer` is `stream`;
2. `offered(user)`;
3. the pod's `browser_pods.agent_version` parses as ≥ 0.7.0 (`ferry_watch.versions.at_least`, shared with
   control). Null or unparseable means classic.

The Settings switch is shown only when `offered(user)`.

**`checkout_url(origin, trip, gen, ticket, viewer)`**:
- classic: `{origin}/t/{trip}/g/{gen}/#{ticket}` (byte-identical to 0.6.4);
- stream: `{origin}/t/{trip}/g/{gen}/#v=s1&t={ticket}`.

The ticket format and claims are unchanged: 0.6.4 pods decode `disp` with `forbid_unknown_fields`.

**ferry-control** (security review 6). `build_configure(…)` sets
`cast_allowed = user_active and offered(trip owner)`. `TripRow` gains `owner_is_admin` from the trips query.
- `Configure` is `forbid_unknown_fields` on 0.6.x agents, so `Hub._send` drops `cast_allowed` from the encoded
  message for peers whose hello `agent_version` is below 0.7.0. `Peer` keeps the hello's version.
- A rollback to 0.6.4 ferry-control sends no `cast_allowed`; 0.7.0 agents default it to false, refuse new streams
  and close live ones with 4005 on the next configure.
- Flipping `streamViewer` rolls ferry-control and ferry-web (config change). Agents reconnect, get the new
  configure, and live streams close with 4005, which takes the viewer to KasmVNC.

The opt-in itself is not enforced at the pod. It is a user preference, not an authorization boundary: a classic
ticket offered with `fw.cast1` by hand opens the owner's own checkout, under the same admission and the kill
switch.

### 8.4 vnc-edge landing and the shared session logic

**`fw/session.js`** (new, a classic script defining `window.FwSession`, frozen). Moved out of `shim.js` and
`landing.js`:
- `where(pathOrHash)` → `{trip, gen}`;
- `record(key)`/`write`;
- `nonce()`;
- `offer(record, protocol)` → `[protocol, "fw.n1.N", ("fw.t1.T")]` or null;
- `opened()`;
- `pendingTicket()`;
- `expired(record)`;
- `classicUrl(trip, gen)`: 0.6.4's `viewerUrl()`, including `anti_aliasing=1` and the touch-only
  `virtual_keyboard_visible=1`;
- `streamUrl(trip, gen)` → `/fw/viewer.html#t=<trip>&g=<gen>`;
- `useClassic(trip, gen)`: sets `viewer_fallback`;
- `viewerReady()`;
- the escape-hatch watchdog, active only on viewer.html.

**`landing.js`**:
- Fragment parsing: a fragment starting `fw1.` is the 0.6.4 form (classic). Otherwise
  `URLSearchParams(fragment)`: `t` is the ticket, and `v=s1` means stream.
- It stores `record.viewer` ("classic"/"stream") when a fresh ticket arrives, unless `record.viewer_fallback`.
- It opens `streamUrl` when `record.viewer === "stream" && !record.viewer_fallback`, else `classicUrl`.

**`shim.js`**: its socket wrapper calls `FwSession` (same behaviour); its phone IIFEs are unchanged. `build.py`
concatenates `session.js` + `shim.js` into the served `fw/shim.js`, so vnc.html still has exactly one inserted
script line and its CSP hash is unchanged. It serves `fw/landing.js` the same way, `session.js` + `landing.js`, and
landing.html loads only that: vnc-edge rolls one replica at a time (and back, on a rollback), so a page and its
script can come from different versions, and neither may need another file of its own version. `landing.js` also
works on 0.6.4's landing.html. Found in review: 0.6.4's page with 0.7.0's script never opened the checkout.

### 8.5 Frontend

- **`schema.ts`**: `settings.checkout_viewer: z.enum(["classic","stream"]).default("classic")`,
  `settings.stream_viewer_offered: z.boolean().default(false)`; `CheckoutSchema` gains `viewer` (optional,
  default classic).
- **`api.ts`**: `setCheckoutViewer(viewer)` → `PATCH /api/me {checkout_viewer}`.
- **`AccountCard.tsx`**: when offered, a `Switch` "New checkout viewer (beta)", with one line: "Checkout looks and
  scrolls like a normal page on your phone. If anything goes wrong, the checkout has a 'Use the classic viewer'
  button." It saves at once with a toast.
- **`CheckoutPage.tsx`**: when the response's `viewer` is stream, a muted line under the buttons: "Opens in the new
  checkout viewer (beta)."

### 8.6 Rollout and compatibility

| Situation | Result |
|---|---|
| 0.7.0 ferry-web/control, pod from before the upgrade (agent 0.6.x) | classic URL (version check); no `cast_allowed` sent to it |
| 0.7.0 ferry-web, 0.6.4 vnc-edge still serving (rollout seconds) | a stream URL lands on the old landing page ("No checkout here"); opening again after vnc-edge rolls works. Order vnc-edge first, or keep `streamViewer: off` until it has |
| 0.6.4 ferry-web, 0.7.0 vnc-edge/pods | classic URLs only; the new landing page handles the 0.6.4 fragment |
| vnc-edge rolling between 0.6.4 and 0.7.0 replicas (either way) | a landing page and its script may come from different versions: each served script carries `session.js`, and `landing.js` works on 0.6.4's page (`test_checkout_edge.py` opens a classic link with each pairing) |
| Rollback to 0.6.4 (all services) | the column is ignored; classic URLs; running 0.7.0 pods get `cast_allowed` absent = false: new streams refused, live ones closed with 4005 → classic |
| Rollback of the images only, keeping the 0.7.0 values | set `checkout.streamViewer: "off"` first, or in the same change: with any other value the configs carry the key, which 0.6.4's ferry-web, ferry-control and db hook refuse, and the upgrade fails |
| Kill switch `streamViewer: off` | no stream URLs, no Settings switch; every pod's `cast_allowed` false within one configure push: live streams move to classic |

## 9. vnc-edge build, nginx, CSP, smoke test (workstream B)

**`build.py`**:
- `BUNDLES`: `fw/session.js` in front of `fw/shim.js` and of `fw/landing.js`, each written under its own name;
  `session.js` is also copied as is (viewer.html loads it).
- `viewer_csp(checkout_host)` returns the C38 policy, and `default.conf.in` gets a `__FW_VIEWER_CSP__` placeholder.
- The build fails if `viewer.html` contains an inline `<script>` without `src`, a `style=` attribute, an `on…=`
  handler, a `<style>` element, or lacks `#fw-escape`.
- vnc.html's patch and `csp()` are untouched. A test pins vnc.html's served CSP to the 0.6.4 string.

**`default.conf.in`**: one exact-match location. It takes precedence over the `^/(…|fw)/` regex. nginx's
`add_header` in a location replaces the server's set, hence the full set:

```nginx
location = /fw/viewer.html {
  root /usr/share/fw-vnc;
  add_header Content-Security-Policy "__FW_VIEWER_CSP__" always;
  add_header Referrer-Policy no-referrer always;
  add_header X-Content-Type-Options nosniff always;
  add_header Cache-Control no-store always;
}
```

The viewer's modules and CSS are served by the existing `/fw/` location, which inherits the server's
`Cache-Control: no-store`.

**`smoke-test.sh`**:
- `/fw/viewer.html`, `/fw/session.js`, `/fw/viewer/main.js`, `/fw/viewer/core/protocol.js` and `/fw/viewer.css`
  answer 200, and the modules have `Cache-Control: no-store`.
- The viewer's CSP equals the C38 string.
- vnc.html's CSP is unchanged.
- The served `shim.js` and `landing.js` start with `session.js`'s content.

The vnc-edge **Dockerfile** is unchanged.

## 10. Tests

### 10.1 Agent, gate and control (workstream A unless marked C)

| File | Covers |
|---|---|
| `tests/test_cast_conformance.py` (new) | Loads `tests/fixtures/cast1/`: every `messages.json` case gives its outcome (carried out / `error invalid` / 4400); `headers.json`; `rects.json` through `frame_rect()`; `mapping.json` through `InputMapper` with a fake CDP; `text.json` outputs all pass the value rules |
| `tests/test_cast_protocol.py` (new) | 50k-mutation fuzz with no unexpected exception; typed-text rules; outbound cleaning (`origin_of`, credential-token stripping, caps) |
| `tests/test_gate.py` (extend) | Both, neither or an unknown kind → 400; cast refused when `cast_allowed` false (not a failure); cast first connect, reconnect by nonce, classic after cast and the reverse with no ticket; a ticket refused once locked whichever kind locked it; 4097-byte message → 1009; `close(reason, code)` codes; 4001 only after a slow teardown, including when cancelled mid-teardown; cap → 4004 with a short cap; session pacing (two cast upgrades 0.2 s apart: the second starts ≥ 1 s after the first, no refusal); **restore happens before a binary bridge and never at a cast teardown, a reconnect or `close()`; dialogs are kept at every admission and released only by `close()` with 4000/4002/4003**; `viewer_changed` sequence across a kind switch |
| `tests/test_stream.py` (new; fake CDP session, fake `Screen`, fake clock) | The allowlist (methods, `Runtime.evaluate` without a world or timeout refused, viewer strings can't reach expressions); hello timeout 4408; message before `ready` 4400; value errors → `error invalid` and the drop limit → 4429; queue bound and **wheel/move coalescing** (100 wheels in one tick → one dispatch with the exact sum; order kept around a `tap`); ack window, newest-wins, repeat cache; **live/still/paused transitions** with synthetic frame rates; **seeded image** on start, `page show`, `visible: true`; **`verdict()` before every input and output and on the 1 s tick**: freeze the watch loop, advance the clock past `release_due` while the client sends only acks → close 4000 within 1 s and no image after the verdict changed; **tap staleness** (moved scroll → `error stale` and no Input call); pinch → pinch + pan calls; key `ph` tracking, ≤ 6 held, released at teardown; `choose`/`date` sequences: stale → no Input call at all, focus check failing → `Escape` only; teardown order and bounding; **caplog contains no typed text** after a card number, an IME string, a prompt answer and a bad-value `text` |
| `tests/test_cast_cdp.py` (new) | `call()`/`script_of()` refuse anything but numbers; every method and parameter shape outside C34 refused before sending (`Runtime.evaluate` without a world it made, without `returnByValue` or the 100 ms timeout, or of any other expression; `callFunctionOn` but `STORE_OWNER` on a resolved owner; right/middle buttons, modifiers but Shift/Ctrl, touch); errors name the method only; worlds for in-process and out-of-process frames, owners kept once, dropped on navigation and after a failed call; depth and count limits |
| `tests/test_screen.py` (new) | Fake `xrandr`/`xsettingsd` on `PATH`: mode names, DPI file and mode, SIGHUP on change; **apply twice with the same size runs nothing the second time**; DPI before mode; restore deletes the modes, kills a hung `xrandr` within 3 s, never raises; any number of sizes with ≤ 16 modes at once, the least recently used making room |
| `tests/test_dialogs.py` (new) | Keeper: dialogs kept across `detach`/`attach`, ids per session, origin matched per page (either event first; "" after 0.5 s), a dialog answered on the screen or on a closed page goes (`dialog_closed`), our own answer's closing never takes another dialog, `keep()` doesn't wait for `Page.enable`, `dismiss_all`, `release` unregisters (a new dialog is auto-dismissed again), answers never logged |
| `tests/browser/test_checkout_dialogs.py` (new) | The classic viewer on the real pod screen: without a viewer a dialog is dismissed as in 0.6.4; with one, a confirm shows on KasmVNC's canvas and OK clicked over VNC returns true; a prompt typed into returns the text; a dialog survives a reconnect and is dismissed by `gate.close`; an iframe's dialog; on a phone, the whole confirm, alert and prompt are on the canvas and a tap on OK answers each |
| `tests/test_watch.py` (extend) | `viewer_verdict` order and codes; a cast viewer closed with 4005 when `cast_allowed` turns false |
| `tests/test_protocol.py` (extend) | `Configure.cast_allowed` default false; a 0.6.4-shaped configure decodes |
| `tests/test_hub.py` (C) | `cast_allowed` per `offered(owner)` and `user_active`; dropped for a peer with `agent_version` 0.6.4; included for 0.7.0; a `streamViewer` change pushes a new configure |
| `tests/test_control_config.py` (C) | `checkout.streamViewer` parsing and default |

### 10.2 Browser tier: the real pod screen (workstream A)

`tests/browser/test_checkout_stream.py` (new) uses a **Python protocol client** (aiohttp) as the viewer.

**The fixture.**
- The scratch sites become `tests/fixtures/checkout-site/{shop,pay}`, plus `occl.html`/`card3.html` from round 2
  and `inner.html`, `anim.html`, `swap.html` from the feasibility review. The shop is served on 127.0.0.1 and the
  card on 127.0.0.2: two sites, so the card iframe is out of process with PodBrowser's own arguments (no
  `--host-resolver-rules`).
- Assert the card iframe is its own CDP target, or the test is meaningless.

**Tests:**
1. Admission, then images: the first image is screenshot-seeded, `w·2 × h·2` for a 390x664/DPR-3 hello and 1x
   for 1280x800/DPR 2; the pod tab reports the hello's size and DPR 2.
2. **Reconnect with the same hello on `swap.html` with typed values**: no resize event, values kept, and a first
   image arrives within 1 s on a static page.
3. Rotation: one resize. Phone → desktop → phone: every `innerWidth` sequence monotone.
4. Taps focus the main field and the cross-site card field at three scroll offsets. A tap whose `seq` predates a
   scroll gets `error stale`, and the page logs no click.
5. Wheel momentum with the iframe present; a 120 Hz client stream of wheels finishes within 100 ms of its last
   message.
6. Pinch to 2 about (x0, y0) with a pan to (x1, y1): the content point ends at (x1, y1) ±2 px; ctrl+wheel and no
   TouchEvent; `maxTouchPoints` 0.
7. Text with a pasted LF into an input (spaces, not 4400) and a textarea (kept); `key {c}`, IME, Backspace into the
   card iframe; Tab advances; desktop `ph` keys with Shift give `shiftKey`.
8. **`choose`/`date` on `occl.html`**: covered → `error stale`; partly covered → set; custom select → `error
   failed`; iframe select under the parent footer → `error stale`. **The footer's click log is empty after all
   of them.**
9. **Field map**: rectangles within 2 px of the truth at scale 1 and at scale 2 with a visual viewport offset, main
   and iframe (PROTOCOL §11.3's vectors); covered fields absent; `inner.html` scrolled by the client → a new map whose
   rectangles moved; `anim.html` → a map within 500 ms despite the spinner.
10. **Bounds**: the bomb pages (100k labels, 5,000 inputs with 20,000-character `autocomplete`s, 5,000 inputs) →
    each JSON message ≤ 64 KiB, `trunc`, agent RSS growth under 20 MB, evaluation wall time under 150 ms (500 ms
    for the first evaluation in a new document, which creates the worlds: 172 ms measured), timed once the page
    has loaded (an evaluation while it loads waits for a busy renderer: 0.7 s measured with the `autocomplete`
    page, still within the 2 s call bound); a 20 M-character title → `pages` title of 80. A 200,000-input page
    didn't finish loading on the headed pod screen in 2 minutes, so it can't be part of the tier; the script's scan
    stops at 500 candidates whatever the page holds.
11. **Still mode**: `anim.html` with no input for 2 s → images ≤ 2/s, and Chromium CPU over 5 s under 40% of a
    core; a tap → live again.
12. alert/confirm/prompt from the main page and the iframe, with the right `origin`; a dialog open across a
    reconnect is announced again and answerable.
13. Pop-ups: `window.open`, `target=_blank`, from the iframe; OTP typing; `page close`.
14. **Then the stock KasmVNC client through the shim reconnects with the same nonce**: DPR 1 and 1280x1000 before
    its resize (the page reports it among its sizes before the client's own; the restore's first event can be the
    DPR change alone), xsettingsd stopped, no fw modes left, and `test_checkout_screen.py`'s fitted/click/type
    assertions pass. The same when a classic viewer came first (classic → stream → classic, which Xvnc's
    replaced launch mode broke until review).
15. Hold end: `gate.close` → 4000 and open dialogs dismissed.

`tests/browser/test_checkout_screen.py` stays as is and must pass. `docker/browser/selftest.py` gains a screen
check: apply 390x664 at 2x, the page reports it, restore, the page reports 1280x1000 at 1x.

### 10.3 The viewer (workstream B)

- **`vnc-edge/tests/*.test.mjs`** (new, `node --test`, Node 22 as in the `web` job): the `core/` modules against
  every conformance file the viewer column of PROTOCOL §10.1 names.
- **`tests/browser/test_stream_viewer.py`** (new): the real viewer files under the real C38 CSP against a **fake
  agent** (aiohttp, `tests/browser/fake_agent.py`, driven by the conformance transcripts). It runs in iPhone
  emulation (390x664, DPR 3, touch, mobile), a 1280x800 desktop, and a **2560x1440 desktop**.
  1. `hello` (clamped to 1920 on the wide desktop, image letterboxed and taps mapped through it); acks in order;
     slots and repeats.
  2. `tap` carries the drawn `seq`; no tap while a prediction or local zoom is drawn.
  3. On a usable map the proxy is focused **within the `touchend` task** with the right attributes per field type;
     a **password field gives `type=text`, `autocomplete=off`**; credential tokens never appear; a map made
     unusable by `n` or `seq` gives no proxy, but the `tap` is sent.
  4. **A second tap inside the focused field sends `tap` and keeps the proxy focused; a drag starting on it sends
     `wheel`** (pointer-events).
  5. Typing gives `key {c}`; paste of `"4242\n"`, CRLF text and an emoji at position 256 gives PROTOCOL §11.4's messages;
     IME gives `compose` then `text`; desktop keys give `ph` down/up with Shift.
  6. Drag gives at most one `wheel` per 33 ms with exact sums; a flick coasts and stops; a tap during a coast sends
     no `tap`; on `inner.html`-style images (no `sy` change) no prediction shift is drawn.
  7. Two-point touch gives a local zoom and exactly one `pinch` with `x0..y1`.
  8. Overlays only for `m` = 511 fields of a usable map; covered cells don't hit; a pick answered `error failed`
     takes its overlay away for the revision, with a notice (and on the pod, `test_stream_midpoint.py` with
     `occl.html`'s custom select).
  9. Dialog sheet: origin heading (same-origin, embedded, empty) and answer; page list show/close; menu Paste.
  10. Close codes: 4000, 4001, 4005 (goes to classic), 1006 backoff, three failures → classic prominent, the hard
      deadline → `/gone.html`.
  11. **`main.js` answering 404 → the escape bar appears within 3 s and works**; a thrown error in a module → the
      bar appears.
  12. **Zero CSP violations**. A dialog message, title and option label containing `<img src=x onerror=…>` show
      literally.

Playwright's `wait_for_function` compiles with `eval`, which this CSP refuses. Poll with `evaluate` instead.

`tests/browser/test_checkout_edge.py` (extend):
- the landing page takes both fragment forms;
- the classic form still produces 0.6.4's exact vnc.html fragment;
- the stream form opens `viewer.html`;
- the fallback is sticky across a fresh stream ticket;
- vnc.html's CSP equals the 0.6.4 string;
- the existing shim tests pass unchanged.

### 10.4 ferry-web and frontend (workstream C)

- **`tests/test_db.py`**: 0006 applies on 0005 data; default `classic`; the CHECK rejects other values; a
  0.6.4-style insert works.
- **`tests/test_web_api.py`**:
  - `PATCH` `checkout_viewer` (stream, classic, invalid; stream when not offered → 403);
  - state carries `checkout_viewer` and `stream_viewer_offered` for `off`/`admins`/`everyone` × admin/non-admin;
  - the checkout URL: classic for a default user; the `#v=s1&t=` form for an opted-in, offered user with
    `agent_version` 0.7.0; classic for 0.6.4, null or garbage versions and when not offered;
  - the response's `viewer`;
  - the quiet-hours tests unchanged.
- **`tests/test_web_config.py`**: `checkout.streamViewer` values and default.
- **Frontend (vitest)**: the AccountCard switch shown only when offered, PATCHes and reflects state; schema
  defaults for an older server; CheckoutPage's beta line.

### 10.5 CI

- The `browser` job installs `xsettingsd x11-xserver-utils` next to KasmVNC and matchbox, and checks the tools are
  on `PATH`.
- The `web` job gains `node --test vnc-edge/tests/*.test.mjs` (no npm dependencies; Node 22's `--test` takes
  files and globs, not a directory).
- The image's smoke test covers the image.

## 11. Build plan

The three workstreams build against `PROTOCOL.md`, the conformance pack, and §8.3's URL format. Each step below
lists its files, the functions it adds or changes, its tests, and what "done" means. A step is done only when its
tests pass in CI.

### Step 0: the shared conformance pack (A and B together, 1 day, before anything else)

- **Files:** `tests/fixtures/cast1/{messages,headers,rects,hits,usable,text,gestures,mapping,close_codes}.json`
  and `tests/fixtures/cast1/README.md`. The README gives each file's schema and the PROTOCOL section it encodes.
- **Content:** written from PROTOCOL.md by the A and B leads together, seeded with:
  - PROTOCOL §4.4 and §11's vectors;
  - round 1's 15 valid and 22 invalid messages (`proto/fuzz_protocol.py`);
  - the round-2 measurements (`round2/out/s7*.json` for rects, `s1-choose.json` masks for hits);
  - every boundary of §6 and §7.1.
- **Acceptance:**
  - every PROTOCOL rule that A and B both implement has at least one case;
  - each case names its PROTOCOL section;
  - both leads sign off;
  - after this, a protocol change is a change to PROTOCOL.md **and** the pack in the same PR.

### Workstream A: agent, gate, pod image (10-12 days)

| Step | Files and functions | Tests | Done when |
|---|---|---|---|
| A1 | `agent/cast_protocol.py`: inbound structs, `check()`, `typed_text_ok()`, header pack/unpack, outbound builders, `CloseCode`, rate table | `tests/test_cast_conformance.py` (messages, headers, text), `tests/test_cast_protocol.py` | every `messages.json`/`headers.json`/`text.json` case passes; the fuzz finds no unexpected exception |
| A2 | `agent/screen.py` (`Screen.apply/restore/current`), `agent/settings.py` (`xsettings_dir`); `Dockerfile` display stage; `.github/workflows/ci.yml` apt line | `tests/test_screen.py` | no-op apply, DPI-before-mode, bounded restore all pass; the image builds with both tools on `PATH` |
| A3 | `agent/gate.py`: routing, `cast_allowed` admission, pacing, `close(reason, code)`, cap, uncancellable teardown, restore before a binary bridge, dialogs kept from every admission; `agent/dialogs.py` (`DialogKeeper`), wired into `agent/main.py` for the classic viewer's dialogs; `protocol.py` `Configure.cast_allowed`; `agent/watch.py` `viewer_verdict`, 4005 on withdrawal | `tests/test_gate.py`, `tests/test_dialogs.py`, `tests/test_watch.py`, `tests/test_protocol.py`, `tests/browser/test_checkout_dialogs.py` | all listed gate cases pass, including restore-only-before-binary and pacing; existing gate tests unchanged and green |
| A4 | `agent/cast_scripts.py` (the seven scripts, `call()`), `agent/stream.py`: `Cdp`, `InboundQueue`, `RateLimiter`, `FramePump` (live/still/paused, seeding), `Worlds`, `FieldMapper`, `FocusReporter`, `InputMapper`, `Pages`, `CastSession` | `tests/test_stream.py`; `test_cast_conformance.py` (rects through `frame_rect()`, mapping) | all listed cases pass, including verdict-on-output and caplog; **midpoint checkpoint (below) passed** |
| A5 | `bcferries/site.py` (`context`/`page` properties), `agent/main.py` wiring (`Screen`, `DialogKeeper`, `cast=`, `verdict=`; A3 already wired the properties, `DialogKeeper`, `dialogs=` and `verdict=` for the classic viewer's dialogs) | `tests/test_agent.py`/`test_watch.py` updates for `close_viewer(reason, code)` | the agent starts with the stream wired; 0.6.4 agent tests green |
| A6 | `tests/fixtures/checkout-site/` (all pages of §10.2), `tests/browser/test_checkout_stream.py`; `docker/browser/selftest.py` screen check | §10.2 tests 1-15 | all pass in the `browser` CI job; `test_checkout_screen.py` unchanged and green |
| A7 | Docs: design-reference §5.5 (subprotocols, close codes, cap, pacing, kill switch), §5.6 (the stream's processes, no new listener), C17, C33-C42, R4 wording, §13's pre-existing findings | review | merged with A6 |

### Workstream B: viewer and vnc-edge (10-12 days)

| Step | Files and functions | Tests | Done when |
|---|---|---|---|
| B1 | `site/fw/session.js` (extracted `FwSession`, `viewerReady`, the watchdog), `site/fw/shim.js` (uses it, behaviour unchanged), `build.py` bundling | existing `test_checkout_edge.py` and `test_checkout_screen.py` | both green with no test changes; vnc.html's served CSP equals 0.6.4's |
| B2 | `site/fw/landing.js` (fragment forms, `record.viewer`, routing), `landing.html` copy | `test_checkout_edge.py` additions (§10.3) | all pass |
| B3 | `viewer/core/{protocol,text,layout,reconnect}.js`, `viewer.html` (static escape bar), `viewer.css`, `viewer/{main,socket,frames}.js`; `tests/browser/fake_agent.py` | `vnc-edge/tests/{protocol,text,layout,reconnect}.test.mjs`; viewer tests 1, 10, 11, 12 | conformance files pass in Node; the 404 test shows the escape bar |
| B4 | `viewer/core/{fields,gestures,predict}.js`, `viewer/{input,keyboard,overlays}.js` | `vnc-edge/tests/{fields,gestures,predict}.test.mjs` (`hits`, `usable`, `gestures`); viewer tests 2-8 | all pass; **midpoint checkpoint passed** |
| B5 | `viewer/sheets.js` (dialog sheet with origin, option sheet, menu with Paste, pages, status, classic escape) | viewer test 9 | passes |
| B6 | `build.py` (`viewer_csp`, the markup checks), `default.conf.in` location, `smoke-test.sh` | `tests/browser/test_stream_viewer.py` complete; the image smoke test | zero CSP violations in every test; smoke test green |
| B7 | Docs: design-reference §7.9 (the viewer, its CSP, the landing routing, the escape hatch) | review | merged with B6 |

### Workstream C: ferry-web, ferry-control, DB, frontend (3-4 days)

| Step | Files and functions | Tests | Done when |
|---|---|---|---|
| C1 | `control/migrations/0006_checkout_viewer.sql` | `tests/test_db.py` | applies on 0005 data; 0.6.4 inserts work |
| C2 | `web/repo.py` (`set_settings`, snapshot, `held_cart.agent_version`), `web/api.py` (`update_me`, `open_checkout`, `checkout_url()`, `offered()`), `web/config.py` (`CheckoutConfig.stream_viewer`), `ferry_watch/versions.py` (`at_least`) | `tests/test_web_api.py`, `tests/test_web_config.py` | all §10.4 cases pass; `docs/api.md` updated |
| C3 | `control/config.py` (`checkout.stream_viewer` → `HubSettings.stream_viewer`), `control/configure.py` (`build_configure(…, cast_allowed)`), `control/store.py` (`TripRow.owner_is_admin`), `control/hub.py` (`Peer.agent_version`, `_send` dropping `cast_allowed` below 0.7.0) | `tests/test_hub.py`, `tests/test_control_config.py` | a 0.6.4 agent's configure decodes with the 0.6.4 schema; a 0.7.0 agent gets the field |
| C4 | `deploy/helmrelease.yaml` (`checkout.streamViewer` into both configs), `web/src/mt/{schema.ts,api.ts}`, `AccountCard.tsx`, `CheckoutPage.tsx` | vitest; `tests/test_deploy.py`; `tests/apiserver/test_render.py` | rendered configs contain the key unless `off`, and with `off` are 0.6.4's; frontend tests green |
| C5 | `pyproject.toml`/`web/package.json` version 0.7.0; release notes | — | — |

### The midpoint integration checkpoint (1 day, after A4 and B4)

The real `CastSession` behind the real `Gate`, on the pod screen (A's A6 fixture site), with the real viewer
modules in Playwright (phone and desktop contexts), served under the C38 CSP by a test server
(`tests/browser/test_stream_midpoint.py`, kept as a regression test). It must pass:
1. a tap into the main page and the cross-site card iframe, and the proxy focused in `touchend`;
2. a tap at scale 2 after a pinch with pan (zoomed map rectangles);
3. a paste with a LF into an input and a textarea;
4. a drag + coast at 60 Hz: the agent finishes within 100 ms of the last wheel;
5. `choose` through the overlay on `occl.html`'s clear select, and a covered one refused;
6. a reconnect with no resize on `swap.html`;
7. the 2560x1440 desktop letterboxed with correct taps.

Any disagreement between A and B is fixed in PROTOCOL.md and the pack first, then in the code.

### Final integration (3-4 days, including devices)

1. **`tests/browser/test_checkout_stream_e2e.py`** (new).
   - Path: a ticket from `tickets.mint` and a URL from ferry-web's `checkout_url(…, "stream")`, then the real
     landing page (vnc-edge build) → `viewer.html` → the gate + `CastSession` → the pod screen.
   - Steps: image; tap and type into the iframe card field through the proxy; select via the overlay; a 3DS pop-up
     OTP and close; a confirm dialog with its origin; a reconnect (no resize); the classic escape → KasmVNC
     connected with the same lock, DPR 1, the page state kept; `gate.close` → the classic shim goes to
     `/gone.html`.
   - Repeat in the desktop context. Zero CSP violations.
2. The classic path regression: `test_checkout_screen.py`, `test_checkout_edge.py` and the image smoke test pass
   unchanged. Opted-out users' URL is byte-identical to 0.6.4.
3. The kill switch on a staging deploy:
   - `streamViewer: off` closes a live stream with 4005 within one configure push, and the viewer lands in
     KasmVNC;
   - an old pod gets classic;
   - a rollback of ferry-control alone leaves 0.7.0 pods refusing streams.
4. **On devices, before `streamViewer: everyone`.**
   - Devices and links: iPhone Safari and the iOS home-screen app; one Android Chrome; LTE.
   - Keyboard: it opens on a tap for name, email (email keyboard), phone, card (numeric); a second tap inside the
     field moves the caret; Gboard swipe; QuickType; iOS AutoFill of contact and card into the proxy; the menu's
     Paste.
   - **With a saved `auth.chestr.dev` login on each phone: tapping a pod password field and an
     `autocomplete=username` field offers no chestr.dev credential, and no "save password" prompt appears.**
   - Native select wheel and date picker; rotation; pinch feel (with pan); momentum constants.
   - A real 3-D Secure approval with a bank-app switch and return: no resize, the page as left.
   - An animated page's still mode, judged on how it feels.
   - The classic escape mid-checkout.
   - BC Ferries' real checkout at phone width with the desktop UA.

## 12. Risks and mitigations

1. **iOS keyboard opening with a `pointer-events: none` proxy** (programmatic focus inside `touchend`; unverified on
   a device). Mitigations: the "Tap to type" chip fallback; the escape hatch; the device checks (§11, final integration item 4) before widening.
2. **Password managers on iOS/Android** may still offer credentials to a `type=text` field they judge a username or
   password field by heuristics. Mitigation: no credential tokens, no `name`/`id` hints, `autocomplete=off` on
   anything password-like; verified on devices (§11, final integration item 4); if a phone still offers chestr.dev
   credentials, the proxy for password fields falls back to the "Tap to type" chip with a plain textarea, as
   KasmVNC uses.
3. **Chromium behaviour changes on bumps.** Areas at risk: OOPIF input routing, `synthesizePinchGesture`
   coordinates, Xft/DPI handling, screencast and screenshot metadata, `Runtime.evaluate` timeouts. The browser-tier
   tests (§10.2) guard each, and Renovate's Chromium bump is already a PR.
4. **CPU.** About 1 core of Chromium while continuously scrolling at 2x, inside the pod's 1.5-core limit. Animated
   pages are bounded by still mode (about 20%). Measure throttling in the cluster during the beta.
5. **Mobile data.** 8.6 Mbit/s bursts while scrolling; 40 kB/s for an animated page left alone. The ack window
   degrades gracefully. Adaptive quality is next.
6. **BC Ferries' real pages.**
   - Nested iframes: the owner chain handles any depth to 3.
   - Custom selects and date widgets are ordinary DOM and take taps.
   - A page that remounts at breakpoints is reflowed once per first connect and once per escape to classic. Nothing
     is lost on reconnects or rotation within one layout class.
7. **Fingerprint.** DPR 2 on phones, and one monotone resize at the first connect, are natural for a Mac moving to
   a Retina screen. Desktops now match classic's pointer and key behaviour. No touch, no emulation.
8. **Two viewers on one device** (a stream tab and a classic tab) replace each other, as two classic tabs do today.
9. **Rollout ordering** (§8.6) and the kill switch, now enforced at the pod.
10. **We maintain the viewer.** It is small, CSP-locked, split into DOM-free tested modules, and checked against
    the same conformance pack as the agent.

## 13. Deferred, and found in passing

**Deferred** (not in 0.7.0):
- double-tap to zoom;
- text selection, copy out of the pod, drags (sliders), long-press menus;
- multi-field autofill;
- 2x for desktop viewers (the pixel budget);
- cropping the classic scrollbar gutter;
- adaptive JPEG quality and a WebCodecs/H.264 stream;
- "try the new viewer again" from the classic viewer;
- telemetry of which viewer each checkout used (§16 decision 3);
- removing KasmVNC (only after the beta has proved itself on devices).

**Found in passing.**
- **Classic dialogs (0.6.4; fixed in 0.7.0, §16 decision 2).** The pod's Chromium had no dialog listener, so
  Playwright auto-dismissed every JavaScript dialog for classic viewers: a `confirm()` returned false unseen, and
  an `alert()` vanished (round 1 `out/s2f-pickers.json`). From the first viewer of a pod's life the dialog keeper
  listens, so Chromium draws each dialog on the pod's screen and the classic viewer answers it with its mouse or
  keyboard (`tests/browser/test_checkout_dialogs.py`: a confirm clicked OK over VNC returns true, a prompt typed
  into returns the text, a dialog survives a reconnect and is dismissed at hold end). Two things seen on the real
  screen: Chromium ignores a click that comes within about half a second of a dialog appearing (its guard against
  stray clicks), and on a phone-sized screen (390 CSS px) the dialog, 450 px wide and centred in Chromium's
  500-DIP minimum window, runs off the right edge (the classic viewer's existing 500-DIP overhang, §4.2): Cancel
  is whole, OK shows only its left part, an alert's only button a 20 px sliver, and a prompt's input is cut off.
  Found in review to be more than a phone user can be expected to answer, so it is fixed: while a classic viewer
  is bridged and a dialog is open, a screen narrower than 500 px is widened to 500 x its height with one RandR mode
  (`Screen.widen()`, run by `stream.ClassicScreen.fit_dialogs()`, which the gate runs while it bridges a classic
  viewer). KasmVNC scales the frame into the phone (letterboxed) and the whole dialog is on it; the viewer's own
  mode comes back once no dialog is open (`Screen.narrow()`), unless the viewer has resized the screen since (a
  phone turned; the screen is looked at twice a second while a dialog is open). The page can't run while its
  dialog is open; once it's answered it sees one resize to 500 and back, about 40 ms apart (measured), so a page
  that swaps its layout between 390 and 500 px would lay out twice. Desktops (500 px or wider) see nothing. The
  gate waits for the viewer's own screen to be back before the next viewer. Tested on the real screen in
  `test_checkout_dialogs.py` (confirm, alert and prompt: the whole dialog inside the canvas, OK answered by a tap,
  and 390x664 at DPR 1 again).
- **Unbounded `prompt()` defaults (0.6.4 and later).** The default text of a page's `prompt()` reaches Playwright's
  Node driver unbounded (20 M characters measured, `round2/s2_bounds.py`). With a stream connected it also reaches
  the Python agent. Neither can be bounded below Playwright. A fix needs Playwright upstream (a size cap on dialog
  events) or handling dialogs below Playwright. The pod's memory limit and the C22 exit bound the damage to the one
  hold.
- **The screencast's first frame** (found while verifying the lazy restore). `Page.startScreencast` sends nothing
  on a static page, so a seeded screenshot is required (§4.5).
- **A page with an open JavaScript dialog answers almost no CDP** (found while building the dialog keeper, on the
  pod screen, a dialog kept open by a listener). On that page's session `Runtime.evaluate` (even in an isolated
  world, `timeout: 100`), `Page.getLayoutMetrics`, `Page.captureScreenshot`, `Page.getFrameTree`,
  `Page.startScreencast`, `DOM.enable` and a new session's `Page.enable` all got no answer in 2 s; the screencast
  sent no frame; `Target.getTargetInfo` (3 ms) and `Input.dispatchMouseEvent` (acknowledged, 3 ms) answered. So a
  stream session that starts, or switches pages, while a dialog is open there must send `ready`, `pages`, `clock`
  and the `dialog`s without waiting on the page, bound every page call (2 s), and seed the image, start the
  screencast and map fields for that page only once the dialog is answered (the keeper's `answer()` returning, or
  its `dialog_closed`). Input for it meanwhile is answered `error stale`, or ignored for `move` and `wheel`
  (PROTOCOL §5.5): queueing it would replay taps aimed at an image from before the dialog. The dialog keeper's own `Page.enable` therefore runs in the
  background, and the stream's per-page sessions don't call it (§6.3).
- **The click that opens a dialog isn't acknowledged until the dialog is answered** (found in integration,
  `tests/browser/test_checkout_stream_e2e.py`, with the card frame's "Card declined" `alert()`, then measured for
  the main frame's too: `Input.dispatchMouseEvent` `mouseReleased` had no answer in 3 s while its dialog was open).
  The acknowledged `Input.dispatchMouseEvent` above was a later event, sent to a page whose dialog was already open.
  The session carried messages out strictly one at a time, so the dialog's answer, the viewer's next message,
  waited 2 s behind the call's bound, the tap was answered `error failed`, and a viewer that reconnected meanwhile
  lost the answer. Now a message whose input opens a dialog counts as carried out once the dialog opens; the rest
  of it finishes after the answer, before any later input (PROTOCOL §3.4). The answer reaches the page in about
  65 ms. Dialogs can therefore come faster than the 4 answers a second that §7.1 allows; a person reads each first.
- **`everyNthFrame` 2 loses the last frame of a change** (found building the browser tier,
  `tests/browser/test_checkout_stream.py`). With 2, the screencast can skip the final frame of a short change (one
  wheel step) and then, the page being still, sends nothing more: the viewer kept showing the page one step behind
  (the image's `sy` and pixels were from before the step) until something else changed. The live mode uses 1. The
  extra frames are mostly repeats while nothing moves and are capped at 15 a second while it does; the cost is
  Chromium CPU while scrolling continuously (125-128% of a core instead of 100-106%, round 1's measurement).

## 14. Evidence index

Everything is under `/tmp/claude-0/-home-user/4741bd6b-b935-53bf-a028-f8a3ad730592/scratchpad/stream-design/`.

**Round 1:**

| What | Script | Result |
|---|---|---|
| Pod screen harness | `scripts/pod.py` | — |
| Two-origin site | `site/shop/index.html`, `site/pay/*.html` | — |
| RandR on Xvnc; Xft/DPI live; emulation comparison; revert pixel identity | `scripts/probe_xrandr*.py`, `s1_dpi.py`, `probe_xsd_lazy.py` | `out/s1-dpi.json`; `shots/s1-*` |
| captureScreenshot vs screencast | `scripts/s1b_capture.py` | `out/s1b-capture.json` |
| Taps, wheel, momentum, pinch | `scripts/s2a…s2d, s2j` | `out/s2a…s2d-*.json`, `out/s2j-cancelpinch.json` |
| Text, IME, keys; select, date, dialogs, pop-ups | `scripts/s2e_text.py`, `s2f_pickers.py`, `s2g_select.py`, `s2i_time.py` | `out/s2e…s2i-*.json` |
| Field map cost, invisibility | `scripts/s2h_fieldmap.py` | `out/s2h-fieldmap.json` |
| Frame pipeline; everyNthFrame; memory, agent CPU | `scripts/s3_pipeline.py`, `s3b_everynth.py`, `s4_cost.py` | `out/s3*.json`, `out/s4-cost.json` |
| Protocol fuzz; gate routing; end to end | `proto/*` | `out/fuzz-protocol.txt`, `out/gate-routing.txt`, `out/e2e-*.json` |

**The reviews:** `review-security/*.py` and `*.out`/`.json`; `review-feasibility/r*.py`, `out/r*.json`.

**Round 2** (`round2/`; reference code `cast_scripts.py`, `worlds.py`, `screen2.py`):

| Finding | Script | Result |
|---|---|---|
| `choose`/`date` occlusion safety, masks, focus check | `s1_choose.py` (+ `site/shop2/occl.html`, `site/pay2/card3.html`) | `out/s1-choose.json` |
| Field map cost with masks and the owner chain | `s1b_mapcost.py` | `out/s1b-mapcost.json` |
| Bounds: bombs, timeouts, titles, prompt defaults, Page.enable | `s2_bounds.py`, `s2b_cost_dialog.py`, `s2c_timebudget.py` | `out/s2*.json` |
| Screen: no-op apply, lazy restore, orderings, monotone widths | `s4_screen.py`, `s4b_monotone.py` | `out/s4-screen.json`, `out/s4b-monotone.json` |
| Wheel coalescing at 60/120 Hz | `s5_wheel.py` | `out/s5-wheel.json` |
| Still mode CPU and bandwidth; screenshot first frame; select open | `s6_idle.py`, `s6b_firstframe.py` | `out/s6-idle.json`, `out/s6b-firstframe.json` |
| Zoomed rectangles (main and iframe); pinch with pan | `s7_zoom.py`, `s7b_zoom_main.py` | `out/s7-zoom.json`, `out/s7b-zoom-main.json` |
| Desktop pointer and keys | `s8_desktop.py` | `out/s8-desktop.json` |
| Native paste rules; LF doesn't submit | `s9_paste.py`, `s9b_lf_submit.py` | `out/s9-paste.json`, `out/s9b-lf-submit.json` |

Run any script with `/home/user/ferry-watch/.venv/bin/python -I <script>` from the `stream-design` directory. They
need Xvnc (KasmVNC 1.5.0), matchbox-window-manager, xsettingsd and xrandr on `PATH`, as installed here.

## 15. Review round 2: findings and resolutions

Verdicts:
- **Holds**: reproduced or accepted as described; fixed.
- **Holds, partly**: the problem is real, but the proposal was adjusted.
- **Does not hold**: rebutted, with the reason.

### Security review

| # | Finding (severity) | Verdict | Evidence | Resolution |
|---|---|---|---|---|
| S1 | `choose`/`date` click a covered point, then Enter: "Pay" clicked twice (blocker) | **Holds** | Reproduced exactly (`round2/out/s1-choose.json`, `prototype_on_s1`) | Hit-tested click points through every ancestor frame; focus check before any key; covered fields dropped from the map via 3×3 masks; overlays only for fully hit-clear fields. Verified on 9 cases with the footer never clicked. PROTOCOL §5.3.1-5.3.2, §6.11-6.12, §8.3; DESIGN §4.3, C40 |
| S2 | Field map and titles unbounded until they reach the agent (major) | **Holds** | Reproduced and fixed: 20 MB → 15.8 kB, 100 MB → 4.9 kB; 200k inputs 28.7 s → ≤ 101 ms; titles 20 M → 80 (`round2/out/s2*.json`) | All bounds inside the scripts, a 30 ms in-script budget, `Runtime.evaluate` `timeout: 100` (`callFunctionOn` ignores it: measured), ≤ 8 frames, worlds dropped on detach, titles from `Target.getTargetInfo`, a 64 KiB assembly rule. PROTOCOL §5.2, §5.3.2; C39. The one unbounded input left (`prompt()` defaults) predates 0.7.0 and is recorded in §13 |
| S3 | Proxy mirrors password fields and credential tokens; prompt sheet looks like Ferry Watch (major) | **Holds** (not device-verified; the browser behaviour cited is documented) | KasmVNC's own hidden input is a plain `textarea`, so `type=text` is parity; iframe dialog origin available only with `Page.enable` (`round2/out/s2b-cost-dialog.json`) | Proxy never `type=password`; credential tokens stripped by agent and viewer; password fields get KasmVNC's attributes; `dialog.origin` from the opening frame, the Chrome-style heading, a sheet styled as page content; a device check in §11 (final integration item 4). PROTOCOL §5.3.1, §5.5, §8.5, §8.8; C41 |
| S4 | Every reconnect: 5 resizes, 4 layouts, restore on the hold-end path (major) | **Holds** | Reproduced; fixed: same-device reconnect 0 resizes; 96/96 transitions monotone; restore 44 ms, only before binary (`round2/out/s4*.json`) | No-op apply, DPI then mode, lazy restore only before a `binary` bridge (bounded 3 s, killing hung `xrandr`), never at hold end or drain, cast sessions paced at 1/s, dialogs kept across reconnects. Fingerprint table corrected. PROTOCOL §2.2 step 7, §2.3, §3.2, §3.5; DESIGN §4.1, §4.2 |
| S5 | Desktop: no pointer movement or hover, 4 ms dwell (minor) | **Holds** | Fixed and measured: 15 moves + mouseenter, 99 ms press, real key dwells with Shift and rollover (`round2/out/s8-desktop.json`) | `move`, `tap.ms`, `key.ph` with `Shift`; phones unchanged. PROTOCOL §6.4, §6.5, §6.9, §8.4; DESIGN §4.2 desktop column |
| S6 | Kill switch and rollback leave `fw.cast1` open on running pods for 26 h (minor) | **Holds, partly** | `Configure` is `forbid_unknown_fields` on 0.6.x agents, so the field must be sent only to ≥ 0.7.0 peers | `cast_allowed` in `configure` (default false, so a rollback also closes streams), gate refusal and 4005. The proposed `cast` ticket claim is not adopted: the opt-in is a preference, not an authorization boundary (DESIGN §8.3). PROTOCOL §2.2 step 4, §3.6; C42 |
| S7 | Only input is re-checked; images keep flowing to an ack-only viewer (minor) | **Holds** | Design inspection (`checkout_cart()` turns None on the clock independently of the loop) | `verdict()` before every input and output and every second, reading `release_due` on the clock; test with a frozen watch loop. PROTOCOL §7.2; C35; DESIGN §6.2 |

### Feasibility review

| # | Finding (severity) | Verdict | Evidence | Resolution |
|---|---|---|---|---|
| F1 | Map goes stale without a scroll change; an animated page never gets a map (major) | **Holds** | Reviewer's `r3b`, `r5` | One normative schedule (≤ 4/s, no debounce, `fields_ok`); validity by `seq` (±4) **and** by message count `n`, which invalidates the map as soon as the user scrolls, taps or types. PROTOCOL §5.3.4, §8.3 |
| F2 | Scroll prediction with an inner scroller slides the image away; taps land on unseen things (major) | **Holds** | Reviewer's `r3_inner` | Prediction only after an image confirms the document moves, capped, dropped on a 250 ms timer; no taps while shifted; taps carry `seq` and are refused when the page moved. DESIGN §4.5, §7.4; PROTOCOL §6.4, §8.2 |
| F3 | Field rectangles while zoomed: spec and prototype disagree, 300 px off (major) | **Holds** | Fixed: exact at scale 2 for main and iframe fields, clicks focus each (`round2/out/s7*.json`) | Normative formulas using `getBoundingClientRect` and owner content boxes in each parent's world; `DOM.getBoxModel` banned for offsets; measured test vectors; zoomed-map tests in A6 and in the midpoint checkpoint. PROTOCOL §1, §5.3.3, §11.3 |
| F4 | One wheel per animation frame outruns one-at-a-time processing (major) | **Holds** | Reproduced (strict: peaks 43/136) and fixed (peak 1, done within 47 ms) (`round2/out/s5-wheel.json`); latency figure corrected | Agent coalescing of queued wheels (and moves); viewer ≤ 1 per 33 ms; bucket 40/s. PROTOCOL §3.4, §6.6, §7.1; DESIGN §4.5 |
| F5 | An animated page costs about 1.3 cores with no input (major) | **Holds** | Reproduced (143%) and fixed (19%) (`round2/out/s6-idle.json`) | Live/still/paused modes with a measured trigger (still only when frames keep coming: polling costs 9% on a static page); `visible`. PROTOCOL §3.8, §4.3; DESIGN §4.5 |
| F6 | Restore at every teardown flips the page through four layouts (major) | **Holds** | Same as S4 | Same as S4 |
| F7 | The opacity-0 proxy swallows taps, drags and wheel over the focused field (major) | **Holds** | Reviewer's `r4` | `pointer-events: none` and a gesture layer above; "Paste" in the menu; viewer tests for a second tap and a drag on the field. PROTOCOL §8.5; DESIGN §4.4, §7.4-7.5 |
| F8 | Pasting text with a line break kills the session with 4400 (major) | **Holds, partly** | Chromium's `insertText` applies native paste rules itself and never submits on a LF (`round2/out/s9*.json`) | `text` may carry LF and TAB (the page gets exactly a native paste); the viewer only normalises CR, strips other controls and splits by code points; value errors are `error invalid`, never 4400. Viewer-side LF → space replacement is not adopted, because Chromium already does it and keeps LFs in textareas. PROTOCOL §1, §6, §6.10, §8.7, §11.4 |
| F9 | Workstreams test against their own readings of the protocol (major) | **Holds** | Five prototype/spec disagreements listed by the reviewer | Step 0 shared conformance pack loaded by both suites; a midpoint integration checkpoint after A4/B4 with seven named checks. PROTOCOL §10.1; DESIGN §11 |
| F10 | Desktops wider than 1920 or taller than 1600 get 4400 forever (major) | **Holds** | Reviewer's `r1` | The viewer clamps; images are letterboxed and taps mapped through the box; `ready.w/h` and header `w/h` authoritative; a 2560x1440 test context. PROTOCOL §4.1, §8.1 |
| F11 | Desktop: no pointer movement (minor) | **Holds** | Same as S5 | Same as S5 |
| F12 | Pinch pan, a tap's reference image, long selects and oversize maps unspecified (minor) | **Holds** | Pinch with pan measured exact (`round2/out/s7-zoom.json`) | `pinch {seq, x0, y0, x1, y1, scale}` = pinch about the start then a pan; `seq` on `tap` and `pinch` with the staleness check; options 400 with `nopt`/`trunc`; the 64 KiB assembly rule. PROTOCOL §5.3.1-5.3.2, §6.4, §6.8. `seq` on `wheel` is not adopted: wheel deltas are relative and the gesture's start point only picks the scroll target |
| F13 | The escape hatch exists only once the viewer's modules have run (minor) | **Holds** | Design inspection | Static markup + the `session.js` watchdog (3 s, and on any uncaught error); modules already `no-store` through the server-level header; a 404 test. PROTOCOL §8.10; DESIGN §7.7, §9 |

## 16. Decisions for the owner

These are the only choices this design can't make for you. Everything else in it follows from your decisions in §2.

1. **Who sees the Settings switch at launch.**
   - **(a)** Everyone, as you specified.
   - **(b)** Admins only (that's you), until the device checks (§11, final integration item 4) pass, then everyone by changing
     `checkout.streamViewer` from `admins` to `everyone`.

   The mechanism supports both, and switching between them is one values change. The keyboard, AutoFill and
   password-manager behaviour on real iPhones and Android phones can't be tested here, so **(b) is recommended**.
2. **Whether to fix the classic viewer's auto-dismissed dialogs** (§13), now or later. **Decided: in 0.7.0**, as
   the one deliberate change to the classic path.
   - The fix: a dialog listener that leaves dialogs to the viewer while a classic viewer is connected.
   - Today a `confirm()` in BC Ferries' checkout silently returns false for every classic user.
   - Built as: dialogs are kept from the gate's first admission of a viewer (either kind) until the checkout is
     over, access is revoked or the pod drains, so a dialog also survives a reconnect and a switch between kinds.
3. **Whether you want per-checkout telemetry of which viewer was used** (and whether the user switched to classic)
   to judge the beta. It needs one new event parameter between agent and operator. That protocol is
   `forbid_unknown_fields`, so it is sent only to a ferry-control that understands it, about 1 day in A and C.
   Without it, the beta is judged by your own use and users' feedback.
