import { ArrowRight, CalendarDays, Car, ChevronDown, Clock, ExternalLink, Footprints, Pencil, ShoppingCart, Truck, Users } from "lucide-react";
import { useState } from "react";
import { api, type Terminals, type Trip, type TripConfig } from "@/lib/api";
import { useStateMutation } from "@/lib/hooks";
import { cn, fmt12, longDate, relDays, splitTerminal } from "@/lib/utils";
import { CartBanner } from "./CartBanner";
import { SailingsList } from "./SailingsList";
import { StatusPanel } from "./StatusPanel";
import { Button, LinkButton } from "./ui/button";
import { Card } from "./ui/card";
import { Switch } from "./ui/switch";
import { Tooltip } from "./ui/tooltip";

export function paxLabel(c: TripConfig) {
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
    <span className="inline-flex h-7 items-center gap-1.5 rounded-full border bg-card/80 px-2.5 text-[13px] font-medium text-muted-foreground [&_svg]:size-3.5">
      {icon}
      {children}
    </span>
  );
}

export function TripCard({
  trip,
  terminals,
  watching,
  vncUrl,
  defaultOpen,
  onEdit,
}: {
  trip: Trip;
  terminals?: Terminals;
  watching: boolean;
  vncUrl: string;
  defaultOpen: boolean;
  onEdit: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const toggle = useStateMutation((enabled: boolean) => api.updateTrip(trip.id, { enabled }), (on) => (on ? "Trip resumed" : "Trip paused"));
  const [fromName, fromCity] = splitTerminal(terminals?.[trip.from]?.short);
  const [toName, toCity] = splitTerminal(terminals?.[trip.to]?.short);
  const anyTime = trip.earliest <= "00:00" && trip.latest >= "23:59";
  const bookable = trip.sailings.filter((s) => s.status === "available" && s.in_window !== false).length;

  return (
    <Card className={cn("overflow-hidden", !trip.enabled && "opacity-75")}>
      <div className="border-b bg-gradient-to-br from-accent/70 via-card to-card px-5 pb-4 pt-5 sm:px-6">
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2 text-[13px] font-medium text-muted-foreground">
            <CalendarDays className="size-4 shrink-0" />
            <span className="text-foreground">{longDate(trip.date)}</span>
            <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-semibold text-primary">{relDays(trip.date)}</span>
          </div>
          <div className="-my-1 -mr-2 flex shrink-0 items-center gap-0.5">
            <Tooltip content={trip.enabled ? "Pause this trip" : "Resume this trip"}>
              <span className="mr-1 inline-flex">
                <Switch checked={trip.enabled} onCheckedChange={(v) => toggle.mutate(v)} disabled={toggle.isPending} aria-label="Watch this trip" />
              </span>
            </Tooltip>
            <Tooltip content="Edit trip">
              <Button variant="ghost" size="icon" onClick={onEdit} aria-label="Edit trip">
                <Pencil />
              </Button>
            </Tooltip>
            <Tooltip content="Open on bcferries.com">
              <LinkButton variant="ghost" size="icon" href={trip.booking_link} target="_blank" rel="noopener" aria-label="Open on bcferries.com" className="hidden sm:inline-flex">
                <ExternalLink />
              </LinkButton>
            </Tooltip>
          </div>
        </div>
        <div className="mt-2 flex items-center gap-3">
          <div className="min-w-0">
            <div className="truncate text-xl font-bold leading-tight tracking-tight sm:text-[26px]">{fromName}</div>
            <div className="text-[13px] text-muted-foreground">{fromCity}</div>
          </div>
          <div className="grid size-7 shrink-0 place-items-center rounded-full border bg-card text-muted-foreground shadow-sm sm:size-8">
            <ArrowRight className="size-3.5" />
          </div>
          <div className="min-w-0">
            <div className="truncate text-xl font-bold leading-tight tracking-tight sm:text-[26px]">{toName}</div>
            <div className="text-[13px] text-muted-foreground">{toCity}</div>
          </div>
        </div>
        <div className="mt-3.5 flex flex-wrap gap-1.5">
          <Chip icon={<Users />}>{paxLabel(trip)}</Chip>
          {trip.vehicle === "none" ? (
            <Chip icon={<Footprints />}>Walk-on</Chip>
          ) : trip.vehicle === "tall" ? (
            <Chip icon={<Truck />}>Over 7 ft ({trip.height_ft} ft)</Chip>
          ) : (
            <Chip icon={<Car />}>Standard vehicle</Chip>
          )}
          <Chip icon={<Clock />}>{anyTime ? "Any time" : `${fmt12(trip.earliest)} – ${fmt12(trip.latest)}`}</Chip>
          {trip.hold && <Chip icon={<ShoppingCart />}>Auto-cart</Chip>}
        </div>
      </div>
      <StatusPanel trip={trip} watching={watching} />
      <CartBanner trip={trip} vncUrl={vncUrl} />
      {trip.sailings.length > 0 && (
        <>
          {open && <SailingsList trip={trip} />}
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="flex w-full items-center justify-center gap-1.5 border-t py-2.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
          >
            {open ? "Hide sailings" : `Show ${trip.sailings.length} sailings${bookable ? ` · ${bookable} bookable` : ""}`}
            <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
          </button>
        </>
      )}
    </Card>
  );
}
