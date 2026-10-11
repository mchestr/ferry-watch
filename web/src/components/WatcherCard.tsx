import {
  AlertTriangle,
  Bell,
  BellOff,
  Check,
  Globe,
  KeyRound,
  Loader2,
  MonitorPlay,
  RefreshCw,
  Send,
  X,
} from "lucide-react";
import type * as React from "react";
import { api } from "@/lib/api";
import { useNow, useStateMutation } from "@/lib/hooks";
import type { State } from "@/lib/schema";
import { ago, clock, countdown, everyLabel, serverNow, serverToClientMs } from "@/lib/time";
import { isActive } from "@/lib/trip";
import { cn, plural } from "@/lib/utils";
import { ringFraction, watcherPhase, type WatcherPhase } from "@/lib/watcher";
import { Button, LinkButton } from "./ui/button";
import { Card, CardHeader, CardTitle } from "./ui/card";
import { Field } from "./ui/controls";
import { Select } from "./ui/select";

const INTERVAL_CHOICES = [15, 30, 60, 120, 300];

function Ring({
  fraction,
  tone,
  children,
}: {
  fraction: number;
  tone: "primary" | "warning" | "danger";
  children: React.ReactNode;
}) {
  const r = 34;
  const c = 2 * Math.PI * r;
  return (
    <div className="relative grid size-[84px] shrink-0 place-items-center">
      <svg className="absolute inset-0 -rotate-90" viewBox="0 0 84 84" aria-hidden>
        <circle cx="42" cy="42" r={r} fill="none" strokeWidth="6" className="stroke-muted" />
        <circle
          cx="42"
          cy="42"
          r={r}
          fill="none"
          strokeWidth="6"
          strokeLinecap="round"
          className={cn(
            "motion-safe:transition-[stroke-dashoffset] motion-safe:duration-1000 motion-safe:ease-linear",
            tone === "primary" && "stroke-primary",
            tone === "warning" && "stroke-warning",
            tone === "danger" && "stroke-danger",
          )}
          strokeDasharray={c}
          strokeDashoffset={c * (1 - fraction)}
        />
      </svg>
      {children}
    </div>
  );
}

function phaseView(phase: WatcherPhase, skew: number): { center: React.ReactNode; line: string; detail?: string } {
  const at = (serverSeconds: number) => clock(serverToClientMs(serverSeconds, skew));
  const paused = <span className="text-xs font-semibold text-muted-foreground">Paused</span>;
  const spinner = <Loader2 className="size-6 text-primary motion-safe:animate-spin" aria-hidden />;
  switch (phase.kind) {
    case "stalled":
      return {
        center: <AlertTriangle className="size-6 text-danger" aria-hidden />,
        line: "Watcher stalled",
        detail: `No heartbeat since ${at(phase.heartbeatAt)}. Restart Ferry Watch if this lasts.`,
      };
    case "off":
      return { center: paused, line: "Watching is off" };
    case "idle":
      return { center: paused, line: phase.reason === "held" ? "Waiting on held carts" : "No trips being watched" };
    case "checking":
      return { center: spinner, line: "Checking now" };
    case "starting":
      return { center: spinner, line: "Starting up" };
    case "due":
      return { center: spinner, line: "Checking…" };
    case "overdue":
      return {
        center: <span className="text-xs font-semibold text-warning">Overdue</span>,
        line: "Next check is overdue",
        detail: `It was due at ${at(phase.at)}.`,
      };
    case "countdown":
      return {
        center: (
          <div className="text-center leading-none">
            <div className="tabular text-lg font-bold">{countdown(phase.remaining)}</div>
            <div className="mt-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">next</div>
          </div>
        ),
        line: `Next check at ${at(phase.at)}`,
      };
  }
}

/** The countdown ring and its caption. Ticks every second, so it's kept apart from the rest of the card. */
function NextCheck({ state, skew, activeCount }: { state: State; skew: number; activeCount: number }) {
  const now = useNow(1000);
  const phase = watcherPhase(state, serverNow(skew, now));
  const view = phaseView(phase, skew);
  const lastChecked = Math.max(0, ...state.trips.map((t) => t.checked_at ?? 0));
  const tone = phase.kind === "stalled" ? "danger" : phase.kind === "overdue" ? "warning" : "primary";
  return (
    <div className="flex items-center gap-4 px-5 pb-4">
      <Ring fraction={ringFraction(phase, state.interval_seconds)} tone={tone}>
        {view.center}
      </Ring>
      <div className="min-w-0 space-y-1 text-sm">
        <div className={cn("font-medium", phase.kind === "stalled" && "text-danger")}>{view.line}</div>
        {view.detail && <div className="text-muted-foreground">{view.detail}</div>}
        <div className="text-muted-foreground">
          {activeCount} of {plural(state.trips.length, "trip")} active
        </div>
        <div className="text-muted-foreground">
          {lastChecked ? `Updated ${ago(serverToClientMs(lastChecked, skew), now)}` : "Not checked yet"}
        </div>
      </div>
    </div>
  );
}

function Row({ ok, icon, label, detail }: { ok: boolean; icon: React.ReactNode; label: string; detail: string }) {
  return (
    <li className="flex items-center gap-3 py-2.5">
      <div
        aria-hidden
        className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground [&_svg]:size-4"
      >
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{label}</div>
        <div className="truncate text-xs text-muted-foreground">{detail}</div>
      </div>
      <span
        role="img"
        aria-label={ok ? "Ready" : "Not set up"}
        className={cn(
          "grid size-5 place-items-center rounded-full [&_svg]:size-3",
          ok ? "bg-success text-success-foreground" : "bg-muted text-muted-foreground",
        )}
      >
        {ok ? <Check strokeWidth={3} aria-hidden /> : <X strokeWidth={3} aria-hidden />}
      </span>
    </li>
  );
}

export function WatcherCard({ state, skew }: { state: State; skew: number }) {
  const check = useStateMutation(() => api.checkNow(), { success: "Checking now…" });
  const test = useStateMutation(() => api.testAlert(), { success: "Test alert sent" });
  const interval = useStateMutation((seconds: number) => api.updateSettings({ interval_seconds: seconds }), {
    success: (seconds) => `Checking every ${everyLabel(seconds)}`,
  });

  const active = state.trips.filter((t) => isActive(t, state.enabled));
  const checking = state.trips.some((t) => t.checking);
  const sessions = state.trips.filter((t) => t.session !== null).length;
  const intervals = [...new Set([...INTERVAL_CHOICES, state.interval_seconds])].sort((a, b) => a - b);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Watcher</CardTitle>
        <Button
          variant="outline"
          size="sm"
          onClick={() => check.mutate()}
          disabled={checking || check.isPending || !active.length || !state.watcher.healthy}
        >
          <RefreshCw className={cn(checking && "motion-safe:animate-spin")} aria-hidden /> Check now
        </Button>
      </CardHeader>
      <NextCheck state={state} skew={skew} activeCount={active.length} />
      <div className="border-t px-5 py-3">
        <Field label="Check every" hint="Each trip reloads its open sailings page, about 3 seconds per trip.">
          <Select
            value={String(state.interval_seconds)}
            onValueChange={(value) => interval.mutate(Number(value))}
            disabled={interval.isPending}
            options={intervals.map((seconds) => ({
              value: String(seconds),
              label: everyLabel(seconds),
              hint: seconds < 30 ? "Fastest, but busier on BC Ferries' site" : undefined,
            }))}
          />
        </Field>
      </div>
      <div className="border-t px-5 py-2">
        <ul className="divide-y divide-border/70">
          <Row
            ok={sessions > 0}
            icon={<Globe />}
            label="Booking sessions"
            detail={sessions ? `${sessions} open in the live browser` : "Opened on the next check"}
          />
          <Row
            ok={state.alerts_configured}
            icon={state.alerts_configured ? <Bell /> : <BellOff />}
            label="Pushover alerts"
            detail={state.alerts_configured ? "Phone alerts with a booking link" : "Not configured"}
          />
          <Row
            ok={state.login_configured}
            icon={<KeyRound />}
            label="BC Ferries login"
            detail={state.login_configured ? "Used only for auto-cart" : "Not configured, so auto-cart is off"}
          />
        </ul>
      </div>
      <div className="flex gap-2 border-t px-5 py-3">
        {state.checkout_url && (
          <LinkButton variant="ghost" size="sm" className="flex-1" href={state.checkout_url} target="_blank">
            <MonitorPlay aria-hidden /> Live browser
          </LinkButton>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="flex-1"
          onClick={() => test.mutate()}
          disabled={test.isPending || !state.alerts_configured}
        >
          <Send aria-hidden /> Test alert
        </Button>
      </div>
    </Card>
  );
}
