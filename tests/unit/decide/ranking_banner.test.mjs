/* Plan J M96: the uplift Output page says which ranking ordered the contact list
   (`ranking_choice.json`), in the server's words. Run by tests/unit/decide/test_ranking_banner_js.py,
   or directly: `node --test tests/unit/decide/ranking_banner.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const views = await import(new URL("../../../ui/modules/uplift/views.js", import.meta.url));

const uc = { id: "win-back", name: "Win-back", marker: "P", stars: "★", type_label: "Predictive" };
const scoreRun = {
  run_id: "r-score",
  mode: "score",
  state: "done",
  created_at: "2026-10-08T10:00:00Z",
  file_name: "customers.csv",
  problem_type: "uplift",
};
const policy = {
  run_id: "r-score",
  computed_on: "scored",
  rows: 1000,
  eligible_persuadables: 300,
  contacts_recommended: 80,
  stop_reason: "budget",
  budget_contacts: 80,
  predicted_incremental_conversions: 9.1,
  expected_incremental_conversions: null,
  cost_per_contact: null,
  value_per_conversion: null,
  expected_cost: null,
  expected_value: null,
  expected_net_value: null,
  causal: true,
};
const fallback = {
  run_id: "r-score",
  model_version_id: "m_up",
  ranking: "propensity_model",
  code: "UPLIFT_NOT_BETTER_THAN_RISK",
  beats_risk: false,
  propensity_model_id: "m_prop",
  contacts: 80,
  reason:
    "This model does not beat risk ranking (by the model's own chance of the outcome without contact): the AUUC difference is -0.0123 (95% CI -0.0200 to -0.0050), a range that lies below zero. So this contact list is ranked by the approved propensity model (LightGBM, version 3), contacting the same number of customers the uplift model chose.",
  computed_at: "2026-10-08T10:00:00Z",
};

test("a list ranked by the propensity model says so, with the server's reason, above the list", () => {
  const html = views.outputPageHtml(uc, scoreRun, { "policy_recommendation.json": policy }, { ranking: fallback });
  assert.match(html, /data-ranking="propensity_model"/);
  assert.match(html, /data-code="UPLIFT_NOT_BETTER_THAN_RISK"/);
  assert.match(html, /This list is ranked by the propensity model, not by uplift/);
  assert.ok(html.includes("does not beat risk ranking"));
  assert.ok(html.indexOf('data-ranking="propensity_model"') < html.indexOf("Targeting recommendation"));
});

test("an uplift ranking that failed the check, with nothing to fall back to, warns", () => {
  const kept = {
    ...fallback,
    ranking: "uplift",
    propensity_model_id: null,
    reason: "This model does not beat risk ranking (…). This use case has no approved propensity model to rank by instead, so the list keeps the uplift ranking: treat its order with caution.",
  };
  const html = views.rankingBanner(kept);
  assert.match(html, /The uplift model does not beat risk ranking/);
  assert.match(html, /no approved propensity model/);
  assert.match(html, /data-ranking="uplift"/);
});

test("a model that passed gets one quiet line, and an older run nothing", () => {
  const passed = { ...fallback, ranking: "uplift", code: null, beats_risk: true, reason: "Ranked by predicted uplift. It beats risk ranking." };
  assert.match(views.rankingBanner(passed), /^<p class="caption" data-ranking="uplift">Ranked by predicted uplift\./);
  assert.equal(views.rankingBanner(null), "");
  const html = views.outputPageHtml(uc, scoreRun, { "policy_recommendation.json": policy }, {});
  assert.equal(html.includes("data-ranking"), false);
});

test("the reason is escaped, never markup", () => {
  const html = views.rankingBanner({ ...fallback, reason: "<b>x</b>" });
  assert.ok(html.includes("&lt;b&gt;x&lt;/b&gt;"));
});
