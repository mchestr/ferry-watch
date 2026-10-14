import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders, type RecordedRequest } from "@/test/render";
import { browserVersions, quantityGiB, type Overview, type People, type Unmatched } from "./admin";
import { AdminPage } from "./AdminPage";

const ME = "a".repeat(20);
const BEA = "b".repeat(20);
const CY = "c".repeat(20);
const DEE = "d".repeat(20);
const TRIP = "3f9c0a1b2c3d4e5f";

function people(): People {
  const limits = { maxTrips: { value: 3, source: "default" }, maxActiveBrowsers: { value: 2, source: "override" } };
  const user = {
    email: null,
    state: "active",
    is_admin: false,
    paused: false,
    trips: 1,
    created_at: Date.now() / 1000 - 86400,
    last_seen_at: Date.now() / 1000 - 300,
    limits,
    sessions: 1,
    devices: 0,
    usage_today: {},
    bootstrap: false,
    emails_seen: [],
  };
  return {
    users: [
      { ...user, id: ME, username: "ann", is_admin: true, bootstrap: true },
      {
        ...user,
        id: BEA,
        username: "bea",
        email: "bea@example.com",
        sessions: 2,
        devices: 1,
        usage_today: { holds: 1, manual_checks: 3, vnc_seconds: 0 },
        emails_seen: ["bea@work.example"],
      },
      { ...user, id: CY, username: "cy", state: "suspended", sessions: 0 },
      { ...user, id: DEE, username: "dee", is_admin: true, bootstrap: true },
    ],
    invites: [
      {
        id: "0123456789abcdef",
        username: "cat",
        email: null,
        reason: "family",
        expires_at: Date.now() / 1000 + 7 * 86400,
      },
    ],
  };
}

function overview(): Overview {
  return {
    pods: [
      { mode: "warm", phase: "watching", count: 2 },
      { mode: "warm", phase: "holding", count: 1 },
      { mode: "short_check", phase: "starting", count: 1 },
    ],
    queued: 3,
    holds: [
      {
        user: BEA,
        trip: TRIP,
        pod: "fw-3f9c0a1b2c3d4e5f-2",
        since: Date.now() / 1000 - 120,
        cart: { status: "ready", depart: "09:00", hard_deadline: Date.now() / 1000 + 25 * 60 },
      },
      { user: DEE, trip: "0a0b0c0d0e0f0102", pod: "fw-0a0b0c0d0e0f0102-1", since: 0, cart: { status: "failed" } },
    ],
    switches: { maintenance: false, pause_carting: true, breaker: "closed" },
    outcomes_7d: { booked: 4, released: 1 },
    fleet: {
      at: Date.now() / 1000 - 90,
      browsers: { max: 7, live: 3 },
      memory: { pod: "1Gi", requested_mb: 3072 },
      queued: 3,
      short_checks: { due: 2, overdue: 1 },
      budget: { checks_per_minute: 20, demand_per_minute: 4.5, stretch: 1 },
      brake: { multiplier: 2, paused: false },
      breaker: "closed",
    },
    control_seen: Date.now() / 1000 - 90,
    attention: [
      { kind: "delivery_failing", trip: null, user: BEA, at: Date.now() / 1000 - 600, detail: null },
      { kind: "release_unverified", trip: TRIP, user: null, at: Date.now() / 1000 - 3600, detail: null },
    ],
    versions: {
      web: { version: __APP_VERSION__ },
      control: { version: "0.6.3", at: Date.now() / 1000 - 90 },
      browsers: [
        { version: "0.6.3", image: "sha256:aaa", pods: 2 },
        { version: null, image: null, pods: 1 },
      ],
    },
  };
}

const unmatched: Unmatched = [
  { issuer: "https://auth.chestr.dev", username: "dan", email: "dan@example.com", last_seen: Date.now() / 1000 },
];

/** The value next to a label in a fleet card. */
const fact = (label: string) => screen.getByText(label, { selector: "dt" }).nextSibling?.textContent;

function api(extra: (r: RecordedRequest) => Response | undefined = () => undefined) {
  return mockFetch((r) => {
    const answer = extra(r);
    if (answer) return answer;
    if (r.url === "/api/admin/users") return jsonResponse(people());
    if (r.url === "/api/admin/overview") return jsonResponse(overview());
    if (r.url === "/api/admin/unmatched") return jsonResponse(unmatched);
    return jsonResponse({ ok: true });
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function row(element: HTMLElement): HTMLElement {
  const li = element.closest("li");
  if (!li) throw new Error("not in a list row");
  return li;
}

describe("fleet", () => {
  it("shows browsers, holds by name with the time left, and the week's outcomes", async () => {
    api();
    renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("3 queued")).toBeInTheDocument();
    const warm = screen.getByRole("row", { name: /Warm/ });
    expect(
      within(warm)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual(["0", "2", "1"]);
    expect(await screen.findByText("bea")).toBeInTheDocument();
    expect(screen.getByText("25 min left")).toBeInTheDocument();
    expect(screen.getByText("Booked").nextSibling?.textContent).toBe("4");
  });

  it("asks why before flipping a switch, and sends the reason", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    const maintenance = await screen.findByRole("switch", { name: "Maintenance" });
    expect(screen.getByRole("switch", { name: "Pause carting" })).toBeChecked();
    await user.click(maintenance);
    const dialog = await screen.findByRole("dialog", { name: "Turn maintenance on?" });
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled(); // no reason yet
    await user.type(within(dialog).getByLabelText("Reason"), "Talos upgrade");
    await user.click(confirm);
    await waitFor(() =>
      expect(requests.find((r) => r.url === "/api/admin/switches")?.body).toEqual({
        switch: "maintenance",
        on: true,
        reason: "Talos upgrade",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("says which version each part runs, on both pages", async () => {
    api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    const line = `Dashboard ${__APP_VERSION__} · Web ${__APP_VERSION__} · Control 0.6.3 · Browsers 0.6.3 ×2, unknown ×1`;
    expect(await screen.findByText(line)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload to update" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "People" }));
    expect(await screen.findByRole("button", { name: "Suspend bea" })).toBeInTheDocument();
    expect(screen.getByText(line)).toBeInTheDocument();
  });

  it("offers a reload when ferry-web is newer than this page", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    api((r) =>
      r.url === "/api/admin/overview"
        ? jsonResponse({ ...overview(), versions: { web: { version: "9.9.9" }, control: null, browsers: [] } })
        : undefined,
    );
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    expect(
      await screen.findByText(`Dashboard ${__APP_VERSION__} · Web 9.9.9 · Control —`, { exact: false }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reload to update" }));
    expect(reload).toHaveBeenCalled();
  });

  it("shows only its own version for a server that doesn't say", async () => {
    const older: Partial<Overview> = overview();
    delete older.versions;
    api((r) => (r.url === "/api/admin/overview" ? jsonResponse(older) : undefined));
    renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("3 queued")).toBeInTheDocument();
    expect(screen.getByText(`Dashboard ${__APP_VERSION__}`)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload to update" })).toBeNull();
  });

  it("reports capacity and budget from ferry-control's last pass", async () => {
    api();
    renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("ferry-control last reported 1 min ago")).toBeInTheDocument();
    expect(fact("Browsers running")).toBe("3 of 7");
    expect(fact("Memory requested")).toBe("3 GiB of 7 GiB");
    expect(fact("Queued")).toBe("3");
    expect(fact("Short checks")).toBe("2 due · 1 overdue");
    expect(fact("Allowed")).toBe("20 checks a minute");
    expect(fact("Asked for")).toBe("4.5 checks a minute");
    expect(fact("Stretch")).toBe("None");
    expect(fact("Brake")).toBe("×2 slower");
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close the breaker" })).toBeNull();
  });

  it("warns when ferry-control is late or has never reported", async () => {
    let seen: number | null = Date.now() / 1000 - 5 * 60;
    api((r) =>
      r.url === "/api/admin/overview"
        ? jsonResponse({ ...overview(), control_seen: seen, fleet: seen === null ? null : overview().fleet })
        : undefined,
    );
    const { queryClient } = renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("ferry-control last reported 5 min ago. Is it running?")).toBeInTheDocument();
    seen = null;
    await queryClient.invalidateQueries({ queryKey: ["admin"] });
    expect(await screen.findByText("ferry-control hasn't reported. Is it running?")).toBeInTheDocument();
    expect(fact("Last report")).toBe("None yet");
  });

  it("closes an open breaker, with a reason", async () => {
    const { requests } = api((r) =>
      r.url === "/api/admin/overview"
        ? jsonResponse({ ...overview(), switches: { maintenance: false, pause_carting: false, breaker: "open" } })
        : undefined,
    );
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("Open")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close the breaker" }));
    const dialog = await screen.findByRole("dialog", { name: "Close the breaker?" });
    await user.type(within(dialog).getByLabelText("Reason"), "new image rolled out");
    await user.click(within(dialog).getByRole("button", { name: "Close the breaker" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === "/api/admin/switches")?.body).toEqual({
        switch: "breaker",
        on: false,
        reason: "new image rolled out",
      }),
    );
  });

  it("releases a held cart and recycles a browser, each with a reason", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "Release bea's cart" }));
    let dialog = await screen.findByRole("dialog", { name: "Release bea's cart?" });
    expect(dialog).toHaveTextContent("Empties the cart on BC Ferries and tells bea it was released.");
    await user.type(within(dialog).getByLabelText("Reason"), "stuck hold");
    await user.click(within(dialog).getByRole("button", { name: "Release" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === `/api/admin/trips/${TRIP}/release`)?.body).toEqual({
        reason: "stuck hold",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Recycle bea's browser" }));
    dialog = await screen.findByRole("dialog", { name: "Recycle bea's browser?" });
    expect(dialog).toHaveTextContent("Restarts their browser (fw-3f9c0a1b2c3d4e5f-2).");
    await user.type(within(dialog).getByLabelText("Reason"), "frozen page");
    await user.click(within(dialog).getByRole("button", { name: "Recycle" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === `/api/admin/trips/${TRIP}/recycle`)?.body).toEqual({
        reason: "frozen page",
      }),
    );
  });

  it("offers release only for a cart that's held and not being paid for", async () => {
    const holds: Overview["holds"] = [
      ...overview().holds,
      { user: CY, trip: "1a1b1c1d1e1f1011", pod: "fw-1a1b1c1d1e1f1011-g1", since: 0, cart: { status: "adding" } },
      {
        user: ME,
        trip: "2a2b2c2d2e2f2021",
        pod: "fw-2a2b2c2d2e2f2021-g1",
        since: 0,
        cart: { status: "ready", payment_in_progress: true },
      },
    ];
    api((r) => (r.url === "/api/admin/overview" ? jsonResponse({ ...overview(), holds }) : undefined));
    renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByRole("button", { name: "Recycle dee's browser" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Release bea's cart" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release dee's cart" })).toBeNull(); // it failed
    // Still going in: its pod would drop a release.
    expect(screen.queryByRole("button", { name: "Release cy's cart" })).toBeNull();
    expect(
      within(row(screen.getByRole("button", { name: "Recycle cy's browser" }))).getByText("Being added"),
    ).toBeInTheDocument();
    // Mid-payment: pulling it could charge them for nothing.
    expect(screen.queryByRole("button", { name: "Release ann's cart" })).toBeNull();
    expect(
      within(row(screen.getByRole("button", { name: "Recycle ann's browser" }))).getByText("Paying now"),
    ).toBeInTheDocument();
  });

  it("lists what needs attention in plain words", async () => {
    api();
    renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("Alerts to bea keep failing")).toBeInTheDocument();
    expect(screen.getByText("Couldn't confirm someone's cart was released")).toBeInTheDocument();
  });

  it("says when nothing needs attention", async () => {
    api((r) => (r.url === "/api/admin/overview" ? jsonResponse({ ...overview(), attention: [] }) : undefined));
    renderWithProviders(<AdminPage meId={ME} />);
    expect(await screen.findByText("Nothing needs you.")).toBeInTheDocument();
  });
});

describe("people", () => {
  it("suspends someone else, with a reason, but never offers it for yourself", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await user.click(await screen.findByRole("button", { name: "Suspend bea" }));
    expect(screen.queryByRole("button", { name: "Suspend ann" })).toBeNull();
    const dialog = await screen.findByRole("dialog", { name: "Suspend bea?" });
    await user.type(within(dialog).getByLabelText("Reason"), "asked to leave");
    await user.click(within(dialog).getByRole("button", { name: "Suspend" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === `/api/admin/users/${BEA}/state`)?.body).toEqual({
        state: "suspended",
        reason: "asked to leave",
        holds: "finish",
      }),
    );
  });

  it("can release their held cart when suspending", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await user.click(await screen.findByRole("button", { name: "Suspend bea" }));
    const dialog = await screen.findByRole("dialog", { name: "Suspend bea?" });
    expect(within(dialog).getByRole("radio", { name: "Let it finish" })).toBeChecked();
    await user.click(within(dialog).getByRole("radio", { name: "Release their held cart now" }));
    await user.type(within(dialog).getByLabelText("Reason"), "abuse");
    await user.click(within(dialog).getByRole("button", { name: "Suspend" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === `/api/admin/users/${BEA}/state`)?.body).toEqual({
        state: "suspended",
        reason: "abuse",
        holds: "release",
      }),
    );
  });

  it("shows sessions, devices, today's usage and other sign-in emails", async () => {
    api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    expect(await screen.findByText("2 sessions · alerts on 1 device")).toBeInTheDocument();
    expect(screen.getByText("Today: 1 hold · 3 check-nows")).toBeInTheDocument();
    expect(screen.getByText("Signed in with another email: bea@work.example")).toBeInTheDocument();
    expect(screen.getAllByText("Bootstrap")).toHaveLength(2);
  });

  it("signs someone out everywhere, but not yourself", async () => {
    const { requests } = api((r) => (r.url.endsWith("/revoke-sessions") ? jsonResponse({ revoked: 2 }) : undefined));
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    expect(screen.queryByRole("button", { name: "Sign out everywhere for ann" })).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere for bea" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign bea out everywhere?" });
    await user.type(within(dialog).getByLabelText("Reason"), "lost phone");
    await user.click(within(dialog).getByRole("button", { name: "Sign out" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === `/api/admin/users/${BEA}/revoke-sessions`)?.body).toEqual({
        reason: "lost phone",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("makes someone an admin, but never changes yours or a bootstrap admin's", async () => {
    const { requests } = api((r) => (r.method === "PATCH" ? jsonResponse({ is_admin: true }) : undefined));
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await user.click(await screen.findByRole("button", { name: "Make admin for bea" }));
    const dialog = await screen.findByRole("dialog", { name: "Make bea an admin?" });
    await user.type(within(dialog).getByLabelText("Reason"), "helps run it");
    await user.click(within(dialog).getByRole("button", { name: "Make admin" }));
    await waitFor(() => {
      const sent = requests.find((r) => r.method === "PATCH");
      expect(sent?.url).toBe(`/api/admin/users/${BEA}`);
      expect(sent?.body).toEqual({ is_admin: true, reason: "helps run it" });
    });
    expect(screen.queryByRole("button", { name: "Remove admin for ann" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove admin for dee" })).toBeNull();
    expect(screen.getAllByText(/set in the config, so their admin can't be removed here/)).toHaveLength(2);
  });

  it("removes an admin who isn't a bootstrap admin", async () => {
    const { requests } = api((r) => {
      if (r.method === "PATCH") return jsonResponse({ is_admin: false });
      if (r.url !== "/api/admin/users") return undefined;
      const list = people();
      return jsonResponse({ ...list, users: list.users.map((u) => (u.id === DEE ? { ...u, bootstrap: false } : u)) });
    });
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await user.click(await screen.findByRole("button", { name: "Remove admin for dee" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove dee's admin?" });
    await user.type(within(dialog).getByLabelText("Reason"), "stepped back");
    await user.click(within(dialog).getByRole("button", { name: "Remove admin" }));
    await waitFor(() =>
      expect(requests.find((r) => r.method === "PATCH")?.body).toEqual({ is_admin: false, reason: "stepped back" }),
    );
  });

  it("deletes only someone suspended, after their username is typed exactly", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await screen.findByRole("button", { name: "Suspend bea" });
    expect(screen.queryByRole("button", { name: "Delete bea" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete ann" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Delete cy" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete cy?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete" });
    await user.type(within(dialog).getByLabelText("Reason"), "asked to be removed");
    expect(confirm).toBeDisabled(); // the username isn't typed yet
    const typed = within(dialog).getByLabelText("Type cy to confirm");
    await user.type(typed, "Cy");
    expect(confirm).toBeDisabled();
    await user.clear(typed);
    await user.type(typed, "cy");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      const sent = requests.find((r) => r.method === "DELETE");
      expect(sent?.url).toBe(`/api/admin/users/${CY}`);
      expect(sent?.body).toEqual({ reason: "asked to be removed" });
    });
  });

  it("sets a limit and puts an override back to the default", async () => {
    const { requests } = api((r) =>
      r.url.endsWith("/limits") ? jsonResponse({ value: 5, source: "override" }) : undefined,
    );
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await user.click(await screen.findByRole("button", { name: "Limits for bea" }));
    const trips = screen.getByRole("textbox", { name: "Trips watched" });
    await user.clear(trips);
    await user.type(trips, "5");
    await user.click(within(row(trips)).getByRole("button", { name: "Set" }));
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason"), "planning a big trip");
    await user.click(within(dialog).getByRole("button", { name: "Set" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === `/api/admin/users/${BEA}/limits`)?.body).toEqual({
        key: "maxTrips",
        value: 5,
        reason: "planning a big trip",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const browsers = row(screen.getByRole("textbox", { name: "Browsers at once" }));
    await user.click(within(browsers).getByRole("button", { name: "Browsers at once: use the default" }));
    dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason"), "trip done");
    await user.click(within(dialog).getByRole("button", { name: "Use the default" }));
    await waitFor(() =>
      expect(requests.filter((r) => r.url === `/api/admin/users/${BEA}/limits`).at(-1)?.body).toEqual({
        key: "maxActiveBrowsers",
        value: null,
        reason: "trip done",
      }),
    );
  });

  it("invites someone who was turned away, prefilled from their sign-in", async () => {
    const { requests } = api((r) =>
      r.url === "/api/admin/invites" ? jsonResponse({ id: "fedcba9876543210" }, 201) : undefined,
    );
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await screen.findByText("dan");
    await user.click(screen.getByRole("button", { name: "Invite dan" }));
    expect(screen.getByLabelText("Sign-in username")).toHaveValue("dan");
    expect(screen.getByLabelText("Email (optional)")).toHaveValue("dan@example.com");
    await user.type(screen.getByLabelText("Reason"), "a friend");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() =>
      expect(requests.find((r) => r.url === "/api/admin/invites")?.body).toEqual({
        username: "dan",
        email: "dan@example.com",
        reason: "a friend",
      }),
    );
    await waitFor(() => expect(screen.getByLabelText("Sign-in username")).toHaveValue(""));
  });

  it("withdraws an open invitation", async () => {
    const { requests } = api();
    const { user } = renderWithProviders(<AdminPage meId={ME} />);
    await user.click(await screen.findByRole("button", { name: "People" }));
    await user.click(await screen.findByRole("button", { name: "Withdraw" }));
    const dialog = await screen.findByRole("dialog", { name: "Withdraw cat's invitation?" });
    await user.type(within(dialog).getByLabelText("Reason"), "typo");
    await user.click(within(dialog).getByRole("button", { name: "Withdraw" }));
    await waitFor(() => {
      const sent = requests.find((r) => r.method === "DELETE");
      expect(sent?.url).toBe("/api/admin/invites/0123456789abcdef");
      expect(sent?.body).toEqual({ reason: "typo" });
    });
  });
});

describe("quantityGiB", () => {
  it("reads Kubernetes memory quantities", () => {
    expect(quantityGiB("1Gi")).toBe(1);
    expect(quantityGiB("1536Mi")).toBe(1.5);
    expect(quantityGiB("2G")).toBeCloseTo(1.863, 3);
    expect(quantityGiB("lots")).toBeNull();
  });
});

describe("browserVersions", () => {
  it("counts the pods by version, most first", () => {
    expect(
      browserVersions([
        { version: "0.6.2", image: "sha256:old", pods: 1 },
        { version: "0.6.3", image: "sha256:a", pods: 1 },
        { version: "0.6.3", image: "sha256:b", pods: 2 },
        { version: null, image: null, pods: 1 },
        { version: "0.6.1", image: null, pods: 0 },
      ]),
    ).toBe("0.6.3 ×3, 0.6.2 ×1, unknown ×1");
    expect(browserVersions([])).toBe("");
  });
});
