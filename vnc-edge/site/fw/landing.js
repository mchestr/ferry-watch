// The checkout landing page, /t/<trip>/g/<gen>/#<fragment> (docs/multi-tenant/design-reference.md §7.9;
// docs/multi-tenant/streamed-viewer/DESIGN.md §8.4).
//
// It turns the ticket Ferry Watch put in the fragment into a pending ticket for this tab, and gives
// the device a nonce N it keeps in localStorage, keyed by (trip, gen); then it opens the viewer, which
// offers both to the pod's gate. Once a connect succeeds the gate locks the cart to sha256(N),
// so only this device can come back, and it comes back with N alone.
//
// The fragment is `#<ticket>` (0.6.4's form: KasmVNC, the classic viewer) or `#v=s1&t=<ticket>` (the
// streamed viewer). The device remembers which, per checkout, and opens that viewer from then on, unless
// the streamed viewer has given way to the classic one for this checkout on this device.
//
// The ticket's display fields are signed but not checked here: they are shown as text, never markup.
(() => {
  "use strict";
  const session = window.FwSession; // fw/session.js, served in front of this file (build.py)
  const app = document.querySelector('meta[name="fw-app"]').content;
  const at = session.where(location.pathname);
  if (!at) return;
  const { trip, gen } = at;
  const key = session.key(trip, gen);
  const now = Date.now();
  const TICKET = /^fw1\.[a-z0-9]{1,16}\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+$/;
  const MAX_TICKET_LENGTH = 1024;
  const OPEN_AFTER_MS = 2000;
  const MAX_HOLD_MS = 90 * 60 * 1000; // protocol.MAX_HARD_S, for a ticket that doesn't say
  const PACIFIC = "America/Vancouver";

  /** The ticket's claims, if the fragment holds a well-formed ticket for this trip. */
  function claimsOf(ticket) {
    const match = ticket.length <= MAX_TICKET_LENGTH && TICKET.exec(ticket);
    if (!match) return null;
    try {
      const base64 = match[1].replace(/-/g, "+").replace(/_/g, "/");
      const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
      const claims = JSON.parse(new TextDecoder().decode(bytes));
      return claims && claims.trip === trip && typeof claims.exp === "number" ? claims : null;
    } catch {
      return null;
    }
  }

  /** The ticket in the fragment and the viewer it opens in. */
  function fromFragment(fragment) {
    if (fragment.startsWith("fw1.")) return { ticket: fragment, viewer: "classic" };
    const params = new URLSearchParams(fragment);
    return { ticket: params.get("t") || "", viewer: params.get("v") === "s1" ? "stream" : "classic" };
  }

  // Forget checkouts whose holds are over (and anything unreadable).
  try {
    for (const name of Object.keys(localStorage)) {
      if (!name.startsWith("fw:")) continue;
      const old = session.read(localStorage, name);
      if (!old || !(old.hard_deadline * 1000 > now)) localStorage.removeItem(name);
    }
  } catch {
    // storage is blocked; the write below says so
  }

  const { ticket, viewer } = fromFragment(location.hash.slice(1));
  history.replaceState(null, "", location.pathname); // the ticket stays out of history
  const claims = ticket ? claimsOf(ticket) : null;
  let record = session.record(key);
  const opened = Boolean(record && record.opened);

  if (claims && claims.exp * 1000 <= now && !opened) {
    location.replace(`${app}/c/${trip}`); // too slow to arrive: Ferry Watch opens it again
    return;
  }
  if (claims && claims.exp * 1000 > now) {
    const disp = claims.disp && typeof claims.disp === "object" ? claims.disp : {};
    record = {
      n: record && typeof record.n === "string" ? record.n : session.nonce(),
      opened,
      disp,
      hard_deadline: Number(disp.hard_deadline) || Math.floor((now + MAX_HOLD_MS) / 1000),
      session_deadline: Number.isFinite(disp.session_s) ? now + disp.session_s * 1000 : null,
      viewer,
      viewer_fallback: Boolean(record && record.viewer_fallback), // sticky for this checkout on this device
    };
    const stored = session.write(localStorage, key, record);
    if (!stored || !session.write(sessionStorage, `${key}:ticket`, { ticket, exp: claims.exp })) {
      fail(
        "Can't open the checkout in this window",
        "This browser isn't letting the page save the checkout's key. Try a regular (not private) window.",
      );
      return;
    }
  }
  const pending = session.pendingTicket(key);
  if (!opened && !(pending && pending.exp * 1000 > now)) {
    fail(
      "No checkout here",
      "No checkout is open on this device. Open it from Ferry Watch, on the device you want to pay on.",
    );
    return;
  }

  const streamed = Boolean(record && record.viewer === "stream" && !record.viewer_fallback);
  ready(record && record.disp);
  const beta = document.getElementById("beta"); // not on a page from a 0.6.4 replica, mid-rollout
  if (beta) beta.hidden = !streamed;
  const go = () => location.replace(streamed ? session.streamUrl(trip, gen) : session.classicUrl(trip, gen));
  document.getElementById("open").addEventListener("click", go);
  setTimeout(go, OPEN_AFTER_MS);

  function fail(title, text) {
    document.getElementById("title").textContent = title;
    const nothing = document.getElementById("nothing");
    nothing.textContent = text;
    nothing.hidden = false;
  }

  function ready(disp) {
    document.getElementById("details").hidden = false;
    if (!disp) return;
    const sailing = [disp.route, dateLabel(disp.date), timeLabel(disp.depart), disp.fare].filter(Boolean);
    document.getElementById("sailing").textContent = sailing.map(String).join(" · ");
    const closes = record.session_deadline
      ? `Checkout closes at ${clock(record.session_deadline)} (paused while you're on the payment page).`
      : "";
    const latest = record.hard_deadline ? ` At the latest ${clock(record.hard_deadline * 1000)}.` : "";
    document.getElementById("deadline").textContent = closes + latest;
  }

  function clock(epochMs) {
    return new Intl.DateTimeFormat("en-CA", { hour: "numeric", minute: "2-digit", timeZone: PACIFIC })
      .format(epochMs)
      .replace(/\./g, "");
  }

  function timeLabel(hhmm) {
    const match = /^(\d{2}):(\d{2})$/.exec(String(hhmm));
    if (!match) return null;
    const hour = Number(match[1]);
    return `${hour % 12 || 12}:${match[2]} ${hour < 12 ? "am" : "pm"}`;
  }

  function dateLabel(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso));
    if (!match) return null;
    const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
    const format = { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" };
    return new Intl.DateTimeFormat("en-CA", format).format(day);
  }
})();
