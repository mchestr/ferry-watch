# Ferry Watch HTTP API

The web UI is the only client. The service is meant to run behind an authenticating
reverse proxy (Authelia); it has no auth of its own.

This is the single-container app's API (`ferry_watch.server`). The multi-tenant ferry-web's API, including
the checkout link and the streamed viewer setting (`PATCH /api/me {checkout_viewer}`), is described in
[`multi-tenant/design-reference.md` §6.3](multi-tenant/design-reference.md) and
[`multi-tenant/streamed-viewer/DESIGN.md` §8](multi-tenant/streamed-viewer/DESIGN.md).

## Conventions

- Timestamps are epoch seconds (`number`). Times of day are `"HH:MM"` (24-hour). Dates are
  `"YYYY-MM-DD"`. The UI formats all of them.
- Every mutating request sends a JSON body with `Content-Type: application/json` (use `{}`
  when there's nothing to send). Requests from another origin are refused.
- Every mutation responds with the full new `State` (same shape as `GET /api/state`).
- Errors respond with `{"error": string, "field": string | null}`:

  | Status | Meaning |
  |---|---|
  | 400 | Validation failed; `field` names the offending key when there is one |
  | 403 | Cross-origin request |
  | 404 | No such trip |
  | 409 | Conflict, e.g. no cart to release, or the cart id doesn't match |
  | 415 | Body isn't JSON |
  | 429 | Test alert sent too recently |
  | 502 | Pushover rejected the test alert |
  | 503 | BC Ferries' terminal list is unavailable (needed to validate a route) |

## Types

```ts
type Vehicle = "standard" | "tall" | "none";
type Fare = "cheapest" | "saver" | "prepaid" | "reservation";

interface TripSettings {
  enabled: boolean;
  from: string;            // terminal code, e.g. "SWB"
  to: string;              // terminal code, e.g. "TSA"
  date: string;            // "YYYY-MM-DD"
  adults: number;          // 0-9 each; total <= 9; adults + seniors >= 1
  children: number;
  infants: number;
  seniors: number;
  vehicle: Vehicle;
  height_ft: number;       // over 7 and at most 14; only used when vehicle == "tall"
  earliest: string;        // "HH:MM"; earliest <= latest
  latest: string;          // "HH:MM"
  auto_cart: boolean;      // add an opened sailing to the cart (a guest cart unless a login is configured)
  fare: Fare;
}

interface Trip extends TripSettings {
  id: string;
  title: string;           // "Swartz Bay → Tsawwassen · Mon Oct 12"
  route: { from_place: string; from_area: string; to_place: string; to_area: string };
  past: boolean;           // the date has passed (Pacific time); not checked any more
  sailings: Sailing[];     // results of the last successful check
  checked_at: number | null;
  checking: boolean;
  error: string | null;    // last check's failure, cleared by the next success
  error_at: number | null;
  retry_at: number | null; // backing off after failures until then
  session: { opened_at: number; logged_in: boolean } | null;
  cart: CartState | null;
  booking_url: string | null;  // bcferries.com with route and date filled in
}

interface Sailing {
  depart: string;          // "HH:MM"
  arrive: string;          // "HH:MM"
  vessel: string;
  status: "available" | "sold_out" | "unavailable";
  note: string;            // BC Ferries' wording when not available
  price: string | null;    // lowest fare, e.g. "$93.45", when available
  in_window: boolean;
}

interface CartState {
  id: string;              // send back with release/dismiss
  status: "adding" | "ready" | "failed";
  depart: string;          // "HH:MM"
  since: number;
  fare: string | null;     // "Saver", ...
  price: string | null;    // "$93.45"
  error: string | null;    // when failed
  expires_at: number | null;  // a ready cart is handed back at this time (null: held until released)
}

interface ActivityEvent {
  id: number;              // increasing
  ts: number;
  kind: "info" | "alert" | "cart" | "error";
  text: string;
  trip_id: string | null;
  trip_title: string | null;  // the trip's title when the event happened
}

interface State {
  boot_id: string;         // changes when the service restarts
  seq: number;             // increases with every change within a boot; ignore older snapshots
  server_time: number;
  enabled: boolean;        // global watching switch
  interval_seconds: number;  // 15-3600
  next_check_at: number | null;
  max_trips: number;
  trip_defaults: TripSettings;  // for the "add a trip" form (date is tomorrow, Pacific time)
  trips: Trip[];
  events: ActivityEvent[];      // newest first, at most 40
  checkout_url: string | null;  // noVNC view of the browser holding carts
  login_configured: boolean;    // BC Ferries credentials present (auto-cart logs in; otherwise guest carts)
  alerts_configured: boolean;   // Pushover configured
  watcher: { heartbeat_at: number; healthy: boolean };
}

interface Terminal {
  code: string;
  label: string;           // "Sidney - Victoria (Swartz Bay)"
  place: string;           // "Swartz Bay"
  area: string;            // "Victoria"
  dests: string[];         // codes reachable with a bookable route
}
```

## Endpoints

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/state` | | `State` |
| GET | `/api/terminals` | | `{ "terminals": Terminal[] }`, sorted by label |
| PATCH | `/api/settings` | `{ enabled?, interval_seconds? }` | |
| POST | `/api/trips` | `TripSettings` (only `from`, `to`, `date` required) | 400 when `max_trips` reached |
| PATCH | `/api/trips/{id}` | partial `TripSettings` | send only changed fields |
| DELETE | `/api/trips/{id}` | `{}` | releases any held cart |
| POST | `/api/trips/{id}/cart/release` | `{ "cart_id": string }` | empties the cart, resumes the trip |
| POST | `/api/trips/{id}/cart/dismiss` | `{ "cart_id": string }` | clears a failed cart |
| POST | `/api/check` | `{}` | check every active trip now (skips back-off) |
| POST | `/api/alerts/test` | `{}` | at most once every 30 s |
| GET | `/healthz` | | 200 `ok`, or 503 when the watcher loop has stalled |

## WebSocket `/api/ws`

Server-to-client only. Messages are JSON:

- `{"type": "state", "data": State}` on connect and after every change (changes within
  ~100 ms are coalesced).
- `{"type": "ping", "server_time": number}` every 15 s when nothing else was sent, so
  clients can tell a live connection from a dead one (treat ~35 s of silence as dead).
