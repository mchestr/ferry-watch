/**
 * The app's copy of the server State. Snapshots arrive from three places (WebSocket pushes, the
 * polling query, mutation responses) and can arrive out of order, so all of them go through
 * mergeSnapshot, which keeps an older snapshot from replacing a newer one.
 */
import type { QueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { State } from "./schema";

export const STATE_KEY = ["state"] as const;

export interface Snapshot {
  readonly state: State;
  /** Client clock minus server clock, in seconds, measured when this snapshot arrived. */
  readonly skew: number;
  /** Client time (epoch ms) when this snapshot arrived. */
  readonly receivedAt: number;
}

/**
 * True when `incoming` may replace `current`: it comes from a new boot (the service restarted and
 * its seq started over), or it's at least as new. An equal seq is accepted: it confirms the data
 * and refreshes the clock skew.
 */
export function supersedes(current: State, incoming: State): boolean {
  return incoming.boot_id !== current.boot_id || incoming.seq >= current.seq;
}

/** Returns the snapshot to keep: a new one built from `incoming`, or `current` when `incoming` is older. */
export function mergeSnapshot(current: Snapshot | undefined, incoming: State, receivedAt: number): Snapshot {
  if (current && !supersedes(current.state, incoming)) return current;
  return { state: incoming, skew: receivedAt / 1000 - incoming.server_time, receivedAt };
}

export function readSnapshot(qc: QueryClient): Snapshot | undefined {
  return qc.getQueryData<Snapshot>(STATE_KEY);
}

/** Writes a State from a WebSocket push or a mutation response into the cache, if it's not older. */
export function writeState(qc: QueryClient, incoming: State, receivedAt: number = Date.now()): void {
  const current = readSnapshot(qc);
  const next = mergeSnapshot(current, incoming, receivedAt);
  if (next !== current) qc.setQueryData<Snapshot>(STATE_KEY, next);
}

/** The state query's fetcher. Returns the merged snapshot, so a slow response can't roll the cache back. */
export async function fetchSnapshot(qc: QueryClient, signal?: AbortSignal): Promise<Snapshot> {
  const incoming = await api.state(signal);
  return mergeSnapshot(readSnapshot(qc), incoming, Date.now());
}
