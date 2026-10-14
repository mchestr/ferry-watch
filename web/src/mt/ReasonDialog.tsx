import * as Dialog from "@radix-ui/react-dialog";
import { Loader2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/controls";

/**
 * Asks an admin why, before a change that lands in the audit log. Nothing happens without a reason, and it
 * can't be dismissed while `pending`.
 */
export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  pending = false,
  error = null,
  danger = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  onConfirm: (reason: string) => void;
  pending?: boolean;
  error?: string | null;
  danger?: boolean;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => {
    if (open) setReason("");
  }, [open]);
  const trimmed = reason.trim();
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!pending) onOpenChange(next);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[70] bg-slate-950/50 motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-[70] w-[calc(100%-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border bg-card p-6 shadow-2xl motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0 motion-safe:data-[state=open]:zoom-in-95">
          <Dialog.Title className="text-base font-semibold tracking-tight">{title}</Dialog.Title>
          <Dialog.Description asChild>
            <div className="mt-2 space-y-1 text-sm text-muted-foreground">
              {description ?? "This goes in the audit log."}
            </div>
          </Dialog.Description>
          <form
            className="mt-4 space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (trimmed && !pending) onConfirm(trimmed);
            }}
          >
            <Field label="Reason" error={error}>
              <Input value={reason} maxLength={500} autoFocus onChange={(e) => setReason(e.target.value)} />
            </Field>
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Button type="button" variant="outline" disabled={pending} onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" variant={danger ? "danger" : "default"} disabled={pending || !trimmed}>
                {pending && <Loader2 className="motion-safe:animate-spin" aria-hidden />}
                {confirmLabel}
              </Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
