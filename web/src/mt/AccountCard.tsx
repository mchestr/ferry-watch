/**
 * The user's own settings: alerts on this device, quiet hours, the checkout viewer beta where it's offered, signing
 * out, and the Ko-fi link.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, Coffee, LogOut } from "lucide-react";
import { useId, useState } from "react";
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
import type { CheckoutViewer, MyState, QuietHours } from "./schema";

/**
 * The user's quiet hours. Everyone has them, 10 pm to 5 am unless they change them; changed times wait for Save.
 */
function QuietHoursSetting({ saved }: { saved: QuietHours }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<QuietHours>(saved);
  // Saved here or on another device: the times start afresh from what's saved.
  const key = `${saved.start}-${saved.end}`;
  const [shownKey, setShownKey] = useState(key);
  if (key !== shownKey) {
    setShownKey(key);
    setDraft(saved);
  }
  const save = useMutation({
    mutationFn: myApi.setQuietHours,
    onSuccess: (next, quiet) => {
      qc.setQueryData(MY_STATE_KEY, next);
      toast.success(`Quiet hours: ${timeLabel(quiet.start)} to ${timeLabel(quiet.end)}`);
    },
    onError: (error) => reportError(qc, error),
  });
  const blank = parseTimeOfDay(draft.start) === null || parseTimeOfDay(draft.end) === null;
  const error = blank
    ? "Enter a start and an end."
    : draft.start === draft.end
      ? "Start and end can't be the same."
      : null;
  const changed = draft.start !== saved.start || draft.end !== saved.end;
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (error === null) save.mutate(draft);
      }}
    >
      <fieldset className="min-w-0 space-y-3">
        <legend className="text-sm">
          Quiet hours <span className="text-muted-foreground">(Pacific time)</span>
        </legend>
        <p className="text-xs text-muted-foreground">
          Your trips aren't checked during them, so nothing is held and you get no alerts. Check now and Hold it for me
          still work.
        </p>
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
      </fieldset>
    </form>
  );
}

/** The streamed checkout viewer (beta), for users the deployment offers it to. It saves at once. */
function CheckoutViewerSetting({ saved }: { saved: CheckoutViewer }) {
  const qc = useQueryClient();
  const id = useId();
  const save = useMutation({
    mutationFn: myApi.setCheckoutViewer,
    onSuccess: (next, viewer) => {
      qc.setQueryData(MY_STATE_KEY, next);
      toast.success(viewer === "stream" ? "Checkouts open in the new viewer" : "Checkouts open in the classic viewer");
    },
    onError: (error) => reportError(qc, error),
  });
  const viewer = save.isPending ? save.variables : saved;
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <label htmlFor={id} className="text-sm">
          New checkout viewer (beta)
        </label>
        <p id={`${id}-hint`} className="mt-0.5 text-xs text-muted-foreground">
          Checkout looks and scrolls like a normal page on your phone. If anything goes wrong, the checkout has a{" "}
          <strong>Use the classic viewer</strong> button.
        </p>
      </div>
      <Switch
        id={id}
        aria-describedby={`${id}-hint`}
        checked={viewer === "stream"}
        disabled={save.isPending}
        onCheckedChange={(on) => save.mutate(on ? "stream" : "classic")}
      />
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
      {state.settings.stream_viewer_offered && <CheckoutViewerSetting saved={state.settings.checkout_viewer} />}
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
