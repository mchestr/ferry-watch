// The keyboard (PROTOCOL.md §8.4, §8.5, §8.7; DESIGN.md §4.4, §7.5).
//
// A phone opens its keyboard only for a field of the viewer's own page focused inside a tap, so the viewer has a
// hidden input, the proxy, which it focuses in the `touchend` of a tap on a field of a usable map (or of the "Tap
// to type" chip when the map couldn't say), with the attributes that give the right keyboard. The proxy takes no
// touches (`pointer-events: none`): the gesture layer above it does, so taps in the field move the pod's caret and
// drags scroll. What is typed into it goes to the pod as `key`, `text` and `compose`.
//
// The phone's keyboard works from the text before the caret: it capitalises from it, and a suggestion, AutoFill, an
// autocorrection or a swiped word replaces the word that text ends with. So the proxy keeps the end of what was
// typed since the field was tapped, as the pod's field has it before its caret (at most 64 code points, after the
// last line break), lets the browser make those edits to it itself, and sends the pod the difference: a suggestion
// replaces exactly what it replaced, rather than adding to it. Whenever it can't be sure that is what the pod has
// (a tap, a focus change, a navigation key), it starts again from nothing. A password field's proxy keeps nothing,
// so the phone's keyboard never has a password to show or suggest. Nothing typed is ever stored or logged (§8.9).
//
// A computer's keyboard is sent as it is, key down and key up, whether or not the proxy has focus; the proxy then
// only serves compositions (dead keys, IME) and paste.

import { keepsText, proxyAttributes, typable } from "./core/fields.js";
import { toView } from "./core/layout.js";
import { NAMED_KEYS } from "./core/protocol.js";
import { codePoints, composeMessages, edit, isClean, normalise, tail, textMessages } from "./core/text.js";

const KEPT = 64; // code points of what was typed that the proxy keeps for the phone's keyboard
const REPEAT_MS = 25; // a held key's repeats are sent at most this often (PROTOCOL.md §8.4)
const REVEAL_MARGIN = 16; // CSS px between a field and the phone's keyboard
const MAX_DELTA = 4000;
const DELETES = {
  deleteContentBackward: { k: "Backspace" },
  deleteContentForward: { k: "Delete" },
  deleteWordBackward: { k: "Backspace", word: true },
  deleteWordForward: { k: "Delete", word: true },
};
const PASTED = new Set(["insertFromPaste", "insertFromPasteAsQuotation", "insertFromDrop", "insertReplacementText"]);
const APPLIED = new Set(["insertText", "insertReplacementText"]); // edits the browser makes to what the proxy keeps
const LINE_BREAKS = new Set(["insertLineBreak", "insertParagraph"]);
const EDITABLE = new Set(["text", "area", "date", "time"]);
const TRIMMED = new Set(["email", "url"]); // input types whose value leaves out ASCII whitespace at either end
const SPACES_BEFORE = /^[\t\n\f\r ]+/;
const SPACES_AFTER = /[\t\n\f\r ]*$/;

export class Keyboard {
  /**
   * - `send(msg)`: sends a message, giving its number or null;
   * - `screen()`: what is drawn, `{image, box}`, or null;
   * - `computer`: a mouse and a physical keyboard, rather than a touch screen.
   */
  constructor(document, { send, screen, computer }) {
    this.document = document;
    this.proxy = document.getElementById("proxy");
    this.chip = document.getElementById("type-chip");
    this.send = send;
    this.screen = screen;
    this.computer = computer;
    this.field = null; // the field the proxy types into
    this.reported = null; // the field the agent's latest focus report named
    this.setup = ""; // the proxy's attributes for the field, and whether it keeps what is typed into it
    this.keeps = false; // whether the proxy keeps what was typed (see above)
    this.context = ""; // what the proxy keeps: the end of what was typed before the pod's caret
    this.revealed = false;
    this.composition = null; // the composition last sent
    this.held = new Map(); // a physical key's `code` → the key message its `down` sent
    this.repeated = new Map(); // a held key's `code` → when its latest repeat was sent

    const proxy = this.proxy;
    proxy.addEventListener("beforeinput", (event) => this.beforeinput(event));
    proxy.addEventListener("input", (event) => this.input(event));
    proxy.addEventListener("compositionupdate", (event) => this.compose(event.data ?? ""));
    proxy.addEventListener("compositionend", (event) => this.composed(event.data ?? ""));
    proxy.addEventListener("keydown", (event) => this.screenKey(event));
    proxy.addEventListener("focus", () => this.refocused());
    addEventListener("keydown", (event) => this.keydown(event));
    addEventListener("keyup", (event) => this.keyup(event));
    addEventListener("blur", () => this.releaseAll());
    this.chip.addEventListener("click", () => this.reported && this.focusFor(this.reported));
    globalThis.visualViewport?.addEventListener("resize", () => this.reveal());
  }

  /** A new connection: nothing of the last one's fields carries over. */
  reset() {
    this.releaseAll();
    this.reported = null;
    this.blur();
  }

  // ---- focus

  /** In the tap that reached `field` (or the chip's): the proxy takes it, and the phone's keyboard opens. */
  focusFor(field) {
    this.take(field);
    this.revealed = false;
    if (this.document.activeElement !== this.proxy) this.proxy.focus({ preventScroll: true });
    this.chip.hidden = true;
  }

  /**
   * The proxy has the focus: in a tap (focusFor), or back from the menu, which gives back the focus it took. Either
   * way the chip's job is done. Back from the menu, the proxy follows what the agent reported while the menu was
   * open: the field the chip was offering, or no field that takes typing (the keyboard stays away).
   */
  refocused() {
    if (!this.chip.hidden && this.reported) this.take(this.reported, true);
    this.chip.hidden = true;
    if (!this.field) this.proxy.blur();
  }

  /**
   * A tap is about to be sent. On a field that takes typing the proxy takes it now, inside the tap; elsewhere on a
   * usable map (`known`) the keyboard goes; without one, the agent's focus report will say.
   */
  tapped(field, known) {
    this.chip.hidden = true;
    if (typable(field)) this.focusFor(field);
    else if (known) this.blur();
  }

  /** The agent's report of the field with focus in the pod (§5.4), or null. */
  report(field) {
    this.reported = field;
    if (!field || field.ro || !EDITABLE.has(field.k)) {
      this.blur();
      return;
    }
    if (this.document.activeElement === this.proxy) {
      this.take(field, true); // the page moved the focus itself: a card number on to its expiry
    } else if (this.computer) {
      this.focusFor(field);
    } else {
      this.chip.hidden = false; // the phone's keyboard opens only inside a tap
    }
  }

  blur() {
    this.field = null;
    this.chip.hidden = true;
    this.forget();
    if (this.document.activeElement === this.proxy) this.proxy.blur();
  }

  /** Sets the proxy up for `field`. `moved`: only if it is another kind of field; the caret is where it was. */
  take(field, moved = false) {
    const attributes = proxyAttributes(field);
    const keeps = keepsText(field);
    const setup = JSON.stringify([attributes, keeps]); // a password's attributes may be another field's
    this.field = field;
    if (moved && setup === this.setup) return;
    this.setup = setup;
    for (const [name, value] of Object.entries(attributes)) {
      if (value === "") this.proxy.removeAttribute(name);
      else if (this.proxy.getAttribute(name) !== value) this.proxy.setAttribute(name, value);
    }
    this.keeps = keeps;
    this.forget();
  }

  /** The pod's caret may have moved: what was typed before is no longer before it. */
  forget() {
    this.context = "";
    this.proxy.value = "";
  }

  /** Keeps `s` as typed before the caret, where the phone's keyboard needs it. */
  remember(s) {
    this.keep(this.context + s);
  }

  /**
   * Keeps as much of `text`, which ends where the pod's caret is, as the phone's keyboard needs: what follows the last
   * line break, at most KEPT code points. `inProxy`: what the browser has just put in the proxy itself, if it has.
   */
  keep(text, inProxy = null) {
    if (!this.keeps) return this.forget();
    this.context = tail(text.slice(text.lastIndexOf("\n") + 1), KEPT);
    if (this.context !== inProxy) this.show();
  }

  /**
   * Puts what is kept in the proxy, with its caret at the end, as the pod's is. `again`: even if the proxy's value
   * already reads so, as an email input's does with spaces at its end that the user typed but its value leaves out.
   */
  show(again = false) {
    const proxy = this.proxy;
    const trims = TRIMMED.has(proxy.type);
    if (trims) {
      // Its value loses the spaces at either end, so it holds text ending in one only when the user typed them there.
      this.context = this.context.replace(SPACES_BEFORE, "");
      if (SPACES_AFTER.exec(this.context)[0]) this.context = "";
    }
    if (again || proxy.value !== this.context) {
      if (trims) proxy.value = ""; // its text then changes, spaces and all, and its caret goes to the end
      proxy.value = this.context;
    }
    if (proxy.selectionStart === null) return; // an email input has no selection: setSelectionRange throws
    const end = this.context.length;
    if (proxy.selectionStart !== end || proxy.selectionEnd !== end) proxy.setSelectionRange(end, end);
  }

  /** Once the phone's keyboard is up, scrolls the pod's page so the field shows above it, as a browser does. */
  reveal() {
    const viewport = globalThis.visualViewport;
    const drawn = this.screen();
    if (!viewport || !drawn || !this.field || this.revealed || this.document.activeElement !== this.proxy) return;
    if (viewport.height >= innerHeight - 1) return; // no keyboard
    this.revealed = true;
    const [x, y, w, h] = this.field.r;
    const [, bottom] = toView(drawn.box, x, y + h);
    const visible = viewport.offsetTop + viewport.height - REVEAL_MARGIN;
    if (bottom <= visible) return;
    const { image, box } = drawn;
    const at = [Math.min(Math.max(x + w / 2, 0), image.w), Math.min(Math.max(y + h / 2, 0), image.h)];
    const dy = Math.min((bottom - visible) / box.k, MAX_DELTA);
    this.send({ t: "wheel", page: image.page, x: at[0], y: at[1], dx: 0, dy });
  }

  // ---- what the proxy is given

  beforeinput(event) {
    const type = event.inputType;
    if (event.isComposing || type === "insertCompositionText") return; // the composition's events say it
    if (this.keeps && APPLIED.has(type)) return; // the browser makes the edit; input() sends what it changed
    event.preventDefault();
    if (type === "insertText") {
      this.typed(event.data ?? "", "insertText");
    } else if (PASTED.has(type)) {
      this.typed(event.dataTransfer?.getData("text/plain") || event.data || "", "paste");
    } else if (Object.hasOwn(DELETES, type)) {
      this.press(DELETES[type]);
    } else if (LINE_BREAKS.has(type)) {
      this.press({ k: "Enter" });
    }
  }

  /**
   * The browser changed the proxy itself: what was typed, a suggestion, AutoFill or an autocorrection, in a field it
   * keeps text for; some autofill in any field. Sends the difference: Backspaces for what the edit took away from the
   * end of what was kept, then what it put there.
   */
  input(event) {
    if (event.isComposing) return;
    const now = this.applied(event);
    if (now === this.context) return;
    const { removed, added } = edit(this.context, now);
    for (let i = 0; i < Math.min(removed, KEPT); i++) this.send({ t: "key", k: "Backspace" });
    this.sendAll(textMessages(added, codePoints(added) === 1 ? "insertText" : "paste"));
    this.keep(normalise(now), now);
  }

  /**
   * What the proxy holds after the browser's edit: its value, and the spaces typed at its end that an email or URL
   * input's value leaves out (they reach the pod as typed; Backspace deletes them there).
   */
  applied(event) {
    const value = this.proxy.value;
    if (!TRIMMED.has(this.proxy.type)) return value;
    const data = event.data ?? "";
    const spaces = SPACES_AFTER.exec(data)[0];
    if (spaces && spaces === data && value === trimmed(this.context)) return this.context + data; // only spaces
    return value.endsWith(spaces) ? value : value + spaces;
  }

  /** The composition changed. The browser says so again as it commits it: once is enough. */
  compose(data) {
    const messages = composeMessages(data);
    if (!messages.length || messages[0].s === this.composition) return;
    this.composition = messages[0].s;
    this.sendAll(messages);
  }

  /** A composition was committed: its text replaces it in the pod. */
  composed(data) {
    this.composition = null;
    const messages = textMessages(data, "paste");
    this.sendAll(messages.length ? messages : [{ t: "compose", s: "" }]);
    this.remember(normalise(data));
  }

  /** Text typed (one character on its own is a key press) or pasted. */
  typed(s, via) {
    const messages = textMessages(s, via);
    this.sendAll(messages);
    if (messages.length) this.remember(normalise(s));
  }

  /** Paste from the menu, into the field that has focus in the pod. */
  paste(s) {
    this.typed(s, "paste");
  }

  /** A whole key press from the phone's keyboard: `{k, shift?, word?}`. */
  press(msg) {
    this.send({ t: "key", ...msg });
    if (msg.k !== "Backspace" || msg.word || !this.context) return this.forget();
    this.context = [...this.context].slice(0, -1).join("");
    this.show(true);
  }

  sendAll(messages) {
    for (const msg of messages) this.send(msg);
  }

  // ---- keys

  /**
   * A phone keyboard's keys the proxy gets without any `beforeinput` to show for them: Backspace in an empty field,
   * Enter in a one-line field, the arrows of a keyboard attached to a phone.
   */
  screenKey(event) {
    if (this.computer || event.isComposing || event.ctrlKey || event.altKey || event.metaKey) return;
    if (!NAMED_KEYS.includes(event.key)) return;
    event.preventDefault();
    this.press(event.shiftKey ? { k: event.key, shift: true } : { k: event.key });
  }

  /** Whether a key event is the checkout's, not one for the viewer's own controls (a sheet, a button). */
  mine(event) {
    const target = event.target;
    return target === this.proxy || target === this.document.body || target === this.document.documentElement;
  }

  /** A computer's key goes down (§8.4): sent as it is, unless it makes a shortcut. */
  keydown(event) {
    if (!this.computer || !this.mine(event) || event.isComposing) return;
    const msg = physical(event.key);
    const altGraph = event.getModifierState?.("AltGraph") ?? false; // a character, not a shortcut
    if (!msg || ((event.ctrlKey || event.altKey || event.metaKey) && !(altGraph && msg.c))) return;
    event.preventDefault();
    const held = this.held.get(event.code);
    if (held) {
      this.repeat(event.code, held);
      return;
    }
    if (event.repeat) return; // it went down before the viewer had the keyboard
    this.held.set(event.code, msg);
    this.send({ t: "key", ...msg, ph: "down" });
    if (msg.k && msg.k !== "Shift" && msg.k !== "Backspace") this.forget();
  }

  /**
   * The computer's keyboard repeats a key held down (Backspace to clear a field, an arrow to move the caret): the
   * pod's Chromium never repeats one itself, so each repeat goes as another `down` of the same key (§6.9), at most
   * one every REPEAT_MS. Shift doesn't repeat.
   */
  repeat(code, msg) {
    const now = performance.now();
    if (msg.k === "Shift" || now - (this.repeated.get(code) ?? -Infinity) < REPEAT_MS) return;
    this.repeated.set(code, now);
    this.send({ t: "key", ...msg, ph: "down" });
  }

  keyup(event) {
    const msg = this.held.get(event.code);
    if (!msg) return;
    this.held.delete(event.code);
    this.repeated.delete(event.code);
    event.preventDefault();
    this.send({ t: "key", ...msg, ph: "up" });
  }

  /** The viewer's window lost the keyboard: every key it sent down goes up. */
  releaseAll() {
    for (const msg of this.held.values()) this.send({ t: "key", ...msg, ph: "up" });
    this.held.clear();
    this.repeated.clear();
  }
}

/** `s` without ASCII whitespace at either end, as an email or URL input's value has it. */
const trimmed = (s) => s.replace(SPACES_BEFORE, "").replace(SPACES_AFTER, "");

/** What a `key` message says of a physical key, or null for one that isn't sent (Ctrl, F-keys, dead keys, IME). */
function physical(key) {
  if (key === "Shift" || NAMED_KEYS.includes(key)) return { k: key };
  if (codePoints(key) === 1 && isClean(key)) return { c: key };
  return null;
}
