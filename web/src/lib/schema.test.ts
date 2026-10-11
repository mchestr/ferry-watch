import { describe, expect, it } from "vitest";
import { makeCart, makeSailing, makeState, makeTrip, TERMINALS } from "@/test/fixtures";
import { StateSchema, TerminalListSchema, WsMessageSchema, isHttpUrl } from "./schema";

describe("schemas", () => {
  it("accept a full state (and the test fixtures follow the contract)", () => {
    const state = makeState({
      trips: [makeTrip({ cart: makeCart(), sailings: [makeSailing()] })],
      events: [{ id: 1, ts: 1, kind: "alert", text: "Opened", trip_id: "t1", trip_title: "A → B" }],
    });
    expect(StateSchema.parse(state)).toEqual(state);
    expect(TerminalListSchema.parse({ terminals: TERMINALS }).terminals).toEqual(TERMINALS);
  });

  it("strip unknown fields, so the server can add some", () => {
    const parsed = StateSchema.parse({ ...makeState(), added_later: true });
    expect(parsed).not.toHaveProperty("added_later");
  });

  it("reject times and dates in the wrong format", () => {
    expect(StateSchema.safeParse(makeState({ trips: [makeTrip({ earliest: "7:00 am" })] })).success).toBe(false);
    expect(StateSchema.safeParse(makeState({ trips: [makeTrip({ date: "Oct 12" })] })).success).toBe(false);
  });

  it("only let http(s) links through", () => {
    expect(isHttpUrl("https://www.bcferries.com/")).toBe(true);
    expect(isHttpUrl("/vnc/vnc.html?autoconnect=1")).toBe(true);
    expect(isHttpUrl("javascript:alert(1)")).toBe(false);
    expect(isHttpUrl("data:text/html,hi")).toBe(false);
    expect(StateSchema.safeParse(makeState({ checkout_url: "javascript:alert(1)" })).success).toBe(false);
  });

  it("parse both WebSocket message types", () => {
    expect(WsMessageSchema.parse({ type: "ping", server_time: 5 })).toEqual({ type: "ping", server_time: 5 });
    expect(WsMessageSchema.parse({ type: "state", data: makeState() }).type).toBe("state");
    expect(WsMessageSchema.safeParse({ type: "hello" }).success).toBe(false);
  });
});
