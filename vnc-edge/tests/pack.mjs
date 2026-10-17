// The shared wl.cast1 conformance pack (tests/fixtures/cast1/README.md), as the viewer's tests read it.
import { readFileSync } from "node:fs";

export function pack(name) {
  return JSON.parse(readFileSync(new URL(`../../tests/fixtures/cast1/${name}`, import.meta.url), "utf8"));
}

/** The cases of an array the viewer runs: all but the agent's own. */
export function forViewer(cases) {
  return cases.filter((c) => c.only !== "agent");
}

export function bytes(hex) {
  return Uint8Array.from(hex.match(/../g) ?? [], (pair) => parseInt(pair, 16));
}
