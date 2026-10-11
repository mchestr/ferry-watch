import * as Dialog from "@radix-ui/react-dialog";
import { Loader2 } from "lucide-react";
import { useRef, type ReactNode } from "react";
import { Button } from "./button";

/** Asks before something destructive. Focus starts on Cancel; it can't be dismissed while `pending`. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  pending = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  pending?: boolean;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!pending) onOpenChange(next);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[70] bg-slate-950/50 motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0" />
        <Dialog.Content
          role="alertdialog"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelRef.current?.focus();
          }}
          className="fixed left-1/2 top-1/2 z-[70] w-[calc(100%-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border bg-card p-6 shadow-2xl motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0 motion-safe:data-[state=open]:zoom-in-95"
        >
          <Dialog.Title className="text-base font-semibold tracking-tight">{title}</Dialog.Title>
          <Dialog.Description asChild>
            <div className="mt-2 space-y-1 text-sm text-muted-foreground">{description}</div>
          </Dialog.Description>
          <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button ref={cancelRef} variant="outline" disabled={pending} onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button variant="danger" disabled={pending} onClick={onConfirm}>
              {pending && <Loader2 className="motion-safe:animate-spin" aria-hidden />}
              {confirmLabel}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
