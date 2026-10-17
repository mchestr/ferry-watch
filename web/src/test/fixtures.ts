import type { Sailing, Terminal, TripSettings } from "@/lib/schema";

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
