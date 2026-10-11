/**
 * Light / dark / system theme, shared by everything that needs it (the header toggle, the toaster).
 * index.html applies the stored theme with an inline script before React loads, so there's no
 * flash; applyTheme here must stay in step with that script.
 */
import { useEffect, useSyncExternalStore } from "react";
import { useMediaQuery } from "./hooks";

export type ThemeSetting = "light" | "dark" | "system";

const STORAGE_KEY = "theme";
const listeners = new Set<() => void>();
let setting: ThemeSetting = readStoredTheme();

function readStoredTheme(): ThemeSetting {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

export function setTheme(next: ThemeSetting): void {
  setting = next;
  try {
    if (next === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Storage unavailable (private browsing): the choice lasts until the page reloads.
  }
  listeners.forEach((listener) => {
    listener();
  });
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function applyTheme(dark: boolean): void {
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  root.style.colorScheme = dark ? "dark" : "light";
}

export function useTheme() {
  const current = useSyncExternalStore(
    subscribe,
    () => setting,
    () => setting,
  );
  const systemDark = useMediaQuery("(prefers-color-scheme: dark)");
  const resolved: "light" | "dark" = current === "dark" || (current === "system" && systemDark) ? "dark" : "light";
  useEffect(() => {
    applyTheme(resolved === "dark");
  }, [resolved]);
  return { setting: current, resolved, setTheme };
}
