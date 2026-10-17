/**
 * New watch (docs/waitlist/design.md §7.3): a bottom sheet on phones and a dialog on wide screens. Step 1 picks a
 * provider (skipped for /new/<provider>, the empty state's buttons, or when only one is offered); step 2 is that
 * provider's form. The same sheet edits a watch and opens "Watch another day", from any card or page.
 *
 * /new and /new/<provider> open it over the dashboard; an entry the app pushed is popped again on close, so the
 * phone's back button closes it too. Edits and seeded forms open it in place, with no address of their own.
 */
import * as Dialog from "@radix-ui/react-dialog";
import { useIsMutating } from "@tanstack/react-query";
import { ChevronLeft, RotateCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { SHEET_MUTATION_KEY } from "@/components/TripSheet";
import { Button } from "@/components/ui/button";
import { SheetFooter, SheetHeader } from "@/components/ui/sheet";
import { providerUi } from "@/providers/registry";
import type { SheetMode } from "@/providers/types";
import { ProviderPicker } from "./ProviderPicker";
import { navigate, type Route } from "./router";
import { capacityFor, infoFor, pickable, rememberProvider, WatchSheetContext, type WatchSheetApi } from "./watches";
import type { ProviderInfo, WaitlistState, Watch } from "./watch-schema";

type Opened = { step: "picker" } | { step: "form"; provider: string; mode: SheetMode<Watch>; back: boolean };

/** Marks the history entries this sheet pushes, so closing it can pop them. */
const SHEET_ENTRY = { wlSheet: true };
const pushedHere = (): boolean => (history.state as { wlSheet?: unknown } | null)?.wlSheet === true;

/** Which step: a new key mounts a fresh form. */
const keyOf = (opened: Opened): string =>
  opened.step === "picker"
    ? "picker"
    : `${opened.provider}:${opened.mode.kind === "editing" ? opened.mode.watch.id : "new"}`;
const signature = (opened: Opened): string => (opened.step === "form" ? `${keyOf(opened)}:${opened.back}` : "picker");

/** Provides `useWatchSheet()` to everything inside, and shows the sheet over it. */
export function WatchSheets({ state, route, children }: { state: WaitlistState; route: Route; children: ReactNode }) {
  const [local, setLocal] = useState<Opened | null>(null);
  const [viaPicker, setViaPicker] = useState(false);
  const busy = useIsMutating({ mutationKey: [SHEET_MUTATION_KEY] }) > 0;
  const content = useRef<HTMLDivElement>(null);
  /** What had focus when the sheet opened: it gets it back on close. */
  const opener = useRef<HTMLElement | null>(null);
  const options = pickable(state, (id) => providerUi(id) !== null);

  // What the address asks for, decided as it's entered: a provider this user can't add a watch on falls back to the
  // picker, which says why. Not again on each new state, so the save that fills the last slot (or a watch added on
  // another device) can't swap the open form for the picker; the server refuses a save past the limit anyway.
  const entered = route.page === "new" ? (route.provider ?? "") : null;
  const [decided, setDecided] = useState<{ entered: string; target: string | null } | null>(null);
  let target: string | null = null;
  if (entered === null) {
    if (decided !== null) setDecided(null);
  } else if (decided?.entered === entered) {
    target = decided.target;
  } else {
    // /new with only one provider to offer goes straight to its form.
    const wanted = entered !== "" ? entered : options.length === 1 ? (options[0]?.id ?? null) : null;
    target =
      wanted !== null && options.some((p) => p.id === wanted) && capacityFor(state, wanted).canAdd ? wanted : null;
    setDecided({ entered, target });
  }
  let fromRoute: Opened | null = null;
  if (entered !== null) {
    fromRoute =
      target !== null
        ? { step: "form", provider: target, mode: { kind: "adding" }, back: viaPicker && options.length > 1 }
        : { step: "picker" };
  }
  const fallBack = entered !== null && entered !== "" && target === null;
  useEffect(() => {
    if (fallBack) navigate("/new", { replace: true, state: history.state });
  }, [fallBack]);
  useEffect(() => {
    if (route.page !== "new") setViaPicker(false);
  }, [route.page]);

  // While it animates closed, it keeps showing what it showed.
  const current = local ?? fromRoute;
  const [last, setLast] = useState<Opened | null>(null);
  if (current && (last === null || signature(last) !== signature(current))) setLast(current);
  const shown = current ?? last;

  const close = useCallback(() => {
    if (local) {
      setLocal(null);
    } else if (pushedHere()) {
      history.back();
    } else {
      navigate("/", { replace: true });
    }
  }, [local]);

  const api = useMemo<WatchSheetApi>(() => {
    const remember = () => {
      const focused = document.activeElement;
      if (focused instanceof HTMLElement && focused !== document.body && !content.current?.contains(focused)) {
        opener.current = focused;
      }
    };
    return {
      add: (provider, seed) => {
        remember();
        if (provider && seed) setLocal({ step: "form", provider, mode: { kind: "adding", seed }, back: false });
        else navigate(provider ? `/new/${provider}` : "/new", { state: SHEET_ENTRY });
      },
      edit: (watch) => {
        if (watch.kind === "unknown") return;
        remember();
        setLocal({ step: "form", provider: watch.kind, mode: { kind: "editing", watch }, back: false });
      },
    };
  }, []);

  // Radix would hand focus back to a Dialog.Trigger, and this sheet has none (the address or a card opens it): back
  // to what opened it, or, when that has gone or can't take it (opened from the address bar, New watch disabled at
  // the limit, a card that moved section), the page's heading, never the top of the document.
  const restoreFocus = (event: Event) => {
    event.preventDefault();
    const back = opener.current;
    opener.current = null;
    const target = back?.isConnected && !back.matches(":disabled") ? back : null;
    (target ?? document.querySelector<HTMLElement>("main h1"))?.focus();
  };

  const pick = (provider: string) => {
    rememberProvider(provider);
    setViaPicker(true);
    navigate(`/new/${provider}`, { replace: true, state: history.state });
  };

  return (
    <WatchSheetContext.Provider value={api}>
      {children}
      <Dialog.Root
        open={current !== null}
        onOpenChange={(open) => {
          if (!open && !busy) close();
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-slate-950/40 backdrop-blur-[2px] motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0 motion-safe:data-[state=closed]:animate-out motion-safe:data-[state=closed]:fade-out-0" />
          <Dialog.Content
            ref={content}
            // Focus the sheet, not its first button: a phone shouldn't open on a ringed close button.
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              content.current?.focus();
            }}
            onCloseAutoFocus={restoreFocus}
            className="fixed inset-x-0 bottom-0 z-50 flex max-h-[92dvh] flex-col overflow-hidden rounded-t-2xl outline-none bg-background shadow-2xl duration-300 motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:slide-in-from-bottom motion-safe:data-[state=closed]:animate-out motion-safe:data-[state=closed]:slide-out-to-bottom sm:inset-0 sm:m-auto sm:h-fit sm:max-h-[85vh] sm:w-[calc(100%-2rem)] sm:max-w-lg sm:rounded-2xl sm:border sm:duration-200 sm:motion-safe:data-[state=open]:slide-in-from-bottom-0 sm:motion-safe:data-[state=open]:fade-in-0 sm:motion-safe:data-[state=open]:zoom-in-95 sm:motion-safe:data-[state=closed]:slide-out-to-bottom-0 sm:motion-safe:data-[state=closed]:fade-out-0 sm:motion-safe:data-[state=closed]:zoom-out-95"
          >
            <div className="flex justify-center bg-card pt-2 sm:hidden" aria-hidden>
              <span className="h-1 w-10 rounded-full bg-muted-foreground/30" />
            </div>
            {shown && (
              <Step
                key={keyOf(shown)}
                opened={shown}
                state={state}
                options={options}
                onPick={pick}
                onBack={() => navigate("/new", { replace: true, state: history.state })}
                onDone={close}
              />
            )}
            <div className="bg-card pb-[env(safe-area-inset-bottom)] sm:hidden" aria-hidden />
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </WatchSheetContext.Provider>
  );
}

function Step({
  opened,
  state,
  options,
  onPick,
  onBack,
  onDone,
}: {
  opened: Opened;
  state: WaitlistState;
  options: readonly ProviderInfo[];
  onPick: (provider: string) => void;
  onBack: () => void;
  onDone: () => void;
}) {
  if (opened.step === "picker") return <ProviderPicker state={state} providers={options} onPick={onPick} />;
  const ui = providerUi(opened.provider);
  const info = infoFor(state, opened.provider);
  if (!ui || !info) {
    // Its catalog entry is missing (a newer server's that this build couldn't read, say): never an empty sheet.
    return (
      <>
        <SheetHeader
          title="Can't open this form"
          description="This version of Waitlist can't show it. Reload the page."
        />
        <SheetFooter>
          <Button variant="outline" onClick={onDone}>
            Close
          </Button>
          <Button onClick={() => window.location.reload()}>
            <RotateCw aria-hidden /> Reload
          </Button>
        </SheetFooter>
      </>
    );
  }
  const Form = ui.Sheet;
  return (
    <>
      {opened.back && (
        <div className="bg-card px-4 pt-2 sm:pt-3">
          <button
            type="button"
            onClick={onBack}
            className="flex items-center gap-0.5 rounded-md py-1 pr-2 text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronLeft className="size-4" aria-hidden /> All providers
          </button>
        </div>
      )}
      <Form mode={opened.mode} state={state} info={info} onDone={() => onDone()} />
    </>
  );
}
