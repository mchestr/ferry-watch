/**
 * Times and dates. The API sends epoch seconds, "HH:MM" (24-hour) and "YYYY-MM-DD". Parsers return
 * null for anything they can't read, so the UI never shows "12:undefined am" or a silent 0.
 */

/** Where the ferries run. The server judges dates (today, a date that has passed) in this zone too. */
export const FERRY_TIME_ZONE = "America/Vancouver";

const TIME_OF_DAY = /^([01]\d|2[0-3]):([0-5]\d)$/;
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

const pad2 = (n: number) => String(n).padStart(2, "0");

/** "07:30" -> 450 (minutes after midnight); null unless it's a valid "HH:MM". */
export function parseTimeOfDay(value: string): number | null {
  const match = TIME_OF_DAY.exec(value);
  if (!match) return null;
  const [, hours = "", minutes = ""] = match;
  return Number(hours) * 60 + Number(minutes);
}

/** "13:05" -> "1:05 pm", "00:00" -> "12:00 am"; null unless it's a valid "HH:MM". */
export function fmt12(value: string): string | null {
  const total = parseTimeOfDay(value);
  if (total === null) return null;
  const hours = Math.floor(total / 60);
  return `${((hours + 11) % 12) + 1}:${pad2(total % 60)} ${hours < 12 ? "am" : "pm"}`;
}

/** For display: the 12-hour time, or the raw value when it can't be parsed. */
export function timeLabel(value: string): string {
  return fmt12(value) ?? value;
}

/** Travel time between two "HH:MM" times, wrapping past midnight: "1h 35m". Null if either is invalid. */
export function duration(depart: string, arrive: string): string | null {
  const from = parseTimeOfDay(depart);
  const to = parseTimeOfDay(arrive);
  if (from === null || to === null) return null;
  const minutes = (to - from + 1440) % 1440;
  return `${Math.floor(minutes / 60)}h ${pad2(minutes % 60)}m`;
}

/** "YYYY-MM-DD" -> that day at UTC midnight; null if malformed or not on the calendar ("2026-02-30"). */
export function parseIsoDate(value: string): Date | null {
  const match = ISO_DATE.exec(value);
  if (!match) return null;
  const [, y = "", m = "", d = ""] = match;
  const year = Number(y);
  const month = Number(m) - 1;
  const day = Number(d);
  const date = new Date(Date.UTC(year, month, day));
  date.setUTCFullYear(year); // Date.UTC maps years 0-99 to 1900-1999
  return date.getUTCFullYear() === year && date.getUTCMonth() === month && date.getUTCDate() === day ? date : null;
}

function toIsoDate(date: Date): string {
  return `${String(date.getUTCFullYear()).padStart(4, "0")}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`;
}

/**
 * Calendar arithmetic on "YYYY-MM-DD". It moves the day with setUTCDate rather than adding
 * 86 400 000 ms, so a DST change (a 23- or 25-hour day) can't skip or repeat a date.
 */
export function addDays(iso: string, days: number): string | null {
  const date = parseIsoDate(iso);
  if (!date) return null;
  date.setUTCDate(date.getUTCDate() + days);
  return toIsoDate(date);
}

const pacificDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: FERRY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** Today's date in Pacific time, as "YYYY-MM-DD". */
export function todayInPacific(now: Date = new Date()): string {
  const parts = pacificDate.formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** "Monday, October 12" for a "YYYY-MM-DD" date; null if it isn't one. */
export function longDate(iso: string): string | null {
  const date = parseIsoDate(iso);
  return date
    ? date.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" })
    : null;
}

/** Local wall-clock time of an epoch in milliseconds: "3:05 pm". Empty for NaN or Infinity. */
export function clock(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  const date = new Date(ms);
  return fmt12(`${pad2(date.getHours())}:${pad2(date.getMinutes())}`) ?? "";
}

/** "just now", "45s ago", "5 min ago", "3h ago", then a date. Empty for NaN or Infinity. */
export function ago(ms: number, now: number): string {
  if (!Number.isFinite(ms) || !Number.isFinite(now)) return "";
  const seconds = Math.max(0, Math.floor((now - ms) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
