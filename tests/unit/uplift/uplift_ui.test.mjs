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
  assert.equal(format.fmtCi(cv(0.0123, 0.0041, 0.0205)), "0.0123 (95% CI 0.0041 to 0.0205)");
  assert.equal(format.fmtCi(cv(0.5, null, null)), `0.5 (95% CI ${EM})`);
  assert.equal(format.fmtCi(null), EM);
  assert.equal(format.fmtPts(0.023), "+2.3 pts");
  assert.equal(format.fmtPts(-0.004), "−0.4 pts");
  assert.equal(format.fmtPts(null), EM);
  assert.equal(format.fmtDay("2026-10-01"), "01 Oct 2026");
  assert.equal(format.fmtDay(null), EM);
  assert.equal(format.fmtP(0.0001), "< 0.001");
  assert.equal(format.fmtP(0.0421), "0.042");
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
  assert.match(html, /0\.0123/);
  assert.match(text(html), /95% CI 0\.0041 to 0\.0205/);
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
  assert.match(html, /has not produced qini_curve\.json yet/);
  assert.match(html, /has not produced uplift_evaluation\.json yet/);
  noJunk(html);
});

test("model page carries the banner when the evaluation is not causal", () => {
  const art = { "uplift_evaluation.json": { ...evaluation, causal: false }, "qini_curve.json": curve };
  assert.match(views.modelPageHtml(uc, trainRun, art, null), /unotcausal/);
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
      ["Recommended to contact", "150"],
      ["Expected incremental conversions", "9.5"],
      ["Eligible persuadables", "270"],
      ["Expected net value", EM],
    ],
  );
  assert.match(text(html), /9\.5 \(95% CI 3\.1 to 15\.9\)/);
  assert.match(html, /href="\/runs\/r-score\/scores\.csv">Download treat list \(CSV\)/);
  assert.match(html, /#\/campaign\/win-back\/r-score/);
  assert.match(html, /The contact budget is reached/);
  noJunk(html);
});

test("output page of a training run points at scoring runs instead of offering a download", () => {
  const html = views.outputPageHtml(uc, trainRun, {}, { scoreRuns: [{ ...scoreRun, row_count: 5 }] });
  assert.ok(!html.includes("Download treat list"));
  assert.match(html, /#\/uplift\/win-back\/output\/r-score/);
  assert.match(html, /has not produced segments\.json yet/);
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
  assert.match(html, /Upload campaign outcomes/);
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
  assert.match(text(html), /\+8\.00 pts|\+8 pts/);
  assert.match(text(html), /95% CI \+1\.1 pts to \+13\.9 pts/);
  assert.match(html, /0\.041/);
  assert.match(html, /\+66\.7%/);
  assert.ok(!html.includes("Results available on"));
  noJunk(html);
});

test("campaign results on a training run explain that a scoring run is needed", () => {
  const html = views.campaignPageHtml(uc, trainRun, { form: {} });
  assert.match(html, /measured on a scoring run/);
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
  assert.match(html, /1 problem must be fixed or acknowledged/);
  assert.equal(views.setupBlocker(state), "");
  const acked = views.upliftScreenHtml(uc, { ...state, acknowledged: ["TREATMENT_NOT_RANDOM"] });
  assert.match(acked, /data-uack="TREATMENT_NOT_RANDOM" checked/);
  assert.match(acked, /0 problems must be fixed/);
  assert.match(acked, /Not causal: the treatment was not randomly assigned/);
  noJunk(html);
});

test("setup blocks a run until the treatment and outcome are distinct columns", () => {
  const base = { upload: { profile: {} }, pk: "id", mode: "train", target: "y", treatment: "" };
  assert.equal(views.setupBlocker({ ...base, upload: null }), "Upload a dataset to continue");
  assert.equal(views.setupBlocker(base), "Choose the treatment column");
  assert.match(views.setupBlocker({ ...base, treatment: "y" }), /different columns/);
  assert.equal(views.setupBlocker({ ...base, mode: "score", modelVersionId: "" }), "Train an uplift model first");
});

test("only AUUC model versions count as uplift models", () => {
  const versions = [{ version: { metric: "auuc" } }, { version: { metric: "roc_auc" } }];
  assert.equal(views.upliftVersions(versions).length, 1);
});

test("the index page links every use case to its uplift setup", () => {
  const html = views.upliftIndexHtml({
    industries: [{ stages: [{ name: "Retention", use_cases: [{ id: "a", name: "A" }, { id: "b", name: "B" }] }] }],
  });
  assert.match(html, /href="#\/uplift\/a"/);
  assert.match(html, /href="#\/uplift\/b"/);
});
