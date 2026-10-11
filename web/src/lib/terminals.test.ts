import { describe, expect, it } from "vitest";
import { TERMINALS } from "@/test/fixtures";
import { destinations, origins } from "./terminals";

const codes = (list: { code: string }[]) => list.map((t) => t.code);

describe("terminals", () => {
  it("lists origins that have a destination, by label", () => {
    expect(codes(origins(TERMINALS))).toEqual(["DEP", "DUK", "LNG", "HSB", "TSA", "SWB"]);
    expect(codes(origins([{ code: "X", label: "X", place: "X", area: "X", dests: ["NOWHERE"] }]))).toEqual([]);
  });

  it("lists bookable destinations by label, dropping codes it doesn't know", () => {
    // HSB's dests are ["LNG", "BOW", "DEP"]: BOW is unknown, and the label order puts DEP first.
    expect(codes(destinations(TERMINALS, "HSB"))).toEqual(["DEP", "LNG"]);
    expect(codes(destinations(TERMINALS, "nope"))).toEqual([]);
  });
});
