// What the viewer does when its connection closes (PROTOCOL.md §3.6, §3.7). DOM-free.
//
// `nextAction(state, close)` takes how a connection went (`opened`: it reached `open`; `ready`: it reached the
// agent's `ready`; `code`: its close code, 1006 for an upgrade that failed; `deadlinePassed`: the hold's hard
// deadline had passed) and gives:
// - `action`: `{do: "reconnect", delay}` (seconds), `{do: "stop", show?}` with the screen to show ("ended",
//   "elsewhere", "revoked"), `{do: "classic"}` (switch to the classic viewer now) or `{do: "gone"}` (/gone.html);
// - `prominent`: whether to offer the classic viewer prominently from now on;
// - `state`: the history the next close is judged against.

export const DELAYS = Object.freeze([0.5, 1, 2, 5, 10]); // seconds; the last repeats

const STOPS = {
  4000: { do: "stop", show: "ended" },
  4001: { do: "stop", show: "elsewhere" },
  4002: { do: "stop", show: "revoked" },
  4003: { do: "stop", show: "ended" },
  4004: { do: "stop" },
};
const FIXED_DELAYS = { 4400: 1, 4429: 2 };

export function initialState() {
  return {
    attempts: 0, // reconnect attempts since the page loaded or the last connection that reached `ready`
    neverOpened: 0, // connections in a row that never reached `open`
    protocolErrors: 0, // 4400s in this page's life
    internalErrors: 0, // 4500s in a row
  };
}

export function nextAction(state, { opened, ready, code, deadlinePassed = false }) {
  const closed = code === 1009 ? 4400 : code; // an over-long message is a protocol violation too
  const next = {
    attempts: ready ? 0 : state.attempts,
    neverOpened: opened ? 0 : state.neverOpened + 1,
    protocolErrors: state.protocolErrors + (closed === 4400 ? 1 : 0),
    internalErrors: closed === 4500 ? state.internalErrors + 1 : 0,
  };
  let action;
  let prominent = next.neverOpened >= 3 || next.internalErrors >= 2;
  if (Object.hasOwn(STOPS, closed)) {
    action = STOPS[closed];
  } else if (closed === 4005) {
    action = { do: "classic" };
  } else if (closed === 4400 && next.protocolErrors >= 2) {
    action = { do: "stop" };
    prominent = true;
  } else if (deadlinePassed) {
    action = { do: "gone" };
  } else {
    next.attempts += 1;
    const delay = FIXED_DELAYS[closed] ?? DELAYS[Math.min(next.attempts, DELAYS.length) - 1];
    action = { do: "reconnect", delay };
  }
  return { action, prominent, state: next };
}
