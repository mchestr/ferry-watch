import type { Sailing, Terminal, TripSettings } from "@/lib/schema";
import type { MyTrip } from "@/mt/schema";
import {
  WaitlistStateSchema,
  type BcferriesWatch,
  type BuntzenWatch,
  type ProviderInfo,
  type WaitlistState,
  type Watch,
} from "@/mt/watch-schema";
import fixture from "./fixtures/api-state.json";
import { jsonResponse, mockFetch, type RecordedRequest } from "./render";

/** 2026-10-11 12:00 PDT, a Sunday. Trips default to the next day, Monday Oct 12. */
export const NOW = new Date("2026-10-11T12:00:00-07:00");

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

// ---- Waitlist's state (docs/waitlist/design.md §6.3): the shared fixture, and watches built from trips

/**
 * The shared fixture's moment: 2027-06-30 at 7:31:50 am PDT, just after its open Buntzen watch was checked. Its
 * other Buntzen watch (for Sat Jul 3) is waiting for Friday's 7:00 release.
 */
export const FIXTURE_NOW = new Date(1_814_365_910_000);

/** A fresh copy of the shared fixture, as waitlist-web sends it. */
export function apiState(): typeof fixture {
  return structuredClone(fixture);
}

/** The shared fixture, parsed as the app keeps it. */
export function parsedState(): WaitlistState {
  return WaitlistStateSchema.parse(apiState());
}

/** BC Ferries' catalog entry (GET /api/providers). */
export const BCFERRIES_INFO: ProviderInfo = {
  id: "bcferries",
  name: "BC Ferries",
  noun: "trip",
  description: "Sailing reservations. Can hold a sailing in a cart for you to check out.",
  checker: "browser",
  capabilities: { auto_hold: true, checkout: true, alert_only: false },
  horizon_days: 30,
  offered: true,
  paused: false,
};

/** Buntzen Lake's catalog entry. */
export const BUNTZEN_INFO: ProviderInfo = {
  id: "buntzen",
  name: "Buntzen Lake",
  noun: "pass",
  description: "BC Hydro day-use parking passes, sold on Yodel. Alerts only: you book on Yodel.",
  checker: "http",
  capabilities: { auto_hold: false, checkout: false, alert_only: true },
  horizon_days: 30,
  offered: true,
  paused: false,
};

/** A BC Ferries trip as a watch: 0.7.1's entry, plus the fields every watch has. */
export function asWatch(trip: MyTrip): BcferriesWatch {
  return { ...trip, provider: "bcferries", id: trip.trip.id, enabled: trip.trip.enabled, kind: "bcferries" };
}

/** The BC Ferries trips in a state. */
export function tripsOf(state: WaitlistState): BcferriesWatch[] {
  return state.watches.filter((w): w is BcferriesWatch => w.kind === "bcferries");
}

/** The fixture's open Buntzen watch (morning open, afternoon sold out), with changes. */
export function makeBuntzenWatch(
  overrides: Partial<Omit<BuntzenWatch, "check" | "spec">> & {
    check?: Partial<BuntzenWatch["check"]>;
    spec?: Partial<BuntzenWatch["spec"]>;
  } = {},
): BuntzenWatch {
  const base = parsedState().watches[0];
  if (base?.kind !== "buntzen") throw new Error("the fixture's first watch is a Buntzen one");
  const { check, spec, ...rest } = overrides;
  return { ...base, ...rest, check: { ...base.check, ...check }, spec: { ...base.spec, ...spec } };
}

/** A whole state around these watches: the fixture's user, catalog and settings. */
export function stateWith(watches: Watch[], overrides: Partial<WaitlistState> = {}): WaitlistState {
  return { ...parsedState(), watches, ...overrides };
}

/**
 * waitlist-web as a stub for a whole-app test: /api/state answers the current state, /api/terminals the terminals,
 * and `answer` anything else (a Response as is, a state to keep and send back); otherwise the state.
 */
export function serveState(
  initial: WaitlistState,
  answer?: (request: RecordedRequest, state: WaitlistState) => Response | WaitlistState | undefined,
) {
  let state = initial;
  const mock = mockFetch((request) => {
    if (request.url === "/api/terminals") return jsonResponse({ terminals: TERMINALS });
    const out = request.url === "/api/state" ? undefined : answer?.(request, state);
    if (out instanceof Response) return out;
    if (out) state = out;
    return jsonResponse(state);
  });
  return {
    ...mock,
    current: () => state,
    set: (next: WaitlistState) => {
      state = next;
    },
  };
}

/** For a test's `x ?? fail()`: something the fixture has, but the types can't know it. */
export function fail(what = "something the fixture has"): never {
  throw new Error(`Missing ${what}`);
}
