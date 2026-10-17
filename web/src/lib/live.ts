/** Where the server's WebSocket is, and how long to wait between attempts to reach it (mt/live.ts). */
export const BASE_BACKOFF_MS = 1_000;
export const MAX_BACKOFF_MS = 15_000;

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
