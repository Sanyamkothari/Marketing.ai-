/* The plain results summary a person sees first when a campaign is ready, in the order a marketer asks: who to contact
   (how many, who was left out, the first customers best first), why these customers, how good the model is (one
   honest sentence, what is not proven yet), what was done to the data (a link to the approved plan), "See more" folds
   for the detail, and everything else one click away behind "Show technical details". Drawn by the
   wizard's own builders in jsdom from recorded-shape answers. Run through tests/integration/test_campaign_run_results.py. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>`, {
  url: "http://localhost/ui/#/campaigns/new/review/c_1",
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const V = await import("../../../../../ui/modules/campaigns/wizard_views.js");
const SV = await import("../../../../../ui/modules/campaigns/summary_views.js");
const SR = await import("../../../../../ui/modules/campaigns/summary_rules.js");
const RV = await import("../../../../../ui/modules/campaigns/run_views.js");
const { findNeverShown } = await import("../../../../../ui/labels.js");

const app = document.getElementById("app");
const $ = (sel) => app.querySelector(sel);
const $$ = (sel) => [...app.querySelectorAll(sel)];
const draw = (html) => {
  app.innerHTML = html;
  return app;
};
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);

const campaign = { campaign_id: "c_1", use_case_id: "win-back-campaign", run_ids: ["r_score"], status: "live" };
const list = {
  total_rows: 8214,
  treat_rows: 1240,
  holdout_rows: 600,
  suppressed_rows: 35,
  uncontactable_rows: 0,
  compliance_withheld_rows: 12,
};
const version = { model_id: "m_1", run_id: "r_train", metric: "roc_auc", metric_label: "ROC AUC", test_score: 0.781 };
const top = {
  customers: [
    { customer: "C7", score: 0.91, priority: "High", offer: "Call back", channel: "phone", reason: "Short contract" },
    { customer: "C9", score: 0.7, priority: "Medium", offer: "Email", channel: "email", reason: null },
  ],
  unavailable: null,
};
const model = { state: "approved", canApprove: true, own: false, version };
const review = (extra = {}) =>
  V.reviewHtml({ campaign, list, preview: null, cost: null, compliance: null, resultsOn: "2026-11-10", model, canApprove: true, summary: { top, caseId: "case_42" }, ...extra });

test("the summary comes first: how many to contact, who was left out and why, in plain words", () => {
  draw(review());
  assert.equal(app.firstElementChild, $("[data-cw-results]"), "the summary is the first thing on the page");
  assert.equal(text($("[data-cw-sum-contact]")), "1,240 customers to contact of the 8,214 checked");
  const left = $$("[data-cw-sum-excluded] li").map(text);
  assert.equal(left.length, 4, "only the reasons that hold somebody: comparison group, contact rules, compliance, not picked");
  assert.match(left[0], /^600 customers: kept back on purpose as the comparison group/);
  assert.match(left[1], /^35 customers: left out by your contact rules/);
  assert.match(left[2], /^12 customers: taken off by the compliance check/);
  assert.match(left[3], /^6,327 customers: not on the list this time \(the model ranks them lower/, "the rest of the 8,214 are said, so the numbers add up");
});

test("the list sentence tells the truth about the file: send only the customers marked to contact", () => {
  draw(review());
  const line = text($("[data-cw-sum-list]"));
  assert.match(line, /Send only the customers the file marks to contact, not simply the top of it/);
  assert.doesNotMatch(line, /come first, then the ones left out/, "held-back and withheld customers are not sorted after the contactable ones");
});

test("the first customers are a table, best first, with their likelihood, and only the columns that hold something", () => {
  draw(review());
  const rows = $$("[data-cw-sum-customers] tbody tr").map((r) => [...r.children].map(text));
  assert.deepEqual(rows[0], ["C7", "91%", "High", "Call back", "phone", "Short contract"]);
  assert.deepEqual(rows[1].slice(0, 2), ["C9", "70%"]);
  draw(
    SV.resultsSummaryHtml({
      list,
      version,
      top: { customers: [{ customer: "C1", score: 0.5, priority: null, offer: null, channel: null, reason: null }] },
    }),
  );
  assert.deepEqual($$("[data-cw-sum-customers] th").map(text), ["Customer", "Likelihood"]);
});

test("when the names cannot be shown the page says why, in words, instead of leaving a gap", () => {
  draw(review({ summary: { top: { customers: [], unavailable: "The new model still waits for approval." }, caseId: null } }));
  assert.match(text($("[data-cw-sum-names]")), /No customers are shown yet\. The new model still waits for approval\./);
  draw(review({ summary: { top: { error: new Error("down") }, caseId: null } }));
  assert.match(text($("[data-cw-sum-names]")), /could not be loaded just now/);
  draw(review({ summary: { top: { denied: true }, caseId: null } }));
  assert.match(text($("[data-cw-sum-names]")), /needs the Analyst role/);
  draw(V.reviewHtml({ campaign, list, preview: null, cost: null, compliance: null, resultsOn: null, model }));
  assert.equal($("[data-cw-sum-names]"), null, "a list made without a campaign run has no names to show, and no gap");
  assert.ok($("[data-cw-sum-contact]"));
  draw(review({ summary: { top: { elsewhere: true }, caseId: null } }));
  assert.match(text($("[data-cw-sum-names]")), /on the full results page/, "a list no campaign run made says where its customers are");
});

test("how well it did on customers it had not seen: the Model page's sentence; a missing record is said, not skipped", () => {
  draw(review());
  assert.match(
    text($("[data-cw-sum-model]")),
    /^On customers it had not seen while learning, the model put a customer who went on to come back above one who did not, 78% of the time\. 50% would be no better than a coin toss; 100% would be perfect\.$/,
  );
  const other = SR.unseenSentence({ ...version, metric: "auuc", metric_label: "AUUC", test_score: 0.4123 });
  assert.match(other, /main measure was 0\.412\./);
  assert.deepEqual(findNeverShown(other), {}, "an analyst's measure name is not shown here");
  assert.equal(SR.unseenSentence(null), null);
  draw(review({ model: { ...model, version: null } }));
  assert.match(text($("[data-cw-sum-model]")), /could not be read/);
});

test("what is not proven yet is always said, with the day results can be added", () => {
  draw(review());
  assert.match(text($("[data-cw-sum-unproven]")), /^Not yet proven: nothing shows yet that contacting these customers works\./);
  assert.match(text($("[data-cw-sum-unproven]")), /Add the results after /);
  draw(review({ resultsOn: null }));
  assert.doesNotMatch(text($("[data-cw-sum-unproven]")), /Add the results after/);
});

test("What the Doctor did opens the case; it is not offered when the list has no case", () => {
  draw(review());
  assert.equal($("[data-cw-sum-doctor]").getAttribute("href"), "#/data/check/case/case_42");
  assert.equal(text($("[data-cw-sum-doctor]")), "See the approved plan");
  draw(review({ summary: { top, caseId: null } }));
  assert.equal($("[data-cw-sum-doctor]"), null);
  assert.equal(SR.doctorHref("a b"), "#/data/check/case/a%20b");
});

test("Approve and download follows the summary; the numbers and tables are folded behind Show technical details", () => {
  draw(review());
  const order = $$("[data-cw-results], [data-cw-review], [data-cw-technical]").map((el) => el.getAttribute("data-cw-results") !== null ? "summary" : el.hasAttribute("data-cw-review") ? "approve" : "technical");
  assert.deepEqual(order, ["summary", "approve", "technical"]);
  assert.equal(text($("[data-cw-approve]")), "Approve and download");
  const box = $("details[data-cw-technical]");
  assert.equal(box.open, false, "closed until asked for");
  assert.equal(text(box.querySelector("summary .cw-techbox-show")), "Show technical details");
  assert.ok(box.querySelector("[data-cw-tile=contacted]"), "the old tiles are inside");
  assert.ok(box.querySelector("[data-cw-audience]"), "... and the old tables");
  assert.deepEqual(
    [...box.querySelectorAll("[data-cw-technical-links] a")].map((a) => a.getAttribute("href")),
    ["#/uc/win-back-campaign/output/r_score", "#/uc/win-back-campaign/model/r_train"],
    "the way to the existing full pages",
  );
  assert.equal($$("[data-cw-results] .btn.primary").length, 0, "one primary button on the page: Approve");
});

test("a summary with nothing to go on says so rather than showing zeros", () => {
  draw(SV.resultsSummaryHtml({}));
  assert.match(text($("[data-cw-sum-contact]")), /could not be read for this list/);
  assert.equal($("[data-cw-sum-excluded]"), null);
  assert.deepEqual(SR.exclusionLines({ holdout_rows: 0, suppressed_rows: 0 }), []);
  assert.deepEqual(
    SR.exclusionLines({ exclusions: [{ label: "Duplicates", rows: 3 }] }).map((l) => l.text),
    ["3 customers: Duplicates"],
  );
});

test("the words are plain: none of the glossary's never-shown words", () => {
  draw(review());
  const words = text($("[data-cw-results]"));
  assert.deepEqual(findNeverShown(words), {});
});

test("the progress page, once done, leads to the summary and links to What the Doctor did when it has a case", () => {
  const record = (extra) => ({
    record: { campaign_run_id: "cr_1", state: "done", use_case_id: "win-back-campaign", campaign_id: "c_1", score_run_id: "r_score", steps: [], ...extra },
    pct: 100,
    summary: "Done",
  });
  draw(RV.runPageHtml(record({ case_id: "case_42" })));
  assert.equal($("[data-run-doctor]").getAttribute("href"), "#/data/check/case/case_42");
  assert.match(text($("[data-run-summary]")), /who to contact, why these customers, and how good the model is/);
  assert.equal($("[data-run-ready]").getAttribute("href"), "#/campaigns/new/review/c_1");
  draw(RV.runPageHtml(record({ case_id: null })));
  assert.equal($("[data-run-doctor]"), null);
  assert.equal($("[data-run-headline]"), null, "no count is made up while the list's counts are unknown");
  draw(RV.runPageHtml(record({ case_id: null }), { headline: SR.contactSentence(list) }));
  assert.equal(text($("[data-run-headline]")), "1,240 customers to contact of the 8,214 checked");
});

const lift = {
  mode: "classification",
  base_rate_pct: 11.6667,
  bins: [
    { rows: 60, actual_rate_pct: 58.3333, lift: 5.0 },
    { rows: 60, actual_rate_pct: 21.6667, lift: 1.8571 },
    { rows: 60, actual_rate_pct: 11.6667, lift: 1.0 },
    { rows: 60, actual_rate_pct: 1.6667, lift: 0.14 },
    { rows: 60, actual_rate_pct: 0, lift: 0 },
  ],
};
const importance = {
  items: [
    { feature: "plan_tier", share_pct: 47.9 },
    { feature: "visits_last_7d", share_pct: 27.6 },
    { feature: "ad_ctr_90d", share_pct: 16.0 },
    { feature: "region", share_pct: 6.5 },
    { feature: "age", share_pct: 0.0 },
  ],
};

test("how good the model is: one honest sentence a marketer understands, from the lift of customers it had not seen", () => {
  assert.equal(
    SR.liftSentence(lift),
    "The 20% of customers the model puts first are 5 times as likely as the average customer to have the outcome this goal looks for (58% against 12% for an average customer). Measured on 300 customers the model had not seen while learning.",
  );
  draw(review({ summary: { top, caseId: "case_42", story: { lift, importance } } }));
  assert.match(text($("[data-cw-sum-model]")), /^The 20% of customers the model puts first are 5 times as likely as the average customer/);
  assert.match(text($("[data-cw-sum-unproven]")), /^Not yet proven:/);
  // the groups and the model's own measure are one click away, not in the way
  const fold = $("[data-cw-sum-groups]").closest("details");
  assert.equal(fold.open, false);
  assert.equal(text(fold.querySelector("summary")), "See more");
  assert.equal($$("[data-cw-sum-groups] tbody tr").length, 5);
  assert.match(text(fold), /the model put a customer who went on to come back above one who did not, 78% of the time/);
  // a ranking no better than average is never sold as a result
  const flat = { ...lift, bins: [{ rows: 60, actual_rate_pct: 12, lift: 1.02 }, ...lift.bins.slice(1)] };
  assert.match(SR.liftSentence(flat), /not clearly more likely than the average customer .* so do not rely on this list yet/);
  // an answer that is not a yes/no ranking falls back to the model's own sentence
  assert.equal(SR.liftSentence({ mode: "regression", bins: lift.bins }), null);
  assert.equal(SR.liftSentence(null), null);
  assert.equal(SR.liftSentence({ ...lift, base_rate_pct: 0 }), null);
  draw(review({ summary: { top, caseId: "case_42", story: { lift: null, importance: null } } }));
  assert.match(text($("[data-cw-sum-model]")), /^On customers it had not seen while learning/);
  assert.equal($("[data-cw-sum-groups]"), null);
});

test("the caveat says what the numbers do not prove: without a comparison group, only who responds among those contacted", () => {
  const none = SR.notProvenText("", false);
  assert.match(none, /No customers are kept back to compare with, so this shows who is likely to respond among the customers you contact, not how many extra sales contacting them brings\./);
  assert.doesNotMatch(none, /extra customers this campaign brings can be measured/);
  assert.match(SR.notProvenText("", true), /Some customers are kept back and not contacted, so the extra customers this campaign brings can be measured/);
  draw(review({ list: { ...list, holdout_rows: null } }));
  assert.match(text($("[data-cw-sum-unproven]")), /No customers are kept back to compare with/);
  draw(review({ campaign: { ...campaign, counts: { holdout: 320 } }, list: { ...list, holdout_rows: null } }));
  assert.match(text($("[data-cw-sum-unproven]")), /Some customers are kept back/, "the campaign's own comparison group counts");
  assert.match(text($("[data-cw-sum-excluded]")), /320 customers: kept back on purpose as the comparison group/);
  assert.equal(SR.comparisonRows(null, null), 0);
});

test("why these customers: what the model relied on most in plain words, three in view and the rest folded", () => {
  assert.deepEqual(SR.reasonItems(importance), [
    { name: "Plan tier", share: "48%" },
    { name: "Visits last 7d", share: "28%" },
    { name: "Ad ctr 90d", share: "16%" },
    { name: "Region", share: "7%" },
  ], "a column that counts for nothing is left out");
  draw(review({ summary: { top, caseId: "case_42", story: { lift, importance } } }));
  assert.deepEqual($$("[data-cw-sum-reasons] li").map((li) => text(li).replace(/ \(.*$/, "")), ["Plan tier", "Visits last 7d", "Ad ctr 90d"]);
  assert.match(text($("[data-cw-sum-reasons] li")), /Plan tier \(48% of what it relied on\)/);
  assert.equal($$("[data-cw-results] details[data-cw-sum-more]").length, 2, "the other reason and the groups are each behind See more");
  draw(review());
  assert.match(text($("[data-cw-sum-why]")), /could not be read for this list/, "a missing file is said, not skipped");
  assert.equal(SR.columnWords("visits_last_7d"), "Visits last 7d");
});

test("the card answers in order: who to contact, why these customers, how good the model is, what was done to the data", () => {
  draw(review({ summary: { top, caseId: "case_42", story: { lift, importance } } }));
  const heads = $$("[data-cw-results] .cw-sum-h").map(text);
  assert.deepEqual(heads, ["Who to contact", "Why these customers", "How good the model is", "What was done to your data"]);
  assert.match(text($("[data-cw-sum-list]")), /Send only the customers the file marks to contact/);
  assert.deepEqual(findNeverShown(text($("[data-cw-results]"))), {}, "plain words only");
  draw(review({ summary: { top, caseId: null } }));
  assert.equal($("[data-cw-sum-data]"), null, "no plan to point to when the file did not come through the Data Doctor");
});

test("the goal's result is named in plain words, with a safe generic fallback for a goal without one", () => {
  assert.match(SR.liftSentence(lift, "telco-churn"), /5 times as likely as the average customer to leave \(58% against 12%/);
  assert.match(SR.unseenSentence(version, "retail-win-back"), /a customer who went on to buy again above one who did not, 78% of the time/);
  assert.match(SR.liftSentence(lift, "some-new-goal"), /as the average customer to have the outcome this goal looks for/);
  draw(review({ summary: { top, caseId: "case_42", story: { lift, importance } } }));
  assert.doesNotMatch(text($("[data-cw-sum-model]")), /the result you want/);
  assert.match(text($("[data-cw-sum-groups] thead")), /Went on to come back/);
});

const evaluation = {
  rows_evaluated: 4000,
  treated_rows: 2000,
  control_rows: 2000,
  causal: true,
  uplift_at: [
    { fraction: 0.1, uplift: { value: 0.042, ci_low: 0.021, ci_high: 0.063 } },
    { fraction: 0.2, uplift: { value: 0.03, ci_low: 0.01, ci_high: 0.05 } },
  ],
};

test("an effect model gets one honest sentence: the gap between contacted and not contacted, measured on customers it had not seen", () => {
  const sentence = SR.upliftSentence(evaluation, "targeted-advertisement");
  assert.equal(
    sentence,
    "Among the 10% of customers the model puts first, those who were contacted went on to buy 4.2 percentage points more often than those who were not contacted (somewhere between 2.1 and 6.3 points). Measured on 4,000 customers the model had not seen while learning (2,000 were contacted and 2,000 were not contacted).",
  );
  assert.doesNotMatch(sentence, /extra sales|will bring/i, "no claim about this campaign's sales");
  // contacting was not picked at random: the sentence says the gap may come from who was contacted
  assert.match(SR.upliftSentence({ ...evaluation, causal: false }), /not picked for contact at random/);
  // a gap that chance could give is never sold as an effect; a harmful one is said
  const unclear = { ...evaluation, uplift_at: [{ fraction: 0.1, uplift: { value: 0.004, ci_low: -0.02, ci_high: 0.03 } }] };
  assert.match(SR.upliftSentence(unclear), /not clear whether contacting them changes .* too small to tell apart from chance, so do not rely/);
  const harm = { ...evaluation, uplift_at: [{ fraction: 0.1, uplift: { value: -0.03, ci_low: -0.05, ci_high: -0.01 } }] };
  assert.match(SR.upliftSentence(harm), /less often .* may do harm/);
  assert.match(SR.upliftSentence({ ...harm, causal: false }), /may do harm.*not picked for contact at random/);
  // several offers: the numbers compare the first offer only, so the sentence names it
  const offers = SR.upliftSentence({ ...evaluation, arms: [{ arm: "Mens E-Mail" }, { arm: "Womens E-Mail" }] });
  assert.match(offers, /those sent the Mens E-Mail went on to have the outcome/);
  assert.match(offers, /2,000 were sent the Mens E-Mail and 2,000 were not contacted/);
  assert.equal(SR.upliftSentence({ ...evaluation, arms: [{}] }), null);
  // no comparison group in the evaluation: nothing to say, so the page falls back
  assert.equal(SR.upliftSentence({ ...evaluation, control_rows: 0 }), null);
  assert.equal(SR.upliftSentence(null), null);
  const uplifted = { ...campaign, use_case_id: "criteo-uplift" };
  draw(review({ campaign: uplifted, summary: { top, caseId: "case_42", story: { lift: null, importance: null, evaluation } } }));
  assert.match(text($("[data-cw-sum-model]")), /^Among the 10% of customers the model puts first, those who were contacted went on to buy 4\.2 percentage points more often/);
  assert.match(text($("[data-cw-sum-unproven]")), /^Not yet proven:/);
});
