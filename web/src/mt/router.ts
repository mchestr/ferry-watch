/** A tiny router: the four pages ferry-web serves index.html on. */
import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  | { page: "checkout"; tripId: string }
  | { page: "settings" }
  | { page: "admin" }
  | { page: "callback" }
  | { page: "missing" };

export function route(pathname: string): Route {
  if (pathname === "/") return { page: "home" };
  if (pathname === "/settings") return { page: "settings" };
  if (pathname === "/admin") return { page: "admin" };
  if (pathname === "/auth/callback") return { page: "callback" };
  const checkout = /^\/c\/([a-f0-9]{16})$/.exec(pathname);
  if (checkout?.[1]) return { page: "checkout", tripId: checkout[1] };
  return { page: "missing" };
}

const listeners = new Set<() => void>();

export function navigate(path: string, { replace = false } = {}): void {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
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
