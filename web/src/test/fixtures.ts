import type { CartState, Sailing, State, Terminal, Trip, TripSettings } from "@/lib/schema";

/** 2026-10-11 12:00 PDT, a Sunday. Trips default to the next day, Monday Oct 12. */
export const NOW = new Date("2026-10-11T12:00:00-07:00");
export const NOW_S = NOW.getTime() / 1000;

export function makeSettings(overrides: Partial<TripSettings> = {}): TripSettings {
  return {
    enabled: true,
    from: "SWB",
    to: "TSA",
    date: "2026-10-12",
    adults: 1,
    children: 0,
    infants: 0,
    seniors: 0,
    vehicle: "standard",
    height_ft: 8,
    earliest: "00:00",
    latest: "23:59",
    auto_cart: true,
    fare: "cheapest",
    ...overrides,
  };
}

export function makeSailing(overrides: Partial<Sailing> = {}): Sailing {
  return {
    depart: "07:00",
    arrive: "08:35",
    vessel: "Spirit of British Columbia",
    status: "sold_out",
    note: "",
    price: null,
    in_window: true,
    ...overrides,
  };
}

export function makeCart(overrides: Partial<CartState> = {}): CartState {
  return {
    id: "cart-1",
    status: "ready",
    depart: "07:00",
    since: NOW_S - 120,
    fare: "Saver",
    price: "$93.45",
    error: null,
    ...overrides,
  };
}

export function makeTrip(overrides: Partial<Trip> = {}): Trip {
  return {
    ...makeSettings(),
    id: "t1",
    title: "Swartz Bay → Tsawwassen · Mon Oct 12",
    route: { from_place: "Swartz Bay", from_area: "Victoria", to_place: "Tsawwassen", to_area: "Vancouver" },
    past: false,
    sailings: [],
    checked_at: NOW_S - 30,
    checking: false,
    error: null,
    error_at: null,
    retry_at: null,
    session: { opened_at: NOW_S - 600, logged_in: true },
    cart: null,
    booking_url: "https://www.bcferries.com/",
    ...overrides,
  };
}

export function makeState(overrides: Partial<State> = {}): State {
  return {
    boot_id: "boot-a",
    seq: 1,
    server_time: NOW_S,
    enabled: true,
    interval_seconds: 30,
    next_check_at: NOW_S + 20,
    max_trips: 6,
    trip_defaults: makeSettings(),
    trips: [makeTrip()],
    events: [],
    checkout_url: "https://ferry.example/vnc/vnc.html",
    login_configured: true,
    alerts_configured: true,
    watcher: { heartbeat_at: NOW_S, healthy: true },
    ...overrides,
  };
}

/** Sorted by label, as GET /api/terminals returns them. */
export const TERMINALS: Terminal[] = [
  { code: "DEP", label: "Nanaimo (Departure Bay)", place: "Departure Bay", area: "Nanaimo", dests: ["HSB"] },
  { code: "DUK", label: "Nanaimo (Duke Point)", place: "Duke Point", area: "Nanaimo", dests: ["TSA"] },
  { code: "LNG", label: "Sunshine Coast (Langdale)", place: "Langdale", area: "Sunshine Coast", dests: ["HSB"] },
  // "BOW" isn't in the list, so it can't be chosen.
  {
    code: "HSB",
    label: "Vancouver (Horseshoe Bay)",
    place: "Horseshoe Bay",
    area: "Vancouver",
    dests: ["LNG", "BOW", "DEP"],
  },
  { code: "TSA", label: "Vancouver (Tsawwassen)", place: "Tsawwassen", area: "Vancouver", dests: ["SWB", "DUK"] },
  { code: "SWB", label: "Victoria (Swartz Bay)", place: "Swartz Bay", area: "Victoria", dests: ["TSA"] },
];
