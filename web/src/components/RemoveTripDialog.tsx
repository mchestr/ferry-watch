import { removeConsequence, type CartSummary } from "@/lib/trip";
import { ConfirmDialog } from "./ui/confirm-dialog";

/** Confirms removing a trip. With a held cart, it names the sailing that will be released. */
export function RemoveTripDialog({
  trip,
  open,
  onOpenChange,
  onConfirm,
  pending,
}: {
  trip: { title: string; cart: CartSummary | null };
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Remove this trip?"
      description={
        <>
          <p className="font-medium text-foreground">{trip.title}</p>
          <p>{removeConsequence(trip)}</p>
        </>
      }
      confirmLabel="Remove trip"
      onConfirm={onConfirm}
      pending={pending}
    />
  );
}
