// The viewer's touch and mouse gestures, made into fw.cast1 messages (PROTOCOL.md §6.4-6.8, §8.1-8.4; DESIGN.md
// §7.4). DOM-free: the clock and the screen are injected, so `node --test` drives it through gestures.json.
//
// Touch: a touch that stays within 8 CSS px and lasts under 500 ms is a tap where it began. Moving 8 px starts a
// scroll that follows the finger from where it began, sent as `wheel` at most every 33 ms with the deltas summed,
// and a quick release coasts. Two fingers zoom the drawn image locally, then send one `pinch`. A touch that begins
// outside the image does nothing. A touch that begins while a coast, a prediction shift or a local zoom is drawn
// is never a tap: what the user saw there isn't where the page is.
//
// Mouse: `move` while over the image, at most every 33 ms; a click is a `tap` at the press, with its duration; the
// wheel is `wheel`, a line counting 40 px.
//
// Points on the screen are CSS px of the viewer; everything sent is frame px of the drawn image, through the §8.1
// box the screen gives.

import { toFrame } from "./layout.js";
import { Prediction } from "./predict.js";

export const TAP_SLOP = 8; // CSS px
export const TAP_MS = 500;
export const MAX_PRESS_MS = 300;
export const WHEEL_MS = 33;
export const MOVE_MS = 33;
export const MAX_DELTA = 4000; // frame px per `wheel`
export const LINE = 40; // px per wheel line
export const FLICK_GAP_MS = 80; // a release this soon after the last move can coast
export const FLICK_SPEED = 0.3; // CSS px/ms
export const DECAY = 0.997; // a coast's speed, per ms
export const STOP_SPEED = 0.02; // CSS px/ms: a coast ends below this
export const ZOOM_MS = 600; // a local zoom waits this long for an image at the new scale
const MIN_SCALE = 0.25;
const MAX_SCALE = 4;

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
const centroid = ([a, b]) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
const spread = ([a, b]) => Math.hypot(a[0] - b[0], a[1] - b[1]);

/** A point of the screen in frame px of the image in `box`, pulled onto the image if it's just off it. */
function onImage(box, [x, y]) {
  return [clamp((x - box.x) / box.k, 0, box.w), clamp((y - box.y) / box.k, 0, box.h)];
}

export class Gestures {
  /**
   * - `clock`: `now()` in ms, `later(fn, ms)`/`cancel(id)`, `frame(fn)`/`cancelFrame(id)` (animation frames);
   * - `screen()`: what is drawn, `{image, box}` (the image's header and its §8.1 box), or null;
   * - `send(msg)`: sends a `tap`, `wheel`, `move` or `pinch`; a tap's handler may focus the keyboard proxy first;
   * - `changed()`: what view() gives has changed, so the screen needs drawing again;
   * - `contentHeight()`: the document's height in CSS px from the field map, or null.
   */
  constructor({ clock, screen, send, changed = () => {}, contentHeight = () => null }) {
    this.clock = clock;
    this.screen = screen;
    this.send = send;
    this.changed = changed;
    this.contentHeight = contentHeight;
    this.prediction = new Prediction();
    this.touch = null; // the gesture of the fingers down now
    this.coast = null;
    this.zoom = null; // the local zoom drawn: { page, scale, c0, c1, f, until }
    this.press = null; // a mouse button down: { at, t0, blocked }
    this.wheels = { pending: null, sentAt: -Infinity, timer: 0 };
    this.moves = { pending: null, sentAt: -Infinity, timer: 0 };
    this.expiry = { shift: 0, zoom: 0 }; // timers that redraw once a prediction or a zoom has gone
  }

  /** A new connection: nothing carries over. */
  reset() {
    this.stopCoast();
    this.touch = null;
    this.press = null;
    this.zoom = null;
    this.prediction.drop();
    for (const queue of [this.wheels, this.moves]) {
      this.clock.cancel(queue.timer);
      queue.pending = null;
      queue.timer = 0;
    }
    this.changed();
  }

  /** What to draw over the image: the prediction's shift (frame px, up) and the local zoom, if any. */
  view(now = this.clock.now()) {
    const drawn = this.screen();
    const shift = drawn ? this.prediction.offset(now, drawn.image, this.contentHeight()) : 0;
    const zoom = this.zoomed(now) ? { c0: this.zoom.c0, c1: this.zoom.c1, f: this.zoom.f } : null;
    return { shift, zoom };
  }

  /** Whether a touch beginning now would be no tap (§8.2). */
  blocked(now = this.clock.now()) {
    const { shift, zoom } = this.view(now);
    return this.coast !== null || shift !== 0 || zoom !== null;
  }

  /** A gesture is under way, or its picture drawn: native pickers stay hidden meanwhile. */
  get busy() {
    return (this.touch !== null && this.touch.kind !== "pending") || this.blocked();
  }

  zoomed(now) {
    return this.zoom !== null && now < this.zoom.until;
  }

  // ---- touch: `touches` are the fingers down after the event, as [x, y], each finger at the same index throughout

  touchstart(touches) {
    const now = this.clock.now();
    if (this.touch === null) {
      const blocked = this.blocked(now);
      this.stopCoast();
      const drawn = this.screen();
      if (!drawn || !toFrame(drawn.box, ...touches[0])) {
        this.touch = { kind: "none" }; // begun off the image: nothing until every finger is up
        return;
      }
      this.touch = { kind: "pending", start: touches[0], t0: now, blocked };
      if (touches.length >= 2) this.startPinch(touches);
    } else if (touches.length >= 2 && (this.touch.kind === "pending" || this.touch.kind === "scroll")) {
      this.flushWheel();
      this.startPinch(touches);
    }
  }

  touchmove(touches) {
    const now = this.clock.now();
    const touch = this.touch;
    if (!touch || touches.length === 0) return;
    if (touch.kind === "pinch") {
      if (touches.length < 2) return;
      this.zoom.c1 = centroid(touches);
      this.zoom.f = clamp(spread(touches) / touch.spread, MIN_SCALE, MAX_SCALE);
      this.changed();
    } else if (touch.kind === "pending") {
      const [x, y] = touches[0];
      if (Math.hypot(x - touch.start[0], y - touch.start[1]) < TAP_SLOP) return;
      const drawn = this.screen();
      if (!drawn) return;
      Object.assign(touch, {
        kind: "scroll",
        last: touch.start,
        lastAt: touch.t0,
        vx: 0,
        vy: 0,
        page: drawn.image.page,
        origin: onImage(drawn.box, touch.start),
        k: drawn.box.k,
      });
      this.prediction.begin(drawn.image);
      this.scroll(touch, touches[0], now);
    } else if (touch.kind === "scroll") {
      this.scroll(touch, touches[0], now);
    }
  }

  scroll(touch, [x, y], now) {
    const dx = touch.last[0] - x;
    const dy = touch.last[1] - y;
    const dt = now - touch.lastAt;
    if (dt > 0) {
      touch.vx = 0.8 * (dx / dt) + 0.2 * touch.vx;
      touch.vy = 0.8 * (dy / dt) + 0.2 * touch.vy;
    }
    touch.last = [x, y];
    touch.lastAt = now;
    this.queueWheel(touch.page, touch.origin, dx / touch.k, dy / touch.k, true);
  }

  touchend(touches) {
    const now = this.clock.now();
    const touch = this.touch;
    if (!touch) return;
    if (touch.kind === "pinch" && touches.length < 2) {
      this.endPinch(touch, now);
    } else if (touches.length === 0) {
      if (touch.kind === "pending") this.tapEnded(touch, now);
      else if (touch.kind === "scroll") this.released(touch, now);
    }
    if (touches.length === 0) this.touch = null;
  }

  /** The system took the touches (a call, a gesture of its own): nothing is sent for them. */
  touchcancel() {
    if (this.touch?.kind === "pinch") {
      this.zoom = null;
      this.changed();
    }
    this.flushWheel();
    this.touch = null;
  }

  tapEnded(touch, now) {
    const held = now - touch.t0;
    if (touch.blocked) {
      // It ends what was drawn instead (§8.2): the picture goes back to the page as the images show it.
      this.prediction.drop();
      this.changed();
      return;
    }
    if (held >= TAP_MS) return; // a long press: nothing
    this.tap(touch.start, held);
  }

  released(touch, now) {
    const speed = Math.hypot(touch.vx, touch.vy);
    if (now - touch.lastAt <= FLICK_GAP_MS && speed > FLICK_SPEED) {
      this.coast = { page: touch.page, origin: touch.origin, k: touch.k, vx: touch.vx, vy: touch.vy, at: now };
      this.coast.frame = this.clock.frame(() => this.coasting());
    }
  }

  coasting() {
    const coast = this.coast;
    if (!coast) return;
    const now = this.clock.now();
    const dt = now - coast.at;
    coast.at = now;
    const decay = DECAY ** dt;
    coast.vx *= decay;
    coast.vy *= decay;
    if (Math.hypot(coast.vx, coast.vy) < STOP_SPEED) {
      this.coast = null;
      return;
    }
    this.queueWheel(coast.page, coast.origin, (coast.vx * dt) / coast.k, (coast.vy * dt) / coast.k, true);
    coast.frame = this.clock.frame(() => this.coasting());
  }

  stopCoast() {
    if (!this.coast) return;
    this.clock.cancelFrame(this.coast.frame);
    this.coast = null;
  }

  startPinch(touches) {
    const drawn = this.screen();
    const c0 = centroid(touches);
    this.touch = { kind: "pinch", spread: Math.max(1, spread(touches)), image: drawn.image, box: drawn.box };
    this.zoom = { page: drawn.image.page, scale: drawn.image.scale, c0, c1: c0, f: 1, until: Infinity };
    this.prediction.drop();
    this.stopCoast();
    this.changed();
  }

  endPinch(touch, now) {
    const { image, box } = touch;
    const zoom = this.zoom;
    const [x0, y0] = onImage(box, zoom.c0);
    const [x1, y1] = onImage(box, zoom.c1);
    this.send({ t: "pinch", page: image.page, seq: image.seq, x0, y0, x1, y1, scale: zoom.f });
    zoom.until = now + ZOOM_MS;
    this.touch = { kind: "none" }; // the finger left does nothing
    this.redrawAt("zoom", zoom.until);
  }

  // ---- mouse

  pointermove(x, y) {
    const drawn = this.screen();
    const at = drawn && toFrame(drawn.box, x, y);
    if (!at) return;
    this.queue(this.moves, MOVE_MS, { t: "move", page: drawn.image.page, x: at[0], y: at[1] });
  }

  pointerdown(x, y) {
    const now = this.clock.now();
    const drawn = this.screen();
    this.press = drawn && toFrame(drawn.box, x, y) ? { at: [x, y], t0: now, blocked: this.blocked(now) } : null;
  }

  pointerup() {
    const press = this.press;
    this.press = null;
    if (!press || press.blocked) return;
    this.flush(this.moves);
    this.tap(press.at, this.clock.now() - press.t0);
  }

  /** A wheel event at (x, y): `mode` is its deltaMode (0 pixels, 1 lines, 2 pages). */
  wheel(x, y, dx, dy, mode = 0) {
    const drawn = this.screen();
    const at = drawn && toFrame(drawn.box, x, y);
    if (!at) return;
    const unit = mode === 1 ? LINE : mode === 2 ? drawn.box.height : 1;
    const k = drawn.box.k;
    this.queueWheel(drawn.image.page, at, (dx * unit) / k, (dy * unit) / k, false);
  }

  // ---- what is sent

  /** A tap at a point of the screen, held `held` ms, on the image drawn now. */
  tap(point, held) {
    const drawn = this.screen();
    const at = drawn && toFrame(drawn.box, ...point);
    if (!at) return;
    const { page, seq } = drawn.image;
    this.send({ t: "tap", page, seq, x: at[0], y: at[1], ms: Math.min(Math.round(held), MAX_PRESS_MS) });
  }

  /** Scrolling over frame px `at`; `predicted`: a touch scroll, which the picture follows (DESIGN.md §4.5). */
  queueWheel(page, at, dx, dy, predicted) {
    const pending = this.wheels.pending;
    if (pending && pending.page !== page) this.flush(this.wheels);
    const next = this.wheels.pending ?? { t: "wheel", page, x: at[0], y: at[1], dx: 0, dy: 0 };
    next.x = at[0];
    next.y = at[1];
    next.dx += dx;
    next.dy += dy;
    if (predicted) {
      this.prediction.wheel(dy, this.clock.now());
      this.changed();
      this.redrawAt("shift", this.prediction.until);
    }
    this.queue(this.wheels, WHEEL_MS, next);
  }

  flushWheel() {
    this.flush(this.wheels);
  }

  /** At most one message of a queue per `every` ms: now if it may go, else the latest when it may. */
  queue(queue, every, msg) {
    queue.pending = msg;
    if (queue.timer) return;
    const wait = queue.sentAt + every - this.clock.now();
    if (wait <= 0) this.flush(queue);
    else queue.timer = this.clock.later(() => this.flush(queue), wait);
  }

  flush(queue) {
    this.clock.cancel(queue.timer);
    queue.timer = 0;
    const msg = queue.pending;
    queue.pending = null;
    if (!msg) return;
    if (msg.t === "wheel") {
      if (msg.dx === 0 && msg.dy === 0) return;
      // A sum too large for one message leaves the rest for the next.
      const dx = clamp(msg.dx, -MAX_DELTA, MAX_DELTA);
      const dy = clamp(msg.dy, -MAX_DELTA, MAX_DELTA);
      if (dx !== msg.dx || dy !== msg.dy) {
        queue.pending = { ...msg, dx: msg.dx - dx, dy: msg.dy - dy };
        queue.timer = this.clock.later(() => this.flush(queue), WHEEL_MS);
      }
      Object.assign(msg, { dx, dy });
    }
    queue.sentAt = this.clock.now();
    this.send(msg);
  }

  /** Draws again once the prediction shift or the local zoom (`what`), which ends at `until` (ms), has gone. */
  redrawAt(what, until) {
    this.clock.cancel(this.expiry[what]);
    this.expiry[what] = until === null ? 0 : this.clock.later(() => this.changed(), until - this.clock.now());
  }

  /** A new image is drawn. */
  image(header) {
    this.prediction.image(header);
    const zoom = this.zoom;
    if (
      zoom &&
      this.touch?.kind !== "pinch" &&
      (header.page !== zoom.page || Math.abs(header.scale - zoom.scale) > 0.001)
    ) {
      this.zoom = null;
    }
  }
}
