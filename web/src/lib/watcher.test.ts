import { describe, expect, it } from "vitest";
import { makeCart, makeState, makeTrip } from "@/test/fixtures";
import { OVERDUE_GRACE_S, ringFraction, watcherPhase } from "./watcher";

const NOW = 10_000;

describe("watcherPhase", () => {
  it("counts down to next_check_at on the server's clock", () => {
    expect(watcherPhase(makeState({ next_check_at: NOW + 25 }), NOW)).toEqual({
      kind: "countdown",
      remaining: 25,
      at: NOW + 25,
    });
  });

  it("says 'checking' instead of sitting at 0:00, then 'overdue'", () => {
    const state = makeState({ next_check_at: NOW });
    expect(watcherPhase(state, NOW).kind).toBe("due");
    expect(watcherPhase(state, NOW + OVERDUE_GRACE_S - 1).kind).toBe("due");
    expect(watcherPhase(state, NOW + OVERDUE_GRACE_S).kind).toBe("overdue");
  });

  it("shows a running check", () => {
    expect(watcherPhase(makeState({ trips: [makeTrip({ checking: true })] }), NOW).kind).toBe("checking");
  });

  it("reports a stalled watcher above everything else", () => {
    const state = makeState({ enabled: false, watcher: { heartbeat_at: NOW - 300, healthy: false } });
    expect(watcherPhase(state, NOW)).toEqual({ kind: "stalled", heartbeatAt: NOW - 300 });
  });

  it("knows when there's nothing to check", () => {
    expect(watcherPhase(makeState({ enabled: false }), NOW).kind).toBe("off");
    expect(watcherPhase(makeState({ trips: [] }), NOW)).toEqual({ kind: "idle", reason: "no-trips" });
    expect(watcherPhase(makeState({ trips: [makeTrip({ cart: makeCart() })] }), NOW)).toEqual({
      kind: "idle",
      reason: "held",
    });
  });

  it("guards against a missing or non-finite next check", () => {
    expect(watcherPhase(makeState({ next_check_at: null }), NOW).kind).toBe("starting");
    expect(watcherPhase(makeState(), NaN).kind).toBe("starting");
  });
});

describe("ringFraction", () => {
  it("fills as the next check approaches", () => {
    expect(ringFraction({ kind: "countdown", remaining: 15, at: 0 }, 30)).toBe(0.5);
    expect(ringFraction({ kind: "countdown", remaining: 45, at: 0 }, 30)).toBe(0);
    expect(ringFraction({ kind: "overdue", at: 0 }, 30)).toBe(1);
    expect(ringFraction({ kind: "off" }, 30)).toBe(0);
  });

  it("is never NaN", () => {
    expect(ringFraction({ kind: "countdown", remaining: 15, at: 0 }, 0)).toBe(0);
    expect(ringFraction({ kind: "countdown", remaining: NaN, at: 0 }, 30)).toBe(0);
  });
});
