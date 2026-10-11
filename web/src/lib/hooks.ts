import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, type State } from "./api";

export function useAppState(live: boolean) {
  return useQuery({
    queryKey: ["state"],
    queryFn: api.state,
    // The WebSocket pushes every change; poll only while it's disconnected.
    refetchInterval: live ? false : 5000,
    refetchIntervalInBackground: false,
  });
}

/** Keeps the ["state"] query current from the server's WebSocket push. Returns whether it's connected. */
export function useLiveUpdates() {
  const qc = useQueryClient();
  const [live, setLive] = useState(false);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const connect = () => {
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
      ws.onopen = () => {
        retry = 0;
        setLive(true);
      };
      ws.onmessage = (e) => qc.setQueryData(["state"], JSON.parse(e.data) as State);
      ws.onclose = () => {
        setLive(false);
        if (!stopped) timer = setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    // Phones suspend sockets in the background; reconnect straight away on return.
    const onVisible = () => {
      if (document.visibilityState === "visible" && ws?.readyState !== WebSocket.OPEN) {
        clearTimeout(timer);
        retry = 0;
        ws?.close();
        connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
      ws?.close();
    };
  }, [qc]);
  return live;
}

export function useTerminals() {
  return useQuery({ queryKey: ["terminals"], queryFn: api.terminals, staleTime: Infinity });
}

/** Wraps an API call that returns fresh state: writes it into the cache and toasts. */
export function useStateMutation<A>(fn: (arg: A) => Promise<State>, success?: string | ((arg: A) => string)) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (data, arg) => {
      qc.setQueryData(["state"], data);
      if (success) toast.success(typeof success === "function" ? success(arg) : success);
    },
    onError: (e: Error) => toast.error(e.message),
  });
}

/** Re-renders every `ms` so countdowns and "x ago" labels stay live. */
export function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

export function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark" | "system">(() => {
    try {
      return (localStorage.getItem("theme") as "light" | "dark" | null) ?? "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && mq.matches));
    apply();
    mq.addEventListener("change", apply);
    try {
      if (theme === "system") localStorage.removeItem("theme");
      else localStorage.setItem("theme", theme);
    } catch {
      /* storage unavailable */
    }
    return () => mq.removeEventListener("change", apply);
  }, [theme]);
  return [theme, setTheme] as const;
}
