import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MtApp } from "@/mt/MtApp";
import type { WaitlistState } from "@/mt/watch-schema";
import { FakeWebSocket } from "@/test/fake-websocket";
import { FIXTURE_NOW, parsedState, serveState, fail } from "@/test/fixtures";
import { jsonResponse, renderWithProviders } from "@/test/render";

beforeEach(() => {
  // jsdom has no pointer capture; a toast's action button (View) asks for it.
  Element.prototype.setPointerCapture = () => undefined;
  localStorage.setItem("wl.token", "t-1");
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW); // Wed Jun 30 2027, 7:31 am
  history.replaceState(null, "", "/new/buntzen");
});

afterEach(() => {
  Reflect.deleteProperty(Element.prototype, "setPointerCapture");
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const REMIND = "Remind me at 6:55 am the day before (passes go on sale at 7:00)";
const days = () => screen.getAllByRole("radio").map((r) => r.textContent);
const passes = () => ["All day", "Morning", "Afternoon"].map((name) => screen.getByRole("button", { name }));

async function open(state: WaitlistState = parsedState(), answer?: Parameters<typeof serveState>[1]) {
  const served = serveState(state, answer);
  const view = renderWithProviders(<MtApp />);
  await screen.findByRole("dialog", { name: "Watch Buntzen Lake" });
  return { ...served, ...view };
}

describe("the Buntzen Lake form", () => {
  it("offers today while a pass is still worth finding, tomorrow, the next five days and another date", async () => {
    await open();
    expect(days()).toEqual(["Today", "Tomorrow", "Fri 2", "Sat 3", "Sun 4", "Mon 5", "Tue 6", "Other date"]);
    expect(screen.getAllByRole("radio").some((r) => r.getAttribute("aria-checked") === "true")).toBe(false);
  });

  it("drops Today after 6 pm, when every pass has stopped counting", async () => {
    vi.setSystemTime(new Date("2027-06-30T18:00:00-07:00"));
    await open();
    expect(days()).toEqual(["Tomorrow", "Fri 2", "Sat 3", "Sun 4", "Mon 5", "Tue 6", "Other date"]);
  });

  it("starts with every pass on, and keeps at least one", async () => {
    const { user } = await open();
    expect(passes().map((p) => p.getAttribute("aria-pressed"))).toEqual(["true", "true", "true"]);
    expect(screen.getByText("Morning passes stop counting at noon on the day.")).toBeInTheDocument();
    const [allDay, morning, afternoon] = passes();
    await user.click(allDay ?? fail());
    await user.click(morning ?? fail());
    await user.click(afternoon ?? fail()); // the last one: stays on
    expect(passes().map((p) => p.getAttribute("aria-pressed"))).toEqual(["false", "false", "true"]);
  });

  it("offers the reminder only while 6:55 the day before is still ahead", async () => {
    const { user } = await open();
    expect(screen.getByRole("switch", { name: REMIND })).toBeChecked(); // on by default
    await user.click(screen.getByRole("radio", { name: "Tomorrow" })); // its reminder was at 6:55 this morning
    expect(screen.queryByRole("switch", { name: REMIND })).toBeNull();
    await user.click(screen.getByRole("radio", { name: "Fri 2" }));
    expect(screen.getByRole("switch", { name: REMIND })).toBeChecked();
  });

  it("says when a date is out of season", async () => {
    vi.setSystemTime(new Date("2027-09-25T12:00:00-07:00"));
    const { user } = await open();
    await user.click(screen.getByRole("radio", { name: "Mon 27" }));
    expect(screen.queryByText(/outside Buntzen's reservation season/)).toBeNull();
    await user.click(screen.getByRole("radio", { name: "Fri 1" }));
    expect(
      screen.getByText(
        "That date is outside Buntzen's reservation season, so Yodel probably won't offer passes. We'll check every 30 minutes anyway.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: REMIND })).toBeNull(); // no reminders out of season
  });

  it("warns when the user's quiet hours cover the 7:00 release", async () => {
    const state = parsedState();
    state.settings.quiet_hours = { start: "23:00", end: "07:30" };
    await open(state);
    expect(
      screen.getByText(
        "Your quiet hours (until 7:30 am) cover the 7:00 release, so no reminder and no checks until then. Change them in Settings.",
      ),
    ).toBeInTheDocument();
  });

  it("doesn't warn for the default quiet hours, which end at 5:00", async () => {
    await open();
    expect(screen.queryByText(/cover the 7:00 release/)).toBeNull();
    expect(
      screen.getByText(
        "Waitlist only alerts you. You book on Yodel with your own account, licence plate and mobile number.",
      ),
    ).toBeInTheDocument();
  });

  it("asks for a day before sending anything", async () => {
    const { user, requests } = await open();
    await user.click(screen.getByRole("button", { name: "Start watching" }));
    expect(screen.getByText("Pick a day")).toBeInTheDocument();
    expect(requests.some((r) => r.method === "POST")).toBe(false);
  });

  it("takes another date, within the horizon", async () => {
    const { user, requests } = await open(parsedState(), (r) =>
      r.method === "POST" ? jsonResponse({ watch_id: "0f3c9a1b2d4e5f60", state: parsedState() }) : undefined,
    );
    await user.click(screen.getByRole("radio", { name: "Other date" }));
    const input = screen.getByLabelText("Date");
    expect(input).toHaveAttribute("min", "2027-06-30");
    expect(input).toHaveAttribute("max", "2027-07-30");
    await user.type(input, "2027-07-31");
    await user.click(screen.getByRole("button", { name: "Start watching" }));
    expect(screen.getByText("Pick a date from today to 30 days ahead")).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "2027-07-20");
    await user.click(screen.getByRole("button", { name: "Start watching" }));
    await waitFor(() =>
      expect(requests.find((r) => r.method === "POST")?.body).toMatchObject({ spec: { date: "2027-07-20" } }),
    );
  });

  it("puts the server's field errors on their field, and a refusal above the buttons", async () => {
    let refusal: Response = jsonResponse({ error: "Those passes are over for today", field: "passes" }, 400);
    const { user } = await open(parsedState(), (r) => (r.method === "POST" ? refusal : undefined));
    await user.click(screen.getByRole("radio", { name: "Tomorrow" }));
    await user.click(screen.getByRole("button", { name: "Start watching" }));
    expect(await screen.findByText("Those passes are over for today")).toBeInTheDocument();
    refusal = jsonResponse({ error: "You can have up to 6 alert-only watches", field: "provider", limit: 6 }, 409);
    await user.click(screen.getByRole("button", { name: "Start watching" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You can have up to 6 alert-only watches");
    expect(screen.getByRole("dialog", { name: "Watch Buntzen Lake" })).toBeInTheDocument(); // still open
  });

  it("edits with a PATCH of only what changed", async () => {
    history.replaceState(null, "", "/");
    const { requests } = serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    const card = (await screen.findByRole("heading", { name: "Morning or afternoon pass" })).closest("div.rounded-2xl");
    if (!(card instanceof HTMLElement)) throw new Error("no card");
    await user.click(within(card).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit watch" });
    expect(within(dialog).getByRole("radio", { name: "Tomorrow" })).toBeChecked();
    expect(passes().map((p) => p.getAttribute("aria-pressed"))).toEqual(["false", "true", "true"]);
    await user.click(passes()[0] ?? fail());
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests.find((r) => r.method === "PATCH")).toMatchObject({
        url: "/api/watches/0f3c9a1b2d4e5f60",
        body: { spec: { passes: ["full_day", "morning", "afternoon"] } },
      }),
    );
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("closes without a request when nothing changed", async () => {
    history.replaceState(null, "", "/");
    const { requests } = serveState(parsedState());
    const { user } = renderWithProviders(<MtApp />);
    const card = (await screen.findByRole("heading", { name: "All-day pass" })).closest("div.rounded-2xl");
    if (!(card instanceof HTMLElement)) throw new Error("no card");
    await user.click(within(card).getByRole("button", { name: "Edit" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(requests.some((r) => r.method === "PATCH")).toBe(false);
  });
});
