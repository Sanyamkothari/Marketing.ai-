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

/** id, where to go, what to do there, and a pattern the page must show (so a blank page cannot pass). */
export const PAGES = [
  { id: "home", hash: "#/", expect: /Customer Lifecycle/ },
  { id: "results", hash: "#/results", expect: /Needs your attention|Value proven|Runs/ },
  { id: "setup-guided-win-back", hash: `#/uc/${WIN}`, expect: /Guided setup/ },
  { id: "setup-manual-win-back", hash: `#/uc/${WIN}`, steps: [click('[data-setup-tab="manual"]')], expect: /Upload CSV, Excel or Parquet/ },
  {
    id: "setup-manual-win-back-score",
    hash: `#/uc/${WIN}`,
    steps: [click('[data-setup-tab="manual"]'), click('.seg button[data-mode="score"]')],
    expect: /Score|score/,
  },
  { id: "setup-guided-hillstrom", hash: `#/uc/${UC}`, expect: /Guided setup/ },
  { id: "setup-manual-hillstrom", hash: `#/uc/${UC}`, steps: [click('[data-setup-tab="manual"]')], expect: /Upload CSV, Excel or Parquet/ },
  { id: "run-training", hash: `#/uc/${UC}/run/${TRAIN_RISK}`, expect: /Training complete/ },
  { id: "data-training", hash: `#/uc/${UC}/data/${TRAIN_RISK}`, expect: /Data/ },
  { id: "model-training", hash: `#/uc/${UC}/model/${TRAIN_RISK}`, expect: /Model/ },
  { id: "output-training", hash: `#/uc/${UC}/output/${TRAIN_RISK}`, expect: /Output/ },
  { id: "run-scoring", hash: `#/uc/${UC}/run/${SCORE_RISK}`, expect: /Scoring complete/ },
  { id: "data-scoring", hash: `#/uc/${UC}/data/${SCORE_RISK}`, expect: /Data/ },
  { id: "model-scoring", hash: `#/uc/${UC}/model/${SCORE_RISK}`, expect: /Model/ },
  { id: "output-scoring", hash: `#/uc/${UC}/output/${SCORE_RISK}`, expect: /contact list|Contact list/ },
  { id: "campaign-results-scoring", hash: `#/campaign/${UC}/${SCORE_RISK}`, expect: /Campaign results/ },
  { id: "run-uplift-training", hash: `#/uc/${UC}/run/${TRAIN_UPLIFT}`, expect: /Training complete/ },
  { id: "data-uplift-training", hash: `#/uc/${UC}/data/${TRAIN_UPLIFT}`, expect: /Data/ },
  { id: "model-uplift-training", hash: `#/uplift/${UC}/model/${TRAIN_UPLIFT}`, expect: /Model/ },
  { id: "contact-list-uplift-training", hash: `#/uplift/${UC}/output/${TRAIN_UPLIFT}`, expect: /contact/i },
  { id: "run-uplift-scoring", hash: `#/uc/${UC}/run/${SCORE_UPLIFT}`, expect: /Scoring complete/ },
  { id: "contact-list-uplift-scoring", hash: `#/uc/${UC}/output/${SCORE_UPLIFT}`, expect: /contact/i },
  { id: "campaign-conversion", hash: `#/campaigns/${C_CONVERSION}`, expect: /Plan the test|campaign/i },
  { id: "campaign-spend", hash: `#/campaigns/${C_SPEND}`, expect: /Plan the test|campaign/i },
  { id: "proof-pack-conversion", hash: `#/pilot/proof/${C_CONVERSION}`, expect: /Value Proof Pack/ },
  { id: "proof-pack-spend", hash: `#/pilot/proof/${C_SPEND}`, expect: /Value Proof Pack/ },
  { id: "reports", hash: "#/pilot", expect: /Reports|reports/ },
  { id: "value-report", hash: `#/pilot/value/${SCORE_RISK}`, expect: /rupees|Value/ },
  { id: "settings", hash: "#/settings", expect: /AI service/ },
  { id: "connections", hash: "#/connections", expect: /Connections/ },
  { id: "data-doctor", hash: "#/doctor", expect: /Data Doctor|Bring your data/ },
  { id: "uplift-workbench", hash: "#/uplift", expect: /Uplift|uplift/ },
  { id: "campaigns-scored-lists", hash: "#/monitoring/runs", expect: /Campaigns|campaigns/ },
  { id: "add-outcomes", hash: `#/monitoring/runs/${SCORE_RISK}`, expect: /outcome/i },
  { id: "audit-campaign", hash: "#/audit", expect: /Audit/ },
  { id: "model-health", hash: "#/monitoring/alerts", expect: /alerts|Alerts|Model health/ },
  { id: "schedules", hash: "#/monitoring/schedules", expect: /Schedules|schedule/ },
  { id: "cost", hash: "#/cost", expect: /Cost|cost/ },
  { id: "approvals", hash: "#/approvals", expect: /approval/i },
  { id: "data-request-kit", hash: "#/pilot/kit", expect: /data request|Data request|Build data/ },
];

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
  const unrecorded = [...missing].filter((k) => !k.includes("/__"));
  if (unrecorded.length) process.stdout.write(`(requests with no recorded answer, answered 404: ${unrecorded.join(", ")})\n`);
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
