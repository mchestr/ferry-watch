# The checkout test site

Two sites the streamed viewer's browser tests (`tests/browser/test_checkout_stream.py`) serve to the pod's real
Chromium, on two loopback addresses so that the card iframe is cross-site, as a payment provider's is: its own
renderer process and its own CDP target. The test server writes the two origins into `{{SHOP}}` and `{{PAY}}`.

| Page | What |
|---|---|
| `shop/index.html` | The checkout: contact fields, a select, date and time fields, a textarea, a sticky header, buttons that open `alert`/`confirm`/`prompt`, a pop-up and a new tab, the card iframe and 70 sections to scroll. It logs its events in `window.__log` and its sizes in `window.__sizes` |
| `pay/card.html` | The card iframe: number (moves on to the expiry by itself once complete), expiry and CVC; a pop-up and an `alert` from inside the iframe |
| `pay/popup.html` | The bank's window: a one-time code and an Approve button that closes it |
| `shop/occl.html`, `pay/card3.html` | Fields under a `position: fixed` "Pay now" footer: a fully covered select, a half-covered one, a clear one, a custom select whose list never opens, covered and clear date fields, and selects in a cross-site iframe whose lower part is under the footer. The footer logs every click it gets in `window.log` |
| `shop/inner.html` | An app shell whose `<main>` scrolls, not the page |
| `shop/anim.html` | A spinner and a countdown: a page that keeps changing with nobody touching it |
| `shop/swap.html` | A page that swaps its component tree at a breakpoint in script (typed values are lost if it re-renders) |
| `shop/bomb.html` | A page that tries to flood whatever reads it: huge option labels, thousands of piled-up inputs, a huge title (sizes from the query string) |

The pages come from the design's spikes (DESIGN §3), with the origins made placeholders.
