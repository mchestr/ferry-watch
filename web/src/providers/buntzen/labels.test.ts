import { describe, expect, it } from "vitest";
import type { BuntzenWatch, WatchOption } from "@/mt/watch-schema";
import { FIXTURE_NOW, makeBuntzenWatch, fail } from "@/test/fixtures";
import {
  badge,
  chip,
  currentOptions,
  dayLabel,
  LABELS,
  notes,
  pacificClock,
  pacificDate,
  pacificDay,
  PAGES,
  passesPhrase,
  relativeDay,
  timeline,
} from "./labels";

const NOW = FIXTURE_NOW.getTime(); // Wed Jun 30 2027, 7:31 am
const open = makeBuntzenWatch(); // Thu Jul 1: morning open, afternoon sold out; checking
const option = (status: WatchOption["status"], changes: Partial<WatchOption> = {}): WatchOption => ({
  ...(open.options[0] ?? fail()),
  status,
  ...changes,
});
const withOptions = (options: WatchOption[], check: Partial<BuntzenWatch["check"]> = {}) =>
  makeBuntzenWatch({ options, check });

describe("Buntzen Lake's words (design.md §5.5, §5.7)", () => {
  it("names the passes and their pages as the provider does", () => {
    expect(LABELS).toEqual({ full_day: "All-day pass", morning: "Morning pass", afternoon: "Afternoon pass" });
    expect(PAGES).toEqual({
      full_day: "https://yodelportal.com/buntzen-lake/All-Day-Pass",
      morning: "https://yodelportal.com/buntzen-lake/Half-Day-Pass",
      afternoon: "https://yodelportal.com/buntzen-lake/Half-Day-Pass",
    });
    expect(passesPhrase(["morning"])).toBe("morning pass");
    expect(passesPhrase(["afternoon", "morning"])).toBe("morning or afternoon pass");
    expect(passesPhrase(["morning", "full_day", "afternoon"])).toBe("all-day, morning or afternoon pass");
    expect(dayLabel("2027-07-01")).toBe("Thu Jul 1");
    expect(dayLabel("2027-12-25")).toBe("Sat Dec 25");
  });
});

describe("a Buntzen card's badge (design.md §7.4)", () => {
  const cases: [string, ReturnType<typeof makeBuntzenWatch>, string, string][] = [
    ["a wanted pass open", open, "Open now", "warning"],
    ["a wanted pass that may be open", withOptions([option("maybe")]), "May be open", "warning"],
    [
      "waiting for the release",
      makeBuntzenWatch({ check: { state: "waiting_release" }, options: [] }),
      "On sale Wed 7:00 am",
      "soft",
    ],
    ["checking", withOptions([option("sold_out")]), "Watching", "success-outline"],
    ["never checked", withOptions([option("unchecked")], { state: "pending" }), "Starting", "muted"],
    ["in quiet hours", withOptions([], { state: "quiet" }), "Quiet hours", "soft"],
    ["paused", withOptions([], { state: "paused" }), "Paused", "outline"],
    ["paused by an admin", withOptions([], { state: "provider_paused" }), "Paused by admin", "outline"],
    ["with checks not running", withOptions([], { state: "unavailable" }), "Checks not running", "soft"],
    ["backing off", withOptions([], { state: "backoff" }), "Problem", "danger"],
    ["failing", withOptions([], { state: "error" }), "Problem", "danger"],
    ["over", withOptions([], { state: "over" }), "Over", "outline"],
    ["in a state this build doesn't know", withOptions([], { state: "unknown" }), "Unknown", "outline"],
  ];
  it.each(cases)("for %s", (_, watch, text, tone) => {
    expect(badge(watch, NOW)).toMatchObject({ text, tone });
  });

  it("pulses only while something is open, and never for a pass not watched", () => {
    expect(badge(open, NOW).pulse).toBe(true);
    expect(badge(withOptions([option("open", { wanted: false })]), NOW).text).toBe("Watching");
  });

  it("stops calling a pass open at its stop time", () => {
    const noon = new Date("2027-07-01T12:00:00-07:00").getTime();
    expect(currentOptions(open, noon).map((o) => o.status)).toEqual(["over", "sold_out"]);
    expect(badge(open, noon).text).toBe("Watching");
  });
});

describe("a pass's chip", () => {
  it.each<[WatchOption["status"], string]>([
    ["open", "Open"],
    ["maybe", "May be open"],
    ["sold_out", "Sold out"],
    ["not_released", "Not on sale yet"],
    ["not_offered", "Not offered"],
    ["over", "Over"],
    ["unchecked", "Not checked yet"],
    ["unknown", "Unknown"],
  ])("says %s as %s", (status, text) => {
    expect(chip(option(status), "ok").text).toBe(text);
  });

  it("says Couldn't check for a pass whose check failed while others' didn't", () => {
    expect(chip(option("unchecked"), "error")).toEqual({ text: "Couldn't check", tone: "danger" });
  });
});

describe("a Buntzen card's notes", () => {
  it("says when passes go on sale, and the reminder when it's on", () => {
    const waiting = makeBuntzenWatch({
      spec: { date: "2027-07-03", passes: ["full_day"] },
      check: { state: "waiting_release", opens_at: 1_814_536_800, outcome: "pending" },
      options: [],
    });
    expect(notes(waiting, NOW)).toEqual([
      "Passes for Sat Jul 3 go on sale Fri Jul 2 at 7:00 am. We'll start checking then, and remind you at 6:55.",
    ]);
    const quiet = { ...waiting, spec: { ...waiting.spec, release_reminder: false } };
    expect(notes(quiet, NOW)).toEqual([
      "Passes for Sat Jul 3 go on sale Fri Jul 2 at 7:00 am. We'll start checking then.",
    ]);
  });

  it("says when it was checked and how often", () => {
    expect(notes(open, NOW)).toEqual(["Checked 7:31 am, every minute."]);
    expect(notes(open, NOW, undefined, { cadence: false })).toEqual([]);
  });

  it("never says checked of a watch whose every check so far failed", () => {
    // Yodel answered 429 to the first check: it was tried (last_check_at), but nothing was read (checked_at).
    const failedFirst = makeBuntzenWatch({
      check: {
        state: "checking",
        outcome: "error",
        error_class: "rate_limited",
        consecutive_errors: 1,
        checked_at: null,
      },
      options: [option("unchecked")],
    });
    expect(failedFirst.check.last_check_at).not.toBeNull();
    expect(notes(failedFirst, NOW)).toEqual(["Checking every minute."]);
  });

  it("says out of season that Yodel isn't offering passes", () => {
    const offSeason = makeBuntzenWatch({
      spec: { date: "2026-10-20" },
      check: { state: "checking", outcome: "no_inventory", opens_at: 1_792_936_800, checked_at: 1_792_277_000 },
    });
    expect(notes(offSeason, new Date("2026-10-17T10:00:00-07:00").getTime())).toEqual([
      "Yodel isn't offering passes for Tue Oct 20. Buntzen's reservation season usually runs from mid-May to early September.",
      "Checked 3:43 pm, every 30 minutes.",
    ]);
  });

  it("says when checks are in trouble, and when Yodel's answers can't be read", () => {
    const retry = makeBuntzenWatch({
      check: { state: "backoff", error_class: "upstream_error", next_check_at: 1_814_366_510 },
    });
    expect(notes(retry, NOW)).toEqual(["We're having trouble checking Yodel; retrying at 7:41 am."]);
    const changed = makeBuntzenWatch({ check: { state: "error", error_class: "site_changed" } });
    expect(notes(changed, NOW)).toEqual(["We can't read Yodel's answers right now. Keep an eye on Yodel yourself."]);
    expect(notes(makeBuntzenWatch({ check: { state: "unavailable" } }), NOW)).toEqual([
      "Checks aren't running right now. Keep an eye on Yodel yourself.",
    ]);
  });

  it("says nothing, rather than throwing, of a time no Date can hold", () => {
    // Finite epochs past the year 275760 pass the schema, but Intl's formatters throw on them.
    for (const ms of [1e16, -1e16, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect([pacificClock(ms), pacificDay(ms), pacificDate(ms), relativeDay(ms, NOW)]).toEqual(["", "", "", ""]);
    }
    const far = makeBuntzenWatch({ check: { state: "waiting_release", opens_at: 1e13 }, options: [] });
    expect(() => [badge(far, NOW), notes(far, NOW), timeline(far)]).not.toThrow();
  });

  it("has a timeline for the watch's page", () => {
    expect(timeline(open)).toBe(
      "On sale Wed Jun 30 at 7:00 am; checked every 30 s for 20 minutes, then every minute; morning passes count until noon",
    );
  });
});
