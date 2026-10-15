import { describe, expect, it } from "vitest";
import data from "./route-map-data.json";
import { berth, decode, routePath, type RouteMapData } from "./route-map";

/** BC Ferries' terminals and where each books to (its booking form on 2026-10-11: tests/fixtures/flow/route.html). */
const BOOKABLE: Record<string, string> = {
  TSA: "DUK NAN PLH POB PSB PST PVB SWB",
  HSB: "BOW LNG NAN",
  BOW: "HSB",
  SWB: "FUL PLH POB PSB PST PVB TSA",
  NAH: "GAB",
  NAN: "HSB",
  DUK: "TSA",
  CMX: "PWR",
  PPH: "BEC KLE PBB POF PPR SHW",
  BTW: "MIL",
  BKY: "DNM",
  CAM: "QDR",
  CHM: "PEN THT",
  CFT: "VES",
  MIL: "BTW",
  MCN: "ALR SOI",
  LNG: "HSB",
  PWR: "CMX TEX",
  SLT: "ERL",
  ERL: "SLT",
  TEX: "PWR",
  POB: "PLH PSB PST PVB SWB TSA",
  PSB: "PLH POB PST PVB SWB TSA",
  PVB: "PLH POB PSB PST SWB TSA",
  PST: "PLH POB PSB PVB SWB TSA",
  GAB: "NAH",
  PEN: "CHM THT",
  PLH: "POB PSB PST PVB SWB TSA",
  FUL: "SWB",
  VES: "CFT",
  THT: "CHM PEN",
  ALR: "MCN SOI",
  COR: "HRB",
  DNM: "BKY",
  DNE: "HRN",
  HRN: "DNE",
  SOI: "ALR MCN",
  HRB: "COR",
  QDR: "CAM",
  BEC: "PBB POF PPH SHW",
  PBB: "BEC KLE POF PPH PPR SHW",
  POF: "BEC PBB PPH SHW",
  SHW: "BEC PBB POF PPH",
  PPR: "KLE PBB PPH PSK",
  KLE: "PBB PPH PPR",
  PSK: "ALF PPR",
  ALF: "PSK",
};

const map: RouteMapData = data;
const [width = 0, height = 0] = map.size;
const onMap = ([x, y]: readonly [number, number]) => x >= 0 && y >= 0 && x <= width && y <= height;

describe("the route map's data", () => {
  it("has a berth on the map for every terminal", () => {
    expect(Object.keys(BOOKABLE)).toHaveLength(47);
    for (const code of Object.keys(BOOKABLE)) {
      const at = berth(map, code);
      expect(at, code).toBeDefined();
      if (at) expect(onMap(at), code).toBe(true);
    }
  });

  it("has a path for every route BC Ferries books, from one berth to the other", () => {
    for (const [from, dests] of Object.entries(BOOKABLE)) {
      for (const to of dests.split(" ")) {
        const path = routePath(map, from, to);
        expect(path, `${from}-${to}`).toBeDefined();
        if (!path) continue;
        expect(path[0], `${from}-${to}`).toEqual(berth(map, from));
        expect(path[path.length - 1], `${from}-${to}`).toEqual(berth(map, to));
        expect(path.every(onMap), `${from}-${to}`).toBe(true);
      }
    }
  });

  it("keeps the coast on the map", () => {
    expect(map.land.length).toBeGreaterThan(100);
    expect(map.land.every((ring) => decode(ring).every(onMap))).toBe(true);
  });
});
