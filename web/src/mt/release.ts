/**
 * Releasing a held cart. waitlist-web only passes the request on, so the cart stays held until the trip's browser
 * lets it go: until then the trip card and /c/<trip> both say "Releasing…".
 */
import { useMutation, useMutationState, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { reportError } from "@/lib/hooks";
import { MY_STATE_KEY, myApi } from "./api";
import type { CartInfo } from "./schema";

const RELEASE = "release";

export function useRelease(tripId: string, cart: CartInfo | null | undefined) {
  const qc = useQueryClient();
  const release = useMutation({
    // Per cart: a new cart for the trip can be released again.
    mutationKey: [RELEASE, tripId, cart?.id ?? ""],
    mutationFn: () => myApi.release(tripId, cart?.id ?? ""),
    onSuccess: (state) => {
      qc.setQueryData(MY_STATE_KEY, state);
      toast.success("Releasing the cart");
    },
    onError: (error) => reportError(error),
  });
  // Every release asked for, here or on the other page (a mutation outlives the page that ran it). The filter
  // stays fixed: useMutationState only looks again when a mutation changes, not when the cart does.
  const asked = useMutationState({
    filters: {
      mutationKey: [RELEASE],
      predicate: (m) => m.state.status === "pending" || m.state.status === "success",
    },
    select: (m) => m.options.mutationKey,
  });
  return {
    release: () => release.mutate(),
    releasing: cart?.status === "ready" && asked.some((key) => key?.[1] === tripId && key[2] === cart.id),
  };
}
