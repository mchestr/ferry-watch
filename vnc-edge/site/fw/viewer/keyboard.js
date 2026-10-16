// The keyboard (PROTOCOL.md §8.4, §8.5, §8.7; DESIGN.md §4.4, §7.5).
//
// A phone opens its keyboard only for a field of the viewer's own page focused inside a tap, so the viewer has a
// hidden input, the proxy, which it focuses in the `touchend` of a tap on a field of a usable map (or of the "Tap
// to type" chip when the map couldn't say), with the attributes that give the right keyboard. The proxy takes no
// touches (`pointer-events: none`): the gesture layer above it does, so taps in the field move the pod's caret and
// drags scroll. What is typed into it goes to the pod as `key`, `text` and `compose`, and never stays in it, with
// one exception: in a name, an address or a sentence, the phone's keyboard decides capitals from the text before
// the caret, so the proxy keeps the last 64 code points typed since the field was tapped, and lets the browser
// make replacements to that text itself, sending the difference. Nothing typed is ever stored or logged (§8.9).
//
// A computer's keyboard is sent as it is, key down and key up, whether or not the proxy has focus; the proxy then
// only serves compositions (dead keys, IME) and paste.

import { proxyAttributes, typable } from "./core/fields.js";
import { toView } from "./core/layout.js";
import { NAMED_KEYS } from "./core/protocol.js";
import { codePoints, composeMessages, edit, isClean, normalise, tail, textMessages } from "./core/text.js";

const KEPT = 64; // code points of context kept for the phone's keyboard
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
const LINE_BREAKS = new Set(["insertLineBreak", "insertParagraph"]);
const EDITABLE = new Set(["text", "area", "date", "time"]);

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
    this.attributes = "";
    this.keeps = false; // whether the proxy keeps what was typed (see above)
    this.context = "";
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
    const signature = JSON.stringify(attributes);
    this.field = field;
    if (moved && signature === this.attributes) return;
    this.attributes = signature;
    for (const [name, value] of Object.entries(attributes)) {
      if (value === "") this.proxy.removeAttribute(name);
      else if (this.proxy.getAttribute(name) !== value) this.proxy.setAttribute(name, value);
    }
    this.keeps = attributes.type === "text" && ["words", "sentences"].includes(attributes.autocapitalize);
    this.forget();
  }

  /** The pod's caret may have moved: what was typed before is no longer before it. */
  forget() {
    this.context = "";
    this.proxy.value = "";
  }

  /** Keeps `s` as typed before the caret, where the phone's keyboard needs it. */
  remember(s) {
    if (!this.keeps) return this.forget();
    this.context = tail(this.context + s, KEPT);
    this.proxy.value = this.context;
    const end = this.context.length;
    this.proxy.setSelectionRange(end, end);
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
    if (type === "insertReplacementText" && this.context) return; // input() sends what the browser changed
    event.preventDefault();
    if (type === "insertText") {
      this.typed(event.data ?? "", "insertText");
    } else if (PASTED.has(type)) {
      this.typed(event.dataTransfer?.getData("text/plain") ?? event.data ?? "", "paste");
    } else if (Object.hasOwn(DELETES, type)) {
      this.press(DELETES[type]);
    } else if (LINE_BREAKS.has(type)) {
      this.press({ k: "Enter" });
    }
  }

  /** A change no `beforeinput` was cancelled for: a replacement, or some autofill. Sends the difference. */
  input(event) {
    if (event.isComposing) return;
    const value = this.proxy.value;
    if (value === this.context) return;
    const { removed, added } = edit(this.context, value);
    for (let i = 0; i < Math.min(removed, KEPT); i++) this.sendAll([{ t: "key", k: "Backspace" }]);
    this.sendAll(textMessages(added, codePoints(added) === 1 ? "insertText" : "paste"));
    this.context = "";
    this.remember(normalise(value));
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
    this.proxy.value = this.context;
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

/** What a `key` message says of a physical key, or null for one that isn't sent (Ctrl, F-keys, dead keys, IME). */
function physical(key) {
  if (key === "Shift" || NAMED_KEYS.includes(key)) return { k: key };
  if (codePoints(key) === 1 && isClean(key)) return { c: key };
  return null;
}
