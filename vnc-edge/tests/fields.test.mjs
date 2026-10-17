// core/fields.js against hits.json and usable.json (PROTOCOL.md §8.3), and the keyboard proxy's attributes (§8.5).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  fieldAt,
  findSelect,
  keepsText,
  mapUsable,
  overlayFor,
  proxyAttributes,
  typable,
  withOk,
} from "../site/wl/viewer/core/fields.js";
import { forViewer, pack } from "./pack.mjs";

const hits = pack("hits.json");
const usable = pack("usable.json");
const CREDENTIALS = ["username", "current-password", "new-password", "webauthn"];

/** The case's map with its `fields_ok`s folded in, and whether it describes the case's image. */
function judged(c) {
  const map = c.oks.reduce(withOk, c.map);
  return { map, usable: mapUsable(map, c.image, c.last_input, c.shifted) };
}

describe("whether a map describes the drawn image (usable.json)", () => {
  for (const c of forViewer(usable.cases)) {
    test(`${c.id} (§${c.sec})`, () => {
      assert.equal(judged(c).usable, c.usable);
    });
  }
});

describe("what a tap hits, and which fields get a native picker (hits.json)", () => {
  for (const c of forViewer(hits.cases)) {
    test(`${c.id} (§${c.sec})`, () => {
      const { map, usable: ok } = judged(c);
      for (const tap of c.taps) {
        const field = ok ? fieldAt(map, tap.x, tap.y) : null;
        assert.equal(field?.id ?? null, tap.field, `(${tap.x}, ${tap.y})${tap.note ? `: ${tap.note}` : ""}`);
      }
      for (const { field: id, overlay } of c.overlays) {
        const field = map.f.find((f) => f.id === id);
        assert.equal(ok ? overlayFor(field, c.image) : null, overlay, `field ${id}`);
      }
    });
  }

  test("a fields_ok of another page or revision changes nothing", () => {
    const { map } = hits.cases[0];
    assert.equal(withOk(map, { t: "fields_ok", page: 2, rev: map.rev, seq: 99, n: 99 }), map);
    assert.equal(withOk(null, { t: "fields_ok", page: 1, rev: 1, seq: 1, n: 1 }), null);
  });
});

const FIELD = { k: "text", r: [0, 0, 10, 10], c: [0, 0, 10, 10], m: 511, im: "", ekh: "", cap: "", max: null };
const field = (changes) => ({ ...FIELD, type: "text", ac: "", ro: false, ...changes });

describe("the keyboard proxy's attributes (§8.5)", () => {
  test("a password field's proxy is KasmVNC's plain text input", () => {
    for (const ac of ["current-password", "new-password", "cc-number", ""]) {
      assert.deepEqual(proxyAttributes(field({ type: "password", ac, im: "numeric", ekh: "go", cap: "words" })), {
        type: "text",
        inputmode: "",
        autocomplete: "off",
        enterkeyhint: "",
        autocapitalize: "off",
        autocorrect: "off",
        spellcheck: "false",
      });
    }
  });

  test("it is never a password input, and never carries a credential token", () => {
    const types = ["text", "email", "tel", "url", "number", "password", "search", "date", "time", "textarea"];
    for (const type of types) {
      for (const ac of [...CREDENTIALS, "username email", "USERNAME cc-number", "webauthn one-time-code"]) {
        const attributes = proxyAttributes(field({ type, ac }));
        assert.notEqual(attributes.type, "password");
        for (const token of CREDENTIALS) assert.ok(!attributes.autocomplete.split(" ").includes(token), ac);
        assert.equal(attributes.autocorrect, "off");
        assert.equal(attributes.spellcheck, "false");
      }
    }
  });

  test("email, tel and url keep their type; everything else is text", () => {
    for (const type of ["email", "tel", "url"]) assert.equal(proxyAttributes(field({ type })).type, type);
    for (const type of ["text", "number", "search", "textarea", "contenteditable"]) {
      assert.equal(proxyAttributes(field({ type })).type, "text");
    }
  });

  test("the keyboard: inputmode from the page, a decimal pad for numbers", () => {
    assert.equal(proxyAttributes(field({ im: "numeric", ac: "cc-number" })).inputmode, "numeric");
    assert.equal(proxyAttributes(field({ type: "number" })).inputmode, "decimal");
    assert.equal(proxyAttributes(field({ type: "number", im: "numeric" })).inputmode, "numeric");
    assert.equal(proxyAttributes(field({})).inputmode, "");
    assert.equal(proxyAttributes(field({ ekh: "next" })).enterkeyhint, "next");
  });

  test("capitals: the page's, else words for names and addresses, none for machines' text", () => {
    const cap = (changes) => proxyAttributes(field(changes)).autocapitalize;
    assert.equal(cap({ cap: "characters", ac: "email" }), "characters");
    assert.equal(cap({ ac: "given-name" }), "words");
    assert.equal(cap({ ac: "cc-name" }), "words");
    assert.equal(cap({ ac: "shipping street-address" }), "words");
    assert.equal(cap({ ac: "email" }), "off");
    assert.equal(cap({ type: "email" }), "off");
    assert.equal(cap({ type: "url" }), "off");
    assert.equal(cap({ ac: "cc-number", im: "numeric" }), "off");
    assert.equal(cap({ ac: "cc-exp" }), "off");
    assert.equal(cap({ ac: "one-time-code" }), "off");
    assert.equal(cap({ ac: "postal-code" }), "characters");
    assert.equal(cap({}), "sentences");
  });

  test("only text and area fields that aren't read-only take typing", () => {
    assert.ok(typable(field({})));
    assert.ok(typable(field({ k: "area", type: "textarea" })));
    assert.ok(!typable(field({ ro: true })));
    assert.ok(!typable(field({ k: "select", type: "select" })));
    assert.ok(!typable(null));
  });

  test("the proxy keeps what is typed into any of them but a password", () => {
    for (const type of ["text", "email", "tel", "url", "number", "search"]) assert.ok(keepsText(field({ type })), type);
    assert.ok(keepsText(field({ im: "numeric", ac: "cc-number" })));
    assert.ok(keepsText(field({ k: "area", type: "textarea" })));
    assert.ok(!keepsText(field({ type: "password" })));
    assert.ok(!keepsText(field({ type: "password", ac: "cc-number" })));
    assert.ok(!keepsText(field({ ro: true })));
    assert.ok(!keepsText(field({ k: "date", type: "date" })));
    assert.ok(!keepsText(field({ k: "time", type: "time" })));
    assert.ok(!keepsText(null));
  });
});

describe("the select a focus report is about", () => {
  const map = hits.cases.find((c) => c.id === "occlusion-page").map;
  const [s1, , s2] = map.f;

  test("found by its place, or by its options when it alone has them", () => {
    assert.equal(findSelect(map, { ...s1, r: [20.5, 60, 300, 40] }), s1);
    assert.equal(findSelect(map, { ...s2, r: [20, 0, 300, 40] }), null, "two selects have those options");
    const month = map.f.find((f) => f.id === 6);
    assert.equal(findSelect(map, { ...month, r: [0, 0, 1, 1] }), month);
  });

  test("not for anything else", () => {
    assert.equal(findSelect(map, { ...s1, k: "date" }), null);
    assert.equal(findSelect(null, s1), null);
  });
});
