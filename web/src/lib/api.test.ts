import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { makeSailing, makeSettings, TERMINALS } from "@/test/fixtures";
import { jsonResponse, mockFetch, opaqueRedirect } from "@/test/render";
import {
  ApiError,
  REQUEST_TIMEOUT_MS,
  SESSION_EXPIRED_MESSAGE,
  describeIssues,
  fetchTerminals,
  isAuthError,
  request,
} from "./api";
import { SailingSchema } from "./schema";

const TripsSchema = z.object({ trips: z.array(z.object({ id: z.string(), sailings: z.array(SailingSchema) })) });
const getTrips = (signal?: AbortSignal) => request("/api/state", { schema: TripsSchema, signal });

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => {
      throw new Error("expected the request to fail");
    },
    (reason: unknown) => reason,
  );
  if (!(error instanceof ApiError)) throw new Error(`expected an ApiError, got ${String(error)}`);
  return error;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("request", () => {
  it("parses and validates a JSON response", async () => {
    mockFetch(() => jsonResponse({ terminals: TERMINALS }));
    await expect(fetchTerminals()).resolves.toEqual(TERMINALS);
  });

  it("sends mutations as JSON, without following redirects", async () => {
    const { requests, fetchMock } = mockFetch(() => jsonResponse({ trips: [] }));
    await request("/api/trips/t1", { method: "PATCH", body: { latest: "21:00" }, schema: TripsSchema });
    expect(requests).toEqual([
      expect.objectContaining({ method: "PATCH", url: "/api/trips/t1", body: { latest: "21:00" } }),
    ]);
    expect(requests[0]?.headers.get("Content-Type")).toBe("application/json");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ redirect: "manual", cache: "no-store" });
  });

  it("uses the server's JSON error message and field", async () => {
    mockFetch(() => jsonResponse({ error: "Choose a date that hasn't passed", field: "date" }, 400));
    const error = await failure(request("/api/trips", { method: "POST", body: makeSettings(), schema: TripsSchema }));
    expect(error).toMatchObject({
      kind: "http",
      status: 400,
      field: "date",
      message: "Choose a date that hasn't passed",
    });
  });

  it("falls back to 'HTTP <status>' for a non-JSON error page", async () => {
    mockFetch(
      () =>
        new Response("<html><body>502 Bad Gateway</body></html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        }),
    );
    const error = await failure(getTrips());
    expect(error).toMatchObject({ kind: "http", status: 502, message: "HTTP 502", field: null });
  });

  it("falls back to 'HTTP <status>' when the JSON has no usable error", async () => {
    mockFetch(() => jsonResponse({ detail: "nope" }, 500));
    expect((await failure(getTrips())).message).toBe("HTTP 500");
  });

  it("treats an opaque redirect (the auth proxy's login page) as an expired sign-in", async () => {
    mockFetch(() => opaqueRedirect());
    const error = await failure(getTrips());
    expect(error).toMatchObject({ kind: "auth", status: null, message: SESSION_EXPIRED_MESSAGE });
    expect(isAuthError(error)).toBe(true);
  });

  it("treats a 401 as an expired sign-in", async () => {
    mockFetch(() => new Response("Unauthorized", { status: 401 }));
    expect(await failure(getTrips())).toMatchObject({ kind: "auth", status: 401 });
  });

  it("rejects a 2xx response that isn't JSON", async () => {
    mockFetch(
      () => new Response("<!doctype html><title>Sign in</title>", { headers: { "Content-Type": "text/html" } }),
    );
    expect(await failure(getTrips())).toMatchObject({ kind: "format", status: 200 });
  });

  it("rejects malformed JSON", async () => {
    mockFetch(() => new Response("{", { headers: { "Content-Type": "application/json" } }));
    expect(await failure(getTrips())).toMatchObject({ kind: "format" });
  });

  it("rejects JSON that breaks the contract, saying where", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    mockFetch(() => jsonResponse({ trips: [{ id: "t1", sailings: [makeSailing(), { depart: "7:00 am" }] }] }));
    const error = await failure(getTrips());
    expect(error.kind).toBe("schema");
    expect(error.message).toMatch(/^Unexpected data from the server \(trips\.0\.sailings\.1\.depart: expected HH:MM/);
  });

  it("times out after 15 s, aborting the request", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | null | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            signal = init.signal;
            init.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            });
          }),
      ),
    );
    const result = failure(getTrips());
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS - 1);
    expect(signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await result).toMatchObject({ kind: "timeout", message: "The server took too long to respond" });
    expect(signal?.aborted).toBe(true);
  });

  it("reports a network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    expect(await failure(getTrips())).toMatchObject({ kind: "network", message: "Can't reach the server" });
  });

  it("leaves a caller's cancellation as an AbortError, not an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            });
          }),
      ),
    );
    const controller = new AbortController();
    const pending = getTrips(controller.signal);
    controller.abort();
    const reason: unknown = await pending.catch((error: unknown) => error);
    expect(reason).toBeInstanceOf(DOMException);
    expect(reason).toMatchObject({ name: "AbortError" });
  });
});

describe("describeIssues", () => {
  it("names the first problem and counts the rest", () => {
    const result = z.object({ seq: z.number(), enabled: z.boolean() }).safeParse({ seq: "1", enabled: "yes" });
    expect(result.success).toBe(false);
    if (!result.success)
      expect(describeIssues(result.error)).toBe("seq: Expected number, received string (and 1 more)");
  });
});
