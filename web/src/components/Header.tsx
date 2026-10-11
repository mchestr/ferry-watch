import { Monitor, Moon, Sun } from "lucide-react";
import { api, type State } from "@/lib/api";
import { useStateMutation, useTheme } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";
import { Switch } from "./ui/switch";
import { Tooltip } from "./ui/tooltip";

export function Header({ state, live }: { state?: State; live: boolean }) {
  const [theme, setTheme] = useTheme();
  const toggle = useStateMutation((enabled: boolean) => api.saveSettings({ enabled }), (on) => (on ? "Watching resumed" : "Watching paused"));
  const next = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
  const ThemeIcon = theme === "system" ? Monitor : theme === "light" ? Sun : Moon;

  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
        <div className="flex items-center gap-2.5">
          <img src="/favicon.svg" alt="" className="size-8" />
          <span className="text-[17px] font-bold tracking-tight">Ferry Watch</span>
          <Tooltip content={live ? "Updates arrive in real time" : "Reconnecting, falling back to polling"}>
            <span
              className={cn(
                "ml-1 hidden items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide sm:inline-flex",
                live ? "bg-success-soft text-success" : "bg-warning-soft text-warning",
              )}
            >
              <span className={cn("size-1.5 rounded-full", live ? "bg-success animate-pulse-ring" : "bg-warning")} />
              {live ? "Live" : "Reconnecting"}
            </span>
          </Tooltip>
        </div>
        <div className="flex items-center gap-2">
          {state && (
            <label className="flex h-9 cursor-pointer items-center gap-2.5 rounded-full border bg-card pl-3.5 pr-1.5 text-[13px] font-medium shadow-sm">
              {state.enabled ? "Watching" : "Paused"}
              <Switch checked={state.enabled} onCheckedChange={(v) => toggle.mutate(v)} disabled={toggle.isPending} />
            </label>
          )}
          <Tooltip content={`Theme: ${theme}`}>
            <Button variant="ghost" size="icon" onClick={() => setTheme(next)} aria-label="Change theme">
              <ThemeIcon />
            </Button>
          </Tooltip>
        </div>
      </div>
    </header>
  );
}
