/**
 * Live updates over the server's WebSocket. "Live" means messages are arriving: the server sends the
 * state on every change and a ping every 15 s otherwise, so ~35 s of silence means the connection is
 * dead, whatever the socket's readyState says.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { parseJson } from "./api";
import { WsMessageSchema } from "./schema";
import { writeState } from "./state";

export const SILENCE_TIMEOUT_MS = 35_000;
export const BASE_BACKOFF_MS = 1_000;
export const MAX_BACKOFF_MS = 15_000;

export type LiveStatus =
  /** No message yet, and no attempt has failed. */
  | "connecting"
  /** A message arrived within the last SILENCE_TIMEOUT_MS. */
  | "live"
  /** An attempt closed or went silent; retrying with backoff. */
  | "reconnecting";

export interface LiveState {
  status: LiveStatus;
  /** Consecutive attempts that ended without receiving a message. */
  failures: number;
  /**
   * Client time (epoch ms) of the last message, or null before the first. Updated when the status
   * changes, not on every message, so pings don't re-render the app.
   */
  lastMessageAt: number | null;
}

/** Exponential backoff with jitter: between half and all of min(15 s, 1 s × 2^attempt). */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const ceiling = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempt);
  return Math.round(ceiling * (0.5 + random() * 0.5));
}

/** The WebSocket endpoint on this page's host: wss: for https: pages. */
export function socketUrl(pageUrl: string = window.location.href): string {
  const url = new URL("/api/ws", pageUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

/** Keeps the state query current from WebSocket pushes, and reports whether they're arriving. */
export function useLiveUpdates(): LiveState {
  const qc = useQueryClient();
  const [live, setLive] = useState<LiveState>({ status: "connecting", failures: 0, lastMessageAt: null });

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    // A single timer: the silence watchdog while a socket is open or opening, the backoff delay between attempts.
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let isLive = false;
    let lastMessageAt = 0;
    let attemptStartedAt = 0;

    const schedule = (ms: number, fn: () => void) => {
      clearTimeout(timer);
      timer = setTimeout(fn, ms);
    };

    // Detach the handlers first, so a socket on its way out can't report anything.
    const drop = () => {
      const ws = socket;
      if (!ws) return;
      socket = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.close();
    };

    const noteFailure = () => {
      failures += 1;
      isLive = false;
      setLive({ status: "reconnecting", failures, lastMessageAt: lastMessageAt || null });
    };

    const retryLater = () => {
      schedule(backoffDelay(failures - 1), connect);
    };

    // The current attempt closed, or went silent: give up on it, wait, try again.
    const fail = () => {
      drop();
      noteFailure();
      retryLater();
    };

    function connect() {
      if (stopped) return;
      drop();
      attemptStartedAt = Date.now();
      let ws: WebSocket;
      try {
        ws = new WebSocket(socketUrl());
      } catch {
        noteFailure();
        retryLater();
        return;
      }
      socket = ws;
      let heard = false;
      // Also bounds an attempt that never gets through.
      schedule(SILENCE_TIMEOUT_MS, fail);

      ws.onmessage = (event: MessageEvent<unknown>) => {
        if (ws !== socket) return;
        lastMessageAt = Date.now();
        schedule(SILENCE_TIMEOUT_MS, fail);
        if (!heard) {
          // Only a connection that has delivered something earns a backoff reset.
          heard = true;
          failures = 0;
        }
        if (!isLive) {
          isLive = true;
          setLive({ status: "live", failures: 0, lastMessageAt });
        }
        const message = WsMessageSchema.safeParse(typeof event.data === "string" ? parseJson(event.data) : undefined);
        if (!message.success) {
          console.warn("Ignoring a WebSocket message that doesn't match the API contract", message.error.issues);
          return;
        }
        if (message.data.type === "state") writeState(qc, message.data.data, lastMessageAt);
      };
      ws.onclose = () => {
        if (ws === socket) fail();
      };
      // No onerror: an error is always followed by a close event, which does the reconnecting.
    }

    // Phones suspend timers and sockets in the background, so on return the watchdog may never
    // have fired. Check the silence now, and skip whatever is left of a backoff wait.
    const onWake = () => {
      if (stopped || document.visibilityState !== "visible") return;
      if (socket && Date.now() - Math.max(lastMessageAt, attemptStartedAt) < SILENCE_TIMEOUT_MS) return;
      if (socket) {
        drop();
        noteFailure();
      }
      connect();
    };

    connect();
    document.addEventListener("visibilitychange", onWake);
    window.addEventListener("online", onWake);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("online", onWake);
      drop();
    };
  }, [qc]);

  return live;
}
