/** The user's own settings: alerts on this device, quiet hours, signing out, and the Ko-fi link. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, Coffee, LogOut } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Button, LinkButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/controls";
import { Switch } from "@/components/ui/switch";
import { reportError } from "@/lib/hooks";
import { parseTimeOfDay, timeLabel } from "@/lib/time";
import { MY_STATE_KEY, myApi } from "./api";
import { signOut } from "./auth";
import { pushSupported, subscribe } from "./push";
import type { MyState, QuietHours } from "./schema";

/** What quiet hours start as when turned on. */
const NIGHT: QuietHours = { start: "23:00", end: "06:00" };

/**
 * The user's quiet hours: off unless they turn them on. The switch saves at once (with the times last shown,
 * 11 pm to 6 am at first); changed times wait for Save.
 */
function QuietHoursSetting({ saved }: { saved: QuietHours | null }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<QuietHours>(saved ?? NIGHT);
  // Saved here or on another device: the times start afresh from what's saved.
  const key = saved ? `${saved.start}-${saved.end}` : "";
  const [shownKey, setShownKey] = useState(key);
  if (key !== shownKey) {
    setShownKey(key);
    if (saved) setDraft(saved);
  }
  const save = useMutation({
    mutationFn: myApi.setQuietHours,
    onSuccess: (next, quiet) => {
      qc.setQueryData(MY_STATE_KEY, next);
      toast.success(
        quiet ? `Quiet hours: ${timeLabel(quiet.start)} to ${timeLabel(quiet.end)}` : "Quiet hours are off",
      );
    },
    onError: (error) => reportError(qc, error),
  });
  // While saving, show what's being saved.
  const on = save.isPending ? save.variables !== null : saved !== null;
  const blank = parseTimeOfDay(draft.start) === null || parseTimeOfDay(draft.end) === null;
  const error = blank
    ? "Enter a start and an end."
    : draft.start === draft.end
      ? "Start and end can't be the same."
      : null;
  const changed = saved !== null && (draft.start !== saved.start || draft.end !== saved.end);
  return (
    <div className="space-y-3">
      <label className="flex items-center justify-between gap-3 text-sm">
        <span>
          Quiet hours <span className="text-muted-foreground">(Pacific time)</span>
        </span>
        <Switch
          checked={on}
          disabled={save.isPending}
          onCheckedChange={(next) => save.mutate(next ? (error === null ? draft : NIGHT) : null)}
        />
      </label>
      <p className="text-xs text-muted-foreground">
        Your trips aren't checked during them, so nothing is held and you get no alerts. Check now and Hold it for me
        still work.
      </p>
      {on && (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (error === null) save.mutate(draft);
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <Field label="Start" error={error}>
              <Input
                type="time"
                required
                value={draft.start}
                onChange={(e) => setDraft((d) => ({ ...d, start: e.target.value }))}
              />
            </Field>
            <Field label="End">
              <Input
                type="time"
                required
                value={draft.end}
                onChange={(e) => setDraft((d) => ({ ...d, end: e.target.value }))}
              />
            </Field>
          </div>
          {changed && (
            <Button type="submit" size="sm" disabled={save.isPending || error !== null}>
              Save
            </Button>
          )}
        </form>
      )}
    </div>
  );
}

export function AccountCard({ state }: { state: MyState }) {
  const qc = useQueryClient();
  const onError = (error: unknown) => reportError(qc, error);
  const enable = useMutation({
    mutationFn: async () => {
      const key = state.push.key;
      const subscription = key ? await subscribe(key) : null;
      if (!subscription) throw new Error("Notifications weren't allowed on this device");
      await myApi.subscribe(subscription);
    },
    onSuccess: () => {
      toast.success("Alerts are on for this device");
      void qc.invalidateQueries({ queryKey: MY_STATE_KEY });
    },
    onError,
  });
  const test = useMutation({ mutationFn: myApi.testAlert, onSuccess: () => toast.success("Test alert sent"), onError });

  return (
    <Card className="space-y-4 p-5">
      <h2 className="font-semibold">Your account</h2>
      <div className="space-y-2">
        <p className="text-sm">
          {state.devices > 0
            ? `Alerts go to ${state.devices} device${state.devices === 1 ? "" : "s"}.`
            : "Alerts go to your email until you turn them on for a device."}
        </p>
        <div className="flex flex-wrap gap-2">
          {state.push.key && pushSupported() && (
            <Button size="sm" variant="outline" disabled={enable.isPending} onClick={() => enable.mutate()}>
              <Bell aria-hidden /> Alerts on this device
            </Button>
          )}
          <Button size="sm" variant="ghost" disabled={test.isPending} onClick={() => test.mutate()}>
            Send a test alert
          </Button>
        </div>
      </div>
      <QuietHoursSetting saved={state.settings.quiet_hours} />
      <p className="text-xs text-muted-foreground">
        Your nearest trip within a week is watched closely, every 5 minutes a week out down to every 30 seconds in the
        last 6 hours. Other trips are checked every 6 hours.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="ghost" onClick={() => void signOut()}>
          <LogOut aria-hidden /> Sign out
        </Button>
        {state.links.kofi && (
          <LinkButton size="sm" variant="ghost" href={state.links.kofi} target="_blank" rel="noopener noreferrer">
            <Coffee aria-hidden /> Buy me a coffee
          </LinkButton>
        )}
      </div>
    </Card>
  );
}
