// This device's checkout session, shared by the landing page, the classic viewer's shim and the streamed viewer
// (docs/multi-tenant/streamed-viewer/DESIGN.md §8.4). A classic script: it defines window.FwSession before any
// module runs, so the streamed viewer's escape hatch works however broken its modules are.
//
// The device keeps one record per checkout in localStorage, `fw:<trip>:<gen>`: its nonce N, whether a connect has
// succeeded, the ticket's display fields and deadlines, and which viewer the checkout opens in. The ticket Ferry
// Watch handed over waits in the tab's sessionStorage, `fw:<trip>:<gen>:ticket`, until the first connect succeeds.
// Neither ever goes into a URL's path or query.
(() => {
  "use strict";
  const LANDING = /^\/t\/([a-f0-9]{16})\/g\/([0-9]{1,6})\/$/; // the landing page's path
  const CLASSIC = /[#&]path=t\/([a-f0-9]{16})\/g\/([0-9]{1,6})\/ws(?:&|$)/; // vnc.html's fragment
  const STREAM = /^#t=([a-f0-9]{16})&g=([0-9]{1,6})$/; // viewer.html's fragment
  const VIEWER = "/fw/viewer.html";
  const WATCHDOG_MS = 3000;

  /** The checkout a landing path, vnc.html fragment or viewer.html fragment names: `{trip, gen}`, or null. */
  function where(pathOrHash) {
    const match = LANDING.exec(pathOrHash) || CLASSIC.exec(pathOrHash) || STREAM.exec(pathOrHash);
    return match ? { trip: match[1], gen: match[2] } : null;
  }

  const key = (trip, gen) => `fw:${trip}:${gen}`;

  function read(store, name) {
    try {
      return JSON.parse(store.getItem(name));
    } catch {
      return null;
    }
  }

  /** False when storage is full or blocked. */
  function write(store, name, value) {
    try {
      store.setItem(name, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  }

  const record = (name) => read(localStorage, name);
  const pendingTicket = (name) => read(sessionStorage, `${name}:ticket`);

  function nonce() {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    return btoa(String.fromCharCode(...bytes))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  /**
   * The subprotocols a viewer offers the pod's gate: its own, the device's nonce, and the pending ticket until
   * the first connect succeeds. Null when the device has nothing to open the checkout with.
   */
  function offer(device, pending, protocol, now = Date.now()) {
    if (!device || typeof device.n !== "string") return null;
    const protocols = [protocol, `fw.n1.${device.n}`];
    if (pending && typeof pending.ticket === "string" && pending.exp * 1000 > now) {
      protocols.push(`fw.t1.${pending.ticket}`);
    } else if (!device.opened) {
      return null;
    }
    return protocols;
  }

  /** A connect succeeded: the gate has locked the cart to this device's nonce, which is all it needs from now on. */
  function opened(name) {
    try {
      sessionStorage.removeItem(`${name}:ticket`);
    } catch {
      // nothing to forget
    }
    const device = record(name);
    if (device && !device.opened) write(localStorage, name, { ...device, opened: true });
  }

  /** The hold's hard deadline has passed: nothing can be opened any more. */
  const expired = (device, now = Date.now()) => Boolean(device) && now >= device.hard_deadline * 1000;

  /** vnc.html, with KasmVNC's client's settings in the fragment, where it reads them first. */
  function classicUrl(trip, gen) {
    const settings = [
      `path=t/${trip}/g/${gen}/ws`,
      "autoconnect=1",
      "reconnect=1",
      "reconnect_delay=1000",
      "idle_disconnect=120",
      // Smoothing on, which also turns off the client's auto-scaling on small high-DPI screens. That makes the
      // screen about 1.5x a phone's viewport (585 px wide on an iPhone's 390), so the checkout shows at two
      // thirds of its size. Without it the screen is the phone's viewport, and the checkout lays out for a phone.
      "anti_aliasing=1",
    ];
    // A touch screen has no keyboard until a text field on this page has focus: the client's floating
    // keyboard button gives it one. A tap on a field in the checkout focuses only the pod's field.
    if (navigator.maxTouchPoints > 0) settings.push("virtual_keyboard_visible=1");
    return `/vnc.html#${settings.join("&")}`;
  }

  const streamUrl = (trip, gen) => `${VIEWER}#t=${trip}&g=${gen}`;

  /** The streamed viewer gave way to the classic one: this checkout opens in the classic viewer on this device. */
  function useClassic(trip, gen) {
    const name = key(trip, gen);
    const device = record(name);
    if (device) write(localStorage, name, { ...device, viewer_fallback: true });
  }

  // The escape hatch (PROTOCOL.md §8.10). viewer.html's own markup holds a hidden bar with a "Use the classic
  // viewer" button. This shows it 3 s after the page starts, unless the viewer has reached its first `ready` by
  // then, and at once on any uncaught error or unhandled rejection, or a script or stylesheet that failed to load.
  // The button works without any of the viewer's modules.
  let ready = false;
  let shown = null; // why the bar is showing: "slow" or "error"

  function showEscape(why) {
    if (why === "slow" && ready) return;
    if (shown !== "error") shown = why;
    placeEscape();
  }

  function placeEscape() {
    const bar = document.getElementById("fw-escape");
    if (bar) bar.hidden = !shown;
  }

  /** The streamed viewer has reached its first `ready`. A bar shown only because it was slow goes away again. */
  function viewerReady() {
    ready = true;
    if (shown === "slow") shown = null;
    placeEscape();
  }

  if (location.pathname === VIEWER) {
    setTimeout(() => showEscape("slow"), WATCHDOG_MS);
    addEventListener("error", () => showEscape("error"), true); // capturing: a script that didn't load too
    addEventListener("unhandledrejection", () => showEscape("error"));
    document.addEventListener("DOMContentLoaded", () => {
      placeEscape();
      const at = where(location.hash);
      const button = document.getElementById("fw-escape-classic");
      if (!at || !button) return;
      button.addEventListener("click", () => {
        useClassic(at.trip, at.gen);
        location.replace(classicUrl(at.trip, at.gen)); // leaving closes the stream's socket
      });
    });
  }

  Object.defineProperty(window, "FwSession", {
    value: Object.freeze({
      where,
      key,
      read,
      write,
      record,
      pendingTicket,
      nonce,
      offer,
      opened,
      expired,
      classicUrl,
      streamUrl,
      useClassic,
      viewerReady,
    }),
  });
})();
