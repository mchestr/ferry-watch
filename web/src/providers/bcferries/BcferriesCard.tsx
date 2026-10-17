/**
 * A BC Ferries trip on the dashboard (docs/waitlist/design.md §7.4): the provider line above the trip card, which
 * is unchanged: its sailings, held cart, "Hold it for me" and "Did you book it?".
 */
import { Ship } from "lucide-react";
import { useTerminals } from "@/lib/hooks";
import { MyTripCard } from "@/mt/MyTripCard";
import { routeTitle, shortDate } from "@/mt/trips";
import { ProviderLine } from "@/mt/WatchCard";
import { useWatchSheet } from "@/mt/watches";
import type { BcferriesWatch } from "@/mt/watch-schema";
import type { WatchProps } from "../types";

export function BcferriesCard({ watch, info }: WatchProps<BcferriesWatch>) {
  const terminals = useTerminals();
  const sheet = useWatchSheet();
  return (
    <MyTripCard
      trip={watch}
      terminals={terminals.data}
      onEdit={() => sheet.edit(watch)}
      header={
        <ProviderLine
          icon={Ship}
          name={info?.name ?? "BC Ferries"}
          date={shortDate(watch.trip.date)}
          watchId={watch.id}
        />
      }
      heading={routeTitle(watch, terminals.data)}
    />
  );
}
