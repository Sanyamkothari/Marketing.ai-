/* The browser's copy of the control-group power maths (ui/modules/measure/power.js) against the
   Python engine's (engine/uplift/power.py), case for case from tests/fixtures/power_cases.json, and
   the Output page's card rendered from scoring_summary-shaped data. Run by tests/unit/uplift/test_power_js.py,
   or directly: `node --test tests/unit/uplift/power.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const UI = new URL("../../../ui/", import.meta.url);
const power = await import(new URL("modules/measure/power.js", UI));
const cases = JSON.parse(fs.readFileSync(new URL("../../fixtures/power_cases.json", import.meta.url), "utf8"));
const near = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} vs ${b}`);
const EM = "—";

test("the normal CDF and quantile match known values", () => {
  near(power.normalCdf(0), 0.5, 1e-15);
  near(power.normalCdf(1.959963984540054), 0.975, 1e-12);
  near(power.normalQuantile(0.975), 1.959963984540054, 1e-9);
  near(power.normalQuantile(0.8), 0.8416212335729143, 1e-9);
  near(power.normalCdf(-8), 6.220960574271785e-16, 1e-20);
});

test("minimum detectable lift agrees with Python", () => {
  for (const c of cases.min_detectable_lift) {
    const got = power.minDetectableLift(c.n, c.fraction, c.p0, c.alpha, c.power);
    if (c.expected === null) {
      assert.equal(got, null, JSON.stringify(c));
      continue;
    }
    near(got.absolute, c.expected.absolute, 1e-8);
    if (c.expected.relative === null) assert.equal(got.relative, null);
    else near(got.relative, c.expected.relative, 1e-7);
    assert.equal(got.controlRows, c.expected.control_rows);
    assert.equal(got.treatedRows, c.expected.treated_rows);
  }
});

test("the smallest control fraction agrees with Python", () => {
  for (const c of cases.min_control_fraction) {
    const got = power.minControlFraction(c.n, c.p0, c.lift, c.alpha, c.power);
    if (c.expected === null) {
      assert.equal(got, null, JSON.stringify(c));
      continue;
    }
    assert.equal(got.controlRows, c.expected.control_rows, JSON.stringify(c));
    near(got.fraction, c.expected.fraction, 1e-12);
  }
});

test("power of a lift agrees with Python", () => {
  for (const c of cases.power_of_lift) {
    near(power.powerOfLift(c.n_treated, c.n_control, c.p0, c.lift, c.alpha), c.expected, 1e-10);
  }
});

const uc = { problem_type: "binary_classification" };
const summary = {
  rows_scored: 12000,
  score_mean: 0.1,
  control_group_rows: 1000,
  suppressed: [
    { reason: "opted_out", rows: 1500 },
    { reason: "consent_false", rows: 500 },
  ],
};

test("the card states the lift in points and relative, and says where the baseline came from", () => {
  const html = power.powerCardBody(uc, summary);
  assert.match(
    html,
    /With 10,000 eligible customers and a 10% control group, this campaign can reliably detect a lift of 3 points \(30% relative\) or more\./,
  );
  assert.match(html, /average predicted chance across everyone scored/);
  assert.match(html, /convert at 10%/);
  assert.match(html, /data-power-control/);
});

test("a figure the run did not write is a dash, never a number", () => {
  const broken = [
    { ...summary, rows_scored: undefined },
    { ...summary, control_group_rows: undefined },
    { ...summary, score_mean: undefined },
    { ...summary, score_mean: 0 },
  ];
  for (const bad of broken) {
    const html = power.powerCardBody(uc, bad);
    assert.ok(html.includes(EM), JSON.stringify(bad));
    assert.doesNotMatch(html, /reliably detect a lift of/);
    assert.doesNotMatch(html, /data-power-control/);
  }
  // a regression score is not a rate
  assert.ok(power.powerCardBody({ problem_type: "regression" }, summary).includes(EM));
  // nobody held back
  assert.match(power.powerCardBody(uc, { ...summary, control_group_rows: 0 }), /Nobody was held back/);
});

test("trying another control size or a target lift", () => {
  const inputs = power.powerInputs(uc, summary);
  assert.match(power.tryText(inputs, "50", ""), /At 50% held back \(5,000 customers\), a lift of 1\.7 points \(17% relative\)/);
  assert.match(power.tryText(inputs, "", "3"), /hold back at least 10\.\d% \(1,0\d\d customers\)/);
  assert.match(power.tryText(inputs, "", "0.1"), /cannot be reliably detected, even holding back the maximum 50%/);
  assert.match(power.tryText(inputs, "70", ""), /between 0% and 50%/);
  assert.equal(power.tryText(inputs, "", ""), "");
});

test("the scoring Output page carries the card, and bindPower answers as the person types", async () => {
  const pages = await import(new URL("pages.js", UI));
  const run = { run_id: "r-s", mode: "score", state: "done", problem_type: "binary_classification", primary_key: "customer_id" };
  const page = { ...uc, id: "u", pages: {}, advanced_settings: { stages: [] } };
  const full = {
    ...summary,
    score_field: "propensity",
    kpi: { label: "Customers to contact", formula: "x", display: "855", value: 855 },
    bands: [{ name: "High", action: "Act", rows: 10, share_pct: 1 }],
    actions: [],
    sample_rows: [],
  };
  const html = pages.renderPage("output", page, run, { "scoring_summary.json": full }, "#");
  assert.match(html, /Is the held-back group big enough\?/);
  assert.match(html, /can reliably detect a lift of 3 points/);
  // without a mean score the same page says so with a dash, and invents nothing
  const bare = pages.renderPage("output", page, run, { "scoring_summary.json": { ...full, score_mean: undefined } }, "#");
  assert.match(bare, /Is the held-back group big enough\?/);
  assert.ok(bare.includes(EM));
  assert.doesNotMatch(bare, /reliably detect a lift of/);

  // A hand-made root: just enough of the DOM for bindPower.
  const listeners = [];
  const el = (value = "") => ({ value, textContent: "", addEventListener: (_t, fn) => listeners.push(fn) });
  const control = el("10");
  const lift = el("");
  const answer = el();
  const cardEl = {
    dataset: { eligible: "10000", baseline: "0.1", held: "1000" },
    querySelector: (sel) => ({ "[data-power-control]": control, "[data-power-lift]": lift, "[data-power-answer]": answer })[sel],
  };
  power.bindPower({ querySelectorAll: (sel) => (sel === "[data-power-card]" ? [cardEl] : []) });
  control.value = "50";
  listeners.forEach((fn) => fn());
  assert.match(answer.textContent, /At 50% held back/);
  lift.value = "3";
  listeners.forEach((fn) => fn());
  assert.match(answer.textContent, /hold back at least 10\.\d%/);
});
