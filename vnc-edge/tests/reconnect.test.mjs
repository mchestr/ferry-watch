// core/reconnect.js against close_codes.json (PROTOCOL.md §3.6, §3.7).
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { DELAYS, initialState, nextAction } from "../site/fw/viewer/core/reconnect.js";
import { forViewer, pack } from "./pack.mjs";

const codes = pack("close_codes.json");

test("the backoff is the pack's", () => {
  assert.deepEqual([...DELAYS], codes.delays);
});

describe("what each close leads to (close_codes.json)", () => {
  for (const c of forViewer(codes.cases)) {
    test(`${c.id} (§${c.sec})`, () => {
      let state = initialState();
      c.steps.forEach((step, i) => {
        const close = { opened: step.opened, ready: step.ready, code: step.code, deadlinePassed: step.deadline_passed };
        const next = nextAction(state, close);
        assert.deepEqual(
          { action: next.action, prominent: next.prominent },
          { action: step.action, prominent: step.prominent },
          `step ${i + 1}`,
        );
        state = next.state;
      });
    });
  }
});
