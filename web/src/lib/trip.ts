import type { CartState, Sailing, Trip, TripSettings } from "./schema";
import { timeLabel } from "./time";
import { plural } from "./utils";

type Passengers = Pick<TripSettings, "adults" | "children" | "infants" | "seniors">;

/** "2 adults, 1 child" */
export function paxLabel(p: Passengers): string {
  const parts = [
    p.adults ? plural(p.adults, "adult") : null,
    p.children ? plural(p.children, "child", "children") : null,
    p.infants ? plural(p.infants, "infant") : null,
    p.seniors ? plural(p.seniors, "senior") : null,
  ].filter((part) => part !== null);
  return parts.join(", ") || "No passengers";
}

/** A cart being filled or waiting for checkout. A failed one holds nothing. */
export function heldCart(trip: Pick<Trip, "cart">): CartState | null {
  return trip.cart && trip.cart.status !== "failed" ? trip.cart : null;
}

/** Whether the watcher checks this trip each round. */
export function isActive(trip: Trip, watching: boolean): boolean {
  return watching && trip.enabled && !trip.past && heldCart(trip) === null;
}

/** In the departure window and open for booking. */
export function isBookable(sailing: Sailing): boolean {
  return sailing.in_window && sailing.status === "available";
}

export function isAnyTime(t: Pick<TripSettings, "earliest" | "latest">): boolean {
  return t.earliest === "00:00" && t.latest === "23:59";
}

/** "Any time" or "7:00 am – 9:00 pm" */
export function windowLabel(t: Pick<TripSettings, "earliest" | "latest">): string {
  return isAnyTime(t) ? "Any time" : `${timeLabel(t.earliest)} – ${timeLabel(t.latest)}`;
}

/** What removing the trip does, for the confirmation. Names the held sailing, if there is one. */
export function removeConsequence(trip: Pick<Trip, "cart">): string {
  const cart = heldCart(trip);
  if (cart?.status === "ready") return `Removing it releases the ${timeLabel(cart.depart)} sailing in your cart.`;
  if (cart?.status === "adding") return `Removing it stops adding the ${timeLabel(cart.depart)} sailing to your cart.`;
  return "You'll stop getting alerts for it.";
}

export type TripStatus =
  /** The last check failed. `resultsFrom` is when the (stale) sailings shown were fetched. */
  | { kind: "error"; message: string; resultsFrom: number | null; retryAt: number | null }
  | { kind: "past" }
  | { kind: "paused"; scope: "trip" | "all" }
  | { kind: "held"; cart: CartState }
  /** Never checked successfully, and a check is running now. */
  | { kind: "checking" }
  /** Never checked successfully. */
  | { kind: "waiting" }
  | { kind: "bookable"; sailings: Sailing[] }
  | { kind: "sold_out"; inWindow: Sailing[]; total: number };

/**
 * The one thing the trip's status panel shows, in priority order: error, past, paused (the trip,
 * then everything), cart held, no results yet, bookable, sold out.
 */
export function tripStatus(trip: Trip, watching: boolean): TripStatus {
  if (trip.error !== null) {
    const stale = trip.sailings.length > 0 ? trip.checked_at : null;
    return { kind: "error", message: trip.error, resultsFrom: stale, retryAt: trip.retry_at };
  }
  if (trip.past) return { kind: "past" };
  if (!trip.enabled) return { kind: "paused", scope: "trip" };
  if (!watching) return { kind: "paused", scope: "all" };
  const cart = heldCart(trip);
  if (cart) return { kind: "held", cart };
  if (trip.checked_at === null) return trip.checking ? { kind: "checking" } : { kind: "waiting" };
  const bookable = trip.sailings.filter(isBookable);
  if (bookable.length) return { kind: "bookable", sailings: bookable };
  return { kind: "sold_out", inWindow: trip.sailings.filter((s) => s.in_window), total: trip.sailings.length };
}

/** "7:00 am ($93.45), 9:15 am, 11:00 am +2 more" */
export function bookableSummary(sailings: Sailing[], max = 3): string {
  const shown = sailings
    .slice(0, max)
    .map((s) => (s.price ? `${timeLabel(s.depart)} (${s.price})` : timeLabel(s.depart)));
  const more = sailings.length - shown.length;
  return shown.join(", ") + (more > 0 ? ` +${more} more` : "");
}

/** Why nothing is bookable, without claiming "0 sailings ... all sold out". */
export function soldOutSummary(inWindow: Sailing[], total: number): string {
  if (total === 0) return "BC Ferries shows no sailings on this date.";
  if (inWindow.length === 0) {
    return total === 1
      ? "The only sailing that day is outside your departure window."
      : `None of the ${total} sailings that day are in your departure window.`;
  }
  const soldOut = inWindow.filter((s) => s.status === "sold_out").length;
  const unavailable = inWindow.length - soldOut;
  if (inWindow.length === 1) {
    return `The one sailing in your window is ${soldOut ? "sold out" : "unavailable"}.`;
  }
  if (unavailable === 0) return `All ${inWindow.length} sailings in your window are sold out.`;
  if (soldOut === 0) return `All ${inWindow.length} sailings in your window are unavailable.`;
  return `${inWindow.length} sailings in your window: ${soldOut} sold out, ${unavailable} unavailable.`;
}
