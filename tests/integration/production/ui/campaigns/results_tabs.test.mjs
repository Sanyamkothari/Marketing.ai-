/* The Review page's tabs: Data | Model | Output drawn by ui/pages.js inside the campaign's Review page, the Data Doctor's
   "What the Data Doctor changed" card on the Data tab (from a recorded-shape case screen), and the tabs' wiring (one
   panel at a time, the runs' own pages filled in once). No file is offered by the tabs: the download is Approve's.
   Run through tests/integration/test_campaign_run_results.py. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>`, {
  url: "http://localhost/ui/#/campaigns/new/review/c_1",
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const screen = JSON.parse(fs.readFileSync(new URL("./doctor_screen.json", import.meta.url), "utf8"));
const DC = await import("../../../../../ui/modules/campaigns/doctor_changes.js");
const RT = await import("../../../../../ui/modules/campaigns/results_tabs.js");
const RM = await import("../../../../../ui/modules/campaigns/results_mount.js");
const { findNeverShown } = await import("../../../../../ui/labels.js");

const app = document.getElementById("app");
const $ = (sel) => app.querySelector(sel);
const $$ = (sel) => [...app.querySelectorAll(sel)];
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);

const describe = (name) => (name === "orders_365d" ? "Orders in the last 365 days" : null);
const changes = () => DC.doctorChanges(screen, { entity: "shopper", describe, outcome: { name: "reactivated", text: "The shopper placed another order in the 90 days after the snapshot" } });
const draw = (c = changes()) => {
  app.innerHTML = DC.doctorChangesHtml(c, "case_1");
  return app;
};

test("the card lists every kind of change the approved plan made, grouped", () => {
  draw();
  assert.equal(text($("h3")), "What the Data Doctor changed");
  assert.match(text($("[data-dd-funnel]")), /^541,909 rows in, 1,402 rows out: 4,372 shoppers became 1,402, 39\.4% of them with the outcome\.$/);
  assert.deepEqual(
    $$("[data-dd-group]").map((g) => g.getAttribute("data-dd-group")),
    ["fixes", "rows", "removed", "added", "joined", "decisions", "approved"],
  );
  assert.match(text($("[data-dd-group=fixes] + ul")), /Read 'InvoiceDate' as dates/);
  const rows = $$("[data-dd-group=rows] + ul li").map(text);
  assert.match(rows[0], /as-of date and leave out the 25,525 rows dated after it\.\s*Rows 541,909 → 516,384 · Shoppers 4,372 → 4,331$/, "the as-of cut-off, with its before and after");
  assert.match(rows[1], /^Audience: Only 1,402 shoppers who have gone quiet .*\s*Rows 516,384 → 1,402 · Shoppers 4,331 → 1,402$/, "the audience choice");
  assert.match(text($("[data-dd-removed]")), /InvoiceNo, StockCode, Description/);
  const added = $$("[data-dd-group=added] + p + ul li").map(text);
  assert.ok(added.some((l) => /Days since last order\s*days_since_last_order/.test(l)), "an engineered column is listed by name");
  assert.ok(added.some((l) => /Orders 365d\s*orders_365d\s*Orders in the last 365 days/.test(l)), "with the goal's description where there is one");
  assert.ok(added.some((l) => /Reactivated reactivated\s*What the model predicts\s*The shopper placed another order/.test(l)), "the outcome column the build made");
  assert.match(text($("[data-dd-group=joined] + ul")), /^Training table: one row per shopper .*Rows 516,384 → 1,402/);
});

test("the person's own decisions, and who approved and when", () => {
  draw();
  const decided = $$("[data-dd-group=decisions] + ul li").map(text);
  assert.ok(decided.some((l) => /Which column names the customers in it\?\s*You chose: CustomerID/.test(l)));
  assert.ok(decided.some((l) => /Leave them out and use 2011-11-30 as the as-of date\?\s*You chose: Leave them out/.test(l)));
  assert.ok(decided.some((l) => /^Checked and kept as it is: 4,989 rows .* exact copies/.test(l)), "a flag the person looked at and kept as it is");
  const approved = $$("[data-dd-group=approved] + ul li").map(text);
  assert.equal(approved.length, 3);
  assert.match(approved[0], /^local-operator approved this on 30 Oct 2026/);
  assert.match(approved[2], /Prepare 1 file with 5 changes/);
  assert.equal($("[data-dd-plan]").getAttribute("href"), "#/data/check/case/case_1");
  assert.equal(text($("[data-dd-plan]")), "See the full approved plan");
});

test("no card without a case; a case that cannot be read says so and keeps the link; a plan that changed nothing says so", () => {
  assert.equal(DC.doctorChangesHtml(changes(), null), "");
  draw(null);
  assert.match(text($("[data-dd-error]")), /could not be loaded just now/);
  assert.ok($("[data-dd-plan]"));
  assert.equal(DC.doctorChanges(null), null);
  draw(DC.doctorChanges({ case: { items: [], approvals: [] }, view: {} }));
  assert.match(text($("[data-dd-none]")), /made no change to the file itself/);
  assert.equal($("[data-dd-funnel]"), null, "no count is made up");
});

test("only names, counts and the plan's own sentences: no cell value, no raw number noise, plain words", () => {
  draw();
  const words = text($("[data-dd-changes]"));
  assert.doesNotMatch(words, /undefined|NaN|\[object Object\]/);
  assert.doesNotMatch(words, /WHITE HANGING|United Kingdom|17850/, "no cell value of the file");
  // "Training table" is the Data Doctor's own sentence for the table it built, as its own screen words it
  assert.deepEqual(Object.keys(findNeverShown(words)), ["training"]);
});

test("many added columns: a dozen in view and the rest one click away", () => {
  const wide = JSON.parse(JSON.stringify(screen));
  wide.view.preview.files[0].columns = Array.from({ length: 20 }, (_, i) => ({ name: `feature_${i}` }));
  draw(DC.doctorChanges(wide, { entity: "shopper" }));
  assert.equal($$("[data-dd-group=added] + p + ul > li").length, 12);
  assert.match(text($("[data-dd-more] summary")), /Show the other 8 columns/);
});

test("every accepted change is on the card, also those no question asked about: the key, consent, as-of date, outcome, privacy and a new column", () => {
  const item = (item_id, kind, title, payload = {}) => ({ item_id, kind, state: "accepted", superseded_by: null, title, payload });
  const bare = {
    case: {
      approvals: [],
      items: [
        item("key.1", "key", "CustomerID names the customers", { column: "CustomerID" }),
        item("consent.1", "consent_column", "Marketing_OptIn says who agreed to be contacted", { column: "Marketing_OptIn" }),
        item("asof.1", "as_of_column", "InvoiceDate is the as-of date", { column: "InvoiceDate" }),
        item("outcome.1", "outcome_column", "Reactivated is what the model predicts", { column: "reactivated" }),
        item("pii.1", "mask_pii", "Email is kept away from the AI", { column: "Email" }),
        item("derive.1", "recipe_step", "Add orders_90d: the orders in the 90 days before the snapshot", { step: { kind: "derive", column: "orders_90d" } }),
        { ...item("old.1", "key", "OldID names the customers"), superseded_by: "key.1" },
      ],
    },
    view: { review: { plan: [], decisions: [] } },
  };
  draw(DC.doctorChanges(bare, { entity: "shopper" }));
  const all = text($("[data-dd-changes]"));
  for (const line of [/CustomerID names the customers/, /Marketing_OptIn says who agreed/, /InvoiceDate is the as-of date/, /Reactivated is what the model predicts/, /Email is kept away from the AI/, /Add orders_90d/]) {
    assert.match(all, line);
  }
  assert.doesNotMatch(all, /OldID/, "a change a newer proposal replaced is not in force");
  assert.deepEqual(
    $$("[data-dd-group]").map((g) => g.getAttribute("data-dd-group")),
    ["fixes", "rows", "read", "outcome", "privacy"],
  );
});

test("a reader without the file preview still sees the columns the model learns from, and is told why the before list is missing", () => {
  const noPreview = JSON.parse(JSON.stringify(screen));
  noPreview.view.preview = null;
  draw(DC.doctorChanges(noPreview, { entity: "shopper", describe, outcome: { name: "reactivated", text: "The shopper came back" }, trainingColumns: ["days_since_last_order", "orders_365d", { name: "spend_90d" }] }));
  assert.match(text($("[data-dd-needs-analyst]")), /Analyst role/);
  const added = $$("[data-dd-group=added] ~ ul li").map(text);
  assert.ok(added.some((l) => /days_since_last_order/.test(l)));
  assert.ok(added.some((l) => /spend_90d/.test(l)));
  assert.ok(added.some((l) => /reactivated\s*What the model predicts\s*The shopper came back/.test(l)), "the outcome column");
  assert.equal($("[data-dd-removed]"), null, "no before list, so no made-up removals");
  draw(DC.doctorChanges(noPreview, { entity: "shopper" }));
  assert.equal($("[data-dd-needs-analyst]"), null, "nothing to explain when no column is drawn");
});

// --- the tabs' wiring ---------------------------------------------------------------------------------------

const useCase = { id: "retail-win-back", name: "Retail Win-back", marker: "p", entity: "Shopper", journey: "Win back", pages: { data: "Data", model: "Model", output: "Output" }, config: {} };
const runs = {
  r_train: { run_id: "r_train", mode: "train", state: "done", use_case_id: "retail-win-back", target: "reactivated", finished_at: "2026-10-30T06:30:00Z" },
  r_score: { run_id: "r_score", mode: "score", state: "done", use_case_id: "retail-win-back", finished_at: "2026-10-30T06:35:00Z" },
};
const artefacts = {
  "r_score/scoring_summary.json": { rows_scored: 1402, kpi: { label: "Shoppers worth an offer", value: 148 }, bands: [{ name: "High", action: "Best offer", rows: 79, share_pct: 5.6 }] },
  "r_train/leaderboard.json": {
    metric_label: "PR-AUC",
    models_trained: 7,
    time_limit_seconds: 900,
    best_model_name: "WeightedEnsemble_L2",
    entries: Array.from({ length: 7 }, (_, i) => ({ rank: i + 1, model_name: i ? `LightGBM_${i}` : "WeightedEnsemble_L2", family_label: i ? "LightGBM" : null, validation_score: 0.5 - i / 100, test_score: 0.55, fit_time_seconds: 1.5 })),
  },
  "r_train/best_model.json": { model_name: "WeightedEnsemble_L2", display_name: "Ensemble (Logistic Regression + XGBoost)", hyperparameters: { n_bins: 5 }, hyperparameters_summary: "weighted ensemble" },
  "r_train/evaluation.json": { primary_metric: "pr_auc", primary_metric_label: "PR-AUC", headline_score: 0.53, metrics: [], threshold: 0.8, threshold_detail: "THRESHOLD_FALLBACK", calibration: { method: "isotonic" } },
  "r_train/split.json": {
    type: "random_stratified",
    group_column: null,
    parts: [
      { name: "train", rows: 982, share: 0.7004, positive_rate: 0.3931, start_date: null, end_date: null },
      { name: "validation", rows: 210, share: 0.1498, positive_rate: 0.3952, start_date: "2026-01-01", end_date: "2026-02-01" },
      { name: "test", rows: 210, share: 0.1498, positive_rate: 0.3952, start_date: null, end_date: null },
    ],
  },
  "r_train/decile_lift.json": { unit: "x", values: [1.9, 1.7], bins: [{ label: "D1" }, { label: "D2" }] },
};
const requested = [];
globalThis.fetch = async (input) => {
  const { pathname } = new URL(String(input));
  requested.push(pathname);
  const json = (body, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
  const art = /^\/runs\/([^/]+)\/artefacts\/(.+)$/.exec(pathname);
  if (art) {
    const found = artefacts[`${art[1]}/${decodeURIComponent(art[2])}`];
    return found ? json(found) : json({ detail: { code: "NOT_FOUND", message: "no" } }, 404);
  }
  const run = /^\/runs\/([^/]+)$/.exec(pathname);
  if (run) return json({ run: runs[run[1]] });
  if (pathname === "/use-cases/retail-win-back") return json(useCase);
  if (pathname === "/cases/case_1/screen") return json(screen);
  if (pathname === "/cases/case_2/screen") return json({ ...screen, view: { ...screen.view, preview: null } });
  return json({ detail: { code: "NOT_FOUND", message: "no" } }, 404);
};

const ctx = { useCaseId: "retail-win-back", trainRunId: "r_train", scoreRunId: "r_score", caseId: "case_1" };
const settle = () => new Promise((resolve) => setTimeout(resolve, 60));

test("the tabs show one panel at a time and fill the training and scoring runs' own pages", async () => {
  app.innerHTML = RT.resultsTabsHtml({ list: { total_rows: 1402, treat_rows: 129 }, campaign: { use_case_id: "retail-win-back" } });
  RM.mountResultsTabs(app, ctx);
  assert.match(text($("[data-cw-slot=data]")), /Loading/);
  await settle();
  // Data: the training run's page, with the Doctor's card, and no header, tab bar or Next button of its own
  assert.ok($("[data-cw-panel=data] [data-dd-changes]"));
  assert.equal($("[data-cw-panel=data] .tabs-bar"), null);
  assert.equal($("[data-cw-panel=data] .pg-next"), null);
  assert.equal($("[data-cw-panel=data] h1"), null);
  // the split into learning, choosing and testing parts: rows, outcome share and dates
  const split = $$("[data-cw-panel=data] .card").find((c) => /How the rows were split/.test(c.textContent));
  const parts = [...split.querySelectorAll("tbody tr")].map(text);
  assert.match(parts[0], /To learn from.*982.*70%.*39%.*Chosen at random/);
  assert.match(parts[1], /To choose the model.*210.*15%.*40%.*2026/);
  assert.match(parts[2], /To test it.*210/);
  // Model: the model search is in view with the chosen one marked; hyperparameters and the cut-off sit in "More details"
  assert.equal($$("[data-cw-panel=model] [data-leaderboard] tbody tr").length, 5);
  assert.match(text($("[data-cw-panel=model] [data-leaderboard] tbody tr")), /Ensemble \(Logistic Regression \+ XGBoost\) Chosen/);
  assert.equal($$("[data-cw-panel=model] [data-chosen]").length, 1);
  assert.match(text($("[data-cw-panel=model] [data-leaderboard-more] summary")), /Show the other 2 models/);
  const fold = $("[data-cw-panel=model] details[data-pg-fold], [data-cw-panel=model] details.pg-fold");
  assert.equal(text(fold.querySelector("summary")), "More details");
  assert.match(text(fold), /Hyperparameters/);
  assert.match(text(fold), /Calibration/);
  assert.match(text(fold), /Decision threshold/);
  // Output: both runs' parts on the one tab, and no file of its own
  assert.ok($("[data-cw-panel=output] [data-lift]"), "the training run's lift");
  assert.match(text($("[data-cw-slot=output-score]")), /Shoppers worth an offer/);
  assert.ok($("[data-cw-panel=output] [data-cw-contact]"), "who to contact and who not");
  assert.equal($$("[data-cw-tabs] a[download]").length, 0);
  assert.equal($("[data-cw-slot=output-score] [data-all-scores]"), null, "the all-scores file is not offered either");
  assert.doesNotMatch(app.textContent, /undefined|NaN|\[object Object\]/);
  // switching
  $("[data-cw-tab=output]").click();
  assert.equal($("[data-cw-panel=output]").hidden, false);
  assert.equal($("[data-cw-panel=data]").hidden, true);
  assert.equal($("[data-cw-tab=output]").getAttribute("aria-selected"), "true");
  assert.equal($("[data-cw-tab=data]").getAttribute("aria-selected"), "false");
});

test("a redraw of the page keeps the open tab and asks the server for nothing again", async () => {
  const before = requested.length;
  app.innerHTML = RT.resultsTabsHtml({});
  RM.mountResultsTabs(app, ctx);
  assert.equal($("[data-cw-panel=output]").hidden, false, "the tab that was open stays open");
  await settle();
  assert.ok($("[data-cw-panel=data] [data-dd-changes]"), "the parts are drawn from the kept answer");
  assert.equal(requested.length, before, "nothing is asked again");
});

test("a reader who gets no file preview still sees the columns the model learns from, from the training run's own names", async () => {
  artefacts["r_train/prepare.json"] = { feature_columns: ["days_since_last_order", "orders_90d"], dropped_columns: [], carried_columns: [] };
  app.innerHTML = RT.resultsTabsHtml({});
  RM.mountResultsTabs(app, { ...ctx, caseId: "case_2" });
  await settle();
  assert.match(text($("[data-cw-panel=data] [data-dd-needs-analyst]")), /Analyst role/);
  const added = $$("[data-cw-panel=data] [data-dd-group=added] ~ ul li").map(text);
  assert.ok(added.some((l) => /days_since_last_order/.test(l)));
  assert.ok(added.some((l) => /reactivated\s*What the model predicts/.test(l)), "the outcome column");
  delete artefacts["r_train/prepare.json"];
});

test("a run with no Data Doctor case has no card, and an effect model's Model and Output point to the effect pages", async () => {
  app.innerHTML = RT.resultsTabsHtml({});
  RM.mountResultsTabs(app, { ...ctx, caseId: null, trainRunId: "r_up", scoreRunId: null });
  runs.r_up = { ...runs.r_train, run_id: "r_up", problem_type: "uplift" };
  await settle();
  assert.equal($("[data-dd-changes]"), null);
  assert.equal($("[data-cw-panel=model] a[href^='#/uplift/retail-win-back/model/r_up']").textContent.trim(), "Open its page");
});
