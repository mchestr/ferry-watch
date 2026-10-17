/**
 * Runtime schemas shared by the app: a trip's settings, sailings, BC Ferries' terminals and error bodies.
 * ferry-web's own responses are in mt/schema.ts, built on these. Every response is parsed before the app sees
 * it. Unknown keys are stripped, so the server can add fields without breaking an open page.
 */
import { z } from "zod";

/** "HH:MM", 24-hour. */
const TIME_OF_DAY = /^([01]\d|2[0-3]):[0-5]\d$/;
/** "YYYY-MM-DD". Whether the date exists on the calendar is the form's concern, not the wire format's. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

const timeOfDay = z.string().regex(TIME_OF_DAY, "expected HH:MM");
const isoDate = z.string().regex(ISO_DATE, "expected YYYY-MM-DD");
const count = z.number().int().nonnegative();

/**
 * A status the server reports. A value this build doesn't know reads as "unknown" instead of failing the
 * whole message, so a newer server (a new sailing status, cart state or event kind) can't blank an open
 * page. Enums the page sends back (vehicle, fare) stay strict.
 */
function reported<const T extends string>(values: readonly [T, ...T[]]) {
  return z.enum([...values, "unknown"] as [T | "unknown", ...(T | "unknown")[]]).catch("unknown");
}

export const VehicleSchema = z.enum(["standard", "tall", "none"]);
export const FareSchema = z.enum(["cheapest", "saver", "prepaid", "reservation"]);

export const TripSettingsSchema = z.object({
  enabled: z.boolean(),
  from: z.string(),
  to: z.string(),
  date: isoDate,
  adults: count,
  children: count,
  infants: count,
  seniors: count,
  vehicle: VehicleSchema,
  height_ft: z.number().finite(),
  earliest: timeOfDay,
  latest: timeOfDay,
  auto_cart: z.boolean(),
  fare: FareSchema,
});

/** The keys of TripSettings, in a fixed order. */
export const TRIP_SETTINGS_FIELDS = TripSettingsSchema.keyof().options;

export const SailingSchema = z.object({
  depart: timeOfDay,
  arrive: timeOfDay,
  vessel: z.string(),
  status: reported(["available", "sold_out", "unavailable"]),
  note: z.string(),
  price: z.string().nullable(),
  in_window: z.boolean(),
});

export const TerminalSchema = z.object({
  code: z.string().min(1),
  label: z.string(),
  place: z.string(),
  area: z.string(),
  dests: z.array(z.string()),
});

export const TerminalListSchema = z.object({ terminals: z.array(TerminalSchema) });

/** Error responses. Lenient on purpose: anything without a usable `error` falls back to "HTTP <status>". */
export const ErrorBodySchema = z.object({ error: z.string().min(1), field: z.string().nullish() });

export type Vehicle = z.infer<typeof VehicleSchema>;
export type Fare = z.infer<typeof FareSchema>;
export type TripSettings = z.infer<typeof TripSettingsSchema>;
export type Sailing = z.infer<typeof SailingSchema>;
export type Terminal = z.infer<typeof TerminalSchema>;
