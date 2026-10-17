// wl.cast1, the streamed checkout viewer's wire protocol (docs/multi-tenant/streamed-viewer/PROTOCOL.md): what the
// viewer may send, checked before it goes; the agent's messages, read as far as the viewer knows them; the images'
// binary header. DOM-free: `node --test` checks it against the shared conformance pack (tests/fixtures/cast1/).

import { viewport } from "./layout.js";
import { MAX_COMPOSE, MAX_DIALOG, MAX_TEXT, MAX_TEXT_BYTES, codePoints, isClean, isTyped } from "./text.js";

export const SUBPROTOCOL = "wl.cast1";
export const VERSION = 1;
export const MAX_FRAME_BYTES = 4096;
export const HEADER_BYTES = 32;
export const IMAGE = 1;
export const REPEAT = 2;

const U16 = 0xffff;
const U32 = 0xffffffff;
const MAX_X = 1920; // an image is at most the largest viewport a viewer can ask for
const MAX_Y = 1600;
const MAX_WHEEL = 4000;

export const NAMED_KEYS = Object.freeze([
  "Backspace",
  "Delete",
  "Enter",
  "Tab",
  "Escape",
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);
const WORD_KEYS = new Set(["Backspace", "Delete", "ArrowLeft", "ArrowRight"]);

// The messages §8.3 waits for: a field map is usable only once the agent has carried out the latest of these.
const INPUTS = new Set([
  "tap",
  "wheel",
  "pinch",
  "key",
  "text",
  "compose",
  "choose",
  "date",
  "dialog",
  "page",
  "resize",
]);

const utf8 = new TextEncoder();
const isObject = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const between = (v, low, high) => v >= low && v <= high;

// ---- viewer → agent (§6)

// Each type's fields and their JSON types; a trailing "?" marks an optional field, which is absent, never null.
const SENT = {
  hello: { v: "integer", w: "integer", h: "integer", dpr: "number" },
  resize: { w: "integer", h: "integer", dpr: "number" },
  ack: { seq: "integer" },
  tap: { page: "integer", seq: "integer", x: "number", y: "number", "ms?": "integer" },
  move: { page: "integer", x: "number", y: "number" },
  wheel: { page: "integer", x: "number", y: "number", dx: "number", dy: "number" },
  visible: { v: "boolean" },
  pinch: {
    page: "integer",
    seq: "integer",
    x0: "number",
    y0: "number",
    x1: "number",
    y1: "number",
    scale: "number",
  },
  key: { "k?": "string", "c?": "string", "shift?": "boolean", "word?": "boolean", "ph?": "string" },
  text: { s: "string" },
  compose: { s: "string" },
  choose: { page: "integer", rev: "integer", f: "integer", i: "integer" },
  date: { page: "integer", rev: "integer", f: "integer", v: "string" },
  dialog: { id: "integer", ok: "boolean", "s?": "string" },
  page: { op: "string", page: "integer" },
};

const JSON_TYPES = {
  integer: (v) => typeof v === "number" && Number.isInteger(v),
  number: (v) => typeof v === "number" && Number.isFinite(v),
  boolean: (v) => typeof v === "boolean",
  string: (v) => typeof v === "string",
};

/** §6 step 1: what the agent closes 4400 for. */
function structure(msg) {
  if (!isObject(msg) || typeof msg.t !== "string" || !Object.hasOwn(SENT, msg.t)) return "not a viewer message";
  const fields = SENT[msg.t];
  const known = new Set(Object.keys(fields).map((name) => name.replace(/\?$/, "")));
  for (const name of Object.keys(msg)) if (name !== "t" && !known.has(name)) return `${msg.t}: unknown field ${name}`;
  for (const [field, type] of Object.entries(fields)) {
    const name = field.replace(/\?$/, "");
    if (!Object.hasOwn(msg, name)) {
      if (!field.endsWith("?")) return `${msg.t}: no ${name}`;
    } else if (!JSON_TYPES[type](msg[name])) {
      return `${msg.t}: ${name} isn't a JSON ${type}`;
    }
  }
  return null;
}

const page = (v) => between(v, 1, U16);
const seq = (v) => between(v, 1, U32);
const point = (x, y) => between(x, 0, MAX_X) && between(y, 0, MAX_Y);
const size = (m) => between(m.w, 240, 1920) && between(m.h, 240, 1600) && between(m.dpr, 0.5, 5);

function realDate(v) {
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(v);
  if (!match) return false;
  const [year, month, day] = match.slice(1).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  return year >= 1 && days !== undefined && between(day, 1, days);
}

const clockTime = (v) => /^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(v);

function keyOk({ k, c, shift = false, word = false, ph }) {
  if ((k === undefined) === (c === undefined)) return false; // exactly one of them
  if (ph !== undefined && (!["down", "up"].includes(ph) || shift || word)) return false;
  if (c !== undefined) return codePoints(c) === 1 && isClean(c) && !shift && !word;
  if (k === "Shift") return ph !== undefined;
  return NAMED_KEYS.includes(k) && (!word || WORD_KEYS.has(k));
}

/** §6 step 2: what the agent drops with `error invalid`. */
const VALUES = {
  hello: (m) => m.v === VERSION && size(m),
  resize: size,
  ack: (m) => seq(m.seq),
  tap: (m) => page(m.page) && seq(m.seq) && point(m.x, m.y) && (m.ms === undefined || between(m.ms, 0, 300)),
  move: (m) => page(m.page) && point(m.x, m.y),
  wheel: (m) =>
    page(m.page) && point(m.x, m.y) && between(m.dx, -MAX_WHEEL, MAX_WHEEL) && between(m.dy, -MAX_WHEEL, MAX_WHEEL),
  visible: () => true,
  pinch: (m) => page(m.page) && seq(m.seq) && point(m.x0, m.y0) && point(m.x1, m.y1) && between(m.scale, 0.25, 4),
  key: keyOk,
  text: (m) => between(codePoints(m.s), 1, MAX_TEXT) && isTyped(m.s) && utf8.encode(m.s).length <= MAX_TEXT_BYTES,
  compose: (m) => codePoints(m.s) <= MAX_COMPOSE && isClean(m.s),
  choose: (m) => page(m.page) && between(m.rev, 0, U32) && between(m.f, 1, U16) && between(m.i, 0, 399),
  date: (m) => page(m.page) && between(m.rev, 0, U32) && between(m.f, 1, U16) && (realDate(m.v) || clockTime(m.v)),
  dialog: (m) => between(m.id, 1, U16) && (m.s === undefined || (codePoints(m.s) <= MAX_DIALOG && isClean(m.s))),
  page: (m) => ["show", "close"].includes(m.op) && page(m.page),
};

/** Why the agent wouldn't carry out `msg` as it is, or null. The reason never quotes a value. */
export function check(msg) {
  const problem = structure(msg);
  if (problem) return problem;
  return VALUES[msg.t](msg) ? null : `${msg.t}: a value breaks PROTOCOL.md §6`;
}

/** The text frame for a message; throws if the agent wouldn't carry it out. */
export function encode(msg) {
  const problem = check(msg);
  if (problem) throw new TypeError(`wl.cast1: ${problem}`);
  const frame = JSON.stringify(msg);
  if (utf8.encode(frame).length > MAX_FRAME_BYTES) throw new TypeError(`wl.cast1: ${msg.t} is too long`);
  return frame;
}

export const hello = (innerWidth, innerHeight, devicePixelRatio) => ({
  t: "hello",
  v: VERSION,
  ...viewport(innerWidth, innerHeight, devicePixelRatio),
});

export const resize = (innerWidth, innerHeight, devicePixelRatio) => ({
  t: "resize",
  ...viewport(innerWidth, innerHeight, devicePixelRatio),
});

/**
 * Message numbers (§1): `hello` is 1 and every later message but `ack` the next. `lastInput` is the number of the
 * latest message a field map must have caught up with to be usable (§8.3).
 */
export class Numbering {
  count = 0;
  lastInput = 0;

  /** Numbers a message about to be sent; null for an `ack`. */
  next(t) {
    if (t === "ack") return null;
    this.count += 1;
    if (INPUTS.has(t)) this.lastInput = this.count;
    return this.count;
  }
}

// ---- agent → viewer (§4, §5)

const INVALID = Symbol("invalid");
const take = {
  integer: (v) => (JSON_TYPES.integer(v) ? v : INVALID),
  number: (v) => (JSON_TYPES.number(v) ? v : INVALID),
  boolean: (v) => (JSON_TYPES.boolean(v) ? v : INVALID),
  string: (v) => (JSON_TYPES.string(v) ? v : INVALID),
};
const nullable = (inner) => (v) => (v === null ? null : inner(v));

function list(inner) {
  return (v) => {
    if (!Array.isArray(v)) return INVALID;
    const out = v.map(inner);
    return out.includes(INVALID) ? INVALID : out;
  };
}

function tuple(...inners) {
  return (v) => {
    if (!Array.isArray(v) || v.length !== inners.length) return INVALID;
    const out = inners.map((inner, i) => inner(v[i]));
    return out.includes(INVALID) ? INVALID : out;
  };
}

/** The known fields of an object, checked; the rest (a later agent's additions, §9) are left behind. */
function shape(fields) {
  return (v) => {
    if (!isObject(v)) return INVALID;
    const out = {};
    for (const [field, inner] of Object.entries(fields)) {
      const name = field.replace(/\?$/, "");
      if (!Object.hasOwn(v, name)) {
        if (field.endsWith("?")) continue;
        return INVALID;
      }
      const value = inner(v[name]);
      if (value === INVALID) return INVALID;
      out[name] = value;
    }
    return out;
  };
}

const rect = tuple(take.number, take.number, take.number, take.number);
const FIELD = {
  k: take.string,
  r: rect,
  c: rect,
  m: take.integer,
  type: take.string,
  im: take.string,
  ac: take.string,
  ekh: take.string,
  cap: take.string,
  max: nullable(take.integer),
  ro: take.boolean,
  "nopt?": take.integer,
  "opts?": list(tuple(take.string, take.boolean)),
  "sel?": take.integer,
  "trunc?": take.boolean,
  "v?": take.string,
};
const map = { page: take.integer, rev: take.integer, seq: take.integer, n: take.integer };

const RECEIVED = {
  ready: shape({
    t: take.string,
    v: take.integer,
    w: take.integer,
    h: take.integer,
    dsf: take.integer,
    "features?": list(take.string),
  }),
  pages: shape({
    t: take.string,
    front: take.integer,
    pages: list(shape({ id: take.integer, title: take.string, origin: take.string, main: take.boolean })),
  }),
  fields: shape({
    t: take.string,
    ...map,
    sx: take.number,
    sy: take.number,
    scale: take.number,
    cw: take.number,
    ch: take.number,
    trunc: take.boolean,
    f: list(shape({ id: take.integer, ...FIELD })),
  }),
  fields_ok: shape({ t: take.string, ...map }),
  focus: shape({
    t: take.string,
    page: take.integer,
    seq: take.integer,
    n: take.integer,
    f: nullable(shape(FIELD)),
    popup: take.boolean,
  }),
  dialog: shape({
    t: take.string,
    id: take.integer,
    page: take.integer,
    kind: take.string,
    origin: take.string,
    "main?": take.boolean, // absent: a frame inside the page (§5.5), never the page itself
    msg: take.string,
    def: take.string,
  }),
  dialog_closed: shape({ t: take.string, id: take.integer }),
  error: shape({ t: take.string, code: take.string, ref: take.string }),
  clock: shape({ t: take.string, ends: nullable(take.number), hard: take.number, paused: take.boolean }),
};

/**
 * An agent's text frame as a message the viewer knows, with only the fields it knows; null for anything else (a
 * type a later agent added, or an agent bug), which the viewer ignores.
 */
export function decode(frame) {
  let msg;
  try {
    msg = JSON.parse(frame);
  } catch {
    return null;
  }
  if (!isObject(msg) || typeof msg.t !== "string" || !Object.hasOwn(RECEIVED, msg.t)) return null;
  const out = RECEIVED[msg.t](msg);
  return out === INVALID ? null : out;
}

/**
 * An image's or a repeat's header (§4.1), with `first` (flag bit 0) and, for an image, its JPEG bytes. Null for a
 * binary message that is neither (§4.2): the viewer neither draws nor acks it.
 */
export function parseHeader(data) {
  if (!(data instanceof ArrayBuffer) || data.byteLength < HEADER_BYTES) return null;
  const view = new DataView(data);
  const kind = view.getUint8(0);
  const slot = view.getUint8(1);
  if ((kind !== IMAGE && kind !== REPEAT) || slot > 1) return null;
  if (kind === REPEAT ? data.byteLength !== HEADER_BYTES : data.byteLength === HEADER_BYTES) return null;
  const header = {
    kind,
    slot,
    page: view.getUint16(2, true),
    seq: view.getUint32(4, true),
    w: view.getUint16(8, true),
    h: view.getUint16(10, true),
    scale: view.getFloat32(12, true),
    sx: view.getFloat32(16, true),
    sy: view.getFloat32(20, true),
    top: view.getFloat32(24, true),
    dsf: view.getUint8(28),
    flags: view.getUint8(29),
  };
  const drawable = header.w > 0 && header.h > 0 && header.scale > 0 && Number.isFinite(header.scale);
  if (!drawable || !Number.isFinite(header.sx) || !Number.isFinite(header.sy)) return null;
  return {
    ...header,
    first: (header.flags & 1) === 1,
    jpeg: kind === IMAGE ? new Uint8Array(data, HEADER_BYTES) : null,
  };
}

// ---- the keyboard proxy's autocomplete (§5.3.1, §8.5)

const CREDENTIALS = new Set(["username", "current-password", "new-password", "webauthn"]);

/**
 * The proxy's `autocomplete` for a field: its `ac` tokens cleaned again, as the agent cleans them, so that no
 * credential token ever reaches it whatever the agent sent; `off` for a password field or when nothing remains.
 */
export function proxyAutocomplete(ac, type) {
  if (type === "password") return "off";
  const tokens = String(ac)
    .split(/[\t\n\f\r ]+/)
    .filter((token) => /^[A-Za-z0-9-]+$/.test(token))
    .map((token) => token.toLowerCase())
    .filter((token) => !CREDENTIALS.has(token));
  return tokens.length ? tokens.join(" ") : "off";
}
