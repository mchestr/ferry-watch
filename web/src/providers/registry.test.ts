import { describe, expect, it } from "vitest";
import { parsedState } from "@/test/fixtures";
import { providerUi, uiFor } from "./registry";

describe("the provider registry", () => {
  it("knows BC Ferries and Buntzen Lake", () => {
    expect(providerUi("bcferries")?.kind).toBe("bcferries");
    expect(providerUi("buntzen")?.kind).toBe("buntzen");
  });

  it("is a Map: an id that's a property of every object resolves to nothing", () => {
    for (const id of ["constructor", "__proto__", "toString", "hasOwnProperty", "bcparks", ""]) {
      expect(providerUi(id)).toBeNull();
    }
  });

  it("gives an unknown watch no UI, so it's shown as unknown", () => {
    const kinds = parsedState().watches.map((w) => uiFor(w)?.kind ?? null);
    expect(kinds).toEqual(["buntzen", "buntzen", "bcferries", null]);
  });
});
