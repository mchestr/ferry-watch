import type { State } from "./schema";
import { heldCart, isActive } from "./trip";

/** How long past next_check_at before a check that hasn't started counts as overdue. */
export const OVERDUE_GRACE_S = 20;

export type WatcherPhase =
  | { kind: "stalled"; heartbeatAt: number }
  | { kind: "off" }
  | { kind: "idle"; reason: "no-trips" | "held" }
  | { kind: "checking" }
  | { kind: "starting" }
  | { kind: "countdown"; remaining: number; at: number }
  /** Due, and a check should be starting any moment. */
  | { kind: "due"; at: number }
  | { kind: "overdue"; at: number };

/** What the watcher is doing, given the server's clock now (epoch seconds). */
export function watcherPhase(state: State, serverNow: number): WatcherPhase {
  if (!state.watcher.healthy) return { kind: "stalled", heartbeatAt: state.watcher.heartbeat_at };
  if (!state.enabled) return { kind: "off" };
  if (!state.trips.some((t) => isActive(t, true))) {
    const held = state.trips.some((t) => t.enabled && !t.past && heldCart(t) !== null);
    return { kind: "idle", reason: held ? "held" : "no-trips" };
  }
  if (state.trips.some((t) => t.checking)) return { kind: "checking" };
  const at = state.next_check_at;
  const remaining = at === null ? NaN : at - serverNow;
  if (at === null || !Number.isFinite(remaining)) return { kind: "starting" };
  if (remaining > 0) return { kind: "countdown", remaining, at };
  return remaining > -OVERDUE_GRACE_S ? { kind: "due", at } : { kind: "overdue", at };
}

/** How full the countdown ring is, 0 to 1. Never NaN. */
export function ringFraction(phase: WatcherPhase, intervalSeconds: number): number {
  switch (phase.kind) {
    case "countdown": {
      const fraction = intervalSeconds > 0 ? 1 - phase.remaining / intervalSeconds : 0;
      return Number.isFinite(fraction) ? Math.min(1, Math.max(0, fraction)) : 0;
    }
    case "checking":
    case "due":
    case "overdue":
      return 1;
    default:
      return 0;
  }
}
