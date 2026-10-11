import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { Toaster } from "sonner";
import { describe, expect, it } from "vitest";
import { makeState } from "@/test/fixtures";
import { createTestQueryClient, deferred, jsonResponse, mockFetch, opaqueRedirect } from "@/test/render";
import { api } from "./api";
import { useStateMutation } from "./hooks";
import { STATE_KEY, readSnapshot, writeState } from "./state";

function setup() {
  const qc = createTestQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      {children}
      <Toaster />
    </QueryClientProvider>
  );
  return { qc, wrapper };
}

describe("useStateMutation", () => {
  it("writes the response through the seq rule and toasts", async () => {
    const { qc, wrapper } = setup();
    writeState(qc, makeState({ seq: 9 }));
    mockFetch(() => jsonResponse(makeState({ seq: 8, enabled: false })));
    const { result } = renderHook(() => useStateMutation(() => api.checkNow(), { success: "Checking now…" }), {
      wrapper,
    });
    act(() => result.current.mutate());
    expect(await screen.findByText("Checking now…")).toBeInTheDocument();
    // The response was older than what a push had already delivered, so it was dropped.
    expect(readSnapshot(qc)?.state.seq).toBe(9);
  });

  it("sends one request at a time, even when triggered twice before re-rendering", async () => {
    const { wrapper } = setup();
    const response = deferred<Response>();
    const { requests } = mockFetch(() => response.promise);
    const { result } = renderHook(() => useStateMutation(() => api.testAlert()), { wrapper });
    act(() => {
      result.current.mutate();
      result.current.mutate();
    });
    await waitFor(() => expect(requests).toHaveLength(1));
    response.resolve(jsonResponse(makeState()));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(requests).toHaveLength(1);

    mockFetch(() => jsonResponse(makeState()));
    act(() => result.current.mutate());
    await waitFor(() => expect(result.current.isPending).toBe(false));
    expect(result.current.isSuccess).toBe(true);
  });

  it("toasts the server's message on failure, unless the caller handles it", async () => {
    const { wrapper } = setup();
    mockFetch(() => jsonResponse({ error: "Test alert sent too recently", field: null }, 429));
    const { result } = renderHook(() => useStateMutation(() => api.testAlert()), { wrapper });
    act(() => result.current.mutate());
    expect(await screen.findByText("Test alert sent too recently")).toBeInTheDocument();

    const handled = renderHook(() => useStateMutation(() => api.testAlert(), { onError: () => true }), { wrapper });
    act(() => handled.result.current.mutate());
    await waitFor(() => expect(handled.result.current.isError).toBe(true));
    expect(screen.getAllByText("Test alert sent too recently")).toHaveLength(1);
  });

  it("answers an expired sign-in by refetching the state (which raises the banner), not with a toast", async () => {
    const { qc, wrapper } = setup();
    writeState(qc, makeState());
    mockFetch(() => opaqueRedirect());
    const { result } = renderHook(() => useStateMutation(() => api.checkNow()), { wrapper });
    act(() => result.current.mutate());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(qc.getQueryState(STATE_KEY)?.isInvalidated).toBe(true);
    expect(screen.queryByText("Your sign-in expired")).not.toBeInTheDocument();
  });
});
