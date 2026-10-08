/* Plan J M96: the Approver's advisory checks (`ApprovalItem.checks`, `engine/model_gates.py`) on the
   Approvals screen, against a REAL `GET /approvals` body (PB_FIXTURES, written by
   test_approvals_ui_js.py) whose item is given the three checks an uplift challenger carries. The
   screen draws the server's code, verdict and sentence - nothing computed in the browser - and the
   checks never take the Approve and Reject buttons away: they are advice. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, until } from "../harness.mjs";
import { installOps, ok } from "../ops/fake.mjs";

const body = fixture("approvals_approver");
const item = body.items[0];
const modelId = item.version.model_id;
const CHECKS = [
  {
    code: "UPLIFT_NOT_BETTER_THAN_RISK",
    passed: false,
    message:
      "This model does not beat risk ranking (by the model's own chance of the outcome without contact): the AUUC difference is 0.0004 (95% CI -0.0061 to 0.0072), a range that includes zero.",
  },
  {
    code: "UPLIFT_UNSTABLE_ACROSS_FOLDS",
    passed: null,
    message: "Not measured. Off by default: turn on uplift.evidence.fold_auuc to refit the model on each fold. Turning it on refits the model 5 times: about 40 seconds on this data.",
  },
  {
    code: "UPLIFT_MISCALIBRATED",
    passed: true,
    message: "Predicted uplift matches what was measured: 9 of 10 groups contain the prediction in their 95% range; the average gap is 1.2 points.",
  },
];
const withChecks = { ...body, items: [{ ...item, checks: CHECKS }, ...body.items.slice(1)] };

installOps({
  tokens: { "tok-approver": fixture("me_approver") },
  token: "tok-approver",
  hash: "#/approvals",
  table: [["GET", "/approvals", () => ok(withChecks)]],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");

test("each check is the server's verdict and sentence", async () => {
  await until(() => $(`[data-approval="${modelId}"] [data-check]`), 3000, "the checks");
  const rows = $$(`[data-approval="${modelId}"] [data-check]`);
  assert.deepEqual(
    rows.map((row) => [row.dataset.check, row.dataset.passed]),
    [
      ["UPLIFT_NOT_BETTER_THAN_RISK", "false"],
      ["UPLIFT_UNSTABLE_ACROSS_FOLDS", "null"],
      ["UPLIFT_MISCALIBRATED", "true"],
    ],
  );
  assert.match(rows[0].textContent, /Beats plain risk ranking/);
  assert.match(rows[0].textContent, /does not beat risk ranking/);
  assert.match(rows[0].querySelector(".pill").textContent, /Check/);
  assert.match(rows[1].querySelector(".pill").textContent, /Not measured/);
  assert.match(rows[1].textContent, /about 40 seconds/);
  assert.match(rows[2].querySelector(".pill").textContent, /Passed/);
  for (const [index, check] of CHECKS.entries()) assert.ok(rows[index].textContent.includes(check.message));
});

test("the checks are advice: the decision form is still there", async () => {
  await until(() => $(`.pb-decide[data-model="${modelId}"]`), 3000, "the decision form");
  assert.ok($(`[data-decision="approve"][data-model="${modelId}"]`));
});

test("an item without checks draws none", () => {
  const plain = body.items.slice(1);
  for (const other of plain) assert.equal($$(`[data-approval="${other.version.model_id}"] [data-check]`).length, 0);
});
