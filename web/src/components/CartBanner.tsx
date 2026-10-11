import { AlertTriangle, Loader2, MonitorSmartphone, RotateCcw, ShoppingCart, X } from "lucide-react";
import { api, type Trip } from "@/lib/api";
import { useStateMutation } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { Button, LinkButton } from "./ui/button";

export function CartBanner({ trip, vncUrl }: { trip: Trip; vncUrl: string }) {
  const dismiss = useStateMutation(() => api.dismissCart(trip.id));
  const release = useStateMutation(() => api.releaseCart(trip.id), "Cart released, watching resumes");
  const h = trip.cart;
  if (!h || h.status === "closed") return null;

  const failed = h.status === "failed";
  return (
    <div className={cn("flex flex-col gap-4 border-t px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6", failed ? "bg-danger-soft" : "bg-accent")}>
      <div className="flex min-w-0 items-start gap-3.5">
        <div className={cn("grid size-9 shrink-0 place-items-center rounded-xl [&_svg]:size-[18px]", failed ? "bg-danger text-danger-foreground" : "bg-primary text-primary-foreground")}>
          {h.status === "adding" ? <Loader2 className="animate-spin" /> : failed ? <AlertTriangle /> : <ShoppingCart />}
        </div>
        <div className="min-w-0">
          <div className="text-[15px] font-semibold">
            {h.status === "adding" && `Adding ${h.depart} to your cart…`}
            {h.status === "ready" && `${h.depart} is in your cart`}
            {failed && `Couldn't add ${h.depart} to the cart`}
          </div>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {h.status === "adding" && "Selecting your fare."}
            {h.status === "ready" && (
              <>
                {h.fare} {h.price} · Finish checkout in the {vncUrl ? "checkout browser" : "browser window"}. Nothing is booked until you pay.
                This trip is paused until you release the cart.
              </>
            )}
            {failed && h.error}
          </p>
        </div>
      </div>
      {h.status === "ready" && (
        <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:flex-row">
          <Button variant="outline" onClick={() => release.mutate(undefined)} disabled={release.isPending}>
            <RotateCcw /> Release
          </Button>
          {vncUrl && (
            <LinkButton href={vncUrl} target="_blank" rel="noopener">
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
