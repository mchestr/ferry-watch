/**
 * ferry-web's admin API (docs/multi-tenant/design-reference.md §9.4). Every change carries a reason, which
 * lands in the audit log. Admins see people, limits and the fleet; never a tenant's screen or sailings.
 */
import { z } from "zod";
import { request } from "@/lib/api";

const epoch = z.number().finite();
const OkSchema = z.object({}).passthrough();

export const AdminLimitSchema = z.object({ value: z.number().int(), source: z.string() });

export const OverviewSchema = z.object({
  pods: z.array(z.object({ mode: z.string(), phase: z.string(), count: z.number().int() })),
  queued: z.number().int(),
  holds: z.array(
    z.object({
      user: z.string(),
      trip: z.string(),
      since: epoch,
      // The cart summary only.
      cart: z
        .object({
          status: z.string().optional(),
          depart: z.string().optional(),
          hard_deadline: epoch.nullable().optional(),
        })
        .nullable(),
    }),
  ),
  switches: z.record(z.string(), z.unknown()),
  outcomes_7d: z.record(z.string(), z.number().int()),
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

export type Overview = z.infer<typeof OverviewSchema>;
export type AdminUser = z.infer<typeof AdminUserSchema>;
export type Invite = z.infer<typeof InviteSchema>;
export type People = z.infer<typeof PeopleSchema>;
export type Unmatched = z.infer<typeof UnmatchedSchema>;
export type FleetSwitch = "maintenance" | "pause_carting";

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

const userPath = (id: string) => `/api/admin/users/${encodeURIComponent(id)}`;

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
  setState: (id: string, state: "active" | "suspended", reason: string) =>
    request(`${userPath(id)}/state`, { method: "POST", body: { state, reason }, schema: OkSchema }),
  /** `null` goes back to the default. The answer is what applies, never past the ceiling. */
  setLimit: (id: string, key: string, value: number | null, reason: string) =>
    request(`${userPath(id)}/limits`, { method: "POST", body: { key, value, reason }, schema: AdminLimitSchema }),
  setSwitch: (name: FleetSwitch, on: boolean, reason: string) =>
    request("/api/admin/switches", { method: "POST", body: { switch: name, on, reason }, schema: OkSchema }),
};
