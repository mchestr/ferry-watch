import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeSettings } from "@/test/fixtures";
import { FakeWebSocket } from "@/test/fake-websocket";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { MtApp } from "./MtApp";
import type { MyState } from "./schema";

const TRIP_ID = "3f9c0a1b2c3d4e5f";

function myState(overrides: Partial<MyState> = {}): MyState {
  return {
    me: { id: "u1", username: "ann", display: "Ann", is_admin: false, paused: false },
    limits: { maxTrips: { value: 3, source: "default" } },
    usage: {},
    trips: [
      {
        trip: { ...makeSettings(), id: TRIP_ID },
        rev: 1,
        paused_reason: null,
        hold: { reason: null, can_request: true, requested_at: null, requested_until: null },
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
      },
    ],
    capacity: { can_add: true, reason: null },
    quiet_hours: { start: "23:00", end: "06:00", now: false },
    devices: 0,
    events: [],
    links: { kofi: null },
    push: { key: null },
    ...overrides,
  };
}

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
    localStorage.setItem("fw.token", "t-1");
    const { requests } = mockFetch((r) =>
      r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] }),
    );
    renderWithProviders(<Fresh />);
    expect(await screen.findByRole("heading", { name: "Your trips" })).toBeInTheDocument();
    expect(requests.map((r) => r.headers.get("Authorization"))).toEqual(requests.map(() => "Bearer t-1"));
    expect(localStorage.getItem("fw.token")).toBe("t-1");
  });

  it("lands on the trips straight after a first sign-in", async () => {
    const flow = { state: "S", nonce: "N", verifier: "V", returnTo: "/" };
    sessionStorage.setItem("fw.signin", JSON.stringify(flow));
    history.replaceState(null, "", "/auth/callback#code=c1&state=S");
    const { requests } = mockFetch((r) => {
      if (r.url === "/auth/session") return jsonResponse({ token: "t-2" });
      if (r.url === "/api/state") return jsonResponse(myState());
      return jsonResponse({ terminals: [] });
    });
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("heading", { name: "Your trips" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(location.pathname).toBe("/");
    expect(requests.find((r) => r.url === "/api/state")?.headers.get("Authorization")).toBe("Bearer t-2");
  });
});

describe("signed in", () => {
  beforeEach(() => {
    localStorage.setItem("fw.token", "t-1");
  });

  it("shows the user's trips with a held cart, sending the token", async () => {
    const { requests } = mockFetch((r) =>
      r.url === "/api/state" ? jsonResponse(myState()) : jsonResponse({ terminals: [] }),
    );
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("9:00 am is held for you (Saver, $93.45)")).toBeInTheDocument();
    expect(screen.getByText("Holding a cart")).toBeInTheDocument();
    expect(requests.find((r) => r.url === "/api/state")?.headers.get("Authorization")).toBe("Bearer t-1");
  });

  it("opens the checkout only when the user picks this device", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, pathname: `/c/${TRIP_ID}`, search: "" });
    const url = `https://ferry-checkout.chestr.dev/t/${TRIP_ID}/g/2/#ticket`;
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
          client_id: "ferry-watch",
          redirect_uri: "https://ferry.chestr.dev/auth/callback",
          scopes: "openid",
        });
      }
      return jsonResponse({ terminals: [] });
    });
    renderWithProviders(<MtApp />);
    await waitFor(() => expect(assign).toHaveBeenCalled());
    const target = new URL(String(assign.mock.calls[0]?.[0]));
    expect(target.origin).toBe("https://auth.chestr.dev");
    const flow = JSON.parse(sessionStorage.getItem("fw.signin") ?? "{}") as { returnTo?: string };
    expect(flow.returnTo).toBe(`/c/${TRIP_ID}`); // a cart alert still lands on its checkout
  });

  it("shows the signed-out screen when the session was revoked", async () => {
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse({ reason: "revoked" }, 401) : jsonResponse({})));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(localStorage.getItem("fw.token")).toBeNull();
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

  function unheldState(hold: Partial<MyState["trips"][number]["hold"]> = {}, checkedAt: number | null = null) {
    const base = myState().trips[0];
    if (!base) throw new Error("no trip");
    return myState({
      trips: [
        {
          ...base,
          booking_url: BOOK,
          hold: { reason: "quiet_hours", can_request: true, requested_at: null, requested_until: null, ...hold },
          browser: { ...base.browser, cart: null, sailings: [SAILING], checked_at: checkedAt },
        },
      ],
    });
  }

  beforeEach(() => {
    localStorage.setItem("fw.token", "t-1");
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
    expect(screen.getByText("We don't hold carts overnight. Hold it for me, or book it yourself.")).toBeInTheDocument();
    expect(screen.getByText("Bookable now")).toBeInTheDocument();
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

  it("is where a Bookable alert lands", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(unheldState()) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("9:00 am is bookable, not held")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Hold it for me/ })).toBeInTheDocument();
    expect(screen.queryByText("Nothing is held right now")).toBeNull();
  });
});

describe("admin", () => {
  beforeEach(() => {
    localStorage.setItem("fw.token", "t-1");
    history.replaceState(null, "", "/admin");
  });

  it("opens the admin pages for an admin", async () => {
    const me = { id: "u1", username: "ann", display: "Ann", is_admin: true, paused: false };
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
    expect(await screen.findByRole("heading", { name: "Your trips" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Admin" })).toBeNull();
    expect(requests.some((r) => r.url.startsWith("/api/admin"))).toBe(false);
  });
});
