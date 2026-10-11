import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { LiveState } from "@/lib/live";
import { mergeSnapshot } from "@/lib/state";
import { NOW, makeState } from "@/test/fixtures";
import { ConnectionBanner } from "./Banners";

const MINUTE = 60_000;
const snapshotFrom = (msAgo: number) => mergeSnapshot(undefined, makeState(), NOW.getTime() - msAgo);
const live: LiveState = { status: "live", failures: 0, lastMessageAt: NOW.getTime() };
const reconnecting = (failures: number): LiveState => ({ status: "reconnecting", failures, lastMessageAt: null });

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ConnectionBanner", () => {
  it("shows nothing while live updates are arriving", () => {
    const { container } = render(<ConnectionBanner snapshot={snapshotFrom(10 * MINUTE)} live={live} error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("asks you to sign in again when the session expired", () => {
    render(
      <ConnectionBanner snapshot={snapshotFrom(0)} live={live} error={new ApiError("Your sign-in expired", "auth")} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Your sign-in expired. Reload to sign in again.");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("suspects the sign-in when the socket keeps failing and HTTP fails too", () => {
    render(
      <ConnectionBanner
        snapshot={snapshotFrom(MINUTE)}
        live={reconnecting(3)}
        error={new ApiError("Can't reach the server", "network")}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Can't reach Ferry Watch. Your sign-in may have expired");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("doesn't blame the sign-in when the server answered with an error", () => {
    render(
      <ConnectionBanner
        snapshot={snapshotFrom(5 * MINUTE)}
        live={reconnecting(5)}
        error={new ApiError("HTTP 503", "http", { status: 503 })}
      />,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Showing data from 5 min ago. HTTP 503.");
  });

  it("says how old the data is when nothing has been heard for a while", () => {
    render(<ConnectionBanner snapshot={snapshotFrom(5 * MINUTE)} live={reconnecting(1)} error={null} />);
    expect(screen.getByRole("status")).toHaveTextContent("Showing data from 5 min ago. Reconnecting…");
  });

  it("says so as soon as a refresh fails on top of cached data", () => {
    render(
      <ConnectionBanner
        snapshot={snapshotFrom(20_000)}
        live={reconnecting(1)}
        error={new ApiError("The server took too long to respond", "timeout")}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Showing data from 20s ago. The server took too long to respond.",
    );
  });

  it("offers a reload when the server sends data this page doesn't understand", () => {
    render(
      <ConnectionBanner
        snapshot={snapshotFrom(0)}
        live={reconnecting(1)}
        error={new ApiError("Unexpected data from the server (seq: Required)", "schema")}
      />,
    );
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });
});
