import { describe, expect, it } from "vitest";
import { makeCart, makeSailing, makeTrip } from "@/test/fixtures";
import {
  bookableSummary,
  heldCart,
  isActive,
  paxLabel,
  removeConsequence,
  soldOutSummary,
  tripStatus,
  windowLabel,
} from "./trip";

const open = makeSailing({ status: "available", price: "$93.45" });
const soldOut = makeSailing({ depart: "09:00" });
const unavailable = makeSailing({ depart: "11:00", status: "unavailable", note: "Vehicle too tall" });

describe("tripStatus", () => {
  it("puts a failed check first, with when the stale results are from", () => {
    const trip = makeTrip({
      error: "Queue-it timed out",
      error_at: 2_000,
      checked_at: 1_000,
      retry_at: 2_060,
      sailings: [open],
      enabled: false,
      past: true,
      cart: makeCart(),
    });
    expect(tripStatus(trip, false)).toEqual({
      kind: "error",
      message: "Queue-it timed out",
      resultsFrom: 1_000,
      retryAt: 2_060,
    });
  });

  it("doesn't claim old results when there are none", () => {
    expect(tripStatus(makeTrip({ error: "x", sailings: [], checked_at: null }), true)).toMatchObject({
      resultsFrom: null,
    });
  });

  it("then a passed date", () => {
    expect(tripStatus(makeTrip({ past: true, enabled: false, cart: makeCart() }), false).kind).toBe("past");
  });

  it("then paused: the trip, then everything", () => {
    expect(tripStatus(makeTrip({ enabled: false, cart: makeCart() }), false)).toEqual({
      kind: "paused",
      scope: "trip",
    });
    expect(tripStatus(makeTrip({ cart: makeCart() }), false)).toEqual({ kind: "paused", scope: "all" });
  });

  it("then a held cart, which beats bookable sailings (no 'Book' button while one is held)", () => {
    const cart = makeCart({ status: "adding" });
    expect(tripStatus(makeTrip({ cart, sailings: [open] }), true)).toEqual({ kind: "held", cart });
  });

  it("ignores a failed cart: it holds nothing", () => {
    expect(tripStatus(makeTrip({ cart: makeCart({ status: "failed" }), sailings: [open] }), true).kind).toBe(
      "bookable",
    );
  });

  it("then the first check", () => {
    expect(tripStatus(makeTrip({ checked_at: null, checking: true }), true).kind).toBe("checking");
    expect(tripStatus(makeTrip({ checked_at: null }), true).kind).toBe("waiting");
  });

  it("then bookable sailings in the window, then sold out", () => {
    const outside = makeSailing({ status: "available", in_window: false });
    expect(tripStatus(makeTrip({ sailings: [soldOut, open, outside] }), true)).toEqual({
      kind: "bookable",
      sailings: [open],
    });
    expect(tripStatus(makeTrip({ sailings: [soldOut, outside] }), true)).toEqual({
      kind: "sold_out",
      inWindow: [soldOut],
      total: 2,
    });
  });
});

describe("summaries", () => {
  it("never says '0 sailings in your window, all sold out'", () => {
    expect(soldOutSummary([], 0)).toBe("BC Ferries shows no sailings on this date.");
    expect(soldOutSummary([], 1)).toBe("The only sailing that day is outside your departure window.");
    expect(soldOutSummary([], 8)).toBe("None of the 8 sailings that day are in your departure window.");
    expect(soldOutSummary([soldOut], 8)).toBe("The one sailing in your window is sold out.");
    expect(soldOutSummary([unavailable], 8)).toBe("The one sailing in your window is unavailable.");
    expect(soldOutSummary([soldOut, soldOut], 8)).toBe("All 2 sailings in your window are sold out.");
    expect(soldOutSummary([unavailable, unavailable], 8)).toBe("All 2 sailings in your window are unavailable.");
    expect(soldOutSummary([soldOut, unavailable, soldOut], 8)).toBe(
      "3 sailings in your window: 2 sold out, 1 unavailable.",
    );
  });

  it("lists bookable sailings in 12-hour time", () => {
    const later = makeSailing({ depart: "13:15", status: "available", price: null });
    expect(bookableSummary([open, later])).toBe("7:00 am ($93.45), 1:15 pm");
    expect(bookableSummary([open, later, open, later, open])).toBe(
      "7:00 am ($93.45), 1:15 pm, 7:00 am ($93.45) +2 more",
    );
  });

  it("describes passengers and the window", () => {
    expect(paxLabel({ adults: 2, children: 1, infants: 0, seniors: 1 })).toBe("2 adults, 1 child, 1 senior");
    expect(paxLabel({ adults: 0, children: 0, infants: 0, seniors: 0 })).toBe("No passengers");
    expect(windowLabel({ earliest: "00:00", latest: "23:59" })).toBe("Any time");
    expect(windowLabel({ earliest: "07:00", latest: "21:30" })).toBe("7:00 am – 9:30 pm");
  });
});

describe("carts", () => {
  it("names the held sailing when removing would release it", () => {
    expect(removeConsequence({ cart: makeCart({ status: "ready", depart: "07:00" }) })).toBe(
      "Removing it releases the 7:00 am sailing in your cart.",
    );
    expect(removeConsequence({ cart: makeCart({ status: "adding", depart: "15:30" }) })).toBe(
      "Removing it stops adding the 3:30 pm sailing to your cart.",
    );
    expect(removeConsequence({ cart: makeCart({ status: "failed" }) })).toBe("You'll stop getting alerts for it.");
    expect(removeConsequence({ cart: null })).toBe("You'll stop getting alerts for it.");
  });

  it("doesn't check a trip that's holding a cart, past, or paused", () => {
    expect(heldCart({ cart: makeCart({ status: "failed" }) })).toBeNull();
    expect(isActive(makeTrip(), true)).toBe(true);
    expect(isActive(makeTrip(), false)).toBe(false);
    expect(isActive(makeTrip({ cart: makeCart() }), true)).toBe(false);
    expect(isActive(makeTrip({ past: true }), true)).toBe(false);
    expect(isActive(makeTrip({ enabled: false }), true)).toBe(false);
  });
});
