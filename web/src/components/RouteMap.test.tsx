import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ROUTE_MAP_KEY } from "@/lib/hooks";
import { TERMINALS } from "@/test/fixtures";
import { createTestQueryClient, renderWithProviders } from "@/test/render";
import { RouteMap } from "./RouteMap";

describe("RouteMap", () => {
  it("loads the map and draws the route between the two terminals", async () => {
    const { container } = renderWithProviders(<RouteMap from="TSA" to="SWB" terminals={TERMINALS} />);
    const map = await screen.findByRole("img", { name: "Map of the route from Tsawwassen to Swartz Bay" });
    expect(within(map).getByText("Tsawwassen")).toBeInTheDocument();
    expect(within(map).getByText("Swartz Bay")).toBeInTheDocument();
    // The coast, then the route's casing, its line and its moving dashes.
    expect(container.querySelectorAll("path")).toHaveLength(4);
  });

  it("shows everywhere the departure goes before there's an arrival", async () => {
    renderWithProviders(<RouteMap from="TSA" to="" terminals={TERMINALS} />);
    const map = await screen.findByRole("img", { name: "Map of the routes from Tsawwassen" });
    expect(within(map).getByText("Tsawwassen")).toBeInTheDocument();
    // TSA's destinations in the fixture: Swartz Bay and Duke Point, each a dot.
    expect(map.querySelectorAll("circle")).toHaveLength(3);
  });

  it("draws nothing for a terminal the map doesn't know", async () => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(ROUTE_MAP_KEY, await import("@/lib/route-map"));
    const { container } = renderWithProviders(<RouteMap from="DEP" to="HSB" terminals={TERMINALS} />, {
      queryClient,
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("holds the map's space while it loads", async () => {
    const { container } = renderWithProviders(<RouteMap from="TSA" to="SWB" terminals={TERMINALS} />);
    expect(container.querySelector("[aria-hidden]")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("img")).toBeInTheDocument());
  });
});
