/* Phase 1's Data, Model and Output pages (ui/pages.js) on an uplift run (M53).
   `pages.js` is pure - strings in, strings out - so each page is rendered here exactly as the
   browser renders it, from uplift artefact fixtures. Run by tests/unit/uplift/test_phase1_pages_uplift.py,
   or directly: `node --test tests/unit/uplift/phase1_pages_uplift.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const UI = new URL("../../../ui/", import.meta.url);
const pages = await import(new URL("pages.js", UI));
const charts = await import(new URL("modules/uplift/charts.js", UI));

const EM = "—";
const uc = {
  id: "win-back-campaign",
  name: "Win-back <campaign>",
  marker: "P",
  stars: "★",
  type_label: "Predictive",
  lifecycle_stage: "Retention",
  ai_type: "predictive",
  problem_type: "binary_classification",
  problem_type_label: "Classification (yes / no)",
  setup: { problem_type_choices: [{ value: "binary_classification", label: "Classification (yes / no)" }] },
  pages: { data: "Data", model: "Model", output: "Output" },
  target: { label: "Outcome", definition: "", label_source: "" },
  output: { kpi: { label: "Customers reactivated" } },
  advanced_settings: { stages: [] },
};
const KEY = ["customer_id", "snapshot_date"];
const trainRun = {
  run_id: "r-train",
  mode: "train",
  state: "done",
  created_at: "2026-09-01T10:00:00Z",
  finished_at: "2026-09-01T10:05:00Z",
  file_name: "campaign.csv",
  problem_type: "uplift",
  primary_key: KEY,
  target: "reactivated_90d",
  best_model: "X-learner",
  engine_version: "0.1.0",
};
const scoreRun = { ...trainRun, run_id: "r-score", mode: "score" };
const phase1Run = { ...trainRun, run_id: "r-p1", problem_type: "binary_classification", primary_key: "customer_id" };

const cv = (value, lo, hi) => ({ value, ci_low: lo, ci_high: hi, confidence_level: 0.95 });
const upliftValidation = {
  run_id: "r-train",
  upload_id: "u-1",
  treatment_column: "treatment",
  checks: [],
  passed: true,
  causal: true,
  randomness_auc: 0.512,
  rows_checked: 7500,
  rows_immature: 0,
  checked_at: "2026-09-01T10:00:00Z",
  treated_rows: 3750,
  control_rows: 3750,
  entity_column: "customer_id",
  treated_entities: 1250,
  control_entities: 1250,
};
const split = {
  run_id: "r-train",
  type: "random_stratified",
  group_column: "customer_id",
  parts: [
    { name: "train", rows: 5250, share: 0.7 },
    { name: "validation", rows: 0, share: 0 },
    { name: "test", rows: 2250, share: 0.3 },
  ],
};
const evaluation = {
  run_id: "r-train",
  rows_evaluated: 2250,
  treated_rows: 1125,
  control_rows: 1125,
  treated_rate: 0.21,
  control_rate: 0.18,
  average_treatment_effect: cv(0.03, 0.01, 0.05),
  auuc: cv(0.0123, 0.0041, 0.0205),
  qini_coefficient: cv(0.0087, 0.002, 0.015),
  uplift_at: [0.1, 0.2, 0.3].map((f) => ({ fraction: f, uplift: cv(0.08, 0.02, 0.14) })),
  deciles: [],
  bootstrap_samples: 200,
  measurable_uplift: true,
  causal: true,
  summary: "Targeting by predicted uplift beats random targeting.",
};
const curve = {
  run_id: "r-train",
  rows_evaluated: 2250,
  causal: true,
  points: Array.from({ length: 11 }, (_, i) => ({
    fraction: i / 10,
    qini: 0.03 * Math.sqrt(i / 10),
    random: 0.03 * (i / 10),
    uplift_curve: 0.02 * (i / 10),
  })),
};
const drift = {
  run_id: "r-score",
  model_version_id: "m-1",
  training_run_id: "r-train",
  features: {
    status: "watch",
    max_psi: 0.123,
    drifted_features: [],
    summary: "PSI 0.12, watch",
  },
  features_reason: null,
  treatment: {
    status: "not_applicable",
    treatment_column: "treatment",
    training_treated_share: 0.5,
    tolerance: 0.05,
    training_rows: 5250,
    rows_compared: 0,
    reason: "This file has no 'treatment' column, so its treated share cannot be compared.",
  },
  summary: "PSI 0.12, watch · treated share not compared",
};
const summary = {
  rows_scored: 5000,
  control_group_rows: 500,
  score_field: "uplift",
  kpi: { label: "Persuadables recommended to contact", display: "1,200" },
  bands: [
    { name: "Persuadables", action: "Treat", rows: 1500, share_pct: 30 },
    { name: "Sleeping dogs", action: "Never treat (contact makes it worse)", rows: 500, share_pct: 10 },
  ],
  sample_rows: [
    {
      primary_key: "C0000001|2026-04-30",
      score: 0.0712,
      band: "Persuadables",
      action: "Treat",
      reasons: [{ text: "Visits in the last 30 days is high" }],
      suppressed_reason: null,
    },
  ],
};
const policy = {
  contacts_recommended: 1200,
  eligible_persuadables: 1350,
  stop_reason: "all_persuadables",
  budget_contacts: null,
  predicted_incremental_conversions: 91.2,
  expected_incremental_conversions: cv(80.5, 41.1, 120.9),
  causal: true,
};

const text = (html) =>
  html
    .replace(/<[^>]*>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, " ");
const noJunk = (html) => {
  for (const bad of ["undefined", "NaN", "null", "[object Object]", "Infinity"]) {
    assert.ok(!text(html).includes(bad), `rendered text contains ${bad}`);
  }
};
const PHASE1_ONLY = ["prepare.json", "drift.json", "decile_lift.json"];

test("an uplift run never requests an artefact it does not write", () => {
  for (const kind of ["data", "model", "output"]) {
    const names = pages.pageArtefacts(kind, trainRun);
    for (const name of PHASE1_ONLY) assert.ok(!names.includes(name), `${kind} asks for ${name}`);
    assert.deepEqual(names, pages.UPLIFT_PAGE_ARTEFACTS[kind]);
    assert.deepEqual(pages.pageArtefacts(kind, phase1Run), pages.PAGE_ARTEFACTS[kind]);
  }
  assert.ok(pages.pageArtefacts("data", trainRun).includes("uplift_validation.json"));
  assert.ok(pages.pageArtefacts("model", trainRun).includes("qini_curve.json"));
  assert.ok(pages.pageArtefacts("model", trainRun).includes("uplift_evaluation.json"));
  assert.ok(pages.pageArtefacts("output", trainRun).includes("uplift_drift.json"));
});

test("data page: treatment and control in customers and rows, the randomness check, a grouped split", () => {
  const art = {
    "profile.json": { file_name: "campaign.csv", row_count: 7500, column_count: 12 },
    "split.json": split,
    "uplift_validation.json": upliftValidation,
    "run_config.json": { config: { uplift: { randomness_auc_max: 0.6 } } },
  };
  const html = pages.renderPage("data", uc, trainRun, art, "#");
  const seen = text(html);
  assert.match(seen, /Treatment & control/);
  assert.match(seen, /1,250 customers \(3,750 rows\)/);
  assert.match(seen, /Treated 1,250 Control 1,250 Randomness AUC 0\.51/);
  assert.match(seen, /Passed \(AUC 0\.51, limit 0\.6\)/);
  assert.match(seen, /Treated share 50%/);
  assert.match(seen, /grouped by customer_id/);
  assert.match(seen, /customer_id \+ snapshot_date/);
  assert.match(seen, /Every uplift check passed/);
  assert.ok(!seen.includes("prepare.json"), "no Phase 1 prepare card on an uplift run");
  assert.ok(!seen.includes("Drift against"), "a training run has no drift card");
  noJunk(html);
});

test("data page: a failed, acknowledged randomness check and its finding are shown, marked not causal", () => {
  const finding = {
    code: "TREATMENT_NOT_RANDOM",
    severity: "error",
    message: "Who was treated can be predicted.",
    acknowledgeable: true,
    acknowledged: true,
  };
  const art = { "uplift_validation.json": { ...upliftValidation, causal: false, randomness_auc: 0.71, checks: [finding] } };
  const seen = text(pages.renderPage("data", uc, trainRun, art, "#"));
  assert.match(seen, /Targeted, acknowledged \(AUC 0\.71\)/);
  assert.match(seen, /TREATMENT_NOT_RANDOM error \(acknowledged\)/);
  assert.match(seen, /Not causal/);
});

test("data page of a scoring run: the drift card, honest about the treated share", () => {
  const html = pages.renderPage("data", uc, scoreRun, { "uplift_drift.json": drift }, "#");
  const seen = text(html);
  assert.match(seen, /Drift against the training data/);
  assert.match(seen, /Watch · max PSI 0\.123/);
  assert.match(seen, /has no 'treatment' column/);
  assert.match(seen, /not checked as an experiment/);
  noJunk(html);
});

test("model page: the Qini curve and AUUC instead of ROC and lift", () => {
  const art = { "uplift_evaluation.json": evaluation, "qini_curve.json": curve };
  const html = pages.renderPage("model", uc, trainRun, art, "#", { qiniChart: charts.qiniChart });
  const seen = text(html);
  assert.match(html, /<svg[^>]*aria-label="Qini curve/);
  assert.match(seen, /AUUC 0\.0123/);
  assert.match(seen, /0\.0123 \(95% CI 0\.0041 to 0\.0205\)/);
  assert.match(seen, /Qini coefficient 0\.0087/);
  assert.match(seen, /Uplift in the top 10%/);
  for (const phase1 of ["Confusion matrix", "ROC", "Model search", "Decision threshold"]) {
    assert.ok(!seen.includes(phase1), `an uplift Model page shows ${phase1}`);
  }
  noJunk(html);
});

test("model page without the uplift module lists the curve's points instead of drawing it", () => {
  const html = pages.renderPage("model", uc, trainRun, { "qini_curve.json": curve }, "#");
  assert.ok(!html.includes('aria-label="Qini curve'));
  assert.match(text(html), /share targeted model random/);
  assert.match(text(html), /10% 0\.95% 0\.3% .* 100% 3% 3%/);
});

test("model page with nothing written says what is missing, never a number", () => {
  const html = pages.renderPage("model", uc, trainRun, {}, "#");
  const seen = text(html);
  assert.match(seen, /has not produced qini_curve\.json yet/);
  assert.match(seen, /has not produced uplift_evaluation\.json yet/);
  assert.match(seen, new RegExp(`AUUC ${EM}`));
  noJunk(html);
});

test("output page: segments, the targeting recommendation, drift and the treat list", () => {
  const art = {
    "scoring_summary.json": summary,
    "segments.json": { computed_on: "scored", causal: true, segments: [] },
    "policy_recommendation.json": policy,
    "uplift_drift.json": drift,
  };
  const html = pages.renderPage("output", uc, scoreRun, art, "/runs/r-score/scores.csv");
  const seen = text(html);
  assert.match(seen, /Persuadables recommended to contact 1,200/);
  assert.match(seen, /Persuadables Treat 1,500 30%/);
  assert.match(seen, /Contacts recommended 1,200/);
  assert.match(seen, /80\.5 \(95% CI 41\.1 to 120\.9\)/);
  assert.match(seen, /Drift against the training data/);
  assert.match(seen, /customer_id \+ snapshot_date uplift segment top reason action/);
  assert.match(seen, /C0000001\|2026-04-30 0\.0712 Persuadables/);
  assert.ok(!seen.includes("Lift (top decile)"), "no propensity wording on an uplift run");
  assert.ok(html.includes('href="/runs/r-score/scores.csv"'));
  noJunk(html);
});

test("a Phase 1 run still gets the Phase 1 pages", () => {
  const html = pages.renderPage("model", uc, phase1Run, {}, "#");
  assert.match(text(html), /Confusion matrix/);
  assert.ok(!text(html).includes("Qini"));
});

test("problem types are labelled for the runs list", () => {
  assert.equal(pages.problemTypeLabel(uc, "binary_classification"), "Classification (yes / no)");
  assert.equal(pages.problemTypeLabel(uc, "uplift"), "Uplift");
  assert.equal(pages.problemTypeLabel(uc, null), EM);
});
