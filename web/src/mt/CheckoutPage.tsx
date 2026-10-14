/**
 * /c/<trip>: where a cart alert lands. It never opens the checkout by itself: the first device to open
 * it is the only one that can, so the user chooses (docs/multi-tenant/design-reference.md §10).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Laptop, Loader2, Smartphone } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { errorMessage } from "@/lib/api";
import { reportError, useNow } from "@/lib/hooks";
import type { Terminal } from "@/lib/schema";
import { clock, timeLabel } from "@/lib/time";
import { MY_STATE_KEY, myApi } from "./api";
import { navigate } from "./router";
import type { MyState } from "./schema";
import { NotHeld } from "./NotHeld";
import { holdPending, minutesLeft, tripBadge, tripTitle, unheld } from "./trips";

export function CheckoutPage({
  state,
  tripId,
  terminals,
}: {
  state: MyState;
  tripId: string;
  terminals: readonly Terminal[] | undefined;
}) {
  useNow(15_000);
  const qc = useQueryClient();
  const [elsewhere, setElsewhere] = useState(false);
  const trip = state.trips.find((t) => t.trip.id === tripId);
  const cart = trip?.browser.cart;
  const open = useMutation({
    mutationFn: () => myApi.checkout(tripId),
    onSuccess: ({ url }) => location.assign(url),
  });
  const release = useMutation({
    mutationFn: () => myApi.release(tripId, cart?.id ?? ""),
    onSuccess: (next) => {
      qc.setQueryData(MY_STATE_KEY, next);
      navigate("/");
    },
    onError: (error) => reportError(qc, error),
  });

  const back = (
    <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
      <ArrowLeft aria-hidden /> Your trips
    </Button>
  );
  if (!trip) {
    return (
      <div className="mx-auto max-w-xl space-y-4">
        {back}
        <Card className="p-6">
          <h1 className="font-semibold">This trip isn't on your list any more</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            It was booked or removed. Your other trips are still watched.
          </p>
        </Card>
      </div>
    );
  }
  if (!cart || cart.status !== "ready") {
    // Where a "Bookable" alert lands too: the sailing may be open with nothing holding it yet.
    const paused =
      trip.paused_reason === "after_checkout"
        ? "Paused until you say whether you booked it, on Your trips."
        : !trip.trip.enabled || trip.paused_reason !== null || trip.hold.reason === "paused"
          ? "Paused, so nothing is checked or held."
          : null;
    const badge = tripBadge(trip);
    const quietUntil = state.quiet_hours?.end;
    return (
      <div className="mx-auto max-w-xl space-y-4">
        {back}
        <Card className="space-y-4 p-6">
          <div className="flex items-start justify-between gap-3">
            <h1 className="min-w-0 text-lg font-bold">{tripTitle(trip, terminals)}</h1>
            <Badge variant={badge.tone}>{badge.text}</Badge>
          </div>
          {paused ? (
            <p className="text-sm text-muted-foreground">{paused}</p>
          ) : unheld(trip).length > 0 || holdPending(trip) ? (
            <NotHeld trip={trip} />
          ) : cart?.status === "adding" ? (
            <p className="flex items-center gap-2 text-sm" role="status">
              <Loader2 className="size-4 motion-safe:animate-spin" aria-hidden /> Holding it. This page updates when
              it's held.
            </p>
          ) : (
            <div role="status">
              <h2 className="font-semibold">Nothing is held right now</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {trip.quiet
                  ? `It's your quiet hours, so this trip isn't checked${quietUntil ? ` until ${timeLabel(quietUntil)}` : ""}.`
                  : "Nothing in your times is bookable. We'll alert you when a sailing opens up."}
              </p>
            </div>
          )}
        </Card>
      </div>
    );
  }
  const deadline = cart.session_deadline ?? cart.unclaimed_deadline;
  const left = minutesLeft(deadline);
  return (
    <div className="mx-auto max-w-xl space-y-4">
      {back}
      <Card className="space-y-4 p-6">
        <div>
          <h1 className="text-lg font-bold">
            {timeLabel(cart.depart)} · {tripTitle(trip, terminals)}
          </h1>
          <p className="text-sm text-muted-foreground">
            {cart.fare ? `${cart.fare}${cart.price ? `, ${cart.price}` : ""}. ` : ""}
            {deadline ? `Held until ${clock(deadline * 1000)}${left !== null ? ` (${left} min)` : ""}.` : ""}
            {cart.hard_deadline ? ` At the latest ${clock(cart.hard_deadline * 1000)}.` : ""}
          </p>
        </div>
        <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li>Checkout will be locked to the device you choose.</li>
          <li>
            Check out as a guest. Don't sign in to BC Ferries there: it doesn't accept a sign-in while a cart is held.
          </li>
          <li>Have your card and phone ready; your bank may ask you to approve in its app.</li>
        </ul>
        {open.error && <p className="text-sm text-danger">{errorMessage(open.error)}</p>}
        <div className="grid gap-2 sm:grid-cols-2">
          <Button size="lg" variant="success" disabled={open.isPending} onClick={() => open.mutate()}>
            <Smartphone aria-hidden /> Check out on this device
          </Button>
          <Button size="lg" variant="outline" onClick={() => setElsewhere(true)}>
            <Laptop aria-hidden /> I'll use my computer
          </Button>
        </div>
        {elsewhere && (
          <p className="rounded-xl bg-muted p-3 text-sm">
            Open Ferry Watch on your computer, sign in, and choose this trip's <strong>Open checkout</strong>. A
            computer is easiest.
          </p>
        )}
        <Button variant="ghost" className="w-full" disabled={release.isPending} onClick={() => release.mutate()}>
          Release it, I'll book it myself
        </Button>
      </Card>
    </div>
  );
}
