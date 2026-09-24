/* Phase 3b §8 — uplift: the uplift screen's Setup (its problem type and treatment picker), the
   TREATMENT_NOT_RANDOM acknowledgement, Results → Model (Qini, AUUC with its interval, uplift by
   decile), Results → Output (four segments, who to contact, the treat list) and the Campaign results
   page. DEC-608: uplift is not a Phase 1 Setup choice. The uplift screens live under
   #/uplift/win-back-campaign, reached from the "Uplift for this use case" link under the use case's
   header; the use case's own Setup (#/uc/win-back-campaign) is Phase 1's. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, go, ev, $, $$, body, click, set, settle, upload, repoText } from "./harness.mjs";

const WB = "win-back-campaign";
const PTYPE = "Uplift (who changes because of your action)";
/* The uplift screen (DEC-608). Its Setup form is UPSTATE[WB]; the uplift view of the use case is upView(u). */
const UP = `#/uplift/${WB}`;
const submit = (dom) => $(dom, "#f-setup").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
const kpiMap = (dom) => Object.fromEntries($$(dom, ".kpi").map((k) => [k.querySelector(".l").textContent, k.querySelector(".v").textContent]));
const kv = (dom, key) => {
  const row = $$(dom, ".kv").find((r) => r.querySelector(".k").textContent === key);
  return row ? row.querySelector(".v").textContent : undefined;
};

/** A fresh page on the win-back use case's own (Phase 1) Setup, with the clock pinned, so
 *  "Results available on" is stable. */
function winback() {
  const dom = load(`#/uc/${WB}`);
  ev(dom, "AS_OF='2026-09-23T10:00:00Z'");
  return dom;
}
/** The same, on win-back's uplift screen: where every uplift run starts (DEC-608). */
function upliftScreen() {
  const dom = load(UP);
  ev(dom, "AS_OF='2026-09-23T10:00:00Z'");
  return dom;
}
/** Run `fn` with the process in time zone `tz` (Node re-reads TZ when it is assigned, jsdom's Date included). */
async function inTimeZone(tz, fn) {
  const was = process.env.TZ;
  process.env.TZ = tz;
  try {
    return await fn();
  } finally {
    if (was === undefined) delete process.env.TZ;
    else process.env.TZ = was;
  }
}
async function trainUplift(dom, { targeted = false } = {}) {
  click(dom, targeted ? "#f-sampletargeted" : "#f-samplecampaign");
  if (targeted) set(dom, "#f-ack", true);
  submit(dom);
  await settle(dom);
  assert.ok($(dom, ".results"), "the uplift run reaches its results");
}
async function scoreUplift(dom) {
  if ($(dom, "#f-again")) click(dom, "#f-again");
  click(dom, '.seg button[data-mode="score"]');
  click(dom, "#f-sample");
  submit(dom);
  await settle(dom);
  assert.ok($(dom, ".results"), "the scoring run reaches its results");
}

/* ---------- Setup ---------- */

test("the treatment picker appears only when a column looks like a treatment", async () => {
  const dom = upliftScreen();
  assert.equal($(dom, "#f-treat"), null, "nothing to pick before a file");
  assert.equal($(dom, "#f-sample"), null, "the plain sample is Phase 1's: no treatment column, so the uplift screen does not offer it");
  // an upload with no column named like a treatment gets the product's note instead of a picker
  await upload(dom, "#f-file", "plain.csv", "customer_id,months_since_churn,reactivated_90d\nC1,2,0\nC2,5,1\n");
  assert.equal($(dom, "#f-treat"), null);
  assert.match($(dom, "[data-up-notreat]").textContent, /^No 0\/1 column in this file looks like a treatment\. Uplift needs one/);
  assert.equal($(dom, ".reason").textContent, "Choose the treatment column");
  click(dom, "#f-samplecampaign");
  assert.equal($(dom, "[data-up-notreat]"), null);
  const pick = $(dom, "#f-treat");
  assert.ok(pick, "the sample campaign file carries a treatment column");
  assert.deepEqual([...pick.options].map((o) => o.textContent), ["None", "treatment"]);
  assert.equal(pick.value, "treatment", "the detected column is pre-selected, as the API's `detected` is");
  const field = pick.closest(".field");
  assert.match(field.textContent, /1 means the customer received the action, 0 means held out\. It must be randomly assigned\./);
  assert.equal(field.querySelector("[data-up-share]").textContent, "Treated share: 90% treated");
  // the step list does not grow: the picker is a field inside step 2
  assert.deepEqual($$(dom, ".flabel").map((e) => e.textContent.trim()), ["Dataset", "Columns", "Model"]);
  // a real win-back upload with a hinted column gets the picker, and its treated share is not guessed
  await upload(dom, "#f-file", "offers.csv",
    "customer_id,months_since_churn,treatment,reactivated_90d\nC1,2,1,0\nC2,5,0,1\n");
  assert.equal($(dom, "#f-treat").value, "treatment");
  assert.equal($(dom, "[data-up-share]").textContent, "Treated share: —");
  assert.equal($(dom, ".ptype .pill").textContent, PTYPE);
  // the use case's own Setup is Phase 1's: its plain sample has no treatment column, and no picker
  go(dom, `#/uc/${WB}`);
  await settle(dom);
  click(dom, "#f-sample");
  assert.equal($(dom, "#f-treat"), null);
  // another use case's sample has no such column, so no picker
  go(dom, "#/uc/payment-propensity");
  await settle(dom); // let the hashchange render land before a file read starts
  click(dom, "#f-sample");
  assert.equal($(dom, "#f-treat"), null);
});

test("Uplift is the uplift screen's problem type, with its one-line explanation", () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  assert.equal($(dom, ".ptype .pill").textContent, PTYPE);
  // DEC-608: the type is the screen's, not detected and not changeable there (Phase 1's Setup is elsewhere)
  assert.equal($(dom, "#f-ptype"), null, "no change… on the uplift screen");
  assert.doesNotMatch($(dom, ".ptype").textContent, /detected from|set manually/);
  assert.equal($(dom, ".ptype .upwhy").textContent, "Predicts who changes behaviour because of your action.");
  assert.doesNotMatch($(dom, ".ptype").textContent, /Metrics and models will switch/);
  // step 3 offers the meta-learners instead of the algorithm list
  assert.deepEqual([...$(dom, "#f-learner").options].map((o) => o.textContent),
    ["X-learner (recommended)", "T-learner", "S-learner (baseline)"]);
  assert.deepEqual([...$(dom, "#f-base").options].map((o) => o.textContent), ["AutoGluon fast", "LightGBM"]);
  assert.equal($(dom, "#f-model"), null);
  assert.equal($(dom, "#f-run").disabled, false);
});

test("DEC-608: Phase 1's change… never offers Uplift, not even for a file with a treatment column", async () => {
  const dom = winback();
  const PHASE1 = ["Classification (yes / no)", "Regression (a number)", "Forecasting (a number over time)", "Clustering (no target)"];
  const opts = (d) => [...$(d, "#f-ptype").options].map((o) => o.textContent).slice(1);
  go(dom, "#/uc/payment-propensity");
  click(dom, "#f-sample");
  assert.deepEqual(opts(dom), PHASE1, "no Uplift");
  assert.match($(dom, ".ptype .pill").textContent, /^Classification/);
  go(dom, `#/uc/${WB}`);
  click(dom, "#f-sample");
  assert.deepEqual(opts(dom), PHASE1, "the plain win-back sample has no treatment column either");
  assert.equal($(dom, "#f-samplecampaign"), null, "the campaign files are the uplift screen's");
  // a win-back file that does carry a treatment column is an ordinary Phase 1 file here
  await upload(dom, "#f-file", "offers.csv", campaignCsv(2400));
  assert.deepEqual(opts(dom), PHASE1, "a treatment column does not bring Uplift into Phase 1's Setup");
  assert.equal($(dom, "#f-treat"), null);
  assert.match($(dom, ".ptype .pill").textContent, /^Classification/);
  assert.match($(dom, ".ptype").textContent, /detected from the target column/);
  assert.ok($$(dom, "#f-target option").some((o) => o.textContent === "treatment"), "treatment stays a column");
  assert.ok($(dom, "#f-model"), "the Phase 1 model step");
  assert.equal($(dom, "#f-learner"), null);
  assert.equal($(dom, ".upchecks"), null);
  // Uplift is chosen by going to its screen, from the related link in the header's actions
  const link = $(dom, ".head-actions a.related");
  assert.equal(link.getAttribute("href"), UP);
  click(dom, link);
  await settle(dom);
  assert.equal(dom.window.location.hash, UP);
  assert.equal($(dom, "h1").textContent, "Win-back Campaign · Uplift");
});

test("an uplift run without a treatment column is blocked, with the reason", () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  set(dom, "#f-treat", "");
  assert.equal($(dom, ".ptype .pill").textContent, PTYPE, "the problem type stays Uplift");
  assert.equal($(dom, ".reason").textContent, "Choose the treatment column");
  assert.equal($(dom, "#f-run").disabled, true);
  set(dom, "#f-treat", "treatment");
  assert.equal($(dom, ".reason").textContent, "");
  assert.equal($(dom, "#f-run").disabled, false);
});

test("uplift settings sit inside the existing stages 5 and 6, each with a hint", () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  assert.deepEqual($$(dom, ".stage-d .st").map((e) => e.textContent), [
    "Data preparation", "Data split", "Feature engineering", "Model search",
    "Evaluation & explainability", "Actions & output", "Monitoring & retraining", "Governance & privacy",
  ]);
  const val = (k) => $(dom, `[data-up-num="${k}"]`).value;
  assert.equal(val("upBoot"), "200");
  assert.equal(val("upTest"), "30");
  assert.equal(val("upPers"), "0.02");
  assert.equal(val("upSleep"), "-0.01");
  assert.equal(val("upBudget"), "4000");
  assert.equal(val("upSure"), "", "sure_thing_min_probability is unset: the training base rate");
  assert.match($(dom, '[data-up-num="upSure"]').closest(".field").textContent, /Empty means the training base rate \(0\.099\)\./);
  assert.equal(val("upCost"), "", "unset is empty, shown as —");
  assert.equal($(dom, '[data-up-num="upCost"]').placeholder, "—");
  assert.equal($(dom, '[data-up-num="upValue"]').value, "");
  // the control group is shown, never edited
  assert.equal($(dom, "[data-up-control]").textContent, "10% · fixed");
  assert.equal($(dom, '[data-adv="control"]'), null);
  assert.equal($(dom, '[data-adv="hi"]'), null, "segments replace the High / Medium cuts");
  // the stage summaries follow the settings
  const summ = $$(dom, ".stage-d .ss").map((e) => e.textContent);
  assert.match(summ[4], /AUUC · 200 bootstrap resamples · 30% hold-out/);
  assert.match(summ[5], /Persuadable ≥ 0\.02 · sleeping dog ≤ -0\.01 · sure thing P ≥ base rate · budget 4,000/);
  set(dom, '[data-up-num="upSure"]', "0.15");
  assert.match($$(dom, ".stage-d .ss")[5].textContent, /sure thing P ≥ 0\.15 ·/);
  set(dom, '[data-up-num="upBudget"]', "");
  assert.match($$(dom, ".stage-d .ss")[5].textContent, /budget —/);
  // every uplift field explains itself in one sentence
  for (const el of $$(dom, "[data-up-num], #f-treat, #f-learner, #f-base, [data-up-control]")) {
    const hint = el.closest(".field").querySelector(".uphint");
    assert.ok(hint && hint.textContent.endsWith("."), `no one-line hint for ${el.dataset.upNum || el.id || "control"}`);
  }
});

test("TREATMENT_NOT_RANDOM: the six checks fail inline and Run waits for an acknowledgement", () => {
  const dom = upliftScreen();
  click(dom, "#f-sampletargeted");
  const checks = $$(dom, ".upchecks .checks li");
  assert.deepEqual(checks.map((li) => li.dataset.upCheck), ["TREATMENT_COLUMN_MISSING", "TREATMENT_NOT_BINARY",
    "TREATMENT_ARM_TOO_SMALL", "TREATMENT_NOT_RANDOM", "OUTCOME_WINDOW_IMMATURE", "FEATURE_AFTER_TREATMENT"]);
  const bad = checks.filter((li) => li.classList.contains("bad"));
  assert.equal(bad.length, 1);
  assert.equal(bad[0].dataset.upCheck, "TREATMENT_NOT_RANDOM");
  // the engine's own finding (engine/uplift/checks.py::_not_random), word for word
  assert.equal(bad[0].querySelector("span:last-child").textContent,
    "Who was treated can be predicted from the customers' own data (AUC 0.74, where a random assignment " +
    "scores about 0.50 and the limit is 0.60). The strongest signs were 'months_since_churn' and " +
    "'avg_monthly_spend'. The campaign looks targeted, so comparing treated with untreated customers would " +
    "mix what the campaign changed with how the chosen customers already differed.");
  assert.equal($(dom, '[data-up-fix="TREATMENT_NOT_RANDOM"]').textContent,
    "TREATMENT_NOT_RANDOM Use data from a campaign with a randomly chosen hold-out group. If you go ahead " +
    "anyway, every uplift result will be labelled not causal.");
  assert.match(checks[2].textContent, /each above 1,000 rows and 50 conversions/);
  assert.equal($(dom, "#f-run").disabled, true);
  assert.match($(dom, ".reason").textContent, /TREATMENT_NOT_RANDOM/);
  set(dom, "#f-ack", true);
  assert.equal($(dom, "#f-run").disabled, false);
  assert.equal($(dom, ".reason").textContent, "");
  // the random sample never shows the panel
  click(dom, "#f-samplecampaign");
  assert.equal($(dom, ".upchecks"), null);
});

test("an acknowledged run is labelled not causal on Model, Output and Campaign results", async () => {
  const dom = upliftScreen();
  await trainUplift(dom, { targeted: true });
  assert.equal($$(dom, ".progress .pt").length, 0);
  assert.match($(dom, ".summary").textContent, /Not promoted: not causal/);
  assert.equal(ev(dom, `UPSTATE['${WB}'].current.causal`), false, "a targeted run is not causal");
  assert.doesNotMatch(body(dom), /Candidate · promote/, "and never offered for promotion");
  const note = ev(dom, "NOT_CAUSAL_NOTE");
  go(dom, `${UP}/model`);
  assert.equal($(dom, ".ncbanner").textContent.replace("⚠", "").trim(), note);
  assert.ok($(dom, "[data-up-summary]").textContent.startsWith(note + " Targeting by predicted uplift"),
    "the summary sentence carries the note, as UpliftEvaluation.summary does");
  go(dom, `${UP}/output`);
  assert.equal($(dom, ".ncbanner").textContent.replace("⚠", "").trim(), note);
  go(dom, UP);
  await scoreUplift(dom);
  go(dom, `${UP}/output`);
  assert.ok($(dom, ".ncbanner"), "a list scored with a not-causal model is labelled too");
  go(dom, `${UP}/campaign`);
  // the lift itself stays causal: the engine drew the control group at random
  assert.match($(dom, ".ncbanner").textContent, /The model behind this list is not causal/);
  assert.match($(dom, ".ncbanner").textContent, /The lift below is still causal/);
});

/* ---------- Running and results ---------- */

test("the uplift run checks the treatment, trains the learner and measures on the hold-out", async () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  submit(dom);
  assert.deepEqual($$(dom, ".progress .pt").map((e) => e.textContent), [
    "Checking the treatment", "Preparing features", "Training the X-learner",
    "Measuring uplift on the hold-out (200 resamples)", "Segmenting and saving",
  ]);
  await settle(dom);
  const done = ev(dom, `STATE['${WB}'].runs.at(-1)`);
  assert.equal(done.ptype, "Uplift");
  assert.equal(done.score, "AUUC 0.0125");
  // DEC-609/DEC-649: Win-back is configured for classification and its slot holds a propensity
  // champion, so a causal, measurable uplift run stays a candidate until a person promotes it.
  assert.equal(done.champion, false);
  assert.equal(ev(dom, `STATE['${WB}'].runs[0].champion`), true, "the propensity champion is untouched");
  assert.match($(dom, ".summary").textContent, /Best model X-learner · AutoGluon fast · AUUC 0\.0125/);
  assert.match($(dom, ".summary").textContent, /Candidate · promote it on the Models page/);
  assert.doesNotMatch(body(dom), /Set as uplift champion|Uplift champion/);
  assert.equal($$(dom, ".flow .block").length, 3, "still three blocks");
  assert.match($$(dom, ".flow .block")[2].textContent, /Recommended to contact: 4,000/);
  assert.match($$(dom, ".runrow .r1").at(-1).textContent, /^X-learner · AutoGluon fast$/, "listed without a champion badge");
});

test("Results → Model: Qini curve with the random line, AUUC with its interval, uplift by decile", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  go(dom, `${UP}/model`);
  assert.equal($(dom, ".ncbanner"), null, "a random treatment is causal");
  assert.deepEqual(kpiMap(dom), {
    "Learner": "X-learner", "AUUC": "0.0125", "Qini coefficient": "0.0113", "Uplift in the top 10%": "+13.50 pts",
  });
  // the chart: the model, the random line, and they meet at 100%
  const model = $(dom, 'polyline[data-line="model"]').getAttribute("points").split(" ");
  const random = $(dom, 'polyline[data-line="random"]').getAttribute("points").split(" ");
  assert.equal(model.length, 11);
  assert.equal(random.length, 11);
  assert.equal(model[0], random[0]);
  assert.equal(model.at(-1), random.at(-1), "the Qini curve ends on the random line");
  assert.equal($(dom, 'polyline[data-line="model"]').getAttribute("stroke"), "var(--c)");
  assert.match($(dom, 'polyline[data-line="random"]').getAttribute("stroke-dasharray"), /\d/);
  assert.match($(dom, ".qini").textContent, /Share of customers targeted/);
  // AUUC with its interval and the metrics.py sentence
  assert.match($(dom, "#up-auuc .civ").textContent, /^0\.0125\s*95% CI 0\.0098 to 0\.0151$/);
  assert.equal($(dom, "[data-up-summary]").textContent,
    "Targeting by predicted uplift beats random targeting: AUUC 0.0125 (95% CI 0.0098 to 0.0151).");
  assert.match($(dom, "#up-auuc").textContent, /Candidate/);
  assert.match($(dom, "#up-auuc").textContent, /promote it on the Models page/);
  assert.doesNotMatch($(dom, "#up-auuc").textContent, /Uplift champion/);
  assert.equal(kv(dom, "Hold-out"), "96,000 rows · 86,400 treated · 9,600 control");
  // UpliftEvaluation.uplift_at: the top 10%, 20% and 30%, each with its interval
  assert.equal(kv(dom, "Uplift in the top 10%"), "+13.50 pts (95% CI 11.87 to 14.83)");
  assert.equal(kv(dom, "Uplift in the top 20%"), "+11.40 pts (95% CI 10.26 to 12.39)");
  assert.equal(kv(dom, "Uplift in the top 30%"), "+9.85 pts (95% CI 8.91 to 10.69)");
  assert.ok($(dom, "[data-up-at]").textContent.endsWith("."));
  // uplift by decile: diverging bars, the sleeping-dog deciles below zero
  const rows = $$(dom, ".dbrow");
  assert.equal(rows.length, 10);
  assert.deepEqual(rows.filter((r) => r.querySelector(".dob.neg")).map((r) => r.dataset.upDecile), ["9", "10"]);
  assert.match(rows[0].querySelector(".dv").textContent, /^\+13\.50 pts · predicted \+14\.20 pts$/);
  assert.equal($(dom, ".vchart"), null, "the Phase 1 bar chart cannot draw negative bars");
  assert.equal($$(dom, ".dbars ~ .tbl-wrap tbody tr").length, 10);
  assert.deepEqual($$(dom, ".dbars ~ .tbl-wrap th").map((e) => e.textContent),
    ["decile", "n", "treated rate", "control rate", "observed uplift", "predicted uplift"]);
  // what a null result reads like
  click(dom, "#up-null");
  assert.equal($(dom, "[data-up-summary]").textContent,
    "No measurable uplift: the AUUC interval (-0.0021 to 0.0034) includes zero, so this model cannot be shown to target better than random.");
  assert.ok($(dom, "#up-auuc .ciband.bad"));
  click(dom, "#up-null");
  assert.match($(dom, "[data-up-summary]").textContent, /^Targeting by predicted uplift/);
});

test("Results → Output after training: four segments on the hold-out, and no list to download yet", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  go(dom, `${UP}/output`);
  assert.equal(kpiMap(dom)["Recommended to contact"], "4,000");
  assert.equal(kpiMap(dom)["Persuadables"], "57,600");
  const segs = $$(dom, ".segrow");
  assert.deepEqual(segs.map((r) => r.querySelector(".sn").firstChild.textContent),
    ["Persuadables", "Sure things", "Lost causes", "Sleeping dogs"]);
  assert.deepEqual(segs.map((r) => r.querySelector(".sa").textContent),
    ["Treat", "Don't treat (converts anyway)", "Don't treat (won't convert)", "Never treat (contact makes it worse)"]);
  assert.match($(dom, "[data-up-computed]").textContent, /Computed on the 96,000-customer hold-out of the training run/);
  assert.match($(dom, "[data-up-computed]").textContent,
    /in between, sure thing when P\(convert without contact\) ≥ 0\.099, the training base rate, otherwise lost cause\.$/);
  assert.equal($(dom, "#up-dl").disabled, true);
  assert.match($(dom, "#up-dl").parentElement.textContent, /Score a list with this model to download who to contact\./);
  assert.equal(ev(dom, `treatListRows(upView(UC.find(u=>u.id==='${WB}'))).length`), 0);
  // the campaign copy is still there, unchanged, below the uplift content
  assert.equal($$(dom, ".cccard").length, 12);
  assert.match($(dom, ".ccbar").textContent, /7 approved · 3 pending review · 2 blocked/);
});

test("Results → Output after scoring: who to contact within budget, and a treat list with no sleeping dog", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  await scoreUplift(dom);
  assert.match($(dom, ".summary").textContent, /14,500 rows scored with X-learner · AutoGluon fast · Recommended to contact: 4,000/);
  // v1: Campaign results is the fourth block of a scoring run's flow
  assert.ok($(dom, ".flow .block#f-campaign"), "the campaign results, as the fourth block");
  assert.equal($$(dom, ".flow .block").length, 4);
  go(dom, `${UP}/output`);
  assert.deepEqual($$(dom, ".tabs .tab").map((t) => t.textContent.replace(/^\d+/, "")),
    ["Data", "Model", "Output", "Campaign results"]);
  assert.deepEqual(kpiMap(dom), {
    "Recommended to contact": "4,000", "Expected incremental conversions": "405",
    "Persuadables": "5,220", "Sleeping dogs, never contacted": "1,160",
  });
  assert.match($(dom, "[data-up-computed]").textContent, /Computed on every one of the 14,500 scored customers/);
  assert.equal($(dom, "[data-up-n]").textContent, "4,000");
  assert.match($(dom, ".upbig").textContent, /Stopped at the contact budget\. Budget: 4,000 contacts\./);
  assert.equal(kv(dom, "Eligible persuadables"), "4,032");
  assert.match(kv(dom, "Expected incremental conversions"), /^405 \(95% CI \d+ to \d+\)$/);
  for (const k of ["Cost per contact", "Value per conversion", "Expected cost", "Expected value", "Expected net value"]) {
    assert.equal(kv(dom, k), "—", `${k} is not configured, so it is not invented`);
  }
  // the download
  assert.equal($(dom, "#up-dl").disabled, false);
  const csv = ev(dom, `treatListCsv(upView(UC.find(u=>u.id==='${WB}')))`).split("\n");
  assert.equal(csv[0], "customer_id,uplift,p_treated,p_control,segment,band,action,reason_1,reason_2,reason_3,suppressed_reason,control_group,intended_treatment");
  assert.equal(csv.length, 4001, "N rows, and nobody outside the budget");
  const rows = JSON.parse(ev(dom, `JSON.stringify(treatListRows(upView(UC.find(u=>u.id==='${WB}'))))`));
  assert.ok(rows.every((r) => r.segment === "persuadable" && r.action === "Treat"), "persuadables only");
  assert.ok(!rows.some((r) => r.segment === "sleeping_dog"), "never a sleeping dog");
  assert.ok(rows.every((r) => r.control_group === "false"), "never a control-group row");
  assert.ok(rows.every((r) => Number(r.uplift) >= 0.02));
  assert.ok(rows.every((r, i) => i === 0 || Number(r.uplift) <= Number(rows[i - 1].uplift)), "highest uplift first");
  assert.ok(rows.every((r) => Math.abs(Number(r.p_treated) - Number(r.p_control) - Number(r.uplift)) < 2e-4));
  // a smaller budget is a shorter list; the value/cost rule can stop it earlier still
  go(dom, UP);
  ev(dom, `UPSTATE['${WB}'].current.adv.upBudget=1000`);
  go(dom, `${UP}/output`);
  assert.equal(kpiMap(dom)["Recommended to contact"], "1,000");
  ev(dom, `Object.assign(UPSTATE['${WB}'].current.adv,{upBudget:null,upCost:40,upValue:600})`);
  go(dom, `${UP}/output`);
  assert.match($(dom, ".upbig").textContent, /cost more than it is expected to earn/);
  assert.equal(kv(dom, "Cost per contact"), "₹40");
  assert.notEqual(kv(dom, "Expected net value"), "—");
});

/* ---------- Campaign results ---------- */

test("Campaign results: a list scored today says when its results will be ready", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  await scoreUplift(dom);
  go(dom, `${UP}/campaign`);
  assert.equal($(dom, ".tabs .tab.on").textContent.replace(/^\d+/, ""), "Campaign results");
  assert.equal($(dom, "#cr-run").value, ev(dom, `UPSTATE['${WB}'].current.id`), "the current run is chosen");
  // 23 Sep 2026 + 90 days
  assert.equal($(dom, "[data-up-wait] .cw1").textContent, "Results available on 22 Dec 2026");
  assert.deepEqual(Object.values(kpiMap(dom)), ["—", "—"], "nothing is measured before the window ends");
  click(dom, "#cr-sample");
  assert.equal($(dom, "[data-up-wait] .cw1").textContent, "Results available on 22 Dec 2026");
  // incrementality.py::_summary's IMMATURE sentence, verbatim (the engine prints the ISO date and a bare count)
  assert.equal($(dom, "[data-up-summary]").textContent,
    "Results available on 2026-12-22: the 90-day outcome window has not elapsed yet for any of the 4518 treated and control customers.");
});

test("Campaign results: the 1 May campaign, measured once its window has ended", () => {
  const dom = winback();
  go(dom, `#/uc/${WB}/campaign`);
  assert.equal($$(dom, ".tabs .tab").length, 4);
  assert.equal($(dom, "#cr-run").value, "r0501");
  assert.match($(dom, "#cr-run").selectedOptions[0].textContent, /Scored 14,500 rows · 1 May 2026/);
  assert.match(body(dom), /Upload the outcomes file to measure this campaign\./);
  click(dom, "#cr-sample");
  // v1: the verdict, in the words and sign of the rupee value view, then two labelled tiles
  assert.equal($(dom, ".verdict .vline").textContent, "The campaign worked: about 390 extra customers came back.");
  assert.deepEqual(kpiMap(dom), {
    "Contacted customers who came back": "11.0% of 11,200", "Control group who came back": "7.5% of 1,450",
  });
  assert.equal(kv(dom, "Absolute lift"), "+3.48 pts (95% CI 1.91 to 4.86)");
  assert.equal(kv(dom, "Relative lift"), "+46.3%");
  assert.equal(kv(dom, "Incremental conversions"), "390 (95% CI 213 to 545)");
  assert.equal(kv(dom, "p-value (two-sided)"), "5.0e-5");
  assert.equal(kv(dom, "Rows still inside their window"), "0");
  assert.equal(kv(dom, "Rows without an outcome"), "0");
  assert.equal(kv(dom, "Suppressed or not treated"), "1,850");
  // incrementality.py::_summary, verbatim
  assert.equal($(dom, "[data-up-summary]").textContent,
    "Treated customers converted at 11.0% against 7.5% for the control group: a lift of +3.5 points " +
    "(95% CI +1.9 points to +4.9 points; p < 0.001), about 390 extra conversions caused by the campaign.");
  assert.equal($(dom, ".ncbanner"), null, "a Phase 1 control group is causal");
  // a longer window has not ended yet: 1 May + 180 days
  set(dom, "#cr-window", "180");
  assert.equal($(dom, "[data-up-wait] .cw1").textContent, "Results available on 28 Oct 2026");
  assert.match($(dom, "[data-up-summary]").textContent, /for any of the 12650 treated and control customers\.$/);
  // every campaign setting explains itself
  for (const el of $$(dom, "#cr-run, #cr-file, #cr-outcome, #cr-window, #cr-datecol")) {
    const hint = el.closest(".field").querySelector(".uphint");
    assert.ok(hint && hint.textContent.endsWith("."), `no hint for ${el.id}`);
  }
});

/* ---------- Everything else stays as it was ---------- */

test("the other use cases, and win-back before an uplift run, get no uplift UI", async () => {
  const dom = winback();
  for (const id of ["targeted-advertisement", "order-fulfillment", "fault-prediction", "payment-propensity", "rca"]) {
    go(dom, `#/uc/${id}`);
    click(dom, "#f-sample");
    assert.equal($(dom, "#f-treat"), null, `${id} has no treatment picker`);
    assert.equal($(dom, "#f-sampletargeted"), null, `${id} has no targeted sample`);
    assert.equal($(dom, ".upwhy"), null);
    assert.match($(dom, ".ptype .pill").textContent, /^Classification/);
    for (const page of ["model", "output"]) {
      go(dom, `#/uc/${id}/${page}`);
      assert.equal($(dom, ".qini"), null);
      assert.equal($(dom, ".segrow"), null);
      assert.equal($(dom, ".ncbanner"), null);
      assert.equal($$(dom, ".tabs .tab").length, 3);
    }
    go(dom, `#/uc/${id}/campaign`);
    assert.equal($(dom, ".tabs"), null, `${id} has no campaign page`);
  }
  // win-back's own pages are the Phase 1 ones until an uplift run is current
  for (const page of ["model", "output"]) {
    go(dom, `#/uc/${WB}/${page}`);
    assert.equal($(dom, ".qini"), null);
    assert.equal($(dom, ".segrow"), null);
    assert.equal($$(dom, ".tabs .tab").length, 3);
  }
  assert.equal($$(dom, ".cccard").length, 12);
  // the uplift screens exist for win-back only: another use case's #/uplift URL is the uplift index
  for (const id of ["payment-propensity", "rca", "ai-onboarding-assistant"]) {
    go(dom, `#/uplift/${id}`);
    assert.equal($(dom, "h1").textContent, "Uplift modelling");
    assert.equal($(dom, "#f-setup"), null, `${id} gets no uplift Setup with win-back's numbers`);
    assert.equal($(dom, `.uindex a[href="#/uplift/${id}"]`), null);
  }
  // a classification run on win-back stays a classification run, even on a file with a treatment column
  go(dom, `#/uc/${WB}`);
  await settle(dom);
  await upload(dom, "#f-file", "offers.csv", campaignCsv(2400));
  assert.match($(dom, ".ptype .pill").textContent, /^Classification/);
  submit(dom);
  await settle(dom);
  assert.equal(ev(dom, `STATE['${WB}'].current.ptype`), "Classification");
  go(dom, `#/uc/${WB}/output`);
  assert.equal($(dom, ".segrow"), null);
  assert.ok($(dom, ".vchart"));
});

/* ---------- Review round 1 ---------- */

test("the plain win-back sample is the Phase 1 path: no treatment column, a propensity champion", async () => {
  const dom = winback();
  click(dom, "#f-sample");
  const feats = JSON.parse(ev(dom, `JSON.stringify(UC.find(u=>u.id==='${WB}').data.ft.map(f=>f[0]))`));
  assert.deepEqual(JSON.parse(ev(dom, `JSON.stringify(STATE['${WB}'].cols)`)),
    ["customer_id", "snapshot_date", ...feats, "reactivated_90d"], "the columns are Phase 1's");
  assert.equal(ev(dom, `STATE['${WB}'].file.name`), "win_back_campaign_history.csv");
  assert.equal($(dom, "#f-treat"), null);
  assert.equal($(dom, ".upwhy"), null);
  assert.equal($(dom, ".ptype .pill").textContent, "Classification (yes / no)");
  assert.ok($(dom, "#f-model"), "the Phase 1 model step, not the meta-learners");
  assert.equal($(dom, "#f-learner"), null);
  // DEC-608: uplift is not a choice on this Setup; it is a link to the uplift screen, under the header
  assert.equal($(dom, "#f-samplecampaign"), null, "the campaign files are on the uplift screen");
  assert.equal($(dom, "#f-sampletargeted"), null);
  assert.equal($(dom, ".head-actions a.related").getAttribute("href"), UP, "uplift is an explicit opt-in, on its own screen");
  submit(dom);
  assert.equal($$(dom, ".progress .pt")[0].textContent === "Checking the treatment", false);
  await settle(dom);
  const summary = $(dom, ".summary").textContent;
  assert.match(summary, /Best model LightGBM \+ Claude copy · ROC-AUC 0\.78/);
  assert.match(summary, /Set as champion/);
  assert.doesNotMatch(summary, /uplift/i);
  assert.equal(ev(dom, `STATE['${WB}'].current.ptype`), "Classification");
  go(dom, `#/uc/${WB}/model`);
  assert.match($(dom, "h1").textContent, /Win-back Propensity \+ GenAI Content/);
  assert.equal($(dom, ".qini"), null);
  go(dom, `#/uc/${WB}/output`);
  assert.equal($(dom, ".segrow"), null);
  assert.ok($(dom, ".vchart"));
});

test("uplift never leaks into another use case: a treatment-like column there is an ordinary column", async () => {
  const dom = winback();
  go(dom, "#/uc/payment-propensity");
  await settle(dom);
  await upload(dom, "#f-file", "offers.csv",
    "account_id,late_payments_6m,contacted,late_or_missed_payment\nA1,2,1,0\nA2,0,0,1\n");
  set(dom, "#f-target", "late_or_missed_payment");
  assert.equal($(dom, "#f-treat"), null, "no treatment picker outside win-back");
  assert.match($(dom, ".ptype .pill").textContent, /^Classification/);
  assert.ok(![...$(dom, "#f-ptype").options].some((o) => o.textContent === PTYPE));
  assert.ok($$(dom, "#f-target option").some((o) => o.textContent === "contacted"), "contacted stays a column");
  submit(dom);
  await settle(dom);
  assert.notEqual(ev(dom, "STATE['payment-propensity'].current.ptype"), "Uplift");
  for (const page of ["model", "output"]) {
    go(dom, `#/uc/payment-propensity/${page}`);
    assert.equal($(dom, ".segrow"), null);
    assert.equal($(dom, ".qini"), null);
    assert.doesNotMatch(body(dom), /57,600|months_since_churn|Recommended to contact|Persuadables/, `${page} shows no win-back uplift numbers`);
  }
});

test("a two-row upload on win-back is stopped by TREATMENT_ARM_TOO_SMALL, in the engine's words", async () => {
  const dom = upliftScreen();
  await upload(dom, "#f-file", "tiny.csv",
    "customer_id,months_since_churn,treatment,reactivated_90d\nC1,2,1,0\nC2,5,0,1\n");
  assert.equal($(dom, ".ptype .pill").textContent, PTYPE);
  const bad = $$(dom, ".upchecks .checks li.bad");
  assert.deepEqual(bad.map((li) => li.dataset.upCheck), ["TREATMENT_ARM_TOO_SMALL"]);
  assert.equal(bad[0].querySelector("span:last-child").textContent,
    "There are too few customers to measure what the campaign changed: the treated group has 1 customers " +
    "(at least 1,000 needed); only 0 customers in the treated group had a positive 'reactivated_90d' " +
    "(at least 50 needed); the control group has 1 customers (at least 1,000 needed); only 1 customers in " +
    "the control group had a positive 'reactivated_90d' (at least 50 needed).");
  assert.match($(dom, '[data-up-fix="TREATMENT_ARM_TOO_SMALL"]').textContent,
    /Use a longer period or a larger campaign, or hold out a bigger control group next time\.$/);
  // nothing the page could not count is claimed to pass
  assert.ok($(dom, '[data-up-check="TREATMENT_NOT_RANDOM"]').classList.contains("skip"));
  assert.match($(dom, '[data-up-check="TREATMENT_NOT_RANDOM"]').textContent, /fewer than 30 customers/);
  assert.equal($(dom, "#f-ack"), null, "only TREATMENT_NOT_RANDOM can be acknowledged");
  assert.equal($(dom, "#f-run").disabled, true);
  assert.match($(dom, ".reason").textContent, /TREATMENT_ARM_TOO_SMALL/);
  // a value that is not 0 or 1 is TREATMENT_NOT_BINARY instead, and the arm check does not run
  await upload(dom, "#f-file", "yes.csv",
    "customer_id,months_since_churn,treatment,reactivated_90d\nC1,2,yes,0\nC2,5,0,1\n");
  assert.equal($(dom, '[data-up-check="TREATMENT_NOT_BINARY"] span:last-child').textContent,
    "'treatment' should be 1 for treated customers and 0 for held-out customers, but 1 of 2 rows (50.0%) are blank or hold another value.");
  assert.ok($(dom, '[data-up-check="TREATMENT_ARM_TOO_SMALL"]').classList.contains("skip"));
  assert.equal($(dom, "#f-run").disabled, true);
});

test("the no-measurable-uplift example belongs to the page it was opened on", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  go(dom, `${UP}/model`);
  click(dom, "#up-null");
  assert.match($(dom, "#up-auuc h3").textContent, /example with no measurable uplift/);
  go(dom, `${UP}/data`);
  await settle(dom);
  go(dom, `${UP}/model`);
  await settle(dom);
  assert.equal($(dom, "#up-auuc h3").textContent, "AUUC", "back on Model, the real run again");
  assert.match($(dom, "[data-up-summary]").textContent, /^Targeting by predicted uplift beats random targeting/);
  assert.equal($(dom, "#up-null").textContent, "What a model with no measurable uplift looks like");
});

test("Campaign results: an uploaded outcomes file is not measured with the sample's numbers", async () => {
  const dom = winback();
  go(dom, `#/uc/${WB}/campaign`);
  await upload(dom, "#cr-file", "my_outcomes.csv", "customer_id,reactivated_90d\nC1,1\n");
  assert.equal($(dom, "#cr-file").closest("label").querySelector(".fname").textContent, "my_outcomes.csv");
  assert.deepEqual(Object.values(kpiMap(dom)), ["—", "—"]);
  assert.match($(dom, "[data-up-unread]").textContent, /my_outcomes\.csv is not read by the prototype, so nothing is computed from it\./);
  assert.equal($(dom, "[data-up-summary]"), null);
  assert.doesNotMatch(body(dom), /11,200|1,232|\+3\.48/);
  // the sample link is still the way to a measured campaign
  click(dom, "#cr-sample");
  assert.equal(kv(dom, "Absolute lift"), "+3.48 pts (95% CI 1.91 to 4.86)");
});

/* ---------- Review round 2 ---------- */

/** A win-back upload with a random-looking treatment: 1,200 rows per arm, 20% converted in each. */
function campaignCsv(rows) {
  const out = ["customer_id,months_since_churn,treatment,reactivated_90d"];
  for (let i = 0; i < rows; i++) out.push(`C${i},${i % 12},${i % 2},${i % 20 < 4 ? 1 : 0}`);
  return out.join("\n") + "\n";
}

test("Campaign results for a Phase 1 scoring run counts that run's customers, not the seed list's", async () => {
  const dom = winback();
  click(dom, "#f-sample");
  submit(dom);
  await settle(dom);
  click(dom, "#f-again");
  click(dom, '.seg button[data-mode="score"]');
  click(dom, "#f-sample");
  submit(dom);
  await settle(dom);
  assert.match($(dom, ".summary").textContent, /12,480 rows scored/);
  click(dom, "#f-campaign");
  go(dom, `#/uc/${WB}/campaign`);
  click(dom, "#cr-sample");
  // 12,480 rows: a 10% control group (1,248), the sample's suppressed share (1,592), the rest sent (9,640)
  const m = $(dom, "[data-up-summary]").textContent.match(/for any of the (\d+) treated and control customers\.$/);
  assert.ok(m, "the engine's IMMATURE sentence");
  assert.equal(Number(m[1]), 10888);
  assert.ok(Number(m[1]) <= 12480, "never more customers than the run scored");
  const rep = JSON.parse(ev(dom, `JSON.stringify(campaignReport(STATE['${WB}'].current,{},today()))`));
  assert.equal(rep.rows_immature, 9640 + 1248);
  assert.equal(rep.rows_suppressed_or_untreated, 1592);
  // the seeded 1 May run is 14,500 rows and keeps its own counts
  set(dom, "#cr-run", "r0501");
  set(dom, "#cr-window", "180");
  assert.match($(dom, "[data-up-summary]").textContent, /for any of the 12650 treated and control customers\.$/);
  // a row count the page only estimated is not counted
  assert.equal(ev(dom, "campaignArms({rows:'~120K',mode:'score',ptype:''})"), null);
});

test("the plain sample after the campaign file with treatment None is Phase 1 again, runnable", async () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  set(dom, "#f-treat", "");
  assert.equal($(dom, ".reason").textContent, "Choose the treatment column");
  // DEC-608: the plain sample is the use case's own Setup, whose form the uplift screen never touches
  assert.equal($(dom, "#f-sample"), null, "no plain sample on the uplift screen");
  go(dom, `#/uc/${WB}`);
  await settle(dom);
  assert.equal($(dom, ".fname").textContent, "Upload CSV or Parquet", "the Phase 1 form is its own");
  click(dom, "#f-sample");
  assert.equal($(dom, ".ptype .pill").textContent, "Classification (yes / no)");
  assert.doesNotMatch($(dom, ".ptype").textContent, /set manually/);
  assert.equal($(dom, "#f-treat"), null);
  assert.equal($(dom, ".reason").textContent, "");
  assert.equal($(dom, "#f-run").disabled, false);
  assert.ok($(dom, "#f-model"), "the Phase 1 model step");
});

test("an uploaded file's uplift run reports the checks it got, not the sample's randomness AUC", async () => {
  const dom = upliftScreen();
  await upload(dom, "#f-file", "offers.csv", campaignCsv(2400));
  assert.equal($(dom, ".ptype .pill").textContent, PTYPE);
  assert.equal($(dom, "#f-run").disabled, false);
  const step = () => ev(dom, `runSteps(upView(UC.find(u=>u.id==='${WB}')))[0][1]`);
  assert.equal(step(), "3 of 6 checks passed · not run in the prototype: TREATMENT_NOT_RANDOM, " +
    "OUTCOME_WINDOW_IMMATURE, FEATURE_AFTER_TREATMENT");
  assert.doesNotMatch(step(), /AUC|^6 checks passed/);
  // a file too big to read in the page: no check is claimed at all
  await upload(dom, "#f-file", "big.csv", campaignCsv(25000));
  assert.match(ev(dom, `UPSTATE['${WB}'].file.rows`), /^~/);
  assert.equal(ev(dom, `upliftChecks(upView(UC.find(u=>u.id==='${WB}')))`), null);
  assert.equal(step(), "The 6 checks run on the whole file in the engine; the prototype read only its first 256 KB, so none is shown");
  // the sample campaign files keep their measured AUC
  click(dom, "#f-samplecampaign");
  assert.equal(step(), "6 checks passed · random assignment: AUC 0.52 ≤ 0.60");
});

test("the hold-out share in stage 5 sizes the hold-out Model and Output report", async () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  set(dom, '[data-up-num="upTest"]', "50");
  assert.match($$(dom, ".stage-d .ss")[4].textContent, /50% hold-out/);
  submit(dom);
  await settle(dom);
  const run = JSON.parse(ev(dom, `JSON.stringify(UPSTATE['${WB}'].current)`));
  assert.equal(run.upTest, 50);
  assert.equal(run.score, "AUUC 0.0125", "the rates are the sample's; only the hold-out grows");
  go(dom, `${UP}/model`);
  // 320K × 0.50
  assert.equal(kv(dom, "Hold-out"), "160,000 rows · 144,000 treated · 16,000 control");
  assert.match($(dom, ".qini").closest("section").querySelector(".caption").textContent, /Measured on the 160,000-customer hold-out/);
  assert.deepEqual($$(dom, ".dbars ~ .tbl-wrap tbody tr").map((r) => r.children[1].textContent), Array(10).fill("16,000"));
  // a bigger hold-out, a narrower interval than the 30% one (0.0098 to 0.0151)
  const [lo, hi] = $(dom, "#up-auuc .civ small").textContent.match(/-?\d\.\d{4}/g).map(Number);
  assert.ok(lo > 0.0098 && hi < 0.0151, `interval ${lo} to ${hi}`);
  go(dom, `${UP}/output`);
  assert.match($(dom, "[data-up-computed]").textContent, /Computed on the 160,000-customer hold-out of the training run/);
  assert.equal(kpiMap(dom).Persuadables, "96,000");
  assert.doesNotMatch(body(dom), /96,000-customer/);
  // the run keeps its hold-out: a later change to the setting does not rewrite it
  go(dom, UP);
  ev(dom, `UPSTATE['${WB}'].adv.upTest=20`);
  go(dom, `${UP}/model`);
  assert.equal(kv(dom, "Hold-out"), "160,000 rows · 144,000 treated · 16,000 control");
});

test("an emptied uplift setting the config cannot leave empty goes back to its default", async () => {
  const dom = upliftScreen();
  click(dom, "#f-samplecampaign");
  for (const k of ["upBoot", "upTest", "upPers", "upSleep"]) set(dom, `[data-up-num="${k}"]`, "");
  const val = (k) => $(dom, `[data-up-num="${k}"]`).value;
  assert.deepEqual(["upBoot", "upTest", "upPers", "upSleep"].map(val), ["200", "30", "0.02", "-0.01"]);
  assert.equal($(dom, '[data-up-num="upBoot"]').placeholder, "200", "the placeholder is the default, not —");
  assert.equal($(dom, '[data-up-num="upBudget"]').placeholder, "—", "a nullable setting may be empty");
  const summ = $$(dom, ".stage-d .ss").map((e) => e.textContent);
  assert.match(summ[4], /AUUC · 200 bootstrap resamples · 30% hold-out/);
  assert.match(summ[5], /^Persuadable ≥ 0\.02 · sleeping dog ≤ -0\.01 ·/);
  assert.doesNotMatch(summ.join(" "), /null/);
  // outside the config's range is brought inside it
  set(dom, '[data-up-num="upTest"]', "90");
  assert.equal(val("upTest"), "50");
  set(dom, '[data-up-num="upBoot"]', "2");
  assert.equal(val("upBoot"), "10");
  set(dom, '[data-up-num="upTest"]', "");
  set(dom, '[data-up-num="upBoot"]', "");
  assert.equal($$(dom, ".progress .pt").length, 0);
  submit(dom);
  assert.equal($$(dom, ".progress .pt")[3].textContent, "Measuring uplift on the hold-out (200 resamples)");
  await settle(dom);
  go(dom, `${UP}/model`);
  assert.equal(kv(dom, "Bootstrap resamples"), "200");
  go(dom, `${UP}/output`);
  assert.match($(dom, "[data-up-computed]").textContent, /Persuadable at predicted uplift ≥ 0\.02;/);
  assert.equal(kpiMap(dom).Persuadables, "57,600");
});

test("an uploaded file's checks panel lists all six checks, TREATMENT_NOT_BINARY passing", async () => {
  const dom = upliftScreen();
  await upload(dom, "#f-file", "tiny.csv",
    "customer_id,months_since_churn,treatment,reactivated_90d\nC1,2,1,0\nC2,5,0,1\n");
  const checks = $$(dom, ".upchecks .checks li");
  assert.deepEqual(checks.map((li) => li.dataset.upCheck), ["TREATMENT_COLUMN_MISSING", "TREATMENT_NOT_BINARY",
    "TREATMENT_ARM_TOO_SMALL", "TREATMENT_NOT_RANDOM", "OUTCOME_WINDOW_IMMATURE", "FEATURE_AFTER_TREATMENT"]);
  const nb = $(dom, '[data-up-check="TREATMENT_NOT_BINARY"]');
  assert.equal(nb.className, "", "a pass, neither bad nor skipped");
  assert.equal(nb.querySelector("span:last-child").textContent, "Every treatment value is 0 or 1.");
});

test("Campaign results spells the month as its other dates do ('Sep', never 'Sept')", async () => {
  await inTimeZone("UTC", async () => {
    const dom = upliftScreen();
    await trainUplift(dom);
    await scoreUplift(dom);
    go(dom, `${UP}/campaign`);
    // the run select dates the run as the send line does, from the page's one month table, not the run stamp
    assert.equal($(dom, "#cr-run").selectedOptions[0].textContent, "Scored 14,500 rows · 23 Sep 2026, 10:00 am");
    assert.match($(dom, ".cw2").textContent, /^Sent 23 Sep 2026\./);
    assert.equal([...$(dom, "#cr-run").options].at(-1).textContent, "Scored 14,500 rows · 1 May 2026");
    assert.doesNotMatch(body(dom), /Sept/);
  });
});

/* ---------- Review round 3 ---------- */

/** A scoring file of `rows` customers, no outcome and no treatment. */
function weekCsv(rows) {
  const out = ["customer_id,snapshot_date,months_since_churn"];
  for (let i = 0; i < rows; i++) out.push(`W${i},2026-09-21,${i % 12}`);
  return out.join("\n") + "\n";
}
async function scoreUpload(dom, name, csv) {
  click(dom, "#f-again");
  click(dom, '.seg button[data-mode="score"]');
  await upload(dom, "#f-file", name, csv);
  assert.equal($(dom, "#f-run").disabled, false, "the scoring run can start");
  submit(dom);
  await settle(dom);
  assert.ok($(dom, ".results"), "the scoring run reaches its results");
}

test("an uplift scoring run on an uploaded file counts that file's customers, not the sample list's", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  await scoreUpload(dom, "week.csv", weekCsv(3));
  const summary = $(dom, ".summary").textContent;
  assert.match(summary, /3 rows scored/);
  const n = Number(summary.match(/Recommended to contact: ([\d,]+)/)[1].replace(/,/g, ""));
  assert.ok(n <= 3, `never more contacts than the file has customers (${n})`);
  const pol = JSON.parse(ev(dom, `JSON.stringify(upliftPolicy(UPSTATE['${WB}'].current))`));
  assert.equal(pol.rows, 3);
  assert.equal(Object.values(pol.segs).reduce((a, b) => a + b, 0), 3, "the four segments add up to the file");
  assert.equal(pol.n, n);
  const arms = JSON.parse(ev(dom, `JSON.stringify(campaignArms(UPSTATE['${WB}'].current))`));
  assert.equal(arms.eligible, 3);
  assert.ok(arms.treated + arms.control <= 3, JSON.stringify(arms));
  const rep = JSON.parse(ev(dom, `JSON.stringify(campaignReport(UPSTATE['${WB}'].current,{},today()))`));
  assert.ok(rep.rows_suppressed_or_untreated >= 0, `rows outside ${rep.rows_suppressed_or_untreated}`);
  go(dom, `${UP}/output`);
  assert.match($(dom, "[data-up-computed]").textContent, /Computed on every one of the 3 scored customers\./);
  assert.equal(Number(kpiMap(dom)["Recommended to contact"]), n);
  // every count on the uplift output is within the file (the budget setting, 4,000, is a setting, not a count)
  const counts = () => [...Object.values(kpiMap(dom)), $(dom, "[data-up-n]").textContent, kv(dom, "Eligible persuadables"),
    ...$$(dom, "[data-up-seg] .sc2").map((e) => e.textContent)];
  assert.ok(counts().every((v) => v === "—" || Number(v.replace(/,/g, "")) <= 3), counts().join(" | "));
  go(dom, `${UP}/campaign`);
  click(dom, "#cr-sample");
  assert.doesNotMatch(body(dom), /4518|4,518/);
  // a list the page could not count: no count is claimed anywhere
  go(dom, UP);
  await scoreUpload(dom, "big_week.csv", weekCsv(20000));
  const run = JSON.parse(ev(dom, `JSON.stringify(UPSTATE['${WB}'].current)`));
  assert.match(run.rows, /^~/);
  assert.match($(dom, ".summary").textContent, /Recommended to contact: —/);
  go(dom, `${UP}/output`);
  assert.equal(kpiMap(dom)["Recommended to contact"], "—");
  assert.equal(kpiMap(dom).Persuadables, "—");
  assert.deepEqual(counts().filter((v) => v !== "—"), [], "no count at all");
  assert.match($(dom, "[data-up-computed]").textContent, /could not count/);
  assert.equal($(dom, "#up-dl").disabled, true, "no list to download from rows the page never counted");
});

test("an uplift run trained on an uploaded file reports that file's hold-out, not the sample's 96,000", async () => {
  const dom = upliftScreen();
  await upload(dom, "#f-file", "offers.csv", campaignCsv(2400));
  assert.equal($(dom, "#f-run").disabled, false);
  submit(dom);
  await settle(dom);
  go(dom, `${UP}/model`);
  // 2,400 rows × 30%
  assert.match(kv(dom, "Hold-out"), /^720 rows/);
  assert.match($(dom, ".qini").closest("section").querySelector(".caption").textContent, /Measured on the 720-customer hold-out/);
  assert.doesNotMatch(body(dom), /96,000|86,400|9,600/);
  go(dom, `${UP}/output`);
  assert.match($(dom, "[data-up-computed]").textContent, /Computed on the 720-customer hold-out of the training run/);
  const k = kpiMap(dom);
  assert.ok(Number(k.Persuadables.replace(/,/g, "")) <= 720, k.Persuadables);
  assert.ok(Number(k["Recommended to contact"].replace(/,/g, "")) <= 720, k["Recommended to contact"]);
  assert.ok(Number(kv(dom, "Eligible persuadables").replace(/,/g, "")) <= 720);
  assert.doesNotMatch(body(dom), /96,000|57,600/);
  // at a 50% hold-out the same file holds out 1,200, not 160,000
  go(dom, UP);
  click(dom, "#f-again");
  set(dom, '[data-up-num="upTest"]', "50");
  submit(dom);
  await settle(dom);
  go(dom, `${UP}/model`);
  assert.match(kv(dom, "Hold-out"), /^1,200 rows/);
  assert.doesNotMatch(body(dom), /160,000/);
});

test("approving a template keeps the approval stamp's form: the seeded cards' flow is unchanged", () => {
  const dom = load(`#/uc/${WB}/output`);
  set(dom, "#cc-approver", "R. Rao");
  const idx = $$(dom, ".cccard").findIndex((c) => c.querySelector(".pill").textContent === "Pending review");
  $$(dom, ".cccard")[idx].querySelector("[data-cc-ok]").click();
  const line = [...$$(dom, ".cccard")[idx].querySelectorAll(".ccm")].find((e) => /^Approved by/.test(e.textContent));
  const stamp = line.textContent.replace(/^Approved by R\. Rao · /, "");
  // the same en-IN stamp as before the uplift screens (8f0d358), not a new 24-hour form
  const was = ev(dom, `new Date().toLocaleString("en-IN",{day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"}).replace(/(\\d{4}),?\\s*/,"$1, ")`);
  assert.match(stamp, /\d{1,2}:\d{2}\s?(am|pm)$/i, stamp);
  assert.equal(stamp.replace(/\d{2}:\d{2}/, ""), was.replace(/\d{2}:\d{2}/, ""));
  assert.equal(ev(dom, "nowStamp()").replace(/\d{2}:\d{2}/, ""), was.replace(/\d{2}:\d{2}/, ""));
});

test("using the sample dataset again keeps a problem type set by hand, on every Phase 1 use case", () => {
  const dom = load("#/uc/payment-propensity");
  click(dom, "#f-sample");
  set(dom, "#f-ptype", "Regression (a number)");
  assert.match($(dom, ".ptype").textContent, /Regression \(a number\)\s*set manually/);
  click(dom, "#f-sample");
  assert.match($(dom, ".ptype").textContent, /Regression \(a number\)\s*set manually/);
  go(dom, `#/uc/${WB}`);
  click(dom, "#f-sample");
  set(dom, "#f-ptype", "Regression (a number)");
  click(dom, "#f-sample");
  assert.match($(dom, ".ptype").textContent, /Regression \(a number\)\s*set manually/);
});

test("Campaign results for a run with a 0% control group says the effect cannot be measured, in the engine's words", async () => {
  const dom = winback();
  click(dom, "#f-sample");
  set(dom, '[data-adv="control"]', "0");
  submit(dom);
  await settle(dom);
  click(dom, "#f-again");
  click(dom, '.seg button[data-mode="score"]');
  click(dom, "#f-sample");
  submit(dom);
  await settle(dom);
  assert.equal(ev(dom, `STATE['${WB}'].current.adv.control`), 0);
  go(dom, `#/uc/${WB}/campaign`);
  click(dom, "#cr-sample");
  const noControl = ev(dom, "INC_TEXT.noControl");
  assert.equal($(dom, "[data-up-summary]").textContent, noControl);
  assert.doesNotMatch(body(dom), /treated and control customers/);
  const rep = JSON.parse(ev(dom, `JSON.stringify(campaignReport(STATE['${WB}'].current,{},today()))`));
  assert.equal(rep.causal, false, "IncrementalityReport.causal is has_control_group");
  assert.equal(rep.summary, noControl);
  assert.equal(rep.control_rows, 0);
  assert.doesNotMatch(body(dom), /every scoring run keeps one/);
  assert.doesNotMatch(body(dom), /The lift below is still causal/);
  // the seeded 1 May run kept its 10% control group and is measured as before
  set(dom, "#cr-run", "r0501");
  assert.equal(kv(dom, "Absolute lift"), "+3.48 pts (95% CI 1.91 to 4.86)");
});

/* ---------- Review round 4 ---------- */

/** A Phase 1 win-back model trained on the plain sample with stage 6's control share at `control`%. */
async function trainPhase1(dom, control) {
  click(dom, "#f-sample");
  set(dom, '[data-adv="control"]', String(control));
  submit(dom);
  await settle(dom);
  assert.ok($(dom, ".results"), "the Phase 1 run reaches its results");
}

test("a 0% control group on a list the page only estimated is still not measurable, in the engine's words", async () => {
  const dom = winback();
  await trainPhase1(dom, 0);
  await scoreUpload(dom, "big_week.csv", weekCsv(20000));
  const run = JSON.parse(ev(dom, `JSON.stringify(STATE['${WB}'].current)`));
  assert.match(run.rows, /^~20K$/, "the row count is an estimate");
  assert.equal(run.adv.control, 0);
  assert.equal(ev(dom, `campaignArms(STATE['${WB}'].current)`), null, "no arms from rows the page did not count");
  const rep = JSON.parse(ev(dom, `JSON.stringify(campaignReport(STATE['${WB}'].current,{},today()))`));
  const noControl = ev(dom, "INC_TEXT.noControl");
  assert.equal(rep.causal, false, "IncrementalityReport.causal is has_control_group");
  assert.equal(rep.summary, noControl);
  assert.equal(rep.rows_immature, null, "no count of customers the page did not count");
  assert.equal(rep.rows_suppressed_or_untreated, null);
  go(dom, `#/uc/${WB}/campaign`);
  click(dom, "#cr-sample");
  assert.equal($(dom, "[data-up-summary]").textContent, noControl);
  assert.equal($(dom, "[data-up-wait]"), null, "no 'Results available on' for a run that cannot be measured");
  assert.doesNotMatch(body(dom), /treated and control customers|held out at random/);
  // the same estimated list at 10% keeps a control group and waits for its window, as before
  go(dom, `#/uc/${WB}`);
  click(dom, "#f-again");
  click(dom, '.seg button[data-mode="train"]');
  await trainPhase1(dom, 10);
  await scoreUpload(dom, "big_week.csv", weekCsv(20000));
  const rep10 = JSON.parse(ev(dom, `JSON.stringify(campaignReport(STATE['${WB}'].current,{},today()))`));
  assert.equal(rep10.causal, true);
  assert.equal(rep10.status, "immature");
});

test("the run picker's hint mentions a control group only for a run that held one out", async () => {
  const dom = winback();
  await trainPhase1(dom, 0);
  click(dom, "#f-again");
  click(dom, '.seg button[data-mode="score"]');
  click(dom, "#f-sample");
  submit(dom);
  await settle(dom);
  go(dom, `#/uc/${WB}/campaign`);
  click(dom, "#cr-sample");
  const hint = () => $(dom, "#cr-run").closest(".field").querySelector(".uphint").textContent;
  assert.ok($(dom, "[data-up-nocontrol]"));
  assert.doesNotMatch(hint(), /control group/);
  assert.ok(hint().endsWith("."), "still a one-line hint");
  // the seeded 1 May run held its control group out at random, and says so
  set(dom, "#cr-run", "r0501");
  assert.equal(hint(), "Its treated customers are compared with the control group it held out at random.");
});

test("uplift restores the engine's fixed control share, and its no-control advice is one the user can follow", async () => {
  const yaml = repoText("configs/engine.yaml");
  const pct = Math.round(Number(yaml.match(/control_group_fraction:\s*([\d.]+)/)[1]) * 100);
  const dom = winback();
  click(dom, "#f-sample");
  set(dom, '[data-adv="control"]', "0");
  // the uplift screen, from the link under the header: a 0% share set on Phase 1's stage 6 does not reach it
  go(dom, UP);
  await settle(dom);
  click(dom, "#f-samplecampaign");
  assert.equal($(dom, "[data-up-control]").textContent, `${pct}% · fixed`);
  assert.match($$(dom, ".stage-d .ss")[5].textContent, new RegExp(` · ${pct}% control group$`));
  submit(dom);
  await settle(dom);
  assert.equal(ev(dom, `UPSTATE['${WB}'].current.adv.control`), pct, "the uplift run holds out the engine's share");
  // back on Phase 1 training, the share the user set there is theirs again
  go(dom, `#/uc/${WB}`);
  await settle(dom);
  assert.equal($(dom, '[data-adv="control"]').value, "0");
  click(dom, "#f-sample");
  assert.equal($(dom, '[data-adv="control"]').value, "0");
  // an uplift list too small for its fixed share: the advice is to score a larger list, not a setting uplift locks
  go(dom, UP);
  await settle(dom);
  click(dom, "#f-again");
  click(dom, "#f-samplecampaign");
  submit(dom);
  await settle(dom);
  await scoreUpload(dom, "week.csv", weekCsv(3));
  assert.equal(ev(dom, `UPSTATE['${WB}'].current.adv.control`), pct);
  go(dom, `${UP}/campaign`);
  click(dom, "#cr-sample");
  const box = $(dom, "[data-up-nocontrol]");
  assert.ok(box, "three customers at 10% hold out no one");
  assert.doesNotMatch(box.textContent, /Advanced|Control group holdout/);
  assert.equal(box.querySelector(".gb2").textContent,
    `On a list of 3 customers, a ${pct}% control group rounds to no one. Score a larger list to measure the next campaign.`);
});

test("an uploaded file's uplift Output labels the sample's rows and segment means, and shows no mean as the file's", async () => {
  const dom = upliftScreen();
  await trainUplift(dom);
  await scoreUplift(dom);
  go(dom, `${UP}/output`);
  // the sample list: unchanged
  const rowsCaption = () => $$(dom, ".caption").find((c) => /scores\.csv/.test(c.textContent)).textContent;
  assert.match(rowsCaption(), /^Sample rows of scores\.csv\./);
  assert.ok($$(dom, "[data-up-seg] small").every((e) => /mean predicted uplift [+−-]?\d/.test(e.textContent)));
  assert.equal($(dom, "[data-up-sample]"), null);
  // a scored upload
  go(dom, UP);
  await scoreUpload(dom, "week.csv", weekCsv(3));
  go(dom, `${UP}/output`);
  assert.ok(body(dom).includes("C-90112"), "the illustrative rows are still shown");
  assert.match(rowsCaption(), /^Illustrative sample rows, not rows of week\.csv: the prototype does not score the file/);
  assert.doesNotMatch(body(dom), /Sample rows of scores\.csv/);
  assert.ok($$(dom, "[data-up-seg] small").every((e) => e.textContent === "mean predicted uplift —"));
  assert.match($(dom, "[data-up-sample]").textContent, /does not score week\.csv: the segment shares .* are the sample's/);
  // a training upload
  const dom2 = upliftScreen();
  await upload(dom2, "#f-file", "offers.csv", campaignCsv(2400));
  submit(dom2);
  await settle(dom2);
  go(dom2, `${UP}/output`);
  const cap2 = $$(dom2, ".caption").find((c) => /scores\.csv/.test(c.textContent)).textContent;
  assert.match(cap2, /^Illustrative sample rows, not rows of offers\.csv: the prototype does not train on the file/);
  assert.ok($$(dom2, "[data-up-seg] small").every((e) => e.textContent === "mean predicted uplift —"));
  assert.match($(dom2, "[data-up-sample]").textContent, /does not train on offers\.csv/);
});

test("Campaign results and Results date a run on the same day in Asia/Kolkata (IST, UTC+5:30)", async () => {
  await inTimeZone("Asia/Kolkata", async () => {
    const dom = load(UP);
    // 20:00 UTC on 23 Sep is 01:30 on 24 Sep in Kolkata
    ev(dom, "AS_OF='2026-09-23T20:00:00Z'");
    await trainUplift(dom);
    await scoreUplift(dom);
    const stamp = ev(dom, `UPSTATE['${WB}'].current.at`);
    // the Results stamp keeps its 8f0d358 form (en-IN), on the page's one clock
    assert.equal(stamp, "24 Sept 2026, 01:30 am");
    assert.ok($$(dom, ".runrow .r2, .muted").some((e) => e.textContent.endsWith(stamp)), "Results shows the stamp");
    go(dom, `${UP}/campaign`);
    assert.equal($(dom, "#cr-run").selectedOptions[0].textContent, "Scored 14,500 rows · 24 Sep 2026, 01:30 am");
    assert.match($(dom, ".cw2").textContent, /^Sent 24 Sep 2026\./);
    assert.doesNotMatch(body(dom), /23 Sep 2026|UTC/);
  });
});

/* ---------- DEC-608: uplift is not a Phase 1 Setup choice ---------- */

test("DEC-608: win-back's Setup links to its uplift screen, the product's entry point, in the product's words", async () => {
  const dom = winback();
  // v1 (the audit's navigation section): a quiet related link in the use case's own header actions,
  // never a pill injected under the rule
  const entry = () => $(dom, ".head .head-actions a.related");
  assert.equal($(dom, ".uentry"), null);
  assert.equal(entry().textContent, "Also: target with uplift ›");
  assert.equal(entry().getAttribute("title"), "Uplift predicts who changes behaviour because of your action");
  assert.equal(entry().getAttribute("href"), UP);
  // on every state of the use case's screen: Setup, Running and Results
  click(dom, "#f-sample");
  submit(dom);
  assert.ok($(dom, ".progress") && entry(), "Running keeps the link");
  await settle(dom);
  assert.ok($(dom, ".results") && entry(), "Results keeps the link");
  // no other use case has uplift sample content
  for (const id of ["targeted-advertisement", "ai-onboarding-assistant", "order-fulfillment", "fault-prediction", "payment-propensity", "rca"]) {
    go(dom, `#/uc/${id}`);
    assert.equal(entry(), null, `${id} has no uplift link`);
  }
  // the reviewer's ruling on DEC-666 (DEC-952): the Data / Model / Output pages carry the use-case link,
  // since v1 in their header actions too (screenshots 14 and 17)
  for (const page of ["data", "model", "output", "campaign"]) {
    go(dom, `#/uc/${WB}/${page}`);
    assert.equal(entry().getAttribute("href"), UP, `${page} links to the uplift screen`);
  }
  // Home carries no uplift pill: "Uplift models" is an entry of the top bar's Models menu (screenshot 25)
  go(dom, "#/");
  assert.equal($(dom, ".uentry"), null);
  assert.equal($(dom, '#pb-bar #tn-models a[href="#/uplift"]').textContent, "Uplift models");
  // the uplift screen: the product's header, the breadcrumb as the way back, and its own Setup
  go(dom, UP);
  assert.equal(dom.window.document.title, "Win-back Campaign · Uplift · Marketing AI");
  assert.equal($(dom, "h1").textContent, "Win-back Campaign · Uplift");
  assert.deepEqual($$(dom, ".crumbs a, .crumbs .cur").map((e) => e.textContent), ["Home", "Customer Lifecycle", "Win-back Campaign", "Uplift"]);
  assert.equal($(dom, ".crumbs a:last-of-type").getAttribute("href"), `#/uc/${WB}`);
  assert.equal($(dom, ".desc").textContent, "Uplift predicts who changes behaviour because of your action.");
  assert.deepEqual($$(dom, ".chips .chip").map((c) => c.textContent).slice(-1), ["Uplift"]);
  assert.deepEqual($$(dom, ".seg button").map((b) => b.textContent), ["Train uplift model", "Score new data"]);
  assert.equal($(dom, "#f-run").textContent, "Train uplift model");
  assert.equal($(dom, ".pick"), null, "one file of a past campaign; the raw-table builder is Phase 1's");
  assert.equal($(dom, "main").dataset.module, "uplift");
  // #/uplift, the product's index: every use case, win-back the one with a link
  go(dom, "#/uplift");
  assert.equal($(dom, "h1").textContent, "Uplift modelling");
  assert.deepEqual($$(dom, ".uindex > *").map((e) => e.firstElementChild.textContent), JSON.parse(ev(dom, "JSON.stringify(UC.map(u=>u.name))")));
  assert.deepEqual($$(dom, ".uindex a").map((a) => a.getAttribute("href")), [UP]);
});

test("DEC-608: the uplift screen has its own form and shares the use case's runs", async () => {
  const dom = upliftScreen();
  // before any uplift run: nothing to score with, and no uplift page shows Phase 1's figures
  click(dom, '.seg button[data-mode="score"]');
  click(dom, "#f-sample");
  assert.equal($(dom, ".reason").textContent, "Train an uplift model first");
  assert.deepEqual($$(dom, "#f-scorerun option").map((o) => o.textContent), ["No trained uplift model yet"]);
  assert.equal($(dom, "#f-run").textContent, "Score customers");
  assert.equal($(dom, ".runs-list .empty").textContent, "No uplift runs yet.");
  for (const page of ["model", "output"]) {
    go(dom, `${UP}/${page}`);
    assert.ok($(dom, "[data-up-empty]"), `${page} before an uplift run`);
    assert.equal($(dom, ".vchart"), null);
    assert.deepEqual($$(dom, ".crumbs a, .crumbs .cur").map((e) => e.textContent), ["Home", "Customer Lifecycle", "Win-back Campaign", "Uplift", page === "model" ? "Model" : "Output"]);
  }
  go(dom, UP);
  click(dom, '.seg button[data-mode="train"]');
  await trainUplift(dom);
  // the pipeline's blocks and Campaign results stay on the uplift screens
  assert.deepEqual($$(dom, ".flow .block").map((b) => b.getAttribute("href")), ["data", "model", "output"].map((p) => `${UP}/${p}`));
  assert.equal($(dom, ".cap").textContent, "Uplift pipeline");
  await scoreUplift(dom);
  assert.equal($(dom, "#f-campaign").getAttribute("href"), `${UP}/campaign`);
  go(dom, `${UP}/output`);
  assert.deepEqual($$(dom, ".tabs .tab").map((t) => t.getAttribute("href")), ["data", "model", "output", "campaign"].map((p) => `${UP}/${p}`));
  // one registry: the use case's own Setup lists the uplift runs, but never offers an uplift model to
  // Phase 1 scoring, and its form, its current run and its pages are untouched
  go(dom, `#/uc/${WB}`);
  await settle(dom);
  assert.ok($$(dom, ".runrow .r1").some((e) => /^X-learner · AutoGluon fast/.test(e.textContent)), "the uplift run is in the use case's runs");
  assert.ok(!$$(dom, ".runrow .r1").some((e) => /Uplift champion/.test(e.textContent)), "as a candidate, not a champion");
  assert.equal(ev(dom, `STATE['${WB}'].current`), null);
  assert.equal(ev(dom, `STATE['${WB}'].file`), null);
  click(dom, '.seg button[data-mode="score"]');
  assert.deepEqual($$(dom, "#f-scorerun option").map((o) => o.textContent), ["LightGBM + Claude copy · ROC-AUC 0.78 · 14 Sep 2026, 10:20 · Champion"]);
  for (const page of ["model", "output"]) {
    go(dom, `#/uc/${WB}/${page}`);
    assert.equal($(dom, ".qini"), null);
    assert.equal($(dom, ".segrow"), null);
    assert.equal($$(dom, ".tabs .tab").length, 3, "no uplift run is current on Phase 1's pages");
  }
  // and the uplift screen offers only uplift models to score with
  go(dom, UP);
  click(dom, "#f-again");
  assert.ok($$(dom, "#f-scorerun option").every((o) => /X-learner/.test(o.textContent)));
});
