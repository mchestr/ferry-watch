/**
 * /admin: the fleet and the people (docs/multi-tenant/design-reference.md §9.4). Every change asks why
 * and lands in the audit log. Admins never see a tenant's screen or sailings, only the cart summary.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, RotateCcw, UserPlus } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/controls";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import { ago, timeLabel } from "@/lib/time";
import { cn, plural } from "@/lib/utils";
import {
  ADMIN_KEYS,
  adminApi,
  browserVersions,
  LIMIT_LABELS,
  quantityGiB,
  USAGE_LABELS,
  type AdminUser,
  type FleetReport,
  type FleetSwitch,
  type Invite,
  type Overview,
} from "./admin";
import { minutesLeft } from "./trips";
import { ReasonDialog } from "./ReasonDialog";

type Tab = "fleet" | "people";

const PHASES = ["creating", "starting", "watching", "holding", "draining", "failed"] as const;
const OUTCOMES: readonly [string, string][] = [
  ["booked", "Booked"],
  ["released", "Released"],
  ["expired", "Ran out of time"],
  ["expired_by_site", "Expired by BC Ferries"],
  ["payment_left_to_site", "Paid on bcferries.com"],
  ["none", "Ended without an answer"],
];
interface FleetSwitchCopy {
  name: FleetSwitch;
  label: string;
  /** What it does while on: always shown, and asked before turning it on. */
  on: string;
  /** What turning it off does. */
  off: string;
  /** The reason dialog's title, turning it on and off. */
  ask: readonly [on: string, off: string];
  done: readonly [on: string, off: string];
}
const SWITCHES: readonly FleetSwitchCopy[] = [
  {
    name: "maintenance",
    label: "Pause everything",
    on: "Runs no browsers, so nothing is checked. Holds already in progress finish.",
    off: "Browsers start again as trips need them.",
    ask: ["Pause everything?", "Run everything again?"],
    done: ["Everything is paused", "Everything runs again"],
  },
  {
    name: "pause_carting",
    label: "Pause carting",
    on: "Alerts only: nobody's sailing is put in a cart.",
    off: "Bookable sailings go into a cart for their owner again.",
    ask: ["Pause carting?", "Cart sailings again?"],
    done: ["Carting is paused", "Carting is back on"],
  },
];

/** ferry-control reports every pass; past this, the Fleet page says it may not be running. */
const CONTROL_LATE_MS = 2 * 60_000;

/** The attention list, in plain words. `who` is a username, or "someone". */
const ATTENTION: Readonly<Record<string, (who: string) => string>> = {
  needs_attention: (who) => `${who}'s trip needs attention`,
  delivery_failing: (who) => `Alerts to ${who} keep failing`,
  release_unverified: (who) => `Couldn't confirm ${who}'s cart was released`,
  checkout_blocked: (who) => `${who}'s checkout hit a blocked page`,
};

/** One attention entry as a sentence; a kind this page doesn't know yet shows by its name. */
function attentionText(kind: string, who: string): string {
  const text = ATTENTION[kind]?.(who) ?? `${who}: ${kind.replace(/_/g, " ")}`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** What a change needs from the reason dialog. */
interface Ask {
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  /** They must type this too, such as the username before a delete. */
  confirmText?: string;
  /** One more thing to decide, as radio buttons; `run` gets the value picked. */
  choice?: { legend: string; options: readonly { value: string; label: string }[]; initial: string };
  done: string;
  run: (reason: string, choice: string | undefined) => Promise<unknown>;
}

/** One reason dialog for a section: `ask` opens it, and a confirmed change refreshes the admin data. */
function useReasoned() {
  const qc = useQueryClient();
  const choiceName = useId();
  const [ask, setAsk] = useState<Ask | null>(null);
  const [picked, setPicked] = useState<string | undefined>();
  const change = useMutation({
    mutationFn: ({ run, reason, choice }: { run: Ask["run"]; reason: string; choice: string | undefined }) =>
      run(reason, choice),
    onSuccess: async () => {
      if (ask) toast.success(ask.done);
      setAsk(null);
      await qc.invalidateQueries({ queryKey: ["admin"] });
    },
  });
  const dialog = (
    <ReasonDialog
      open={ask !== null}
      onOpenChange={(open) => {
        if (!open) setAsk(null);
      }}
      title={ask?.title ?? ""}
      description={ask?.description}
      confirmLabel={ask?.confirmLabel ?? "Save"}
      danger={ask?.danger}
      confirmText={ask?.confirmText}
      pending={change.isPending}
      error={change.error ? errorMessage(change.error) : null}
      onConfirm={(reason) => {
        if (ask) change.mutate({ run: ask.run, reason, choice: picked });
      }}
    >
      {ask?.choice && (
        <fieldset disabled={change.isPending}>
          <legend className="mb-2 text-[13px] font-medium text-foreground/80">{ask.choice.legend}</legend>
          <div className="space-y-2">
            {ask.choice.options.map((o) => (
              <label key={o.value} className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name={choiceName}
                  value={o.value}
                  checked={picked === o.value}
                  onChange={() => setPicked(o.value)}
                  className="size-4 accent-primary"
                />
                {o.label}
              </label>
            ))}
          </div>
        </fieldset>
      )}
    </ReasonDialog>
  );
  return {
    ask: (next: Ask) => {
      change.reset();
      setPicked(next.choice?.initial);
      setAsk(next);
    },
    dialog,
  };
}

function Loading({ error }: { error: unknown }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
      {error ? (
        <>
          <AlertTriangle className="size-4 text-danger" aria-hidden /> {errorMessage(error)}
        </>
      ) : (
        <Loader2 className="size-4 motion-safe:animate-spin" aria-label="Loading" />
      )}
    </div>
  );
}

export function AdminPage({ meId }: { meId: string }) {
  const [tab, setTab] = useState<Tab>("fleet");
  const people = useQuery({ queryKey: ADMIN_KEYS.people, queryFn: ({ signal }) => adminApi.people(signal) });
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-bold tracking-tight">Admin</h1>
          <Versions />
        </div>
        <div className="flex gap-1 rounded-xl bg-muted p-1" role="group" aria-label="Admin pages">
          {(["fleet", "people"] as const).map((name) => (
            <Button
              key={name}
              size="sm"
              variant={tab === name ? "outline" : "ghost"}
              aria-pressed={tab === name}
              onClick={() => setTab(name)}
            >
              {name === "fleet" ? "Fleet" : "People"}
            </Button>
          ))}
        </div>
      </div>
      {tab === "fleet" ? (
        <Fleet users={people.data?.users} />
      ) : people.data ? (
        <PeoplePage meId={meId} users={people.data.users} invites={people.data.invites} />
      ) : (
        <Loading error={people.error} />
      )}
    </div>
  );
}

/**
 * Which release each part runs: this page, ferry-web, ferry-control and the browsers. A page left open across
 * a deploy keeps its old code, so it offers a reload when it's older than ferry-web.
 */
function Versions() {
  const overview = useQuery({ queryKey: ADMIN_KEYS.overview, queryFn: ({ signal }) => adminApi.overview(signal) });
  const versions = overview.data?.versions;
  const web = versions?.web?.version ?? null;
  const parts = [`Dashboard ${__APP_VERSION__}`];
  if (versions) {
    parts.push(`Web ${web ?? "—"}`, `Control ${versions.control?.version ?? "—"}`);
    const browsers = browserVersions(versions.browsers);
    if (browsers) parts.push(`Browsers ${browsers}`);
  }
  return (
    <p className="text-sm text-muted-foreground">
      {parts.join(" · ")}
      {web !== null && web !== __APP_VERSION__ && (
        <>
          {" · "}
          <button
            type="button"
            className="font-medium text-foreground underline underline-offset-2"
            onClick={() => location.reload()}
          >
            Reload to update
          </button>
        </>
      )}
    </p>
  );
}

// ---- Fleet

function Fleet({ users }: { users: readonly AdminUser[] | undefined }) {
  const now = useNow(15_000);
  const overview = useQuery({
    queryKey: ADMIN_KEYS.overview,
    queryFn: ({ signal }) => adminApi.overview(signal),
    refetchInterval: 15_000,
  });
  const { ask, dialog } = useReasoned();
  const data = overview.data;
  if (!data) return <Loading error={overview.error} />;
  const name = (id: string) => users?.find((u) => u.id === id)?.username ?? id;
  const fleet = data.fleet ?? null;
  const attention = data.attention ?? [];
  const breaker = breakerOpen(data);

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <ControlSeen seen={data.control_seen ?? fleet?.at ?? null} now={now} />

      <Card>
        <CardHeader>
          <CardTitle>Capacity</CardTitle>
        </CardHeader>
        <Facts rows={capacity(fleet, data.queued)} />
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Budget</CardTitle>
        </CardHeader>
        <Facts rows={budget(fleet)} />
        <div className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-4">
          <div className="min-w-0 text-sm">
            <p className="font-medium">
              Breaker: {breaker ? <span className="text-danger">Open</span> : <span>Closed</span>}
            </p>
            <p className="text-muted-foreground">
              {breaker
                ? "BC Ferries' pages changed on most trips, so checks are paused. Holds continue."
                : "Checks run as the budget allows."}
            </p>
          </div>
          {breaker && (
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                ask({
                  title: "Close the breaker?",
                  description:
                    "Checks start again. Close it once the browsers can read BC Ferries' pages, usually after a new image.",
                  confirmLabel: "Close the breaker",
                  done: "The breaker is closed",
                  run: (reason) => adminApi.closeBreaker(reason),
                })
              }
            >
              Close the breaker
            </Button>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Attention</CardTitle>
        </CardHeader>
        {attention.length ? (
          <ul className="divide-y border-t">
            {attention.map((a, i) => (
              <li key={`${a.kind}-${i}`} className="px-5 py-3 text-sm">
                <span className="font-medium">{attentionText(a.kind, a.user ? name(a.user) : "someone")}</span>
                <span className="block break-words text-muted-foreground">
                  {a.detail ? `${a.detail} · ` : ""}
                  {ago(a.at * 1000, now)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-5 pb-5 text-sm text-muted-foreground">Nothing needs you.</p>
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Switches</CardTitle>
        </CardHeader>
        <div className="space-y-4 px-5 pb-5">
          {SWITCHES.map((s) => {
            const on = data.switches[s.name] === true;
            return (
              <div key={s.name} className="flex items-start justify-between gap-4">
                <div>
                  <p id={`switch-${s.name}`} className="text-sm font-medium">
                    {s.label}
                  </p>
                  <p className="text-sm text-muted-foreground">{s.on}</p>
                </div>
                <Switch
                  aria-labelledby={`switch-${s.name}`}
                  checked={on}
                  onCheckedChange={(next) =>
                    ask({
                      title: next ? s.ask[0] : s.ask[1],
                      description: next ? s.on : s.off,
                      confirmLabel: next ? "Turn on" : "Turn off",
                      danger: next,
                      done: next ? s.done[0] : s.done[1],
                      run: (reason) => adminApi.setSwitch(s.name, next, reason),
                    })
                  }
                />
              </div>
            );
          })}
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Browsers</CardTitle>
          <span className="text-sm text-muted-foreground">{data.queued} queued</span>
        </CardHeader>
        <BrowserTable pods={data.pods} />
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Holds now</CardTitle>
        </CardHeader>
        {data.holds.length ? (
          <ul className="divide-y border-t">
            {data.holds.map((hold) => {
              const left = minutesLeft(hold.cart?.hard_deadline ?? null, now);
              const who = name(hold.user);
              const adding = hold.cart?.status === "adding";
              const paying = hold.cart?.payment_in_progress === true;
              // The server releases only a cart that's in and not being paid for: one still being added gets a
              // new id once it's in, and pulling one mid-payment could charge them for nothing.
              const releasable = hold.cart?.status === "ready" && !paying;
              return (
                <li key={hold.trip} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-sm">
                  <span className="min-w-0">
                    <span className="font-medium">{who}</span>
                    <span className="text-muted-foreground">
                      {hold.cart?.depart ? ` · ${timeLabel(hold.cart.depart)}` : ""} · held{" "}
                      {ago(hold.since * 1000, now)}
                    </span>
                  </span>
                  <span className="flex items-center gap-1">
                    {adding && <Badge variant="muted">Being added</Badge>}
                    {paying && <Badge variant="warning">Paying now</Badge>}
                    {left !== null && <Badge variant={left < 10 ? "warning" : "muted"}>{left} min left</Badge>}
                    {releasable && (
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={`Release ${who}'s cart`}
                        onClick={() =>
                          ask({
                            title: `Release ${who}'s cart?`,
                            description: `Empties the cart on BC Ferries and tells ${who} it was released.`,
                            confirmLabel: "Release",
                            danger: true,
                            done: `Releasing ${who}'s cart`,
                            run: (reason) => adminApi.releaseCart(hold.trip, reason),
                          })
                        }
                      >
                        Release
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Recycle ${who}'s browser`}
                      onClick={() =>
                        ask({
                          title: `Recycle ${who}'s browser?`,
                          description: `Restarts their browser${hold.pod ? ` (${hold.pod})` : ""}. The cart it holds is emptied first, unless they're already paying.`,
                          confirmLabel: "Recycle",
                          danger: true,
                          done: `Recycling ${who}'s browser`,
                          run: (reason) => adminApi.recyclePod(hold.trip, reason),
                        })
                      }
                    >
                      Recycle
                    </Button>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="px-5 pb-5 text-sm text-muted-foreground">Nobody is holding a cart.</p>
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Holds this week</CardTitle>
        </CardHeader>
        <Facts rows={OUTCOMES.map(([key, label]) => [label, data.outcomes_7d[key] ?? 0] as const)} />
      </Card>
      {dialog}
    </div>
  );
}

/** Whether ferry-control is alive. It turns into a warning past two minutes, or when it never reported. */
function ControlSeen({ seen, now }: { seen: number | null; now: number }) {
  const late = seen === null || now - seen * 1000 > CONTROL_LATE_MS;
  return (
    <p
      className={cn(
        "flex items-center gap-2 text-sm lg:col-span-2",
        late ? "font-medium text-warning" : "text-muted-foreground",
      )}
    >
      {late && <AlertTriangle className="size-4 shrink-0" aria-hidden />}
      {seen === null
        ? "ferry-control hasn't reported. Is it running?"
        : `ferry-control last reported ${ago(seen * 1000, now)}${late ? ". Is it running?" : ""}`}
    </p>
  );
}

type Fact = readonly [label: string, value: ReactNode];

/** Labels and values, two columns. */
function Facts({ rows }: { rows: readonly Fact[] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 px-5 pb-5 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="tabular text-right font-medium">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** 1.25 -> "1.3", 3 -> "3". */
const decimal = (n: number) => String(Number(n.toFixed(1)));
const gib = (n: number) => `${decimal(n)} GiB`;

/** Only what ferry-control reported; queued falls back to ferry-web's own count. */
function capacity(fleet: FleetReport | null, queued: number): Fact[] {
  const rows: Fact[] = [];
  const max = fleet?.browsers?.max ?? null;
  const live = fleet?.browsers?.live ?? null;
  if (live !== null) rows.push(["Browsers running", max !== null ? `${live} of ${max}` : String(live)]);
  const requested = fleet?.memory?.requested_mb ?? null;
  const pod = fleet?.memory?.pod ? quantityGiB(fleet.memory.pod) : null;
  const total = fleet?.memory?.budget_mb ?? (pod !== null && max !== null ? pod * max * 1024 : null);
  if (requested !== null) {
    rows.push(["Memory requested", `${gib(requested / 1024)}${total !== null ? ` of ${gib(total / 1024)}` : ""}`]);
  }
  rows.push(["Queued", fleet?.queued ?? queued]);
  const due = fleet?.short_checks?.due ?? null;
  const overdue = fleet?.short_checks?.overdue ?? null;
  if (due !== null || overdue !== null) {
    rows.push([
      "Short checks",
      <>
        {due ?? 0} due · <span className={overdue ? "text-warning" : undefined}>{overdue ?? 0} overdue</span>
      </>,
    ]);
  }
  return rows;
}

function budget(fleet: FleetReport | null): Fact[] {
  if (!fleet) return [["Last report", "None yet"]];
  const rows: Fact[] = [];
  const allowed = fleet.budget?.checks_per_minute ?? null;
  const demand = fleet.budget?.demand_per_minute ?? null;
  const stretch = fleet.budget?.stretch ?? null;
  if (allowed !== null) rows.push(["Allowed", `${decimal(allowed)} checks a minute`]);
  if (demand !== null) rows.push(["Asked for", `${decimal(demand)} checks a minute`]);
  if (stretch !== null) {
    rows.push(["Stretch", stretch > 1 ? <span className="text-warning">×{decimal(stretch)} slower</span> : "None"]);
  }
  if (fleet.brake) {
    const { multiplier, paused } = fleet.brake;
    rows.push([
      "Brake",
      paused ? (
        <span className="text-danger">Checks stopped</span>
      ) : multiplier && multiplier > 1 ? (
        <span className="text-warning">×{decimal(multiplier)} slower</span>
      ) : (
        "Off"
      ),
    ]);
  }
  return rows;
}

/** The breaker as stored (an admin's close shows at once), else as ferry-control last reported it. */
function breakerOpen(data: Overview): boolean {
  const stored = data.switches.breaker;
  return (stored === "open" || stored === "closed" ? stored : data.fleet?.breaker) === "open";
}

function BrowserTable({ pods }: { pods: Overview["pods"] }) {
  const count = (mode: string, phase: string) =>
    pods.filter((p) => p.mode === mode && p.phase === phase).reduce((n, p) => n + p.count, 0);
  const phases = PHASES.filter((phase) => pods.some((p) => p.phase === phase));
  if (!phases.length) return <p className="px-5 pb-5 text-sm text-muted-foreground">No browsers running.</p>;
  return (
    <div className="overflow-x-auto px-5 pb-5">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="py-1 pr-3 font-medium" scope="col">
              <span className="sr-only">Kind</span>
            </th>
            {phases.map((phase) => (
              <th key={phase} scope="col" className="py-1 pr-3 font-medium capitalize">
                {phase}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(
            [
              ["warm", "Warm"],
              ["short_check", "Short checks"],
            ] as const
          ).map(([mode, label]) => (
            <tr key={mode}>
              <th scope="row" className="py-1 pr-3 text-left font-medium">
                {label}
              </th>
              {phases.map((phase) => (
                <td key={phase} className="tabular py-1 pr-3">
                  {count(mode, phase)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- People

function PeoplePage({
  meId,
  users,
  invites,
}: {
  meId: string;
  users: readonly AdminUser[];
  invites: readonly Invite[];
}) {
  const now = useNow(60_000);
  const qc = useQueryClient();
  const unmatched = useQuery({ queryKey: ADMIN_KEYS.unmatched, queryFn: ({ signal }) => adminApi.unmatched(signal) });
  const { ask, dialog } = useReasoned();
  const [draft, setDraft] = useState({ username: "", email: "", reason: "" });
  const invite = useMutation({
    mutationFn: () =>
      adminApi.invite({ username: draft.username.trim(), email: draft.email.trim() || null, reason: draft.reason }),
    onSuccess: async () => {
      toast.success(draft.email.trim() ? "Invited; the email is on its way" : "Invited");
      setDraft({ username: "", email: "", reason: "" });
      await qc.invalidateQueries({ queryKey: ["admin"] });
    },
  });

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div className="min-w-0 space-y-4">
        {users.map((user) => (
          <PersonCard key={user.id} user={user} isMe={user.id === meId} now={now} ask={ask} />
        ))}
      </div>
      <aside className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Add a person</CardTitle>
          </CardHeader>
          <form
            className="space-y-3 px-5 pb-5"
            onSubmit={(event) => {
              event.preventDefault();
              invite.mutate();
            }}
          >
            <Field label="Sign-in username" hint="As your identity provider knows them.">
              <Input
                value={draft.username}
                required
                onChange={(e) => setDraft({ ...draft, username: e.target.value })}
              />
            </Field>
            <Field label="Email (optional)" hint="We'll send them the invitation.">
              <Input type="email" value={draft.email} onChange={(e) => setDraft({ ...draft, email: e.target.value })} />
            </Field>
            <Field label="Reason" error={invite.error ? errorMessage(invite.error) : null}>
              <Input
                value={draft.reason}
                required
                maxLength={500}
                onChange={(e) => setDraft({ ...draft, reason: e.target.value })}
              />
            </Field>
            <Button
              type="submit"
              className="w-full"
              disabled={invite.isPending || !draft.username.trim() || !draft.reason.trim()}
            >
              <UserPlus aria-hidden /> Invite
            </Button>
          </form>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Open invitations</CardTitle>
          </CardHeader>
          {invites.length ? (
            <ul className="divide-y border-t">
              {invites.map((i) => (
                <li key={i.id} className="flex items-center justify-between gap-3 px-5 py-3 text-sm">
                  <span className="min-w-0">
                    <span className="font-medium">{i.username}</span>
                    <span className="block break-words text-muted-foreground">
                      {i.email ? `${i.email} · ` : ""}until{" "}
                      {new Date(i.expires_at * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                    </span>
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      ask({
                        title: `Withdraw ${i.username}'s invitation?`,
                        confirmLabel: "Withdraw",
                        danger: true,
                        done: "Invitation withdrawn",
                        run: (reason) => adminApi.revokeInvite(i.id, reason),
                      })
                    }
                  >
                    Withdraw
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-5 pb-5 text-sm text-muted-foreground">None open.</p>
          )}
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Turned away</CardTitle>
          </CardHeader>
          <p className="px-5 pb-3 text-sm text-muted-foreground">Sign-ins without an invitation, last 30 days.</p>
          {unmatched.data?.length ? (
            <ul className="divide-y border-t">
              {unmatched.data.map((u) => (
                <li
                  key={`${u.issuer}|${u.username ?? ""}|${u.email ?? ""}`}
                  className="flex items-center justify-between gap-3 px-5 py-3 text-sm"
                >
                  <span className="min-w-0">
                    <span className="font-medium">{u.username ?? "(no username)"}</span>
                    <span className="block break-words text-muted-foreground">
                      {u.email ? `${u.email} · ` : ""}
                      {ago(u.last_seen * 1000, now)}
                    </span>
                  </span>
                  {u.username && (
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Invite ${u.username}`}
                      onClick={() => setDraft({ username: u.username ?? "", email: u.email ?? "", reason: "" })}
                    >
                      Invite
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          ) : unmatched.data ? (
            <p className="px-5 pb-5 text-sm text-muted-foreground">Nobody.</p>
          ) : (
            <div className="px-5">
              <Loading error={unmatched.error} />
            </div>
          )}
        </Card>
      </aside>
      {dialog}
    </div>
  );
}

function PersonCard({
  user,
  isMe,
  now,
  ask,
}: {
  user: AdminUser;
  isMe: boolean;
  now: number;
  ask: (ask: Ask) => void;
}) {
  const [showLimits, setShowLimits] = useState(false);
  const suspended = user.state === "suspended";
  const counts = [
    user.sessions !== undefined ? plural(user.sessions, "session") : null,
    user.devices !== undefined ? `alerts on ${plural(user.devices, "device")}` : null,
  ].filter((part) => part !== null);
  const usage = Object.entries(user.usage_today ?? {})
    .filter(([, amount]) => amount > 0)
    .map(([kind, amount]) => USAGE_LABELS[kind]?.(amount) ?? `${amount} ${kind}`);
  const otherEmails = user.emails_seen ?? [];
  // Bootstrap admins come from the config; the server refuses to demote them.
  const adminChange = !isMe && !(user.is_admin && user.bootstrap);
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex flex-wrap items-center gap-2 font-semibold">
            {user.username}
            {user.is_admin && <Badge variant="outline">Admin</Badge>}
            {user.bootstrap && <Badge variant="outline">Bootstrap</Badge>}
            {suspended && <Badge variant="danger">Suspended</Badge>}
          </h2>
          <p className="text-sm text-muted-foreground">
            {user.email ? `${user.email} · ` : ""}
            {plural(user.trips, "trip")} ·{" "}
            {user.last_seen_at ? `seen ${ago(user.last_seen_at * 1000, now)}` : "never signed in"}
          </p>
          {counts.length > 0 && <p className="text-sm text-muted-foreground">{counts.join(" · ")}</p>}
          {usage.length > 0 && <p className="text-sm text-muted-foreground">Today: {usage.join(" · ")}</p>}
          {otherEmails.length > 0 && (
            <p className="mt-1 flex items-start gap-1.5 break-words text-sm font-medium text-warning">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span className="min-w-0">Signed in with another email: {otherEmails.join(", ")}</span>
            </p>
          )}
          {user.bootstrap && user.is_admin && (
            <p className="mt-1 text-xs text-muted-foreground">
              A bootstrap admin: set in the config, so their admin can't be removed here.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            aria-label={`Limits for ${user.username}`}
            aria-expanded={showLimits}
            onClick={() => setShowLimits(!showLimits)}
          >
            Limits
          </Button>
          {!isMe && (
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Sign out everywhere for ${user.username}`}
              onClick={() =>
                ask({
                  title: `Sign ${user.username} out everywhere?`,
                  description: "Every device they're signed in on has to sign in again.",
                  confirmLabel: "Sign out",
                  danger: true,
                  done: `${user.username} is signed out everywhere`,
                  run: (reason) => adminApi.revokeSessions(user.id, reason),
                })
              }
            >
              Sign out everywhere
            </Button>
          )}
          {adminChange && (
            <Button
              size="sm"
              variant="ghost"
              aria-label={`${user.is_admin ? "Remove admin" : "Make admin"} for ${user.username}`}
              onClick={() =>
                ask(
                  user.is_admin
                    ? {
                        title: `Remove ${user.username}'s admin?`,
                        description: "They keep their account and trips.",
                        confirmLabel: "Remove admin",
                        danger: true,
                        done: `${user.username} is no longer an admin`,
                        run: (reason) => adminApi.setAdmin(user.id, false, reason),
                      }
                    : {
                        title: `Make ${user.username} an admin?`,
                        description:
                          "They'll see the fleet and everyone's limits, and can change them. Never anyone's screen or sailings.",
                        confirmLabel: "Make admin",
                        done: `${user.username} is an admin`,
                        run: (reason) => adminApi.setAdmin(user.id, true, reason),
                      },
                )
              }
            >
              {user.is_admin ? "Remove admin" : "Make admin"}
            </Button>
          )}
          {!isMe && (
            <Button
              size="sm"
              variant={suspended ? "outline" : "ghost"}
              aria-label={`${suspended ? "Reinstate" : "Suspend"} ${user.username}`}
              onClick={() =>
                ask(
                  suspended
                    ? {
                        title: `Reinstate ${user.username}?`,
                        description: "They can sign in again; their trips start back up.",
                        confirmLabel: "Reinstate",
                        done: `${user.username} is reinstated`,
                        run: (reason) => adminApi.setState(user.id, "active", reason),
                      }
                    : {
                        title: `Suspend ${user.username}?`,
                        description:
                          "Signs them out everywhere and stops their browsers. A cart they're paying for is left to finish.",
                        confirmLabel: "Suspend",
                        danger: true,
                        choice: {
                          legend: "If they're holding a cart",
                          options: [
                            { value: "release", label: "Release their held cart now" },
                            { value: "finish", label: "Let it finish" },
                          ],
                          initial: "finish",
                        },
                        done: `${user.username} is suspended`,
                        run: (reason, holds) =>
                          adminApi.setState(user.id, "suspended", reason, holds === "release" ? "release" : "finish"),
                      },
                )
              }
            >
              {suspended ? "Reinstate" : "Suspend"}
            </Button>
          )}
          {suspended && !isMe && (
            <Button
              size="sm"
              variant="ghost"
              className="text-danger"
              aria-label={`Delete ${user.username}`}
              onClick={() =>
                ask({
                  title: `Delete ${user.username}?`,
                  description: (
                    <>
                      <p>Deletes their account, trips, sessions and alerts for good.</p>
                      <p>The audit log and hold history stay.</p>
                      <p>
                        It doesn't keep them out: if an invitation or group still lets them in, they can sign in again
                        as someone new. Remove them from the sign-in group first.
                      </p>
                    </>
                  ),
                  confirmLabel: "Delete",
                  danger: true,
                  confirmText: user.username,
                  done: `${user.username} is deleted`,
                  run: (reason) => adminApi.deleteUser(user.id, reason),
                })
              }
            >
              Delete
            </Button>
          )}
        </div>
      </div>
      {showLimits && <LimitsEditor user={user} ask={ask} />}
    </Card>
  );
}

function LimitsEditor({ user, ask }: { user: AdminUser; ask: (ask: Ask) => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  return (
    <ul className="mt-4 divide-y border-t">
      {Object.entries(LIMIT_LABELS).map(([key, label]) => {
        const limit = user.limits[key];
        if (!limit) return null;
        const typed = values[key] ?? String(limit.value);
        const next = /^\d{1,4}$/.test(typed) ? Number(typed) : null;
        return (
          <li key={key} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 py-2 text-sm">
            <span>
              {label}{" "}
              <Badge variant={limit.source === "default" ? "muted" : "warning"} className="ml-1">
                {limit.source}
              </Badge>
            </span>
            <span className="flex items-center gap-2">
              <Input
                aria-label={label}
                inputMode="numeric"
                className="h-8 w-16"
                value={typed}
                onChange={(e) => setValues({ ...values, [key]: e.target.value })}
              />
              <Button
                size="sm"
                variant="outline"
                disabled={next === null || next === limit.value}
                onClick={() =>
                  ask({
                    title: `${label}: ${next ?? ""} for ${user.username}?`,
                    description: "Never more than the ceiling the chart sets.",
                    confirmLabel: "Set",
                    done: `${label} set`,
                    run: (reason) => adminApi.setLimit(user.id, key, next, reason),
                  })
                }
              >
                Set
              </Button>
              {limit.source === "default" ? (
                <span className="size-8" aria-hidden />
              ) : (
                <Button
                  size="icon"
                  variant="ghost"
                  className="size-8"
                  aria-label={`${label}: use the default`}
                  title="Use the default"
                  onClick={() =>
                    ask({
                      title: `${label}: back to the default for ${user.username}?`,
                      confirmLabel: "Use the default",
                      done: `${label} is back to the default`,
                      run: (reason) => adminApi.setLimit(user.id, key, null, reason),
                    })
                  }
                >
                  <RotateCcw aria-hidden />
                </Button>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
