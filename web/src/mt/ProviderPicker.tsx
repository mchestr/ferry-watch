/**
 * Step 1 of a new watch (docs/waitlist/design.md §7.3): one tile per provider offered to this user and known to this
 * build, the last used first. A provider at its limit is shown but disabled, with why; a paused one stays pickable.
 * Each tile says what the provider is (its description's first sentence) and, in a chip, what it does (its tagline).
 */
import { ChevronRight } from "lucide-react";
import { SheetBody, SheetHeader } from "@/components/ui/sheet";
import { providerUi } from "@/providers/registry";
import { capacityFor, whatItIs } from "./watches";
import type { ProviderInfo, WaitlistState } from "./watch-schema";

export function ProviderPicker({
  state,
  providers,
  onPick,
}: {
  state: WaitlistState;
  /** The pickable providers, in order (watches.pickable). */
  providers: readonly ProviderInfo[];
  onPick: (provider: string) => void;
}) {
  return (
    <>
      <SheetHeader title="New watch" description="What would you like an alert for?" />
      <SheetBody className="py-5">
        {providers.length === 0 ? (
          <p className="text-sm text-muted-foreground">There's nothing you can watch right now.</p>
        ) : (
          <ul className="space-y-3" aria-label="Providers">
            {providers.map((provider) => {
              const ui = providerUi(provider.id);
              if (!ui) return null;
              const Icon = ui.icon;
              const room = capacityFor(state, provider.id);
              return (
                <li key={provider.id}>
                  <button
                    type="button"
                    disabled={!room.canAdd}
                    onClick={() => onPick(provider.id)}
                    className="group flex w-full items-start gap-3 rounded-2xl border bg-card p-4 text-left shadow-sm transition-colors hover:border-primary/40 hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:hover:border-border disabled:hover:bg-card"
                  >
                    <span
                      className={
                        room.canAdd
                          ? "grid size-11 shrink-0 place-items-center rounded-xl bg-accent text-accent-foreground"
                          : "grid size-11 shrink-0 place-items-center rounded-xl bg-muted text-muted-foreground"
                      }
                    >
                      <Icon className="size-5" aria-hidden />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span
                        className={room.canAdd ? "block font-semibold" : "block font-semibold text-muted-foreground"}
                      >
                        {provider.name}
                      </span>
                      <span className="mt-0.5 block text-sm text-muted-foreground">
                        {whatItIs(provider.description)}
                      </span>
                      <span className="mt-2 inline-flex rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-foreground/80">
                        {ui.tagline}
                      </span>
                      {provider.paused && (
                        <span className="mt-1.5 block text-xs font-medium text-warning">
                          Checks are paused by an admin
                        </span>
                      )}
                      {!room.canAdd && room.reason && (
                        <span className="mt-1.5 block text-xs font-medium text-danger">{room.reason}</span>
                      )}
                    </span>
                    {room.canAdd && (
                      <ChevronRight
                        className="mt-3 size-5 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5"
                        aria-hidden
                      />
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </SheetBody>
    </>
  );
}
