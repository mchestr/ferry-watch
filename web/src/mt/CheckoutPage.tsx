/**
 * /c/<trip>: where a cart alert lands. It never opens the checkout by itself: the first device to open
 * it is the only one that can, so the user chooses (docs/multi-tenant/design-reference.md §10).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Laptop, Smartphone } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { errorMessage } from "@/lib/api";
import { reportError, useNow } from "@/lib/hooks";
import type { Terminal } from "@/lib/schema";
import { clock, timeLabel } from "@/lib/time";
import { MY_STATE_KEY, myApi } from "./api";
import { navigate } from "./router";
import type { MyState } from "./schema";
import { minutesLeft, tripTitle } from "./trips";

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
  if (!trip || !cart || cart.status !== "ready") {
    return (
      <div className="mx-auto max-w-xl space-y-4">
        {back}
        <Card className="p-6">
          <h1 className="font-semibold">Nothing is held right now</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            The hold may have ended. We'll alert you if the sailing opens up again.
          </p>
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
