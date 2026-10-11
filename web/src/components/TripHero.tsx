import { ArrowRight, CalendarDays, Car, Clock, ExternalLink, Footprints, Pencil, ShoppingCart, Truck, Users } from "lucide-react";
import type { Config, State, Terminals } from "@/lib/api";
import { fmt12, longDate, relDays, splitTerminal } from "@/lib/utils";
import { Button, LinkButton } from "./ui/button";
import { Card } from "./ui/card";
import { StatusPanel } from "./StatusPanel";

function paxLabel(c: Config) {
  const parts: string[] = [];
  const add = (n: number, one: string, many: string) => n > 0 && parts.push(`${n} ${n === 1 ? one : many}`);
  add(c.adults, "adult", "adults");
  add(c.children, "child", "children");
  add(c.infants, "infant", "infants");
  add(c.seniors, "senior", "seniors");
  return parts.join(", ") || "No passengers";
}

function Chip({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="inline-flex h-7 items-center gap-1.5 rounded-full border bg-card/80 px-3 text-[13px] font-medium text-muted-foreground backdrop-blur [&_svg]:size-3.5">
      {icon}
      {children}
    </span>
  );
}

export function TripHero({ state, terminals, onEdit }: { state: State; terminals?: Terminals; onEdit: () => void }) {
  const c = state.config;
  const [fromName, fromCity] = splitTerminal(terminals?.[c.from]?.short);
  const [toName, toCity] = splitTerminal(terminals?.[c.to]?.short);
  const anyTime = c.earliest <= "00:00" && c.latest >= "23:59";
  const vehicle =
    c.vehicle === "none" ? (
      <Chip icon={<Footprints />}>Walk-on</Chip>
    ) : c.vehicle === "tall" ? (
      <Chip icon={<Truck />}>Over 7 ft ({c.height_ft} ft)</Chip>
    ) : (
      <Chip icon={<Car />}>Standard vehicle</Chip>
    );

  return (
    <Card className="overflow-hidden">
      <div className="relative border-b bg-gradient-to-br from-accent/80 via-card to-card px-5 pb-5 pt-5 sm:px-7 sm:pt-6">
        <div className="relative flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2 text-[13px] font-medium text-muted-foreground">
              <CalendarDays className="size-4" />
              <span className="text-foreground">{longDate(c.date)}</span>
              <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-semibold text-primary">{relDays(c.date)}</span>
            </div>
            <div className="mt-3 flex items-center gap-3 sm:gap-4">
              <div className="min-w-0">
                <div className="truncate text-[22px] font-bold leading-tight tracking-tight sm:text-[32px]">{fromName}</div>
                <div className="text-sm text-muted-foreground">{fromCity}</div>
              </div>
              <div className="grid size-8 shrink-0 place-items-center rounded-full border bg-card text-muted-foreground shadow-sm sm:size-9">
                <ArrowRight className="size-4" />
              </div>
              <div className="min-w-0">
                <div className="truncate text-[22px] font-bold leading-tight tracking-tight sm:text-[32px]">{toName}</div>
                <div className="text-sm text-muted-foreground">{toCity}</div>
              </div>
            </div>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onEdit}>
              <Pencil /> Edit trip
            </Button>
            <LinkButton variant="ghost" size="sm" href={state.booking_link} target="_blank" rel="noopener" className="hidden sm:inline-flex">
              BC Ferries <ExternalLink />
            </LinkButton>
          </div>
        </div>
        <div className="relative mt-4 flex flex-wrap gap-2">
          <Chip icon={<Users />}>{paxLabel(c)}</Chip>
          {vehicle}
          <Chip icon={<Clock />}>{anyTime ? "Any time" : `${fmt12(c.earliest)} – ${fmt12(c.latest)}`}</Chip>
          {c.hold && <Chip icon={<ShoppingCart />}>Auto-cart</Chip>}
        </div>
      </div>
      <StatusPanel state={state} />
    </Card>
  );
}
