# Waitlist: design contract (final)

**What this is.** Ferry Watch becomes **Waitlist**: one app, one dashboard, for watching several reservation
systems ("providers"). BC Ferries stays exactly as it is. Buntzen Lake is the second provider. This document is
the single source of truth for four engineers working in parallel (W1 backend core, W2 Buntzen and the fetcher,
W3 frontend, W4 home-cluster). Names, JSON shapes, SQL, paths and signatures here are binding. Where it says
*verbatim*, copy the code exactly; a change to verbatim code is a contract amendment (§9.7).

**Base.** Design against the post-rename tree on branch `wl/foundation` (commits 88ad5dc, 96172b4, b83d597):
`src/waitlist/`, scripts `waitlist-{web,control,agent}`, env `WL_*`, labels `waitlist.chestr.dev/*`, pods
`wl-<16hex>-g<gen>`, namespaces `waitlist`/`waitlist-browsers`, DB `waitlist` with roles
`waitlist_web`/`waitlist_control`, NOTIFY channel `wl_user`, hosts `waitlist.chestr.dev` /
`waitlist-checkout.chestr.dev`. Where this document and the foundation's code disagree on a *name*, the
foundation's code wins. The foundation does not rename the concept "trip"; this design decides where it stays.
Migration 0007 is the foundation's; this design's migration is 0008. The foundation's user-id hash
(`"fw/v1|iss|sub"` in `web/identity.py`) is never changed.

**How it was built.** The security-first draft is the base. Ideas were grafted from the extensible and product
drafts, and every must-fix item from the three reviews is resolved here with one decision. Real owner choices
are listed in §11 with the default the engineers implement.

**What was verified while writing this** (in a scratch copy of the foundation tree, never in the real repos):

- The contract-commit files (§9.0) pass `ruff format --check`, `ruff check`, `mypy` (strict, whole tree),
  `lint-imports` (9 contracts kept, the 4 existing plus the 5 in §2.5), and the full non-browser suite
  (1376 passed, 2 skipped). On the web side, `tsc -b`, `eslint`, `prettier --check` and vitest (259 passed)
  pass with `watch-schema.ts`, its test and the shared fixture.
- Migration 0008 and the §3.2 grants ran on PostgreSQL 16 on top of the real 0001-0007. Every row of §3.3 was
  exercised as the role named, including the compare-and-set upsert of §4.5. The §3.5 cutover flow ran end to
  end: a 0006 database with 0.7.1's grants for `ferrywatch_*`, `SET ROLE waitlist; pg_dump --no-owner
  --no-privileges | psql --single-transaction`, then `migrate` and `grant`. Afterwards every row reads
  `provider = 'bcferries'`, `pg_policy` names only `waitlist_*`, every table is owned by `waitlist`, and no grant
  names `ferrywatch_*`.
- Applying 0008 without the insert-site changes of §3.4 fails exactly 122 existing tests in 5 modules. All of
  them come from the 7 `INSERT INTO trips` sites §3.4 lists.
- After the completeness review (Revision notes, at the end), the amended verbatim pieces (two `base.py`
  docstrings and the check-loop contract's two extra forbidden modules) were re-checked on a copy of that
  scratch tree: `ruff format --check` and `ruff check` on `providers/`, `mypy` on `providers/` and
  `fetchwire.py`, `lint-imports` (9 kept, 0 broken) and `tests/test_contract.py` (6 passed). The 0008 change is
  one SQL comment.

---

## Amendments after the contract

These amend the contract below, and win where they disagree with it. The rest of the document is as it was
when the workstreams started.

### Amendment 1 (owner): nuke and pave

Nobody uses Ferry Watch, so nothing carries over. Waitlist is deployed fresh with an empty `waitlist` database
(the db hook's `waitlist-control migrate` creates the schema, 0001 to 0008), and the same home-cluster change
that adds `kubernetes/apps/waitlist/` deletes `kubernetes/apps/ferry-watch/`. The runbook is home-cluster's
`kubernetes/apps/waitlist/waitlist/README.md`.

- No `pg_dump`/restore of the `ferrywatch` database, no freeze of either app, no smoke-test database to
  replace and no verification SQL against restored rows.
- One home-cluster change instead of PR-A, PR-C and PR-D. No `legacy-redirects.yaml`: `ferry.chestr.dev` and
  `ferry-checkout.chestr.dev` go away with Ferry Watch, and nothing redirects them.
- No `moved` notification. The kind is gone from `web/messages.py` (`EMAIL_KINDS`, `GENERIC_KINDS` and its
  message) and from `tests/test_outbox.py`. Nothing queues it, and waitlist-web's insert policy never allowed it.
- No push continuity. `WAITLIST_VAPID_KEY` is a new key from `waitlist-web keys`, not a copy of Ferry Watch's,
  and no subscription carries over: people sign in at `waitlist.chestr.dev` and turn alerts on there.
- `tests/test_cutover_restore.py` is deleted, with the flow it rehearsed.
- Kept: migration 0007, which lets browser pods be named `wl-` (on a new database it only relaxes 0001's
  check), and the user-id hash `"fw/v1|iss|sub"` in `web/identity.py`.

Superseded: the "What was verified" bullet about the cutover flow; §0's Cutover row; §3.5 in full; the `moved`
kind in §6.5; the legacy redirects in §8.1 and §8.2; §8.4's copied `WAITLIST_VAPID_KEY`; §8.6's PR-C and PR-D
(and "Ferry Watch is untouched" in PR-A); in §8.7, every dump, restore, freeze, redirect and `moved` step
(Phase S step 1's comparison with `ferrywatch`, C1-C9, R1-R2, PR-D's subscription cleanup), while Phase 0's
release steps and the rest of Phase S's smoke tests still apply as the home-cluster runbook restates them; the
tail of §9.6 after the images; §10's `test_cutover_restore.py` row, the `moved` line of the `test_outbox.py`
row, PR-C/PR-D in the W4 criteria and acceptance item 4; and the continuity concerns of §11 Q3 (with an empty
database every user is new, so a different Authelia subject changes nothing) and Q4.

### Amendment 2 (W2): waitlist-fetcher inflates compressed bodies itself, inside the byte cap

§4.4's shared upstream session adds `auto_decompress=False`: aiohttp inflated each read whole before any cap
saw it, so a small gzip body could inflate past the pod's memory limit. `AiohttpTransport` asks for
`Accept-Encoding: gzip, deflate` (unless the provider's client sets its own), and inflates a `gzip`, `x-gzip`
or `deflate` body as it streams in, never past `max_bytes` of output; past that it is `too_large`, as
an uncompressed body is. Any other `Content-Encoding`, or a stream that doesn't inflate whole,
is `site_changed`. The fetcher's own server likewise leaves request bodies as sent and refuses a compressed
request, or one declared over `MAX_REQUEST_BYTES`, before reading it.

### Amendment 3 (end-to-end run): Check now in quiet hours

§7.4 listed the Buntzen card's Check now as enabled for `checking`, `pending` and `error`, which left out
`quiet`. §6.2 and §4.5 have a check lift the owner's quiet hours for 10 minutes, and waitlist-web accepts one
(refusing it only while a backoff or a cooldown would still hold it back), as BC Ferries' card always has. The
card's Check now is now enabled for `quiet` as well.

---

## 0. Decisions at a glance

| Question | Decision |
|---|---|
| Generic noun | **watch**, in the UI, the API and new code. A BC Ferries watch is still a *trip* on its card; a Buntzen watch is a *pass*. |
| "trip" in SQL and the agent protocol | **Stays**: `trips`, `trip_id`, `trip_runtime`, `trip_state`, the protocol's `trip`, `/t/<trip>/g/<gen>`, ticket claims. A `trips` row is a watch on any provider. |
| Provider identity | `trips.provider` column: NOT NULL, **no default**, closed CHECK list, immutable by trigger. Never a key in the spec (older agents refuse unknown keys). |
| Framework | `src/waitlist/providers/`. Providers are pure. HTTP providers implement `HttpProvider` plus a `Fetcher` client. BC Ferries registers only its `ProviderMeta` and keeps its own path. |
| Where HTTP checks run | **Option B'': `waitlist-fetcher`**, a stateless Deployment with no DB credentials, no ServiceAccount token, no secrets, no RBAC. Its egress goes only to `api.yodelpass.com:443` (FQDN), and it refuses non-public addresses. Ingress comes only from waitlist-control, which keeps all scheduling and all writes and calls it over a closed, typed wire. |
| Results | New `check_results` table (control writes, web reads). Scheduling reuses `trip_runtime`, alert memory `alert_memory`; alerts go through `notifications`, history through `events`, and `pg_notify` works as today. |
| Scheduling | Control's `HttpChecks` loop wakes at the exact due, release and reminder instants (tick ≤ 5 s). The first check after a release is at 07:00:02. Then every 30 s for 20 min, every 60 s after, and every 30 min for dates out of season. Identical queries are coalesced across users. Budget 12 requests/min in control, hard cap 30/min in the fetcher. Next-check writes are compare-and-set. |
| Failures | An outage of our own fetcher (or its own rate cap) charges no watch: it retries in 30 s, and admins are alerted after 3 min. Provider-side errors count: a user alert after 5, and backoff after 3 only when *every* query of the watch failed. Evaluation is per query, so a failing half-day query never blocks an all-day alert. A site's 429 or 401/403 cools the whole provider down. |
| Buntzen | Alert-only. Watch = one date + any of all-day/morning/afternoon + `release_reminder` (default on, 06:55 the day before). Checkable from 07:00 Pacific the day before until each pass's stop time (morning 12:00, afternoon 18:00, all-day 18:00). Auto-archived 12 h after the window closes. |
| Unknown statuses | `maybe`: alert once, worded "may be open". "Sold out" with no period name (the Home Assistant quirk) is sold out, not an error. |
| Rollout | Per-provider `offer: off/admins/everyone` (the `streamViewer` pattern). Buntzen ships `admins` until a real capture validates the parser. Admins can pause a provider at runtime with no deploy. |
| Web API | New `/api/watches` (CRUD, `check`, `done`) and `GET /api/providers`. BC Ferries actions stay on `/api/trips/{id}/hold|cart/*|vnc|booked`. The old `/api/trips` CRUD stays as a BC Ferries-only alias for 1.0, and the SPA never calls it. |
| Push | Same-origin only (`sw.js` unchanged). A push opens `/w/<id>`, whose first element is **Book on Yodel**. The email carries the direct Yodel link. |
| Frontend | One dashboard grouped *Needs you now / Watching / Paused / Ended*. "New watch" with a provider picker; per-provider card, form and detail page; a client registry; a zod union that degrades an unknown watch instead of blanking the page; an error boundary per card. |
| Cluster | New `kubernetes/apps/waitlist/` with `waitlist` and `waitlist-browsers`, DB `waitlist`, new roles, OIDC client, lldap group and 1Password item. The fetcher runs in the same HelmRelease. |
| Cutover | Waitlist is deployed beside Ferry Watch and smoke-tested first. Then a freeze, an in-pod `pg_dump \| psql` (no PII leaves the pod), verification SQL, and redirect routes created *before* the old routes are deleted (no DNS gap). `ferry.chestr.dev` 302s to `waitlist.chestr.dev`, made a 301 at decommission. The old app and DB stay as the rollback for ≥ 14 days. |
| Release | Waitlist **1.0.0**. Images `ghcr.io/mchestr/waitlist-{control,browser,vnc-edge}:1.0.0`, made public before the first deploy. |
| Work | W1 core (DB, check loop, web API, outbox, admin). W2 Buntzen provider **and the fetcher service**. W3 frontend. W4 home-cluster and the app's deploy copy. Merge W2 → W1 → W3 → W4 after one contract commit. |

---

## 1. Concepts and vocabulary

| Term | Meaning | Where it lives |
|---|---|---|
| **provider** | A reservation system: `bcferries`, `buntzen`. Id matches `^[a-z][a-z0-9_]{1,31}$`. | `trips.provider`, `waitlist.providers.REGISTRY`, the catalog |
| **watch** | One thing a user wants on one provider: a sailing on a date, a pass on a date. One `trips` row. | `trips` (id `^[a-f0-9]{16}$`, from `secrets.token_hex(8)` as today) |
| **spec** | A watch's provider-specific settings, validated by the provider. Unknown keys refused. | `trips.spec` jsonb; API `spec` |
| **checker** | How a provider's watches are checked: `"browser"` (pods; BC Ferries) or `"http"` (fetcher; Buntzen). | `ProviderMeta.checker` |
| **query** | One upstream request's inputs, a frozen msgspec Struct, e.g. `{place_id: 10672, date: "2027-07-01"}`. Hashable, so identical queries coalesce. | `HttpProvider.queries()`; fetch wire |
| **inventory** | What the fetcher parsed from one response: a bounded msgspec Struct with a closed vocabulary. | fetch wire, in memory only |
| **option** | One bookable thing as the last check saw it: key, label, status, wanted, booking link. | `check_results.options`; API `options[]` |
| **opening** | A wanted option becoming `open` or `maybe` that wasn't alerted yet. One `opening` notification. | `alert_memory.alerted` (option keys) |
| **check state** | Where an HTTP watch's checking is (`waiting_release`, `checking`, `over`...): `base.CheckState`. | computed; API `check.state` |
| **capability** | `auto_hold` (carts), `checkout` (viewer), `alert_only`. | `ProviderMeta.capabilities`; catalog |
| **alert-only** | The provider only alerts. The user books on the provider's site with their own account. | Buntzen |
| **offer** | Who may create watches of a provider: `off`, `admins`, `everyone` (web config). | `WebConfig.providers.<id>.offer`; catalog `offered` |

### Where "trip" stays and where "watch" is used

| Layer | Word | Why |
|---|---|---|
| SQL tables and columns (`trips`, `trip_id`, `trip_runtime`, `trip_state`, the FKs, RLS) | **trip** | A rename would rewrite every SQL string on the BC Ferries path, every FK, the grants and policies, and add risk to the dump/restore, for no user value. `COMMENT ON TABLE trips` says what it holds. |
| Agent ↔ control protocol (`trip`, `TripSpec`), pod labels `waitlist.chestr.dev/trip`, vnc-edge `/t/<trip>/g/<gen>`, tickets' `trip` claim | **trip** | Wire stability: agents N-1, the admission policy, vnc-edge's regex. The protocol is **frozen** (golden test, §10). |
| `waitlist.models.Trip` | **Trip** | It *is* the BC Ferries trip; the agent imports it. |
| New Python (`providers/`, `fetcher/`, `control/check*.py`, `web/watches.py`) | **watch** (`watch_id` variables; the column is still `trip_id`) | Readability |
| HTTP API | **watch** (`/api/watches`, state `watches[]`). BC Ferries actions stay on `/api/trips/{id}/…` | The generic surface is new; BC Ferries' is unchanged. |
| `events[].trip_id`, `notifications.trip_id` | **trip_id** | Rows and JSON keys as today |
| UI | "Your watches", "New watch". A BC Ferries card and form say *trip*; Buntzen says *pass*. | `ProviderMeta.noun` |

### What stays BC Ferries-only

The agent and browser pods, the agent protocol, the planner, fleet, pods, kube client and hub, the hold clocks
(`holds.py`), carts and cart claims (`hold_claims`), VNC and streamed checkout (`tickets.py`, `vnc_mints`,
vnc-edge, `checkout.streamViewer`), `hold_outcomes` and "Did you book it?", "Hold it for me", the terminal list
and route map, the brake and breaker, `trip_state`, the limits `maxTrips`, `maxActiveBrowsers`,
`maxHoldsPerDay`, `maxCartAttemptsPerDay`, `vncMinutesPerDay` and `mintsPerMinute`, the alert kinds
`bookable`, `cart_*`, `hold_*`, `booked*`, `release_unverified`, `checkout_blocked` (and BC Ferries' own
`failing`), every BC Ferries message text, and waitlist-web's egress to `www.bcferries.com`.

Generic, shared by every provider: users, invites, OIDC, sessions, the limits `maxAlertOnlyWatches` (new),
`manualChecksPerHour` and `testAlertsPerHour`, quiet hours, the `maintenance` switch, `trip_runtime`'s
scheduling columns, `alert_memory`, `events`, `notifications` and the outbox, Web Push and email, the live
socket, admin pages and the audit log.

### Invariants this design keeps, and two it adds

- **I1** waitlist-web has no Kubernetes access and never writes what a checker reports. Grants enforce it: web
  has no write privilege on `check_results`, `trip_state` or `alert_memory` beyond 0.7.1's DELETE.
- **I2** waitlist-control has no internet egress. Its only new egress is the fetcher's Service port.
- **I3** Only BC Ferries trips ever get a browser pod, a cart claim or a checkout ticket (DB triggers, new).
- **I4** No third-party credentials are stored. Buntzen is alert-only.
- **I5** Every trust boundary has a closed, typed, bounded vocabulary: the agent protocol, and the fetch wire
  (new).
- **I6** Untrusted site content is parsed only in low-privilege sandboxes: browser pods for BC Ferries, the
  fetcher for HTTP providers.
- **I7** Each process holds only its own secrets. The fetcher holds none.
- **I8** A push opens only a same-origin page (`sw.js` unchanged). An email's second link is a booking constant
  within the provider's `booking_prefixes`. No link ever comes from a site's response or a user's input, and no
  site text ever enters a notification.
- **I9** Sign-in stays invite-only (a new OIDC client and lldap group, same rules).
- **I10 (new)** waitlist-control re-validates every stored spec with the provider's `load_spec` before using
  it, as agents re-validate `Trip`. The fetcher re-validates every query with the provider's Struct type.
- **I11 (new)** Our own outage never counts against a user's watch. `fetcher_unavailable` (which includes the
  fetcher's own rate cap) and `bad_reply` change no per-watch counter, cause no provider cooldown and send no
  user alert.

---

## 2. Provider framework

### 2.1 Package layout (post-rename paths)

```
src/waitlist/
├── providers/
│   ├── __init__.py        # REGISTRY, PROVIDER_IDS                      verbatim, contract commit
│   ├── base.py            # the interface                               verbatim, contract commit
│   ├── bcferries.py       # BC Ferries' ProviderMeta only               verbatim, contract commit
│   └── buntzen/
│       ├── __init__.py    # PROVIDER (HttpProvider)                     stub in contract commit; W2 replaces
│       ├── meta.py        # META, BUDGET, HOSTS, pages                  verbatim, contract commit; nobody edits
│       ├── spec.py        # BuntzenSpec, parse/load/dump                W2
│       ├── model.py       # BuntzenQuery, PassItem, BuntzenInventory    W2
│       ├── policy.py      # window, interval, seasons, evaluate, links  W2
│       ├── messages.py    # render                                      W2
│       └── client.py      # build_request, parse_catalog, FETCHER       stub in contract commit; W2 replaces
├── fetchwire.py           # control <-> fetcher wire, v1                verbatim, contract commit
├── fetcher/               # the waitlist-fetcher service                W2
│   ├── __init__.py        #                                             stub in contract commit; W2 replaces
│   ├── main.py            # entry point `waitlist-fetcher run`          stub in contract commit; W2 replaces
│   ├── config.py  transport.py  registry.py  app.py                     W2
└── control/
    ├── checkplan.py       # pure: check_state, plan_checks              stub in contract commit; W1 replaces
    ├── checks.py          # the HTTP check loop (HttpChecks)            stub in contract commit; W1 replaces
    └── fetch.py           # control's client for the fetcher            stub in contract commit; W1 replaces
```

`pyproject.toml` gains the script `waitlist-fetcher = "waitlist.fetcher.main:main"` and the §2.5 contracts in
the contract commit. No workstream edits `pyproject.toml` or `uv.lock` after that, and none adds a dependency.
The one later edit is the orchestrator's 1.0.0 version bump of both files at release time (§8.7 Phase 0 step 2,
§9.6).

### 2.2 Rules every provider follows

1. **Pure.** No sockets, files, database, aiohttp, clock reads (`now` is passed in) or randomness. Enforced by
   an import-linter contract (§2.5).
2. **The client is separate.** `<provider>/client.py` builds requests and parses responses; only
   `waitlist.fetcher` imports it (a contract). `<provider>/__init__.py` never imports `.client`, because control
   and web import `waitlist.providers` and import-linter follows transitive imports.
3. **I/O goes through `Transport`**, which the fetcher supplies: https:443 to the client's declared `hosts`
   only, public addresses only, no redirects, no cookies, a capped body read to its end. A buggy or compromised
   client can't reach anything else.
4. **Closed vocabularies.** Queries and inventories are msgspec Structs with `forbid_unknown_fields=True`,
   `Literal` enums and bounded lengths. The only site text that crosses to control is a sanitised ≤ 32-character
   status (`PassItem.status` → `Option.detail`). It is shown escaped on the card and never placed in a
   notification.
5. **Links are constants.** `booking_url` and `Notice.also` come from the provider's code, inside
   `meta.booking_prefixes`, never from a response.
6. **Specs are validated twice.** `parse_spec` runs on input in web; `load_spec` runs everywhere a stored spec
   is used (I10).
7. **Every `now` and `at` a provider receives is timezone-aware.** Control and web keep epoch floats and pass
   `datetime.fromtimestamp(t, UTC)`. A provider converts with `.astimezone(PACIFIC)` and never builds or
   compares a naive datetime. W1's `FakeHttpProvider` asserts `now.tzinfo is not None` in every method that
   takes one, so a naive caller fails in W1's own tests, not after the rebase onto W2.

### 2.3 The shared interface (verbatim)

`src/waitlist/providers/base.py`:

```python
"""What every Waitlist provider is (docs/waitlist/design.md §2.3). Written verbatim from the design contract.

A *provider* is a reservation system Waitlist can watch: BC Ferries, Buntzen Lake. A *watch* (a `trips` row) is
one thing a user wants on one provider: a sailing on a date, a parking pass on a date. Each provider's *checker*
says how its watches are checked:

- "browser": a disposable browser pod per watch, driven by the agent (BC Ferries only). Its scheduling, results,
  carts and checkout are the existing BC Ferries path; it registers only its `ProviderMeta` here.
- "http": waitlist-control decides when a watch is due and asks waitlist-fetcher (no database, no secrets, no
  Kubernetes) to make the provider's request. The fetcher parses the response into the provider's typed
  *inventory*; control evaluates it into *options* and alerts. Everything below is that contract.

This module is pure: no I/O, no aiohttp, no database. A provider's client (its request builder and response
parser) implements `Fetcher` against `Transport`, which the fetcher supplies, so a provider never opens a socket
itself and can only reach the hosts it declares.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, TypeVar

import msgspec

from waitlist.models import ValidationError

# ---- vocabulary

PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
ProviderId = Annotated[str, msgspec.Meta(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
OptionKey = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,31}$")]
Label = Annotated[str, msgspec.Meta(min_length=1, max_length=64)]
# Short, plain text. Never HTML, never a URL; React escapes it anyway. Never placed in a notification.
Detail = Annotated[str, msgspec.Meta(max_length=64, pattern=r"^[A-Za-z0-9 .,:;'()/&+-]*$")]
HttpsUrl = Annotated[str, msgspec.Meta(max_length=300, pattern=r"^https://[a-z0-9.-]+/[A-Za-z0-9._~%/+-]*$")]

Checker = Literal["browser", "http"]
Urgency = Literal["very-low", "low", "normal", "high"]  # Web Push's, as waitlist.web.push has it

# What one option (a pass, a sailing time...) looks like at the last check.
OptionStatus = Literal[
    "open",  # bookable now: alert
    "maybe",  # the site said something we don't recognise that isn't "sold out": alert, worded as "may be"
    "sold_out",
    "not_released",  # not on sale yet (before the release time, or the site says "coming soon")
    "not_offered",  # the site offers no such option for this date (out of season, say)
    "over",  # too late to be useful (the pass period has ended)
    "unchecked",  # no result yet
]
ALERTABLE: frozenset[str] = frozenset({"open", "maybe"})

# Where an HTTP-checked watch's checking is (waitlist.control.checkplan.check_state; the SPA shows it).
CheckState = Literal[
    "pending",  # checkable, never checked yet
    "waiting_release",  # before window.opens_at
    "checking",
    "error",  # checked, but the last BACKOFF_AFTER or more checks failed
    "backoff",  # failing, or the provider is cooling down after a 429/403: next check later
    "quiet",  # in the owner's quiet hours, not lifted
    "paused",  # the watch is paused, or its owner suspended
    "provider_paused",  # maintenance, or an admin paused the provider
    "unavailable",  # checks for the provider aren't running (no fresh check report, or waitlist-fetcher is down)
    "over",  # after window.closes_at
]
# What the last check of a watch found (check_results.outcome).
CheckOutcome = Literal["pending", "ok", "no_inventory", "error"]

# Why a fetch failed. Closed on purpose: nothing a site says crosses to control as text.
FetchErrorKind = Literal[
    "site_changed",  # a response we can't read: a new shape, HTML where JSON was, a 4xx meaning the API moved
    "rate_limited",  # 429, or the fetcher's own per-provider cap
    "blocked",  # 401/403: a WAF or geo-block
    "upstream_error",  # 5xx
    "timeout",
    "network",  # includes a host that resolved to a non-public address
    "too_large",
    "unexpected",
]
# A check's error as control records it: the fetch's, or control's own trouble reaching the fetcher.
CheckErrorKind = Literal[
    "site_changed",
    "rate_limited",
    "blocked",
    "upstream_error",
    "timeout",
    "network",
    "too_large",
    "unexpected",
    "fetcher_unavailable",  # ours: waitlist-fetcher didn't answer
    "bad_reply",  # ours: waitlist-fetcher answered with something that isn't a valid reply
]

# Notification kinds control queues for http providers, rendered by the watch's provider (params as control
# writes them; the provider re-checks them):
OPENING = "opening"  # {"options": [OptionKey, ...], "uncertain": [OptionKey, ...]}; uncertain ⊆ options, ≤ 10 each
RELEASE_REMINDER = "release_reminder"  # {}
FAILING = "failing"  # {"error_class": CheckErrorKind}
NOTICE_KINDS = frozenset({OPENING, RELEASE_REMINDER, FAILING})
# Admin-only kinds control queues, rendered by waitlist-web itself (never by a provider):
SITE_CHANGED = "site_changed"  # {"provider": ProviderId}
CHECKER_DOWN = "checker_down"  # {"provider": ProviderId, "since": epoch seconds}
ADMIN_NOTICE_KINDS = frozenset({SITE_CHANGED, CHECKER_DOWN})


class SpecError(ValidationError):
    """A watch spec that can't be accepted. `field` names the spec key; waitlist-web answers 400 with it."""


class FetchError(Exception):
    """A fetch that didn't produce an inventory. Raised by a Transport or a Fetcher; crosses the wire as `kind`."""

    def __init__(self, kind: FetchErrorKind, *, http_status: int | None = None, retry_after_s: int | None = None):
        super().__init__(kind)
        self.kind: FetchErrorKind = kind
        self.http_status = http_status
        self.retry_after_s = retry_after_s


# ---- what a provider is


@dataclass(frozen=True, slots=True)
class Capabilities:
    auto_hold: bool  # can hold a booking in a cart for the user (BC Ferries' carts)
    checkout: bool  # has a checkout viewer (VNC or the streamed viewer)
    alert_only: bool  # only alerts; the user books on the provider's site

    def __post_init__(self) -> None:
        if self.alert_only and (self.auto_hold or self.checkout):
            raise ValueError("An alert-only provider can't hold or check out")

    def to_json(self) -> dict[str, bool]:
        return {"auto_hold": self.auto_hold, "checkout": self.checkout, "alert_only": self.alert_only}


@dataclass(frozen=True, slots=True)
class ProviderMeta:
    """What the picker, the catalog and the limits need to know about a provider."""

    id: str  # "bcferries", "buntzen": trips.provider
    name: str  # "Buntzen Lake"
    noun: str  # what one watch is on its card: "trip", "pass"
    description: str  # one line, for the provider picker
    checker: Checker
    capabilities: Capabilities
    limit_key: str  # the waitlist.control.limits.LimitKey that caps this provider's watches per user
    booking_prefixes: tuple[str, ...]  # every booking link this provider makes starts with one of these
    horizon_days: int = 30  # how far ahead a watch's date may be

    def __post_init__(self) -> None:
        if not PROVIDER_ID.fullmatch(self.id):
            raise ValueError(f"Not a provider id: {self.id!r}")
        if not self.booking_prefixes:
            raise ValueError("A provider needs at least one booking prefix")
        for prefix in self.booking_prefixes:
            if not re.fullmatch(r"https://[a-z0-9.-]+/([A-Za-z0-9._~%+-]+/)*", prefix):
                raise ValueError(f"A booking prefix must be an https origin and a path ending in /: {prefix!r}")
        if not 1 <= self.horizon_days <= 120:
            raise ValueError("horizon_days must be from 1 to 120")

    def allows_booking_url(self, url: str) -> bool:
        return any(url.startswith(prefix) for prefix in self.booking_prefixes)

    def to_json(self) -> dict[str, Any]:
        """The catalog entry (GET /api/providers); waitlist-web adds `offered` and `paused`."""
        return {
            "id": self.id,
            "name": self.name,
            "noun": self.noun,
            "description": self.description,
            "checker": self.checker,
            "capabilities": self.capabilities.to_json(),
            "horizon_days": self.horizon_days,
        }


@dataclass(frozen=True, slots=True)
class Budget:
    """How hard control may lean on a provider's site, in requests (distinct queries) a minute."""

    default_per_minute: int
    max_per_minute: int  # the ceiling a deployment's config can't pass
    concurrency: int = 2  # fetches in flight at once

    def __post_init__(self) -> None:
        if not 1 <= self.default_per_minute <= self.max_per_minute <= 60 or not 1 <= self.concurrency <= 4:
            raise ValueError("A budget is 1-60 a minute (default within the ceiling) and 1-4 at once")


@dataclass(frozen=True, slots=True)
class CheckWindow:
    """When checking a watch is useful: from `opens_at` (e.g. the release time) until `closes_at`."""

    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        if self.opens_at.tzinfo is None or self.closes_at.tzinfo is None:
            raise ValueError("A check window's times must be timezone-aware")
        if self.closes_at <= self.opens_at:
            raise ValueError("A check window must close after it opens")

    def phase(self, now: datetime) -> Literal["before", "open", "after"]:
        if now < self.opens_at:
            return "before"
        return "open" if now < self.closes_at else "after"


class Option(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    """One bookable thing as the last check saw it. Stored in check_results.options and shown as is."""

    key: OptionKey  # stable across checks: what alert memory remembers ("morning", "08:30")
    label: Label  # "Morning pass": the provider's own words, never the site's
    status: OptionStatus
    wanted: bool  # the user asked for it (only wanted options alert)
    booking_url: HttpsUrl | None = None  # a constant of the provider's, never from a response
    detail: Detail | None = None  # e.g. the site's unrecognised status, sanitised; shown on the card only


MAX_OPTIONS = 20


@dataclass(frozen=True, slots=True)
class Notice:
    """What a notification says. The outbox turns it into a push and, for some kinds, an email."""

    title: str  # at most 120 characters
    body: str  # at most 300 characters
    url: str  # what a push opens: always the watch's own page on the site (`watch_url`)
    urgency: Urgency = "normal"
    ttl: int = 3600  # seconds a push service keeps it for an offline device
    topic: str | None = None  # replaces an earlier push with the same topic; [A-Za-z0-9_-]{1,32}
    also: tuple[str, str] | None = None  # (label, url) for email only: a booking link, within booking_prefixes

    def __post_init__(self) -> None:
        if not self.title or len(self.title) > 120 or len(self.body) > 300:
            raise ValueError("A notice's title is 1-120 characters and its body at most 300")
        if self.topic is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.topic):
            raise ValueError("A topic is 1-32 letters, digits, - or _")


# ---- the HTTP checker

SpecT = TypeVar("SpecT")
QueryT = TypeVar("QueryT", bound=msgspec.Struct)
InventoryT = TypeVar("InventoryT", bound=msgspec.Struct)
QueryT_contra = TypeVar("QueryT_contra", bound=msgspec.Struct, contravariant=True)
InventoryT_co = TypeVar("InventoryT_co", bound=msgspec.Struct, covariant=True)


class HttpProvider(Protocol[SpecT, QueryT, InventoryT]):
    """A provider checked over HTTP. Pure: imported by waitlist-web, waitlist-control and waitlist-fetcher alike.

    `SpecT` is the provider's own watch spec (frozen). `QueryT` is one upstream request's inputs: a frozen
    msgspec Struct (hashable, so control can coalesce identical queries across watches and users).
    `InventoryT` is what the fetcher parsed from one response: a msgspec Struct, bounded, closed vocabulary.

    Every `now` passed in is timezone-aware (callers pass `datetime.fromtimestamp(t, UTC)`); convert it with
    `.astimezone(PACIFIC)`, and never compare it with a naive datetime.
    """

    @property
    def meta(self) -> ProviderMeta: ...
    @property
    def budget(self) -> Budget: ...
    @property
    def query_type(self) -> type[QueryT]: ...
    @property
    def inventory_type(self) -> type[InventoryT]: ...

    # -- the watch

    def parse_spec(self, data: Mapping[str, Any], *, base: SpecT | None, now: datetime) -> SpecT:
        """A spec from the user (POST/PATCH /api/watches), every field validated, unknown keys refused.

        `base`: a partial update on top of the current spec. `now`: for the date rules (no past dates, within
        the horizon, not already over). Raises SpecError(field, message). Never sees `id` or `enabled`.
        """
        ...

    def load_spec(self, stored: Mapping[str, Any]) -> SpecT:
        """A spec as stored in trips.spec (dump_spec's output), validated again but without the date rules,
        so yesterday's watch still loads. Raises SpecError."""
        ...

    def dump_spec(self, spec: SpecT) -> dict[str, Any]:
        """The JSON object stored in trips.spec and sent as the watch's `spec`. load_spec(dump_spec(s)) == s."""
        ...

    def search_key(self, spec: SpecT) -> tuple[object, ...]:
        """What defines the search: an edit that changes it forgets what was alerted (alert memory) and hides the
        last result (search_hash). Parts are str, int, float, bool, None or dates."""
        ...

    def watch_date(self, spec: SpecT) -> date:
        """The date the user wants (Pacific): the dashboard sorts by it."""
        ...

    def title(self, spec: SpecT) -> str:
        """Plain words for admin pages and logs, e.g. "Buntzen Lake, Thu Jul 1: morning pass"."""
        ...

    # -- scheduling (control's check loop)

    def window(self, spec: SpecT) -> CheckWindow:
        """When checks are useful. Control checks nothing before `opens_at` and stops at `closes_at`."""
        ...

    def interval(self, spec: SpecT, now: datetime) -> timedelta:
        """The poll curve: how long after a check at `now` the next is due. Control never goes below 30 s,
        adds ±10 % jitter, and stretches it to stay within the budget."""
        ...

    def reminder_at(self, spec: SpecT) -> datetime | None:
        """When to send RELEASE_REMINDER for this watch (once), or None."""
        ...

    # -- checking

    def queries(self, spec: SpecT) -> tuple[QueryT, ...]:
        """The upstream requests this watch needs, 1-4 of them, deduplicated."""
        ...

    def evaluate(self, spec: SpecT, inventories: Mapping[QueryT, InventoryT], now: datetime) -> tuple[Option, ...]:
        """The watch's options from what was fetched. Total: an option whose query has no inventory here is
        "unchecked" (or "not_released"/"over" by time); control reads "unchecked" as "not seen this round" and
        keeps its alert memory. Wanted options first; at most MAX_OPTIONS."""
        ...

    def booking_url(self, spec: SpecT, options: Sequence[Option]) -> str | None:
        """Where to book it yourself, within meta.booking_prefixes: the first open option's link, else the
        watch's default page."""
        ...

    # -- what users read

    def render(self, kind: str, params: Mapping[str, Any], spec: SpecT, *, watch_url: str) -> Notice | None:
        """The notice for one notification of NOTICE_KINDS (params as control writes them, re-checked here), or
        None for a kind with nothing to say to the user. Every link is `watch_url` or a booking constant."""
        ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What a Transport hands back: the status, the media type (lowercased, no parameters) and at most the
    transport's byte limit of body. Redirects are never followed; a 3xx arrives as itself."""

    status: int
    content_type: str
    body: bytes
    retry_after_s: int | None = None  # a 429's or 503's Retry-After in seconds, if it gave one (0-3600)


class Transport(Protocol):
    """The fetcher's HTTP client, as a provider sees it. It refuses (FetchError "unexpected") any URL that isn't
    https on port 443 at one of the fetcher's declared hosts, connects only to public addresses (FetchError
    "network" otherwise), follows no redirects, sends no cookies, reads the whole body up to its byte limit
    (FetchError "too_large" past it), enforces its timeout (FetchError "timeout"), and never logs bodies."""

    async def post_json(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str]) -> HttpResponse: ...

    async def get(self, url: str, *, headers: Mapping[str, str]) -> HttpResponse: ...


class Fetcher(Protocol[QueryT_contra, InventoryT_co]):
    """A provider's client: builds the request for a query, makes it through the transport, and parses the
    response defensively into an inventory. Imported only by waitlist-fetcher (an import-linter contract)."""

    @property
    def provider_id(self) -> str: ...
    @property
    def hosts(self) -> tuple[str, ...]: ...  # the only hosts its requests go to (the fetcher's egress allowlist)

    async def fetch(self, transport: Transport, query: QueryT_contra) -> InventoryT_co:
        """One query's inventory, or FetchError. A response it can't read is "site_changed", never an empty
        inventory: "nothing open" must only ever mean the site said so."""
        ...


def search_hash(key: tuple[object, ...]) -> str:
    """A short, stable digest of a search_key (check_results.search_hash), so a result made for an earlier search
    is never shown for the current one. A search_key's parts are str, int, float, bool, None or dates: never sets,
    whose order isn't stable between processes."""
    text = json.dumps(list(key), default=str, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def check_status(response: HttpResponse) -> None:
    """The same reading of HTTP statuses for every provider. Returns for 200; raises FetchError otherwise."""
    status = response.status
    if status == 200:
        return
    if status == 429:
        raise FetchError("rate_limited", http_status=status, retry_after_s=response.retry_after_s)
    if status in (401, 403):
        raise FetchError("blocked", http_status=status)
    if status in (400, 404, 405, 406, 410, 415, 422, 426) or 300 <= status < 400:
        raise FetchError("site_changed", http_status=status)
    if status == 503:
        raise FetchError("upstream_error", http_status=status, retry_after_s=response.retry_after_s)
    if status >= 500:
        raise FetchError("upstream_error", http_status=status)
    raise FetchError("unexpected", http_status=status)
```

`src/waitlist/providers/__init__.py`:

```python
"""The providers Waitlist can watch, and the one registry everything looks them up in (design.md §2.3).

Written verbatim from the design contract. Adding a provider is: its package under providers/, a line here, its
id in migration CHECK trips_provider_check (an expand-only migration), its client in waitlist.fetcher.registry,
and its card and form in web/src/providers/.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import HttpProvider, ProviderMeta
from .bcferries import META as BCFERRIES
from .buntzen import PROVIDER as BUNTZEN

__all__ = ["PROVIDER_IDS", "REGISTRY", "Registry"]


@dataclass(frozen=True, slots=True)
class Registry:
    """Every provider's meta, in picker order, and the HTTP-checked ones' implementations."""

    metas: tuple[ProviderMeta, ...]
    http: Mapping[str, HttpProvider[Any, Any, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [m.id for m in self.metas]
        if len(set(ids)) != len(ids):
            raise ValueError("Provider ids must be unique")
        for provider_id, provider in self.http.items():
            if provider.meta.id != provider_id or provider.meta not in self.metas:
                raise ValueError(f"{provider_id}: an http provider must be registered under its own meta")
            if provider.meta.checker != "http":
                raise ValueError(f"{provider_id}: only http providers have an implementation here")
        for meta in self.metas:
            if meta.checker == "http" and meta.id not in self.http:
                raise ValueError(f"{meta.id}: an http provider needs its implementation")

    @classmethod
    def of(cls, *, browser: Iterable[ProviderMeta], http: Iterable[HttpProvider[Any, Any, Any]]) -> Registry:
        providers = list(http)
        return cls(metas=(*browser, *(p.meta for p in providers)), http={p.meta.id: p for p in providers})

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(m.id for m in self.metas)

    def meta(self, provider_id: str) -> ProviderMeta | None:
        return next((m for m in self.metas if m.id == provider_id), None)

    def http_provider(self, provider_id: str) -> HttpProvider[Any, Any, Any] | None:
        return self.http.get(provider_id)


REGISTRY = Registry.of(browser=[BCFERRIES], http=[BUNTZEN])
# Exactly the ids migration 0008's CHECK (trips_provider_check) allows; a test holds the two together.
PROVIDER_IDS = REGISTRY.ids
```

`src/waitlist/providers/bcferries.py`:

```python
"""BC Ferries as a provider: its catalog entry only (design.md §2.4). Written verbatim from the design contract.

BC Ferries is the "browser" checker: its trips are watched by browser pods (waitlist.agent), scheduled by the fleet
(waitlist.control.planner, .fleet), and stored, alerted and shown by the existing code paths, which this module
doesn't change. A BC Ferries watch's spec is waitlist.models.Trip.to_json(), as before.
"""

from __future__ import annotations

from .base import Capabilities, ProviderMeta

ID = "bcferries"

META = ProviderMeta(
    id=ID,
    name="BC Ferries",
    noun="trip",
    description="Sailing reservations. Can hold a sailing in a cart for you to check out.",
    checker="browser",
    capabilities=Capabilities(auto_hold=True, checkout=True, alert_only=False),
    limit_key="maxTrips",
    booking_prefixes=("https://www.bcferries.com/",),
    horizon_days=30,
)
```

`src/waitlist/providers/buntzen/meta.py` (nobody changes it without an amendment):

```python
"""Buntzen Lake's fixed facts (design.md §5.1). Written verbatim from the design contract; W2 doesn't change it."""

from __future__ import annotations

from waitlist.providers.base import Budget, Capabilities, ProviderMeta

ID = "buntzen"

META = ProviderMeta(
    id=ID,
    name="Buntzen Lake",
    noun="pass",
    description="BC Hydro day-use parking passes, sold on Yodel. Alerts only: you book on Yodel.",
    checker="http",
    capabilities=Capabilities(auto_hold=False, checkout=False, alert_only=True),
    limit_key="maxAlertOnlyWatches",
    booking_prefixes=("https://yodelportal.com/buntzen-lake/",),
    horizon_days=30,
)

# Distinct queries a minute across every user's Buntzen watches (the owner's Home Assistant made 1-4).
BUDGET = Budget(default_per_minute=12, max_per_minute=30, concurrency=2)

# The only host waitlist-fetcher may call for Buntzen; home-cluster's fetcher egress policy lists the same.
API_HOST = "api.yodelpass.com"
HOSTS = (API_HOST,)

FULL_DAY_PAGE = "https://yodelportal.com/buntzen-lake/All-Day-Pass"
HALF_DAY_PAGE = "https://yodelportal.com/buntzen-lake/Half-Day-Pass"
```

**Contract-commit stubs.** They exist so that the registry, import-linter and every worktree's tests work from
the start. Each stub belongs to exactly one workstream, which replaces it.

`src/waitlist/providers/buntzen/__init__.py` (W2 replaces):

```python
"""Buntzen Lake: STUB from the contract commit. W2 replaces this file with the real provider (design.md §5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import msgspec

from waitlist.providers.base import Budget, CheckWindow, HttpProvider, Notice, Option, ProviderMeta

from .meta import BUDGET, META


class _Query(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class _Inventory(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class _NotYet:
    meta: ProviderMeta = META
    budget: Budget = BUDGET
    query_type: type[_Query] = _Query
    inventory_type: type[_Inventory] = _Inventory

    def parse_spec(self, data: Mapping[str, Any], *, base: object | None, now: datetime) -> object:
        raise NotImplementedError

    def load_spec(self, stored: Mapping[str, Any]) -> object:
        raise NotImplementedError

    def dump_spec(self, spec: object) -> dict[str, Any]:
        raise NotImplementedError

    def search_key(self, spec: object) -> tuple[object, ...]:
        raise NotImplementedError

    def watch_date(self, spec: object) -> date:
        raise NotImplementedError

    def title(self, spec: object) -> str:
        raise NotImplementedError

    def window(self, spec: object) -> CheckWindow:
        raise NotImplementedError

    def interval(self, spec: object, now: datetime) -> timedelta:
        raise NotImplementedError

    def reminder_at(self, spec: object) -> datetime | None:
        raise NotImplementedError

    def queries(self, spec: object) -> tuple[_Query, ...]:
        raise NotImplementedError

    def evaluate(self, spec: object, inventories: Mapping[_Query, _Inventory], now: datetime) -> tuple[Option, ...]:
        raise NotImplementedError

    def booking_url(self, spec: object, options: Sequence[Option]) -> str | None:
        raise NotImplementedError

    def render(self, kind: str, params: Mapping[str, Any], spec: object, *, watch_url: str) -> Notice | None:
        raise NotImplementedError


PROVIDER: HttpProvider[Any, Any, Any] = _NotYet()
```

`src/waitlist/providers/buntzen/client.py` (W2 replaces):

```python
"""Buntzen Lake's client: STUB from the contract commit. W2 replaces this file (design.md §5.3-5.4)."""

from __future__ import annotations

from typing import Any

import msgspec

from waitlist.providers.base import Fetcher, Transport

from .meta import HOSTS, ID


class _NotYet:
    provider_id: str = ID
    hosts: tuple[str, ...] = HOSTS

    async def fetch(self, transport: Transport, query: msgspec.Struct) -> msgspec.Struct:
        raise NotImplementedError


FETCHER: Fetcher[Any, Any] = _NotYet()
```

`src/waitlist/fetcher/__init__.py` (W2 replaces):

```python
"""waitlist-fetcher: STUB from the contract commit. W2 builds the package (design.md §4.4)."""
```

`src/waitlist/fetcher/main.py` (W2 replaces):

```python
"""waitlist-fetcher's entry point: STUB from the contract commit. W2 replaces it (design.md §4.4)."""

from __future__ import annotations

import sys


def main() -> None:
    sys.exit("waitlist-fetcher: not built yet")
```

`src/waitlist/control/checkplan.py`, `checks.py`, `fetch.py` (W1 replaces each). One line, with the module
name filled in:

```python
"""STUB from the contract commit: W1 builds waitlist.control.checkplan (design.md §4.5)."""
```

### 2.4 How BC Ferries is adapted without changing its behaviour

BC Ferries registers its `ProviderMeta` (catalog, picker, limit key) and nothing else. It does **not**
implement `HttpProvider`. Its CRUD, snapshot entry, alerts, carts, checkout and scheduling stay on the existing
code paths. The only edits to that path are **provider filters**, so that no other provider's row can ever reach
BC Ferries code that would choke on it. Today a non-BC Ferries spec returns 500 for the whole dashboard and makes
the fleet log an exception every 10 s. Every edit is W1's.

| File | Edit |
|---|---|
| `control/fleet.py` `Fleet._inputs` | The trips query's WHERE becomes exactly `WHERE t.provider = 'bcferries' AND (t.archived_at IS NULL OR t.id IN (SELECT trip_id FROM browser_pods WHERE ended_at IS NULL))`. **The parentheses are required**: appending `AND` to the existing `A OR B` would bind only to `B` and let live Buntzen rows through. A test covers it (§10). |
| `control/store.py` `ControlStore.trip_row` | `… WHERE t.id = $1 AND t.provider = 'bcferries'`. A non-BC Ferries id reads as "no such trip", which the hub already handles. |
| `web/repo.py` `WebStore.trip` | `… WHERE id = $1 AND user_id = $2 AND archived_at IS NULL AND provider = 'bcferries'` (404 `No such trip`). That covers `command`, `check_now`, `request_hold` and `held_cart`, which all call it first. |
| `web/repo.py` `update_trip`, `answer_booked` | Their `FOR UPDATE` lookups add `AND provider = 'bcferries'`. |
| `web/repo.py` `archive_trip` | Becomes `archive_trip(uid, trip_id, *, provider: str | None = "bcferries")`. The `/api/trips` alias passes the default; `DELETE /api/watches/{id}` passes `None` (any provider). `_archive` itself is unchanged. |
| `web/repo.py` `add_trip` | `INSERT INTO trips (id, user_id, provider, spec, enabled) VALUES ($1, $2, 'bcferries', $3, $4)`. The `maxTrips` count adds `AND provider = 'bcferries'`. Wording unchanged: `You can watch up to {n} trips`. |
| `web/repo.py` `snapshot` | The pre-0004 "pause all" shim's `UPDATE trips … WHERE archived_at IS NULL AND enabled AND user_id IN (…)` gains `AND provider = 'bcferries'`, so it never writes `enabled` into another provider's spec. BC Ferries entries are built exactly as today, from `provider = 'bcferries'` rows, plus `provider`, `id` and `enabled` (§6.3). HTTP and degraded entries are new code. |
| `web/api.py` `user_state` | Its booking-URL loop iterates `w for w in state["watches"] if w["provider"] == "bcferries"` (reading `w["trip"]`, as today). It still returns `state | app[PUBLIC]`, and `app[PUBLIC]` holds only `links` and `push` (asserted). `providers`, `capacity` and `watches` are per-snapshot keys and must never be in PUBLIC: the right side of `|` wins. |
| `web/outbox.py` `_deliver` | Selects `t.provider` as well. Dispatch is §6.5: BC Ferries rows go to today's `Messages.render` byte for byte. |
| `web/admin.py` | `overview` pops `check_report` from the switches as it pops `fleet_report`. Admin additions are in §6.6. `release_cart`, `recycle_pod` and suspend-with-release are unchanged: they act on `trip_state` carts and `browser_pods`, which I3 makes BC Ferries-only. |

**Unchanged, and tested as unchanged (§10):** `protocol.py` (frozen), `agent/**`, `control/planner.py`,
`control/pods.py`, `control/hub.py`, `control/configure.py`, `holds.py`, `tickets.py`, `vnc-edge/**`, and BC
Ferries' message texts and links (`web/messages.py`'s `Messages.render` branches).

### 2.5 Import-linter contracts (verbatim TOML, appended to `pyproject.toml` in the contract commit)

The contract commit also adds `"waitlist.fetchwire"` to the `source_modules` of the existing contract "The
shared core never drives a browser". The control stubs exist in the contract commit, so the last contract can
name them from the start.

```toml
[[tool.importlinter.contracts]]
name = "Providers are pure: no I/O, no database, no app, no browser"
type = "forbidden"
source_modules = ["waitlist.providers"]
forbidden_modules = [
  "aiohttp",
  "asyncpg",
  "playwright",
  "selectolax",
  "waitlist.agent",
  "waitlist.control",
  "waitlist.web",
  "waitlist.fetcher",
  "waitlist.fetchwire",
  "waitlist.bcferries.site",
  "waitlist.bcferries.parse",
]

[[tool.importlinter.contracts]]
name = "Only waitlist-fetcher runs a provider's client (it parses what a site sends)"
type = "forbidden"
source_modules = ["waitlist.web", "waitlist.control", "waitlist.agent"]
forbidden_modules = ["waitlist.providers.buntzen.client", "waitlist.fetcher"]

[[tool.importlinter.contracts]]
name = "waitlist-fetcher holds nothing: no database, no Kubernetes, no app, no browser"
type = "forbidden"
source_modules = ["waitlist.fetcher"]
forbidden_modules = [
  "asyncpg",
  "jwt",
  "playwright",
  "selectolax",
  "waitlist.agent",
  "waitlist.control",
  "waitlist.web",
  "waitlist.tickets",
  "waitlist.bcferries",
]

[[tool.importlinter.contracts]]
name = "Providers don't know about each other"
type = "independence"
modules = ["waitlist.providers.bcferries", "waitlist.providers.buntzen"]

[[tool.importlinter.contracts]]
name = "The HTTP check loop never touches Kubernetes or a browser"
type = "forbidden"
source_modules = ["waitlist.control.checks", "waitlist.control.checkplan", "waitlist.control.fetch"]
forbidden_modules = [
  "waitlist.control.kube",
  "waitlist.control.fleet",
  "waitlist.control.pods",
  "waitlist.control.hub",
  "waitlist.control.config",
  "waitlist.control.main",
  "waitlist.agent",
  "playwright",
  "selectolax",
]
```

`control.config` is listed explicitly because it imports `control.pods` (so importing it would break this
contract anyway). The rule behind the contract: `checks`, `checkplan` and `fetch` import only
`waitlist.providers`, `waitlist.fetchwire`, `waitlist.polling`, `waitlist.models`, `control.planner` (for the
pure `UserView`), `control.limits` and `control.store` (for `USER_CHANNEL`), besides the standard library,
msgspec, asyncpg and aiohttp. `checkplan` is also imported by waitlist-web, so it stays free of asyncpg and
aiohttp as well. main.py passes the loop plain values (`rates`, the fetcher URL and timeout), never a
`ControlConfig`.

W1 adds a meta-test, `tests/test_import_contracts.py::test_every_client_is_fenced`. Every
`src/waitlist/providers/*/client.py` must appear in the "Only waitlist-fetcher runs a provider's client"
contract's `forbidden_modules`, and every provider package in the independence contract, so a third provider
can't slip past.

### 2.6 Adding a provider later (checklist)

1. Add `src/waitlist/providers/<id>/` with `meta.py`, a pure `__init__.py` exporting `PROVIDER`, and
   `client.py`.
2. Register it in `providers/__init__.py` (`http=[BUNTZEN, NEW]`) and `fetcher/registry.py`.
3. Add an expand-only migration widening `trips_provider_check`.
4. Add it to the two import-linter contracts.
5. Add its card, form and detail page under `web/src/providers/<id>/`, its zod variant in `watch-schema.ts`
   (an amendment) and its booking prefix.
6. In home-cluster, add its hosts to the fetcher's DNS/FQDN egress and its `waitlist.providers.<id>` values.

---

## 3. Database (W1)

### 3.1 Migration `src/waitlist/control/migrations/0008_providers.sql` (verbatim)

The foundation's 0007 touches only `browser_pods_name_check`; the `fleet_state_key_check` below lists all six
keys 0002 allows, plus the two new ones.

```sql
-- Waitlist 1.0: watches on more than one provider (docs/waitlist/design.md §3).
-- A `trips` row is a watch on any provider: "trip" stays its name in SQL and in the agent protocol.
-- Expand only, except that the new column's default is dropped at once (design.md §3.4): every row before this
-- migration is a BC Ferries trip, and no binary older than this one ever runs against a Waitlist database.

-- ---- 1. Which provider each watch is on, fixed for its life

ALTER TABLE trips ADD COLUMN provider text NOT NULL DEFAULT 'bcferries';
-- From now on every insert says which provider: a forgotten one fails instead of becoming a BC Ferries trip.
ALTER TABLE trips ALTER COLUMN provider DROP DEFAULT;
-- The ids in waitlist.providers.PROVIDER_IDS (a test holds the two together). A new provider widens this list.
ALTER TABLE trips ADD CONSTRAINT trips_provider_check CHECK (provider IN ('bcferries', 'buntzen'));
CREATE INDEX trips_provider_live ON trips (provider, user_id) WHERE archived_at IS NULL;

CREATE FUNCTION trips_provider_is_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.provider <> OLD.provider THEN
        RAISE EXCEPTION 'a watch''s provider never changes';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trips_provider_is_fixed BEFORE UPDATE OF provider ON trips
    FOR EACH ROW EXECUTE FUNCTION trips_provider_is_fixed();

COMMENT ON TABLE trips IS 'One watch per row, on any provider (trips.provider). Named for BC Ferries, the first.';

-- ---- 2. What only BC Ferries trips may have, and what only the others may

-- Browser pods, cart claims and checkout tickets exist for BC Ferries trips only, whatever a bug in
-- waitlist-control or waitlist-web asks for. (Rows restored from Ferry Watch predate these triggers.)
CREATE FUNCTION bcferries_trip_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM trips WHERE id = NEW.trip_id AND provider = 'bcferries') THEN
        RAISE EXCEPTION '% rows are for BC Ferries trips only', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER browser_pods_bcferries_only BEFORE INSERT ON browser_pods
    FOR EACH ROW EXECUTE FUNCTION bcferries_trip_only();
CREATE TRIGGER hold_claims_bcferries_only BEFORE INSERT OR UPDATE OF trip_id ON hold_claims
    FOR EACH ROW EXECUTE FUNCTION bcferries_trip_only();
CREATE TRIGGER vnc_mints_bcferries_only BEFORE INSERT ON vnc_mints
    FOR EACH ROW EXECUTE FUNCTION bcferries_trip_only();

CREATE FUNCTION http_trip_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM trips WHERE id = NEW.trip_id AND provider <> 'bcferries') THEN
        RAISE EXCEPTION '% rows are for HTTP-checked watches only', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$;

-- ---- 3. What an HTTP check last found (waitlist-control writes it; waitlist-web reads it)

CREATE TABLE check_results (
    trip_id char(16) PRIMARY KEY REFERENCES trips (id) ON DELETE CASCADE,
    -- base.search_hash of the spec this result is for: waitlist-web shows it only while the spec still matches.
    search_hash text NOT NULL CHECK (search_hash ~ '^[a-f0-9]{16}$'),
    outcome text NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'ok', 'no_inventory', 'error')),
    -- waitlist.providers.base.Option, as msgspec writes them: at most 20, at most 16 KiB.
    options jsonb NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) <= 20
               AND octet_length(options::text) <= 16384),
    attempted_at timestamptz,  -- the last check that reached the site, whatever came of it
    checked_at timestamptz,  -- the last check in which at least one of the watch's queries read the site
    error_class text CHECK (error_class ~ '^[a-z_]{1,32}$'),  -- base.CheckErrorKind of the last failed check
    consecutive_errors integer NOT NULL DEFAULT 0 CHECK (consecutive_errors >= 0),
    error_since timestamptz,  -- the first failure of the current streak (NULL while checks succeed)
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER check_results_http_only BEFORE INSERT ON check_results
    FOR EACH ROW EXECUTE FUNCTION http_trip_only();

-- ---- 4. Two more fleet_state keys: the admin's per-provider pause, and the check loop's last report

ALTER TABLE fleet_state
    DROP CONSTRAINT fleet_state_key_check,
    ADD CONSTRAINT fleet_state_key_check CHECK (key IN (
        'brake', 'breaker', 'maintenance', 'pause_carting', 'control_alive_at', 'fleet_report',
        'paused_providers', 'check_report'
    ));
-- Seeded, so waitlist-web (which may only update it) can flip it: a JSON array of provider ids, sorted.
INSERT INTO fleet_state (key, value) VALUES ('paused_providers', '[]') ON CONFLICT (key) DO NOTHING;
```

### 3.2 Grants (`src/waitlist/control/db.py`; verbatim replacement of `WEB_NOTIFICATION_KINDS`, `WEB_FLEET_KEYS` and `_grants`)

Changes from 0.7.1, with every other privilege kept exactly:

- `REVOKE ALL` first, so the grants are exact and idempotent: a privilege an older release granted is gone
  after `migrate`. It all runs in one transaction under the migrator's advisory lock (`grant()` is unchanged),
  so nobody sees the gap.
- `trips` for web: SELECT, INSERT, DELETE, and a column-level UPDATE on `spec, enabled, rev, paused_reason,
  archived_at, updated_at`. Web can't move a watch to another owner or provider, or rewrite its id. These are
  exactly the columns web writes: `repo.py` `_archive` (archived_at, enabled, paused_reason, rev, updated_at),
  `update_trip` (spec, enabled, rev, updated_at), `answer_booked` (paused_reason, rev, updated_at), the snapshot
  shim (enabled, spec, rev, updated_at), and the new `PATCH /api/watches` (spec, enabled, rev, updated_at).
- `check_results`: SELECT for web; full DML for control.
- `paused_providers` joins `WEB_FLEET_KEYS` (the admin's per-provider pause). `check_report` is control's only.
- `Roles` defaults stay the foundation's: `waitlist_web`, `waitlist_control`.

```python
# src/waitlist/control/db.py: these replace 0.7.1's WEB_NOTIFICATION_KINDS, WEB_FLEET_KEYS and _grants.

WEB_NOTIFICATION_KINDS = ("test", "account", "admin", "invited")
WEB_FLEET_KEYS = ("maintenance", "pause_carting", "paused_providers")


def _grants(web: str, control: str) -> list[str]:
    kinds = ", ".join(f"'{k}'" for k in WEB_NOTIFICATION_KINDS)
    keys = ", ".join(f"'{k}'" for k in WEB_FLEET_KEYS)
    return [
        # ---- start from nothing, so the grants below are exactly what each role has (a privilege an older
        # release granted and this one doesn't is gone). One transaction: nobody sees the gap.
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {web}, {control}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {web}, {control}",
        # ---- waitlist-web: people, watches, the API's reads, the outbox's sending
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON users, identities, unmatched_identities, invites, sessions,"
        f" user_limits, user_settings, push_subscriptions TO {web}",
        # Watches: never a new owner or provider (the triggers say so too), never a new id.
        f"GRANT SELECT, INSERT, DELETE ON trips TO {web}",
        f"GRANT UPDATE (spec, enabled, rev, paused_reason, archived_at, updated_at) ON trips TO {web}",
        f"GRANT SELECT ON trip_runtime, browser_pods, hold_claims, trip_state, alert_memory, check_results TO {web}",
        # ... and may make a check due now (a new watch, a re-enable, a manual check), ask for a hold
        # ("Hold it for me") and lift quiet hours for a watch (a manual check), nothing more.
        f"GRANT INSERT (trip_id), UPDATE (next_check_at, hold_requested_until, quiet_lifted_until) ON trip_runtime"
        f" TO {web}",
        f"GRANT DELETE ON alert_memory TO {web}",  # an edited search forgets what the old one alerted
        f"GRANT SELECT, INSERT ON agent_commands, events, vnc_mints, notifications TO {web}",
        f"GRANT DELETE ON events TO {web}",  # deleting a user takes their history with them
        f"GRANT UPDATE (sent_at, attempts, last_error, next_attempt_at) ON notifications TO {web}",
        f"GRANT SELECT ON hold_outcomes TO {web}",
        f"GRANT UPDATE (answer_reason) ON hold_outcomes TO {web}",
        f"GRANT SELECT, INSERT, UPDATE ON usage_ledger TO {web}",
        f"GRANT SELECT ON fleet_state TO {web}",
        f"GRANT UPDATE (value) ON fleet_state TO {web}",
        f"GRANT INSERT ON audit_log TO {web}",
        f"GRANT USAGE ON SEQUENCE agent_commands_id_seq, events_id_seq, notifications_id_seq,"
        f" push_subscriptions_id_seq, audit_log_id_seq TO {web}",
        # ---- waitlist-control: pods, what agents and the fetcher report, and the watches' pause and archive
        f"GRANT SELECT ON users, user_limits, user_settings, trips TO {control}",
        f"GRANT UPDATE (paused_reason, archived_at, updated_at) ON trips TO {control}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON trip_runtime, browser_pods, hold_claims, trip_state,"
        f" alert_memory, check_results TO {control}",
        f"GRANT SELECT ON agent_commands TO {control}",
        f"GRANT UPDATE (consumed_at) ON agent_commands TO {control}",
        f"GRANT SELECT, INSERT ON events, notifications, hold_outcomes TO {control}",
        f"GRANT SELECT, INSERT, UPDATE ON usage_ledger, fleet_state TO {control}",
        f"GRANT INSERT ON audit_log TO {control}",
        f"GRANT USAGE ON SEQUENCE events_id_seq, notifications_id_seq, hold_outcomes_id_seq, audit_log_id_seq"
        f" TO {control}",
        # ---- row-level limits (as 0.7.1, with the new switch in WEB_FLEET_KEYS)
        "ALTER TABLE notifications ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS web_reads ON notifications",
        f"CREATE POLICY web_reads ON notifications FOR SELECT TO {web} USING (true)",
        "DROP POLICY IF EXISTS web_queues_its_own_kinds ON notifications",
        f"CREATE POLICY web_queues_its_own_kinds ON notifications FOR INSERT TO {web} WITH CHECK (kind IN ({kinds}))",
        "DROP POLICY IF EXISTS web_sends ON notifications",
        f"CREATE POLICY web_sends ON notifications FOR UPDATE TO {web} USING (true) WITH CHECK (true)",
        "DROP POLICY IF EXISTS control_notifies ON notifications",
        f"CREATE POLICY control_notifies ON notifications FOR ALL TO {control} USING (true) WITH CHECK (true)",
        "ALTER TABLE fleet_state ENABLE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS web_reads ON fleet_state",
        f"CREATE POLICY web_reads ON fleet_state FOR SELECT TO {web} USING (true)",
        # waitlist-web flips the admin's switches, and may close the breaker (only waitlist-control opens it).
        "DROP POLICY IF EXISTS web_switches ON fleet_state",
        f"CREATE POLICY web_switches ON fleet_state FOR UPDATE TO {web} USING (key IN ({keys}, 'breaker'))"
        f" WITH CHECK (key IN ({keys}) OR (key = 'breaker' AND value = '\"closed\"'::jsonb))",
        "DROP POLICY IF EXISTS control_runs_the_fleet ON fleet_state",
        f"CREATE POLICY control_runs_the_fleet ON fleet_state FOR ALL TO {control} USING (true) WITH CHECK (true)",
    ]
```

### 3.3 What the database now guarantees (each claim exercised as the named role on PostgreSQL 16)

| Guarantee | Enforced by |
|---|---|
| Every insert names a provider, from the closed list | `provider NOT NULL` without default; `trips_provider_check` |
| A watch's provider never changes | trigger `trips_provider_is_fixed` (even for the owner); web lacks UPDATE on the column |
| A watch's owner never changes | 0001's trigger; web lacks UPDATE on `user_id` |
| Browser pods, cart claims and checkout tickets exist only for BC Ferries trips | triggers `browser_pods_bcferries_only`, `hold_claims_bcferries_only`, `vnc_mints_bcferries_only` |
| HTTP results exist only for HTTP-checked watches | trigger `check_results_http_only` |
| Only control writes check results | grants (web: SELECT only) |
| Web can't queue `opening`, `release_reminder`, `failing`, `site_changed`, `checker_down` | RLS `web_queues_its_own_kinds` (list unchanged: `test, account, admin, invited`) |
| Web can flip only `maintenance`, `pause_carting`, `paused_providers`, and close the breaker | RLS `web_switches` |
| Results stay bounded | `check_results` CHECKs: ≤ 20 options, ≤ 16 KiB, `error_class ~ '^[a-z_]{1,32}$'` |
| Control never edits a spec | grants (control: UPDATE only on `paused_reason, archived_at, updated_at`) |
| Control can lock a watch's row to compare `rev` while recording | `SELECT … FOR KEY SHARE OF t` works with control's column UPDATE grant |
| A manual check made while a fetch is in flight survives the recording | the compare-and-set upsert of §4.5 (verified as `waitlist_control` against a concurrent `waitlist_web` write) |

### 3.4 Expand/contract, and the default that 0008 drops

- **Decision: 0008 drops the provider default in the same migration.** This is safe because the first release
  that runs against the `waitlist` database already contains 0007, 0008 and code that always names a provider.
  No older binary ever runs against it: Ferry Watch keeps its own `ferrywatch` database (§8.7). A forgotten
  provider therefore fails loudly instead of silently becoming a BC Ferries trip.
- **Every `INSERT INTO trips` site W1 changes, in the same commit as 0008.** There are exactly 7, and adding
  `provider` with `'bcferries'` fixes all 122 failures:

  | Site | Change |
  |---|---|
  | `src/waitlist/web/repo.py:235` (`add_trip`) | `(id, user_id, provider, spec, enabled)` with `'bcferries'` |
  | `tests/test_db.py:48` (`seed`) | add `provider` `'bcferries'` |
  | `tests/test_db.py:210` | add `provider` `'bcferries'` |
  | `tests/test_db.py:302` | add `provider` `'bcferries'` |
  | `tests/test_hub.py:80` (the rig's trip) | add `provider` `'bcferries'` |
  | `tests/test_fleet.py:112` | add `provider` `'bcferries'` |
  | `tests/test_outbox.py:95` | add `provider` `'bcferries'` |

  Every new insert, in code or tests, names its provider.
- The column-level `trips` grant is not a migration: `grant()` reapplies it on every `migrate`.
- From 1.0 on the usual rule applies again: every migration is expand-only for a release, and release N-1 must
  run against N's schema. For example, a third provider widens `trips_provider_check`, and nothing narrows it a
  release later.
- No table is renamed or dropped. `trip_state` stays BC Ferries-only; HTTP results get their own table.

### 3.5 How a 0006/0007 database flows through (the cutover)

The `ferrywatch` database is at 0006 (Ferry Watch 0.7.1). The runbook (§8.7, C5) streams `pg_dump
--no-owner --no-privileges -d ferrywatch`, prefixed with `SET ROLE waitlist;`, into `psql --single-transaction
-v ON_ERROR_STOP=1 -d waitlist`, inside the CNPG primary pod. What happens:

1. Every object ends up owned by `waitlist` (because of `SET ROLE`), and no GRANT to `ferrywatch_*` is
   restored (`--no-privileges`). The ferrywatch roles still exist during the cutover, so the restore succeeds.
2. **The RLS policies come back still naming `ferrywatch_web`/`ferrywatch_control`.** Policies are schema, not
   privileges. `grant()`'s `DROP POLICY IF EXISTS` + `CREATE POLICY` (same names) rebinds every one to
   `waitlist_*`. Until then the Waitlist roles have no privileges at all, so the window fails closed. C8
   checks the result.
3. `schema_version` comes along (0001-0006). `waitlist-control migrate` then applies 0007 and 0008 and runs
   `grant()`. 0008's `ADD COLUMN … DEFAULT 'bcferries'` fills every restored row before the default is dropped.
4. Rows restored before 0008 (e.g. `browser_pods` history named `fw-…`) aren't checked by the new triggers,
   which fire on INSERT only. 0007 accepts both pod-name prefixes.
5. Restored state that would mislead the new deployment is cleaned in C6: sessions revoked, live pod rows
   ended, cart claims deleted, carts cleared, stale unsent notifications closed, and maintenance forced on.
   Waitlist starts in maintenance, and the owner turns it off after verifying (C8).

W1's `tests/test_cutover_restore.py` (local PostgreSQL 16) does this end to end:

- It builds a database with migrations 0001-0006, under owner `ferrywatch`.
- It applies 0.7.1's grant statements for `ferrywatch_web`/`ferrywatch_control`. They are frozen into the test
  as a literal list copied from the foundation's `_grants`.
- It inserts a user, a trip, its runtime, state and alert memory, an ended `fw-` pod, a session and an unsent
  notification.
- It runs the exact C5 shell pipeline with `/usr/lib/postgresql/16/bin/{pg_dump,psql}`, then C6's SQL, then
  `migrate` and `grant(Roles())`.
- It asserts: `migrate` applied exactly `0007_waitlist_names` and `0008_providers`; every trips row has
  `provider = 'bcferries'`; `pg_policy` names only `waitlist_web`/`waitlist_control`; every public table's
  owner is `waitlist`; `information_schema.role_table_grants` has no `ferrywatch*` grantee; and the §3.3 table
  holds, as each role.

---

## 4. Where HTTP-provider checks run, and how

### 4.1 Options

| Option | What it is | Verdict |
|---|---|---|
| A. control makes the call | Internet egress and third-party parsing in the process that holds pod create/delete, the egress-label patch and TokenReview | **No.** It breaks I2 and I6, and puts the biggest blast radius next to the most hostile input. |
| B. a "poller" Deployment with its own DB role | Egress to Yodel plus Postgres; scheduling and writes duplicated outside control; a new CNPG role, a 1Password field and RLS | **No.** A parser compromise gets DB credentials: user emails, every watch, the power to queue alerts. That breaks "only control writes what checkers report". |
| **B''. stateless `waitlist-fetcher`** | No DB, no SA token, no secrets, no RBAC. Ingress only from control, egress only to declared provider hosts. Control schedules, coalesces, budgets, evaluates and writes. | **Chosen.** It mirrors the browser-pod model: untrusted content is parsed in a sandbox that reports typed results to control. A compromise can forge only the inventories it returns for queries it's sent. It learns no user identity, only dates and place ids. |
| C. inside waitlist-web | The internet-facing process holds the OIDC, ticket and VAPID secrets, and would gain the writes | **No.** It breaks I1 and I7. |
| D. a lightweight pod per check via the fleet | A new admitted pod shape, VAP changes, a pod start per minute | **No.** Heavy, and it widens what control can create. |

### 4.2 Components after the change

```
 waitlist-web ──SQL (waitlist_web)──► Postgres ◄──SQL (waitlist_control)── waitlist-control
   (internet-facing; no k8s)             ▲  pg_notify wl_user                │  hub :8766 ◄── browser pods (BC Ferries)
                                         └────────── LISTEN ──────── web     │  fleet ──► kube-apiserver
                                                                             │  checks ──HTTP :8767──► waitlist-fetcher
                                                                                                        │ (no DB, no token,
                                                                                                        │  no secrets)
                                                                                                        ▼
                                                                                      api.yodelpass.com:443 only (public IPs)
```

| Identity | Can | Cannot |
|---|---|---|
| waitlist-fetcher (new) | Receive `POST /v1/fetch` from control's pods. Make HTTPS requests to `api.yodelpass.com` within its own per-provider cap (30/min) and concurrency (2). | Reach Postgres, the API server, other pods, the LAN, any other host, any non-public address, or DNS for any other name. It holds no secrets, no token, no writable filesystem beyond `/tmp`. Its replies carry no free text beyond Buntzen's sanitised ≤ 32-character status. |
| waitlist-control | Everything it had, plus calling the fetcher, writing `check_results`, queuing `opening`, `release_reminder`, `failing`, `site_changed` and `checker_down`, and archiving ended HTTP watches. | Reach the internet (unchanged). Edit specs. |
| waitlist-web | Everything it had, plus creating, editing and removing watches of offered providers, and pausing a provider (admins). | Write `check_results`. Queue HTTP alert kinds. Reach the fetcher (network policy). |
| Browser pods | Unchanged. | Reach the fetcher (their egress allows only the hub). |

### 4.3 The wire: `src/waitlist/fetchwire.py` (verbatim)

```python
"""waitlist-control <-> waitlist-fetcher, version 1 (design.md §4.3). Written verbatim from the design contract.

Control POSTs one FetchRequest to the fetcher's /v1/fetch and reads one FetchReply. The vocabulary is closed, as
the agent protocol's is: a request names a provider and that provider's typed query (never a URL, a header or a
host); a reply carries that provider's typed inventory or an error kind (never text from the site). Both sides
convert `query` and `inventory` with the provider's own Struct types, which refuse unknown fields.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

import msgspec

from waitlist.providers.base import FetchErrorKind, ProviderId

VERSION = 1
PATH = "/v1/fetch"
MAX_REQUEST_BYTES = 4 * 1024
MAX_REPLY_BYTES = 64 * 1024

RequestId = Annotated[str, msgspec.Meta(pattern=r"^[a-f0-9]{16}$")]
HttpStatus = Annotated[int, msgspec.Meta(ge=100, le=599)]
RetryAfter = Annotated[int, msgspec.Meta(ge=0, le=3600)]
Millis = Annotated[int, msgspec.Meta(ge=0, le=600_000)]


class _Wire(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    pass


class FetchRequest(_Wire, frozen=True, kw_only=True):
    v: Literal[1] = 1
    id: RequestId  # for matching log lines on both sides; carries nothing about who is watching
    provider: ProviderId
    query: dict[str, Any]  # msgspec.to_builtins(the provider's query_type)


class FetchOk(_Wire, tag="ok", tag_field="result", frozen=True, kw_only=True):
    v: Literal[1] = 1
    id: RequestId
    inventory: dict[str, Any]  # msgspec.to_builtins(the provider's inventory_type)
    elapsed_ms: Millis


class FetchFailed(_Wire, tag="failed", tag_field="result", frozen=True, kw_only=True):
    v: Literal[1] = 1
    id: RequestId
    error: FetchErrorKind
    http_status: HttpStatus | None = None
    retry_after_s: RetryAfter | None = None
    elapsed_ms: Millis = 0


FetchReply = FetchOk | FetchFailed

_request_decoder = msgspec.json.Decoder(FetchRequest)
_reply_decoder = msgspec.json.Decoder(FetchReply)
_encoder = msgspec.json.Encoder()


class WireError(ValueError):
    """A body that isn't a valid message of this protocol."""


def encode(message: FetchRequest | FetchOk | FetchFailed) -> bytes:
    return _encoder.encode(message)


def decode_request(body: bytes) -> FetchRequest:
    if len(body) > MAX_REQUEST_BYTES:
        raise WireError("request too large")
    try:
        return _request_decoder.decode(body)
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise WireError(str(e)) from None


def decode_reply(body: bytes) -> FetchReply:
    if len(body) > MAX_REPLY_BYTES:
        raise WireError("reply too large")
    try:
        return cast(FetchReply, _reply_decoder.decode(body))
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise WireError(str(e)) from None
```

The endpoint is `POST /v1/fetch`. Its body is a `FetchRequest` (≤ 4 KiB) with `Content-Type: application/json`.

- The reply is always HTTP 200 with a `FetchReply` (≤ 64 KiB), **including failures** (`FetchFailed`). That keeps
  "the site failed" apart from "the fetcher failed".
- An invalid request gets HTTP 400 with an empty body.
- A provider not in the fetcher's config gets `FetchFailed(error="unexpected")`. An exhausted cap (token
  bucket or semaphore) gets `FetchFailed(error="rate_limited")` with `http_status` null, at once; nothing
  queues. A site's own 429 always carries `http_status: 429` (`check_status`), and the fetcher's own refusals
  never carry a status. That is how control tells them apart: the fetcher's cap is our own limit, so control
  reads it as `fetcher_unavailable` (I11), not as the site rate-limiting us (§4.5, the fetcher client).
- `GET /livez` returns 200 `ok`; `GET /readyz` returns 200 once serving. There are no other routes.
  `client_max_size = MAX_REQUEST_BYTES`.
- `id` is a fresh `secrets.token_hex(8)` per request, and control checks that the reply's `id` matches.

Wire examples:

```json
{"v":1,"id":"5be0d1c27f6a9e31","provider":"buntzen","query":{"place_id":10672,"date":"2027-07-01"}}
```

```json
{"result":"ok","v":1,"id":"5be0d1c27f6a9e31","elapsed_ms":412,
 "inventory":{"place_id":10672,"date":"2027-07-01","items":[{"period":"morning","availability":"open","status":"Available"},{"period":"afternoon","availability":"sold_out","status":"Sold Out"}]}}
```

```json
{"result":"failed","v":1,"id":"5be0d1c27f6a9e31","error":"site_changed","http_status":404,"retry_after_s":null,"elapsed_ms":201}
```

### 4.4 `waitlist-fetcher` (W2)

**Entry point.** `waitlist-fetcher run` (script `waitlist.fetcher.main:main`; `waitlist-fetcher --help`
exits 0). Config comes from the JSON file at `$WL_FETCHER_CONFIG` (default
`/etc/waitlist-fetcher/config.json`), and the log level from `WL_LOG_LEVEL`. It runs from the same image as
waitlist-web and waitlist-control (`ghcr.io/mchestr/waitlist-control`, target `control`, uid 65532). It never
imports asyncpg (a contract). A config error exits with `waitlist-fetcher: <message>`, as the other two
processes do.

**Config (`waitlist/fetcher/config.py`).** msgspec, camelCase, `forbid_unknown_fields`:

```python
class _Config(msgspec.Struct, forbid_unknown_fields=True, kw_only=True, rename="camel"):
    pass

class ProviderLimit(_Config, kw_only=True):
    max_per_minute: Annotated[int, msgspec.Meta(ge=1, le=60)]

class FetcherConfig(_Config, kw_only=True):
    listen_port: int = 8767
    upstream_timeout_seconds: Annotated[int, msgspec.Meta(ge=1, le=30)] = 10
    max_body_bytes: Annotated[int, msgspec.Meta(ge=1024, le=1_048_576)] = 262_144
    providers: dict[str, ProviderLimit] = {}

    def check(self) -> FetcherConfig: ...   # keys ⊆ registry.FETCHERS; max_per_minute ≤ that provider's budget.max_per_minute

def load(path: Path) -> FetcherConfig: ...  # ConfigError(ValueError) on any problem
```

Rendered JSON: `{"listenPort": 8767, "upstreamTimeoutSeconds": 10, "maxBodyBytes": 262144, "providers":
{"buntzen": {"maxPerMinute": 30}}}`. A provider absent from `providers` is refused at request time.

**Registry (`waitlist/fetcher/registry.py`).**
`FETCHERS: Mapping[str, Fetcher[Any, Any]] = {"buntzen": waitlist.providers.buntzen.client.FETCHER}`. Query and
inventory types come from `waitlist.providers.REGISTRY.http[id]`. At import it asserts that
`FETCHERS.keys() ⊆ REGISTRY.http.keys()` and that each fetcher's `provider_id` equals its key.

**Request handling (`waitlist/fetcher/app.py`, `build(config, *, transport_factory) -> web.Application`).**

1. Decode with `fetchwire.decode_request` (400 on `WireError`).
2. Look up the provider (`unexpected` if not configured).
3. `query = msgspec.convert(req.query, provider.query_type, strict=True)` (400 on failure).
4. Take a token from the provider's bucket (capacity `maxPerMinute`, refill `maxPerMinute/60` per second,
   starting full) and a slot from its semaphore (`budget.concurrency`, non-blocking). If either is unavailable,
   reply `FetchFailed(error="rate_limited")` with no `http_status`. This cap is the fetcher's own and holds
   whatever control asks.
5. `inventory = await fetcher.fetch(transport, query)` under `asyncio.timeout(upstreamTimeoutSeconds + 2)`.
6. Reply `FetchOk(inventory=msgspec.to_builtins(inventory), elapsed_ms=…)`. If `len(encode(reply)) >
   MAX_REPLY_BYTES`, reply `too_large` instead.
7. `FetchError` → `FetchFailed(error=e.kind, http_status=e.http_status, retry_after_s=e.retry_after_s)`. A
   `TimeoutError` from step 5 → `timeout`. Any other exception → `FetchFailed(error="unexpected")`, logged with
   its class name only.

Each request logs one INFO line: `fetch provider=<id> id=<request id> result=<ok|failed> error=<kind|->
http_status=<n|-> elapsed_ms=<n>`. **Never a body, a header, or the query's date in an error message.** A test
asserts that no body or header ever appears in caplog.

**Transport (`waitlist/fetcher/transport.py`).** `AiohttpTransport(session, hosts: frozenset[str], *,
max_bytes: int, _scheme: str = "https", _port: int = 443)` implements `providers.base.Transport`. The two
private keywords are a **test seam only**: production code (`app.py`, `scripts/capture_yodel.py`) never passes
them, and a test in `test_transport.py` greps `src/` and `scripts/` to hold that. With them, the pre-I/O check
demands that scheme and port instead of https:443, so `test_transport.py` can drive the real `post_json`/`get`
code paths (redirects not followed, the multi-chunk read, the byte cap, Retry-After) against an
`aiohttp.test_utils` server on `http://127.0.0.1:<port>`, with `hosts={"127.0.0.1"}` and a session the test
builds itself without `PublicOnlyResolver` (pytest-socket's `--allow-hosts=127.0.0.1,::1` already permits it).
`PublicOnlyResolver` is tested separately, with an injected fake inner resolver. It follows these rules; the
two helpers are normative code, written as shown:

- **Before any I/O**, `urlsplit(url)` must give scheme `https`, port `None` or `443`, no username or password,
  and `hostname in hosts`. Anything else is `FetchError("unexpected")`, logged at ERROR.
- The one shared `aiohttp.ClientSession` is built in `app.py` as
  `aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=8,
  ttl_dns_cache=60), cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
  timeout=aiohttp.ClientTimeout(total=upstreamTimeoutSeconds))`, with default TLS verification. Every request
  passes `allow_redirects=False`.
- `post_json` sends `msgspec.json.encode(dict(payload))` as the body with the caller's headers; `get` sends
  none.
- A `Content-Length` over `max_bytes` is `FetchError("too_large")` before reading. The body is then read **to
  its end** by `_read_capped` (`StreamReader.read(n)` returns only what is buffered, so a single read would
  truncate a body that arrives in several chunks).
- `content_type` is `resp.content_type.lower()`. `retry_after_s` is the integer seconds of `Retry-After`,
  clamped to 0-3600; the HTTP-date form gives None.
- `TimeoutError` → `"timeout"`. `aiohttp.ClientConnectionError`, `aiohttp.ClientPayloadError`, `ssl.SSLError`
  and `OSError` (including the resolver's refusal) → `"network"`.

```python
async def _read_capped(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """The whole body, read to its end, or FetchError("too_large") once it passes max_bytes."""
    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > max_bytes:
            raise FetchError("too_large", http_status=resp.status)
    return bytes(body)


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """aiohttp's ThreadedResolver, keeping only global addresses: a DNS answer pointing at the LAN, a pod, a
    service, loopback, link-local or CGNAT space is refused, so toFQDNs can't be steered inward (DNS rebinding)."""

    def __init__(self, inner: aiohttp.abc.AbstractResolver | None = None) -> None:
        self._inner = inner if inner is not None else aiohttp.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        found = await self._inner.resolve(host, port, family)
        public = [r for r in found if ipaddress.ip_address(r["host"]).is_global]
        if not public:
            raise OSError(f"{host} resolved to no public address")
        return public

    async def close(self) -> None:
        await self._inner.close()
```

**Authentication between control and the fetcher.** There are two layers, and deliberately no shared secret
(I7):

- **The network.** Native and Cilium policies admit only waitlist-control's pods to :8767. The fetcher can
  reach only kube-dns (for its declared names) and its declared hosts on public addresses.
- **The vocabulary.** A caller can name only a registered provider and a query its Struct accepts, e.g.
  `place_id ∈ {10671, 10672}`. There is no URL, host, header or body field. Even a caller that got past the
  policy has only a rate-capped availability oracle for public data, not a proxy.

Control doesn't authenticate the fetcher either. Its replies are untrusted input: bounded and strictly
decoded. At worst they cause openings, which are capped at 6 per watch per hour, damped by a 120 s re-alert
cooldown per option, and deduplicated by alert memory (§4.5). They carry no text into notifications.

**Deployment shape** (rendered by W4, §8.2):

- One replica, RollingUpdate (surge 1, unavailable 0). It holds no lock and no state beyond its buckets.
  Control treats a brief outage as ours (I11).
- `automountServiceAccountToken: false`; its own ServiceAccount `waitlist-fetcher`, with no bindings.
- No secret volumes, no `PG*` env.
- `readOnlyRootFilesystem`, `drop: [ALL]`, `runAsNonRoot`, seccomp `RuntimeDefault`; `/tmp` an emptyDir.
- Requests cpu 10m and memory 64Mi; memory limit 192Mi.
- Probes `/livez` and `/readyz` on 8767. `dnsConfig` `ndots: 1`.

### 4.5 The check loop in waitlist-control (W1)

`HttpChecks` runs as a third task in `control/main.py` `_serve`, beside the hub and the fleet, under the same
run lock (single instance). It starts only when `ControlConfig.fetcher` is set. It uses its own
`aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())` opened in `_serve`, never the
kube session.

**Constants (`control/checkplan.py`).**

```python
TICK_SECONDS = 5.0              # the longest the loop sleeps: web's "Check now" and new watches are seen within this
MIN_SLEEP_SECONDS = 0.25
REPORT_SECONDS = 30.0           # fleet_state 'check_report' cadence
MIN_INTERVAL_S = 30.0           # floor for any provider's interval
JITTER = 0.10                   # ±10 % on every computed next_check_at
RELEASE_DELAY_S = 2.0           # the first check this long after window.opens_at
OUR_SIDE_RETRY_S = 30.0         # fetcher_unavailable / bad_reply: try again this soon, charging nothing
CHECKER_DOWN_ALERT_AFTER_S = 180.0
BACKOFF_AFTER = 3               # consecutive provider-side errors before a watch backs off
BACKOFF_MINUTES = (5, 10, 20, 40, 60)
FAILING_ALERT_AFTER = 5         # consecutive provider-side errors before the owner is told (once per streak)
MAX_OPENINGS_PER_HOUR = 6       # per watch; more are held back (flapping, or a forged inventory)
REALERT_COOLDOWN_S = 120.0      # an option that closes within this of being alerted stays remembered
RATE_LIMITED_COOLDOWN_S = 300   # provider-wide after a 429, or Retry-After if longer (≤ 3600)
BLOCKED_COOLDOWN_S = 1800       # provider-wide after a 401/403
ARCHIVE_AFTER_CLOSE_S = 43_200  # control archives an HTTP watch 12 h after its window closes
STALE_REPORT_S = 120.0          # web: an older check_report means checks aren't running

CHECKABLE: frozenset[CheckState] = frozenset({"pending", "checking", "error"})
OUR_SIDE: frozenset[CheckErrorKind] = frozenset({"fetcher_unavailable", "bad_reply"})
PROVIDER_WIDE: frozenset[CheckErrorKind] = frozenset({"rate_limited", "blocked"})
```

**`check_state` (pure; web imports it from `control/checkplan.py`, so both sides agree).**

```python
def check_state(*, enabled: bool, paused_reason: str | None, owner_active: bool, window: CheckWindow,
                quiet: bool, provider_paused: bool, provider_running: bool, backoff_until: float | None,
                cooldown_until: float | None, consecutive_errors: int, attempted_at: float | None,
                now: float) -> CheckState:
    """The first that applies:
     1 paused           not enabled, paused_reason set, or not owner_active
     2 over             window.phase(now) == "after"
     3 provider_paused  maintenance is on, or the provider is in fleet_state.paused_providers
     4 waiting_release  window.phase(now) == "before"
     5 quiet            in the owner's quiet hours and not lifted (trip_runtime.quiet_lifted_until)
     6 unavailable      not provider_running
     7 backoff          backoff_until > now, or the provider's cooldown_until > now
     8 error            consecutive_errors >= BACKOFF_AFTER   (still checked)
     9 pending          attempted_at is None
    10 checking"""
```

Both callers pass `owner_active = user.active and not user.paused`, from `users.state = 'active'` and
`users.paused` (the pre-0004 "Pause all", which `planner._ineligible` still honours as `paused`), so a BC
Ferries trip and a Buntzen watch of the same user always agree. A user missing from the users query is not
active.

Control calls it with `provider_running=True`: control is the runner, and keeps trying while the fetcher is
down. That's how it notices the recovery. Web calls it with `provider_running = report is fresh (check_report.at
≥ now − STALE_REPORT_S) and report.providers[id] exists and its "fetcher" == "ok"`, and `cooldown_until` from the
same report entry.

**Planning (`control/checkplan.py`, pure).**

```python
@dataclass(frozen=True, slots=True)
class WatchInput:
    id: str
    user_id: str
    provider: str
    rev: int
    spec: object                    # the provider's loaded SpecT
    window: CheckWindow
    enabled: bool
    paused_reason: str | None
    next_check_at: float | None
    backoff_until: float | None
    quiet_lifted_until: float | None
    consecutive_errors: int
    attempted_at: float | None


@dataclass(frozen=True, slots=True)
class Job:                          # one upstream request
    provider: str
    query: msgspec.Struct
    watch_ids: tuple[str, ...]      # sorted: every checkable watch whose queries this round includes in full


@dataclass(frozen=True, slots=True)
class Reminder:
    watch_id: str                   # the lowest id among the owner's watches for that date
    user_id: str
    date: date                      # the watch date (Pacific)


@dataclass(slots=True)
class CheckPlan:
    jobs: list[Job]
    reminders: list[Reminder]
    states: dict[str, CheckState]   # every watch's state at `now`
    deferred: dict[str, int]        # per provider: due watches that didn't fit the budget this round
    ended: list[str]                # watch ids whose window closed ≥ ARCHIVE_AFTER_CLOSE_S ago
    wake_at: float | None           # the earliest future instant at which something becomes due


def plan_checks(watches: Sequence[WatchInput], users: Mapping[str, UserView], registry: Registry, *,
                provider_paused: frozenset[str], cooldown_until: Mapping[str, float],
                tokens: Mapping[str, int], now: float) -> CheckPlan: ...
```

`UserView` is `control.planner.UserView`, which is pure. Rules:

1. A watch is **due** when its state is in `CHECKABLE`, `(next_check_at or 0) <= now`, and
   `now >= opens_at + RELEASE_DELAY_S`.
2. Due watches are taken oldest first (`next_check_at or opens_at`, then id). One is admitted only if **all**
   of its `provider.queries(spec)` fit in the provider's remaining `tokens`; a query already in this round
   costs nothing. A watch that doesn't fit is counted in `deferred[provider]` and waits for the next tick.
3. Every other watch in `CHECKABLE` with `now >= opens_at + RELEASE_DELAY_S` whose queries are all in the round
   joins their jobs: free freshness, no extra requests.
4. One `Job` per distinct `(provider, query)`.
5. **Reminders.** Take the watches with `reminder_at(spec)` not None, `reminder_at <= now < opens_at`, a state
   not in `{paused, provider_paused, over}`, and an owner not in quiet hours at `now`. Collapse them per
   `(user_id, watch_date)` into one `Reminder` carrying the lowest watch id.
6. `ended`: every watch with `now >= closes_at + ARCHIVE_AFTER_CLOSE_S`.
7. `wake_at` is the minimum of these future instants: `opens_at + RELEASE_DELAY_S` of `waiting_release`
   watches; `reminder_at` of watches whose reminder is still ahead; `next_check_at` of watches in `CHECKABLE`;
   and `closes_at + ARCHIVE_AFTER_CLOSE_S`. It is None when there are none.

**The loop (`control/checks.py`).**

```python
class HttpChecks:
    def __init__(self, pool: asyncpg.Pool, client: FetchClient, *, rates: Mapping[str, int],
                 registry: Registry = REGISTRY, clock: Callable[[], float] = time.time,
                 rng: random.Random | None = None) -> None: ...
    async def run(self, stop: asyncio.Event) -> None: ...
    async def tick(self) -> CheckPlan: ...
```

`rates` is `ControlConfig.http_rates()`: provider id → requests a minute. The providers checked are exactly
`rates.keys()`. Tokens come from in-memory buckets (capacity = rate, refill rate/60 per second, starting full).
`run` first restores each provider's `cooldown_until` from the last `fleet_state.check_report`, where it is
still in the future, so a restart doesn't forget a 429. It then loops: `plan = await tick()`, then sleep
`min(TICK_SECONDS, max(MIN_SLEEP_SECONDS, plan.wake_at - now))` (or `TICK_SECONDS` without a `wake_at`), or
until `stop`. A tick that raises (`asyncpg.PostgresError`, `OSError`) is logged, and the loop sleeps
`TICK_SECONDS`.

`tick()`:

1. **Read**, as `waitlist_control`:
   ```sql
   SELECT t.id, t.user_id, t.provider, t.spec, t.enabled, t.paused_reason, t.rev,
          rt.next_check_at, rt.backoff_until, rt.quiet_lifted_until, rt.failures,
          cr.consecutive_errors, cr.attempted_at, cr.error_since, m.alerted
   FROM trips t
   LEFT JOIN trip_runtime rt ON rt.trip_id = t.id
   LEFT JOIN check_results cr ON cr.trip_id = t.id
   LEFT JOIN alert_memory m ON m.trip_id = t.id
   WHERE t.archived_at IS NULL AND t.provider = ANY($1::text[])
   ```
   Users and their quiet hours come from checks.py's **own copy** of the fleet's users SQL (it may not import
   `control.fleet`, §2.5):
   ```sql
   SELECT u.id, u.state, u.paused, s.quiet_start, s.quiet_end FROM users u
   LEFT JOIN user_settings s ON s.user_id = u.id
   ```
   Each row becomes `planner.UserView(id, active=state == 'active', paused=paused,
   quiet=polling.quiet_hours(quiet_start, quiet_end))`. The switches come from `fleet_state` (`maintenance`,
   `paused_providers`). `provider.load_spec(json.loads(spec))` runs on each row (I10); a `SpecError` skips the
   watch, logged at WARNING once per `(id, rev)`.
2. `provider_paused` = every configured provider if maintenance is on, else `paused_providers ∩ rates`.
3. `plan_checks(…)`.
4. **Reminders.** For each reminder whose dedup key wasn't inserted by this process yet (in-memory set):
   `INSERT INTO notifications (user_id, trip_id, kind, params, dedup_key) VALUES ($user, $watch,
   'release_reminder', '{}', 'remind:<user>:<YYYY-MM-DD>') ON CONFLICT (dedup_key) DO NOTHING`.
5. **Archive ended watches.** For each id in `ended`, in one transaction: `UPDATE trips SET archived_at =
   now(), updated_at = now() WHERE id = $1 AND archived_at IS NULL`. Only when that reports `UPDATE 1`, insert
   the event `(kind 'check', code 'watch_ended', params '{}', dedup_key 'ended:<watch>') ON CONFLICT
   (dedup_key) DO NOTHING`. After the commit, `pg_notify('wl_user', owner)`. No notification row is queued: an
   ended watch tells nobody anything.
6. **Fetch.** Run the jobs with at most `budget.concurrency` in flight per provider, through
   `FetchClient.fetch(provider, query)`. Each job gives an inventory or a `CheckFailed`.
7. **Provider-level effects**, in this order:
   - `rate_limited` on any job: `cooldown_until[p] = now + max(RATE_LIMITED_COOLDOWN_S, retry_after_s or 0)`.
   - `blocked`: `cooldown_until[p] = now + BLOCKED_COOLDOWN_S`.
   - `site_changed` on any job: one admin notification, `kind 'site_changed'`, params `{"provider": p}`, user =
     the first active admin (`SELECT id FROM users WHERE is_admin AND state = 'active' ORDER BY id LIMIT 1`;
     none: skip), trip NULL, dedup `site_changed:<p>:<Pacific YYYY-MM-DD>`.
   - `rate_limited` and the cooldown apply only to a `CheckFailed("rate_limited")`, which the fetcher client
     produces only for a site's own 429 (§4.5, the fetcher client). The fetcher's own cap arrives as
     `fetcher_unavailable` and costs no cooldown.
   - Every job of p failed with `replied` false (the fetcher didn't answer, or answered garbage):
     `down_since[p] = down_since[p] or now`. Any job of p that got a reply from the fetcher (an inventory, or a
     `CheckFailed` with `replied` true, the fetcher's own cap included) clears `down_since[p]`. When `now −
     down_since[p] ≥ CHECKER_DOWN_ALERT_AFTER_S`: an admin notification `kind 'checker_down'`, params
     `{"provider": p, "since": down_since[p]}`, same user rule, dedup `checker_down:<p>:<UTC YYYYMMDDHH>`.
8. **Record each watch** in the round's jobs, in one transaction per watch (below). Each watch's transaction
   runs inside `try/except asyncpg.PostgresError`, logged at ERROR with the watch id, so one bad row (an FK
   violation from a user deleted mid-fetch, say) never stops the other watches of the tick.
9. `pg_notify('wl_user', user_id)` once per affected user, after commit.
10. Every `REPORT_SECONDS`, upsert `fleet_state['check_report']` (§6.6 shape).

**Recording one watch.** It opens with:

```sql
SELECT rev, enabled, archived_at, paused_reason FROM trips WHERE id = $1 FOR KEY SHARE
```

If no row comes back (the watch or its owner was deleted while the fetch was in flight), or the watch was
edited, archived, disabled or paused since planning (`rev` differs, `archived_at` set, `NOT enabled`, or
`paused_reason` set), the transaction rolls back and the result is dropped; the next tick replans.

Each of the watch's queries ended one of three ways: **ok** (an inventory), **theirs** (a `CheckFailed` whose
kind isn't in `OUR_SIDE`) or **ours** (an `OUR_SIDE` kind). `kind` is the first *theirs* failure in
`queries(spec)` order. The record is the first case that applies:

| Case | When | Evaluates? | Counters (`consecutive_errors`, `failures`) | Per-watch backoff |
|---|---|---|---|---|
| **Success** | every query ok | yes | reset to 0 | cleared |
| **Partial** | at least one ok, at least one failed | yes, over the ok queries | +1 if any *theirs*; else unchanged | never |
| **Provider-side failure** | none ok, at least one *theirs* | no | +1 | from `BACKOFF_AFTER` on |
| **Our-side failure** | none ok, all *ours* | no | unchanged | unchanged |

Records are per watch, but evaluation is per query: a query that keeps failing (the half-day place answering
`site_changed`, say) never stops a watch from alerting on what its other queries found, and never throttles
them. That is safe because `evaluate` is total over missing inventories.

Every write to `trip_runtime.next_check_at` below is a **compare-and-set** against the value read in step 1
(`$planned`), so a "Check now" that waitlist-web wrote while the fetch was in flight survives:

```sql
INSERT INTO trip_runtime (trip_id, last_check_at, next_check_at, failures, backoff_until, updated_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (trip_id) DO UPDATE SET
  last_check_at = coalesce(EXCLUDED.last_check_at, trip_runtime.last_check_at),
  failures = EXCLUDED.failures, backoff_until = EXCLUDED.backoff_until, updated_at = now(),
  next_check_at = CASE WHEN trip_runtime.next_check_at IS NOT DISTINCT FROM $6
                       THEN EXCLUDED.next_check_at ELSE trip_runtime.next_check_at END
```

`interval_at(now)` below means `now + max(MIN_INTERVAL_S, provider.interval(spec, at) × uniform(1 − JITTER,
1 + JITTER))`, where `at = datetime.fromtimestamp(now, UTC)` (§2.2 rule 7). `trip_runtime.mode` stays NULL
and `phase` stays `'off'` (fleet columns). `needs_attention` is never set for HTTP watches: they recover by
themselves when the site does.

*Evaluation* (Success and Partial):

```
ok        = {q: inventory for each of the watch's queries that returned one}
options   = provider.evaluate(spec, ok, at)          # tuple[Option, ...], provider order; a failed query's
                                                     # options come out "unchecked"
unchecked = {o.key for o in options if o.status == "unchecked"}
outcome   = 'no_inventory' if no wanted option is open/maybe/sold_out/not_released and some wanted one is
            not_offered, else 'ok'
alertable = [o.key for o in options if o.wanted and o.status in ALERTABLE]          # provider order
previous  = set(alert_memory.alerted or ())
gone      = previous - set(alertable) - unchecked    # an option whose query failed this round is never "gone"
recent    = {k in gone with an event (trip_id = $w, code 'opening_seen', params->>'option' = k)
             newer than now - REALERT_COOLDOWN_S}                        # stays remembered: damps flapping
fresh     = [k for k in alertable if k not in previous]
events 'opening_gone' {"option": k} for k in gone - recent
if fresh:
    n = count(notifications WHERE trip_id = $w AND kind = 'opening' AND created_at > now() - interval '1 hour')
    if n >= MAX_OPENINGS_PER_HOUR:      # not remembered: they alert once the hour allows
        event 'alert_held_back' {"count": n}, dedup 'held:<w>:<UTC YYYYMMDDHH>' ON CONFLICT DO NOTHING
        fresh = []
    else:
        events 'opening_seen' {"option": k} for k in fresh
        INSERT notifications (user_id, trip_id, kind, params, dedup_key) VALUES
          (owner, $w, 'opening', {"options": fresh, "uncertain": [k in fresh with status "maybe"]},
           'open:<w>:<int(now * 1000)>')
UPSERT alert_memory (trip_id, key_hash, alerted, offered) =
       ($w, '', sorted((previous & (set(alertable) | unchecked)) | recent | set(fresh)), '{}')
```

*Success* then writes:

```
UPSERT check_results SET search_hash = base.search_hash(provider.search_key(spec)), outcome,
       options = msgspec.json.encode(list(options)), attempted_at = now, checked_at = now,
       error_class = NULL, consecutive_errors = 0, error_since = NULL, updated_at = now()
trip_runtime CAS: last_check_at = now, failures = 0, backoff_until = NULL, next_check_at = interval_at(now)
```

*Partial, with a theirs failure* (the site failed part of the watch; what it did answer was alerted on above):

```
e = consecutive_errors + 1;  since = error_since or now
UPSERT check_results SET search_hash = base.search_hash(provider.search_key(spec)), outcome = 'error',
       options = msgspec.json.encode(list(options)), attempted_at = now, checked_at = now,
       error_class = kind, consecutive_errors = e, error_since = since, updated_at = now()
next = cooldown_until[p]        if any theirs failure is in PROVIDER_WIDE      # the provider waits
       now + OUR_SIDE_RETRY_S   elif any query failed ours
       interval_at(now)         otherwise                                       # the healthy queries keep pace
trip_runtime CAS: last_check_at = now, failures = e, backoff_until = NULL, next_check_at = next
event 'check_failed' and the 'failing' notification: exactly as for a provider-side failure (below)
```

*Partial, ours only* (I11: our trouble changes no counter, no outcome, no event):

```
INSERT check_results (trip_id, search_hash, outcome, options, attempted_at, checked_at)  -- a first-ever row:
       VALUES ($w, base.search_hash(...), outcome, options, now, now)                   -- outcome as computed,
ON CONFLICT (trip_id) DO UPDATE SET search_hash, options, attempted_at, checked_at,      -- zero errors
       updated_at = now()           -- outcome, error_class, consecutive_errors, error_since keep their values
trip_runtime CAS: last_check_at = now, failures and backoff_until as read in step 1,
                  next_check_at = now + OUR_SIDE_RETRY_S
```

*Provider-side failure* (every query failed, at least one on the site's side: no evaluation, no openings, the
last options kept; this is the only case that backs the watch off):

```
e = consecutive_errors + 1;  since = error_since or now
UPSERT check_results SET outcome = 'error', error_class = kind, consecutive_errors = e, error_since = since,
       attempted_at = now, updated_at = now()
       (a first-ever row: search_hash of the current spec, options '[]')
if any theirs failure is in PROVIDER_WIDE:
                            backoff = NULL;  next = cooldown_until[p]                       # the provider waits, not the watch
else:                       backoff = now + BACKOFF_MINUTES[min(e - BACKOFF_AFTER, 4)] min  if e >= BACKOFF_AFTER else NULL
                            next = max(backoff or 0, interval_at(now))
trip_runtime CAS: failures = e, backoff_until = backoff, next_check_at = next  (last_check_at unchanged)
event 'check_failed' {"class": kind}   only when e == 1 or e == FAILING_ALERT_AFTER
if e == FAILING_ALERT_AFTER:
    notification 'failing' {"error_class": kind}, dedup 'failing:<w>:<int(since)>'
```

*Our-side failure* (I11): nothing in `check_results` or the counters changes, and no event or alert is
written. Only `next_check_at` moves, still compare-and-set:

```sql
UPDATE trip_runtime SET next_check_at = $2, updated_at = now()     -- $2 = now + OUR_SIDE_RETRY_S
WHERE trip_id = $1 AND next_check_at IS NOT DISTINCT FROM $3       -- $3 = the value read in step 1
```

**Everything control queues for HTTP watches.** Every notification insert is `ON CONFLICT (dedup_key) DO
NOTHING`.

| kind | user_id | trip_id | params | dedup_key |
|---|---|---|---|---|
| `opening` | owner | watch | `{"options": [...], "uncertain": [...]}` | `open:<watch>:<epoch ms>` |
| `release_reminder` | owner | lowest watch id for that date | `{}` | `remind:<user>:<YYYY-MM-DD>` |
| `failing` | owner | watch | `{"error_class": kind}` | `failing:<watch>:<epoch s of the streak's first error>` |
| `site_changed` | first active admin | NULL | `{"provider": id}` | `site_changed:<id>:<Pacific YYYY-MM-DD>` |
| `checker_down` | first active admin | NULL | `{"provider": id, "since": epoch}` | `checker_down:<id>:<UTC YYYYMMDDHH>` |

| events.kind | code | params | dedup_key |
|---|---|---|---|
| `check` | `opening_seen` | `{"option": key}` | NULL |
| `check` | `opening_gone` | `{"option": key}` | NULL |
| `check` | `alert_held_back` | `{"count": n}` | `held:<watch>:<UTC YYYYMMDDHH>` |
| `check` | `check_failed` | `{"class": kind}` | NULL |
| `check` | `watch_ended` | `{}` | `ended:<watch>` |
| `watch` (written by web) | `booked` | `{}` | NULL |

**Who decides what, at a glance.**

| Concern | Decided by | Value for Buntzen |
|---|---|---|
| When a watch is checkable | `provider.window(spec)` | 07:00 PT the day before → the latest stop time of its passes |
| Quiet hours | the user's own (`user_settings`), as for BC Ferries; lifted by "Check now" for 10 min | default 22:00-05:00 |
| The 7:00 release | the loop wakes at `opens_at + RELEASE_DELAY_S`; then `interval` = 30 s for 20 min | ≤ 2 requests per date at 07:00:02 |
| Steady polling | `interval` = 60 s; out of season 30 min; ±10 % jitter; floor 30 s | |
| Politeness budget | control's token bucket per provider (`requestsPerMinute`) + the fetcher's own hard cap | 12/min; cap 30/min |
| Backoff | per watch after 3 provider-side errors in a row in which every query failed (5 → 60 min); a watch with one healthy query keeps its pace; provider-wide cooldown on a site's 429 (≥ 5 min) and 401/403 (30 min), persisted through `check_report` | |
| Our own outage | never charged (I11); retry in 30 s; admins after 3 min | |
| Who is told | the owner (`opening`, `release_reminder`, `failing`); admins (`site_changed`, `checker_down`) | |
| When it's cleared away | control archives it 12 h after `closes_at` | |

**The fetcher client (`control/fetch.py`).**

```python
class CheckFailed(Exception):
    def __init__(self, kind: CheckErrorKind, *, retry_after_s: int | None = None, replied: bool = False) -> None: ...
    # replied: the fetcher answered with a valid FetchFailed (step 7's down_since uses it)

class FetchClient:
    def __init__(self, http: aiohttp.ClientSession, url: str, *, timeout_s: float = 20.0) -> None: ...
    async def fetch(self, provider: HttpProvider[Any, Any, Any], query: msgspec.Struct) -> msgspec.Struct: ...
```

- It POSTs `fetchwire.encode(FetchRequest(id=secrets.token_hex(8), provider=provider.meta.id,
  query=msgspec.to_builtins(query)))` to `{url}/v1/fetch` with `aiohttp.ClientTimeout(total=timeout_s)`, and
  reads the body to its end, capped at `MAX_REPLY_BYTES` (the `_read_capped` loop).
- Connection error, `TimeoutError` → `CheckFailed("fetcher_unavailable")`.
- Non-200, a body over the cap, `WireError`, or an id mismatch → `CheckFailed("bad_reply")`.
- `FetchFailed(error="rate_limited", http_status=None)`: the fetcher's own cap, which never carries a status
  (§4.3) → `CheckFailed("fetcher_unavailable", replied=True)`. It is our own limit: retried in 30 s, charging
  no watch and no provider cooldown (I11).
- Any other `FetchFailed` → `CheckFailed(error, retry_after_s=…, replied=True)`. A `rate_limited` here always
  has `http_status == 429`, so it is the site's, and step 7 cools the provider down.
- `FetchOk` → `msgspec.convert(reply.inventory, provider.inventory_type, strict=True)`. A failure is
  `bad_reply`.

**Control config (`control/config.py`).** Both new fields default to off, so a 1.0 control with 0.7.1-shaped
config runs exactly as before:

```python
class FetcherClientConfig(_Config, kw_only=True):
    url: str                                                   # ^https?://[a-z0-9.-]+(:\d+)?$, checked in check()
    timeout_seconds: Annotated[int, msgspec.Meta(ge=1, le=60)] = 20

class HttpCheckConfig(_Config, kw_only=True):
    requests_per_minute: Annotated[int, msgspec.Meta(ge=1, le=60)] | None = None  # None: budget.default_per_minute

class ControlConfig(_Config, kw_only=True):
    ...                                                        # every 0.7.1 field, unchanged
    fetcher: FetcherClientConfig | None = None
    http_checks: dict[str, HttpCheckConfig] = {}

    def http_rates(self) -> dict[str, int]: ...                # id -> requests_per_minute or the budget's default
```

`check()` refuses an `httpChecks` key not in `REGISTRY.http`, a rate above that provider's
`budget.max_per_minute`, a non-empty `httpChecks` without `fetcher`, and a `fetcher.url` that doesn't match.
Rendered JSON: `"fetcher": {"url": "http://waitlist-fetcher.waitlist.svc.cluster.local.:8767",
"timeoutSeconds": 20}, "httpChecks": {"buntzen": {"requestsPerMinute": 12}}`.

---

## 5. The Buntzen Lake provider (W2)

### 5.1 Facts this design relies on

- BC Hydro's Buntzen Lake day-use parking is sold on Yodel and is free. There are three passes: all-day
  (`placeId` 10671, page `…/All-Day-Pass`) and the half-day morning and afternoon passes (`placeId` 10672, page
  `…/Half-Day-Pass`).
- Passes for date D are released at **07:00 Pacific on D−1**, and popular ones sell out in seconds.
  Cancellations put passes back during the day.
- The 2026 season ran May 14 to Sep 7 (booking opened May 13). Out of season the API probably lists nothing,
  or only sold-out items: a normal "nothing open", **not an error**.
- A booking needs a Yodel account, a licence plate and a mobile number, and isn't transferable. Hence
  alert-only (I4).
- **Unverified:** the response shape beyond the Home Assistant automation, the exact pass periods, and whether
  the portal accepts a date in its URL. Hence a defensive parser, `site_changed`, fixtures labelled as
  synthesized, a capture script (§5.8), and `offer: "admins"` until a real capture confirms it (§11 Q11).
- **A detail that matters:** Home Assistant's `rest_command` response variable is `{"status": <http status>,
  "content": <parsed body>}`. `response['content']['catalogItemExtendedResultLst']` therefore most likely
  means the **body's top level** holds `catalogItemExtendedResultLst`. The parser accepts the list at the top
  level, under `content`, or under `data`.
- **The automation's quirk:** for half-day passes it compared `catalogItemStatus == 'Morning'`/`'Afternoon'`.
  That suggests the status *is* the period while a pass is on sale, and a sold-out item reads plainly
  "Sold Out" with no period word. If so, the most common in-season answer is a list in which every item says
  "Sold Out" and none names its period. That must read as **both half-day passes sold out**, not as a changed
  site (§5.4 step 8).

### 5.2 Spec (`buntzen/spec.py`)

```python
Pass = Literal["full_day", "morning", "afternoon"]
PASSES: tuple[Pass, ...] = ("full_day", "morning", "afternoon")   # canonical order everywhere

@dataclass(frozen=True, slots=True)
class BuntzenSpec:
    date: date                      # the day at the lake (Pacific)
    passes: tuple[Pass, ...]        # non-empty, unique, in PASSES order
    release_reminder: bool = True   # a push at 06:55 the day before, when passes go on sale at 7:00

def parse_spec(data: Mapping[str, Any], *, base: BuntzenSpec | None, now: datetime) -> BuntzenSpec
def load_spec(stored: Mapping[str, Any]) -> BuntzenSpec
def dump_spec(spec: BuntzenSpec) -> dict[str, Any]
```

Stored in `trips.spec` and sent as `spec`, always with all three keys:
`{"date": "2027-07-01", "passes": ["morning", "afternoon"], "release_reminder": true}`.

Validation (`parse_spec`). Each failure raises `SpecError(field, message)` with exactly these messages, checked
in this order:

| Rule | field | message |
|---|---|---|
| `data` isn't an object | `spec` | `Expected a JSON object` |
| a key other than date, passes, release_reminder (`id` and `enabled` included) | that key (the first, sorted) | `Unknown field` |
| `date` missing (no base) | `date` | `Required` |
| `date` isn't a `YYYY-MM-DD` string | `date` | `Use a date like 2027-07-01` |
| `date` isn't on the calendar | `date` | `That date doesn't exist` |
| `passes` missing (no base), not a list, or empty | `passes` | `Pick at least one pass` |
| a pass not in PASSES | `passes` | `Passes are full_day, morning or afternoon` |
| `release_reminder` present and not a bool | `release_reminder` | `Must be true or false` |
| date before today (Pacific, from `now`) | `date` | `Pick today or a later date` |
| date after today + `META.horizon_days` | `date` | `Pick a date from today to 30 days ahead` |
| date is today and every chosen pass's stop time (§5.5) has passed | `passes` | `Those passes are over for today` |

- With `base`, `data` is merged over `dump_spec(base)`, as `Trip.from_json(base=)` does.
- The three **date rules** (the last three rows) apply only when there is no base, or when `data` carries a
  `date` different from `base.date`. Toggling the reminder on a watch for today must never fail.
- `release_reminder` defaults to `True` when absent on create.
- Duplicates in `passes` collapse, and the order is normalised to PASSES.
- `bool` is never accepted as an int, nor an int as a bool.

`load_spec` applies the type and shape rules only, never the date rules, so yesterday's watch still loads. It
requires all three keys.

The remaining methods:

- `search_key(spec) = (spec.date,)`. Changing passes keeps alert memory, which is keyed by pass.
- `watch_date(spec) = spec.date`.
- `title(spec)` reads like `"Buntzen Lake, Thu Jul 1: morning or afternoon pass"`: lowercase labels joined with
  ", " and a final " or ", and the date as `%a %b %-d`.

### 5.3 Queries and the request (`buntzen/model.py`, `buntzen/client.py`)

```python
FULL_DAY_PLACE = 10671
HALF_DAY_PLACE = 10672
IsoDate = Annotated[str, msgspec.Meta(pattern=r"^\d{4}-\d{2}-\d{2}$")]

class BuntzenQuery(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    place_id: Literal[10671, 10672]       # the wire can't name any other place
    date: IsoDate
```

`queries(spec)` returns, in this order and deduplicated: `BuntzenQuery(place_id=10671, date=…)` if
`"full_day"` is wanted, and `BuntzenQuery(place_id=10672, date=…)` if `"morning"` or `"afternoon"` is wanted.

`build_request(query) -> tuple[str, dict[str, Any], dict[str, str]]` (url, payload, headers) gives exactly:

```
POST https://api.yodelpass.com/api/portal-catalog-items
user-agent:    Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36
x-api-version: 5.6
content-type:  application/json
{"userId":0,"placeId":"10672","reservationDate":"2027-07-01","catalogItemType":0,"scopeName":"buntzen lake","filterStartDate":null,"filterEndDate":null}
```

- `placeId` is a **string**, as Home Assistant sent it. The body's key order is as shown; a golden test pins
  the encoded bytes.
- The user agent is Buntzen's own constant `USER_AGENT` (the independence contract forbids importing BC
  Ferries').
- No cookies and no other headers: exactly the three Home Assistant sent, which are known to work. In
  particular no `accept` header; add one only if the May 2027 capture shows it is harmless. The golden test in
  `test_buntzen_client.py` pins the header set as well as the payload bytes.
- The URL is a module constant, `API_URL = f"https://{API_HOST}/api/portal-catalog-items"`. Tests use a fake
  `Transport`, never a socket.

### 5.4 The defensive parser (`buntzen/model.py`, `buntzen/client.py`)

The inventory lives in `model.py` (pure, imported by both the client and `policy.evaluate`):

```python
class PassItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    period: Literal["full_day", "morning", "afternoon", "unknown"]
    availability: Literal["open", "sold_out", "not_released", "unknown"]
    status: Detail                                   # base.Detail: the site's status, sanitised, ≤ 32 chars

class BuntzenInventory(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    place_id: Literal[10671, 10672]
    date: IsoDate
    items: Annotated[list[PassItem], msgspec.Meta(max_length=20)] = []
```

`client.py`:

```python
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
API_URL = f"https://{API_HOST}/api/portal-catalog-items"
def build_request(query: BuntzenQuery) -> tuple[str, dict[str, Any], dict[str, str]]
def parse_catalog(response: HttpResponse, query: BuntzenQuery) -> BuntzenInventory   # raises FetchError
class Client:   # provider_id = "buntzen", hosts = HOSTS; fetch() = build_request → transport.post_json → parse_catalog
FETCHER: Fetcher[BuntzenQuery, BuntzenInventory] = Client()
```

`parse_catalog` steps. **SC** means `raise FetchError("site_changed", http_status=response.status)`.

1. `base.check_status(response)`: non-200 is mapped there (429, 401/403, 3xx/404, 5xx…).
2. A `text/html` content type is **SC**. Otherwise the body must decode as JSON (`msgspec.json.decode`,
   catching `msgspec.DecodeError`, `ValueError` and `RecursionError`), else **SC**. This holds whatever the
   content type says, since some .NET APIs send `text/plain`.
3. The root must be an object, else **SC**. If it has any of `isSuccess`, `success` or `succeeded` with the
   value `false`, that's **SC**: the API refused the request (an obsolete `x-api-version`, say).
4. **The list** is `root["catalogItemExtendedResultLst"]`; else `root["content"]["catalogItemExtendedResultLst"]`
   when `content` is an object; else `root["data"]["catalogItemExtendedResultLst"]` when `data` is an object.
   If the key is in none of them, **SC**. `null` means an empty list. A value that isn't a list is **SC**, and
   so are more than 50 entries.
5. Each entry must be an object whose `catalogItemStatus` is a string (the field Home Assistant relied on),
   else **SC**.
6. **Period.**
   - Scan these fields in order: `catalogItemCategory`, `catalogItemName`, `name`, `title`, `description`,
     `catalogItemStatus`. Only string fields count. Each is normalised: lowercase, `-` and `_` become spaces,
     whitespace collapsed.
   - Tokens, matched as whole words or phrases: `full_day` ← "all day", "full day", "allday", "daily";
     `morning` ← "morning"; `afternoon` ← "afternoon". Times such as "8 am" are never tokens.
   - The first field that names exactly one period decides. A field naming two periods is skipped. No field
     gives `"unknown"`.
   - On place 10671, every item is `full_day` whatever its labels say. On place 10672, a `full_day` reading
     becomes `"unknown"`.
7. **Availability**, from the normalised `catalogItemStatus`:

   | Normalised status | availability |
   |---|---|
   | `sold out`, `soldout`, `full`, `unavailable`, `not available`, `no availability`, `closed`, or starting with `sold out` | `sold_out` |
   | `coming soon`, `not yet available`, `not released`, `upcoming`, `opens soon` | `not_released` |
   | `available`, `book now`, `open`, `limited`, `limited availability`, `few left`, `almost full`, `selling fast` | `open` |
   | `morning`, `afternoon`, `all day`, `full day` (the quirk: the status *is* the period while it's on sale) | `open` |
   | anything else, including empty | `unknown` |

   **Quantity fallback**, only when the status gave `unknown`: take the first of `availableQuantity`,
   `quantityAvailable`, `remainingQuantity`, `availableCount`, `remaining` that is an int (not a bool) ≥ 0.
   More than 0 means `open`; 0 means `sold_out`. A recognised status word always wins over a number.
8. **Half-day consistency** (place 10672 only). An item whose period is `unknown` and whose availability is
   `open` or `unknown` is **SC**: something may be bookable and we can't say which pass. An item whose period
   is `unknown` and whose availability is `sold_out` or `not_released` is kept. It stands for every half-day
   pass that has no item of its own (§5.5), which is the Home Assistant quirk read faithfully. So a non-empty
   list in which every item is an unlabelled "Sold Out" is a valid answer: morning and afternoon sold out.
9. `status` = the original status with characters outside `[A-Za-z0-9 .,:;'()/&+-]` removed, whitespace
   collapsed, and cut to 32 characters.
10. Keep the first 20 items. Build the inventory with `place_id` and `date` from the **query**, never from the
    body.

### 5.5 Options, stop times, seasons, cadence and booking links (`buntzen/policy.py`)

These constants are binding; W3 mirrors the values it displays (§7.3, §9.5):

```python
LABELS = {"full_day": "All-day pass", "morning": "Morning pass", "afternoon": "Afternoon pass"}
RELEASE_AT = time(7, 0)                 # Pacific, on date - 1
REMINDER_AT = time(6, 55)               # Pacific, on date - 1 (release_reminder)
BURST = timedelta(seconds=30)           # how often, for BURST_FOR after the release
BURST_FOR = timedelta(minutes=20)
STEADY = timedelta(seconds=60)
OFF_SEASON = timedelta(minutes=30)      # dates outside the season: checked, slowly
# When a pass stops being worth finding (Pacific, on the day). Unverified: confirm with the May 2027 capture.
STOP_AT = {"full_day": time(18, 0), "morning": time(12, 0), "afternoon": time(18, 0)}
PAGES = {"full_day": FULL_DAY_PAGE, "morning": HALF_DAY_PAGE, "afternoon": HALF_DAY_PAGE}
# Reservation seasons, both ends inclusive. Other years use the estimate, which is generous on purpose.
KNOWN_SEASONS = {2026: (date(2026, 5, 14), date(2026, 9, 7))}
ESTIMATED_SEASON = ((5, 1), (9, 30))    # (month, day) to (month, day)

def in_season(d: date) -> bool: ...     # inside KNOWN_SEASONS[d.year], else inside ESTIMATED_SEASON of d.year
```

- `window(spec)` = `CheckWindow(opens_at = combine(date − 1 day, RELEASE_AT, PACIFIC), closes_at =
  max(combine(date, STOP_AT[p], PACIFIC) for p in passes))`. A watch for today made at 09:00 is in its window
  at once (cancellations).
- `interval(spec, now)` = `OFF_SEASON` if not `in_season(spec.date)`, else `BURST` if `now < opens_at +
  BURST_FOR`, else `STEADY`.
- `reminder_at(spec)` = `combine(date − 1 day, REMINDER_AT, PACIFIC)` if `spec.release_reminder and
  in_season(spec.date)`, else None. There are no reminders out of season.

`evaluate(spec, inventories, now)` returns options in PASSES order. The **wanted** ones come first, then the
unwanted ones whose place is among `queries(spec)`: wanting only morning still shows the afternoon pass, for
context. The set and order of keys depend on the spec alone, never on which inventories are present, so
`evaluate(spec, {}, now)` lists exactly the keys a full check would (waitlist-web relies on it, §6.3).
Unwanted options never alert. Each option's status is the first rule that applies:

| Rule | status |
|---|---|
| `now ≥ combine(date, STOP_AT[p])` | `over` |
| its place's inventory isn't in `inventories`, and `now < opens_at` | `not_released` |
| its place's inventory isn't in `inventories` | `unchecked` |
| any item of period p is `open` | `open` |
| any item of period p is `unknown` | `maybe`, with `detail` = that item's `status` (if non-empty) |
| any item of period p is `not_released` | `not_released` |
| any item of period p is `sold_out` | `sold_out` |
| no item of period p, and an `unknown`-period item on its place is `not_released` | `not_released` |
| no item of period p, and an `unknown`-period item on its place is `sold_out` | `sold_out` |
| no item of period p | `not_offered` |

The other fields: `key` is the pass id; `label` is `LABELS[p]`; `wanted` is `p in spec.passes`;
`booking_url` is `PAGES[p]`.

`booking_url(spec, options)` = the first wanted option with a status in `ALERTABLE` → its page. Otherwise
`FULL_DAY_PAGE` if `"full_day"` is wanted, else `HALF_DAY_PAGE`. No date parameter is added: the portal's URL
scheme is unverified.

### 5.6 Booking-window rules and poll policy, in one place

- **Creatable** for today to today + 30 days (Pacific), unless every chosen pass is already over today.
- **Checked** from 07:00:02 PT on D−1 (the loop wakes for it) until `closes_at`. Options drop out one by one as
  each `STOP_AT` passes. Nothing is checked before the release: there's nothing to find, and it would only
  spend the budget.
- **Poll curve:** every 30 s for the first 20 minutes after the release, then every 60 s. Every 30 min for
  dates outside the season. ±10 % jitter, floor 30 s. Coalesced per `(place, date)` across every user. Capped
  at 12 distinct requests a minute by control and 30 by the fetcher. At most four distinct Buntzen queries are
  checkable at once (two places × today and tomorrow), so the peak is about 6 requests a minute. Home
  Assistant made up to 4.
- **Quiet hours.** The user's own quiet hours apply (no checks, no reminder). The form warns when they cover
  06:55 or 07:00 (§7.3).
- **Out of season.** An empty or `null` list makes every option `not_offered`, so the outcome is
  `no_inventory`. The card says: *"Yodel isn't offering passes for Thu Jul 1. Buntzen's reservation season
  usually runs from mid-May to early September."* No alert, no error, no reminder, a 30-minute cadence.
- **Ended.** After the last stop time the watch is `over`; control archives it 12 h later.

### 5.7 Alert wording and deep links (`buntzen/messages.py`)

`render(kind, params, spec, *, watch_url)`:

- re-checks `params`: `options` and `uncertain` must be lists of distinct pass ids, with `uncertain ⊆ options`
  and `options` non-empty. Otherwise it returns None (the outbox marks the row Unrenderable, §6.5) and logs.
- leaves `topic` as None (the outbox sets `<kind[:8]>-<watch id>`);
- sets `url` to `watch_url` (`https://waitlist.chestr.dev/w/<id>`), always;
- never uses `Option.detail` or anything else a site sent.

In the strings below:

- `{day}` is `%a %b %-d` of `spec.date`.
- Pass names are lowercase from `LABELS` (`all-day pass`, `morning pass`, `afternoon pass`), joined with " and ".
  With more than one, the noun is plural: "morning and afternoon passes".
- `{certain}` names the fresh passes not in `uncertain`; `{uncertain}` names the ones in it.

| kind | title | body | urgency, ttl | `also` (email only) |
|---|---|---|---|---|
| `opening`, some certain | `Buntzen Lake: {certain} open for {day}` | `Book it on Yodel now: passes go fast. Have your Yodel account, licence plate and mobile number ready.` If some are uncertain, it adds ` Yodel also shows the {uncertain} with a status we don't recognise; it may be bookable.` | high, 900 | (`Book on Yodel`, page of the first certain pass) |
| `opening`, all uncertain | `Buntzen Lake: {uncertain} may be open for {day}` | `Yodel shows a status we don't recognise; it may be bookable. Check on Yodel now.` | high, 900 | (`Book on Yodel`, page of the first) |
| `release_reminder` | `Buntzen Lake passes for {day} go on sale at 7:00 am` | `Popular passes sell out within seconds. Open Yodel and sign in now; we'll alert you if one opens up later.` | high, 600 | (`Open Yodel`, `booking_url(spec, ())`) |
| `failing` | `Having trouble checking Buntzen Lake` | `We'll keep trying. Until then, check Yodel yourself for {day}.` | low, 3600 | (`Open Yodel`, `booking_url(spec, ())`) |
| anything else | None | | | |

### 5.8 Fixtures and the capture script

The fixtures live in `tests/fixtures/buntzen/`. Every synthesized file is named `synth-*.json` (or
`synth-*.html`) and listed in `tests/fixtures/buntzen/README.md`, whose first line reads:
**"Synthesized from the Home Assistant automation's field names; not captured from Yodel. Replace with
`captured-*` files from scripts/capture_yodel.py."** No file carries a marker key inside it, which would
change the shape under test. A test asserts that every file is named `synth-*` or `captured-*` (plus
`.meta.json` companions) and appears in the README.

| File | Body | Expected `parse_catalog` → options |
|---|---|---|
| `synth-fullday-available.json` | `{"catalogItemExtendedResultLst": [ … ]}` (the list key at the root, not a bare array), 1 item `{catalogItemCategory: "All Day Pass", catalogItemStatus: "Available"}` | `[full_day/open]` → full_day `open` |
| `synth-fullday-soldout.json` | the same (list key at the root), `"Sold Out"` | full_day `sold_out` |
| `synth-bare-array.json` | a bare JSON array at the root, `[{…}]` | **SC** (step 3: the root must be an object) |
| `synth-halfday-morning-open.json` | `Morning`/`Available`, `Afternoon`/`Sold Out` (by category) | morning `open`, afternoon `sold_out` |
| `synth-halfday-ha-quirk.json` | categories `"Half Day Pass"`; statuses `"Morning"` and `"Sold Out"` | `[morning/open, unknown/sold_out]` → morning `open`, afternoon `sold_out` |
| `synth-halfday-ha-quirk-both.json` | statuses `"Morning"`, `"Afternoon"` | both `open` |
| `synth-halfday-ha-quirk-soldout.json` | categories `"Half Day Pass"`, both statuses `"Sold Out"` | `[unknown/sold_out ×2]` → morning `sold_out`, afternoon `sold_out` (**not** SC) |
| `synth-halfday-both-soldout.json` | `Morning`/`Sold Out`, `Afternoon`/`Sold Out` by category | both `sold_out` |
| `synth-wrapped-content.json` | the list under `"content"` | as unwrapped |
| `synth-wrapped-data.json` | the list under `"data"` | as unwrapped |
| `synth-empty.json` | `{"catalogItemExtendedResultLst": []}` | `[]` → `not_offered` (`no_inventory`) |
| `synth-null-list.json` | `{"catalogItemExtendedResultLst": null}` | `[]` |
| `synth-unknown-status.json` | morning `"Waitlist only"` | `[morning/unknown]` → morning `maybe`, detail `Waitlist only` |
| `synth-quantity-only.json` | morning, status `"Ready"`, `availableQuantity: 3` | morning `open` |
| `synth-not-released.json` | `"Coming Soon"` | `not_released` |
| `synth-changed-shape.json` | `{"items": []}` | **SC** |
| `synth-not-success.json` | `{"isSuccess": false, "catalogItemExtendedResultLst": []}` | **SC** |
| `synth-halfday-unplaceable-open.json` | two items, no period words anywhere, statuses `"Available"` | **SC** |
| `synth-status-not-string.json` | `catalogItemStatus: 3` | **SC** |
| `synth-error-page.html` | an HTML page, content type `text/html`, status 200 | **SC** |
| `synth-huge.json` | 51 items | **SC** |

**`scripts/capture_yodel.py`** (W2). The owner runs it from a workstation on the home network, never in CI:

```
uv run python scripts/capture_yodel.py --date 2027-05-20 [--place full|half|both] [--repeat N --every SECONDS] [--out tests/fixtures/buntzen] [--dry-run]
```

- `--dry-run` prints the exact request (`build_request`) for each place and sends nothing. A test runs it.
- Without `--dry-run` it refuses to run when `CI` or `PYTEST_CURRENT_TEST` is set.
- It uses `build_request` and `parse_catalog` from `waitlist.providers.buntzen.client`. Its transport is the
  fetcher's own `AiohttpTransport` with `PublicOnlyResolver`: https only, `api.yodelpass.com` only, no
  redirects, no cookies, a 256 KiB cap. It waits at least 5 s between requests.
- For each response it writes `captured-<place>-<date>-<YYYYMMDDTHHMMSSZ>.json` (the body, pretty-printed if
  it's JSON, else raw with `.txt`). Beside it goes `….meta.json`: `{"captured_at", "status", "content_type",
  "request": {"url", "payload"}, "parsed": <inventory as builtins> | {"error": kind}}`. It stores no response
  headers, so no cookies leak into the repo, and it prints the parsed result.
- `docs/providers/buntzen.md` (W2) says to run it once now, out of season, to capture the real envelope, and
  again on the first day of the 2027 season around 07:00. Each capture becomes a `captured-*` regression test.

---

## 6. Web API (W1)

### 6.1 Endpoints

Every mutation answers with the caller's whole state (the `GET /api/state` shape, §6.3), as today, except
`POST /api/watches`, which wraps it as `{"watch_id": "<id>", "state": {…}}` so the SPA can link to the new
watch. Errors are `{"error": msg, "field"?: f, "limit"?: n, "usage"?: n}`, as today. Path ids match
`[a-f0-9]{16}`.

| Method, path | Body | Notes |
|---|---|---|
| `GET /api/state` | | **Changed shape** (§6.3): `watches[]`, `providers[]` and the new `capacity` replace `trips` and the old `capacity` |
| `GET /api/providers` | | **New.** `{"providers": [ProviderInfo]}`, the same list as `state.providers` |
| `POST /api/watches` | `{"provider": str, "spec": object, "enabled"?: bool}` | **New.** 200 `{"watch_id", "state"}` |
| `PATCH /api/watches/{id}` | `{"spec"?: object, "enabled"?: bool}`, at least one | **New.** `spec` is partial, over the current one |
| `DELETE /api/watches/{id}` | | **New.** Archives any provider's watch (BC Ferries: its pod drains and hands back any cart, as today) |
| `POST /api/watches/{id}/check` | `{}` | **New.** Manual check, any provider |
| `POST /api/watches/{id}/done` | `{}` | **New.** "I booked it" for an alert-only watch: archives it and records an event |
| `POST /api/trips/{id}/hold`, `/cart/release`, `/cart/dismiss`, `/vnc`, `/booked` | as 0.7.1 | **Unchanged**, BC Ferries only. Another provider's id gives 404 `No such trip` on `/hold`, `/vnc`, `/cart/release` and `/cart/dismiss` (they go through `WebStore.trip`; the cart routes answer 400 `Which cart?` first without a valid `cart_id`), and 409 `There's no checkout to answer for` on `/booked`, exactly as for any trip without a checkout (`answer_booked`'s lookup finds no row) |
| `POST /api/trips`, `PATCH`/`DELETE /api/trips/{id}`, `POST /api/trips/{id}/check` | as 0.7.1 | **Kept for 1.0** as BC Ferries-only aliases (404 for another provider's id), so 0.7.1's request shapes and status codes still work. Their response bodies are the new state shape (`watches`, not `trips`). The SPA never calls them. Removed in 1.1. |
| `GET /api/terminals`, `PATCH /api/me`, push subscriptions, `POST /api/alerts/test`, `/auth/*`, `/api/ws` | | Unchanged. The socket sends the new state shape. |
| `POST /api/admin/switches` | also takes `{"switch": "pause_provider", "provider": str, "on": bool, "reason": str}` | **Extended** (§6.6) |
| `GET /api/admin/overview` | | adds `checks` and `providers`; pops `check_report` out of `switches` (§6.6) |
| `GET /api/admin/users` | | each user gains `watches: {"bcferries": n, "buntzen": m}` (live watches by provider, every registered provider present, 0 if none). The existing `trips` stays the BC Ferries count, for compatibility: its subquery gains `AND t.provider = 'bcferries'` |

`GET /api/providers`:

```json
{"providers": [
  {"id": "bcferries", "name": "BC Ferries", "noun": "trip", "description": "Sailing reservations. Can hold a sailing in a cart for you to check out.",
   "checker": "browser", "capabilities": {"auto_hold": true, "checkout": true, "alert_only": false}, "horizon_days": 30, "offered": true, "paused": false},
  {"id": "buntzen", "name": "Buntzen Lake", "noun": "pass", "description": "BC Hydro day-use parking passes, sold on Yodel. Alerts only: you book on Yodel.",
   "checker": "http", "capabilities": {"auto_hold": false, "checkout": false, "alert_only": true}, "horizon_days": 30, "offered": true, "paused": false}
]}
```

Each entry is `meta.to_json()` plus two fields, computed **per request**. One value is overridden: bcferries'
`horizon_days` is the deployment's `polling.horizonDays`: the `PollPolicy` that `web/main.py` passes to
`WebStore` (`config.policy()`), which `add_trip` enforces through `PollPolicy.within_horizon`. W1 exposes it as
a read-only `WebStore.horizon_days` property (`self._policy.horizon.days`) and the catalog builder uses it,
so the SPA and the server always agree. `BcferriesSheet` keeps `TripSheet`'s own date input unchanged (`min`
today, no `max`; a date past the horizon gets the server's 400 on `date`, as in 0.7.1).

- `offered`: the signed-in user may create one. Always true for bcferries. For an http provider, `offer ==
  "everyone"`, or `offer == "admins"` and the user is an admin.
- `paused`: maintenance is on, or the id is in `fleet_state.paused_providers`. Always false for bcferries.

The order is `REGISTRY.metas`. Providers that aren't offered are **included**, so an existing watch still names
its provider.

Server code lives in `web/watches.py` (new). It holds the handlers for `/api/providers` and `/api/watches*`,
and the app keys `REGISTRY_KEY = web.AppKey("registry", Registry)` and `OFFERS = web.AppKey("offers",
dict[str, Offer])`, set in `web/app.py` `build()` from `WebConfig.providers`.

### 6.2 Requests, validation, limits

**`POST /api/watches`**, e.g. `{"provider": "buntzen", "spec": {"date": "2027-07-01", "passes": ["morning",
"afternoon"], "release_reminder": true}}`. Checks run in this order:

1. A body key other than `provider`, `spec`, `enabled` → 400, field = that key, `Unknown field`.
2. `provider` isn't a string naming a provider in `REGISTRY` → 400 `provider`, `Not a provider you can watch`.
   Known but not offered to this user → 403 `provider`, `{name} isn't available to you`.
3. `spec` isn't an object → 400 `spec`, `Expected a JSON object`. `enabled` present and not a bool → 400
   `enabled`, `Must be true or false`.
4. **bcferries.** `id` or `enabled` inside `spec` → 400, that key, `Unknown field` (`enabled` belongs at the top
   level). Then `data = {**spec} | ({"enabled": enabled} if given else {})` goes to 0.7.1's `add_trip`
   unchanged. That means `Trip.from_json`, the configured horizon (`PollPolicy.within_horizon`), and
   `maxTrips` counted over `provider = 'bcferries'` rows under the user's `FOR UPDATE` lock, with 409 `You can
   watch up to {n} trips`. The only difference from 0.7.1 is the provider column it inserts.
5. **http provider.**
   - `spec = provider.parse_spec(spec, base=None, now=now)` (`SpecError` → 400 with its field and message).
   - In one transaction, with `SELECT 1 FROM users WHERE id = $1 FOR UPDATE`, count the user's live watches on
     every provider whose `meta.limit_key == "maxAlertOnlyWatches"`. At or over the limit → **409**
     `You can have up to {n} alert-only watches`, field `provider`, with `limit` and `usage`.
   - `INSERT INTO trips (id, user_id, provider, spec, enabled)` with `id = secrets.token_hex(8)` and `spec =
     json.dumps(provider.dump_spec(spec))`; then `INSERT INTO trip_runtime (trip_id)`. A NULL `next_check_at`
     means due as soon as the window allows.
6. Notify (`pg_notify`), then 200 `{"watch_id": id, "state": …}`.

**`PATCH /api/watches/{id}`**, e.g. `{"spec": {"passes": ["full_day"]}}` or `{"enabled": false}`.

- A `provider` key → 400 `provider`, `A watch's provider can't change`. Any other key besides `spec` and
  `enabled` → 400, that key, `Unknown field`. Neither present → 400 `spec`, `Nothing to change`.
- The watch is found with `SELECT provider, spec, enabled, rev FROM trips WHERE id = $1 AND user_id = $2 AND
  archived_at IS NULL FOR UPDATE`, else 404 `No such watch`. The spec isn't parsed before the provider is known.
- bcferries → 0.7.1's `update_trip(uid, id, {**spec, **({"enabled": e} if given else {})})`. `id`/`enabled`
  inside `spec` are refused as above.
- http → `new = provider.parse_spec(spec or {}, base=provider.load_spec(current), now=now)`. Store
  `dump_spec(new)` and `enabled`, with `rev = rev + 1, updated_at = now()`.
  - If the stored spec changed at all (`dump_spec(new) != current`, e.g. only `passes`): `UPDATE trip_runtime
    SET next_check_at = NULL WHERE trip_id = $1`, so the watch is checked again within one tick and its
    stored options catch up with the new spec. (An in-flight check's record sees the new `rev` and is
    dropped.) Until then the snapshot re-marks the stored options (§6.3).
  - If `search_hash(search_key)` changed as well: also `DELETE FROM alert_memory WHERE trip_id = $1`.
  - If `enabled` goes false → true: `next_check_at = NULL`.

**`DELETE /api/watches/{id}`**: `archive_trip(uid, id, provider=None)`, i.e. `_archive` for any provider.
404 `No such watch`.

**`POST /api/watches/{id}/check`.** The provider is read first, without parsing the spec.

- **bcferries**: 0.7.1's `check_now`, unchanged.
- **http**: the spec is loaded with `provider.load_spec`, and the state computed with `check_state` (web's
  inputs, §4.5). These refusals come first; they are 409 and consume nothing:
  - `waiting_release` → `Checks start {opens_at}`, with `opens_at` formatted in Pacific as `%a %b %-d at ` plus
    `models.format_time`, e.g. `Checks start Wed Jun 30 at 7:00 am`.
  - `over` → `It's too late for this watch`.
  - `provider_paused` → `Checks for {name} are paused right now`.
  - `unavailable` → `Checks for {name} aren't running right now`.
  - `paused` → `This watch is paused`.
  - `backoff`, or `quiet` while `resume = max(backoff_until, cooldown_until)` is in the future (a lifted quiet
    hour would still find the watch backing off) → `Checks resume at {time}`, where `{time}` is
    `models.format_time` of `resume` in Pacific, e.g. `Checks resume at 7:15 am`. Web can't clear
    `backoff_until` (it has no grant on it), so accepting the check would spend a `manualChecksPerHour` unit on
    nothing.

  Then, in one transaction, 0.7.1's `_check_now` (an `agent_commands` `check_now` row with `consumed_at =
  now`, since an http watch has no warm pod, counted against `manualChecksPerHour`: 429 `Up to {n} checks an
  hour`; and `trip_runtime.next_check_at = now`), and `quiet_lifted_until = now + 600`. Control picks it up
  within one tick. A Buntzen watch's check returns 200 (a test, §10).

**`POST /api/watches/{id}/done`**: only for providers with `capabilities.alert_only`. Otherwise 409 `Not for
this kind of watch` (BC Ferries answers "Did you book it?" instead). In one transaction: `_archive`, then
`INSERT INTO events (user_id, trip_id, kind, code, params) VALUES ($uid, $id, 'watch', 'booked', '{}')`.

**Limits.**

- New `LimitKey` `maxAlertOnlyWatches`: default **6**, ceiling **20**, in `control/limits.py` `LimitKey`,
  `DEFAULTS` and `CEILINGS`. It is shown in the state's `limits` and in admin overrides.
- `maxTrips` stays BC Ferries' alone, with its old wording.
- `manualChecksPerHour` is shared by every provider.

### 6.3 The state snapshot

**Top level.** As 0.7.1 (`me`, `limits`, `usage`, `settings`, `quiet_hours`, `devices`, `events`, and from
`app[PUBLIC]` `links` and `push`), **minus `trips` and the old `capacity`**, plus `watches`, `providers` and
the new `capacity`. `limits` shows `maxTrips`, `maxAlertOnlyWatches`, `maxActiveBrowsers`, `maxHoldsPerDay`,
`vncMinutesPerDay` and `manualChecksPerHour`.

**`watches`**: every live row of the user (`archived_at IS NULL`), ordered by `created_at` as today, whatever
the provider.

- **A bcferries entry** is 0.7.1's entry exactly (`trip` with its `id`, `enabled` and BC Ferries fields; `rev`;
  `paused_reason`; `quiet`; `hold`; `browser`; and `booking_url`, added by `api.user_state`), plus
  `"provider": "bcferries"`, `"id"` (= `trip.id`) and `"enabled"` (= `trip.enabled`). There is no `spec` key:
  `trip` is the one source. A golden test holds the 0.7.1 keys byte for byte (§10).
- **An http entry:**
  - `provider`, `id`, `rev`, `enabled`, `paused_reason`, `quiet` (`HoldContext.quiet(quiet_lifted_until, now)`,
    as for trips).
  - `spec`: `dump_spec(load_spec(stored))`.
  - `check`: `state` from `checkplan.check_state` (web's inputs); `opens_at`/`closes_at` from `window`;
    `last_check_at` from `check_results.attempted_at`; `checked_at`; `next_check_at` from `trip_runtime`,
    adjusted by 0.7.1's `_after_quiet`; and `outcome`, `error_class`, `consecutive_errors` from
    `check_results` (`pending`, null, 0 without a row).
  - `options`: `check_results.options` **only if** `check_results.search_hash == search_hash(search_key(spec))`.
    Otherwise `provider.evaluate(spec, {}, now)` (placeholders), with `outcome` reported as `pending`.
    When the hash matches, the stored options are always **re-marked against the current spec**, because they
    may come from an earlier spec with the same search key (e.g. `passes` edited, and a provider-side failure
    since then kept the old options): take `placeholders = provider.evaluate(spec, {}, now)` (its keys and
    order depend on the spec alone, §5.5); for each placeholder, use the stored option with the same key with
    `wanted` replaced by the placeholder's, or the placeholder itself if none is stored. Stored keys the
    placeholders don't list are dropped. For an unchanged spec this is the identity. So a pass no longer
    watched never shows **Book on Yodel**, and a newly wanted one shows its last-seen status until the next
    check (which the PATCH made due at once).
  - `booking_url`: `provider.booking_url(spec, options)` if `meta.allows_booking_url`, else null.
- **A row that won't load** (a provider not in this build's `REGISTRY`, or a `SpecError`) becomes
  `{"provider", "id", "rev", "enabled", "paused_reason", "quiet": false}`, logged once per `(id, rev)` at
  WARNING. It never fails the state; the SPA shows it as an unknown watch with Remove.

**`providers`**: as in `GET /api/providers`, built per snapshot (never in `app[PUBLIC]`).

**`capacity`**: `{"can_add": bool, "reason": str | null, "providers": {id: {"can_add", "reason", "limit",
"count"}}}`.

- bcferries: limit `maxTrips`, count = live bcferries rows, reason at the limit `You can watch up to {n} trips`.
- Each http provider: limit `maxAlertOnlyWatches`; count = live rows on every provider with that limit key.
  `can_add = offered and count < limit`, with reason `Not available to you` when not offered, else `You can have
  up to {n} alert-only watches` at the limit.
- `paused` does **not** block adding: the watch waits until checks resume.
- Top level: `can_add` = any provider's `can_add`; `reason` = null when it can, else `You're watching as many
  as you can`.

**The shared fixture** (`web/src/test/fixtures/api-state.json`, verbatim, contract commit). W3's vitest parses
it (`watch-schema.test.ts`). `tests/test_contract.py` checks its catalog and options against the Python
contract. W1's `tests/test_api_fixture.py` asserts that a snapshot built for equivalent rows has the same keys
and value types, entry by entry, for the two `buntzen` entries (open and waiting) and the `bcferries` entry.
The `bcparks` entry exists only to exercise the SPA's unknown-provider fallback (a newer server's provider).

```json
{
  "me": {
    "id": "u0000000000000000001",
    "username": "mike",
    "display": "Mike",
    "is_admin": true,
    "paused": false
  },
  "limits": {
    "maxTrips": {
      "value": 3,
      "source": "default"
    },
    "maxAlertOnlyWatches": {
      "value": 6,
      "source": "default"
    },
    "maxActiveBrowsers": {
      "value": 1,
      "source": "default"
    },
    "maxHoldsPerDay": {
      "value": 4,
      "source": "default"
    },
    "vncMinutesPerDay": {
      "value": 60,
      "source": "default"
    },
    "manualChecksPerHour": {
      "value": 6,
      "source": "default"
    }
  },
  "usage": {
    "manual_checks": 1
  },
  "watches": [
    {
      "provider": "buntzen",
      "id": "0f3c9a1b2d4e5f60",
      "rev": 2,
      "enabled": true,
      "paused_reason": null,
      "quiet": false,
      "spec": {
        "date": "2027-07-01",
        "passes": ["morning", "afternoon"],
        "release_reminder": true
      },
      "check": {
        "state": "checking",
        "opens_at": 1814364000.0,
        "closes_at": 1814490000.0,
        "last_check_at": 1814365907.0,
        "checked_at": 1814365907.0,
        "next_check_at": 1814365967.0,
        "outcome": "ok",
        "error_class": null,
        "consecutive_errors": 0
      },
      "options": [
        {
          "key": "morning",
          "label": "Morning pass",
          "status": "open",
          "wanted": true,
          "booking_url": "https://yodelportal.com/buntzen-lake/Half-Day-Pass",
          "detail": null
        },
        {
          "key": "afternoon",
          "label": "Afternoon pass",
          "status": "sold_out",
          "wanted": true,
          "booking_url": "https://yodelportal.com/buntzen-lake/Half-Day-Pass",
          "detail": null
        }
      ],
      "booking_url": "https://yodelportal.com/buntzen-lake/Half-Day-Pass"
    },
    {
      "provider": "buntzen",
      "id": "7a7a7a7a7a7a7a7a",
      "rev": 1,
      "enabled": true,
      "paused_reason": null,
      "quiet": false,
      "spec": {
        "date": "2027-07-03",
        "passes": ["full_day"],
        "release_reminder": true
      },
      "check": {
        "state": "waiting_release",
        "opens_at": 1814536800.0,
        "closes_at": 1814662800.0,
        "last_check_at": null,
        "checked_at": null,
        "next_check_at": null,
        "outcome": "pending",
        "error_class": null,
        "consecutive_errors": 0
      },
      "options": [
        {
          "key": "full_day",
          "label": "All-day pass",
          "status": "not_released",
          "wanted": true,
          "booking_url": "https://yodelportal.com/buntzen-lake/All-Day-Pass",
          "detail": null
        }
      ],
      "booking_url": "https://yodelportal.com/buntzen-lake/All-Day-Pass"
    },
    {
      "provider": "bcferries",
      "id": "a1b2c3d4e5f60718",
      "enabled": true,
      "trip": {
        "id": "a1b2c3d4e5f60718",
        "enabled": true,
        "from": "TSA",
        "to": "SWB",
        "date": "2027-07-03",
        "adults": 2,
        "children": 0,
        "infants": 0,
        "seniors": 0,
        "vehicle": "standard",
        "height_ft": 8.0,
        "earliest": "07:00",
        "latest": "12:00",
        "auto_cart": true,
        "fare": "cheapest"
      },
      "rev": 5,
      "paused_reason": null,
      "quiet": false,
      "hold": {
        "reason": null,
        "can_request": false,
        "holdable": [],
        "requested_at": null,
        "requested_until": null
      },
      "browser": {
        "phase": "watching",
        "mode": "warm",
        "queue_position": null,
        "next_check_at": null,
        "last_check_at": 1814365865.0,
        "checked_at": 1814365865.0,
        "next_due": 1814366165.0,
        "effective_interval": 300,
        "heartbeat_at": 1814365905.0,
        "sailings": [
          {
            "depart": "09:00",
            "arrive": "10:35",
            "vessel": "Spirit of Vancouver Island",
            "status": "sold_out",
            "fares": []
          }
        ],
        "error_class": null,
        "consecutive_errors": 0,
        "cart": null,
        "viewer_connected": false
      },
      "booking_url": "https://www.bcferries.com/?sailing=true&departureLocation=TSA&departureLocationName=Vancouver+%28Tsawwassen%29&arrivalLocation=SWB&arrivalLocationName=Victoria+%28Swartz+Bay%29&departingDateTime=07%2F03%2F2027"
    },
    {
      "provider": "bcparks",
      "id": "c0ffee00c0ffee00",
      "rev": 1,
      "enabled": true,
      "paused_reason": null,
      "quiet": false,
      "spec": {
        "park": "garibaldi"
      }
    }
  ],
  "providers": [
    {
      "id": "bcferries",
      "name": "BC Ferries",
      "noun": "trip",
      "description": "Sailing reservations. Can hold a sailing in a cart for you to check out.",
      "checker": "browser",
      "capabilities": {
        "auto_hold": true,
        "checkout": true,
        "alert_only": false
      },
      "horizon_days": 30,
      "offered": true,
      "paused": false
    },
    {
      "id": "buntzen",
      "name": "Buntzen Lake",
      "noun": "pass",
      "description": "BC Hydro day-use parking passes, sold on Yodel. Alerts only: you book on Yodel.",
      "checker": "http",
      "capabilities": {
        "auto_hold": false,
        "checkout": false,
        "alert_only": true
      },
      "horizon_days": 30,
      "offered": true,
      "paused": false
    }
  ],
  "capacity": {
    "can_add": true,
    "reason": null,
    "providers": {
      "bcferries": {
        "can_add": true,
        "reason": null,
        "limit": 3,
        "count": 1
      },
      "buntzen": {
        "can_add": true,
        "reason": null,
        "limit": 6,
        "count": 2
      }
    }
  },
  "settings": {
    "quiet_hours": {
      "start": "22:00",
      "end": "05:00"
    },
    "checkout_viewer": "classic",
    "stream_viewer_offered": true
  },
  "quiet_hours": {
    "start": "22:00",
    "end": "05:00",
    "now": false
  },
  "devices": 1,
  "events": [
    {
      "id": 41,
      "trip_id": "0f3c9a1b2d4e5f60",
      "ts": 1814365907.0,
      "kind": "check",
      "code": "opening_seen",
      "params": {
        "option": "morning"
      }
    }
  ],
  "links": {
    "kofi": null
  },
  "push": {
    "key": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM"
  }
}
```

### 6.4 Live updates

The mechanics are unchanged. Control's check loop calls `pg_notify('wl_user', user_id)` after each committed
record, reminder, archive or alert. LiveHub rebuilds the snapshot through `api.user_state`, and the outbox wakes
on the same channel. An admin's pause appears in the very next snapshot, because `providers[].paused` is read
per snapshot.

### 6.5 Messages and the outbox, per provider

`Outbox.deliver_due` selects `t.provider` as well. `_deliver` dispatches in this order:

1. **Generic kinds**, `test`, `invited` and `moved`, go to `Messages.render` as today (no trip). The new
   copy: `invited` reads `You're invited to Waitlist` / `Sign in with your account to start watching
   reservations for you.`; `test` is unchanged (`Alerts from Waitlist reach this device.`); `moved` reads
   `Ferry Watch is now Waitlist` / `Same app, new address: waitlist.chestr.dev. Sign in there, add it to your
   home screen again and turn alerts on for each device. Your BC Ferries trips came with you.`, url
   `{base}/`, ttl 86400. `EMAIL_KINDS` gains `moved`. Rows of this kind are inserted only by the database owner
   at cutover (§8.7 C9); web's RLS kind list stays as it is.
2. **`site_changed`, `checker_down`** (`base.ADMIN_NOTICE_KINDS`): nothing for the row's user. `_to_admins`
   sends `Admin: site changed` / `{name}'s answers can't be read; its checks are failing.`, or `Admin: checker
   down` / `waitlist-fetcher isn't answering; {name} checks are on hold.` (with `{name}` from
   `REGISTRY.meta(params["provider"])`, url `/admin`). The row is then marked sent.
3. **`t.provider` is NULL or `bcferries`**: 0.7.1's `Messages.render(kind, params, trip, cart)` and the rest of
   today's flow, **byte for byte**. There is one exception: a non-admin kind whose trip spec doesn't load is
   Unrenderable (step 5) instead of silently marked sent.
4. **Any other provider:** `provider = REGISTRY.http_provider(t.provider)`, then `notice = provider.render(kind,
   params, provider.load_spec(spec), watch_url=f"{base}/w/{trip_id}")`. Before converting it to
   `Message(title, body, url, urgency, ttl, topic or f"{kind[:8]}-{trip_id}", also)`, assert that
   `notice.url == watch_url` and that `also` is None or `meta.allows_booking_url(also[1])`.
5. **Unrenderable** is never silent. That covers a provider missing from `REGISTRY`, a `SpecError`, an
   exception in `render`, `render` returning None for a kind in `NOTICE_KINDS`, or a failed assertion. Log
   at ERROR, then `UPDATE notifications SET attempts = 6, last_error = 'Unrenderable' WHERE id = $1` (with
   `sent_at` still NULL). The admin attention list shows it as "Not delivered after 6 tries: … (Unrenderable)",
   and nothing goes to the user.

Also:

- `admin.ATTENTION_ALERTS` gains `"site_changed": "A provider's site changed shape; its checks are failing"`
  and `"checker_down": "waitlist-fetcher isn't answering; HTTP checks are on hold"`.
- `EMAIL_KINDS` otherwise unchanged. An `opening` goes by push, and by email only when no device took it
  (0.7.1's fallback). The email then carries the direct `Book on Yodel` link (`also`).
- BC Ferries' `ADMIN_KINDS` and `Messages.render` branches are untouched (goldens, §10).

### 6.6 Admin additions

`GET /api/admin/overview` keeps its 0.7.1 keys and changes these:

- `switches` no longer contains `check_report` (popped, as `fleet_report` is). It still contains
  `paused_providers` (a JSON array).
- new `"checks"`: `fleet_state.check_report` passed through, or null.
- new `"providers"`: `[{"id": "buntzen", "name": "Buntzen Lake", "offer": "admins", "paused": false}]`, one per
  http provider in `REGISTRY`, with `offer` from web's config and `paused` from `paused_providers`.

Control writes the report every 30 s:

```json
{"at": 1814365907.0, "version": "1.0.0",
 "providers": {"buntzen": {
   "running": true, "paused": false, "fetcher": "ok", "fetcher_down_since": null,
   "budget_per_minute": 12, "demand_per_minute": 4.0, "fetches_last_hour": 118, "errors_last_hour": 0,
   "last_ok_at": 1814365907.0, "last_error": null, "last_error_at": null, "cooldown_until": null,
   "watches": 3, "checking": 2, "deferred": 0}}}
```

- `running`: configured and not paused.
- `paused`: maintenance, or admin-paused.
- `fetcher`: `"ok"` or `"down"`.
- `last_error`: a `CheckErrorKind` or null.
- `demand_per_minute`: distinct queries per minute that the current watches' intervals ask for.
- `watches`: live watches of the provider.
- `checking`: watches in `CHECKABLE`.
- `deferred`: the last plan's count.

`POST /api/admin/switches {"switch": "pause_provider", "provider": "buntzen", "on": true, "reason": "Yodel
asked"}`:

- `provider` must be in `REGISTRY.http`, else 400 `provider`, `Not a provider whose checks can be paused`.
  `on` must be a bool and `reason` is required, as for the other switches.
- Sets `fleet_state.paused_providers` to the sorted list with or without the id.
- Writes an audit row `set_switch`, with `target_kind 'fleet'`, `target_id 'pause_provider:buntzen'`, and
  `after {"paused_providers": [...]}`.
- The existing switches are unchanged.

### 6.7 Web config additions (`web/config.py`), default off

```python
Offer = Literal["off", "admins", "everyone"]

class ProviderConfig(_Config, kw_only=True):
    offer: Offer = "off"

class WebConfig(_Config, kw_only=True):
    ...                                            # every 0.7.1 field, unchanged
    providers: dict[str, ProviderConfig] = {}      # keys ⊆ REGISTRY.http (bcferries is always offered); check() refuses others
```

Rendered JSON: `"providers": {"buntzen": {"offer": "admins"}}`. `LimitsConfig` accepts `maxAlertOnlyWatches`
because it is now a `LimitKey`. The control and fetcher configs are in §4.5 and §4.4.

### 6.8 SPA routes served by web (`web/main.py`)

```python
SPA_ROUTES = (
    "/",
    "/new",
    "/new/{provider:[a-z][a-z0-9_]{1,31}}",
    "/w/{watch_id:[a-f0-9]{16}}",
    "/c/{trip_id:[a-f0-9]{16}}",
    "/admin",
    "/settings",
    "/auth/callback",
)  # all serve index.html
```

---

## 7. Frontend information architecture (W3)

No new npm dependencies. Every string below is the copy to ship.

### 7.1 Routes (`web/src/mt/router.ts`)

| Path | Page | Notes |
|---|---|---|
| `/` | **Dashboard**: every watch, all providers | |
| `/new` | Dashboard with the New watch sheet open on the picker | |
| `/new/<provider>` | Dashboard with the sheet open on that provider's form | an unknown or un-offered provider falls back to `/new`; PWA shortcuts link here |
| `/w/<16hex>` | **Watch page**: the provider's `Detail` | where a push lands; BC Ferries' Detail is its card plus `CheckoutPage`'s held-cart panel |
| `/c/<16hex>` | BC Ferries checkout, unchanged | a non-BC Ferries id → `navigate("/w/<id>", {replace: true})` |
| `/settings`, `/admin`, `/auth/callback` | unchanged | admin gains a Providers panel |

```ts
export type Route =
  | { page: "home" }
  | { page: "new"; provider: string | null }
  | { page: "watch"; watchId: string }
  | { page: "checkout"; tripId: string }
  | { page: "settings" }
  | { page: "admin" }
  | { page: "callback" }
  | { page: "missing" };
```

### 7.2 Dashboard (`mt/Dashboard.tsx`, rendered by `MtApp.tsx`)

- **Header**: lucide `BellRing` and "Waitlist"; the gear and shield buttons as today.
- **Heading** "Your watches", with the subline `{n} watched` and, in quiet hours, ` · quiet until 5:00 am PT`.
- **Alerts banner** (`mt/AlertsBanner.tsx`): shown while *this device* has no push subscription
  (`PushManager.getSubscription()` is null) and the user has at least one watch. The text is "Turn on alerts on
  this device. It's how you'll hear when something opens." with **Turn on**. On iOS outside a home-screen app,
  it reads instead "To get alerts on iPhone, add Waitlist to your Home Screen (Share → Add to Home Screen), then
  open it from there." The banner is dismissible per device (`localStorage` `wl.alertsBannerDismissed`, every
  access in try/catch).
- **Sections**, each from the provider UI's `group(watch, now)`:
  - **Needs you now**: BC Ferries with a cart `adding`/`ready`, `paused_reason == "after_checkout"`, or
    bookable-and-unheld sailings (`unheld(trip).length > 0`); Buntzen with a wanted option `open`/`maybe`.
  - **Watching**: everything else that is live. Unknown watches go here.
  - **Paused** (collapsed, with a count): BC Ferries `!enabled` or another `paused_reason`; Buntzen
    `check.state == "paused"`.
  - **Ended** (collapsed): BC Ferries whose date is before today (Pacific); Buntzen `check.state == "over"`.

  Within a section the order is `date(watch)` ascending, then server order.
- **"Next" hint** under the heading when nothing needs the user: the soonest `next(watch, now)` among Watching,
  e.g. "Next: Buntzen Lake passes for Sat Jul 3 go on sale Fri at 7:00 am".
- **Each card** is wrapped in `mt/WatchCard.tsx`: an error boundary (`components/ErrorBoundary.tsx`, which
  gains an optional `fallback?: (error: Error) => ReactNode` prop; without it the full-screen "Something went
  wrong" is unchanged) around the provider's `Card`, or `UnknownCard`. WatchCard's fallback is an in-card
  "Couldn't show this watch" with Remove (`removeWatch`); the rest of the page stays.
- **New watch** button (header on wide screens, floating on phones), disabled when `capacity.can_add` is false,
  with `capacity.reason` as its tooltip.
- **Empty state**: a card "Nothing watched yet" with one large button per offered provider, "Watch a ferry"
  (`/new/bcferries`) and "Watch Buntzen Lake" (`/new/buntzen`), each with its tile's tagline.

### 7.3 New watch (`mt/NewWatchSheet.tsx`, `mt/ProviderPicker.tsx`)

A bottom sheet on phones and a dialog on wide screens (`components/ui/sheet.tsx`).

**Step 1, the picker**, is skipped for `/new/<provider>`, for the empty state's buttons, and when only one
provider is offered:

- One tile per `state.providers` entry that is `offered` **and** known to the client registry (a provider the
  SPA doesn't know is never offered).
- Each tile shows the icon, name, description and tagline: BC Ferries "Can hold a sailing in a cart for you";
  Buntzen "Alert only: you book on Yodel".
- A tile whose `capacity.providers[id].can_add` is false is disabled and shows its `reason`.
- A paused provider's tile stays pickable and says "Checks are paused by an admin".
- The provider last used comes first (`localStorage` `wl.lastProvider`, in try/catch).

**Step 2, the provider's form** (`ui.Sheet`):

- **bcferries** → `providers/bcferries/BcferriesSheet.tsx`, which wraps the existing `TripSheetView`
  (route, date, party, vehicle, times, auto-hold) unchanged. Its `TripSource.save` calls
  `addWatch({provider: "bcferries", spec: <settings without enabled>, enabled})` and `updateWatch(id, {spec:
  <patch without enabled>, enabled?})`.
- **buntzen** → `providers/buntzen/BuntzenSheet.tsx`:
  - "Which day?" chips: **Today** (only while some pass is still useful today, i.e. before 18:00 PT),
    **Tomorrow**, then the next five days as `Sat 3`, and **Other date** (a date input bounded by
    `horizon_days`).
  - "Which passes would do?" toggle chips: All day, Morning, Afternoon, all three on by default. At least one
    must stay on. Under the Morning chip: "Morning passes stop counting at noon on the day."
  - A switch, on by default: "Remind me at 6:55 am the day before (passes go on sale at 7:00)". It is shown only
    while that time is still ahead.
  - Out of season (`season.ts`, mirroring §5.5's `KNOWN_SEASONS` and `ESTIMATED_SEASON`): "That date is outside
    Buntzen's reservation season, so Yodel probably won't offer passes. We'll check every 30 minutes anyway."
  - If the user's quiet hours cover 06:55 or 07:00: "Your quiet hours (until {end}) cover the 7:00 release, so
    no reminder and no checks until then. Change them in Settings."
  - Help: "Waitlist only alerts you. You book on Yodel with your own account, licence plate and mobile number."
  - **Start watching** → `POST /api/watches`. On success the sheet closes and a toast "Watching Buntzen Lake for
    Sat Jul 3" offers **View** (`/w/<watch_id>`). Server errors land on their field (`date`, `passes`); a 409
    shows its message. Edit mode uses `PATCH` with only the changed keys.
  - Tap count from the dashboard: New watch, Buntzen Lake, Tomorrow, Start watching: **4**.

"Watch another day" (on a Buntzen card or watch page) opens this form with the same passes and the next day.

### 7.4 Cards and pages

**BC Ferries** (`providers/bcferries/BcferriesCard.tsx`): a provider header line (icon `Ship`, "BC FERRIES",
the date) above the existing `mt/MyTripCard.tsx`, which receives the watch itself (a `BcferriesWatch` is a
`MyTrip` plus fields). `MyTripCard`, `NotHeld`, `BookedQuestion`, `CheckoutPage` and `release.ts` keep their
logic and tests. Their remove and check calls move to `removeWatch`/`checkWatch`, and `MyTripCard`'s
Pause/Resume (today `myApi.updateTrip(id, {enabled})`) moves to `updateWatch(id, {enabled})`; hold, release,
dismiss, booked and checkout keep their `/api/trips/{id}/…` paths. There is **no behavioural change**.

**Buntzen** (`providers/buntzen/BuntzenCard.tsx`): header line (icon `Trees`, "BUNTZEN LAKE", `{day}`), then the
passes chosen, then a badge:

| Condition (first that applies) | Badge |
|---|---|
| a wanted option `open` | **Open now** (warning, pulsing) |
| a wanted option `maybe` | **May be open** (warning) |
| `waiting_release` | **On sale {Wed} 7:00 am** (soft) |
| `checking` | **Watching** (success-outline) |
| `pending` | **Starting** |
| `quiet` | **Quiet hours** |
| `paused` | **Paused** |
| `provider_paused` | **Paused by admin** |
| `unavailable` | **Checks not running** (soft) |
| `backoff` or `error` | **Problem** (danger) |
| `over` | **Over** (outline) |
| `unknown` | **Unknown** (outline) |

- **One row per option**: its label and a status chip (Open / May be open / Sold out / Not on sale yet / Not
  offered / Over / Not checked yet, or "Couldn't check" for `unchecked` while `check.outcome` is `error`, which
  is a partial failure, §4.5). Unwanted options are greyed and marked "not watched". For `maybe`, the chip
  has a tooltip "Yodel says: {detail}" (escaped; `detail` never appears anywhere else).
- **For open or maybe options**, a primary **Book on Yodel** link to its `booking_url` (`target="_blank"
  rel="noopener noreferrer"`), shown only if the URL passed the schema's prefix check. Beside it, **I booked
  it** → `POST /api/watches/{id}/done`, then a toast "Enjoy the lake."
- **Notes:**
  - `outcome === "no_inventory"` → "Yodel isn't offering passes for {day}. Buntzen's reservation season usually
    runs from mid-May to early September."
  - `waiting_release` → "Passes for {day} go on sale {Wed Jun 30} at 7:00 am. We'll start checking then" plus
    ", and remind you at 6:55" when `release_reminder`.
  - `error`/`backoff` → "We're having trouble checking Yodel; retrying {next}." (with `error_class ==
    "site_changed"`: "We can't read Yodel's answers right now. Keep an eye on Yodel yourself.")
  - `unavailable` → "Checks aren't running right now. Keep an eye on Yodel yourself."
  - `checking` → "Checked {time}, every {minute}."
- **Actions**: Check now (enabled for `checking`, `pending`, `error`), Pause/Resume (`PATCH {enabled}`), Edit,
  Remove (`RemoveTripDialog`, reworded generically: "Stop watching this?"), and Watch another day (when
  `over`).
- **Footer line**, muted: "Alert only. You book on Yodel with your own account; Waitlist never asks for it."
- **What alert-only cards never show**: a cart, holds, "Hold it for me", checkout, or "Did you book it?".

**Buntzen detail** (`providers/buntzen/BuntzenDetail.tsx`, at `/w/<id>`): the status line and the open options'
**Book on Yodel** buttons first and full width (that's what the alert was for). Then the three passes with
status and "checked {time}"; the timeline ("On sale Wed Jun 30 at 7:00 am; checked every 30 s for 20 minutes,
then every minute; morning passes count until noon"); "How booking works" (a Yodel account with your licence
plate and phone number; passes are tied to them and can't be transferred; Waitlist never asks for your Yodel
password); Edit, Pause, Watch another day, Remove; and "Back to your watches". A watch that isn't in the state:
"This watch isn't on your list any more", with a link home.

**Unknown** (`providers/unknown/UnknownCard.tsx`): "This watch is for {provider} and needs a newer version of
Waitlist. Reload the page." with **Reload** and **Remove** (`DELETE /api/watches/{id}`), and nothing else.

### 7.5 Client provider registry (`web/src/providers/`)

```ts
// web/src/providers/types.ts
import type { ComponentType } from "react";
import type { LucideIcon } from "lucide-react";
import type { Tone } from "@/mt/trips";
import type { ProviderInfo, Watch, WaitlistState } from "@/mt/watch-schema";

export type Group = "now" | "watching" | "paused" | "ended";
export type KnownKind = Exclude<Watch["kind"], "unknown">;

export interface WatchProps<W extends Watch> {
  watch: W;
  state: WaitlistState;
  info: ProviderInfo | undefined;
}

export type SheetMode<W extends Watch> = { kind: "adding"; seed?: Record<string, unknown> } | { kind: "editing"; watch: W };

export interface SheetProps<W extends Watch> {
  mode: SheetMode<W>;
  state: WaitlistState;
  info: ProviderInfo;
  onDone: (watchId: string | null) => void; // null: cancelled
}

export interface ProviderUi<W extends Watch = Watch> {
  kind: KnownKind;
  icon: LucideIcon;
  tagline: string;
  title(watch: W, ctx: { terminals?: readonly { code: string; label: string }[] }): string;
  date(watch: W): string; // ISO date, for sorting
  group(watch: W, now: number): Group;
  badge(watch: W, now: number): { text: string; tone: Tone };
  next(watch: W, now: number): { at: number; text: string } | null; // for the dashboard's "Next" hint
  Card: ComponentType<WatchProps<W>>;
  Detail: ComponentType<WatchProps<W>>;
  Sheet: ComponentType<SheetProps<W>>;
}
```

`web/src/providers/registry.ts` exports `providerUi(kind: string): ProviderUi | null`, backed by a `Map` (never
an object index: `"constructor"` must resolve to nothing). It holds entries for `bcferries` and `buntzen`;
anything else gets `UnknownCard`.

### 7.6 The zod contract (`web/src/mt/watch-schema.ts`, verbatim, contract commit; extend it, never loosen it)

The app switches from `MyStateSchema` to `WaitlistStateSchema` (in `mt/api.ts` and `mt/live.ts`, where
`WaitlistLiveMessageSchema` replaces `LiveMessageSchema`). `mt/schema.ts` keeps the BC Ferries sub-schemas this
file builds on, and `MyStateSchema` itself with its `trips` and `capacity` keys, because this file `.omit()`s
them. W3 may stop *using* `MyStateSchema`, but must not change its keys without an amendment.

```ts
/**
 * Waitlist's state: every watch on every provider (docs/waitlist/design.md §6, §7.6). Verbatim from the design
 * contract; extend it, never loosen it. A watch that doesn't parse (a provider this build doesn't know, or a shape
 * it doesn't expect) becomes an "unknown" watch rather than failing the whole state: a newer server, or one bad
 * row, can never blank the page.
 */
import { z } from "zod";
import { MyStateSchema, MyTripSchema } from "./schema";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
export const WATCH_ID = /^[a-f0-9]{16}$/;
export const PROVIDER_ID = /^[a-z][a-z0-9_]{1,31}$/;
const OPTION_KEY = /^[a-z0-9][a-z0-9_.:-]{0,31}$/;
const epoch = z.number().finite();

/** A status the server reports: one this build doesn't know reads as "unknown". */
function reported<const T extends string>(values: readonly [T, ...T[]]) {
  return z.enum([...values, "unknown"] as [T | "unknown", ...(T | "unknown")[]]).catch("unknown");
}

export const CapabilitiesSchema = z.object({
  auto_hold: z.boolean(),
  checkout: z.boolean(),
  alert_only: z.boolean(),
});

/** One entry of the provider catalog (GET /api/providers, and `providers` in every state). */
export const ProviderInfoSchema = z.object({
  id: z.string().regex(PROVIDER_ID),
  name: z.string().min(1),
  noun: z.string().min(1),
  description: z.string(),
  checker: reported(["browser", "http"]),
  capabilities: CapabilitiesSchema,
  horizon_days: z.number().int().positive(),
  offered: z.boolean(), // this user may start a new watch on it ("New watch" lists it)
  paused: z.boolean().default(false), // an admin paused its checks (or maintenance is on)
});

/** A BC Ferries trip: 0.7.1's trip entry exactly (trip, rev, paused_reason, quiet, hold, browser, booking_url),
 * plus the fields every watch has. `trip.id` and `trip.enabled` equal `id` and `enabled`. */
export const BcferriesWatchSchema = MyTripSchema.extend({
  provider: z.literal("bcferries"),
  id: z.string().regex(WATCH_ID),
  enabled: z.boolean(),
}).transform((w) => ({ ...w, kind: "bcferries" as const }));

export const OptionSchema = z.object({
  key: z.string().regex(OPTION_KEY),
  label: z.string().min(1).max(64),
  status: reported(["open", "maybe", "sold_out", "not_released", "not_offered", "over", "unchecked"]),
  wanted: z.boolean(),
  booking_url: z.string().url().startsWith("https://").nullable().catch(null).default(null),
  detail: z.string().max(64).nullable().catch(null).default(null),
});

/** Where an HTTP-checked watch's checking is, as the server worked it out (base.CheckState). */
export const CheckSchema = z.object({
  state: reported([
    "pending",
    "waiting_release",
    "checking",
    "error",
    "backoff",
    "quiet",
    "paused",
    "provider_paused",
    "unavailable",
    "over",
  ]),
  opens_at: epoch.nullable(),
  closes_at: epoch.nullable(),
  last_check_at: epoch.nullable(),
  checked_at: epoch.nullable(),
  next_check_at: epoch.nullable(),
  outcome: reported(["pending", "ok", "no_inventory", "error"]),
  error_class: z.string().nullable(),
  consecutive_errors: z.number().int().nonnegative(),
});

const common = {
  id: z.string().regex(WATCH_ID),
  rev: z.number().int(),
  enabled: z.boolean(),
  paused_reason: z.string().nullable(),
  quiet: z.boolean().default(false), // in the user's quiet hours and not lifted: not being checked
};

export const BUNTZEN_PASSES = ["full_day", "morning", "afternoon"] as const;
export type BuntzenPass = (typeof BUNTZEN_PASSES)[number];

export const BuntzenSpecSchema = z.object({
  date: z.string().regex(ISO_DATE),
  passes: z.array(z.enum(BUNTZEN_PASSES)).min(1).max(3),
  release_reminder: z.boolean(),
});

/** Only ever a Buntzen page on Yodel's portal: anything else reads as no link. */
export const BUNTZEN_BOOKING_PREFIX = "https://yodelportal.com/buntzen-lake/";
const buntzenUrl = z.string().url().startsWith(BUNTZEN_BOOKING_PREFIX).nullable().catch(null).default(null);

export const BuntzenWatchSchema = z
  .object({
    provider: z.literal("buntzen"),
    ...common,
    spec: BuntzenSpecSchema,
    check: CheckSchema,
    options: z.array(OptionSchema.extend({ booking_url: buntzenUrl })).max(20),
    booking_url: buntzenUrl,
  })
  .transform((w) => ({ ...w, kind: "buntzen" as const }));

/** What's left of a watch this build can't show: enough to list it and remove it. */
export const UnknownWatchSchema = z
  .object({
    provider: z.string().max(32),
    id: z.string().regex(WATCH_ID),
    rev: z.number().int().catch(0),
    enabled: z.boolean().catch(true),
  })
  .transform((w) => ({ ...w, kind: "unknown" as const }));

export type BcferriesWatch = z.output<typeof BcferriesWatchSchema>;
export type BuntzenWatch = z.output<typeof BuntzenWatchSchema>;
export type UnknownWatch = z.output<typeof UnknownWatchSchema>;
export type Watch = BcferriesWatch | BuntzenWatch | UnknownWatch;
export type WatchOption = z.output<typeof OptionSchema>;
export type WatchCheck = z.output<typeof CheckSchema>;
export type ProviderInfo = z.output<typeof ProviderInfoSchema>;

const KNOWN = new Map<string, z.ZodType<Watch, z.ZodTypeDef, unknown>>([
  ["bcferries", BcferriesWatchSchema],
  ["buntzen", BuntzenWatchSchema],
]);

/** One watch from the server: its provider's schema, else the unknown fallback, else null (dropped). */
export function parseWatch(raw: unknown): Watch | null {
  const provider = typeof raw === "object" && raw !== null ? (raw as { provider?: unknown }).provider : undefined;
  const schema = typeof provider === "string" ? KNOWN.get(provider) : undefined;
  if (schema) {
    const parsed = schema.safeParse(raw);
    if (parsed.success) return parsed.data;
  }
  const fallback = UnknownWatchSchema.safeParse(raw);
  return fallback.success ? fallback.data : null;
}

export const WatchListSchema = z
  .array(z.unknown())
  .transform((items) => items.map(parseWatch).filter((w): w is Watch => w !== null));

const ProviderCapacitySchema = z.object({
  can_add: z.boolean(),
  reason: z.string().nullable(),
  limit: z.number().int().nonnegative(),
  count: z.number().int().nonnegative(),
});

export const ProviderListSchema = z
  .array(ProviderInfoSchema.nullable().catch(null))
  .transform((items) => items.filter((p): p is ProviderInfo => p !== null));

export const WaitlistStateSchema = MyStateSchema.omit({ trips: true, capacity: true }).extend({
  watches: WatchListSchema,
  providers: ProviderListSchema,
  capacity: z.object({
    can_add: z.boolean(), // at least one offered provider has room
    reason: z.string().nullable(),
    providers: z.record(z.string(), ProviderCapacitySchema).default({}),
  }),
});

export type WaitlistState = z.output<typeof WaitlistStateSchema>;

export const WaitlistLiveMessageSchema = z.object({ type: z.literal("state"), state: WaitlistStateSchema });

/** GET /api/providers. */
export const ProvidersResponseSchema = z.object({ providers: ProviderListSchema });

/** POST /api/watches: the new watch's id, and the caller's whole state. */
export const CreatedWatchSchema = z.object({ watch_id: z.string().regex(WATCH_ID), state: WaitlistStateSchema });

/** The bodies the SPA sends (§6.2). */
export interface NewWatchRequest {
  provider: string;
  spec: Record<string, unknown>;
  enabled?: boolean;
}
export interface WatchPatchRequest {
  spec?: Record<string, unknown>;
  enabled?: boolean;
}
```

With it, the contract commit adds `web/src/mt/watch-schema.test.ts` (verbatim):

```ts
import { describe, expect, it } from "vitest";
import fixture from "@/test/fixtures/api-state.json";
import { CreatedWatchSchema, WaitlistStateSchema, parseWatch } from "./watch-schema";

describe("the Waitlist state contract", () => {
  it("parses the shared fixture", () => {
    const state = WaitlistStateSchema.parse(fixture);
    expect(state.watches.map((w) => w.kind)).toEqual(["buntzen", "buntzen", "bcferries", "unknown"]);
    const buntzen = state.watches[0];
    if (buntzen?.kind !== "buntzen") throw new Error("expected buntzen");
    expect(buntzen.options[0]?.status).toBe("open");
    expect(buntzen.booking_url).toBe("https://yodelportal.com/buntzen-lake/Half-Day-Pass");
    const waiting = state.watches[1];
    if (waiting?.kind !== "buntzen") throw new Error("expected buntzen");
    expect(waiting.check.state).toBe("waiting_release");
    const bcf = state.watches[2];
    if (bcf?.kind !== "bcferries") throw new Error("expected bcferries");
    expect(bcf.trip.from).toBe("TSA");
    expect(bcf.trip.id).toBe(bcf.id);
    expect(state.providers.map((p) => [p.id, p.offered])).toEqual([
      ["bcferries", true],
      ["buntzen", true],
    ]);
    expect(state.capacity.providers.buntzen?.limit).toBe(6);
  });
  it("never lets one bad watch blank the page", () => {
    expect(parseWatch({ provider: "buntzen", id: "0f3c9a1b2d4e5f60", rev: 1, enabled: true })?.kind).toBe("unknown");
    expect(parseWatch({ provider: "constructor", id: "0f3c9a1b2d4e5f60" })?.kind).toBe("unknown");
    expect(parseWatch({ nope: true })).toBeNull();
    expect(parseWatch(null)).toBeNull();
  });
  it("reads a check state or status this build doesn't know as unknown", () => {
    const raw = structuredClone(fixture.watches[0]) as Record<string, unknown> & { check: Record<string, unknown> };
    raw.check.state = "teleporting";
    const w = parseWatch(raw);
    expect(w?.kind === "buntzen" && w.check.state).toBe("unknown");
  });
  it("drops a booking link off Yodel's Buntzen pages", () => {
    const raw = structuredClone(fixture.watches[0]) as Record<string, unknown>;
    raw.booking_url = "https://evil.example/buntzen-lake/";
    const w = parseWatch(raw);
    expect(w?.kind === "buntzen" && w.booking_url).toBe(null);
  });
  it("parses a created watch", () => {
    expect(CreatedWatchSchema.parse({ watch_id: "0f3c9a1b2d4e5f60", state: fixture }).watch_id).toBe(
      "0f3c9a1b2d4e5f60",
    );
  });
});
```

### 7.7 API client (`mt/api.ts`): exactly the paths W3 calls

```ts
const watchPath = (id: string) => `/api/watches/${encodeURIComponent(id)}`;
const tripPath = (id: string) => `/api/trips/${encodeURIComponent(id)}`; // BC Ferries actions only

state: (signal?) => request("/api/state", { schema: WaitlistStateSchema, signal }),
providers: (signal?) => request("/api/providers", { schema: ProvidersResponseSchema, signal }),
terminals: …,                                                            // unchanged
addWatch: (body: NewWatchRequest) => request("/api/watches", { method: "POST", body, schema: CreatedWatchSchema }),
updateWatch: (id: string, patch: WatchPatchRequest) => mutate("PATCH", watchPath(id), patch),
removeWatch: (id: string) => mutate("DELETE", watchPath(id)),
checkWatch: (id: string) => mutate("POST", `${watchPath(id)}/check`),
doneWatch: (id: string) => mutate("POST", `${watchPath(id)}/done`),
hold, release, dismiss, booked, checkout: …                              // unchanged, on tripPath(id)
setQuietHours, setCheckoutViewer, subscribe, unsubscribe, testAlert, logout: …  // unchanged
```

`mutate` parses with `WaitlistStateSchema`. `addTrip`, `updateTrip`, `removeTrip` and the old `checkNow` are
removed from the client. `mt/admin.ts` adds `pauseProvider(provider, on, reason)` →
`POST /api/admin/switches {switch: "pause_provider", provider, on, reason}`. Its overview schema gains
`checks` (the §6.6 report: `nullish().catch(null)`, with every field `.catch`-tolerant) and `providers` (an
array, `.catch([])`).

### 7.8 Branding and PWA

- `index.html`: `<title>Waitlist</title>`, `apple-mobile-web-app-title` "Waitlist", description "Get an alert
  the moment a reservation opens up."
- `public/manifest.webmanifest`:
  - `name`/`short_name` "Waitlist"; `id` "/"; `theme_color` `#0b4f94` unchanged;
  - description "Alerts the moment a reservation opens up: BC Ferries sailings, Buntzen Lake passes and more.";
  - `shortcuts`: `{"name": "New Buntzen Lake watch", "short_name": "Buntzen Lake", "url": "/new/buntzen"}` and
    `{"name": "New BC Ferries watch", "short_name": "BC Ferries", "url": "/new/bcferries"}`;
  - icons: `/icon.svg` (`"sizes": "any"`, purpose `any`) first, then the four PNGs.
- **Icons, decided.** A new mark, a bell on the existing blue tile; the ferry stays only as BC Ferries' card
  icon:
  ```svg
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#0b4f94"/><g fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.5 21.5V15a5.5 5.5 0 0 1 11 0v6.5"/><path d="M8.5 21.5h15"/><path d="M14.5 25h3"/><path d="M16 7.5v2"/></g></svg>
  ```
  It ships as `public/favicon.svg` and `public/icon.svg`. W3 regenerates `icon-192.png`, `icon-512.png`,
  `icon-maskable-192.png`/`-512.png` (with 20 % padding) and `apple-touch-icon.png` (180) with
  `web/scripts/render-icons.py`. That script uses the repo's Playwright Chromium (`uv run --extra browser
  python web/scripts/render-icons.py`) and must pass `ruff`. The PNGs are committed.
- `public/sw.js`: **not edited**. The same-origin rule is unchanged (I8), and its default title is already
  "Waitlist" from the foundation.
- `SignedOut` reads "Waitlist" / "Get an alert the moment a reservation opens: BC Ferries sailings and Buntzen
  Lake passes."

### 7.9 Admin

`AdminPage` gains a **Providers** panel built from `overview.providers` and `overview.checks`. For each
provider it shows: offer, running or paused, the fetcher's state (with "down since"), budget against demand a
minute, fetches and errors in the last hour, the last success and last error, any cooldown, and the counts. A
**Pause checks**/**Resume checks** switch asks for a reason, like the others. The limits editor labels
`maxAlertOnlyWatches` "Alert-only watches". The attention list shows `site_changed` and `checker_down` with the
§6.5 texts. The user list shows watches by provider.

### 7.10 Files (W3)

**Create:**
- `web/src/mt/{Dashboard.tsx, WatchCard.tsx, NewWatchSheet.tsx, ProviderPicker.tsx, WatchPage.tsx, AlertsBanner.tsx, watches.ts}`
- `web/src/providers/{types.ts, registry.ts}`
- `web/src/providers/bcferries/{index.ts, BcferriesCard.tsx, BcferriesSheet.tsx, BcferriesDetail.tsx}`
- `web/src/providers/buntzen/{index.ts, BuntzenCard.tsx, BuntzenSheet.tsx, BuntzenDetail.tsx, labels.ts, season.ts}`
- `web/src/providers/unknown/UnknownCard.tsx`
- `web/src/sw.test.ts`: loads `public/sw.js` into a fake `self` with `node:vm`. It asserts that a same-origin
  `url` opens that page, a cross-origin one opens `/`, and a payload without a title shows "Waitlist".
- `web/scripts/render-icons.py`, `web/public/icon.svg`
- a test beside each new component

**Change:**
- `web/src/mt/{MtApp.tsx, api.ts, live.ts, router.ts, CheckoutPage.tsx, AdminPage.tsx, admin.ts, AccountCard.tsx, MyTripCard.tsx, BookedQuestion.tsx, NotHeld.tsx, release.ts, trips.ts}`
- `web/src/components/{RemoveTripDialog.tsx, TripSheet.tsx}` (copy, and embedding in the sheet)
- `web/src/components/ErrorBoundary.tsx` (the optional `fallback` prop only; the default is unchanged)
- `web/src/test/fixtures.ts`
- `web/index.html`, `web/public/{manifest.webmanifest, favicon.svg, *.png}`
- The signed-out copy (§7.8) is `SignedOut` in `mt/MtApp.tsx`, already listed. `web/src/main.tsx` and
  `web/public/sw.js` are **unchanged** (`sw.js` already defaults to "Waitlist" and keeps its same-origin rule,
  I8; `sw.test.ts` pins both).
- the existing tests these touch, by fixture and path changes only

**Don't touch:** `web/src/mt/watch-schema.ts`, `web/src/mt/watch-schema.test.ts` and
`web/src/test/fixtures/api-state.json`, except by amendment, and anything outside `web/`.

---

## 8. home-cluster: the full rename, the fetcher, the cutover (W4)

### 8.1 Layout and names

```
kubernetes/apps/waitlist/
├── kustomization.yaml          # namespace: waitlist; components: [../../components/common]; resources: [./waitlist/ks.yaml]
└── waitlist/
    ├── README.md               # 1Password fields, values, key rotation, the fetcher, and this runbook (§8.7)
    ├── ks.yaml                 # Kustomization "waitlist" (namespace waitlist); components [../../../../components/cnpg];
    │                           # postBuild.substitute APP: waitlist; dependsOn onepassword-store (external-secrets),
    │                           # cloudnative-pg-cluster (database); path ./kubernetes/apps/waitlist/waitlist/app;
    │                           # targetNamespace waitlist; interval 30m; timeout 15m; wait true; prune true
    └── app/
        ├── kustomization.yaml  # resources: ./externalsecret.yaml, ./helmrelease.yaml (+ ./legacy-redirects.yaml from PR-C)
        ├── externalsecret.yaml # Secret "waitlist": the foundation's deploy/externalsecret.yaml, unchanged
        ├── helmrelease.yaml    # HelmRelease "waitlist": the foundation's renamed deploy/helmrelease.yaml + §8.2,
        │                       # with streamViewer "admins" (0.7.1's live value), offer "admins", bootstrapAdmins,
        │                       # the 1.0.0 digests, and vncPubkeys = the public half printed by `waitlist-web keys`
        │                       # for the NEW WAITLIST_CHECKOUT_KEY (§8.4), all filled in at Phase 0 step 6
        └── legacy-redirects.yaml  # PR-C: two plain HTTPRoutes for the ferry hostnames (§8.2); not in the HelmRelease
```

| Thing | Old | New |
|---|---|---|
| Namespaces | `ferry-watch`, `ferry-watch-browsers` | `waitlist`, `waitlist-browsers` |
| HelmRelease / `app.kubernetes.io/instance` | `ferry-watch` | `waitlist` |
| Controllers | ferry-web, ferry-control, vnc-edge, db (`ferry-db`) | waitlist-web, waitlist-control, **waitlist-fetcher**, vnc-edge, db (`waitlist-db`) |
| Database, owner, login roles | `ferrywatch`, `ferrywatch`, `ferrywatch_web`/`_control` | `waitlist`, `waitlist`, `waitlist_web`/`_control` |
| Cluster-scoped objects (VAPs and bindings, ClusterRoles and bindings, PriorityClass) | `fw-*`, `ferry-watch-*`, `ferry-browser` | `wl-*`, `waitlist-*`, `waitlist-browser` |
| Hosts | `ferry.chestr.dev`, `ferry-checkout.chestr.dev` | `waitlist.chestr.dev`, `waitlist-checkout.chestr.dev` |
| Seccomp profile (node file, `sandbox: false`) | `profiles/fw-chromium.json` | `profiles/wl-chromium.json` (the foundation's render) |

Both releases coexist during the cutover. **Every cluster-scoped object must have a new name, and no admission
policy may reach a `ferry*` namespace.** The foundation's three VAPs scope themselves in two different ways,
and both stay as they are:

- `wl-browser-pod` and `wl-browser-pod-lock`: the policy's `matchConstraints.namespaceSelector` and the
  binding's `matchResources.namespaceSelector` both select only `kubernetes.io/metadata.name:
  waitlist-browsers`.
- `wl-ns-objects`: the policy's `matchConstraints.namespaceSelector` selects exactly `kubernetes.io/metadata.name
  In [waitlist, waitlist-browsers]`, and its binding has no `matchResources`. Its expression names
  `system:serviceaccount:waitlist:` and `system:serviceaccount:waitlist-browsers:`. It must keep covering the
  `waitlist` namespace: that is what stops control's ServiceAccount from creating Secrets, Roles or HTTPRoutes
  there.

W4 asserts exactly this in `tests/test_deploy.py` (`test_admission_policies_reach_only_waitlist`): no rendered
cluster-scoped name contains `ferry` or `fw-`; no VAP or binding selects a namespace whose name starts with
`ferry`; the two pod policies and their bindings select only `waitlist-browsers`; `wl-ns-objects`' policy
selects exactly `[waitlist, waitlist-browsers]` and its expression names both ServiceAccount prefixes.

### 8.2 HelmRelease changes beyond the rename

**Values.** These are rendered into three configs from one place, the way `checkout.streamViewer` already is:

```yaml
waitlist:
  hosts:
    dashboard: waitlist.chestr.dev
    checkout: waitlist-checkout.chestr.dev
  providers:
    buntzen:
      # Who may create Buntzen watches: "off", "admins" or "everyone" (quoted: YAML reads a bare off as false).
      # "off" renders nothing about Buntzen: no fetcher URL or httpChecks for control, no offer for web.
      offer: "admins"
      requestsPerMinute: 12   # waitlist-control's budget (1-30)
      fetcherPerMinute: 30    # waitlist-fetcher's own hard cap (requestsPerMinute-30)
  fetcher:
    upstreamTimeoutSeconds: 10
```

What the render produces:

- It fails unless `offer` is one of the three values, unless `1 ≤ requestsPerMinute ≤ fetcherPerMinute ≤ 30`,
  and if anyone sets `waitlist.control.fetcher`, `waitlist.control.httpChecks` or `waitlist.web.providers` by
  hand (one source, as for `streamViewer`).
- With `offer` not `"off"`:
  - control's `config.json` gains `"fetcher": {"url": "http://waitlist-fetcher.waitlist.svc.cluster.local.:8767",
    "timeoutSeconds": 20}` and `"httpChecks": {"buntzen": {"requestsPerMinute": 12}}`. `migrate-config` shares
    the `&controlConfig` anchor, so it gets the same keys automatically; the W1 config classes must therefore be
    merged before W4 (§9.6).
  - web's `config.json` gains `"providers": {"buntzen": {"offer": "admins"}}`.
- With `"off"`, none of those keys appear, and both configs are BC Ferries-only.
- A new ConfigMap `fetcher-config` (`forceRename: waitlist-fetcher-config`, `includeChecksumInControllers:
  [waitlist-fetcher]`) holds `{"listenPort": 8767, "upstreamTimeoutSeconds": 10, "maxBodyBytes": 262144,
  "providers": {"buntzen": {"maxPerMinute": 30}}}` (`"providers": {}` when off). The fetcher Deployment is
  always rendered.

**The fetcher controller:**

```yaml
    controllers:
      waitlist-fetcher:
        forceRename: waitlist-fetcher
        strategy: RollingUpdate      # Kubernetes' 25 % defaults round to surge 1 / unavailable 0 for one replica
        serviceAccount:
          identifier: waitlist-fetcher
        pod:
          automountServiceAccountToken: false
          dnsConfig:
            options: [{ name: ndots, value: "1" }]   # api.yodelpass.com resolves as itself, not via search domains
        containers:
          app:
            image: *image                            # ghcr.io/mchestr/waitlist-control:1.0.0@sha256:…
            args: [waitlist-fetcher, run]
            env:
              WL_FETCHER_CONFIG: /etc/waitlist-fetcher/config.json
              WL_LOG_LEVEL: *logLevel
            ports:
              - name: http
                containerPort: &fetcherPort 8767
            probes:
              liveness:
                enabled: true
                custom: true
                spec:
                  httpGet: { path: /livez, port: *fetcherPort }
                  periodSeconds: 15
                  timeoutSeconds: 2
                  failureThreshold: 3
              readiness:
                enabled: true
                custom: true
                spec:
                  httpGet: { path: /readyz, port: *fetcherPort }
                  periodSeconds: 10
                  timeoutSeconds: 2
                  failureThreshold: 3
            resources:
              requests: { cpu: 10m, memory: 64Mi }
              limits: { memory: 192Mi }
            securityContext: *securityContext
    serviceAccount:
      waitlist-fetcher:
        forceRename: waitlist-fetcher
        automountServiceAccountToken: false
    service:
      waitlist-fetcher:
        forceRename: waitlist-fetcher
        controller: waitlist-fetcher
        ports:
          http:
            port: *fetcherPort
    persistence:
      fetcher-config:
        type: configMap
        identifier: fetcher-config
        advancedMounts:
          waitlist-fetcher:
            app:
              - path: /etc/waitlist-fetcher
                readOnly: true
      tmp:   # add the fetcher to the existing emptyDir's advancedMounts
        advancedMounts:
          waitlist-fetcher:
            app:
              - path: /tmp
```

The anchor names `*image`, `*logLevel` and `*securityContext` are whatever the foundation's file already uses
for waitlist-control's container; use those. The fetcher has no `PG*` env, no secret volume and no RBAC.
`tests/test_deploy.py` asserts all three, for the rendered Deployment and its ServiceAccount.

**Network policies.** The release's `default-deny` (podSelector instance `waitlist`) already covers the
fetcher.

```yaml
    networkpolicies:
      waitlist-fetcher:
        forceRename: waitlist-fetcher
        type: native
        controller: waitlist-fetcher
        policyTypes: [Ingress, Egress]
        rules:
          ingress:
            - from:
                - podSelector:
                    matchLabels:
                      app.kubernetes.io/instance: *app
                      app.kubernetes.io/controller: waitlist-control
              ports:
                - protocol: TCP
                  port: *fetcherPort
          egress: []   # DNS and HTTPS only by name: the Cilium policy below
      waitlist-fetcher-cilium:
        forceRename: waitlist-fetcher-cilium
        type: cilium
        controller: waitlist-fetcher
        egress:
          - toEndpoints:
              - *kubeDns
            toPorts:
              - ports:
                  - port: "53"
                    protocol: ANY
                rules:
                  dns:
                    - matchName: api.yodelpass.com
          - toFQDNs:
              - matchName: api.yodelpass.com
            toPorts:
              - ports:
                  - port: "443"
                    protocol: TCP
        # Defence in depth beside the fetcher's PublicOnlyResolver: whatever api.yodelpass.com resolves to, never
        # the LAN, CGNAT, link-local or loopback. (Cilium's CIDR rules don't select cluster endpoints, so DNS to
        # kube-dns is unaffected; Phase S proves it.)
        egressDeny:
          - toCIDRSet:
              - cidr: 10.0.0.0/8
              - cidr: 172.16.0.0/12
              - cidr: 192.168.0.0/16
              - cidr: 100.64.0.0/10
              - cidr: 169.254.0.0/16
              - cidr: 127.0.0.0/8
              - cidr: fc00::/7
              - cidr: fe80::/10
```

Add this to the existing `waitlist-control` native policy's `egress`. Nothing else changes; control still has
no internet:

```yaml
            - to:
                - podSelector:
                    matchLabels:
                      app.kubernetes.io/instance: *app
                      app.kubernetes.io/controller: waitlist-fetcher
              ports:
                - protocol: TCP
                  port: *fetcherPort
```

waitlist-web gets no new egress: it never talks to Yodel or the fetcher. The browser-namespace policies change
only by the rename.

**Routes.**

- `waitlist-web` serves `waitlist.chestr.dev` and `vnc-edge` serves `waitlist-checkout.chestr.dev`, both on
  envoy-external, with 0.7.1's header-stripping filters.
- Homepage annotations: name `Waitlist`, icon `mdi-bell-ring`.

**Legacy redirects (PR-C only).** They are a plain file in home-cluster,
`kubernetes/apps/waitlist/waitlist/app/legacy-redirects.yaml`, added to `app/kustomization.yaml`'s `resources`
by PR-C and applied by the `waitlist` Flux Kustomization (targetNamespace `waitlist`, `prune: true`). They are
**not** in the HelmRelease, so:

- home-cluster's `helmrelease.yaml` stays the app repo's copy (§8.5), and the app's copy never names a ferry
  hostname;
- merging PR-C, PR-D's 301 edit, or R2's revert of PR-C never upgrades the Helm release mid-cutover (no db
  hook re-run, no rollout);
- `prune: true` removes them when PR-C is reverted (R2).

Merged while the old routes still exist, they are newer, and Gateway API gives identical matches to the
*oldest* route, so the old routes keep serving until they are deleted (C9). external-dns (`policy: sync`,
source `gateway-httproute`, `--gateway-name=envoy-external`) sees the ferry hostnames on a route throughout, so
the DNS records are never deleted.

```yaml
---
# Ferry Watch's old hostnames, redirected to Waitlist (§8.7 C9). 302 until PR-D makes them 301: a cached 301
# would defeat a rollback.
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: waitlist-legacy-dashboard
  annotations:
    gatus.home-operations.com/enabled: "false"
spec:
  parentRefs: [{ name: envoy-external, namespace: networking, sectionName: https }]
  hostnames: [ferry.chestr.dev]
  rules:
    - filters:
        - type: RequestRedirect
          requestRedirect:
            scheme: https
            hostname: waitlist.chestr.dev
            statusCode: 302
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: waitlist-legacy-checkout
  annotations:
    gatus.home-operations.com/enabled: "false"
spec:
  parentRefs: [{ name: envoy-external, namespace: networking, sectionName: https }]
  hostnames: [ferry-checkout.chestr.dev]
  rules:
    - filters:
        - type: RequestRedirect
          requestRedirect:
            scheme: https
            hostname: waitlist.chestr.dev
            path: { type: ReplaceFullPath, replaceFullPath: / }   # old checkout links are dead anyway
            statusCode: 302
```

The Cloudflare tunnel's `*.chestr.dev` ingress (`networking/cloudflared/app/resources/config.yaml`) already
covers the new hosts, and external-dns creates their records from the routes. There is no DNS Terraform change.

### 8.3 Outside the app directory

During coexistence every per-namespace rule lists **both** the old and the new names. The PR column says when
each change lands.

| File | Change | PR |
|---|---|---|
| `kubernetes/apps/database/cloudnative-pg/cluster/cluster17.yaml` | Add managed roles `waitlist_web` (comment "Waitlist waitlist-web") and `waitlist_control` ("Waitlist waitlist-control"), each `ensure: present`, `login: true`, `connectionLimit: 20`, `passwordSecret` `waitlist-web-role` / `waitlist-control-role`. | A |
| same | The `ferrywatch_*` roles become `ensure: absent` (never just deleted, which would orphan LOGIN roles). | D |
| same | Remove the `ensure: absent` entries. | a release after D |
| `kubernetes/apps/database/cloudnative-pg/roles/externalsecret.yaml` | Add ExternalSecrets `waitlist-web-role` and `waitlist-control-role` (`kubernetes.io/basic-auth`, `cnpg.io/reload: "true"`, from `waitlist_web_password` / `waitlist_control_password`). Remove the ferrywatch ones in D. | A, D |
| `kubernetes/apps/default/authelia/app/externalsecret.yaml` | Add `WAITLIST_OAUTH_CLIENT_ID: "{{ .WAITLIST_OAUTH_CLIENT_ID }}"` and `WAITLIST_OAUTH_CLIENT_SECRET: "{{ .WAITLIST_OAUTH_CLIENT_SECRET_HASH }}"`. Remove the FERRY_WATCH ones in D. | A, D |
| `kubernetes/apps/default/authelia/app/resources/configuration.yaml` | Add authorization policy `waitlist` (`default_policy: deny`; rule `one_factor` for `group:waitlist` and `group:admin`). Add client "Waitlist", guarded by `{{- if env "WAITLIST_OAUTH_CLIENT_ID" }}`: 0.7.1's client with `authorization_policy: waitlist` and `redirect_uris: [https://waitlist.chestr.dev/auth/callback]`, keeping `public: false`, `require_pkce: true`, `pkce_challenge_method: S256`, `consent_mode: pre-configured`, `pre_configured_consent_duration: 1y`, the four scopes, `response_types: [code]`, `response_modes: [fragment]`, `grant_types: [authorization_code]`, `userinfo_signed_response_alg: none` and `claims_policy: default`. No `sector_identifier_uri`, so the subject matches (§11 Q3). Remove the ferry-watch client and policy in D. | A, D |
| `kubernetes/apps/default/authelia/app/README.md` (line 24) | Its "complete worked example" link becomes `kubernetes/apps/waitlist/waitlist/README.md`, so it doesn't dangle after D. | A |
| lldap (UI, not git) | Create group `waitlist` and add the `ferry-watch` group's members. Delete `ferry-watch` in D. | Phase 0, D |
| `kubernetes/apps/default/smtp-relay/app/helmrelease.yaml` | Add an ingress rule for `app.kubernetes.io/controller: waitlist-web`, `app.kubernetes.io/instance: waitlist` in namespace `waitlist`. Remove the ferry rule in D. | A, D |
| `kubernetes/apps/observability/silence-operator/app/helmrelease.yaml` | Add a Silence for namespace `waitlist-browsers`, a copy of the ferry one. Remove the old one in D. | A, D |
| `kubernetes/apps/observability/fluent-bit/app/helmrelease.yaml` | Add `exclude_path /var/log/containers/*_waitlist-browsers_*.log`. The fetcher's logs are collected: they hold no secrets and no bodies. Remove the old line in D. | A, D |
| `kubernetes/apps/kube-system/descheduler/app/helmrelease.yaml` | Add `waitlist-browsers` to the excluded namespaces. Remove the old one in D. | A, D |
| `kubernetes/apps/kube-system/cilium/app/resources/values.yaml` | Comment only: "(Waitlist's)". | D |
| `.github/renovate/groups.json5` | Add group "Waitlist": `matchPackageNames: ["/^ghcr\\.io/mchestr/waitlist-(control|browser|vnc-edge)$/"]`, the same shape as Ferry Watch's. Remove Ferry Watch's in D. | A, D |
| `.github/renovate/autoMerge.json5` | Add `"/waitlist/"` to the never-automerge list (DB migrations). Remove `"/ferry-watch/"` in D. | A, D |
| `terraform/cloudflare/rulesets.tf` | The host list becomes `{"waitlist.chestr.dev" "waitlist-checkout.chestr.dev" "ferry.chestr.dev" "ferry-checkout.chestr.dev"}`, description "Waitlist: leave the HTML alone". `task terraform:cf:apply` right after the merge, before anyone uses the new host (Rocket Loader and email obfuscation break the CSP hashes). The ferry hosts stay until D, when only redirects answer on them. | A, D |
| `AGENTS.md`, `docs/src/notes/application-deployment.md`, `docs/src/notes/networking.md` | Add `waitlist` to the namespace lists and reference implementations. Remove `ferry-watch` in D. | A, D |
| `media/subs`, `media/jellyseerr`, `default/immich`, `default/radicale`, `default/saidcompany` helmreleases | Comments "(see ferry-watch)" become "(see waitlist)". | D |

### 8.4 1Password (vault `kubernetes`)

Values never reach the screen: they are passed to `op` as arguments, as in the 0.7.1 README. **Create the item
before editing it.**

| Item | Field | Source |
|---|---|---|
| `waitlist` (**new item**: `op item create --vault kubernetes --category password --title waitlist >/dev/null`) | `WAITLIST_CHECKOUT_KEY` | **New**, from `waitlist-web keys` (`k1`). Its public half (the last line of that output, not a secret) goes into `waitlist.control.pod.vncPubkeys` in both copies of the HelmRelease (§8.7 Phase 0 steps 6-7), replacing 0.7.1's; Phase S step 4 checks the pod got it. No old browser pod survives the cutover, so rotating costs nothing. |
| `waitlist` | `WAITLIST_VAPID_KEY` | **Copied** from `ferry-watch/FERRY_WATCH_VAPID_KEY`: `op item edit waitlist --vault kubernetes "WAITLIST_VAPID_KEY[password]=$(op read "op://kubernetes/ferry-watch/FERRY_WATCH_VAPID_KEY")" >/dev/null`. Existing push subscriptions are bound to this key, so they keep delivering until they are deleted in D (§11 Q4). |
| `authelia` | `WAITLIST_OAUTH_CLIENT_ID`, `WAITLIST_OAUTH_CLIENT_SECRET`, `WAITLIST_OAUTH_CLIENT_SECRET_HASH` | Generated (`authelia crypto rand` / `authelia crypto hash generate`), exactly as the 0.7.1 README does. A **new** secret. |
| `cloudnative-pg` | `waitlist_postgres_username` = `waitlist`, `waitlist_postgres_password`, `waitlist_web_password`, `waitlist_control_password` | generated |

The fetcher has **no** field.

### 8.5 What W4 also changes in the app repo

`deploy/helmrelease.yaml` is home-cluster's `kubernetes/apps/waitlist/waitlist/app/helmrelease.yaml`, **identical
except two lines**, each a deployment's own choice:

| Line | App copy | home-cluster |
|---|---|---|
| `streamViewer:` | `"off"` (asserted by `test_the_streamed_viewer_is_one_of_its_three_values`) | `"admins"` |
| `offer:` (under `waitlist.providers.buntzen`) | `"off"` | `"admins"` |

W4 extends `tests/test_deploy.py`'s mask (renaming `_STREAM_VIEWER` to `_DEPLOYMENT_CHOICES`) so
`test_home_cluster_runs_this_copy` ignores both lines and nothing else. Both lines carry no trailing comment,
because the mask matches the whole line:

```python
# Who is offered the streamed viewer and each provider is each deployment's own choice: the lines the copies may
# differ in.
_DEPLOYMENT_CHOICES = re.compile(r'^( +(?:streamViewer|offer): )"(?:off|admins|everyone)"$', re.MULTILINE)
```

and adds `test_providers_are_off_in_this_copy` beside the streamViewer test (the app copy's every
`waitlist.providers.*.offer` is `"off"`). Because the app copy is off, the render tests that need Buntzen on
render with `tests.deploy.render(helm, overrides={"waitlist": {"providers": {"buntzen": {"offer": "admins"}}}})`;
the `offer: "off"` assertions render the copy as is.

**What lands when.** W4's app-repo branch merges before the release (§9.6), so it can't know the 1.0.0
digests or the new `vncPubkeys`: it carries the foundation's placeholders, which `TEST_VALUES` overrides in
every render anyway. The real values exist only after Phase 0 steps 2-4, and are written into **both** copies
at Phase 0 steps 6-7 (§8.7). Until step 7, `test_home_cluster_runs_this_copy` is expected to fail against a
PR-A checkout; it is skipped in CI, where `WL_HOME_CLUSTER` is unset. W4 also reconciles
`deploy/externalsecret.yaml`, `tests/deploy.py`, `tests/test_deploy.py` (§10) and `tests/apiserver/**`, where
the new names, the fetcher and the policies above all appear. The legacy redirects are home-cluster-only
(§8.2), so they never touch the app copy.

### 8.6 The PRs

| PR | Repo | Content |
|---|---|---|
| **PR-A** | home-cluster | `kubernetes/apps/waitlist/**` (with the 1.0.0 digests), and every row of §8.3 marked A. Ferry Watch is untouched. |
| **PR-C** | home-cluster | `kubernetes/apps/waitlist/waitlist/app/legacy-redirects.yaml` and its line in `app/kustomization.yaml` (§8.2). Nothing else; the HelmRelease is untouched. |
| **PR-D** | home-cluster | Delete `kubernetes/apps/ferry-watch/`; every row of §8.3 marked D; `statusCode: 301` in `legacy-redirects.yaml` (the only edit to that file). |

PR-C and PR-D are prepared as separate branches when PR-A is. Their exact steps are in §8.7.

### 8.7 Cutover runbook

Shell helpers used throughout:

```sh
PRIMARY=$(kubectl -n database get cluster postgres17 -o jsonpath='{.status.currentPrimary}')
pg() { kubectl -n database exec -i "$PRIMARY" -c postgres -- "$@"; }
```

**Phase 0: before any cluster change.**

1. In GitHub settings, rename `mchestr/ferry-watch` to `mchestr/waitlist`.
2. On `main`, after W4's app-repo branch merges (§9.6), the orchestrator sets `version = "1.0.0"` in
   `pyproject.toml` and runs `uv lock`; only the `waitlist` package's own `version` line in `uv.lock` changes
   (check with `git diff --stat`: two files). Commit both together: CI (`uv sync --locked`), the Dockerfile
   (`uv sync --locked`) and the Release workflow all refuse a `pyproject.toml` whose version disagrees with
   `uv.lock`. Then run the Release workflow, which tags `v1.0.0` on main's head and pushes
   `ghcr.io/mchestr/waitlist-{control,browser,vnc-edge}:1.0.0`. Record each `<version>@sha256:<digest>` with
   `docker buildx imagetools inspect`.
3. **Make the three new GHCR packages public.** New packages start private, and the cluster pulls with no
   `imagePullSecrets`. In GitHub → Packages → each `waitlist-*` → Package settings → Change visibility →
   Public. Then verify an anonymous pull:
   ```sh
   for p in control browser vnc-edge; do
     t=$(curl -fsS "https://ghcr.io/token?scope=repository:mchestr/waitlist-$p:pull" | jq -r .token)
     curl -fsSI -H "Authorization: Bearer $t" -H 'Accept: application/vnd.oci.image.index.v1+json' \
       "https://ghcr.io/v2/mchestr/waitlist-$p/manifests/1.0.0" | head -1    # HTTP/2 200, three times
   done
   ```
4. Create the 1Password item and fields (§8.4). Check them by label only:
   ```sh
   for i in authelia cloudnative-pg waitlist; do op item get "$i" --vault kubernetes --format json \
     | jq -r --arg i "$i" '.fields[].label | select(test("WAITLIST|waitlist")) | "\($i): \(.)"'; done
   ```
5. In lldap, create the group `waitlist` and copy in the `ferry-watch` group's members.
6. Fill in PR-A's `helmrelease.yaml`: the three digests, `bootstrapAdmins`, and `vncPubkeys` = the
   `k1:<base64url>` public half that `waitlist-web keys` printed (its last line) when step 4 made
   `WAITLIST_CHECKOUT_KEY`. The foundation's copy still carries 0.7.1's `k1:ODYa…`, which belongs to
   `FERRY_WATCH_CHECKOUT_KEY`: left in place, browser pods would reject every ticket waitlist-web mints, and
   BC Ferries checkout would break. flux-local renders green for PR-A, PR-C and PR-D.
7. Bring the app repo's copy level (no release needed): copy PR-A's `helmrelease.yaml` over the app repo's
   `deploy/helmrelease.yaml`, set its `streamViewer:` and `offer:` lines back to `"off"` (§8.5), and commit to
   `main` (`chore: deploy copy at 1.0.0`). With `WL_HOME_CLUSTER` pointing at the PR-A checkout,
   `uv run pytest tests/test_deploy.py -k home_cluster` passes.

**PR-A: Waitlist beside Ferry Watch, with no user impact.** Merge it, then run `task terraform:cf:apply`
immediately. Flux creates `waitlist` and `waitlist-browsers`, the empty `waitlist` DB (the db hook runs
0001-0008 and the grants), the workloads, the fetcher, and the new Authelia client.

**Phase S: smoke test (the owner only, about 20 minutes).**

1. Sign in at `https://waitlist.chestr.dev`, then check that Authelia gives the new client the same subject:
   ```sh
   for db in ferrywatch waitlist; do pg psql -At -d "$db" -c "SELECT iss, sub FROM identities WHERE username = 'mike'"; done
   ```
   Both lines must match. If they don't, stop: add an `identity.aliases` entry that maps the new subject to the
   old user id before going on (§11 Q3).
2. Open Admin. The Fleet page shows control at 1.0.0. The Providers panel shows buntzen `running`, with the
   fetcher `ok`.
3. Add a Buntzen watch for tomorrow. Before 07:00 it shows "On sale … 7:00 am"; after, it checks. Today
   (out of season) the expected result is **`no_inventory`** with the out-of-season note, or plain `sold_out`.
   Both prove DNS, the egress policies (including `egressDeny`), the resolver and the parser's envelope.
   `kubectl -n waitlist logs deploy/waitlist-fetcher` shows `result=ok`.
   - `site_changed` means the real envelope differs. Capture it (§5.8), fix the parser and release 1.0.1
     **before** cutover, or set `offer: "off"` and go ahead with BC Ferries only.
   - `fetcher_unavailable` (the card says "Checks not running") points at the network policies. If DNS is the
     cause, drop the `egressDeny` block (the resolver check remains) and repeat.
4. Add a BC Ferries trip. A `wl-<id>-g1` pod starts in `waitlist-browsers` and its first check completes. That
   proves the VAPs, quotas and egress under the new names. While it runs, check that the pod trusts the key
   waitlist-web signs with (no secret is printed; run it from the app repo):
   ```sh
   want=$(op read "op://kubernetes/waitlist/WAITLIST_CHECKOUT_KEY" | uv run python -c '
   import base64, sys
   from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
   from waitlist.web.config import checkout_key
   kid, key = checkout_key(sys.stdin.read().strip(), "k1")
   pub = base64.urlsafe_b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).rstrip(b"=")
   print(kid + ":" + pub.decode())')
   have=$(kubectl -n waitlist-browsers get pods -l app.kubernetes.io/name=waitlist-browser \
     -o jsonpath='{.items[0].spec.containers[*].env[?(@.name=="WL_VNC_PUBKEYS")].value}')
   case ",$have," in *",$want,"*) echo "pubkey ok" ;; *) echo "MISMATCH: fix vncPubkeys before C1" ;; esac
   ```
   It must print `pubkey ok`. If a sailing can be carted, also open the held cart's checkout once. Remove the
   trip.
5. Settings → turn on alerts on this device → the test alert arrives.

**Phase C: cutover (announce about 30 minutes of downtime).**

- **C1. Drain the old app.** In Ferry Watch: Admin → Maintenance on. Wait until no pods remain (holds end within
  their 60-minute hard cap, or use Admin → Release): `kubectl -n ferry-watch-browsers get pods`. Then
  `pg psql -At -d ferrywatch -c "SELECT count(*) FROM browser_pods WHERE ended_at IS NULL"` must print `0`.
- **C2. Freeze the old app.**
  ```sh
  flux suspend kustomization ferry-watch -n ferry-watch && flux suspend helmrelease ferry-watch -n ferry-watch
  kubectl -n ferry-watch scale deployment ferry-web ferry-control vnc-edge --replicas=0
  kubectl -n ferry-watch-browsers delete pods --all --wait
  ```
- **C3. Freeze the new app.**
  ```sh
  flux suspend kustomization waitlist -n waitlist && flux suspend helmrelease waitlist -n waitlist
  kubectl -n waitlist scale deployment waitlist-web waitlist-control waitlist-fetcher vnc-edge --replicas=0
  kubectl -n waitlist-browsers delete pods --all --wait
  ```
- **C4. Replace the smoke-test database.** Guarded: it refuses if real users are there.
  ```sh
  pg psql -v ON_ERROR_STOP=1 -d waitlist -c "DO \$\$ BEGIN IF (SELECT count(*) FROM users) > 1 THEN RAISE EXCEPTION 'waitlist has real users: stop'; END IF; END \$\$"
  pg psql -v ON_ERROR_STOP=1 -d postgres -c 'DROP DATABASE waitlist WITH (FORCE)' -c 'CREATE DATABASE waitlist OWNER waitlist'
  ```
- **C5. Copy, inside the primary pod.** No dump file is written and no data leaves the pod.
  ```sh
  pg bash -c 'set -euo pipefail; { echo "SET ROLE waitlist;"; pg_dump --no-owner --no-privileges -d ferrywatch; } | psql -v ON_ERROR_STOP=1 --single-transaction -q -d waitlist'
  ```
  It's all or nothing (`--single-transaction`). On any error, `DROP DATABASE waitlist` / `CREATE DATABASE
  waitlist OWNER waitlist` and repeat, or roll back (R1).
- **C6. Clean up what doesn't carry over** (as superuser, before 0008 adds its triggers):
  ```sh
  pg psql -v ON_ERROR_STOP=1 --single-transaction -d waitlist \
    -c "UPDATE sessions SET revoked_at = now(), revoked_reason = 'cutover' WHERE revoked_at IS NULL" \
    -c "UPDATE browser_pods SET ended_at = now(), phase = 'gone', end_reason = 'cutover' WHERE ended_at IS NULL" \
    -c "DELETE FROM hold_claims" \
    -c "UPDATE trip_state SET cart = NULL, viewer_connected = false WHERE cart IS NOT NULL OR viewer_connected" \
    -c "UPDATE trip_runtime SET hold_requested_until = NULL WHERE hold_requested_until IS NOT NULL" \
    -c "UPDATE notifications SET sent_at = now(), last_error = 'cutover' WHERE sent_at IS NULL" \
    -c "UPDATE fleet_state SET value = 'true' WHERE key = 'maintenance'"
  ```
- **C7. Migrate and start.** The forced upgrade runs the db hook: postgres-init is a no-op, then `waitlist-control
  migrate` applies 0007 and 0008 and runs `grant()`. Then scale explicitly; don't rely on Helm to undo the
  manual scale-down:
  ```sh
  flux resume helmrelease waitlist -n waitlist && flux reconcile helmrelease waitlist -n waitlist --force
  flux resume kustomization waitlist -n waitlist
  kubectl -n waitlist scale deployment waitlist-web waitlist-control waitlist-fetcher --replicas=1
  kubectl -n waitlist scale deployment vnc-edge --replicas=2
  kubectl -n waitlist rollout status deployment/waitlist-control deployment/waitlist-web deployment/waitlist-fetcher
  ```
- **C8. Verify.** Each command must print what its comment says.
  ```sh
  pg psql -At -d waitlist -c "SELECT string_agg(version, ' ' ORDER BY version) FROM schema_version"   # ends … 0007_waitlist_names 0008_providers
  pg psql -At -d waitlist -c "SELECT provider, count(*) FROM trips GROUP BY 1"                          # bcferries|<n> only
  pg psql -At -d waitlist -c "SELECT count(*) FROM pg_policy p, unnest(p.polroles) r WHERE r::regrole::text LIKE 'ferrywatch%'"   # 0
  pg psql -At -d waitlist -c "SELECT string_agg(DISTINCT tableowner, ',') FROM pg_tables WHERE schemaname = 'public'"           # waitlist
  pg psql -At -d waitlist -c "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee LIKE 'ferrywatch%'"        # 0
  for t in users trips notifications events push_subscriptions hold_outcomes; do
    printf '%s ' "$t"; for db in ferrywatch waitlist; do pg psql -At -d "$db" -c "SELECT count(*) FROM $t" | tr '\n' ' '; done; echo
  done                                                                                                    # equal pairs
  ```
  Then:
  - sign in at `waitlist.chestr.dev`: your BC Ferries trips are there;
  - Admin → **Maintenance off**;
  - your nearest trip gets a `wl-` warm pod, and its first check completes;
  - a Buntzen watch checks.
- **C9. Switch the hosts, with no DNS gap.**
  1. Merge **PR-C**, then `flux reconcile kustomization waitlist -n waitlist --with-source`. The redirect routes
     `waitlist-legacy-dashboard` and `waitlist-legacy-checkout` appear (`kubectl -n waitlist get httproute`);
     the Helm release isn't touched. They are newer than the old routes, so the old (scaled-down) routes still
     win for now.
  2. `kubectl -n ferry-watch delete httproute ferry-web vnc-edge`. The old app is suspended, so Flux won't
     recreate them, and the redirects take over at once.
  3. Check:
     - `curl -sI https://ferry.chestr.dev/c/0123456789abcdef` gives `302` with `location:
       https://waitlist.chestr.dev/c/0123456789abcdef`;
     - `dig +short ferry.chestr.dev` still resolves;
     - `kubectl -n networking logs deploy/external-dns-cloudflare --since=10m | grep -i ferry` shows no
       delete.
  4. Tell every active user (the database owner bypasses web's RLS kind list):
     ```sh
     pg psql -v ON_ERROR_STOP=1 -d waitlist -c "INSERT INTO notifications (user_id, kind, dedup_key) SELECT id, 'moved', 'moved:' || id FROM users WHERE state = 'active' ON CONFLICT (dedup_key) DO NOTHING"
     ```
     It goes by email and by push. Old subscriptions still deliver, because the VAPID key was copied; a tap
     lands on `ferry.chestr.dev/`, which redirects.
  5. Tell invitees in person as well: open waitlist.chestr.dev, sign in, add it to the home screen again, and turn
     alerts on for each device.

**Rollback.**

- **R1, before C9.** Nothing was lost: `ferrywatch` hasn't been written since C2.
  ```sh
  flux suspend helmrelease waitlist -n waitlist && kubectl -n waitlist scale deployment waitlist-web waitlist-control waitlist-fetcher vnc-edge --replicas=0
  flux resume kustomization ferry-watch -n ferry-watch && flux resume helmrelease ferry-watch -n ferry-watch
  kubectl -n ferry-watch scale deployment ferry-web ferry-control --replicas=1 && kubectl -n ferry-watch scale deployment vnc-edge --replicas=2
  ```
  Then, in Ferry Watch, Admin → Maintenance off. If C9 had already started (PR-C merged or routes deleted),
  follow R2 instead.
- **R2, after C9 and before PR-D.** The HelmRelease has no `driftDetection`, so `flux resume` alone recreates
  neither the deleted routes nor the replicas.
  1. Bring the old routes back first, while the redirects still name the hosts (no DNS gap):
     ```sh
     flux resume kustomization ferry-watch -n ferry-watch && flux resume helmrelease ferry-watch -n ferry-watch
     flux reconcile helmrelease ferry-watch -n ferry-watch --force     # the forced upgrade re-creates the HTTPRoutes
     kubectl -n ferry-watch get httproute ferry-web vnc-edge           # both listed
     kubectl -n ferry-watch scale deployment ferry-web ferry-control --replicas=1 && kubectl -n ferry-watch scale deployment vnc-edge --replicas=2
     ```
  2. Revert PR-C, then `flux reconcile kustomization waitlist -n waitlist --with-source`. The redirect routes go
     away, and the old routes serve again.
  3. Check: `curl -sI https://ferry.chestr.dev/` answers 200 from Ferry Watch, and `dig +short ferry.chestr.dev`
     still resolves.
  4. Ferry Watch: Admin → Maintenance off. Waitlist: Admin → Maintenance on, or scale it to 0.

  Changes users made in Waitlist after C5 don't carry back; tell them. `ferrywatch` is exactly as of C2.
- **After PR-D** there is no rollback to Ferry Watch. Only Waitlist's own release rollbacks remain, on an
  expand-only schema.

**PR-D: decommission (≥ 14 days after C9, with Waitlist healthy).** The old app's Flux Kustomization and
HelmRelease stay **suspended** throughout. Deleting `kubernetes/apps/ferry-watch/` alone removes nothing that
matters, and the README says why:

- `kubernetes/components/common/namespace.yaml` carries `kustomize.toolkit.fluxcd.io/prune: disabled`, so the
  parent Kustomization never deletes the `ferry-watch` Namespace.
- It does delete the Flux Kustomization `ferry-watch`, but a suspended Kustomization is deleted without garbage
  collection. The suspended HelmRelease, the Deployments, the Secret `ferry-watch` (OIDC secret, VAPID key, DB
  passwords), the ExternalSecrets and the CronJob `ferrywatch-pg-backups` would all stay.

So the namespace is deleted by hand (step 5), with everything still suspended: a suspended HelmRelease drops
its finalizer without a Helm uninstall, and the suspended Kustomization's GC is skipped, so nothing can
re-create or touch the old database. What Helm would have removed outside the namespace is deleted by name in
step 6.

1. Confirm that the newest `ferrywatch-pg-backups` dump exists on the NFS share; keep it for 90 days.
2. Stop the old backup job before its database goes, or it fails every 4 h from step 4 on:
   `kubectl -n ferry-watch delete cronjob ferrywatch-pg-backups`.
3. Delete the push subscriptions made on the old origin. The old service worker can't update behind a
   redirect, so they would otherwise duplicate every push for good. The cut-off is when 0007 was applied
   (C7): after every restored subscription and before any new-origin one could exist (waitlist-web was scaled
   to 0 from C3 to C7). Print it, then delete:
   ```sh
   pg psql -At -d waitlist -c "SELECT applied_at FROM schema_version WHERE version = '0007_waitlist_names'"   # the C7 time
   pg psql -v ON_ERROR_STOP=1 -d waitlist -c "DELETE FROM push_subscriptions WHERE created_at < (SELECT applied_at FROM schema_version WHERE version = '0007_waitlist_names')"
   ```
4. `pg psql -v ON_ERROR_STOP=1 -d postgres -c 'DROP DATABASE ferrywatch WITH (FORCE)'`.
5. Merge PR-D. It deletes `kubernetes/apps/ferry-watch/`, applies every "D" row of §8.3 (the `ferrywatch_*`
   roles become `ensure: absent`), and sets `statusCode: 301` in `legacy-redirects.yaml`. Then run `task
   terraform:cf:apply`. `flux reconcile kustomization cluster-apps -n flux-system --with-source`; once `flux
   get kustomizations -A | grep ferry` shows nothing (cluster-apps deleted the suspended Kustomization without
   its GC), delete the namespace, with the HelmRelease still suspended:
   ```sh
   kubectl -n ferry-watch get helmrelease ferry-watch -o jsonpath='{.spec.suspend}'   # true (if not: flux suspend helmrelease ferry-watch -n ferry-watch)
   kubectl delete namespace ferry-watch --wait
   ```
6. Delete the leftovers outside that namespace, then check:
   ```sh
   kubectl delete validatingadmissionpolicybinding fw-browser-pod fw-browser-pod-lock fw-ns-objects --ignore-not-found
   kubectl delete validatingadmissionpolicy fw-browser-pod fw-browser-pod-lock fw-ns-objects --ignore-not-found
   kubectl delete clusterrolebinding ferry-watch-control-tokenreview ferry-watch-control-nodes --ignore-not-found
   kubectl delete clusterrole ferry-watch-control-tokenreview ferry-watch-control-nodes --ignore-not-found
   kubectl delete priorityclass ferry-browser --ignore-not-found
   kubectl delete namespace ferry-watch-browsers --ignore-not-found   # kept by helm.sh/resource-policy: keep
   pg psql -d postgres -c 'DROP ROLE ferrywatch'
   kubectl get ns ferry-watch                                        # NotFound
   kubectl get validatingadmissionpolicies,validatingadmissionpolicybindings,clusterroles,clusterrolebindings,priorityclasses | grep -Ei 'ferry|fw-'   # prints nothing
   ```
   W4 checks these names against the old HelmRelease's rendered cluster-scoped objects and lists any it finds
   missing here in the README.
7. In 1Password, remove `FERRY_WATCH_*` from `authelia`, `ferrywatch_*` from `cloudnative-pg`, and the item
   `ferry-watch`. (The ExternalSecrets that read them went with the namespace in step 5.) In lldap, delete the
   group `ferry-watch`.
8. A release later, remove the `ensure: absent` role entries.

---

## 9. Work breakdown

### 9.0 Before the workstreams

1. **Foundation** (done: branch `wl/foundation`). The rename, the removal of the legacy app, and migration
   0007, with the tree green.
2. **Contract commit**, made by the orchestrator on top of the foundation. Every workstream branches from it.
   All of its files are verbatim from this document:

| Path | Section |
|---|---|
| `src/waitlist/providers/__init__.py`, `base.py`, `bcferries.py` | §2.3 |
| `src/waitlist/providers/buntzen/meta.py` | §2.3 |
| `src/waitlist/providers/buntzen/__init__.py`, `client.py` (stubs) | §2.3 |
| `src/waitlist/fetchwire.py` | §4.3 |
| `src/waitlist/fetcher/__init__.py`, `main.py` (stubs) | §2.3 |
| `src/waitlist/control/checkplan.py`, `checks.py`, `fetch.py` (one-line stubs) | §2.3 |
| `pyproject.toml`: the `waitlist-fetcher` script, `"waitlist.fetchwire"` added to "The shared core never drives a browser", and the five contracts appended | §2.1, §2.5 |
| `tests/test_contract.py` | §9.8 |
| `web/src/mt/watch-schema.ts`, `web/src/mt/watch-schema.test.ts`, `web/src/test/fixtures/api-state.json` | §6.3, §7.6 |
| `docs/waitlist/design.md` (this document) | |

It passes `uv run pytest -m "not browser and not apiserver"` (1376 passed), `ruff format --check`, `ruff
check`, `mypy`, `lint-imports` (9 kept), and `npm test`, `npm run lint`, `npm run format:check` and `npm run
typecheck` in `web/`. All of this was verified on a scratch copy of the foundation.

The rule for every workstream: **a file is owned by exactly one workstream.** A workstream that needs
something from another's file asks for an amendment (§9.7) or adds what it needs in a file of its own.

### 9.1 W1: backend core, DB, check loop, web API

**Creates (or replaces a stub):**
- `src/waitlist/control/migrations/0008_providers.sql`
- `src/waitlist/control/{checkplan.py, checks.py, fetch.py}` (replacing the stubs)
- `src/waitlist/web/watches.py`
- `tests/{test_migration_0008.py, test_cutover_restore.py, test_checkplan.py, test_checks.py, test_fetch_client.py, test_web_watches.py, test_api_fixture.py, test_protocol_golden.py, test_bcferries_golden.py, test_import_contracts.py, golden_capture.py}`
- `tests/golden/{agent-protocol.json, bcferries-entry.json, bcferries-messages.json}`

**Modifies:**
- `src/waitlist/control/{db.py, store.py, fleet.py, main.py, config.py, limits.py}`
- `src/waitlist/web/{api.py, repo.py, outbox.py, messages.py, admin.py, config.py, main.py, app.py}`
- `README.md` (what Waitlist is now; the API section)
- the existing tests touched by the provider column and the state shape:
  `tests/{test_web_api.py, test_db.py, test_hub.py, test_fleet.py, test_outbox.py, test_web_config.py, test_control_config.py, test_web_auth.py, fakes.py, postgres.py}`.
  Each change is either the 7 insert sites of §3.4 or reading `watches` instead of `trips`, and W1's PR
  description lists them.

**Must not touch:** any verbatim file; `src/waitlist/providers/buntzen/**`; `src/waitlist/fetcher/**`;
`src/waitlist/protocol.py`; `src/waitlist/agent/**`; `vnc-edge/**`; `web/**`; `deploy/**`; `docker/**`;
`scripts/**`; `pyproject.toml`; `uv.lock`; `tests/deploy.py`; `tests/test_deploy.py`; `tests/apiserver/**`;
`tests/fixtures/buntzen/**`; `tests/test_buntzen_*.py`, `tests/test_fetcher*.py`, `tests/test_transport.py`,
`tests/test_capture_script.py`.

**Internal order**, so that W3 and W4 are never blocked by work in progress:
1. Capture the goldens at the contract base (`uv run python -m tests.golden_capture`), before any change.
2. 0008, grants, the 7 insert sites, and the provider filters of §2.4.
3. `ControlConfig`/`WebConfig`/`LimitKey` additions: these are what W4's render tests load.
4. `checkplan`, `fetch`, `checks`, and `main` wiring.
5. Web: `watches.py`, the snapshot, `api.user_state`, SPA routes.
6. Outbox, messages, admin.

**Interim.** Until W2 merges, `buntzen.PROVIDER` is the stub. W1's tests use a **fake provider**:
`tests/fakes.py: FakeHttpProvider` is a complete `HttpProvider` with a one-field spec `{"date": "YYYY-MM-DD"}`.
Each watch makes **two** queries, `FakeQuery(date, part: Literal["a", "b"])`, the shape of Buntzen's two
places, and each part's inventory carries one option (key `"a"` or `"b"`, status settable per test), so
partial records (§4.5) are testable without W2. Its window is 07:00 PT on date − 1 to 18:00 PT on date, its
interval 60 s, its reminder 06:55 PT on date − 1, and its `render` returns a fixed `Notice` per kind with
`url = watch_url`. Every method that takes `now` asserts `now.tzinfo is not None` (§2.2 rule 7). Its `meta` is **`waitlist.providers.buntzen.meta.META`**, so its
rows pass `trips_provider_check`. It is registered as `Registry.of(browser=[BCFERRIES],
http=[FakeHttpProvider()])`. Every W1 component takes a `registry: Registry = REGISTRY` parameter.
`test_api_fixture.py` and the real-Buntzen cases of `test_web_watches.py` skip while
`type(waitlist.providers.buntzen.PROVIDER).__name__ == "_NotYet"`, and run once rebased onto W2.

### 9.2 W2: the Buntzen provider and the fetcher service

**Creates (or replaces a stub):**
- `src/waitlist/providers/buntzen/{__init__.py, spec.py, model.py, policy.py, messages.py, client.py}`
- `src/waitlist/fetcher/{__init__.py, main.py, config.py, transport.py, registry.py, app.py}`
- `scripts/capture_yodel.py`
- `docs/providers/buntzen.md`
- `tests/{test_buntzen_spec.py, test_buntzen_parse.py, test_buntzen_policy.py, test_buntzen_messages.py, test_buntzen_client.py, test_fetcher.py, test_fetcher_config.py, test_transport.py, test_capture_script.py}`
- `tests/fixtures/buntzen/README.md` and every file in §5.8

**Modifies:**
- `docker/control/smoke-test.sh`, adding `waitlist-fetcher --help >/dev/null` and `python -c
  "import waitlist.fetcher.main"`.
- `docs/multi-tenant/README.md` §1 (the security model the brief says must be preserved): a `waitlist-fetcher`
  row in the "Components" table (what it does, what it can do, what it cannot do: the three columns of §4.2's
  identity table), and a short paragraph after "Why the operator cannot get secrets out of browser pods"
  stating I2, I6 and I7 for the fetcher (control still has no internet egress; Yodel's answers are parsed
  only in the fetcher; the fetcher holds no secret, token or database credential). Nothing else in that file
  changes.

**Must not touch:** `meta.py`, `base.py`, `fetchwire.py`, any other verbatim file, and everything outside the
lists above (in particular `tests/fakes.py`, `tests/conftest.py`, `pyproject.toml`). W2 adds no dependencies:
the provider uses only msgspec and `Transport`, and the fetcher uses aiohttp, which is already a core dependency.

**Internal order:** `fetcher/config.py` first (W4's render tests load it), then the provider, then the rest of
the fetcher.

**Deliverable check:** `PROVIDER: HttpProvider[BuntzenSpec, BuntzenQuery, BuntzenInventory]` and `FETCHER:
Fetcher[BuntzenQuery, BuntzenInventory]` type-check; `buntzen/__init__.py` doesn't import `.client`; and
`lint-imports` keeps all 9 contracts.

### 9.3 W3: frontend

**Creates and modifies:** `web/**` as listed in §7.10.

**Must not touch:** the three verbatim web files, and anything outside `web/`.

**Interim:** W3 develops against the shared fixture and stubbed `fetch` responses in tests (no MSW; no new
dependencies). It runs no backend until W1 merges.

### 9.4 W4: home-cluster and the app's deploy copy

**Creates and modifies:** everything in §8.1-8.3 in `/home/user/home-cluster`, as three branches: PR-A, PR-C
and PR-D (§8.6). The kubernetes/apps/waitlist README carries the runbook. In the app repo: `deploy/helmrelease.yaml`,
`deploy/externalsecret.yaml`, `tests/deploy.py`, `tests/test_deploy.py` and `tests/apiserver/**`.

**Must not touch:** the app's `src/**`, `web/**`, `docker/**`, `scripts/**` and `pyproject.toml`.

**Interim:** the render tests that load configs into `ControlConfig`, `WebConfig` and `FetcherConfig` pass only
once W1 step 3 and W2's `fetcher/config.py` are merged. W4 merges last, rebased onto W2, W1 and W3. Until then
it writes those tests against the JSON shapes in §4.4, §4.5 and §6.7. W4's app-repo branch carries placeholder
digests and `vncPubkeys` (§8.5); the orchestrator levels the app copy with PR-A at Phase 0 step 7, after the
release.

### 9.5 The interfaces between workstreams

| Between | Through | Where |
|---|---|---|
| W1 ↔ W2 | `base.py`, `fetchwire.py`, `buntzen/meta.py` (verbatim); `REGISTRY.http["buntzen"]` at runtime; the fetcher is reached only over the wire | §2.3, §4.3 |
| W1 ↔ W3 | the state, catalog and mutation JSON; `watch-schema.ts` and `api-state.json` (verbatim); the admin overview and switch shapes | §6, §7.6 |
| W1, W2 ↔ W4 | config JSON (`fetcher`, `httpChecks`, `providers`, the fetcher's config), `WL_FETCHER_CONFIG`, port 8767, the script `waitlist-fetcher run`, probes `/livez` `/readyz` | §4.4, §4.5, §6.7, §8.2 |
| W2 ↔ W3 | Buntzen's displayed constants (stop times, 06:55, 07:00, seasons, labels), which W3 mirrors in `labels.ts`/`season.ts` and pins with a test against §5.5's values | §5.5 |

### 9.6 Merge order

```
foundation ─► contract commit ─┬─ W2 ─┐
                               ├─ W1 ─┤  merge W2, then W1, then W3, then W4 (app repo), each rebased and green
                               ├─ W3 ─┤  ─► version 1.0.0, tag ─► images (made public) ─► home-cluster PR-A ─► S ─► C1-C9 (PR-C) ─► PR-D
                               └─ W4 ─┘
```

- **W2 first:** it is a leaf, and W1's real-Buntzen tests need it.
- **W1 second:** the server W3 talks to, and the config classes W4 loads.
- **W3 third:** it needs the real server only for manual end-to-end checks; its contract is the fixture.
- **W4 last:** its render tests load W1's and W2's config classes, and `migrate-config` shares control's
  anchor.

The file sets are disjoint, and `pyproject.toml` and `uv.lock` change only in the contract commit, with one
exception: the orchestrator's release commit on `main` after W4 merges, which sets `version = "1.0.0"` in
`pyproject.toml` and the matching `uv lock` line, and nothing else (§8.7 Phase 0 step 2). The orchestrator also
makes Phase 0 step 7's `deploy/helmrelease.yaml` commit. After each merge, run the full Python and web
suites. After W1 lands on W2, the skipped real-Buntzen tests must run and
pass before W3 merges.

### 9.7 Changing a verbatim file

A verbatim file changes only by a **contract amendment**: one commit, by the orchestrator, applied to the
contract base and announced to every workstream, which then rebases. Workstreams **add** what they need in their
own files (a helper beside `base.py`, for example) rather than editing a verbatim one.

### 9.8 `tests/test_contract.py` (verbatim, contract commit)

```python
"""The design contract's shared pieces agree with each other (docs/waitlist/design.md §9.0). Verbatim."""

from __future__ import annotations

import json
from pathlib import Path

import msgspec
import pytest

from waitlist.fetchwire import FetchFailed, FetchOk, FetchRequest, decode_reply, decode_request, encode
from waitlist.providers import PROVIDER_IDS, REGISTRY
from waitlist.providers.base import ADMIN_NOTICE_KINDS, NOTICE_KINDS, Option, ProviderMeta, search_hash
from waitlist.providers.buntzen.meta import HOSTS, META

FIXTURE = Path(__file__).parent.parent / "web" / "src" / "test" / "fixtures" / "api-state.json"


def _fixture() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text())
    return data


def test_the_registry_has_exactly_the_providers_the_contract_names() -> None:
    assert {"bcferries", "buntzen"} == PROVIDER_IDS
    assert [m.id for m in REGISTRY.metas] == ["bcferries", "buntzen"]
    assert set(REGISTRY.http) == {"buntzen"}
    assert REGISTRY.meta("buntzen") is META
    assert HOSTS == ("api.yodelpass.com",)
    assert not NOTICE_KINDS & ADMIN_NOTICE_KINDS


def test_the_catalog_in_the_shared_fixture_is_the_metas() -> None:
    providers = _fixture()["providers"]
    assert isinstance(providers, list)
    entries = {p["id"]: {k: v for k, v in p.items() if k not in ("offered", "paused")} for p in providers}
    assert entries == {m.id: m.to_json() for m in REGISTRY.metas}


def test_the_fixtures_buntzen_options_are_options() -> None:
    watches = _fixture()["watches"]
    assert isinstance(watches, list)
    for watch in (w for w in watches if w["provider"] == "buntzen"):
        options = msgspec.convert(watch["options"], list[Option])
        assert all(META.allows_booking_url(o.booking_url or "") for o in options)
        assert META.allows_booking_url(watch["booking_url"])


def test_a_booking_prefix_must_be_https_and_a_directory() -> None:
    meta = REGISTRY.metas[0]
    with pytest.raises(ValueError, match="booking prefix"):
        ProviderMeta(**{**{f: getattr(meta, f) for f in meta.__slots__}, "booking_prefixes": ("http://x.example/",)})


def test_the_fetch_wire_round_trips_and_refuses_extras() -> None:
    request = FetchRequest(id="5be0d1c27f6a9e31", provider="buntzen", query={"place_id": 10672, "date": "2027-07-01"})
    assert decode_request(encode(request)) == request
    with pytest.raises(ValueError, match="unknown field"):
        decode_request(encode(request)[:-1] + b',"url":"https://evil.example"}')
    ok = FetchOk(id="5be0d1c27f6a9e31", inventory={"place_id": 10672}, elapsed_ms=3)
    failed = FetchFailed(id="5be0d1c27f6a9e31", error="site_changed", http_status=404)
    assert decode_reply(encode(ok)) == ok
    assert decode_reply(encode(failed)) == failed


def test_search_hash_is_stable() -> None:
    assert search_hash(("2027-07-01",)) == search_hash(("2027-07-01",))
    assert len(search_hash(("x", 1, None, True))) == 16
```

---

## 10. Test plan and acceptance criteria

Python tests run with `uv run pytest -m "not browser and not apiserver" -q` against the local PostgreSQL 16
(`tests/postgres.py`, binaries in `/usr/lib/postgresql/16/bin`). Frontend tests run with `npm test`. Every
workstream also keeps `ruff format --check`, `ruff check`, `mypy`, `lint-imports` (9 contracts), and `npm run
lint`, `format:check`, `typecheck` and `build` green. The slow suites use the tooling the foundation report
lists (`WL_HELM`, `WL_ENVTEST_BIN`, `WL_APP_TEMPLATE`, `WL_HOME_CLUSTER`, `WL_KASMVNC_WWW`).

### W1

| Test | What it proves |
|---|---|
| `test_migration_0008.py` | On a fresh database at 0008, every row of §3.3 holds **as each role** (`waitlist_web`, `waitlist_control`, the owner), including the triggers, the column-level `trips` grant, the RLS kind list, the `paused_providers` switch, and `FOR KEY SHARE OF t` as control. `PROVIDER_IDS` equals the ids parsed from `pg_constraint` `trips_provider_check`. `paused_providers` is seeded `[]`. |
| `test_cutover_restore.py` | The §3.5 flow end to end, with every assertion listed there. |
| `test_checkplan.py` | `check_state` precedence, one case per rule (web's and control's `provider_running`). Coalescing: two users and three watches on one date give 2 jobs. The budget admits whole watches only, and `deferred` is counted. Nothing is due before `opens_at + 2 s`, and `wake_at` equals it. Quiet hours, lifted and not. Reminders: once per user and date, never in quiet hours, never at or after `opens_at`, none for a disabled reminder. Paused, archived and over watches are never planned. A user with `users.paused` true (the pre-0004 "Pause all") or `state` not `active` makes `check_state` answer `paused` (`owner_active = user.active and not user.paused`), matching `planner._ineligible` for that user's BC Ferries trips. `ended` lists watches 12 h after `closes_at`. DST: for releases on 2027-03-14 and 2027-11-07, `wake_at` is 07:00:02 local. |
| `test_checks.py` (FakeHttpProvider, a fake `FetchClient`, real DB) | Success writes `check_results`, `trip_runtime` and `alert_memory`. One `opening` per fresh set, and none while it stays open. A reopen after closing re-alerts, but not within 120 s. The 6/h cap holds alerts back with one `alert_held_back` event per hour. A changed `rev` drops the result. Our-side failures change no counter, write no event, and set `next_check_at` to now + 30 s. Provider-wide failures set the cooldown and count without per-watch backoff. Per-check failures back off at 3 and alert `failing` once at 5 (dedup on `error_since`). **Partial records** (FakeHttpProvider's two parts): part `b` fails `site_changed` while part `a` returns open → exactly one `opening` for `a`, `outcome = 'error'`, `consecutive_errors` + 1, no `backoff_until` even at 3 or more, and the next check at the normal interval; a `b` that was in alert memory stays there (not `gone`, no `opening_gone`); part `b` failing `fetcher_unavailable` while `a` is open → one `opening` for `a`, counters and outcome unchanged, `next_check_at` = now + 30 s. Once rebased onto W2 (skipped while the stub): a watch for all three passes whose half-day job fails `site_changed` while the all-day job returns open gets exactly one `opening`, for `full_day`. An archive whose `UPDATE` changes no row writes no `watch_ended` event, and a second archive attempt doesn't duplicate it. A watch deleted between plan and record (no row for `FOR KEY SHARE`) is dropped silently, and a `PostgresError` in one watch's transaction doesn't stop the others in the tick. `site_changed` queues one admin notification per provider per Pacific day, `checker_down` one per provider per hour after 180 s. **Compare-and-set:** a `next_check_at` written as `waitlist_web` between plan and record survives the record. Auto-archive at `closes_at + 12 h` with a `watch_ended` event. Reminders are inserted once, even across ticks. `pg_notify` fires once per affected user. `check_report` has the §6.6 shape, and cooldowns are restored from it at startup. |
| `test_fetch_client.py` (aiohttp test server) | Every `CheckFailed` mapping in §4.5: refused connection or timeout → `fetcher_unavailable`; non-200, a reply over 64 KiB, an id mismatch, a `WireError`, an inventory that fails `convert` → `bad_reply`; `FetchFailed` passed through with `retry_after_s` and `replied=True`. `FetchFailed(error="rate_limited", http_status=None)` (the fetcher's own cap) → `fetcher_unavailable` with `replied=True`, and `test_checks` shows it sets no provider cooldown and charges no watch; `rate_limited` with `http_status: 429` stays `rate_limited`. A reply sent in several chunks is read whole. |
| `test_web_watches.py` | `POST`/`PATCH`/`DELETE`/`check`/`done` on `/api/watches` for both providers. Every 400/403/404/409/429 message in §6.2. Limits per group, and `offer` honoured (403 for a non-admin under `admins`). **`POST /api/watches/{buntzen id}/check` returns 200**, and PATCH/DELETE on a Buntzen watch never reach `Trip.from_json`. A check while the watch backs off (or the provider cools down) gives 409 `Checks resume at {time}` and consumes no `manualChecksPerHour` unit. `/api/trips/{buntzen id}/hold`, `/vnc`, `/cart/release` and `/cart/dismiss` (sent with a valid `cart_id`, or `_cart_id` answers 400 first), and `PATCH`/`DELETE /api/trips/{buntzen id}`, give 404 `No such trip`; `/api/trips/{buntzen id}/booked` gives 409 `There's no checkout to answer for`, the same answer as any trip without a checkout. A PATCH changing only `passes` sets `next_check_at` to NULL and keeps alert memory, and the next snapshot re-marks the stored options' `wanted` (a pass no longer watched has no Book link). bcferries' catalog `horizon_days` equals the configured `polling.horizonDays`. The `/api/trips` aliases still work for BC Ferries. Editing the date clears alert memory and hides stale results. A provider can't be changed. `/done` is refused for BC Ferries. |
| `test_api_fixture.py` | A snapshot built for rows equivalent to the fixture's two Buntzen entries and its BC Ferries entry matches the fixture's keys and value types, entry by entry. `app[PUBLIC]`'s keys ⊆ `{links, push}`. An admin pause shows as `providers[].paused: true` in the next snapshot. A row whose spec won't load degrades to the minimal entry, and the rest of the state is intact. |
| `test_fleet.py` (addition) | With a live Buntzen row and an **archived BC Ferries trip that still has a live pod**, `Fleet._inputs` returns the BC Ferries trip, never the Buntzen row, and logs no exception. That is the parenthesisation guard. |
| `test_outbox.py` (additions) | Buntzen kinds render through the provider, with url = `/w/<id>` and `also` within the prefixes. Unrenderable rows (unknown provider, bad spec, render None, a failed assertion) get `attempts = 6, last_error = 'Unrenderable'` and appear in the admin attention list. `site_changed`/`checker_down` go to admins only. `moved` is generic and emailed. A BC Ferries row whose spec doesn't load is Unrenderable. |
| `test_web_api.py` (additions) | The admin overview pops `check_report` and adds `checks` and `providers`. The `pause_provider` switch and its audit row. `list_users` returns `watches` per provider, and its `trips` still counts BC Ferries trips only (a user with one trip and two Buntzen watches shows `trips: 1`). |
| `test_control_config.py`, `test_web_config.py` (additions) | `fetcher`/`httpChecks`/`providers` parse, default off, and refuse unknown providers, rates above the ceiling, and `httpChecks` without `fetcher`. `maxAlertOnlyWatches` is accepted in `limits`. |
| `test_protocol_golden.py`, `test_bcferries_golden.py` | The goldens below. |
| `test_import_contracts.py` | Every `providers/*/client.py` is fenced; every provider package is in the independence contract. |

### W2

| Test | What it proves |
|---|---|
| `test_buntzen_spec.py` | Every row of §5.2 with its exact message and order. Merging with `base`. Date rules only when the date changes. `release_reminder` defaults to true. Canonical pass order. `load_spec(dump_spec(s)) == s`, and `load_spec` accepts yesterday's watch. |
| `test_buntzen_parse.py` | Every §5.8 fixture gives exactly its expected result. That includes **`synth-halfday-ha-quirk-soldout.json` → both half-day passes sold out, not SC**; `synth-halfday-unplaceable-open.json` → SC; the quantity fallback; the `data` location. A seeded property test: random JSON objects never raise anything but `FetchError`, and never give a non-empty inventory without the list key. Statuses are sanitised to the `Detail` pattern. `place_id` and `date` come from the query, never the body. |
| `test_buntzen_policy.py` | `window` across DST (2027-03-14, 2027-11-07: 07:00 local on both sides). `interval` is 30 s in the burst, 60 s after, and 30 min out of season. `reminder_at` is 06:55 the day before, None when disabled, None out of season. `in_season` covers `KNOWN_SEASONS` and the estimate's edges. `queries` for every pass subset. `evaluate` for every rule of §5.5, including `over` by stop time, `not_released` before `opens_at`, `unchecked`, both unknown-period rules, and unwanted options shown but never alerting. `booking_url` always lies within `META.booking_prefixes`. |
| `test_buntzen_messages.py` | The exact §5.7 strings for one, two and three passes, certain, uncertain and mixed. Bad params give None. Every `also` link lies within the prefixes, and `url == watch_url`. No `Option.detail` text appears in any notice. |
| `test_buntzen_client.py` (a fake `Transport`) | The exact request: the URL, exactly Home Assistant's three headers (`user-agent`, `x-api-version: 5.6`, `content-type: application/json`; no `accept`), and a golden of the payload bytes (with the string `placeId`). Every `check_status` mapping. An HTML content type is SC. |
| `test_fetcher.py` (aiohttp test server, a fake `Fetcher`) | 400 on a bad request or an extra field. `unexpected` for an unconfigured provider. The token-bucket and semaphore caps give `rate_limited` with `http_status` null, without queueing; a site's 429 passes through with `http_status: 429`. `FetchError` maps to `FetchFailed`; an unknown exception gives `unexpected`; the reply size is capped. **No body or header ever appears in caplog.** Only `/v1/fetch`, `/livez` and `/readyz` exist. |
| `test_transport.py` | Through the `_scheme`/`_port` seam against an `aiohttp.test_utils` server (§4.4), plus the production defaults for the refusals. It refuses a non-declared host, a non-443 port, http and userinfo, before any I/O. No file under `src/` or `scripts/` passes `_scheme` or `_port`. A resolver answer that is private (10.0.0.5, 192.168.1.1, 100.64.0.1, ::1) gives `network`, via an injected fake inner resolver. It doesn't follow a 302. **A body served in three delayed chunks is read whole** (the truncation regression). Over `max_bytes`, by streaming or by `Content-Length`, gives `too_large`. Retry-After is parsed and clamped. No cookie is stored. |
| `test_fetcher_config.py` | camelCase parsing, the bounds, unknown providers, and a cap above the budget's ceiling are refused. |
| `test_capture_script.py` | `--dry-run` prints the exact request and opens no socket. A real run refuses under `PYTEST_CURRENT_TEST`. The fixtures README names every fixture file. |

### W3

- `watch-schema.test.ts` (verbatim).
- The registry uses a `Map`, and `"constructor"` resolves to nothing.
- Grouping per provider into the four sections, and their order. The "Next" hint.
- A card that throws is contained by `WatchCard`'s error boundary (its in-card fallback with Remove), and
  `ErrorBoundary` without a `fallback` still renders the full-screen page.
- `MyTripCard`'s Pause/Resume calls `updateWatch(id, {enabled})`.
- The empty state's buttons.
- The picker: offered providers only, disabled tiles with reasons, a paused tile still pickable, skipped with one
  provider, last used first.
- `/new` and `/new/<provider>`, including an un-offered provider falling back to `/new`.
- `BuntzenSheet`: the day chips (Today hidden after 18:00), all passes on by default, the reminder switch's
  visibility, the out-of-season and quiet-hours notes, server field errors, and PATCH sending only changed keys.
- `BuntzenCard`: every badge row of §7.4, every option chip (including "Couldn't check" for `unchecked` under
  `outcome: "error"`), every note, "Book on Yodel" only for open or maybe
  options with a valid prefix, "I booked it" calling `/done`, and no cart or checkout UI.
- `WatchPage` for each kind; a non-BC Ferries id on `/c/` redirects to `/w/`.
- `UnknownCard`'s Remove works.
- The alerts banner (iOS wording included).
- The admin Providers panel and its pause switch.
- `sw.test.ts`.
- `labels.ts`/`season.ts` values equal §5.5's.
- **The BC Ferries components' existing tests pass with only fixture and path changes.**

### W4

- `tests/test_deploy.py`:
  - the render succeeds with the new values, and fails for a bad `offer`, for `requestsPerMinute >
    fetcherPerMinute` and for `fetcherPerMinute > 30`;
  - the rendered control, migrate, web and fetcher configs load into `ControlConfig`, `WebConfig` and
    `FetcherConfig`;
  - with `offer: "off"` they carry no `fetcher`, `httpChecks` or `providers` keys;
  - the fetcher Deployment has `automountServiceAccountToken: false`, no `PG*` env, no secret volumes, the
    security context, the probes and `ndots: 1`;
  - the fetcher policies match §8.2, including `egressDeny`, and control's egress gains only the fetcher;
  - `test_admission_policies_reach_only_waitlist`, exactly the §8.1 rule: no cluster-scoped name contains
    `ferry` or `fw-`; no VAP or binding selects a namespace whose name starts with `ferry`; `wl-browser-pod`
    and `wl-browser-pod-lock` (policy `matchConstraints` and binding `matchResources`) select only
    `kubernetes.io/metadata.name: waitlist-browsers`; `wl-ns-objects`' `matchConstraints` select exactly
    `[waitlist, waitlist-browsers]`, and its expression names `system:serviceaccount:waitlist:` and
    `system:serviceaccount:waitlist-browsers:`;
  - the app copy has `streamViewer: "off"` and every `waitlist.providers.*.offer: "off"`; the Buntzen-on
    assertions render with the `offer: "admins"` override (§8.5);
  - `test_home_cluster_runs_this_copy` masks exactly the `streamViewer:` and `offer:` lines (a test feeds it
    two texts differing in a third line and expects a failure); it is skipped without `WL_HOME_CLUSTER` and
    passes against PR-A once Phase 0 step 7 is done;
  - kubeconform passes.
- `tests/apiserver/**` (envtest): the mutation suite and the "no identity holds a verb outside its list" check
  pass under the new names, and the fetcher's ServiceAccount holds **no** verbs.
- flux-local passes in home-cluster for PR-A, PR-C and PR-D. PR-C's diff shows the two HTTPRoutes and no
  HelmRelease change; PR-D's shows only `statusCode: 301` in `legacy-redirects.yaml` for the waitlist
  Kustomization.

### BC Ferries behaviour unchanged (acceptance gate; all must hold)

1. **Protocol.** `tests/golden/agent-protocol.json` holds `msgspec.json.schema(AgentMessage)` and
   `msgspec.json.schema(ControlMessage)`, generated at the contract base. It is unchanged after every merge.
2. **Pod.** `pods.browser_pod(…)` for fixed inputs is unchanged (`test_control_pods.py`), and the VAP suite
   passes.
3. **State.** For a user with one BC Ferries trip, `watches[0]` minus `provider`, `id` and `enabled` equals
   `tests/golden/bcferries-entry.json`, captured at the base (0.7.1's `trip`, `rev`, `paused_reason`, `quiet`,
   `hold`, `browser`, `booking_url`).
4. **Messages.** Every BC Ferries kind (`bookable`, `cart_held`, `hold_reminder`, `hold_ending`, `cart_failed`,
   `cart_lost`, `cart_expired`, `failing`, `booked_it`, `booked`, `checkout_blocked`), rendered with fixed
   params, gives the same title, body, url, `also`, urgency, ttl and topic as
   `tests/golden/bcferries-messages.json`, captured at the base.
5. **Flows.** The existing hub, fleet, store, planner, watch, holds, tickets, gate and web API test modules pass.
   Their changes are limited to the 7 insert sites and to reading `watches` filtered to `bcferries` instead of
   `trips`.
6. **Isolation.** A Buntzen watch due now never appears in `Fleet._inputs`. It never gets a pod (the trigger
   refuses even a forced insert). Its id is 404 on `/api/trips/{id}/hold`, `/vnc`, `/cart/*` and on
   `PATCH`/`DELETE /api/trips/{id}`, and 409 `There's no checkout to answer for` on `/booked`, which is
   exactly what any BC Ferries trip without a checkout gets (0.7.1's answer, unchanged).

### Acceptance criteria (Waitlist 1.0)

1. All of the above pass, and `lint-imports` keeps all 9 contracts.
2. In a test environment with a fake fetcher, a Buntzen watch created at 06:30 PT on D−1 does the following:
   - it shows `waiting_release`, and its reminder is queued at 06:55;
   - the loop wakes and makes its first check at 07:00:02;
   - an `open` inventory gives exactly one push, linking to `/w/<id>`, and an email whose `also` is the Yodel
     page, if no device took the push;
   - an unlabelled all-sold-out half-day answer gives both passes `sold_out`, no error and no alert;
   - a half-day answer that keeps failing `site_changed` never stops an all-day opening from alerting, and
     never backs the watch off;
   - a fetcher outage of 10 minutes changes no `consecutive_errors` and sends no user alert, and admins get one
     `checker_down`.
3. As rendered, the fetcher Deployment can reach nothing but `api.yodelpass.com:443` (public addresses) and
   kube-dns, and accepts connections only from waitlist-control.
4. The cutover rehearsal (`test_cutover_restore.py`) passes, and the runbook's C8 checks match its assertions.
5. With `waitlist.providers.buntzen.offer: "off"`, the rendered configs carry no `fetcher`, `httpChecks` or
   `providers` keys, and Waitlist is BC Ferries-only.

---

## 11. Risks and open questions

Rows marked **owner's call** are genuinely the owner's to decide. The engineers implement the stated default,
and changing it later is one value.

| # | Risk or question | Recommendation (implemented) |
|---|---|---|
| Q1 | **Yodel's real response shape is unverified.** The sandbox can't reach it, and it's out of season. A wrong guess means Buntzen reports `site_changed` (loudly) instead of alerting. | Accept. The parser fails loudly by design, and admins hear about it. **The owner runs `scripts/capture_yodel.py` once now** (the out-of-season envelope still shows the shape) and again at the May 2027 opening. The captures become `captured-*` regression tests, and the parser is adjusted in a 1.0.x. Buntzen can be paused, or set to `offer: "off"`, without touching BC Ferries. |
| Q2 | **Pass stop times** (morning 12:00, afternoon 18:00, all-day 18:00) are assumptions. | They stay W2 constants, labelled unverified, and lean late so as not to miss a usable pass. Confirm them against BC Hydro's 2027 page or the capture; a change is a one-line release. Not deployment config. |
| Q3 | **Authelia's `sub` might differ for the new OIDC client.** If it does, every user looks new, and invite-only refuses them. | Authelia's subject is per user, and no `sector_identifier_uri` is configured, so it should match. Phase S step 1 checks before cutover. If it differs, map it with `identity.aliases` (already supported) rather than editing the DB. The foundation's user-id hash is never changed. |
| Q4 | **Push continuity.** Old devices are subscribed through `ferry.chestr.dev`'s service worker, which can't update once its URL redirects. | Copy the VAPID key (rename, not rotate), so old subscriptions keep delivering through the cutover. The `moved` notification (email and push) tells everyone to re-enable alerts on the new origin. PR-D deletes subscriptions created before the cutover, which ends duplicates; email covers anyone who hasn't re-subscribed. Rotate the VAPID key later with the documented procedure, if wanted. |
| Q5 | **Owner's call:** should a Buntzen push open Yodel directly (one tap) instead of `/w/<id>`? The brief says the alert deep-links to the booking page. | **Default: no, not in 1.0.** `sw.js`'s same-origin rule (I8) is cheap and valuable, and iOS's handling of a cross-origin `openWindow` from an installed PWA is unverified. The push lands on `/w/<id>`, whose first element is a full-width **Book on Yodel**; the email links Yodel directly. The follow-up, if the extra tap proves costly: a compiled allowlist in `sw.js` equal to the providers' path-level `booking_prefixes` (never a whole origin), plus an "Open Waitlist" action, with a test holding the two lists equal. Ship it only after testing on iOS. |
| Q6 | Alerting on **unrecognised statuses** (`maybe`) risks false alarms; not alerting risks missing the one pass that matters. | Alert once (alert memory, the 120 s cooldown, the 6/h cap), worded "may be bookable". The card shows the sanitised status, so the owner can extend the table. Missing an opening is the worse failure for a watcher, and Home Assistant alerted on anything but "Sold Out". |
| Q7 | **Politeness and terms.** Yodel's API is unofficial: a desktop-browser UA, as Home Assistant used, from the home IP. | Keep the owner's established practice: 12 distinct requests a minute at most in control and a hard 30 in the fetcher (Home Assistant made about 4), coalescing across users, 30-minute checks out of season. If Yodel objects or blocks, use the admin's per-provider pause (no deploy). A 401/403 also triggers a 30-minute cooldown by itself. |
| Q8 | The fetcher runs **one replica**: a restart or a node drain pauses Buntzen checks briefly. | Accept. I11 makes it free for users: no counters and no alerts, a retry in 30 s, admins told only after 3 minutes. Two replicas would halve each cap's meaning. |
| Q9 | **No authentication between control and the fetcher**, only the network policy and the closed vocabulary. | Accept for 1.0. A caller past the policy can only ask for Buntzen availability: public data, rate-capped by the fetcher itself, with no URL field. A shared secret would give the fetcher a secret to hold, against I7. mTLS or a projected-token audience can come if a provider with sensitive queries ever arrives. |
| Q10 | The **`/api/trips` CRUD aliases**. | Keep them in 1.0 so 0.7.1's request shapes and status codes still work (the response bodies are the new state shape, and the existing API tests read `watches`); the SPA never calls them. Remove them in 1.1. |
| Q11 | **Owner's call:** who is offered Buntzen at launch? | **Default `offer: "admins"`** until a real capture confirms the parser (Q1). Then set `"everyone"` in the HelmRelease values: one line, no release. |
| Q12 | **The season estimate could be wrong for 2027** (an earlier start would get 30-minute checks and no reminder). | The estimate (May 1 to Sep 30) is deliberately wider than any recent season. When BC Hydro announces 2027's dates, add them to `KNOWN_SEASONS` in a 1.0.x. The worst case is one opening seen up to 30 minutes late. |
| Q13 | **Cilium's `egressDeny` with CIDRs** might, on this cluster's Cilium, interfere with DNS to kube-dns. | Cilium's CIDR rules don't select cluster endpoints, so it shouldn't. Phase S proves it with a real Buntzen check. The fallback is to drop the block, because the fetcher's own `PublicOnlyResolver` already refuses non-public answers. |
| Q14 | **Owner's call:** the release number. | **Default `1.0.0`** (a new product name, and the foundation's minimum was 0.8.0). Nothing depends on the number except the version shown in the UI. |
| Q15 | **Booking links can't carry the date** (the portal's URL scheme is unverified), so the user picks the day on Yodel. | Accept. The card and the alert say which day. If the May 2027 capture shows a dated URL, add it in W2's `PAGES` logic within the same prefix. |

---

## Revision notes (completeness review, round 2)

Each finding was checked against the foundation tree (`/home/user/wt/foundation`) and home-cluster before it
was fixed. All majors and every minor were fixed.

**Major**

1. **Per-query recording (§4.5).** The three all-or-nothing outcomes became four cases: Success, **Partial**,
   Provider-side failure and Our-side failure. Evaluation runs over the queries that succeeded. A failed
   query's options come out `unchecked` and are kept in alert memory (`gone` excludes them). A partial failure
   on the site's side counts an error but never backs the watch off. A partial failure on our side changes no
   counter. `FakeHttpProvider` now makes two queries, so W1 can test this, and test_checks gains the
   "half-day `site_changed`, all-day open → one `opening` for `full_day`" case. §0, I11 and the
   "Who decides" table follow. `checked_at`'s SQL comment was reworded.
2. **PR-D (§8.7).** The common Namespace has `prune: disabled`, and a suspended Kustomization is deleted
   without GC. So PR-D now deletes the backup CronJob before dropping the DB, then runs `kubectl delete
   namespace ferry-watch --wait` after the merge, with everything still suspended. The README states why.
3. **VAP rule (§8.1, §10 W4).** It now says exactly how each of the three policies is scoped.
   `wl-ns-objects` keeps covering `[waitlist, waitlist-browsers]`. The test is
   `test_admission_policies_reach_only_waitlist`.
4. **vncPubkeys (§8.1, §8.4, Phase 0/S).** The checkout key is new, and its public half goes into both
   HelmRelease copies at Phase 0 steps 6-7. Phase S step 4 compares it with the pod's `WL_VNC_PUBKEYS`
   without printing a secret. The snippet was tested here with a throwaway key.
5. **Deploy copies (§8.5).** The two copies are identical except for the `streamViewer:` and `offer:` lines
   (app copy: `"off"`), and W4 widens the test mask to match. W4's branch carries placeholders. The
   orchestrator levels the app copy at the new Phase 0 step 7.
6. **Legacy redirects (§8.2, §8.6).** They moved out of the HelmRelease into the plain
   `legacy-redirects.yaml` (`waitlist-legacy-dashboard`, `waitlist-legacy-checkout`). PR-C, PR-D and R2 never
   upgrade the Helm release.
7. **Version bump (§2.1, §8.7 step 2, §9.6).** `pyproject.toml` and `uv.lock` are bumped together by
   `uv lock`. This is the only edit to either file after the contract commit.
8. **`/booked` on a Buntzen id (§6.1, §10).** It answers 409 `There's no checkout to answer for`, which is
   0.7.1's answer for any trip without a checkout. The other BC Ferries routes answer 404; the cart routes
   need a valid `cart_id` to get there.

**Minor**

- §2.5: checks.py copies the users SQL. The check-loop contract also forbids `control.config` and
  `control.main`, and the modules those files may import are listed.
- §4.5: `owner_active = user.active and not user.paused`, with a test in test_checkplan.
- §2.2 rule 7 and the `HttpProvider` docstring: every `now` is timezone-aware; the fake asserts it.
- §4.3, §4.4, §4.5: the fetcher's own cap (`rate_limited`, no `http_status`) maps to `fetcher_unavailable`
  (`replied=True`). It triggers no cooldown and charges nothing.
- §4.5 steps 5 and 8:
  - `watch_ended` is inserted only on `UPDATE 1`, with `ON CONFLICT`, and followed by `pg_notify` only.
  - A missing row drops the result.
  - Each watch's transaction is wrapped in `try/except PostgresError`.
- §6.2: a `backoff` refusal, 409 `Checks resume at {time}`. A PATCH that changes the spec makes the watch
  due.
- §6.3: stored options are always re-marked against the current spec. §5.5 makes `evaluate`'s key set
  depend on the spec alone.
- §6.1:
  - The admin `trips` count stays BC Ferries only, beside `watches`.
  - bcferries' catalog `horizon_days` comes from `WebStore.horizon_days`.
  - The alias wording no longer promises byte-stable bodies (also Q10).
- §7: `ErrorBoundary` gains a `fallback` prop. SignedOut lives in `MtApp.tsx`. `main.tsx` and `sw.js` are
  unchanged. MyTripCard's Pause uses `updateWatch`. A "Couldn't check" chip covers partial failures.
- §4.4: the transport's `_scheme`/`_port` test seam.
- §5.3: exactly Home Assistant's three headers (no `accept`).
- §5.8: "list key at the root" wording, plus `synth-bare-array.json` → SC.
- §8.3: authelia's README link now points at the waitlist README.
- W2: updates docs/multi-tenant/README.md §1 (the fetcher row, I2/I6/I7).
- PR-D: the old-subscription cut-off is 0007's `schema_version.applied_at`, not a lost `$CUTOVER`.
