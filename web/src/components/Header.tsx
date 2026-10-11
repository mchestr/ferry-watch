import { Monitor, Moon, Sun } from "lucide-react";
import { api } from "@/lib/api";
import { useStateMutation } from "@/lib/hooks";
import type { LiveStatus } from "@/lib/live";
import type { State } from "@/lib/schema";
import { useTheme, type ThemeSetting } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";
import { Switch } from "./ui/switch";
import { Tooltip } from "./ui/tooltip";

const NEXT_THEME: Record<ThemeSetting, ThemeSetting> = { system: "light", light: "dark", dark: "system" };
const THEME_ICON = { system: Monitor, light: Sun, dark: Moon } as const;

const CONNECTION = {
  connecting: {
    label: "Connecting",
    spoken: "Connecting to live updates",
    detail: "Connecting for live updates…",
    badge: "bg-muted text-muted-foreground",
    dot: "bg-muted-foreground",
  },
  live: {
    label: "Live",
    spoken: "Live updates on",
    detail: "Updates arrive in real time",
    badge: "bg-success-soft text-success",
    dot: "bg-success motion-safe:animate-pulse-ring",
  },
  reconnecting: {
    label: "Reconnecting",
    spoken: "Live updates lost, reconnecting",
    detail: "Live updates dropped. Checking every 5 seconds until they're back.",
    badge: "bg-warning-soft text-warning",
    // Hollow, so it differs from "live" by more than color.
    dot: "border-2 border-warning",
  },
} as const;

function ConnectionIndicator({ status }: { status: LiveStatus }) {
  const c = CONNECTION[status];
  return (
    <Tooltip content={c.detail}>
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1.5 rounded-full p-1.5 text-[11px] font-semibold uppercase tracking-wide sm:ml-1 sm:px-2 sm:py-0.5",
          c.badge,
        )}
      >
        <span role="img" aria-label={c.spoken} className={cn("size-2 rounded-full", c.dot)} />
        <span aria-hidden className="hidden sm:inline">
          {c.label}
        </span>
      </span>
    </Tooltip>
  );
}

export function Header({ state, live }: { state: State | undefined; live: LiveStatus }) {
  const { setting, setTheme } = useTheme();
  const toggle = useStateMutation((enabled: boolean) => api.updateSettings({ enabled }), {
    success: (on) => (on ? "Watching resumed" : "Watching paused"),
  });
  const next = NEXT_THEME[setting];
  const ThemeIcon = THEME_ICON[setting];

  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-2 px-4 sm:gap-3 sm:px-6">
        <div className="flex min-w-0 items-center gap-2 sm:gap-2.5">
          <img src="/favicon.svg" alt="" className="size-7 shrink-0 sm:size-8" />
          <span className="truncate text-[17px] font-bold tracking-tight">Ferry Watch</span>
          <ConnectionIndicator status={live} />
        </div>
        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          {state && (
            <label className="flex h-9 cursor-pointer items-center gap-2.5 rounded-full border bg-card pl-3.5 pr-1.5 text-[13px] font-medium shadow-sm">
              {state.enabled ? "Watching" : "Paused"}
              <Switch
                checked={state.enabled}
                onCheckedChange={(on) => toggle.mutate(on)}
                disabled={toggle.isPending}
                aria-label="Watch all trips"
              />
            </label>
          )}
          <Tooltip content={`Theme: ${setting}`}>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setTheme(next)}
              aria-label={`Theme: ${setting}. Switch to ${next}`}
            >
              <ThemeIcon aria-hidden />
            </Button>
          </Tooltip>
        </div>
      </div>
    </header>
  );
}
