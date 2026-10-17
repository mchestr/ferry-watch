/**
 * How a Buntzen Lake watch reads (docs/waitlist/design.md §5.5, §5.7, §7.3-7.4): the passes' names, dates as the
 * provider writes them ("Thu Jul 1"), the card's badge, each pass's status chip and the notes under them.
 */
import { clock, FERRY_TIME_ZONE, fmt12, parseIsoDate, timeLabel, todayInPacific } from "@/lib/time";
import type { Tone } from "@/mt/trips";
import { BUNTZEN_PASSES, type BuntzenPass, type BuntzenWatch, type WatchOption } from "@/mt/watch-schema";
import { inSeason, intervalSeconds, releaseAt, RELEASE_AT, REMINDER_AT, stopAt } from "./season";

/** The provider's own names for the passes (policy.LABELS). */
export const LABELS: Readonly<Record<BuntzenPass, string>> = {
  full_day: "All-day pass",
  morning: "Morning pass",
  afternoon: "Afternoon pass",
};

/** The form's toggle chips. */
export const CHIPS: Readonly<Record<BuntzenPass, string>> = {
  full_day: "All day",
  morning: "Morning",
  afternoon: "Afternoon",
};

/** Where to book each pass yourself (policy.PAGES): the half-day passes share a page. */
export const PAGES: Readonly<Record<BuntzenPass, string>> = {
  full_day: "https://yodelportal.com/buntzen-lake/All-Day-Pass",
  morning: "https://yodelportal.com/buntzen-lake/Half-Day-Pass",
  afternoon: "https://yodelportal.com/buntzen-lake/Half-Day-Pass",
};

export const isPass = (key: string): key is BuntzenPass => (BUNTZEN_PASSES as readonly string[]).includes(key);

/** "morning or afternoon pass", "all-day, morning or afternoon pass". */
export function passesPhrase(passes: readonly BuntzenPass[]): string {
  const words = BUNTZEN_PASSES.filter((p) => passes.includes(p)).map((p) =>
    LABELS[p].replace(/ pass$/, "").toLowerCase(),
  );
  const joined = words.length > 1 ? `${words.slice(0, -1).join(", ")} or ${words.at(-1) ?? ""}` : (words[0] ?? "");
  return `${joined} pass`;
}

/** The release and reminder times as the copy says them: "7:00", "6:55". */
export const RELEASE_LABEL = RELEASE_AT.replace(/^0/, "");
export const REMINDER_LABEL = REMINDER_AT.replace(/^0/, "");

const capitalise = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** The card's title: the passes chosen. */
export const passesTitle = (passes: readonly BuntzenPass[]): string => capitalise(passesPhrase(passes));

const utcDay = (iso: string, options: Intl.DateTimeFormatOptions) =>
  parseIsoDate(iso)?.toLocaleDateString("en-US", { ...options, timeZone: "UTC" }) ?? iso;

/** "Thu Jul 1" for "2027-07-01" (the provider's `%a %b %-d`). */
export const dayLabel = (iso: string): string =>
  parseIsoDate(iso)
    ? `${utcDay(iso, { weekday: "short" })} ${utcDay(iso, { month: "short" })} ${utcDay(iso, { day: "numeric" })}`
    : iso;

/** "Sat 3", for the form's day chips. */
export const chipDay = (iso: string): string =>
  parseIsoDate(iso) ? `${utcDay(iso, { weekday: "short" })} ${utcDay(iso, { day: "numeric" })}` : iso;

const pacificTime = new Intl.DateTimeFormat("en-US", {
  timeZone: FERRY_TIME_ZONE,
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
});
const pacificWeekday = new Intl.DateTimeFormat("en-US", { timeZone: FERRY_TIME_ZONE, weekday: "short" });

/**
 * Whether `ms` is a time a Date can hold. Intl's formatters throw on any other (NaN, Infinity, or a finite epoch
 * past the year 275760, which the schema's `epoch` accepts), so every Pacific label checks first.
 */
const datable = (ms: number): boolean => !Number.isNaN(new Date(ms).getTime());

/** "7:00 am" in Pacific time, for the release and other fixed times. */
export function pacificClock(ms: number): string {
  if (!datable(ms)) return "";
  const parts = pacificTime.formatToParts(new Date(ms));
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return fmt12(`${part("hour")}:${part("minute")}`) ?? "";
}

/** "Wed" in Pacific time. */
export const pacificDay = (ms: number): string => (datable(ms) ? pacificWeekday.format(new Date(ms)) : "");

/** "Wed Jun 30" in Pacific time. */
export const pacificDate = (ms: number): string => (datable(ms) ? dayLabel(todayInPacific(new Date(ms))) : "");

/** "today", "tomorrow" or "Fri", from `now`'s point of view. */
export function relativeDay(ms: number, now: number): string {
  if (!datable(ms) || !datable(now)) return "";
  const day = todayInPacific(new Date(ms));
  const today = todayInPacific(new Date(now));
  if (day === today) return "today";
  if (day === todayInPacific(new Date(now + 86_400_000))) return "tomorrow";
  return pacificDay(ms);
}

/** When passes for this watch go on sale: the server's opens_at, else the provider's rule. */
export const opensAt = (watch: BuntzenWatch): number =>
  watch.check.opens_at !== null ? watch.check.opens_at * 1000 : releaseAt(watch.spec.date);

/**
 * The watch's options as they stand at `now`: a pass whose stop time has passed is over, whatever the last check
 * saw (the provider's first rule), so a pass found open at 11:59 isn't offered for booking at noon.
 */
export function currentOptions(watch: BuntzenWatch, now: number): WatchOption[] {
  return watch.options.map((o) =>
    isPass(o.key) && o.status !== "over" && now >= stopAt(watch.spec.date, o.key) ? { ...o, status: "over" } : o,
  );
}

const ALERTABLE = new Set(["open", "maybe"]);

/** The wanted passes that are open or may be: what "Book on Yodel" is for. */
export const bookable = (watch: BuntzenWatch, now: number): WatchOption[] =>
  currentOptions(watch, now).filter((o) => o.wanted && ALERTABLE.has(o.status));

/** The card's badge (§7.4): the first row that applies. `pulse`: something is open now. */
export function badge(watch: BuntzenWatch, now: number): { text: string; tone: Tone; pulse?: boolean } {
  const wanted = currentOptions(watch, now).filter((o) => o.wanted);
  if (wanted.some((o) => o.status === "open")) return { text: "Open now", tone: "warning", pulse: true };
  if (wanted.some((o) => o.status === "maybe")) return { text: "May be open", tone: "warning" };
  switch (watch.check.state) {
    case "waiting_release": {
      const at = opensAt(watch);
      return { text: `On sale ${pacificDay(at)} ${pacificClock(at)}`, tone: "soft" };
    }
    case "checking":
      return { text: "Watching", tone: "success-outline" };
    case "pending":
      return { text: "Starting", tone: "muted" };
    case "quiet":
      return { text: "Quiet hours", tone: "soft" };
    case "paused":
      return { text: "Paused", tone: "outline" };
    case "provider_paused":
      return { text: "Paused by admin", tone: "outline" };
    case "unavailable":
      return { text: "Checks not running", tone: "soft" };
    case "backoff":
    case "error":
      return { text: "Problem", tone: "danger" };
    case "over":
      return { text: "Over", tone: "outline" };
    case "unknown":
      return { text: "Unknown", tone: "outline" };
  }
}

const CHIP_TEXT: Readonly<Record<WatchOption["status"], string>> = {
  open: "Open",
  maybe: "May be open",
  sold_out: "Sold out",
  not_released: "Not on sale yet",
  not_offered: "Not offered",
  over: "Over",
  unchecked: "Not checked yet",
  unknown: "Unknown",
};

const CHIP_TONE: Readonly<Record<WatchOption["status"], Tone>> = {
  open: "success",
  maybe: "warning",
  sold_out: "outline",
  not_released: "muted",
  not_offered: "muted",
  over: "muted",
  unchecked: "muted",
  unknown: "outline",
};

/** One pass's status chip. A pass the last check couldn't reach (a partial failure) says so. */
export function chip(option: WatchOption, outcome: BuntzenWatch["check"]["outcome"]): { text: string; tone: Tone } {
  if (option.status === "unchecked" && outcome === "error") return { text: "Couldn't check", tone: "danger" };
  return { text: CHIP_TEXT[option.status], tone: CHIP_TONE[option.status] };
}

/** "every 30 seconds", "every minute", "every 30 minutes". */
function every(seconds: number): string {
  if (seconds % 60 !== 0) return `every ${seconds} seconds`;
  const minutes = seconds / 60;
  return minutes === 1 ? "every minute" : `every ${minutes} minutes`;
}

/**
 * The notes under the passes (§7.4, §5.6), in order. `quietEnd` is the user's quiet hours' end ("HH:MM");
 * `cadence: false` leaves out "Checked 7:31 am, every minute." where the page says it elsewhere.
 */
export function notes(
  watch: BuntzenWatch,
  now: number,
  quietEnd?: string,
  { cadence = true }: { cadence?: boolean } = {},
): string[] {
  const { check, spec } = watch;
  const day = dayLabel(spec.date);
  const out: string[] = [];
  if (check.outcome === "no_inventory") {
    out.push(
      `Yodel isn't offering passes for ${day}. Buntzen's reservation season usually runs from mid-May to early September.`,
    );
  }
  switch (check.state) {
    case "waiting_release": {
      const at = opensAt(watch);
      const remind = spec.release_reminder && inSeason(spec.date) ? `, and remind you at ${REMINDER_LABEL}` : "";
      out.push(
        `Passes for ${day} go on sale ${pacificDate(at)} at ${pacificClock(at)}. We'll start checking then${remind}.`,
      );
      break;
    }
    case "error":
    case "backoff":
      out.push(
        check.error_class === "site_changed"
          ? "We can't read Yodel's answers right now. Keep an eye on Yodel yourself."
          : `We're having trouble checking Yodel; retrying ${check.next_check_at !== null ? `at ${clock(check.next_check_at * 1000)}` : "soon"}.`,
      );
      break;
    case "unavailable":
      out.push("Checks aren't running right now. Keep an eye on Yodel yourself.");
      break;
    case "checking": {
      if (!cadence) break;
      const at = check.checked_at; // the last check that read Yodel: a failed one (last_check_at) read nothing
      const pace = every(intervalSeconds(spec.date, now, opensAt(watch)));
      out.push(at !== null ? `Checked ${clock(at * 1000)}, ${pace}.` : `Checking ${pace}.`);
      break;
    }
    case "quiet":
      out.push(`It's your quiet hours, so this isn't checked${quietEnd ? ` until ${timeLabel(quietEnd)} PT` : ""}.`);
      break;
    case "provider_paused":
      out.push("Checks are paused by an admin.");
      break;
    default:
      break;
  }
  return out;
}

/** The watch page's timeline: when passes go on sale and how often they're checked (§7.4). */
export function timeline(watch: BuntzenWatch): string {
  const at = opensAt(watch);
  const sale = `On sale ${pacificDate(at)} at ${pacificClock(at)}`;
  const morning = watch.spec.passes.includes("morning") ? "; morning passes count until noon" : "";
  if (!inSeason(watch.spec.date)) return `${sale}; outside the season, checked every 30 minutes${morning}`;
  return `${sale}; checked every 30 s for 20 minutes, then every minute${morning}`;
}
