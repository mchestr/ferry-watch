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
  if (b.phase === "queued") {
    return { text: `Waiting for a free browser${b.queue_position ? ` (#${b.queue_position})` : ""}`, tone: "soft" };
  }
  if (b.phase === "starting" || b.phase === "creating") return { text: "Starting", tone: "soft" };
  if (b.consecutive_errors >= 3) return { text: "Having trouble checking", tone: "danger" };
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

/** Whole minutes from now until `deadline` (epoch seconds), never negative. */
export function minutesLeft(deadline: number | null, now: number = Date.now()): number | null {
  return deadline === null ? null : Math.max(0, Math.round((deadline * 1000 - now) / 60_000));
}
