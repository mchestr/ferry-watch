// core/gestures.js (with core/predict.js) against gestures.json, on a fake clock (PROTOCOL.md §6.6, §8.1-8.4).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { Gestures } from "../site/fw/viewer/core/gestures.js";
import { box } from "../site/fw/viewer/core/layout.js";
import { Prediction } from "../site/fw/viewer/core/predict.js";
import { forViewer, pack } from "./pack.mjs";

const { cases } = pack("gestures.json");
const FRAME_MS = 16;
const near = (a, b) => Math.abs(a - b) <= 1e-6;

/** Timers and animation frames on a clock the test moves; frames run every 16 ms while one is wanted. */
class FakeClock {
  t = 0;
  ids = 0;
  timers = new Map();
  frames = new Map();

  now = () => this.t;
  later = (fn, ms) => this.add(this.timers, { at: this.t + Math.max(0, ms), fn });
  cancel = (id) => this.timers.delete(id);
  frame = (fn) => this.add(this.frames, { fn });
  cancelFrame = (id) => this.frames.delete(id);

  add(into, entry) {
    this.ids += 1;
    into.set(this.ids, entry);
    return this.ids;
  }

  /** Runs everything due up to `to`, in time order, then stands at `to`. */
  advance(to) {
    for (;;) {
      const timers = [...this.timers].filter(([, timer]) => timer.at <= to).sort((a, b) => a[1].at - b[1].at);
      const tick = this.frames.size ? (Math.floor(this.t / FRAME_MS) + 1) * FRAME_MS : Infinity;
      const next = Math.min(timers.length ? timers[0][1].at : Infinity, tick);
      if (next > to) break;
      this.t = Math.max(this.t, next);
      if (timers.length && timers[0][1].at === next) {
        this.timers.delete(timers[0][0]);
        timers[0][1].fn();
      } else {
        const due = [...this.frames.values()];
        this.frames.clear();
        for (const frame of due) frame.fn();
      }
    }
    this.t = to;
  }
}

/** Runs a case: its events at their times, then the clock to its end. What was sent, each with its time. */
function run(c) {
  const clock = new FakeClock();
  let image = c.image;
  const sent = [];
  const gestures = new Gestures({
    clock,
    screen: () => ({ image, box: box(...c.viewport, image.w, image.h) }),
    send: (msg) => sent.push({ at: clock.now(), msg: structuredClone(msg) }),
  });
  for (const event of c.events) {
    clock.advance(event.at);
    if (event.type.startsWith("touch")) gestures[event.type](event.touches);
    else if (event.type === "wheel") gestures.wheel(event.x, event.y, event.dx, event.dy, event.mode);
    else if (event.type === "image") gestures.image((image = event.image));
    else gestures[event.type](event.x, event.y);
  }
  clock.advance(c.end);
  return sent;
}

function assertExact(actual, expected, what) {
  assert.equal(actual.length, expected.length, `${what}: ${JSON.stringify(actual)}`);
  actual.forEach((msg, i) => {
    const want = expected[i].t === "tap" ? { ms: 0, ...expected[i] } : expected[i];
    assert.deepEqual(Object.keys(msg).sort(), Object.keys(want).sort(), what);
    for (const [key, value] of Object.entries(want)) {
      assert.ok(typeof value === "number" ? near(msg[key], value) : msg[key] === value, `${what} ${key}`);
    }
  });
}

function assertSpaced(sent, gap, what) {
  for (let i = 1; i < sent.length; i++) assert.ok(sent[i].at - sent[i - 1].at >= gap, `${what} ${i} too soon`);
}

describe("gestures become these messages (gestures.json)", () => {
  for (const c of forViewer(cases)) {
    test(`${c.id} (§${c.sec})`, () => {
      const sent = run(c);
      const of = (t) => sent.filter(({ msg }) => msg.t === t);
      assertExact(
        of("tap").map(({ msg }) => msg),
        c.expect.taps ?? [],
        "taps",
      );
      assertExact(
        of("pinch").map(({ msg }) => msg),
        c.expect.pinches ?? [],
        "pinches",
      );

      const wheels = of("wheel");
      const want = c.expect.wheels;
      if (!want) {
        assert.deepEqual(wheels, [], "no wheels");
      } else {
        assert.ok(wheels.length > 0, "wheels");
        for (const { msg } of wheels) {
          assert.deepEqual([msg.page, msg.x, msg.y], [want.page, want.x, want.y]);
          assert.ok(Math.abs(msg.dx) <= 4000 && Math.abs(msg.dy) <= 4000);
        }
        assertSpaced(wheels, 33, "wheel");
        const sum = (key) => wheels.reduce((total, { msg }) => total + msg[key], 0);
        assert.ok(near(sum("dx"), want.dx), `dx ${sum("dx")}`);
        if ("dy" in want) assert.ok(near(sum("dy"), want.dy), `dy ${sum("dy")}`);
        else {
          assert.equal(Math.sign(sum("dy")), Math.sign(want.dy_more_than));
          assert.ok(Math.abs(sum("dy")) > Math.abs(want.dy_more_than), `dy ${sum("dy")}: no coast`);
        }
        if ("coast_after" in want)
          assert.ok(
            wheels.some(({ at }) => at > want.coast_after),
            "no coast",
          );
        if ("none_after" in want)
          assert.ok(
            wheels.every(({ at }) => at <= want.none_after),
            "coasted too long",
          );
      }

      const moves = of("move");
      if (!c.expect.moves) {
        assert.deepEqual(moves, [], "no moves");
      } else {
        assert.ok(moves.every(({ msg }) => msg.page === c.expect.moves.page));
        assertSpaced(moves, 33, "move");
        const last = moves.at(-1).msg;
        assert.deepEqual([last.x, last.y], c.expect.moves.last);
      }
    });
  }
});

describe("the prediction", () => {
  const image = { page: 1, seq: 7, w: 390, h: 664, scale: 1, sx: 0, sy: 0 };

  test("draws nothing until an image confirms the document moves", () => {
    const prediction = new Prediction();
    prediction.begin(image);
    prediction.wheel(40, 0);
    assert.equal(prediction.offset(10, image), 0);
    prediction.image({ ...image, seq: 8, sy: 0 }); // an inner element scrolled
    prediction.wheel(40, 20);
    assert.equal(prediction.offset(30, image), 0);
    assert.equal(prediction.until, null);
  });

  test("then shifts by what was scrolled since the latest image that moved, for 250 ms", () => {
    const prediction = new Prediction();
    prediction.begin(image);
    prediction.wheel(40, 0);
    const moved = { ...image, seq: 8, sy: 30 };
    prediction.image(moved);
    assert.equal(prediction.offset(10, moved), 0, "nothing scrolled since");
    prediction.wheel(25, 20);
    prediction.wheel(15, 40);
    assert.equal(prediction.offset(50, moved), 40);
    assert.equal(prediction.until, 290);
    assert.equal(prediction.offset(289, moved), 40);
    assert.equal(prediction.offset(290, moved), 0);
    const further = { ...moved, seq: 9, sy: 60 };
    prediction.image(further);
    assert.equal(prediction.offset(60, further), 0, "the image caught up");
  });

  test("never past the document's ends or a viewport, nor on another page or scale", () => {
    const prediction = new Prediction();
    prediction.begin(image);
    prediction.wheel(10, 0);
    const moved = { ...image, seq: 8, sy: 1000 };
    prediction.image(moved);
    prediction.wheel(3000, 10);
    assert.equal(prediction.offset(20, moved), 664);
    assert.equal(prediction.offset(20, moved, 1200), 1200 - 664 - 1000);
    assert.equal(prediction.offset(20, { ...moved, scale: 2 }), 0);
    assert.equal(prediction.offset(20, { ...moved, page: 2 }), 0);
    prediction.image({ ...moved, page: 2, seq: 9 });
    assert.equal(prediction.offset(20, moved), 0, "a page change ends it");
  });

  test("scrolling back up is predicted too", () => {
    const prediction = new Prediction();
    const low = { ...image, sy: 500 };
    prediction.begin(low);
    prediction.wheel(-30, 0);
    const moved = { ...low, seq: 8, sy: 480 };
    prediction.image(moved);
    prediction.wheel(-20, 10);
    assert.equal(prediction.offset(20, moved, 3000), -20);
  });
});
