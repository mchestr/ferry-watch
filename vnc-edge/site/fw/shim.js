// Ferry Watch's checkout shim. vnc.html loads it before KasmVNC's client, which is stock
// (docs/multi-tenant/design-reference.md §7.9). build.py serves it as one file behind fw/session.js, which keeps
// the device's checkout session, so vnc.html still has exactly one inserted script.
//
// The client opens its RFB socket with one `new WebSocket(url, ["binary"])`. For this checkout's
// own socket, and only that one, the shim offers the device's nonce (`fw.n1.<N>`) and, until the
// first connect succeeds, the checkout ticket (`fw.t1.<ticket>`) the landing page left in this tab.
// The pod's gate checks both. If the client stops retrying before the hold could have ended, the
// shim reloads the page, backing off; after the hold's hard deadline it gives up.
//
// Below that, two separate pieces make the checkout behave more like a page on a phone. They rely on
// the client's DOM and its gesture events, so each does nothing if those change.
(() => {
  "use strict";
  const session = window.FwSession; // fw/session.js, bundled ahead of this file
  const at = session.where(location.hash);
  if (!at) return;
  const { trip, gen } = at;
  const key = session.key(trip, gen);
  const landing = `/t/${trip}/g/${gen}/`;
  const socketPath = `${landing}ws`;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const RELOAD_BACKOFF_S = [1, 2, 5, 10];
  const GIVE_UP_MS = 4000; // the client retries a second after a close (reconnect_delay)

  const Native = window.WebSocket;
  let giveUp;

  function ours(url) {
    let target;
    try {
      target = new URL(url, location.href);
    } catch {
      return false; // let the real constructor throw its own error
    }
    return (
      target.protocol === scheme &&
      target.host === location.host &&
      target.pathname === socketPath &&
      !target.search &&
      !target.hash
    );
  }

  function opened() {
    session.opened(key);
    try {
      sessionStorage.removeItem(`${key}:reloads`);
    } catch {
      // nothing to forget
    }
  }

  function closed() {
    clearTimeout(giveUp);
    giveUp = setTimeout(() => {
      const reloads = Number(session.read(sessionStorage, `${key}:reloads`)) || 0;
      session.write(sessionStorage, `${key}:reloads`, reloads + 1);
      const delay = RELOAD_BACKOFF_S[Math.min(reloads, RELOAD_BACKOFF_S.length - 1)];
      setTimeout(() => location.reload(), delay * 1000);
    }, GIVE_UP_MS);
  }

  function FerryWebSocket(url, protocols) {
    if (!new.target) throw new TypeError("Failed to construct 'WebSocket': Please use the 'new' operator.");
    if (!ours(url)) return new Native(url, protocols);
    clearTimeout(giveUp);
    const record = session.record(key);
    if (session.expired(record)) {
      location.replace("/gone.html");
      throw new Error("This checkout has ended");
    }
    const protocolsToOffer = session.offer(record, session.pendingTicket(key), "binary");
    if (!protocolsToOffer) {
      // The landing page sends the user back to Ferry Watch, to open the checkout again.
      const pending = session.pendingTicket(key);
      location.replace(pending && typeof pending.ticket === "string" ? `${landing}#${pending.ticket}` : landing);
      throw new Error("No checkout ticket");
    }
    // A real WebSocket, not a subclass: the client checks the object's own prototype.
    const socket = new Native(url, protocolsToOffer);
    socket.addEventListener("open", opened);
    socket.addEventListener("close", closed);
    return socket;
  }
  FerryWebSocket.prototype = Native.prototype;
  Object.setPrototypeOf(FerryWebSocket, Native); // CONNECTING, OPEN, CLOSING, CLOSED
  window.WebSocket = FerryWebSocket;
})();

// Keep the client's own controls off the checkout. It opens its side panel on load and closes it 2 s after
// connecting, but on a phone the panel covers most of the screen, and a tap on it in those 2 s keeps it open:
// close it as the client connects (its handle still opens it). The client's own close would then take the focus
// back from the phone's keyboard, if the keyboard button had been tapped in those 2 s, so it's cancelled as the
// client cancels it when the panel is in use: an `input` event on the panel. The client arms it on every
// connect, reconnects included. It's cancelled only once the panel has opened with the class this removes: if
// the client named it differently, cancelling its close would leave the panel open. The client also names its
// stream mode in large type over the top of the screen for a few seconds.
(() => {
  "use strict";
  const root = document.documentElement;
  let known = false;
  let connected = false;
  new MutationObserver(() => {
    const panel = document.getElementById("noVNC_control_bar");
    known = known || Boolean(panel && panel.classList.contains("noVNC_open"));
    const now = root.classList.contains("noVNC_connected");
    if (now && !connected && known) {
      panel.dispatchEvent(new Event("input")); // the panel's own listeners only: it doesn't bubble
      panel.classList.remove("noVNC_open");
    }
    connected = now;
  }).observe(root, { attributes: true, attributeFilter: ["class"] });
  document.addEventListener("DOMContentLoaded", () => {
    const toast = document.getElementById("noVNC_notification_overlay");
    if (toast) toast.style.setProperty("display", "none", "important");
  });
})();

// One finger scrolls the checkout, as it would a page, instead of dragging the mouse across it (which
// selects text). The client turns touches into gesture events on its canvas; this listener sees them first,
// takes the one-finger drag, and gives it back to the client as wheel events, then coasts after a flick.
// Taps, long presses, two-finger scrolls, pinches and the mouse reach the client as before. If the client
// stops sending these events, or stops taking wheel events, a drag is the client's own again.
(() => {
  "use strict";
  const SCALE = 0.5; // Xvnc scrolls Chromium about twice a wheel delta: half tracks the finger
  const HELD_MS = 80; // a finger that rests this long before lifting doesn't coast
  const COAST_FROM = 0.3; // CSS px per ms at release
  const COAST_TO = 0.05;
  const COAST_DECAY = 0.995; // per ms, so a flick coasts about 200 ms worth of its speed
  let touchedAt = 0;
  let drag = null;
  let coast = 0;
  let tapStopsCoast = false;

  /** Hands the client a wheel event; true if it took it (it cancels each one it sends to the pod). */
  function wheel(target, axis, delta) {
    const event = new WheelEvent("wheel", {
      clientX: target.x,
      clientY: target.y,
      deltaX: axis === "x" ? delta : 0,
      deltaY: axis === "y" ? delta : 0,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
      cancelable: true,
    });
    return !target.canvas.dispatchEvent(event);
  }

  /** Scrolls by `pixels` of finger travel. The client sends whole units, so a fraction waits for the next. */
  function scroll(target, pixels) {
    target.owed += pixels * SCALE;
    const whole = Math.trunc(target.owed);
    if (!whole) return;
    target.owed -= whole;
    wheel(target, target.axis, whole);
  }

  function glide(target, speed) {
    let then = performance.now();
    const step = (now) => {
      const elapsed = Math.max(0, now - then);
      then = now;
      speed *= COAST_DECAY ** elapsed;
      if (Math.abs(speed) < COAST_TO) {
        coast = 0;
        return;
      }
      scroll(target, speed * elapsed);
      coast = requestAnimationFrame(step);
    };
    coast = requestAnimationFrame(step);
  }

  function stopCoast() {
    cancelAnimationFrame(coast);
    coast = 0;
  }

  function onTouch(event) {
    if (event.touches.length !== 1) return;
    touchedAt = performance.now();
    tapStopsCoast = coast !== 0; // as in a browser, a tap that stops a coast doesn't click
    stopCoast();
  }

  function onGesture(event) {
    const gesture = event instanceof CustomEvent ? event.detail : null;
    if (!gesture) return;
    if (gesture.type === "onetap" && tapStopsCoast) {
      event.stopImmediatePropagation();
      return;
    }
    if (gesture.type !== "drag") return;
    const now = performance.now();
    if (event.type === "gesturestart") {
      const at = { canvas: event.target, x: gesture.clientX, y: gesture.clientY };
      // A wheel event that moves nothing: if the client doesn't take it, the drag stays the client's.
      drag = wheel(at, "y", 0)
        ? { ...at, axis: null, owed: 0, lastX: at.x, lastY: at.y, movedAt: touchedAt || now, speed: 0 }
        : null;
    }
    if (!drag) return;
    event.stopImmediatePropagation();
    if (event.type === "gesturemove") {
      const dx = drag.lastX - gesture.clientX;
      const dy = drag.lastY - gesture.clientY;
      // Like a browser, a drag scrolls along the axis it starts on; the first move is the client's 50 px.
      drag.axis = drag.axis || (Math.abs(dx) > Math.abs(dy) ? "x" : "y");
      const moved = drag.axis === "x" ? dx : dy;
      drag.speed = 0.8 * (moved / Math.max(1, now - drag.movedAt)) + 0.2 * drag.speed;
      drag.lastX = gesture.clientX;
      drag.lastY = gesture.clientY;
      drag.movedAt = now;
      scroll(drag, moved);
    } else if (event.type === "gestureend") {
      if (drag.axis && now - drag.movedAt < HELD_MS && Math.abs(drag.speed) > COAST_FROM) glide(drag, drag.speed);
      drag = null;
    }
  }

  addEventListener("touchstart", onTouch, { capture: true, passive: true });
  for (const type of ["gesturestart", "gesturemove", "gestureend"]) addEventListener(type, onGesture, true);
})();
