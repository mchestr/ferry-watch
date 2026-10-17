import * as Collapsible from "@radix-ui/react-collapsible";
import { useIsMutating, useMutation } from "@tanstack/react-query";
import { Car, ChevronRight, Footprints, Loader2, Trash2, Truck } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { errorMessage, hasStatus } from "@/lib/api";
import { reportError, useTerminals } from "@/lib/hooks";
import type { Fare, Terminal, TripSettings, Vehicle } from "@/lib/schema";
import { destinations, origins } from "@/lib/terminals";
import type { CartSummary } from "@/lib/trip";
import { todayInPacific } from "@/lib/time";
import {
  DRAFT_FIELDS,
  MAX_PASSENGERS,
  changedSettings,
  draftFieldFor,
  draftFromSettings,
  inlineErrorField,
  newTripSettings,
  pickSettings,
  settingsFromDraft,
  validateDraft,
  type DraftField,
  type TripDraft,
} from "@/lib/trip-form";
import { RemoveTripDialog } from "./RemoveTripDialog";
import { RouteMap } from "./RouteMap";
import { Button } from "./ui/button";
import { ChoiceGroup, Field, Input, Stepper, type Choice } from "./ui/controls";
import { Select, type Option } from "./ui/select";
import { Sheet, SheetBody, SheetFooter, SheetHeader } from "./ui/sheet";
import { Switch } from "./ui/switch";

export type SheetMode = { kind: "closed" } | { kind: "adding" } | { kind: "editing"; tripId: string };
type OpenMode = Exclude<SheetMode, { kind: "closed" }>;

/** Every request the sheet makes carries this key, so the sheet knows when one is in flight. */
const SHEET_MUTATION = ["trip-sheet"] as const;

const modeKey = (mode: OpenMode) => (mode.kind === "editing" ? `trip:${mode.tripId}` : "new");

const VEHICLES: readonly Choice<Vehicle>[] = [
  { value: "standard", label: "Car / SUV", icon: <Car aria-hidden /> },
  { value: "tall", label: "Over 7 ft", icon: <Truck aria-hidden /> },
  { value: "none", label: "Walk-on", icon: <Footprints aria-hidden /> },
];

const FARES: Option[] = [
  { value: "cheapest", label: "Cheapest available" },
  { value: "saver", label: "Saver", hint: "Pay in full, lowest price, strict changes" },
  { value: "prepaid", label: "Prepaid", hint: "Pay in full, flexible until the day before" },
  { value: "reservation", label: "Reservation Only", hint: "$20 non-refundable fee, rest paid at the terminal" },
];
const isFare = (value: string): value is Fare => FARES.some((f) => f.value === value);

const PASSENGERS = [
  { key: "children", label: "Children", hint: "5 to 11", singular: "child", plural: "children" },
  { key: "infants", label: "Infants", hint: "0 to 4", singular: "infant", plural: "infants" },
  { key: "seniors", label: "BC seniors", hint: "65+, BC residents", singular: "senior", plural: "seniors" },
] as const;

export type SaveRequest =
  | { kind: "add"; settings: TripSettings }
  | { kind: "update"; id: string; patch: Partial<TripSettings> };

/** The places a trip's terminals are in, for labelling a terminal that's no longer in BC Ferries' list. */
export interface TripRoute {
  from_place: string;
  from_area: string;
  to_place: string;
  to_area: string;
}

/** A trip the sheet can edit. */
export interface EditableTrip extends TripSettings {
  id: string;
  title: string;
  route?: TripRoute;
  cart: CartSummary | null;
}

/** Where the sheet's trips come from and go to (ferry-web's API, in MtApp). */
export interface TripSource {
  defaults: TripSettings;
  trips: readonly EditableTrip[];
  /** Resolves once saved (and whatever the app keeps is updated); rejects with an ApiError. */
  save: (request: SaveRequest) => Promise<unknown>;
  remove: (id: string) => Promise<unknown>;
  /** The help under the auto-cart switch. */
  autoCartHint: string;
  /** What the toast says after adding a trip. */
  addedMessage: string;
}

/**
 * The add/edit trip panel. `mode` says what's open; while the sheet animates closed it keeps
 * showing the last trip, and each open starts a fresh form.
 */
export function TripSheet({ mode, onClose, source }: { mode: SheetMode; onClose: () => void; source: TripSource }) {
  const [lastOpen, setLastOpen] = useState<OpenMode | null>(null);
  if (mode.kind !== "closed" && (lastOpen === null || modeKey(lastOpen) !== modeKey(mode))) setLastOpen(mode);
  const shown = mode.kind === "closed" ? lastOpen : mode;
  const busy = useIsMutating({ mutationKey: SHEET_MUTATION }) > 0;

  return (
    <Sheet
      open={mode.kind !== "closed"}
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      {shown && (
        // The content unmounts once the sheet has closed, and the key changes with the trip, so the
        // draft is always built fresh on mount.
        <TripForm
          key={modeKey(shown)}
          mode={shown}
          open={mode.kind !== "closed"}
          trip={shown.kind === "editing" ? source.trips.find((t) => t.id === shown.tripId) : undefined}
          source={source}
          busy={busy}
          onClose={onClose}
        />
      )}
    </Sheet>
  );
}

function Section({ title, headingId, children }: { title: string; headingId?: string; children: ReactNode }) {
  return (
    <section className="space-y-4">
      <h3 id={headingId} className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </h3>
      {children}
    </section>
  );
}

function terminalOptions(list: Terminal[], current: string, fallbackLabel: string): Option[] {
  const options = list.map((t) => ({ value: t.code, label: t.label }));
  return current && !options.some((o) => o.value === current)
    ? [{ value: current, label: fallbackLabel }, ...options]
    : options;
}

function TripForm({
  mode,
  open,
  trip,
  source,
  busy,
  onClose,
}: {
  mode: OpenMode;
  /** False while the sheet animates closed. */
  open: boolean;
  /** The trip being edited, from live state: undefined once it's been removed. */
  trip: EditableTrip | undefined;
  source: TripSource;
  busy: boolean;
  onClose: () => void;
}) {
  const editing = mode.kind === "editing";
  const today = todayInPacific();
  const terminals = useTerminals();
  const baseId = useId();
  const ids = {
    form: `${baseId}-form`,
    from: `${baseId}-from`,
    to: `${baseId}-to`,
    date: `${baseId}-date`,
    passengers: `${baseId}-passengers`,
    passengersHeading: `${baseId}-passengers-heading`,
    passengersError: `${baseId}-passengers-error`,
    vehicleHeading: `${baseId}-vehicle-heading`,
    height: `${baseId}-height`,
    earliest: `${baseId}-earliest`,
    latest: `${baseId}-latest`,
    autoCart: `${baseId}-auto-cart`,
    fare: `${baseId}-fare`,
  };

  // What the draft started from: Save sends only what differs from it.
  const [base] = useState<TripSettings>(() => (trip ? pickSettings(trip) : newTripSettings(source.defaults, today)));
  const [route] = useState(() => trip?.route);
  const [draft, setDraft] = useState<TripDraft>(() => draftFromSettings(base));
  // A past date is flagged straight away; other problems show once a field is touched, or on Save.
  const [touched, setTouched] = useState<ReadonlySet<DraftField>>(
    () => new Set<DraftField>(base.date < today ? ["date"] : []),
  );
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<{ field: DraftField; message: string } | null>(null);
  const [morePax, setMorePax] = useState(base.children + base.infants + base.seniors > 0);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const inFlight = useRef(false);

  const save = useMutation({
    mutationKey: SHEET_MUTATION,
    mutationFn: source.save,
    onSuccess: () => toast.success(editing ? "Trip saved" : source.addedMessage),
    onError: (error) => {
      // Shown inline, or handled below by closing the sheet.
      if (inlineErrorField(error) === null && !hasStatus(error, 404)) reportError(error);
    },
  });
  const tripId = editing ? mode.tripId : "";
  const remove = useMutation({
    mutationKey: SHEET_MUTATION,
    mutationFn: () => source.remove(tripId),
    onSuccess: () => toast.success("Trip removed"),
    onError: (error) => {
      if (hasStatus(error, 404)) toast.info("That trip was already removed.");
      else reportError(error);
    },
  });

  // Every close this form asks for goes through here, so a push and a response racing each other
  // can't close it (or say why) twice.
  const closed = useRef(false);
  const close = useCallback(() => {
    if (closed.current) return;
    closed.current = true;
    onClose();
  }, [onClose]);
  const closeWithNotice = useCallback(
    (message: string) => {
      if (closed.current) return;
      toast.info(message);
      close();
    },
    [close],
  );

  // Removed somewhere else (another device, say) while open: close, and say why. Not when it was
  // this sheet's own Remove.
  const missing = editing && trip === undefined;
  useEffect(() => {
    if (open && missing && !remove.isPending && !remove.isSuccess) closeWithNotice("This trip was removed elsewhere.");
  }, [open, missing, remove.isPending, remove.isSuccess, closeWithNotice]);

  const errors = validateDraft(draft, today);
  const shownError = (field: DraftField): string | undefined => {
    if (serverError?.field === field) return serverError.message;
    const seen = submitted || touched.has(field) || (field === "latest" && touched.has("earliest"));
    return seen ? errors[field] : undefined;
  };
  const touch = (field: DraftField) => setTouched((prev) => (prev.has(field) ? prev : new Set(prev).add(field)));
  const update = <K extends keyof TripDraft>(key: K, value: TripDraft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    const field = draftFieldFor(key);
    if (field && serverError?.field === field) setServerError(null);
  };
  const focusField = (field: DraftField) => document.getElementById(ids[field])?.focus();

  const terminalList = terminals.data;
  const originList = terminalList ? origins(terminalList) : [];
  const destinationList = terminalList ? destinations(terminalList, draft.from) : [];
  const routeLabel = (code: string, place?: string, area?: string) => (place ? `${area} (${place})` : code);
  const originOptions = terminalOptions(
    originList,
    draft.from,
    draft.from === base.from ? routeLabel(draft.from, route?.from_place, route?.from_area) : draft.from,
  );
  const destinationOptions = terminalOptions(
    destinationList,
    draft.to,
    draft.to === base.to ? routeLabel(draft.to, route?.to_place, route?.to_area) : draft.to,
  );
  const terminalsHint = terminalList
    ? undefined
    : terminals.failureCount > 0
      ? `Couldn't load BC Ferries' terminals (${errorMessage(terminals.failureReason)}). Retrying…`
      : "Loading terminals…";

  const setFrom = (from: string) => {
    // Default the destination from the same list the "To" dropdown shows.
    const options = terminalList ? destinations(terminalList, from) : [];
    setDraft((d) => ({
      ...d,
      from,
      to: options.some((t) => t.code === d.to) ? d.to : (options[0]?.code ?? ""),
    }));
    touch("from");
    if (serverError?.field === "from" || serverError?.field === "to") setServerError(null);
  };

  const setPassengers = (key: "adults" | "children" | "infants" | "seniors", value: number) => {
    update(key, value);
    touch("passengers");
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || inFlight.current) return;
    setSubmitted(true);
    const invalid = DRAFT_FIELDS.find((field) => errors[field] !== undefined);
    if (invalid) {
      if (invalid === "passengers") setMorePax(true);
      focusField(invalid);
      return;
    }
    const settings = settingsFromDraft(draft, base);
    let request: SaveRequest;
    if (mode.kind === "editing") {
      const patch = changedSettings(base, settings);
      if (Object.keys(patch).length === 0) {
        close();
        return;
      }
      request = { kind: "update", id: mode.tripId, patch };
    } else {
      request = { kind: "add", settings };
    }
    inFlight.current = true;
    // These callbacks only run while this form is mounted, so a late response can't close another trip's sheet.
    save.mutate(request, {
      onSuccess: close,
      onError: (error) => {
        const field = inlineErrorField(error);
        if (field) {
          setServerError({ field, message: errorMessage(error) });
          focusField(field);
        } else if (hasStatus(error, 404)) {
          closeWithNotice("This trip was removed elsewhere.");
        }
      },
      onSettled: () => {
        inFlight.current = false;
      },
    });
  };

  const confirmRemove = () => {
    if (busy || inFlight.current) return;
    inFlight.current = true;
    remove.mutate(undefined, {
      onSuccess: () => {
        setConfirmingRemove(false);
        close();
      },
      onError: (error) => {
        // A 404 means it was already gone; useRemoveTrip has said so.
        if (!hasStatus(error, 404)) return;
        setConfirmingRemove(false);
        close();
      },
      onSettled: () => {
        inFlight.current = false;
      },
    });
  };

  const passengersError = shownError("passengers");

  return (
    <form id={ids.form} noValidate onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
      <SheetHeader
        title={editing ? "Edit trip" : "Add a trip"}
        description={
          editing
            ? `${trip?.title ?? "This trip"}. Changes take effect on the next check.`
            : "Watch another route or date alongside your other trips."
        }
        closeDisabled={busy}
      />
      <SheetBody>
        <fieldset disabled={busy} className="min-w-0 space-y-8">
          <Section title="Route & date">
            <Field id={ids.from} label="From" error={shownError("from")} hint={terminalsHint}>
              <Select
                value={draft.from}
                onValueChange={setFrom}
                options={originOptions}
                placeholder="Choose a terminal"
                disabled={!terminalList}
              />
            </Field>
            <Field id={ids.to} label="To" error={shownError("to")}>
              <Select
                value={draft.to}
                onValueChange={(to) => {
                  update("to", to);
                  touch("to");
                }}
                options={destinationOptions}
                placeholder="Choose a destination"
                disabled={!terminalList}
              />
            </Field>
            {terminalList && <RouteMap from={draft.from} to={draft.to} terminals={terminalList} />}
            <Field id={ids.date} label="Departure date" error={shownError("date")}>
              <Input
                type="date"
                min={today}
                required
                value={draft.date}
                onChange={(e) => {
                  update("date", e.target.value);
                  touch("date");
                }}
              />
            </Field>
          </Section>

          <Section title="Travellers" headingId={ids.passengersHeading}>
            <div
              id={ids.passengers}
              role="group"
              aria-labelledby={ids.passengersHeading}
              aria-describedby={passengersError ? ids.passengersError : undefined}
              tabIndex={-1}
              className="space-y-4 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <PassengerRow
                id={`${baseId}-adults`}
                label="Adults"
                hint="12 and over"
                singular="adult"
                plural="adults"
                value={draft.adults}
                onChange={(v) => setPassengers("adults", v)}
              />
              <Collapsible.Root open={morePax} onOpenChange={setMorePax}>
                <Collapsible.Trigger className="flex items-center gap-1 rounded text-sm font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <ChevronRight
                    aria-hidden
                    className={`size-4 motion-safe:transition-transform ${morePax ? "rotate-90" : ""}`}
                  />
                  Children, infants &amp; seniors
                </Collapsible.Trigger>
                <Collapsible.Content className="mt-4 space-y-4">
                  {PASSENGERS.map((p) => (
                    <PassengerRow
                      key={p.key}
                      id={`${baseId}-${p.key}`}
                      label={p.label}
                      hint={p.hint}
                      singular={p.singular}
                      plural={p.plural}
                      value={draft[p.key]}
                      onChange={(v) => setPassengers(p.key, v)}
                    />
                  ))}
                </Collapsible.Content>
              </Collapsible.Root>
              {passengersError && (
                <p id={ids.passengersError} className="text-xs font-medium text-danger">
                  {passengersError}
                </p>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              At least one adult or senior, and up to {MAX_PASSENGERS} people in total.
            </p>
          </Section>

          <Section title="Vehicle" headingId={ids.vehicleHeading}>
            <ChoiceGroup
              aria-labelledby={ids.vehicleHeading}
              value={draft.vehicle}
              onChange={(vehicle) => update("vehicle", vehicle)}
              options={VEHICLES}
            />
            {draft.vehicle === "tall" && (
              <Field
                id={ids.height}
                label="Total height (ft)"
                error={shownError("height")}
                hint="Over 7 ft and up to 14 ft, including anything on the roof."
              >
                <Input
                  inputMode="decimal"
                  autoComplete="off"
                  value={draft.height}
                  onChange={(e) => update("height", e.target.value)}
                  onBlur={() => touch("height")}
                />
              </Field>
            )}
          </Section>

          <Section title="Departure window">
            <div className="grid grid-cols-2 gap-3">
              <Field id={ids.earliest} label="Earliest" error={shownError("earliest")}>
                <Input
                  type="time"
                  required
                  value={draft.earliest}
                  onChange={(e) => update("earliest", e.target.value)}
                  onBlur={() => touch("earliest")}
                />
              </Field>
              <Field id={ids.latest} label="Latest" error={shownError("latest")}>
                <Input
                  type="time"
                  required
                  value={draft.latest}
                  onChange={(e) => update("latest", e.target.value)}
                  onBlur={() => touch("latest")}
                />
              </Field>
            </div>
            <p className="text-xs text-muted-foreground">Alerts and auto-cart only fire for sailings in this window.</p>
          </Section>

          <Section title="Auto-cart">
            <div className="flex items-start justify-between gap-4 rounded-xl border bg-card p-4">
              <div>
                <label htmlFor={ids.autoCart} className="text-sm font-medium">
                  Add open sailings to my cart
                </label>
                <p id={`${ids.autoCart}-hint`} className="mt-0.5 text-xs text-muted-foreground">
                  {source.autoCartHint}
                </p>
              </div>
              <Switch
                id={ids.autoCart}
                aria-describedby={`${ids.autoCart}-hint`}
                checked={draft.auto_cart}
                onCheckedChange={(on) => update("auto_cart", on)}
              />
            </div>
            {draft.auto_cart && (
              <Field id={ids.fare} label="Fare to select">
                <Select
                  value={draft.fare}
                  onValueChange={(fare) => {
                    if (isFare(fare)) update("fare", fare);
                  }}
                  options={FARES}
                />
              </Field>
            )}
          </Section>
        </fieldset>
      </SheetBody>
      <SheetFooter>
        {editing && (
          <Button
            variant="ghost"
            className="mr-auto text-muted-foreground hover:text-danger"
            disabled={busy || trip === undefined}
            onClick={() => setConfirmingRemove(true)}
          >
            <Trash2 aria-hidden /> Remove
          </Button>
        )}
        <Button variant="outline" disabled={busy} onClick={close}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy}>
          {save.isPending ? (
            <>
              <Loader2 className="motion-safe:animate-spin" aria-hidden /> Saving…
            </>
          ) : editing ? (
            "Save"
          ) : (
            "Add trip"
          )}
        </Button>
      </SheetFooter>
      {trip && (
        <RemoveTripDialog
          trip={trip}
          open={confirmingRemove}
          onOpenChange={setConfirmingRemove}
          pending={remove.isPending}
          onConfirm={confirmRemove}
        />
      )}
    </form>
  );
}

function PassengerRow({
  id,
  label,
  hint,
  singular,
  plural,
  value,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  singular: string;
  plural: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <div id={`${id}-label`} className="text-sm font-medium">
          {label}
        </div>
        <div id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </div>
      </div>
      <Stepper
        aria-labelledby={`${id}-label`}
        aria-describedby={`${id}-hint`}
        value={value}
        singular={singular}
        plural={plural}
        onChange={onChange}
      />
    </div>
  );
}
