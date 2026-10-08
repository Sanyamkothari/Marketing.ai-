/* Plan J M98 (review fix): the treat list card reaches the Output page of every scoring run.
   The first build wrote the card and a panel, but `renderPage` dropped `extra`, so the card appeared
   nowhere and only its hidden request ran. These tests render the real pages (`ui/pages.js` for a
   propensity run, `ui/modules/uplift/views.js` for an uplift run) with the panel markup and look for it.
   Run by tests/unit/decide/test_treat_list_card_js.py, or directly:
   `node --test tests/unit/decide/treat_list_page.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const UI = new URL("../../../ui/", import.meta.url);
const pages = await import(new URL("pages.js", UI));
const upliftViews = await import(new URL("modules/uplift/views.js", UI));
const { treatListCardHtml } = await import(new URL("modules/decide/views.js", UI));

const uc = {
  id: "win-back-campaign",
  name: "Win-back campaign",
  marker: "P",
  stars: "*",
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
const base = {
  run_id: "r-score",
  mode: "score",
  state: "done",
  created_at: "2026-09-01T10:00:00Z",
  finished_at: "2026-09-01T10:05:00Z",
  file_name: "campaign.csv",
  primary_key: "customer_id",
  target: "reactivated_90d",
  best_model: "X",
  engine_version: "0.1.0",
};
const summary = {
  total_rows: 24,
  treat_rows: 9,
  holdout_rows: 5,
  explore_rows: 1,
  suppressed_rows: 3,
  net_value_total: null,
  net_value_unit: null,
  expected_gross_value_total: 1234.5,
  holdout_note: null,
  net_value_note: null,
  expected_gross_value_note: null,
};
const scoring = {
  rows_scored: 24,
  score_field: "churn_prob",
  control_group_rows: 5,
  kpi: { label: "At risk", formula: 'count_where_band_in(["High"])', display: "9" },
  bands: [{ name: "High", action: "Retention call", rows: 9, share_pct: 37.5 }],
  actions: [],
  suppressed: [],
  drift_status: "ok",
  rows_with_fallback_reasons: 0,
  sample_rows: [],
};
const primaries = (html) => (html.match(/class="btn primary"/g) || []).length;
const panel = `<div data-treat-panel="r-score">${treatListCardHtml(summary, { treatListHref: "/runs/r-score/artefacts/treat_list.csv" })}</div>`;

test("a propensity run's Output page draws the panel it is given", () => {
  const html = pages.renderPage("output", uc, base, { "scoring_summary.json": scoring }, "#", { panelsHtml: panel });
  assert.match(html, /data-treat-panel="r-score"/);
  assert.match(html, /Download treat list \(CSV\)/);
  assert.match(html, /Download contact list \(CSV\)/);
  assert.equal(primaries(html), 1, "the contact list stays the page's one primary action");
});

test("without a panel the propensity Output page is unchanged", () => {
  const html = pages.renderPage("output", uc, base, { "scoring_summary.json": scoring }, "#", {});
  assert.doesNotMatch(html, /data-treat-panel/);
});

test("an uplift run's Output page draws the panel it is given", () => {
  const run = { ...base, problem_type: "uplift", primary_key: ["customer_id", "snapshot_date"] };
  const html = upliftViews.outputPageHtml(uc, run, {}, { scoresHref: "/runs/r-score/scores.csv", panelsHtml: panel });
  assert.match(html, /data-treat-panel="r-score"/);
  assert.match(html, /Download treat list \(CSV\)/);
  assert.match(html, /Download contact list \(CSV\)/);
});

test("an uplift run's Output page without a panel is unchanged", () => {
  const run = { ...base, problem_type: "uplift", primary_key: ["customer_id", "snapshot_date"] };
  const html = upliftViews.outputPageHtml(uc, run, {}, { scoresHref: "/x" });
  assert.doesNotMatch(html, /data-treat-panel/);
});
