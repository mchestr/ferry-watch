/**
 * The dashboard's nudge to turn alerts on (docs/waitlist/design.md §7.2): shown while this device has no push
 * subscription and the user watches something, since that's how they'll hear. On an iPhone outside a home-screen
 * app, push isn't possible, so it says how to get there instead. Dismissed per device.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BellRing, X } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { reportError } from "@/lib/hooks";
import { MY_STATE_KEY, myApi } from "./api";
import { pushSupported, subscribe } from "./push";
import { alertsBannerDismissed, dismissAlertsBanner } from "./watches";
import type { WaitlistState } from "./watch-schema";

function isIos(): boolean {
  const ua = navigator.userAgent;
  // iPadOS asks for desktop sites, so it says "Macintosh"; only the touch screen gives it away.
  return /iPad|iPhone|iPod/.test(ua) || (ua.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

function isStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

/** Whether this device already gets push alerts from this site. */
async function subscribedHere(): Promise<boolean> {
  try {
    const registration = await navigator.serviceWorker.getRegistration();
    return (await registration?.pushManager.getSubscription()) != null;
  } catch {
    return true; // can't tell: don't nag
  }
}

export function AlertsBanner({ state }: { state: WaitlistState }) {
  const qc = useQueryClient();
  const supported = pushSupported();
  const [dismissed, setDismissed] = useState(alertsBannerDismissed);
  const [subscribed, setSubscribed] = useState<boolean | null>(null); // null until we know
  useEffect(() => {
    if (!supported) return;
    let current = true;
    void subscribedHere().then((yes) => {
      if (current) setSubscribed(yes);
    });
    return () => {
      current = false;
    };
  }, [supported]);
  const enable = useMutation({
    mutationFn: async () => {
      const subscription = state.push.key ? await subscribe(state.push.key) : null;
      if (!subscription) throw new Error("Notifications weren't allowed on this device");
      await myApi.subscribe(subscription);
    },
    onSuccess: () => {
      setSubscribed(true);
      toast.success("Alerts are on for this device");
      void qc.invalidateQueries({ queryKey: MY_STATE_KEY });
    },
    onError: (error) => reportError(error),
  });

  if (dismissed || state.watches.length === 0) return null;
  const iosTab = !supported && isIos() && !isStandalone();
  if (!iosTab && (!supported || subscribed !== false || !state.push.key)) return null;

  return (
    <div
      role="region"
      aria-label="Alerts on this device"
      className="flex items-start gap-3 rounded-2xl border border-primary/20 bg-accent p-4 text-accent-foreground sm:items-center"
    >
      <BellRing className="mt-0.5 size-5 shrink-0" aria-hidden />
      <div className="flex min-w-0 flex-1 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm font-medium">
          {iosTab
            ? "To get alerts on iPhone, add Waitlist to your Home Screen (Share → Add to Home Screen), then open it from there."
            : "Turn on alerts on this device. It's how you'll hear when something opens."}
        </p>
        {!iosTab && (
          <Button
            size="sm"
            className="self-start sm:self-auto"
            disabled={enable.isPending}
            onClick={() => enable.mutate()}
          >
            Turn on
          </Button>
        )}
      </div>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => {
          dismissAlertsBanner();
          setDismissed(true);
        }}
        className="-m-1 rounded-lg p-1 text-accent-foreground/70 hover:bg-background/40 hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X className="size-4" aria-hidden />
      </button>
    </div>
  );
}
