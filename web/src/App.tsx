import { AlertTriangle, Loader2, Plus, Ship } from "lucide-react";
import { useState } from "react";
import { ActivityCard } from "./components/ActivityCard";
import { Header } from "./components/Header";
import { TripCard } from "./components/TripCard";
import { TripSheet } from "./components/TripSheet";
import { Button } from "./components/ui/button";
import { Card } from "./components/ui/card";
import { WatcherCard } from "./components/WatcherCard";
import type { TripConfig } from "./lib/api";
import { useAppState, useLiveUpdates, useTerminals } from "./lib/hooks";
import { splitTerminal } from "./lib/utils";

export default function App() {
  const live = useLiveUpdates();
  const { data: state, error, isLoading } = useAppState(live);
  const { data: terminals } = useTerminals();
  // undefined = closed, null = adding, otherwise the trip being edited
  const [editing, setEditing] = useState<TripConfig | null | undefined>(undefined);

  // Server clock offset, so countdowns and timestamps line up with the watcher.
  const skew = state ? Date.now() / 1000 - state.now : 0;
  const canAdd = state ? state.trips.length < state.max_trips : false;

  const tripLabels = Object.fromEntries(
    (state?.trips ?? []).map((t) => {
      const day = new Date(`${t.date}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      return [t.id, `${splitTerminal(terminals?.[t.from]?.short)[0]} → ${splitTerminal(terminals?.[t.to]?.short)[0]} · ${day}`];
    }),
  );

  return (
    <div className="min-h-screen">
      <Header state={state} live={live} />
      <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
        {isLoading && (
          <div className="grid place-items-center py-32 text-muted-foreground">
            <Loader2 className="size-6 animate-spin" />
          </div>
        )}
        {error && !state && (
          <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-32 text-center">
            <AlertTriangle className="size-8 text-danger" />
            <div className="font-semibold">Can't reach the watcher</div>
            <p className="text-sm text-muted-foreground">{(error as Error).message}. Retrying…</p>
          </div>
        )}
        {state && (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
            <div className="min-w-0 space-y-4">
              <div className="flex items-end justify-between gap-4">
                <div>
                  <h1 className="text-xl font-bold tracking-tight">Your trips</h1>
                  <p className="text-sm text-muted-foreground">
                    {state.trips.length} of {state.max_trips} watched
                  </p>
                </div>
                <Button onClick={() => setEditing(null)} disabled={!canAdd}>
                  <Plus /> Add trip
                </Button>
              </div>
              {state.trips.length ? (
                state.trips.map((t) => (
                  <TripCard
                    key={t.id}
                    trip={t}
                    terminals={terminals}
                    watching={state.enabled}
                    vncUrl={state.vnc_url}
                    defaultOpen={state.trips.length === 1}
                    onEdit={() => setEditing(t)}
                  />
                ))
              ) : (
                <Card className="flex flex-col items-center gap-3 px-6 py-16 text-center">
                  <div className="grid size-12 place-items-center rounded-2xl bg-accent text-accent-foreground">
                    <Ship className="size-6" />
                  </div>
                  <div className="font-semibold">No trips yet</div>
                  <p className="max-w-sm text-sm text-muted-foreground">Add a route and date, and you'll get an alert the moment a reservation opens up.</p>
                  <Button onClick={() => setEditing(null)} className="mt-2">
                    <Plus /> Add your first trip
                  </Button>
                </Card>
              )}
            </div>
            <aside className="space-y-6">
              <WatcherCard state={state} skew={skew} />
              <ActivityCard events={state.events} skew={skew} tripLabels={tripLabels} />
            </aside>
          </div>
        )}
      </main>
      {state && (
        <TripSheet
          open={editing !== undefined}
          onOpenChange={(o) => !o && setEditing(undefined)}
          trip={editing ?? null}
          terminals={terminals}
          loginConfigured={state.login}
        />
      )}
    </div>
  );
}
