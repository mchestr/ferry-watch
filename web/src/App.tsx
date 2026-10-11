import { AlertTriangle, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ActivityCard } from "./components/ActivityCard";
import { Header } from "./components/Header";
import { HoldBanner } from "./components/HoldBanner";
import { SailingsCard } from "./components/SailingsCard";
import { TripHero } from "./components/TripHero";
import { TripSheet } from "./components/TripSheet";
import { WatcherCard } from "./components/WatcherCard";
import { useAppState, useTerminals } from "./lib/hooks";

export default function App() {
  const { data: state, error, isLoading } = useAppState();
  const { data: terminals } = useTerminals();
  const [editing, setEditing] = useState(false);

  // Server clock offset, so countdowns and timestamps line up with the watcher.
  const skew = state ? Date.now() / 1000 - state.now : 0;

  // First run: open the editor so the trip gets configured.
  const opened = useRef(false);
  useEffect(() => {
    if (state && !opened.current) {
      opened.current = true;
      if (!state.last_check && !state.sailings.length && !state.checking) setEditing(true);
    }
  }, [state]);

  return (
    <div className="min-h-screen">
      <Header state={state} />
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
            <div className="min-w-0 space-y-6">
              <TripHero state={state} terminals={terminals} onEdit={() => setEditing(true)} />
              <HoldBanner state={state} />
              <SailingsCard state={state} />
            </div>
            <aside className="space-y-6">
              <WatcherCard state={state} skew={skew} />
              <ActivityCard events={state.events} skew={skew} />
            </aside>
          </div>
        )}
      </main>
      {state && <TripSheet open={editing} onOpenChange={setEditing} config={state.config} terminals={terminals} loginConfigured={state.login} />}
    </div>
  );
}
