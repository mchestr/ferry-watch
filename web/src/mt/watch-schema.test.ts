import { describe, expect, it } from "vitest";
import fixture from "@/test/fixtures/api-state.json";
import { CreatedWatchSchema, WaitlistStateSchema, parseWatch } from "./watch-schema";

describe("the Waitlist state contract", () => {
  it("parses the shared fixture", () => {
    const state = WaitlistStateSchema.parse(fixture);
    expect(state.watches.map((w) => w.kind)).toEqual(["buntzen", "buntzen", "bcferries", "unknown"]);
    const buntzen = state.watches[0];
    if (buntzen?.kind !== "buntzen") throw new Error("expected buntzen");
    expect(buntzen.options[0]?.status).toBe("open");
    expect(buntzen.booking_url).toBe("https://yodelportal.com/buntzen-lake/Half-Day-Pass");
    const waiting = state.watches[1];
    if (waiting?.kind !== "buntzen") throw new Error("expected buntzen");
    expect(waiting.check.state).toBe("waiting_release");
    const bcf = state.watches[2];
    if (bcf?.kind !== "bcferries") throw new Error("expected bcferries");
    expect(bcf.trip.from).toBe("TSA");
    expect(bcf.trip.id).toBe(bcf.id);
    expect(state.providers.map((p) => [p.id, p.offered])).toEqual([
      ["bcferries", true],
      ["buntzen", true],
    ]);
    expect(state.capacity.providers.buntzen?.limit).toBe(6);
  });
  it("never lets one bad watch blank the page", () => {
    expect(parseWatch({ provider: "buntzen", id: "0f3c9a1b2d4e5f60", rev: 1, enabled: true })?.kind).toBe("unknown");
    expect(parseWatch({ provider: "constructor", id: "0f3c9a1b2d4e5f60" })?.kind).toBe("unknown");
    expect(parseWatch({ nope: true })).toBeNull();
    expect(parseWatch(null)).toBeNull();
  });
  it("reads a check state or status this build doesn't know as unknown", () => {
    const raw = structuredClone(fixture.watches[0]) as Record<string, unknown> & { check: Record<string, unknown> };
    raw.check.state = "teleporting";
    const w = parseWatch(raw);
    expect(w?.kind === "buntzen" && w.check.state).toBe("unknown");
  });
  it("drops a booking link off Yodel's Buntzen pages", () => {
    const raw = structuredClone(fixture.watches[0]) as Record<string, unknown>;
    raw.booking_url = "https://evil.example/buntzen-lake/";
    const w = parseWatch(raw);
    expect(w?.kind === "buntzen" && w.booking_url).toBe(null);
  });
  it("parses a created watch", () => {
    expect(CreatedWatchSchema.parse({ watch_id: "0f3c9a1b2d4e5f60", state: fixture }).watch_id).toBe(
      "0f3c9a1b2d4e5f60",
    );
  });
});
