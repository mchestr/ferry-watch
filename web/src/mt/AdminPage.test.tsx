import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders, type RecordedRequest } from "@/test/render";
import type { Overview, People, Unmatched } from "./admin";
import { AdminPage } from "./AdminPage";

const ME = "a".repeat(20);
const BEA = "b".repeat(20);

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
  };
  return {
    users: [
      { ...user, id: ME, username: "ann", is_admin: true },
      { ...user, id: BEA, username: "bea", email: "bea@example.com" },
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
        trip: "3f9c0a1b2c3d4e5f",
        since: Date.now() / 1000 - 120,
        cart: { status: "ready", depart: "09:00", hard_deadline: Date.now() / 1000 + 25 * 60 },
      },
    ],
    switches: { maintenance: false, pause_carting: true },
    outcomes_7d: { booked: 4, released: 1 },
  };
}

const unmatched: Unmatched = [
  { issuer: "https://auth.chestr.dev", username: "dan", email: "dan@example.com", last_seen: Date.now() / 1000 },
];

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
      }),
    );
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
