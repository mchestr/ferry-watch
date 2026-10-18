// The viewer's pointer input (DESIGN.md §7.4): the gesture layer's touches, clicks and wheels, given to
// core/gestures.js on the page's own clock.
//
// The gesture layer covers the screen above the canvas and the keyboard proxy, with `touch-action: none`, so the
// viewer's page itself never scrolls or zooms. The native pickers over fields (overlays.js) sit above it and take
// their own taps, which open the platform's picker and are never sent; a drag that starts on one still scrolls.

import { Gestures } from "./core/gestures.js";

const clock = {
  now: () => performance.now(),
  later: (fn, ms) => setTimeout(fn, ms),
  cancel: (id) => clearTimeout(id),
  frame: (fn) => requestAnimationFrame(fn),
  cancelFrame: (id) => cancelAnimationFrame(id),
};

export class Input {
  /** `stage` holds the gesture layer and the overlays; the rest is core/gestures.js's. */
  constructor(stage, { screen, send, changed, contentHeight }) {
    this.gestures = new Gestures({ clock, screen, send: (msg) => this.sent(msg), changed, contentHeight });
    this.send = send;
    this.fingers = []; // the identifiers of the fingers down, in the order they touched
    this.onPicker = false; // the touch or press began on a native picker, which takes it if it's a tap
    const touches = (handler) => (event) => this.touched(event, handler);
    const options = { passive: false };
    stage.addEventListener("touchstart", touches("touchstart"), options);
    stage.addEventListener("touchmove", touches("touchmove"), options);
    stage.addEventListener("touchend", touches("touchend"), options);
    stage.addEventListener("touchcancel", (event) => this.cancelled(event), options);
    stage.addEventListener("pointermove", (event) => {
      if (event.pointerType === "mouse") this.gestures.pointermove(event.clientX, event.clientY);
    });
    stage.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "mouse" || event.button !== 0) return;
      this.onPicker = picker(event);
      if (!this.onPicker) this.gestures.pointerdown(event.clientX, event.clientY);
    });
    // A click on the checkout leaves the keyboard proxy its focus; a picker's opens it.
    stage.addEventListener("mousedown", (event) => picker(event) || event.preventDefault());
    stage.addEventListener("pointerup", (event) => {
      if (event.pointerType === "mouse" && event.button === 0) this.gestures.pointerup(event.clientX, event.clientY);
    });
    stage.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault(); // the viewer's page never scrolls or zooms
        if (event.ctrlKey) return; // a trackpad's pinch, or the browser's zoom: not sent
        this.gestures.wheel(event.clientX, event.clientY, event.deltaX, event.deltaY, event.deltaMode);
      },
      options,
    );
  }

  touched(event, handler) {
    if (handler === "touchstart" && this.fingers.length === 0) this.onPicker = picker(event);
    const touches = [...event.touches];
    this.fingers = this.fingers.filter((id) => touches.some((touch) => touch.identifier === id));
    for (const touch of touches) if (!this.fingers.includes(touch.identifier)) this.fingers.push(touch.identifier);
    const down = this.fingers.map((id) => {
      const touch = touches.find((each) => each.identifier === id);
      return [touch.clientX, touch.clientY];
    });
    this.gestures[handler](down);
    // A picker's own tap must reach it, so its touches go on to the browser unless they scroll or pinch.
    if (!this.onPicker || this.gestures.busy) event.preventDefault();
  }

  cancelled(event) {
    event.preventDefault();
    this.fingers = [];
    this.gestures.touchcancel();
  }

  sent(msg) {
    if (msg.t === "tap" && this.onPicker) return; // the picker opened instead
    this.send(msg);
  }
}

const picker = (event) => Boolean(event.target.closest?.("#overlays > *"));
