import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, type State } from "./api";

export function useAppState() {
  return useQuery({
    queryKey: ["state"],
    queryFn: api.state,
    // Poll fast while a check is running so results appear as soon as they land.
    refetchInterval: (q) => (q.state.data?.checking ? 1500 : 4000),
    refetchIntervalInBackground: false,
  });
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
