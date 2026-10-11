export type Vehicle = "standard" | "tall" | "none";
export type Fare = "cheapest" | "saver" | "prepaid" | "reservation";

export interface TripConfig {
  id: string;
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
  hold: boolean;
  fare: Fare;
}

export interface Trip extends TripConfig {
  /** runtime state of the trip's cart (TripConfig.hold is the auto-cart setting) */
  sailings: Sailing[];
  last_check: string | null;
  last_error: string | null;
  session: { since: number; logged_in: boolean } | null;
  cart: Hold | null;
  checking: boolean;
  booking_link: string;
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
  trip: string | null;
}

export interface State {
  enabled: boolean;
  interval_seconds: number;
  trips: Trip[];
  checking: boolean;
  next_check: number | null;
  now: number;
  events: ActivityEvent[];
  vnc_url: string;
  login: boolean;
  pushover: boolean;
  max_trips: number;
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
  saveSettings: (patch: { enabled?: boolean; interval_seconds?: number }) => request<State>("/api/settings", patch),
  addTrip: (trip: Partial<TripConfig>) => request<State>("/api/trips", trip),
  updateTrip: (id: string, patch: Partial<TripConfig>) => request<State>(`/api/trips/${id}`, patch),
  deleteTrip: (id: string) => request<State>(`/api/trips/${id}/delete`, {}),
  releaseCart: (id: string) => request<State>(`/api/trips/${id}/release`, {}),
  dismissCart: (id: string) => request<State>(`/api/trips/${id}/dismiss`, {}),
  check: () => request<State>("/api/check", {}),
  test: () => request<State>("/api/test", {}),
};
