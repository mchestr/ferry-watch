import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Trip } from "@/lib/schema";
import { NOW_S, makeCart, makeSailing, makeTrip } from "@/test/fixtures";
import { StatusPanel } from "./StatusPanel";

function renderPanel(trip: Trip, watching = true) {
  const onPause = vi.fn();
  const onRemove = vi.fn();
  render(
    <StatusPanel trip={trip} watching={watching} skew={0} onPause={onPause} pausePending={false} onRemove={onRemove} />,
  );
  return { onPause, onRemove };
}

const bookable = makeSailing({ status: "available", price: "$93.45" });

describe("StatusPanel", () => {
  it("shows a failed check with the time of the results still on screen", () => {
    const checkedAt = new Date("2026-10-11T11:30:00-07:00").getTime() / 1000;
    renderPanel(
      makeTrip({ error: "Queue-it timed out", error_at: NOW_S, checked_at: checkedAt, sailings: [bookable] }),
    );
    expect(screen.getByText("Last check failed")).toBeInTheDocument();
    expect(screen.getByText("Queue-it timed out")).toBeInTheDocument();
    expect(screen.getByText("Showing results from 11:30 am.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Book on BC Ferries/ })).not.toBeInTheDocument();
  });

  it("offers Book on BC Ferries for bookable sailings, but not while a cart is held", () => {
    const { unmount } = render(
      <StatusPanel
        trip={makeTrip({ sailings: [bookable] })}
        watching
        skew={0}
        onPause={vi.fn()}
        pausePending={false}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByText("1 sailing bookable now")).toBeInTheDocument();
    expect(screen.getByText("7:00 am ($93.45)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Book on BC Ferries/ })).toHaveAttribute(
      "href",
      "https://www.bcferries.com/",
    );
    unmount();

    renderPanel(makeTrip({ sailings: [bookable], cart: makeCart({ status: "ready" }) }));
    expect(screen.getByText("Checks paused while your cart is held")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Book on BC Ferries/ })).not.toBeInTheDocument();
  });

  it("offers Pause and Remove for a trip whose date has passed", async () => {
    const user = userEvent.setup();
    const { onPause, onRemove } = renderPanel(makeTrip({ past: true }));
    expect(screen.getByText("This date has passed")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(onPause).toHaveBeenCalledOnce();
    expect(onRemove).toHaveBeenCalledOnce();
  });

  it("doesn't offer Pause for a past trip that's already paused", () => {
    renderPanel(makeTrip({ past: true, enabled: false }));
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("explains a pause", () => {
    renderPanel(makeTrip({ sailings: [bookable] }), false);
    expect(screen.getByText("Watching is paused")).toBeInTheDocument();
  });

  it("words 'nothing bookable' honestly, with a labelled watching dot", () => {
    renderPanel(makeTrip({ sailings: [makeSailing({ in_window: false })] }));
    expect(screen.getByText("No reservations available yet")).toBeInTheDocument();
    expect(screen.getByText("The only sailing that day is outside your departure window.")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Watching" })).toBeInTheDocument();
  });
});
