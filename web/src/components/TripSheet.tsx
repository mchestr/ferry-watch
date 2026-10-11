import * as Collapsible from "@radix-ui/react-collapsible";
import { Car, ChevronRight, Footprints, Truck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Trash2 } from "lucide-react";
import { api, type Fare, type Terminals, type TripConfig, type Vehicle } from "@/lib/api";
import { useStateMutation } from "@/lib/hooks";
import { Button } from "./ui/button";
import { Field, Input, Segmented, Stepper } from "./ui/controls";
import { Select } from "./ui/select";
import { Sheet } from "./ui/sheet";
import { Switch } from "./ui/switch";

type Draft = Omit<TripConfig, "id">;

const tomorrow = () => {
  const d = new Date(Date.now() + 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

export const NEW_TRIP: Draft = {
  enabled: true,
  date: tomorrow(),
  from: "SWB",
  to: "TSA",
  adults: 1,
  children: 0,
  infants: 0,
  seniors: 0,
  vehicle: "standard",
  height_ft: 8,
  earliest: "00:00",
  latest: "23:59",
  hold: true,
  fare: "cheapest",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

export function TripSheet({
  open,
  onOpenChange,
  trip,
  terminals,
  loginConfigured,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  /** the trip to edit, or null to add a new one */
  trip: TripConfig | null;
  terminals?: Terminals;
  loginConfigured: boolean;
}) {
  const config: Draft = trip ?? { ...NEW_TRIP, date: tomorrow() };
  const [d, setD] = useState<Draft>(config);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [morePax, setMorePax] = useState(false);
  useEffect(() => {
    if (open) {
      setD(config);
      setConfirmDelete(false);
      setMorePax(config.children + config.infants + config.seniors > 0);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useStateMutation((c: Draft) => (trip ? api.updateTrip(trip.id, c) : api.addTrip(c)), trip ? "Trip saved" : "Trip added, opening a booking session…");
  const remove = useStateMutation(() => api.deleteTrip(trip!.id), "Trip removed");
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((p) => ({ ...p, [k]: v }));

  const byLabel = (a: string, b: string) => (terminals?.[a]?.label ?? a).localeCompare(terminals?.[b]?.label ?? b);
  const origins = useMemo(() => Object.keys(terminals ?? {}).filter((c) => terminals![c].dests.length).sort(byLabel), [terminals]); // eslint-disable-line react-hooks/exhaustive-deps
  const dests = useMemo(() => (terminals?.[d.from]?.dests ?? []).filter((c) => terminals![c]).sort(byLabel), [terminals, d.from]); // eslint-disable-line react-hooks/exhaustive-deps
  const opt = (c: string) => ({ value: c, label: terminals?.[c]?.label ?? c });

  const setFrom = (from: string) => {
    const ds = terminals?.[from]?.dests ?? [];
    setD((p) => ({ ...p, from, to: ds.includes(p.to) ? p.to : (ds[0] ?? p.to) }));
  };

  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title={trip ? "Edit trip" : "Add a trip"}
      description={trip ? "Changes take effect on the next check." : "Watch another route or date alongside your other trips."}
      footer={
        <>
          {trip && (
            <Button
              variant="ghost"
              className={confirmDelete ? "mr-auto text-danger hover:bg-danger-soft" : "mr-auto text-muted-foreground"}
              onClick={() => (confirmDelete ? remove.mutate(undefined, { onSuccess: () => onOpenChange(false) }) : setConfirmDelete(true))}
              disabled={remove.isPending}
            >
              <Trash2 /> {confirmDelete ? "Confirm remove" : "Remove"}
            </Button>
          )}
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => save.mutate(d, { onSuccess: () => onOpenChange(false) })}
            disabled={save.isPending}
          >
            {trip ? "Save" : "Add trip"}
          </Button>
        </>
      }
    >
      <div className="space-y-8">
        <Section title="Route & date">
          <Field label="From">
            <Select value={d.from} onValueChange={setFrom} options={origins.map(opt)} />
          </Field>
          <Field label="To">
            <Select value={d.to} onValueChange={(v) => set("to", v)} options={dests.map(opt)} />
          </Field>
          <Field label="Departure date">
            <Input type="date" value={d.date} onChange={(e) => e.target.value && set("date", e.target.value)} />
          </Field>
        </Section>

        <Section title="Travellers">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium">Adults</div>
              <div className="text-xs text-muted-foreground">12 and over</div>
            </div>
            <Stepper label="adults" value={d.adults} min={1} onChange={(v) => set("adults", v)} />
          </div>
          <Collapsible.Root open={morePax} onOpenChange={setMorePax}>
            <Collapsible.Trigger className="flex items-center gap-1 text-sm font-medium text-primary hover:underline">
              <ChevronRight className={`size-4 transition-transform ${morePax ? "rotate-90" : ""}`} />
              Children, infants &amp; seniors
            </Collapsible.Trigger>
            <Collapsible.Content className="mt-4 space-y-4">
              {(
                [
                  ["children", "Children", "5 to 11"],
                  ["infants", "Infants", "0 to 4"],
                  ["seniors", "BC seniors", "65+, BC residents"],
                ] as const
              ).map(([k, l, h]) => (
                <div key={k} className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium">{l}</div>
                    <div className="text-xs text-muted-foreground">{h}</div>
                  </div>
                  <Stepper label={l.toLowerCase()} value={d[k]} onChange={(v) => set(k, v)} />
                </div>
              ))}
            </Collapsible.Content>
          </Collapsible.Root>
        </Section>

        <Section title="Vehicle">
          <Segmented<Vehicle>
            value={d.vehicle}
            onChange={(v) => set("vehicle", v)}
            options={[
              { value: "standard", label: "Car / SUV", icon: <Car /> },
              { value: "tall", label: "Over 7 ft", icon: <Truck /> },
              { value: "none", label: "Walk-on", icon: <Footprints /> },
            ]}
          />
          {d.vehicle === "tall" && (
            <Field label="Total height (ft)">
              <Input type="number" min={7.1} max={14} step={0.1} value={d.height_ft} onChange={(e) => set("height_ft", parseFloat(e.target.value) || 8)} />
            </Field>
          )}
        </Section>

        <Section title="Departure window">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Earliest">
              <Input type="time" value={d.earliest} onChange={(e) => set("earliest", e.target.value)} />
            </Field>
            <Field label="Latest">
              <Input type="time" value={d.latest} onChange={(e) => set("latest", e.target.value)} />
            </Field>
          </div>
          <p className="text-xs text-muted-foreground">Alerts and auto-cart only fire for sailings in this window.</p>
        </Section>

        <Section title="Auto-cart">
          <label className="flex items-start justify-between gap-4 rounded-xl border bg-card p-4">
            <div>
              <div className="text-sm font-medium">Add open sailings to my cart</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                {loginConfigured ? "Logs in and holds the sailing in a browser you can finish checkout in." : "Needs BC Ferries login credentials (BCF_USER / BCF_PASS)."}
              </div>
            </div>
            <Switch checked={d.hold} onCheckedChange={(v) => set("hold", v)} />
          </label>
          {d.hold && (
            <Field label="Fare to select">
              <Select
                value={d.fare}
                onValueChange={(v) => set("fare", v as Fare)}
                options={[
                  { value: "cheapest", label: "Cheapest available" },
                  { value: "saver", label: "Saver", hint: "Pay in full, lowest price, strict changes" },
                  { value: "prepaid", label: "Prepaid", hint: "Pay in full, flexible until the day before" },
                  { value: "reservation", label: "Reservation Only", hint: "$20 non-refundable fee, rest paid at the terminal" },
                ]}
              />
            </Field>
          )}
        </Section>

      </div>
    </Sheet>
  );
}
