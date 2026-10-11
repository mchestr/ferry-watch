/**
 * Runtime schemas for everything the server sends (docs/api.md). Every HTTP response and WebSocket
 * message is parsed with these before the app sees it. Unknown keys are stripped, so the server can
 * add fields without breaking an open page.
 */
import { z } from "zod";

/** "HH:MM", 24-hour. */
const TIME_OF_DAY = /^([01]\d|2[0-3]):[0-5]\d$/;
/** "YYYY-MM-DD". Whether the date exists on the calendar is the form's concern, not the wire format's. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** True for absolute http(s) URLs and for relative ones (which resolve against this page). */
export function isHttpUrl(value: string): boolean {
  try {
    const { protocol } = new URL(value, "http://relative.invalid");
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

const timeOfDay = z.string().regex(TIME_OF_DAY, "expected HH:MM");
const isoDate = z.string().regex(ISO_DATE, "expected YYYY-MM-DD");
/** Epoch seconds. */
const epoch = z.number().finite();
const count = z.number().int().nonnegative();
/** Rendered as a link, so `javascript:` and friends never get through. */
const httpUrl = z.string().min(1).refine(isHttpUrl, "expected an http(s) URL");

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
  status: z.enum(["available", "sold_out", "unavailable"]),
  note: z.string(),
  price: z.string().nullable(),
  in_window: z.boolean(),
});

export const CartStateSchema = z.object({
  id: z.string().min(1),
  status: z.enum(["adding", "ready", "failed"]),
  depart: timeOfDay,
  since: epoch,
  fare: z.string().nullable(),
  price: z.string().nullable(),
  error: z.string().nullable(),
  expires_at: epoch.nullable(), // a ready cart is handed back at this time (null: held until released)
});

export const TripSchema = TripSettingsSchema.extend({
  id: z.string().min(1),
  title: z.string(),
  route: z.object({ from_place: z.string(), from_area: z.string(), to_place: z.string(), to_area: z.string() }),
  past: z.boolean(),
  sailings: z.array(SailingSchema),
  checked_at: epoch.nullable(),
  checking: z.boolean(),
  error: z.string().nullable(),
  error_at: epoch.nullable(),
  retry_at: epoch.nullable(),
  session: z.object({ opened_at: epoch, logged_in: z.boolean() }).nullable(),
  cart: CartStateSchema.nullable(),
  booking_url: httpUrl.nullable(),
});

export const ActivityEventSchema = z.object({
  id: z.number().int(),
  ts: epoch,
  kind: z.enum(["info", "alert", "cart", "error"]),
  text: z.string(),
  trip_id: z.string().nullable(),
  trip_title: z.string().nullable(),
});

export const StateSchema = z.object({
  boot_id: z.string().min(1),
  seq: z.number().int(),
  server_time: epoch,
  enabled: z.boolean(),
  interval_seconds: z.number().int().positive(),
  next_check_at: epoch.nullable(),
  max_trips: count,
  trip_defaults: TripSettingsSchema,
  trips: z.array(TripSchema),
  events: z.array(ActivityEventSchema),
  checkout_url: httpUrl.nullable(),
  login_configured: z.boolean(),
  alerts_configured: z.boolean(),
  watcher: z.object({ heartbeat_at: epoch, healthy: z.boolean() }),
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

export const WsMessageSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("state"), data: StateSchema }),
  z.object({ type: z.literal("ping"), server_time: epoch }),
]);

export type Vehicle = z.infer<typeof VehicleSchema>;
export type Fare = z.infer<typeof FareSchema>;
export type TripSettings = z.infer<typeof TripSettingsSchema>;
export type Sailing = z.infer<typeof SailingSchema>;
export type CartState = z.infer<typeof CartStateSchema>;
export type Trip = z.infer<typeof TripSchema>;
export type ActivityEvent = z.infer<typeof ActivityEventSchema>;
export type State = z.infer<typeof StateSchema>;
export type Terminal = z.infer<typeof TerminalSchema>;
export type WsMessage = z.infer<typeof WsMessageSchema>;
