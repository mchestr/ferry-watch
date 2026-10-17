/**
 * The service worker (public/sw.js), run in a stand-in for its global scope: a push opens only a page on this site
 * (docs/waitlist/design.md I8), and an alert without a title says "Waitlist".
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

const ORIGIN = "https://waitlist.chestr.dev";
const SOURCE = readFileSync(join(__dirname, "..", "public", "sw.js"), "utf8");

type Listener = (event: unknown) => void;

/** Loads sw.js into a fresh fake `self`; returns its listeners and what it asked the browser to do. */
function load() {
  const listeners = new Map<string, Listener>();
  const showNotification = vi.fn<(title: string, options: object) => Promise<void>>(() => Promise.resolve());
  const openWindow = vi.fn<(url: string) => Promise<null>>(() => Promise.resolve(null));
  const self = {
    location: { origin: ORIGIN },
    registration: { showNotification },
    clients: { openWindow },
    addEventListener: (type: string, listener: Listener) => listeners.set(type, listener),
  };
  runInNewContext(SOURCE, { self, URL });
  const fire = (type: string, event: object) => {
    const listener = listeners.get(type);
    if (!listener) throw new Error(`sw.js doesn't listen for ${type}`);
    const waited: unknown[] = [];
    listener({ ...event, waitUntil: (p: unknown) => waited.push(p) });
    return waited;
  };
  return { fire, showNotification, openWindow };
}

/** A notification click on an alert that said to open `url`. */
function click(url: unknown) {
  const sw = load();
  const close = vi.fn();
  sw.fire("notificationclick", { notification: { close, data: { url } } });
  expect(close).toHaveBeenCalled();
  return sw.openWindow.mock.calls.map(([url]) => url);
}

describe("the service worker", () => {
  it("opens the page an alert names on this site, such as a watch's page", () => {
    expect(click(`${ORIGIN}/w/0f3c9a1b2d4e5f60`)).toEqual([`${ORIGIN}/w/0f3c9a1b2d4e5f60`]);
    expect(click("/c/a1b2c3d4e5f60718")).toEqual([`${ORIGIN}/c/a1b2c3d4e5f60718`]);
  });

  it("opens the dashboard for anywhere else, even a booking page", () => {
    expect(click("https://yodelportal.com/buntzen-lake/Half-Day-Pass")).toEqual([`${ORIGIN}/`]);
    expect(click("https://evil.example/w/0f3c9a1b2d4e5f60")).toEqual([`${ORIGIN}/`]);
    expect(click("javascript:alert(1)")).toEqual([`${ORIGIN}/`]);
    expect(click(undefined)).toEqual([`${ORIGIN}/`]);
  });

  it("shows an alert, titled Waitlist when the message has no title", () => {
    const sw = load();
    sw.fire("push", { data: { json: () => ({ body: "Something opened", url: "/w/0f3c9a1b2d4e5f60" }) } });
    expect(sw.showNotification).toHaveBeenCalledWith("Waitlist", {
      body: "Something opened",
      data: { url: "/w/0f3c9a1b2d4e5f60" },
      tag: "Waitlist",
    });
    const titled = load();
    titled.fire("push", { data: { json: () => ({ title: "Buntzen Lake: morning pass open for Thu Jul 1" }) } });
    expect(titled.showNotification.mock.calls[0]?.[0]).toBe("Buntzen Lake: morning pass open for Thu Jul 1");
  });

  it("still shows an alert for a message it can't read", () => {
    const sw = load();
    sw.fire("push", {
      data: {
        json: () => {
          throw new SyntaxError("not JSON");
        },
      },
    });
    expect(sw.showNotification.mock.calls[0]?.[0]).toBe("Waitlist");
  });
});
