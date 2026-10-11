import { AlertTriangle, Clock, KeyRound, RotateCw } from "lucide-react";
import type * as React from "react";
import { ApiError } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import type { LiveState } from "@/lib/live";
import type { Snapshot } from "@/lib/state";
import { ago, clock, serverToClientMs } from "@/lib/time";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";

/** No word from the server for this long means the data on screen may be out of date. */
export const STALE_AFTER_MS = 45_000;
/** After this many failed WebSocket attempts, a failing HTTP request may mean the sign-in expired. */
export const SOCKET_FAILURES_BEFORE_SIGN_IN_HINT = 3;

const reload = () => window.location.reload();

function Banner({
  tone,
  icon,
  role,
  children,
  action,
}: {
  tone: "danger" | "warning";
  icon: React.ReactNode;
  role: "alert" | "status";
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div role={role} className={cn("border-b", tone === "danger" ? "bg-danger-soft" : "bg-warning-soft")}>
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-2.5 sm:px-6">
        <div className="flex min-w-0 items-start gap-2.5 text-sm">
          <span
            aria-hidden
            className={cn("mt-0.5 shrink-0 [&_svg]:size-4", tone === "danger" ? "text-danger" : "text-warning")}
          >
            {icon}
          </span>
          <p className="min-w-0">{children}</p>
        </div>
        {action}
      </div>
    </div>
  );
}

function ReloadButton() {
  return (
    <Button size="sm" variant="outline" onClick={reload}>
      <RotateCw aria-hidden /> Reload
    </Button>
  );
}

/**
 * Sign-in problems first, then out-of-date data. A 401 or an opaque redirect means the proxy's
 * session expired. When the WebSocket keeps failing and HTTP also fails without a definite
 * answer from the server, the sign-in may have expired too, so that offers a reload as well.
 */
export function ConnectionBanner({
  snapshot,
  live,
  error,
}: {
  snapshot: Snapshot | undefined;
  live: LiveState;
  error: Error | null;
}) {
  const now = useNow(5_000);
  const apiError = error instanceof ApiError ? error : null;

  if (apiError?.kind === "auth") {
    return (
      <Banner tone="danger" role="alert" icon={<KeyRound />} action={<ReloadButton />}>
        <span className="font-semibold text-danger">Your sign-in expired.</span> Reload to sign in again.
      </Banner>
    );
  }
  const signInSuspect =
    live.failures >= SOCKET_FAILURES_BEFORE_SIGN_IN_HINT &&
    (apiError?.kind === "network" || apiError?.kind === "format") &&
    navigator.onLine;
  if (signInSuspect) {
    return (
      <Banner tone="danger" role="alert" icon={<KeyRound />} action={<ReloadButton />}>
        <span className="font-semibold text-danger">Can't reach Ferry Watch.</span> Your sign-in may have expired:
        reload to sign in again.
      </Banner>
    );
  }
  if (!snapshot) return null;

  // While messages are arriving, what's on screen is current.
  const since = live.status === "live" ? now : Math.max(snapshot.receivedAt, live.lastMessageAt ?? 0);
  const stale = now - since > STALE_AFTER_MS;
  if (!stale && !error) return null;

  const age = ago(since, now);
  const detail = error ? `${error.message}.` : "Reconnecting…";
  const contractProblem = apiError?.kind === "format" || apiError?.kind === "schema";
  return (
    <Banner tone="warning" role="status" icon={<Clock />} action={contractProblem ? <ReloadButton /> : undefined}>
      {live.status === "live" && !stale ? (
        <>
          <span className="font-semibold">Couldn't refresh from the server.</span> {detail} Live updates are still
          arriving.
        </>
      ) : (
        <>
          <span className="font-semibold">Showing data from {age === "just now" ? "a moment ago" : age}.</span> {detail}
        </>
      )}
    </Banner>
  );
}

export function WatcherStalledBanner({ heartbeatAt, skew }: { heartbeatAt: number; skew: number }) {
  return (
    <Banner tone="danger" role="alert" icon={<AlertTriangle />}>
      <span className="font-semibold text-danger">The watcher has stalled,</span> so trips aren't being checked. Its
      last heartbeat was at {clock(serverToClientMs(heartbeatAt, skew))}.
    </Banner>
  );
}
