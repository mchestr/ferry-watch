// sheets.js: the dialog sheet's heading, as the browser words it (PROTOCOL.md §8.8, C41).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { heading } from "../site/wl/viewer/sheets.js";

const SHOP = "https://www.bcferries.com";
const PAY = "https://pay.example";
const pages = {
  t: "pages",
  front: 1,
  pages: [
    { id: 1, title: "Checkout", origin: SHOP, main: true },
    { id: 2, title: "", origin: "", main: false }, // a pop-up at about:blank
  ],
};
const dialog = (fields) => ({ t: "dialog", id: 1, page: 1, kind: "alert", msg: "Hi", def: "", ...fields });

describe("a dialog's heading", () => {
  const cases = [
    ["the page's main frame", { origin: SHOP, main: true }, `${SHOP} says:`],
    ["a main frame with no origin (an about:blank pop-up)", { page: 2, origin: "", main: true }, "This page says:"],
    ["a frame of the page's own origin", { origin: SHOP, main: false }, `${SHOP} says:`],
    ["a third party's frame", { origin: PAY, main: false }, `An embedded page at ${PAY} says:`],
    // Found in review: a third party's frame made a child at about:srcdoc, blob: or data:, whose dialog came with no
    // origin and was headed "This page says:", as if the checkout's own page were asking.
    ["a frame whose URL gives no origin", { origin: "", main: false }, "An embedded page on this page says:"],
    ["an agent that doesn't say which frame", { origin: "" }, "An embedded page on this page says:"],
    ["the page's origin, not said to be its main frame", { origin: SHOP }, `${SHOP} says:`],
    ["a main frame the page list doesn't know yet", { page: 9, origin: PAY, main: true }, `${PAY} says:`],
    [
      "a frame of a page the list doesn't know yet",
      { page: 9, origin: PAY, main: false },
      `An embedded page at ${PAY} says:`,
    ],
  ];
  for (const [name, fields, expected] of cases) {
    test(name, () => {
      assert.equal(heading(dialog(fields), pages), expected);
    });
  }

  test('only a main frame is ever "This page says:"', () => {
    for (const main of [false, undefined, "true", 1]) {
      assert.notEqual(heading(dialog({ origin: "", main }), pages), "This page says:", String(main));
      assert.notEqual(heading(dialog({ origin: "", main }), null), "This page says:", String(main));
    }
  });
});
