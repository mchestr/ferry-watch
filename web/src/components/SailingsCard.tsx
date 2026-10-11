import * as Tabs from "@radix-ui/react-tabs";
import { Ship } from "lucide-react";
import { useState } from "react";
import type { Sailing, State } from "@/lib/api";
import { cn, duration } from "@/lib/utils";
import { Badge } from "./ui/badge";
import { Card, CardHeader, CardTitle } from "./ui/card";

type Filter = "window" | "all" | "bookable";

function SailingRow({ s }: { s: Sailing }) {
  const ok = s.status === "available";
  const out = s.in_window === false;
  return (
    <li className={cn("grid grid-cols-[76px_minmax(0,1fr)_auto] items-center gap-3 rounded-xl px-3 py-3 sm:grid-cols-[96px_minmax(0,1fr)_auto] sm:gap-4", ok && !out && "bg-success-soft", out && "opacity-50")}>
      <div className="tabular text-[15px] font-bold tracking-tight sm:text-[17px]">{s.depart}</div>
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{s.vessel}</div>
        <div className="tabular truncate text-xs text-muted-foreground">
          Arrives {s.arrive}
          <span className="hidden sm:inline"> · {duration(s.depart, s.arrive)}</span>
          {out && <span className="hidden sm:inline"> · outside your window</span>}
        </div>
      </div>
      {ok ? (
        <Badge variant="success">
          <span className="hidden sm:inline">Bookable{s.price && " · "}</span>
          {s.price || "Bookable"}
        </Badge>
      ) : /sold out/i.test(s.status) ? (
        <Badge variant="outline">Sold out</Badge>
      ) : (
        <Badge variant="warning">{s.status}</Badge>
      )}
    </li>
  );
}

export function SailingsCard({ state }: { state: State }) {
  const [filter, setFilter] = useState<Filter>("all");
  const all = state.sailings;
  const lists: Record<Filter, Sailing[]> = {
    all,
    window: all.filter((s) => s.in_window !== false),
    bookable: all.filter((s) => s.status === "available" && s.in_window !== false),
  };
  const shown = lists[filter];

  return (
    <Card>
      <CardHeader className="flex-wrap">
        <div>
          <CardTitle>Sailings</CardTitle>
          <p className="text-[13px] text-muted-foreground">
            {all.length ? `${lists.bookable.length} of ${all.length} bookable` : "Results appear after the first check"}
          </p>
        </div>
        <Tabs.Root value={filter} onValueChange={(v) => setFilter(v as Filter)}>
          <Tabs.List className="inline-flex rounded-lg border bg-muted p-0.5 text-[13px]">
            {(
              [
                ["all", "All"],
                ["window", "In window"],
                ["bookable", "Bookable"],
              ] as const
            ).map(([v, l]) => (
              <Tabs.Trigger
                key={v}
                value={v}
                className="h-7 rounded-md px-3 font-medium text-muted-foreground data-[state=active]:bg-card data-[state=active]:text-foreground data-[state=active]:shadow-sm"
              >
                {l}
                <span className="tabular ml-1.5 text-xs text-muted-foreground">{lists[v].length}</span>
              </Tabs.Trigger>
            ))}
          </Tabs.List>
        </Tabs.Root>
      </CardHeader>
      <div className="px-2 pb-2 sm:px-3 sm:pb-3">
        {shown.length ? (
          <ul className="divide-y divide-border/70 [&>li.bg-success-soft]:border-transparent">
            {shown.map((s) => (
              <SailingRow key={s.depart} s={s} />
            ))}
          </ul>
        ) : (
          <div className="flex flex-col items-center gap-2 px-6 py-12 text-center text-sm text-muted-foreground">
            <Ship className="size-8 text-muted-foreground/50" />
            {state.checking && !all.length ? "Fetching sailings from BC Ferries…" : filter === "bookable" ? "Nothing bookable yet. You'll get an alert when that changes." : "No sailings to show"}
          </div>
        )}
      </div>
    </Card>
  );
}
