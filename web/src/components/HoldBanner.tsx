import { AlertTriangle, Loader2, MonitorSmartphone, RotateCcw, ShoppingCart, X } from "lucide-react";
import { api, type State } from "@/lib/api";
import { useStateMutation } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { Button, LinkButton } from "./ui/button";

export function HoldBanner({ state }: { state: State }) {
  const dismiss = useStateMutation(() => api.dismissHold());
  const release = useStateMutation(() => api.releaseHold(), "Cart released, watching resumes");
  const h = state.hold;
  if (!h || h.status === "closed") return null;

  const failed = h.status === "failed";
  return (
    <div
      className={cn(
        "flex flex-col gap-4 rounded-2xl border p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5",
        failed ? "border-danger/30 bg-danger-soft" : "border-primary/25 bg-accent",
      )}
    >
      <div className="flex min-w-0 items-start gap-3.5">
        <div className={cn("grid size-10 shrink-0 place-items-center rounded-xl [&_svg]:size-5", failed ? "bg-danger text-danger-foreground" : "bg-primary text-primary-foreground")}>
          {h.status === "adding" ? <Loader2 className="animate-spin" /> : failed ? <AlertTriangle /> : <ShoppingCart />}
        </div>
        <div className="min-w-0">
          <div className="font-semibold">
            {h.status === "adding" && `Adding ${h.depart} to your cart…`}
            {h.status === "ready" && `${h.depart} is in your cart`}
            {failed && `Couldn't add ${h.depart} to the cart`}
          </div>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {h.status === "adding" && "Logging in to BC Ferries and selecting your fare."}
            {h.status === "ready" && (
              <>
                {h.fare} {h.price} · Finish checkout in the {state.vnc_url ? "checkout browser" : "browser window on the host"}. Nothing is booked until you pay. Checks are paused until you release the cart.
              </>
            )}
            {failed && h.error}
          </p>
        </div>
      </div>
      {h.status === "ready" && (
        <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:flex-row">
          <Button variant="outline" size="lg" onClick={() => release.mutate(undefined)} disabled={release.isPending}>
            <RotateCcw /> Release &amp; resume
          </Button>
          {state.vnc_url && (
            <LinkButton size="lg" href={state.vnc_url} target="_blank" rel="noopener">
              <MonitorSmartphone /> Open checkout
            </LinkButton>
          )}
        </div>
      )}
      {failed && (
        <Button variant="ghost" size="sm" onClick={() => dismiss.mutate(undefined)} className="self-end sm:self-auto">
          <X /> Dismiss
        </Button>
      )}
    </div>
  );
}
