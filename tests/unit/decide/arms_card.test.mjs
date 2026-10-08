/* Plan J M100: the uplift model page shows one card per offer of a model of several offers, every
   number the server's (`uplift_evaluation.json`'s `arms`, `arm_policy_value.json`), and nothing for a
   model of one offer. Run by tests/unit/decide/test_arms_card_js.py, or directly:
   `node --test tests/unit/decide/arms_card.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const views = await import(new URL("../../../ui/modules/uplift/views.js", import.meta.url));

const uc = { id: "win-back", name: "Win-back", marker: "P", stars: "★", type_label: "Predictive" };
const trainRun = {
  run_id: "r-train",
  mode: "train",
  state: "done",
  created_at: "2026-10-08T10:00:00Z",
  file_name: "offers.csv",
  problem_type: "uplift",
};
const cv = (value, low, high) => ({ value, ci_low: low, ci_high: high, confidence_level: 0.95 });
const arm = (name, position, effect, rate, auuc) => ({
  arm: name,
  position,
  control: "none",
  rows: 2000,
  treated_rows: 1000,
  control_rows: 1000,
  treated_rate: rate,
  control_rate: 0.171,
  effect,
  auuc,
  qini_coefficient: cv(0.0123, 0.004, 0.02),
  measurable_uplift: true,
});
const evaluation = {
  run_id: "r-train",
  learner: "x_learner",
  base_model: "lightgbm",
  rows_evaluated: 2000,
  treated_rows: 1000,
  control_rows: 1000,
  treated_rate: 0.2213,
  control_rate: 0.171,
  average_treatment_effect: cv(0.0503, 0.02, 0.08),
  auuc: cv(0.0321, 0.011, 0.05),
  qini_coefficient: cv(0.0123, 0.004, 0.02),
  uplift_at: [],
  deciles: [],
  bootstrap_samples: 50,
  measurable_uplift: true,
  causal: true,
  summary: "x",
  evaluated_at: "2026-10-08T10:00:00Z",
  arms: [
    arm("offer_a", 1, cv(0.0503, 0.02, 0.08), 0.2213, cv(0.0321, 0.011, 0.05)),
    arm("offer_b", 2, cv(0.0377, 0.0061, 0.0712), 0.2087, cv(0.0299, 0.009, 0.051)),
  ],
};
const value = {
  run_id: "r-train",
  rows: 3000,
  arms: ["none", "offer_a", "offer_b"],
  arm_rows: [1000, 1000, 1000],
  value_weighted: false,
  value_column: null,
  values_missing: 0,
  best_offer: cv(0.061, 0.04, 0.083),
  first_treatment: cv(0.031, 0.012, 0.05),
  difference: cv(0.03, 0.0144, 0.046),
  best_offer_better: true,
  policy_shares: { none: 0.3, offer_a: 0.35, offer_b: 0.35 },
  bootstrap_samples: 50,
  costs_included: false,
  promotion: "Not promoted: this model chooses between several offers.",
  promotion_code: "MULTI_ARM_PROMOTION_REFUSED",
  causal: true,
  summary: "Choosing the offer per customer adds 0.0300 conversions per customer.",
  computed_at: "2026-10-08T10:00:00Z",
};

test("a model of several offers shows one card per offer with the server's numbers", () => {
  const html = views.modelPageHtml(uc, trainRun, { "uplift_evaluation.json": evaluation, "arm_policy_value.json": value }, null);
  assert.match(html, /data-arm="offer_a"/);
  assert.match(html, /data-arm="offer_b"/);
  assert.match(html, /Offer 2: offer_b/);
  // The second offer's effect and its interval are the server's, formatted, never re-computed.
  assert.match(html, /\+3\.8 pts/);
  assert.match(html, /likely \+0\.6 to \+7\.1/);
  assert.match(html, /20\.9%/);
  assert.match(html, /does better than the first offer alone/);
  assert.match(html, /Not promoted: this model chooses between several offers\./);
});

test("the comparison says so when choosing per customer is not shown to be better", () => {
  const notBetter = { ...value, best_offer_better: false, difference: cv(0.002, -0.01, 0.014) };
  const html = views.armsCard(evaluation, notBetter);
  assert.match(html, /not yet shown to do better/);
});

test("a model of one offer shows no offer cards", () => {
  const binary = { ...evaluation };
  delete binary.arms;
  const html = views.modelPageHtml(uc, trainRun, { "uplift_evaluation.json": binary }, null);
  assert.ok(!html.includes("data-arm="));
  assert.equal(views.armsCard(binary, null), "");
  assert.equal(views.armsCard(null, null), "");
});

test("a missing number is a dash, never a made-up value", () => {
  const thin = { ...evaluation, arms: [{ arm: "offer_a", position: 1, control: "none", effect: null }] };
  const html = views.armsCard(thin, null);
  assert.match(html, /data-arm="offer_a"/);
  assert.match(html, /—/);
  assert.ok(!/NaN|undefined/.test(html));
});
