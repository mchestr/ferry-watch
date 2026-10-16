// core/protocol.js against messages.json, headers.json and usable.json's message numbers.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  Numbering,
  check,
  decode,
  encode,
  hello,
  parseHeader,
  proxyAutocomplete,
  resize,
} from "../site/fw/viewer/core/protocol.js";
import { bytes, forViewer, pack } from "./pack.mjs";

const messages = pack("messages.json");
const headers = pack("headers.json");
const usable = pack("usable.json");

/** Whether every field of `part` is in `whole`, recursively. */
function within(part, whole) {
  if (typeof part !== "object" || part === null) return Object.is(part, whole);
  if (typeof whole !== "object" || whole === null || Array.isArray(part) !== Array.isArray(whole)) return false;
  if (Array.isArray(part) && part.length !== whole.length) return false;
  return Object.keys(part).every((key) => Object.hasOwn(whole, key) && within(part[key], whole[key]));
}

describe("the encoder sends only what the agent carries out (messages.json cases)", () => {
  const frames = forViewer(messages.cases).filter((c) => !("hex" in c));
  for (const c of frames) {
    const msg = "raw" in c ? JSON.parse(c.raw) : c.msg;
    if (typeof msg !== "object" || msg === null || Array.isArray(msg)) continue; // the viewer only builds objects
    test(`${c.id} (§${c.sec}): ${c.outcome}`, () => {
      if (c.outcome === "ok") {
        assert.equal(check(msg), null);
        assert.deepEqual(JSON.parse(encode(msg)), msg);
      } else {
        assert.notEqual(check(msg), null);
        assert.throws(() => encode(msg), TypeError);
      }
    });
  }
});

describe("hello and resize come from the window (messages.json hello_env)", () => {
  for (const c of messages.hello_env) {
    test(`${c.id}`, () => {
      assert.deepEqual(hello(c.inner[0], c.inner[1], c.dpr), c.hello);
      const { v, ...size } = c.hello;
      assert.equal(v, 1);
      assert.deepEqual(resize(c.inner[0], c.inner[1], c.dpr), { ...size, t: "resize" });
      assert.equal(check(c.hello), null);
    });
  }
});

describe("the agent's messages are read as far as the viewer knows them (messages.json outbound)", () => {
  for (const c of forViewer(messages.outbound)) {
    test(`${c.id}: ${c.outcome}`, () => {
      const decoded = decode(JSON.stringify(c.msg));
      if (c.outcome === "accept") {
        assert.deepEqual(decoded, c.msg);
      } else if (c.outcome === "extra") {
        assert.notEqual(decoded, null);
        assert.ok(within(decoded, c.msg), "only the message's own values");
        assert.ok(!within(c.msg, decoded), "the unknown field is left behind");
      } else {
        assert.equal(decoded, null);
      }
    });
  }

  test("an agent bug is ignored, not trusted", () => {
    assert.equal(decode("{"), null);
    assert.equal(decode("[]"), null);
    assert.equal(decode(JSON.stringify({ t: "clock", ends: "soon", hard: 1, paused: false })), null);
    assert.equal(decode(JSON.stringify({ t: "pages", front: 1 })), null);
    assert.equal(decode(JSON.stringify({ t: "focus", page: 1, seq: 1, n: 1, f: { k: "text" }, popup: false })), null);
  });
});

describe("the keyboard proxy's autocomplete (messages.json autocomplete)", () => {
  for (const c of forViewer(messages.autocomplete)) {
    test(`${c.id}`, () => {
      assert.equal(proxyAutocomplete(c.ac, "text"), c.proxy);
      assert.equal(proxyAutocomplete(c.ac, "password"), "off");
    });
  }

  test("a token is never repaired into a credential token", () => {
    assert.equal(proxyAutocomplete("Kelvin user_name USERNAME new-password one-time-code", "text"), "one-time-code");
  });
});

describe("image headers (headers.json)", () => {
  for (const c of headers.cases) {
    test(`${c.id}`, () => {
      const message = bytes(c.header + c.jpeg);
      const parsed = parseHeader(message.buffer);
      assert.notEqual(parsed, null);
      const { first, jpeg, ...fields } = parsed;
      assert.deepEqual(fields, c.fields);
      assert.equal(first, c.first);
      assert.deepEqual(jpeg === null ? "" : Buffer.from(jpeg).toString("hex"), c.jpeg);
    });
  }

  for (const c of headers.malformed) {
    test(`${c.id} is refused`, () => {
      assert.equal(parseHeader(bytes(c.hex).buffer), null);
    });
  }

  test("only an ArrayBuffer is a binary message", () => {
    assert.equal(parseHeader(bytes(headers.cases[0].header + "ffd8")), null);
    assert.equal(parseHeader("01000100"), null);
  });
});

describe("message numbers (usable.json numbering)", () => {
  for (const c of usable.numbering) {
    test(`${c.id}`, () => {
      const numbering = new Numbering();
      assert.deepEqual(
        c.sent.map((t) => numbering.next(t)),
        c.numbers,
      );
      assert.equal(numbering.lastInput, c.last_input);
    });
  }
});
