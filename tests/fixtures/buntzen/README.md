Synthesized from the Home Assistant automation's field names; not captured from Yodel. Replace with `captured-*` files from scripts/capture_yodel.py.

# Buntzen Lake fixtures (Yodel's `portal-catalog-items` answers)

**Every `synth-*` file here is synthesized.** None of them was captured from Yodel: the sandbox can't reach
`api.yodelpass.com`, and the 2026 season is over. They are built from the only two fields the owner's Home
Assistant automation read (`catalogItemExtendedResultLst[].catalogItemStatus` and `catalogItemCategory`), plus
the quirk it relied on (a half-day pass on sale has its period as its status, and a sold-out one says "Sold Out"
with no period). Their purpose is to pin the parser's rules (docs/waitlist/design.md §5.4), not to describe
Yodel. No file carries a marker key, since that would change the shape under test; this README is the label.

`tests/test_buntzen_parse.py` holds the expected result of each one, as below; `tests/test_capture_script.py`
checks that every file in this directory is named `synth-*` or `captured-*` (a capture's `.meta.json` companion
included) and is listed here.

"Place" is the query the body answers: 10671 is the all-day pass, 10672 the morning and afternoon passes. Every
body is served as HTTP 200 `application/json`, except `synth-error-page.html` (`text/html`). **SC** means
`parse_catalog` raises `FetchError("site_changed")`.

| File (synthesized) | Place | Body | `parse_catalog` items | Options (a watch for all three passes, inside its window) |
|---|---|---|---|---|
| `synth-fullday-available.json` | 10671 | the list key at the root, one item: category `All Day Pass`, status `Available` | `[full_day/open]` | full_day `open` |
| `synth-fullday-soldout.json` | 10671 | the same, status `Sold Out` | `[full_day/sold_out]` | full_day `sold_out` |
| `synth-bare-array.json` | either | a bare JSON array at the root | **SC** (the root must be an object) | |
| `synth-halfday-morning-open.json` | 10672 | categories `Morning`/`Afternoon`, statuses `Available`/`Sold Out` | `[morning/open, afternoon/sold_out]` | morning `open`, afternoon `sold_out` |
| `synth-halfday-ha-quirk.json` | 10672 | categories `Half Day Pass`; statuses `Morning` and `Sold Out` | `[morning/open, unknown/sold_out]` | morning `open`, afternoon `sold_out` |
| `synth-halfday-ha-quirk-both.json` | 10672 | categories `Half Day Pass`; statuses `Morning` and `Afternoon` | `[morning/open, afternoon/open]` | morning `open`, afternoon `open` |
| `synth-halfday-ha-quirk-soldout.json` | 10672 | categories `Half Day Pass`; both statuses `Sold Out` | `[unknown/sold_out, unknown/sold_out]` | morning `sold_out`, afternoon `sold_out` (**not** SC) |
| `synth-halfday-both-soldout.json` | 10672 | categories `Morning`/`Afternoon`, both `Sold Out` | `[morning/sold_out, afternoon/sold_out]` | morning `sold_out`, afternoon `sold_out` |
| `synth-wrapped-content.json` | 10672 | `synth-halfday-morning-open`'s list under `"content"` | as unwrapped | as unwrapped |
| `synth-wrapped-data.json` | 10672 | the same list under `"data"` | as unwrapped | as unwrapped |
| `synth-empty.json` | either | `{"catalogItemExtendedResultLst": []}` | `[]` | every option on that place `not_offered` (outcome `no_inventory`) |
| `synth-null-list.json` | either | `{"catalogItemExtendedResultLst": null}` | `[]` | as empty |
| `synth-unknown-status.json` | 10672 | category `Morning`, status `Waitlist only` | `[morning/unknown "Waitlist only"]` | morning `maybe` (detail `Waitlist only`), afternoon `not_offered` |
| `synth-quantity-only.json` | 10672 | category `Morning`, status `Ready`, `availableQuantity: 3` | `[morning/open]` (the quantity fallback) | morning `open`, afternoon `not_offered` |
| `synth-not-released.json` | 10671 | category `All Day Pass`, status `Coming Soon` | `[full_day/not_released]` | full_day `not_released` |
| `synth-changed-shape.json` | either | `{"items": []}` | **SC** (no list key anywhere) | |
| `synth-not-success.json` | either | `{"isSuccess": false, "catalogItemExtendedResultLst": []}` | **SC** (the API refused the request) | |
| `synth-halfday-unplaceable-open.json` | 10672 | two items, category `Half Day Pass`, statuses `Available`: no period word anywhere | **SC** (open, and we can't say which pass) | |
| `synth-status-not-string.json` | either | category `Morning`, `catalogItemStatus: 3` | **SC** | |
| `synth-error-page.html` | either | an HTML maintenance page, `text/html`, status 200 | **SC** | |
| `synth-huge.json` | either | 51 items | **SC** (more than 50 entries) | |

## Captured files

`uv run python scripts/capture_yodel.py --date YYYY-MM-DD` (run by the owner from the home network, never in
CI; see docs/providers/buntzen.md) writes, per response:

- `captured-<full|half>-<date>-<YYYYMMDDTHHMMSSZ>.json`: the body, pretty-printed (or `.txt`, raw, when it
  isn't JSON);
- `captured-<full|half>-<date>-<YYYYMMDDTHHMMSSZ>.meta.json`: `captured_at`, `status`, `content_type`, the
  `request` (`url`, `payload`) and what the parser made of it (`parsed`: the inventory, or `{"error": kind}`).
  No response headers are stored, so no cookie reaches the repo.

Every captured pair is a regression test as it stands (`test_buntzen_parse.py` re-parses each body with its
status and content type and expects `parsed`). When a capture shows the parser reading Yodel wrongly, fix the
parser, then correct that capture's `parsed` by hand and say so in the commit. List each captured file here,
with what it shows, under this heading.
