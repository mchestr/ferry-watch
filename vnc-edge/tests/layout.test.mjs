// core/layout.js: the box an image is drawn in, and the frame px under a point (PROTOCOL.md §8.1).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { backing, box, toFrame, toView, viewport } from "../site/fw/viewer/core/layout.js";
import { pack } from "./pack.mjs";

const { hello_env: env } = pack("messages.json");
const gestures = pack("gestures.json").cases;
const geometry = (id) => gestures.find((c) => c.id === id);

describe("the viewport the viewer asks for (messages.json hello_env)", () => {
  for (const c of env) {
    test(c.id, () => {
      const { w, h, dpr } = c.hello;
      assert.deepEqual(viewport(c.inner[0], c.inner[1], c.dpr), { w, h, dpr });
    });
  }
});

describe("the image's box", () => {
  test("a phone's image fills the screen", () => {
    const at = box(390, 664, 390, 664);
    assert.deepEqual([at.x, at.y, at.width, at.height, at.k], [0, 0, 390, 664, 1]);
    assert.deepEqual(toFrame(at, 120.5, 300), [120.5, 300]);
  });

  test("a pop-up's shorter page is centred", () => {
    const at = box(390, 664, 390, 600);
    assert.deepEqual([at.x, at.y, at.k], [0, 32, 1]);
    assert.equal(toFrame(at, 100, 20), null);
    assert.deepEqual(toFrame(at, 100, 32), [100, 0]);
    assert.deepEqual(toFrame(at, 390, 632), [390, 600], "the image's far edge is on it");
  });

  test("a window wider than 1920 px letterboxes the image (gestures.json mouse-click-wide)", () => {
    const c = geometry("mouse-click-wide");
    const at = box(...c.viewport, c.image.w, c.image.h);
    assert.deepEqual([at.x, at.y, at.width, at.height, at.k], [320, 0, 1920, 1440, 1]);
    const { x, y } = c.events.find((e) => e.type === "pointerdown");
    const { x: fx, y: fy } = c.expect.taps[0];
    assert.deepEqual(toFrame(at, x, y), [fx, fy]);
    assert.deepEqual(toView(at, fx, fy), [x, y]);
    const outside = geometry("mouse-click-letterbox").events[0];
    assert.equal(toFrame(at, outside.x, outside.y), null);
  });

  test("an image larger than the screen is shrunk to fit (gestures.json tap-letterboxed)", () => {
    const c = geometry("tap-letterboxed");
    const at = box(...c.viewport, c.image.w, c.image.h);
    assert.deepEqual([at.x, at.y, at.k], [0, 50, 0.5]);
    const [first, , second] = c.events;
    assert.equal(toFrame(at, ...first.touches[0]), null, "on the letterbox");
    const { x, y } = c.expect.taps[0];
    assert.deepEqual(toFrame(at, ...second.touches[0]), [x, y]);
  });

  test("the canvas has a pixel per device pixel", () => {
    assert.deepEqual(backing(390, 664, 3), [1170, 1992]);
    assert.deepEqual(backing(412, 839, 2.625), [1082, 2202]);
    assert.deepEqual(backing(0, 0, 1), [1, 1]);
  });
});
