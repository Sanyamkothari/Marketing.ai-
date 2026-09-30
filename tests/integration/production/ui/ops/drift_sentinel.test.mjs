/* The drift sentinel (DEC-1210, DEC-1211): a scoring run's "What moved" table and drifted notice with
   its one-click "Retrain on recent data", and the same button on a drift alert. The drift report is
   shaped as compute_drift writes it; the bodies the fake API answers with for the alerts screen are the
   real ones (PB_FIXTURES) with one alert turned into a drift alert. Each request is checked for what it
   sent. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, until } from "../harness.mjs";
import { created, installOps, ok, refused } from "./fake.mjs";

const tokens = { "tok-analyst": fixture("me_analyst") };
const openAlerts = fixture("alerts_open").alerts;
const driftAlert = { ...openAlerts[0], kind: "drift_above_threshold", use_case_id: "targeted-advertisement", client_id: "cl_1", acknowledged_at: null };
const otherAlert = { ...openAlerts[0], alert_id: "al_other", kind: "performance_drop", acknowledged_at: null };
let retrainReply = () => created({ firing_id: "fir_1", schedule_id: "sch_1", kind: "retrain", status: "running", run_id: "r_20260930_00000001" });

const { w, calls } = installOps({
  tokens,
  token: "tok-analyst",
  hash: "#/monitoring/alerts",
  table: [
    ["GET", "/industries", () => ok(fixture("industries"))],
    ["GET", "/monitoring/alerts", () => ok({ alerts: [driftAlert, otherAlert] })],
    ["POST", "/schedules/retrain-now", () => retrainReply()],
    ["GET", "/audit/events", () => ok({ events: [], total: 0, offset: 0, limit: 50 })],
  ],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");
const pages = await import("../../../../../ui/pages.js");

const last = (method, path) => calls.filter((c) => c.method === method && c.path === path).pop();

const feature = (name, psi, status, extra = {}) => ({
  feature: name,
  psi,
  status,
  null_rate_baseline: 0,
  null_rate_current: 0,
  ...extra,
});
const drift = {
  run_id: "r_score",
  baseline_run_id: "r_train",
  status: "drifted",
  max_psi: 0.41,
  threshold: 0.2,
  drifted_features: ["monthly_spend", "plan_type"],
  features: [
    feature("plan_type", 0.3, "drifted", { what_moved: "Share of plan_type = 'prepaid' went from 31% to 48%" }),
    feature("monthly_spend", 0.41, "drifted", { what_moved: "Average monthly_spend moved from 42.1 to 53.0 (+26%)" }),
    feature("tenure", 0.12, "watch"), // an older report: no line, so a dash
    feature("age", 0.01, "stable"),
  ],
};
const uc = { id: "targeted-advertisement", name: "Targeted Advertisement", pages: { output: "Output" }, output: { kpi: { label: "Customers" } }, target: {}, problem_type: "binary_classification", advanced_settings: { stages: [] }, setup: {} };
const run = { run_id: "r_score", mode: "score", state: "done", client_id: "cl_1" };
const summary = { rows_scored: 100, bands: [], actions: [], suppressed: [], kpi: { label: "Customers", value: 3, display: "3" }, drift_status: "drifted" };

function outputPage(report) {
  const host = w.document.createElement("div");
  host.innerHTML = pages.renderPage("output", uc, run, { "scoring_summary.json": { ...summary, drift_status: report.status }, "drift.json": report }, "#");
  w.document.body.appendChild(host);
  pages.bindPage("output", host);
  return host;
}

test("the drifted notice names the top moved features and the table lists them by PSI with what moved", () => {
  const host = outputPage(drift);
  const notice = host.querySelector('[data-code="DRIFT_DRIFTED"]');
  assert.match(notice.textContent, /Moved the most: monthly_spend, plan_type and tenure\./);
  const rows = [...host.querySelectorAll("table tr")].map((tr) => [...tr.children].map((c) => c.textContent));
  const moved = rows.filter((r) => ["monthly_spend", "plan_type", "tenure", "age"].includes(r[0]));
  assert.deepEqual(moved.map((r) => r[0]), ["monthly_spend", "plan_type", "tenure"], "most PSI first; a stable feature is left out");
  assert.equal(moved[0][2], "Average monthly_spend moved from 42.1 to 53.0 (+26%)");
  assert.equal(moved[1][2], "Share of plan_type = 'prepaid' went from 31% to 48%");
  assert.equal(moved[2][2], "—", "no number in the report, so no sentence");
  host.remove();
});

test("a watch-level report has the table but no retrain button; a stable one has neither", () => {
  const watch = outputPage({ ...drift, status: "watch", features: [feature("tenure", 0.12, "watch")] });
  assert.ok(watch.textContent.includes("tenure"));
  assert.equal(watch.querySelector("[data-retrain]"), null);
  watch.remove();
  const calm = outputPage({ ...drift, status: "stable", features: [feature("age", 0.01, "stable")] });
  assert.equal(calm.querySelector("[data-retrain]"), null);
  assert.ok(!calm.textContent.includes("What moved"));
  calm.remove();
});

test("Retrain on recent data on the run page posts the use case and client, and says the model awaits approval", async () => {
  const host = outputPage(drift);
  host.querySelector("[data-retrain]").click();
  await until(() => /Retraining has started/.test(host.textContent), 2000, "the started line");
  assert.deepEqual(last("POST", "/schedules/retrain-now").body, { use_case_id: "targeted-advertisement", client_id: "cl_1" });
  assert.match(host.textContent, /waits for an approver/);
  assert.ok(host.querySelector('a[href="#/uc/targeted-advertisement/model/r_20260930_00000001"]'));
  host.remove();
});

test("with no labelled data the button says so plainly and stays usable", async () => {
  const sentence = "There is no labelled data to retrain on: no saved recipe defines the outcome to learn.";
  retrainReply = () => refused(409, { detail: { code: "NO_TRAINING_DATA", message: sentence, path: null } });
  const host = outputPage(drift);
  host.querySelector("[data-retrain]").click();
  await until(() => host.textContent.includes(sentence), 2000, "the refusal");
  assert.equal(host.querySelector("[data-retrain]").disabled, false);
  assert.ok(!/Retraining has started/.test(host.textContent), "nothing pretends a run began");
  host.remove();
});

test("only a drift alert offers the click, and it reports the started run", async () => {
  retrainReply = () => created({ firing_id: "fir_2", schedule_id: "sch_1", kind: "retrain", status: "running", run_id: "r_20260930_00000002" });
  await until(() => $$("tr[data-alert]").length === 2, 3000, "the alerts");
  assert.equal($$("#app [data-retrain]").length, 1, "the performance-drop alert has none");
  const button = $(`tr[data-alert="${driftAlert.alert_id}"] [data-retrain]`);
  assert.ok(button, "a drift alert has the button");
  button.click();
  await until(() => /Retraining has started/.test($("#app").textContent), 2000, "the started line");
  assert.deepEqual(last("POST", "/schedules/retrain-now").body, { use_case_id: "targeted-advertisement", client_id: "cl_1" });
  assert.ok($('#app a[href="#/monitoring/runs/r_20260930_00000002"]'));
});
