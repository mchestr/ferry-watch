/**
 * The dashboard's model of the user's watches (docs/waitlist/design.md §7.2-7.3): which section each is in and in
 * what order, the "Next" hint, which providers a new watch can be on, and the mutations every card shares.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext } from "react";
import { toast } from "sonner";
import { reportError } from "@/lib/hooks";
import type { Group, ProviderUi } from "@/providers/types";
import { MY_STATE_KEY } from "./api";
import type { ProviderInfo, WaitlistState, Watch } from "./watch-schema";

export const SECTIONS: readonly { group: Group; title: string; collapsed: boolean }[] = [
  { group: "now", title: "Needs you now", collapsed: false },
  { group: "watching", title: "Watching", collapsed: false },
  { group: "paused", title: "Paused", collapsed: true },
  { group: "ended", title: "Ended", collapsed: true },
];

/** A watch's provider UI (providers/registry's `uiFor`), passed in: null for an unknown watch. */
export type UiLookup = (watch: Watch) => ProviderUi | null;

/** Unknown watches have no date: they sort after every dated one. */
const NO_DATE = "9999-12-31";

/**
 * A provider's answer about one watch, or `otherwise` if it throws. These run while the dashboard renders, outside
 * every card's error boundary: one odd watch must cost only its own card (which then says it can't show it), never
 * the page.
 */
function soft<T>(ask: () => T, otherwise: T): T {
  try {
    return ask();
  } catch {
    return otherwise;
  }
}

/** Every watch in its section (an unknown one is Watching), each section by date, then in the server's order. */
export function sections(watches: readonly Watch[], now: number, lookup: UiLookup): Record<Group, Watch[]> {
  const out: Record<Group, Watch[]> = { now: [], watching: [], paused: [], ended: [] };
  watches
    .map((watch, index) => ({ watch, index, ui: lookup(watch) }))
    .map((entry) => ({ ...entry, date: soft(() => entry.ui?.date(entry.watch), undefined) ?? NO_DATE }))
    .sort((a, b) => (a.date === b.date ? a.index - b.index : a.date < b.date ? -1 : 1))
    .forEach(({ watch, ui }) => out[soft(() => ui?.group(watch, now), undefined) ?? "watching"].push(watch));
  return out;
}

/** The soonest thing that will happen among the watches being watched, for the line under the heading. */
export function nextHint(watching: readonly Watch[], now: number, lookup: UiLookup): string | null {
  let best: { at: number; text: string } | null = null;
  for (const watch of watching) {
    const next = soft(() => lookup(watch)?.next(watch, now) ?? null, null);
    if (next && next.at >= now && (best === null || next.at < best.at)) best = next;
  }
  return best ? `Next: ${best.text}` : null;
}

export function infoFor(state: WaitlistState, provider: string): ProviderInfo | undefined {
  return state.providers.find((p) => p.id === provider);
}

/**
 * The providers a new watch can be on: offered to this user and known to this build (`known`: providers/registry
 * has it), last used first.
 */
export function pickable(state: WaitlistState, known: (provider: string) => boolean): ProviderInfo[] {
  const offered = state.providers.filter((p) => p.offered && known(p.id));
  const last = lastProvider();
  return [...offered.filter((p) => p.id === last), ...offered.filter((p) => p.id !== last)];
}

/**
 * What a provider is: the first sentence of its catalog description. The rest says what it does, which the tile's
 * tagline chip already says ("Sailing reservations. Can hold a sailing in a cart for you to check out." shows
 * "Sailing reservations."). A sentence ends at ".", "!" or "?" after a lowercase letter, a digit or ")", before a
 * capital, so "B.C. Hydro" stays whole.
 */
export function whatItIs(description: string): string {
  return /^(.*?[a-z0-9)][.!?])\s+(?=[A-Z])/.exec(description)?.[1] ?? description;
}

/** Whether this user may add a watch on `provider` now, and if not, why. */
export function capacityFor(state: WaitlistState, provider: string): { canAdd: boolean; reason: string | null } {
  const room = state.capacity.providers[provider];
  return room ? { canAdd: room.can_add, reason: room.reason } : { canAdd: true, reason: null };
}

// ---- per device, in localStorage (every access may throw: private mode, blocked storage)

const LAST_PROVIDER_KEY = "wl.lastProvider";
const BANNER_DISMISSED_KEY = "wl.alertsBannerDismissed";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Unavailable: it lasts as long as the page.
  }
}

export const lastProvider = (): string | null => read(LAST_PROVIDER_KEY);
export const rememberProvider = (id: string): void => write(LAST_PROVIDER_KEY, id);
export const alertsBannerDismissed = (): boolean => read(BANNER_DISMISSED_KEY) === "1";
export const dismissAlertsBanner = (): void => write(BANNER_DISMISSED_KEY, "1");

// ---- the New watch sheet, which any card or page can open

export interface WatchSheetApi {
  /** A new watch: on `provider` (its form), or the picker for null. `seed` prefills the form. */
  add: (provider: string | null, seed?: Record<string, unknown>) => void;
  edit: (watch: Watch) => void;
}

export const WatchSheetContext = createContext<WatchSheetApi>({ add: () => undefined, edit: () => undefined });

export const useWatchSheet = (): WatchSheetApi => useContext(WatchSheetContext);

// ---- mutations

/** A call that answers with the whole state: kept as the app's state, with an optional toast. */
export function useStateMutation<A>(fn: (arg: A) => Promise<WaitlistState>, success?: string | ((arg: A) => string)) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (state, arg) => {
      qc.setQueryData(MY_STATE_KEY, state);
      if (success) toast.success(typeof success === "string" ? success : success(arg));
    },
    onError: (error) => reportError(error),
  });
}
