import { describe, expect, it } from "vitest";
import {
  BURST_FOR_S,
  BURST_S,
  ESTIMATED_SEASON,
  inSeason,
  intervalSeconds,
  KNOWN_SEASONS,
  OFF_SEASON_S,
  pacificEpoch,
  RELEASE_AT,
  releaseAt,
  REMINDER_AT,
  reminderAt,
  STEADY_S,
  stillUseful,
  STOP_AT,
  stopAt,
} from "./season";

const at = (local: string) => new Date(local).getTime();

describe("Buntzen Lake's calendar, as the provider has it (design.md §5.5)", () => {
  it("has the provider's values", () => {
    expect(KNOWN_SEASONS).toEqual({ 2026: ["2026-05-14", "2026-09-07"] });
    expect(ESTIMATED_SEASON).toEqual([
      [5, 1],
      [9, 30],
    ]);
    expect(RELEASE_AT).toBe("07:00");
    expect(REMINDER_AT).toBe("06:55");
    expect(STOP_AT).toEqual({ full_day: "18:00", morning: "12:00", afternoon: "18:00" });
    expect([BURST_S, BURST_FOR_S, STEADY_S, OFF_SEASON_S]).toEqual([30, 20 * 60, 60, 30 * 60]);
  });

  it("knows the 2026 season and estimates the others, both ends inclusive", () => {
    expect(inSeason("2026-05-13")).toBe(false);
    expect(inSeason("2026-05-14")).toBe(true);
    expect(inSeason("2026-09-07")).toBe(true);
    expect(inSeason("2026-09-08")).toBe(false);
    expect(inSeason("2027-04-30")).toBe(false);
    expect(inSeason("2027-05-01")).toBe(true);
    expect(inSeason("2027-09-30")).toBe(true);
    expect(inSeason("2027-10-01")).toBe(false);
  });

  it("puts passes on sale at 7:00 Pacific the day before, across DST", () => {
    expect(releaseAt("2027-07-01")).toBe(at("2027-06-30T07:00:00-07:00"));
    expect(releaseAt("2027-07-01")).toBe(1_814_364_000_000); // the shared fixture's opens_at
    expect(releaseAt("2027-03-15")).toBe(at("2027-03-14T07:00:00-07:00")); // the first day of PDT
    expect(releaseAt("2027-11-08")).toBe(at("2027-11-07T07:00:00-08:00")); // the first day of PST
    expect(reminderAt("2027-07-01")).toBe(at("2027-06-30T06:55:00-07:00"));
    expect(pacificEpoch("2027-01-15", "00:00")).toBe(at("2027-01-15T00:00:00-08:00"));
  });

  it("stops each pass at its own time on the day", () => {
    expect(stopAt("2027-07-01", "morning")).toBe(at("2027-07-01T12:00:00-07:00"));
    expect(stopAt("2027-07-01", "afternoon")).toBe(at("2027-07-01T18:00:00-07:00"));
    expect(stillUseful("2027-07-01", ["morning"], at("2027-07-01T11:59:00-07:00"))).toBe(true);
    expect(stillUseful("2027-07-01", ["morning"], at("2027-07-01T12:00:00-07:00"))).toBe(false);
    expect(stillUseful("2027-07-01", ["morning", "full_day"], at("2027-07-01T17:00:00-07:00"))).toBe(true);
  });

  it("checks every 30 s for 20 minutes after the release, then every minute, and every 30 minutes out of season", () => {
    expect(intervalSeconds("2027-07-01", at("2027-06-30T07:19:00-07:00"))).toBe(30);
    expect(intervalSeconds("2027-07-01", at("2027-06-30T07:21:00-07:00"))).toBe(60);
    expect(intervalSeconds("2026-10-20", at("2026-10-19T07:01:00-07:00"))).toBe(30 * 60);
  });
});
