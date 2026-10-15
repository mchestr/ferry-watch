// The viewer's own screens over the checkout (PROTOCOL.md §8.6, §8.8, §8.10; DESIGN.md §7.6).
//
// - The status screen says what the connection is doing when there is no live checkout to show, with the way on
//   from there (§3.6): "Show it here", the classic viewer, back to Ferry Watch.
// - The menu, from a handle on the right edge: the time left, the checkout's open pages, Paste, the classic viewer
//   and the way back to Ferry Watch.
// - The option sheet, for a select the pod opened its own list for.
// - The dialog sheet, for the checkout's JavaScript dialogs, headed with the origin of the frame that opened it and
//   drawn as the browser draws its own, never as Ferry Watch's, so that no page can pass its prompt off as ours.
// Everything here that comes from the checkout's pages (titles, origins, option labels, dialog text) is set as
// text, never as markup.

import { dialogAnswer } from "./core/text.js";

const PACIFIC = "America/Vancouver";
const CLOSING_MS = 300; // a sheet's closing transition

const SCREENS = {
  opening: { title: "Opening your checkout…", busy: true },
  reconnecting: {
    title: "Reconnecting…",
    text: "The connection to your checkout dropped.",
    busy: true,
    classic: true,
  },
  ended: { title: "This checkout has ended", text: "Its hold is over.", back: true },
  elsewhere: {
    title: "Opened elsewhere on this device",
    text: "The checkout is open in another tab or window.",
    here: true,
    back: true,
  },
  revoked: { title: "Checkout access was withdrawn", text: "Ferry Watch closed this checkout.", back: true },
  stopped: {
    title: "The checkout viewer stopped",
    text: "The classic viewer can show this checkout instead.",
    classic: true,
    back: true,
  },
  nothing: { title: "No checkout here", text: "Open the checkout from Ferry Watch.", back: true },
};

const byId = (document) => (id) => document.getElementById(id);

export class Status {
  /** `here()` for "Show it here", `classic()` for "Use the classic viewer". */
  constructor(document, { here, classic }) {
    const $ = byId(document);
    this.root = $("status");
    this.spinner = $("status-spinner");
    this.title = $("status-title");
    this.text = $("status-text");
    this.here = $("status-here");
    this.classic = $("status-classic");
    this.back = $("status-back");
    this.here.addEventListener("click", here);
    this.classic.addEventListener("click", classic);
    this.showing = "opening";
  }

  /** Shows a screen; `prominent` makes the classic viewer the screen's main way on. */
  show(name, { prominent = false } = {}) {
    const screen = SCREENS[name];
    this.showing = name;
    this.title.textContent = screen.title;
    this.text.textContent = screen.text ?? "";
    this.text.hidden = !screen.text;
    this.spinner.hidden = !screen.busy;
    this.here.hidden = !screen.here;
    this.classic.hidden = !(screen.classic || prominent);
    this.classic.classList.toggle("primary", prominent);
    this.back.hidden = !screen.back;
    this.root.hidden = false;
  }

  hide() {
    this.showing = null;
    this.root.hidden = true;
  }
}

/** A sheet that slides up over the checkout; anything marked `data-close` in it, or Escape, closes it. */
class Sheet {
  constructor(root, onClose = () => {}) {
    this.root = root;
    this.onClose = onClose;
    this.timer = 0;
    for (const closer of root.querySelectorAll("[data-close]")) closer.addEventListener("click", () => this.close());
    root.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      this.close();
    });
  }

  get isOpen() {
    return !this.root.hidden && this.root.classList.contains("open");
  }

  open() {
    clearTimeout(this.timer);
    this.root.hidden = false;
    this.root.getBoundingClientRect(); // lay it out closed, so that opening is a transition
    this.root.classList.add("open");
  }

  /** Closes it; `quietly` without telling whoever opened it (they asked for it). */
  close(quietly = false) {
    if (!this.isOpen) return;
    this.root.classList.remove("open");
    this.timer = setTimeout(() => (this.root.hidden = true), CLOSING_MS);
    if (!quietly) this.onClose();
  }
}

export class Menu {
  /**
   * - `show(page)`, `close(page)`: bring a page of the checkout to the front, or close it;
   * - `paste(text)`: what the clipboard holds, to type into the checkout;
   * - `classic()`: "Use the classic viewer".
   */
  constructor(document, { show, close, paste, classic }) {
    const $ = byId(document);
    this.document = document;
    this.sheet = new Sheet($("menu"), () => this.stopClock());
    this.handle = $("menu-handle");
    this.badge = $("menu-badge");
    this.clockLine = $("menu-clock");
    this.deadline = $("menu-deadline");
    this.pagesSection = $("menu-pages-section");
    this.list = $("menu-pages");
    this.row = $("page-row");
    this.pasteButton = $("menu-paste");
    this.pasteNote = $("menu-paste-note");
    this.actions = { show, close, paste, classic };
    this.pages = null;
    this.clock = null;
    this.ticking = 0;

    this.handle.addEventListener("click", () => this.open());
    $("menu-classic").addEventListener("click", () => classic());
    this.pasteButton.hidden = !navigator.clipboard?.readText;
    this.pasteButton.addEventListener("click", () => this.paste());
  }

  get isOpen() {
    return this.sheet.isOpen;
  }

  open() {
    this.pasteNote.hidden = true;
    this.render();
    this.sheet.open();
    this.tick();
    this.ticking = setInterval(() => this.tick(), 1000);
    this.sheet.root.querySelector(".close").focus({ preventScroll: true });
  }

  close() {
    this.sheet.close();
  }

  stopClock() {
    clearInterval(this.ticking);
  }

  /** The agent's latest `pages` and `clock` (§5.2, §5.8); either may be null before they come. */
  update({ pages, clock }) {
    this.pages = pages;
    this.clock = clock;
    const count = pages?.pages.length ?? 0;
    this.badge.hidden = count < 2;
    this.badge.textContent = String(count);
    this.handle.setAttribute("aria-label", count < 2 ? "Checkout menu" : `Checkout menu, ${count} pages open`);
    if (this.isOpen) this.render();
  }

  render() {
    const pages = this.pages?.pages ?? [];
    this.pagesSection.hidden = pages.length < 2;
    const rows = pages.map((page) => {
      const row = this.row.content.firstElementChild.cloneNode(true);
      const front = page.id === this.pages.front;
      row.querySelector(".page-title").textContent = page.title || page.origin || "Untitled page";
      row.querySelector(".page-origin").textContent = page.title ? page.origin : "";
      if (front) row.setAttribute("aria-current", "page");
      const show = row.querySelector(".page-show");
      show.addEventListener("click", () => {
        if (!front) this.actions.show(page.id);
        this.close();
      });
      const close = row.querySelector(".page-close");
      if (page.main) close.remove();
      else close.addEventListener("click", () => this.actions.close(page.id));
      return row;
    });
    this.list.replaceChildren(...rows);
    this.tick();
  }

  /** The time left, as the hold's clocks run (§5.8). */
  tick() {
    const clock = this.clock;
    this.clockLine.classList.toggle("paused", Boolean(clock?.paused));
    if (!clock) {
      this.clockLine.textContent = "";
      this.deadline.textContent = "";
      return;
    }
    const latest = `At the latest ${timeOfDay(clock.hard * 1000)}.`;
    if (clock.paused || clock.ends === null) {
      this.clockLine.textContent = "Clock paused";
      this.deadline.textContent = `Paused while you're on the payment page. ${latest}`;
      return;
    }
    const left = Math.max(0, Math.round(clock.ends - Date.now() / 1000));
    const seconds = String(left % 60).padStart(2, "0");
    this.clockLine.textContent = `${Math.floor(left / 60)}:${seconds} left`;
    this.deadline.textContent = `Checkout closes at ${timeOfDay(clock.ends * 1000)}. ${latest}`;
  }

  /** Paste, read inside its own tap: the phone asks the user first. */
  async paste() {
    let text;
    try {
      text = await navigator.clipboard.readText();
    } catch {
      this.pasteNote.hidden = false;
      return;
    }
    this.actions.paste(text);
    this.close();
  }
}

export class OptionSheet {
  /** `pick(index)` for an option chosen, `cancel()` when the sheet is closed without one, `classic()`. */
  constructor(document, { pick, cancel, classic }) {
    const $ = byId(document);
    this.sheet = new Sheet($("options"), () => cancel());
    this.list = $("options-list");
    this.more = $("options-more");
    this.row = $("option-row");
    this.pick = pick;
    $("options-classic").addEventListener("click", () => classic());
  }

  get isOpen() {
    return this.sheet.isOpen;
  }

  /** The options of a select the pod opened (a focus report's field, §8.6), labels as text. */
  show(field) {
    const options = field.opts ?? [];
    const rows = options.map(([label, disabled], index) => {
      const row = this.row.content.firstElementChild.cloneNode(true);
      const button = row.querySelector(".option");
      row.querySelector(".option-label").textContent = label;
      button.disabled = disabled;
      button.setAttribute("aria-selected", String(index === field.sel));
      button.addEventListener("click", () => {
        this.sheet.close(true);
        this.pick(index);
      });
      return row;
    });
    this.list.replaceChildren(...rows);
    this.more.hidden = !field.trunc;
    this.sheet.open();
    const selected = this.list.querySelector('[aria-selected="true"]') ?? this.list.querySelector(".option");
    selected?.scrollIntoView({ block: "center" });
  }

  hide() {
    this.sheet.close(true);
  }
}

export class DialogSheet {
  /** `answer(id, ok, s)` sends the user's answer to a dialog; `s` only for a prompt's OK. */
  constructor(document, { answer }) {
    const $ = byId(document);
    this.root = $("dialog");
    this.form = $("dialog-box");
    this.origin = $("dialog-origin");
    this.message = $("dialog-message");
    this.input = $("dialog-input");
    this.cancel = $("dialog-cancel");
    this.ok = $("dialog-ok");
    this.answer = answer;
    this.open = new Map(); // id → the agent's `dialog`, oldest first
    this.pages = null;
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.answered(true);
    });
    this.cancel.addEventListener("click", () => this.answered(false));
    this.root.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      this.answered(this.showing?.kind === "alert");
    });
  }

  /** The checkout's pages, for the heading: a frame of the page's own origin, or an embedded one. */
  setPages(pages) {
    this.pages = pages;
    if (this.showing) this.render(this.showing);
  }

  get showing() {
    return this.open.values().next().value ?? null;
  }

  /** The agent's `dialog` (§5.5): shown now, or after the ones before it. */
  add(dialog) {
    this.open.set(dialog.id, dialog);
    if (this.showing === dialog) this.render(dialog);
  }

  /** The agent's `dialog_closed`, or an answer sent: it goes, and the next shows. */
  remove(id) {
    const was = this.showing;
    this.open.delete(id);
    const next = this.showing;
    if (next && next !== was) this.render(next);
    else if (!next) this.root.hidden = true;
  }

  /** A new connection: the agent announces again every dialog still open, with new ids. */
  reset() {
    this.open.clear();
    this.root.hidden = true;
  }

  render(dialog) {
    const prompt = dialog.kind === "prompt";
    const leaving = dialog.kind === "beforeunload";
    this.origin.textContent = heading(dialog, this.pages);
    this.message.textContent = dialog.msg || (leaving ? "Changes you made may not be saved." : "");
    this.message.hidden = !this.message.textContent;
    this.input.hidden = !prompt;
    this.input.value = prompt ? dialog.def : "";
    this.cancel.hidden = dialog.kind === "alert";
    this.cancel.textContent = leaving ? "Stay" : "Cancel";
    this.ok.textContent = leaving ? "Leave" : "OK";
    this.root.hidden = false;
    (prompt ? this.input : this.ok).focus({ preventScroll: true });
  }

  answered(ok) {
    const dialog = this.showing;
    if (!dialog) return;
    const s = dialog.kind === "prompt" && ok ? dialogAnswer(this.input.value) : undefined;
    this.input.value = "";
    this.answer(dialog.id, ok, s);
    this.remove(dialog.id);
  }
}

/** The dialog's heading, as the browser words it (§8.8). */
export function heading(dialog, pages) {
  if (!dialog.origin) return "This page says:";
  const page = pages?.pages.find((each) => each.id === dialog.page);
  return page && page.origin === dialog.origin
    ? `${dialog.origin} says:`
    : `An embedded page at ${dialog.origin} says:`;
}

function timeOfDay(epochMs) {
  return new Intl.DateTimeFormat("en-CA", { hour: "numeric", minute: "2-digit", timeZone: PACIFIC })
    .format(epochMs)
    .replace(/\./g, "");
}
