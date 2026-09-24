/* The uplift screens (ui/modules/uplift/), rendered in node from artefact fixtures.
   `views.js`, `charts.js` and `format.js` are pure - no `window`, no fetch - so each screen is a
   string here exactly as it is in the browser. Run by tests/unit/uplift/test_uplift_ui.py, or
   directly: `node --test tests/unit/uplift/uplift_ui.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const UI = new URL("../../../ui/modules/uplift/", import.meta.url);
const views = await import(new URL("views.js", UI));
const charts = await import(new URL("charts.js", UI));
const format = await import(new URL("format.js", UI));

const EM = "—";
const uc = {
  id: "win-back",
  name: "Win-back <campaign>",
  marker: "P",
  stars: "★",
  type_label: "Predictive",
  lifecycle_stage: "Retention",
  ai_type: "predictive",
};
const trainRun = {
  run_id: "r-train",
  mode: "train",
  state: "done",
  created_at: "2026-09-01T10:00:00Z",
  file_name: "campaign.csv",
  problem_type: "uplift",
};
const scoreRun = { ...trainRun, run_id: "r-score", mode: "score" };

const cv = (value, lo, hi) => ({ value, ci_low: lo, ci_high: hi, confidence_level: 0.95 });
const deciles = Array.from({ length: 10 }, (_, i) => ({
  decile: i + 1,
  rows: 100,
  treated_rows: 50,
  control_rows: 50,
  treated_rate: 0.3 - i * 0.02,
  control_rate: 0.2,
  observed_uplift: i === 9 ? null : 0.1 - i * 0.02,
  predicted_uplift: 0.09 - i * 0.02,
}));
const evaluation = {
  run_id: "r-train",
  learner: "x_learner",
  base_model: "lightgbm",
  rows_evaluated: 1000,
  treated_rows: 500,
  control_rows: 500,
  treated_rate: 0.21,
  control_rate: 0.18,
  average_treatment_effect: cv(0.03, 0.01, 0.05),
  auuc: cv(0.0123, 0.0041, 0.0205),
  qini_coefficient: cv(0.0087, 0.002, 0.015),
  uplift_at: [0.1, 0.2, 0.3].map((f) => ({ fraction: f, uplift: cv(0.08, 0.02, 0.14) })),
  deciles,
  bootstrap_samples: 200,
  measurable_uplift: true,
  causal: true,
  summary: "Targeting by predicted uplift beats random targeting.",
  evaluated_at: "2026-09-01T10:05:00Z",
};
const curve = {
  run_id: "r-train",
  rows_evaluated: 1000,
  causal: true,
  points: Array.from({ length: 11 }, (_, i) => ({
    fraction: i / 10,
    qini: 0.03 * Math.sqrt(i / 10),
    random: 0.03 * (i / 10),
    uplift_curve: 0.02 * (i / 10),
  })),
};
const segments = {
  run_id: "r-score",
  computed_on: "scored",
  rows: 1000,
  thresholds: {
    persuadable_min_uplift: 0.02,
    sleeping_dog_max_uplift: -0.01,
    sure_thing_min_probability: 0.2,
    sure_thing_from_base_rate: true,
  },
  segments: [
    ["persuadable", "Persuadables", 300, "Treat"],
    ["sure_thing", "Sure things", 250, "Don't treat (converts anyway)"],
    ["lost_cause", "Lost causes", 350, "Don't treat (won't convert)"],
    ["sleeping_dog", "Sleeping dogs", 100, "Never treat (contact makes it worse)"],
  ].map(([segment, label, rows, action]) => ({
    segment,
    label,
    rows,
    share_pct: rows / 10,
    mean_predicted_uplift: segment === "sleeping_dog" ? -0.04 : 0.05,
    mean_p_treated: 0.3,
    mean_p_control: 0.25,
    action,
  })),
  causal: true,
};
const policy = {
  run_id: "r-score",
  computed_on: "scored",
  rows: 1000,
  eligible_persuadables: 270,
  contacts_recommended: 150,
  stop_reason: "budget",
  budget_contacts: 150,
  predicted_incremental_conversions: 11.2,
  expected_incremental_conversions: cv(9.5, 3.1, 15.9),
  cost_per_contact: null,
  value_per_conversion: null,
  expected_cost: null,
  expected_value: null,
  expected_net_value: null,
  causal: true,
};

/** Text of an HTML string with the tags removed, for "what does the reader see" assertions. */
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
const noJunk = (html) => {
  for (const bad of ["undefined", "NaN", "null", "[object Object]", "Infinity"]) {
    assert.ok(!text(html).includes(bad), `rendered text contains ${bad}`);
  }
};

test("formats: CI, points, dates, p-values, and the em dash for anything missing", () => {
  // Metrics carry at most three decimals (docs/UI_AUDIT.md §3 NUMBERS).
  assert.equal(format.fmtCi(cv(0.0123, 0.0041, 0.0205)), "0.012 (95% CI 0.004 to 0.021)");
  assert.equal(format.fmtCi(cv(0.5, null, null)), `0.5 (95% CI ${EM})`);
  assert.equal(format.fmtCi(null), EM);
  assert.equal(format.fmtPts(0.023), "+2.3 pts");
  assert.equal(format.fmtPts(-0.004), "−0.4 pts");
  assert.equal(format.fmtPts(null), EM);
  assert.equal(format.fmtDay("2026-10-01"), "01 Oct 2026");
  assert.equal(format.fmtDay(null), EM);
  assert.equal(format.fmtP(0.0001), "< 0.001");
  assert.equal(format.fmtP(0.0421), "0.042");
  // People are whole and grouped; rates carry one fixed decimal.
  assert.equal(format.fmtCount(280.857), "281");
  assert.equal(format.fmtCount(-1204.4), "−1,204");
  assert.equal(format.fmtRate(0.39), "39.0%");
  assert.equal(format.fmtLikely(167.8, 373.7), "likely 168 to 374");
});

test("niceTicks covers the range with round steps", () => {
  const ticks = charts.niceTicks(-0.013, 0.031, 5);
  assert.ok(ticks[0] <= -0.013 && ticks[ticks.length - 1] >= 0.031);
  assert.ok(ticks.includes(0));
});

test("the not-causal banner appears for any artefact with causal false, and only then", () => {
  assert.equal(views.notCausalBanner(null, { causal: true }), "");
  const banner = views.notCausalBanner(null, { causal: false });
  assert.match(banner, /Not causal/);
  assert.match(banner, /not randomly assigned/);
});

test("model page: Qini SVG with the random line, AUUC with its interval, ten decile groups", () => {
  const art = {
    "uplift_validation.json": { causal: true, randomness_auc: 0.512 },
    "uplift_evaluation.json": evaluation,
    "qini_curve.json": curve,
  };
  const html = views.modelPageHtml(uc, trainRun, art, { report: null, topSharePct: "" });
  assert.match(html, /data-module="uplift"/);
  assert.match(html, /<polyline class="model"/);
  assert.match(html, /<polyline class="rand"/);
  // The finding first, in words; AUUC and its interval only behind "Technical metrics".
  assert.match(text(html), /Contacting the top 10% the model picks: \+8 points more responses/);
  const metrics = html.slice(html.indexOf("<summary>Technical metrics</summary>"));
  assert.ok(html.indexOf("AUUC") >= html.indexOf("<summary>Technical metrics</summary>"), "AUUC appears before Technical metrics");
  assert.match(text(metrics), /0\.012 \(95% CI 0\.004 to 0\.021\)/);
  const tiles = [...html.matchAll(/<div class="kpi"><div class="l">([^<]*)<\/div><div class="v">([^<]*)<\/div>/g)].map((m) => m[1]);
  assert.deepEqual(tiles, ["Top 10% gain", "Top 30% gain", "Everyone contacted", "Model beats random targeting"]);
  assert.match(html, /<details class="adv utable"><summary>Show table<\/summary>/);
  assert.match(html, /id="u-ope-share"[^>]*value="10"/, "What if… is pre-filled with 10");
  assert.equal((html.match(/<g role="img" aria-label="Decile/g) || []).length, 10);
  assert.equal((html.match(/<rect class="(pos|neg)"/g) || []).length, 9, "the decile with no observed uplift gets no bar");
  assert.ok(!html.includes("unotcausal"));
  assert.ok(!html.includes("<campaign>"), "the use-case name is escaped");
  noJunk(html);
});

test("model page with no artefacts shows em dashes and says what is missing, never a number", () => {
  const html = views.modelPageHtml(uc, trainRun, {}, null);
  const kpis = [...html.matchAll(/<div class="v">([^<]*)<\/div>/g)].map((m) => m[1]);
  assert.deepEqual(kpis, [EM, EM, EM, EM]);
  assert.match(html, /The gain chart is not available for this run/);
  assert.match(html, /The model&#39;s test results are not available|The model's test results are not available/);
  noJunk(html);
});

test("model page carries the banner when the evaluation is not causal", () => {
  const art = { "uplift_evaluation.json": { ...evaluation, causal: false }, "qini_curve.json": curve };
  assert.match(views.modelPageHtml(uc, trainRun, art, null), /unotcausal/);
});

test("model page carries the banner when only the Qini curve says it is not causal", () => {
  // qini_curve.json carries `causal` like every uplift artefact (DEC-675); the chart alone is enough.
  const art = { "uplift_evaluation.json": evaluation, "qini_curve.json": { ...curve, causal: false } };
  assert.match(views.modelPageHtml(uc, trainRun, art, null), /unotcausal/);
  const causal = { "uplift_evaluation.json": evaluation, "qini_curve.json": { ...curve, causal: true } };
  assert.ok(!views.modelPageHtml(uc, trainRun, causal, null).includes("unotcausal"));
});

test("output page of a scoring run: four segments, recommended contacts, CI, treat-list download", () => {
  const art = { "segments.json": segments, "policy_recommendation.json": policy };
  const html = views.outputPageHtml(uc, scoreRun, art, { scoresHref: "/runs/r-score/scores.csv" });
  assert.equal((html.match(/<div class="useg /g) || []).length, 4);
  assert.match(html, /Sleeping dogs/);
  assert.match(html, /Never treat \(contact makes it worse\)/);
  const kpis = [...html.matchAll(/<div class="kpi"><div class="l">([^<]*)<\/div><div class="v">([^<]*)<\/div>/g)];
  assert.deepEqual(
    kpis.map((m) => [m[1], m[2]]),
    [
      ["Customers to contact", "150"],
      ["Extra customers expected to respond", "about 10"],
      ["Held back to measure", EM],
    ],
  );
  assert.match(text(html), /likely 3 to 16/);
  assert.match(text(html), /10 \(95% CI 3 to 16\)/, "the interval stays under Details");
  // The primary action is the download; the campaign link is the secondary.
  assert.match(html, /<a class="btn primary" href="\/runs\/r-score\/scores\.csv" download>Download contact list \(CSV\)/);
  assert.match(html, /<a class="btn secondary" href="#\/campaign\/win-back\/r-score">Measure campaign results/);
  assert.match(html, /The contact budget is reached/);
  // 300 persuadables vs 150 contacted is explained in one line.
  assert.match(text(html), /Contact 150 customers\. Of 300 persuadable customers, 30 are held back at random to measure the campaign or were opted out\./);
  assert.match(text(html), /120 more could be contacted/);
  // The segment formulas are behind Details, the meanings in words.
  assert.match(html, /<summary>How the segments are cut<\/summary>/);
  assert.match(text(html), /Sleeping dogs: Contact makes them less likely to respond/);
  noJunk(html);
});

test("output page of a training run points at scoring runs instead of offering a download", () => {
  const html = views.outputPageHtml(uc, trainRun, {}, { scoreRuns: [{ ...scoreRun, row_count: 5 }] });
  assert.ok(!html.includes("Download contact list"));
  assert.match(html, /#\/uplift\/win-back\/output\/r-score/);
  assert.match(html, /has not produced segments\.json yet/);
  assert.match(html, /Score customers with this model/);
  noJunk(html);
});

test("campaign results: an immature report says when results arrive and shows no lift", () => {
  const report = {
    run_id: "r-score",
    outcome_column: "reactivated",
    outcome_window_days: 30,
    as_of: "2026-09-20T00:00:00Z",
    status: "immature",
    results_available_on: "2026-10-01",
    treated_rows: 0,
    treated_conversions: 0,
    treated_rate: null,
    control_rows: 0,
    control_conversions: 0,
    control_rate: null,
    absolute_lift: null,
    relative_lift: null,
    incremental_conversions: null,
    p_value: null,
    rows_immature: 1200,
    rows_without_outcome: 0,
    rows_suppressed_or_untreated: 40,
    causal: true,
    summary: "Not yet.",
    computed_at: "2026-09-20T00:00:00Z",
  };
  const html = views.campaignPageHtml(uc, scoreRun, { report, form: {} });
  assert.match(html, /Results available on 01 Oct 2026/);
  assert.ok(!html.includes("Absolute lift"));
  assert.ok(!html.includes('class="kpi"'), "no tile before the window has elapsed");
  // A report exists, so the upload is folded and the value view is not offered yet.
  assert.match(html, /<summary>Measure again with a new outcomes file<\/summary>/);
  assert.ok(!html.includes("See the value in rupees"));
  noJunk(html);
});

test("campaign results: a mature report shows lift with its interval and the p-value", () => {
  const report = {
    run_id: "r-score",
    outcome_column: "reactivated",
    outcome_window_days: 30,
    as_of: "2026-11-20T00:00:00Z",
    status: "mature",
    results_available_on: null,
    treated_rows: 900,
    treated_conversions: 180,
    treated_rate: 0.2,
    control_rows: 100,
    control_conversions: 12,
    control_rate: 0.12,
    absolute_lift: cv(0.08, 0.011, 0.139),
    relative_lift: 0.6667,
    incremental_conversions: cv(72, 9.9, 125.1),
    p_value: 0.041,
    rows_immature: 0,
    rows_without_outcome: 3,
    rows_suppressed_or_untreated: 40,
    causal: true,
    summary: "The campaign added conversions.",
    computed_at: "2026-11-20T00:00:00Z",
  };
  const html = views.campaignPageHtml(uc, scoreRun, { report, form: {} });
  // The statistics sit in "Statistical details", closed.
  const stats = html.slice(html.indexOf("<summary>Statistical details</summary>"));
  assert.ok(html.indexOf("p-value") > html.indexOf("<summary>Statistical details</summary>"));
  assert.match(text(stats), /\+8\.00 pts|\+8 pts/);
  assert.match(text(stats), /95% CI \+1\.1 pts to \+13\.9 pts/);
  assert.match(stats, /0\.041/);
  assert.match(stats, /\+66\.7%/);
  assert.ok(!html.includes("Results available on"));
  // Rows are named for people, whole numbers and one-decimal rates.
  assert.match(text(html), /Contacted 900 180 20\.0%/);
  assert.match(text(html), /Not contacted \(control group\) 100 12 12\.0%/);
  // "Not part of the test" is explained by a line that adds up; the zero row is hidden.
  assert.match(text(html), /Of 1,043 customers on this campaign's list: 900 contacted, 100 held back as the control group, 40 not part of the test \(opted out or not selected\), 3 with no row in the outcomes file\./);
  assert.ok(!text(html).includes("Not yet known (outcome period still running)"));
  assert.match(html, /<a class="btn primary" href="#\/pilot\/value\/r-score">See the value in rupees/);
  assert.match(html, /<summary>Measure again with a new outcomes file<\/summary>/);
  assert.match(html, /<nav class="crumbs"[^>]*><a href="#\/">Home<\/a>.*<a href="#\/monitoring\/runs">Campaigns<\/a>/);
  noJunk(html);
});

/** `GET /pilot/roi/{run}` for a measured campaign: `benefit` is already turned round for an outcome to prevent. */
const roiOf = (good, incremental) => ({
  status: "measured",
  outcome_is_good: good,
  incremental,
  benefit: good ? incremental : { value: -incremental.value, low: -incremental.high, high: -incremental.low },
  benefit_label: good ? "Extra customers because of the campaign" : "Customers kept by the campaign",
});

const churnReport = {
  run_id: "r-score",
  outcome_column: "churn_next_60d",
  outcome_window_days: 60,
  as_of: "2026-09-24T05:54:52Z",
  status: "mature",
  results_available_on: null,
  treated_rows: 1800,
  treated_conversions: 582,
  treated_rate: 0.3233,
  control_rows: 200,
  control_conversions: 78,
  control_rate: 0.39,
  absolute_lift: cv(-0.0667, -0.1389, 0.0019),
  relative_lift: -0.171,
  incremental_conversions: cv(-120.0, -250.04, 3.36),
  p_value: 0.057,
  rows_immature: 0,
  rows_without_outcome: 0,
  rows_suppressed_or_untreated: 0,
  causal: true,
  summary: "Treated customers converted at 32.3% against 39.0% for the control group.",
  computed_at: "2026-09-24T05:54:52Z",
};

test("campaign results of a churn campaign speak of customers kept, in the value view's sign", () => {
  const roi = roiOf(false, { value: -120.0, low: -250.04, high: 3.36 });
  const html = views.campaignPageHtml(uc, { ...scoreRun, problem_type: "binary_classification" }, {
    report: churnReport,
    roi,
    form: {},
    title: "Retention offers",
  });
  const seen = text(html);
  assert.match(
    seen,
    /We cannot yet tell whether the campaign kept customers: most likely 120 kept, but the range \(−3 to 250\) includes zero\./,
  );
  const tiles = [...html.matchAll(/<div class="kpi"><div class="l">([^<]*)<\/div><div class="v">([^<]*)<\/div>/g)];
  assert.deepEqual(
    tiles.map((m) => [m[1], m[2]]),
    [
      ["Customers kept by the campaign", "120"],
      ["Share lost: contacted vs not contacted", "32.3% vs 39.0%"],
    ],
  );
  for (const wrong of ["fewer conversions", "Incremental conversions", "−120", "converted"]) {
    assert.ok(!seen.includes(wrong), `a churn campaign reads "${wrong}"`);
  }
  // A Phase 1 run: its Output step is the Phase 1 page, and there is no Uplift chip.
  assert.match(html, /href="#\/uc\/win-back\/output\/r-score">Output</);
  assert.ok(!html.includes('class="chip type"'));
  assert.match(html, /<h1 class="h1">Retention offers<\/h1>/);
  noJunk(html);
});

test("campaign verdicts: worked, did harm, and the direction of an outcome to prevent", () => {
  const worked = views.campaignVerdict(null, roiOf(true, { value: 280.86, low: 167.8, high: 373.7 }));
  assert.equal(worked.tone, "ok");
  assert.equal(worked.title, "The campaign worked: about 281 extra customers responded because of it (likely 168 to 374).");
  const kept = views.campaignVerdict(null, roiOf(false, { value: -120, low: -250, high: -10 }));
  assert.equal(kept.title, "The campaign worked: it kept about 120 customers (likely 10 to 250).");
  const harm = views.campaignVerdict(null, roiOf(false, { value: 40, low: 10, high: 70 }));
  assert.equal(harm.tone, "bad");
  assert.equal(harm.title, "The campaign did harm: about 40 more customers were lost than without it (likely −70 to −10).");
  // Without the value view the sentence states the difference in rates, never a count of conversions.
  const plain = views.campaignVerdict(churnReport, null);
  assert.match(plain.title, /differs from the control group by −6\.7 points/);
});

test("campaign results on a training run explain that a scoring run is needed", () => {
  const html = views.campaignPageHtml(uc, trainRun, { form: {} });
  assert.match(html, /measured on a scoring run/);
  assert.match(html, /href="#\/uplift\/win-back\/score">Score new data/);
  assert.ok(!html.includes("u-camp-file"));
});

test("setup: the uplift explanation once a treatment is picked, and TREATMENT_NOT_RANDOM can be acknowledged", () => {
  const state = {
    view: "setup",
    mode: "train",
    upload: {
      upload_id: "u1",
      profile: {
        file_name: "c.csv",
        row_count: 10,
        column_count: 4,
        file_size_bytes: 100,
        columns: ["id", "treated", "converted", "visits"].map((name) => ({ name })),
      },
    },
    pk: "id",
    target: "converted",
    treatment: "treated",
    candidates: { candidates: [{ column: "treated", treated_share: 0.5, hinted: true }], detected: "treated" },
    models: [],
    runs: [],
    validation: null,
    upliftValidation: {
      checks: [
        {
          code: "TREATMENT_NOT_RANDOM",
          severity: "error",
          message: "Treatment can be predicted from the features.",
          suggestion: "Use a randomised campaign.",
          details: {},
          acknowledgeable: true,
          acknowledged: false,
        },
      ],
      passed: false,
      causal: true,
      randomness_auc: 0.71,
    },
    acknowledged: [],
  };
  const html = views.upliftScreenHtml(uc, state);
  assert.match(html, /predicts who changes behaviour because of your action/);
  assert.match(html, /data-uack="TREATMENT_NOT_RANDOM"/);
  assert.match(html, /50% treated · named like a treatment/);
  assert.match(html, /1 thing to fix before training\./);
  // The code is in data-code, the pill is inline, the randomness measure is merged into the refusal, red.
  assert.match(html, /<div class="vitem" data-code="TREATMENT_NOT_RANDOM"><span class="pill bad" data-code="TREATMENT_NOT_RANDOM">Must fix<\/span>/);
  assert.match(html, /<span class="pill bad" data-code="RANDOMNESS">Not random<\/span><span>Randomness measured 0\.71 \(0\.5 = random\)\.<\/span>/);
  assert.equal((html.match(/data-code="RANDOMNESS"/g) || []).length, 1, "one randomness line");
  // The acknowledgement sits next to the Train button.
  assert.ok(html.indexOf("data-uack") < html.indexOf('id="u-run"') && html.indexOf("data-uack") > html.indexOf('class="vlist"'));
  assert.match(html, /Column that says who was contacted \(1\) or held back \(0\)/);
  assert.equal(views.setupBlocker(state), "");
  const acked = views.upliftScreenHtml(uc, { ...state, acknowledged: ["TREATMENT_NOT_RANDOM"] });
  assert.match(acked, /data-uack="TREATMENT_NOT_RANDOM" checked/);
  assert.match(acked, /Nothing left to fix before training/);
  assert.match(acked, /Not causal: the treatment was not randomly assigned/);
  noJunk(html);
});

test("setup: a passing randomness check is a green pill, and warnings are folded and grouped", () => {
  const warning = (message) => ({ code: "LOW_POSITIVE_RATE", severity: "warning", message, details: {}, acknowledged: false });
  const state = {
    view: "setup",
    mode: "train",
    upload: { upload_id: "u1", profile: { file_name: "c.csv", row_count: 10, column_count: 2, file_size_bytes: 10, columns: [] } },
    pk: "id",
    target: "y",
    treatment: "t",
    models: [],
    runs: [],
    validation: { checks: [warning("Few converted."), warning("Few converted in March.")] },
    upliftValidation: { checks: [], randomness_auc: 0.503 },
    acknowledged: [],
  };
  const html = views.validationHtml(state);
  assert.match(html, /Nothing left to fix before training \(1 warning can be ignored\)\./);
  assert.match(html, /<span class="pill ok" data-code="RANDOMNESS">Looks random<\/span>/);
  assert.match(html, /<details class="uwarns"><summary>Show 1 warning<\/summary>/);
  assert.match(html, /\(2 times\)/);
});

test("setup before a file: step 2 is one line, and score mode has its own footer", () => {
  const empty = { view: "setup", mode: "train", upload: null, models: [], runs: [], acknowledged: [] };
  const html = views.upliftScreenHtml(uc, empty);
  assert.ok(!html.includes('id="u-pk"'), "no empty dropdowns before a file");
  assert.match(html, /Available once a file is uploaded\./);
  assert.match(html, /After the run: Model/);
  const score = views.upliftScreenHtml(uc, { ...empty, mode: "score" });
  assert.match(score, /After scoring: download the contact list/);
  assert.ok(!score.includes("Qini curve, AUUC"));
});

test("setup blocks a run until the treatment and outcome are distinct columns", () => {
  const base = { upload: { profile: {} }, pk: "id", mode: "train", target: "y", treatment: "" };
  assert.equal(views.setupBlocker({ ...base, upload: null }), "Upload a dataset to continue");
  assert.equal(views.setupBlocker(base), "Choose the column that says who was contacted");
  assert.match(views.setupBlocker({ ...base, treatment: "y" }), /different columns/);
  assert.equal(views.setupBlocker({ ...base, mode: "score", modelVersionId: "" }), "Train an uplift model first");
});

test("only AUUC model versions count as uplift models", () => {
  const versions = [{ version: { metric: "auuc" } }, { version: { metric: "roc_auc" } }];
  assert.equal(views.upliftVersions(versions).length, 1);
});

test("the index page links every use case to its uplift setup, with a status, and greys out the rest", () => {
  const html = views.upliftIndexHtml(
    {
      industries: [
        {
          stages: [
            {
              name: "Retention",
              use_cases: [
                { id: "a", name: "A" },
                { id: "b", name: "B" },
                { id: "g", name: "G", ai_type: "generative" },
                { id: "p", name: "P", status: "planned" },
              ],
            },
          ],
        },
      ],
    },
    { a: { created_at: "2026-09-24T05:54:59Z", approved: true }, b: null },
  );
  assert.match(html, /href="#\/uplift\/a"/);
  assert.match(html, /href="#\/uplift\/b"/);
  assert.ok(!html.includes('href="#/uplift/g"') && !html.includes('href="#/uplift/p"'));
  assert.match(html, /Writes text, so uplift does not apply/);
  assert.match(html, /Coming soon/);
  assert.match(text(html), /Uplift model trained 24 Sep(t)? 2026 \(approved\)/);
  assert.match(html, /No uplift model yet/);
  assert.match(html, /<h1 class="h1">Measure what a campaign changes \(uplift\)<\/h1>/);
  assert.ok(html.indexOf("Pick a use case") < html.indexOf('class="uindex"'), "the hint is above the list");
});

test("a finished scoring run's Campaign results block says when it applies instead of saying done", () => {
  const done = { ...scoreRun, state: "done", row_count: 5 };
  const html = views.upliftScreenHtml(uc, { view: "results", detail: { run: done }, runs: [] });
  const blocks = [...html.matchAll(/<div class="lab"><span>([^<]*)<\/span><span class="bstate([^"]*)">([^<]*)<\/span>/g)];
  assert.deepEqual(
    blocks.map((m) => [m[1], m[3]]),
    [
      ["Data", "✓ Done"],
      ["Contact list", "✓ Done"],
      ["Campaign results", "After the campaign"],
    ],
  );
  assert.equal(blocks[2][2], " waiting");
  assert.match(html, /<a class="btn primary" href="#" download>Download contact list \(CSV\)<\/a>/);
  noJunk(html);
});

test("a finished training run leads with the gain sentence and offers scoring as the primary action", () => {
  const done = { ...trainRun, state: "done", best_model: "X-learner (LightGBM)" };
  const html = views.upliftScreenHtml(uc, { view: "results", detail: { run: done }, runs: [], evaluation });
  assert.match(text(html), /Uplift model trained Contacting the top 10% the model picks raises the response rate by about 8 points\./);
  assert.match(html, /<a class="btn primary" href="#\/uplift\/win-back\/score">Score customers with this model<\/a>/);
  assert.ok(!text(html).includes("AUUC"), "no metric code on the results screen");
  noJunk(html);
});

test("running: plain step names, a warning in --warn, and a two-step cancel", () => {
  const status = {
    stages: [
      { group_label: "Validating data", state: "done", detail: "2 warnings · treatment column treatment" },
      { group_label: "Preparing features", state: "done", detail: "train 7K · test 3K · stratified on treatment and outcome" },
      { group_label: "Training candidate models", state: "running", detail: "" },
    ],
  };
  const s = { view: "running", detail: { run: { ...trainRun, state: "running" }, status }, runs: [] };
  const html = views.upliftScreenHtml(uc, s);
  assert.match(html, /Checking the campaign data/);
  assert.match(html, /<div class="pd w"[^>]*>2 warnings to review<\/div>/);
  assert.match(html, /Learning group about 7,000 · testing group about 3,000 customers/);
  assert.match(html, /<button type="button" class="btn danger sm" id="u-cancel">Cancel run<\/button>/);
  const confirming = views.upliftScreenHtml(uc, { ...s, confirmCancel: true });
  assert.match(confirming, /id="u-cancel">Yes, cancel run</);
  assert.match(confirming, /id="u-cancel-keep"/);
});

test("the stepper: Data · Model · Contact list · Campaign results, a step that does not apply is not a link", () => {
  const html = views.outputPageHtml(uc, scoreRun, {}, {});
  const tabs = [...html.matchAll(/class="tab(?: [^"]*)?"[^>]*>([^<]*)</g)].map((m) => m[1]);
  assert.deepEqual(tabs, ["Data", "Model", "Contact list", "Campaign results"]);
  assert.match(html, /<span class="tab off" aria-disabled="true"[^>]*>Model<\/span>/);
  assert.ok(html.indexOf("r-score<") > html.indexOf("<summary>Technical details</summary>"), "the run id is only in Technical details");
});
