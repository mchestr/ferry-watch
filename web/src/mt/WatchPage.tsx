/**
 * /w/<id>: one watch's page, where its alert lands (docs/waitlist/design.md §7.1, §7.4). It shows the provider's
 * Detail, inside the same error boundary as a card.
 */
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { uiFor } from "@/providers/registry";
import { navigate } from "./router";
import { WatchCard } from "./WatchCard";
import type { WaitlistState } from "./watch-schema";

export function WatchPage({ state, watchId }: { state: WaitlistState; watchId: string }) {
  const watch = state.watches.find((w) => w.id === watchId);
  return (
    <div className="mx-auto max-w-xl space-y-4">
      <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
        <ArrowLeft aria-hidden /> Your watches
      </Button>
      {watch ? (
        <WatchCard watch={watch} state={state} ui={uiFor(watch)} detail />
      ) : (
        <Card className="p-6">
          <h1 className="font-semibold">This watch isn't on your list any more</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            It was booked, removed or has ended.{" "}
            <a
              href="/"
              className="font-medium text-primary underline-offset-2 hover:underline"
              onClick={(event) => {
                event.preventDefault();
                navigate("/");
              }}
            >
              See your other watches
            </a>
            .
          </p>
        </Card>
      )}
    </div>
  );
}
