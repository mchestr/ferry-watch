/**
 * The app as ferry-web serves it: each person signs in (OIDC) and sees only their own trips
 * (docs/multi-tenant/design-reference.md §10).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, LogOut, Plus, Settings, Shield, Ship } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { TripSheetView, type SheetMode, type TripSource } from "@/components/TripSheet";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { configureBearerAuth, errorMessage, hasStatus, type AuthReason } from "@/lib/api";
import type { TripSettings } from "@/lib/schema";
import { timeLabel } from "@/lib/time";
import { AccountCard } from "./AccountCard";
import { AdminPage } from "./AdminPage";
import { MY_STATE_KEY, myApi } from "./api";
import { completeSignIn, isSignedOut, markSignedOut, signOut, startSignIn, token } from "./auth";
import { CheckoutPage } from "./CheckoutPage";
import { useMyLive } from "./live";
import { MyTripCard } from "./MyTripCard";
import { navigate, useRoute } from "./router";
import type { MyState } from "./schema";
import { tripTitle } from "./trips";

/** A new trip's defaults; the form fills in the date. */
const DEFAULTS: TripSettings = {
  enabled: true,
  from: "",
  to: "",
  date: "",
  adults: 1,
  children: 0,
  infants: 0,
  seniors: 0,
  vehicle: "standard",
  height_ft: 8,
  earliest: "00:00",
  latest: "23:59",
  auto_cart: true,
  fare: "cheapest",
};

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-24 text-center">{children}</div>;
}

function SignedOut({ message }: { message?: string }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <Centered>
      <div className="grid size-12 place-items-center rounded-2xl bg-accent text-accent-foreground">
        <Ship className="size-6" aria-hidden />
      </div>
      <h1 className="text-xl font-bold">Ferry Watch</h1>
      <p className="text-sm text-muted-foreground">{message ?? "Sign in to watch your BC Ferries sailings."}</p>
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

function useTripSource(state: MyState | undefined, terminals: ReturnType<typeof useTerminalList>): TripSource {
  const qc = useQueryClient();
  return useMemo(() => {
    const write = (next: MyState) => qc.setQueryData(MY_STATE_KEY, next);
    return {
      defaults: DEFAULTS,
      trips: (state?.trips ?? []).map((t) => ({
        ...t.trip,
        title: tripTitle(t, terminals.data),
        cart: t.browser.cart,
      })),
      save: async (request) => {
        write(
          request.kind === "add"
            ? await myApi.addTrip(request.settings)
            : await myApi.updateTrip(request.id, request.patch),
        );
      },
      remove: async (id) => {
        write(await myApi.removeTrip(id));
      },
      autoCartHint: "Holds the sailing in a guest cart; you check out in a browser we keep for you.",
      addedMessage: "Trip added. It'll be checked shortly.",
    };
  }, [qc, state, terminals.data]);
}

function useTerminalList() {
  return useQuery({ queryKey: ["terminals"], queryFn: ({ signal }) => myApi.terminals(signal), staleTime: 3_600_000 });
}

function SignedIn() {
  const route = useRoute();
  const live = useMyLive();
  const query = useQuery({
    queryKey: MY_STATE_KEY,
    queryFn: ({ signal }) => myApi.state(signal),
    refetchInterval: live ? false : 15_000,
  });
  const terminals = useTerminalList();
  const state = query.data;
  const source = useTripSource(state, terminals);
  const [sheet, setSheet] = useState<SheetMode>({ kind: "closed" });
  const closeSheet = useCallback(() => setSheet({ kind: "closed" }), []);

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
    <div className="min-h-screen">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <button type="button" className="flex items-center gap-2 font-bold" onClick={() => navigate("/")}>
            <Ship className="size-5" aria-hidden /> Ferry Watch
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
        ) : route.page === "admin" && state.me.is_admin ? (
          <AdminPage meId={state.me.id} />
        ) : route.page === "settings" ? (
          <div className="mx-auto max-w-xl">
            <AccountCard state={state} />
          </div>
        ) : (
          <div className="mx-auto max-w-3xl space-y-4">
            <div className="flex items-end justify-between gap-4">
              <div>
                <h1 className="text-xl font-bold tracking-tight">Your trips</h1>
                <p className="text-sm text-muted-foreground">
                  {state.trips.length} of {state.limits.maxTrips?.value ?? 3} watched
                  {state.quiet_hours?.now && (
                    <>
                      {" · "}
                      <span className="whitespace-nowrap">quiet until {timeLabel(state.quiet_hours.end)}</span>
                    </>
                  )}
                </p>
              </div>
              <Button onClick={() => setSheet({ kind: "adding" })} disabled={!state.capacity.can_add}>
                <Plus aria-hidden /> Add trip
              </Button>
            </div>
            {state.trips.length ? (
              state.trips.map((trip) => (
                <MyTripCard
                  key={trip.trip.id}
                  trip={trip}
                  terminals={terminals.data}
                  onEdit={() => setSheet({ kind: "editing", tripId: trip.trip.id })}
                />
              ))
            ) : (
              <Card className="px-6 py-16 text-center">
                <h2 className="font-semibold">No trips yet</h2>
                <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
                  Add a route and a date within the next 30 days, and you'll hear the moment a reservation opens up.
                </p>
              </Card>
            )}
          </div>
        )}
      </main>
      <TripSheetView mode={sheet} onClose={closeSheet} source={source} />
    </div>
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
