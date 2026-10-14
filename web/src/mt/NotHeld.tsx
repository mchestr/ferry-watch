/**
 * A bookable sailing that nothing holds: why, "Hold it for me", and BC Ferries' own booking form. Shown on
 * the trip card and on /c/<trip>, where a "Bookable" alert lands.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Loader2, ShoppingCart } from "lucide-react";
import { toast } from "sonner";
import { Button, LinkButton } from "@/components/ui/button";
import { reportError, useNow } from "@/lib/hooks";
import { timeLabel } from "@/lib/time";
import { MY_STATE_KEY, myApi } from "./api";
import type { MyTrip } from "./schema";
import { holdPending, notHeldReason, unheld } from "./trips";

function times(departures: string[]): string {
  const shown = departures.slice(0, 3).map(timeLabel).join(", ");
  return departures.length > 3 ? `${shown} and ${departures.length - 3} more` : shown;
}

export function NotHeld({ trip }: { trip: MyTrip }) {
  const now = useNow(5_000);
  const qc = useQueryClient();
  const departures = unheld(trip);
  const failed = trip.browser.cart?.status === "failed" ? trip.browser.cart : null;
  const hold = useMutation({
    mutationFn: () => myApi.hold(trip.trip.id),
    onSuccess: (state) => {
      qc.setQueryData(MY_STATE_KEY, state);
      toast.success("Trying to hold it");
    },
    onError: (error) => reportError(qc, error),
  });
  const dismiss = useMutation({
    mutationFn: () => myApi.dismiss(trip.trip.id, failed?.id ?? ""),
    onSuccess: (state) => qc.setQueryData(MY_STATE_KEY, state),
    onError: (error) => reportError(qc, error),
  });
  // Asked, and no check since has acted on it: it's on its way (a new pod may still be starting, with no
  // sailings yet). After that check, no cart means it couldn't, and the reason and the button come back.
  const trying = hold.isPending || holdPending(trip, now);
  if (departures.length === 0 && !trying) return null;
  const verb = departures.length === 1 ? "is" : "are";
  const target = trip.hold.holdable[0]; // the pod holds the earliest it still may
  const which = departures.length <= 1 || !target ? "it" : timeLabel(target);
  return (
    <div className="space-y-3 rounded-xl border border-warning bg-warning-soft p-4">
      <div className="flex items-start gap-3">
        <ShoppingCart className="mt-0.5 size-5 text-warning" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-semibold">
            {departures.length ? `${times(departures)} ${verb} bookable, not held` : "Trying to hold it"}
          </p>
          <p className="text-sm text-muted-foreground" role="status">
            {trying
              ? `Trying to hold ${which} now. This page updates when it's held.`
              : trip.hold.can_request
                ? `${notHeldReason(trip)} Hold ${which} for me, or book it yourself.`
                : `${notHeldReason(trip)} You can still book it yourself.`}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {trip.hold.can_request && (
          <Button variant="success" disabled={trying} onClick={() => hold.mutate()}>
            {trying ? <Loader2 className="motion-safe:animate-spin" aria-hidden /> : <ShoppingCart aria-hidden />}
            {trying ? `Holding ${which}…` : `Hold ${which} for me`}
          </Button>
        )}
        {trip.booking_url && (
          <LinkButton variant="outline" href={trip.booking_url} target="_blank" rel="noopener noreferrer">
            Book on bcferries.com <ExternalLink aria-hidden />
          </LinkButton>
        )}
        {failed && !trying && (
          <Button variant="ghost" disabled={dismiss.isPending} onClick={() => dismiss.mutate()}>
            Dismiss
          </Button>
        )}
      </div>
    </div>
  );
}
