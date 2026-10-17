/**
 * The BC Ferries form in the New watch sheet (docs/waitlist/design.md §7.3): the existing trip form (route, date,
 * party, vehicle, times, auto-hold), unchanged, saving through /api/watches.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef } from "react";
import { TripSheetForm, type OpenMode, type TripSource } from "@/components/TripSheet";
import { useTerminals } from "@/lib/hooks";
import type { TripSettings } from "@/lib/schema";
import { MY_STATE_KEY, myApi } from "@/mt/api";
import { tripTitle } from "@/mt/trips";
import { rememberProvider } from "@/mt/watches";
import type { BcferriesWatch, WaitlistState, WatchPatchRequest } from "@/mt/watch-schema";
import type { SheetProps } from "../types";

/** A new trip's defaults; the form fills in the date. */
const DEFAULTS: TripSettings = {
  enabled: true,
  from: "",
  to: "",
  date: "",
  adults: 1,
  children: 0,
  infants: 0,
  seniors: 0,
  vehicle: "standard",
  height_ft: 8,
  earliest: "00:00",
  latest: "23:59",
  auto_cart: true,
  fare: "cheapest",
};

export function BcferriesSheet({ mode, state, onDone }: SheetProps<BcferriesWatch>) {
  const qc = useQueryClient();
  const terminals = useTerminals();
  const created = useRef<string | null>(null);
  const source = useMemo((): TripSource => {
    const keep = (next: WaitlistState) => qc.setQueryData(MY_STATE_KEY, next);
    return {
      defaults: DEFAULTS,
      trips: state.watches.flatMap((w) =>
        w.kind === "bcferries" ? [{ ...w.trip, title: tripTitle(w, terminals.data), cart: w.browser.cart }] : [],
      ),
      save: async (request) => {
        if (request.kind === "add") {
          // `enabled` belongs beside the spec, never in it.
          const { enabled, ...spec } = request.settings;
          const answer = await myApi.addWatch({ provider: "bcferries", spec, enabled });
          created.current = answer.watch_id;
          rememberProvider("bcferries"); // the picker lists it first next time
          keep(answer.state);
        } else {
          const { enabled, ...spec } = request.patch;
          const patch: WatchPatchRequest = {};
          if (Object.keys(spec).length > 0) patch.spec = spec;
          if (enabled !== undefined) patch.enabled = enabled;
          keep(await myApi.updateWatch(request.id, patch));
        }
      },
      remove: async (id) => {
        keep(await myApi.removeWatch(id));
      },
      autoCartHint: "Holds the sailing in a cart; you check out in a browser we keep for you.",
      addedMessage: "Trip added. It'll be checked shortly.",
    };
  }, [qc, state, terminals.data]);
  const tripMode: OpenMode = mode.kind === "editing" ? { kind: "editing", tripId: mode.watch.id } : { kind: "adding" };
  return <TripSheetForm mode={tripMode} open source={source} onClose={() => onDone(created.current)} />;
}
