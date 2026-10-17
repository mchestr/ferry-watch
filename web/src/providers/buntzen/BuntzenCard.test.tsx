import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MtApp } from "@/mt/MtApp";
import { parseWatch, type BuntzenWatch, type WatchOption } from "@/mt/watch-schema";
import { FakeWebSocket } from "@/test/fake-websocket";
import {
  apiState,
  BUNTZEN_INFO,
  FIXTURE_NOW,
  makeBuntzenWatch,
  parsedState,
  serveState,
  stateWith,
  fail,
} from "@/test/fixtures";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { BuntzenCard } from "./BuntzenCard";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FIXTURE_NOW); // Wed Jun 30 2027, 7:31 am
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const open = makeBuntzenWatch(); // Thu Jul 1: morning open, afternoon sold out
const option = (key: string, status: WatchOption["status"], changes: Partial<WatchOption> = {}): WatchOption => ({
  ...(open.options[0] ?? fail()),
  key,
  label: { full_day: "All-day pass", morning: "Morning pass", afternoon: "Afternoon pass" }[key] ?? key,
  status,
  ...changes,
});

function show(watch: BuntzenWatch) {
  const state = stateWith([watch]);
  return renderWithProviders(<BuntzenCard watch={watch} state={state} info={BUNTZEN_INFO} />);
}

/** Each pass's row: its label and chip. */
const rows = () =>
  within(screen.getByRole("list", { name: "Passes" }))
    .getAllByRole("listitem")
    .map((li) => li.textContent);

describe("a Buntzen Lake card", () => {
  it("says the provider, the day and the passes, with a badge", () => {
    show(open);
    const line = screen.getByText(
      (_, element) => element?.tagName === "P" && (element.textContent ?? "").startsWith("Buntzen Lake"),
    );
    expect(line).toHaveTextContent("Buntzen Lake · , Thu Jul 1"); // ", " for screen readers, " · " for the eye
    expect(screen.getByRole("heading", { name: "Morning or afternoon pass" })).toBeInTheDocument();
    expect(screen.getByText("Open now")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Details/ })).toHaveAttribute("href", `/w/${open.id}`);
  });

  it("lists each pass with its status, greying the ones not watched", () => {
    show(
      makeBuntzenWatch({
        spec: { passes: ["morning"] },
        options: [option("morning", "not_released"), option("afternoon", "sold_out", { wanted: false })],
      }),
    );
    expect(rows()).toEqual(["Morning passNot on sale yet", "Afternoon pass · not watchedSold out"]);
  });

  it.each<[WatchOption["status"], string]>([
    ["open", "Open"],
    ["maybe", "May be open"],
    ["sold_out", "Sold out"],
    ["not_released", "Not on sale yet"],
    ["not_offered", "Not offered"],
    ["over", "Over"],
    ["unchecked", "Not checked yet"],
  ])("shows a pass that's %s as %s", (status, text) => {
    show(makeBuntzenWatch({ options: [option("morning", status)] }));
    expect(rows()).toEqual([`Morning pass${text}`]);
  });

  it("says Couldn't check for a pass whose check failed while the others' worked", () => {
    show(
      makeBuntzenWatch({
        check: { outcome: "error", consecutive_errors: 1 },
        options: [option("full_day", "unchecked"), option("morning", "sold_out")],
      }),
    );
    expect(rows()).toEqual(["All-day passCouldn't check", "Morning passSold out"]);
  });

  it("shows what Yodel said, escaped, only in the maybe chip's tooltip", async () => {
    const detail = "Waitlist only <b>";
    const { user } = show(makeBuntzenWatch({ options: [option("morning", "maybe", { detail })] }));
    expect(screen.queryByText(/Waitlist only/)).toBeNull();
    await user.hover(screen.getByRole("button", { name: /May be open/ }));
    await waitFor(() => expect(screen.getAllByText(`Yodel says: ${detail}`).length).toBeGreaterThan(0));
  });

  it("links to Yodel for an open or maybe-open pass, and only for those", () => {
    show(
      makeBuntzenWatch({
        spec: { passes: ["full_day", "morning"] },
        options: [
          option("full_day", "maybe"),
          option("morning", "sold_out"),
          option("afternoon", "open", { wanted: false }),
        ],
      }),
    );
    const book = screen.getByRole("link", { name: /Book on Yodel/ });
    expect(book).toHaveAttribute("href", "https://yodelportal.com/buntzen-lake/Half-Day-Pass");
    expect(book).toHaveAttribute("target", "_blank");
    expect(book).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getAllByRole("link", { name: /Book/ })).toHaveLength(1);
  });

  it("names the passes when two open passes book on different pages", () => {
    show(
      makeBuntzenWatch({
        spec: { passes: ["full_day", "morning"] },
        options: [
          option("full_day", "open", { booking_url: "https://yodelportal.com/buntzen-lake/All-Day-Pass" }),
          option("morning", "open"),
        ],
      }),
    );
    expect(screen.getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Details",
      "Book all-day pass on Yodel",
      "Book morning pass on Yodel",
    ]);
  });

  it("never links off Yodel's Buntzen pages", () => {
    const raw = apiState().watches[0] as unknown as { options: { booking_url: string | null }[] };
    (raw.options[0] ?? fail()).booking_url = "https://evil.example/buntzen-lake/Half-Day-Pass";
    const watch = parseWatch(raw);
    if (watch?.kind !== "buntzen") throw new Error("expected buntzen");
    show(watch);
    expect(screen.queryByRole("link", { name: /Book on Yodel/ })).toBeNull();
    expect(screen.getByRole("button", { name: "I booked it" })).toBeInTheDocument();
  });

  it("archives the watch on I booked it", async () => {
    const { requests } = mockFetch(() => jsonResponse(stateWith([])));
    const { user } = show(open);
    await user.click(screen.getByRole("button", { name: "I booked it" }));
    await waitFor(() =>
      expect(requests.map((r) => [r.method, r.url])).toEqual([["POST", `/api/watches/${open.id}/done`]]),
    );
    expect(await screen.findByText("Enjoy the lake.")).toBeInTheDocument();
  });

  it("says when it was last checked without announcing every check", () => {
    show(makeBuntzenWatch({ check: { state: "checking", checked_at: 1_814_365_860 }, options: [] }));
    expect(screen.getByText("Checked 7:31 am, every minute.")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull(); // it changes every check: not a live region
  });

  it("never shows a cart, a hold or a checkout", () => {
    show(open);
    expect(screen.queryByText(/cart|hold it|checkout|did you book it/i)).toBeNull();
    expect(
      screen.getByText("Alert only. You book on Yodel with your own account; Waitlist never asks for it."),
    ).toBeInTheDocument();
  });

  it.each([
    ["checking", true],
    ["pending", true],
    ["error", true],
    ["waiting_release", false],
    ["backoff", false],
    ["quiet", false],
    ["paused", false],
    ["provider_paused", false],
    ["unavailable", false],
  ] as const)("offers Check now when %s: %s", (state, enabled) => {
    show(makeBuntzenWatch({ check: { state }, options: [option("morning", "sold_out")] }));
    const check = screen.getByRole("button", { name: "Check now" });
    if (enabled) expect(check).toBeEnabled();
    else expect(check).toBeDisabled();
  });

  it("checks now, pauses and resumes through /api/watches", async () => {
    const quiet = makeBuntzenWatch({ options: [option("morning", "sold_out")] });
    const { requests } = mockFetch(() => jsonResponse(stateWith([quiet])));
    const { user, rerender } = show(quiet);
    await user.click(screen.getByRole("button", { name: "Check now" }));
    expect(await screen.findByText("Checking now")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    const paused = { ...quiet, enabled: false, check: { ...quiet.check, state: "paused" as const } };
    rerender(<BuntzenCard watch={paused} state={stateWith([paused])} info={BUNTZEN_INFO} />);
    await user.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() =>
      expect(requests.map((r) => [r.method, r.url, r.body])).toEqual([
        ["POST", `/api/watches/${quiet.id}/check`, {}],
        ["PATCH", `/api/watches/${quiet.id}`, { enabled: false }],
        ["PATCH", `/api/watches/${quiet.id}`, { enabled: true }],
      ]),
    );
  });

  it("removes the watch after asking", async () => {
    const { requests } = mockFetch(() => jsonResponse(stateWith([])));
    const { user } = show(open);
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Stop watching this?" });
    expect(dialog).toHaveTextContent("Buntzen Lake, Thu Jul 1: morning or afternoon pass");
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(requests.map((r) => [r.method, r.url])).toEqual([["DELETE", `/api/watches/${open.id}`]]),
    );
  });

  it("offers Watch another day once it's over: the form, with the same passes and the next day", async () => {
    localStorage.setItem("wl.token", "t-1");
    vi.stubGlobal("WebSocket", FakeWebSocket);
    history.replaceState(null, "", "/");
    const over = makeBuntzenWatch({
      spec: { date: "2027-06-30", passes: ["afternoon"] },
      check: { state: "over" },
      options: [option("afternoon", "over")],
    });
    serveState(stateWith([over], { capacity: parsedState().capacity }));
    const { user } = renderWithProviders(<MtApp />);
    await user.click(await screen.findByRole("button", { name: "Ended 1" }));
    expect(screen.queryByRole("button", { name: "Check now" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Watch another day" }));
    const dialog = await screen.findByRole("dialog", { name: "Watch Buntzen Lake" });
    expect(within(dialog).getByRole("radio", { name: "Tomorrow" })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: "Afternoon" })).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByRole("button", { name: "Morning" })).toHaveAttribute("aria-pressed", "false");
  });
});
