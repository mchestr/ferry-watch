import { AlertTriangle, Loader2, Plus, Ship } from "lucide-react";
import { useCallback, useState } from "react";
import { ActivityCard } from "./components/ActivityCard";
import { ConnectionBanner, WatcherStalledBanner } from "./components/Banners";
import { Header } from "./components/Header";
import { TripCard } from "./components/TripCard";
import { TripSheet, type SheetMode } from "./components/TripSheet";
import { Button } from "./components/ui/button";
import { Card } from "./components/ui/card";
import { WatcherCard } from "./components/WatcherCard";
import { errorMessage } from "./lib/api";
import { useStateQuery, useTerminals } from "./lib/hooks";
import { useLiveUpdates } from "./lib/live";
import { todayInPacific } from "./lib/time";

export default function App() {
  const live = useLiveUpdates();
  const query = useStateQuery(live.status === "live");
  // Load (and keep retrying) the terminal list up front, so the trip sheet opens ready.
  useTerminals();
  const snapshot = query.data;
  const state = snapshot?.state;
  const [sheet, setSheet] = useState<SheetMode>({ kind: "closed" });
  const closeSheet = useCallback(() => setSheet({ kind: "closed" }), []);
  const addTrip = () => setSheet({ kind: "adding" });

  const today = todayInPacific();
  const canAdd = state ? state.trips.length < state.max_trips : false;

  return (
    <div className="min-h-screen">
      <Header state={state} live={live.status} />
      <ConnectionBanner snapshot={snapshot} live={live} error={query.error} />
      {snapshot && !snapshot.state.watcher.healthy && (
        <WatcherStalledBanner heartbeatAt={snapshot.state.watcher.heartbeat_at} skew={snapshot.skew} />
      )}
      <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
        {!snapshot &&
          (query.isPending ? (
            <div className="grid place-items-center py-32 text-muted-foreground" role="status">
              <Loader2 className="size-6 motion-safe:animate-spin" aria-hidden />
              <span className="sr-only">Loading</span>
            </div>
          ) : (
            <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-32 text-center">
              <AlertTriangle className="size-8 text-danger" aria-hidden />
              <h1 className="font-semibold">Can't reach the watcher</h1>
              <p className="text-sm text-muted-foreground">{errorMessage(query.error)}. Retrying…</p>
            </div>
          ))}
        {snapshot && state && (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
            <div className="min-w-0 space-y-4">
              <div className="flex items-end justify-between gap-4">
                <div>
                  <h1 className="text-xl font-bold tracking-tight">Your trips</h1>
                  <p className="text-sm text-muted-foreground">
                    {state.trips.length} of {state.max_trips} watched
                    {!canAdd && state.trips.length > 0 && " · remove one to add another"}
                  </p>
                </div>
                <Button onClick={addTrip} disabled={!canAdd}>
                  <Plus aria-hidden /> Add trip
                </Button>
              </div>
              {state.trips.length ? (
                state.trips.map((trip) => (
                  <TripCard
                    key={trip.id}
                    trip={trip}
                    watching={state.enabled}
                    checkoutUrl={state.checkout_url}
                    skew={snapshot.skew}
                    today={today}
                    defaultOpen={state.trips.length === 1}
                    onEdit={() => setSheet({ kind: "editing", tripId: trip.id })}
                  />
                ))
              ) : (
                <Card className="flex flex-col items-center gap-3 px-6 py-16 text-center">
                  <div className="grid size-12 place-items-center rounded-2xl bg-accent text-accent-foreground">
                    <Ship className="size-6" aria-hidden />
                  </div>
                  <h2 className="font-semibold">No trips yet</h2>
                  <p className="max-w-sm text-sm text-muted-foreground">
                    Add a route and date, and you'll get an alert the moment a reservation opens up.
                  </p>
                  <Button onClick={addTrip} disabled={!canAdd} className="mt-2">
                    <Plus aria-hidden /> Add your first trip
                  </Button>
                </Card>
              )}
            </div>
            <aside className="space-y-6">
              <WatcherCard state={state} skew={snapshot.skew} />
              <ActivityCard events={state.events} skew={snapshot.skew} />
            </aside>
          </div>
        )}
      </main>
      {state && <TripSheet mode={sheet} onClose={closeSheet} state={state} />}
    </div>
  );
}
