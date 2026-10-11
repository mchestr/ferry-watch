import {
  AlertTriangle,
  CalendarX2,
  CheckCircle2,
  ExternalLink,
  Loader2,
  PauseCircle,
  Radar,
  ShoppingCart,
  Trash2,
} from "lucide-react";
import type * as React from "react";
import type { Trip } from "@/lib/schema";
import { clock, serverToClientMs, timeLabel } from "@/lib/time";
import { bookableSummary, soldOutSummary, tripStatus } from "@/lib/trip";
import { cn, plural } from "@/lib/utils";
import { Button, LinkButton } from "./ui/button";

type Tone = "ok" | "bad" | "idle" | "live";

interface View {
  tone: Tone;
  icon: React.ReactNode;
  title: string;
  lines: string[];
}

function describe(trip: Trip, watching: boolean, skew: number): View & { kind: ReturnType<typeof tripStatus>["kind"] } {
  const status = tripStatus(trip, watching);
  const at = (serverSeconds: number) => clock(serverToClientMs(serverSeconds, skew));
  switch (status.kind) {
    case "error": {
      const lines = [status.message];
      if (status.resultsFrom !== null) lines.push(`Showing results from ${at(status.resultsFrom)}.`);
      if (status.retryAt !== null && serverToClientMs(status.retryAt, skew) > Date.now()) {
        lines.push(`Trying again at ${at(status.retryAt)}.`);
      }
      return { kind: status.kind, tone: "bad", icon: <AlertTriangle />, title: "Last check failed", lines };
    }
    case "past":
      return {
        kind: status.kind,
        tone: "idle",
        icon: <CalendarX2 />,
        title: "This date has passed",
        lines: ["Past dates aren't checked any more. Remove the trip, or pause it to keep it around."],
      };
    case "paused":
      return status.scope === "trip"
        ? {
            kind: status.kind,
            tone: "idle",
            icon: <PauseCircle />,
            title: "This trip is paused",
            lines: ["Turn it back on to resume checks and alerts."],
          }
        : {
            kind: status.kind,
            tone: "idle",
            icon: <PauseCircle />,
            title: "Watching is paused",
            lines: ["Turn watching back on to resume checks."],
          };
    case "held":
      return status.cart.status === "ready"
        ? {
            kind: status.kind,
            tone: "idle",
            icon: <ShoppingCart />,
            title: "Checks paused while your cart is held",
            lines: [`The ${timeLabel(status.cart.depart)} sailing is in your cart. Release it to resume watching.`],
          }
        : {
            kind: status.kind,
            tone: "idle",
            icon: <ShoppingCart />,
            title: "A sailing opened up",
            lines: [`Adding the ${timeLabel(status.cart.depart)} sailing to your cart.`],
          };
    case "checking":
      return {
        kind: status.kind,
        tone: "idle",
        icon: <Loader2 className="motion-safe:animate-spin" />,
        title: "Opening a booking session…",
        lines: ["Walking through the BC Ferries booking form. This takes up to a minute."],
      };
    case "waiting":
      return { kind: status.kind, tone: "idle", icon: <Radar />, title: "Waiting for the first check", lines: [] };
    case "bookable":
      return {
        kind: status.kind,
        tone: "ok",
        icon: <CheckCircle2 />,
        title: `${plural(status.sailings.length, "sailing")} bookable now`,
        lines: [bookableSummary(status.sailings)],
      };
    case "sold_out":
      return {
        kind: status.kind,
        tone: "live",
        icon: <Radar />,
        title: status.total ? "No reservations available yet" : "No sailings found",
        lines: [soldOutSummary(status.inWindow, status.total)],
      };
  }
}

export function StatusPanel({
  trip,
  watching,
  skew,
  onPause,
  pausePending,
  onRemove,
}: {
  trip: Trip;
  watching: boolean;
  skew: number;
  onPause: () => void;
  pausePending: boolean;
  onRemove: () => void;
}) {
  const view = describe(trip, watching, skew);
  return (
    <div
      className={cn(
        "flex flex-col gap-4 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6",
        view.tone === "ok" && "bg-success-soft",
        view.tone === "bad" && "bg-danger-soft",
      )}
    >
      <div className="flex min-w-0 items-start gap-3.5">
        <div
          aria-hidden
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-xl [&_svg]:size-[18px]",
            view.tone === "ok" && "bg-success text-success-foreground",
            view.tone === "bad" && "bg-danger text-danger-foreground",
            view.tone === "live" && "bg-accent text-accent-foreground",
            view.tone === "idle" && "bg-muted text-muted-foreground",
          )}
        >
          {view.icon}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[15px] font-semibold">
            {view.title}
            {view.tone === "live" && (
              <span
                role="img"
                aria-label="Watching"
                className="size-2 shrink-0 rounded-full bg-success motion-safe:animate-pulse-ring"
              />
            )}
          </div>
          {view.lines.map((line) => (
            <p key={line} className="tabular mt-0.5 break-words text-sm text-muted-foreground">
              {line}
            </p>
          ))}
        </div>
      </div>
      {view.kind === "bookable" && trip.booking_url && (
        <LinkButton variant="success" href={trip.booking_url} target="_blank" className="w-full sm:w-auto">
          Book on BC Ferries <ExternalLink aria-hidden />
        </LinkButton>
      )}
      {view.kind === "past" && (
        <div className="flex w-full gap-2 sm:w-auto">
          {trip.enabled && (
            <Button variant="outline" className="flex-1 sm:flex-none" onClick={onPause} disabled={pausePending}>
              <PauseCircle aria-hidden /> Pause
            </Button>
          )}
          <Button variant="outline" className="flex-1 sm:flex-none" onClick={onRemove}>
            <Trash2 aria-hidden /> Remove
          </Button>
        </div>
      )}
    </div>
  );
}
