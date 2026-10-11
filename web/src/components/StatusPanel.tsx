import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, PauseCircle, Radar } from "lucide-react";
import type { State } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LinkButton } from "./ui/button";

export function StatusPanel({ state }: { state: State }) {
  const { config: c, sailings, checking, last_error } = state;
  const inWindow = sailings.filter((s) => s.in_window !== false);
  const bookable = inWindow.filter((s) => s.status === "available");

  let tone: "ok" | "bad" | "idle" | "live" = "live";
  let icon = <Radar />;
  let title = "No reservations available yet";
  let sub = `${inWindow.length} sailing${inWindow.length === 1 ? "" : "s"} in your window, all sold out. You'll be alerted the moment one opens.`;

  if (bookable.length) {
    tone = "ok";
    icon = <CheckCircle2 />;
    title = `${bookable.length} sailing${bookable.length > 1 ? "s" : ""} bookable now`;
    sub = bookable.map((s) => (s.price ? `${s.depart} (${s.price})` : s.depart)).join(", ");
  } else if (!sailings.length && checking) {
    tone = "idle";
    icon = <Loader2 className="animate-spin" />;
    title = "Checking BC Ferries…";
    sub = "Walking through the booking flow, this takes about 10 seconds.";
  } else if (!sailings.length && last_error) {
    tone = "bad";
    icon = <AlertTriangle />;
    title = "Last check failed";
    sub = "Retrying automatically. Details are in the activity feed.";
  } else if (!c.enabled) {
    tone = "idle";
    icon = <PauseCircle />;
    title = "Watching is paused";
    sub = "Turn watching back on to resume checks and alerts.";
  } else if (!sailings.length) {
    tone = "idle";
    title = "Waiting for the first check";
    sub = "";
  }

  return (
    <div
      className={cn(
        "flex flex-col gap-4 px-5 py-5 sm:flex-row sm:items-center sm:justify-between sm:px-7",
        tone === "ok" && "bg-success-soft",
        tone === "bad" && "bg-danger-soft",
      )}
    >
      <div className="flex min-w-0 items-start gap-3.5">
        <div
          className={cn(
            "grid size-10 shrink-0 place-items-center rounded-xl [&_svg]:size-5",
            tone === "ok" && "bg-success text-success-foreground",
            tone === "bad" && "bg-danger text-danger-foreground",
            tone === "live" && "bg-accent text-accent-foreground",
            tone === "idle" && "bg-muted text-muted-foreground",
          )}
        >
          {icon}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 font-semibold">
            {title}
            {tone === "live" && c.enabled && <span className="size-2 rounded-full bg-success animate-pulse-ring" aria-label="watching" />}
          </div>
          {sub && <p className="mt-0.5 text-sm text-muted-foreground tabular">{sub}</p>}
        </div>
      </div>
      {tone === "ok" && (
        <LinkButton variant="success" size="lg" href={state.booking_link} target="_blank" rel="noopener" className="w-full sm:w-auto">
          Book on BC Ferries <ExternalLink />
        </LinkButton>
      )}
    </div>
  );
}
