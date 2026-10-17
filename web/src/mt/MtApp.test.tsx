import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { asWatch, BCFERRIES_INFO, makeSettings, tripsOf } from "@/test/fixtures";
import { addDays, todayInPacific } from "@/lib/time";
import { FakeWebSocket } from "@/test/fake-websocket";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import type { QueryClient } from "@tanstack/react-query";
import { MY_STATE_KEY } from "./api";
import { MtApp } from "./MtApp";
import type { CheckoutViewer, MyTrip } from "./schema";
import type { WaitlistState } from "./watch-schema";

const TRIP_ID = "3f9c0a1b2c3d4e5f";

/** The one trip, tomorrow: a held cart. (A trip dated before today is in the dashboard's folded Ended section.) */
function firstTrip(): MyTrip {
  return {
    trip: { ...makeSettings({ date: addDays(todayInPacific(), 1) ?? "" }), id: TRIP_ID },
    rev: 1,
    paused_reason: null,
    quiet: false,
    hold: { reason: null, can_request: true, holdable: [], requested_at: null, requested_until: null },
    booking_url: null,
    browser: {
      phase: "holding",
      mode: "warm",
      queue_position: null,
      next_check_at: null,
      last_check_at: null,
      checked_at: null,
      next_due: null,
      effective_interval: 120,
      heartbeat_at: null,
      sailings: [],
      error_class: null,
      consecutive_errors: 0,
      cart: {
        id: "cart1",
        status: "ready",
        depart: "09:00",
        fare: "Saver",
        price: "$93.45",
        ready_at: null,
        unclaimed_deadline: Date.now() / 1000 + 1500,
        session_deadline: null,
        hard_deadline: Date.now() / 1000 + 3000,
        payment_in_progress: false,
      },
      viewer_connected: false,
    },
  };
}

/** The user's state, with these BC Ferries trips (the one above unless told). */
function myState({
  trips = [firstTrip()],
  ...overrides
}: Partial<Omit<WaitlistState, "watches">> & { trips?: MyTrip[] } = {}): WaitlistState {
  return {
    me: { id: "u1", username: "ann", display: "Ann", is_admin: false },
    limits: { maxTrips: { value: 3, source: "default" } },
    usage: {},
    watches: trips.map(asWatch),
    providers: [BCFERRIES_INFO],
    capacity: {
      can_add: true,
      reason: null,
      providers: { bcferries: { can_add: true, reason: null, limit: 3, count: trips.length } },
    },
    settings: {
      quiet_hours: { start: "22:00", end: "05:00" },
      checkout_viewer: "classic",
      stream_viewer_offered: false,
    },
    quiet_hours: { start: "22:00", end: "05:00", now: false },
    devices: 0,
    events: [],
    links: { kofi: null },
    push: { key: null },
    ...overrides,
  };
}

/** The status badge beside the trip card's title. */
const statusBadge = () =>
  screen.getByRole("heading", { level: 2, name: /→/ }).parentElement?.nextElementSibling?.textContent;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("signed out", () => {
  it("shows one button and starts nothing on its own", () => {
    const { fetchMock } = mockFetch(() => jsonResponse({}));
    renderWithProviders(<MtApp />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("a page load", () => {
  it("sends the token with the very first requests", async () => {
    vi.resetModules(); // a fresh page: nothing wired up yet by an earlier test
    const { MtApp: Fresh } = await import("./MtApp");
    localStorage.setItem("wl.token", "t-1");
    const { requests } = mockFetch((r) =>
      r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] }),
    );
    renderWithProviders(<Fresh />);
    expect(await screen.findByRole("heading", { name: "Your watches" })).toBeInTheDocument();
    expect(requests.map((r) => r.headers.get("Authorization"))).toEqual(requests.map(() => "Bearer t-1"));
    expect(localStorage.getItem("wl.token")).toBe("t-1");
  });

  it("lands on the trips straight after a first sign-in", async () => {
    const flow = { state: "S", nonce: "N", verifier: "V", returnTo: "/" };
    sessionStorage.setItem("wl.signin", JSON.stringify(flow));
    history.replaceState(null, "", "/auth/callback#code=c1&state=S");
    const { requests } = mockFetch((r) => {
      if (r.url === "/auth/session") return jsonResponse({ token: "t-2" });
      if (r.url === "/api/state") return jsonResponse(myState());
      return jsonResponse({ terminals: [] });
    });
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Your watches" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(location.pathname).toBe("/");
    expect(requests.find((r) => r.url === "/api/state")?.headers.get("Authorization")).toBe("Bearer t-2");
  });
});

describe("signed in", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  it("shows the user's trips with a held cart, sending the token", async () => {
    const { requests } = mockFetch((r) =>
      r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] }),
    );
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("9:00 am is held for you (Saver, $93.45)")).toBeInTheDocument();
    expect(statusBadge()).toBe("Held");
    expect(requests.find((r) => r.url === "/api/state")?.headers.get("Authorization")).toBe("Bearer t-1");
  });

  it("opens the checkout only when the user picks this device", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, pathname: `/c/${TRIP_ID}`, search: "" });
    const url = `https://waitlist-checkout.chestr.dev/t/${TRIP_ID}/g/2/#ticket`;
    const { requests } = mockFetch((r) => {
      if (r.url === "/api/state") return jsonResponse(myState());
      if (r.url.endsWith("/vnc")) return jsonResponse({ url, ttl: 60 });
      return jsonResponse({ terminals: [] });
    });
    const { user } = renderWithProviders(<MtApp />);
    const button = await screen.findByRole("button", { name: /Check out on this device/ });
    expect(requests.some((r) => r.url.endsWith("/vnc"))).toBe(false); // nothing automatic
    await user.click(button);
    await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
  });

  it("signs in again quietly when the session needs re-verifying", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, pathname: `/c/${TRIP_ID}`, search: "" });
    mockFetch((r) => {
      if (r.url === "/api/state") return jsonResponse({ reason: "reverify" }, 401);
      if (r.url === "/auth/config") {
        return jsonResponse({
          authorization_endpoint: "https://auth.chestr.dev/api/oidc/authorization",
          client_id: "waitlist",
          redirect_uri: "https://waitlist.chestr.dev/auth/callback",
          scopes: "openid",
        });
      }
      return jsonResponse({ terminals: [] });
    });
    renderWithProviders(<MtApp />);
    await waitFor(() => expect(assign).toHaveBeenCalled());
    const target = new URL(String(assign.mock.calls[0]?.[0]));
    expect(target.origin).toBe("https://auth.chestr.dev");
    const flow = JSON.parse(sessionStorage.getItem("wl.signin") ?? "{}") as { returnTo?: string };
    expect(flow.returnTo).toBe(`/c/${TRIP_ID}`); // a cart alert still lands on its checkout
  });

  it("shows the signed-out screen when the session was revoked", async () => {
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse({ reason: "revoked" }, 401) : jsonResponse({})));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(localStorage.getItem("wl.token")).toBeNull();
  });
});

describe("settings", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  const api = () =>
    mockFetch((r) => {
      if (r.url === "/api/state") return jsonResponse(myState());
      if (r.url.startsWith("/auth/")) return jsonResponse({});
      return jsonResponse({ terminals: [] });
    });

  /** The card under a heading. */
  const card = (heading: HTMLElement): HTMLElement => {
    const element = heading.closest("div");
    if (!element) throw new Error("not in a card");
    return element;
  };

  it("has its own page, opened from the header, not beside the trips", async () => {
    api();
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Your watches" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your account" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Your account" })).toBeInTheDocument();
    expect(location.pathname).toBe("/settings");
    expect(screen.queryByRole("heading", { name: "Your watches" })).toBeNull();
  });

  it.each([
    ["the settings page", (account: HTMLElement) => within(account).getByRole("button", { name: "Sign out" })],
    ["the header", () => within(screen.getByRole("banner")).getByRole("button", { name: "Sign out" })],
  ])("signs out this device only, from %s", async (_where, button) => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, pathname: "/settings", search: "" });
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    const account = card(await screen.findByRole("heading", { name: "Your account" }));
    await user.click(button(account));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
    expect(requests.filter((r) => r.url.startsWith("/auth/")).map((r) => r.url)).toEqual(["/auth/logout"]);
    expect(localStorage.getItem("wl.token")).toBeNull();
    expect(localStorage.getItem("wl.signedOut")).toBe("1");
  });
});

describe("pausing a trip", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  /** The one trip, with nothing held, enabled or not. */
  function tripState(enabled: boolean, extra: Partial<MyTrip> = {}): WaitlistState {
    const base = firstTrip();
    return myState({
      trips: [
        {
          ...base,
          trip: { ...base.trip, enabled },
          hold: { ...base.hold, reason: enabled ? null : "paused", can_request: enabled },
          browser: { ...base.browser, phase: enabled ? "watching" : "off", cart: null },
          ...extra,
        },
      ],
    });
  }

  it("pauses and resumes one trip, and doesn't check a paused one", async () => {
    let enabled = true;
    const { requests } = mockFetch((r) => {
      if (r.url === `/api/watches/${TRIP_ID}` && r.method === "PATCH") {
        enabled = (r.body as { enabled: boolean }).enabled;
      }
      if (r.url === "/api/state" || r.url.startsWith("/api/trips/") || r.url.startsWith("/api/watches/"))
        return jsonResponse(tripState(enabled));
      return jsonResponse({ terminals: [] });
    });
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: "Check now" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Trip paused")).toBeInTheDocument();
    expect(requests.find((r) => r.method === "PATCH")).toMatchObject({
      url: `/api/watches/${TRIP_ID}`,
      body: { enabled: false },
    });
    expect(statusBadge()).toBe("Paused");
    expect(screen.getByRole("button", { name: "Check now" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText("Trip resumed")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "PATCH").map((r) => r.body)).toEqual([
      { enabled: false },
      { enabled: true },
    ]);
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check now" })).toBeEnabled();
  });

  it("says a held cart stays held", async () => {
    mockFetch((r) => {
      if (r.method === "PATCH") {
        const held = firstTrip();
        return jsonResponse(myState({ trips: [{ ...held, trip: { ...held.trip, enabled: false } }] }));
      }
      return r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] });
    });
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Trip paused. Your cart is still held.")).toBeInTheDocument();
    expect(screen.getByText("9:00 am is held for you (Saver, $93.45)")).toBeInTheDocument();
  });

  it("can't be paused while it asks whether you booked it", async () => {
    mockFetch((r) =>
      r.url === "/api/state"
        ? jsonResponse(tripState(true, { paused_reason: "after_checkout" }))
        : jsonResponse({ terminals: [] }),
    );
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: "Pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Check now" })).toBeDisabled();
  });

  it("is per trip: settings has no pause", async () => {
    history.replaceState(null, "", "/settings");
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Your account" })).toBeInTheDocument();
    expect(screen.queryByText(/Pause all/)).toBeNull();
    expect(screen.queryByRole("switch", { name: /pause/i })).toBeNull();
  });
});

describe("releasing a held cart", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  /** waitlist-web only passes a release on: the cart is still held when it answers. */
  function api() {
    return mockFetch((r) =>
      r.url === "/api/state" || r.url.startsWith("/api/trips/") || r.url.startsWith("/api/watches/")
        ? jsonResponse(myState())
        : jsonResponse({ terminals: [] }),
    );
  }

  /** The browser lets the cart go (or holds another one). */
  function cartIs(queryClient: QueryClient, cart: MyTrip["browser"]["cart"]) {
    const base = firstTrip();
    queryClient.setQueryData(MY_STATE_KEY, myState({ trips: [{ ...base, browser: { ...base.browser, cart } }] }));
  }

  it("says it's releasing on the card until the cart is let go", async () => {
    const { requests } = api();
    const { user, queryClient } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Release" }));
    expect(await screen.findByText("Releasing the cart")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Releasing…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Open checkout/ })).toBeDisabled();
    expect(requests.filter((r) => r.url.endsWith("/cart/release")).map((r) => r.body)).toEqual([{ cart_id: "cart1" }]);

    const held = firstTrip().browser.cart;
    if (!held) throw new Error("no cart");
    cartIs(queryClient, { ...held, id: "cart2" }); // a new cart can be released again
    expect(await screen.findByRole("button", { name: "Release" })).toBeEnabled();
    cartIs(queryClient, null);
    await waitFor(() => expect(screen.queryByRole("button", { name: /Releas/ })).toBeNull());
  });

  it("stays on the checkout page, and the card says so too", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Release it, I'll book it myself" }));
    expect(await screen.findByText("Releasing the cart")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Releasing…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Check out on this device/ })).toBeDisabled();
    expect(location.pathname).toBe(`/c/${TRIP_ID}`);

    await user.click(screen.getByRole("button", { name: "Your watches" }));
    expect(await screen.findByRole("heading", { name: "Your watches" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Releasing…" })).toBeDisabled();
  });
});

describe("removing a trip", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  type Cart = MyTrip["browser"]["cart"];

  /** waitlist-web archives the trip at once (waitlist-control then lets any cart go); a second DELETE is a 404. */
  function api(cart?: Cart) {
    const base = firstTrip();
    let state = cart === undefined ? myState() : myState({ trips: [{ ...base, browser: { ...base.browser, cart } }] });
    return mockFetch((r) => {
      if (r.url === `/api/watches/${TRIP_ID}` && r.method === "DELETE") {
        if (state.watches.length === 0) return jsonResponse({ error: "No such trip" }, 404);
        state = myState({ trips: [] });
      }
      if (r.url === "/api/state" || r.url.startsWith("/api/trips/") || r.url.startsWith("/api/watches/"))
        return jsonResponse(state);
      return jsonResponse({ terminals: [] });
    });
  }

  const removals = (requests: { method: string; url: string }[]) =>
    requests.filter((r) => r.method === "DELETE" && r.url === `/api/watches/${TRIP_ID}`).length;

  const held = firstTrip().browser.cart;

  it.each<[string, Cart, string]>([
    ["a held cart", held, "Removing it releases the 9:00 am sailing in your cart."],
    [
      "a cart being added",
      held && { ...held, status: "adding" },
      "Removing it stops adding the 9:00 am sailing to your cart.",
    ],
    ["nothing held", null, "You'll stop getting alerts for it."],
  ])("asks first, from the card, and says what happens to %s", async (_, cart, consequence) => {
    const { requests } = api(cart);
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove this trip?" });
    expect(dialog).toHaveTextContent(consequence);
    expect(removals(requests)).toBe(0);

    await user.click(within(dialog).getByRole("button", { name: "Remove trip" }));
    expect(await screen.findByText("Trip removed")).toBeInTheDocument();
    expect(screen.getByText("Nothing watched yet")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(removals(requests)).toBe(1);
  });

  it("keeps the trip on cancel", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(screen.getByText("9:00 am is held for you (Saver, $93.45)")).toBeInTheDocument();
    expect(removals(requests)).toBe(0);
  });

  it("says so when it was already removed somewhere else", async () => {
    const { requests } = api();
    const { user, queryClient } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Remove trip" }));
    expect(await screen.findByText("Nothing watched yet")).toBeInTheDocument();
    act(() => {
      queryClient.setQueryData(MY_STATE_KEY, myState()); // a stale view, from before the removal
    });
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Remove trip" }));
    expect(await screen.findByText("That trip was already removed.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(await screen.findByText("Nothing watched yet")).toBeInTheDocument(); // fetched afresh
    expect(removals(requests)).toBe(2);
  });

  it("still works from the trip form", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const sheet = await screen.findByRole("dialog", { name: "Edit trip" });
    await user.click(within(sheet).getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove this trip?" });
    expect(dialog).toHaveTextContent("Removing it releases the 9:00 am sailing in your cart.");
    await user.click(within(dialog).getByRole("button", { name: "Remove trip" }));
    expect(await screen.findByText("Trip removed")).toBeInTheDocument();
    expect(await screen.findByText("Nothing watched yet")).toBeInTheDocument();
    expect(removals(requests)).toBe(1);
  });
});

describe("did you book it?", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  /** The trip after a viewed checkout, paused until it's answered. */
  function asking(): WaitlistState {
    const base = firstTrip();
    return myState({
      trips: [
        {
          ...base,
          paused_reason: "after_checkout",
          hold: { ...base.hold, reason: "paused", can_request: false },
          browser: { ...base.browser, phase: "off", mode: "off", cart: null },
        },
      ],
    });
  }

  /** waitlist-web as the contract has it: yes and "no, cancel" archive the trip; "no" resumes it. */
  function api() {
    let state = asking();
    return mockFetch((r) => {
      if (r.url === `/api/trips/${TRIP_ID}/booked`) {
        const answer = r.body as { booked: boolean; stop?: boolean };
        const trip = tripsOf(state)[0];
        state =
          answer.booked || answer.stop || !trip
            ? myState({ trips: [] })
            : myState({ trips: [{ ...trip, paused_reason: null, hold: { ...trip.hold, reason: null } }] });
      }
      if (r.url === "/api/state" || r.url.startsWith("/api/trips/") || r.url.startsWith("/api/watches/"))
        return jsonResponse(state);
      return jsonResponse({ terminals: [] });
    });
  }

  const booked = (requests: { url: string; body: unknown }[]) =>
    requests.filter((r) => r.url === `/api/trips/${TRIP_ID}/booked`).map((r) => r.body);

  it("asks on the trip card, with three answers", async () => {
    api();
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("Did you book it?")).toBeInTheDocument();
    expect(statusBadge()).toBe("Paused");
    expect(screen.getByRole("button", { name: "Yes, booked" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "No, keep watching" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "No, cancel" })).toBeInTheDocument();
  });

  it("archives the trip on yes", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Yes, booked" }));
    expect(await screen.findByText("Nothing watched yet")).toBeInTheDocument();
    expect(booked(requests)).toEqual([{ booked: true }]);
  });

  it("keeps watching on no, with a reason or without", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "No, keep watching" }));
    await user.click(screen.getByRole("button", { name: "Checkout broke" }));
    await waitFor(() => expect(screen.queryByText("Did you book it?")).toBeNull());
    expect(booked(requests)).toEqual([{ booked: false, reason: "checkout_broke" }]);
    expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
  });

  it("asks afresh after the next checkout", async () => {
    api();
    const { user, queryClient } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "No, keep watching" }));
    await user.click(screen.getByRole("button", { name: "Checkout broke" }));
    await waitFor(() => expect(screen.queryByText("Did you book it?")).toBeNull());
    act(() => {
      queryClient.setQueryData(MY_STATE_KEY, asking()); // held and checked out again
    });
    expect(await screen.findByRole("button", { name: "Yes, booked" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Checkout broke" })).toBeNull();
  });

  it("can skip the reason", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "No, keep watching" }));
    await user.click(screen.getByRole("button", { name: "Skip" }));
    await waitFor(() => expect(screen.queryByText("Did you book it?")).toBeNull());
    expect(booked(requests)).toEqual([{ booked: false }]);
  });

  it("stops watching on no, cancel", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "No, cancel" }));
    expect(await screen.findByText("Stopped watching")).toBeInTheDocument();
    expect(screen.getByText("Nothing watched yet")).toBeInTheDocument();
    expect(booked(requests)).toEqual([{ booked: false, stop: true }]);
  });

  it.each([
    ["No, cancel", { booked: false, stop: true }],
    ["Yes, booked", { booked: true }],
  ])("asks on the trip's checkout page too, and goes back to the trips after %s", async (button, body) => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    const { requests } = api();
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByText("Did you book it?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: button }));
    expect(await screen.findByRole("heading", { name: "Your watches" })).toBeInTheDocument();
    expect(location.pathname).toBe("/");
    expect(booked(requests)).toEqual([body]);
  });

  it("stays on the checkout page after no, keep watching", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    api();
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "No, keep watching" }));
    await user.click(screen.getByRole("button", { name: "Skip" }));
    expect(await screen.findByText("Nothing is held right now")).toBeInTheDocument();
    expect(location.pathname).toBe(`/c/${TRIP_ID}`);
  });
});

describe("quiet hours", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
    history.replaceState(null, "", "/settings");
  });

  const DEFAULT = { start: "22:00", end: "05:00" }; // everyone's, until they change them
  const group = () => screen.findByRole("group", { name: "Quiet hours (Pacific time)" });

  /** waitlist-web as the contract has it: PATCH /api/me {quiet_hours} answers with the whole state. */
  function api(initial: { start: string; end: string }) {
    let quiet = initial;
    const state = () =>
      myState({ settings: { ...myState().settings, quiet_hours: quiet }, quiet_hours: { ...quiet, now: false } });
    return mockFetch((r) => {
      if (r.url === "/api/me" && r.method === "PATCH") quiet = (r.body as { quiet_hours: typeof quiet }).quiet_hours;
      if (r.url === "/api/state" || r.url === "/api/me") return jsonResponse(state());
      return jsonResponse({ terminals: [] });
    });
  }

  it("are always on: 10 pm to 5 am unless changed, with no switch", async () => {
    api(DEFAULT);
    renderWithProviders(<MtApp />);
    const quiet = await group();
    expect(within(quiet).getByLabelText("Start")).toHaveValue("22:00");
    expect(within(quiet).getByLabelText("End")).toHaveValue("05:00");
    expect(
      within(quiet).getByText(
        "Your trips aren't checked during them, so nothing is held and you get no alerts. Check now and Hold it for me still work.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull(); // nothing changed yet
  });

  it("save changed times", async () => {
    const { requests } = api(DEFAULT);
    const { user } = renderWithProviders(<MtApp />);
    const quiet = await group();
    fireEvent.change(within(quiet).getByLabelText("Start"), { target: { value: "22:30" } });
    fireEvent.change(within(quiet).getByLabelText("End"), { target: { value: "06:00" } });
    await user.click(within(quiet).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Quiet hours: 10:30 pm to 6:00 am")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "PATCH")).toMatchObject([
      { url: "/api/me", body: { quiet_hours: { start: "22:30", end: "06:00" } } },
    ]);
    await waitFor(() => expect(within(quiet).queryByRole("button", { name: "Save" })).toBeNull()); // saved
    expect(within(quiet).getByLabelText("Start")).toHaveValue("22:30");
  });

  it("hide Save again once the saved times are back", async () => {
    api(DEFAULT);
    renderWithProviders(<MtApp />);
    const start = within(await group()).getByLabelText("Start");
    fireEvent.change(start, { target: { value: "23:00" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    fireEvent.change(start, { target: { value: "22:00" } });
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("won't save a start and end that are the same, or a blank one", async () => {
    const { requests } = api({ start: "22:00", end: "07:00" });
    renderWithProviders(<MtApp />);
    const quiet = await group();
    const start = within(quiet).getByLabelText("Start");
    const end = within(quiet).getByLabelText("End");
    fireEvent.change(end, { target: { value: "22:00" } });
    expect(start).toHaveAccessibleDescription("Start and end can't be the same.");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(end, { target: { value: "" } });
    expect(start).toHaveAccessibleDescription("Enter a start and an end.");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.submit(end);
    expect(requests.some((r) => r.method === "PATCH")).toBe(false);
  });

  it("are the default for a server that doesn't send them", async () => {
    const older: Partial<WaitlistState> = myState();
    delete older.settings;
    delete older.quiet_hours;
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(older) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    const quiet = await group();
    expect(within(quiet).getByLabelText("Start")).toHaveValue("22:00");
    expect(within(quiet).getByLabelText("End")).toHaveValue("05:00");
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("say when it's quiet now, on the trips", async () => {
    history.replaceState(null, "", "/");
    const base = firstTrip();
    const state = myState({
      settings: { ...myState().settings, quiet_hours: DEFAULT },
      quiet_hours: { ...DEFAULT, now: true },
      trips: [{ ...base, quiet: true, browser: { ...base.browser, phase: "off", mode: "off", cart: null } }],
    });
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    const heading = await screen.findByRole("heading", { name: "Your watches" });
    expect(heading.nextElementSibling).toHaveTextContent("1 watched · quiet until 5:00 am PT");
    expect(statusBadge()).toBe("Quiet hours");
  });

  it("say on a quiet trip's checkout page when it's checked again", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    const base = firstTrip();
    const state = myState({
      settings: { ...myState().settings, quiet_hours: DEFAULT },
      quiet_hours: { ...DEFAULT, now: true },
      trips: [{ ...base, quiet: true, browser: { ...base.browser, phase: "off", mode: "off", cart: null } }],
    });
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(
      await screen.findByText("It's your quiet hours, so this trip isn't checked until 5:00 am PT."),
    ).toBeVisible();
    expect(screen.getByText("Quiet hours")).toBeInTheDocument();
  });
});

describe("the new checkout viewer (beta)", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
    history.replaceState(null, "", "/settings");
  });

  const NAME = "New checkout viewer (beta)";
  const offered = (viewer: CheckoutViewer = "classic") =>
    myState({ settings: { ...myState().settings, checkout_viewer: viewer, stream_viewer_offered: true } });
  const beta = () => screen.findByRole("switch", { name: NAME });

  /** waitlist-web as the contract has it: PATCH /api/me {checkout_viewer} answers with the whole state, or a 403. */
  function api(initial: WaitlistState, { refuse = false } = {}) {
    let state = initial;
    return mockFetch((r) => {
      if (r.url === "/api/me" && r.method === "PATCH") {
        if (refuse) return jsonResponse({ error: "The new checkout viewer isn't available to you" }, 403);
        const viewer = (r.body as { checkout_viewer: CheckoutViewer }).checkout_viewer;
        state = { ...state, settings: { ...state.settings, checkout_viewer: viewer } };
      }
      if (r.url === "/api/state" || r.url === "/api/me") return jsonResponse(state);
      return jsonResponse({ terminals: [] });
    });
  }

  it("isn't in settings unless the deployment offers it to the user", async () => {
    api(myState({ settings: { ...myState().settings, checkout_viewer: "stream" } }));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Your account" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: NAME })).toBeNull();
    expect(screen.queryByText(/classic viewer/)).toBeNull();
  });

  it("is off until the user turns it on, and says what it is", async () => {
    api(offered());
    renderWithProviders(<MtApp />);
    const toggle = await beta();
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(
      "Checkout looks and scrolls like a normal page on your phone. If anything goes wrong, the checkout has a Use the classic viewer button.",
    );
  });

  it("saves at once, on and off", async () => {
    const { requests } = api(offered());
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await beta());
    expect(await screen.findByText("Checkouts open in the new viewer")).toBeInTheDocument();
    await waitFor(async () => expect(await beta()).toBeChecked());
    await waitFor(async () => expect(await beta()).toBeEnabled());
    await user.click(await beta());
    expect(await screen.findByText("Checkouts open in the classic viewer")).toBeInTheDocument();
    await waitFor(async () => expect(await beta()).not.toBeChecked());
    expect(requests.filter((r) => r.method === "PATCH")).toMatchObject([
      { url: "/api/me", body: { checkout_viewer: "stream" } },
      { url: "/api/me", body: { checkout_viewer: "classic" } },
    ]);
  });

  it("shows what's saved when the server refuses", async () => {
    api(offered(), { refuse: true });
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await beta());
    expect(await screen.findByText("The new checkout viewer isn't available to you")).toBeInTheDocument();
    await waitFor(async () => expect(await beta()).toBeEnabled());
    expect(await beta()).not.toBeChecked();
  });

  describe("on the checkout page", () => {
    const LINE = "Opens in the new checkout viewer (beta).";

    function open(state: WaitlistState, viewer?: CheckoutViewer) {
      history.replaceState(null, "", `/c/${TRIP_ID}`);
      const assign = vi.fn();
      vi.stubGlobal("location", { ...window.location, assign, pathname: `/c/${TRIP_ID}`, search: "" });
      const url = `https://waitlist-checkout.chestr.dev/t/${TRIP_ID}/g/2/#${viewer === "stream" ? "v=s1&t=" : ""}ticket`;
      mockFetch((r) => {
        if (r.url === "/api/state") return jsonResponse(state);
        if (r.url.endsWith("/vnc")) return jsonResponse({ url, ttl: 60, ...(viewer ? { viewer } : {}) });
        return jsonResponse({ terminals: [] });
      });
      return { assign, url, ...renderWithProviders(<MtApp />) };
    }

    it("says so under the buttons for a user who turned it on", async () => {
      const { assign, url, user } = open(offered("stream"), "stream");
      const button = await screen.findByRole("button", { name: /Check out on this device/ });
      expect(screen.getByText(LINE)).toBeVisible();
      await user.click(button);
      await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
      expect(screen.getByText(LINE)).toBeVisible();
    });

    it("says what the link opens: classic from a browser pod from before the beta", async () => {
      const { assign, url, user } = open(offered("stream"), "classic");
      await user.click(await screen.findByRole("button", { name: /Check out on this device/ }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
      expect(screen.queryByText(LINE)).toBeNull();
    });

    it.each([
      ["a user who didn't turn it on", offered("classic")],
      ["a user it isn't offered to", myState({ settings: { ...myState().settings, checkout_viewer: "stream" } })],
    ])("says nothing for %s, and an older server's link opens as before", async (_who, state) => {
      const { assign, url, user } = open(state);
      await user.click(await screen.findByRole("button", { name: /Check out on this device/ }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
      expect(screen.queryByText(LINE)).toBeNull();
    });
  });
});

describe("a bookable sailing nothing holds", () => {
  const SAILING = {
    depart: "09:00",
    arrive: "10:35",
    vessel: "Queen of Oak Bay",
    status: "available" as const,
    fares: [],
  };
  const BOOK = "https://www.bcferries.com/?sailing=true&departureLocation=NAN";

  function unheldState(hold: Partial<MyTrip["hold"]> = {}, checkedAt: number | null = null) {
    const base = firstTrip();
    return myState({
      trips: [
        {
          ...base,
          booking_url: BOOK,
          hold: {
            reason: "quiet_hours",
            can_request: true,
            holdable: ["09:00"],
            requested_at: null,
            requested_until: null,
            ...hold,
          },
          browser: { ...base.browser, cart: null, sailings: [SAILING], checked_at: checkedAt },
        },
      ],
    });
  }

  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
  });

  it("says why, offers to hold it, and links to bcferries.com", async () => {
    const { requests } = mockFetch((r) => {
      if (r.url === "/api/state") return jsonResponse(unheldState());
      if (r.url.endsWith("/hold")) {
        const now = Date.now() / 1000;
        return jsonResponse(unheldState({ requested_at: now, requested_until: now + 600 }));
      }
      return jsonResponse({ terminals: [] });
    });
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByText("9:00 am is bookable, not held")).toBeInTheDocument();
    expect(screen.getByText("You're in your quiet hours. Hold it for me, or book it yourself.")).toBeInTheDocument();
    expect(statusBadge()).toBe("Bookable");
    expect(screen.getByRole("link", { name: /Book on bcferries.com/ })).toHaveAttribute("href", BOOK);
    await user.click(screen.getByRole("button", { name: /Hold it for me/ }));
    expect(await screen.findByRole("button", { name: /Holding it/ })).toBeDisabled();
    expect(requests.some((r) => r.method === "POST" && r.url === `/api/trips/${TRIP_ID}/hold`)).toBe(true);
  });

  it("offers the button again once a check after the request held nothing", async () => {
    const now = Date.now() / 1000;
    const state = unheldState({ reason: null, requested_at: now - 60, requested_until: now + 540 }, now - 5);
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: /Hold it for me/ })).toBeEnabled();
  });

  it("only links to bcferries.com when a hold can't be asked for", async () => {
    const state = unheldState({ reason: "holding_elsewhere", can_request: false });
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(
      await screen.findByText("You're already holding a cart on another trip. You can still book it yourself."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Hold it for me/ })).toBeNull();
  });

  it("offers only bcferries.com for a sailing that was held once already", async () => {
    const state = unheldState({ reason: "already_held", can_request: false, holdable: [] });
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(
      await screen.findByText("It was held for you once already. You can still book it yourself."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Hold .* for me/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Book on bcferries.com/ })).toHaveAttribute("href", BOOK);
  });

  it("keeps saying it's trying while a new pod starts with no sailings yet", async () => {
    const now = Date.now() / 1000;
    const base = unheldState({ requested_at: now - 20, requested_until: now + 580 });
    const trip = tripsOf(base)[0];
    if (!trip) throw new Error("no trip");
    const starting = myState({
      trips: [{ ...trip, browser: { ...trip.browser, sailings: [], phase: "starting" as const } }],
    });
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(starting) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("Trying to hold it")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Trying to hold it now.");
  });

  it("says when the trip is gone", async () => {
    history.replaceState(null, "", "/c/ffffffffffffffff");
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(unheldState()) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("This trip isn't on your list any more")).toBeInTheDocument();
  });

  it("is where a Bookable alert lands", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(unheldState()) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("9:00 am is bookable, not held")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Hold it for me/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Book on bcferries.com/ })).toHaveAttribute("href", BOOK);
    expect(screen.queryByText("Nothing is held right now")).toBeNull();
  });
});

describe("admin", () => {
  beforeEach(() => {
    localStorage.setItem("wl.token", "t-1");
    history.replaceState(null, "", "/admin");
  });

  it("opens the admin pages for an admin", async () => {
    const me = { id: "u1", username: "ann", display: "Ann", is_admin: true };
    mockFetch((r) => {
      if (r.url === "/api/state") return jsonResponse(myState({ me }));
      if (r.url === "/api/admin/users") return jsonResponse({ users: [], invites: [] });
      if (r.url === "/api/admin/overview") {
        return jsonResponse({ pods: [], queued: 0, holds: [], switches: {}, outcomes_7d: {} });
      }
      return jsonResponse({ terminals: [] });
    });
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Admin" })).toBeInTheDocument();
    expect(await screen.findByText("No browsers running.")).toBeInTheDocument();
  });

  it("shows everyone else their own trips", async () => {
    const { requests } = mockFetch((r) =>
      r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] }),
    );
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Your watches" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Admin" })).toBeNull();
    expect(requests.some((r) => r.url.startsWith("/api/admin"))).toBe(false);
  });
});
