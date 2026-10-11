/* Plan M, M170 (DEC-1600): the visible-text check.

   Every main page is drawn in jsdom, as the browser draws it (app.js and every module in the order of
   ui/index.html), against the API answers recorded from the app running on the validated Hillstrom demo
   (tests/fixtures/capture_ui_demo.py -> tests/fixtures/ui_demo/api.json). The text a person can read is
   collected (collect.mjs: technical sections, hidden elements, scripts and styles are left out;
   aria-label, title and placeholder count) and searched for the glossary's never-shown words
   (ui/labels.js).

   Each page has an allow-list of today's violations, the BASELINE (baseline.json: page, term, count). A page
   fails when it shows a term that is not on its list, or shows more of one than the list allows, so nothing
   gets worse. The table the test prints is M172's to-do list; M172 empties the file.

   Plan M M180 (DEC-1610): the check runs in BOTH positions of the person's "Show technical details" switch
   (ui/technical.js). Each page is drawn once and read twice:
     - on: as above (collect.mjs reads the technical marks), against the baseline;
     - off: the switch is turned off on the drawn page, and collect.mjs's "off" mode reads what the real CSS rule hides
       (computed style), not the marks. Every marked element must be hidden on screen, and the text is checked
       against the same baseline. Whenever baseline.json is EMPTY (M172 done), "off" must show zero never-shown
       terms on every page: that assertion is written below and switches itself on when the file empties.

   Environment:
     GLOSSARY_UPDATE_BASELINE=lower  rewrite baseline.json with every count at most what the pages show now
     GLOSSARY_UPDATE_BASELINE=init   rewrite baseline.json from what the pages show now (first creation only)
     GLOSSARY_DUMP=<dir>             write each page's visible text to <dir>/<page>.txt
*/
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { $, settle } from "../harness.mjs";
import { describe, lowered, table, violations } from "./check.mjs";
import { hiddenOnScreen, technicalElements, visibleText } from "./collect.mjs";
import { installDemoPage, loadModules } from "./replay.mjs";

const BASELINE = new URL("./baseline.json", import.meta.url);
const UC = "hillstrom-email";
const WIN = "win-back-campaign";
const TRAIN_RISK = "r_20261010_11100001";
const TRAIN_UPLIFT = "r_20261010_11100002";
const SCORE_UPLIFT = "r_20261010_11100003";
const SCORE_RISK = "r_20261010_11100004";
const C_CONVERSION = "c_20261011_c9276210";
const C_SPEND = "c_20261011_5ee8a5d4";

const click = (selector) => async () => {
  const el = $(selector);
  assert.ok(el, `nothing to click: ${selector}`);
  el.click();
};

/** id, where to go, what to do there, and what the page must show so a blank or error page cannot pass.
    `expect` is a sentence the page draws from its own content and `selector` an element it must contain; both
    are chosen so that they are NOT glossary words, because M172 renames those and must not need to edit this
    guard to finish its own task. */
export const PAGES = [
  { id: "home", hash: "#/", expect: /Customer Lifecycle/ },
  { id: "results", hash: "#/results", expect: /Needs your attention/ },
  { id: "setup-guided-win-back", hash: `#/uc/${WIN}`, expect: /Guided setup/ },
  { id: "setup-manual-win-back", hash: `#/uc/${WIN}`, steps: [click('[data-setup-tab="manual"]')], expect: /Upload CSV, Excel or Parquet/ },
  {
    id: "setup-manual-win-back-score",
    hash: `#/uc/${WIN}`,
    steps: [click('[data-setup-tab="manual"]'), click('.seg button[data-mode="score"]')],
    expect: /Win-back Campaign/,
    selector: '.seg button[data-mode="score"]',
  },
  { id: "setup-guided-hillstrom", hash: `#/uc/${UC}`, expect: /Guided setup/ },
  { id: "setup-manual-hillstrom", hash: `#/uc/${UC}`, steps: [click('[data-setup-tab="manual"]')], expect: /Upload CSV, Excel or Parquet/ },
  // Plan M M180: the run page's Data, Model and Output blocks are links to technical pages, marked `data-technical`,
  // so "View details" is no longer visible text in either mode; the blocks are asserted by selector instead.
  {
    id: "run-training",
    hash: `#/uc/${UC}/run/${TRAIN_RISK}`,
    expect: /Ranking quality/,
    selector: 'a.block[data-technical][href*="/model/"]',
  },
  { id: "data-training", hash: `#/uc/${UC}/data/${TRAIN_RISK}`, expect: /how it was prepared/ },
  { id: "model-training", hash: `#/uc/${UC}/model/${TRAIN_RISK}`, expect: /How well the model predicts/, selector: "[data-beeswarm]" },
  { id: "output-training", hash: `#/uc/${UC}/output/${TRAIN_RISK}`, expect: /Lift in the top 10%/ },
  {
    id: "run-scoring",
    hash: `#/uc/${UC}/run/${SCORE_RISK}`,
    expect: /Download contact list/,
    selector: 'a.block[data-technical][href*="/output/"]',
  },
  { id: "data-scoring", hash: `#/uc/${UC}/data/${SCORE_RISK}`, expect: /how it was prepared/ },
  { id: "model-scoring", hash: `#/uc/${UC}/model/${SCORE_RISK}`, expect: /nothing new to measure/ },
  { id: "output-scoring", hash: `#/uc/${UC}/output/${SCORE_RISK}`, expect: /Customers worth an e-mail/ },
  { id: "campaign-results-scoring", hash: `#/campaign/${UC}/${SCORE_RISK}`, expect: /Campaign results/ },
  {
    id: "run-uplift-training",
    hash: `#/uc/${UC}/run/${TRAIN_UPLIFT}`,
    expect: /E-mail Offer Choice/,
    selector: 'a.block[data-technical][href*="/model/"]',
  },
  { id: "data-uplift-training", hash: `#/uc/${UC}/data/${TRAIN_UPLIFT}`, expect: /how it was prepared/ },
  { id: "model-uplift-training", hash: `#/uplift/${UC}/model/${TRAIN_UPLIFT}`, expect: /Top 10% gain/ },
  { id: "contact-list-uplift-training", hash: `#/uplift/${UC}/output/${TRAIN_UPLIFT}`, expect: /Customers to contact/ },
  {
    id: "run-uplift-scoring",
    hash: `#/uc/${UC}/run/${SCORE_UPLIFT}`,
    expect: /Download contact list/,
    selector: 'a.block[data-technical][href*="/output/"]',
  },
  { id: "contact-list-uplift-scoring", hash: `#/uc/${UC}/output/${SCORE_UPLIFT}`, expect: /Customers to contact/ },
  { id: "campaign-conversion", hash: `#/campaigns/${C_CONVERSION}`, expect: /Plan the test/ },
  { id: "campaign-spend", hash: `#/campaigns/${C_SPEND}`, expect: /Plan the test/ },
  { id: "proof-pack-conversion", hash: `#/pilot/proof/${C_CONVERSION}`, expect: /Value Proof Pack/ },
  { id: "proof-pack-spend", hash: `#/pilot/proof/${C_SPEND}`, expect: /Value Proof Pack/ },
  { id: "reports", hash: "#/pilot", expect: /What the models found/ },
  // Plan M, M178: the new Reports page, one entry per tab, each with an EMPTY allow-list (a new page shows no never-shown word).
  { id: "reports-value-proof", hash: "#/reports", expect: /Value Proof Packs/, selector: "[data-proofs]" },
  { id: "reports-programme", hash: "#/reports/programme", expect: /Value proven to date/, selector: "[data-proven]" },
  { id: "reports-check", hash: "#/reports/check", expect: /Who was in which group/, selector: "[data-rp-check-form]" },
  { id: "reports-models", hash: "#/reports/models", expect: /Model reports/, selector: "[data-models-part]" },
  { id: "value-report", hash: `#/pilot/value/${SCORE_RISK}`, expect: /Value of one extra customer/ },
  { id: "settings", hash: "#/settings", expect: /AI service/ },
  { id: "connections", hash: "#/connections", expect: /The places your data lives/ },
  { id: "data-doctor", hash: "#/doctor", expect: /Data Doctor|Bring your data/ },
  { id: "uplift-workbench", hash: "#/uplift", expect: /Measure what a campaign changes/ },
  { id: "campaigns-scored-lists", hash: "#/monitoring/runs", expect: /Every list of customers/ },
  { id: "add-outcomes", hash: `#/monitoring/runs/${SCORE_RISK}`, expect: /What really happened to the customers/ },
  { id: "audit-campaign", hash: "#/audit", expect: /Measure a campaign another tool ran/ },
  { id: "model-health", hash: "#/monitoring/alerts", expect: /Problems Marketing AI found on its own/ },
  { id: "schedules", hash: "#/monitoring/schedules", expect: /that run on their own/ },
  { id: "cost", hash: "#/cost", expect: /Estimates at AWS/ },
  { id: "approvals", hash: "#/approvals", expect: /Models waiting for your approval/ },
  { id: "data-request-kit", hash: "#/pilot/kit", expect: /Ask your data team/ },
];

/** Requests a page may make that the recording does not hold (none today). Anything else fails the page. */
const EXPECTED_UNRECORDED = new Set([]);

const baseline = JSON.parse(fs.readFileSync(BASELINE, "utf8"));
const mode = process.env.GLOSSARY_UPDATE_BASELINE || "";
const dump = process.env.GLOSSARY_DUMP || "";

const { w, state, missing } = installDemoPage({ hash: "#/__start" });
await loadModules();
// The same module instance app.js loaded (one URL, one module): the page's own switch.
const technical = await import("../../../../../ui/technical.js");
const BASELINE_EMPTY = Object.keys(baseline).length === 0;

/** Wait until the page has stopped changing: nothing in flight and the same markup for a few ticks. */
async function untilStable(ms = 10000) {
  const started = Date.now();
  let last = null;
  let same = 0;
  while (Date.now() - started < ms) {
    await new Promise((resolve) => setTimeout(resolve, 30));
    const now = `${$("#app")?.innerHTML.length}:${document.body.innerHTML.length}`;
    if (state.inFlight === 0 && now === last) same += 1;
    else same = 0;
    last = now;
    if (same >= 6) return;
  }
}

async function visit(hash) {
  if (w.location.hash === hash) {
    w.location.hash = "#/__between";
    await untilStable();
  }
  w.location.hash = hash;
  await settle(2);
  await untilStable();
}

const shown = {};
const shownOff = {};

for (const page of PAGES) {
  test(`${page.id}: no engine word outside technical details beyond the baseline`, async () => {
    missing.clear();
    await visit(page.hash);
    for (const step of page.steps || []) {
      await step();
      await settle(2);
      await untilStable();
    }
    const text = visibleText(document.body);
    if (dump) {
      fs.mkdirSync(dump, { recursive: true });
      fs.writeFileSync(path.join(dump, `${page.id}.txt`), text);
    }
    assert.match(text, page.expect, `${page.id} did not draw (it showed: ${text.slice(0, 200)})`);
    if (page.selector) assert.ok($(page.selector), `${page.id} did not draw: nothing matches ${page.selector}`);
    // A page that asks for an answer the recording lacks would draw an empty or error state and so be tested
    // against fewer words; fail it instead, and say how to refresh the recording.
    const unrecorded = [...missing].filter((k) => !k.includes("/__") && !EXPECTED_UNRECORDED.has(k));
    assert.deepEqual(
      unrecorded,
      [],
      `${page.id} asked for answers that are not recorded; re-record with python -m tests.fixtures.capture_ui_demo`,
    );
    const v = violations(text, baseline[page.id] || {});
    shown[page.id] = v.found;
    if (!mode) assert.ok(v.ok, describe(page.id, v));
  });

  test(`${page.id} with "Show technical details" off: technical marks hidden by the CSS, no engine word beyond the baseline`, async () => {
    assert.ok(technical.showTechnical(), "the classic layout's default is on, so the page above was drawn as today");
    try {
      assert.equal(await technical.setShowTechnical(false), false);
      await settle(2);
      assert.ok(document.documentElement.classList.contains(technical.HIDE_CLASS), "the switch's class is on <html>");
      // Every technical mark on the page is hidden on screen by the real style sheet (or was hidden already).
      const visibleMarks = technicalElements(document.body).filter((el) => !hiddenOnScreen(el));
      assert.deepEqual(
        visibleMarks.map((el) => el.outerHTML.slice(0, 120)),
        [],
        `${page.id}: a technical section is still on screen with the switch off`,
      );
      const text = visibleText(document.body, { mode: "off" });
      if (dump) fs.writeFileSync(path.join(dump, `${page.id}.off.txt`), text);
      const v = violations(text, baseline[page.id] || {});
      shownOff[page.id] = v.found;
      if (!mode) assert.ok(v.ok, describe(`${page.id} (technical details off)`, v));
      // Plan M acceptance (M180): with the switch off no glossary never-shown term appears anywhere. Active as soon
      // as baseline.json is empty (M172's sweep); until then the baseline above bounds it.
      if (BASELINE_EMPTY) assert.deepEqual(v.found, {}, `${page.id} shows engine words with technical details off`);
    } finally {
      await technical.setShowTechnical(true);
      await settle(1);
    }
    assert.equal(document.documentElement.classList.contains(technical.HIDE_CLASS), false);
  });
}

test("every page in the baseline is a page this test draws", () => {
  const known = new Set(PAGES.map((p) => p.id));
  const stray = Object.keys(baseline).filter((id) => !known.has(id));
  assert.deepEqual(stray, [], "baseline.json names a page that no longer exists; remove it");
});

test("the baseline: what each page still shows (the to-do list for M172)", () => {
  const next =
    mode === "init"
      ? Object.fromEntries(Object.entries(shown).filter(([, terms]) => Object.keys(terms).length))
      : mode === "lower"
        ? lowered(baseline, shown)
        : baseline;
  if (mode) fs.writeFileSync(BASELINE, `${JSON.stringify(sorted(next), null, 2)}\n`);
  const total = Object.values(next).reduce((n, t) => n + Object.values(t).reduce((a, b) => a + b, 0), 0);
  process.stdout.write(`\nVisible-text baseline: ${Object.keys(next).length} pages, ${total} occurrences\n${table(next)}\n`);
  assert.ok(true);
});

function sorted(object) {
  return Object.fromEntries(
    Object.keys(object)
      .sort()
      .map((page) => [page, Object.fromEntries(Object.entries(object[page]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])))]),
  );
}

test("a page that shows an engine word outside technical details fails (planted violations)", async () => {
  // A real drawn page with one planted sentence outside technical details.
  await visit("#/");
  const planted = document.createElement("p");
  planted.textContent = "Uplift by decile";
  $("#app").appendChild(planted);
  const failing = violations(visibleText(document.body), baseline.home || {});
  assert.equal(failing.ok, false, "a planted 'uplift' on Home must fail the check");
  assert.deepEqual(
    failing.unlisted.filter((u) => u.term === "uplift"),
    [{ term: "uplift", count: 1 }],
    "the planted word is not on Home's allow-list",
  );
  assert.match(describe("home", failing), /home: shows "uplift" 1x and it is not on this page's allow-list/);
  planted.remove();

  // The same word inside technical details is left alone, in each way a section can be marked.
  const holder = document.createElement("div");
  holder.innerHTML = [
    '<details class="tech"><summary>Details</summary><p>uplift</p></details>',
    '<details><summary>Technical details</summary><p>uplift AUUC</p></details>',
    '<details class="tech"><summary>Technical details</summary><p>uplift</p></details>',
    '<section class="card"><details><summary>Details</summary><div data-technical><p>uplift, SHAP</p></div></details></section>',
    '<section data-technical><p>uplift, X-learner</p></section>',
    "<p hidden>uplift</p>",
    '<p style="display: none">uplift</p>',
    "<script>uplift</script><style>.uplift{}</style>",
  ].join("");
  document.body.appendChild(holder);
  assert.equal(violations(visibleText(holder), {}).ok, true, "technical, hidden, script and style text is not visible text");
  // ... and visible words that sit around it still count, including a word only in an attribute a person reads.
  holder.insertAdjacentHTML("beforeend", '<input placeholder="Search by holdout"><button aria-label="Treat list" title="x">Go</button>');
  const attrs = violations(visibleText(holder), {});
  assert.deepEqual(attrs.unlisted.map((u) => u.term).sort(), ["holdout", "treat list"]);
  // The <summary> of a technical <details> is on screen even when it is closed, so it is read, except the two
  // generic labels; its folded body is still left out.
  holder.innerHTML =
    '<details class="tech"><summary>Uplift settings</summary><p>holdout</p></details>' +
    '<details class="tech"><summary>Settings used for this run</summary><p>psi</p></details>' +
    '<details class="tech" hidden><summary>Uplift settings</summary></details>';
  assert.deepEqual(violations(visibleText(holder), {}).found, { uplift: 1, run: 1 });
  holder.remove();
});

test("with the switch off the run page's model report and Model health links are hidden; the campaign value report stays (M180)", async () => {
  await visit(`#/uc/${UC}/run/${TRAIN_RISK}`);
  const report = $(`a.uc-link[href="#/pilot/view/results/${UC}"]`);
  assert.ok(report, "a finished training run links to its model results report");
  assert.ok(report.hasAttribute("data-technical"), "and the link is marked technical");
  assert.match(visibleText(document.body, { mode: "off" }), /Results report/, "with the switch on it is on screen");
  const health = $('a.uc-link[href="#/monitoring/alerts"]');
  if (health) assert.ok(health.hasAttribute("data-technical"), "Model health is a technical link too");
  try {
    await technical.setShowTechnical(false);
    await settle(2);
    const off = visibleText(document.body, { mode: "off" });
    assert.doesNotMatch(off, /Results report/, "with the switch off the model report link is not on screen");
    assert.doesNotMatch(off, /Model health/);
    assert.match(off, /Ranking quality/, "the run's own verdict stays");
    // Between hidden blocks no lone arrow is left over.
    const arrows = [...document.querySelectorAll(".flow > .arrow")].filter((a) => !hiddenOnScreen(a));
    assert.deepEqual(arrows.length, 0, "no arrow is left between hidden blocks");
    // A scoring run's "Campaign value report" is for marketers: it stays.
    await visit(`#/uc/${UC}/run/${SCORE_RISK}`);
    const value = $('a.uc-link[href^="#/pilot/value/"]');
    if (value) {
      assert.equal(value.hasAttribute("data-technical"), false);
      assert.equal(hiddenOnScreen(value), false, "the campaign value report link stays on screen");
    }
  } finally {
    await technical.setShowTechnical(true);
  }
});

test("the uplift workbench's run page: its Data, Model and Contact list blocks are technical; Campaign results is not (M180)", async () => {
  for (const [run, technicalHrefs, plain] of [
    [TRAIN_UPLIFT, [`#/uc/${UC}/data/${TRAIN_UPLIFT}`, `#/uplift/${UC}/model/${TRAIN_UPLIFT}`, `#/uplift/${UC}/output/${TRAIN_UPLIFT}`], null],
    [SCORE_UPLIFT, [`#/uc/${UC}/data/${SCORE_UPLIFT}`, `#/uplift/${UC}/output/${SCORE_UPLIFT}`], `#/campaign/${UC}/${SCORE_UPLIFT}`],
  ]) {
    await visit(`#/uplift/${UC}/run/${run}`);
    for (const href of technicalHrefs) {
      const a = $(`a.block[href="${href}"]`);
      assert.ok(a, `the uplift run page links to ${href}`);
      assert.ok(a.hasAttribute("data-technical"), `${href} is marked technical`);
    }
    const blocks = [...document.querySelectorAll(".flow a.block")];
    const plainBlock = blocks.find((a) => !a.hasAttribute("data-technical"));
    if (plain) assert.ok(plainBlock && /Campaign results/.test(plainBlock.textContent), "Campaign results stays unmarked");
    else assert.equal(plainBlock, undefined, "after a training run every block is technical");
    try {
      await technical.setShowTechnical(false);
      await settle(2);
      for (const href of technicalHrefs) assert.ok(hiddenOnScreen($(`a.block[href="${href}"]`)), `${href} is hidden off`);
      if (plainBlock) assert.equal(hiddenOnScreen(plainBlock), false, "Campaign results stays on screen");
      const arrows = [...document.querySelectorAll(".flow > .arrow")].filter((a) => !hiddenOnScreen(a));
      assert.equal(arrows.length, 0, "no arrow is left between hidden blocks");
      const off = visibleText(document.body, { mode: "off" });
      // Only the Campaign results block (a scoring run's) still offers "View details".
      assert.equal((off.match(/View details/g) || []).length, plain ? 1 : 0, off);
      if (!plain) assert.doesNotMatch(off, /Uplift pipeline/, "nothing is left under the pipeline caption, so it goes too");
    } finally {
      await technical.setShowTechnical(true);
      await settle(1);
    }
  }
});

test("with the switch off a technical page still opens at its address: only the links to it are hidden (M180)", async () => {
  try {
    await technical.setShowTechnical(false);
    await visit(`#/uc/${UC}/model/${TRAIN_RISK}`);
    assert.match(visibleText(document.body, { mode: "off" }), /How well the model predicts/);
    assert.ok($("[data-beeswarm]"), "the Model page is drawn in full");
    await visit(`#/uc/${UC}/run/${TRAIN_RISK}`);
    const link = $(`a.block[href="#/uc/${UC}/model/${TRAIN_RISK}"]`);
    assert.ok(link, "the link is still in the page");
    assert.ok(hiddenOnScreen(link), "and hidden on screen");
    assert.match(visibleText(document.body, { mode: "off" }), /Ranking quality/, "the run's own verdict stays");
  } finally {
    await technical.setShowTechnical(true);
  }
});

test('"Show technical details" off: what the real CSS hides, in every way a section can be marked (M180)', async () => {
  await visit("#/");
  const holder = document.createElement("div");
  holder.innerHTML = [
    '<details class="tech"><summary>Uplift settings</summary><p>holdout</p></details>',
    '<section data-technical><p>X-learner</p></section>',
    '<a class="block" href="#/uc/x/model/r" data-technical>Model: AUUC 0.2</a>',
    // drawn by hand, with no class: ui/technical.js marks it (data-technical-details) as it arrives
    "<details><summary>Technical details</summary><p>SHAP</p></details>",
    '<p>Campaign value</p>',
  ].join("");
  $("#app").appendChild(holder);
  await settle(2);
  assert.ok(holder.querySelector("details:not(.tech)").hasAttribute("data-technical-details"));
  try {
    await technical.setShowTechnical(false);
    const off = violations(visibleText(holder, { mode: "off" }), {});
    assert.deepEqual(off.found, {}, "every technical mark is hidden by the CSS, summary and all");
    assert.match(visibleText(holder, { mode: "off" }), /Campaign value/);
    // The "off" collector reads the style sheet, not the marks: with the switch on, the same marks are on screen.
    await technical.setShowTechnical(true);
    const on = violations(visibleText(holder, { mode: "off" }), {});
    for (const term of ["uplift", "holdout", "auuc", "shap"]) {
      assert.ok(on.found[term] >= 1, `"${term}" is read once its section is shown: ${JSON.stringify(on.found)}`);
    }
  } finally {
    await technical.setShowTechnical(true);
    holder.remove();
  }
});

test("the off-mode table, and whether the zero-terms assertion is active (it is once baseline.json is empty)", () => {
  const total = Object.values(shownOff).reduce((n, t) => n + Object.values(t).reduce((a, b) => a + b, 0), 0);
  const status = BASELINE_EMPTY ? "ACTIVE (baseline.json is empty)" : "waiting for M172 to empty baseline.json";
  process.stdout.write(`\nWith "Show technical details" off: ${total} occurrences; zero-terms assertion ${status}\n`);
  assert.equal(Object.keys(shownOff).length, PAGES.length, "every page was read with the switch off");
});

test("a page may not get worse than its baseline, and an improvement is fine", () => {
  const text = "Recipe, recipes and a Recipe. Treat list. PSI 0.2";
  assert.deepEqual(violations(text, { recipe: 3, "treat list": 1, psi: 1 }).found, { recipe: 3, "treat list": 1, psi: 1 });
  assert.equal(violations(text, { recipe: 3, "treat list": 1, psi: 1 }).ok, true);
  const worse = violations(text, { recipe: 2, "treat list": 1, psi: 1 });
  assert.deepEqual(worse.worse, [{ term: "recipe", count: 3, allowed: 2 }]);
  assert.equal(violations(text, { recipe: 5, "treat list": 4, psi: 9 }).ok, true, "fewer than allowed is not a failure");
  assert.equal(violations(text, { recipe: 3, "treat list": 1 }).ok, false, "a term missing from the list fails");
});

test("the baseline can only be lowered, never raised", () => {
  assert.deepEqual(lowered({ p: { a: 5, b: 2 }, q: { c: 1 } }, { p: { a: 3, b: 4 }, q: {} }), { p: { a: 3, b: 2 } });
});
