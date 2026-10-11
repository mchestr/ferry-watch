import { skipToken, useQuery, type QueryClient } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TERMINALS_KEY } from "@/lib/hooks";
import type { State } from "@/lib/schema";
import { STATE_KEY, writeState, type Snapshot } from "@/lib/state";
import { NOW, TERMINALS, makeCart, makeSettings, makeState, makeTrip } from "@/test/fixtures";
import { createTestQueryClient, deferred, jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { TripSheet, type SheetMode } from "./TripSheet";

const EDIT_T1: SheetMode = { kind: "editing", tripId: "t1" };

/** Like App: the sheet gets its state from the query cache, so mutation responses flow back in. */
function Harness({ mode, onClose }: { mode: SheetMode; onClose: () => void }) {
  const { data } = useQuery<Snapshot>({ queryKey: STATE_KEY, queryFn: skipToken });
  return data ? <TripSheet mode={mode} onClose={onClose} state={data.state} /> : null;
}

function renderSheet({ mode = EDIT_T1, state = makeState() }: { mode?: SheetMode; state?: State } = {}) {
  const queryClient: QueryClient = createTestQueryClient();
  queryClient.setQueryData(TERMINALS_KEY, TERMINALS);
  writeState(queryClient, state);
  const onClose = vi.fn();
  const view = renderWithProviders(<Harness mode={mode} onClose={onClose} />, { queryClient });
  return {
    ...view,
    onClose,
    setMode: (next: SheetMode) => view.rerender(<Harness mode={next} onClose={onClose} />),
    push: (next: State) => act(() => writeState(queryClient, next)),
  };
}

const saveButton = () => screen.getByRole("button", { name: "Save" });
const setValue = (label: string, value: string) => {
  const input = screen.getByLabelText(label);
  fireEvent.change(input, { target: { value } });
  fireEvent.blur(input);
  return input;
};

beforeEach(() => {
  // Only the clock is faked (today is 2026-10-11, Pacific); timers stay real for user-event.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("TripSheet", () => {
  it("labels every control", () => {
    renderSheet();
    expect(screen.getByRole("dialog", { name: "Edit trip" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "From" })).toHaveTextContent("Victoria (Swartz Bay)");
    expect(screen.getByRole("combobox", { name: "To" })).toHaveTextContent("Vancouver (Tsawwassen)");
    expect(screen.getByLabelText("Departure date")).toHaveValue("2026-10-12");
    expect(screen.getByRole("group", { name: "Travellers" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Adults" })).toHaveTextContent("1 adult");
    expect(screen.getByRole("radiogroup", { name: "Vehicle" })).toBeInTheDocument();
    expect(screen.getByLabelText("Earliest")).toHaveValue("00:00");
    expect(screen.getByRole("switch", { name: "Add open sailings to my cart" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Fare to select" })).toBeInTheDocument();
  });

  it("PATCHes only the changed fields, then closes", async () => {
    const { requests } = mockFetch(() => jsonResponse(makeState({ seq: 2 })));
    const { user, onClose } = renderSheet({
      state: makeState({ trips: [makeTrip({ earliest: "06:00", latest: "20:00" })] }),
    });
    setValue("Latest", "21:00");
    await user.click(saveButton());
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(requests).toEqual([
      expect.objectContaining({ method: "PATCH", url: "/api/trips/t1", body: { latest: "21:00" } }),
    ]);
    expect(await screen.findByText("Trip saved")).toBeInTheDocument();
  });

  it("just closes when nothing changed", async () => {
    const { requests } = mockFetch(() => jsonResponse(makeState()));
    const { user, onClose } = renderSheet();
    await user.click(saveButton());
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(requests).toHaveLength(0);
  });

  describe("departure window", () => {
    it("blocks Save on a blank time, with the error on the field", async () => {
      const { requests } = mockFetch(() => jsonResponse(makeState()));
      const { user, onClose } = renderSheet();
      const earliest = setValue("Earliest", "");
      await user.click(saveButton());
      expect(earliest).toHaveAttribute("aria-invalid", "true");
      expect(earliest).toHaveAccessibleDescription("Enter the earliest departure time.");
      expect(earliest).toHaveFocus();
      expect(requests).toHaveLength(0);
      expect(onClose).not.toHaveBeenCalled();
    });

    it("blocks Save when earliest is after latest", async () => {
      const { requests } = mockFetch(() => jsonResponse(makeState()));
      const { user } = renderSheet();
      setValue("Earliest", "10:00");
      const latest = setValue("Latest", "08:00");
      expect(latest).toHaveAccessibleDescription("Must be at or after the earliest time.");
      await user.click(saveButton());
      expect(latest).toHaveFocus();
      expect(requests).toHaveLength(0);
    });
  });

  describe("vehicle height", () => {
    it("keeps the text as typed, validates on blur and on Save, and sends a valid height", async () => {
      const { requests } = mockFetch(() => jsonResponse(makeState({ seq: 2 })));
      const { user, onClose } = renderSheet();
      await user.click(screen.getByRole("radio", { name: "Over 7 ft" }));
      const height = screen.getByLabelText("Total height (ft)");
      expect(height).toHaveValue("8");
      expect(height).toHaveAttribute("inputmode", "decimal");

      await user.clear(height);
      await user.type(height, "7");
      expect(screen.queryByText(/Must be over 7 ft/)).not.toBeInTheDocument(); // not while typing the first time
      await user.tab();
      expect(height).toHaveAccessibleDescription(/^Must be over 7 ft\. Under that, choose Car \/ SUV\./);

      await user.clear(height);
      await user.type(height, "14.5");
      expect(screen.getByText("Must be 14 ft or less.")).toBeInTheDocument();
      await user.click(saveButton());
      expect(height).toHaveFocus();
      expect(requests).toHaveLength(0);

      await user.clear(height);
      await user.type(height, "8,");
      expect(height).toHaveValue("8,"); // no snapping back to 8 mid-edit
      await user.type(height, "5");
      await user.click(saveButton());
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(requests[0]?.body).toEqual({ vehicle: "tall", height_ft: 8.5 });
    });

    it("is a radio group with arrow-key navigation", async () => {
      const { user } = renderSheet();
      const group = screen.getByRole("radiogroup", { name: "Vehicle" });
      const car = within(group).getByRole("radio", { name: "Car / SUV" });
      expect(car).toBeChecked();
      act(() => car.focus());
      // Radix moves focus on a timer and checks the item if an arrow key is still down, so hold it.
      await user.keyboard("{ArrowRight>}");
      await waitFor(() => expect(within(group).getByRole("radio", { name: "Over 7 ft" })).toHaveFocus());
      await user.keyboard("{/ArrowRight}");
      expect(within(group).getByRole("radio", { name: "Over 7 ft" })).toBeChecked();
      expect(screen.getByLabelText("Total height (ft)")).toBeInTheDocument();
    });
  });

  describe("passengers", () => {
    it("allows zero adults when there's a senior", async () => {
      const { requests } = mockFetch(() => jsonResponse(makeState({ seq: 2 })));
      const { user, onClose } = renderSheet();
      const removeAdult = screen.getByRole("button", { name: "Remove adult" });
      await user.click(removeAdult);
      expect(screen.getByText("Add at least one adult or senior.")).toBeInTheDocument();
      // At the minimum the button is aria-disabled, not disabled, so it keeps focus.
      expect(removeAdult).toHaveAttribute("aria-disabled", "true");
      expect(removeAdult).toHaveFocus();

      await user.click(screen.getByRole("button", { name: "Children, infants & seniors" }));
      await user.click(screen.getByRole("button", { name: "Add senior" }));
      expect(screen.queryByText("Add at least one adult or senior.")).not.toBeInTheDocument();
      await user.click(saveButton());
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(requests[0]?.body).toEqual({ adults: 0, seniors: 1 });
    });

    it("allows at most 9 in total", async () => {
      const { requests } = mockFetch(() => jsonResponse(makeState()));
      const { user } = renderSheet();
      const addAdult = screen.getByRole("button", { name: "Add adult" });
      for (let i = 0; i < 9; i++) await user.click(addAdult);
      expect(screen.getByRole("group", { name: "Adults" })).toHaveTextContent("9 adults");
      expect(addAdult).toHaveAttribute("aria-disabled", "true");

      await user.click(screen.getByRole("button", { name: "Children, infants & seniors" }));
      await user.click(screen.getByRole("button", { name: "Add child" }));
      expect(screen.getByText("At most 9 passengers in total.")).toBeInTheDocument();
      await user.click(saveButton());
      expect(screen.getByRole("group", { name: "Travellers" })).toHaveFocus();
      expect(requests).toHaveLength(0);
    });
  });

  describe("date", () => {
    it("can't be in the past", async () => {
      const { requests } = mockFetch(() => jsonResponse(makeState()));
      const { user } = renderSheet();
      const date = setValue("Departure date", "2026-10-01");
      expect(date).toHaveAttribute("min", "2026-10-11");
      expect(date).toHaveAccessibleDescription("This date has passed.");
      await user.click(saveButton());
      expect(requests).toHaveLength(0);
    });

    it("flags a trip whose date has passed as soon as it opens", () => {
      renderSheet({ state: makeState({ trips: [makeTrip({ date: "2026-10-01", past: true })] }) });
      expect(screen.getByLabelText("Departure date")).toHaveAccessibleDescription("This date has passed.");
    });
  });

  it("sends one request on a double click, and stays open and locked until it's done", async () => {
    const response = deferred<Response>();
    const { requests } = mockFetch(() => response.promise);
    const { user, onClose } = renderSheet();
    setValue("Latest", "21:00");
    await user.dblClick(saveButton());
    expect(requests).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();

    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();

    response.resolve(jsonResponse(makeState({ seq: 2 })));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(requests).toHaveLength(1);
  });

  it("shows a server validation error on its field, not as a toast", async () => {
    const message = "There's no bookable route between those terminals";
    mockFetch(() => jsonResponse({ error: message, field: "to" }, 400));
    const { user, onClose } = renderSheet();
    setValue("Latest", "21:00");
    await user.click(saveButton());
    const to = screen.getByRole("combobox", { name: "To" });
    await waitFor(() => expect(to).toHaveAccessibleDescription(message));
    expect(to).toHaveFocus();
    expect(screen.getAllByText(message)).toHaveLength(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes with a notice when the trip is removed elsewhere", async () => {
    const { onClose, push } = renderSheet();
    push(makeState({ seq: 2, trips: [] }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("This trip was removed elsewhere.")).toBeInTheDocument();
  });

  it("closes once, with one notice, when a push and a 404 both say the trip is gone", async () => {
    const response = deferred<Response>();
    mockFetch(() => response.promise);
    // onClose doesn't change `mode` here, so the form stays mounted for the late response.
    const { user, onClose, push } = renderSheet();
    setValue("Latest", "21:00");
    await user.click(saveButton());
    push(makeState({ seq: 2, trips: [] }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

    response.resolve(jsonResponse({ error: "No such trip", field: null }, 404));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Saving…" })).not.toBeInTheDocument());
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText("This trip was removed elsewhere.")).toHaveLength(1);
    expect(screen.queryByText("No such trip")).not.toBeInTheDocument();
  });

  it("asks before removing, naming the sailing held in the cart", async () => {
    const { requests } = mockFetch(() => jsonResponse(makeState({ seq: 2, trips: [] })));
    const { user, onClose } = renderSheet({
      state: makeState({ trips: [makeTrip({ cart: makeCart({ status: "ready", depart: "07:00" }) })] }),
    });
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove this trip?" });
    expect(dialog).toHaveTextContent("Removing it releases the 7:00 am sailing in your cart.");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();

    await user.click(within(dialog).getByRole("button", { name: "Remove trip" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(requests).toEqual([expect.objectContaining({ method: "DELETE", url: "/api/trips/t1", body: {} })]);
    expect(await screen.findByText("Trip removed")).toBeInTheDocument();
    expect(screen.queryByText("This trip was removed elsewhere.")).not.toBeInTheDocument();
  });

  it("adds a trip starting from the server's trip_defaults", async () => {
    const defaults = makeSettings({ from: "TSA", to: "SWB", adults: 2, auto_cart: false, fare: "saver" });
    const { requests } = mockFetch(() => jsonResponse(makeState({ seq: 2 })));
    const { user, onClose } = renderSheet({ mode: { kind: "adding" }, state: makeState({ trip_defaults: defaults }) });
    expect(screen.getByRole("dialog", { name: "Add a trip" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Add open sailings to my cart" })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Add trip" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(requests).toEqual([expect.objectContaining({ method: "POST", url: "/api/trips", body: defaults })]);
  });

  it("explains and disables auto-cart when there's no BC Ferries login", () => {
    renderSheet({ state: makeState({ login_configured: false, trips: [makeTrip({ auto_cart: true })] }) });
    const autoCart = screen.getByRole("switch", { name: "Add open sailings to my cart" });
    expect(autoCart).toBeDisabled();
    expect(autoCart).not.toBeChecked();
    expect(autoCart).toHaveAccessibleDescription(/Needs a BC Ferries login/);
    expect(screen.queryByRole("combobox", { name: "Fare to select" })).not.toBeInTheDocument();
  });

  it("keeps showing the trip while it animates closed", () => {
    // jsdom has no CSS, so give Radix's Presence an exit animation to wait for.
    const style = document.createElement("style");
    style.textContent =
      "[role=dialog][data-state=open]{animation-name:sheet-in}[role=dialog][data-state=closed]{animation-name:sheet-out}";
    document.head.append(style);
    try {
      const { setMode } = renderSheet();
      setMode({ kind: "closed" });
      const closing = screen.getByRole("dialog", { hidden: true });
      expect(closing).toHaveAttribute("data-state", "closed");
      expect(within(closing).getByText("Edit trip")).toBeInTheDocument();
      expect(screen.queryByText("Add a trip")).not.toBeInTheDocument();

      const end = new Event("animationend", { bubbles: true });
      Object.defineProperty(end, "animationName", { value: "sheet-out" });
      act(() => {
        closing.dispatchEvent(end);
      });
      expect(screen.queryByRole("dialog", { hidden: true })).not.toBeInTheDocument();
    } finally {
      style.remove();
    }
  });
});
