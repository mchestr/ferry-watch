import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, MonitorSmartphone, RotateCcw, ShoppingCart, Timer, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api, hasStatus } from "@/lib/api";
import { useNow, useStateMutation } from "@/lib/hooks";
import type { Trip } from "@/lib/schema";
import { STATE_KEY } from "@/lib/state";
import { clock, countdown, serverNow, serverToClientMs, timeLabel } from "@/lib/time";
import { cn } from "@/lib/utils";
import { Button, LinkButton } from "./ui/button";

/** Under this many seconds left, the countdown turns to a warning. */
export const CART_EXPIRY_WARNING_S = 120;

/** How long the cart is still held. Ticks every second, so it's kept apart from the rest of the banner. */
function CartCountdown({ expiresAt, skew }: { expiresAt: number; skew: number }) {
  const now = useNow(1000);
  const remaining = expiresAt - serverNow(skew, now);
  const until = clock(serverToClientMs(expiresAt, skew));
  return (
    <p
      className={cn(
        "flex items-center gap-1.5 text-sm font-medium",
        remaining < CART_EXPIRY_WARNING_S ? "text-warning" : "text-foreground",
      )}
    >
      <Timer aria-hidden className="size-4 shrink-0" />
      {remaining > 0 ? (
        <span>
          {/* A timer is never announced on its own, so this doesn't chatter every second. */}
          <span role="timer" className="tabular">
            {countdown(remaining)}
          </span>{" "}
          left to check out · held until {until}
        </span>
      ) : (
        <span>Time's up: your cart is being released.</span>
      )}
    </p>
  );
}

export function CartBanner({ trip, checkoutUrl, skew }: { trip: Trip; checkoutUrl: string | null; skew: number }) {
  const qc = useQueryClient();
  // The cart being released. "Releasing…" lasts until a state arrives without that cart.
  const [releasingId, setReleasingId] = useState<string | null>(null);

  // 409: the cart id no longer matches (already released, or replaced). Show the server's version.
  const onConflict = (error: unknown) => {
    if (!hasStatus(error, 409)) return false;
    toast.info("Your cart had already changed. Showing the latest.");
    void qc.invalidateQueries({ queryKey: STATE_KEY });
    return true;
  };
  const release = useStateMutation((cartId: string) => api.releaseCart(trip.id, cartId), {
    // If the response still has the cart, the banner keeps saying "Releasing…" until a push drops it.
    success: (cartId, state) =>
      state.trips.find((t) => t.id === trip.id)?.cart?.id === cartId ? null : "Cart released. Watching resumes.",
    onError: onConflict,
  });
  const dismiss = useStateMutation((cartId: string) => api.dismissCart(trip.id, cartId), { onError: onConflict });

  const cart = trip.cart;
  if (!cart) return null;
  const time = timeLabel(cart.depart);
  const failed = cart.status === "failed";
  const releasing = releasingId === cart.id;
  const fare = [cart.fare, cart.price].filter((part) => part !== null && part !== "").join(" ");

  const startRelease = () => {
    if (releasing) return;
    setReleasingId(cart.id);
    release.mutate(cart.id, { onError: () => setReleasingId(null) });
  };

  return (
    <div
      className={cn(
        "flex flex-col gap-4 border-t px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6",
        failed ? "bg-danger-soft" : "bg-accent",
      )}
    >
      <div className="flex min-w-0 items-start gap-3.5">
        <div
          aria-hidden
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-xl [&_svg]:size-[18px]",
            failed ? "bg-danger text-danger-foreground" : "bg-primary text-primary-foreground",
          )}
        >
          {cart.status === "adding" ? (
            <Loader2 className="motion-safe:animate-spin" />
          ) : failed ? (
            <AlertTriangle />
          ) : (
            <ShoppingCart />
          )}
        </div>
        <div className="min-w-0 space-y-1">
          <div className="text-[15px] font-semibold">
            {cart.status === "adding" && `Adding the ${time} sailing to your cart…`}
            {cart.status === "ready" && `The ${time} sailing is in your cart`}
            {failed && `Couldn't add the ${time} sailing to your cart`}
          </div>
          {cart.status === "adding" && <p className="text-sm text-muted-foreground">Selecting your fare.</p>}
          {cart.status === "ready" && (
            <>
              {cart.expires_at !== null && <CartCountdown expiresAt={cart.expires_at} skew={skew} />}
              <p className="text-sm text-muted-foreground">
                {fare && `${fare} · `}
                Added at {clock(serverToClientMs(cart.since, skew))}. Finish checkout in the{" "}
                {checkoutUrl ? "checkout browser" : "watcher's browser window"}. Nothing is booked until you pay.
              </p>
              <p className="text-sm text-muted-foreground">
                Booked it? Pause or remove this trip so it doesn't hold another.
              </p>
            </>
          )}
          {failed && cart.error && <p className="break-words text-sm text-muted-foreground">{cart.error}</p>}
        </div>
      </div>
      {cart.status === "ready" && (
        <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:shrink-0 sm:flex-row">
          <Button variant="outline" onClick={startRelease} disabled={releasing}>
            {releasing ? (
              <>
                <Loader2 className="motion-safe:animate-spin" aria-hidden /> Releasing…
              </>
            ) : (
              <>
                <RotateCcw aria-hidden /> Release
              </>
            )}
          </Button>
          {checkoutUrl && (
            <LinkButton href={checkoutUrl} target="_blank">
              <MonitorSmartphone aria-hidden /> Open checkout
            </LinkButton>
          )}
        </div>
      )}
      {failed && (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => dismiss.mutate(cart.id)}
          disabled={dismiss.isPending}
          className="self-end sm:self-auto"
        >
          <X aria-hidden /> Dismiss
        </Button>
      )}
    </div>
  );
}
