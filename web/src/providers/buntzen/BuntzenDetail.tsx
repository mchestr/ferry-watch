/**
 * A Buntzen Lake watch's page, /w/<id>: where its alert lands (docs/waitlist/design.md §7.4). What the alert was for
 * comes first: the status and, when a pass is open, Book on Yodel, full width. Then each pass, the timeline and how
 * booking works.
 */
import { ArrowLeft, Trees } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useNow } from "@/lib/hooks";
import { clock } from "@/lib/time";
import { navigate } from "@/mt/router";
import { ProviderLine, ToneBadge } from "@/mt/WatchCard";
import type { BuntzenWatch } from "@/mt/watch-schema";
import type { WatchProps } from "../types";
import { BookOnYodel, BuntzenActions, FOOTER, Notes, PassList } from "./BuntzenCard";
import { badge, bookable, dayLabel, isPass, passesPhrase, passesTitle, timeline } from "./labels";

/** The line under the title: what's open, in plain words, or nothing when the badge and notes say it all. */
function statusLine(watch: BuntzenWatch, now: number): string | null {
  const open = bookable(watch, now);
  const certain = open
    .filter((o) => o.status === "open")
    .map((o) => o.key)
    .filter(isPass);
  const uncertain = open
    .filter((o) => o.status === "maybe")
    .map((o) => o.key)
    .filter(isPass);
  const verb = (n: number) => (n === 1 ? "is" : "are");
  if (certain.length > 0) {
    const names = passesPhrase(certain).replace(/ pass$/, certain.length > 1 ? " passes" : " pass");
    return `The ${names} ${verb(certain.length)} open on Yodel for ${dayLabel(watch.spec.date)}. Book it now: passes go fast.`;
  }
  if (uncertain.length > 0) {
    return "Yodel shows a status we don't recognise; it may be bookable. Check on Yodel now.";
  }
  return null;
}

export function BuntzenDetail({ watch, state, info }: WatchProps<BuntzenWatch>) {
  const now = useNow(30_000);
  const status = badge(watch, now);
  const line = statusLine(watch, now);
  const checked = watch.check.checked_at; // the last check that read Yodel, never a failed attempt
  return (
    <div className="space-y-4">
      <Card className="space-y-4 p-5 sm:p-6">
        <ProviderLine icon={Trees} name={info?.name ?? "Buntzen Lake"} date={dayLabel(watch.spec.date)} />
        <div className="flex items-start justify-between gap-3">
          <h1 className="min-w-0 text-lg font-bold tracking-tight">{passesTitle(watch.spec.passes)}</h1>
          <ToneBadge tone={status.tone} pulse={status.pulse}>
            {status.text}
          </ToneBadge>
        </div>
        {line && <p className="text-sm font-medium">{line}</p>}
        <BookOnYodel watch={watch} now={now} full />
        <Notes watch={watch} state={state} now={now} cadence={false} />
      </Card>

      <Card className="space-y-3 p-5 sm:p-6">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Passes</h2>
        <PassList watch={watch} now={now} />
        <p className="text-sm text-muted-foreground">
          {checked !== null ? `Checked ${clock(checked * 1000)}.` : "Not checked yet."} {timeline(watch)}.
        </p>
        <BuntzenActions watch={watch} onRemoved={() => navigate("/")} />
      </Card>

      <Card className="space-y-2 p-5 sm:p-6">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">How booking works</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li>You book on Yodel with a Yodel account that has your licence plate and phone number.</li>
          <li>Passes are tied to them and can't be transferred.</li>
          <li>Waitlist never asks for your Yodel password.</li>
        </ul>
        <p className="pt-1 text-xs text-muted-foreground">{FOOTER}</p>
      </Card>

      <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
        <ArrowLeft aria-hidden /> Back to your watches
      </Button>
    </div>
  );
}
