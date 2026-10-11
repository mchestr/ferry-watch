import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NOW, NOW_S, makeState, makeTrip } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";
import { WatcherCard } from "./WatcherCard";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WatcherCard", () => {
  it("counts down to the next check using the measured clock skew", () => {
    // skew = client clock − server clock. The server is 10 s ahead, so its NOW_S + 85 is 75 s away.
    renderWithProviders(<WatcherCard state={makeState({ next_check_at: NOW_S + 85 })} skew={-10} />);
    expect(screen.getByText("1:15")).toBeInTheDocument();
    expect(screen.getByText("Next check at 12:01 pm")).toBeInTheDocument();
  });

  it("says 'Checking…' when the check is due, not 0:00", () => {
    renderWithProviders(<WatcherCard state={makeState({ next_check_at: NOW_S - 2 })} skew={0} />);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.queryByText("0:00")).not.toBeInTheDocument();
  });

  it("says when the next check is overdue", () => {
    renderWithProviders(<WatcherCard state={makeState({ next_check_at: NOW_S - 120 })} skew={0} />);
    expect(screen.getByText("Overdue")).toBeInTheDocument();
    expect(screen.getByText("Next check is overdue")).toBeInTheDocument();
  });

  it("shows a stalled watcher clearly", () => {
    renderWithProviders(
      <WatcherCard state={makeState({ watcher: { heartbeat_at: NOW_S - 600, healthy: false } })} skew={0} />,
    );
    expect(screen.getByText("Watcher stalled")).toBeInTheDocument();
    expect(screen.getByText(/No heartbeat since 11:50 am/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check now" })).toBeDisabled();
  });

  it("labels the interval picker and the setup checks", () => {
    renderWithProviders(
      <WatcherCard state={makeState({ alerts_configured: false, trips: [makeTrip({ session: null })] })} skew={0} />,
    );
    expect(screen.getByRole("combobox", { name: "Check every" })).toHaveTextContent("30 seconds");
    expect(screen.getAllByRole("img", { name: "Not set up" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Test alert" })).toBeDisabled();
  });
});
