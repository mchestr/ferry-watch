/** The user's own settings: alerts on this device, pausing everything, sessions, and the Ko-fi link. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, Coffee, LogOut } from "lucide-react";
import { toast } from "sonner";
import { Button, LinkButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { reportError } from "@/lib/hooks";
import { MY_STATE_KEY, myApi } from "./api";
import { signOut } from "./auth";
import { pushSupported, subscribe } from "./push";
import type { MyState } from "./schema";

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
  const pause = useMutation({
    mutationFn: myApi.pauseAll,
    onSuccess: (next) => qc.setQueryData(MY_STATE_KEY, next),
    onError,
  });

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
      <label className="flex items-center justify-between gap-3 text-sm">
        Pause all my trips
        <Switch checked={state.me.paused} onCheckedChange={(paused) => pause.mutate(paused)} />
      </label>
      <p className="text-xs text-muted-foreground">
        Your nearest trip within a week is watched closely, every 5 minutes a week out down to every 30 seconds in the
        last 6 hours. Other trips are checked every 6 hours. Between {state.quiet_hours.start} and{" "}
        {state.quiet_hours.end} we only alert, without holding a cart.
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
