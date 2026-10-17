import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TERMINALS_KEY } from "@/lib/hooks";
import type { BcferriesWatch } from "@/mt/watch-schema";
import { BCFERRIES_INFO, FIXTURE_NOW, parsedState, stateWith, TERMINALS, tripsOf, fail } from "@/test/fixtures";
import { createTestQueryClient, renderWithProviders } from "@/test/render";
import { BcferriesDetail } from "./BcferriesDetail";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function show(watch: BcferriesWatch) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(TERMINALS_KEY, TERMINALS);
  return renderWithProviders(<BcferriesDetail watch={watch} state={stateWith([watch])} info={BCFERRIES_INFO} />, {
    queryClient,
  });
}

describe("a BC Ferries trip's page", () => {
  const trip = tripsOf(parsedState())[0] ?? fail();

  it("is the trip card, without a link to itself, its title the page's", () => {
    show(trip);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("→");
    expect(screen.queryByRole("link", { name: /Details/ })).toBeNull();
  });

  it("leads with the held cart's checkout", () => {
    show({
      ...trip,
      browser: {
        ...trip.browser,
        cart: {
          id: "cart1",
          status: "ready",
          depart: "09:00",
          fare: null,
          price: null,
          ready_at: null,
          unclaimed_deadline: FIXTURE_NOW.getTime() / 1000 + 600,
          session_deadline: null,
          hard_deadline: null,
          payment_in_progress: false,
        },
      },
    });
    const checkout = screen.getByRole("button", { name: /Check out on this device/ });
    const card = screen.getByRole("heading", { level: 2 });
    expect(checkout.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("button", { name: "Release it, I'll book it myself" })).toBeInTheDocument();
  });
});
