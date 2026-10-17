import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "@/test/fake-websocket";
import { FIXTURE_NOW, makeBuntzenWatch, parsedState, serveState, stateWith, fail } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";
import { MtApp } from "./MtApp";

beforeEach(() => {
  localStorage.setItem("wl.token", "t-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW); // Wed Jun 30 2027, 7:31 am
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** The section headed `title`. */
const section = (title: string) => {
  const heading = screen.getByRole("heading", { level: 2, name: new RegExp(`^${title}`) });
  const element = heading.closest("section");
  if (!element) throw new Error(`${title} isn't a section`);
  return element;
};

describe("the dashboard", () => {
  it("mixes every provider's watches in one list, grouped by what they need", async () => {
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    const heading = await screen.findByRole("heading", { level: 1, name: "Your watches" });
    expect(heading.nextElementSibling).toHaveTextContent("4 watched");
    expect(screen.getByRole("button", { name: "Waitlist" })).toBeInTheDocument();
    const titles = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(titles).toEqual([
      "Needs you now1",
      "Morning or afternoon pass",
      "Watching3",
      "All-day pass",
      "Tsawwassen → Swartz Bay",
    ]);
    expect(within(section("Needs you now")).getByText("Open now")).toBeInTheDocument();
    const watching = section("Watching");
    expect(within(watching).getByText("On sale Fri 7:00 am")).toBeInTheDocument();
    expect(within(watching).getByText("Checking")).toBeInTheDocument();
    // A newer server's provider is listed, and can be removed, without blanking the page.
    expect(
      within(watching).getByText("This watch is for bcparks and needs a newer version of Waitlist. Reload the page."),
    ).toBeInTheDocument();
  });

  it("says what's next when nothing needs the user", async () => {
    const state = parsedState();
    serveState(stateWith(state.watches.slice(1)));
    renderWithProviders(<MtApp />);
    expect(
      await screen.findByText("Next: Buntzen Lake passes for Sat Jul 3 go on sale Fri at 7:00 am"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Needs you now/ })).toBeNull();
  });

  it("still shows every watch when one has a time no Date can hold", async () => {
    const state = parsedState();
    // Finite, so the schema takes it, but past the year 275760: Intl's formatters throw on it.
    const odd = makeBuntzenWatch({
      id: "0dd0dd0dd0dd0dd0",
      check: { state: "waiting_release", opens_at: 1e13 },
      options: [],
    });
    serveState(stateWith([odd, ...state.watches.slice(1)]));
    renderWithProviders(<MtApp />);
    const heading = await screen.findByRole("heading", { level: 1, name: "Your watches" });
    expect(heading.nextElementSibling).toHaveTextContent("4 watched");
    expect(screen.getByText("Next: Buntzen Lake passes for Sat Jul 3 go on sale Fri at 7:00 am")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Tsawwassen → Swartz Bay" })).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).toBeNull();
  });

  it("doesn't say what's next while something needs the user", async () => {
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    await screen.findByRole("heading", { name: /^Needs you now/ });
    expect(screen.queryByText(/^Next:/)).toBeNull();
  });

  it("folds away what's paused and what has ended, with a count", async () => {
    const soldOut = [makeBuntzenWatch().options[1] ?? fail()]; // nothing open
    const paused = makeBuntzenWatch({
      id: "5a5a5a5a5a5a5a5a",
      enabled: false,
      check: { state: "paused" },
      options: soldOut,
    });
    const over = makeBuntzenWatch({
      id: "e1e1e1e1e1e1e1e1",
      spec: { date: "2027-06-29" },
      check: { state: "over" },
      options: soldOut,
    });
    serveState(stateWith([paused, over]));
    const { user } = renderWithProviders(<MtApp />);
    const fold = await screen.findByRole("button", { name: "Paused 1" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: "Ended 1" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    await user.click(fold);
    expect(await screen.findByRole("button", { name: "Resume" })).toBeInTheDocument();
  });

  it("unfolds Paused when a watch is paused, so it doesn't vanish", async () => {
    const open = makeBuntzenWatch({ options: [{ ...(makeBuntzenWatch().options[1] ?? fail()) }] });
    const paused = makeBuntzenWatch({ enabled: false, check: { state: "paused" }, options: open.options });
    const { requests } = serveState(stateWith([open]), (r, state) =>
      r.method === "PATCH" ? stateWith([paused], { capacity: state.capacity }) : undefined,
    );
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByRole("button", { name: "Resume" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Paused 1" })).toHaveAttribute("aria-expanded", "true");
    expect(requests.find((r) => r.method === "PATCH")).toMatchObject({
      url: `/api/watches/${open.id}`,
      body: { enabled: false },
    });
  });

  it("offers one large button per provider when nothing is watched, straight to its form", async () => {
    serveState(stateWith([]));
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByText("Nothing watched yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Watch a ferry/ })).toHaveTextContent(
      "Can hold a sailing in a cart for you",
    );
    await user.click(screen.getByRole("button", { name: /Watch Buntzen Lake/ }));
    expect(await screen.findByRole("dialog", { name: "Watch Buntzen Lake" })).toBeInTheDocument();
    expect(location.pathname).toBe("/new/buntzen");
  });

  it("disables New watch at every limit, and says why", async () => {
    const state = parsedState();
    serveState({
      ...state,
      capacity: { ...state.capacity, can_add: false, reason: "You're watching as many as you can" },
    });
    const { user } = renderWithProviders(<MtApp />);
    const button = await screen.findByRole("button", { name: "New watch" });
    expect(button).toBeDisabled();
    await user.hover(button.parentElement ?? button);
    await waitFor(() => expect(screen.getAllByText("You're watching as many as you can").length).toBeGreaterThan(0));
  });

  it("on a phone, leaves room below the last card for the floating New watch, above the home indicator", async () => {
    serveState(parsedState());
    renderWithProviders(<MtApp />); // jsdom matches no media query: the phone layout
    const heading = await screen.findByRole("heading", { level: 1, name: "Your watches" });
    const floating = screen.getByRole("button", { name: "New watch" }).parentElement ?? fail();
    expect(floating).toHaveClass("fixed", "bottom-[max(1rem,env(safe-area-inset-bottom))]");
    // The same offset, plus the button's 2.75rem and a gap: scrolled to the end, no card's actions are under it.
    const list = heading.parentElement?.parentElement?.parentElement ?? fail();
    expect(list).toContainElement(floating);
    expect(list).toHaveClass("pb-[calc(max(1rem,env(safe-area-inset-bottom))+4rem)]");
  });

  it("says when it's quiet now", async () => {
    const state = parsedState();
    serveState({ ...state, quiet_hours: { start: "22:00", end: "05:00", now: true } });
    renderWithProviders(<MtApp />);
    const heading = await screen.findByRole("heading", { level: 1, name: "Your watches" });
    expect(heading.nextElementSibling).toHaveTextContent("4 watched · quiet until 5:00 am PT");
  });
});
