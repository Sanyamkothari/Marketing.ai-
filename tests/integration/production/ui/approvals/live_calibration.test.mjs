/* Plan J M106: the Approver's "how the model that chose the last list did on that campaign" block
   (`ApprovalItem.live_calibration`, `engine/measurement/learn.py`) on the Approvals screen, against a
   REAL `GET /approvals` body (PB_FIXTURES, written by test_approvals_ui_js.py) whose item is given the
   block a challenger learned from a campaign carries - here the planted miscalibration of
   tests/integration/measurement/test_learn_from_cycle.py: the model that chose the list predicted a
   large gain for its top tenth, and the campaign measured a loss. The screen draws the server's sentence, numbers and
   verdicts (only the unit changes, a share as points), and the block is advice: the Approve and Reject
   buttons stay. An item without the block draws none. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, until } from "../harness.mjs";
import { installOps, ok } from "../ops/fake.mjs";

const body = fixture("approvals_approver");
const item = body.items[0];
const modelId = item.version.model_id;
const SUMMARY =
  "For the tenth it ranked highest the model that chose the last list predicted +21.9 points; the last campaign measured -29.8 points (95% range -35.1 points to -24.0 points). In 1 of 10 tenths the measured range holds the prediction, so its predictions do not match what the campaign measured.";
const decile = (n, predicted, value, low, high, inside) => ({
  schema_version: 1,
  decile: n,
  rows: 671,
  predicted,
  measured: { schema_version: 1, value, ci_low: low, ci_high: high, confidence_level: 0.95 },
  inside_range: inside,
});
const BLOCK = {
  schema_version: 1,
  source_run_id: "r_20261009_6a000002",
  model_id: "m_win-back-campaign_1",
  rows: 6710,
  deciles: [
    decile(1, 0.219, -0.298, -0.351, -0.24, false),
    decile(2, 0.204, -0.21, -0.27, -0.15, false),
    decile(3, 0.003, 0.006, -0.02, 0.031, true),
    ...[4, 5, 6, 7, 8, 9].map((n) => decile(n, -0.005, 0.05, 0.02, 0.08, false)),
    { schema_version: 1, decile: 10, rows: 671, predicted: -0.2, measured: null, inside_range: null },
  ],
  deciles_with_range: 9,
  deciles_inside: 1,
  matches: false,
  average_gap: 0.21,
  resamples: 50,
  summary: SUMMARY,
};
const withBlock = { ...body, items: [{ ...item, live_calibration: BLOCK }, ...body.items.slice(1)] };

installOps({
  tokens: { "tok-approver": fixture("me_approver") },
  token: "tok-approver",
  hash: "#/approvals",
  table: [["GET", "/approvals", () => ok(withBlock)]],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");

test("the block is the server's sentence and verdict, for the run the model learned from", async () => {
  await until(() => $(`[data-approval="${modelId}"] [data-live]`), 3000, "the live block");
  const block = $(`[data-approval="${modelId}"] [data-live]`);
  assert.equal(block.dataset.live, "r_20261009_6a000002");
  assert.equal(block.dataset.matches, "false");
  assert.ok(block.textContent.includes(SUMMARY));
  assert.match(block.textContent, /How the model that chose the last list did on that campaign/);
});

test("each tenth shows the server's predicted and measured change as points, with its range", () => {
  const rows = $$(`[data-approval="${modelId}"] [data-decile]`);
  assert.equal(rows.length, 10);
  assert.deepEqual(
    rows.map((row) => [row.dataset.decile, row.dataset.inside]),
    [
      ["1", "false"],
      ["2", "false"],
      ["3", "true"],
      ["4", "false"],
      ["5", "false"],
      ["6", "false"],
      ["7", "false"],
      ["8", "false"],
      ["9", "false"],
      ["10", "null"],
    ],
  );
  const top = rows[0].textContent;
  assert.match(top, /\+21\.9 points/);
  assert.match(top, /-29\.8 points/);
  assert.match(top, /-35\.1 points to -24\.0 points/);
  assert.match(top, /Does not hold it/);
  assert.match(rows[9].textContent, /Not measured/);
});

test("the block is advice: the decision form is still there", async () => {
  await until(() => $(`.pb-decide[data-model="${modelId}"]`), 3000, "the decision form");
  assert.ok($(`[data-decision="approve"][data-model="${modelId}"]`));
});

test("an item without the block draws none", () => {
  for (const other of body.items.slice(1)) {
    assert.equal($$(`[data-approval="${other.version.model_id}"] [data-live]`).length, 0);
  }
});
