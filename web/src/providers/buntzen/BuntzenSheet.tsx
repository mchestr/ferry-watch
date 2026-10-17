/**
 * The Buntzen Lake form in the New watch sheet (docs/waitlist/design.md §7.3): which day, which passes would do,
 * and the 6:55 reminder. Quick on a phone: New watch, Buntzen Lake, Tomorrow, Start watching. Edit mode sends only
 * what changed.
 */
import * as RadioGroup from "@radix-ui/react-radio-group";
import * as ToggleGroup from "@radix-ui/react-toggle-group";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { SHEET_MUTATION_KEY } from "@/components/TripSheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/controls";
import { SheetBody, SheetFooter, SheetHeader } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { ApiError } from "@/lib/api";
import { reportError, useNow } from "@/lib/hooks";
import { addDays, parseIsoDate, parseTimeOfDay, timeLabel, todayInPacific } from "@/lib/time";
import { MY_STATE_KEY, myApi } from "@/mt/api";
import { navigate } from "@/mt/router";
import { rememberProvider } from "@/mt/watches";
import {
  BUNTZEN_PASSES,
  BuntzenSpecSchema,
  type BuntzenPass,
  type BuntzenWatch,
  type WaitlistState,
} from "@/mt/watch-schema";
import type { SheetProps } from "../types";
import { CHIPS, chipDay, dayLabel, isPass, passesPhrase, RELEASE_LABEL } from "./labels";
import { inSeason, RELEASE_AT, REMINDER_AT, reminderAt, stillUseful } from "./season";

type Field = "date" | "passes";
const OTHER = "other";

/** The next days offered as chips after Today and Tomorrow ("Sat 3"). */
const MORE_DAYS = 5;

/** A seed from "Watch another day": only the keys and values a spec could have. */
function fromSeed(seed: Record<string, unknown> | undefined) {
  const parsed = BuntzenSpecSchema.partial().safeParse(seed ?? {});
  return parsed.success ? parsed.data : {};
}

/** Whether the user's quiet hours ("HH:MM", Pacific, possibly past midnight) cover `time`. */
function covers(quiet: { start: string; end: string }, time: string): boolean {
  const start = parseTimeOfDay(quiet.start);
  const end = parseTimeOfDay(quiet.end);
  const at = parseTimeOfDay(time);
  if (start === null || end === null || at === null || start === end) return false;
  return start < end ? start <= at && at < end : at >= start || at < end;
}

const canonical = (passes: readonly BuntzenPass[]) => BUNTZEN_PASSES.filter((p) => passes.includes(p));

export function BuntzenSheet({ mode, state, info, onDone }: SheetProps<BuntzenWatch>) {
  const now = useNow(30_000);
  const qc = useQueryClient();
  const ids = useId();
  const today = todayInPacific(new Date(now));
  const editing = mode.kind === "editing" ? mode.watch : null;
  const seed = mode.kind === "adding" ? fromSeed(mode.seed) : {};
  const horizon = addDays(today, info.horizon_days) ?? today;

  // The day chips: Today while a pass is still worth finding today, Tomorrow, then the next few days.
  const showToday = stillUseful(today, BUNTZEN_PASSES, now);
  const tomorrow = addDays(today, 1) ?? today;
  const later = Array.from({ length: MORE_DAYS }, (_, i) => addDays(today, i + 2) ?? today);
  const days: { value: string; label: string }[] = [
    ...(showToday ? [{ value: today, label: "Today" }] : []),
    { value: tomorrow, label: "Tomorrow" },
    ...later.map((d) => ({ value: d, label: chipDay(d) })),
  ];

  // What the form started from: edits send only what differs from it.
  const [base] = useState(() => editing?.spec ?? null);
  const [date, setDate] = useState<string>(() => base?.date ?? seed.date ?? "");
  const [other, setOther] = useState(() => date !== "" && !days.some((d) => d.value === date));
  const [passes, setPasses] = useState<BuntzenPass[]>(() => canonical(base?.passes ?? seed.passes ?? BUNTZEN_PASSES));
  const [reminder, setReminder] = useState<boolean>(() => base?.release_reminder ?? seed.release_reminder ?? true);
  const [errors, setErrors] = useState<Partial<Record<Field | "form", string>>>({});
  const inFlight = useRef(false);

  // Removed somewhere else (another device, say) while being edited: close, and say why.
  const gone = editing !== null && !state.watches.some((w) => w.id === editing.id);
  const closed = useRef(false);
  useEffect(() => {
    if (gone && !closed.current) {
      closed.current = true;
      toast.info("This watch was removed elsewhere.");
      onDone(null);
    }
  }, [gone, onDone]);

  /**
   * Where the form shows a failed save: a field's problem on that field, a refusal (a limit, a provider not offered)
   * above the buttons, a watch removed elsewhere by closing with a note. Null: a toast says it.
   */
  const placeOf = (error: Error): { field: Field } | "form" | "gone" | null => {
    if (!(error instanceof ApiError)) return null;
    if (error.field === "date" || error.field === "passes") return { field: error.field };
    if (error.status === 409 || error.status === 403) return "form";
    if (error.status === 404 && editing) return "gone";
    return null;
  };
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // The state and the toasts land even if the sheet has closed meanwhile (the phone's back button, say). Closing it
  // and the form's errors happen in save.mutate's callbacks, which only run while this form is mounted, so a late
  // answer can't close a sheet opened since.
  const save = useMutation({
    mutationKey: [SHEET_MUTATION_KEY],
    mutationFn: async (spec: Record<string, unknown>): Promise<{ id: string; state: WaitlistState }> => {
      if (editing) return { id: editing.id, state: await myApi.updateWatch(editing.id, { spec }) };
      const created = await myApi.addWatch({ provider: "buntzen", spec });
      return { id: created.watch_id, state: created.state };
    },
    onSuccess: ({ id, state: next }, spec) => {
      qc.setQueryData(MY_STATE_KEY, next);
      if (editing) {
        toast.success("Saved");
      } else {
        rememberProvider("buntzen");
        const day = typeof spec.date === "string" ? spec.date : date;
        toast.success(`Watching Buntzen Lake for ${dayLabel(day)}`, {
          action: { label: "View", onClick: () => navigate(`/w/${id}`) },
        });
      }
    },
    onError: (error) => {
      const place = placeOf(error);
      if (place === null) reportError(error);
      else if (!mounted.current) {
        // The form that would have shown it has gone: a toast instead.
        if (place === "gone") toast.info("This watch was removed elsewhere.");
        else reportError(error);
      }
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });
  const busy = save.isPending;

  const pickDay = (value: string) => {
    setErrors((e) => ({ ...e, date: undefined, passes: undefined, form: undefined }));
    if (value === OTHER) {
      setOther(true);
      // Focus stays on the chip: arrowing through the days lands here too, and mustn't be pulled out of the group.
      // The date it asks for is the next stop for Tab.
      if (days.some((d) => d.value === date)) setDate("");
      return;
    }
    setOther(false);
    setDate(value);
  };

  const validate = (): Partial<Record<Field, string>> => {
    if (!date) return { date: "Pick a day" };
    if (!parseIsoDate(date)) return { date: "Use a date like 2027-07-01" };
    // Unchanged from what it was, the date rules don't apply (the server's rule too).
    if (date === base?.date) return {};
    if (date < today) return { date: "Pick today or a later date" };
    if (date > horizon) return { date: `Pick a date from today to ${info.horizon_days} days ahead` };
    if (date === today && !stillUseful(today, passes, now)) return { passes: "Those passes are over for today" };
    return {};
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || inFlight.current) return;
    const problems = validate();
    if (problems.date || problems.passes) {
      setErrors(problems);
      // The control to fix: with Other date chosen, its date, not the chips.
      const at = problems.date ? (other ? `${ids}-date-input` : `${ids}-date`) : `${ids}-passes`;
      document.getElementById(at)?.focus();
      return;
    }
    const spec: Record<string, unknown> = { date, passes, release_reminder: reminder };
    if (base) {
      // PATCH only the keys that changed.
      if (date === base.date) delete spec.date;
      if (passes.join() === canonical(base.passes).join()) delete spec.passes;
      if (reminder === base.release_reminder) delete spec.release_reminder;
      if (Object.keys(spec).length === 0) {
        onDone(editing?.id ?? null);
        return;
      }
    }
    setErrors({});
    inFlight.current = true;
    save.mutate(spec, {
      onSuccess: ({ id }) => onDone(id),
      onError: (error) => {
        const place = placeOf(error);
        if (place === "gone") {
          toast.info("This watch was removed elsewhere.");
          onDone(null);
        } else if (place === "form") {
          setErrors({ form: error.message });
        } else if (place !== null) {
          setErrors({ [place.field]: error.message });
        }
      },
    });
  };

  // The reminder goes out at 6:55 the day before, in season: offered only while that's still ahead.
  const remindable = date === "" || (inSeason(date) && now < reminderAt(date));
  const quiet = state.settings.quiet_hours;
  const quietRelease = covers(quiet, REMINDER_AT) || covers(quiet, RELEASE_AT);
  const dayValue = other ? OTHER : date;

  return (
    <form noValidate onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
      <SheetHeader
        title={editing ? "Edit watch" : "Watch Buntzen Lake"}
        description={
          editing
            ? `Buntzen Lake, ${dayLabel(editing.spec.date)}: ${passesPhrase(editing.spec.passes)}.`
            : "Day-use parking passes, sold on Yodel. We'll alert you the moment one opens up."
        }
        closeDisabled={busy}
      />
      <SheetBody>
        <fieldset disabled={busy} className="min-w-0 space-y-7">
          <Section title="Which day?" id={`${ids}-day`} error={errors.date} errorId={`${ids}-date-error`}>
            <RadioGroup.Root
              id={`${ids}-date`}
              value={dayValue}
              onValueChange={pickDay}
              aria-labelledby={`${ids}-day`}
              aria-describedby={errors.date ? `${ids}-date-error` : undefined}
              className="grid grid-cols-3 gap-2 sm:grid-cols-4"
            >
              {days.map((d) => (
                <Chip key={d.value} as="radio" value={d.value}>
                  {d.label}
                </Chip>
              ))}
              <Chip as="radio" value={OTHER}>
                Other date
              </Chip>
            </RadioGroup.Root>
            {other && (
              <Input
                id={`${ids}-date-input`}
                type="date"
                aria-label="Date"
                aria-invalid={errors.date ? true : undefined}
                aria-describedby={errors.date ? `${ids}-date-error` : undefined}
                min={today}
                max={horizon}
                value={date}
                onChange={(e) => {
                  setDate(e.target.value);
                  setErrors((x) => ({ ...x, date: undefined, passes: undefined }));
                }}
              />
            )}
          </Section>

          <Section
            title="Which passes would do?"
            id={`${ids}-passes-label`}
            error={errors.passes}
            errorId={`${ids}-passes-error`}
          >
            <ToggleGroup.Root
              id={`${ids}-passes`}
              type="multiple"
              value={passes}
              onValueChange={(values) => {
                const next = canonical(values.filter(isPass));
                if (next.length === 0) return; // at least one stays on
                setPasses(next);
                setErrors((x) => ({ ...x, passes: undefined }));
              }}
              aria-labelledby={`${ids}-passes-label`}
              aria-describedby={errors.passes ? `${ids}-passes-error` : `${ids}-passes-hint`}
              className="grid grid-cols-3 gap-2"
            >
              {BUNTZEN_PASSES.map((pass) => (
                <Chip key={pass} as="toggle" value={pass}>
                  {CHIPS[pass]}
                </Chip>
              ))}
            </ToggleGroup.Root>
            <p id={`${ids}-passes-hint`} className="text-xs text-muted-foreground">
              Morning passes stop counting at noon on the day.
            </p>
          </Section>

          {remindable && (
            <div className="flex items-start justify-between gap-4 rounded-xl border bg-card p-4">
              <label htmlFor={`${ids}-reminder`} className="text-sm font-medium">
                Remind me at {timeLabel(REMINDER_AT)} the day before (passes go on sale at {RELEASE_LABEL})
              </label>
              <Switch id={`${ids}-reminder`} checked={reminder} onCheckedChange={setReminder} />
            </div>
          )}

          {(date && !inSeason(date)) || quietRelease ? (
            <div className="space-y-2">
              {date && !inSeason(date) && (
                <Note>
                  That date is outside Buntzen's reservation season, so Yodel probably won't offer passes. We'll check
                  every 30 minutes anyway.
                </Note>
              )}
              {quietRelease && (
                <Note>
                  Your quiet hours (until {timeLabel(quiet.end)}) cover the {RELEASE_LABEL} release, so no reminder and
                  no checks until then. Change them in Settings.
                </Note>
              )}
            </div>
          ) : null}

          <p className="flex items-start gap-2 text-xs text-muted-foreground">
            <Info className="mt-px size-3.5 shrink-0" aria-hidden />
            Waitlist only alerts you. You book on Yodel with your own account, licence plate and mobile number.
          </p>
        </fieldset>
      </SheetBody>
      {errors.form && (
        <p
          role="alert"
          className="flex items-start gap-2 border-t bg-danger-soft px-6 py-3 text-sm font-medium text-danger"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          {errors.form}
        </p>
      )}
      <SheetFooter>
        <Button variant="outline" disabled={busy} onClick={() => onDone(null)}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy}>
          {busy ? (
            <>
              <Loader2 className="motion-safe:animate-spin" aria-hidden /> Saving…
            </>
          ) : editing ? (
            "Save"
          ) : (
            "Start watching"
          )}
        </Button>
      </SheetFooter>
    </form>
  );
}

function Section({
  title,
  id,
  error,
  errorId,
  children,
}: {
  title: string;
  id: string;
  error?: string;
  errorId: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3">
      <h3 id={id} className="text-sm font-semibold">
        {title}
      </h3>
      {children}
      {error && (
        <p id={errorId} className="text-xs font-medium text-danger">
          {error}
        </p>
      )}
    </section>
  );
}

const CHIP =
  "flex h-11 items-center justify-center rounded-xl border bg-card px-2 text-sm font-medium text-foreground/80 shadow-sm transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=checked]:border-primary data-[state=on]:border-primary data-[state=checked]:bg-accent data-[state=on]:bg-accent data-[state=checked]:text-accent-foreground data-[state=on]:text-accent-foreground data-[state=checked]:font-semibold data-[state=on]:font-semibold";

/** A day (one of several) or a pass (any of them): a large, tappable chip. */
function Chip({ as, value, children }: { as: "radio" | "toggle"; value: string; children: ReactNode }) {
  return as === "radio" ? (
    <RadioGroup.Item value={value} className={CHIP}>
      {children}
    </RadioGroup.Item>
  ) : (
    <ToggleGroup.Item value={value} className={CHIP}>
      {children}
    </ToggleGroup.Item>
  );
}

function Note({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-start gap-2 rounded-xl border border-warning/40 bg-warning-soft p-3 text-sm">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
      <span>{children}</span>
    </p>
  );
}
