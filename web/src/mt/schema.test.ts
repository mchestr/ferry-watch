import { describe, expect, it } from "vitest";
import { makeSettings } from "@/test/fixtures";
import { CheckoutSchema, DEFAULT_QUIET_HOURS, MyStateSchema } from "./schema";

/** /api/state as ferry-web 0.6.2 sent it: one global quiet hours, me.paused, no settings. */
function older(): Record<string, unknown> {
  return {
    me: { id: "u1", username: "ann", display: null, is_admin: false, paused: false },
    limits: {},
    usage: {},
    trips: [
      {
        trip: { ...makeSettings(), id: "3f9c0a1b2c3d4e5f" },
        rev: 1,
        paused_reason: null,
        hold: { reason: null, can_request: true, holdable: [], requested_at: null, requested_until: null },
        browser: {
          phase: "off",
          mode: "off",
          queue_position: null,
          next_check_at: null,
          last_check_at: null,
          checked_at: null,
          next_due: null,
          effective_interval: null,
          heartbeat_at: null,
          sailings: [],
          error_class: null,
          consecutive_errors: 0,
          cart: null,
          viewer_connected: false,
        },
      },
    ],
    capacity: { can_add: true, reason: null },
    quiet_hours: { start: "23:00", end: "06:00", now: false },
    events: [],
  };
}

describe("MyStateSchema", () => {
  it("reads the user's own quiet hours, and whether a trip is quiet now", () => {
    const quiet = { start: "23:00", end: "06:00" };
    const base = older();
    const [trip] = base.trips as Record<string, unknown>[];
    const state = MyStateSchema.parse({
      ...base,
      me: { id: "u1", username: "ann", display: null, is_admin: false, paused: false },
      trips: [{ ...trip, quiet: true }],
      settings: { quiet_hours: quiet },
      quiet_hours: { ...quiet, now: true },
    });
    expect(state.settings.quiet_hours).toEqual(quiet);
    expect(state.quiet_hours).toEqual({ ...quiet, now: true });
    expect(state.trips[0]?.quiet).toBe(true);
  });

  it("reads an older server's no quiet hours as the default, which everyone has now", () => {
    const state = MyStateSchema.parse({ ...older(), settings: { quiet_hours: null }, quiet_hours: null });
    expect(state.settings.quiet_hours).toEqual({ start: "22:00", end: "05:00" });
    expect(state.quiet_hours).toBeNull();
  });

  it("tolerates an older server: no settings of the user's own, and no trip is quiet", () => {
    const state = MyStateSchema.parse(older());
    expect(state.settings).toEqual({
      quiet_hours: DEFAULT_QUIET_HOURS,
      checkout_viewer: "classic",
      stream_viewer_offered: false,
    });
    expect(state.trips[0]?.quiet).toBe(false);
    const none = older();
    delete none.quiet_hours;
    expect(MyStateSchema.parse(none).quiet_hours).toBeNull();
  });
});

describe("the checkout viewer", () => {
  const quiet = { start: "22:00", end: "05:00" };

  it("reads the user's choice and whether the beta is offered to them", () => {
    const settings = { quiet_hours: quiet, checkout_viewer: "stream", stream_viewer_offered: true };
    expect(MyStateSchema.parse({ ...older(), settings }).settings).toEqual(settings);
  });

  it("is classic, and not offered, from a server before the beta (0.6.4)", () => {
    const { settings } = MyStateSchema.parse({ ...older(), settings: { quiet_hours: quiet } });
    expect(settings).toEqual({ quiet_hours: quiet, checkout_viewer: "classic", stream_viewer_offered: false });
  });

  it("is classic where a server says something this build doesn't know", () => {
    const settings = { quiet_hours: quiet, checkout_viewer: "stream2", stream_viewer_offered: "yes" };
    const parsed = MyStateSchema.parse({ ...older(), settings }).settings;
    expect([parsed.checkout_viewer, parsed.stream_viewer_offered]).toEqual(["classic", false]);
  });

  it("is what the checkout link says it opens, classic from a server before the beta", () => {
    const url = "https://ferry-checkout.chestr.dev/t/3f9c0a1b2c3d4e5f/g/2/#ticket";
    expect(CheckoutSchema.parse({ url, ttl: 60 }).viewer).toBe("classic");
    expect(CheckoutSchema.parse({ url, ttl: 60, viewer: "stream" }).viewer).toBe("stream");
    expect(CheckoutSchema.parse({ url, ttl: 60, viewer: "other" }).viewer).toBe("classic");
  });
});
