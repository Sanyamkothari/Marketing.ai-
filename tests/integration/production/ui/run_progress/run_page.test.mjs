/* The campaign progress page moves on to the review by itself when the person watched the run finish, and only
   then: a run opened once it was done shows its page and its button. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, installPage, until } from "../harness.mjs";

const answers = [];
const record = (state) => ({
  record: { campaign_run_id: "cr_1", state, use_case_id: "win-back", campaign_id: "cmp_1", score_run_id: "s_1", steps: [] },
  pct: state === "done" ? 100 : 40,
  eta_seconds: null,
  summary: state === "done" ? "Done." : "Building the list.",
});
const { w } = installPage((request) => {
  if (request.method === "GET" && request.path === "/campaign-runs/cr_1") return { status: 200, body: answers.shift() || record("done") };
  return null;
}, { hash: "#/campaigns/run/cr_1" });
const page = await import("../../../../../ui/modules/campaigns/run_page.js");

test("watched to the end: the page goes on to the review by itself", async () => {
  answers.push(record("running"), record("done"));
  w.location.hash = "#/campaigns/run/cr_1";
  await page.renderRun($("#app"), "cr_1");
  await until(() => w.location.hash === "#/campaigns/new/review/cmp_1", page.RUN_POLL_MS + 3000, "the review");
});

test("a run opened once it was done shows its page and its button", async () => {
  answers.push(record("done"));
  w.location.hash = "#/campaigns/run/cr_1";
  await page.renderRun($("#app"), "cr_1");
  assert.ok($("[data-run-ready]"), "the button is there");
  assert.equal(w.location.hash, "#/campaigns/run/cr_1");
});
