# fw.cast1 conformance pack

The shared test cases of the streamed checkout viewer's wire protocol
([PROTOCOL.md](../../../docs/multi-tenant/streamed-viewer/PROTOCOL.md) §10.1; DESIGN §11, step 0). The agent's
Python tests and the viewer's JavaScript tests (`node --test`) load the same files, and an implementation conforms
only if it passes every case meant for it. A protocol change is a change to PROTOCOL.md **and** to this pack, in
the same pull request. `tests/test_cast_pack.py` checks that the files match the schemas below.

## Conventions

- Each file is one JSON object: `"protocol": "fw.cast1"`, one or more arrays of cases (named below), and in some
  files shared data (`context`, `rate_table`, `drop_limit`, `delays`).
- Every case is an object with:
  - `id`: unique within its file. Ids starting `round1-` are round 1's 15 valid and 22 invalid messages (its
    `proto/fuzz_protocol.py`, DESIGN §14), brought up to date where round 2 changed the protocol (the `note` says
    how);
  - `sec`: the PROTOCOL.md section the case encodes (`"6.4"`, or `"1"` for §1);
  - `note` (optional): why the case is there;
  - `only` (optional): `"agent"` or `"viewer"` when only that side runs it. Otherwise each side runs it when the
    file's table below names a check for that side.
- Messages appear as the JSON values they are on the wire (objects with `t`).
- Units are PROTOCOL's: frame px, the viewer's CSS px, milliseconds for times inside a case (`at`, `end`, `ms`,
  `wait_ms`), seconds where PROTOCOL counts seconds (`delays`, `delay`).
- Numbers compare with an absolute tolerance of 1e-6. Integer fields hold JSON integers. Header floats are exact in
  f32.
- Strings are UTF-8, but control, format, combining and non-ASCII space characters are written as `\uXXXX` so that
  nothing invisible hides in a case. A JSON parser returns the characters themselves, lone surrogates included.
- Loading: `json.loads(path.read_text("utf-8"))` in Python;
  `JSON.parse(readFileSync(new URL("../../tests/fixtures/cast1/messages.json", import.meta.url), "utf8"))` from
  `vnc-edge/tests/*.test.mjs`.

## Files

| File | Arrays | PROTOCOL | The agent runs | The viewer runs |
|---|---|---|---|---|
| `messages.json` | `cases` | §1, §2.2, §3.2, §6 | each frame's outcome (carried out, `error invalid`, close) | its encoder accepts the `ok` ones and refuses the rest |
| | `offers` | §2.2 | the gate's choice | — |
| | `viewer_offers` | §2.1 | — | `FwSession.offer()` |
| | `hello_env` | §8.1 | — | `hello` from the window |
| | `dsf` | §6.1 | the scale factor | — |
| | `rates` | §7.1 | the rate limiter and the drop limit | — |
| | `coalesce` | §3.4 | the inbound queue | — |
| | `outbound` | §5, §9 | its outbound schema accepts the `accept` ones as they are | decodes them, ignoring what it doesn't know |
| | `autocomplete` | §5.3.1, §8.5 | `input` → `ac` | `ac` → the proxy's `autocomplete` |
| `headers.json` | `cases`, `malformed` | §4 | pack | parse; refuse the malformed |
| `rects.json` | `cases` | §1, §5.3.1-5.3.3, §11.3 | `frame_rect()` | — |
| `hits.json` | `cases` | §8.3 | — | `fieldAt()`, `overlayFor()` |
| `usable.json` | `numbering` | §1, §8.3 | message numbers (`n`) | message numbers |
| | `cases` | §8.3 | — | `mapUsable()` |
| `text.json` | `cases` | §8.7, §11.4 | every output passes the value rules | `normalise()` and splitting |
| `gestures.json` | `cases` | §6.6, §8.1-8.4 | — | the gesture state machine, on a fake clock |
| `mapping.json` | `inputs`, `keys`, `date_keys`, `choose_keys` | §1, §3.5, §6.4-6.12, §11.2-11.3 | `InputMapper`, with a fake CDP | — |
| `close_codes.json` | `cases` | §3.6, §3.7 | — | `nextAction()` |

## messages.json

### `context`

The session every `cases` entry runs in unless its `state` says otherwise: `hello` and `ready` are done, `front` is
the front page, `images` are the binary messages the agent has sent (`seq`, `page`, `w`, `h`; the latest of a page
is what `move` and `wheel` are checked against), `rev` and `fields` are the field map the agent holds (the `k` of
each field, which decides the form of a `date` message's `v`), and `dialogs` are the open dialog ids.

### `cases`: viewer→agent frames

| Field | Type | Meaning |
|---|---|---|
| `msg` | any JSON value | a text frame holding this value as JSON (any serialisation: the outcome doesn't depend on it) |
| `raw` | string | a text frame of exactly this text, where the text itself matters (`7.0`, `NaN`, escapes, size) |
| `hex` | string | a binary frame of these bytes |
| `pad_to` | integer | with `raw`: append spaces until the frame is this many bytes of UTF-8 |
| `state` | `"open"`, `"hello"`, `"ready"` | when the frame arrives: as the connection's first message; after `hello`, before `ready`; or in `context` (the default) |
| `outcome` | `"ok"`, `"invalid"`, `"close"` | `ok`: it passes both steps of §6 and is carried out, or ignored or answered as its own section says (`stale`, `failed`); `invalid`: dropped and answered `error invalid` with `ref` = its `t`; `close`: the connection closes with `code` |
| `code` | integer | with `close`: 4400, or 1009 for a frame over 4096 bytes |

Each case has exactly one of `msg`, `raw` and `hex`. The viewer runs the cases that aren't `only: "agent"` and whose
frame is a JSON object (it parses `raw`): its encoder must accept an `ok` message, producing an equal object, and
refuse any other. The viewer never needs `state` or `context`: cases whose outcome depends on them are
`only: "agent"`.

### `offers`: the gate's choice (agent)

| Field | Type | Meaning |
|---|---|---|
| `offered` | array of strings | the offered subprotocols, with an otherwise admissible upgrade (Origin, cart, nonce and lock as in 0.6.4) |
| `cast_allowed` | boolean | the latest `configure`'s kill switch |
| `result` | string or integer | the selected subprotocol, or the refusal's HTTP status |

### `viewer_offers` (viewer)

| Field | Type | Meaning |
|---|---|---|
| `record` | object or null | the device record `fw:<trip>:<gen>` in localStorage (only `n` and `opened` matter) |
| `ticket` | object or null | the pending ticket in sessionStorage: `{ticket, exp}`, `exp` in epoch seconds |
| `now` | number | epoch seconds |
| `offer` | array or null | the subprotocols the viewer offers, in order; null: it doesn't connect (the landing page takes over) |

### `hello_env` (viewer) and `dsf` (agent)

`hello_env`: `inner` = `[innerWidth, innerHeight]` and `dpr` = `devicePixelRatio` → `hello`, the message the viewer
sends (a `resize` carries the same values). `dsf`: `hello` → `dsf`, the scale factor the agent applies and reports
in `ready`.

### `rate_table`, `drop_limit` and `rates` (agent)

`rate_table` is §7.1's table: `{type: {rate, burst}}`, `text` with `cp_rate` and `cp_burst` for its code points,
`*` for the overall bucket. `drop_limit` is `{drops, window_ms, code}`.

Each `rates` case is a session's rate limiter alone (the queue and `verdict()` play no part), with every bucket
full at 0 ms:

| Field | Type | Meaning |
|---|---|---|
| `steps` | array | `{at, msg, times, expect, code?}`: at `at` ms, `times` copies of `msg` arrive, and each gets `expect`: `"ok"` (admitted), `"rate"` (dropped by its bucket: `error rate`), `"invalid"` (dropped by the value rules) or `"close"` (the connection closes with `code`) |

### `coalesce` (agent)

| Field | Type | Meaning |
|---|---|---|
| `queue` | array of messages | the messages waiting, oldest first (not the one being carried out) |
| `arrives` | message | the message that arrives |
| `after` | array of messages | the waiting messages afterwards |
| `close` | integer | instead of `after`: the connection closes with this code |

### `outbound`: agent→viewer messages

| Field | Type | Meaning |
|---|---|---|
| `msg` | object | the message |
| `outcome` | `"accept"`, `"extra"`, `"unknown"` | `accept`: well formed per §5; the agent's outbound schema takes it exactly as it is, and the viewer decodes it. `extra`: a known type with fields the viewer doesn't know; it decodes the rest. `unknown`: a type the viewer doesn't know; it ignores the message |

### `autocomplete`

| Field | Type | Meaning |
|---|---|---|
| `input` | string | optional: the page's `autocomplete` attribute, as the script cut it; the agent makes `ac` from it |
| `ac` | string | the field's `ac` |
| `proxy` | string | the `autocomplete` the viewer gives its proxy for a field with this `ac` (a field of type `password` gets `off` whatever its `ac`) |

## headers.json

`cases`:

| Field | Type | Meaning |
|---|---|---|
| `fields` | object | `kind`, `slot`, `page`, `seq`, `w`, `h`, `scale`, `sx`, `sy`, `top`, `dsf`, `flags` (§4.1) |
| `header` | hex string | the 32 header bytes (reserved 0) |
| `jpeg` | hex string | the bytes after the header (a stand-in for a JPEG; `""` for kind 2) |
| `first` | boolean | flag bit 0 |

The agent packs `fields` into `header`. The viewer parses `header` + `jpeg` into `fields`, `first` and the JPEG
bytes. `malformed` cases hold a whole binary message in `hex` that the viewer must refuse (§4.2).

## rects.json

| Field | Type | Meaning |
|---|---|---|
| `s` | number | the main frame's `visualViewport.scale` |
| `ox`, `oy` | number | its `offsetLeft`, `offsetTop` |
| `origins` | array of `[a, b]` | the content-box origin of each owner iframe in its parent's client coordinates, innermost first; `[]` for a field of the main frame |
| `clip` | `[x, y, w, h]` | the field's frame's clip in its own client coordinates: the main frame's visual viewport (`[ox, oy, width, height]`), or an iframe's own viewport (`[0, 0, innerWidth, innerHeight]`) |
| `client` | `[x, y, w, h]` | the field's `getBoundingClientRect()` in its own frame |
| `r` | `[x, y, w, h]` | in frame px (§5.3.3) |
| `c` | `[x, y, w, h]` or null | `client` ∩ `clip`, in frame px; null when nothing of the field is inside the clip (it isn't reported) |

The values are exact: no rounding.

## hits.json

| Field | Type | Meaning |
|---|---|---|
| `map` | message | a `fields` message |
| `oks` | array | the `fields_ok` messages received after it |
| `image` | object | the drawn image's header fields: `page`, `seq`, `w`, `h`, `scale`, `sx`, `sy` |
| `last_input` | integer | the number of the viewer's latest input message (§8.3) |
| `shifted` | boolean | a prediction shift or a local zoom is drawn |
| `taps` | array | `{x, y, field, note?}`: a point in frame px of the image, and the id of the field it hits or null |
| `overlays` | array | `{field, overlay}` for every field of the map: the native overlay shown for it (`"select"`, `"date"` or `"time"`) or null |

The map's usability (§8.3) is part of the answer: an unusable map hits nothing and shows no overlay.

## usable.json

`numbering`:

| Field | Type | Meaning |
|---|---|---|
| `sent` | array of strings | the `t` of each message the viewer sends, from `hello` |
| `numbers` | array | each one's number (§1); null for `ack` |
| `last_input` | integer | the number of the latest of `tap`, `wheel`, `pinch`, `key`, `text`, `compose`, `choose`, `date`, `dialog`, `page` and `resize`; 0 if none |

The viewer numbers what it sends this way; the agent's `n`, once it has carried out all of them, is the last
number.

`cases`: `map`, `oks`, `image`, `last_input` and `shifted` as in hits.json, and `usable`, a boolean.

## text.json

| Field | Type | Meaning |
|---|---|---|
| `as` | `"text"`, `"compose"`, `"dialog"` | what the input is: typed or pasted text, a composition, a prompt answer |
| `via` | `"paste"`, `"insertText"` | for `text` only: a paste (`insertFromPaste`, `insertReplacementText`, `insertFromDrop`, the menu's Paste) or a keyboard's `insertText` |
| `input` | string | the raw string |
| `messages` | array | for `text` and `compose`: the messages the viewer sends, in order |
| `s` | string | for `dialog`: the `s` of the `dialog` answer |

The agent checks that every message passes its value rules (§6), and so does a `dialog` answer
`{"t": "dialog", "id": 1, "ok": true, "s": s}`.

## gestures.json

| Field | Type | Meaning |
|---|---|---|
| `viewport` | `[w, h]` | the viewer's `innerWidth`, `innerHeight` |
| `image` | object | the image drawn at the start: `page`, `seq`, `w`, `h`, `scale`, `sx`, `sy` |
| `events` | array | in time order, each with `at` (ms) and `type`, below |
| `end` | integer | the time the clock runs to after the last event |
| `expect` | object | below |

Events:
- `touchstart`, `touchmove`, `touchend`: `touches`, the touches down after the event as `[x, y]` in the viewer's
  CSS px, each finger at the same index throughout (a `touchend` lists the fingers still down);
- `pointermove`, `pointerdown`, `pointerup`: `x`, `y`; a mouse (`pointerType` `"mouse"`), primary button;
- `wheel`: `x`, `y`, `dx`, `dy`, `mode` (`deltaMode`: 0 pixels, 1 lines);
- `image`: `image`, a new image drawn (fields as `image` above; acks play no part here).

Before each event the harness moves a fake clock to its `at`, running the timers and animation frames due on the way
(frames at most 16 ms apart); after the last, it moves the clock to `end`.

`expect` has the keys `taps`, `pinches`, `wheels` and `moves`; no message of a type without its key may be sent
(`{}`: nothing at all).
- `taps`, `pinches`: the exact messages, in order (a missing `ms` counts as 0).
- `wheels`: every wheel has this `page`, `x` and `y` (the gesture's start point, frame px); consecutive wheels are
  at least 33 ms apart; `dx` is the exact sum of their `dx`; `dy` the exact sum of their `dy`, except after a flick,
  where `dy_more_than` replaces it (the sum has its sign and a larger magnitude: the coast adds to the finger's
  travel) and `coast_after` says that at least one wheel is sent after that time; `none_after` (optional): no wheel
  is sent after that time.
- `moves`: every move has this `page`; consecutive moves are at least 33 ms apart; the last one sent is at `last`,
  `[x, y]`.

The momentum constants are tuned on devices (DESIGN §7.4): the flick cases hold for any decay that ends a coast
within 3.5 s.

## mapping.json

`inputs`:

| Field | Type | Meaning |
|---|---|---|
| `front` | integer | the front page |
| `images` | array | the binary messages the agent remembers (§4.2), oldest first: `seq`, `page`, `w`, `h`, `scale`, `sx`, `sy` |
| `live` | object | what `Page.getLayoutMetrics`' `cssVisualViewport` reports when the message is carried out: `pageX`, `pageY`, `scale` |
| `after` | object | `pinch` only: `{scale}`, the scale reported after the pinch gesture |
| `msg` | message | the message carried out |
| `calls` | array | the `Input` calls made, in order: `{method, params}` |
| `wait_ms` | integer | `tap` only: the pause between `mousePressed` and `mouseReleased` |
| `error` | string | the `error` code answered instead (`"stale"`); `calls` is then empty |

Every listed param must be there with this value; an implementation may add others, except that a missing
`modifiers` counts as 0 and a present one must match.

`keys`: `msgs`, key messages carried out in order from a fresh session; `teardown` (optional), the session then
tears down (§3.5); `events`, the params of each `Input.dispatchKeyEvent` in order, matched as `calls` params are.

`date_keys`: `k` (`"date"` or `"time"`) and `v` → `chars`, the characters pressed in order as whole key presses
(§6.12, en-US).

`choose_keys`: `opts` (`[label, disabled]` pairs) and `i` → `keys`, the named keys pressed once the select has focus
(§6.11 step 5), or `error` (`"failed"`, step 1) and no key.

## close_codes.json

`delays` is §3.7's backoff, `[0.5, 1, 2, 5, 10]` seconds, the last repeating. Each case is one page's successive
connections:

| Field | Type | Meaning |
|---|---|---|
| `steps` | array | `{opened, ready, code, deadline_passed?, action, prominent}` for each connection, in order |

- `opened`, `ready`: whether the connection reached `open` and `ready`; `code`: how it closed (1006 for an upgrade
  that failed);
- `deadline_passed` (default false): the device record's `hard_deadline` had passed when it closed;
- `action`: `{"do": "reconnect", "delay": seconds}`; `{"do": "stop", "show"?}` with `show` naming §3.6's screen
  (`"ended"`: "This checkout has ended"; `"elsewhere"`: "Opened elsewhere on this device" with "Show it here";
  `"revoked"`: checkout access withdrawn; absent where §3.6 names none); `{"do": "classic"}`: switch to the classic
  viewer now (§8.10); `{"do": "gone"}`: go to `/gone.html`;
- `prominent`: whether the classic viewer option is shown prominently after this close.

## Not in the pack

Rules that only one side implements are tested on that side (DESIGN §10). The agent's tests cover the `hello`
timeout, session pacing, the ack window, repeats and image modes (§4.3), `verdict()` (§7.2), the field-map scripts
and schedule (§5.3.2, §5.3.4), `resize` debouncing and its 16 sizes (§6.2), one `error` a second (§5.7), the page
checks of `choose` and `date` (§6.11-6.12), teardown (§3.5), dialogs (§5.5) and input that opens one (§3.4). The
viewer's tests cover drawing and acks (§4.2, §8.1), the proxy's other attributes (§8.5), the sheets and dialog
headings (§8.6, §8.8), storage (§8.9) and the escape hatch (§8.10). No case exercises the overall rate bucket (§7.1) alone: the type buckets'
bursts add up to 203 and their rates to 298 a second, so it can't be reached while every type stays within its own.
