import { describe, expect, it } from "vitest";
import { backoffDelay, socketUrl } from "./live";

describe("socketUrl and backoffDelay", () => {
  it("connects to /api/ws on the page's host, over wss for https", () => {
    expect(socketUrl("https://waitlist.example/")).toBe("wss://waitlist.example/api/ws");
    expect(socketUrl("http://localhost:5173/")).toBe("ws://localhost:5173/api/ws");
  });

  it("doubles from 1 s, caps at 15 s, and jitters between half and all of it", () => {
    expect([0, 1, 2, 3, 4, 10].map((attempt) => backoffDelay(attempt, () => 1))).toEqual([
      1000, 2000, 4000, 8000, 15000, 15000,
    ]);
    expect(backoffDelay(0, () => 0)).toBe(500);
    expect(backoffDelay(10, () => 0)).toBe(7500);
  });
});
