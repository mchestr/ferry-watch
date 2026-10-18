import { describe, expect, it } from "vitest";
import { makeSailing } from "@/test/fixtures";
import { isBookable, removeConsequence } from "./trip";

describe("isBookable", () => {
  it("is an available sailing in the departure window", () => {
    expect(isBookable(makeSailing({ status: "available" }))).toBe(true);
    expect(isBookable(makeSailing({ status: "available", in_window: false }))).toBe(false);
    expect(isBookable(makeSailing({ status: "sold_out" }))).toBe(false);
    expect(isBookable(makeSailing({ status: "unavailable" }))).toBe(false);
  });
});

describe("carts", () => {
  it("names the held sailing when removing would release it", () => {
    expect(removeConsequence({ cart: { status: "ready", depart: "07:00" } })).toBe(
      "Removing it releases the 7:00 am sailing in your cart.",
    );
    expect(removeConsequence({ cart: { status: "adding", depart: "15:30" } })).toBe(
      "Removing it stops adding the 3:30 pm sailing to your cart.",
    );
    expect(removeConsequence({ cart: { status: "failed", depart: "07:00" } })).toBe(
      "You'll stop getting alerts for it.",
    );
    expect(removeConsequence({ cart: null })).toBe("You'll stop getting alerts for it.");
  });
});
