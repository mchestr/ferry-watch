import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "@/test/fake-websocket";
import { FIXTURE_NOW, parsedState, serveState, stateWith, tripsOf, fail } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";
import { MtApp } from "./MtApp";

beforeEach(() => {
  localStorage.setItem("wl.token", "t-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const at = (path: string) => history.replaceState(null, "", path);

describe("a watch's page", () => {
  it("leads a Buntzen watch with what the alert was for: Book on Yodel, full width", async () => {
    at("/w/0f3c9a1b2d4e5f60");
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { level: 1, name: "Morning or afternoon pass" })).toBeInTheDocument();
    expect(
      screen.getByText("The morning pass is open on Yodel for Thu Jul 1. Book it now: passes go fast."),
    ).toBeInTheDocument();
    const links = screen.getAllByRole("link");
    expect(links[0]).toHaveAccessibleName("Book on Yodel");
    expect(links[0]).toHaveAttribute("href", "https://yodelportal.com/buntzen-lake/Half-Day-Pass");
    expect(screen.getByText(/On sale Wed Jun 30 at 7:00 am; checked every 30 s for 20 minutes/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "How booking works" })).toBeInTheDocument();
    expect(screen.getByText("Waitlist never asks for your Yodel password.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back to your watches" })).toBeInTheDocument();
    for (const name of ["Check now", "Pause", "Edit", "Remove"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("goes back to the watches after I booked it", async () => {
    at("/w/0f3c9a1b2d4e5f60");
    const state = parsedState();
    serveState(state, (r) => (r.url.endsWith("/done") ? stateWith(state.watches.slice(1)) : undefined));
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "I booked it" }));
    await waitFor(() => expect(location.pathname).toBe("/"));
    expect(await screen.findByText("Enjoy the lake.")).toBeInTheDocument();
  });

  it("shows a BC Ferries trip's held cart checkout first, then its card", async () => {
    const state = parsedState();
    const trip = tripsOf(state)[0] ?? fail();
    const held = {
      ...trip,
      browser: {
        ...trip.browser,
        cart: {
          id: "cart1",
          status: "ready" as const,
          depart: "09:00",
          fare: "Saver",
          price: "$93.45",
          ready_at: null,
          unclaimed_deadline: FIXTURE_NOW.getTime() / 1000 + 1500,
          session_deadline: null,
          hard_deadline: null,
          payment_in_progress: false,
        },
      },
    };
    at(`/w/${trip.id}`);
    serveState(stateWith([held]));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: /Check out on this device/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: /→/ })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1); // the checkout's
    expect(screen.queryByText("9:00 am is held for you (Saver, $93.45)")).toBeNull(); // said once, above
  });

  it("shows a BC Ferries trip without a cart as its card, whose title heads the page", async () => {
    const trip = tripsOf(parsedState())[0] ?? fail();
    at(`/w/${trip.id}`);
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { level: 1, name: /→/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: /→/ })).toBeNull();
    expect(screen.getByText("Checking")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Check out on this device/ })).toBeNull();
  });

  it("shows an unknown watch as the unknown card", async () => {
    at("/w/c0ffee00c0ffee00");
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    expect(await screen.findByText(/This watch is for bcparks and needs a newer version/)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "A watch this version of Waitlist can't show" }),
    ).toBeInTheDocument();
  });

  it("says when the watch isn't on the list any more, with a way home", async () => {
    at("/w/ffffffffffffffff");
    serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    const card = (await screen.findByText("This watch isn't on your list any more")).parentElement;
    if (!card) throw new Error("no card");
    await user.click(within(card).getByRole("link", { name: "See your other watches" }));
    expect(location.pathname).toBe("/");
  });
});

describe("the checkout page", () => {
  it("sends another provider's watch to its own page", async () => {
    at("/c/0f3c9a1b2d4e5f60");
    serveState(parsedState());
    renderWithProviders(<MtApp />);
    await waitFor(() => expect(location.pathname).toBe("/w/0f3c9a1b2d4e5f60"));
    expect(await screen.findByRole("heading", { level: 1, name: "Morning or afternoon pass" })).toBeInTheDocument();
  });
});
