# Fixtures

Real bcferries.com pages, captured 2026-10-11 without logging in, so the parsers are tested
against what the site actually serves.

| File | What |
|---|---|
| `fare-selection-sold-out.html` | First 3 sailing cards of Swartz Bay → Tsawwassen on Mon Oct 12, 2026 (2 adults, standard vehicle), all "Advance bookings sold out" |
| `fare-selection-available.html` | First 3 sailing cards of the same route on Wed Oct 14, 2026 (1 adult, standard vehicle), all bookable, with their fare options |
| `route-info.json` | `GET /route-info`, trimmed to SWB, TSA, HSB and NAN and the routes between them |

To keep them small, hidden form inputs, scripts and popover text were removed. Nothing the
parsers read was changed. When BC Ferries changes its markup, capture fresh copies the same way
and update the expectations in the tests.

## `flow/`

The booking-flow pages the browser contract tests walk through, captured the same way:

| File | What |
|---|---|
| `route.html` | The home page's booking form (route pre-filled) |
| `passengers.html`, `vehicle.html` | The passenger and vehicle steps |
| `login.html` | The login page |
| `fare-selection.html` | The same cards as `../fare-selection-available.html` |
| `review.html`, `my-account.html` | Synthetic: the text the real pages show, without the account details |

Scripts, external stylesheets, modals, navigation and the footer were removed. A small `<style>`
re-adds the few Bootstrap rules that hide things (`.modal`, `.hidden`, collapsed sections), so
what's visible matches the real site.

## `vnc-edge-0.6.4/`

The checkout landing page and its script exactly as vnc-edge 0.6.4 served them (release commit 1fd4302).
During a rolling update of vnc-edge, or a rollback, a page and its script can come from different versions:
`tests/browser/test_checkout_edge.py` opens the checkout with each pairing.
