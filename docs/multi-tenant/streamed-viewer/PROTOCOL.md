# fw.cast1: the streamed checkout viewer's wire protocol

Status: normative for Ferry Watch 0.7.0 (round 2, after the security and feasibility reviews). Companion to
`DESIGN.md` (same directory), which gives the reasons and the evidence; DESIGN §15 maps every review finding to
the section of this document that resolves it. The key words MUST, MUST NOT, SHOULD and MAY are used as in
RFC 2119.

Two parties:
- **the viewer**: `vnc-edge/site/fw/viewer.html` and its scripts, running in the user's browser on
  `https://ferry-checkout.chestr.dev`;
- **the agent**: the browser pod's agent, specifically its gate (`src/ferry_watch/agent/gate.py`) and the stream
  session (`src/ferry_watch/agent/stream.py`) that the gate runs for an admitted `fw.cast1` connection.

vnc-edge, Envoy, cloudflared and Cloudflare carry the WebSocket unchanged; nothing in them parses it.

Section 8 states what the viewer MUST do with what it receives (drawing, coordinates, the field map, the keyboard
proxy, page-derived text, the escape hatch), because the agent's safety rules depend on it. Section 10 names the
shared conformance pack that both implementations MUST pass.

---

## 1. Conventions

- **The image.** Every image depicts the front page's **visual viewport** (what the pod's window shows, after any
  pinch zoom).
- **Frame px.** Coordinates in the image: `(0, 0)` is the image's top-left corner and the image is `w`×`h` frame px
  (the header's `w`, `h`, §4). A frame px is a CSS px of the pod page's layout viewport at page scale 1; at page
  scale `s` the image shows `w/s`×`h/s` CSS px of the page, magnified. All coordinates the viewer sends, and all
  rectangles the agent sends, are in frame px.
- **Mapping.** With the main frame's `visualViewport.offsetLeft/offsetTop` = `(ox, oy)` and scale `s`:
  - a main-frame client point `(X, Y)` (CSS px, as `getBoundingClientRect` reports) is at frame px
    `((X − ox)·s, (Y − oy)·s)`; a length `L` is `L·s` frame px;
  - a frame px point `(x, y)` is dispatched to CDP `Input` at `(x/s, y/s)` (CDP Input coordinates are CSS px
    relative to the visual viewport).
  Section 5.3.3 extends the first rule to frames inside iframes. Test vectors are in §11.3.
- **Strings** are JSON strings. Lengths are counted in Unicode scalar values ("code points") unless stated as bytes.
  **Clean text** means: no C0 controls (U+0000-U+001F), no DEL (U+007F), no C1 controls (U+0080-U+009F), no
  surrogate code points (U+D800-U+DFFF; inbound, one can only arrive as a lone `\ud800`-style escape, which the
  agent's decoder refuses as malformed JSON, §6). **Typed text** (§6.10) is clean text except that it MAY contain
  U+0009 (TAB) and U+000A (LF).
- **Integers** are JSON numbers written without a fraction or an exponent (`7`, never `7.0` or `7e0`). **Numbers**
  are any finite JSON number (one too large for a double, such as `1e400`, is not).
- **Binary** values are little-endian.
- **Epoch** values are seconds since 1970-01-01T00:00:00Z, as JSON numbers.
- Every JSON message is an object with a string field `t` (its type). Field order is irrelevant.
- **Message numbers.** Every viewer→agent message except `ack` has a number: `hello` is 1 and each later
  message (other than `ack`) is the next integer. Both sides count; the agent reports, in `fields`, `fields_ok`
  and `focus`, how many of them it had carried out (`n`, §5.3). A message counts as carried out once the agent has
  dispatched it, merged it into one it dispatched (§3.4), dropped it (§7.1) or answered it with `error`.

## 2. Transport and admission

### 2.1 The upgrade

The viewer opens

```
new WebSocket("wss://ferry-checkout.chestr.dev/t/<trip>/g/<gen>/ws", protocols)
```

with `binaryType = "arraybuffer"`, from a page on `https://ferry-checkout.chestr.dev` (so the browser sends that
`Origin`). nginx proxies the path to the pod `fw-<trip>-g<gen>`'s gate at `:6080/ws`, unchanged from 0.6.4.

`protocols` MUST be, in this order:
1. `fw.cast1`
2. `fw.n1.<N>`: the device's nonce for `(trip, gen)` from localStorage (§2.4); `N` matches `[A-Za-z0-9_-]{22,64}`.
3. `fw.t1.<ticket>`: only while a checkout ticket is pending for this tab (before the first successful open).

The viewer MUST NOT offer `binary`. KasmVNC's client (the classic viewer) keeps offering `binary` and never
`fw.cast1`.

### 2.2 What the gate does

The gate runs its 0.6.4 admission unchanged, with the rules marked **new**:

1. `Origin` MUST equal `FW_VNC_ORIGIN`, else **403**.
2. A READY cart with every hold clock running, lockdown passed, not draining, `vnc_allowed`; else **409**.
3. **new:** the offered list MUST contain exactly one of `binary` and `fw.cast1`; else **400**. (Offering both, or
   neither, or an unknown `fw.cast2`, is refused.) It MUST be a WebSocket upgrade; else **400**.
4. **new:** for `fw.cast1`, the operator's latest `configure` MUST have `cast_allowed: true` (the streamed viewer's
   kill switch, DESIGN §8.3); else **400**. This refusal is not counted as a failed attempt.
5. Nonce, then first connect (ticket, `jti`, lock = `sha256(N)`) or reconnect (`sha256(N)` equals the lock):
   unchanged, including the 5-failures-a-minute limit (**403**/**429**). Tickets are never accepted once the lock is
   set, whichever subprotocol set it: a `fw.cast1` first connect and a `binary` reconnect (or the reverse) share
   one lock.
6. The response selects the offered one of `binary`/`fw.cast1` as `Sec-WebSocket-Protocol`.
7. **new:** a `fw.cast1` connection whose admission completes less than **1 s** after the previous `fw.cast1`
   session started waits, inside the gate, until 1 s has passed, then proceeds. Nothing is refused or counted.

Refusals are a bare HTTP status; the browser sees only a failed connection (close code 1006).

For `fw.cast1` the gate MUST set the WebSocket's maximum inbound message size to **4096 bytes** (the RFB path keeps
256 KiB). A bigger inbound message closes the connection with **1009** (aiohttp does this).

### 2.3 One viewer, either kind

There is one viewer slot per pod, shared by both kinds. An admitted connection replaces the current viewer (of
either kind): the gate ends the old one (close code **4001** for a `fw.cast1` viewer) and, for an old `fw.cast1`
viewer, waits until its session has finished its teardown (§3.5) before the new connection proceeds. Both kinds
count as "a viewer" for viewer seconds and the hold clocks exactly as in 0.6.4.

**Before it bridges a `binary` viewer, the gate MUST**, after the previous viewer's teardown, restore the pod's
own screen (the X screen back to its launch size, Xft/DPI back to the default by stopping xsettingsd, the added
RandR modes removed; DESIGN §4.1), bounded to 3 s in total. A `binary` viewer therefore always finds the pod's
screen as a 0.6.4 classic viewer would. The screen is restored at no other time (§3.5). A JavaScript dialog a
stream session left open stays open: Chromium draws it on the pod's screen, where the classic viewer answers it
(§5.5).

### 2.4 The device's nonce and ticket

The viewer MUST use the same storage the classic viewer's shim uses (`fw/session.js`, DESIGN §8.4): localStorage key
`fw:<trip>:<gen>` holding `{n, opened, disp, hard_deadline, session_deadline, viewer, viewer_fallback}` and
sessionStorage key `fw:<trip>:<gen>:ticket` holding `{ticket, exp}`. On the first `open` event it MUST drop the
pending ticket and set `opened: true`, exactly as the shim does. Neither value ever appears in a URL path or query.

### 2.5 Liveness

The agent sends a WebSocket ping every 20 s (aiohttp `heartbeat`); a missing pong closes the connection. The
viewer needs no application-level keepalive.

## 3. Session lifecycle

### 3.1 States

```
open --hello--> ready --(images, input)--> ... --close--> closed
  \--5 s without hello--> closed 4408
```

Every connection is a fresh session: image sequence numbers, the repeat cache, message numbers, page ids,
field-map revisions and dialog ids all start again. The pod's page itself (its content, scroll, focus, typed
values, an open JavaScript dialog) and the pod's screen size persist across connections.

### 3.2 Opening

1. After `open`, the viewer MUST send `hello` (§6.1) as its first message.
2. The agent MUST receive `hello` within **5 s** of the upgrade, else close **4408**. Any other first message, a
   `hello` that fails any rule of §6.1, or a second `hello`: close **4400**.
3. The agent applies the size (DESIGN §4.1). If the X screen already has exactly this size and scale factor (a
   reconnect from the same device), nothing changes and the page sees nothing. Otherwise the agent changes
   Xft/DPI first and then the RandR mode, without waiting in between, and waits until the page reports the new
   size (at most 1 s).
4. It sends, in this order: `ready` (§5.1), `pages` (§5.2), `clock` (§5.8), a `dialog` (§5.5) for each JavaScript
   dialog already open on a page of the checkout, then a first image (§4) taken with `Page.captureScreenshot`,
   with header flag bit 0 set. (Chromium's screencast sends nothing until the page changes, so without this a
   static page would never be shown.) While a dialog is open on the front page, that page can't be captured at
   all (DESIGN §13): the first image then follows the dialog's answer, and a viewer MUST be able to show a
   `dialog` before it has any image.
5. Images follow (§4); `fields`, `fields_ok` and `focus` follow whenever they apply.

The viewer MUST NOT send any message other than `hello` before it receives `ready`; the agent closes **4400** if
it does.

### 3.3 Ordering guarantees (agent → viewer)

- `ready` is the first message of a session; `pages` precedes the first image.
- Every image's `page` appears in the most recent `pages` message.
- Binary messages are numbered `seq` = 1, 2, 3, ... with no gaps.
- `fields`, `fields_ok` and `focus` name a `page`; a viewer MUST ignore one whose page isn't the page of the image
  it is drawing.

### 3.4 Order of processing (viewer → agent)

The agent MUST carry out the viewer's messages one at a time, in arrival order, finishing the browser commands for
one message before starting the next (so a `text` sent after a `tap` lands in the field the tap focused), with
these exceptions:
- `ack` and `visible` are applied on arrival.
- **Coalescing.** When a `wheel` arrives while the last message waiting in the queue is a `wheel` for the same page,
  the waiting one absorbs it: its `dx`, `dy` become the sums (as long as each stays within ±8000; otherwise the new
  one is queued separately) and its `x`, `y` become the new one's. A `move` arriving while the last waiting
  message is a `move` replaces it. Nothing else is merged, so order is kept.
- At most **64** messages may wait (a merged message counts once); one more closes **4429**.

### 3.5 Closing and teardown

Either side may close. Whatever the cause, the agent's teardown MUST, before the gate lets another viewer in:
stop the screencast (or the still-image polling, §4.3); release any key it holds down and the mouse button if a
`tap` is mid-press (§6.4, §6.9); and detach the CDP sessions it opened. Teardown MUST complete even if the gate's
task is cancelled meanwhile, and MUST be bounded (2 s per CDP call).

Teardown MUST NOT restore the pod's screen and MUST NOT dismiss an open JavaScript dialog: a viewer that reconnects
(the bank app, a screen lock, a network change) finds the page exactly as it left it. The screen is restored only
before a `binary` viewer is bridged (§2.3). Open dialogs are dismissed only when the gate closes the viewer
because the checkout is over, access was revoked or the pod is draining (codes 4000, 4002, 4003; §5.5).

### 3.6 Close codes

The agent closes with these codes. The viewer's required behaviour is in the last column.

| Code | Meaning | Viewer MUST |
|---|---|---|
| 1000 | Normal (the agent never uses it except at shutdown races) | reconnect with backoff (§3.7) |
| 1006 | No close frame: network loss, or the upgrade was refused (any HTTP status) | reconnect with backoff |
| 1009 | An inbound message exceeded 4096 bytes | treat as 4400 |
| 4000 | The checkout is over: released, expired, booked, browser lost, or the cart changed (§7.2) | stop; show "This checkout has ended" and the way back to Ferry Watch; no reconnect |
| 4001 | Replaced: another connection with this device's nonce took the slot (another tab, a reload, the classic viewer) | stop; show "Opened elsewhere on this device" with a "Show it here" button that reconnects |
| 4002 | Revoked: checkout access withdrawn (`vnc_revoke`, suspension, `vnc_allowed` false, lockdown failed) | stop; show that checkout access was withdrawn; no reconnect |
| 4003 | Draining: the pod is shutting down | stop; show "This checkout has ended"; no reconnect |
| 4004 | Connection time cap (6000 s) reached | stop; no reconnect |
| 4005 | The streamed viewer was withdrawn (the kill switch, §2.2 step 4) | switch to the classic viewer at once, as "Use the classic viewer" does (§8.10) |
| 4400 | Protocol violation: a structurally malformed or unexpected message (§6) | reconnect once after 1 s; on a second 4400 in this page's life, stop and offer the classic viewer |
| 4408 | No `hello` within 5 s | reconnect with backoff |
| 4429 | The queue limit (§3.4), the drop limit or the overall rate (§7.1) was exceeded | reconnect after 2 s |
| 4500 | The agent couldn't run the session (CDP or screen failure) | reconnect with backoff; after 2 consecutive 4500s, offer the classic viewer prominently |
| any other | Not sent by the agent; a proxy on the way may close with, for example, 1001 or 1011 | reconnect with backoff |

The viewer closes with **1000** when it leaves (for example to the classic viewer); the agent treats any
viewer-initiated close the same way.

### 3.7 Reconnecting

The viewer reconnects after a close that the table says to, offering `fw.cast1` and the nonce (and the pending
ticket only if it never opened). Delays between attempts: **0.5, 1, 2, 5, 10, 10, ...** seconds, reset after a
connection that reached `ready`: the n-th reconnect attempt since the page loaded or since the last connection that
reached `ready` waits the n-th delay, except that after a 4400 or a 4429 the table's fixed delay replaces it (that
attempt still counts). The viewer MUST stop and go to `/gone.html` instead of reconnecting once the device record's
`hard_deadline` has passed. After **3** consecutive connections that never reached `open` (the page's first
included; one that opens ends the run), it MUST show the classic viewer option prominently (the gate may be
refusing it for a reason a browser can't see, such as the kill switch). "2 consecutive 4500s" in §3.6 means two
closes in a row with 4500.

### 3.8 Visibility

The viewer MUST send `visible` (§6.7) with `v: false` when its document becomes hidden
(`document.visibilityState`), and with `v: true` when it becomes visible again. While the latest `visible` says
false, the agent sends no images and stops the screencast and the still-image polling. On `v: true` it sends a
fresh image taken with `Page.captureScreenshot` within 500 ms, then continues as in §4.3.

## 4. Images (agent → viewer, binary messages)

### 4.1 The header

Every binary message from the agent is one image or one repeat, with a 32-byte header:

| Offset | Type | Field | Meaning |
|---|---|---|---|
| 0 | u8 | `kind` | 1 = image (JPEG follows the header), 2 = repeat (no payload) |
| 1 | u8 | `slot` | 0 or 1: the cache slot the image is stored in (kind 1) or drawn from (kind 2) |
| 2 | u16 | `page` | page id (§5.2) |
| 4 | u32 | `seq` | 1 for the session's first binary message, then +1 each |
| 8 | u16 | `w` | the page's layout viewport width in CSS px, scrollbar included (the screencast's `deviceWidth`) |
| 10 | u16 | `h` | its height (`deviceHeight`) |
| 12 | f32 | `scale` | page (pinch) scale factor, 1-4 (`pageScaleFactor`) |
| 16 | f32 | `sx` | visual viewport's left edge in document CSS px (`scrollOffsetX` = `visualViewport.pageLeft`) |
| 20 | f32 | `sy` | visual viewport's top edge in document CSS px (`scrollOffsetY` = `visualViewport.pageTop`) |
| 24 | f32 | `top` | `offsetTop` (CSS px), 0 on this desktop browser |
| 28 | u8 | `dsf` | the screen's scale factor for this image, 1 or 2 |
| 29 | u8 | `flags` | bit 0: first image since the session started, the front page changed, or the viewport size changed. Other bits: 0; viewers MUST ignore unknown bits |
| 30 | u16 | reserved | 0 |
| 32 | bytes | JPEG | kind 1 only: a baseline JPEG, 1 to 2,097,152 bytes; nothing follows it |

`w` and `h` are authoritative: they are what the image shows, whatever the viewer's own size (§8.1). They normally
equal `ready.w`/`ready.h`; a pop-up window with a toolbar has a slightly shorter page, and the header says so.

### 4.2 Rules

- A kind 2 message has exactly 32 bytes. A kind 1 message's JPEG is everything after byte 32.
- The image's pixel size is nominally `w·dsf`×`h·dsf`; a viewer MUST draw the decoded image into the box §8.1
  gives for `w`×`h` frame px, whatever its pixel size.
- **Slots.** The viewer keeps two decoded images. On kind 1 it decodes the JPEG and stores it in `slot`
  (replacing what was there); on kind 2 it draws what `slot` holds, with this header's metadata. The agent sends
  kind 2 only when the image is byte-identical (SHA-256) to the one in that slot, which it tracks as the last two
  distinct images sent, evicting the least recently used. Because the viewer decodes asynchronously, it MUST handle
  binary messages strictly in order (a repeat must see the slot's preceding image).
- **Acks.** The viewer MUST send `ack` (§6.3) with the header's `seq` after it has drawn (or deliberately skipped)
  each binary message, in order. The agent MUST NOT have more than **2** binary messages un-acked; with 2
  outstanding it holds back, keeping only the newest pending image (newer replaces older). This is the only
  backpressure: a slow link gets fewer, fresher images.
- **Metadata kept.** The agent keeps the header fields of the last **16** binary messages it sent, so that it can
  check what a `tap` or `pinch` was aimed at (§6.4, §6.8).
- **Malformed.** A binary message the viewer can't take as one of the above (fewer than 32 bytes, `kind` not 1 or
  2, `slot` not 0 or 1, a kind 2 longer than 32 bytes, a kind 1 with no JPEG) is an agent bug: the viewer neither
  draws nor acks it, closes the socket with 1000 and goes on as after a 4500 (§3.6).

### 4.3 Image sources and rates (agent)

The agent produces images in one of three modes. The viewer can't tell them apart except by rate; they are
normative for the agent because the CPU budget and the first-image rule depend on them.

| Mode | Source | Rate | Enters when |
|---|---|---|---|
| **live** | `Page.startScreencast` (JPEG q50, `everyNthFrame` 2), every Chromium frame acked at once | at most **15** binary messages a second | the session starts; any viewer message other than `ack` while **still**; `visible: true`; **still** saw 4 unchanged captures in a row |
| **still** | every 500 ms: `Page.getLayoutMetrics`, then `Page.captureScreenshot` (JPEG q50, at most 1 s, else skipped); sent only when its bytes or its metadata differ from the last image sent | at most **2** a second | **live**, no viewer message other than `ack` for 1 s, and Chromium delivered at least 8 frames in that second (an animated page) |
| **paused** | none | 0 | `visible: false` |

- On entering **live** from **still** because of a viewer message, the agent restarts the screencast before it
  carries out that message.
- A screenshot-based image's metadata: `w`, `h` as the page's latest screencast frame (or `innerWidth`/
  `innerHeight` if it has none), `scale`, `sx`, `sy` from `Page.getLayoutMetrics` `cssVisualViewport` (`scale`,
  `pageX`, `pageY`), `top` 0.
- The session's first image, the first image after `page show` or a front-page change, and the first image after
  `visible: true` are screenshot-based (§3.2), with flag bit 0 set (except after `visible`).

### 4.4 Test vectors (hex)

- kind 1, slot 0, page 1, seq 7, w 390, h 664, scale 1, sx 0, sy 530.5, top 0, dsf 2, flags 1:
  `01 00 01 00 07 00 00 00 86 01 98 02 00 00 80 3f 00 00 00 00 00 a0 04 44 00 00 00 00 02 01 00 00`
- kind 2, slot 1, page 2, seq 8, w 390, h 664, scale 2, sx 97.5, sy 200, top 0, dsf 2, flags 0:
  `02 01 02 00 08 00 00 00 86 01 98 02 00 00 00 40 00 00 c3 42 00 00 48 43 00 00 00 00 02 00 00 00`

## 5. Agent → viewer JSON messages

Text frames, one JSON object each, at most **65,536 bytes**. A viewer MUST ignore message types it doesn't know
and fields it doesn't know (forward compatibility, §9). All page-derived strings (titles, origins, labels, dialog
text) are untrusted: the agent bounds them before they reach the agent's own process wherever Chromium allows it
(§5.3.2, §5.2), caps and cleans them before sending, and the viewer MUST render them as text only (§8.8).

### 5.1 `ready`

```json
{"t": "ready", "v": 1, "w": 390, "h": 664, "dsf": 2, "features": []}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | protocol minor version, 1 |
| `w`, `h` | integer | the viewport the agent applied: `hello.w`, `hello.h` |
| `dsf` | integer | 1 or 2 |
| `features` | array of strings | optional viewer→agent features this agent accepts; empty in 0.7.0 (§9) |

### 5.2 `pages`

Sent after `ready`, and whenever a page opens, closes, changes title, or the front page changes.

```json
{"t": "pages", "front": 1, "pages": [{"id": 1, "title": "Checkout | BC Ferries", "origin": "https://www.bcferries.com", "main": true}]}
```

| Field | Type | Limits |
|---|---|---|
| `front` | integer | the page whose images are being sent |
| `pages` | array | 1-8 entries, in opening order. Pages beyond 8 are not listed and never shown |
| `pages[].id` | integer | 1-65535, unique per session, never reused within it |
| `pages[].title` | string | ≤ 80 code points, clean text, may be "". The agent reads it with `Target.getTargetInfo` on the page's own session (Chromium caps titles there at 4,096 characters), never with Playwright's `page.title()` (unbounded) |
| `pages[].origin` | string | ≤ 100 code points: `scheme://host[:port]` of the page's URL, or "" (never a path or query) |
| `pages[].main` | boolean | true for the cart's own page (exactly one), which the viewer can't close |

A new page (pop-up or tab) becomes the front page when it opens; when the front page closes, the most recently
opened remaining page becomes front.

### 5.3 `fields` and `fields_ok`: the field map

The editable fields visible on the front page, for the viewer's keyboard proxy and pickers (DESIGN §4.4).

```json
{"t": "fields", "page": 1, "rev": 12, "seq": 340, "n": 57, "sx": 0, "sy": 530.5, "scale": 1, "cw": 390, "ch": 14210,
 "trunc": false,
 "f": [{"id": 1, "k": "text", "r": [22, 281.5, 335, 38.5], "c": [22, 281.5, 335, 38.5], "m": 511,
        "type": "text", "im": "numeric", "ac": "cc-number", "ekh": "", "cap": "", "max": null, "ro": false}]}
```

```json
{"t": "fields_ok", "page": 1, "rev": 12, "seq": 344, "n": 59}
```

#### 5.3.1 Fields

| Field | Type | Meaning, limits |
|---|---|---|
| `page` | integer | the page the map is for |
| `rev` | integer | 0 to 2³²−1, increasing within the session |
| `seq` | integer | the `seq` of the newest binary message the agent had sent when this evaluation started (0 if none) |
| `n` | integer | how many viewer messages the agent had carried out when this evaluation started (§1) |
| `sx`, `sy`, `scale` | number | `visualViewport.pageLeft`, `pageTop`, `scale` of the main frame, **read in the same evaluation as the rectangles** |
| `cw`, `ch` | number | the document's content size in CSS px (`scrollWidth`, `scrollHeight`) |
| `trunc` | boolean | true when the map is incomplete (a time, count or size limit of §5.3.2 was hit). The viewer treats fields that aren't in a truncated map as unknown, never as absent |
| `f` | array | 0-100 fields per frame, at most 800 in all, in frame order then document order |
| `f[].id` | integer | 1-65535, unique within this `rev` |
| `f[].k` | string | `text` \| `area` \| `select` \| `date` \| `time` (`area`: a textarea or a contenteditable element) |
| `f[].r` | [x, y, w, h] | the field's border box, frame px (§5.3.3) |
| `f[].c` | [x, y, w, h] | the part of `r` the hit samples cover (`r` clipped to its frame's viewport, and for the main frame to the visual viewport), frame px |
| `f[].m` | integer | 0-511, the hit mask: bit `j` (`j` = 0-8, row-major, row `j div 3`, column `j mod 3`) is set when a click at the centre of cell `j` of a 3×3 grid over `c` would reach this field and nothing else, in its own frame and in every ancestor frame (§5.3.2). Fields with `m` = 0 are not sent |
| `f[].type` | string | one of `text`, `email`, `tel`, `url`, `number`, `password`, `search`, `date`, `time`, `textarea`, `select`, `contenteditable`; other input types are reported as `text` |
| `f[].im` | string | `inputmode`: one of `none`, `text`, `decimal`, `numeric`, `tel`, `search`, `email`, `url`, or "" |
| `f[].ac` | string | `autocomplete` tokens: the attribute (cut to 64 code points in the script) split on ASCII whitespace and lowercased; a token with any character outside `[a-z0-9-]` is dropped, never repaired (`user_name` doesn't become `username`), and so are the credential tokens `username`, `current-password`, `new-password` and `webauthn`; the rest joined by single spaces; "" if none |
| `f[].ekh` | string | `enterkeyhint`: one of `enter`, `done`, `go`, `next`, `previous`, `search`, `send`, or "" |
| `f[].cap` | string | `autocapitalize`: one of `off`, `none`, `on`, `sentences`, `words`, `characters`, or "" |
| `f[].max` | integer \| null | `maxlength`, if set (1-524288) |
| `f[].ro` | boolean | read-only or disabled |
| `f[].nopt` | integer | `select` only: the select's number of options (0-1,000,000) |
| `f[].opts` | array | `select` only, absent when dropped (§5.3.2): ≤ 400 `[label, disabled]` pairs for options 0, 1, 2, ... in order; label ≤ 64 code points, clean text; `disabled` includes options in a disabled optgroup |
| `f[].sel` | integer | `select` only: selected index, or −1 |
| `f[].trunc` | boolean | `select` only: true when `opts` is absent or shorter than `nopt` |
| `f[].v` | string | `date`/`time` only: the current value (`YYYY-MM-DD` / `HH:MM`) or "" |

The agent MUST NOT send any field's value except a `date`/`time` value and a `select`'s selected index. Inputs of
type `hidden`, `checkbox`, `radio`, `button`, `submit`, `reset`, `image`, `file`, `range`, `color` are omitted,
and so is every field nothing can click (`m` = 0: covered, `visibility: hidden`, `pointer-events: none`, scrolled
out of view).

`fields_ok` says that an evaluation found exactly the map of `rev` again (same page, `sx`, `sy`, `scale`, `cw`,
`ch`, `trunc` and fields), and carries that evaluation's `seq` and `n`.

#### 5.3.2 How the agent evaluates the map

All page access is by fixed scripts (DESIGN §6.4) run in an **isolated world** of each frame
(`Page.createIsolatedWorld`), which page script can't see, with **`Runtime.evaluate`** and **`timeout: 100`** (ms):
the script is a constant function declaration applied to arguments the agent formats itself from integers and
finite floats it computed, never from a viewer string. `Runtime.callFunctionOn` ignores `timeout` and MUST NOT be
used for anything but storing a resolved node in the world (below). A script that times out or throws gives no
result for that frame, and the map is sent with `trunc: true`.

1. **Frames.** The main frame, then each frame whose owner iframe's border box intersects the visual viewport (as
   the parent frame's evaluation reports), breadth first, at most **8** frames in all and at most 3 levels deep.
   Each frame's owner element is resolved into its parent's isolated world once (`DOM.getFrameOwner` on the
   parent's CDP session, `DOM.resolveNode` with the world's `executionContextId`, then one `callFunctionOn` that
   stores it in the world's own global). A frame's world is recreated after the frame navigates; worlds, owners and
   CDP sessions of a detached frame are dropped at once.
2. **In each frame** (`FIELDS`): the candidates are `querySelectorAll("input,textarea,select,[contenteditable]")`
   in document order. The script stops, setting `trunc`, after **500** candidates or **30 ms**. Skipped: the
   omitted input types; elements that aren't editable; border boxes under 4 px in either dimension or outside the
   clip (the main frame's visual viewport; another frame's own viewport). The 3×3 samples are at fractions 1/6,
   1/2, 5/6 of the box intersected with the clip; a sample is a hit when `document.elementFromPoint` returns the
   element or a node inside it. Strings are cut **inside the script** (labels and `autocomplete` to 64, enumerated
   attributes to 16, values to 10), selects report at most **400** options, and the script returns at most **100**
   fields and at most **48 KiB** of serialized JSON: a select whose options would exceed the budget is returned
   without `opts` (with `trunc`); a field that still doesn't fit ends the scan (`trunc`).
3. **Occlusion and offset through ancestors** (`OWNER_HIT`): for a frame inside an iframe, all its points (each
   field's box corner and 9 samples) are passed up the owner chain. At each level the owner's script returns its
   content box's origin in the parent's client coordinates (`getBoundingClientRect()` plus `clientLeft/clientTop`
   plus padding) and, per point, whether the parent would deliver a click there to this iframe
   (`elementFromPoint` at origin + point is the iframe; in the main frame, also inside the visual viewport). The
   points are shifted by the origin and the hits ANDed into the masks. At most 2048 numbers per call, 30 ms per
   call (points not tested in time count as covered).
4. **Assembly.** Rectangles are converted with §5.3.3. If the message would exceed 65,536 bytes, the agent removes
   `opts` from selects, largest first (setting their `trunc`), then drops fields from the end, setting the map's
   `trunc`.

#### 5.3.3 Rectangles in frame px

For a field in frame F with client rectangle `(x, y, w, h)` in F's own coordinates (`getBoundingClientRect`):
- **F is the main frame:** `r = ((x − ox)·s, (y − oy)·s, w·s, h·s)`.
- **F is inside an iframe chain** F → P₁ → … → main, with content-box origins `(aᵢ, bᵢ)` of each owner in its
  parent's client coordinates (step 3 above): the main-frame client point is `(x + Σaᵢ, y + Σbᵢ)` and
  `r = ((x + Σaᵢ − ox)·s, (y + Σbᵢ − oy)·s, w·s, h·s)`. (Pinch zoom applies only to the main frame; frames inside
  iframes have no visual viewport offset of their own.)

`(ox, oy, s)` are the main frame's `visualViewport.offsetLeft`, `offsetTop`, `scale` from the same evaluation.
`DOM.getBoxModel` MUST NOT be used for offsets (its coordinates are relative to the visual viewport, unscaled,
which differs from `getBoundingClientRect`; mixing the two put fields 300 frame px off in review). Test vectors:
§11.3.

#### 5.3.4 When the agent evaluates

One normative schedule:
- An evaluation is **due** when: an image with a `seq` greater than the latest evaluation's `seq` was sent; a frame
  of the front page navigated, attached or detached; the front page changed; or a `tap`, `wheel`, `key`, `text`,
  `compose`, `choose`, `date`, `dialog`, `page`, `pinch` or `resize` was carried out (so the map's `n` catches up
  with every message §8.3 waits for, even one that changed nothing on screen).
- It **starts** when it is due, no evaluation is in flight, at least **250 ms** have passed since the previous one
  started, and at least **50 ms** have passed since the event that made it due. After an evaluation that hit its
  30 ms budget or timed out, the next starts no sooner than **1 s** later. There is no debounce: continuous images
  give one evaluation every 250 ms, never none.
- The result is sent as `fields` with `rev` + 1 when anything differs from the latest `fields`, otherwise as
  `fields_ok` for the same `rev`.

### 5.4 `focus`

Sent when the focused editable element of the front page changes, and after a `tap`, a `Tab`/`Enter`/`Escape`
key, `choose`, `date`, a `dialog` answer and `page show` (50 ms after the message was carried out), and 50 ms
after typing pauses (at most every 250 ms while typing). It includes focus the page moves itself, such as a card
number field advancing to the expiry.

```json
{"t": "focus", "page": 1, "seq": 341, "n": 58, "f": {"k": "text", "r": [22, 281.5, 335, 38.5], "c": [22, 281.5, 335, 38.5], "m": 511, "type": "text", "im": "numeric", "ac": "cc-number", "ekh": "", "cap": "", "max": null, "ro": false}, "popup": false}
```

- `seq`, `n`: as in §5.3.1, for the evaluation behind this report.
- `f` is a field object as in §5.3.1 without `id` (with `nopt`/`opts`/`sel`/`trunc` for a select, `v` for a
  date/time), or `null` when nothing editable has focus. The innermost focused frame's element is reported (a
  frame's focused element being an `IFRAME` means "look inside").
- `popup` is true when the message just carried out was a `tap` whose point lies inside the now-focused element and
  that element is a `<select>`: the agent's own click opened its list in the pod, which is never in the images. The
  viewer MUST then show its own option picker built from `opts` (§8.6).

### 5.5 `dialog`

A JavaScript dialog on any page of the checkout. The page is blocked until it is answered.

```json
{"t": "dialog", "id": 3, "page": 1, "kind": "confirm", "origin": "https://pay.example", "msg": "Leave checkout?", "def": ""}
```

| Field | Type | Limits |
|---|---|---|
| `id` | integer | 1-65535, unique per session (a dialog still open from an earlier session gets a new id) |
| `page` | integer | the page showing it |
| `kind` | string | `alert` \| `confirm` \| `prompt` \| `beforeunload` |
| `origin` | string | ≤ 100 code points: `scheme://host[:port]` of the URL of the **frame** that opened the dialog (`Page.javascriptDialogOpening.url` on the page's CDP session, which has the Page domain enabled for this), or "" if unknown |
| `msg` | string | ≤ 500 code points, clean text except U+000A (line feed) is kept |
| `def` | string | ≤ 256 code points, clean text: the prompt's default ("" otherwise) |

Dialogs are kept for whoever views the checkout, of either kind, from the gate's first admission of a viewer in the
pod's life (before it, Playwright dismisses every dialog the moment it opens, as in 0.6.4, which suits the agent's
own browsing). A kept dialog stays open until it is answered: by a stream viewer, with `dialog` (§6.13); or by a
classic viewer on the pod's screen, where Chromium draws it (0.7.0 fixes 0.6.4's classic viewer, which never saw a
dialog: Playwright dismissed each one unseen). Dialogs stay open across a reconnect (§3.5) and a switch between the
two kinds. A dialog that closes without the stream viewer's answer (its page navigated or closed) is announced to
the stream session as `dialog_closed` (§5.6). The agent dismisses every open dialog (and accepts a `beforeunload`, as
Playwright does) when the gate closes the viewer with 4000, 4002 or 4003 (the checkout is over, access was
revoked, the pod is draining), and then stops keeping them, so its own browsing afterwards (emptying the cart)
can't wait on one. A 4005 leaves them open for the classic viewer the stream moves to.

### 5.6 `dialog_closed`

`{"t": "dialog_closed", "id": 3}`: the dialog went without an answer from this viewer (the page navigated, or the
agent dismissed it). The viewer MUST remove it.

### 5.7 `error`

`{"t": "error", "code": "stale", "ref": "choose"}`: a viewer message was well formed but not carried out. Not
fatal.

| `code` | Meaning |
|---|---|
| `invalid` | A value broke a rule of §6 (out of range, too long, not typed text, a bad key combination). The message was dropped |
| `stale` | It named a page that isn't the front page; an image the agent no longer remembers, or one that no longer matches the page (§6.4); a field-map `rev` the agent no longer holds (it keeps the latest 2); or a field that is gone, changed, or covered at the moment of acting (§6.11). **Nothing was sent to the page** |
| `failed` | The agent acted and the page didn't take it (e.g. the select didn't change, or the field didn't get focus) |
| `rate` | Dropped by a rate limit (§7.1) |
| `unsupported` | e.g. `date` on a pod whose browser locale isn't en-US |

`ref` is the `t` of the message concerned. The agent sends at most one `error` a second for each `code`.

### 5.8 `clock`

```json
{"t": "clock", "ends": 1792039320, "hard": 1792041120, "paused": false}
```

`ends`: epoch when the checkout session clock runs out, or `null` while it is paused (the payment page is
showing); `hard`: epoch of the hold's hard cap; `paused`: the payment pause. Sent after `ready` and whenever any
value changes (the agent checks every second, §7.2).

## 6. Viewer → agent messages

Text frames only, each one JSON object of at most **4096 bytes** UTF-8. The agent decodes each in two steps:

1. **Structure** (msgspec, `forbid_unknown_fields`): a binary frame; text that isn't UTF-8 JSON (including a
   string with a lone surrogate escape such as `"\ud800"`, and a number too large for a double); a JSON value that
   isn't an object; a missing or unknown `t`; an unknown field; a missing required field; a value of the wrong JSON
   type (a string for a number, an integer written with a fraction or an exponent, `null` where not allowed: an
   optional field is absent, never `null`, unless its row says so). Any of these, any message but `hello` before
   `ready`, a second `hello`, and a `hello` that breaks any rule of §6.1, close **4400**.
2. **Values** (in Python, after decoding): every range, length, enumeration, pattern and combination rule below.
   A message that breaks one is dropped and answered with `error invalid`; it counts toward the drop limit (§7.1).

Coordinates `x`, `y` (and `x0`, `y0`, `x1`, `y1`) are frame px in `[0, w]`×`[0, h]` of the image the message
refers to (`w`, `h` from its header: the image named by `seq` for `tap` and `pinch`, otherwise the latest binary
message the agent sent for that page); outside: `error invalid`. A `tap` or `pinch` whose `seq` the agent no
longer remembers (§4.2) passes this step and is answered `error stale` when carried out (§6.4 step 1); a `move` or
`wheel` for a page the agent has sent no image for is ignored.

### 6.1 `hello`

```json
{"t": "hello", "v": 1, "w": 390, "h": 664, "dpr": 3}
```

| Field | Type | Limits | Meaning |
|---|---|---|---|
| `v` | integer | = 1 | the viewer's protocol minor version |
| `w` | integer | 240-1920 | the viewer's layout viewport width in CSS px, clamped by the viewer (§8.1) |
| `h` | integer | 240-1600 | its height, clamped |
| `dpr` | number | 0.5-5 | `devicePixelRatio`, clamped |

The agent applies `w`×`h` as the pod page's viewport, at scale factor 2 when `dpr ≥ 1.5` and `w·h·4 ≤ 1,600,000`,
else 1 (a Mac's two scale factors). A `hello` that breaks any of these rules closes 4400 (the session can't start).

### 6.2 `resize`

`{"t": "resize", "w": 844, "h": 390, "dpr": 3}`: same fields and limits as `hello` (values outside: `error
invalid`). Sent when the viewer's layout viewport changes (rotation, a desktop window resize), never for the
on-screen keyboard. The agent applies the latest after 300 ms without another, as in §3.2 step 3, at most 16
distinct sizes per session (further ones: `error rate`).

### 6.3 `ack`

`{"t": "ack", "seq": 7}`. `seq`: 1 to 2³²−1, a binary message's `seq`. Acks for a `seq` not outstanding are
ignored.

### 6.4 `tap`

`{"t": "tap", "page": 1, "seq": 341, "x": 120.5, "y": 300, "ms": 95}`. A primary click at a point of the image the
viewer is drawing.

| Field | Type | Limits | Meaning |
|---|---|---|---|
| `page` | integer | 1-65535 | the image's page |
| `seq` | integer | 1 to 2³²−1 | the `seq` of the binary message the viewer is drawing |
| `x`, `y` | number | in the image (above) | the point |
| `ms` | integer | 0-300, optional (default 0) | how long the button or finger was down (§8.4) |

Agent:
1. If `page` isn't the front page, or `seq` isn't among the last 16 binary messages sent: `error stale`.
2. Read the live state with `Page.getLayoutMetrics`. If the image's `page` differs from the front page, or its
   `sx`, `sy` differ from `cssVisualViewport.pageX`, `pageY` by more than 1 CSS px, or its `scale` from `scale` by
   more than 0.01: `error stale`. (The user tapped something the page has since moved away from; a click there
   would hit whatever is there now.)
3. Otherwise dispatch `mouseMoved`, `mousePressed`, then wait `ms`, then `mouseReleased` (left button,
   `clickCount` 1), all at (`x/s`, `y/s`) with `s` the live scale. Then `focus` (§5.4) follows.

### 6.5 `move`

`{"t": "move", "page": 1, "x": 120.5, "y": 300}`. The mouse pointer moved (mouse viewers only, §8.4). Agent: if
`page` is the front page, `mouseMoved` at (`x/s`, `y/s`) with no button; otherwise ignored. Coalesced (§3.4).

### 6.6 `wheel`

`{"t": "wheel", "page": 1, "x": 195, "y": 500, "dx": 0, "dy": 14.5}`. Scroll by `dx`, `dy` frame px (each −4000 to
4000), as a wheel over the point (`x`, `y`) where the gesture started. Positive `dy` scrolls the content up
(reveals what's below). Momentum after a flick is the viewer's: it keeps sending decaying `wheel` messages.
**The viewer MUST send at most one `wheel` per 33 ms**, summing the deltas of the frames in between. Agent:
`mouseWheel` at (`x/s`, `y/s`) with `deltaX = dx`, `deltaY = dy` (not divided: the page moves `d/s` CSS px, which
is `d` frame px, following the finger). Coalesced (§3.4). A `wheel` for a page that isn't the front page is
ignored.

### 6.7 `visible`

`{"t": "visible", "v": false}`: `v` boolean, the viewer document's visibility. Applied on arrival (§3.4); the agent's
behaviour is §3.8.

### 6.8 `pinch`

`{"t": "pinch", "page": 1, "seq": 341, "x0": 195, "y0": 400, "x1": 180, "y1": 380, "scale": 1.8}`. A completed
two-finger gesture: the content point under (`x0`, `y0`) when it started ends under (`x1`, `y1`), zoomed by the
factor `scale` (0.25-4). (`x0`, `y0`) is the fingers' centroid when the gesture started, in the image `seq`;
(`x1`, `y1`) their centroid when it ended, in the same image's frame px.

Agent: steps 1-2 of §6.4 with this `seq`. Then `Input.synthesizePinchGesture` with `gestureSourceType: "mouse"`
(a trackpad pinch: the page sees ctrl+wheel events, as from a Mac trackpad) and `scaleFactor = scale` at
(`x0/s`, `y0/s`), which keeps the content under that point where it is; then, if (`x0 − x1`, `y0 − y1`) isn't
(0, 0), `mouseWheel` at (`x1/s′`, `y1/s′`) with `deltaX = x0 − x1`, `deltaY = y0 − y1`, `s′` being the scale after
the pinch, which moves that content to (`x1`, `y1`). The browser clamps the resulting scale to 1-4; a page that
handles pinch itself (cancels ctrl+wheel) gets the events and no zoom, as with a real trackpad.

### 6.9 `key`

A key press, or one half of one: exactly one of `k` (a named key) or `c` (one character), optional `shift`,
`word` and `ph`.

```json
{"t": "key", "k": "Backspace"}
{"t": "key", "c": "4"}
{"t": "key", "k": "Tab", "shift": true}
{"t": "key", "k": "Backspace", "word": true}
{"t": "key", "k": "Shift", "ph": "down"}
{"t": "key", "c": "M", "ph": "down"}
{"t": "key", "c": "M", "ph": "up"}
```

| Field | Values |
|---|---|
| `k` | `Backspace`, `Delete`, `Enter`, `Tab`, `Escape`, `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown`, `Home`, `End`, `PageUp`, `PageDown`; and `Shift` with `ph` only |
| `c` | exactly one code point of clean text (a space is allowed) |
| `shift` | boolean, default false; without `ph`, with `k` only: Shift held for this press |
| `word` | boolean, default false; without `ph`, with `k` ∈ {`Backspace`, `Delete`, `ArrowLeft`, `ArrowRight`} only: word-wise (sent to the pod as Ctrl+key) |
| `ph` | `down` \| `up`, optional. Absent: a whole press (down then up at once). Present: only that half (physical keyboards, §8.4); `shift` and `word` MUST then be absent or false |

Any other combination is `error invalid`. No other modifier exists: Ctrl (except as `word`), Alt and Meta can't be
sent, so no browser shortcut can be.

Agent:
- A whole press: `Input.dispatchKeyEvent` `keyDown` (with `text` = `c`, or `"\r"` for Enter) or `rawKeyDown`, then
  `keyUp`, with the key's `key`, `code` and Windows virtual key code (DESIGN §6.5 has the table), modifiers 8 for
  `shift` (or while a `Shift` is held down), 2 for `word`.
- `ph: down`: the down half only; the agent records the key as held. At most **6** keys are held; a seventh `down`
  first releases the oldest. A `down` for a key already held is ignored (CDP never auto-repeats). While `Shift` is
  held, every key event carries modifiers 8 (the `Shift`'s own `rawKeyDown` included, its `keyUp` not, as a real
  keyboard reports them).
- `ph: up`: the up half, if that key (`k` or `c`, compared exactly) is held; otherwise ignored.
- Teardown releases every held key (§3.5).
- `focus` (§5.4) follows `Tab`, `Enter` and `Escape`.

### 6.10 `text` and `compose`

`{"t": "text", "s": "4242 4242"}`. Insert typed text at the focused field's caret (CDP `Input.insertText`, which
applies the browser's own paste rules: in a single-line input a trailing line feed is dropped and other line feeds
become spaces; a textarea keeps them; nothing submits a form). `s`: 1-256 code points of **typed text** (§1), at
most 1024 bytes as UTF-8. For one character typed on its own, the viewer SHOULD send `key` with `c` instead. The
viewer MUST normalise and split as §8.7 says.

`{"t": "compose", "s": "Ches"}`. Set the IME composition string (CDP `Input.imeSetComposition`, caret at its end).
`s`: 0-64 code points of clean text; `""` cancels the composition. A composition is committed by a `text` message
carrying the final string (which replaces the composition; nothing is duplicated).

### 6.11 `choose`

`{"t": "choose", "page": 1, "rev": 12, "f": 3, "i": 4}`. Pick option index `i` (0-399, an index into the map's
`opts`) of the `select` field `f` of field-map revision `rev`.

Agent, in this order, sending nothing to the page until step 4:
1. `rev` must be one of the latest 2 revisions and `f` a `select` with `opts` in it: else `error stale`. Option `i`
   must exist in `opts` and not be disabled: else `error failed`.
2. In the field's frame world (`CLICK_POINTS`): the element must still be connected, with the same `nopt` and the
   same labels and disabled flags for the options in `opts`; else `error stale`.
3. A click point: the first of the centre and then the 9 sample points of the element's current box where
   `elementFromPoint` is the element itself, and every ancestor frame would deliver the point to the owning iframe
   (`OWNER_HIT`, §5.3.2). None: `error stale`. This never clicks a point where something else (a fixed footer, a
   cookie banner, a parent page's overlay) would get the click.
4. Unless the latest `focus` reported `popup: true` for this same element and nothing has been carried out since,
   a click (`mouseMoved`, `mousePressed`, `mouseReleased`) at that point. Then (`IS_FOCUSED`) the element must be
   its document's `activeElement`, and each ancestor's `activeElement` its owning iframe; if not, send `Escape`
   and `error failed`. **No other key is sent unless this holds.**
5. `Home`, then `ArrowDown` once per enabled option after the first enabled one up to `i`, then `Enter`: one
   trusted `input` and `change`, as a keyboard user gets.
6. Re-read the selected index; if it isn't `i`, send `Escape` and `error failed`.

### 6.12 `date`

`{"t": "date", "page": 1, "rev": 12, "f": 5, "v": "2026-10-16"}`. Set a `date` (`v` = `YYYY-MM-DD`, a real calendar
date) or `time` (`v` = `HH:MM`, 24-hour) field, in ASCII digits. A `v` of the wrong form for the field's `k`:
`error invalid`. Agent: steps 1-2 of §6.11, with `f` a `date` or `time` field of `rev` (else `error stale`); step 3
with one candidate point only, the first segment at (`x + min(12, w/4)`, `y + h/2`) of the element's box (else
`error stale`); step 4 (the click is always made; no digit is typed unless the focus check holds, else `error
failed`); then the digits in the pod's en-US order
(`MMDDYYYY`; `hhmm` then `A`/`P`) as whole key presses, and a re-read of the value (`error failed` if it differs).
If the pod's browser locale isn't en-US: `error unsupported`.

### 6.13 `dialog`

`{"t": "dialog", "id": 3, "ok": true, "s": "FERRY10"}`. Answer dialog `id`: `ok` true accepts (OK), false
dismisses (Cancel). `s` (optional, ≤ 256 code points of clean text) is the prompt's text; ignored for other kinds.
An unknown or already-closed `id` is ignored.

### 6.14 `page`

`{"t": "page", "op": "show", "page": 2}` or `{"t": "page", "op": "close", "page": 2}`. `op`: `show` or `close`.
`show` makes `page` the front page (the agent brings it to the front, sends a screenshot-based image, then streams
it). `close` closes it, running its `beforeunload`; closing the `main` page is not possible (`error failed`).
Unknown pages are ignored.

## 7. Limits and authorization

### 7.1 Rates

The agent keeps a token bucket per message type and one overall. A message over its type's bucket is dropped (not
carried out) and answered with `error rate`. More than **200** dropped messages (over a bucket, or `error
invalid`) in any 10 s, or a message over the overall bucket, closes **4429**.

Each type has a bucket of its own (a row naming two types gives each of them one). Every bucket starts full (its
burst) and refills continuously at its rate. Only messages that passed the value rules (§6) are rate-limited: an
`error invalid` takes no tokens. A message is admitted only if its type's bucket (for `text`, both of its buckets)
holds enough tokens, and then takes them and one from the overall bucket; a message over its type's bucket takes
nothing from any bucket. The 10 s of the drop limit is a sliding window.

| Type | Rate (/s) | Burst | Viewer sends at most |
|---|---|---|---|
| `ack` | 40 | 40 | one per binary message |
| `tap` | 10 | 5 | |
| `move` | 40 | 40 | one per 33 ms |
| `wheel` | 40 | 20 | one per 33 ms (§6.6) |
| `pinch` | 4 | 2 | |
| `key` | 60 | 40 | (down and up halves are separate messages) |
| `text` | 20 messages and 512 code points | 10 messages, 1024 code points | |
| `compose` | 60 | 30 | |
| `choose`, `date` | 4 | 2 | |
| `dialog`, `page` | 4 | 2 | |
| `resize`, `visible` | 4 | 4 | |
| all types together | 300 | 400 | |

### 7.2 Every input and every output is authorized

The gate gives the session a `verdict()` callback (DESIGN §6.2) that returns nothing while the connection's locked
cart is still a cart this viewer may open, and otherwise the close code: **4003** draining; **4002** `vnc_allowed`
false or lockdown failed; **4005** `cast_allowed` false; **4000** the cart was released, expired, booked, lost or
changed, or a hold clock ran out (`release_due`, evaluated on the clock, not on the watch loop's schedule).

The agent MUST call `verdict()` before carrying out any message other than `ack` and `visible`, before sending any
binary or JSON message, and at least once a second while the connection is open; when it returns a code, the agent
closes with it without carrying out or sending anything more. Separately, the gate's own `close()` (release,
expiry, revocation, drain, kill switch) closes with **4000**, **4002**, **4003** or **4005**, and each connection is
closed after **6000 s** with **4004**.

### 7.3 What the agent never does for a viewer

- forward or accept raw CDP, JavaScript, URLs, cookies, storage, or files; or put a viewer string into a CDP method
  name, a script, or a script argument;
- navigate, go back or forward, reload, open a page, or close the main page;
- send key modifiers other than Shift (and Ctrl for the `word` keys listed in §6.9);
- send right-clicks, middle-clicks, drags or touch events;
- click a point where, at that moment, something other than the intended element would receive the click (`choose`,
  `date`), or send a key to a select or date field that hasn't got focus;
- log, store or include in any state message the content of `text`, `compose`, `key`, `dialog.s` or any field.

## 8. What the viewer MUST do

### 8.1 Size and drawing

- `hello.w`/`resize.w` = `min(max(innerWidth, 240), 1920)`; `h` = `min(max(innerHeight, 240), 1600)`; `dpr` =
  `min(max(devicePixelRatio, 0.5), 5)`. (`innerWidth`/`innerHeight` of the layout viewport; the on-screen keyboard
  isn't part of it.)
- Each image is drawn into a box of `w·k` × `h·k` CSS px, centred in the viewer's viewport, with
  `k = min(1, innerWidth / w, innerHeight / h)` and `w`, `h` from that image's header; the rest is a plain
  letterbox. A viewer point (`px`, `py`) (CSS px) is at frame px `((px − bx)/k, (py − by)/k)`, where (`bx`, `by`) is
  the box's top-left corner. Points outside the box are not on the image: they start no gesture and send nothing.

### 8.2 What a tap refers to

- `tap.seq` and `pinch.seq` MUST be the `seq` of the binary message whose image is drawn when the gesture began
  (pinch) or ended (tap).
- While the viewer coasts after a flick, or draws an image shifted by a scroll prediction or scaled by a local
  pinch zoom (DESIGN §7.4), a touch that would be a tap is not a tap: it ends the coast or the prediction, and
  nothing is sent. What counts is the state when the touch begins.

### 8.3 Using the field map

A map `M` (a `fields` message, plus any later `fields_ok` for its `rev`, whose `seq` and `n` replace `M`'s) is
**usable** for the drawn image `I` only when all of these hold:
- `M.page` = `I.page`, `|M.sx − I.sx| ≤ 0.5`, `|M.sy − I.sy| ≤ 0.5`, `|M.scale − I.scale| ≤ 0.001`;
- `|I.seq − M.seq| ≤ 4`;
- `M.n` ≥ the number of the viewer's latest `tap`, `wheel`, `pinch`, `key`, `text`, `compose`, `choose`, `date`,
  `dialog`, `page` or `resize` message (the map was taken after the page had everything the user has done);
- no prediction shift or local zoom is drawn (§8.2).

A tap point **hits** field `f` when it lies in `f.c` and the bit of `f.m` for the 3×3 cell of `f.c` containing it
is set. With `f.c` = `[x, y, w, h]`, the point (`px`, `py`) lies in it when `x ≤ px < x + w` and `y ≤ py < y + h`,
and its cell is column `floor(3·(px − x)/w)`, row `floor(3·(py − y)/h)`. A point that hits more than one field
hits none of them (the viewer can't tell which would get the click; the `focus` report settles it). Only a usable
map's fields can be hit. Native picker overlays (§8.6) are shown only while the map is usable, only for a `select`,
`date` or `time` field with `m` = 511, `c` = `r`, `r` inside the image (`0 ≤ x`, `0 ≤ y`, `x + w ≤` the image's
`w`, `y + h ≤` its `h`), not `ro`, and for a select only with `opts` and not `trunc`; every overlay sits below the
viewer's own controls.

### 8.4 Pointer and keyboard input

- **Touch** (and pen): a touch that stays less than 8 CSS px from where it began and lasts under 500 ms is a tap at
  the point where it began, sent on `touchend` with `ms` = its duration (clamped to 300). One finger moving 8 px or
  more from where it began starts a scroll (`wheel`) whose deltas follow the finger from that point; two fingers a
  local pinch, then one `pinch` as soon as fewer than two fingers remain, its `scale` clamped to 0.25-4 (the
  remaining finger then does nothing). A touch that begins outside the image (§8.1) does nothing. No `move`
  messages.
- **Mouse** (`pointerType === "mouse"`): `move` from `pointermove` while over the image, at most one per 33 ms
  (the latest position); a click is a `tap` at the `pointerdown` point, sent on `pointerup`, with `ms` = the press
  duration (clamped to 300; there is no 500 ms limit); the wheel is `wheel` (line-mode deltas × 40).
- **Physical keyboard** (a `keydown`/`keyup` with `isComposing` false and no Ctrl, Alt or Meta, except AltGr-made
  characters): `key` with `ph: down` on `keydown` and `ph: up` on `keyup`, for the named keys of §6.9, `Shift`, and
  keys whose `key` is one code point of clean text (sent as `c`; the `up` carries the same `c` its `down` did,
  matched by `KeyboardEvent.code`); `preventDefault` on those `keydown`s. Dead keys, IME (`key` "Process") and
  everything else are left to the composition and `beforeinput` paths. On `blur` of the viewer window, every key
  sent down is sent up.
- **On-screen keyboards** (no physical `keydown` for the character, `beforeinput` on the proxy): whole presses
  (`key` without `ph`), `text`, `compose`.
- Cmd/Ctrl/Alt combinations are never sent; paste arrives as `beforeinput insertFromPaste` (§8.7).

### 8.5 The keyboard proxy

The viewer's hidden `<input>` that receives the phone's keyboard (DESIGN §4.4):
- `type`: `email`, `tel` or `url` when the field's `type` is that; otherwise `text`. **Never `password`.**
- For a field of type `password`: `type=text`, `autocomplete=off`, `autocorrect=off`, `autocapitalize=off`,
  `spellcheck=false`, no `inputmode` (the same as KasmVNC's own hidden input in 0.6.4).
- Otherwise: `inputmode` from `im` (`number` fields get `decimal`); `autocomplete` from `ac` with the credential
  tokens removed again (`username`, `current-password`, `new-password`, `webauthn`), or `off` if nothing remains;
  `enterkeyhint` from `ekh`; `autocapitalize` from `cap`, else `words` for names and `off` for email, url and card
  fields; `autocorrect=off` and `spellcheck=false` always.
- `opacity: 0`, `caret-color: transparent`, `font-size: 16px`, and **`pointer-events: none`**: it never takes a
  touch, click or wheel; the viewer's gesture layer above it does, so taps inside a focused field move the pod's
  caret and drags starting on it scroll. It is focused programmatically, inside the `touchend` of the tap that hit
  the field (iOS opens the keyboard only for focus inside a user gesture).
- The prompt input of the dialog sheet (§8.8) follows the password rules above.

### 8.6 Pickers and sheets

- A native `<select>` or `<input type=date|time>` overlay, placed per §8.3; on `change` the viewer sends `choose`
  or `date` with the map's `rev`.
- On `focus.popup`, the viewer shows its own option sheet built from `opts` (labels via `textContent`), and sends
  `choose`; a select with `trunc` shows the options it has and a note that the rest need the classic viewer.

### 8.7 Text normalisation and splitting

Before sending `text`, the viewer MUST:
1. replace each CR LF pair and each lone CR with LF;
2. remove lone surrogates, C0 controls other than TAB and LF, DEL and C1 controls;
3. send nothing if nothing remains;
4. split into consecutive chunks of at most 256 code points (never inside a surrogate pair: split the code-point
   array, not the UTF-16 string), where a chunk that would end with a LF while more text follows ends just before
   that LF instead (the LF starts the next chunk, whose own end is counted from there), and send each as one `text`,
   in order.

The same rules apply to `compose` (without TAB and LF; a composition longer than 64 code points is not sent until it
is committed by `text`) and to `dialog.s` (without TAB and LF; cut at 256). Test vectors: §11.4.

### 8.8 Page-derived text

Titles, origins, option labels, dialog messages and prompt defaults MUST be rendered only with `textContent` or as
attribute values of the viewer's own form controls, never as HTML (the CSP also enforces Trusted Types). The dialog
sheet MUST show the dialog's origin as its heading, as Chrome does: "`<origin>` says:" when it equals the page's
origin in `pages`, "An embedded page at `<origin>` says:" when it differs, and "This page says:" when it is "";
and MUST look like page content, not like the viewer's own controls (it never carries the Ferry Watch name, logo or
the viewer's button style).

### 8.9 Storage

The viewer MUST NOT write anything typed (the proxy's value, `text`, `compose`, prompt answers) to any storage, URL
or log.

### 8.10 The escape hatch

"Use the classic viewer" MUST be reachable however broken the viewer's modules are: `viewer.html`'s static markup
contains it (hidden), and `fw/session.js` (a classic script) shows it 3 s after load unless the viewer module has
called `FwSession.viewerReady()` after its first `ready`, and also on any uncaught `error` or
`unhandledrejection`. Its handler is `FwSession.useClassic(trip, gen)` (sets `viewer_fallback`, sticky for this
checkout on this device), then closing the socket with 1000, then `location.replace(FwSession.classicUrl(trip,
gen))`. The viewer's menu and status screens offer it too, and 4005 takes it automatically.

## 9. Versioning

- The subprotocol name carries the major version. An incompatible change is `fw.cast2`; a viewer that supports both
  offers the newest it knows **alone** (the gate refuses two kinds at once), and falls back to the older on failure
  only if the device record says the pod predates it.
- Within `fw.cast1`, the agent MAY add agent→viewer message types, fields and header flag bits; viewers MUST ignore
  what they don't know.
- The agent's inbound schema is strict. A viewer MUST NOT send optional message types or fields unless the
  session's `ready.features` lists them. 0.7.0 defines none: everything in §6 is the base protocol.
- `hello.v` and `ready.v` are the minor versions (1). An agent receiving a `hello.v` it doesn't support closes
  **4400**.

## 10. Conformance

### 10.1 The shared conformance pack

`tests/fixtures/cast1/` holds JSON cases that **both** the agent's Python tests and the viewer's JavaScript tests
load (DESIGN §11, step 0). An implementation conforms only if it passes all of them:

| File | Cases | Agent checks | Viewer checks |
|---|---|---|---|
| `messages.json` | every message type: valid examples, each limit's boundary, structural violations, value violations; also the upgrade's offers (§2.1-2.2), `hello` from the window (§8.1) and its scale factor (§6.1), the rate limits (§7.1), queue coalescing (§3.4), the agent→viewer shapes (§5, §9) and `autocomplete` cleaning (§5.3.1, §8.5) | outcome: carried out / `error invalid` / close 4400 (1009 for size); the gate's offer outcome, `dsf`, rates, coalescing, its outbound schema, `ac` | the viewer's own encoder produces only the valid ones; its offer, `hello`, decoding of agent messages, the proxy's `autocomplete` |
| `headers.json` | §4.4 and more; malformed binary messages | pack/unpack | parse; refuse the malformed |
| `rects.json` | page states (scale, visual viewport offset, frame chains, client rects) → `r`, `c`; §11.3 | `frame_rect()` | — |
| `hits.json` | a map + an image + tap points → field id or none; overlay or not | — | `fieldAt()`, `overlayFor()` |
| `usable.json` | (map `seq`, `n`, offsets) × (image, last input number, prediction) → usable or not; message numbers (§1) | message numbers | `mapUsable()`, message numbers |
| `text.json` | §11.4: raw input → messages | value rules accept every output | `normalise()` |
| `gestures.json` | timed touch/mouse sequences → messages (taps with `ms`, wheels at most one per 33 ms with exact sums, pinch `x0..y1`, no tap during a prediction) | — | the gesture state machine driven by a fake clock |
| `mapping.json` | frame px + scale → CDP coordinates; pinch → CDP calls; `tap` staleness cases; key events (DESIGN §6.5); `date` digits and `choose` keys | `InputMapper` | — |
| `close_codes.json` | close code (+ history) → reconnect delay / stop / classic | — | `nextAction()` |

`tests/fixtures/cast1/README.md` gives each file's schema and how each side runs its cases; `tests/test_cast_pack.py`
checks that the files match it.

### 10.2 Checklists

An agent conforms when it: refuses offers with both or neither kind, and `fw.cast1` when `cast_allowed` is false
(400); paces session starts; applies the 4096-byte limit; closes 4408/4400 on a missing, late, repeated or bad
`hello`; separates structural violations (4400) from value violations (`error invalid`); never has more than 2
un-acked binary messages; numbers them from 1 without gaps; seeds every session, page switch and return from
hidden with a screenshot-based image; sends kind 2 only for byte-identical images in the named slot; switches
between live, still and paused as §4.3 says; carries out inputs in order, coalescing only as §3.4 says; refuses
stale taps and pinches without dispatching anything; evaluates the field map only in isolated worlds, with every
bound of §5.3.2 inside the scripts and `Runtime.evaluate` timeouts, on the schedule of §5.3.4, with rectangles per
§5.3.3; carries out `choose`/`date` per §6.11-6.12; checks `verdict()` before every input and output and every
second; enforces §7.1; closes with the §3.6 codes; never restores the screen except before a `binary` viewer; keeps
dialogs across reconnects and dismisses them where §5.5 says; releases held keys at teardown; and never logs typed
text.

A viewer conforms when it: offers `fw.cast1` and the nonce (and the pending ticket only before its first open);
sends `hello` first, clamped, and nothing else before `ready`; acks every binary message in order after drawing it;
handles binary messages strictly in order; draws per §8.1; sends coordinates in frame px of the drawn image with its
`seq`; sends no tap while a prediction or local zoom is drawn; uses the field map only when §8.3 says it is usable;
sets the proxy per §8.5 (never `type=password`, no credential tokens, `pointer-events: none`); normalises and splits
text per §8.7; sends at most one `wheel` and one `move` per 33 ms; sends `visible`; never sends Ctrl/Alt/Meta;
renders every page-derived string as text and dialogs with their origin; follows §3.6 and §3.7; always offers the
escape hatch per §8.10; and stores nothing it was typed.

## 11. Test vectors

### 11.1 Headers

§4.4.

### 11.2 Coordinates

At scale 2 with visual viewport offset (175, 250): a tap at frame px (29, 121.5) is dispatched at CDP (14.5, 60.75).
At scale 1: frame px = CDP coordinates.

### 11.3 Rectangles (measured on the pod screen, 390×664 at 2x; `round2/out/s7*.json`)

| Case | Inputs | `r` | Notes |
|---|---|---|---|
| Main frame, zoomed | `s` = 2, (`ox`, `oy`) = (150, 150); field client rect (12, 292.5, 351, 38.5) | (−276, 285, 702, 77) | `c` = (0, 285, 375, 77) (clipped to the visual viewport, whose width excludes the 15 px scrollbar); `m` = 511; a CDP click at (40/2, 323.5/2) focused it |
| Cross-site iframe, zoomed | `s` = 2, (`ox`, `oy`) = (175, 250); the iframe's content-box origin in the main frame (14, 262); field client rect in the iframe (8, 29.5, 335, 38.5) | (−306, 83, 670, 77) | `c` = `r` (clipped to the iframe's own viewport only); the mask's left column is 0 (outside the image): `m` = 438; a CDP click at (29/2, 121.5/2) focused it |
| Cross-site iframe, zoomed | as above; field client rect (182.5, 91.5, 160.5, 38.5) | (43, 207, 321, 77) | clicked at its centre: focused |
| Not zoomed | `s` = 1, (0, 0); client rect (22, 281.5, 335, 38.5) | (22, 281.5, 335, 38.5) | |

Pinch with pan (§6.8): at scale 1, `pinch {x0: 300, y0: 400, x1: 250, y1: 300, scale: 2}` gives
`synthesizePinchGesture` at (300, 400), then `mouseWheel` at (125, 150) with `deltaX` 50, `deltaY` 100; the content
point that was under (300, 400) ends at frame px (250, 300) (measured).

### 11.4 Text

| Input (JSON) | Messages |
|---|---|
| `"4242 4242 4242 4242\n"` | `text "4242 4242 4242 4242\n"` (the pod drops the trailing LF in an input, keeps it in a textarea) |
| `"123 Main St\r\nVictoria BC"` | `text "123 Main St\nVictoria BC"` (input: "123 Main St Victoria BC") |
| `"a\tb"` | `text "a\tb"` |
| `"x\u0000y\u0085z\u007f"` | `text "xyz"` |
| `"a\ud800b"` | `text "ab"` |
| `"\u0001"` | nothing |
| 255 × `"a"` + `"😀"` + `"b"` | `text` (255 × "a" + "😀"), `text "b"` |
| 255 × `"a"` + `"\n"` + `"b"` | `text` (255 × "a"), `text "\nb"` |
| one character typed, `"é"` | `key {c: "é"}` |

The conformance pack (§10.1) holds these vectors and many more.
