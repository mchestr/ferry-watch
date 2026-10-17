import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BUNTZEN_INFO, parsedState, stateWith } from "@/test/fixtures";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { UnknownCard } from "./UnknownCard";

describe("a watch this build can't show", () => {
  const state = parsedState();
  const unknown = state.watches[3]; // the fixture's bcparks watch
  if (unknown?.kind !== "unknown") throw new Error("the fixture's fourth watch is an unknown one");

  it("says it needs a newer Waitlist, with Reload and Remove and nothing else", () => {
    renderWithProviders(<UnknownCard watch={unknown} state={state} info={undefined} />);
    expect(
      screen.getByText("This watch is for bcparks and needs a newer version of Waitlist. Reload the page."),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent?.trim())).toEqual(["Reload", "Remove"]);
  });

  it("names a provider it knows by name", () => {
    const odd = { ...unknown, provider: "buntzen" }; // a Buntzen entry this build couldn't read
    renderWithProviders(<UnknownCard watch={odd} state={state} info={BUNTZEN_INFO} />);
    expect(screen.getByText(/This watch is for Buntzen Lake and needs a newer version/)).toBeInTheDocument();
  });

  it("reloads the page", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    const { user } = renderWithProviders(<UnknownCard watch={unknown} state={state} info={undefined} />);
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("removes it, after asking", async () => {
    const { requests } = mockFetch(() => jsonResponse(stateWith(state.watches.slice(0, 3))));
    const { user } = renderWithProviders(<UnknownCard watch={unknown} state={state} info={undefined} />);
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Stop watching this?" });
    expect(dialog).toHaveTextContent("A watch for bcparks");
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(requests.map((r) => [r.method, r.url])).toEqual([["DELETE", "/api/watches/c0ffee00c0ffee00"]]),
    );
    expect(await screen.findByText("Stopped watching")).toBeInTheDocument();
  });

  it("says so when it was already removed", async () => {
    mockFetch((r) => (r.method === "DELETE" ? jsonResponse({ error: "No such watch" }, 404) : jsonResponse(state)));
    const { user } = renderWithProviders(<UnknownCard watch={unknown} state={state} info={undefined} />);
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("That watch was already removed.")).toBeInTheDocument();
    expect(screen.queryByText("No such watch")).toBeNull(); // one toast, not an error beside it
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("says what went wrong otherwise, and lets them try again", async () => {
    mockFetch((r) => (r.method === "DELETE" ? jsonResponse({ error: "Try again later" }, 500) : jsonResponse(state)));
    const { user } = renderWithProviders(<UnknownCard watch={unknown} state={state} info={undefined} />);
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("Try again later")).toBeInTheDocument();
    expect(screen.queryByText("That watch was already removed.")).toBeNull();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });
});
