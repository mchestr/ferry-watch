import { focusManager, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TERMINALS } from "@/test/fixtures";
import { jsonResponse, mockFetch } from "@/test/render";
import { TERMINALS_STALE_MS, useTerminals } from "./hooks";
import { createQueryClient } from "./query";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the terminal list", () => {
  it("is fetched again when the app comes back into focus an hour later, not before", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const { requests } = mockFetch(() => jsonResponse({ terminals: TERMINALS }));
    const queryClient = createQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useTerminals(), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(TERMINALS));
    const fetched = () => requests.filter((r) => r.url === "/api/terminals").length;
    expect(fetched()).toBe(1);

    const refocus = () =>
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
    vi.setSystemTime(Date.now() + TERMINALS_STALE_MS - 1_000);
    refocus();
    expect(fetched()).toBe(1);
    vi.setSystemTime(Date.now() + 2_000);
    refocus();
    await waitFor(() => expect(fetched()).toBe(2));
    focusManager.setFocused(undefined);
  });
});
