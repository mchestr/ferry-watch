import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Right-hand slide-over panel (full screen on phones). Its content unmounts once it has animated
 * closed. Compose with SheetHeader, SheetBody and SheetFooter.
 */
export function Sheet({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-slate-950/40 backdrop-blur-[2px] motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0 motion-safe:data-[state=closed]:animate-out motion-safe:data-[state=closed]:fade-out-0" />
        <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-full flex-col bg-background shadow-2xl duration-300 sm:max-w-[480px] sm:border-l motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:slide-in-from-right motion-safe:data-[state=closed]:animate-out motion-safe:data-[state=closed]:slide-out-to-right">
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function SheetHeader({
  title,
  description,
  closeDisabled,
}: {
  title: string;
  description: string;
  closeDisabled?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b bg-card px-6 py-5">
      <div className="min-w-0">
        <Dialog.Title className="text-lg font-semibold tracking-tight">{title}</Dialog.Title>
        <Dialog.Description className="mt-0.5 text-sm text-muted-foreground">{description}</Dialog.Description>
      </div>
      <Dialog.Close
        disabled={closeDisabled}
        className="-mr-2 rounded-lg p-2 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
      >
        <X className="size-5" aria-hidden />
        <span className="sr-only">Close</span>
      </Dialog.Close>
    </div>
  );
}

export function SheetBody({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("flex-1 overflow-y-auto px-6 py-6", className)}>{children}</div>;
}

export function SheetFooter({ children }: { children: React.ReactNode }) {
  return <div className="flex justify-end gap-2 border-t bg-card px-6 py-4">{children}</div>;
}
