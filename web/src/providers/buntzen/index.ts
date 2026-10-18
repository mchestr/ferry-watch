/** Buntzen Lake in the SPA (docs/waitlist/design.md §7.3-7.5): an alert-only provider of day-use parking passes. */
import { Trees } from "lucide-react";
import type { BuntzenWatch } from "@/mt/watch-schema";
import type { ProviderUi } from "../types";
import { BuntzenCard } from "./BuntzenCard";
import { BuntzenDetail } from "./BuntzenDetail";
import { BuntzenSheet } from "./BuntzenSheet";
import { badge, bookable, dayLabel, opensAt, pacificClock, passesPhrase, relativeDay } from "./labels";

export const buntzenUi: ProviderUi<BuntzenWatch> = {
  kind: "buntzen",
  icon: Trees,
  tagline: "Alert only: you book on Yodel",
  cta: "Watch Buntzen Lake",
  title: (watch) => `Buntzen Lake, ${dayLabel(watch.spec.date)}: ${passesPhrase(watch.spec.passes)}`,
  date: (watch) => watch.spec.date,
  group: (watch, now) => {
    if (bookable(watch, now).length > 0) return "now";
    if (watch.check.state === "over") return "ended";
    if (watch.check.state === "paused" || !watch.enabled) return "paused";
    return "watching";
  },
  badge: (watch, now) => {
    const { text, tone } = badge(watch, now);
    return { text, tone };
  },
  next: (watch, now) => {
    if (watch.check.state !== "waiting_release") return null;
    const at = opensAt(watch);
    return {
      at,
      text: `Buntzen Lake passes for ${dayLabel(watch.spec.date)} go on sale ${relativeDay(at, now)} at ${pacificClock(at)}`,
    };
  },
  Card: BuntzenCard,
  Detail: BuntzenDetail,
  Sheet: BuntzenSheet,
};
