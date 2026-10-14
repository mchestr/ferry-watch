// Ferry Watch's checkout shim. vnc.html loads it before KasmVNC's client, which is stock
// (docs/multi-tenant/design-reference.md §7.9).
//
// The client opens its RFB socket with one `new WebSocket(url, ["binary"])`. For this checkout's
// own socket, and only that one, the shim offers the device's nonce (`fw.n1.<N>`) and, until the
// first connect succeeds, the checkout ticket (`fw.t1.<ticket>`) the landing page left in this tab.
// The pod's gate checks both. If the client stops retrying before the hold could have ended, the
// shim reloads the page, backing off; after the hold's hard deadline it gives up.
(() => {
  "use strict";
  const params = /[#&]path=t\/([a-f0-9]{16})\/g\/([0-9]{1,6})\/ws(?:&|$)/.exec(location.hash);
  if (!params) return;
  const [, trip, gen] = params;
  const key = `fw:${trip}:${gen}`;
  const landing = `/t/${trip}/g/${gen}/`;
  const socketPath = `${landing}ws`;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const RELOAD_BACKOFF_S = [1, 2, 5, 10];
  const GIVE_UP_MS = 4000; // the client retries a second after a close (reconnect_delay)

  const read = (store, name) => {
    try {
      return JSON.parse(store.getItem(name));
    } catch {
      return null;
    }
  };
  const write = (store, name, value) => {
    try {
      store.setItem(name, JSON.stringify(value));
    } catch {
      // Storage is full or blocked: the next connect fails and the landing page explains.
    }
  };

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

  /** The subprotocols for this device, or null when it has nothing to open the checkout with. */
  function offer(record) {
    if (!record || typeof record.n !== "string") return null;
    const protocols = ["binary", `fw.n1.${record.n}`];
    const pending = read(sessionStorage, `${key}:ticket`);
    if (pending && typeof pending.ticket === "string" && pending.exp * 1000 > Date.now()) {
      protocols.push(`fw.t1.${pending.ticket}`);
    } else if (!record.opened) {
      return null;
    }
    return protocols;
  }

  function opened() {
    try {
      sessionStorage.removeItem(`${key}:ticket`);
      sessionStorage.removeItem(`${key}:reloads`);
    } catch {
      // nothing to forget
    }
    const record = read(localStorage, key);
    if (record && !record.opened) write(localStorage, key, { ...record, opened: true });
  }

  function closed() {
    clearTimeout(giveUp);
    giveUp = setTimeout(() => {
      const reloads = Number(read(sessionStorage, `${key}:reloads`)) || 0;
      write(sessionStorage, `${key}:reloads`, reloads + 1);
      const delay = RELOAD_BACKOFF_S[Math.min(reloads, RELOAD_BACKOFF_S.length - 1)];
      setTimeout(() => location.reload(), delay * 1000);
    }, GIVE_UP_MS);
  }

  function FerryWebSocket(url, protocols) {
    if (!new.target) throw new TypeError("Failed to construct 'WebSocket': Please use the 'new' operator.");
    if (!ours(url)) return new Native(url, protocols);
    clearTimeout(giveUp);
    const record = read(localStorage, key);
    if (record && Date.now() >= record.hard_deadline * 1000) {
      location.replace("/gone.html");
      throw new Error("This checkout has ended");
    }
    const protocolsToOffer = offer(record);
    if (!protocolsToOffer) {
      // The landing page sends the user back to Ferry Watch, to open the checkout again.
      const pending = read(sessionStorage, `${key}:ticket`);
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
