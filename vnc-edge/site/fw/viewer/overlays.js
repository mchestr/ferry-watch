// Native pickers over the checkout's selects and date and time fields (PROTOCOL.md §8.3, §8.6; DESIGN.md §7.6).
//
// While the field map is usable, each select, date or time field that nothing covers, wholly inside the image, gets
// a transparent native <select> or <input type=date|time> laid over it. The user's own tap opens the phone's wheel
// or calendar; the choice goes to the agent as `choose` or `date`, which sets the field with trusted input after
// checking what it is about to click. Option labels come from the page and are set as text only. The pickers sit
// above the gesture layer and below the viewer's own controls, and are hidden while the picture is moving.

import { overlayFor } from "./core/fields.js";
import { toView } from "./core/layout.js";

const FORMS = { date: /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/, time: /^[0-9]{2}:[0-9]{2}$/ };

export class Overlays {
  /** `send(msg)` sends a `choose` or a `date`. */
  constructor(document, { send }) {
    this.document = document;
    this.root = document.getElementById("overlays");
    this.send = send;
    this.shown = new Map(); // `${page}:${rev}:${id}` → the element over that field
  }

  /** Lays pickers over the fields of `map`, a usable map for `image` drawn in `box`, or none (null). */
  update(map, image, box) {
    const wanted = new Map();
    for (const field of map?.f ?? []) {
      const kind = overlayFor(field, image);
      if (kind) wanted.set(`${map.page}:${map.rev}:${field.id}`, { field, kind });
    }
    for (const [key, element] of this.shown) {
      if (!wanted.has(key) && this.document.activeElement !== element) {
        element.remove();
        this.shown.delete(key);
      }
    }
    for (const [key, { field, kind }] of wanted) {
      let element = this.shown.get(key);
      if (!element) {
        element = kind === "select" ? this.select(map, field) : this.input(map, field, kind);
        this.shown.set(key, element);
        this.root.append(element);
      }
      place(element, box, field.r);
    }
  }

  /** Hides them all; one open on the screen stays until it closes. */
  clear() {
    this.update(null, null, null);
  }

  /** The user tapped the checkout elsewhere: a picker still open closes. */
  release() {
    const active = this.document.activeElement;
    if (active && this.root.contains(active)) active.blur();
  }

  select(map, field) {
    const select = this.document.createElement("select");
    select.setAttribute("aria-label", "Choose an option");
    for (const [index, [label, disabled]] of field.opts.entries()) {
      const option = new Option(label, String(index)); // text, never markup
      option.disabled = disabled;
      select.append(option);
    }
    select.selectedIndex = field.sel;
    select.addEventListener("change", () => {
      if (select.selectedIndex < 0) return;
      this.send({ t: "choose", page: map.page, rev: map.rev, f: field.id, i: select.selectedIndex });
      select.blur();
    });
    return select;
  }

  input(map, field, kind) {
    const input = this.document.createElement("input");
    input.type = kind;
    input.setAttribute("aria-label", kind === "date" ? "Choose a date" : "Choose a time");
    input.value = field.v ?? "";
    // A computer's browser opens the picker only from its own button, which isn't where the user clicks.
    input.addEventListener("click", () => {
      try {
        input.showPicker();
      } catch {
        // already open, or not in this browser: the field takes typing instead
      }
    });
    input.addEventListener("change", () => {
      if (!FORMS[kind].test(input.value)) return; // cleared, or still incomplete
      this.send({ t: "date", page: map.page, rev: map.rev, f: field.id, v: input.value });
    });
    return input;
  }
}

/** Puts a picker over frame px rectangle `rect` of the image in `box`. */
function place(element, box, [x, y, w, h]) {
  const [left, top] = toView(box, x, y);
  Object.assign(element.style, {
    left: `${left}px`,
    top: `${top}px`,
    width: `${w * box.k}px`,
    height: `${h * box.k}px`,
  });
}
