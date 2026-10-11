import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, PauseCircle, Radar } from "lucide-react";
import type { Trip } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LinkButton } from "./ui/button";

export function StatusPanel({ trip, watching }: { trip: Trip; watching: boolean }) {
  const { sailings, checking, last_error } = trip;
  const inWindow = sailings.filter((s) => s.in_window !== false);
  const bookable = inWindow.filter((s) => s.status === "available");
  const active = watching && trip.enabled;

  let tone: "ok" | "bad" | "idle" | "live" = "live";
  let icon = <Radar />;
  let title = "No reservations available yet";
  let sub = `${inWindow.length} sailing${inWindow.length === 1 ? "" : "s"} in your window, all sold out.`;

  if (bookable.length) {
    tone = "ok";
    icon = <CheckCircle2 />;
    title = `${bookable.length} sailing${bookable.length > 1 ? "s" : ""} bookable now`;
    const shown = bookable.slice(0, 3).map((s) => (s.price ? `${s.depart} (${s.price})` : s.depart));
    sub = shown.join(", ") + (bookable.length > 3 ? ` +${bookable.length - 3} more` : "");
  } else if (!active) {
    tone = "idle";
    icon = <PauseCircle />;
    title = trip.enabled ? "Watching is paused" : "This trip is paused";
    sub = trip.enabled ? "Turn watching back on to resume checks." : "Turn it back on to resume checks and alerts.";
  } else if (!sailings.length && checking) {
    tone = "idle";
    icon = <Loader2 className="animate-spin" />;
    title = "Opening a booking session…";
    sub = "Walking through the BC Ferries booking form, this takes up to a minute.";
  } else if (!sailings.length && last_error) {
    tone = "bad";
    icon = <AlertTriangle />;
    title = "Last check failed";
    sub = last_error;
  } else if (!sailings.length) {
    tone = "idle";
    title = "Waiting for the first check";
    sub = "";
  }

  return (
    <div
      className={cn(
        "flex flex-col gap-4 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6",
        tone === "ok" && "bg-success-soft",
        tone === "bad" && "bg-danger-soft",
      )}
    >
      <div className="flex min-w-0 items-start gap-3.5">
        <div
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-xl [&_svg]:size-[18px]",
            tone === "ok" && "bg-success text-success-foreground",
            tone === "bad" && "bg-danger text-danger-foreground",
            tone === "live" && "bg-accent text-accent-foreground",
            tone === "idle" && "bg-muted text-muted-foreground",
          )}
        >
          {icon}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[15px] font-semibold">
            {title}
            {tone === "live" && <span className="size-2 rounded-full bg-success animate-pulse-ring" aria-label="watching" />}
          </div>
          {sub && <p className="mt-0.5 break-words text-sm text-muted-foreground tabular">{sub}</p>}
        </div>
      </div>
      {tone === "ok" && (
        <LinkButton variant="success" href={trip.booking_link} target="_blank" rel="noopener" className="w-full sm:w-auto">
          Book on BC Ferries <ExternalLink />
        </LinkButton>
      )}
    </div>
  );
}
