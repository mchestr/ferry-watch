/**
 * The trip sheet's form model: a draft of TripSettings, its validation (the same rules the server
 * applies, see docs/api.md) and the PATCH of changed fields.
 */
import { ApiError } from "./api";
import { TRIP_SETTINGS_FIELDS, type TripSettings } from "./schema";
import { addDays, parseIsoDate, parseTimeOfDay } from "./time";

export const MAX_PASSENGERS = 9;
/** A "tall" vehicle is over this height (exclusive)... */
export const MIN_TALL_HEIGHT_FT = 7;
/** ...and at most this tall. */
export const MAX_TALL_HEIGHT_FT = 14;

/** The trip's settings as edited, with the height kept as typed so "8." or "8,5" survive editing. */
export interface TripDraft extends Omit<TripSettings, "height_ft"> {
  height: string;
}

export type DraftField = "from" | "to" | "date" | "passengers" | "height" | "earliest" | "latest";
export type DraftErrors = Partial<Record<DraftField, string>>;

/** In form order, for focusing the first problem. */
export const DRAFT_FIELDS: readonly DraftField[] = ["from", "to", "date", "passengers", "height", "earliest", "latest"];

/** Just the settings, from a Trip or anything else that has them. */
export function pickSettings(t: TripSettings): TripSettings {
  const {
    enabled,
    from,
    to,
    date,
    adults,
    children,
    infants,
    seniors,
    vehicle,
    height_ft,
    earliest,
    latest,
    auto_cart,
    fare,
  } = t;
  return {
    enabled,
    from,
    to,
    date,
    adults,
    children,
    infants,
    seniors,
    vehicle,
    height_ft,
    earliest,
    latest,
    auto_cart,
    fare,
  };
}

/**
 * A new trip starts from the server's trip_defaults. Its date is tomorrow as of the last snapshot,
 * which can be stale on a page left open overnight, so a passed date becomes tomorrow (Pacific).
 */
export function newTripSettings(defaults: TripSettings, today: string): TripSettings {
  const fresh = parseIsoDate(defaults.date) !== null && defaults.date >= today;
  return { ...pickSettings(defaults), date: fresh ? defaults.date : (addDays(today, 1) ?? today) };
}

export function draftFromSettings(settings: TripSettings): TripDraft {
  const { height_ft, ...rest } = settings;
  return { ...rest, height: Number.isFinite(height_ft) ? String(height_ft) : "" };
}

const HEIGHT = /^\d{1,2}(?:[.,]\d{1,2})?$/;

/** "8.5" or "8,5" (some phone keyboards only offer a comma) -> 8.5; null if it isn't a number of feet. */
export function parseHeight(raw: string): number | null {
  const value = raw.trim();
  return HEIGHT.test(value) ? Number(value.replace(",", ".")) : null;
}

export function validateHeight(raw: string): string | null {
  const height = parseHeight(raw);
  if (height === null) return raw.trim() ? "Enter the height in feet, like 8.5." : "Enter the vehicle's height.";
  if (height <= MIN_TALL_HEIGHT_FT) return `Must be over ${MIN_TALL_HEIGHT_FT} ft. Under that, choose Car / SUV.`;
  if (height > MAX_TALL_HEIGHT_FT) return `Must be ${MAX_TALL_HEIGHT_FT} ft or less.`;
  return null;
}

export function validatePassengers(
  p: Pick<TripSettings, "adults" | "children" | "infants" | "seniors">,
): string | null {
  if (p.adults + p.seniors < 1) return "Add at least one adult or senior.";
  if (p.adults + p.children + p.infants + p.seniors > MAX_PASSENGERS)
    return `At most ${MAX_PASSENGERS} passengers in total.`;
  return null;
}

export function validateDate(value: string, today: string): string | null {
  if (!value) return "Choose a date.";
  if (!parseIsoDate(value)) return "Enter a valid date.";
  if (value < today) return "This date has passed.";
  return null;
}

/** Both times are required, and earliest ≤ latest (the inversion is reported on `latest`). */
export function validateWindow(earliest: string, latest: string): Pick<DraftErrors, "earliest" | "latest"> {
  const errors: Pick<DraftErrors, "earliest" | "latest"> = {};
  const from = parseTimeOfDay(earliest);
  const to = parseTimeOfDay(latest);
  if (from === null) errors.earliest = earliest ? "Enter a valid time." : "Enter the earliest departure time.";
  if (to === null) errors.latest = latest ? "Enter a valid time." : "Enter the latest departure time.";
  else if (from !== null && from > to) errors.latest = "Must be at or after the earliest time.";
  return errors;
}

/** Everything wrong with the draft; empty when it can be saved. */
export function validateDraft(draft: TripDraft, today: string): DraftErrors {
  const errors: DraftErrors = { ...validateWindow(draft.earliest, draft.latest) };
  if (!draft.from) errors.from = "Choose where you're leaving from.";
  if (!draft.to) errors.to = "Choose a destination.";
  else if (draft.to === draft.from) errors.to = "Choose a different destination.";
  const date = validateDate(draft.date, today);
  if (date) errors.date = date;
  const passengers = validatePassengers(draft);
  if (passengers) errors.passengers = passengers;
  if (draft.vehicle === "tall") {
    const height = validateHeight(draft.height);
    if (height) errors.height = height;
  }
  return errors;
}

/**
 * The settings a valid draft describes. The height only means something for a tall vehicle;
 * otherwise `base`'s height is kept, so it's never sent as a change.
 */
export function settingsFromDraft(draft: TripDraft, base: TripSettings): TripSettings {
  const { height, ...rest } = draft;
  const parsed = draft.vehicle === "tall" ? parseHeight(height) : null;
  return { ...rest, height_ft: parsed ?? base.height_ft };
}

function setField<K extends keyof TripSettings>(target: Partial<TripSettings>, key: K, value: TripSettings[K]): void {
  target[key] = value;
}

/** The PATCH body: only the fields that differ from `base`. */
export function changedSettings(base: TripSettings, next: TripSettings): Partial<TripSettings> {
  const patch: Partial<TripSettings> = {};
  for (const key of TRIP_SETTINGS_FIELDS) {
    if (next[key] !== base[key]) setField(patch, key, next[key]);
  }
  return patch;
}

const FIELD_FOR_KEY: Partial<Record<string, DraftField>> = {
  from: "from",
  to: "to",
  date: "date",
  adults: "passengers",
  children: "passengers",
  infants: "passengers",
  seniors: "passengers",
  height: "height",
  height_ft: "height",
  vehicle: "height",
  earliest: "earliest",
  latest: "latest",
};

/** The form field a draft key (or a server error's `field`) belongs to, if any. */
export function draftFieldFor(key: string | null): DraftField | null {
  return key === null ? null : (FIELD_FOR_KEY[key] ?? null);
}

/** For a 400 that names a field this form shows: that field. Such errors are shown inline, not as a toast. */
export function inlineErrorField(error: unknown): DraftField | null {
  return error instanceof ApiError && error.status === 400 ? draftFieldFor(error.field) : null;
}
