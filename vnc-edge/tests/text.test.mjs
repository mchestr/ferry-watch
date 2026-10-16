// core/text.js against text.json (PROTOCOL.md §8.7, §11.4).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { check } from "../site/fw/viewer/core/protocol.js";
import {
  backspaced,
  composeMessages,
  dialogAnswer,
  edit,
  normalise,
  split,
  tail,
  textMessages,
} from "../site/fw/viewer/core/text.js";
import { forViewer, pack } from "./pack.mjs";

const { cases } = pack("text.json");

describe("typed and pasted text becomes these messages (text.json)", () => {
  for (const c of forViewer(cases)) {
    test(`${c.id} (§${c.sec})`, () => {
      if (c.as === "dialog") {
        assert.equal(dialogAnswer(c.input), c.s);
        assert.equal(check({ t: "dialog", id: 1, ok: true, s: dialogAnswer(c.input) }), null);
        return;
      }
      const sent = c.as === "compose" ? composeMessages(c.input) : textMessages(c.input, c.via);
      assert.deepEqual(sent, c.messages);
      for (const msg of sent) assert.equal(check(msg), null, "the agent carries out every one");
    });
  }
});

describe("normalise and split", () => {
  test("line breaks become LF; other controls, DEL, C1 and lone surrogates go", () => {
    assert.equal(normalise("a\r\nb\rc\n\td\u0007\u007f\u0080\ud800 "), "a\nb\nc\n\td ");
    assert.equal(normalise("a\r\nb\tc", { lines: false }), "abc");
  });

  test("chunks never split a surrogate pair or end in a LF that more text follows", () => {
    assert.deepEqual(split("ab\ncd", 3), ["ab", "\ncd"]);
    assert.deepEqual(split("😀😀😀", 2), ["😀😀", "😀"]);
    assert.deepEqual(split("abc\n", 4), ["abc\n"]);
    assert.deepEqual(split(""), []);
  });
});

describe("what the browser changed in the proxy", () => {
  test("deleted from the end, then inserted", () => {
    assert.deepEqual(edit("Mik", "Mike "), { removed: 0, added: "e " });
    assert.deepEqual(edit("I wnt", "I want "), { removed: 2, added: "ant " });
    assert.deepEqual(edit("ab😀", "ab"), { removed: 1, added: "" });
    assert.deepEqual(edit("", "Victoria"), { removed: 0, added: "Victoria" });
    assert.deepEqual(edit("same", "same"), { removed: 0, added: "" });
  });

  test("a Backspace deletes what it deletes in the pod; an edit inside an emoji types its rest again", () => {
    assert.deepEqual(edit("a👍🏽", "a"), { removed: 1, added: "" });
    assert.deepEqual(edit("a👍🏽b", "ax👍🏽b"), { removed: 2, added: "x👍🏽b" });
    assert.deepEqual(edit("a👍🏽", "a👍"), { removed: 1, added: "👍" });
    assert.deepEqual(edit("cafe\u0301", "cafe"), { removed: 1, added: "" });
    assert.deepEqual(edit("👨‍👩‍👧 hi", "👨‍👩‍👧"), { removed: 3, added: "" });
  });

  test("one Backspace deletes an emoji sequence or a flag whole, and combining marks one at a time", () => {
    // Measured with Backspace in the pod's Chromium (141): what each Backspace deleted, from the end.
    const vectors = {
      "a👍🏽": [2, 1],
      "ae\u0301": [1, 1, 1],
      "a🇨🇦🇺🇸": [2, 2, 1],
      "🇨🇦🇨": [1, 2],
      "a👨‍👩‍👧": [5, 1],
      "a👩🏽‍💻": [4, 1],
      "a1️⃣": [3, 1],
      "a#⃣": [2, 1],
      "a❤️": [2, 1],
      "a\ufe0f": [2],
      "a\u0915\u094d\u0937": [1, 1, 1, 1],
      "a🏽": [1, 1],
      "a😀": [1, 1],
      "": [],
    };
    for (const [s, expected] of Object.entries(vectors)) {
      const units = [];
      for (let rest = s; rest; rest = [...rest].slice(0, -units.at(-1)).join("")) units.push(backspaced(rest));
      assert.deepEqual(units, expected, JSON.stringify(s));
    }
  });

  test("the tail kept is counted in code points", () => {
    assert.equal(tail("a😀b", 2), "😀b");
    assert.equal(tail("ab", 5), "ab");
  });
});
