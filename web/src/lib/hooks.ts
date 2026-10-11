import { useMutation, useQuery, useQueryClient, type MutationKey, type QueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { api, errorMessage, hasStatus, isAuthError } from "./api";
import type { State } from "./schema";
import { STATE_KEY, fetchSnapshot, writeState } from "./state";

/** Poll even while the WebSocket is live, in case a push went missing. */
export const SAFETY_POLL_MS = 60_000;
/** Poll this often while it isn't. */
export const FALLBACK_POLL_MS = 5_000;

export const TERMINALS_KEY = ["terminals"] as const;

export function useStateQuery(live: boolean) {
  const qc = useQueryClient();
  return useQuery({
    queryKey: STATE_KEY,
    queryFn: ({ signal }) => fetchSnapshot(qc, signal),
    refetchInterval: live ? SAFETY_POLL_MS : FALLBACK_POLL_MS,
    refetchIntervalInBackground: false,
    retry: (failures, error) => !isAuthError(error) && failures < 1,
  });
}

export function useTerminals() {
  return useQuery({
    queryKey: TERMINALS_KEY,
    queryFn: ({ signal }) => api.terminals(signal),
    staleTime: Infinity,
    // The server gets the list from BC Ferries and answers 503 while it can't. Keep trying, backing off to 30 s.
    retry: (_failures, error) => !isAuthError(error),
    retryDelay: (attempt) => Math.min(30_000, 1_000 * 2 ** attempt),
  });
}

/** The default handling for a failed mutation. An expired sign-in refetches the state, which raises the sign-in banner. */
export function reportError(qc: QueryClient, error: unknown): void {
  if (isAuthError(error)) {
    void qc.invalidateQueries({ queryKey: STATE_KEY });
    return;
  }
  toast.error(errorMessage(error));
}

interface StateMutationOptions<A> {
  mutationKey?: MutationKey;
  /** Toast once the server has accepted the change (none if the function returns null). */
  success?: string | ((arg: A, state: State) => string | null);
  /**
   * Handle an error before the default toast. Return true if it was handled (here, or in the
   * caller's own `mutate(arg, { onError })`) to skip the toast.
   */
  onError?: (error: unknown, arg: A) => boolean;
}

/**
 * A mutation whose response is the full new State: writes it to the cache (through the seq rule)
 * and toasts. These callbacks run even if the component unmounts; anything that touches one
 * particular view (closing a sheet, say) belongs in `mutate(arg, { onSuccess })`, which doesn't.
 */
export function useStateMutation<A = void>(fn: (arg: A) => Promise<State>, options: StateMutationOptions<A> = {}) {
  const qc = useQueryClient();
  const inFlight = useRef(false);
  const { mutationKey, success, onError } = options;
  const mutation = useMutation({
    mutationKey,
    mutationFn: fn,
    onSuccess: (state, arg) => {
      writeState(qc, state);
      const message = typeof success === "function" ? success(arg, state) : success;
      if (message) toast.success(message);
    },
    onError: (error, arg) => {
      if (!onError?.(error, arg)) reportError(qc, error);
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });
  const { mutate: send } = mutation;
  // One request at a time. isPending only updates a tick later (TanStack notifies on a timer), so a
  // quick double click would otherwise send the request twice before the button disables.
  const mutate = useCallback<typeof send>(
    (arg, callbacks) => {
      if (inFlight.current) return;
      inFlight.current = true;
      send(arg, callbacks);
    },
    [send],
  );
  return { ...mutation, mutate };
}

/** Removes a trip. A 404 means it's already gone, which is what the user wanted. */
export function useRemoveTrip(tripId: string, mutationKey?: MutationKey) {
  const qc = useQueryClient();
  return useStateMutation(() => api.removeTrip(tripId), {
    mutationKey,
    success: "Trip removed",
    onError: (error) => {
      if (!hasStatus(error, 404)) return false;
      toast.info("That trip was already removed.");
      void qc.invalidateQueries({ queryKey: STATE_KEY });
      return true;
    },
  });
}

/** Re-renders every `ms` so countdowns and "x ago" labels stay current. Keep it in small leaf components. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => false,
  );
}
