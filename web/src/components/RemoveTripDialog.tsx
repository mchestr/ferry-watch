import { removeConsequence, type CartSummary } from "@/lib/trip";
import { ConfirmDialog } from "./ui/confirm-dialog";

/**
 * Confirms removing a watch, on any provider: "Stop watching this?", or in the provider's own words (a BC Ferries
 * trip's "Remove this trip?"). With a held cart, it names the sailing that will be released.
 */
export function RemoveTripDialog({
  trip,
  open,
  onOpenChange,
  onConfirm,
  pending,
  title = "Stop watching this?",
  confirmLabel = "Remove",
}: {
  trip: { title: string; cart: CartSummary | null };
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  pending: boolean;
  title?: string;
  confirmLabel?: string;
}) {
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={
        <>
          <p className="font-medium text-foreground">{trip.title}</p>
          <p>{removeConsequence(trip)}</p>
        </>
      }
      confirmLabel={confirmLabel}
      onConfirm={onConfirm}
      pending={pending}
    />
  );
}
