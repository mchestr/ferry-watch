import type { z } from "zod";
import { ErrorBodySchema, TerminalListSchema, type Terminal } from "./schema";

export const REQUEST_TIMEOUT_MS = 15_000;
export const SESSION_EXPIRED_MESSAGE = "Your sign-in expired";

export type ApiErrorKind =
  /** Signed out: the proxy redirected to its login page (an opaque redirect) or answered 401. */
  | "auth"
  /** The server answered with an error status. */
  | "http"
  /** No response within REQUEST_TIMEOUT_MS. */
  | "timeout"
  /** No response at all: offline, DNS, connection refused. */
  | "network"
  /** A 2xx response that isn't JSON. */
  | "format"
  /** JSON that doesn't match the API contract. */
  | "schema";

/** Why the server refused a session: sign in again quietly, or show the signed-out screen. */
export type AuthReason = "missing" | "expired" | "reverify" | "revoked";

export class ApiError extends Error {
  override readonly name = "ApiError";
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  /** For a 400: the request field that failed validation, when the server names one. */
  readonly field: string | null;
  /** For a 401 from waitlist-web: why the session doesn't work. */
  readonly reason: AuthReason | null;

  constructor(
    message: string,
    kind: ApiErrorKind,
    details: { status?: number | null; field?: string | null; reason?: AuthReason | null } = {},
  ) {
    super(message);
    this.kind = kind;
    this.status = details.status ?? null;
    this.field = details.field ?? null;
    this.reason = details.reason ?? null;
  }
}

/** The bearer token for every request (waitlist-web's sign-in, mt/auth.ts); none until it's configured. */
let bearerToken: () => string | null = () => null;
let onUnauthorized: (reason: AuthReason) => void = () => undefined;

export function configureBearerAuth(token: () => string | null, unauthorized: (reason: AuthReason) => void): void {
  bearerToken = token;
  onUnauthorized = unauthorized;
}

const AUTH_REASONS: readonly AuthReason[] = ["missing", "expired", "reverify", "revoked"];

function authReason(text: string): AuthReason {
  const body = parseJson(text);
  const reason = typeof body === "object" && body !== null ? (body as { reason?: unknown }).reason : undefined;
  return AUTH_REASONS.find((r) => r === reason) ?? "missing";
}

export const isAuthError = (error: unknown): boolean => error instanceof ApiError && error.kind === "auth";

export const hasStatus = (error: unknown, status: number): error is ApiError =>
  error instanceof ApiError && error.status === status;

export function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "Something went wrong";
}

type Method = "GET" | "POST" | "PATCH" | "DELETE";

interface RequestOptions<T> {
  method?: Method;
  /** Sent as JSON. Mutations always send a body (`{}` when there's nothing to say). */
  body?: unknown;
  /** Validates the response; nothing reaches the app unparsed. */
  schema: z.ZodType<T, z.ZodTypeDef, unknown>;
  /** Cancels the request (TanStack Query passes one). Cancellation rejects with the AbortError, not an ApiError. */
  signal?: AbortSignal;
  timeoutMs?: number;
}

/** Fetches a JSON endpoint and validates the response. Every failure is an ApiError, except cancellation. */
export async function request<T>(path: string, options: RequestOptions<T>): Promise<T> {
  const { method = "GET", body, schema, signal, timeoutMs = REQUEST_TIMEOUT_MS } = options;
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const forwardAbort = () => {
    controller.abort();
  };
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", forwardAbort);

  // A rejected fetch or body read: our timeout, the caller cancelling, or no connection.
  const failure = (cause: unknown): unknown => {
    if (timedOut) return new ApiError("The server took too long to respond", "timeout");
    if (signal?.aborted) return cause;
    return new ApiError("Can't reach the server", "network");
  };

  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const token = bearerToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    let res: Response;
    try {
      res = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "same-origin",
        // The auth proxy redirects a signed-out session to its login page. Don't follow it: report it.
        redirect: "manual",
        signal: controller.signal,
      });
    } catch (cause) {
      throw failure(cause);
    }

    if (res.type === "opaqueredirect") {
      throw new ApiError(SESSION_EXPIRED_MESSAGE, "auth", { status: null });
    }
    if (res.status === 401) {
      const reason = authReason(await res.text().catch(() => ""));
      onUnauthorized(reason);
      throw new ApiError(SESSION_EXPIRED_MESSAGE, "auth", { status: 401, reason });
    }

    let text: string;
    try {
      text = await res.text();
    } catch (cause) {
      throw failure(cause);
    }

    if (!res.ok) throw errorFromResponse(res.status, text);
    if (!isJsonContentType(res.headers.get("Content-Type"))) {
      throw new ApiError("Unexpected response from the server", "format", { status: res.status });
    }
    const json = parseJson(text);
    if (json === undefined) throw new ApiError("The server sent malformed JSON", "format", { status: res.status });

    const parsed = schema.safeParse(json);
    if (!parsed.success) {
      console.error(`${method} ${path}: the response doesn't match the API contract`, parsed.error.issues);
      throw new ApiError(`Unexpected data from the server (${describeIssues(parsed.error)})`, "schema", {
        status: res.status,
      });
    }
    return parsed.data;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

function errorFromResponse(status: number, text: string): ApiError {
  const body = ErrorBodySchema.safeParse(parseJson(text));
  return body.success
    ? new ApiError(body.data.error, "http", { status, field: body.data.field ?? null })
    : new ApiError(`HTTP ${status}`, "http", { status });
}

function isJsonContentType(value: string | null): boolean {
  return /^application\/(?:[\w.+-]+\+)?json\b/i.test(value ?? "");
}

/** The parsed value, or undefined when `text` isn't JSON. */
export function parseJson(text: string): unknown {
  try {
    const value: unknown = JSON.parse(text);
    return value;
  } catch {
    return undefined;
  }
}

/** "trips.0.sailings.2.depart: expected HH:MM (and 1 more)" */
export function describeIssues(error: z.ZodError): string {
  const [first, ...rest] = error.issues;
  if (!first) return "invalid";
  const where = first.path.length ? `${first.path.join(".")}: ` : "";
  return `${where}${first.message}${rest.length ? ` (and ${rest.length} more)` : ""}`;
}

/** BC Ferries' terminals, as the server lists them (GET /api/terminals). */
export async function fetchTerminals(signal?: AbortSignal): Promise<Terminal[]> {
  return (await request("/api/terminals", { schema: TerminalListSchema, signal })).terminals;
}
