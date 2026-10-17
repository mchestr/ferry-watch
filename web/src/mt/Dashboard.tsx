/**
 * The dashboard (docs/waitlist/design.md §7.2): every watch on every provider, in four sections: what needs the user
 * now, what's being watched, and (folded away) what's paused and what has ended.
 */
import * as Collapsible from "@radix-ui/react-collapsible";
import { ChevronRight, Plus } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Tooltip } from "@/components/ui/tooltip";
import { useMediaQuery, useNow } from "@/lib/hooks";
import { timeLabel } from "@/lib/time";
import { cn } from "@/lib/utils";
import { providerUi, uiFor } from "@/providers/registry";
import { AlertsBanner } from "./AlertsBanner";
import { WatchCard } from "./WatchCard";
import { capacityFor, nextHint, pickable, sections, SECTIONS, useWatchSheet } from "./watches";
import type { WaitlistState } from "./watch-schema";

export function Dashboard({ state }: { state: WaitlistState }) {
  const now = useNow(30_000);
  const wide = useMediaQuery("(min-width: 640px)");
  const groups = sections(state.watches, now, uiFor);
  const hint = groups.now.length === 0 ? nextHint(groups.watching, now, uiFor) : null;
  const count = state.watches.length;

  return (
    <div className={cn("mx-auto max-w-3xl space-y-5", !wide && count > 0 && "pb-20")}>
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          {/* Focusable from script: where focus goes when the New watch sheet closes and what opened it has gone. */}
          <h1 tabIndex={-1} className="text-xl font-bold tracking-tight outline-none">
            Your watches
          </h1>
          <p className="text-sm text-muted-foreground">
            {count} watched
            {state.quiet_hours?.now && (
              <>
                {" · "}
                <span className="whitespace-nowrap">quiet until {timeLabel(state.quiet_hours.end)} PT</span>
              </>
            )}
          </p>
          {hint && <p className="mt-1 text-sm text-foreground/80">{hint}</p>}
        </div>
        {wide && <NewWatchButton state={state} />}
      </div>
      <AlertsBanner state={state} />
      {count === 0 ? (
        <EmptyState state={state} />
      ) : (
        SECTIONS.map(({ group, title, collapsed }) => (
          // Always mounted, even empty, so a folded section knows when a watch moves into it.
          <Section key={group} title={title} count={groups[group].length} collapsible={collapsed}>
            {groups[group].map((watch) => (
              <WatchCard key={watch.id} watch={watch} state={state} ui={uiFor(watch)} />
            ))}
          </Section>
        ))
      )}
      {!wide && count > 0 && <NewWatchButton state={state} floating />}
    </div>
  );
}

/** New watch: in the heading on wide screens, floating at the bottom on phones. */
function NewWatchButton({ state, floating = false }: { state: WaitlistState; floating?: boolean }) {
  const sheet = useWatchSheet();
  const { can_add: canAdd, reason } = state.capacity;
  const button = (
    <Button
      size={floating ? "lg" : "default"}
      disabled={!canAdd}
      onClick={() => sheet.add(null)}
      className={cn(floating && "rounded-full shadow-lg")}
    >
      <Plus aria-hidden /> New watch
    </Button>
  );
  const shown =
    canAdd || !reason ? (
      button
    ) : (
      // A disabled button gets no pointer events: the tooltip hangs on a wrapper that can take focus.
      <Tooltip content={reason}>
        <span
          tabIndex={0}
          className="inline-flex rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {button}
        </span>
      </Tooltip>
    );
  return floating ? (
    <div className="fixed bottom-[max(1rem,env(safe-area-inset-bottom))] right-4 z-40">{shown}</div>
  ) : (
    shown
  );
}

function Section({
  title,
  count,
  collapsible,
  children,
}: {
  title: string;
  count: number;
  collapsible: boolean;
  children: ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  // Folded on arrival; a watch moving in while the page is open (say, just paused) unfolds it, so it doesn't vanish.
  const [seen, setSeen] = useState(count);
  if (count !== seen) {
    setSeen(count);
    if (count > seen) setOpen(true);
  }
  if (count === 0) return null;
  const label = "flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground";
  if (!collapsible) {
    return (
      <section aria-labelledby={id} className="space-y-3">
        <h2 id={id} className={label}>
          {title}
          <span className="tabular rounded-full bg-muted px-1.5 text-[11px]">{count}</span>
        </h2>
        {children}
      </section>
    );
  }
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} asChild>
      <section aria-labelledby={id} className="space-y-3">
        <h2 id={id}>
          <Collapsible.Trigger
            className={cn(
              label,
              "rounded py-0.5 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            <ChevronRight
              className={cn("size-3.5 motion-safe:transition-transform", open && "rotate-90")}
              aria-hidden
            />
            {title}
            <span className="tabular rounded-full bg-muted px-1.5 text-[11px]">{count}</span>
          </Collapsible.Trigger>
        </h2>
        <Collapsible.Content className="space-y-3">{children}</Collapsible.Content>
      </section>
    </Collapsible.Root>
  );
}

/** Nothing watched yet: one large button per provider this user can watch, straight to its form. */
function EmptyState({ state }: { state: WaitlistState }) {
  const sheet = useWatchSheet();
  const providers = pickable(state, (id) => providerUi(id) !== null);
  return (
    <Card className="px-5 py-10 text-center sm:px-8 sm:py-12">
      <h2 className="text-lg font-semibold">Nothing watched yet</h2>
      <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
        Pick what you'd like, and you'll hear the moment a reservation opens up.
      </p>
      {providers.length > 0 && (
        <div className="mx-auto mt-6 grid max-w-md gap-3">
          {providers.map((provider) => {
            const ui = providerUi(provider.id);
            if (!ui) return null;
            const Icon = ui.icon;
            return (
              <Button
                key={provider.id}
                size="lg"
                variant="outline"
                disabled={!capacityFor(state, provider.id).canAdd}
                onClick={() => sheet.add(provider.id)}
                className="h-auto justify-start gap-3 whitespace-normal rounded-xl px-4 py-3 text-left"
              >
                <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-accent text-accent-foreground [&_svg]:size-5">
                  <Icon aria-hidden />
                </span>
                <span className="min-w-0">
                  <span className="block text-[15px] font-semibold">{ui.cta}</span>
                  <span className="block text-xs font-normal text-muted-foreground">{ui.tagline}</span>
                </span>
              </Button>
            );
          })}
        </div>
      )}
    </Card>
  );
}
