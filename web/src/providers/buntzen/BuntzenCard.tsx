/**
 * A Buntzen Lake watch on the dashboard (docs/waitlist/design.md §7.4). Alert-only: it says what Yodel last showed
 * for each pass, and when one is open, links to Yodel to book it yourself. There is never a cart, a hold or a
 * checkout here. The pieces below are shared with the watch page (BuntzenDetail).
 */
import { CalendarPlus, ExternalLink, Pause, Pencil, Play, RefreshCw, Trees } from "lucide-react";
import { Button, LinkButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Tooltip } from "@/components/ui/tooltip";
import { useNow } from "@/lib/hooks";
import { addDays } from "@/lib/time";
import { cn } from "@/lib/utils";
import { myApi } from "@/mt/api";
import { navigate } from "@/mt/router";
import { ProviderLine, ToneBadge } from "@/mt/WatchCard";
import { useStateMutation, useWatchSheet } from "@/mt/watches";
import type { BuntzenWatch, WaitlistState, WatchOption } from "@/mt/watch-schema";
import { RemoveWatchButton } from "../unknown/UnknownCard";
import type { WatchProps } from "../types";
import { badge, bookable, chip, currentOptions, dayLabel, isPass, notes, passesPhrase, passesTitle } from "./labels";

export const FOOTER = "Alert only. You book on Yodel with your own account; Waitlist never asks for it.";

export function BuntzenCard({ watch, state, info }: WatchProps<BuntzenWatch>) {
  const now = useNow(30_000);
  const status = badge(watch, now);
  return (
    <Card className="space-y-4 p-4 sm:p-5">
      <ProviderLine
        icon={Trees}
        name={info?.name ?? "Buntzen Lake"}
        date={dayLabel(watch.spec.date)}
        watchId={watch.id}
      />
      <div className="flex items-start justify-between gap-3">
        <h2 className="min-w-0 font-semibold">{passesTitle(watch.spec.passes)}</h2>
        <ToneBadge tone={status.tone} pulse={status.pulse}>
          {status.text}
        </ToneBadge>
      </div>
      <PassList watch={watch} now={now} />
      <BookOnYodel watch={watch} now={now} />
      <Notes watch={watch} state={state} now={now} />
      <BuntzenActions watch={watch} />
      <p className="border-t pt-3 text-xs text-muted-foreground">{FOOTER}</p>
    </Card>
  );
}

/** Each pass with what Yodel last showed for it. The ones not watched are shown greyed, for context. */
export function PassList({ watch, now }: { watch: BuntzenWatch; now: number }) {
  const options = currentOptions(watch, now);
  return (
    <ul className="divide-y overflow-hidden rounded-xl border" aria-label="Passes">
      {options.map((option) => (
        <li
          key={option.key}
          className={cn(
            "flex items-center justify-between gap-3 px-3 py-2.5",
            option.wanted && option.status === "open" && "bg-success-soft",
            option.wanted && option.status === "maybe" && "bg-warning-soft",
          )}
        >
          <span className={cn("min-w-0 text-sm", option.wanted ? "font-medium" : "text-muted-foreground")}>
            {option.label}
            {!option.wanted && <span className="whitespace-nowrap"> · not watched</span>}
          </span>
          <OptionChip option={option} outcome={watch.check.outcome} />
        </li>
      ))}
    </ul>
  );
}

function OptionChip({ option, outcome }: { option: WatchOption; outcome: BuntzenWatch["check"]["outcome"] }) {
  const { text, tone } = chip(option, outcome);
  const shown = (
    <ToneBadge tone={option.wanted ? tone : "muted"} className={cn(!option.wanted && "opacity-80")}>
      {text}
    </ToneBadge>
  );
  // What Yodel said that we don't recognise: only here, in a tooltip, escaped like all text.
  if (option.status !== "maybe" || !option.detail) return shown;
  return (
    <Tooltip content={`Yodel says: ${option.detail}`}>
      <button
        type="button"
        className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {shown}
      </button>
    </Tooltip>
  );
}

/**
 * Book on Yodel for each open (or maybe open) pass watched, one button per page, and "I booked it". `full`: full
 * width, as the watch page leads with them.
 */
export function BookOnYodel({ watch, now, full = false }: { watch: BuntzenWatch; now: number; full?: boolean }) {
  const done = useStateMutation(() => myApi.doneWatch(watch.id), "Enjoy the lake.");
  const open = bookable(watch, now);
  if (open.length === 0) return null;
  const pages = new Map<string, WatchOption[]>();
  for (const option of open) {
    if (option.booking_url) pages.set(option.booking_url, [...(pages.get(option.booking_url) ?? []), option]);
  }
  return (
    <div className={cn("flex flex-wrap gap-2", full && "flex-col")}>
      {[...pages].map(([url, options]) => (
        <LinkButton
          key={url}
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          size={full ? "lg" : "default"}
          className={cn(full && "w-full")}
        >
          {pages.size > 1 ? `Book ${passesPhrase(options.map((o) => o.key).filter(isPass))} on Yodel` : "Book on Yodel"}
          <ExternalLink aria-hidden />
        </LinkButton>
      ))}
      <Button
        variant="outline"
        size={full ? "lg" : "default"}
        className={cn(full && "w-full")}
        disabled={done.isPending}
        onClick={() =>
          done.mutate(undefined, {
            onSuccess: () => {
              if (full) navigate("/"); // the watch page has nothing left to show
            },
          })
        }
      >
        I booked it
      </Button>
    </div>
  );
}

/** What the card needs to say beyond the badge (§7.4's notes). */
export function Notes({
  watch,
  state,
  now,
  cadence = true,
}: {
  watch: BuntzenWatch;
  state: WaitlistState;
  now: number;
  /** False where the page says when it was checked elsewhere. */
  cadence?: boolean;
}) {
  const lines = notes(watch, now, state.settings.quiet_hours.end, { cadence });
  if (lines.length === 0) return null;
  return (
    <div className="space-y-1 text-sm text-muted-foreground" role="status">
      {lines.map((line) => (
        <p key={line}>{line}</p>
      ))}
    </div>
  );
}

const CHECKABLE = new Set(["checking", "pending", "error"]);

/** Check now, Pause/Resume, Edit, Remove, and Watch another day once it's over. */
export function BuntzenActions({ watch, onRemoved }: { watch: BuntzenWatch; onRemoved?: () => void }) {
  const sheet = useWatchSheet();
  const check = useStateMutation(() => myApi.checkWatch(watch.id), "Checking now");
  const pause = useStateMutation(
    (enabled: boolean) => myApi.updateWatch(watch.id, { enabled }),
    (enabled) => (enabled ? "Watching again" : "Paused"),
  );
  const over = watch.check.state === "over";
  return (
    <div className="flex flex-wrap gap-2">
      {over ? (
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            sheet.add("buntzen", {
              date: addDays(watch.spec.date, 1),
              passes: watch.spec.passes,
              release_reminder: watch.spec.release_reminder,
            })
          }
        >
          <CalendarPlus aria-hidden /> Watch another day
        </Button>
      ) : (
        <Button
          size="sm"
          variant="outline"
          disabled={check.isPending || !CHECKABLE.has(watch.check.state)}
          onClick={() => check.mutate(undefined)}
        >
          <RefreshCw aria-hidden /> Check now
        </Button>
      )}
      {!over && (
        <Button size="sm" variant="ghost" disabled={pause.isPending} onClick={() => pause.mutate(!watch.enabled)}>
          {watch.enabled ? (
            <>
              <Pause aria-hidden /> Pause
            </>
          ) : (
            <>
              <Play aria-hidden /> Resume
            </>
          )}
        </Button>
      )}
      <Button size="sm" variant="ghost" onClick={() => sheet.edit(watch)}>
        <Pencil aria-hidden /> Edit
      </Button>
      <RemoveWatchButton
        watchId={watch.id}
        title={`Buntzen Lake, ${dayLabel(watch.spec.date)}: ${passesPhrase(watch.spec.passes)}`}
        onRemoved={onRemoved}
      />
    </div>
  );
}
