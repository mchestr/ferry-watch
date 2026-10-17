import type { Sailing } from "./schema";
import { timeLabel } from "./time";

/** In the departure window and open for booking. */
export function isBookable(sailing: Sailing): boolean {
  return sailing.in_window && sailing.status === "available";
}

/** A cart, as much of it as removing a trip needs to describe. */
export interface CartSummary {
  depart: string;
  status: string;
}

/** What removing the trip does, for the confirmation. Names the held sailing, if there is one. */
export function removeConsequence(trip: { cart: CartSummary | null }): string {
  const cart = trip.cart && trip.cart.status !== "failed" ? trip.cart : null;
  if (cart?.status === "ready") return `Removing it releases the ${timeLabel(cart.depart)} sailing in your cart.`;
  if (cart?.status === "adding") return `Removing it stops adding the ${timeLabel(cart.depart)} sailing to your cart.`;
  return "You'll stop getting alerts for it.";
}
