import { QueryClient } from "@tanstack/react-query";
import { isAuthError } from "./api";

/** The app's QueryClient: one retry for a failed query unless the sign-in needs renewing, refetch on focus, and no
 * retried mutations. A query that must keep trying (the terminal list) says so itself (lib/hooks.ts). */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: (failures, error) => !isAuthError(error) && failures < 1, refetchOnWindowFocus: true },
      mutations: { retry: false },
    },
  });
}
