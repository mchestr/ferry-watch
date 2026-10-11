import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "@/test/fake-websocket";
import { makeState } from "@/test/fixtures";
import { SILENCE_TIMEOUT_MS, backoffDelay, socketUrl, useLiveUpdates } from "./live";
import { readSnapshot } from "./state";

const ping = { type: "ping", server_time: 1 };

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
}

function setup() {
  const qc = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, ...renderHook(() => useLiveUpdates(), { wrapper }) };
}

const advance = (ms: number) => {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
};
const sockets = () => FakeWebSocket.instances.length;

beforeEach(() => {
  vi.useFakeTimers();
  // No jitter by default: retries come at exactly 1, 2, 4, 8 and 15 s.
  vi.spyOn(Math, "random").mockReturnValue(1);
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  setVisibility("visible");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("socketUrl and backoffDelay", () => {
  it("connects to /api/ws on the page's host, over wss for https", () => {
    expect(socketUrl("https://ferry.example/")).toBe("wss://ferry.example/api/ws");
    expect(socketUrl("http://localhost:5173/")).toBe("ws://localhost:5173/api/ws");
  });

  it("doubles from 1 s, caps at 15 s, and jitters between half and all of it", () => {
    expect([0, 1, 2, 3, 4, 10].map((attempt) => backoffDelay(attempt, () => 1))).toEqual([
      1000, 2000, 4000, 8000, 15000, 15000,
    ]);
    expect(backoffDelay(0, () => 0)).toBe(500);
    expect(backoffDelay(10, () => 0)).toBe(7500);
  });
});

describe("useLiveUpdates", () => {
  it("is live only once a message arrives, not when the socket opens", () => {
    const { result } = setup();
    expect(result.current.status).toBe("connecting");
    act(() => FakeWebSocket.latest.serverOpen());
    expect(result.current.status).toBe("connecting");
    act(() => FakeWebSocket.latest.serverSend(ping));
    expect(result.current).toMatchObject({ status: "live", failures: 0 });
  });

  it("writes pushed states through the boot/seq rule", () => {
    const { qc } = setup();
    const ws = FakeWebSocket.latest;
    act(() => ws.serverSend({ type: "state", data: makeState({ seq: 5 }) }));
    expect(readSnapshot(qc)?.state.seq).toBe(5);
    act(() => ws.serverSend({ type: "state", data: makeState({ seq: 4 }) }));
    expect(readSnapshot(qc)?.state.seq).toBe(5);
    act(() => ws.serverSend({ type: "state", data: makeState({ boot_id: "boot-b", seq: 1 }) }));
    expect(readSnapshot(qc)?.state).toMatchObject({ boot_id: "boot-b", seq: 1 });
  });

  it("ignores messages that don't match the contract", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { qc } = setup();
    act(() => FakeWebSocket.latest.serverSend({ type: "state", data: { boot_id: "x" } }));
    act(() => FakeWebSocket.latest.serverSend("not json"));
    expect(readSnapshot(qc)).toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(2);
  });

  it("says 'reconnecting' only after an attempt fails, then backs off 1, 2, 4, 8, 15, 15 s", () => {
    const { result } = setup();
    expect(result.current.status).toBe("connecting");
    for (const [i, delay] of [1000, 2000, 4000, 8000, 15000, 15000].entries()) {
      act(() => FakeWebSocket.latest.serverClose());
      expect(result.current).toMatchObject({ status: "reconnecting", failures: i + 1 });
      advance(delay - 1);
      expect(sockets()).toBe(i + 1);
      advance(1);
      expect(sockets()).toBe(i + 2);
    }
  });

  it("resets the backoff only after a message, not on open", () => {
    setup();
    act(() => FakeWebSocket.latest.serverClose());
    advance(1000);
    act(() => FakeWebSocket.latest.serverClose());
    advance(2000);
    expect(sockets()).toBe(3);

    // Opens, but never says anything: still escalating.
    act(() => FakeWebSocket.latest.serverOpen());
    act(() => FakeWebSocket.latest.serverClose());
    advance(3999);
    expect(sockets()).toBe(3);
    advance(1);
    expect(sockets()).toBe(4);

    // Delivers a message: the next failure starts over at 1 s.
    act(() => FakeWebSocket.latest.serverSend(ping));
    act(() => FakeWebSocket.latest.serverClose());
    advance(999);
    expect(sockets()).toBe(4);
    advance(1);
    expect(sockets()).toBe(5);
  });

  it("adds jitter to the delay", () => {
    vi.mocked(Math.random).mockReturnValue(0);
    setup();
    act(() => FakeWebSocket.latest.serverClose());
    advance(499);
    expect(sockets()).toBe(1);
    advance(1);
    expect(sockets()).toBe(2);
  });

  it("stays live while messages keep coming", () => {
    const { result } = setup();
    for (let i = 0; i < 8; i++) {
      act(() => FakeWebSocket.latest.serverSend(ping));
      advance(15_000);
    }
    expect(result.current.status).toBe("live");
    expect(sockets()).toBe(1);
  });

  it("treats 35 s of silence as a dead connection, even though the socket looks open", () => {
    const { result } = setup();
    const ws = FakeWebSocket.latest;
    act(() => {
      ws.serverOpen();
      ws.serverSend(ping);
    });
    advance(SILENCE_TIMEOUT_MS - 1);
    expect(result.current.status).toBe("live");
    advance(1);
    expect(result.current).toMatchObject({ status: "reconnecting", failures: 1 });
    expect(result.current.lastMessageAt).not.toBeNull();
    expect(ws.closeCalls).toBe(1);
    expect(ws.onmessage).toBeNull(); // detached before closing
    advance(1000);
    expect(sockets()).toBe(2);
  });

  it("gives up on an attempt that never connects", () => {
    const { result } = setup();
    advance(SILENCE_TIMEOUT_MS);
    expect(result.current.status).toBe("reconnecting");
    expect(FakeWebSocket.latest.closeCalls).toBe(1);
  });

  it("checks for silence when the page becomes visible again (timers stop while a phone sleeps)", () => {
    const { result } = setup();
    act(() => FakeWebSocket.latest.serverSend(ping));
    // No timers ran while the page was hidden, so only the clock moved.
    vi.setSystemTime(Date.now() + 40_000);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current.status).toBe("reconnecting");
    expect(sockets()).toBe(2); // straight away, no backoff
  });

  it("leaves a healthy connection alone when the page becomes visible", () => {
    const { result } = setup();
    act(() => FakeWebSocket.latest.serverSend(ping));
    advance(10_000);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current.status).toBe("live");
    expect(sockets()).toBe(1);
  });

  it("skips the rest of a backoff wait on return, keeping a single timer", () => {
    setup();
    for (const delay of [1000, 2000, 4000]) {
      act(() => FakeWebSocket.latest.serverClose());
      advance(delay);
    }
    act(() => FakeWebSocket.latest.serverClose()); // now waiting 8 s
    expect(sockets()).toBe(4);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(sockets()).toBe(5);
    advance(8000); // the old backoff timer must not fire a second connect
    expect(sockets()).toBe(5);
  });

  it("ignores events from a socket it has replaced", () => {
    const { qc, result } = setup();
    const first = FakeWebSocket.latest;
    act(() => first.serverSend(ping));
    // Events the browser may already have queued for the first socket:
    const queuedMessage = first.onmessage;
    const queuedClose = first.onclose;

    vi.setSystemTime(Date.now() + 40_000);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const second = FakeWebSocket.latest;
    expect(second).not.toBe(first);
    expect(first.closeCalls).toBe(1);
    expect([first.onmessage, first.onclose, first.onerror]).toEqual([null, null, null]);

    act(() => {
      queuedMessage?.(new MessageEvent("message", { data: JSON.stringify({ type: "state", data: makeState() }) }));
      queuedClose?.(new CloseEvent("close"));
    });
    expect(readSnapshot(qc)).toBeUndefined();
    expect(sockets()).toBe(2);

    act(() => second.serverSend(ping));
    expect(result.current.status).toBe("live");
  });

  it("doesn't close the socket on error; the close that follows reconnects", () => {
    const { result } = setup();
    const ws = FakeWebSocket.latest;
    act(() => ws.serverError());
    expect(ws.closeCalls).toBe(0);
    expect(result.current.status).toBe("connecting");
    act(() => ws.serverClose());
    advance(1000);
    expect(sockets()).toBe(2);
  });

  it("closes the socket and stops reconnecting on unmount", () => {
    const { unmount } = setup();
    const ws = FakeWebSocket.latest;
    act(() => ws.serverSend(ping));
    unmount();
    expect(ws.closeCalls).toBe(1);
    expect([ws.onmessage, ws.onclose]).toEqual([null, null]);
    vi.advanceTimersByTime(120_000);
    vi.setSystemTime(Date.now() + 120_000);
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("online"));
    expect(sockets()).toBe(1);
  });
});
