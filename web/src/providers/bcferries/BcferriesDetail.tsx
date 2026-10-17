/**
 * A BC Ferries trip's page, /w/<id> (docs/waitlist/design.md §7.4): its held cart's checkout first, when there is
 * one, then the trip card.
 */
import { Ship } from "lucide-react";
import { useTerminals } from "@/lib/hooks";
import { CheckoutPanel } from "@/mt/CheckoutPage";
import { MyTripCard } from "@/mt/MyTripCard";
import { routeTitle, shortDate } from "@/mt/trips";
import { ProviderLine } from "@/mt/WatchCard";
import { useWatchSheet } from "@/mt/watches";
import type { BcferriesWatch } from "@/mt/watch-schema";
import type { WatchProps } from "../types";

export function BcferriesDetail({ watch, state, info }: WatchProps<BcferriesWatch>) {
  const terminals = useTerminals();
  const sheet = useWatchSheet();
  const held = watch.browser.cart?.status === "ready";
  return (
    <div className="space-y-4">
      {held && <CheckoutPanel state={state} trip={watch} terminals={terminals.data} />}
      <MyTripCard
        trip={watch}
        terminals={terminals.data}
        onEdit={() => sheet.edit(watch)}
        header={<ProviderLine icon={Ship} name={info?.name ?? "BC Ferries"} date={shortDate(watch.trip.date)} />}
        heading={routeTitle(watch, terminals.data)}
        showHeldCart={!held}
        headingLevel={held ? 2 : 1} // the checkout's title heads the page while it holds a cart
      />
    </div>
  );
}
