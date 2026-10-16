// The viewer's connection to the pod's gate (PROTOCOL.md §2-§3; DESIGN.md §7.2).
//
// It offers `fw.cast1` with the device's nonce, and the pending ticket until the first connect succeeds, from the
// storage the classic viewer's shim uses (fw/session.js). Its first message is `hello` and it sends nothing else
// before the agent's `ready`. Every connection is a fresh session: message numbers start again from `hello`'s 1.
// When a connection closes, core/reconnect.js says what to do next.

import { Numbering, SUBPROTOCOL, decode, encode, hello, resize } from "./core/protocol.js";
import { initialState, nextAction } from "./core/reconnect.js";

const RESIZE_QUIET_MS = 300; // a window being dragged sends one resize when it stops

export class Connection {
  /**
   * `handlers`:
   * - `ready(msg)`: the agent's `ready`, which starts a session;
   * - `message(msg)`: every later JSON message, decoded (unknown types are left out);
   * - `binary(data)`: every binary message, an ArrayBuffer;
   * - `opening()`: a connection is being opened;
   * - `closed({action, prominent})`: a connection closed, and what core/reconnect.js says to do; a reconnect is
   *   already scheduled;
   * - `away(url)`: there is nothing to connect with here, so the page should go to `url`.
   */
  constructor({ trip, gen }, handlers) {
    this.session = window.FwSession;
    this.key = this.session.key(trip, gen);
    this.landing = `/t/${trip}/g/${gen}/`;
    this.url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${this.landing}ws`;
    this.handlers = handlers;
    this.history = initialState();
    this.ws = null;
    this.live = null; // this connection's session: { opened, ready, failed, numbering, size, visible }
    this.timer = 0;
    this.resizing = 0;
    this.left = false;
    addEventListener("resize", () => this.resized());
    addEventListener("orientationchange", () => this.resized());
    document.addEventListener("visibilitychange", () => this.visibility());
    addEventListener("pagehide", (event) => {
      this.left = !event.persisted; // leaving for good, or into the browser's page cache
      this.ws?.close(1000);
    });
    addEventListener("pageshow", (event) => {
      if (event.persisted) this.connect(); // back from the browser's page cache
    });
  }

  /** The number of the latest message a field map must have caught up with (§8.3); 0 before any. */
  get lastInput() {
    return this.live ? this.live.numbering.lastInput : 0;
  }

  /** Opens a connection now, closing the current one. */
  connect() {
    clearTimeout(this.timer);
    if (this.ws) this.drop(this.ws);
    const device = this.session.record(this.key);
    if (this.session.expired(device)) return this.handlers.away("/gone.html");
    const pending = this.session.pendingTicket(this.key);
    const protocols = this.session.offer(device, pending, SUBPROTOCOL);
    if (!protocols) {
      // The landing page sends the user back to Ferry Watch, to open the checkout again.
      const fresh = pending && typeof pending.ticket === "string" ? `#v=s1&t=${pending.ticket}` : "";
      return this.handlers.away(this.landing + fresh);
    }
    const ws = new WebSocket(this.url, protocols);
    ws.binaryType = "arraybuffer";
    const live = { opened: false, ready: false, failed: false, numbering: new Numbering(), size: null, visible: true };
    this.ws = ws;
    this.live = live;
    this.handlers.opening();
    ws.addEventListener("open", () => {
      live.opened = true;
      this.session.opened(this.key);
      const greeting = hello(innerWidth, innerHeight, devicePixelRatio);
      live.size = greeting;
      this.send(greeting);
    });
    ws.addEventListener("message", (event) => this.received(live, event.data));
    ws.addEventListener("close", (event) => this.closed(ws, live, event.code));
  }

  /** Sends a message of this session; its number, or null if it wasn't sent. Never before `ready` but `hello`. */
  send(msg) {
    const { ws, live } = this;
    if (!ws || ws.readyState !== WebSocket.OPEN || !(live.ready || msg.t === "hello")) return null;
    let frame;
    try {
      frame = encode(msg);
    } catch (e) {
      console.error(e.message); // a viewer bug; the reason never quotes what was typed
      return null;
    }
    const number = live.numbering.next(msg.t);
    ws.send(frame);
    return number;
  }

  /** The agent sent something the viewer can't take: an agent bug, handled as a 4500 (§4.2). */
  fail() {
    if (!this.live || this.live.failed) return;
    this.live.failed = true;
    this.ws.close(1000);
  }

  /** The viewer is leaving the page (the classic viewer, Ferry Watch): close for good. */
  leave() {
    this.left = true;
    clearTimeout(this.timer);
    if (this.ws) this.drop(this.ws);
  }

  drop(ws) {
    this.ws = null;
    this.live = null;
    ws.close(1000);
  }

  received(live, data) {
    if (live !== this.live) return;
    if (typeof data !== "string") {
      if (live.ready) this.handlers.binary(data);
      else this.fail(); // images come after `ready`
      return;
    }
    const msg = decode(data);
    if (!msg) return; // a type this viewer doesn't know (§9)
    if (live.ready) {
      this.handlers.message(msg);
    } else if (msg.t === "ready") {
      live.ready = true;
      this.handlers.ready(msg);
      this.visibility();
      this.applySize();
    }
  }

  closed(ws, live, code) {
    if (ws !== this.ws || this.left) return; // replaced or left: nothing to do
    this.ws = null;
    this.live = null;
    const close = {
      opened: live.opened,
      ready: live.ready,
      code: live.failed ? 4500 : code,
      deadlinePassed: this.session.expired(this.session.record(this.key)),
    };
    const next = nextAction(this.history, close);
    this.history = next.state;
    if (next.action.do === "reconnect") this.timer = setTimeout(() => this.connect(), next.action.delay * 1000);
    this.handlers.closed(next);
  }

  visibility() {
    const visible = document.visibilityState !== "hidden";
    if (!this.live?.ready || this.live.visible === visible) return;
    if (this.send({ t: "visible", v: visible }) !== null) this.live.visible = visible;
  }

  resized() {
    clearTimeout(this.resizing);
    this.resizing = setTimeout(() => this.applySize(), RESIZE_QUIET_MS);
  }

  /** A `resize` when the viewport the agent applied isn't the viewer's any more (not for the phone's keyboard). */
  applySize() {
    const live = this.live;
    if (!live?.ready) return;
    const size = resize(innerWidth, innerHeight, devicePixelRatio);
    const applied = live.size;
    if (applied && size.w === applied.w && size.h === applied.h && size.dpr === applied.dpr) return;
    if (this.send(size) !== null) live.size = size;
  }
}
