import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { State } from "@/lib/schema";
import { FakeWebSocket } from "@/test/fake-websocket";
import { NOW, NOW_S, TERMINALS, makeState, makeTrip } from "@/test/fixtures";
import { jsonResponse, mockFetch, opaqueRedirect, renderWithProviders } from "@/test/render";
import App from "./App";

/** Answers the two GETs the app makes on load. */
const serve = (state: State) =>
  mockFetch(({ url }) => (url === "/api/terminals" ? jsonResponse({ terminals: TERMINALS }) : jsonResponse(state)));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("App", () => {
  it("shows the trips, and goes live once the socket delivers a message", async () => {
    const { requests } = serve(makeState({ trips: [makeTrip({ past: true })] }));
    renderWithProviders(<App />);
    expect(await screen.findByRole("heading", { name: "Swartz Bay → Tsawwassen · Mon Oct 12" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Watch Swartz Bay → Tsawwassen · Mon Oct 12" })).toBeChecked();
    expect(screen.getByText("This date has passed")).toBeInTheDocument();
    // Opening the socket isn't enough to claim "live".
    expect(screen.getByRole("img", { name: "Connecting to live updates" })).toBeInTheDocument();
    act(() => FakeWebSocket.latest.serverOpen());
    expect(screen.getByRole("img", { name: "Connecting to live updates" })).toBeInTheDocument();
    act(() => FakeWebSocket.latest.serverSend({ type: "ping", server_time: NOW_S }));
    expect(screen.getByRole("img", { name: "Live updates on" })).toBeInTheDocument();
    expect(requests.map((r) => r.url).sort()).toEqual(["/api/state", "/api/terminals"]);
  });

  it("applies pushed states", async () => {
    serve(makeState({ seq: 1 }));
    renderWithProviders(<App />);
    expect(await screen.findByText("1 of 6 watched")).toBeInTheDocument();
    act(() => FakeWebSocket.latest.serverSend({ type: "state", data: makeState({ seq: 2, trips: [] }) }));
    expect(await screen.findByText("No trips yet")).toBeInTheDocument();
    // An older snapshot arriving late changes nothing.
    act(() => FakeWebSocket.latest.serverSend({ type: "state", data: makeState({ seq: 1 }) }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText("No trips yet")).toBeInTheDocument();
  });

  it("opens the trip sheet to add or edit, and closes it", async () => {
    serve(makeState());
    const { user } = renderWithProviders(<App />);
    await user.click(await screen.findByRole("button", { name: "Add trip" }));
    const adding = screen.getByRole("dialog", { name: "Add a trip" });
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "From" })).toHaveTextContent("Victoria (Swartz Bay)"),
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(adding).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit Swartz Bay → Tsawwassen · Mon Oct 12" }));
    expect(screen.getByRole("dialog", { name: "Edit trip" })).toHaveAccessibleDescription(
      "Swartz Bay → Tsawwassen · Mon Oct 12. Changes take effect on the next check.",
    );
  });

  it("asks you to sign in again when the auth proxy redirects", async () => {
    mockFetch(() => opaqueRedirect());
    renderWithProviders(<App />);
    expect(await screen.findByText("Your sign-in expired.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });
});
