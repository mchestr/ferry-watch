import { describe, expect, it } from "vitest";
import { makeSettings } from "@/test/fixtures";
import { ApiError } from "./api";
import {
  changedSettings,
  draftFromSettings,
  inlineErrorField,
  newTripSettings,
  parseHeight,
  pickSettings,
  settingsFromDraft,
  validateDate,
  validateDraft,
  validateHeight,
  validatePassengers,
  validateWindow,
} from "./trip-form";

const TODAY = "2026-10-11";
const pax = (adults: number, children = 0, infants = 0, seniors = 0) => ({ adults, children, infants, seniors });

describe("validateWindow", () => {
  it("requires both times", () => {
    expect(validateWindow("", "")).toEqual({
      earliest: "Enter the earliest departure time.",
      latest: "Enter the latest departure time.",
    });
  });

  it("rejects times it can't parse", () => {
    expect(validateWindow("7am", "25:00")).toEqual({ earliest: "Enter a valid time.", latest: "Enter a valid time." });
  });

  it("requires earliest ≤ latest, flagging latest", () => {
    expect(validateWindow("10:00", "08:00")).toEqual({ latest: "Must be at or after the earliest time." });
    expect(validateWindow("08:00", "08:00")).toEqual({});
    expect(validateWindow("00:00", "23:59")).toEqual({});
  });
});

describe("height", () => {
  it.each([
    ["8", 8],
    ["8.5", 8.5],
    ["8,5", 8.5],
    [" 12.25 ", 12.25],
  ])("parses %j as %d ft", (raw, ft) => {
    expect(parseHeight(raw)).toBe(ft);
  });

  it.each(["", "abc", "8.", "8.123", "-9", "1e1", "8 ft"])("doesn't parse %j", (raw) => {
    expect(parseHeight(raw)).toBeNull();
  });

  it("must be over 7 ft and at most 14 ft", () => {
    expect(validateHeight("7")).toBe("Must be over 7 ft. Under that, choose Car / SUV.");
    expect(validateHeight("7.1")).toBeNull();
    expect(validateHeight("14")).toBeNull();
    expect(validateHeight("14.1")).toBe("Must be 14 ft or less.");
    expect(validateHeight("")).toBe("Enter the vehicle's height.");
    expect(validateHeight("tall")).toBe("Enter the height in feet, like 8.5.");
  });
});

describe("validatePassengers", () => {
  it("needs at least one adult or senior", () => {
    expect(validatePassengers(pax(0, 2, 1, 0))).toBe("Add at least one adult or senior.");
    expect(validatePassengers(pax(0, 0, 0, 1))).toBeNull();
    expect(validatePassengers(pax(1))).toBeNull();
  });

  it("allows at most 9 in total", () => {
    expect(validatePassengers(pax(9))).toBeNull();
    expect(validatePassengers(pax(2, 3, 2, 2))).toBeNull();
    expect(validatePassengers(pax(2, 3, 2, 3))).toBe("At most 9 passengers in total.");
  });
});

describe("validateDate", () => {
  it("requires a real date that hasn't passed", () => {
    expect(validateDate("", TODAY)).toBe("Choose a date.");
    expect(validateDate("2026-02-30", TODAY)).toBe("Enter a valid date.");
    expect(validateDate("2026-10-10", TODAY)).toBe("This date has passed.");
    expect(validateDate(TODAY, TODAY)).toBeNull();
  });
});

describe("validateDraft", () => {
  it("is empty for a valid draft", () => {
    expect(validateDraft(draftFromSettings(makeSettings()), TODAY)).toEqual({});
  });

  it("only checks the height for a tall vehicle", () => {
    const draft = { ...draftFromSettings(makeSettings()), height: "3" };
    expect(validateDraft(draft, TODAY)).toEqual({});
    expect(validateDraft({ ...draft, vehicle: "tall" }, TODAY)).toEqual({
      height: "Must be over 7 ft. Under that, choose Car / SUV.",
    });
  });

  it("needs a route with two different terminals", () => {
    const draft = draftFromSettings(makeSettings());
    expect(validateDraft({ ...draft, to: "" }, TODAY).to).toBe("Choose a destination.");
    expect(validateDraft({ ...draft, to: draft.from }, TODAY).to).toBe("Choose a different destination.");
  });
});

describe("drafts and patches", () => {
  it("picks only the settings from a trip", () => {
    const trip = { ...makeSettings(), id: "t1", title: "x", cart: null };
    const settings = pickSettings(trip);
    expect(Object.keys(settings).sort()).toEqual(Object.keys(makeSettings()).sort());
  });

  it("round-trips settings through a draft, keeping the typed height", () => {
    const base = makeSettings({ vehicle: "tall", height_ft: 9.5 });
    const draft = draftFromSettings(base);
    expect(draft.height).toBe("9.5");
    expect(settingsFromDraft(draft, base)).toEqual(base);
  });

  it("keeps the base height when the vehicle isn't tall, so it's never sent", () => {
    const base = makeSettings({ height_ft: 8 });
    const draft = { ...draftFromSettings(base), height: "12" };
    expect(settingsFromDraft(draft, base).height_ft).toBe(8);
  });

  it("patches only the fields that changed", () => {
    const base = makeSettings();
    expect(changedSettings(base, { ...base })).toEqual({});
    expect(changedSettings(base, { ...base, latest: "21:00", seniors: 1, vehicle: "tall", height_ft: 9 })).toEqual({
      latest: "21:00",
      seniors: 1,
      vehicle: "tall",
      height_ft: 9,
    });
  });

  it("starts a new trip from trip_defaults", () => {
    const defaults = makeSettings({ from: "TSA", to: "SWB", date: "2026-10-12", auto_cart: false, fare: "saver" });
    expect(newTripSettings(defaults, TODAY)).toEqual(defaults);
  });

  it("moves stale default dates to tomorrow (a page left open overnight)", () => {
    expect(newTripSettings(makeSettings({ date: "2026-10-10" }), TODAY).date).toBe("2026-10-12");
    expect(newTripSettings(makeSettings({ date: "2026-10-31" }), "2026-11-01").date).toBe("2026-11-02");
  });
});

describe("inlineErrorField", () => {
  it("maps a 400's field to the form field that shows it", () => {
    expect(inlineErrorField(new ApiError("Too many people", "http", { status: 400, field: "seniors" }))).toBe(
      "passengers",
    );
    expect(inlineErrorField(new ApiError("No such route", "http", { status: 400, field: "to" }))).toBe("to");
    expect(inlineErrorField(new ApiError("Bad", "http", { status: 400, field: "height_ft" }))).toBe("height");
  });

  it("leaves everything else to a toast", () => {
    expect(inlineErrorField(new ApiError("Max trips", "http", { status: 400, field: null }))).toBeNull();
    expect(inlineErrorField(new ApiError("Bad fare", "http", { status: 400, field: "fare" }))).toBeNull();
    expect(inlineErrorField(new ApiError("Gone", "http", { status: 404, field: "to" }))).toBeNull();
    expect(inlineErrorField(new Error("boom"))).toBeNull();
  });
});
