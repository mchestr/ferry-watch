import { AlertTriangle, BellRing, Info, ShoppingCart } from "lucide-react";
import { useNow } from "@/lib/hooks";
import type { ActivityEvent } from "@/lib/schema";
import { ago, clock, serverToClientMs } from "@/lib/time";
import { cn } from "@/lib/utils";
import { Card, CardHeader, CardTitle } from "./ui/card";

const KIND = {
  info: { icon: Info, label: "Info", cls: "bg-muted text-muted-foreground" },
  alert: { icon: BellRing, label: "Alert", cls: "bg-success-soft text-success" },
  cart: { icon: ShoppingCart, label: "Cart", cls: "bg-accent text-accent-foreground" },
  error: { icon: AlertTriangle, label: "Error", cls: "bg-danger-soft text-danger" },
} as const;

const SHOWN = 14;

export function ActivityCard({ events, skew }: { events: ActivityEvent[]; skew: number }) {
  const now = useNow(15_000);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity</CardTitle>
      </CardHeader>
      {events.length ? (
        <ol className="relative px-5 pb-5">
          <span className="absolute bottom-8 left-[35px] top-2 w-px bg-border" aria-hidden />
          {events.slice(0, SHOWN).map((e) => {
            const kind = KIND[e.kind];
            const ms = serverToClientMs(e.ts, skew);
            return (
              <li key={e.id} className="relative flex gap-3 py-2">
                <span
                  role="img"
                  aria-label={kind.label}
                  className={cn(
                    "relative z-10 grid size-[30px] shrink-0 place-items-center rounded-full ring-4 ring-card [&_svg]:size-3.5",
                    kind.cls,
                  )}
                >
                  <kind.icon aria-hidden />
                </span>
                <div className="min-w-0 pt-1">
                  {e.trip_title && (
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {e.trip_title}
                    </p>
                  )}
                  <p className="break-words text-sm leading-snug">{e.text}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    <time dateTime={new Date(ms).toISOString()} title={new Date(ms).toLocaleString()}>
                      {clock(ms)} · {ago(ms, now)}
                    </time>
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
