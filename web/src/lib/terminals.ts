import type { Terminal } from "./schema";

const byLabel = (a: Terminal, b: Terminal) => a.label.localeCompare(b.label);

/** Terminals with at least one bookable destination, by label: the "From" list. */
export function origins(terminals: Terminal[]): Terminal[] {
  const known = new Set(terminals.map((t) => t.code));
  return terminals.filter((t) => t.dests.some((code) => code !== t.code && known.has(code))).sort(byLabel);
}

/** Where you can book to from `from`, by label: the "To" list. Unknown codes are left out. */
export function destinations(terminals: Terminal[], from: string): Terminal[] {
  const byCode = new Map(terminals.map((t) => [t.code, t]));
  const codes = new Set(byCode.get(from)?.dests ?? []);
  codes.delete(from);
  return [...codes]
    .map((code) => byCode.get(code))
    .filter((t) => t !== undefined)
    .sort(byLabel);
}
