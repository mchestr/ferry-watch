export type Vehicle = "standard" | "tall" | "none";
export type Fare = "cheapest" | "saver" | "prepaid" | "reservation";

export interface Config {
  enabled: boolean;
  date: string;
  from: string;
  to: string;
  adults: number;
  children: number;
  infants: number;
  seniors: number;
  vehicle: Vehicle;
  height_ft: number;
  earliest: string;
  latest: string;
  interval_seconds: number;
  hold: boolean;
  fare: Fare;
}

export interface Sailing {
  depart: string;
  arrive: string;
  vessel: string;
  status: string;
  price: string;
  in_window?: boolean;
}

export interface Hold {
  status: "adding" | "ready" | "failed" | "closed";
  depart: string;
  date?: string;
  fare?: string | null;
  price?: string | null;
  at?: number;
  error?: string | null;
}

export interface ActivityEvent {
  ts: number;
  kind: "info" | "alert" | "cart" | "error";
  text: string;
}

export interface State {
  config: Config;
  checking: boolean;
  last_check: string | null;
  last_error: string | null;
  next_check: number | null;
  now: number;
  sailings: Sailing[];
  booking_link: string;
  hold: Hold | null;
  session: { since: number; logged_in: boolean } | null;
  events: ActivityEvent[];
  vnc_url: string;
  login: boolean;
  pushover: boolean;
}

export interface Terminal {
  label: string;
  short: string;
  dests: string[];
}
export type Terminals = Record<string, Terminal>;

async function request<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { error?: string }).error ?? res.statusText);
  return data as T;
}

export const api = {
  state: () => request<State>("/api/state"),
  terminals: () => request<Terminals>("/api/terminals"),
  saveConfig: (patch: Partial<Config>) => request<State>("/api/config", patch),
  check: () => request<State>("/api/check", {}),
  test: () => request<State>("/api/test", {}),
  dismissHold: () => request<State>("/api/hold/dismiss", {}),
  releaseHold: () => request<State>("/api/hold/release", {}),
};
