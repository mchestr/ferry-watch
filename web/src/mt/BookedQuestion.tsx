/**
 * "Did you book it?": asked after a checkout the user looked at, on the trip card and on /c/<trip> (where the
 * question's alert lands). The trip stays paused until it's answered, so it doesn't book twice.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { reportError } from "@/lib/hooks";
import { MY_STATE_KEY, myApi, type BookedAnswer, type BookedReason } from "./api";
import type { MyTrip } from "./schema";

const REASONS: readonly { value: BookedReason; label: string }[] = [
  { value: "needed_account", label: "It wanted an account" },
  { value: "payment_failed", label: "Payment didn't go through" },
  { value: "checkout_broke", label: "Checkout broke" },
  { value: "ran_out_of_time", label: "Ran out of time" },
  { value: "changed_mind", label: "Changed my mind" },
];

interface Props {
  trip: MyTrip;
  onAnswered?: (answer: BookedAnswer) => void;
}

/** `onAnswered` runs once the answer is saved (after "Yes" or "No, cancel", the trip is gone: see endsTrip). */
export function BookedQuestion(props: Props) {
  // Mounted only while it's asked, so the next checkout's question starts afresh, not on "why not?".
  return props.trip.paused_reason === "after_checkout" ? <Question {...props} /> : null;
}

function Question({ trip, onAnswered }: Props) {
  const qc = useQueryClient();
  const [explaining, setExplaining] = useState(false);
  const answer = useMutation({
    mutationFn: (a: BookedAnswer) => myApi.booked(trip.trip.id, a),
    onSuccess: (state, a) => {
      qc.setQueryData(MY_STATE_KEY, state);
      if ("stop" in a) toast.success("Stopped watching");
      onAnswered?.(a);
    },
    onError: (error) => reportError(error),
  });
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
          <Button
            size="sm"
            variant="ghost"
            disabled={answer.isPending}
            onClick={() => answer.mutate({ booked: false })}
          >
            Skip
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button disabled={answer.isPending} onClick={() => answer.mutate({ booked: true })}>
            Yes, booked
          </Button>
          <Button variant="outline" disabled={answer.isPending} onClick={() => setExplaining(true)}>
            No, keep watching
          </Button>
          <Button
            variant="outline"
            disabled={answer.isPending}
            onClick={() => answer.mutate({ booked: false, stop: true })}
          >
            No, cancel
          </Button>
        </div>
      )}
    </div>
  );
}
