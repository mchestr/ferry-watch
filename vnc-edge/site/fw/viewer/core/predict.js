// Confirmed scroll prediction (DESIGN.md §4.5, §7.4). DOM-free.
//
// While a finger scrolls, the pod's images trail it by a round trip. The viewer hides some of that by drawing the
// latest image shifted by the scrolling the images don't show yet, but only once an image has confirmed that the
// document itself moves: when an inner element (an app shell's <main>, a modal, a terms box) scrolls instead, the
// images' `sy` never changes, and a shift would slide the picture away from what the page shows. Then:
// - the shift is at most what was scrolled since the latest image that moved, so it never runs ahead of the page
//   by more than one image's worth;
// - it is clamped to the document (`ch` from the field map) and to one viewport;
// - it goes 250 ms after the last scroll, image or not.
// No tap is sent while a shift is drawn (PROTOCOL.md §8.2).

export const HOLD_MS = 250;

export class Prediction {
  constructor() {
    this.gesture = null;
  }

  /** A scroll begins over the drawn image `image`. */
  begin(image) {
    this.gesture = { page: image.page, scale: image.scale, sy: image.sy, sent: 0, confirmed: false, since: 0 };
    this.last = -Infinity;
  }

  /** `dy` frame px were scrolled at `now`. */
  wheel(dy, now) {
    const gesture = this.gesture;
    if (!gesture) return;
    gesture.sent += dy;
    if (gesture.confirmed) gesture.since += dy;
    this.last = now;
  }

  /** A new image is drawn. One whose `sy` moved the way the finger scrolls confirms the document moves. */
  image(header) {
    const gesture = this.gesture;
    if (!gesture) return;
    if (header.page !== gesture.page || Math.abs(header.scale - gesture.scale) > 0.001) {
      this.drop();
      return;
    }
    const moved = header.sy - gesture.sy;
    if (moved !== 0 && Math.sign(moved) === Math.sign(gesture.sent)) {
      gesture.confirmed = true;
      gesture.since = 0;
      gesture.sy = header.sy;
      gesture.sent = 0;
    }
  }

  drop() {
    this.gesture = null;
  }

  /** When the shift goes by itself (ms), or null when none can be drawn. */
  get until() {
    return this.gesture?.confirmed ? this.last + HOLD_MS : null;
  }

  /**
   * How far up to draw `image` at `now`, in its frame px (negative: down): 0 unless a confirmed scroll is under
   * way on this image's page. `contentHeight` is the document's height in CSS px, if the field map has it.
   */
  offset(now, image, contentHeight = null) {
    const gesture = this.gesture;
    if (!gesture?.confirmed || !image || now >= this.last + HOLD_MS || gesture.since === 0) return 0;
    if (image.page !== gesture.page || Math.abs(image.scale - gesture.scale) > 0.001) return 0;
    let shift = Math.max(-image.h, Math.min(image.h, gesture.since));
    if (contentHeight !== null) {
      const bottom = Math.max(0, contentHeight - image.h / image.scale); // the furthest the page can scroll
      const sy = Math.max(0, Math.min(bottom, image.sy + shift / image.scale));
      shift = (sy - image.sy) * image.scale;
    }
    return Math.abs(shift) < 0.01 ? 0 : shift;
  }
}
