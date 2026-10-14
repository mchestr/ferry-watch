/** How a trip reads on the dashboard (docs/multi-tenant/design-reference.md §10, item 5). */
import type { Sailing, Terminal } from "@/lib/schema";
import { clock, everyLabel, longDate } from "@/lib/time";
import type { MyTrip, SailingInfo } from "./schema";

export type Tone = "success" | "warning" | "danger" | "soft" | "outline";

export interface TripBadge {
  text: string;
  tone: Tone;
}

/** "Tsawwassen → Swartz Bay", or the codes until the terminal list has loaded. */
export function routeTitle(trip: MyTrip, terminals: readonly Terminal[] | undefined): string {
  const name = (code: string) => terminals?.find((t) => t.code === code)?.label ?? code;
  return `${name(trip.trip.from)} → ${name(trip.trip.to)}`;
}

export const tripTitle = (trip: MyTrip, terminals: readonly Terminal[] | undefined): string =>
  `${routeTitle(trip, terminals)}, ${longDate(trip.trip.date) ?? trip.trip.date}`;

/** The plain-words status of a trip's browser. */
export function tripBadge(trip: MyTrip): TripBadge {
  const b = trip.browser;
  if (trip.paused_reason === "after_checkout") return { text: "Did you book it?", tone: "warning" };
  if (!trip.trip.enabled) return { text: "Paused", tone: "outline" };
  if (b.cart?.status === "ready") return { text: "Holding a cart", tone: "success" };
  if (b.cart?.status === "adding") return { text: "Holding it…", tone: "success" };
  if (b.phase === "queued") {
    return { text: `Waiting for a free browser${b.queue_position ? ` (#${b.queue_position})` : ""}`, tone: "soft" };
  }
  if (b.consecutive_errors >= 3) return { text: "Having trouble checking", tone: "danger" };
  if (unheld(trip).length > 0) return { text: "Bookable now", tone: "warning" };
  if (b.phase === "starting" || b.phase === "creating") return { text: "Starting", tone: "soft" };
  if (b.mode === "warm") {
    return { text: b.effective_interval ? `Every ${everyLabel(b.effective_interval)}` : "Watching", tone: "success" };
  }
  if (b.mode === "short_check" || b.next_check_at !== null) {
    const next = b.next_check_at ? ` · next ${clock(b.next_check_at * 1000)}` : "";
    return { text: `Checked every 6 h${next}`, tone: "soft" };
  }
  return { text: "Checking soon", tone: "soft" };
}

/** The agent's sailings, as the shared sailings list shows them. */
export function asSailings(trip: MyTrip): Sailing[] {
  const { earliest, latest } = trip.trip;
  return trip.browser.sailings.map((s: SailingInfo) => {
    const prices = s.fares.filter((f) => f.selectable && f.price).map((f) => f.price ?? "");
    return {
      depart: s.depart,
      arrive: s.arrive,
      vessel: s.vessel,
      status: s.status,
      note: "",
      price: prices[0] ?? null,
      in_window: earliest <= s.depart && s.depart <= latest,
    };
  });
}

/** The departures ("HH:MM") the trip's last check found open inside its times. */
export function bookableInWindow(trip: MyTrip): string[] {
  const { earliest, latest } = trip.trip;
  return trip.browser.sailings
    .filter((s) => s.status === "available" && earliest <= s.depart && s.depart <= latest)
    .map((s) => s.depart);
}

/** A bookable sailing that nothing is holding (or trying to): what the "not held" panel is for. */
export function unheld(trip: MyTrip): string[] {
  const cart = trip.browser.cart;
  if (!trip.trip.enabled || trip.paused_reason !== null || trip.hold.reason === "paused") return [];
  if (cart && (cart.status === "ready" || cart.status === "adding")) return [];
  return bookableInWindow(trip);
}

const NOT_HELD: Record<string, string> = {
  quiet_hours: "We don't hold carts overnight.",
  auto_cart_off: "Auto-hold is off for this trip.",
  holding_elsewhere: "You're already holding a cart on another trip.",
  no_holds_left: "You've used today's holds.",
  limit: "You've reached today's limit for holding carts.",
  carting_paused: "Holding carts is paused right now.",
  already_held: "It was held for you once already.",
};

// A check that ends this soon after "Hold it for me" may have started before it, so it doesn't count.
const REQUEST_SLACK_S = 15;

/** "Hold it for me" was asked and is still on its way: no check since has had the chance to act on it. */
export function holdPending(trip: MyTrip, now: number = Date.now()): boolean {
  const { requested_at: askedAt, requested_until: until } = trip.hold;
  if (askedAt === null || until === null || until * 1000 <= now) return false;
  const cart = trip.browser.cart;
  if (cart && (cart.status === "ready" || cart.status === "adding")) return false;
  return (trip.browser.checked_at ?? 0) <= askedAt + REQUEST_SLACK_S;
}

/** Why nothing is holding the trip's bookable sailings, in plain words. */
export function notHeldReason(trip: MyTrip): string {
  const cart = trip.browser.cart;
  if (cart?.status === "failed") return "We tried to hold it, but BC Ferries didn't let us.";
  const reason = trip.hold.reason;
  return reason ? (NOT_HELD[reason] ?? "We couldn't hold it.") : "We couldn't hold it this time.";
}

/** Whole minutes from now until `deadline` (epoch seconds), never negative. */
export function minutesLeft(deadline: number | null, now: number = Date.now()): number | null {
  return deadline === null ? null : Math.max(0, Math.round((deadline * 1000 - now) / 60_000));
}
