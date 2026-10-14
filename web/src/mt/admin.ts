/**
 * ferry-web's admin API (docs/multi-tenant/design-reference.md §9.4). Every change carries a reason, which
 * lands in the audit log. Admins see people, limits and the fleet; never a tenant's screen or sailings.
 */
import { z } from "zod";
import { request } from "@/lib/api";
import { plural } from "@/lib/utils";

const epoch = z.number().finite();
const OkSchema = z.object({}).passthrough();

export const AdminLimitSchema = z.object({ value: z.number().int(), source: z.string() });

/**
 * What ferry-control's last fleet pass knew (fleet_state "fleet_report"). It reports only what it computes,
 * so every part may be missing.
 */
export const FleetReportSchema = z.object({
  at: epoch.nullish(),
  browsers: z.object({ max: z.number().int().nullish(), live: z.number().int().nullish() }).nullish(),
  // `pod` is the chart's podMemory, a Kubernetes quantity such as "1Gi"; `budget_mb` is that times the max.
  memory: z
    .object({ pod: z.string().nullish(), requested_mb: z.number().nullish(), budget_mb: z.number().nullish() })
    .nullish(),
  queued: z.number().int().nullish(),
  short_checks: z.object({ due: z.number().int().nullish(), overdue: z.number().int().nullish() }).nullish(),
  budget: z
    .object({
      checks_per_minute: z.number().nullish(),
      demand_per_minute: z.number().nullish(),
      stretch: z.number().nullish(),
    })
    .nullish(),
  brake: z.object({ multiplier: z.number().nullish(), paused: z.boolean().nullish() }).nullish(),
  breaker: z.string().nullish(),
});

export const AttentionSchema = z.object({
  kind: z.string(),
  trip: z.string().nullable(),
  user: z.string().nullable(),
  at: epoch,
  // Shown as written when it's text; anything else is dropped.
  detail: z.string().nullish().catch(null),
});

/** Which release runs where. Each part may be missing: from an older server, or before ferry-control's first pass. */
export const VersionsSchema = z.object({
  web: z.object({ version: z.string() }).nullish().catch(null),
  control: z.object({ version: z.string(), at: epoch.nullish() }).nullish().catch(null),
  // Live browser pods by agent version and image; a pod that hasn't said hello yet has no version.
  browsers: z
    .array(z.object({ version: z.string().nullish(), image: z.string().nullish(), pods: z.number().int() }))
    .catch([]),
});

export const OverviewSchema = z.object({
  pods: z.array(z.object({ mode: z.string(), phase: z.string(), count: z.number().int() })),
  queued: z.number().int(),
  holds: z.array(
    z.object({
      user: z.string(),
      trip: z.string(),
      // The browser holding it, named in the recycle dialog.
      pod: z.string().optional(),
      since: epoch,
      // The cart summary only.
      cart: z
        .object({
          status: z.string().optional(),
          depart: z.string().optional(),
          hard_deadline: epoch.nullable().optional(),
          // They're on BC Ferries' payment page, so the cart can't be released.
          payment_in_progress: z.boolean().optional(),
        })
        .nullable(),
    }),
  ),
  switches: z.record(z.string(), z.unknown()),
  outcomes_7d: z.record(z.string(), z.number().int()),
  // A report this page can't read shows as missing rather than breaking the page.
  fleet: FleetReportSchema.nullish().catch(null),
  /** When ferry-control last said it was alive (epoch seconds, its clock), or null if it never has. */
  control_seen: epoch.nullish(),
  /** Newest first: ids and usernames only, never sailings. */
  attention: z.array(AttentionSchema).optional(),
  versions: VersionsSchema.nullish().catch(null),
});

export const AdminUserSchema = z.object({
  id: z.string(),
  username: z.string(),
  email: z.string().nullable(),
  state: z.string(),
  is_admin: z.boolean(),
  paused: z.boolean(),
  trips: z.number().int(),
  created_at: epoch,
  last_seen_at: epoch.nullable(),
  limits: z.record(z.string(), AdminLimitSchema),
  /** Live sessions: not revoked, not expired. */
  sessions: z.number().int().optional(),
  /** Devices with alerts on (push subscriptions). */
  devices: z.number().int().optional(),
  /** Today's usage (Pacific), by usage_ledger kind. */
  usage_today: z.record(z.string(), z.number()).optional(),
  /** An admin by the config (bootstrapAdmins), so the admin flag can't be removed here. */
  bootstrap: z.boolean().optional(),
  /** Emails their sign-ins carried that differ from the one on file. */
  emails_seen: z.array(z.string()).optional(),
});

export const InviteSchema = z.object({
  id: z.string(),
  username: z.string(),
  email: z.string().nullable(),
  reason: z.string(),
  expires_at: epoch,
});

export const PeopleSchema = z.object({ users: z.array(AdminUserSchema), invites: z.array(InviteSchema) });

export const UnmatchedSchema = z.array(
  z.object({ issuer: z.string(), username: z.string().nullable(), email: z.string().nullable(), last_seen: epoch }),
);

export type FleetReport = z.infer<typeof FleetReportSchema>;
export type Attention = z.infer<typeof AttentionSchema>;
export type Overview = z.infer<typeof OverviewSchema>;
export type Versions = z.infer<typeof VersionsSchema>;
export type AdminUser = z.infer<typeof AdminUserSchema>;
export type Invite = z.infer<typeof InviteSchema>;
export type People = z.infer<typeof PeopleSchema>;
export type Unmatched = z.infer<typeof UnmatchedSchema>;
export type FleetSwitch = "maintenance" | "pause_carting";
/** When suspending someone holding a cart: release it now, or let the hold run its course. */
export type HoldsChoice = "release" | "finish";

export const ADMIN_KEYS = {
  overview: ["admin", "overview"],
  people: ["admin", "people"],
  unmatched: ["admin", "unmatched"],
} as const;

/** Each limit, in the words the People page uses. */
export const LIMIT_LABELS: Readonly<Record<string, string>> = {
  maxTrips: "Trips watched",
  maxActiveBrowsers: "Browsers at once",
  maxHoldsPerDay: "Holds a day",
  maxCartAttemptsPerDay: "Cart attempts a day",
  vncMinutesPerDay: "Checkout minutes a day",
  manualChecksPerHour: "Check-nows an hour",
  testAlertsPerHour: "Test alerts an hour",
  mintsPerMinute: "Checkout links a minute",
};

/** Today's usage, in the same words. */
export const USAGE_LABELS: Readonly<Record<string, (n: number) => string>> = {
  holds: (n) => plural(n, "hold"),
  cart_attempts: (n) => plural(n, "cart attempt"),
  vnc_seconds: (n) => `${Math.ceil(n / 60)} min at checkout`,
  manual_checks: (n) => plural(n, "check-now", "check-nows"),
  test_alerts: (n) => plural(n, "test alert"),
  mints: (n) => plural(n, "checkout link"),
  pod_starts: (n) => plural(n, "browser start"),
};

const QUANTITY = /^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?$/;
const UNIT_BYTES: Readonly<Record<string, number>> = {
  "": 1,
  Ki: 2 ** 10,
  Mi: 2 ** 20,
  Gi: 2 ** 30,
  Ti: 2 ** 40,
  k: 1e3,
  M: 1e6,
  G: 1e9,
  T: 1e12,
};

/** A Kubernetes memory quantity ("1Gi", "1536Mi", "2G") in GiB; null if it isn't one. */
export function quantityGiB(value: string): number | null {
  const match = QUANTITY.exec(value.trim());
  if (!match) return null;
  const [, amount = "", unit = ""] = match;
  const bytes = UNIT_BYTES[unit];
  return bytes === undefined ? null : (Number(amount) * bytes) / 2 ** 30;
}

/** The browser pods by version, most first: "0.6.3 ×2, 0.6.2 ×1". Empty when none run. */
export function browserVersions(browsers: Versions["browsers"]): string {
  const pods = new Map<string, number>();
  for (const b of browsers) {
    const version = b.version ?? "unknown";
    pods.set(version, (pods.get(version) ?? 0) + b.pods);
  }
  return [...pods]
    .filter(([, n]) => n > 0)
    .sort(([, a], [, b]) => b - a)
    .map(([version, n]) => `${version} ×${n}`)
    .join(", ");
}

const userPath = (id: string) => `/api/admin/users/${encodeURIComponent(id)}`;
const tripPath = (id: string) => `/api/admin/trips/${encodeURIComponent(id)}`;

export const adminApi = {
  overview: (signal?: AbortSignal): Promise<Overview> =>
    request("/api/admin/overview", { schema: OverviewSchema, signal }),
  people: (signal?: AbortSignal): Promise<People> => request("/api/admin/users", { schema: PeopleSchema, signal }),
  unmatched: (signal?: AbortSignal): Promise<Unmatched> =>
    request("/api/admin/unmatched", { schema: UnmatchedSchema, signal }),
  invite: (invite: { username: string; email: string | null; reason: string }) =>
    request("/api/admin/invites", {
      method: "POST",
      body: invite.email ? invite : { username: invite.username, reason: invite.reason },
      schema: z.object({ id: z.string() }),
    }),
  revokeInvite: (id: string, reason: string) =>
    request(`/api/admin/invites/${encodeURIComponent(id)}`, { method: "DELETE", body: { reason }, schema: OkSchema }),
  /** `holds` says what happens to a held cart when suspending; the server lets it finish unless told. */
  setState: (id: string, state: "active" | "suspended", reason: string, holds?: HoldsChoice) =>
    request(`${userPath(id)}/state`, {
      method: "POST",
      body: holds ? { state, reason, holds } : { state, reason },
      schema: OkSchema,
    }),
  revokeSessions: (id: string, reason: string) =>
    request(`${userPath(id)}/revoke-sessions`, {
      method: "POST",
      body: { reason },
      schema: z.object({ revoked: z.number().int() }),
    }),
  /** Never for yourself, and bootstrap admins can't be demoted (the server refuses both). */
  setAdmin: (id: string, isAdmin: boolean, reason: string) =>
    request(userPath(id), {
      method: "PATCH",
      body: { is_admin: isAdmin, reason },
      schema: z.object({ is_admin: z.boolean() }),
    }),
  /** Only once they're suspended. Keeps the audit log and hold history. */
  deleteUser: (id: string, reason: string) =>
    request(userPath(id), { method: "DELETE", body: { reason }, schema: OkSchema }),
  /** `null` goes back to the default. The answer is what applies, never past the ceiling. */
  setLimit: (id: string, key: string, value: number | null, reason: string) =>
    request(`${userPath(id)}/limits`, { method: "POST", body: { key, value, reason }, schema: AdminLimitSchema }),
  setSwitch: (name: FleetSwitch, on: boolean, reason: string) =>
    request("/api/admin/switches", { method: "POST", body: { switch: name, on, reason }, schema: OkSchema }),
  /** Only ferry-control opens the breaker; an admin can only close it. */
  closeBreaker: (reason: string) =>
    request("/api/admin/switches", {
      method: "POST",
      body: { switch: "breaker", on: false, reason },
      schema: OkSchema,
    }),
  /** Empties the trip's held cart and tells its owner. */
  releaseCart: (trip: string, reason: string) =>
    request(`${tripPath(trip)}/release`, { method: "POST", body: { reason }, schema: OkSchema }),
  /** Restarts the trip's browser; ferry-control starts the next one. */
  recyclePod: (trip: string, reason: string) =>
    request(`${tripPath(trip)}/recycle`, { method: "POST", body: { reason }, schema: OkSchema }),
};
