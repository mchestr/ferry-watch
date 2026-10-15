// Drawing the agent's images (PROTOCOL.md §4, §8.1; DESIGN.md §7.3).
//
// Binary messages go through one promise chain, strictly in order, since a repeat draws what its slot held after
// the image before it: decode a JPEG into its slot (closing the bitmap it replaces), draw, then ack. The image is
// drawn into the box §8.1 gives for its header's `w`×`h`, on a canvas with a pixel per device pixel; the rest of
// the screen is letterbox. While a finger scrolls or pinches, the image is drawn moved or zoomed as the gesture
// says (core/gestures.js), inside its box.

import { backing, box } from "./core/layout.js";
import { IMAGE, parseHeader } from "./core/protocol.js";

const STILL = Object.freeze({ shift: 0, zoom: null, moving: false });

export class Frames {
  /**
   * `ack(seq)` once a binary message is drawn; `broken()` for one the viewer can't take (an agent bug: it is
   * neither drawn nor acked); `arriving(header)` just before each one is drawn, `drawn(header)` just after;
   * `view()` the gesture's picture, `{shift, zoom, moving}` (core/gestures.js).
   */
  constructor(canvas, { ack, broken, arriving = () => {}, drawn, view = () => STILL }) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d", { alpha: false });
    this.ack = ack;
    this.broken = broken;
    this.arriving = arriving;
    this.drawn = drawn;
    this.view = view;
    const style = getComputedStyle(document.documentElement);
    this.letterbox = style.getPropertyValue("--letterbox").trim() || "#888";
    this.blank = style.getPropertyValue("--blank").trim() || "#fff"; // what a moved picture uncovers
    this.generation = 0;
    this.chain = Promise.resolve();
    this.slots = [null, null];
    this.current = null; // the header of the image drawn in this session, which taps and pinches refer to
    this.picture = null; // what is on screen: { header, bitmap }, kept across a reconnect until a new image
    this.box = null;
    this.shown = STILL; // the gesture's picture last drawn
    addEventListener("resize", () => this.draw());
  }

  /** Draws again if the gesture's picture has changed since the last time. */
  update() {
    const view = this.view();
    const { shift, zoom, moving } = this.shown;
    const same = (a, b) => a === b || (a && b && a.f === b.f && a.c0 === b.c0 && a.c1 === b.c1);
    if (view.shift !== shift || view.moving !== moving || !same(view.zoom, zoom)) this.draw();
  }

  /** A new connection: its images start again from seq 1, with empty slots. What's on screen stays. */
  reset() {
    this.generation += 1;
    this.chain = Promise.resolve();
    const old = this.slots;
    this.slots = [null, null];
    this.current = null;
    for (const bitmap of old) this.release(bitmap);
  }

  /** A binary message from the agent. */
  push(data) {
    const generation = this.generation;
    this.chain = this.chain.then(() => this.take(data, generation));
  }

  async take(data, generation) {
    if (generation !== this.generation) return;
    const header = parseHeader(data);
    if (!header) return this.broken();
    if (header.kind === IMAGE) {
      let bitmap;
      try {
        bitmap = await createImageBitmap(new Blob([header.jpeg], { type: "image/jpeg" }));
      } catch {
        return generation === this.generation ? this.broken() : undefined; // not a JPEG
      }
      if (generation !== this.generation) return bitmap.close();
      const replaced = this.slots[header.slot];
      this.slots[header.slot] = bitmap;
      this.release(replaced);
    } else if (!this.slots[header.slot]) {
      return this.broken(); // a repeat of a slot this session never filled
    }
    const previous = this.picture;
    this.current = header;
    this.picture = { header, bitmap: this.slots[header.slot] };
    if (previous) this.release(previous.bitmap);
    this.arriving(header);
    this.draw();
    this.ack(header.seq);
    this.drawn(header);
  }

  /** Closes a bitmap once neither a slot nor the screen holds it. */
  release(bitmap) {
    if (!bitmap || this.slots.includes(bitmap) || this.picture?.bitmap === bitmap) return;
    bitmap.close();
  }

  draw() {
    const width = innerWidth;
    const height = innerHeight;
    const [pixelWidth, pixelHeight] = backing(width, height, devicePixelRatio);
    const { canvas, context } = this;
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.fillStyle = this.letterbox;
    context.fillRect(0, 0, pixelWidth, pixelHeight);
    if (!this.picture) return;
    const { header, bitmap } = this.picture;
    const at = box(width, height, header.w, header.h);
    this.box = at;
    const view = this.view();
    const { shift, zoom } = view;
    this.shown = view;
    context.setTransform(pixelWidth / width, 0, 0, pixelHeight / height, 0, 0); // CSS px from here on
    context.imageSmoothingEnabled = true;
    // The best scaling for a still picture; while it moves, a frame's worth of time matters more.
    context.imageSmoothingQuality = view.moving ? "low" : "high";
    if (!shift && !zoom) {
      context.drawImage(bitmap, at.x, at.y, at.width, at.height);
      return;
    }
    context.save();
    context.beginPath();
    context.rect(at.x, at.y, at.width, at.height);
    context.clip();
    context.fillStyle = this.blank;
    context.fillRect(at.x, at.y, at.width, at.height);
    if (zoom) {
      // The content under the fingers' first centroid follows their centroid now, zoomed by their spread.
      context.translate(zoom.c1[0], zoom.c1[1]);
      context.scale(zoom.f, zoom.f);
      context.translate(-zoom.c0[0], -zoom.c0[1]);
    }
    context.drawImage(bitmap, at.x, at.y - shift * at.k, at.width, at.height);
    context.restore();
  }
}
