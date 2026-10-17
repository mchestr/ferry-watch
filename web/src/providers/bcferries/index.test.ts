import { describe, expect, it } from "vitest";
import { asWatch, fail, makeSettings, parsedState, tripsOf } from "@/test/fixtures";
import { bcferriesUi } from "./index";

describe("a BC Ferries watch's title", () => {
  // As GET /api/terminals sends them: a label is BC Ferries' longer "City - Name (Place)".
  const terminals = [
    { code: "SWB", label: "Sidney - Victoria (Swartz Bay)", place: "Swartz Bay", area: "Victoria", dests: ["TSA"] },
    { code: "TSA", label: "Delta - Vancouver (Tsawwassen)", place: "Tsawwassen", area: "Vancouver", dests: ["SWB"] },
  ];
  const base = tripsOf(parsedState())[0] ?? fail();
  const watch = asWatch({
    ...base,
    trip: { ...makeSettings({ from: "SWB", to: "TSA", date: "2026-10-23" }), id: base.id },
  });

  it("names the terminals themselves, as the card and alerts do", () => {
    expect(bcferriesUi.title(watch, { terminals })).toBe("Swartz Bay → Tsawwassen, Fri Oct 23");
  });

  it("falls back to the codes until the list has loaded", () => {
    expect(bcferriesUi.title(watch, {})).toBe("SWB → TSA, Fri Oct 23");
  });
});
