// fw/session.js, the classic script both viewers share: its subprotocol offer against messages.json
// viewer_offers, and the URLs and checkout names it reads and writes.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";
import vm from "node:vm";

import { pack } from "./pack.mjs";

const source = readFileSync(new URL("../site/fw/session.js", import.meta.url), "utf8");
const { viewer_offers: offers } = pack("messages.json");
const TRIP = "3f9c0a1b2c3d4e5f";

/** FwSession as a page at `pathname` gets it; plain values out, as JSON (the script runs in its own realm). */
function load({ pathname = "/", maxTouchPoints = 0 } = {}) {
  const window = {};
  const context = { window, location: { pathname, hash: "" }, navigator: { maxTouchPoints }, crypto, btoa };
  vm.runInNewContext(source, context);
  const plain = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
  return Object.fromEntries(
    Object.entries(window.FwSession).map(([name, f]) => [name, (...args) => plain(f(...args))]),
  );
}

describe("the streamed viewer's offer (messages.json viewer_offers)", () => {
  const session = load();
  for (const c of offers) {
    test(c.id, () => {
      assert.deepEqual(session.offer(c.record, c.ticket, "fw.cast1", c.now * 1000), c.offer);
    });
  }

  test("the classic viewer's is the same with binary, as in 0.6.4", () => {
    const [first] = offers;
    assert.deepEqual(session.offer(first.record, first.ticket, "binary", first.now * 1000), [
      "binary",
      ...first.offer.slice(1),
    ]);
  });
});

describe("which checkout a page is for", () => {
  const session = load();
  test("the landing page's path, vnc.html's fragment and viewer.html's fragment", () => {
    const at = { trip: TRIP, gen: "4" };
    assert.deepEqual(session.where(`/t/${TRIP}/g/4/`), at);
    assert.deepEqual(session.where(`#path=t/${TRIP}/g/4/ws&autoconnect=1`), at);
    assert.deepEqual(session.where(`#autoconnect=1&path=t/${TRIP}/g/4/ws`), at);
    assert.deepEqual(session.where(`#t=${TRIP}&g=4`), at);
  });

  test("nothing else", () => {
    for (const other of [
      `/t/${TRIP}/g/4`,
      `#t=${TRIP}&g=4&x=1`,
      `#t=${TRIP.toUpperCase()}&g=4`,
      `#g=4&t=${TRIP}`,
      "",
      "#",
    ]) {
      assert.equal(session.where(other), null, other);
    }
  });

  test("the viewers' URLs", () => {
    assert.equal(session.streamUrl(TRIP, "4"), `/fw/viewer.html#t=${TRIP}&g=4`);
    const settings = `path=t/${TRIP}/g/4/ws&autoconnect=1&reconnect=1&reconnect_delay=1000&idle_disconnect=120&anti_aliasing=1`;
    assert.equal(session.classicUrl(TRIP, "4"), `/vnc.html#${settings}`);
    assert.equal(load({ maxTouchPoints: 5 }).classicUrl(TRIP, "4"), `/vnc.html#${settings}&virtual_keyboard_visible=1`);
  });

  test("a hold is over at its hard deadline", () => {
    assert.equal(session.expired({ hard_deadline: 100 }, 99_999), false);
    assert.equal(session.expired({ hard_deadline: 100 }, 100_000), true);
    assert.equal(session.expired(null, 100_000), false);
    assert.equal(session.expired({}, 100_000), false);
  });
});
