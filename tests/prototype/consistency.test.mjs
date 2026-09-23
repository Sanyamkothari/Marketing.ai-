/* House rules for the prototype: the illustrative numbers must agree with
   each other, every new setting must carry a one-line hint, and every new
   panel must have a mobile layout. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { pathToFileURL } from "node:url";
import { load, go, ev, $, $$, body, click, set, HTML, ROOT, repoText } from "./harness.mjs";

/* A product file that may not exist on this branch yet (Phase 3b's engine/uplift/ lands on
   phase-3b-uplift). Null when absent; PRODUCT_ROOT points the check at another checkout. */
const productText = (rel, has = () => true) => {
  const roots = [ROOT, process.env.PRODUCT_ROOT && pathToFileURL(process.env.PRODUCT_ROOT.replace(/\/?$/, "/"))];
  for (const root of roots.filter(Boolean)) {
    const url = new URL(rel, root);
    const text = fs.existsSync(url) ? fs.readFileSync(url, "utf8") : null;
    if (text !== null && has(text)) return text;
  }
  return null;
};

const CSS = HTML.slice(HTML.indexOf("<style>"), HTML.indexOf("</style>"));

test("the raw-table numbers add up", () => {
  const dom = load("#/uc/rca");
  assert.equal(ev(dom, "RAW_TABLES.length"), 6);
  assert.equal(ev(dom, "RAW_TABLES[0].rows"), "20,000", "20,000 customers");
  assert.deepEqual(ev(dom, "JSON.stringify(RAW_TABLES.slice(1).map(t=>t.cov))"), "[96,96,96,96,96]");
  assert.equal(ev(dom, "RAW_TABLES[0].cov"), null, "the spine table has no coverage of itself");
  assert.equal(ev(dom, "SNAPS.length"), 6, "six snapshots");
  const mean = ev(dom, "SNAPS.reduce((a,x)=>a+x[1],0)/SNAPS.length");
  assert.equal(Math.round(mean * 10) / 10, 11.8, "the snapshot rates average the headline 11.8%");
  // 20,000 customers × 6 snapshots = 120,000 rows; 11.8% of those = 14,160 positives
  const rows = 20000 * 6;
  assert.equal(rows, 120000);
  assert.equal(Math.round(rows * mean) / 100, 14160);
  assert.ok(ev(dom, "CHECKS.map(c=>c[0]).join(' ')").includes("14,160 positive examples"));
  assert.ok(ev(dom, "CHECKS.map(c=>c[0]).join(' ')").includes("96% of event rows"));
  // 16 suggested features, 2 dropped, 14 built
  assert.equal(ev(dom, "FEATS.length"), 16);
  assert.equal(ev(dom, "DROPPED.length"), 2);
  assert.ok(ev(dom, "DROPPED.every(d=>FEATS.some(f=>f[1]===d[0]))"),
    "a dropped feature must be one that was suggested");
  // the lineage repeats the same totals
  assert.match(ev(dom, "JSON.stringify(LINEAGE)"), /14 features · 6 snapshots/);
  assert.match(ev(dom, "JSON.stringify(LINEAGE)"), /120,000 rows/);
});

test("the document numbers add up", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  assert.equal(ev(dom, "DOCS.reduce((a,d)=>a+d[1],0)"), ev(dom, "DOC_PAGES"));
  assert.equal(ev(dom, "DOCS.reduce((a,d)=>a+d[2],0)"), 1600);
  assert.equal(ev(dom, "DOC_CHUNKS"), "1,600");
  assert.equal(ev(dom, "WORST10.length"), 10);
  assert.ok(ev(dom, "EVAL.pass") > ev(dom, "PASS_THRESHOLD_DEFAULT"), "the sample run passes its threshold");
  assert.equal(ev(dom, "QA_FILE.pairs"), 300, "the same 300-question set the Model page names");
});

test("the reference set and its threshold are the product's, not the prototype's", () => {
  // These read the product's own files, so the prototype cannot drift from them unnoticed.
  const dom = load("#/uc/ai-onboarding-assistant");
  const yaml = repoText("configs/engine.yaml");
  const block = yaml.slice(yaml.indexOf("reference_set:"), yaml.indexOf("root_cause:"));
  const val = (k) => (block.match(new RegExp(`${k}:\\s*([\\w.]+)`)) || [])[1];
  assert.equal(ev(dom, "PASS_THRESHOLD_DEFAULT"), Math.round(Number(val("pass_threshold")) * 100),
    "the threshold shown is generative.reference_set.pass_threshold");
  // the order the API checks the columns in, so the prototype names the same missing one
  const api = repoText("api/routes/generative.py");
  const required = api.slice(api.indexOf("required = ["), api.indexOf("]", api.indexOf("required = [")));
  const order = [...required.matchAll(/(question_column|refusal_column|reference_column|SOURCE_DOC_COLUMN)/g)]
    .map((m) => (m[1] === "SOURCE_DOC_COLUMN" ? "source_doc" : val(m[1])));
  assert.deepEqual(JSON.parse(ev(dom, "JSON.stringify(REF_COLUMNS)")), order);
  // the sample reference set is the downloadable template, column for column
  const template = repoText("templates/ai_onboarding_assistant_template.csv").split(/\r?\n/)[0].split(",");
  const dom2 = load("#/uc/ai-onboarding-assistant");
  click(dom2, "#f-sampledocs");
  click(dom2, "#f-sampleqa");
  assert.deepEqual(JSON.parse(ev(dom2, "JSON.stringify(STATE['ai-onboarding-assistant'].cols)")), template);
  assert.equal(ev(dom, "refInvalid('source_doc')"), "The reference set is missing the source_doc column.",
    "the wording of REFERENCE_SET_INVALID in DATA_CONTRACT.md §9.2");
  assert.ok(repoText("docs/DATA_CONTRACT.md").includes("The reference set is missing the {column} column."));
});

test("the win-back copy counts match the cards", () => {
  const dom = load("#/uc/win-back-campaign/output");
  assert.equal(ev(dom, "COPY_SEED.length"), 12);
  assert.equal(ev(dom, "new Set(COPY_SEED.map(c=>c.ch)).size"), 3);
  assert.equal(ev(dom, "new Set(COPY_SEED.map(c=>c.band)).size"), 2);
  assert.ok(ev(dom, "COPY_SEED.every(c=>c.alt&&c.alt!==c.text)"),
    "every card has different copy to regenerate into");
  // status, block reason and judge scores are all read off the message (engine/generative/
  // guardrails.py does the same), so none of the three can drift from the copy above it
  assert.equal(ev(dom, "COPY_SEED.filter(c=>tplStatus(c)==='Blocked').length"), 2);
  assert.ok(ev(dom, "COPY_SEED.every(c=>(tplStatus(c)==='Blocked')===tplGuards(c).some(g=>g[1]==='blocked'))"),
    "a blocked status must be a blocked guardrail, and nothing else");
  // the SMS blocked for length really is over its channel's limit; the others are not
  assert.ok(ev(dom, "COPY_SEED.find(c=>c.ch==='SMS'&&c.v==='B'&&c.band==='High').text.length") > 160);
  assert.ok(ev(dom, "COPY_SEED.filter(c=>c.ch==='SMS'&&tplStatus(c)!=='Blocked').every(c=>c.text.length<=CH_LIMIT.SMS)"));
  // the two judges the engine runs on copy (win_back.py _TEMPLATE_JUDGES), and nothing invented
  assert.ok(ev(dom, "COPY_SEED.every(c=>/^Compliance \\d\\.\\d\\d · Toxicity \\d\\.\\d\\d · \\d+ characters$/.test(judgeLine(c)))"),
    "the judge line names the judges that actually run");
  // control + suppressed + written-to = eligible
  const n = (k) => Number(ev(dom, `HOLDOUT.${k}`).replace(/,/g, ""));
  assert.equal(n("control") + n("suppressed") + n("sent"), n("eligible"));
});

test("every new setting carries a one-line hint", () => {
  const dom = load("#/uc/rca");
  click(dom, '.pickcard[data-src="raw"]');
  click(dom, "#ob-sample");
  // sources: a hint line for every row's role
  assert.equal($$(dom, ".srchint").length, $$(dom, ".srcrow").length);
  assert.ok(ev(dom, "Object.keys(ROLE_HINT).length") === ev(dom, "ROLES.length"),
    "one hint per role, including the roles not in the sample");
  assert.ok(ev(dom, "Object.values(ROLE_HINT).every(h=>h.length>20&&h.endsWith('.'))"));
  // every required column has a message written for it
  assert.ok(ev(dom, "['customer_id','event_date'].every(k=>REQ_MSG[k])"));
  // each step body opens with a hint
  for (const n of [2, 3, 4]) {
    click(dom, `[data-ob-open="${n}"]`);
    assert.ok($(dom, ".obbody .obhint, .obbody .sechd"), `step ${n} explains itself`);
  }
  // the assistant's refusal message is explained
  go(dom, "#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  assert.match($(dom, '[data-adv="refusal"]').closest(".field").textContent, /Sent instead of a guess/);
});

test("every new panel has a mobile layout", () => {
  for (const rule of [
    ".pick{grid-template-columns:1fr}",      // step 1 choice
    ".srcrow{grid-template-columns:1fr 1fr}", // source rows
    ".maprow{grid-template-columns:1fr 1fr}", // mapping rows
    ".featrow{grid-template-columns:18px 1fr}", // feature checklist
    ".report .rrow{grid-template-columns:1fr 1fr}", // build report
    ".ccgrid{grid-template-columns:1fr}",    // campaign copy
    ".lineage{flex-direction:column}",       // lineage flow
    ".headtools{order:-1;width:100%;",       // client picker stacks above the title
    ".dbrow{grid-template-columns:30px minmax(0,1fr)}", // uplift by decile
    ".segrow{grid-template-columns:12px minmax(0,1fr) 64px}", // four segments
    ".crgrid{grid-template-columns:1fr}",    // campaign results form
  ]) {
    assert.ok(CSS.includes(rule), `no mobile rule for ${rule}`);
  }
  // and every one of those sits inside a max-width media query
  const mobile = CSS.split("@media (max-width:").slice(1).join(" ");
  for (const sel of [".pick{", ".srcrow{", ".maprow{", ".featrow{", ".ccgrid{", ".lineage{", ".dbrow{", ".segrow{", ".crgrid{"]) {
    assert.ok(mobile.includes(sel), `${sel} mobile rule is outside a media query`);
  }
});

test("a number the product has not measured is still an em dash", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  assert.equal(ev(dom, "DOCS[0][1]"), 48, "sample documents carry sample numbers");
  // cost is an artefact now that engine/generative writes one, so the prototype shows its shape
  assert.match(ev(dom, "usageLine(ASSISTANT_USAGE)"), /calls · .+ tokens · .*\$/);
  // but a document the prototype has not read still has no page or chunk count
  assert.ok(HTML.includes('size:"—",rows:"—"'), "a real upload starts with em dashes");
});

/* ---------- Phase 3b §8: uplift ---------- */

test("the uplift label, defaults and thresholds are the product's", (t) => {
  const yaml = productText("configs/engine.yaml", (y) => /^  uplift:\s+#/m.test(y)) || "";
  const start = yaml.search(/^  uplift:\s+#/m);
  if (start < 0) return t.skip("configs/engine.yaml has no uplift: block on this branch yet");
  const dom = load("#/uc/win-back-campaign");
  // "uplift:" appears three times in the file; the label is the problem-type catalogue's
  assert.equal(ev(dom, "UPLIFT_PTYPE"), yaml.match(/^\s+uplift:\s+\{label: "([^"]+)"/m)[1]);
  const next = yaml.slice(start + 1).search(/^  \w/m); // the block may be the file's last
  const block = yaml.slice(start, next < 0 ? undefined : start + 1 + next);
  const val = (k) => (block.match(new RegExp(`^\\s+${k}:\\s*([^#\\n]+?)\\s*(#|$)`, "m")) || [])[1];
  const D = JSON.parse(ev(dom, "JSON.stringify(UPLIFT_DEFAULTS)"));
  assert.equal(D.learner, { x_learner: "X-learner", t_learner: "T-learner", s_learner: "S-learner" }[val("learner")]);
  assert.equal(D.base, { autogluon_fast: "AutoGluon fast", lightgbm: "LightGBM" }[val("base_model")]);
  assert.equal(D.minArmRows, Number(val("min_arm_rows")));
  assert.equal(D.minArmPos, Number(val("min_arm_positives")));
  assert.equal(D.randomAucMax, Number(val("randomness_auc_max")));
  assert.equal(D.bootstrap, Number(val("bootstrap_samples")));
  assert.equal(D.testFraction, Number(val("test_fraction")));
  assert.equal(D.persuadable, Number(val("persuadable_min_uplift")));
  assert.equal(D.sleepingDog, Number(val("sleeping_dog_max_uplift")));
  const sure = val("sure_thing_min_probability");
  assert.equal(D.sureThing, sure === "null" ? null : Number(sure), "unset means the training base rate");
  assert.equal(ev(dom, "defaultAdv(SETUP['win-back-campaign']).upSure"), D.sureThing);
  assert.deepEqual(JSON.parse(ev(dom, "JSON.stringify(TREAT_HINTS)")),
    val("treatment_column_hints").replace(/[[\]]/g, "").split(",").map((x) => x.trim()));
  assert.ok(ev(dom, "UP_LEARNERS[0]").startsWith(D.learner), "the default learner is listed first");
  assert.ok(ev(dom, "UP_BASES").includes(D.base));
  // policy defaults: no budget, cost or value unless a use case sets one
  assert.equal(val("budget_contacts"), "null");
  assert.equal(ev(dom, "defaultAdv(SETUP['payment-propensity']).upBudget"), null);
  assert.equal(ev(dom, "defaultAdv(SETUP['payment-propensity']).upCost"), null);
});

test("the uplift control group is Phase 1's, and it is the holdout the copy block shows", () => {
  const yaml = repoText("configs/engine.yaml");
  const frac = Number(yaml.match(/control_group_fraction:\s*([\d.]+)/)[1]);
  const dom = load("#/uc/win-back-campaign");
  assert.equal(ev(dom, "defaultAdv(SETUP['win-back-campaign']).control"), Math.round(frac * 100));
  const n = (k) => Number(ev(dom, `HOLDOUT.${k}`).replace(/,/g, ""));
  assert.equal(n("control") / n("eligible"), frac, "1,450 of 14,500");
  // the campaign measured on the Campaign results page is that same holdout
  assert.equal(ev(dom, "CAMPAIGN_OUTCOMES.treated[0]"), n("sent"));
  assert.equal(ev(dom, "CAMPAIGN_OUTCOMES.control[0]"), n("control"));
  assert.equal(ev(dom, "CAMPAIGN_OUTCOMES.suppressed"), n("suppressed"));
  assert.equal(ev(dom, "CAMPAIGN_SEED.rows"), ev(dom, "HOLDOUT.eligible"));
});

test("segment labels, actions and the not-causal note are the contract's, verbatim", (t) => {
  const py = productText("engine/uplift/contracts.py");
  if (!py) return t.skip("engine/uplift/contracts.py is not on this branch yet");
  const dom = load("#/");
  const pairs = (from, to) => Object.fromEntries([...py.slice(py.indexOf(from), py.indexOf(to))
    .matchAll(/Segment\.(\w+): "([^"]+)"/g)].map((m) => [m[1].toLowerCase(), m[2]]));
  assert.deepEqual(JSON.parse(ev(dom, "JSON.stringify(SEG_LABELS)")), pairs("SEGMENT_LABELS: Final", "SEGMENT_ACTIONS: Final"));
  assert.deepEqual(JSON.parse(ev(dom, "JSON.stringify(SEG_ACTIONS)")), pairs("SEGMENT_ACTIONS: Final", '"""The recommended'));
  assert.deepEqual(JSON.parse(ev(dom, "JSON.stringify(SEG_IDS)")), Object.keys(pairs("SEGMENT_LABELS: Final", "SEGMENT_ACTIONS: Final")),
    "persuadables, sure things, lost causes, sleeping dogs, in that order");
  const note = py.slice(py.indexOf("NOT_CAUSAL_NOTE: Final[str] = ("), py.indexOf("\n)", py.indexOf("NOT_CAUSAL_NOTE: Final[str] = (")));
  assert.equal(ev(dom, "NOT_CAUSAL_NOTE"), [...note.matchAll(/"([^"]*)"/g)].map((m) => m[1]).join(""));
});

test("the AUUC sentence is metrics.py's, template for template", (t) => {
  const py = productText("engine/uplift/metrics.py");
  if (!py) return t.skip("engine/uplift/metrics.py is not on this branch yet");
  const fn = py.slice(py.indexOf("def _summary("), py.indexOf("def evaluate_uplift("));
  // the four branches in source order: no interval, above zero, below zero, straddling zero
  const templates = fn.split("sentence = (").slice(1).map((chunk) =>
    [...chunk.slice(0, chunk.indexOf("\n        )")).matchAll(/f?"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]).join(""));
  assert.equal(templates.length, 4);
  assert.match(fn, /return f"\{NOT_CAUSAL_NOTE\} \{sentence\}" if not causal else sentence/);
  const dom = load("#/");
  const f4 = (v) => ev(dom, `f4(${v})`);
  const fill = (tpl, c) => tpl.replace("{level}", "95%").replace("{_fmt(auuc.value)}", f4(c.value))
    .replace("{_fmt(auuc.ci_low)}", f4(c.ci_low)).replace("{_fmt(auuc.ci_high)}", f4(c.ci_high));
  const cases = [
    { value: 0.0031, ci_low: null, ci_high: null },
    { value: 0.0125, ci_low: 0.0098, ci_high: 0.0151 },
    { value: -0.0042, ci_low: -0.0071, ci_high: -0.0013 },
    { value: 0.0006, ci_low: -0.0021, ci_high: 0.0034 },
  ];
  cases.forEach((c, i) => {
    const js = ev(dom, `upliftSummary(${JSON.stringify({ ...c, confidence_level: 0.95 })})`);
    assert.equal(js, fill(templates[i], c));
    assert.equal(ev(dom, `upliftSummary(${JSON.stringify(c)},false)`), `${ev(dom, "NOT_CAUSAL_NOTE")} ${js}`);
  });
  assert.equal(f4(-0.00001), "0.0000", "_fmt never prints -0.0000");
});

/* Python string literals of a source file, adjacent ones joined as Python joins them. Each run is
   one template as written (f-string braces left in place); docstrings and comments are skipped. */
function pyStringRuns(src) {
  const runs = [];
  let i = 0, open = false, lastEnd = -1;
  const lit = /([rRbBfF]{0,2})("""|'''|"|')/y;
  while (i < src.length) {
    const ch = src[i];
    if (ch === "#") { i = src.indexOf("\n", i); if (i < 0) break; continue; }
    lit.lastIndex = i;
    const m = /[A-Za-z0-9_]/.test(src[i - 1] || "") ? null : lit.exec(src);
    if (m) {
      const q = m[2], start = i + m[0].length;
      if (q.length === 3) { i = src.indexOf(q, start) + 3; open = false; continue; } // a docstring
      let j = start, body = "";
      while (src[j] !== q) {
        if (src[j] === "\\") { body += src[j + 1] === "n" ? "\n" : src[j + 1]; j += 2; } else body += src[j++];
      }
      if (open && /^\s*$/.test(src.slice(lastEnd, i))) runs[runs.length - 1] += body; else runs.push(body);
      open = true; lastEnd = j + 1; i = j + 1;
      continue;
    }
    if (!/\s/.test(ch)) open = false;
    i++;
  }
  return runs;
}
/* The function `name` of a Python source, up to the next top-level statement. */
const pyFunction = (src, name) => {
  const at = src.indexOf(`\ndef ${name}(`);
  assert.ok(at >= 0, `${name} is not in the engine source`);
  const next = src.slice(at + 1).search(/\n[^\s)#]/);
  return src.slice(at, next < 0 ? undefined : at + 1 + next);
};
/* Both ways: every template the prototype carries is one of the engine's, and every sentence the
   engine's functions write (a literal with a space, longer than a label) is one the prototype carries. */
function assertSameTemplates(py, fns, js, what) {
  const all = new Set(pyStringRuns(py));
  for (const [k, tpl] of Object.entries(js)) assert.ok(all.has(tpl), `${what}.${k} is not the engine's wording: ${tpl}`);
  const carried = new Set(Object.values(js));
  for (const fn of fns) {
    const runs = pyStringRuns(pyFunction(py, fn)).filter((r) => r.includes(" ") && r.length > 15);
    assert.ok(runs.length, `${fn} has no sentence to compare`);
    for (const run of runs) assert.ok(carried.has(run), `${fn} writes a sentence the prototype does not carry: ${run}`);
  }
}

test("the uplift check findings are checks.py's, template for template", (t) => {
  const py = productText("engine/uplift/checks.py", (x) => x.includes("def _not_random("));
  if (!py) return t.skip("engine/uplift/checks.py is not on this branch yet");
  const dom = load("#/");
  assertSameTemplates(py, ["_treatment_not_binary", "_arm_too_small", "_not_random"],
    JSON.parse(ev(dom, "JSON.stringify(UP_CHECK_TEXT)")), "UP_CHECK_TEXT");
  assert.equal(ev(dom, "RANDOMNESS_MIN_ARM_ROWS"), Number(py.match(/^RANDOMNESS_MIN_ARM_ROWS: Final\[int\] = (\d+)/m)[1]));
  const top = Number(py.match(/^TOP_SIGNALS: Final\[int\] = (\d+)/m)[1]);
  assert.ok(ev(dom, "TARGETED_SIGNALS.length") <= top, "the finding names at most TOP_SIGNALS features");
});

test("the Campaign results summary is incrementality.py's, template for template", (t) => {
  const py = productText("engine/uplift/incrementality.py", (x) => x.includes("def _summary("));
  if (!py) return t.skip("engine/uplift/incrementality.py is not on this branch yet");
  const dom = load("#/");
  assertSameTemplates(py, ["_summary", "_points"], JSON.parse(ev(dom, "JSON.stringify(INC_TEXT)")), "INC_TEXT");
  // the branches the page reaches, rendered: a lift, a null lift, a harm, and the immature sentence
  const base = { status: "mature", treated_rows: 11200, control_rows: 1450, rows_immature: 0, results_available_on: null, outcome_window_days: 90 };
  const r = (o) => ev(dom, `incSummary(${JSON.stringify({ ...base, ...o })})`);
  const lift = (v, lo, hi, p) => ({ treated_rate: 0.11, control_rate: 0.0752, absolute_lift: { value: v, ci_low: lo, ci_high: hi },
    incremental_conversions: { value: v * 11200 }, p_value: p });
  assert.match(r(lift(0.0348, 0.0191, 0.0486, 5e-5)), /: a lift of \+3\.5 points \(95% CI \+1\.9 points to \+4\.9 points; p < 0\.001\), about 390 extra conversions/);
  assert.match(r(lift(0.004, -0.01, 0.018, 0.4172)), /a difference of \+0\.4 points, but the 95% interval \(-1\.0 points to \+1\.8 points; p = 0\.417\) includes zero/);
  assert.match(r(lift(-0.03, -0.045, -0.015, 0.0004)), /about 336 fewer conversions caused by the campaign\.$/);
  assert.equal(r({ status: "immature", rows_immature: 4518, results_available_on: "2026-12-22" }),
    "Results available on 2026-12-22: the 90-day outcome window has not elapsed yet for any of the 4518 treated and control customers.");
});

test("the uplift numbers add up", () => {
  const dom = load("#/uc/win-back-campaign");
  const E = (x) => ev(dom, x);
  const D = JSON.parse(E("JSON.stringify(UPLIFT_DECILES)"));
  // 320K rows × the 0.30 hold-out = 96,000, in ten deciles, 90% treated like the 10% control holdout
  assert.equal(D.length, 10);
  assert.ok(D.every((d) => d.rows === d.treated_rows + d.control_rows));
  assert.equal(D.reduce((a, d) => a + d.rows, 0), 320000 * E("UPLIFT_DEFAULTS.testFraction"));
  assert.ok(D.every((d) => d.control_rows / d.rows === E("defaultAdv(SETUP['win-back-campaign']).control") / 100));
  assert.equal(E("UPLIFT_EVAL.treated") + E("UPLIFT_EVAL.control"), 320000);
  assert.equal(E("UPLIFT_EVAL.control") / 320000, 0.1);
  assert.ok(D.every((d, i) => i === 0 || d.predicted_uplift < D[i - 1].predicted_uplift), "ranked by predicted uplift");
  // the deciles' rates: 10.32% treated, 6.31% control, a 4.01-point average effect
  const T = JSON.parse(E("JSON.stringify(armTotals())"));
  assert.equal((T.yt / T.nt * 100).toFixed(2), "10.32");
  assert.equal((T.yc / T.nc * 100).toFixed(2), "6.31");
  assert.equal(E("ateOf()").toFixed(4), "0.0401");
  // the Qini curve runs from 0 to 1 and ends on the random line
  const P = JSON.parse(E("JSON.stringify(qiniPoints())"));
  assert.deepEqual([P[0].fraction, P[0].qini, P[0].random], [0, 0, 0]);
  assert.equal(P.at(-1).fraction, 1);
  assert.equal(P.at(-1).qini, P.at(-1).random);
  assert.equal(P.at(-1).qini.toFixed(5), "0.03607");
  assert.ok(P.every((p) => Math.abs(p.random - p.fraction * P.at(-1).qini) < 1e-12), "the random line is f × qini(1)");
  // AUUC and the Qini coefficient by the trapezoid rule, inside their stored intervals, above zero
  const auuc = JSON.parse(E("JSON.stringify(auucCI())"));
  assert.equal(auuc.value.toFixed(4), "0.0125");
  assert.ok(auuc.ci_low > 0 && auuc.ci_low < auuc.value && auuc.value < auuc.ci_high);
  const qc = E("qiniCoefOf()");
  assert.equal(qc.toFixed(4), "0.0113");
  assert.ok(E("UPLIFT_EVAL.qini.ci_low") < qc && qc < E("UPLIFT_EVAL.qini.ci_high"));
  const nul = JSON.parse(E("JSON.stringify(UPLIFT_NULL)"));
  assert.ok(nul.ci_low < 0 && nul.ci_high > 0 && nul.ci_low < nul.value && nul.value < nul.ci_high);
  // segments: 14,500 scored and the 96,000 hold-out, each summing to its population
  const sum = (o) => Object.values(o).reduce((a, b) => a + b, 0);
  assert.equal(sum(JSON.parse(E("JSON.stringify(UPLIFT_SEGS)"))), Number(E("HOLDOUT.eligible").replace(/,/g, "")));
  assert.equal(sum(JSON.parse(E("JSON.stringify(UPLIFT_SEGS_TEST)"))), 96000);
  const a = "defaultAdv(SETUP['win-back-campaign'])";
  assert.deepEqual(JSON.parse(E(`JSON.stringify(segCounts('scored',${a}))`)), JSON.parse(E("JSON.stringify(UPLIFT_SEGS)")),
    "at the default cuts the counts are the sample's");
  // the hold-out's persuadables sit where the decile means cross the persuadable cut
  const share = E("UPLIFT_SEGS_TEST.persuadable") / 96000;
  assert.ok(Math.abs(share - E("shareAbove(UPLIFT_DEFAULTS.persuadable)")) < 0.01);
  // each segment's mean uplift is P(treated) − P(control), on the right side of the cuts
  const M = JSON.parse(E("JSON.stringify(UPLIFT_SEG_MEANS)"));
  assert.ok(Object.values(M).every(([u, pt, pc]) => Math.abs(u - (pt - pc)) < 1e-9));
  assert.ok(M.persuadable[0] >= 0.02 && M.sleeping_dog[0] <= -0.01);
  assert.ok(M.sure_thing[1] > M.lost_cause[1], "sure things convert anyway, lost causes do not");
  // the policy: N within the eligible persuadables and within the budget. A scoring run is sized by its
  // own row count (the sample list: 14,500); a sample training run by its hold-out (96,000)
  const R = "rows:'14,500'";
  for (const mode of ["score", "train"]) {
    const p = JSON.parse(E(`JSON.stringify(upliftPolicy({mode:'${mode}',adv:${a},causal:true,${R}}))`));
    assert.equal(p.n, 4000);
    assert.equal(p.stop_reason, "budget");
    assert.ok(p.n <= p.eligible_persuadables && p.n <= p.segs.persuadable && p.n <= p.budget);
    assert.ok(p.expected.ci_low > 0 && p.expected.ci_low < p.expected.value && p.expected.value < p.expected.ci_high);
  }
  const ps = JSON.parse(E(`JSON.stringify(upliftPolicy({mode:'score',adv:${a},causal:true,${R}}))`));
  assert.equal(ps.eligible_persuadables, 5220 * 11200 / 14500, "persuadables outside the control group and suppression");
  assert.equal(Math.round(ps.expected.value), 405);
  // with no budget every eligible persuadable is chosen, and never more
  const nb = JSON.parse(E(`JSON.stringify(upliftPolicy({mode:'score',adv:{...${a},upBudget:null},causal:true,${R}}))`));
  assert.equal(nb.n, nb.eligible_persuadables);
  assert.equal(nb.stop_reason, "all_persuadables");
});

test("the campaign report is computed from its counts", () => {
  const dom = load("#/uc/win-back-campaign");
  ev(dom, "AS_OF='2026-09-23T00:00:00Z'");
  const r = JSON.parse(ev(dom, "JSON.stringify(campaignReport(CAMPAIGN_SEED,CAMPAIGN_OUTCOMES,today()))"));
  assert.equal(r.status, "mature");
  assert.equal(r.results_available_on, null);
  assert.equal(r.treated_rate, 1232 / 11200);
  assert.equal(r.control_rate, 109 / 1450);
  assert.ok(Math.abs(r.absolute_lift.value - (r.treated_rate - r.control_rate)) < 1e-15, "lift = treated rate − control rate");
  assert.ok(Math.abs(r.incremental_conversions.value - r.absolute_lift.value * r.treated_rows) < 1e-9,
    "incremental conversions = lift × treated rows");
  assert.ok(Math.abs(r.incremental_conversions.ci_low - r.absolute_lift.ci_low * r.treated_rows) < 1e-9);
  assert.ok(Math.abs(r.relative_lift - r.absolute_lift.value / r.control_rate) < 1e-15);
  // Newcombe and the z-test against the independent oracle (scratchpad intervals_oracle.py)
  assert.equal((r.absolute_lift.ci_low * 100).toFixed(2), "1.91");
  assert.equal((r.absolute_lift.ci_high * 100).toFixed(2), "4.86");
  assert.ok(Math.abs(r.p_value - 5.0447e-5) / 5.0447e-5 < 1e-3);
  assert.equal(r.treated_rows + r.control_rows + r.rows_suppressed_or_untreated, 14500);
  // results_available_on = send date + outcome window, while the window is open
  ev(dom, "STATE['win-back-campaign'].cr.window=180");
  const w = JSON.parse(ev(dom, "JSON.stringify(campaignReport(CAMPAIGN_SEED,CAMPAIGN_OUTCOMES,today()))"));
  assert.equal(w.status, "immature");
  assert.equal(w.results_available_on, "2026-10-28");
  assert.equal(w.absolute_lift, null, "nothing is measured before the window ends");
  assert.equal(w.rows_immature, 11200 + 1450);
  assert.equal(ev(dom, "fmtDay('2026-05-01')"), "1 May 2026");
  assert.equal(ev(dom, "fmtDay(addDays('2026-05-01',90))"), "30 Jul 2026");
});
