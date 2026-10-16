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
// The ticket's signature isn't checked here (only the pod's gate has its keys), so anyone can make a link that
// reaches this page with claims of their own. So nothing the ticket says is shown: every word on the page is the
// page's own. Its trip and expiry decide only where this tab goes, and its hold's hard deadline is kept only as far
// as any real hold could have it (after now, at most MAX_HOLD_S off) and never brings forward a deadline the device
// already keeps: a made-up link can't put its words on the checkout host, nor end a checkout on this device early,
// nor leave a record that never goes (found in review; the same was true of 0.6.4's page).
(() => {
  "use strict";
  const session = window.FwSession; // fw/session.js, served in front of this file (build.py)
  const app = document.querySelector('meta[name="fw-app"]').content;
  const at = session.where(location.pathname);
  if (!at) return;
  const { trip, gen } = at;
  const key = session.key(trip, gen);
  const now = Date.now();
  const nowS = Math.floor(now / 1000);
  const TICKET = /^fw1\.[a-z0-9]{1,16}\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+$/;
  const MAX_TICKET_LENGTH = 1024;
  const OPEN_AFTER_MS = 2000;
  const MAX_HOLD_S = 90 * 60; // protocol.MAX_HARD_S: no hold ends further off than this
  const SKEW_S = 10 * 60; // this device's clock may be behind Ferry Watch's

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

  /** A hold's hard deadline (epoch s) that a real hold could have now: after now, and no further off than any. */
  function plausible(deadline) {
    return Number.isFinite(deadline) && deadline > nowS && deadline <= nowS + MAX_HOLD_S + SKEW_S;
  }

  /** The ticket in the fragment and the viewer it opens in. */
  function fromFragment(fragment) {
    if (fragment.startsWith("fw1.")) return { ticket: fragment, viewer: "classic" };
    const params = new URLSearchParams(fragment);
    return { ticket: params.get("t") || "", viewer: params.get("v") === "s1" ? "stream" : "classic" };
  }

  // Forget checkouts whose holds are over (and anything unreadable, or with a deadline no hold could have).
  try {
    for (const name of Object.keys(localStorage)) {
      if (!name.startsWith("fw:")) continue;
      const old = session.read(localStorage, name);
      if (!old || !plausible(old.hard_deadline)) localStorage.removeItem(name);
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
    const claimed = claims.disp && typeof claims.disp === "object" ? claims.disp.hard_deadline : undefined;
    const kept = record && plausible(record.hard_deadline) ? record.hard_deadline : 0;
    record = {
      n: record && typeof record.n === "string" ? record.n : session.nonce(),
      opened,
      // The later of the deadline kept and the ticket's, if the ticket's is one a hold could have: a hold that ends
      // later in the same pod moves it on, and no link brings it forward.
      hard_deadline: Math.max(kept, plausible(claimed) ? claimed : 0) || nowS + MAX_HOLD_S,
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
  document.getElementById("details").hidden = false;
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
})();
