import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "@/test/fake-websocket";
import { BCFERRIES_INFO, BUNTZEN_INFO, FIXTURE_NOW, parsedState, serveState } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";
import { MtApp } from "./MtApp";
import type { WaitlistState } from "./watch-schema";

beforeEach(() => {
  localStorage.setItem("wl.token", "t-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW);
  history.replaceState(null, "", "/new");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** The picker's tiles, by provider name, in order. */
const tiles = () =>
  within(screen.getByRole("list", { name: "Providers" }))
    .getAllByRole("button")
    .map((b) => b.querySelector("span > span")?.textContent);

const tile = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}`) });

function withProviders(changes: (state: WaitlistState) => void): WaitlistState {
  const state = parsedState();
  changes(state);
  return state;
}

describe("the provider picker", () => {
  it("shows a tile per offered provider, with its name, description and tagline", async () => {
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("dialog", { name: "New watch" })).toBeInTheDocument();
    expect(tiles()).toEqual(["BC Ferries", "Buntzen Lake"]);
    expect(tile("Buntzen Lake")).toHaveTextContent(BUNTZEN_INFO.description);
    expect(tile("Buntzen Lake")).toHaveTextContent("Alert only: you book on Yodel");
    expect(tile("BC Ferries")).toHaveTextContent("Can hold a sailing in a cart for you");
  });

  it("leaves out providers not offered to this user, and ones this build doesn't know", async () => {
    serveState(
      withProviders((s) => {
        s.providers = [
          BCFERRIES_INFO,
          { ...BUNTZEN_INFO, offered: false },
          { ...BUNTZEN_INFO, id: "bcparks", name: "BC Parks" },
        ];
      }),
    );
    renderWithProviders(<MtApp />);
    // Only BC Ferries is left, so the picker is skipped.
    expect(await screen.findByRole("dialog", { name: "Add a trip" })).toBeInTheDocument();
    expect(location.pathname).toBe("/new");
  });

  it("disables a provider at its limit and says why; a paused one stays pickable", async () => {
    serveState(
      withProviders((s) => {
        s.capacity.providers.bcferries = { can_add: false, reason: "You can watch up to 3 trips", limit: 3, count: 3 };
        s.providers = [BCFERRIES_INFO, { ...BUNTZEN_INFO, paused: true }];
      }),
    );
    const { user } = renderWithProviders(<MtApp />);
    await screen.findByRole("dialog", { name: "New watch" });
    expect(tile("BC Ferries")).toBeDisabled();
    expect(tile("BC Ferries")).toHaveTextContent("You can watch up to 3 trips");
    expect(tile("Buntzen Lake")).toBeEnabled();
    expect(tile("Buntzen Lake")).toHaveTextContent("Checks are paused by an admin");
    await user.click(tile("Buntzen Lake"));
    expect(await screen.findByRole("dialog", { name: "Watch Buntzen Lake" })).toBeInTheDocument();
  });

  it("puts the provider last used first", async () => {
    localStorage.setItem("wl.lastProvider", "buntzen");
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    await screen.findByRole("dialog", { name: "New watch" });
    expect(tiles()).toEqual(["Buntzen Lake", "BC Ferries"]);
  });

  it("remembers the provider picked", async () => {
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: /^Buntzen Lake/ }));
    expect(localStorage.getItem("wl.lastProvider")).toBe("buntzen");
  });
});
