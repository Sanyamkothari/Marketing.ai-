/* Plan J M109: the Output page and the campaign's plan card speak the planner's words - the
   *detectable effect* and the *early look* - and not three different phrasings of them.
   Run by tests/unit/decide/test_planner_words_js.py, or directly:
   `node --test tests/unit/decide/planner_words.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const UI = new URL("../../../ui/", import.meta.url);
const power = await import(new URL("power.js", UI));
const plan = await import(new URL("modules/decide/plan.js", UI));
const views = await import(new URL("modules/decide/views.js", UI));

const uc = { problem_type: "binary_classification" };
const summary = { rows_scored: 12000, score_mean: 0.1, control_group_rows: 1000, suppressed: [] };

test("the scoring Output page's card calls the lift it can see the detectable effect", () => {
  const html = power.powerCardBody(uc, summary);
  assert.match(html, /The detectable effect is the smallest lift/);
  assert.doesNotMatch(html, /A "reliable" lift/);
  // the sentence the card leads with is unchanged
  assert.match(html, /can reliably detect a lift of 3 points \(30% relative\) or more\./);
});

test("the plan card and its slider say detectable effect for the planner's number", () => {
  const registered = {
    metric: "conversion",
    outcome_column: "converted",
    outcome_window_days: 30,
    analysis_date: "2026-11-01",
    holdout_fraction: 0.1,
    n_holdout: 1000,
    population_rows: 10000,
    mde_pp: 2,
    base_rate: 0.1,
    achieved_power: 0.8,
    version: 1,
    plan_hash: "abcdef0123456789",
  };
  const html = plan.planCardHtml({ campaign: { campaign_id: "c1" }, plan: registered, versions: [registered] });
  assert.match(html, /Detectable effect planned for/);
  assert.doesNotMatch(html, /Smallest effect worth finding/);
  const preview = {
    current_index: 0,
    eligible: 10000,
    points: [{ holdout_share: 0.1, n_treat: 9000, n_control: 1000, mde_pp: 2.1, cost_of_holdout: null, reason: null }],
  };
  assert.match(plan.previewReadoutHtml(preview, 0), /Detectable effect at this split/);
  assert.doesNotMatch(plan.previewReadoutHtml(preview, 0), /Smallest change the test is sure to see/);
  const form = plan.planCardHtml({ campaign: { campaign_id: "c1" }, canRegister: true });
  assert.match(form, /Detectable effect to plan for, in points/);
});

test("an early look is labelled the same way on the campaign page", () => {
  const view = {
    campaign: { campaign_id: "c1", name: "Autumn", status: "live", treatment_start: "2026-10-01", counts: {} },
    report: { early_look: true, summary: "Early look, before the planned analysis date." },
    verdict: null,
    plan: { analysis_date: "2026-11-01" },
  };
  const html = views.campaignPageHtml({ view, plans: null, can: () => false });
  assert.match(html, /data-early-look>Early look<\/span> Not a final result\./);
});
