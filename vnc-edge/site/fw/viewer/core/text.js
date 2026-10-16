// What the user types or pastes, made into fw.cast1 messages (PROTOCOL.md §1, §6.10, §8.7). DOM-free.
//
// Clean text has no C0 controls, DEL, C1 controls or lone surrogates. Typed text (a `text` message) is clean text
// that may also hold TAB and LF. Lengths count code points, never UTF-16 units: an emoji is one.

export const MAX_TEXT = 256;
export const MAX_TEXT_BYTES = 1024;
export const MAX_COMPOSE = 64;
export const MAX_DIALOG = 256;

const TAB = 0x09;
const LF = 0x0a;

/** Whether a code point may stand in clean text (`lines`: TAB and LF too, as in typed text). */
function allowed(cp, lines) {
  if (cp < 0x20) return lines && (cp === TAB || cp === LF);
  if (cp >= 0x7f && cp <= 0x9f) return false; // DEL and C1
  return cp < 0xd800 || cp > 0xdfff; // a surrogate here is a lone one: a pair iterates as one code point
}

function only(s, lines) {
  for (const ch of s) if (!allowed(ch.codePointAt(0), lines)) return false;
  return true;
}

export const isClean = (s) => only(s, false);
export const isTyped = (s) => only(s, true);
export const codePoints = (s) => [...s].length;

/**
 * §8.7 steps 1-2: CR LF and lone CR become LF; lone surrogates and control characters go, except TAB and LF
 * when `lines` (typed text; a composition or a prompt answer has neither).
 */
export function normalise(s, { lines = true } = {}) {
  let out = "";
  for (const ch of s.replace(/\r\n?/g, "\n")) if (allowed(ch.codePointAt(0), lines)) out += ch;
  return out;
}

/**
 * §8.7 step 4: consecutive chunks of at most `max` code points. A chunk that would end with a LF while more text
 * follows ends just before it, so a line break never arrives last in one `text` and is never split from what
 * follows it.
 */
export function split(s, max = MAX_TEXT) {
  const points = [...s];
  const chunks = [];
  for (let start = 0; start < points.length; ) {
    let end = Math.min(start + max, points.length);
    if (end < points.length && points[end - 1] === "\n" && end - 1 > start) end -= 1;
    chunks.push(points.slice(start, end).join(""));
    start = end;
  }
  return chunks;
}

/**
 * The messages for text the user typed or pasted. One character typed on its own (`via` "insertText") is a key
 * press, as on a keyboard; a paste is always `text`, however short.
 */
export function textMessages(input, via) {
  const s = normalise(input);
  if (!s) return [];
  if (via === "insertText" && codePoints(s) === 1 && isClean(s)) return [{ t: "key", c: s }];
  return split(s).map((chunk) => ({ t: "text", s: chunk }));
}

/** The message for an IME composition: none while it is too long to send, until `text` commits it. */
export function composeMessages(input) {
  const s = normalise(input, { lines: false });
  return codePoints(s) > MAX_COMPOSE ? [] : [{ t: "compose", s }];
}

/** A prompt's answer, as `dialog.s`. */
export function dialogAnswer(input) {
  return [...normalise(input, { lines: false })].slice(0, MAX_DIALOG).join("");
}

/**
 * How `after` differs from `before`, as typing at the end of `before` would make it: how many code points to
 * delete, then what to insert. For an edit the browser made itself to text the viewer kept in the proxy.
 */
export function edit(before, after) {
  const was = [...before];
  const now = [...after];
  let same = 0;
  while (same < was.length && same < now.length && was[same] === now[same]) same += 1;
  return { removed: was.length - same, added: now.slice(same).join("") };
}

/** The last `max` code points of `s`. */
export function tail(s, max) {
  const points = [...s];
  return points.length > max ? points.slice(-max).join("") : s;
}
