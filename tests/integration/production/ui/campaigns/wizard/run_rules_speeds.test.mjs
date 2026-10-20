/* The minutes shown for Quick / Balanced / Thorough are the server's (`campaign_speeds` in configs/engine.yaml, sent with
   the model offer); 5, 15 and 40 stand until it has answered, and for anything that is not a positive number. Pure: no page. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { SPEEDS, aboutMinutes, applySpeedMinutes, speedOf, startSentenceHtml } from "../../../../../../ui/modules/campaigns/run_rules.js";

const minutes = () => SPEEDS.map((s) => s.minutes);

test("before the server has answered, the minutes are 5, 15 and 40", () => {
  assert.deepEqual(minutes(), [5, 15, 40]);
  assert.equal(aboutMinutes("balanced"), "about 15 minutes");
});

test("the server's minutes replace them everywhere the speeds are read", () => {
  applySpeedMinutes({ quick: 2, balanced: 10, thorough: 60 });
  assert.deepEqual(minutes(), [2, 10, 60]);
  assert.equal(speedOf("quick").minutes, 2);
  assert.equal(aboutMinutes("balanced"), "about 10 minutes");
  assert.match(startSentenceHtml({ table: "customers.csv", speed: "thorough" }), /Thorough: about 60 minutes/);
});

test("a speed the server did not give, or gave badly, keeps its fallback", () => {
  applySpeedMinutes({ quick: 3, balanced: "soon", thorough: -1 });
  assert.deepEqual(minutes(), [3, 15, 40]);
  applySpeedMinutes(null);
  assert.deepEqual(minutes(), [5, 15, 40]);
  applySpeedMinutes(undefined);
  assert.deepEqual(minutes(), [5, 15, 40]);
});

test("the recommended speed and the names are unchanged", () => {
  assert.deepEqual(SPEEDS.map((s) => [s.id, s.name, Boolean(s.recommended)]), [
    ["quick", "Quick", false],
    ["balanced", "Balanced", true],
    ["thorough", "Thorough", false],
  ]);
});
