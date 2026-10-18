// What the user types or pastes, made into wl.cast1 messages (PROTOCOL.md §1, §6.10, §8.7). DOM-free.
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
 * How `after` differs from `before`, as typing at the end of `before` in the pod's field would make it: how many
 * Backspaces, then what to type. For an edit the browser made itself to text the viewer kept in the proxy. A
 * Backspace deletes what the pod's Chromium deletes (backspaced()), so an edit that ends inside an emoji types the
 * rest of it again.
 */
export function edit(before, after) {
  const was = [...before];
  const now = [...after];
  let same = 0;
  while (same < was.length && same < now.length && was[same] === now[same]) same += 1;
  let kept = was.length;
  let removed = 0;
  for (; kept > same; removed += 1) kept -= deleted(was, kept);
  return { removed, added: now.slice(kept).join("") };
}

/** How many code points at the end of `s` one Backspace deletes in the pod's field (see deleted()). */
export function backspaced(s) {
  const points = [...s];
  return deleted(points, points.length);
}

const LINE_FEED = 0x0a;
const CARRIAGE_RETURN = 0x0d;
const ZERO_WIDTH_JOINER = 0x200d;
const ENCLOSING_KEYCAP = 0x20e3;
const has = (property) => (cp) => property.test(String.fromCodePoint(cp));
const isEmoji = has(/\p{Emoji}/u);
const isModifierBase = has(/\p{Emoji_Modifier_Base}/u);
const isSelector = has(/\p{Variation_Selector}/u);
const isCombining = has(/\p{Mn}/u); // for Chromium's "combining class other than 0", which JavaScript can't ask for
const isModifier = (cp) => cp >= 0x1f3fb && cp <= 0x1f3ff;
const isRegional = (cp) => cp >= 0x1f1e6 && cp <= 0x1f1ff;
const isKeycapBase = (cp) => (cp >= 0x30 && cp <= 0x39) || cp === 0x23 || cp === 0x2a;

/**
 * How many of the code points `points[0..end)` ends with one Backspace deletes in the pod's field: Chromium's
 * backspace state machine (core/editing/state_machines, as the pod's Chromium 141 has it). An emoji goes whole with
 * its skin tone, variation selector, keycap or ZWJ sequence, a flag's two regional indicators and CR LF together;
 * anything else one code point at a time, a letter's combining marks among them.
 */
function deleted(points, end) {
  if (end <= 0) return 0;
  const at = (i) => points[i].codePointAt(0);
  const last = at(end - 1);
  let state;
  if (last === LINE_FEED) state = "lf";
  else if (isSelector(last)) state = "vs";
  else if (isRegional(last)) state = "oddRegional";
  else if (isModifier(last)) state = "modifier";
  else if (isEmoji(last)) state = "zwjEmoji";
  else if (last === ENCLOSING_KEYCAP) state = "keycap";
  else return 1;
  let n = 1; // code points deleted (a variation selector met in a "vs…" state counts with what it follows)
  for (let i = end - 2; i >= 0; i--) {
    const cp = at(i);
    if (state === "lf") return n + (cp === CARRIAGE_RETURN ? 1 : 0);
    if (state === "keycap" || state === "modifier") {
      if (isSelector(cp)) {
        state = state === "keycap" ? "vsKeycap" : "vsModifier";
        continue;
      }
      if (state === "keycap") return n + (isKeycapBase(cp) ? 1 : 0);
      if (!isModifierBase(cp)) return n;
      n += 1;
      state = "zwjEmoji";
    } else if (state === "vsKeycap") {
      return n + (isKeycapBase(cp) ? 2 : 0);
    } else if (state === "vsModifier") {
      return n + (isModifierBase(cp) ? 2 : 0);
    } else if (state === "vs") {
      if (!isEmoji(cp)) return n + (!isSelector(cp) && !isCombining(cp) ? 1 : 0);
      n += 1;
      state = "zwjEmoji";
    } else if (state === "zwjEmoji") {
      if (cp !== ZERO_WIDTH_JOINER) return n;
      state = "zwj";
    } else if (state === "zwj") {
      if (isEmoji(cp)) {
        n += 2; // with the joiner
        state = isModifier(cp) ? "modifier" : "zwjEmoji";
      } else if (isSelector(cp)) {
        state = "vsZwj";
      } else {
        return n;
      }
    } else if (state === "vsZwj") {
      if (!isEmoji(cp)) return n;
      n += 3; // with the joiner and the selector
      state = "zwjEmoji";
    } else {
      if (!isRegional(cp)) return n;
      n += state === "oddRegional" ? 1 : -1;
      state = state === "oddRegional" ? "evenRegional" : "oddRegional";
    }
  }
  return n;
}

/** The last `max` code points of `s`. */
export function tail(s, max) {
  const points = [...s];
  return points.length > max ? points.slice(-max).join("") : s;
}
