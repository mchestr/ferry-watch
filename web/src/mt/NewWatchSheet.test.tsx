import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "@/test/fake-websocket";
import { BCFERRIES_INFO, BUNTZEN_INFO, FIXTURE_NOW, parsedState, serveState } from "@/test/fixtures";
import { jsonResponse, renderWithProviders } from "@/test/render";
import { MtApp } from "./MtApp";

beforeEach(() => {
  // jsdom has no pointer capture; a toast's action button (View) asks for it.
  Element.prototype.setPointerCapture = () => undefined;
  localStorage.setItem("wl.token", "t-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW); // Wed Jun 30 2027, 7:31 am
  history.replaceState(null, "", "/");
});

afterEach(() => {
  Reflect.deleteProperty(Element.prototype, "setPointerCapture");
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("New watch", () => {
  it("opens on the picker from the dashboard, then the provider's form, and back", async () => {
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "New watch" }));
    expect(await screen.findByRole("dialog", { name: "New watch" })).toBeInTheDocument();
    expect(location.pathname).toBe("/new");
    await user.click(screen.getByRole("button", { name: /^Buntzen Lake/ }));
    expect(await screen.findByRole("dialog", { name: "Watch Buntzen Lake" })).toBeInTheDocument();
    expect(location.pathname).toBe("/new/buntzen");
    await user.click(screen.getByRole("button", { name: "All providers" }));
    expect(await screen.findByRole("dialog", { name: "New watch" })).toBeInTheDocument();
    expect(location.pathname).toBe("/new");
  });

  it("closes back to the dashboard, popping the entry it pushed", async () => {
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "New watch" }));
    await screen.findByRole("dialog", { name: "New watch" });
    const length = history.length;
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(location.pathname).toBe("/"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(history.length).toBe(length); // went back, not forward
  });

  it("opens straight on a provider's form at /new/<provider> (the PWA shortcuts)", async () => {
    history.replaceState(null, "", "/new/buntzen");
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByRole("dialog", { name: "Watch Buntzen Lake" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "All providers" })).toBeNull(); // the picker was skipped
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(location.pathname).toBe("/"));
    expect(screen.getByRole("heading", { name: "Your watches" })).toBeInTheDocument();
  });

  it.each([
    ["a provider this build doesn't know", "/new/bcparks", undefined],
    ["a provider not offered to this user", "/new/buntzen", { ...BUNTZEN_INFO, offered: false }],
  ])("falls back to the picker for %s", async (_, path, buntzen) => {
    history.replaceState(null, "", path);
    const state = parsedState();
    if (buntzen) state.providers = [BCFERRIES_INFO, buntzen, { ...BUNTZEN_INFO, id: "bcparks", name: "BC Parks" }];
    serveState(state);
    renderWithProviders(<MtApp />);
    await waitFor(() => expect(location.pathname).toBe("/new"));
    if (buntzen) {
      // Only BC Ferries is offered: its form, with no picker.
      expect(await screen.findByRole("dialog", { name: "Add a trip" })).toBeInTheDocument();
    } else {
      expect(await screen.findByRole("dialog", { name: "New watch" })).toBeInTheDocument();
    }
  });

  it("takes four taps from the dashboard to watch Buntzen Lake tomorrow", async () => {
    const created = parsedState();
    const { requests } = serveState(parsedState(), (r) =>
      r.url === "/api/watches" && r.method === "POST"
        ? jsonResponse({ watch_id: "0f3c9a1b2d4e5f60", state: created })
        : undefined,
    );
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "New watch" })); // 1
    await user.click(await screen.findByRole("button", { name: /^Buntzen Lake/ })); // 2
    await user.click(await screen.findByRole("radio", { name: "Tomorrow" })); // 3
    await user.click(screen.getByRole("button", { name: "Start watching" })); // 4
    await waitFor(() =>
      expect(requests.find((r) => r.url === "/api/watches")?.body).toEqual({
        provider: "buntzen",
        spec: { date: "2027-07-01", passes: ["full_day", "morning", "afternoon"], release_reminder: true },
      }),
    );
    expect(await screen.findByText("Watching Buntzen Lake for Thu Jul 1")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(location.pathname).toBe("/");
    await user.click(screen.getByRole("button", { name: "View" }));
    expect(location.pathname).toBe("/w/0f3c9a1b2d4e5f60");
  });

  it("edits a watch in place, from its card", async () => {
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    const card = (await screen.findByRole("heading", { name: "All-day pass" })).closest("div.rounded-2xl");
    if (!(card instanceof HTMLElement)) throw new Error("no card");
    await user.click(within(card).getByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("dialog", { name: "Edit watch" })).toBeInTheDocument();
    expect(location.pathname).toBe("/");
    expect(screen.getByRole("radio", { name: "Sat 3" })).toBeChecked();
  });
});
