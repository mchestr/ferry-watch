/**
 * What the dashboard needs from each provider's UI (docs/waitlist/design.md §7.5): how a watch reads (title, date,
 * section, badge, "Next" hint) and its card, page and form. `now` and `at` are epoch milliseconds.
 */
import type { ComponentType } from "react";
import type { LucideIcon } from "lucide-react";
import type { Terminal } from "@/lib/schema";
import type { Tone } from "@/mt/trips";
import type { ProviderInfo, Watch, WaitlistState } from "@/mt/watch-schema";

export type Group = "now" | "watching" | "paused" | "ended";
export type KnownKind = Exclude<Watch["kind"], "unknown">;

export interface WatchProps<W extends Watch> {
  watch: W;
  state: WaitlistState;
  info: ProviderInfo | undefined;
}

export type SheetMode<W extends Watch> =
  | { kind: "adding"; seed?: Record<string, unknown> }
  | { kind: "editing"; watch: W };

export interface SheetProps<W extends Watch> {
  mode: SheetMode<W>;
  state: WaitlistState;
  info: ProviderInfo;
  onDone: (watchId: string | null) => void; // null: cancelled
}

export interface ProviderUi<W extends Watch = Watch> {
  kind: KnownKind;
  icon: LucideIcon;
  tagline: string;
  /** The empty state's button for this provider: "Watch a ferry". */
  cta: string;
  /** The watch in a line, as its card and alerts name it: "Swartz Bay → Tsawwassen, Fri Oct 23". */
  title(watch: W, ctx: { terminals?: readonly Terminal[] }): string;
  date(watch: W): string; // ISO date, for sorting
  group(watch: W, now: number): Group;
  badge(watch: W, now: number): { text: string; tone: Tone };
  next(watch: W, now: number): { at: number; text: string } | null; // for the dashboard's "Next" hint
  Card: ComponentType<WatchProps<W>>;
  Detail: ComponentType<WatchProps<W>>;
  Sheet: ComponentType<SheetProps<W>>;
}
