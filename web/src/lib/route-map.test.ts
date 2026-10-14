import { describe, expect, it } from "vitest";
import {
  FRAME,
  decode,
  fitView,
  linePath,
  placeLabel,
  routePath,
  routeScene,
  smoothRing,
  toScreen,
  type RouteMapData,
} from "./route-map";
import type { Terminal } from "./schema";

/** A made-up map: three terminals in a row, with routes A-B and B-C. */
const DATA: RouteMapData = {
  zoom: 13,
  origin: [0, 0],
  size: [10_000, 10_000],
  land: [[4000, 4000, 100, 0, 0, 100, -100, 0]],
  terminals: { AAA: [1000, 5000], BBB: [3000, 5000], CCC: [5000, 5000] },
  routes: { "AAA-BBB": [1000, 5000, 1000, -500, 1000, 500], "BBB-CCC": [3000, 5000, 2000, 0] },
};

const terminal = (code: string, place: string, dests: string[]): Terminal => ({
  code,
  label: `${place} (${code})`,
  place,
  area: "Somewhere",
  dests,
});

const TERMINALS = [
  terminal("AAA", "Alpha", ["BBB"]),
  terminal("BBB", "Bravo", ["AAA", "CCC", "ZZZ"]),
  terminal("CCC", "Charlie", ["BBB"]),
  terminal("ZZZ", "Zulu", ["BBB"]), // BC Ferries knows it; the map doesn't
];

describe("decode", () => {
  it("adds up the steps", () => {
    expect(decode([10, 20, 1, -2, 3, 4])).toEqual([
      [10, 20],
      [11, 18],
      [14, 22],
    ]);
  });
});

describe("routePath", () => {
  it("runs from the departure to the arrival whichever way it's stored", () => {
    expect(routePath(DATA, "AAA", "BBB")).toEqual([
      [1000, 5000],
      [2000, 4500],
      [3000, 5000],
    ]);
    expect(routePath(DATA, "BBB", "AAA")).toEqual([
      [3000, 5000],
      [2000, 4500],
      [1000, 5000],
    ]);
  });

  it("is undefined for a route the map doesn't have", () => {
    expect(routePath(DATA, "AAA", "CCC")).toBeUndefined();
  });
});

describe("smoothRing", () => {
  it("curves through the edges' midpoints with the points as control points", () => {
    expect(
      smoothRing([
        [0, 0],
        [10, 0],
        [10, 10],
      ]),
    ).toBe("M5 5Q0 0 5 0Q10 0 10 5Q10 10 5 5Z");
  });

  it("passes straight through a doubled point", () => {
    expect(
      smoothRing([
        [0, 0],
        [10, 0],
        [10, 0],
        [10, 10],
      ]),
    ).toContain("Q10 0 10 0Q10 0 10 5");
  });
});

describe("fitView", () => {
  const frame = { width: 400, height: 200, padding: 20, minSpan: 100 };

  it("fits the box inside the padding, centred", () => {
    const view = fitView({ left: 1000, top: 1000, right: 1720, bottom: 1080 }, frame);
    // The width limits it: 360 / 720.
    expect(view.scale).toBe(0.5);
    expect(toScreen(view, [1000, 1000])).toEqual([20, 80]);
    expect(toScreen(view, [1720, 1080])).toEqual([380, 120]);
  });

  it("uses whichever side is tighter", () => {
    const view = fitView({ left: 0, top: 0, right: 100, bottom: 320 }, frame);
    expect(view.scale).toBe(0.5); // 160 / 320
    expect(toScreen(view, [50, 160])).toEqual([200, 100]);
  });

  it("doesn't zoom in past the minimum span", () => {
    const view = fitView({ left: 500, top: 500, right: 510, bottom: 505 }, { ...frame, minSpan: 800 });
    expect(view.scale).toBe(0.5); // 400 across shows 800
    expect(toScreen(view, [505, 502.5])).toEqual([200, 100]);
  });

  it("stays inside the map's limits when it can", () => {
    const limits = { left: 0, top: 0, right: 2000, bottom: 2000 };
    const view = fitView({ left: 0, top: 0, right: 10, bottom: 10 }, { ...frame, minSpan: 800 }, limits);
    expect(toScreen(view, [0, 0])).toEqual([0, 0]);
    // Too big to fit: left centred.
    const wide = fitView({ left: 0, top: 0, right: 4000, bottom: 10 }, frame, limits);
    expect(toScreen(wide, [2000, 5])[0]).toBe(200);
  });
});

describe("linePath", () => {
  it("rounds to a tenth", () => {
    expect(
      linePath([
        [1.234, 5.678],
        [9, 10.05],
      ]),
    ).toBe("M1.2 5.7L9 10.1");
  });
});

describe("placeLabel", () => {
  const frame = { width: 400, height: 200 };
  const options = { fontSize: 10, gap: 8 };

  it("faces away from the other end", () => {
    expect(placeLabel("Alpha", [200, 100], frame, { ...options, away: [300, 100] }).anchor).toBe("end");
    expect(placeLabel("Alpha", [200, 100], frame, { ...options, away: [100, 100] }).anchor).toBe("start");
  });

  it("stays inside the frame", () => {
    const { box } = placeLabel("A long place name", [395, 100], frame, { ...options, away: [300, 100] });
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(400);
  });

  it("keeps clear of what it's told to avoid", () => {
    const avoid = [{ left: 200, top: 0, right: 400, bottom: 200 }];
    const { box } = placeLabel("Alpha", [200, 100], frame, { ...options, avoid, away: [100, 100] });
    expect(box.right).toBeLessThanOrEqual(200);
  });
});

describe("routeScene", () => {
  it("draws the route between two terminals, labelled with their places", () => {
    const scene = routeScene(DATA, "BBB", "AAA", TERMINALS);
    expect(scene?.description).toBe("Map of the route from Bravo to Alpha");
    expect(scene?.from.place).toBe("Bravo");
    expect(scene?.to?.place).toBe("Alpha");
    expect(scene?.route).toMatch(/^M.*L.*L/);
    // Both ends are inside the frame's padding.
    for (const stop of [scene?.from, scene?.to]) {
      expect(stop?.at[0]).toBeGreaterThanOrEqual(FRAME.padding - 0.01);
      expect(stop?.at[0]).toBeLessThanOrEqual(FRAME.width - FRAME.padding + 0.01);
    }
  });

  it("shows where the departure goes when there's no arrival yet", () => {
    const scene = routeScene(DATA, "BBB", "", TERMINALS);
    expect(scene?.description).toBe("Map of the routes from Bravo");
    expect(scene?.to).toBeUndefined();
    // Zulu has no berth on the map, so it's left out.
    expect(scene?.others.map((stop) => stop.code)).toEqual(["AAA", "CCC"]);
  });

  it("is undefined when the map doesn't know a terminal", () => {
    expect(routeScene(DATA, "ZZZ", "BBB", TERMINALS)).toBeUndefined();
    expect(routeScene(DATA, "BBB", "ZZZ", TERMINALS)).toBeUndefined();
  });

  it("shows two known terminals without a line when the map has no route between them", () => {
    const scene = routeScene(DATA, "AAA", "CCC", TERMINALS);
    expect(scene?.to?.place).toBe("Charlie");
    expect(scene?.route).toBeUndefined();
  });
});
