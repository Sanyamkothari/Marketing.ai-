/* The Approvals screen (Plan D M54) in the whole page, against REAL bodies (PB_FIXTURES, written by
   test_approvals_ui_js.py): an Approver approves a challenger with a reason after reading the
   head-to-head; the person who trained it sees the same challenger and cannot decide, with the
   server's sentence in place of the buttons. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, until } from "../harness.mjs";
import { installOps, ok, setField, submit } from "../ops/fake.mjs";

let approved = false;
const { w, calls } = installOps({
  tokens: { "tok-approver": fixture("me_approver"), "tok-trainer": fixture("me_trainer") },
  token: "tok-approver",
  hash: "#/approvals",
  table: [
    [
      "GET",
      "/approvals",
      (r) =>
        ok(
          r.auth === "Bearer tok-trainer"
            ? fixture("approvals_trainer")
            : approved
              ? fixture("approvals_after")
              : fixture("approvals_approver"),
        ),
    ],
    [
      "POST",
      "/models/{model_id}/approve",
      () => {
        approved = true;
        return ok(fixture("approve_ok"));
      },
    ],
  ],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");

const text = () => ($("#app") || {}).textContent || "";
const last = (method, path) => calls.filter((c) => c.method === method && c.path === path).pop();
const item = fixture("approvals_approver").items[0];
const modelId = item.version.model_id;

test("the Approver sees the head-to-head, with the metric the challenger is worse on marked", async () => {
  await until(() => $(`[data-approval="${modelId}"]`), 3000, "the challenger");
  assert.ok($('#pb-bar a[href="#/approvals"]'), "Approvals in the user bar for an Approver");
  for (const row of item.head_to_head.metrics) {
    const tr = $(`tr[data-metric="${row.metric}"]`);
    assert.ok(tr, `a row for ${row.metric}`);
    if (row.better) assert.equal(tr.querySelector("[data-better]").getAttribute("data-better"), row.better);
  }
  assert.ok($("tr.pb-worse"), "the metric the champion wins on is highlighted");
  assert.match(text(), new RegExp(`${item.head_to_head.rows_evaluated} held-out rows, both models`));
  assert.match(text(), new RegExp(`started by ${item.trained_by}`));
});

test("approving needs a reason, and then the challenger becomes champion", async () => {
  const form = $(".pb-decide");
  submit(w, form);
  await until(() => /REASON_REQUIRED/.test(text()), 2000, "the reason is asked for");
  assert.equal(last("POST", `/models/${modelId}/approve`), undefined, "nothing sent without a reason");
  setField(w, $(".pb-decide"), "reason", "beats the champion on ROC-AUC");
  submit(w, $(".pb-decide"));
  await until(() => /approved: it is now the champion/.test(text()), 3000, "the confirmation");
  const sent = last("POST", `/models/${modelId}/approve`);
  assert.equal(sent.body.reason, "beats the champion on ROC-AUC");
  assert.equal(sent.auth, "Bearer tok-approver");
  assert.match(text(), /No challenger is waiting for approval/);
});

test("the trainer sees why they cannot decide, and has no button to press", async () => {
  w.sessionStorage.setItem("marketing-ai.auth", JSON.stringify({ token: "tok-trainer", expires_at: null }));
  const session = await import("../../../../../ui/modules/production/session.js");
  session._resetForTests();
  w.location.hash = "#/";
  await until(() => !$("[data-approval]"), 2000, "left the screen");
  w.location.hash = "#/approvals";
  const blocked = fixture("approvals_trainer").items[0];
  await until(() => $(`[data-blocked="${modelId}"]`), 3000, "the refusal in place of the buttons");
  assert.equal($(`[data-blocked="${modelId}"]`).textContent, blocked.blocked_reason);
  assert.equal($$(".pb-decide").length, 0, "no approve or reject form for the trainer");
});
