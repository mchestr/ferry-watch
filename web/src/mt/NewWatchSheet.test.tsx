import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "@/test/fake-websocket";
import { BCFERRIES_INFO, BUNTZEN_INFO, FIXTURE_NOW, parsedState, serveState, stateWith } from "@/test/fixtures";
import { jsonResponse, renderWithProviders } from "@/test/render";
import { MY_STATE_KEY } from "./api";
import { MtApp } from "./MtApp";
import type { WaitlistState } from "./watch-schema";

/** `state` with no room left for another Buntzen watch. */
const buntzenFull = (state: WaitlistState): WaitlistState => ({
  ...state,
  capacity: {
    ...state.capacity,
    providers: {
      ...state.capacity.providers,
      buntzen: { can_add: false, reason: "You can have up to 6 alert-only watches", count: 6, limit: 6 },
    },
  },
});

/** Every title the sheet shows from now on, in order, each once in a row. */
function titlesShown() {
  const seen: string[] = [];
  const look = () => {
    const id = document.querySelector('[role="dialog"]')?.getAttribute("aria-labelledby");
    const title = id ? document.getElementById(id)?.textContent : undefined;
    if (title && seen.at(-1) !== title) seen.push(title);
  };
  const observer = new MutationObserver(look);
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  look();
  return { seen, stop: () => observer.disconnect() };
}

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

  it("keeps the form showing while the save that fills the last slot closes it", async () => {
    const { requests } = serveState(parsedState(), (r) =>
      r.url === "/api/watches" && r.method === "POST"
        ? jsonResponse({ watch_id: "0f3c9a1b2d4e5f60", state: buntzenFull(parsedState()) })
        : undefined,
    );
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "New watch" }));
    await user.click(await screen.findByRole("button", { name: /^Buntzen Lake/ }));
    await user.click(await screen.findByRole("radio", { name: "Tomorrow" }));
    const titles = titlesShown();
    await user.click(screen.getByRole("button", { name: "Start watching" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    titles.stop();
    expect(requests.some((r) => r.method === "POST")).toBe(true);
    expect(titles.seen).toEqual(["Watch Buntzen Lake"]); // never the picker on its way out
    expect(location.pathname).toBe("/");
  });

  it("doesn't take an open form away when another device fills the last slot", async () => {
    serveState(parsedState());
    const { user, queryClient } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "New watch" }));
    await user.click(await screen.findByRole("button", { name: /^Buntzen Lake/ }));
    await user.click(await screen.findByRole("radio", { name: "Fri 2" }));
    const state = parsedState();
    await act(async () => {
      queryClient.setQueryData<WaitlistState>(MY_STATE_KEY, buntzenFull(stateWith(state.watches.slice(0, 3))));
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByText("3 watched")).toBeInTheDocument()); // the new state is in
    expect(screen.getByRole("dialog", { name: "Watch Buntzen Lake" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Fri 2" })).toBeChecked();
    expect(location.pathname).toBe("/new/buntzen");
  });

  it("gives focus back to what opened it", async () => {
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    const add = await screen.findByRole("button", { name: "New watch" });
    await user.click(add);
    await screen.findByRole("dialog", { name: "New watch" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(add).toHaveFocus();

    const card = (await screen.findByRole("heading", { name: "All-day pass" })).closest("div.rounded-2xl");
    if (!(card instanceof HTMLElement)) throw new Error("no card");
    const edit = within(card).getByRole("button", { name: "Edit" });
    await user.click(edit);
    await user.click(
      within(await screen.findByRole("dialog", { name: "Edit watch" })).getByRole("button", { name: "Cancel" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(edit).toHaveFocus();
  });

  it("gives focus to the page's heading when nothing on the page opened it", async () => {
    history.replaceState(null, "", "/new/buntzen"); // the address bar, or a PWA shortcut
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("heading", { level: 1, name: "Your watches" })).toHaveFocus();
  });

  it("says it can't open a form whose provider's catalog entry it couldn't read, rather than open empty", async () => {
    const state = parsedState();
    serveState(stateWith(state.watches, { providers: [BCFERRIES_INFO] }));
    const { user } = renderWithProviders(<MtApp />);
    const card = (await screen.findByRole("heading", { name: "All-day pass" })).closest("div.rounded-2xl");
    if (!(card instanceof HTMLElement)) throw new Error("no card");
    await user.click(within(card).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Can't open this form" });
    expect(dialog).toHaveAccessibleDescription("This version of Waitlist can't show it. Reload the page.");
    expect(within(dialog).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    await user.click(within(dialog).getAllByRole("button", { name: "Close" }).at(-1) ?? dialog);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
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
