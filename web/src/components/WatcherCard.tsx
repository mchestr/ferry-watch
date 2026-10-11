import { Bell, BellOff, Check, Globe, KeyRound, Loader2, MonitorPlay, RefreshCw, Send, ShoppingCart, X } from "lucide-react";
import { api, type State } from "@/lib/api";
import { useNow, useStateMutation } from "@/lib/hooks";
import { ago, clock, cn } from "@/lib/utils";
import { everyLabel } from "./TripSheet";
import { Button, LinkButton } from "./ui/button";
import { Card, CardHeader, CardTitle } from "./ui/card";

const FARES: Record<string, string> = { cheapest: "cheapest fare", saver: "Saver", prepaid: "Prepaid", reservation: "Reservation Only" };

function Ring({ fraction, children }: { fraction: number; children: React.ReactNode }) {
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
          className="stroke-primary transition-[stroke-dashoffset] duration-1000 ease-linear"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - Math.min(1, Math.max(0, fraction)))}
        />
      </svg>
      {children}
    </div>
  );
}

function Row({ ok, icon, label, detail }: { ok: boolean; icon: React.ReactNode; label: string; detail: string }) {
  return (
    <li className="flex items-center gap-3 py-2.5">
      <div className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground [&_svg]:size-4">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{label}</div>
        <div className="truncate text-xs text-muted-foreground">{detail}</div>
      </div>
      <span className={cn("grid size-5 place-items-center rounded-full [&_svg]:size-3", ok ? "bg-success text-success-foreground" : "bg-muted text-muted-foreground")}>
        {ok ? <Check strokeWidth={3} /> : <X strokeWidth={3} />}
      </span>
    </li>
  );
}

export function WatcherCard({ state, skew }: { state: State; skew: number }) {
  const now = useNow();
  const check = useStateMutation(() => api.check(), "Checking now…");
  const test = useStateMutation(() => api.test(), "Test notification sent");
  const c = state.config;

  const interval = c.interval_seconds;
  const left = state.next_check ? Math.max(0, Math.round(state.next_check - (now / 1000 - skew))) : 0;
  const lastMs = state.last_check ? new Date(state.last_check).getTime() : null;

  const holding = state.hold?.status === "ready";
  let center: React.ReactNode;
  if (holding) center = <ShoppingCart className="size-6 text-primary" />;
  else if (state.checking) center = <Loader2 className="size-6 animate-spin text-primary" />;
  else if (!c.enabled) center = <span className="text-xs font-semibold text-muted-foreground">Paused</span>;
  else
    center = (
      <div className="text-center leading-none">
        <div className="tabular text-lg font-bold">{`${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`}</div>
        <div className="mt-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">next</div>
      </div>
    );

  return (
    <Card>
      <CardHeader>
        <CardTitle>Watcher</CardTitle>
        <Button variant="outline" size="sm" onClick={() => check.mutate(undefined)} disabled={state.checking || check.isPending || holding}>
          <RefreshCw className={cn(state.checking && "animate-spin")} /> Check now
        </Button>
      </CardHeader>
      <div className="flex items-center gap-4 px-5 pb-4">
        <Ring fraction={holding || state.checking ? 1 : c.enabled && interval ? 1 - left / interval : 0}>{center}</Ring>
        <div className="min-w-0 space-y-1 text-sm">
          <div className="font-medium">
            {holding ? "Paused while your cart is held" : state.checking ? "Checking now" : c.enabled && state.next_check ? `Next check at ${clock((state.next_check + skew) * 1000)}` : c.enabled ? "Starting up" : "Automatic checks paused"}
          </div>
          <div className="text-muted-foreground">Every {everyLabel(c.interval_seconds)}</div>
          <div className="text-muted-foreground">{lastMs ? `Updated ${ago(lastMs, now)}` : "Not checked yet"}</div>
        </div>
      </div>
      <div className="border-t px-5 py-2">
        <ul className="divide-y divide-border/70">
          <Row
            ok={!!state.session}
            icon={<Globe />}
            label="Live booking session"
            detail={state.session ? `Open since ${clock((state.session.since + skew) * 1000)}${state.session.logged_in ? ", logged in" : ""}` : "Opens on the next check"}
          />
          <Row ok={state.pushover} icon={state.pushover ? <Bell /> : <BellOff />} label="Pushover alerts" detail={state.pushover ? "Phone alerts with a booking link" : "Not configured"} />
          <Row ok={state.login} icon={<KeyRound />} label="BC Ferries login" detail={state.login ? "Used only to fill the cart" : "Not configured"} />
          <Row ok={c.hold && state.login} icon={<ShoppingCart />} label="Auto-cart" detail={c.hold ? `Holds the ${FARES[c.fare] ?? c.fare}` : "Off"} />
        </ul>
      </div>
      <div className="flex gap-2 border-t px-5 py-3">
        {state.vnc_url && (
          <LinkButton variant="ghost" size="sm" className="flex-1" href={state.vnc_url} target="_blank" rel="noopener">
            <MonitorPlay /> Live browser
          </LinkButton>
        )}
        <Button variant="ghost" size="sm" className="flex-1" onClick={() => test.mutate(undefined)} disabled={test.isPending || !state.pushover}>
          <Send /> Test alert
        </Button>
      </div>
    </Card>
  );
}
