/**
 * Buntzen Lake's calendar, as the provider has it (src/waitlist/providers/buntzen/policy.py; docs/waitlist/design.md
 * §5.5): the reservation seasons, the 7:00 release the day before, the reminder, each pass's stop time and the poll
 * curve. The server decides; the SPA mirrors these only to say them (a test holds them to §5.5). All Pacific.
 */
import type { BuntzenPass } from "@/mt/watch-schema";
import { addDays, FERRY_TIME_ZONE } from "@/lib/time";

/** Reservation seasons, both ends inclusive. Other years use the estimate, which is generous on purpose. */
export const KNOWN_SEASONS: Readonly<Record<number, readonly [from: string, to: string]>> = {
  2026: ["2026-05-14", "2026-09-07"],
};
/** (month, day) to (month, day). */
export const ESTIMATED_SEASON: readonly [readonly [number, number], readonly [number, number]] = [
  [5, 1],
  [9, 30],
];

export const RELEASE_AT = "07:00"; // on the day before
export const REMINDER_AT = "06:55"; // on the day before, when release_reminder is on
/** When a pass stops being worth finding, on the day. */
export const STOP_AT: Readonly<Record<BuntzenPass, string>> = {
  full_day: "18:00",
  morning: "12:00",
  afternoon: "18:00",
};
export const BURST_S = 30; // how often, for BURST_FOR_S after the release
export const BURST_FOR_S = 20 * 60;
export const STEADY_S = 60;
export const OFF_SEASON_S = 30 * 60;

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Whether `iso` ("YYYY-MM-DD") is inside Buntzen's reservation season. */
export function inSeason(iso: string): boolean {
  const year = Number(iso.slice(0, 4));
  const known = KNOWN_SEASONS[year];
  if (known) return known[0] <= iso && iso <= known[1];
  const [[fromMonth, fromDay], [toMonth, toDay]] = ESTIMATED_SEASON;
  const y = iso.slice(0, 4);
  return `${y}-${pad2(fromMonth)}-${pad2(fromDay)}` <= iso && iso <= `${y}-${pad2(toMonth)}-${pad2(toDay)}`;
}

const wallClock = new Intl.DateTimeFormat("en-US", {
  timeZone: FERRY_TIME_ZONE,
  hourCycle: "h23",
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
});

/** How far Pacific wall-clock time is ahead of UTC at `ms` (negative: -7 or -8 hours). */
function offsetMs(ms: number): number {
  const parts = wallClock.formatToParts(new Date(ms));
  const part = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  const wall = Date.UTC(part("year"), part("month") - 1, part("day"), part("hour"), part("minute"));
  return wall - Math.floor(ms / 60_000) * 60_000;
}

/** Epoch milliseconds of "HH:MM" Pacific on `iso`, across DST. */
export function pacificEpoch(iso: string, hhmm: string): number {
  const [y = 0, m = 1, d = 1] = iso.split("-").map(Number);
  const [h = 0, min = 0] = hhmm.split(":").map(Number);
  const wall = Date.UTC(y, m - 1, d, h, min);
  const guess = wall - offsetMs(wall);
  return wall - offsetMs(guess);
}

const dayBefore = (iso: string) => addDays(iso, -1) ?? iso;

/** When passes for `date` go on sale: 7:00 the day before. */
export const releaseAt = (date: string): number => pacificEpoch(dayBefore(date), RELEASE_AT);
/** When the release reminder goes out: 6:55 the day before. */
export const reminderAt = (date: string): number => pacificEpoch(dayBefore(date), REMINDER_AT);
/** When `pass` stops being worth finding on `date`. */
export const stopAt = (date: string, pass: BuntzenPass): number => pacificEpoch(date, STOP_AT[pass]);

/** Whether any of `passes` is still worth finding on `date` at `now`. */
export function stillUseful(date: string, passes: readonly BuntzenPass[], now: number): boolean {
  return passes.some((pass) => now < stopAt(date, pass));
}

/** How often a watch for `date` is checked at `now`, in seconds (the provider's `interval`). */
export function intervalSeconds(date: string, now: number, opensAt: number = releaseAt(date)): number {
  if (!inSeason(date)) return OFF_SEASON_S;
  return now < opensAt + BURST_FOR_S * 1000 ? BURST_S : STEADY_S;
}
