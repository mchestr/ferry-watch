/**
 * Waitlist's state: every watch on every provider (docs/waitlist/design.md §6, §7.6). Verbatim from the design
 * contract; extend it, never loosen it. A watch that doesn't parse (a provider this build doesn't know, or a shape
 * it doesn't expect) becomes an "unknown" watch rather than failing the whole state: a newer server, or one bad
 * row, can never blank the page.
 */
import { z } from "zod";
import { MyStateSchema, MyTripSchema } from "./schema";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
export const WATCH_ID = /^[a-f0-9]{16}$/;
export const PROVIDER_ID = /^[a-z][a-z0-9_]{1,31}$/;
const OPTION_KEY = /^[a-z0-9][a-z0-9_.:-]{0,31}$/;
const epoch = z.number().finite();

/** A status the server reports: one this build doesn't know reads as "unknown". */
function reported<const T extends string>(values: readonly [T, ...T[]]) {
  return z.enum([...values, "unknown"] as [T | "unknown", ...(T | "unknown")[]]).catch("unknown");
}

export const CapabilitiesSchema = z.object({
  auto_hold: z.boolean(),
  checkout: z.boolean(),
  alert_only: z.boolean(),
});

/** One entry of the provider catalog (GET /api/providers, and `providers` in every state). */
export const ProviderInfoSchema = z.object({
  id: z.string().regex(PROVIDER_ID),
  name: z.string().min(1),
  noun: z.string().min(1),
  description: z.string(),
  checker: reported(["browser", "http"]),
  capabilities: CapabilitiesSchema,
  horizon_days: z.number().int().positive(),
  offered: z.boolean(), // this user may start a new watch on it ("New watch" lists it)
  paused: z.boolean().default(false), // an admin paused its checks (or maintenance is on)
});

/** A BC Ferries trip: 0.7.1's trip entry exactly (trip, rev, paused_reason, quiet, hold, browser, booking_url),
 * plus the fields every watch has. `trip.id` and `trip.enabled` equal `id` and `enabled`. */
export const BcferriesWatchSchema = MyTripSchema.extend({
  provider: z.literal("bcferries"),
  id: z.string().regex(WATCH_ID),
  enabled: z.boolean(),
}).transform((w) => ({ ...w, kind: "bcferries" as const }));

export const OptionSchema = z.object({
  key: z.string().regex(OPTION_KEY),
  label: z.string().min(1).max(64),
  status: reported(["open", "maybe", "sold_out", "not_released", "not_offered", "over", "unchecked"]),
  wanted: z.boolean(),
  booking_url: z.string().url().startsWith("https://").nullable().catch(null).default(null),
  detail: z.string().max(64).nullable().catch(null).default(null),
});

/** Where an HTTP-checked watch's checking is, as the server worked it out (base.CheckState). */
export const CheckSchema = z.object({
  state: reported([
    "pending",
    "waiting_release",
    "checking",
    "error",
    "backoff",
    "quiet",
    "paused",
    "provider_paused",
    "unavailable",
    "over",
  ]),
  opens_at: epoch.nullable(),
  closes_at: epoch.nullable(),
  last_check_at: epoch.nullable(),
  checked_at: epoch.nullable(),
  next_check_at: epoch.nullable(),
  outcome: reported(["pending", "ok", "no_inventory", "error"]),
  error_class: z.string().nullable(),
  consecutive_errors: z.number().int().nonnegative(),
});

const common = {
  id: z.string().regex(WATCH_ID),
  rev: z.number().int(),
  enabled: z.boolean(),
  paused_reason: z.string().nullable(),
  quiet: z.boolean().default(false), // in the user's quiet hours and not lifted: not being checked
};

export const BUNTZEN_PASSES = ["full_day", "morning", "afternoon"] as const;
export type BuntzenPass = (typeof BUNTZEN_PASSES)[number];

export const BuntzenSpecSchema = z.object({
  date: z.string().regex(ISO_DATE),
  passes: z.array(z.enum(BUNTZEN_PASSES)).min(1).max(3),
  release_reminder: z.boolean(),
});

/** Only ever a Buntzen page on Yodel's portal: anything else reads as no link. */
export const BUNTZEN_BOOKING_PREFIX = "https://yodelportal.com/buntzen-lake/";
const buntzenUrl = z.string().url().startsWith(BUNTZEN_BOOKING_PREFIX).nullable().catch(null).default(null);

export const BuntzenWatchSchema = z
  .object({
    provider: z.literal("buntzen"),
    ...common,
    spec: BuntzenSpecSchema,
    check: CheckSchema,
    options: z.array(OptionSchema.extend({ booking_url: buntzenUrl })).max(20),
    booking_url: buntzenUrl,
  })
  .transform((w) => ({ ...w, kind: "buntzen" as const }));

/** What's left of a watch this build can't show: enough to list it and remove it. */
export const UnknownWatchSchema = z
  .object({
    provider: z.string().max(32),
    id: z.string().regex(WATCH_ID),
    rev: z.number().int().catch(0),
    enabled: z.boolean().catch(true),
  })
  .transform((w) => ({ ...w, kind: "unknown" as const }));

export type BcferriesWatch = z.output<typeof BcferriesWatchSchema>;
export type BuntzenWatch = z.output<typeof BuntzenWatchSchema>;
export type UnknownWatch = z.output<typeof UnknownWatchSchema>;
export type Watch = BcferriesWatch | BuntzenWatch | UnknownWatch;
export type WatchOption = z.output<typeof OptionSchema>;
export type WatchCheck = z.output<typeof CheckSchema>;
export type ProviderInfo = z.output<typeof ProviderInfoSchema>;

const KNOWN = new Map<string, z.ZodType<Watch, z.ZodTypeDef, unknown>>([
  ["bcferries", BcferriesWatchSchema],
  ["buntzen", BuntzenWatchSchema],
]);

/** One watch from the server: its provider's schema, else the unknown fallback, else null (dropped). */
export function parseWatch(raw: unknown): Watch | null {
  const provider = typeof raw === "object" && raw !== null ? (raw as { provider?: unknown }).provider : undefined;
  const schema = typeof provider === "string" ? KNOWN.get(provider) : undefined;
  if (schema) {
    const parsed = schema.safeParse(raw);
    if (parsed.success) return parsed.data;
  }
  const fallback = UnknownWatchSchema.safeParse(raw);
  return fallback.success ? fallback.data : null;
}

export const WatchListSchema = z
  .array(z.unknown())
  .transform((items) => items.map(parseWatch).filter((w): w is Watch => w !== null));

const ProviderCapacitySchema = z.object({
  can_add: z.boolean(),
  reason: z.string().nullable(),
  limit: z.number().int().nonnegative(),
  count: z.number().int().nonnegative(),
});

export const ProviderListSchema = z
  .array(ProviderInfoSchema.nullable().catch(null))
  .transform((items) => items.filter((p): p is ProviderInfo => p !== null));

export const WaitlistStateSchema = MyStateSchema.omit({ trips: true, capacity: true }).extend({
  watches: WatchListSchema,
  providers: ProviderListSchema,
  capacity: z.object({
    can_add: z.boolean(), // at least one offered provider has room
    reason: z.string().nullable(),
    providers: z.record(z.string(), ProviderCapacitySchema).default({}),
  }),
});

export type WaitlistState = z.output<typeof WaitlistStateSchema>;

export const WaitlistLiveMessageSchema = z.object({ type: z.literal("state"), state: WaitlistStateSchema });

/** GET /api/providers. */
export const ProvidersResponseSchema = z.object({ providers: ProviderListSchema });

/** POST /api/watches: the new watch's id, and the caller's whole state. */
export const CreatedWatchSchema = z.object({ watch_id: z.string().regex(WATCH_ID), state: WaitlistStateSchema });

/** The bodies the SPA sends (§6.2). */
export interface NewWatchRequest {
  provider: string;
  spec: Record<string, unknown>;
  enabled?: boolean;
}
export interface WatchPatchRequest {
  spec?: Record<string, unknown>;
  enabled?: boolean;
}
