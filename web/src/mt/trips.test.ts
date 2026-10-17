import { describe, expect, it } from "vitest";
import { makeSettings } from "@/test/fixtures";
import type { MyTrip } from "./schema";
import { asSailings, minutesLeft, nextCheck, notHeldReason, routeTitle, tripBadge, tripTitle, unheld } from "./trips";

function myTrip(browser: Partial<MyTrip["browser"]> = {}, extra: Partial<MyTrip> = {}): MyTrip {
  return {
    trip: { ...makeSettings(), id: "3f9c0a1b2c3d4e5f", earliest: "07:00", latest: "12:00" },
    rev: 1,
    paused_reason: null,
    quiet: false,
    hold: { reason: null, can_request: true, holdable: [], requested_at: null, requested_until: null },
    booking_url: null,
    browser: {
      phase: "watching",
      mode: "warm",
      queue_position: null,
      next_check_at: null,
      last_check_at: null,
      checked_at: null,
      next_due: null,
      effective_interval: 120,
      heartbeat_at: null,
      sailings: [],
      error_class: null,
      consecutive_errors: 0,
      cart: null,
      viewer_connected: false,
      ...browser,
    },
    ...extra,
  };
}

const open = (depart: string, status: "available" | "sold_out" = "available") => ({
  depart,
  arrive: "10:35",
  vessel: "Queen of Oak Bay",
  status,
  fares: [],
});

describe("route titles", () => {
  // As GET /api/terminals sends them: a label is BC Ferries' "City - Name (Place)", for the route form's lists.
  const terminals = [
    { code: "SWB", label: "Sidney - Victoria (Swartz Bay)", place: "Swartz Bay", area: "Victoria", dests: ["TSA"] },
    { code: "TSA", label: "Delta - Vancouver (Tsawwassen)", place: "Tsawwassen", area: "Vancouver", dests: ["SWB"] },
  ];
  const trip = myTrip(
    {},
    { trip: { ...makeSettings({ from: "SWB", to: "TSA", date: "2026-10-23" }), id: "3f9c0a1b2c3d4e5f" } },
  );

  it("name the terminals themselves, as alerts do", () => {
    expect(routeTitle(trip, terminals)).toBe("Swartz Bay → Tsawwassen");
    expect(tripTitle(trip, terminals)).toBe("Swartz Bay → Tsawwassen, Friday, October 23");
  });

  it("fall back to the codes until the list has loaded", () => {
    expect(routeTitle(trip, undefined)).toBe("SWB → TSA");
    expect(routeTitle(trip, terminals.slice(0, 1))).toBe("Swartz Bay → TSA");
  });
});

describe("trip badges", () => {
  it("say what the trip is doing in a word or two", () => {
    expect(tripBadge(myTrip())).toEqual({ text: "Checking", tone: "success" }); // the warm browser
    expect(tripBadge(myTrip({ mode: "short_check", phase: "off", next_check_at: 1 }))).toEqual({
      text: "Scheduled",
      tone: "soft",
    });
    expect(tripBadge(myTrip({ phase: "queued", queue_position: 2, mode: "off" })).text).toBe("Scheduled");
    expect(tripBadge(myTrip({ phase: "off", mode: "off" })).text).toBe("Scheduled");
    expect(tripBadge(myTrip({}, { quiet: true }))).toEqual({ text: "Quiet hours", tone: "soft" });
    expect(tripBadge(myTrip({ sailings: [open("09:00")] }))).toEqual({ text: "Bookable", tone: "warning" });
    expect(tripBadge(myTrip({ consecutive_errors: 3 }))).toEqual({ text: "Problem", tone: "danger" });
    expect(tripBadge(myTrip({ cart: cart("adding") }))).toEqual({ text: "Holding", tone: "success" });
    expect(tripBadge(myTrip({ cart: cart("ready") }))).toEqual({ text: "Held", tone: "success" });
  });

  it("say Paused for a paused trip, and while it asks whether you booked it", () => {
    expect(tripBadge(myTrip({}, { paused_reason: "after_checkout" }))).toEqual({ text: "Paused", tone: "warning" });
    const held = myTrip({ cart: cart("ready") });
    expect(tripBadge({ ...held, trip: { ...held.trip, enabled: false } })).toEqual({ text: "Paused", tone: "outline" });
    const trip = myTrip();
    expect(tripBadge({ ...trip, hold: { ...trip.hold, reason: "paused" } })).toEqual({
      text: "Paused",
      tone: "outline",
    });
  });

  it("leave a scheduled trip's next check for the card to say", () => {
    expect(nextCheck(myTrip({ mode: "short_check", phase: "off", next_check_at: 1000 }))).toBe(1000);
    expect(nextCheck(myTrip({ next_check_at: 1000 }))).toBeNull(); // checking now
    expect(nextCheck(myTrip({ mode: "off", phase: "off", next_check_at: 1000 }, { quiet: true }))).toBeNull();
  });

  it("put what matters more first", () => {
    expect(tripBadge(myTrip({ cart: cart("ready"), consecutive_errors: 3 })).text).toBe("Held");
    // Trouble checking says more than old results do.
    expect(tripBadge(myTrip({ sailings: [open("09:00")], consecutive_errors: 3 })).text).toBe("Problem");
    // During quiet hours, a sailing the last check found is still worth booking.
    expect(tripBadge(myTrip({ sailings: [open("09:00")] }, { quiet: true })).text).toBe("Bookable");
    expect(tripBadge(myTrip({ mode: "warm" }, { quiet: true })).text).toBe("Quiet hours");
  });
});

describe("sailings", () => {
  it("shows the cheapest bookable fare and the window", () => {
    const trip = myTrip({
      sailings: [
        {
          depart: "09:00",
          arrive: "10:35",
          vessel: "Spirit",
          status: "available",
          fares: [
            { name: "Saver", price: "$93.45", selectable: true },
            { name: "Prepaid", price: "$99.00", selectable: true },
          ],
        },
        { depart: "13:00", arrive: "14:35", vessel: "Spirit", status: "sold_out", fares: [] },
      ],
    });
    const [first, second] = asSailings(trip);
    expect(first).toMatchObject({ price: "$93.45", in_window: true });
    expect(second).toMatchObject({ price: null, in_window: false });
  });
});

describe("countdowns", () => {
  it("never go negative", () => {
    expect(minutesLeft(1000, 940_000)).toBe(1);
    expect(minutesLeft(1000, 2_000_000)).toBe(0);
    expect(minutesLeft(null)).toBeNull();
  });
});

describe("bookable, not held", () => {
  it("counts only open sailings in the trip's times, with nothing holding them", () => {
    const trip = myTrip({ sailings: [open("06:00"), open("09:00"), open("11:00", "sold_out")] });
    expect(unheld(trip)).toEqual(["09:00"]);
    expect(tripBadge(trip)).toEqual({ text: "Bookable", tone: "warning" });
    const adding = myTrip({ sailings: [open("09:00")], cart: { ...cart("adding") } });
    expect(unheld(adding)).toEqual([]);
    expect(tripBadge(adding).text).toBe("Holding");
    expect(unheld(myTrip({ sailings: [open("09:00")] }, { paused_reason: "after_checkout" }))).toEqual([]);
    const pausedAccount = myTrip({ sailings: [open("09:00")] });
    expect(unheld({ ...pausedAccount, hold: { ...pausedAccount.hold, reason: "paused" } })).toEqual([]);
  });

  it("says why", () => {
    const trip = myTrip({ sailings: [open("09:00")] });
    expect(notHeldReason({ ...trip, hold: { ...trip.hold, reason: "auto_cart_off" } })).toBe(
      "Auto-hold is off for this trip.",
    );
    expect(notHeldReason({ ...trip, hold: { ...trip.hold, reason: "unknown" } })).toBe("We couldn't hold it.");
    expect(notHeldReason(myTrip({ sailings: [open("09:00")], cart: cart("failed") }))).toBe(
      "We tried to hold it, but BC Ferries didn't let us.",
    );
  });
});

function cart(status: "adding" | "ready" | "failed") {
  return {
    id: "c1",
    status,
    depart: "09:00",
    fare: null,
    price: null,
    ready_at: null,
    unclaimed_deadline: null,
    session_deadline: null,
    hard_deadline: null,
    payment_in_progress: false,
  } as const;
}
