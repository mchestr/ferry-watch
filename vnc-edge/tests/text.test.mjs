// core/text.js against text.json (PROTOCOL.md §8.7, §11.4).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { check } from "../site/fw/viewer/core/protocol.js";
import { composeMessages, dialogAnswer, normalise, split, textMessages } from "../site/fw/viewer/core/text.js";
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
