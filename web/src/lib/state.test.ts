import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { makeState } from "@/test/fixtures";
import { jsonResponse, mockFetch } from "@/test/render";
import { fetchSnapshot, mergeSnapshot, readSnapshot, supersedes, writeState } from "./state";

describe("supersedes", () => {
  it.each([
    { current: ["a", 5], incoming: ["a", 6], accepted: true, why: "newer seq" },
    { current: ["a", 5], incoming: ["a", 5], accepted: true, why: "same seq (confirms the data)" },
    { current: ["a", 5], incoming: ["a", 4], accepted: false, why: "older seq" },
    { current: ["a", 500], incoming: ["b", 1], accepted: true, why: "new boot, seq restarted" },
  ] as const)("$why: $accepted", ({ current, incoming, accepted }) => {
    expect(
      supersedes(
        makeState({ boot_id: current[0], seq: current[1] }),
        makeState({ boot_id: incoming[0], seq: incoming[1] }),
      ),
    ).toBe(accepted);
  });
});

describe("mergeSnapshot", () => {
  it("takes the first snapshot and measures clock skew when it arrives", () => {
    const state = makeState({ server_time: 1_000 });
    const snapshot = mergeSnapshot(undefined, state, 1_005_000);
    expect(snapshot).toEqual({ state, skew: 5, receivedAt: 1_005_000 });
  });

  it("re-measures skew with each accepted snapshot", () => {
    const first = mergeSnapshot(undefined, makeState({ seq: 1, server_time: 1_000 }), 1_005_000);
    const second = mergeSnapshot(first, makeState({ seq: 1, server_time: 1_100 }), 1_102_000);
    expect(second.skew).toBe(2);
    expect(second.receivedAt).toBe(1_102_000);
  });

  it("keeps the current snapshot (same object) when the incoming one is older", () => {
    const current = mergeSnapshot(undefined, makeState({ seq: 7 }), 1_000);
    expect(mergeSnapshot(current, makeState({ seq: 6 }), 2_000)).toBe(current);
  });

  it("accepts a restarted service even though its seq is lower", () => {
    const current = mergeSnapshot(undefined, makeState({ boot_id: "a", seq: 900 }), 1_000);
    const next = mergeSnapshot(current, makeState({ boot_id: "b", seq: 1 }), 2_000);
    expect(next.state.boot_id).toBe("b");
  });
});

describe("writeState", () => {
  it("never lets an older state overwrite a newer one", () => {
    const qc = new QueryClient();
    writeState(qc, makeState({ seq: 5 }), 1_000);
    writeState(qc, makeState({ seq: 4 }), 2_000);
    expect(readSnapshot(qc)).toMatchObject({ state: { seq: 5 }, receivedAt: 1_000 });
    writeState(qc, makeState({ seq: 6 }), 3_000);
    expect(readSnapshot(qc)).toMatchObject({ state: { seq: 6 }, receivedAt: 3_000 });
  });

  it("applies to the polling query too: a slow response can't roll back a pushed state", async () => {
    const qc = new QueryClient();
    mockFetch(() => jsonResponse(makeState({ seq: 3 })));
    writeState(qc, makeState({ seq: 8 }), 1_000); // a WebSocket push lands while the GET is in flight
    const snapshot = await fetchSnapshot(qc);
    expect(snapshot.state.seq).toBe(8);
  });
});
