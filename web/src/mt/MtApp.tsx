/**
 * The app as waitlist-web serves it: each person signs in (OIDC) and sees only their own watches, on every provider
 * (docs/multi-tenant/design-reference.md §10, docs/waitlist/design.md §7).
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, BellRing, Loader2, LogOut, Settings, Shield } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { configureBearerAuth, errorMessage, hasStatus, type AuthReason } from "@/lib/api";
import { useTerminals } from "@/lib/hooks";
import { AccountCard } from "./AccountCard";
import { AdminPage } from "./AdminPage";
import { MY_STATE_KEY, myApi } from "./api";
import { completeSignIn, isSignedOut, markSignedOut, signOut, startSignIn, token } from "./auth";
import { CheckoutPage } from "./CheckoutPage";
import { Dashboard } from "./Dashboard";
import { useMyLive } from "./live";
import { WatchSheets } from "./NewWatchSheet";
import { navigate, useRoute } from "./router";
import { WatchPage } from "./WatchPage";

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-24 text-center">{children}</div>;
}

function SignedOut({ message }: { message?: string }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <Centered>
      <div className="grid size-12 place-items-center rounded-2xl bg-primary text-primary-foreground">
        <BellRing className="size-6" aria-hidden />
      </div>
      <h1 className="text-xl font-bold">Waitlist</h1>
      <p className="text-sm text-muted-foreground">
        {message ?? "Get an alert the moment a reservation opens: BC Ferries sailings and Buntzen Lake passes."}
      </p>
      <Button
        size="lg"
        onClick={() => {
          startSignIn("/").catch((e: unknown) => setError(errorMessage(e)));
        }}
      >
        Sign in
      </Button>
      {error && <p className="text-sm text-danger">{error}</p>}
    </Centered>
  );
}

function Callback({ onSignedIn }: { onSignedIn: () => void }) {
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const fragment = location.hash;
    history.replaceState(null, "", "/auth/callback"); // the code leaves the address bar at once
    completeSignIn(fragment)
      .then((back) => {
        onSignedIn();
        navigate(back, { replace: true });
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [onSignedIn]);
  if (error) return <SignedOut message={error} />;
  return (
    <Centered>
      <Loader2 className="size-6 motion-safe:animate-spin" aria-hidden />
      <p className="text-sm text-muted-foreground">Signing you in…</p>
    </Centered>
  );
}

function SignedIn() {
  const route = useRoute();
  const live = useMyLive();
  const query = useQuery({
    queryKey: MY_STATE_KEY,
    queryFn: ({ signal }) => myApi.state(signal),
    refetchInterval: live ? false : 15_000,
  });
  // The one terminals query (lib/hooks): the same key with other options would stop its retries after one.
  const terminals = useTerminals();
  const state = query.data;

  if (!state) {
    if (query.error && hasStatus(query.error, 403)) return <SignedOut message={errorMessage(query.error)} />;
    return (
      <Centered>
        {query.error ? (
          <>
            <AlertTriangle className="size-8 text-danger" aria-hidden />
            <p className="text-sm text-muted-foreground">{errorMessage(query.error)}. Retrying…</p>
          </>
        ) : (
          <Loader2 className="size-6 motion-safe:animate-spin" aria-hidden />
        )}
      </Centered>
    );
  }

  return (
    <WatchSheets state={state} route={route}>
      <div className="min-h-screen">
        <header className="border-b bg-card">
          <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
            <button type="button" className="flex items-center gap-2 font-bold" onClick={() => navigate("/")}>
              <span className="grid size-7 place-items-center rounded-lg bg-primary text-primary-foreground">
                <BellRing className="size-4" aria-hidden />
              </span>
              Waitlist
            </button>
            <div className="flex items-center gap-1">
              <span className="hidden text-sm text-muted-foreground sm:inline">
                {state.me.display ?? state.me.username}
              </span>
              {state.me.is_admin && (
                <Button variant="ghost" size="icon" aria-label="Admin" onClick={() => navigate("/admin")}>
                  <Shield aria-hidden />
                </Button>
              )}
              <Button variant="ghost" size="icon" aria-label="Settings" onClick={() => navigate("/settings")}>
                <Settings aria-hidden />
              </Button>
              <Button variant="ghost" size="icon" aria-label="Sign out" onClick={() => void signOut()}>
                <LogOut aria-hidden />
              </Button>
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
          {route.page === "checkout" ? (
            <CheckoutPage state={state} tripId={route.tripId} terminals={terminals.data} />
          ) : route.page === "watch" ? (
            <WatchPage state={state} watchId={route.watchId} />
          ) : route.page === "admin" && state.me.is_admin ? (
            <AdminPage meId={state.me.id} />
          ) : route.page === "settings" ? (
            <div className="mx-auto max-w-xl">
              <AccountCard state={state} />
            </div>
          ) : (
            <Dashboard state={state} />
          )}
        </main>
      </div>
    </WatchSheets>
  );
}

// Every request carries the token from the start. Wiring it up in MtApp's effect was too late: React runs the
// children's effects first, so the page's first queries went out without it, and their 401s signed the user out.
let onUnauthorized: (reason: AuthReason) => void = () => undefined;
configureBearerAuth(token, (reason) => onUnauthorized(reason));

export function MtApp() {
  const route = useRoute();
  const [signedOut, setSignedOut] = useState(() => !token() || isSignedOut());
  const signedIn = useCallback(() => setSignedOut(false), []);

  useEffect(() => {
    onUnauthorized = (reason) => {
      if ((reason === "expired" || reason === "reverify") && !isSignedOut()) {
        // A quiet trip through the identity provider, back to where the user was (a cart alert's /c/<trip>).
        void startSignIn().catch(() => setSignedOut(true));
      } else {
        markSignedOut();
        setSignedOut(true);
      }
    };
  }, []);

  if (route.page === "callback") return <Callback onSignedIn={signedIn} />;
  if (signedOut) return <SignedOut />;
  return <SignedIn />;
}
