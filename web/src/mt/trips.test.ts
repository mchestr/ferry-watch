import { describe, expect, it } from "vitest";
import { makeSettings } from "@/test/fixtures";
import type { MyTrip } from "./schema";
import { asSailings, minutesLeft, tripBadge } from "./trips";

function myTrip(browser: Partial<MyTrip["browser"]> = {}, extra: Partial<MyTrip> = {}): MyTrip {
  return {
    trip: { ...makeSettings(), id: "3f9c0a1b2c3d4e5f", earliest: "07:00", latest: "12:00" },
    rev: 1,
    paused_reason: null,
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

describe("trip badges", () => {
  it("speaks plainly", () => {
    expect(tripBadge(myTrip()).text).toBe("Watching closely · every 2 minutes");
    expect(tripBadge(myTrip({ phase: "queued", queue_position: 2, mode: "off" })).text).toBe(
      "Waiting for a free browser (#2)",
    );
    expect(tripBadge(myTrip({ mode: "short_check", phase: "off" })).text).toBe("Checked every 6 h");
    expect(tripBadge(myTrip({}, { paused_reason: "after_checkout" })).text).toBe("Did you book it?");
    expect(tripBadge(myTrip({ consecutive_errors: 3 })).tone).toBe("danger");
    const held = myTrip({
      cart: {
        id: "c",
        status: "ready",
        depart: "09:00",
        fare: null,
        price: null,
        ready_at: null,
        unclaimed_deadline: null,
        session_deadline: null,
        hard_deadline: null,
        payment_in_progress: false,
      },
    });
    expect(tripBadge(held).text).toBe("Holding a cart");
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
