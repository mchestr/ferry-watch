/** BC Ferries in the SPA (docs/waitlist/design.md §7.4-7.5): its trips read and behave exactly as before. */
import { Ship } from "lucide-react";
import { clock } from "@/lib/time";
import { nextCheck, shortDate, tripBadge, tripGroup } from "@/mt/trips";
import type { BcferriesWatch } from "@/mt/watch-schema";
import type { ProviderUi } from "../types";
import { BcferriesCard } from "./BcferriesCard";
import { BcferriesDetail } from "./BcferriesDetail";
import { BcferriesSheet } from "./BcferriesSheet";

export const bcferriesUi: ProviderUi<BcferriesWatch> = {
  kind: "bcferries",
  icon: Ship,
  tagline: "Can hold a sailing in a cart for you",
  cta: "Watch a ferry",
  title: (watch, { terminals }) => {
    const name = (code: string) => terminals?.find((t) => t.code === code)?.label ?? code;
    return `${name(watch.trip.from)} → ${name(watch.trip.to)}, ${shortDate(watch.trip.date)}`;
  },
  date: (watch) => watch.trip.date,
  group: (watch, now) => tripGroup(watch, now),
  badge: (watch) => tripBadge(watch),
  next: (watch) => {
    const at = nextCheck(watch);
    return at === null
      ? null
      : { at: at * 1000, text: `BC Ferries check for ${shortDate(watch.trip.date)} at ${clock(at * 1000)}` };
  },
  Card: BcferriesCard,
  Detail: BcferriesDetail,
  Sheet: BcferriesSheet,
};
