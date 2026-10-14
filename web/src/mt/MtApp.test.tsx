import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeSettings } from "@/test/fixtures";
import { FakeWebSocket } from "@/test/fake-websocket";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { MtApp } from "./MtApp";
import type { MyState } from "./schema";

const TRIP_ID = "3f9c0a1b2c3d4e5f";

function myState(overrides: Partial<MyState> = {}): MyState {
  return {
    me: { id: "u1", username: "ann", display: "Ann", is_admin: false },
    limits: { maxTrips: { value: 3, source: "default" } },
    usage: {},
    trips: [
      {
        trip: { ...makeSettings(), id: TRIP_ID },
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
      },
    ],
    capacity: { can_add: true, reason: null },
    settings: { quiet_hours: null },
    quiet_hours: null,
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
    expect(statusBadge()).toBe("Held");
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

describe("settings", () => {
  beforeEach(() => {
    localStorage.setItem("fw.token", "t-1");
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
    expect(await screen.findByRole("heading", { name: "Your trips" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your account" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Your account" })).toBeInTheDocument();
    expect(location.pathname).toBe("/settings");
    expect(screen.queryByRole("heading", { name: "Your trips" })).toBeNull();
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
    expect(localStorage.getItem("fw.token")).toBeNull();
    expect(localStorage.getItem("fw.signedOut")).toBe("1");
  });
});

describe("pausing a trip", () => {
  beforeEach(() => {
    localStorage.setItem("fw.token", "t-1");
  });

  /** The one trip, with nothing held, enabled or not. */
  function tripState(enabled: boolean, extra: Partial<MyState["trips"][number]> = {}): MyState {
    const base = myState().trips[0];
    if (!base) throw new Error("no trip");
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
      if (r.url === `/api/trips/${TRIP_ID}` && r.method === "PATCH") {
        enabled = (r.body as { enabled: boolean }).enabled;
      }
      if (r.url === "/api/state" || r.url.startsWith("/api/trips/")) return jsonResponse(tripState(enabled));
      return jsonResponse({ terminals: [] });
    });
    const { user } = renderWithProviders(<MtApp />);
    expect(await screen.findByRole("button", { name: "Check now" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Trip paused")).toBeInTheDocument();
    expect(requests.find((r) => r.method === "PATCH")).toMatchObject({
      url: `/api/trips/${TRIP_ID}`,
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
        const held = myState().trips[0];
        if (!held) throw new Error("no trip");
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

describe("quiet hours", () => {
  beforeEach(() => {
    localStorage.setItem("fw.token", "t-1");
    history.replaceState(null, "", "/settings");
  });

  const NIGHT = { start: "23:00", end: "06:00" };

  /** ferry-web as the contract has it: PATCH /api/me {quiet_hours} answers with the whole state. */
  function api(initial: { start: string; end: string } | null) {
    let quiet = initial;
    const state = () => myState({ settings: { quiet_hours: quiet }, quiet_hours: quiet && { ...quiet, now: false } });
    return mockFetch((r) => {
      if (r.url === "/api/me" && r.method === "PATCH") quiet = (r.body as { quiet_hours: typeof quiet }).quiet_hours;
      if (r.url === "/api/state" || r.url === "/api/me") return jsonResponse(state());
      return jsonResponse({ terminals: [] });
    });
  }

  it("are off unless turned on, and start as 11 pm to 6 am", async () => {
    const { requests } = api(null);
    const { user } = renderWithProviders(<MtApp />);
    const quiet = await screen.findByRole("switch", { name: "Quiet hours (Pacific time)" });
    expect(quiet).not.toBeChecked();
    expect(
      screen.getByText(
        "Your trips aren't checked during them, so nothing is held and you get no alerts. Check now and Hold it for me still work.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Start")).toBeNull();
    await user.click(quiet);
    expect(await screen.findByText("Quiet hours: 11:00 pm to 6:00 am")).toBeInTheDocument();
    expect(requests.find((r) => r.method === "PATCH")).toMatchObject({ url: "/api/me", body: { quiet_hours: NIGHT } });
    expect(quiet).toBeChecked();
    expect(screen.getByLabelText("Start")).toHaveValue("23:00");
    expect(screen.getByLabelText("End")).toHaveValue("06:00");
  });

  it("saves changed times, and turns off as null", async () => {
    const { requests } = api(NIGHT);
    const { user } = renderWithProviders(<MtApp />);
    const start = await screen.findByLabelText("Start");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull(); // nothing changed yet
    fireEvent.change(start, { target: { value: "22:30" } });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Quiet hours: 10:30 pm to 6:00 am")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Quiet hours (Pacific time)" }));
    expect(await screen.findByText("Quiet hours are off")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "PATCH").map((r) => r.body)).toEqual([
      { quiet_hours: { start: "22:30", end: "06:00" } },
      { quiet_hours: null },
    ]);
    expect(screen.queryByLabelText("Start")).toBeNull();
  });

  it("won't save a start and end that are the same", async () => {
    const { requests } = api({ start: "22:00", end: "07:00" });
    const { user } = renderWithProviders(<MtApp />);
    const end = await screen.findByLabelText("End");
    fireEvent.change(end, { target: { value: "22:00" } });
    expect(screen.getByLabelText("Start")).toHaveAccessibleDescription("Start and end can't be the same.");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(requests.some((r) => r.method === "PATCH")).toBe(false);
    // Off and on again: back to the night.
    const quiet = screen.getByRole("switch", { name: "Quiet hours (Pacific time)" });
    await user.click(quiet);
    await waitFor(() => expect(quiet).not.toBeChecked());
    await user.click(quiet);
    await waitFor(() => expect(quiet).toBeChecked());
    expect(requests.filter((r) => r.method === "PATCH").map((r) => r.body)).toEqual([
      { quiet_hours: null },
      { quiet_hours: NIGHT },
    ]);
  });

  it("are off for a server that doesn't send them", async () => {
    const older: Partial<MyState> = myState();
    delete older.settings;
    delete older.quiet_hours;
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(older) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByRole("switch", { name: "Quiet hours (Pacific time)" })).not.toBeChecked();
  });

  it("say when it's quiet now, on the trips", async () => {
    history.replaceState(null, "", "/");
    const base = myState().trips[0];
    if (!base) throw new Error("no trip");
    const state = myState({
      settings: { quiet_hours: NIGHT },
      quiet_hours: { ...NIGHT, now: true },
      trips: [{ ...base, quiet: true, browser: { ...base.browser, phase: "off", mode: "off", cart: null } }],
    });
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByText(/1 of 3 watched · quiet until 6:00 am/)).toBeInTheDocument();
    expect(statusBadge()).toBe("Quiet hours");
  });

  it("say on a quiet trip's checkout page when it's checked again", async () => {
    history.replaceState(null, "", `/c/${TRIP_ID}`);
    const base = myState().trips[0];
    if (!base) throw new Error("no trip");
    const state = myState({
      settings: { quiet_hours: NIGHT },
      quiet_hours: { ...NIGHT, now: true },
      trips: [{ ...base, quiet: true, browser: { ...base.browser, phase: "off", mode: "off", cart: null } }],
    });
    mockFetch((r) => (r.url === "/api/state" ? jsonResponse(state) : jsonResponse({ terminals: [] })));
    renderWithProviders(<MtApp />);
    expect(await screen.findByText("It's your quiet hours, so this trip isn't checked until 6:00 am.")).toBeVisible();
    expect(screen.getByText("Quiet hours")).toBeInTheDocument();
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
    const trip = base.trips[0];
    if (!trip) throw new Error("no trip");
    const starting = {
      ...base,
      trips: [{ ...trip, browser: { ...trip.browser, sailings: [], phase: "starting" as const } }],
    };
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
    localStorage.setItem("fw.token", "t-1");
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
    expect(await screen.findByRole("heading", { name: "Your trips" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Admin" })).toBeNull();
    expect(requests.some((r) => r.url.startsWith("/api/admin"))).toBe(false);
  });
});
