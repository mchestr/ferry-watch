import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** "3:00 pm" -> minutes since midnight */
export function toMinutes(t: string): number {
  const m = t.match(/(\d{1,2}):(\d{2})\s*(am|pm)?/i);
  if (!m) return 0;
  let h = Number(m[1]) % 12;
  if (m[3]?.toLowerCase() === "pm") h += 12;
  if (!m[3]) h = Number(m[1]);
  return h * 60 + Number(m[2]);
}

export function duration(depart: string, arrive: string): string {
  const d = (toMinutes(arrive) - toMinutes(depart) + 1440) % 1440;
  return `${Math.floor(d / 60)}h ${String(d % 60).padStart(2, "0")}m`;
}

export function fmt12(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
}

export function longDate(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

export function relDays(iso: string): string {
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const d = Math.round((new Date(`${iso}T12:00:00`).getTime() - today.getTime()) / 86400000);
  if (d === 0) return "Today";
  if (d === 1) return "Tomorrow";
  if (d === -1) return "Yesterday";
  return d > 0 ? `In ${d} days` : `${-d} days ago`;
}

export function ago(ms: number, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h}h ago` : new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** "Victoria (Swartz Bay)" -> ["Swartz Bay", "Victoria"] */
export function splitTerminal(short?: string): [string, string] {
  const m = (short ?? "").match(/^(.*?)\s*\((.*)\)$/);
  return m ? [m[2], m[1]] : [short ?? "—", ""];
}
