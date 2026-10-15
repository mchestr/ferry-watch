// The streamed checkout viewer (docs/multi-tenant/streamed-viewer/DESIGN.md §7), the module viewer.html loads.
//
// viewer.html#t=<trip>&g=<gen> shows the checkout the pod's agent streams over the gate's WebSocket (fw.cast1,
// PROTOCOL.md). The landing page opened it with a pending ticket and the device's nonce in storage. Everything the
// page shows of the checkout is drawn on the canvas, so nothing the checkout's own pages do can cover the
// viewer's controls; fw/session.js keeps the "Use the classic viewer" escape working even if this module doesn't.

import { fieldAt, findSelect, mapUsable, withOk } from "./core/fields.js";
import { Frames } from "./frames.js";
import { Input } from "./input.js";
import { Keyboard } from "./keyboard.js";
import { Overlays } from "./overlays.js";
import { DialogSheet, Menu, OptionSheet, Status } from "./sheets.js";
import { Connection } from "./socket.js";

const session = window.FwSession;
const at = session.where(location.hash);
const computer = matchMedia("(pointer: fine)").matches; // a mouse and a keyboard, not a touch screen
const FOCUS_KEYS = new Set(["Tab", "Enter", "Escape"]);
let connection = null;

const status = new Status(document, { here: () => connection?.connect(), classic: toClassic });

if (at) start(at);
else status.show("nothing");

function start(checkout) {
  const $ = (id) => document.getElementById(id);
  let showing = false; // this connection has drawn an image
  let drawing = 0; // a redraw is waiting for the next animation frame
  let live = fresh();

  /** What the agent has said in this connection. */
  function fresh() {
    return {
      pages: null,
      clock: null,
      map: null, // the latest field map, with its fields_oks folded in
      focus: null, // the latest focus report that is about what the user has done
      focusMoved: 0, // the number of the latest message that may have moved the pod's focus
    };
  }

  /** What is drawn: the image of this connection and its box, or null. */
  const screen = () => (frames.current && frames.box ? { image: frames.current, box: frames.box } : null);

  const frames = new Frames($("screen"), {
    ack: (seq) => connection.send({ t: "ack", seq }),
    broken: () => connection.fail(),
    arriving: (header) => input.gestures.image(header),
    drawn: () => {
      refresh();
      if (showing) return;
      showing = true;
      status.hide();
    },
    view: () => input.gestures.view(),
  });
  const input = new Input($("stage"), {
    screen,
    send: gestured,
    changed: redraw,
    contentHeight: () => (live.map?.page === frames.current?.page ? live.map.ch : null),
  });
  const keyboard = new Keyboard(document, { send, screen, computer });
  const overlays = new Overlays(document, { send });
  const dialogs = new DialogSheet(document, {
    answer: (id, ok, s) => send(s === undefined ? { t: "dialog", id, ok } : { t: "dialog", id, ok, s }),
  });
  const options = new OptionSheet(document, {
    pick: chose,
    cancel: () => send({ t: "key", k: "Escape" }),
    classic: toClassic,
  });
  const menu = new Menu(document, {
    show: (page) => send({ t: "page", op: "show", page }),
    close: (page) => send({ t: "page", op: "close", page }),
    paste: (text) => keyboard.paste(text),
    classic: toClassic,
  });
  addEventListener("resize", refresh);

  // Each of the agent's JSON messages after `ready`, by type. A type without a handler is ignored.
  const on = {
    pages(msg) {
      live.pages = msg;
      menu.update(live);
      dialogs.setPages(msg);
    },
    clock(msg) {
      live.clock = msg;
      menu.update(live);
    },
    fields(msg) {
      live.map = msg;
      refresh();
    },
    fields_ok(msg) {
      live.map = withOk(live.map, msg);
      refresh();
    },
    focus: focused,
    dialog: (msg) => dialogs.add(msg),
    dialog_closed: (msg) => dialogs.remove(msg.id),
    error: (msg) => console.warn(`The checkout's agent didn't carry out a ${msg.ref}: ${msg.code}`),
  };

  connection = new Connection(checkout, {
    opening() {
      frames.reset();
      input.gestures.reset();
      keyboard.reset();
      dialogs.reset();
      options.hide();
      overlays.clear();
      live = fresh();
      menu.update(live);
      showing = false;
      if (status.showing !== "reconnecting") status.show("opening");
    },
    ready() {
      session.viewerReady();
    },
    message(msg) {
      on[msg.t]?.(msg);
    },
    binary: (data) => frames.push(data),
    closed({ action, prominent }) {
      menu.close();
      if (action.do === "reconnect") status.show("reconnecting", { prominent });
      else if (action.do === "stop") status.show(action.show ?? "stopped", { prominent });
      else if (action.do === "classic") toClassic();
      else away("/gone.html");
    },
    away,
  });
  connection.connect();

  /** Sends a message of this connection; its number, or null. Keeps track of what may move the pod's focus. */
  function send(msg) {
    const number = connection.send(msg);
    const moves = ["tap", "choose", "date", "dialog", "page"].includes(msg.t);
    if (number !== null && (moves || (msg.t === "key" && FOCUS_KEYS.has(msg.k) && msg.ph !== "up"))) {
      live.focusMoved = number;
    }
    if (number !== null && msg.t !== "ack" && msg.t !== "visible" && msg.t !== "move") refresh();
    return number;
  }

  /** A gesture's message. A tap on a field the map knows opens the phone's keyboard inside the tap, first. */
  function gestured(msg) {
    if (msg.t === "tap") {
      overlays.release();
      const map = usableMap();
      keyboard.tapped(map ? fieldAt(map, msg.x, msg.y) : null, map !== null);
    }
    send(msg);
  }

  /** The field map, if it describes exactly what is drawn (PROTOCOL.md §8.3). */
  function usableMap() {
    const { shift, zoom } = input.gestures.view();
    const { map } = live;
    return mapUsable(map, frames.current, connection.lastInput, shift !== 0 || zoom !== null) ? map : null;
  }

  /** Lays the native pickers over what is drawn now, if anything may have one. */
  function refresh() {
    overlays.update(input.gestures.busy ? null : usableMap(), frames.current, frames.box);
  }

  function redraw() {
    if (drawing) return;
    drawing = requestAnimationFrame(() => {
      drawing = 0;
      frames.update();
      refresh();
    });
  }

  /** The agent's report of the pod's focused field (§5.4), if it is about what the user last did. */
  function focused(msg) {
    if (msg.page !== frames.current?.page || msg.n < live.focusMoved) return;
    live.focus = msg;
    keyboard.report(msg.f);
    if (msg.popup && msg.f?.k === "select") options.show(msg.f);
    else if (options.isOpen) options.hide();
  }

  /** An option of the option sheet: for the select of the map that the pod opened. */
  function chose(index) {
    const { map, focus } = live;
    const field = findSelect(map, focus?.f);
    if (field) send({ t: "choose", page: map.page, rev: map.rev, f: field.id, i: index });
    else send({ t: "key", k: "Escape" }); // the map hasn't caught up: close the pod's list, and the user taps again
  }
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
