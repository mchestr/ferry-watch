import { Ship } from "lucide-react";
import { useState } from "react";
import type { Sailing } from "@/lib/schema";
import { duration, timeLabel } from "@/lib/time";
import { isBookable } from "@/lib/trip";
import { cn } from "@/lib/utils";
import { Badge } from "./ui/badge";
import { FilterGroup } from "./ui/toggle-group";

type Filter = "all" | "window" | "bookable";

function StatusBadge({ sailing: s }: { sailing: Sailing }) {
  if (s.status === "available") {
    return (
      <Badge variant={s.in_window ? "success" : "soft"}>
        {s.price ? (
          <>
            <span className="hidden sm:inline">Bookable · </span>
            {s.price}
          </>
        ) : (
          "Bookable"
        )}
      </Badge>
    );
  }
  if (s.status === "sold_out") return <Badge variant="outline">Sold out</Badge>;
  return (
    <Badge variant="warning" className="max-w-[9rem] sm:max-w-[14rem]" title={s.note || undefined}>
      <span className="truncate">{s.note || "Unavailable"}</span>
    </Badge>
  );
}

function SailingRow({ sailing: s }: { sailing: Sailing }) {
  const length = duration(s.depart, s.arrive);
  return (
    <li
      className={cn(
        "grid grid-cols-[76px_minmax(0,1fr)_auto] items-center gap-3 rounded-xl px-3 py-2.5 sm:grid-cols-[92px_minmax(0,1fr)_auto] sm:gap-4",
        isBookable(s) && "bg-success-soft",
        // Outside the window: muted (still 4.5:1) plus words, not just faded.
        !s.in_window && "text-muted-foreground",
      )}
    >
      <div className="tabular text-[15px] font-bold tracking-tight sm:text-base">{timeLabel(s.depart)}</div>
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{s.vessel}</div>
        <div className="tabular flex flex-wrap gap-x-1 text-xs text-muted-foreground">
          <span>Arrives {timeLabel(s.arrive)}</span>
          {length && <span className="hidden sm:inline">· {length}</span>}
          {!s.in_window && <span>· Outside your window</span>}
        </div>
      </div>
      <StatusBadge sailing={s} />
    </li>
  );
}

export function SailingsList({ sailings }: { sailings: Sailing[] }) {
  const [filter, setFilter] = useState<Filter>("all");
  const lists: Record<Filter, Sailing[]> = {
    all: sailings,
    window: sailings.filter((s) => s.in_window),
    bookable: sailings.filter(isBookable),
  };
  const shown = lists[filter];

  return (
    <div className="border-t">
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 pb-2 pt-4 sm:px-6">
        <span className="text-[13px] font-medium text-muted-foreground">
          {lists.bookable.length} of {sailings.length} bookable
        </span>
        <FilterGroup
          label="Show sailings"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All", count: lists.all.length },
            { value: "window", label: "In window", count: lists.window.length },
            { value: "bookable", label: "Bookable", count: lists.bookable.length },
          ]}
        />
      </div>
      <div className="px-2 pb-3 sm:px-3">
        {shown.length ? (
          <ul className="divide-y divide-border/70">
            {shown.map((s) => (
              <SailingRow key={`${s.depart}-${s.vessel}`} sailing={s} />
            ))}
          </ul>
        ) : (
          <div className="flex flex-col items-center gap-2 px-6 py-8 text-center text-sm text-muted-foreground">
            <Ship className="size-7 text-muted-foreground/50" aria-hidden />
            {filter === "bookable"
              ? "Nothing bookable yet. You'll get an alert when that changes."
              : "No sailings to show."}
          </div>
        )}
      </div>
    </div>
  );
}
