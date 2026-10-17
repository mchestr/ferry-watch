# Buntzen Lake

BC Hydro's day-use parking at Buntzen Lake is free but booked on Yodel (`yodelportal.com/buntzen-lake`), and
popular days sell out within seconds of going on sale. Waitlist watches it as an **alert-only** provider: it
tells you when a pass you want opens up, and you book it yourself on Yodel with your own account, licence plate
and mobile number. Waitlist stores no Yodel credential (docs/waitlist/design.md §5, I4).

This page is for whoever runs Waitlist. The design contract is docs/waitlist/design.md §5 (the provider) and
§4.4 (waitlist-fetcher); this page says how it behaves and what still has to be confirmed.

## What a watch is

One date and any of three passes:

| Pass | Yodel place | Booking page | Stops being worth finding (Pacific, on the day) |
|---|---|---|---|
| All-day | 10671 | `https://yodelportal.com/buntzen-lake/All-Day-Pass` | 18:00 |
| Morning | 10672 | `https://yodelportal.com/buntzen-lake/Half-Day-Pass` | 12:00 |
| Afternoon | 10672 | `https://yodelportal.com/buntzen-lake/Half-Day-Pass` | 18:00 |

A watch may be for today up to 30 days ahead, unless every pass it wants is already over today. With the
reminder on (the default), its owner gets a push at **06:55 the day before**: passes for a date go on sale at
**07:00 Pacific the day before**.

## How it is checked

waitlist-control decides when; waitlist-fetcher makes the request (it holds no secret and can reach only
`api.yodelpass.com:443` on public addresses). A watch is checked:

- from **07:00:02 the day before** until its last pass's stop time; nothing before the release, since there is
  nothing to find;
- every **30 s for the first 20 minutes** after the release, then **every 60 s** (±10 % jitter);
- every **30 minutes** for a date outside the season. The 2026 season ran May 14 to Sep 7; other years use an
  estimate of May 1 to Sep 30 until BC Hydro's dates are added to `KNOWN_SEASONS` in
  `src/waitlist/providers/buntzen/policy.py`. Out of season there is no reminder.

Identical queries (one place on one date) are shared by every user's watches. Control spends at most 12 distinct
requests a minute on Buntzen (`httpChecks.buntzen.requestsPerMinute`), and the fetcher refuses more than 30
(`providers.buntzen.maxPerMinute`) whatever control asks. At most four distinct queries are ever checkable at once
(two places, today and tomorrow), so the real peak is about 6 a minute; the owner's Home Assistant automation made
up to 4.

The request is the one that automation sent for years:

```
POST https://api.yodelpass.com/api/portal-catalog-items
user-agent:    Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36
x-api-version: 5.6
content-type:  application/json
{"userId":0,"placeId":"10672","reservationDate":"2027-07-01","catalogItemType":0,"scopeName":"buntzen lake","filterStartDate":null,"filterEndDate":null}
```

## How an answer is read

The parser (`src/waitlist/providers/buntzen/client.py`) fails loudly: an answer it can't vouch for is
`site_changed`, which admins hear about, never a silent "nothing open".

- The catalog list (`catalogItemExtendedResultLst`) may sit at the top level, under `content` or under `data`.
  `null` or an empty list is a normal "nothing on offer" (expected out of season), not an error.
- An item's pass comes from the first of its category, name, title, description and status that names exactly
  one of all-day, morning or afternoon. On the all-day place every item is the all-day pass.
- Its availability comes from the status words (sold out, coming soon, available, ...), then from a quantity
  field if the words are unknown. A status that is a pass name ("Morning") means that pass is on sale: the quirk
  the automation relied on.
- An unlabelled "Sold Out" on the half-day place stands for every half-day pass without an item of its own, so
  the common in-season answer (every item just "Sold Out") reads as both half-day passes sold out.
- An unlabelled item that may be bookable on the half-day place is `site_changed`: we can't say which pass.
- A status we don't recognise alerts once, worded "may be open", and the card shows it (sanitised, at most 32
  characters). It never goes into a notification.

## What is unverified, and the captures that settle it

The sandbox Waitlist was built in can't reach Yodel, and the design was written out of season, so these are
**unverified**: the response envelope beyond the two fields the automation read, the exact pass periods and stop
times, and whether the booking pages accept a date. The test fixtures are therefore *synthesized*
(tests/fixtures/buntzen/README.md says so for each), and Buntzen ships offered to admins only
(`waitlist.providers.buntzen.offer: "admins"`).

Run the capture script from a workstation on the home network, never in CI (it refuses to run with `CI` or
`PYTEST_CURRENT_TEST` set):

1. **Once now, out of season.** The answer is probably an empty or sold-out list, but its envelope shows Yodel's
   real shape:

   ```sh
   uv run python scripts/capture_yodel.py --dry-run            # prints the exact requests, sends nothing
   uv run python scripts/capture_yodel.py --date 2027-05-20    # both places, one request each
   ```

2. **Again on the first day of the 2027 season, around 07:00**, for the next day's passes, a few times across
   the release:

   ```sh
   uv run python scripts/capture_yodel.py --date <tomorrow> --repeat 6 --every 30
   ```

Each response becomes `tests/fixtures/buntzen/captured-<full|half>-<date>-<UTC time>.json` (or `.txt` when it
isn't JSON) with a `.meta.json` beside it holding the status, content type, the request and what the parser made
of it. No response header is stored. The script waits at least 5 s between requests and goes through
waitlist-fetcher's own transport (https to `api.yodelpass.com` only, public addresses, no redirects, no
cookies, 256 KiB at most).

Then:

- Look at what the parser made of each capture (the script prints it). If it is right, commit the files and list
  them in tests/fixtures/buntzen/README.md: every captured pair is re-parsed as a regression test by
  `tests/test_buntzen_parse.py`.
- If it is wrong (`{"error": "site_changed"}` for an answer that is fine, or the wrong pass), fix the parser in a
  1.0.x release, correct that capture's `parsed` by hand, and say so in the commit.
- Once the parser has read a real in-season answer correctly, set `offer: "everyone"` in the HelmRelease values.
  Confirm the stop times against BC Hydro's 2027 page at the same time (`STOP_AT` in `policy.py`).

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| Admin alert "Admin: site changed" (`site_changed`): "Buntzen Lake's answers can't be read" | Yodel answered with something the parser doesn't recognise | Capture it (above) and fix the parser. Pause Buntzen from the admin page meanwhile if it is noisy. |
| Admin alert "Admin: checker down" (`checker_down`): "waitlist-fetcher isn't answering" | Control can't reach the fetcher (its pod, or the network policy) | `kubectl -n waitlist logs deploy/waitlist-fetcher`. Users are not charged for our own outage. |
| `blocked` (401/403) or `rate_limited` (429) | Yodel refused us | Control cools Buntzen down by itself (30 and 5 minutes). If Yodel objects, pause the provider from the admin page; no deploy is needed. |
| `no_inventory` with the out-of-season note | Yodel offers nothing for that date | Expected outside the season. |

The fetcher logs one line per request (`fetch provider=buntzen id=… result=… error=… http_status=…
elapsed_ms=…`) and never a body, a header or the date asked for.
