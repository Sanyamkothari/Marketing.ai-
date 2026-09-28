/* The Results badge in the tab that trained the model (docs/UI_AUDIT.md §8.4 item 1), against REAL
   `GET /approvals` bodies (PB_FIXTURES, written by test_approvals_ui_js.py). The count used to be read
   once per sign-in, so a model trained in this tab never showed on Results until a reload. It is read
   again when a training run finishes and when Results opens. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, fixture, settle, until } from "../harness.mjs";
import { installOps, ok } from "../ops/fake.mjs";

let answer = fixture("approvals_after"); // nothing waiting yet
const { w, calls } = installOps({
  tokens: { "tok-approver": fixture("me_approver") },
  token: "tok-approver",
  hash: "#/connections",
  table: [["GET", "/approvals", () => ok(answer)]],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");
const { RUN_FINISHED_EVENT } = await import("../../../../../ui/dom.js");

const badge = () => {
  const count = $('#pb-bar a.tn-item[href="#/results"] .count');
  return count ? count.textContent : "";
};
const asked = () => calls.filter((c) => c.method === "GET" && c.path === "/approvals").length;
const finished = (run) => w.dispatchEvent(new w.CustomEvent(RUN_FINISHED_EVENT, { detail: run }));

const waitingNow = fixture("approvals_approver").items.filter((item) => item.can_decide).length;

test("the fixtures differ: one model to decide, then none", () => {
  assert.ok(waitingNow > 0);
  assert.equal(fixture("approvals_after").items.filter((item) => item.can_decide).length, 0);
});

test("signed in with nothing waiting, Results has no badge", async () => {
  await until(() => asked() > 0, 3000, "the first count");
  await settle(3);
  assert.equal(badge(), "");
});

test("a training run that finishes in this tab puts its model on the badge", async () => {
  answer = fixture("approvals_approver");
  const before = asked();
  finished({ run_id: "r1", mode: "score", state: "done" });
  await settle(3);
  assert.equal(asked(), before, "a scoring run makes no model to approve: no call");
  finished({ run_id: "r2", mode: "train", state: "failed" });
  await settle(3);
  assert.equal(asked(), before, "a failed training run makes no model either");
  finished({ run_id: "r3", mode: "train", state: "done" });
  await until(() => badge() === String(waitingNow), 3000, "the badge after training");
});

test("opening Results reads the count again", async () => {
  answer = fixture("approvals_after"); // decided elsewhere, in another tab
  const before = asked();
  w.location.hash = "#/results";
  await until(() => asked() > before, 3000, "a recount on Results");
  await until(() => badge() === "", 3000, "the badge cleared");
});
