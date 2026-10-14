import { describe, expect, it } from "vitest";
import { makeSettings } from "@/test/fixtures";
import { MyStateSchema } from "./schema";

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

  it("reads no quiet hours as none", () => {
    const state = MyStateSchema.parse({ ...older(), settings: { quiet_hours: null }, quiet_hours: null });
    expect(state.settings.quiet_hours).toBeNull();
    expect(state.quiet_hours).toBeNull();
  });

  it("tolerates an older server: no settings of the user's own, and no trip is quiet", () => {
    const state = MyStateSchema.parse(older());
    expect(state.settings).toEqual({ quiet_hours: null });
    expect(state.trips[0]?.quiet).toBe(false);
    const none = older();
    delete none.quiet_hours;
    expect(MyStateSchema.parse(none).quiet_hours).toBeNull();
  });
});
