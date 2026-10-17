import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MtApp } from "@/mt/MtApp";
import { FakeWebSocket } from "@/test/fake-websocket";
import { FIXTURE_NOW, parsedState, serveState, fail } from "@/test/fixtures";
import { jsonResponse, renderWithProviders } from "@/test/render";

beforeEach(() => {
  localStorage.setItem("wl.token", "t-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW); // Wed Jun 30 2027
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the BC Ferries form in the New watch sheet", () => {
  it("is the trip form, and adds through POST /api/watches with enabled beside the spec", async () => {
    history.replaceState(null, "", "/new/bcferries");
    const { requests } = serveState(parsedState(), (r) =>
      r.url === "/api/watches" && r.method === "POST"
        ? jsonResponse({ watch_id: "a1b2c3d4e5f60718", state: parsedState() })
        : undefined,
    );
    const { user } = renderWithProviders(<MtApp />);
    const sheet = await screen.findByRole("dialog", { name: "Add a trip" });
    await user.click(within(sheet).getByRole("combobox", { name: "From" }));
    await user.click(await screen.findByRole("option", { name: "Vancouver (Tsawwassen)" }));
    expect(within(sheet).getByRole("combobox", { name: "To" })).toHaveTextContent("Nanaimo (Duke Point)");
    await user.click(within(sheet).getByRole("button", { name: "Add trip" }));
    await waitFor(() => expect(requests.find((r) => r.url === "/api/watches")).toBeDefined());
    const body = requests.find((r) => r.url === "/api/watches")?.body as {
      provider: string;
      spec: Record<string, unknown>;
      enabled: boolean;
    };
    expect(body.provider).toBe("bcferries");
    expect(body.enabled).toBe(true);
    expect(body.spec).toMatchObject({ from: "TSA", to: "DUK", date: "2027-07-01", adults: 1 });
    expect(body.spec).not.toHaveProperty("enabled");
    expect(await screen.findByText("Trip added. It'll be checked shortly.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("edits through PATCH /api/watches/{id} with only the changed settings in spec", async () => {
    history.replaceState(null, "", "/");
    const { requests } = serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    await screen.findByRole("heading", { level: 2, name: /→/ });
    const edits = screen.getAllByRole("button", { name: "Edit" });
    await user.click(edits[edits.length - 1] ?? fail()); // the trip's card is the last with Edit
    const sheet = await screen.findByRole("dialog", { name: "Edit trip" });
    fireEvent.change(within(sheet).getByLabelText("Earliest"), { target: { value: "06:00" } });
    await user.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests.find((r) => r.method === "PATCH")).toMatchObject({
        url: "/api/watches/a1b2c3d4e5f60718",
        body: { spec: { earliest: "06:00" } },
      }),
    );
    expect(await screen.findByText("Trip saved")).toBeInTheDocument();
  });
});
