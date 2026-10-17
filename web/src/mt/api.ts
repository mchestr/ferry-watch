/** waitlist-web's API, for the signed-in user. Every call carries the bearer token (see auth.ts). */
import { z } from "zod";
import { request } from "@/lib/api";
import { TerminalListSchema, type Terminal, type TripSettings } from "@/lib/schema";
import { CheckoutSchema, MyStateSchema, type CheckoutViewer, type MyState, type QuietHours } from "./schema";

export const MY_STATE_KEY = ["my-state"] as const;

const OkSchema = z.object({}).passthrough();
const tripPath = (id: string) => `/api/trips/${encodeURIComponent(id)}`;
const mutate = (method: "POST" | "PATCH" | "DELETE", path: string, body: object = {}): Promise<MyState> =>
  request(path, { method, body, schema: MyStateSchema });

export type BookedReason =
  | "needed_account"
  | "tried_sign_in"
  | "payment_failed"
  | "checkout_broke"
  | "ran_out_of_time"
  | "changed_mind";

/**
 * "Did you book it?": yes ({booked: true}), no and keep watching (with an optional reason), or no and stop
 * watching ({booked: false, stop: true}). Yes and stop both archive the trip.
 */
export type BookedAnswer = { booked: true } | { booked: false; reason?: BookedReason } | { booked: false; stop: true };

/** After "Yes" or "No, cancel" the trip is archived: it's gone from the user's list. */
export const endsTrip = (answer: BookedAnswer): boolean => answer.booked || "stop" in answer;

export const myApi = {
  state: (signal?: AbortSignal): Promise<MyState> => request("/api/state", { schema: MyStateSchema, signal }),
  terminals: async (signal?: AbortSignal): Promise<Terminal[]> =>
    (await request("/api/terminals", { schema: TerminalListSchema, signal })).terminals,
  addTrip: (settings: TripSettings) => mutate("POST", "/api/trips", settings),
  updateTrip: (id: string, patch: Partial<TripSettings>) => mutate("PATCH", tripPath(id), patch),
  removeTrip: (id: string) => mutate("DELETE", tripPath(id)),
  checkNow: (id: string) => mutate("POST", `${tripPath(id)}/check`),
  /** "Hold it for me": check now, and hold a bookable sailing even overnight or with auto-hold off. */
  hold: (id: string) => mutate("POST", `${tripPath(id)}/hold`),
  release: (id: string, cartId: string) => mutate("POST", `${tripPath(id)}/cart/release`, { cart_id: cartId }),
  dismiss: (id: string, cartId: string) => mutate("POST", `${tripPath(id)}/cart/dismiss`, { cart_id: cartId }),
  booked: (id: string, answer: BookedAnswer) => mutate("POST", `${tripPath(id)}/booked`, answer),
  /** The user's quiet hours (Pacific time). Everyone has them, so there's no turning them off. */
  setQuietHours: (quiet: QuietHours) => mutate("PATCH", "/api/me", { quiet_hours: quiet }),
  /** Which viewer opens the user's checkouts; "stream" only while the beta is offered to them. */
  setCheckoutViewer: (viewer: CheckoutViewer) => mutate("PATCH", "/api/me", { checkout_viewer: viewer }),
  /** A one-minute link to this trip's checkout. Not followed automatically: the user chooses the device. */
  checkout: (id: string) => request(`${tripPath(id)}/vnc`, { method: "POST", body: {}, schema: CheckoutSchema }),
  subscribe: (subscription: PushSubscriptionJSON) =>
    request("/api/me/push-subscriptions", { method: "POST", body: subscription, schema: OkSchema }),
  unsubscribe: (endpoint: string) =>
    request("/api/me/push-subscriptions", { method: "DELETE", body: { endpoint }, schema: OkSchema }),
  testAlert: () => request("/api/alerts/test", { method: "POST", body: {}, schema: OkSchema }),
  logout: () => request("/auth/logout", { method: "POST", body: {}, schema: OkSchema }),
};
