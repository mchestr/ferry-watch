import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { Toaster } from "sonner";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

/** Renders inside the app's providers. `rerender` keeps them. */
export function renderWithProviders(
  ui: ReactElement,
  {
    queryClient = createTestQueryClient(),
    ...options
  }: { queryClient?: QueryClient } & Omit<RenderOptions, "wrapper"> = {},
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        {children}
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
  return { user: userEvent.setup(), queryClient, ...render(ui, { wrapper, ...options }) };
}

export interface RecordedRequest {
  method: string;
  url: string;
  /** The parsed JSON body, if one was sent. */
  body: unknown;
  headers: Headers;
}

type Handler = (request: RecordedRequest) => Response | Promise<Response>;

/** Replaces fetch with `handler` and records every request made. */
export function mockFetch(handler: Handler) {
  const requests: RecordedRequest[] = [];
  const fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    const request = { method: init.method ?? "GET", url, body, headers: new Headers(init.headers) };
    requests.push(request);
    return Promise.resolve(handler(request));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, requests };
}

export function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

/** What fetch gives back for a redirect when called with redirect: "manual". */
export function opaqueRedirect(): Response {
  const response = new Response(null, { status: 200 });
  Object.defineProperty(response, "type", { value: "opaqueredirect" });
  Object.defineProperty(response, "status", { value: 0 });
  Object.defineProperty(response, "ok", { value: false });
  return response;
}

/** A promise you settle from the test. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
