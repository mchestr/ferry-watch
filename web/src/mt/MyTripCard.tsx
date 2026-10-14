/** One of the user's trips: its browser's status, its sailings, a held cart and the questions it raises. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Pause, Pencil, Play, RefreshCw, ShoppingCart } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { SailingsList } from "@/components/SailingsList";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { reportError, useNow } from "@/lib/hooks";
import type { Terminal } from "@/lib/schema";
import { clock, timeLabel } from "@/lib/time";
import { MY_STATE_KEY, myApi, type BookedReason } from "./api";
import { navigate } from "./router";
import type { MyState, MyTrip } from "./schema";
import { NotHeld } from "./NotHeld";
import { asSailings, minutesLeft, nextCheck, tripBadge, tripTitle, unheld } from "./trips";

const REASONS: readonly { value: BookedReason; label: string }[] = [
  { value: "needed_account", label: "It wanted an account" },
  { value: "payment_failed", label: "Payment didn't go through" },
  { value: "checkout_broke", label: "Checkout broke" },
  { value: "ran_out_of_time", label: "Ran out of time" },
  { value: "changed_mind", label: "Changed my mind" },
];

function useMyMutation<A>(fn: (arg: A) => Promise<MyState>, success?: string | ((arg: A) => string)) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (state, arg) => {
      qc.setQueryData(MY_STATE_KEY, state);
      if (success) toast.success(typeof success === "string" ? success : success(arg));
    },
    onError: (error) => reportError(qc, error),
  });
}

function HeldCart({ trip }: { trip: MyTrip }) {
  useNow(15_000);
  const cart = trip.browser.cart;
  const release = useMyMutation(() => myApi.release(trip.trip.id, cart?.id ?? ""), "Cart released");
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
                : "Check out soon."}{" "}
            Check out as a guest; don't sign in to BC Ferries there.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="success" onClick={() => navigate(`/c/${trip.trip.id}`)}>
          Open checkout <ChevronRight aria-hidden />
        </Button>
        <Button variant="outline" disabled={release.isPending} onClick={() => release.mutate(undefined)}>
          Release
        </Button>
      </div>
    </div>
  );
}

function BookedQuestion({ trip }: { trip: MyTrip }) {
  const [explaining, setExplaining] = useState(false);
  const answer = useMyMutation((a: { booked: boolean; reason?: BookedReason }) =>
    myApi.booked(trip.trip.id, a.booked, a.reason),
  );
  if (trip.paused_reason !== "after_checkout") return null;
  return (
    <div className="space-y-3 rounded-xl border border-warning bg-warning-soft p-4">
      <p className="font-semibold">Did you book it?</p>
      <p className="text-sm text-muted-foreground">This trip is paused until you say, so it doesn't book twice.</p>
      {explaining ? (
        <div className="flex flex-wrap gap-2">
          {REASONS.map((r) => (
            <Button
              key={r.value}
              variant="outline"
              size="sm"
              disabled={answer.isPending}
              onClick={() => answer.mutate({ booked: false, reason: r.value })}
            >
              {r.label}
            </Button>
          ))}
          <Button size="sm" variant="ghost" onClick={() => answer.mutate({ booked: false })}>
            Skip
          </Button>
        </div>
      ) : (
        <div className="flex gap-2">
          <Button disabled={answer.isPending} onClick={() => answer.mutate({ booked: true })}>
            Yes, I booked it
          </Button>
          <Button variant="outline" onClick={() => setExplaining(true)}>
            No, keep watching
          </Button>
        </div>
      )}
    </div>
  );
}

export function MyTripCard({
  trip,
  terminals,
  onEdit,
}: {
  trip: MyTrip;
  terminals: readonly Terminal[] | undefined;
  onEdit: () => void;
}) {
  const badge = tripBadge(trip);
  const check = useMyMutation(() => myApi.checkNow(trip.trip.id), "Checking now");
  const held = trip.browser.cart?.status === "ready";
  const pause = useMyMutation(
    (enabled: boolean) => myApi.updateTrip(trip.trip.id, { enabled }),
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
  return (
    <Card className="space-y-4 p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate font-semibold">{tripTitle(trip, terminals)}</h2>
          <p className="text-sm text-muted-foreground">
            {timeLabel(trip.trip.earliest)}–{timeLabel(trip.trip.latest)}
            {checked ? ` · checked ${clock(checked * 1000)}` : ""}
            {next ? ` · next ${clock(next * 1000)}` : ""}
          </p>
        </div>
        <Badge variant={badge.tone}>{badge.text}</Badge>
      </div>
      <BookedQuestion trip={trip} />
      <HeldCart trip={trip} />
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
      </div>
    </Card>
  );
}
