/**
 * /admin: the fleet and the people (docs/multi-tenant/design-reference.md §9.4). Every change asks why
 * and lands in the audit log. Admins never see a tenant's screen or sailings, only the cart summary.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, RotateCcw, UserPlus } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/controls";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/api";
import { useNow } from "@/lib/hooks";
import { ago, timeLabel } from "@/lib/time";
import { plural } from "@/lib/utils";
import {
  ADMIN_KEYS,
  adminApi,
  LIMIT_LABELS,
  type AdminUser,
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
const SWITCHES: readonly { name: FleetSwitch; label: string; on: string; off: string }[] = [
  {
    name: "maintenance",
    label: "Maintenance",
    on: "No browsers run. Trips wait until it's off.",
    off: "Browsers start as trips need them.",
  },
  {
    name: "pause_carting",
    label: "Pause carting",
    on: "Alerts only: nobody's sailing is put in a cart (for a Talos upgrade, or if guest checkout breaks).",
    off: "Bookable sailings go into a guest cart for their owner.",
  },
];

/** What a change needs from the reason dialog. */
interface Ask {
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  done: string;
  run: (reason: string) => Promise<unknown>;
}

/** One reason dialog for a section: `ask` opens it, and a confirmed change refreshes the admin data. */
function useReasoned() {
  const qc = useQueryClient();
  const [ask, setAsk] = useState<Ask | null>(null);
  const change = useMutation({
    mutationFn: ({ run, reason }: { run: Ask["run"]; reason: string }) => run(reason),
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
      pending={change.isPending}
      error={change.error ? errorMessage(change.error) : null}
      onConfirm={(reason) => {
        if (ask) change.mutate({ run: ask.run, reason });
      }}
    />
  );
  return {
    ask: (next: Ask) => {
      change.reset();
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
        <h1 className="text-xl font-bold tracking-tight">Admin</h1>
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

  return (
    <div className="grid gap-6 lg:grid-cols-2">
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
                  <p className="text-sm text-muted-foreground">{on ? s.on : s.off}</p>
                </div>
                <Switch
                  aria-labelledby={`switch-${s.name}`}
                  checked={on}
                  onCheckedChange={(next) =>
                    ask({
                      title: `Turn ${s.label.toLowerCase()} ${next ? "on" : "off"}?`,
                      description: next ? s.on : s.off,
                      confirmLabel: next ? "Turn on" : "Turn off",
                      danger: next,
                      done: `${s.label} is ${next ? "on" : "off"}`,
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
              return (
                <li key={hold.trip} className="flex items-center justify-between gap-3 px-5 py-3 text-sm">
                  <span className="min-w-0">
                    <span className="font-medium">{name(hold.user)}</span>
                    <span className="text-muted-foreground">
                      {hold.cart?.depart ? ` · ${timeLabel(hold.cart.depart)}` : ""} · held{" "}
                      {ago(hold.since * 1000, now)}
                    </span>
                  </span>
                  {left !== null && <Badge variant={left < 10 ? "warning" : "muted"}>{left} min left</Badge>}
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
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 px-5 pb-5 text-sm">
          {OUTCOMES.map(([key, label]) => (
            <div key={key} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="tabular text-right font-medium">{data.outcomes_7d[key] ?? 0}</dd>
            </div>
          ))}
        </dl>
      </Card>
      {dialog}
    </div>
  );
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
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex flex-wrap items-center gap-2 font-semibold">
            {user.username}
            {user.is_admin && <Badge variant="outline">Admin</Badge>}
            {suspended && <Badge variant="danger">Suspended</Badge>}
            {user.paused && <Badge variant="muted">Paused</Badge>}
          </h2>
          <p className="text-sm text-muted-foreground">
            {user.email ? `${user.email} · ` : ""}
            {plural(user.trips, "trip")} ·{" "}
            {user.last_seen_at ? `seen ${ago(user.last_seen_at * 1000, now)}` : "never signed in"}
          </p>
        </div>
        <div className="flex gap-2">
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
                        description: "Signs them out everywhere and stops their browsers.",
                        confirmLabel: "Suspend",
                        danger: true,
                        done: `${user.username} is suspended`,
                        run: (reason) => adminApi.setState(user.id, "suspended", reason),
                      },
                )
              }
            >
              {suspended ? "Reinstate" : "Suspend"}
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
