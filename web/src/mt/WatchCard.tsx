/**
 * One watch on the dashboard (docs/waitlist/design.md §7.2): its provider's card inside an error boundary, so a card
 * that can't render says so in place, with Remove, and the rest of the page stays. Also the pieces every card
 * shares: the provider line above it and its status badge.
 */
import { AlertTriangle, ChevronRight, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { RemoveWatchButton, UnknownCard } from "@/providers/unknown/UnknownCard";
import type { ProviderUi } from "@/providers/types";
import { navigate } from "./router";
import type { Tone } from "./trips";
import { infoFor } from "./watches";
import type { WaitlistState, Watch } from "./watch-schema";

/** The watch's provider card, or the unknown card; `detail` shows its page instead. */
export function WatchCard({
  watch,
  state,
  ui,
  detail = false,
}: {
  watch: Watch;
  state: WaitlistState;
  ui: ProviderUi | null;
  detail?: boolean;
}) {
  const info = infoFor(state, watch.provider);
  const Shown = ui ? (detail ? ui.Detail : ui.Card) : null;
  return (
    // A new revision of the watch gets a fresh try.
    <ErrorBoundary key={`${watch.id}:${watch.rev}`} fallback={() => <CardFallback watch={watch} detail={detail} />}>
      {Shown ? (
        <Shown watch={watch} state={state} info={info} />
      ) : (
        <>
          {/* On its page, the page needs a title, and the unknown card has none of its own. */}
          {detail && <h1 className="sr-only">A watch this version of Waitlist can't show</h1>}
          <UnknownCard watch={watch} state={state} info={info} />
        </>
      )}
    </ErrorBoundary>
  );
}

/** A card that threw while rendering: say so, and let the watch be removed. On the watch's page, it's the title. */
function CardFallback({ watch, detail }: { watch: Watch; detail: boolean }) {
  const Title = detail ? "h1" : "p";
  return (
    <Card role="alert" className="flex flex-wrap items-center justify-between gap-3 p-4 sm:p-5">
      <Title className="flex min-w-0 items-center gap-2 text-sm font-medium">
        <AlertTriangle className="size-4 shrink-0 text-danger" aria-hidden />
        Couldn't show this watch
      </Title>
      <RemoveWatchButton watchId={watch.id} title="This watch" />
    </Card>
  );
}

/** The line above a card: the provider's icon and name, the date, and (on the dashboard) a link to its page. */
export function ProviderLine({
  icon: Icon,
  name,
  date,
  watchId,
}: {
  icon: LucideIcon;
  name: string;
  date: string;
  /** Links to the watch's page; left out on the page itself. */
  watchId?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      {/* The date is the part that matters (a Buntzen card's title is only its passes): it wraps whole onto a line
          of its own before anything is cut, and only an overlong provider name is truncated. */}
      <p className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        <Icon className="size-3.5 shrink-0" aria-hidden />
        <span className="min-w-0 truncate">
          {name}
          <span aria-hidden> · </span>
          <span className="sr-only">, </span>
        </span>
        <span className="whitespace-nowrap">{date}</span>
      </p>
      {watchId && (
        <a
          href={`/w/${watchId}`}
          onClick={(event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
            event.preventDefault();
            navigate(`/w/${watchId}`);
          }}
          className="-my-1 flex shrink-0 items-center rounded-md py-1 pl-2 text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Details
          <ChevronRight className="size-3.5" aria-hidden />
        </a>
      )}
    </div>
  );
}

const VARIANT = {
  success: "success",
  warning: "warning",
  danger: "danger",
  soft: "soft",
  outline: "outline",
  "success-outline": "outline",
  muted: "muted",
} as const satisfies Record<Tone, string>;

/** A status badge in one of the watch tones; `pulse` draws attention to something open right now. */
export function ToneBadge({
  tone,
  pulse = false,
  className,
  children,
}: {
  tone: Tone;
  pulse?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Badge
      variant={VARIANT[tone]}
      className={cn(tone === "success-outline" && "border-success/60 text-success", className)}
    >
      {pulse && (
        <span className="relative flex size-2" aria-hidden>
          <span className="absolute inline-flex size-full rounded-full bg-warning opacity-75 motion-safe:animate-ping" />
          <span className="relative inline-flex size-2 rounded-full bg-warning" />
        </span>
      )}
      {children}
    </Badge>
  );
}
