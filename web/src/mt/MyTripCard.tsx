/**
 * One of the user's BC Ferries trips: its browser's status, its sailings, a held cart and the questions it raises.
 * BcferriesCard puts it on the dashboard (a BcferriesWatch is a MyTrip plus the fields every watch has).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Loader2, Pause, Pencil, Play, RefreshCw, ShoppingCart, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { RemoveTripDialog } from "@/components/RemoveTripDialog";
import { SailingsList } from "@/components/SailingsList";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { hasStatus } from "@/lib/api";
import { reportError, useNow } from "@/lib/hooks";
import type { Terminal } from "@/lib/schema";
import { clock, timeLabel } from "@/lib/time";
import { MY_STATE_KEY, myApi } from "./api";
import { BookedQuestion } from "./BookedQuestion";
import { useRelease } from "./release";
import { navigate } from "./router";
import type { MyTrip } from "./schema";
import { NotHeld } from "./NotHeld";
import type { WaitlistState } from "./watch-schema";
import { asSailings, minutesLeft, nextCheck, tripBadge, tripTitle, unheld } from "./trips";

function useMyMutation<A>(fn: (arg: A) => Promise<WaitlistState>, success?: string | ((arg: A) => string)) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (state, arg) => {
      qc.setQueryData(MY_STATE_KEY, state);
      if (success) toast.success(typeof success === "string" ? success : success(arg));
    },
    onError: (error) => reportError(error),
  });
}

function HeldCart({ trip }: { trip: MyTrip }) {
  useNow(15_000);
  const cart = trip.browser.cart;
  const { release, releasing } = useRelease(trip.trip.id, cart);
  if (!cart || cart.status !== "ready") return null;
  const deadline = cart.session_deadline ?? cart.unclaimed_deadline;
  const left = minutesLeft(deadline);
  return (
    <div className="space-y-3 rounded-xl border border-success bg-success-soft p-4">
      <div className="flex items-start gap-3">
        <ShoppingCart className="mt-0.5 size-5 text-success" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-semibold">
            {timeLabel(cart.depart)} is held for you
            {cart.fare ? ` (${cart.fare}${cart.price ? `, ${cart.price}` : ""})` : ""}
          </p>
          <p className="text-sm text-muted-foreground">
            {cart.payment_in_progress
              ? "Paused while you pay."
              : deadline
                ? `Check out by ${clock(deadline * 1000)}${left !== null ? ` (${left} min)` : ""}.`
                : "Check out soon."}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="success" disabled={releasing} onClick={() => navigate(`/c/${trip.trip.id}`)}>
          Open checkout <ChevronRight aria-hidden />
        </Button>
        <Button variant="outline" disabled={releasing} onClick={release}>
          {releasing ? (
            <>
              <Loader2 className="motion-safe:animate-spin" aria-hidden /> Releasing…
            </>
          ) : (
            "Release"
          )}
        </Button>
      </div>
    </div>
  );
}

/**
 * Remove, after asking. waitlist-control drains the trip's browser even while it holds a cart, so the dialog's
 * "releases the sailing in your cart" is what happens.
 */
function RemoveButton({ trip, title }: { trip: MyTrip; title: string }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const remove = useMutation({
    mutationFn: () => myApi.removeWatch(trip.trip.id),
    onSuccess: (state) => {
      qc.setQueryData(MY_STATE_KEY, state);
      toast.success("Trip removed");
    },
    onError: (error) => {
      if (hasStatus(error, 404)) {
        toast.info("That trip was already removed.");
        void qc.invalidateQueries({ queryKey: MY_STATE_KEY });
      } else reportError(error);
    },
  });
  return (
    <>
      <Button size="sm" variant="ghost" className="hover:text-danger" onClick={() => setConfirming(true)}>
        <Trash2 aria-hidden /> Remove
      </Button>
      <RemoveTripDialog
        title="Remove this trip?"
        confirmLabel="Remove trip"
        trip={{ title, cart: trip.browser.cart }}
        open={confirming}
        onOpenChange={setConfirming}
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(undefined, {
            onSuccess: () => setConfirming(false),
            onError: (error) => {
              if (hasStatus(error, 404)) setConfirming(false); // already gone; otherwise they can try again
            },
          })
        }
      />
    </>
  );
}

export function MyTripCard({
  trip,
  terminals,
  onEdit,
  header,
  heading,
  showHeldCart = true,
  headingLevel = 2,
}: {
  trip: MyTrip;
  terminals: readonly Terminal[] | undefined;
  onEdit: () => void;
  /** Above the title, inside the card: the provider line. */
  header?: ReactNode;
  /** The title, when the header already says the date; the route and date otherwise. */
  heading?: string;
  /** False where the page shows the held cart's checkout itself (the watch page). */
  showHeldCart?: boolean;
  /** 1 where the title is the page's (the watch page, when no checkout heads it). */
  headingLevel?: 1 | 2;
}) {
  const badge = tripBadge(trip);
  const check = useMyMutation(() => myApi.checkWatch(trip.trip.id), "Checking now");
  const held = trip.browser.cart?.status === "ready";
  const pause = useMyMutation(
    (enabled: boolean) => myApi.updateWatch(trip.trip.id, { enabled }),
    // Pausing doesn't let go of a held cart: that's Release.
    (enabled) => (enabled ? "Trip resumed" : held ? "Trip paused. Your cart is still held." : "Trip paused"),
  );
  // Paused by the user, or until they say whether they booked it (the question resumes it).
  const paused = !trip.trip.enabled || trip.paused_reason !== null;
  // A failed cart whose sailing has gone again; while it's still bookable, NotHeld says so instead.
  const failed = trip.browser.cart?.status === "failed" && unheld(trip).length === 0 ? trip.browser.cart : null;
  const dismiss = useMyMutation(() => myApi.dismiss(trip.trip.id, failed?.id ?? ""));
  const sailings = asSailings(trip);
  const checked = trip.browser.checked_at;
  const next = nextCheck(trip);
  const title = tripTitle(trip, terminals);
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <Card className="space-y-4 p-4 sm:p-5">
      {header}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Heading className={heading === undefined ? "truncate font-semibold" : "font-semibold"}>
            {heading ?? title}
          </Heading>
          <p className="text-sm text-muted-foreground">
            {timeLabel(trip.trip.earliest)}–{timeLabel(trip.trip.latest)}
            {checked ? ` · checked ${clock(checked * 1000)}` : ""}
            {next ? ` · next ${clock(next * 1000)}` : ""}
          </p>
        </div>
        <Badge variant={badge.tone}>{badge.text}</Badge>
      </div>
      <BookedQuestion trip={trip} />
      {showHeldCart && <HeldCart trip={trip} />}
      <NotHeld trip={trip} />
      {failed && (
        <div className="flex items-center justify-between gap-3 rounded-xl border p-3 text-sm">
          <span>Couldn't hold {timeLabel(failed.depart)}, and it isn't bookable any more.</span>
          <Button size="sm" variant="ghost" onClick={() => dismiss.mutate(undefined)}>
            Dismiss
          </Button>
        </div>
      )}
      {sailings.length > 0 && <SailingsList sailings={sailings} />}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={check.isPending || paused}
          onClick={() => check.mutate(undefined)}
        >
          <RefreshCw aria-hidden /> Check now
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={pause.isPending || (trip.trip.enabled && trip.paused_reason !== null)}
          onClick={() => pause.mutate(!trip.trip.enabled)}
        >
          {trip.trip.enabled ? (
            <>
              <Pause aria-hidden /> Pause
            </>
          ) : (
            <>
              <Play aria-hidden /> Resume
            </>
          )}
        </Button>
        <Button size="sm" variant="ghost" onClick={onEdit}>
          <Pencil aria-hidden /> Edit
        </Button>
        <RemoveButton trip={trip} title={title} />
      </div>
    </Card>
  );
}
