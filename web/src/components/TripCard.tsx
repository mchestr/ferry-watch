import {
  ArrowRight,
  CalendarDays,
  Car,
  ChevronDown,
  Clock,
  ExternalLink,
  Footprints,
  Pencil,
  ShoppingCart,
  Truck,
  Users,
} from "lucide-react";
import { useId, useState } from "react";
import type * as React from "react";
import { api, hasStatus } from "@/lib/api";
import { useRemoveTrip, useStateMutation } from "@/lib/hooks";
import type { Trip } from "@/lib/schema";
import { longDate, relativeDay, timeLabel } from "@/lib/time";
import { heldCart, isBookable, paxLabel, windowLabel } from "@/lib/trip";
import { cn, plural } from "@/lib/utils";
import { CartBanner } from "./CartBanner";
import { RemoveTripDialog } from "./RemoveTripDialog";
import { SailingsList } from "./SailingsList";
import { StatusPanel } from "./StatusPanel";
import { Badge } from "./ui/badge";
import { Button, LinkButton } from "./ui/button";
import { Card } from "./ui/card";
import { Switch } from "./ui/switch";
import { Tooltip } from "./ui/tooltip";

function Chip({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="inline-flex h-7 items-center gap-1.5 rounded-full border bg-card/80 px-2.5 text-[13px] font-medium text-muted-foreground [&_svg]:size-3.5">
      <span aria-hidden className="contents">
        {icon}
      </span>
      {children}
    </span>
  );
}

function Place({ name, area }: { name: string; area: string }) {
  return (
    <div className="min-w-0">
      <div className="truncate text-xl font-bold leading-tight tracking-tight sm:text-[26px]">{name}</div>
      <div className="truncate text-[13px] text-muted-foreground">{area}</div>
    </div>
  );
}

export function TripCard({
  trip,
  watching,
  checkoutUrl,
  loginConfigured,
  skew,
  today,
  defaultOpen,
  onEdit,
}: {
  trip: Trip;
  watching: boolean;
  checkoutUrl: string | null;
  loginConfigured: boolean;
  skew: number;
  /** "YYYY-MM-DD", Pacific time. */
  today: string;
  defaultOpen: boolean;
  onEdit: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const titleId = useId();
  const sailingsId = useId();
  const cart = heldCart(trip);
  const toggle = useStateMutation((enabled: boolean) => api.updateTrip(trip.id, { enabled }), {
    success: (on) => (on ? "Trip resumed" : cart ? "Trip paused. Your cart is still held." : "Trip paused"),
  });
  const remove = useRemoveTrip(trip.id);
  const bookable = trip.sailings.filter(isBookable).length;
  const relative = relativeDay(trip.date, today);
  const switchTip = !trip.enabled
    ? "Resume this trip"
    : cart
      ? `Pause this trip. The ${timeLabel(cart.depart)} sailing stays in your cart.`
      : "Pause this trip";

  return (
    <Card role="article" aria-labelledby={titleId} className="overflow-hidden">
      <div className="border-b bg-gradient-to-br from-accent/70 via-card to-card px-5 pb-4 pt-5 sm:px-6">
        <h2 id={titleId} className="sr-only">
          {trip.title}
        </h2>
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2 text-[13px] font-medium text-muted-foreground">
            <CalendarDays className="size-4 shrink-0" aria-hidden />
            <span className="text-foreground">{longDate(trip.date) ?? trip.date}</span>
            {relative && (
              <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-semibold text-primary">
                {relative}
              </span>
            )}
            {!trip.enabled && <Badge variant="muted">Paused</Badge>}
          </div>
          <div className="-my-1 -mr-2 flex shrink-0 items-center gap-0.5">
            <Tooltip content={switchTip}>
              <span className="mr-1 inline-flex">
                <Switch
                  checked={trip.enabled}
                  onCheckedChange={(on) => toggle.mutate(on)}
                  disabled={toggle.isPending}
                  aria-label={`Watch ${trip.title}`}
                />
              </span>
            </Tooltip>
            <Tooltip content="Edit trip">
              <Button variant="ghost" size="icon" onClick={onEdit} aria-label={`Edit ${trip.title}`}>
                <Pencil aria-hidden />
              </Button>
            </Tooltip>
            {trip.booking_url && (
              <Tooltip content="Open on bcferries.com">
                <LinkButton
                  variant="ghost"
                  size="icon"
                  href={trip.booking_url}
                  target="_blank"
                  aria-label="Open on bcferries.com (new tab)"
                >
                  <ExternalLink aria-hidden />
                </LinkButton>
              </Tooltip>
            )}
          </div>
        </div>
        <div className="mt-2 flex items-center gap-3">
          <Place name={trip.route.from_place} area={trip.route.from_area} />
          <div
            aria-hidden
            className="grid size-7 shrink-0 place-items-center rounded-full border bg-card text-muted-foreground shadow-sm sm:size-8"
          >
            <ArrowRight className="size-3.5" />
          </div>
          <Place name={trip.route.to_place} area={trip.route.to_area} />
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
          <Chip icon={<Clock />}>{windowLabel(trip)}</Chip>
          {trip.auto_cart && loginConfigured && <Chip icon={<ShoppingCart />}>Auto-cart</Chip>}
        </div>
      </div>
      <StatusPanel
        trip={trip}
        watching={watching}
        skew={skew}
        onPause={() => toggle.mutate(false)}
        pausePending={toggle.isPending}
        onRemove={() => setConfirmingRemove(true)}
      />
      <CartBanner trip={trip} checkoutUrl={checkoutUrl} skew={skew} />
      {trip.sailings.length > 0 && (
        <>
          <div id={sailingsId} hidden={!open}>
            <SailingsList sailings={trip.sailings} />
          </div>
          <button
            type="button"
            aria-expanded={open}
            aria-controls={sailingsId}
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-center justify-center gap-1.5 border-t py-2.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          >
            {open
              ? "Hide sailings"
              : `Show ${plural(trip.sailings.length, "sailing")}${bookable ? ` · ${bookable} bookable` : ""}`}
            <ChevronDown aria-hidden className={cn("size-4 motion-safe:transition-transform", open && "rotate-180")} />
          </button>
        </>
      )}
      <RemoveTripDialog
        trip={trip}
        open={confirmingRemove}
        onOpenChange={setConfirmingRemove}
        pending={remove.isPending}
        onConfirm={() => {
          if (remove.isPending) return;
          remove.mutate(undefined, {
            onSuccess: () => setConfirmingRemove(false),
            // Already gone (useRemoveTrip says so and refetches).
            onError: (error) => {
              if (hasStatus(error, 404)) setConfirmingRemove(false);
            },
          });
        }}
      />
    </Card>
  );
}
