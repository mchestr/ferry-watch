/** A tiny router: the pages waitlist-web serves index.html on (docs/waitlist/design.md §6.8, §7.1). */
import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  | { page: "new"; provider: string | null }
  | { page: "watch"; watchId: string }
  | { page: "checkout"; tripId: string }
  | { page: "settings" }
  | { page: "admin" }
  | { page: "callback" }
  | { page: "missing" };

const NEW = /^\/new(?:\/([a-z][a-z0-9_]{1,31}))?$/;
const WATCH = /^\/w\/([a-f0-9]{16})$/;
const CHECKOUT = /^\/c\/([a-f0-9]{16})$/;

export function route(pathname: string): Route {
  if (pathname === "/") return { page: "home" };
  if (pathname === "/settings") return { page: "settings" };
  if (pathname === "/admin") return { page: "admin" };
  if (pathname === "/auth/callback") return { page: "callback" };
  const fresh = NEW.exec(pathname);
  if (fresh) return { page: "new", provider: fresh[1] ?? null };
  const watch = WATCH.exec(pathname);
  if (watch?.[1]) return { page: "watch", watchId: watch[1] };
  const checkout = CHECKOUT.exec(pathname);
  if (checkout?.[1]) return { page: "checkout", tripId: checkout[1] };
  return { page: "missing" };
}

const listeners = new Set<() => void>();

/** Goes to `path`. `state` rides along in the history entry (the New watch sheet marks the entries it adds). */
export function navigate(path: string, { replace = false, state = null }: { replace?: boolean; state?: unknown } = {}) {
  if (replace) history.replaceState(state, "", path);
  else history.pushState(state, "", path);
  for (const listener of listeners) listener();
}

export function useRoute(): Route {
  const [current, setCurrent] = useState(() => route(location.pathname));
  useEffect(() => {
    const update = () => setCurrent(route(location.pathname));
    listeners.add(update);
    window.addEventListener("popstate", update);
    return () => {
      listeners.delete(update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return current;
}
