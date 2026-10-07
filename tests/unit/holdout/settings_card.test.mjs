// Plan J M92 (DEC-1302 (d)): the run's "Settings used for this run" card reads the effective holdout
// share through `holdoutFraction` (ui/modules/measure/rule.js), the browser's copy of
// `engine.holdout.spec.effective_holdout_fraction`, and words it by scope.
import { test } from "node:test";
import assert from "node:assert/strict";

const UI = new URL("../../../ui/", import.meta.url);
const pages = await import(new URL("pages.js", UI));

const uc = {
  id: "telco-churn",
  name: "Telco Customer Churn",
  marker: "P",
  stars: "★",
  type_label: "Predictive",
  lifecycle_stage: "Retention",
  ai_type: "predictive",
  problem_type: "binary_classification",
  problem_type_label: "Classification (yes / no)",
  pages: { data: "Profile", model: "Churn model", output: "Churn risk" },
  target: { label: "Outcome", definition: "", label_source: "" },
  output: { kpi: { label: "Subscribers at risk" } },
  advanced_settings: { stages: [] },
};
const run = {
  run_id: "r-score",
  mode: "score",
  state: "done",
  created_at: "2026-10-01T10:00:00Z",
  finished_at: "2026-10-01T10:05:00Z",
  file_name: "october.csv",
  problem_type: "binary_classification",
  primary_key: "customer_id",
  engine_version: "0.1.0",
};
const scoring = {
  rows_scored: 2000,
  score_field: "churn_prob",
  control_group_rows: 100,
  kpi: { label: "Subscribers at risk", formula: 'count_where_band_in(["High"])', display: "851" },
  bands: [
    { name: "High", action: "Retention call", rows: 851, share_pct: 42.5 },
    { name: "Low", action: "No action", rows: 1149, share_pct: 57.5 },
  ],
  actions: [{ action: "Control (hold out)", rows: 100, share_pct: 5 }],
  suppressed: [],
  drift_status: "stable",
  rows_with_fallback_reasons: 0,
  sample_rows: [],
};
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const bands = [
  { name: "High", min_score: 0.5, action: "Retention call" },
  { name: "Low", min_score: 0.0, action: "No action" },
];

function render(actions) {
  const art = { "scoring_summary.json": scoring, "run_config.json": { config: { actions: { bands, ...actions } } } };
  return text(pages.renderPage("output", uc, run, art, "/runs/r-score/scores.csv"));
}

test("scope run: the control group fraction, drawn at random among eligible customers", () => {
  const seen = render({ control_group_fraction: 0.1, holdout: { scope: "run", fraction: null } });
  assert.match(seen, /Held back to measure results 10% of eligible customers, at random/);
});

test("an older run config without a holdout block reads exactly as before", () => {
  const seen = render({ control_group_fraction: 0.1 });
  assert.match(seen, /Held back to measure results 10% of eligible customers, at random/);
});

for (const scope of ["use_case", "universal"]) {
  test(`scope ${scope}: the holdout's own share, the same customers every run - never the unused default`, () => {
    const seen = render({ control_group_fraction: 0.1, holdout: { scope, fraction: 0.05 } });
    assert.match(seen, /Held back to measure results 5% of all customers, the same ones every run/);
    assert.ok(!seen.includes("10% of eligible customers"), "the unused control_group_fraction is not shown");
  });
}
