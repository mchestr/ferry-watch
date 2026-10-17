// The agent's field map, and what the viewer may do with it (PROTOCOL.md §5.3, §8.3, §8.5). DOM-free.
//
// A map is a `fields` message, with the `seq` and `n` of any later `fields_ok` for its revision folded in. It is
// usable for the drawn image only while it describes exactly what is drawn: the same page and visual viewport,
// an image close to the one it was taken at, and taken after the agent had carried out everything the user did.
// Only a usable map's fields can be hit by a tap or get a native picker over them.

import { proxyAutocomplete } from "./protocol.js";

const ALL_CELLS = 511; // every cell of the 3×3 hit mask
const NEAR_SEQ = 4;
const PICKERS = new Set(["select", "date", "time"]);
const TYPED = new Set(["email", "tel", "url"]); // the input types the proxy takes over as they are
const NAMES = /(^|-)name$|^(street-address|address-line[1-3]|address-level[1-4])$/;
const NOT_WORDS = /^(email|url|impp|tel(-.*)?|one-time-code|cc-(?!name$).*)$/;

/** `map` with a later `fields_ok` folded in: the same evaluation found again, at a later image and count. */
export function withOk(map, ok) {
  if (!map || ok.page !== map.page || ok.rev !== map.rev) return map;
  return { ...map, seq: ok.seq, n: ok.n };
}

/**
 * Whether `map` describes the drawn `image` (§8.3): `lastInput` is the number of the viewer's latest message the
 * map must have caught up with, and `shifted` says a prediction shift or a local zoom is drawn.
 */
export function mapUsable(map, image, lastInput, shifted) {
  if (!map || !image || shifted || map.page !== image.page) return false;
  return (
    Math.abs(map.sx - image.sx) <= 0.5 &&
    Math.abs(map.sy - image.sy) <= 0.5 &&
    Math.abs(map.scale - image.scale) <= 0.001 &&
    Math.abs(image.seq - map.seq) <= NEAR_SEQ &&
    map.n >= lastInput
  );
}

/** Whether a frame px point reaches `field`: inside its sampled box, in a cell of the mask that is clear. */
function hits(field, x, y) {
  const [left, top, width, height] = field.c;
  if (!(x >= left && x < left + width && y >= top && y < top + height)) return false;
  const column = Math.min(2, Math.floor((3 * (x - left)) / width));
  const row = Math.min(2, Math.floor((3 * (y - top)) / height));
  return ((field.m >> (row * 3 + column)) & 1) === 1;
}

/**
 * The field a tap at frame px (x, y) of a usable map reaches, or null. A point two fields claim reaches neither:
 * which one the page would give the click to is the agent's `focus` report to settle.
 */
export function fieldAt(map, x, y) {
  const reached = map.f.filter((field) => hits(field, x, y));
  return reached.length === 1 ? reached[0] : null;
}

const sameRect = (a, b) => a.every((v, i) => Math.abs(v - b[i]) <= 1e-6);

/**
 * The native picker the viewer lays over `field` of a usable map ("select", "date" or "time"), or null: only for a
 * field nothing covers, wholly inside the image, that the page lets the user change, and for a select only when
 * the map holds all its options.
 */
export function overlayFor(field, image) {
  if (!PICKERS.has(field.k) || field.m !== ALL_CELLS || field.ro || !sameRect(field.c, field.r)) return null;
  const [x, y, w, h] = field.r;
  if (x < 0 || y < 0 || x + w > image.w || y + h > image.h) return null;
  if (field.k === "select" && (!field.opts || field.trunc)) return null;
  return field.k;
}

/** Whether a field takes typing through the keyboard proxy. */
export const typable = (field) => Boolean(field) && !field.ro && (field.k === "text" || field.k === "area");

/**
 * Whether the keyboard proxy keeps what is typed into a field (§8.5), so that the phone's keyboard sees the word a
 * suggestion, AutoFill or an autocorrection replaces: every field that takes typing but a password, which keeps
 * nothing, so that no password is ever there for the keyboard to show or suggest. A date or a time keeps nothing
 * either: its digits fill segments, not text.
 */
export const keepsText = (field) => typable(field) && field.type !== "password";

/**
 * The keyboard proxy's attributes for a field (§8.5), every one of them, "" meaning absent. Never a password
 * input, and never a credential `autocomplete` token, whatever the agent sent: a password field gets exactly
 * what KasmVNC's own hidden input has.
 */
export function proxyAttributes(field) {
  const base = { autocorrect: "off", spellcheck: "false" };
  if (field.type === "password") {
    return { ...base, type: "text", inputmode: "", autocomplete: "off", enterkeyhint: "", autocapitalize: "off" };
  }
  const autocomplete = proxyAutocomplete(field.ac, field.type);
  let inputmode = field.im;
  if (!inputmode && field.type === "number") inputmode = "decimal";
  if (!inputmode && (field.k === "date" || field.k === "time")) inputmode = "numeric"; // typed as digits
  return {
    ...base,
    type: TYPED.has(field.type) ? field.type : "text",
    inputmode,
    autocomplete,
    enterkeyhint: field.ekh,
    autocapitalize: field.cap || capitalisation(field, inputmode, autocomplete.split(" ")),
  };
}

/**
 * What the phone's keyboard capitalises when the page doesn't say: each word of a name or a street address, nothing
 * in emails, URLs, numbers and card fields, a postal code throughout, and sentences anywhere else, as a text field
 * of its own would.
 */
function capitalisation(field, inputmode, tokens) {
  if (TYPED.has(field.type) || ["numeric", "decimal", "tel", "email", "url"].includes(inputmode)) return "off";
  if (tokens.includes("postal-code")) return "characters";
  if (tokens.some((token) => NOT_WORDS.test(token))) return "off";
  if (tokens.some((token) => NAMES.test(token))) return "words";
  return "sentences";
}

const sameOptions = (a, b) =>
  Boolean(a && b) && a.length === b.length && a.every(([label, off], i) => label === b[i][0] && off === b[i][1]);

/**
 * The select of `map` that a `focus` report's field is, for the option sheet's `choose`: the one at the same place,
 * or else the only one with the same options. Null when the map doesn't have it (yet).
 */
export function findSelect(map, focused) {
  if (!map || !focused || focused.k !== "select") return null;
  const selects = map.f.filter((field) => field.k === "select");
  const placed = selects.find((field) => field.r.every((v, i) => Math.abs(v - focused.r[i]) <= 1));
  if (placed) return placed;
  const alike = selects.filter((field) => field.nopt === focused.nopt && sameOptions(field.opts, focused.opts));
  return alike.length === 1 ? alike[0] : null;
}
