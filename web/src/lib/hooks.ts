import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { errorMessage, fetchTerminals, isAuthError } from "./api";

export const TERMINALS_KEY = ["terminals"] as const;
/** How long the terminal list is fresh: after that, coming back to the app fetches it again (waitlist-web reloads
 * it from BC Ferries daily, so a page left open for days still learns of a new route). */
export const TERMINALS_STALE_MS = 3_600_000;

export function useTerminals() {
  return useQuery({
    queryKey: TERMINALS_KEY,
    queryFn: ({ signal }) => fetchTerminals(signal),
    staleTime: TERMINALS_STALE_MS,
    // The server gets the list from BC Ferries and answers 503 while it can't. Keep trying, backing off to 30 s.
    retry: (_failures, error) => !isAuthError(error),
    retryDelay: (attempt) => Math.min(30_000, 1_000 * 2 ** attempt),
  });
}

export const ROUTE_MAP_KEY = ["route-map"] as const;

/** The route map's data and maths (lib/route-map), loaded the first time a map is shown. */
export function useRouteMap() {
  return useQuery({
    queryKey: ROUTE_MAP_KEY,
    queryFn: () => import("./route-map"),
    staleTime: Infinity,
    gcTime: Infinity,
    // It's only a picture: if it can't load, the sheet goes without it.
    retry: false,
  });
}

/**
 * The default handling for a failed mutation: a toast with the server's message. An expired sign-in gets none;
 * the 401 has already started signing in again (configureBearerAuth).
 */
export function reportError(error: unknown): void {
  if (isAuthError(error)) return;
  toast.error(errorMessage(error));
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
