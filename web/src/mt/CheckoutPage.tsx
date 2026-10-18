/**
 * /c/<trip>: where a cart alert lands. It never opens the checkout by itself: the first device to open
 * it is the only one that can, so the user chooses (docs/multi-tenant/design-reference.md §10). BC Ferries only:
 * another provider's watch id goes to its own page, /w/<id> (docs/waitlist/design.md §7.1).
 */
import { useMutation } from "@tanstack/react-query";
import { ArrowLeft, Laptop, Loader2, Smartphone } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { errorMessage } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import type { Terminal } from "@/lib/schema";
import { clock, timeLabel } from "@/lib/time";
import { endsTrip, myApi } from "./api";
import { BookedQuestion } from "./BookedQuestion";
import { useRelease } from "./release";
import { navigate } from "./router";
import type { MyTrip } from "./schema";
import { NotHeld } from "./NotHeld";
import { holdPending, minutesLeft, tripBadge, tripTitle, unheld } from "./trips";
import type { WaitlistState } from "./watch-schema";

export function CheckoutPage({
  state,
  tripId,
  terminals,
}: {
  state: WaitlistState;
  tripId: string;
  terminals: readonly Terminal[] | undefined;
}) {
  useNow(15_000);
  const watch = state.watches.find((w) => w.id === tripId);
  const trip = watch?.kind === "bcferries" ? watch : undefined;
  const otherProvider = watch !== undefined && trip === undefined;
  useEffect(() => {
    if (otherProvider) navigate(`/w/${tripId}`, { replace: true }); // not a BC Ferries trip: it has no checkout
  }, [otherProvider, tripId]);
  const cart = trip?.browser.cart;

  const back = (
    <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
      <ArrowLeft aria-hidden /> Your watches
    </Button>
  );
  if (otherProvider) return null;
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
      !trip.trip.enabled || trip.paused_reason !== null || trip.hold.reason === "paused"
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
          {trip.paused_reason === "after_checkout" ? (
            <BookedQuestion
              trip={trip}
              onAnswered={(answer) => {
                if (endsTrip(answer)) navigate("/"); // nothing left to show here
              }}
            />
          ) : paused ? (
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
                  ? `It's your quiet hours, so this trip isn't checked${quietUntil ? ` until ${timeLabel(quietUntil)} PT` : ""}.`
                  : "Nothing in your times is bookable. We'll alert you when a sailing opens up."}
              </p>
            </div>
          )}
        </Card>
      </div>
    );
  }
  return (
    <div className="mx-auto max-w-xl space-y-4">
      {back}
      <CheckoutPanel state={state} trip={trip} terminals={terminals} />
    </div>
  );
}

/**
 * A held cart's checkout: which device, the deadline, and Release. On /c/<trip> and on the trip's watch page.
 * Shown only while the cart is held (status "ready").
 */
export function CheckoutPanel({
  state,
  trip,
  terminals,
}: {
  state: WaitlistState;
  trip: MyTrip;
  terminals: readonly Terminal[] | undefined;
}) {
  useNow(15_000);
  const tripId = trip.trip.id;
  const [elsewhere, setElsewhere] = useState(false);
  const cart = trip.browser.cart;
  const open = useMutation({
    mutationFn: () => myApi.checkout(tripId),
    onSuccess: ({ url }) => location.assign(url),
  });
  // Which viewer the link opens, once there is one. Until then, the user's choice: waitlist-web overrules it only for
  // a browser pod from before the beta, which opens in the classic viewer.
  const { checkout_viewer, stream_viewer_offered } = state.settings;
  const streamed = (open.data?.viewer ?? (stream_viewer_offered ? checkout_viewer : "classic")) === "stream";
  // Stays here: once the cart is let go, this page offers bcferries.com for the sailing.
  const { release, releasing } = useRelease(tripId, cart);
  if (!cart || cart.status !== "ready") return null;
  const deadline = cart.session_deadline ?? cart.unclaimed_deadline;
  const left = minutesLeft(deadline);
  return (
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
        <li>Have your card and phone ready; your bank may ask you to approve in its app.</li>
      </ul>
      {open.error && <p className="text-sm text-danger">{errorMessage(open.error)}</p>}
      <div className="grid gap-2 sm:grid-cols-2">
        <Button size="lg" variant="success" disabled={open.isPending || releasing} onClick={() => open.mutate()}>
          <Smartphone aria-hidden /> Check out on this device
        </Button>
        <Button size="lg" variant="outline" onClick={() => setElsewhere(true)}>
          <Laptop aria-hidden /> I'll use my computer
        </Button>
      </div>
      {streamed && (
        <p className="text-center text-xs text-muted-foreground">Opens in the new checkout viewer (beta).</p>
      )}
      {elsewhere && (
        <p className="rounded-xl bg-muted p-3 text-sm">
          Open Waitlist on your computer, sign in, and choose this trip's <strong>Open checkout</strong>. A computer is
          easiest.
        </p>
      )}
      <Button variant="ghost" className="w-full" disabled={releasing} onClick={release}>
        {releasing ? (
          <>
            <Loader2 className="motion-safe:animate-spin" aria-hidden /> Releasing…
          </>
        ) : (
          "Release it, I'll book it myself"
        )}
      </Button>
    </Card>
  );
}
