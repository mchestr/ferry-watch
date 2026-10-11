import { describe, expect, it } from "vitest";
import {
  addDays,
  ago,
  clock,
  countdown,
  duration,
  everyLabel,
  fmt12,
  longDate,
  parseIsoDate,
  parseTimeOfDay,
  relativeDay,
  serverNow,
  serverToClientMs,
  timeLabel,
  todayInPacific,
  tomorrowInPacific,
} from "./time";

const DAY_MS = 86_400_000;

describe("parseTimeOfDay", () => {
  it.each([
    ["00:00", 0],
    ["07:30", 450],
    ["12:00", 720],
    ["23:59", 1439],
  ])("%s is %i minutes after midnight", (input, minutes) => {
    expect(parseTimeOfDay(input)).toBe(minutes);
  });

  it.each(["", "7:30", "24:00", "12:60", "ab:cd", "07:30:00", " 07:30", "07", "-1:00", "7:30 pm"])(
    "rejects %j instead of guessing",
    (input) => {
      expect(parseTimeOfDay(input)).toBeNull();
    },
  );
});

describe("fmt12", () => {
  it.each([
    ["00:00", "12:00 am"],
    ["00:05", "12:05 am"],
    ["07:00", "7:00 am"],
    ["11:59", "11:59 am"],
    ["12:00", "12:00 pm"],
    ["13:05", "1:05 pm"],
    ["23:59", "11:59 pm"],
  ])("%s -> %s", (input, expected) => {
    expect(fmt12(input)).toBe(expected);
  });

  it.each(["", "7", "12:", "25:00", "noon", "7:00 am"])("returns null for %j (never '12:undefined am')", (input) => {
    expect(fmt12(input)).toBeNull();
  });

  it("timeLabel shows unparseable input as-is", () => {
    expect(timeLabel("19:15")).toBe("7:15 pm");
    expect(timeLabel("soon")).toBe("soon");
  });
});

describe("duration", () => {
  it("formats the crossing time", () => {
    expect(duration("07:00", "08:35")).toBe("1h 35m");
    expect(duration("07:00", "07:00")).toBe("0h 00m");
  });

  it("wraps past midnight", () => {
    expect(duration("23:15", "00:50")).toBe("1h 35m");
  });

  it("returns null when either time is invalid", () => {
    expect(duration("7:00", "08:35")).toBeNull();
    expect(duration("07:00", "")).toBeNull();
  });
});

describe("parseIsoDate", () => {
  it("parses real calendar dates to UTC midnight", () => {
    expect(parseIsoDate("2026-10-12")?.toISOString()).toBe("2026-10-12T00:00:00.000Z");
    expect(parseIsoDate("2028-02-29")?.toISOString()).toBe("2028-02-29T00:00:00.000Z");
  });

  it.each(["2026-02-29", "2026-02-30", "2026-04-31", "2026-13-01", "2026-00-10", "2026-1-5", "", "tomorrow"])(
    "rejects %j",
    (input) => {
      expect(parseIsoDate(input)).toBeNull();
    },
  );
});

describe("addDays", () => {
  it("crosses month, year and leap-day boundaries", () => {
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2028-02-28", 1)).toBe("2028-02-29");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });

  it("moves exactly one calendar day across both DST changes", () => {
    // Pacific DST in 2026 starts Sunday March 8 and ends Sunday November 1.
    expect(addDays("2026-03-07", 1)).toBe("2026-03-08");
    expect(addDays("2026-03-08", 1)).toBe("2026-03-09");
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-11-01", 1)).toBe("2026-11-02");
  });

  it("returns null for an invalid date", () => {
    expect(addDays("2026-02-30", 1)).toBeNull();
  });
});

describe("today and tomorrow (Pacific)", () => {
  it("uses Pacific time, not UTC", () => {
    expect(todayInPacific(new Date("2026-10-12T06:59:00Z"))).toBe("2026-10-11"); // 11:59 pm PDT
    expect(todayInPacific(new Date("2026-10-12T07:00:00Z"))).toBe("2026-10-12"); // midnight PDT
  });

  it("is tomorrow on the 25-hour day, where adding 24 h stays on the same date", () => {
    const night = new Date("2026-11-01T00:30:00-07:00"); // before the 2 am fall-back
    expect(new Date(night.getTime() + DAY_MS).getDate()).toBe(1); // the old bug (tests run on Pacific time)
    expect(tomorrowInPacific(night)).toBe("2026-11-02");
  });

  it("is tomorrow on the 23-hour day, where adding 24 h skips a date", () => {
    const night = new Date("2026-03-07T23:30:00-08:00");
    expect(new Date(night.getTime() + DAY_MS).getDate()).toBe(9); // the old bug
    expect(tomorrowInPacific(night)).toBe("2026-03-08");
  });
});

describe("relativeDay and longDate", () => {
  it.each([
    ["2026-10-11", "Today"],
    ["2026-10-12", "Tomorrow"],
    ["2026-10-10", "Yesterday"],
    ["2026-10-14", "In 3 days"],
    ["2026-10-08", "3 days ago"],
    ["2026-11-02", "In 22 days"], // across the fall-back
  ])("%s relative to 2026-10-11 is %s", (iso, expected) => {
    expect(relativeDay(iso, "2026-10-11")).toBe(expected);
  });

  it("returns null for invalid dates", () => {
    expect(relativeDay("2026-02-30", "2026-10-11")).toBeNull();
    expect(longDate("someday")).toBeNull();
  });

  it("formats the long date without shifting the day", () => {
    expect(longDate("2026-10-12")).toBe("Monday, October 12");
    expect(longDate("2026-11-01")).toBe("Sunday, November 1");
  });
});

describe("clock, ago and countdown", () => {
  it("formats wall-clock time like the sailings", () => {
    expect(clock(new Date("2026-10-11T15:05:00-07:00").getTime())).toBe("3:05 pm");
    expect(clock(new Date("2026-10-11T00:00:00-07:00").getTime())).toBe("12:00 am");
  });

  it("describes elapsed time", () => {
    const now = new Date("2026-10-11T12:00:00-07:00").getTime();
    expect(ago(now - 5_000, now)).toBe("just now");
    expect(ago(now - 45_000, now)).toBe("45s ago");
    expect(ago(now - 119_000, now)).toBe("1 min ago");
    expect(ago(now - 3 * 3_600_000, now)).toBe("3h ago");
    expect(ago(now - 3 * DAY_MS, now)).toBe("Oct 8");
    expect(ago(now + 60_000, now)).toBe("just now"); // a future timestamp (clock skew) isn't negative
  });

  it("counts down in m:ss, and h:mm:ss from an hour", () => {
    expect(countdown(125)).toBe("2:05");
    expect(countdown(0.2)).toBe("0:01");
    expect(countdown(3600)).toBe("1:00:00");
    expect(countdown(-5)).toBe("0:00");
  });

  it("never prints NaN", () => {
    expect(clock(NaN)).toBe("");
    expect(ago(NaN, Date.now())).toBe("");
    expect(ago(Date.now(), Infinity)).toBe("");
    expect(countdown(NaN)).toBe("");
  });

  it("labels check intervals", () => {
    expect(everyLabel(15)).toBe("15 seconds");
    expect(everyLabel(60)).toBe("1 minute");
    expect(everyLabel(300)).toBe("5 minutes");
  });
});

describe("server clock conversion", () => {
  it("maps server timestamps onto the client clock with the measured skew", () => {
    // The client clock runs 5 s ahead of the server's.
    expect(serverToClientMs(1_000, 5)).toBe(1_005_000);
    expect(serverNow(5, 1_005_000)).toBe(1_000);
  });
});
