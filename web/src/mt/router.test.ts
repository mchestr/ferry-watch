import { describe, expect, it } from "vitest";
import { route } from "./router";

describe("the routes waitlist-web serves the app on (design.md §6.8, §7.1)", () => {
  it.each([
    ["/", { page: "home" }],
    ["/new", { page: "new", provider: null }],
    ["/new/buntzen", { page: "new", provider: "buntzen" }],
    ["/new/bc_parks2", { page: "new", provider: "bc_parks2" }],
    ["/w/0f3c9a1b2d4e5f60", { page: "watch", watchId: "0f3c9a1b2d4e5f60" }],
    ["/c/a1b2c3d4e5f60718", { page: "checkout", tripId: "a1b2c3d4e5f60718" }],
    ["/settings", { page: "settings" }],
    ["/admin", { page: "admin" }],
    ["/auth/callback", { page: "callback" }],
  ])("reads %s", (path, expected) => {
    expect(route(path)).toEqual(expected);
  });

  it.each(["/new/", "/new/Buntzen", "/new/b", "/new/9lives", "/w/0F3C9A1B2D4E5F60", "/w/0f3c", "/c/", "/trips"])(
    "reads %s as missing",
    (path) => {
      expect(route(path)).toEqual({ page: "missing" });
    },
  );
});
