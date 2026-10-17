/**
 * A watch this build can't show (docs/waitlist/design.md §7.4): a provider it doesn't know (a newer server's), or an
 * entry it can't read. Enough to know it's there and to remove it, and nothing else.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { HelpCircle, RotateCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { RemoveTripDialog } from "@/components/RemoveTripDialog";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { hasStatus } from "@/lib/api";
import { reportError } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { MY_STATE_KEY, myApi } from "@/mt/api";
import type { Watch } from "@/mt/watch-schema";
import type { WatchProps } from "../types";

export function UnknownCard({ watch, info }: WatchProps<Watch>) {
  const name = info?.name ?? watch.provider;
  return (
    <Card className="space-y-3 p-4 sm:p-5">
      <p className="flex items-start gap-2 text-sm">
        <HelpCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="min-w-0 break-words">
          This watch is for {name} and needs a newer version of Waitlist. Reload the page.
        </span>
      </p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
          <RotateCw aria-hidden /> Reload
        </Button>
        <RemoveWatchButton watchId={watch.id} title={`A watch for ${name}`} />
      </div>
    </Card>
  );
}

/** Remove, after asking: DELETE /api/watches/{id}, for a watch on any provider. */
export function RemoveWatchButton({
  watchId,
  title,
  removed = "Stopped watching",
  onRemoved,
  className,
}: {
  watchId: string;
  /** Named in the dialog. */
  title: string;
  /** The toast once it's gone. */
  removed?: string;
  onRemoved?: () => void;
  className?: string;
}) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const remove = useMutation({
    mutationFn: () => myApi.removeWatch(watchId),
    onSuccess: (state) => {
      qc.setQueryData(MY_STATE_KEY, state);
      toast.success(removed);
    },
    onError: (error) => {
      // Already gone (removed on another device, say): say so, once, and fetch what's there now.
      if (hasStatus(error, 404)) {
        setConfirming(false);
        toast.info("That watch was already removed.");
        void qc.invalidateQueries({ queryKey: MY_STATE_KEY });
      } else reportError(error);
    },
  });
  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        className={cn("hover:text-danger", className)}
        onClick={() => setConfirming(true)}
      >
        <Trash2 aria-hidden /> Remove
      </Button>
      <RemoveTripDialog
        trip={{ title, cart: null }}
        open={confirming}
        onOpenChange={setConfirming}
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(undefined, {
            onSuccess: () => {
              setConfirming(false);
              onRemoved?.();
            },
          })
        }
      />
    </>
  );
}
