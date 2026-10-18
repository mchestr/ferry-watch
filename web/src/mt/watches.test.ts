import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { uiFor } from "@/providers/registry";
import {
  asWatch,
  BCFERRIES_INFO,
  BUNTZEN_INFO,
  FIXTURE_NOW,
  makeBuntzenWatch,
  parsedState,
  stateWith,
  tripsOf,
  fail,
} from "@/test/fixtures";
import { capacityFor, nextHint, pickable, rememberProvider, sections, whatItIs } from "./watches";
import type { ProviderUi } from "@/providers/types";
import type { MyTrip } from "./schema";
import type { Watch } from "./watch-schema";

const NOW = FIXTURE_NOW.getTime();
const fixtureTrip = (): MyTrip => {
  const trip = tripsOf(parsedState())[0];
  if (!trip) throw new Error("the fixture has a BC Ferries trip");
  return trip;
};
/** The fixture's trip (Sat Jul 3, checked, nothing bookable) under another id, with changes. */
const trip = (id: string, changes: Partial<MyTrip> = {}, settings: Partial<MyTrip["trip"]> = {}) => {
  const base = fixtureTrip();
  return asWatch({ ...base, ...changes, trip: { ...base.trip, ...settings, id } });
};
const CART = {
  id: "c1",
  depart: "09:00",
  fare: null,
  price: null,
  ready_at: null,
  unclaimed_deadline: null,
  session_deadline: null,
  hard_deadline: null,
  payment_in_progress: false,
};
const ids = (watches: Watch[]) => watches.map((w) => w.id);
const known = (id: string) => id === "bcferries" || id === "buntzen";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the dashboard's sections", () => {
  it("put the shared fixture's watches where they belong", () => {
    const groups = sections(parsedState().watches, NOW, uiFor);
    expect(ids(groups.now)).toEqual(["0f3c9a1b2d4e5f60"]); // a morning pass is open
    expect(ids(groups.watching)).toEqual(["7a7a7a7a7a7a7a7a", "a1b2c3d4e5f60718", "c0ffee00c0ffee00"]);
    expect(groups.paused).toEqual([]);
    expect(groups.ended).toEqual([]);
  });

  it("sort each section by date, then in the server's order, with unknown watches last", () => {
    const unknown = parsedState().watches[3];
    if (!unknown) throw new Error("the fixture has an unknown watch");
    const watches = [
      unknown,
      makeBuntzenWatch({ id: "0000000000000003", spec: { date: "2027-07-03" }, options: [] }),
      trip("0000000000000001", {}, { date: "2027-07-02" }),
      makeBuntzenWatch({ id: "0000000000000002", spec: { date: "2027-07-02" }, options: [] }),
    ];
    expect(ids(sections(watches, NOW, uiFor).watching)).toEqual([
      "0000000000000001",
      "0000000000000002",
      "0000000000000003",
      "c0ffee00c0ffee00",
    ]);
  });

  it("file BC Ferries trips by cart, question, bookable sailings, pause and date", () => {
    const cart = { ...CART, status: "ready" as const };
    const held = trip("0000000000000001", { browser: { ...fixtureTrip().browser, cart } });
    const asked = trip("0000000000000002", { paused_reason: "after_checkout" });
    const bookable = trip("0000000000000003", {
      browser: {
        ...fixtureTrip().browser,
        sailings: [{ depart: "09:00", arrive: "10:35", vessel: "Queen of Oak Bay", status: "available", fares: [] }],
      },
    });
    const paused = trip("0000000000000004", {}, { enabled: false });
    const past = trip("0000000000000005", {}, { date: "2027-06-29" });
    const groups = sections([held, asked, bookable, paused, past, trip("0000000000000006")], NOW, uiFor);
    expect(ids(groups.now)).toEqual(["0000000000000001", "0000000000000002", "0000000000000003"]);
    expect(ids(groups.paused)).toEqual(["0000000000000004"]);
    expect(ids(groups.ended)).toEqual(["0000000000000005"]);
    expect(ids(groups.watching)).toEqual(["0000000000000006"]);
  });

  it("file Buntzen watches by open passes, pause and the end of their window", () => {
    const open = makeBuntzenWatch({ id: "0000000000000001" });
    const maybe = makeBuntzenWatch({
      id: "0000000000000002",
      options: [{ ...(open.options[0] ?? fail()), status: "maybe", detail: "Waitlist only" }],
    });
    const unwantedOpen = makeBuntzenWatch({
      id: "0000000000000003",
      options: [{ ...(open.options[0] ?? fail()), wanted: false }],
    });
    const paused = makeBuntzenWatch({
      id: "0000000000000004",
      enabled: false,
      check: { state: "paused" },
      options: [],
    });
    const over = makeBuntzenWatch({ id: "0000000000000005", check: { state: "over" }, options: [] });
    const groups = sections([open, maybe, unwantedOpen, paused, over], NOW, uiFor);
    expect(ids(groups.now)).toEqual(["0000000000000001", "0000000000000002"]);
    expect(ids(groups.watching)).toEqual(["0000000000000003"]);
    expect(ids(groups.paused)).toEqual(["0000000000000004"]);
    expect(ids(groups.ended)).toEqual(["0000000000000005"]);
  });

  it("don't call a pass open once its stop time has passed, whatever the last check saw", () => {
    const open = makeBuntzenWatch(); // the morning pass for Thu Jul 1, which stops counting at noon
    expect(ids(sections([open], new Date("2027-07-01T11:59:00-07:00").getTime(), uiFor).now)).toEqual([open.id]);
    expect(sections([open], new Date("2027-07-01T12:00:00-07:00").getTime(), uiFor).now).toEqual([]);
  });
});

describe("the Next hint", () => {
  it("names the soonest release among the watches being watched", () => {
    const groups = sections(parsedState().watches, NOW, uiFor);
    expect(nextHint(groups.watching, NOW, uiFor)).toBe(
      "Next: Buntzen Lake passes for Sat Jul 3 go on sale Fri at 7:00 am",
    );
  });

  it("says today or tomorrow for a release that close", () => {
    const tomorrow = makeBuntzenWatch({
      spec: { date: "2027-07-02" },
      check: { state: "waiting_release", opens_at: new Date("2027-07-01T07:00:00-07:00").getTime() / 1000 },
    });
    expect(nextHint([tomorrow], NOW, uiFor)).toBe(
      "Next: Buntzen Lake passes for Fri Jul 2 go on sale tomorrow at 7:00 am",
    );
  });

  it("passes over a watch whose provider throws, which then sits in Watching undated", () => {
    const state = parsedState();
    const [odd, ...rest] = state.watches;
    if (!odd) fail();
    const broken = (): never => {
      throw new RangeError("Invalid time value");
    };
    const lookup = (watch: Watch): ProviderUi | null => {
      const ui = uiFor(watch);
      return ui && watch.id === odd.id ? { ...ui, date: broken, group: broken, next: broken } : ui;
    };
    const groups = sections(state.watches, NOW, lookup);
    const unknown = state.watches[3]?.id ?? fail(); // the fixture's bcparks watch, also undated
    // After every dated watch, with the undated one, in the server's order.
    expect(ids(groups.watching)).toEqual([
      ...ids(sections(rest, NOW, uiFor).watching).filter((id) => id !== unknown),
      odd.id,
      unknown,
    ]);
    expect(nextHint(groups.watching, NOW, lookup)).toBe(
      "Next: Buntzen Lake passes for Sat Jul 3 go on sale Fri at 7:00 am",
    );
  });

  it("is nothing when nothing is scheduled", () => {
    expect(nextHint([makeBuntzenWatch()], NOW, uiFor)).toBeNull();
    expect(nextHint([], NOW, uiFor)).toBeNull();
  });
});

describe("which providers a new watch can be on", () => {
  it("lists those offered and known to this build, last used first", () => {
    const state = parsedState();
    expect(pickable(state, known).map((p) => p.id)).toEqual(["bcferries", "buntzen"]);
    rememberProvider("buntzen");
    expect(pickable(state, known).map((p) => p.id)).toEqual(["buntzen", "bcferries"]);
    expect(pickable(state, (id) => id === "bcferries").map((p) => p.id)).toEqual(["bcferries"]);
  });

  it("leaves out a provider that isn't offered, and one this build doesn't know", () => {
    const state = stateWith([], {
      providers: [
        { ...BUNTZEN_INFO, offered: false },
        { ...BUNTZEN_INFO, id: "bcparks", name: "BC Parks" },
      ],
    });
    expect(pickable(state, known)).toEqual([]);
  });

  it("says when one is at its limit, and why", () => {
    const state = parsedState();
    expect(capacityFor(state, "buntzen")).toEqual({ canAdd: true, reason: null });
    state.capacity.providers.buntzen = {
      can_add: false,
      reason: "You can have up to 6 alert-only watches",
      limit: 6,
      count: 6,
    };
    expect(capacityFor(state, "buntzen")).toEqual({ canAdd: false, reason: "You can have up to 6 alert-only watches" });
  });
});

describe("what a provider is, as its picker tile says it", () => {
  it("is its description's first sentence: the tagline chip says the rest", () => {
    expect(whatItIs(BCFERRIES_INFO.description)).toBe("Sailing reservations.");
    expect(whatItIs(BUNTZEN_INFO.description)).toBe("BC Hydro day-use parking passes, sold on Yodel.");
  });

  it("is the whole description when it is one sentence, and never ends inside an abbreviation", () => {
    expect(whatItIs("Campsite reservations")).toBe("Campsite reservations");
    expect(whatItIs("Campsites on B.C. Parks. Alerts only.")).toBe("Campsites on B.C. Parks.");
    expect(whatItIs("Passes (day use). Alerts only.")).toBe("Passes (day use).");
  });
});
