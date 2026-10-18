import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TERMINALS_KEY } from "@/lib/hooks";
import { BCFERRIES_INFO, FIXTURE_NOW, parsedState, stateWith, TERMINALS, tripsOf, fail } from "@/test/fixtures";
import { createTestQueryClient, jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { BcferriesCard } from "./BcferriesCard";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function show() {
  const state = parsedState();
  const watch = tripsOf(state)[0] ?? fail();
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(TERMINALS_KEY, TERMINALS);
  return {
    watch,
    ...renderWithProviders(<BcferriesCard watch={watch} state={state} info={BCFERRIES_INFO} />, { queryClient }),
  };
}

describe("a BC Ferries card", () => {
  it("puts the provider line above the trip card, which names the route", () => {
    const { watch } = show();
    const line = screen.getByText(
      (_, element) => element?.tagName === "P" && (element.textContent ?? "").startsWith("BC Ferries"),
    );
    expect(line).toHaveTextContent("BC Ferries · , Sat Jul 3");
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Tsawwassen → Swartz Bay");
    expect(screen.getByText("Checking")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Details/ })).toHaveAttribute("href", `/w/${watch.id}`);
  });

  it("pauses through PATCH /api/watches/{id} {enabled}, and checks through /api/watches/{id}/check", async () => {
    const state = parsedState();
    const { requests } = mockFetch(() => jsonResponse(stateWith(state.watches)));
    const { user, watch } = show();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Trip paused")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() =>
      expect(requests.map((r) => [r.method, r.url, r.body])).toEqual([
        ["PATCH", `/api/watches/${watch.id}`, { enabled: false }],
        ["POST", `/api/watches/${watch.id}/check`, {}],
      ]),
    );
  });

  it("removes the trip with its own words, through DELETE /api/watches/{id}", async () => {
    const { requests } = mockFetch(() => jsonResponse(stateWith([])));
    const { user, watch } = show();
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await user.click(screen.getByRole("button", { name: "Remove trip" }));
    expect(await screen.findByText("Trip removed")).toBeInTheDocument();
    expect(requests.map((r) => [r.method, r.url])).toEqual([["DELETE", `/api/watches/${watch.id}`]]);
  });
});
