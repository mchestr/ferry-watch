// The viewer's own screens over the checkout (DESIGN.md §7.6). The status screen says what the connection is
// doing when there is no live checkout to show, with the way on from there (PROTOCOL.md §3.6): "Show it here",
// the classic viewer, back to Ferry Watch.

const SCREENS = {
  opening: { title: "Opening your checkout…" },
  reconnecting: { title: "Reconnecting…", text: "The connection to your checkout dropped.", classic: true },
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

export class Status {
  /** `here()` for "Show it here", `classic()` for "Use the classic viewer". */
  constructor(document, { here, classic }) {
    const $ = (id) => document.getElementById(id);
    this.root = $("status");
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
