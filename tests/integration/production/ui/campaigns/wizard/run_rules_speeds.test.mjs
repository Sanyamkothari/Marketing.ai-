/* The minutes shown for Quick / Balanced / Thorough are the server's (the whole run's expected time for each of `campaign_speeds` in configs/engine.yaml, sent with
   the model offer); 8, 20 and 45 stand until it has answered, and for anything that is not a positive number. Pure: no page. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { SPEEDS, aboutMinutes, applySpeedMinutes, speedOf, startSentenceHtml } from "../../../../../../ui/modules/campaigns/run_rules.js";

const minutes = () => SPEEDS.map((s) => s.minutes);

test("before the server has answered, the minutes are 8, 20 and 45", () => {
  assert.deepEqual(minutes(), [8, 20, 45]);
  assert.equal(aboutMinutes("balanced"), "up to 20 minutes");
});

test("the server's minutes replace them everywhere the speeds are read", () => {
  applySpeedMinutes({ quick: 2, balanced: 10, thorough: 60 });
  assert.deepEqual(minutes(), [2, 10, 60]);
  assert.equal(speedOf("quick").minutes, 2);
  assert.equal(aboutMinutes("balanced"), "up to 10 minutes");
  assert.match(startSentenceHtml({ table: "customers.csv", speed: "thorough" }), /Thorough: up to 60 minutes/);
});

test("a speed the server did not give, or gave badly, keeps its fallback", () => {
  applySpeedMinutes({ quick: 3, balanced: "soon", thorough: -1 });
  assert.deepEqual(minutes(), [3, 20, 45]);
  applySpeedMinutes(null);
  assert.deepEqual(minutes(), [8, 20, 45]);
  applySpeedMinutes(undefined);
  assert.deepEqual(minutes(), [8, 20, 45]);
});

test("the recommended speed and the names are unchanged", () => {
  assert.deepEqual(SPEEDS.map((s) => [s.id, s.name, Boolean(s.recommended)]), [
    ["quick", "Quick", false],
    ["balanced", "Balanced", true],
    ["thorough", "Thorough", false],
  ]);
});
