import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { State } from "@/lib/schema";
import { STATE_KEY, fetchSnapshot, writeState } from "@/lib/state";
import { makeCart, makeState, makeTrip } from "@/test/fixtures";
import { createTestQueryClient, jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { CartBanner } from "./CartBanner";

/** Reads the trip from the state query, as the app does, so pushes and refetches show up. */
function Harness() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: STATE_KEY,
    queryFn: ({ signal }) => fetchSnapshot(qc, signal),
    staleTime: Infinity,
  });
  const trip = data?.state.trips[0];
  return trip ? <CartBanner trip={trip} checkoutUrl={data.state.checkout_url} skew={data.skew} /> : null;
}

function renderBanner(state: State) {
  const queryClient: QueryClient = createTestQueryClient();
  writeState(queryClient, state);
  return {
    ...renderWithProviders(<Harness />, { queryClient }),
    push: (next: State) => act(() => writeState(queryClient, next)),
  };
}

const withCart = (seq: number, cart: Parameters<typeof makeCart>[0] | null) =>
  makeState({ seq, trips: [makeTrip({ cart: cart === null ? null : makeCart(cart) })] });

describe("CartBanner", () => {
  it("offers checkout and a hint for a ready cart", () => {
    renderBanner(withCart(1, { status: "ready", depart: "07:00" }));
    expect(screen.getByText("The 7:00 am sailing is in your cart")).toBeInTheDocument();
    expect(screen.getByText("Booked it? Pause or remove this trip so it doesn't hold another.")).toBeInTheDocument();
    const checkout = screen.getByRole("link", { name: "Open checkout" });
    expect(checkout).toHaveAttribute("href", "https://ferry.example/vnc/vnc.html");
    expect(checkout).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("says 'Releasing…' until a pushed state no longer has that cart", async () => {
    // The server accepts the release, but the cart is still there in its response.
    const { requests } = mockFetch(() => jsonResponse(withCart(2, { id: "cart-1" })));
    const { user, push } = renderBanner(withCart(1, { id: "cart-1" }));
    await user.click(screen.getByRole("button", { name: "Release" }));
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({
      method: "POST",
      url: "/api/trips/t1/cart/release",
      body: { cart_id: "cart-1" },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Releasing…" })).toBeDisabled());
    expect(screen.queryByText("Cart released. Watching resumes.")).not.toBeInTheDocument();

    push(withCart(3, null));
    await waitFor(() => expect(screen.queryByText(/in your cart/)).not.toBeInTheDocument());
  });

  it("confirms the release when the response shows the cart gone", async () => {
    mockFetch(() => jsonResponse(withCart(2, null)));
    const { user } = renderBanner(withCart(1, { id: "cart-1" }));
    await user.click(screen.getByRole("button", { name: "Release" }));
    expect(await screen.findByText("Cart released. Watching resumes.")).toBeInTheDocument();
    expect(screen.queryByText(/in your cart/)).not.toBeInTheDocument();
  });

  it("handles a 409 by saying the cart changed and fetching the latest state", async () => {
    const { requests } = mockFetch(({ method }) =>
      method === "POST"
        ? jsonResponse({ error: "That cart id doesn't match", field: null }, 409)
        : jsonResponse(withCart(5, null)),
    );
    const { user } = renderBanner(withCart(1, { id: "cart-1" }));
    await user.click(screen.getByRole("button", { name: "Release" }));
    expect(await screen.findByText("Your cart had already changed. Showing the latest.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/in your cart/)).not.toBeInTheDocument());
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual(["POST /api/trips/t1/cart/release", "GET /api/state"]);
    expect(screen.queryByText("That cart id doesn't match")).not.toBeInTheDocument();
  });

  it("dismisses a failed cart by id", async () => {
    const { requests } = mockFetch(() => jsonResponse(withCart(2, null)));
    const { user } = renderBanner(withCart(1, { id: "cart-7", status: "failed", error: "Sold out before checkout" }));
    expect(screen.getByText("Couldn't add the 7:00 am sailing to your cart")).toBeInTheDocument();
    expect(screen.getByText("Sold out before checkout")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({ url: "/api/trips/t1/cart/dismiss", body: { cart_id: "cart-7" } });
  });
});
