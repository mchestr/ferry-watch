import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { providerUi } from "@/providers/registry";
import type { ProviderUi } from "@/providers/types";
import { parsedState, stateWith, fail } from "@/test/fixtures";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { WatchCard } from "./WatchCard";

function Boom(): never {
  throw new Error("This card can't render");
}

/** Buntzen's UI with a card that throws while rendering. */
function broken(): ProviderUi {
  const ui = providerUi("buntzen");
  if (!ui) throw new Error("no buntzen UI");
  return { ...ui, Card: Boom };
}

describe("a watch's card", () => {
  it("shows its provider's card", () => {
    const state = parsedState();
    const watch = state.watches[0] ?? fail();
    renderWithProviders(<WatchCard watch={watch} state={state} ui={providerUi("buntzen")} />);
    expect(screen.getByRole("heading", { name: "Morning or afternoon pass" })).toBeInTheDocument();
  });

  it("contains a card that throws: it says so in place, with Remove, and the rest of the page stays", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined); // React reports the error it caught
    const state = parsedState();
    const [bad, good] = state.watches;
    const { requests } = mockFetch(() => jsonResponse(stateWith([good ?? fail()])));
    const { user } = renderWithProviders(
      <>
        <WatchCard watch={bad ?? fail()} state={state} ui={broken()} />
        <WatchCard watch={good ?? fail()} state={state} ui={providerUi("buntzen")} />
      </>,
    );
    const fallback = screen.getByRole("alert");
    expect(fallback).toHaveTextContent("Couldn't show this watch");
    expect(screen.getByRole("heading", { name: "All-day pass" })).toBeInTheDocument(); // the other card
    expect(screen.queryByText("Something went wrong")).toBeNull();
    await user.click(within(fallback).getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Stop watching this?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(requests.filter((r) => r.method === "DELETE").map((r) => r.url)).toEqual([
        `/api/watches/${(bad ?? fail()).id}`,
      ]),
    );
    expect(await screen.findByText("Stopped watching")).toBeInTheDocument();
  });

  it("leaves the full-screen page to an error boundary without a fallback", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderWithProviders(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByText("This card can't render")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });
});
