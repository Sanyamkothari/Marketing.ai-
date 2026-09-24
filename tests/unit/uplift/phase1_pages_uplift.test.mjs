/* Phase 1's Data, Model and Output pages (ui/pages.js) on an uplift run (M53), and since v1 (WP4) on
   Phase 1 runs too: plain words first, one primary action, no artefact file name on screen.
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
  for (const run of [trainRun, scoreRun]) {
    const names = pages.pageArtefacts("data", run);
    for (const name of PHASE1_ONLY) assert.ok(!names.includes(name), `data asks for ${name}`);
  }
  assert.deepEqual(pages.pageArtefacts("data", trainRun), pages.UPLIFT_PAGE_ARTEFACTS.data);
  assert.deepEqual(pages.pageArtefacts("data", scoreRun), pages.UPLIFT_PAGE_ARTEFACTS.data_score);
  assert.ok(pages.pageArtefacts("data", trainRun).includes("uplift_validation.json"));
  assert.ok(!pages.pageArtefacts("data", trainRun).includes("uplift_drift.json"), "a training run writes no drift");
  assert.ok(pages.pageArtefacts("data", scoreRun).includes("uplift_drift.json"));
  // v1 (WP4): an uplift run's Model and Output are the uplift module's screens; nothing is read here.
  for (const kind of ["model", "output"]) {
    assert.deepEqual(pages.pageArtefacts(kind, trainRun), []);
    assert.deepEqual(pages.pageArtefacts(kind, scoreRun), []);
  }
});

test("a Phase 1 run reads by its mode: a scoring run never asks for decile_lift.json", () => {
  const phase1Score = { ...phase1Run, mode: "score" };
  for (const kind of ["data", "model", "output"]) {
    assert.deepEqual(pages.pageArtefacts(kind, phase1Run), pages.PAGE_ARTEFACTS[kind]);
    assert.deepEqual(pages.pageArtefacts(kind, phase1Score), pages.PAGE_ARTEFACTS[`${kind}_score`]);
  }
  assert.ok(pages.pageArtefacts("output", phase1Run).includes("decile_lift.json"));
  assert.ok(!pages.pageArtefacts("output", phase1Score).includes("decile_lift.json"));
  assert.ok(pages.pageArtefacts("output", phase1Score).includes("scoring_summary.json"));
  assert.ok(!pages.pageArtefacts("data", phase1Score).includes("split.json"), "a scoring run writes no split");
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
  // v1: the finding leads in words; the code stays in its own (hidden-by-default) column.
  assert.match(seen, /Treatment not random Error \(acknowledged\) Who was treated can be predicted\. TREATMENT_NOT_RANDOM/);
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

test("an uplift run's Model and Output redirect to the uplift module's own screens", () => {
  for (const [kind, run] of [
    ["model", trainRun],
    ["output", scoreRun],
    ["output", trainRun],
  ]) {
    const html = pages.renderPage(kind, uc, run, {}, "#", { qiniChart: charts.qiniChart });
    const href = `#/uplift/win-back-campaign/${kind}/${run.run_id}`;
    assert.ok(html.includes(`data-redirect="${href}"`), `${kind} of ${run.run_id} redirects`);
    assert.ok(html.includes(`href="${href}"`), "and offers the link while it does");
    assert.ok(!text(html).includes("has not produced"));
    noJunk(html);
  }
});

test("an uplift run's Data page links its Model and Output tabs to the uplift screens", () => {
  const train = pages.renderPage("data", uc, trainRun, {}, "#");
  assert.ok(train.includes('href="#/uplift/win-back-campaign/model/r-train"'));
  assert.ok(train.includes('href="#/uplift/win-back-campaign/output/r-train"'));
  const score = pages.renderPage("data", uc, scoreRun, {}, "#");
  assert.ok(score.includes('href="#/campaign/win-back-campaign/r-score"'), "a scoring run has Campaign results");
  assert.ok(!score.includes("/model/r-score"), "an uplift scoring run has no Model tab");
  assert.ok(!text(train).includes("has not produced"));
  assert.ok(!text(score).includes("has not produced"));
});

test("a Phase 1 run still gets the Phase 1 pages", () => {
  const evaluation = {
    primary_metric: "roc_auc",
    primary_metric_label: "ROC-AUC",
    headline_score: 0.91,
    metrics: [{ id: "roc_auc", label: "ROC-AUC", value: 0.91 }],
  };
  const matrix = { true_positive: 1, false_negative: 2, false_positive: 3, true_negative: 4, threshold: 0.5 };
  const html = pages.renderPage("model", uc, phase1Run, { "evaluation.json": evaluation, "confusion_matrix.json": matrix }, "#");
  assert.match(text(html), /How good is this model\?/);
  assert.match(text(html), /Confusion matrix/);
  assert.ok(!text(html).includes("Qini"));
});

test("problem types are labelled for the runs list", () => {
  assert.equal(pages.problemTypeLabel(uc, "binary_classification"), "Classification (yes / no)");
  assert.equal(pages.problemTypeLabel(uc, "uplift"), "Uplift");
  assert.equal(pages.problemTypeLabel(uc, null), EM);
});

// --- v1 (WP4): Phase 1's own pages, plain words first ------------------------------------------------
// The same pure renderer, on Phase 1 runs: one primary action per page, no artefact file name on
// screen, codes and ids only inside "Technical details".

const telco = {
  ...uc,
  id: "telco-churn",
  name: "Telco Customer Churn",
  entity: "subscriber",
  pages: { data: "Profile", model: "Churn model", output: "Churn risk" },
  output: { kpi: { label: "Subscribers at risk" } },
  config: {
    suggested_features: [{ name: "days_since_last_activity", description: "Days since the subscriber last did anything." }],
    label: { description: "No activity of any kind in the 60 days after the snapshot date." },
  },
};
const p1Train = { ...trainRun, run_id: "r-p1t", problem_type: "binary_classification", primary_key: KEY, dataset_id: "ds_1" };
const p1Score = { ...p1Train, run_id: "r-p1s", mode: "score", target: null };
const visible = (html) => text(html.split("data-tech>")[0]);
const primaries = (html) => (html.match(/class="btn primary"/g) || []).length;

const scoring = {
  rows_scored: 2000,
  score_field: "churn_prob",
  control_group_rows: 200,
  kpi: { label: "Subscribers at risk", formula: 'count_where_band_in(["High","Medium"])', display: "855" },
  bands: [
    { name: "High", action: "Retention call", rows: 851, share_pct: 42.5 },
    { name: "Medium", action: "Upgrade offer", rows: 4, share_pct: 0.2 },
    { name: "Low", action: "No action", rows: 1145, share_pct: 57.2 },
  ],
  actions: [{ action: "Control (hold out)", rows: 200, share_pct: 10 }],
  suppressed: [],
  drift_status: "drifted",
  rows_with_fallback_reasons: 0,
  sample_rows: [
    {
      primary_key: "1000003|2025-03-30",
      score: 0.9981,
      band: "High",
      action: "Retention call",
      reasons: [{ feature: "days_since_last_activity", value: "240", direction: "up", text: "days_since_last_activity ↑ (240)" }],
      suppressed_reason: null,
    },
  ],
};

test("scoring Output: the contact list is the one primary action, and nothing asks for a lift", () => {
  const art = {
    "scoring_summary.json": scoring,
    "drift.json": { baseline_run_id: "r-p1t", status: "drifted", max_psi: 13.8 },
  };
  const html = pages.renderPage("output", telco, p1Score, art, "/runs/r-p1s/scores.csv");
  const seen = visible(html);
  assert.equal(primaries(html), 1);
  assert.match(html, /<a class="btn primary" href="\/runs\/r-p1s\/scores\.csv"[^>]*>.*Download contact list \(CSV\)<\/a>/);
  assert.match(seen, /Subscribers at risk 855 High 851 \+ Medium 4/);
  assert.match(seen, /Customers scored 2,000/);
  assert.match(seen, /Held back to measure results 200 \(10%\)/);
  assert.match(seen, /The new customers look very different/);
  assert.match(seen, /Subscriber Churn likelihood % Band Main reason Action/);
  assert.match(seen, /1000003 100% High Days since last activity is 240 \(raises it\) Retention call/);
  assert.match(seen, /Held back \(control group\)/);
  for (const gone of ["decile", "Lift", ".json", "has not produced"]) {
    assert.ok(!seen.includes(gone), `the scoring Output shows ${gone}`);
  }
  assert.match(html, /href="#\/campaign\/telco-churn\/r-p1s"/, "Campaign results is a tab");
  noJunk(html);
});

test("training Output: one lift tile in words and 'Score new customers with this model'", () => {
  const lift = {
    unit: "x",
    values: [5.0382, 3.3893, 0.6718, 0.2595],
    bins: [{ label: "D1" }, { label: "D2" }, { label: "D3" }, { label: "D4" }],
  };
  const html = pages.renderPage("output", telco, p1Train, { "decile_lift.json": lift }, "#");
  const seen = visible(html);
  assert.equal(primaries(html), 1);
  assert.match(html, /class="btn primary" href="#\/uc\/telco-churn">Score new customers with this model/);
  assert.match(
    seen,
    /Lift in the top 10% 5× The 10% of subscribers with the highest scores had the outcome 5 times as often as a random 10%\./,
  );
  assert.match(seen, /3\.4×/);
  assert.ok(!seen.includes("0.3×"), "values are labelled on D1 to D3 only");
  assert.ok(!seen.includes("Download contact list"));
  const empty = visible(pages.renderPage("output", telco, p1Train, {}, "#"));
  assert.match(empty, /The lift chart appears when training finishes/);
  assert.ok(!empty.includes(".json"));
});

test("Model: a verdict, the fallback cut-off by its title, a calm baseline warning, codes only in details", () => {
  const evaluation = {
    primary_metric: "roc_auc",
    primary_metric_label: "ROC-AUC",
    headline_score: 0.9554,
    rows_evaluated: 3300,
    metrics: [
      { id: "roc_auc", label: "ROC-AUC", value: 0.9554 },
      { id: "recall", label: "Recall", value: 0.8046 },
    ],
    threshold: 0.8123,
    threshold_mode: "auto",
    threshold_detail:
      "Auto, fell back to the top 10% of validation scores (THRESHOLD_FALLBACK: F1 flagged every row): 0.8123",
  };
  const baseline = {
    baseline_name: "baseline (logistic regression)",
    rows: [{ id: "roc_auc", label: "ROC-AUC", model_value: 0.9554, baseline_value: 0.9604, model_better: false }],
  };
  const importance = { items: Array.from({ length: 10 }, (_, i) => ({ feature: `f_${i}`, share_pct: i + 1 })) };
  const art = {
    "evaluation.json": evaluation,
    "baseline.json": baseline,
    "feature_importance.json": importance,
    "fairness.json": { evaluated: false, reason_not_evaluated: "No sensitive column was configured." },
    "best_model.json": { display_name: "LightGBM", hyperparameters_summary: "53 leaves" },
  };
  const html = pages.renderPage("model", telco, p1Train, art, "#");
  const seen = visible(html);
  assert.match(seen, /It ranks a subscriber who has the outcome above one who does not 96% of the time\./);
  assert.match(seen, /The automatic cut-off was replaced by the top 10%/);
  assert.ok(!seen.includes("THRESHOLD_FALLBACK"), "the code is not shown outside Technical details");
  assert.match(text(html), /THRESHOLD_FALLBACK/, "it is kept in Technical details");
  assert.match(seen, /A simpler yardstick model scored slightly higher/);
  assert.match(seen, /Model LightGBM Ranking quality 0\.955 Cases caught 80%/);
  assert.match(seen, /What drives the score \(largest first\) F 9 /);
  assert.match(seen, /Show all 10/);
  assert.ok(!seen.includes("Fairness") && !seen.includes("sensitive column"), "no fairness card unless evaluated");
  assert.match(html, /Next: Output ›/);
  assert.equal(primaries(html), 1);
  noJunk(html);
});

test("Model of a scoring run: points to the training run instead of empty cards", () => {
  const html = pages.renderPage("model", telco, p1Score, { "drift.json": { baseline_run_id: "r-p1t" } }, "#");
  assert.match(html, /href="#\/uc\/telco-churn\/model\/r-p1t">Open the training run's Model page/);
  assert.ok(!visible(html).includes(".json"));
  assert.equal(primaries(html), 1);
});

test("Data: glance tiles, lineage behind Details, the feature table sorted by missing values", () => {
  const profile = {
    file_name: "ds_1 (built)",
    file_format: "parquet",
    row_count: 22000,
    column_count: 18,
    missing_value_rate_pct: 5.14,
    columns: [
      { name: "customer_id", distinct_count: 2000, null_rate: 0, inferred_type: "integer" },
      { name: "days_since_last_activity", null_rate: 0, inferred_type: "integer" },
      { name: "usage_mb_30d", null_rate: 0.4687, inferred_type: "float" },
    ],
  };
  const prepare = {
    feature_columns: ["days_since_last_activity", "usage_mb_30d"],
    transforms: [
      { kind: "clip_percentile", columns: ["usage_mb_30d"], parameters: { applied: true } },
      { kind: "clip_percentile", columns: ["days_since_last_activity"], parameters: { applied: true } },
    ],
  };
  const node = (id, label) => ({ id, label, detail: "" });
  const lineage = {
    sources: [node("src_1", "customers.csv")],
    mappings: [node("map_1", "Mapping map_1")],
    spec: node("spec_1", "Recipe"),
    dataset: node("ds_1", "Dataset"),
  };
  const run = { ...p1Train, primary_key: ["customer_id", "snapshot_date"] };
  const html = pages.renderPage("data", telco, run, { "profile.json": profile, "prepare.json": prepare }, "#", { lineage });
  const seen = visible(html);
  assert.match(seen, /Rows 22,000 One per subscriber per date Subscribers 2,000 Details used to predict 2 Missing values 5\.1%/);
  assert.match(seen, /Built from 1 table: customers\.csv\./);
  assert.match(html, /<details class="tech" data-lineage><summary>Details: data lineage<\/summary><div class="lineage">/);
  assert.match(seen, /Extreme values capped on 2 columns/);
  assert.match(seen, /Usage MB 30 days 46\.9% .* Days since last activity Days since the subscriber last did anything\. 0%/);
  assert.match(seen, /No activity of any kind in the 60 days after the snapshot date\./);
  assert.ok(!seen.includes("Label source"), "a built dataset's label has no uploaded source");
  assert.ok(!seen.includes("Date range"), "an unknown date range is hidden");
  assert.ok(!seen.includes("PARQUET") && !seen.includes("ds_1 (built)"), "file facts are in Technical details");
  assert.match(html, /Next: Model ›/);
  const bare = visible(pages.renderPage("data", telco, p1Train, {}, "#"));
  assert.ok(!bare.includes(".json") && !bare.includes("has not produced"));
  noJunk(html);
});
