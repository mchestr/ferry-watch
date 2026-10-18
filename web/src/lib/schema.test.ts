import { describe, expect, it } from "vitest";
import { makeSailing, makeSettings, TERMINALS } from "@/test/fixtures";
import { SailingSchema, TerminalListSchema, TripSettingsSchema } from "./schema";

describe("schemas", () => {
  it("accept what the server sends (and the test fixtures follow the contract)", () => {
    expect(TripSettingsSchema.parse(makeSettings())).toEqual(makeSettings());
    expect(SailingSchema.parse(makeSailing())).toEqual(makeSailing());
    expect(TerminalListSchema.parse({ terminals: TERMINALS }).terminals).toEqual(TERMINALS);
  });

  it("strip unknown fields, so the server can add some", () => {
    const parsed = TripSettingsSchema.parse({ ...makeSettings(), added_later: true });
    expect(parsed).not.toHaveProperty("added_later");
  });

  it("read statuses this build doesn't know as unknown, instead of rejecting the response", () => {
    // What a newer server might send: a value this build has never heard of.
    expect(SailingSchema.parse(makeSailing({ status: "waitlist" as never })).status).toBe("unknown");
  });

  it("keep the enums the page sends back strict", () => {
    expect(TripSettingsSchema.safeParse(makeSettings({ vehicle: "bus" as never })).success).toBe(false);
  });

  it("reject times and dates in the wrong format", () => {
    expect(TripSettingsSchema.safeParse(makeSettings({ earliest: "7:00 am" })).success).toBe(false);
    expect(TripSettingsSchema.safeParse(makeSettings({ date: "Oct 12" })).success).toBe(false);
  });
});
