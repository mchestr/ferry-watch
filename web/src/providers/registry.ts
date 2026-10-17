/**
 * The providers this build can show (docs/waitlist/design.md §7.5). A Map, never an object index, so a provider id
 * such as "constructor" or "__proto__" resolves to nothing; anything not here is shown as an unknown watch.
 */
import type { Watch } from "@/mt/watch-schema";
import { bcferriesUi } from "./bcferries";
import { buntzenUi } from "./buntzen";
import type { ProviderUi } from "./types";

/** Each entry is only ever called with watches of its own kind (looked up by `watch.kind`). */
const erase = <W extends Watch>(ui: ProviderUi<W>): ProviderUi => ui as unknown as ProviderUi;

const REGISTRY = new Map<string, ProviderUi>([
  ["bcferries", erase(bcferriesUi)],
  ["buntzen", erase(buntzenUi)],
]);

export function providerUi(kind: string): ProviderUi | null {
  return REGISTRY.get(kind) ?? null;
}

/** The UI for this watch, or null for an unknown one. */
export function uiFor(watch: Watch): ProviderUi | null {
  return watch.kind === "unknown" ? null : providerUi(watch.kind);
}
