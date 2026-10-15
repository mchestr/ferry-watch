// The streamed checkout viewer (docs/multi-tenant/streamed-viewer/DESIGN.md §7), the module viewer.html loads.
//
// viewer.html#t=<trip>&g=<gen> shows the checkout the pod's agent streams over the gate's WebSocket (fw.cast1,
// PROTOCOL.md). The landing page opened it with a pending ticket and the device's nonce in storage. Everything the
// page shows of the checkout is drawn on the canvas, so nothing the checkout's own pages do can cover the
// viewer's controls; fw/session.js keeps the "Use the classic viewer" escape working even if this module doesn't.

import { Frames } from "./frames.js";
import { Status } from "./sheets.js";
import { Connection } from "./socket.js";

const session = window.FwSession;
const at = session.where(location.hash);
let connection = null;

const status = new Status(document, { here: () => connection?.connect(), classic: toClassic });

if (at) start(at);
else status.show("nothing");

function start(checkout) {
  let showing = false; // this connection has drawn an image
  const frames = new Frames(document.getElementById("screen"), {
    ack: (seq) => connection.send({ t: "ack", seq }),
    broken: () => connection.fail(),
    drawn: () => {
      if (showing) return;
      showing = true;
      status.hide();
    },
  });
  const state = { ready: null, pages: null, clock: null };

  // Each of the agent's JSON messages after `ready`, by type. A type without a handler is ignored.
  const on = {
    pages: (msg) => (state.pages = msg),
    clock: (msg) => (state.clock = msg),
    error: (msg) => console.warn(`The checkout's agent didn't carry out a ${msg.ref}: ${msg.code}`),
  };

  connection = new Connection(checkout, {
    opening() {
      frames.reset();
      showing = false;
      if (status.showing !== "reconnecting") status.show("opening");
    },
    ready(msg) {
      state.ready = msg;
      session.viewerReady();
    },
    message(msg) {
      on[msg.t]?.(msg);
    },
    binary: (data) => frames.push(data),
    closed({ action, prominent }) {
      if (action.do === "reconnect") status.show("reconnecting", { prominent });
      else if (action.do === "stop") status.show(action.show ?? "stopped", { prominent });
      else if (action.do === "classic") toClassic();
      else away("/gone.html");
    },
    away,
  });
  connection.connect();
}

/** "Use the classic viewer" (PROTOCOL.md §8.10): sticky for this checkout on this device. */
function toClassic() {
  session.useClassic(at.trip, at.gen);
  away(session.classicUrl(at.trip, at.gen));
}

function away(url) {
  connection?.leave();
  location.replace(url);
}
