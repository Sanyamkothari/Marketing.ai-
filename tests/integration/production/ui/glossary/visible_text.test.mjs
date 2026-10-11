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
import { visibleText } from "./collect.mjs";
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
  { id: "run-training", hash: `#/uc/${UC}/run/${TRAIN_RISK}`, expect: /Ranking quality[\s\S]*View details/ },
  { id: "data-training", hash: `#/uc/${UC}/data/${TRAIN_RISK}`, expect: /how it was prepared/ },
  { id: "model-training", hash: `#/uc/${UC}/model/${TRAIN_RISK}`, expect: /How well the model predicts/, selector: "[data-beeswarm]" },
  { id: "output-training", hash: `#/uc/${UC}/output/${TRAIN_RISK}`, expect: /Lift in the top 10%/ },
  { id: "run-scoring", hash: `#/uc/${UC}/run/${SCORE_RISK}`, expect: /Download contact list[\s\S]*View details/ },
  { id: "data-scoring", hash: `#/uc/${UC}/data/${SCORE_RISK}`, expect: /how it was prepared/ },
  { id: "model-scoring", hash: `#/uc/${UC}/model/${SCORE_RISK}`, expect: /nothing new to measure/ },
  { id: "output-scoring", hash: `#/uc/${UC}/output/${SCORE_RISK}`, expect: /Customers worth an e-mail/ },
  { id: "campaign-results-scoring", hash: `#/campaign/${UC}/${SCORE_RISK}`, expect: /Campaign results/ },
  { id: "run-uplift-training", hash: `#/uc/${UC}/run/${TRAIN_UPLIFT}`, expect: /E-mail Offer Choice[\s\S]*View details/ },
  { id: "data-uplift-training", hash: `#/uc/${UC}/data/${TRAIN_UPLIFT}`, expect: /how it was prepared/ },
  { id: "model-uplift-training", hash: `#/uplift/${UC}/model/${TRAIN_UPLIFT}`, expect: /Top 10% gain/ },
  { id: "contact-list-uplift-training", hash: `#/uplift/${UC}/output/${TRAIN_UPLIFT}`, expect: /Customers to contact/ },
  { id: "run-uplift-scoring", hash: `#/uc/${UC}/run/${SCORE_UPLIFT}`, expect: /Download contact list[\s\S]*View details/ },
  { id: "contact-list-uplift-scoring", hash: `#/uc/${UC}/output/${SCORE_UPLIFT}`, expect: /Customers to contact/ },
  { id: "campaign-conversion", hash: `#/campaigns/${C_CONVERSION}`, expect: /Plan the test/ },
  { id: "campaign-spend", hash: `#/campaigns/${C_SPEND}`, expect: /Plan the test/ },
  { id: "proof-pack-conversion", hash: `#/pilot/proof/${C_CONVERSION}`, expect: /Value Proof Pack/ },
  { id: "proof-pack-spend", hash: `#/pilot/proof/${C_SPEND}`, expect: /Value Proof Pack/ },
  { id: "reports", hash: "#/pilot", expect: /What the models found/ },
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
  // Plan M M174 (DEC-1604): the campaigns layout's shell - sidebar, bell, Help - around a page with an empty allow-list.
  // `after` puts the classic layout back, so every other page here is drawn as before.
  {
    id: "shell-campaigns",
    hash: `#/pilot/value/${SCORE_RISK}`,
    steps: [async () => (await import("../../../../../ui/layout.js")).setUiLayout("campaigns")],
    after: [async () => (await import("../../../../../ui/layout.js")).setUiLayout("classic")],
    expect: /Overview[\s\S]*Campaigns[\s\S]*Data[\s\S]*Reports[\s\S]*Settings[\s\S]*Value of one extra customer/,
    selector: "#pb-bar nav.sh-side a[href='#/overview']",
  },
];

/** Requests a page may make that the recording does not hold (none today). Anything else fails the page. */
const EXPECTED_UNRECORDED = new Set([]);

const baseline = JSON.parse(fs.readFileSync(BASELINE, "utf8"));
const mode = process.env.GLOSSARY_UPDATE_BASELINE || "";
const dump = process.env.GLOSSARY_DUMP || "";

const { w, state, missing } = installDemoPage({ hash: "#/__start" });
await loadModules();

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
    const drewSelector = !page.selector || Boolean($(page.selector));
    // A page that changed something for the whole app in its steps (the layout, M174) puts it back now.
    for (const step of page.after || []) {
      await step();
      await settle(2);
      await untilStable();
    }
    if (dump) {
      fs.mkdirSync(dump, { recursive: true });
      fs.writeFileSync(path.join(dump, `${page.id}.txt`), text);
    }
    assert.match(text, page.expect, `${page.id} did not draw (it showed: ${text.slice(0, 200)})`);
    if (page.selector) assert.ok(drewSelector, `${page.id} did not draw: nothing matches ${page.selector}`);
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
