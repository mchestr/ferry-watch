/**
 * What ferry-web sends: one user's own state (docs/multi-tenant/design-reference.md §6.3). Parsed before
 * the app sees it; statuses the build doesn't know read as "unknown" rather than blanking the page.
 */
import { z } from "zod";
import { TripSettingsSchema } from "@/lib/schema";

const TIME_OF_DAY = /^([01]\d|2[0-3]):[0-5]\d$/;
const timeOfDay = z.string().regex(TIME_OF_DAY, "expected HH:MM");
const epoch = z.number().finite();

function reported<const T extends string>(values: readonly [T, ...T[]]) {
  return z.enum([...values, "unknown"] as [T | "unknown", ...(T | "unknown")[]]).catch("unknown");
}

export const FareInfoSchema = z.object({ name: z.string(), price: z.string().nullable(), selectable: z.boolean() });

export const SailingInfoSchema = z.object({
  depart: timeOfDay,
  arrive: timeOfDay,
  vessel: z.string(),
  status: reported(["available", "sold_out", "unavailable"]),
  fares: z.array(FareInfoSchema).default([]),
});

export const CartInfoSchema = z.object({
  id: z.string().min(1),
  status: reported(["adding", "ready", "failed"]),
  depart: timeOfDay,
  fare: z.string().nullable().default(null),
  price: z.string().nullable().default(null),
  ready_at: epoch.nullable().default(null),
  unclaimed_deadline: epoch.nullable().default(null),
  session_deadline: epoch.nullable().default(null),
  hard_deadline: epoch.nullable().default(null),
  payment_in_progress: z.boolean().default(false),
});

export const BrowserSchema = z.object({
  phase: reported(["off", "queued", "starting", "watching", "holding", "draining", "creating"]),
  mode: reported(["warm", "short_check", "off"]),
  queue_position: z.number().int().nullable(),
  next_check_at: epoch.nullable(),
  last_check_at: epoch.nullable(),
  checked_at: epoch.nullable(),
  next_due: epoch.nullable(),
  effective_interval: z.number().int().nullable(),
  heartbeat_at: epoch.nullable(),
  sailings: z.array(SailingInfoSchema),
  error_class: z.string().nullable(),
  consecutive_errors: z.number().int(),
  cart: CartInfoSchema.nullable(),
  viewer_connected: z.boolean(),
});

export const MyTripSchema = z.object({
  trip: TripSettingsSchema.extend({ id: z.string().regex(/^[a-f0-9]{16}$/) }),
  rev: z.number().int(),
  paused_reason: z.string().nullable(),
  browser: BrowserSchema,
});

const LimitSchema = z.object({ value: z.number().int(), source: reported(["default", "override", "ceiling"]) });

export const MyStateSchema = z.object({
  me: z.object({
    id: z.string(),
    username: z.string(),
    display: z.string().nullable(),
    is_admin: z.boolean(),
    paused: z.boolean(),
  }),
  limits: z.record(z.string(), LimitSchema),
  usage: z.record(z.string(), z.number()),
  trips: z.array(MyTripSchema),
  capacity: z.object({ can_add: z.boolean(), reason: z.string().nullable() }),
  quiet_hours: z.object({ start: timeOfDay, end: timeOfDay, now: z.boolean() }),
  devices: z.number().int().default(0),
  events: z.array(
    z.object({
      id: z.number().int(),
      trip_id: z.string().nullable(),
      ts: epoch,
      kind: z.string(),
      code: z.string(),
      params: z.record(z.string(), z.unknown()),
    }),
  ),
  links: z.object({ kofi: z.string().url().startsWith("https://ko-fi.com/").nullable() }).default({ kofi: null }),
  push: z.object({ key: z.string().nullable() }).default({ key: null }),
});

export const LiveMessageSchema = z.object({ type: z.literal("state"), state: MyStateSchema });

export const CheckoutSchema = z.object({ url: z.string().url().startsWith("https://"), ttl: z.number() });

export type SailingInfo = z.infer<typeof SailingInfoSchema>;
export type CartInfo = z.infer<typeof CartInfoSchema>;
export type Browser = z.infer<typeof BrowserSchema>;
export type MyTrip = z.infer<typeof MyTripSchema>;
export type MyState = z.infer<typeof MyStateSchema>;
