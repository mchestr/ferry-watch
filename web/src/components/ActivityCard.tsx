import { AlertTriangle, BellRing, Info, ShoppingCart } from "lucide-react";
import type { ActivityEvent } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import { ago, clock, cn } from "@/lib/utils";
import { Card, CardHeader, CardTitle } from "./ui/card";

const KIND = {
  info: { icon: Info, cls: "bg-muted text-muted-foreground" },
  alert: { icon: BellRing, cls: "bg-success-soft text-success" },
  cart: { icon: ShoppingCart, cls: "bg-accent text-accent-foreground" },
  error: { icon: AlertTriangle, cls: "bg-danger-soft text-danger" },
} as const;

export function ActivityCard({ events, skew, tripLabels }: { events: ActivityEvent[]; skew: number; tripLabels: Record<string, string> }) {
  const now = useNow(15000);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity</CardTitle>
      </CardHeader>
      {events.length ? (
        <ol className="relative px-5 pb-5">
          <span className="absolute bottom-8 left-[35px] top-2 w-px bg-border" aria-hidden />
          {events.slice(0, 14).map((e, i) => {
            const k = KIND[e.kind] ?? KIND.info;
            const ms = (e.ts + skew) * 1000;
            const label = e.trip ? tripLabels[e.trip] : undefined;
            return (
              <li key={`${e.ts}-${i}`} className="relative flex gap-3 py-2">
                <span className={cn("relative z-10 grid size-[30px] shrink-0 place-items-center rounded-full ring-4 ring-card [&_svg]:size-3.5", k.cls)}>
                  <k.icon />
                </span>
                <div className="min-w-0 pt-1">
                  {label && <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</p>}
                  <p className="break-words text-sm leading-snug">{e.text}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground" title={new Date(ms).toLocaleString()}>
                    {clock(ms)} · {ago(ms, now)}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="px-5 pb-5 text-sm text-muted-foreground">Nothing yet.</p>
      )}
    </Card>
  );
}
