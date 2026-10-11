/* Plan M, M170 (DEC-1600): the visible-text check.

   Every main page is drawn in jsdom, as the browser draws it (app.js and every module in the order of
   ui/index.html), against the API answers recorded from the app running on the validated Hillstrom demo
   (tests/fixtures/capture_ui_demo.py -> tests/fixtures/ui_demo/api.json). The text a person can read is
   collected (collect.mjs: technical sections, hidden elements, scripts and styles are left out;
   aria-label, title and placeholder count) and searched for the glossary's never-shown words
   (ui/labels.js).

   Each page has an allow-list of today's violations, the BASELINE (baseline.json: page, term, count). A page
   fails when it shows a term that is not on its list, or shows more of one than the list allows, so nothing
   gets worse. M172 (DEC-1602) emptied the file: it stays as an empty object, so any never-shown word outside
   technical details now fails its page. A name the person supplied (a file name, a campaign label) is marked
   `data-user-text` (ui/labels.js `userText`) and left out, as their words are not ours.

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
    guard to finish its own task. (M172 did edit three: two goal names that Plan M section 4.2 renamed, and the
    schedules guard, which contained the never-shown word "run".) */
export const PAGES = [
  { id: "home", hash: "#/", expect: /Customer Lifecycle/ },
  { id: "results", hash: "#/results", expect: /Needs your attention/ },
  { id: "setup-guided-win-back", hash: `#/uc/${WIN}`, expect: /Guided setup/ },
  { id: "setup-manual-win-back", hash: `#/uc/${WIN}`, steps: [click('[data-setup-tab="manual"]')], expect: /Upload CSV, Excel or Parquet/ },
  {
    id: "setup-manual-win-back-score",
    hash: `#/uc/${WIN}`,
    steps: [click('[data-setup-tab="manual"]'), click('.seg button[data-mode="score"]')],
    expect: /Win back lapsed customers/,
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
    expect: /Choose the best e-mail for each customer/,
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
  // The workbench's own setup screens, one click from the index (M172 review: they were not swept).
  { id: "uplift-setup", hash: `#/uplift/${UC}`, expect: /Upload CSV, Excel or Parquet/, selector: "[data-umode]" },
  { id: "uplift-setup-score", hash: `#/uplift/${UC}/score`, expect: /Upload CSV, Excel or Parquet/, selector: "[data-umode=score]" },
  // M180: after a training run every block (and its "How well it finds..." line) is technical, so the guard is the
  // run's own next step, and the Model block is asserted by selector.
  {
    id: "uplift-result-training",
    hash: `#/uplift/${UC}/run/${TRAIN_UPLIFT}`,
    expect: /Make a contact list with this model/,
    selector: 'a.block[data-technical][href*="/model/"]',
  },
  { id: "uplift-result-scoring", hash: `#/uplift/${UC}/run/${SCORE_UPLIFT}`, expect: /Contacted vs not contacted/ },
  { id: "campaigns-scored-lists", hash: "#/monitoring/runs", expect: /Every list of customers/ },
  { id: "add-outcomes", hash: `#/monitoring/runs/${SCORE_RISK}`, expect: /What really happened to the customers/ },
  { id: "audit-campaign", hash: "#/audit", expect: /Measure a campaign another tool ran/ },
  { id: "model-health", hash: "#/monitoring/alerts", expect: /Problems Marketing AI found on its own/ },
  { id: "schedules", hash: "#/monitoring/schedules", expect: /on their own/ },
  { id: "cost", hash: "#/cost", expect: /Estimates at AWS/ },
  { id: "approvals", hash: "#/approvals", expect: /Models waiting for your approval/ },
  { id: "data-request-kit", hash: "#/pilot/kit", expect: /Ask your data team/ },
  // Plan M M175: new pages start with an EMPTY allow-list (no entry in baseline.json): they may show no never-shown word.
  { id: "overview", hash: "#/overview", expect: /Needs your attention[\s\S]*Value proven to date/, selector: "[data-attention]" },
  // Plan M, M179 (DEC-1609): the Data page. New pages carry no allow-list: they must not show a never-shown word.
  { id: "data-sources", hash: "#/data", expect: /Where your customer data comes from/, selector: "[data-dt-source='file']" },
  { id: "data-check", hash: "#/data/check", expect: /Data Doctor looks at your files/ },
  // Plan M M176 (DEC-1606): the Campaigns list and every tab of both campaigns' pages, with empty allow-lists.
  { id: "campaigns-list", hash: "#/campaigns", expect: /Every campaign: where it stands/, selector: "[data-campaigns-list]" },
  ...[
    ["conversion", C_CONVERSION],
    ["spend", C_SPEND],
  ].flatMap(([name, id]) => [
    { id: `campaign-${name}-overview`, hash: `#/campaigns/${id}/overview`, expect: /Where it stands/, selector: "[data-overview]" },
    { id: `campaign-${name}-audience`, hash: `#/campaigns/${id}/audience`, expect: /to contact, out of/, selector: "[data-audience-search]" },
    { id: `campaign-${name}-results`, hash: `#/campaigns/${id}/results`, expect: /Open the full report/, selector: "[data-results-measured]" },
    { id: `campaign-${name}-technical`, hash: `#/campaigns/${id}/technical`, expect: /Choose the best e-mail/, selector: "[data-technical-tab]" },
  ]),
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

/* Plan M final integration: the same check in the CAMPAIGNS layout (M174's sidebar shell), where "Show technical
   details" starts off (M180, technical.js `defaultShowTechnical`). Every page here that the campaigns layout keeps at
   its own address (`ui/layout.js` targetFor is null; a redirected one is drawn at its target's own entry) is drawn
   once inside the sidebar shell, right after its classic tests (a goal's setup page remembers its state, so it is
   drawn from the state the classic pass drew it from), and read twice: with the switch on nothing beyond the baseline
   (which is empty), and with it off (the layout's default) no never-shown term at all. */
const layoutModule = await import("../../../../../ui/layout.js");
const CAMPAIGN_LAYOUT_PAGES = PAGES.filter((page) => !page.after && layoutModule.targetFor(page.hash) === null);
const CAMPAIGN_LAYOUT_IDS = new Set(CAMPAIGN_LAYOUT_PAGES.map((page) => page.id));

test("the campaigns-layout sweep covers every place of the sidebar and every Phase 2 page", () => {
  const ids = new Set(CAMPAIGN_LAYOUT_PAGES.map((p) => p.id));
  for (const id of ["overview", "campaigns-list", "reports-value-proof", "reports-check", "reports-models", "data-sources", "data-check", "settings", "approvals"]) {
    assert.ok(ids.has(id), `${id} is drawn in the campaigns layout`);
  }
  assert.ok(CAMPAIGN_LAYOUT_PAGES.filter((p) => p.id.startsWith("campaign-")).length >= 8, "both campaigns, every tab");
});

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

  if (CAMPAIGN_LAYOUT_IDS.has(page.id)) {
    test(`${page.id} in the campaigns layout: no engine word with technical details on, none at all with them off (the layout's default)`, async () => {
      missing.clear();
      // Away from the page first, so the redraw the layout change asks for is over before the page is drawn.
      await visit("#/__between");
      layoutModule.setUiLayout("campaigns");
      try {
        // Drawn with the switch on first, as the classic pass draws it, so the page's guard and its steps (some click
        // what only the analyst's view shows) work unchanged; then read again with the switch off.
        await technical.setShowTechnical(true);
        await settle(2);
        await untilStable();
        await visit(page.hash);
        for (const step of page.steps || []) {
          await step();
          await settle(2);
          await untilStable();
        }
        assert.ok($("#pb-bar nav.sh-side"), `${page.id}: the sidebar shell is drawn`);
        const on = visibleText(document.body);
        assert.match(on, page.expect, `${page.id} did not draw in the campaigns layout (it showed: ${on.slice(0, 1200)})`);
        if (page.selector) assert.ok($(page.selector), `${page.id} did not draw: nothing matches ${page.selector}`);
        const vOn = violations(on, baseline[page.id] || {});
        if (!mode) assert.ok(vOn.ok, describe(`${page.id} (campaigns layout, technical details on)`, vOn));
        assert.equal(await technical.setShowTechnical(false), false);
        await settle(2);
        const visibleMarks = technicalElements(document.body).filter((el) => !hiddenOnScreen(el));
        assert.deepEqual(visibleMarks.map((el) => el.outerHTML.slice(0, 120)), [], `${page.id}: a technical section is on screen`);
        const vOff = violations(visibleText(document.body, { mode: "off" }), baseline[page.id] || {});
        if (!mode) assert.ok(vOff.ok, describe(`${page.id} (campaigns layout, technical details off)`, vOff));
        if (BASELINE_EMPTY) assert.deepEqual(vOff.found, {}, `${page.id} shows engine words in the campaigns layout`);
      } finally {
        layoutModule.setUiLayout("classic");
        await technical.setShowTechnical(true);
        await settle(2);
        await untilStable();
      }
    });
  }
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

test("a name the person supplied is left out, with its title; our words around it still count (M172)", () => {
  const holder = document.createElement("div");
  holder.innerHTML =
    '<p>Uploaded file <span data-user-text title="training.csv">training.csv</span>.</p>' +
    '<p class="sub" data-user-text>Holdout test of the treat list</p>' +
    '<span class="cn">Win back lapsed customers — Oct 2026<span data-user-text>Q4 uplift run</span></span>';
  document.body.appendChild(holder);
  assert.deepEqual(violations(visibleText(holder), {}).found, {}, "the person's own names are theirs, not ours");
  // The mark covers the name only: a sentence of ours beside it is read as before.
  holder.insertAdjacentHTML("beforeend", '<p>Scored by run <span data-user-text>training.csv</span></p>');
  assert.deepEqual(violations(visibleText(holder), {}).found, { run: 1 });
  holder.remove();
});

test("with the switch off a name the person supplied is still theirs, and it is not a technical mark (M172 + M180)", async () => {
  const holder = document.createElement("div");
  holder.innerHTML =
    '<p>Uploaded file <span data-user-text title="uplift_train.csv">uplift_train.csv</span>.</p>' +
    '<span class="cn">Win back lapsed customers — Oct 2026<span data-user-text>Q4 uplift run</span></span>';
  $("#app").appendChild(holder);
  try {
    await technical.setShowTechnical(false);
    await settle(1);
    assert.deepEqual(technicalElements(holder), [], "a person's name is not hidden by the switch");
    assert.ok(holder.querySelectorAll("[data-user-text]").length === 2);
    assert.ok([...holder.querySelectorAll("[data-user-text]")].every((el) => !hiddenOnScreen(el)), "it stays on screen");
    assert.deepEqual(violations(visibleText(holder, { mode: "off" }), {}).found, {}, "and is left out of our words");
    assert.match(visibleText(holder, { mode: "off" }), /Uploaded file[\s\S]*Win back lapsed customers/);
  } finally {
    await technical.setShowTechnical(true);
    holder.remove();
  }
});

test("the Users page's data-user (a user id on our own forms and buttons) does not hide our words (M172 review)", () => {
  const holder = document.createElement("div");
  holder.innerHTML =
    '<form data-user="u_1"><label>Score the run password</label></form>' +
    '<button type="button" data-user="u1">Retrain uplift</button>';
  document.body.appendChild(holder);
  assert.match(visibleText(holder), /Score the run password[\s\S]*Retrain uplift/);
  assert.deepEqual(violations(visibleText(holder), {}).found, { score: 1, run: 1, uplift: 1 });
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
      if (!plain) assert.doesNotMatch(off, /\bSteps\b/, "nothing is left under the Steps caption, so it goes too");
    } finally {
      await technical.setShowTechnical(true);
      await settle(1);
    }
  }
});

test("the Output page's one Technical details disclosure holds the All scores link directly (M171 + M180 integration)", async () => {
  // M180 gave techDetails' <details> an attribute; pages.js strips that wrapper to build its own disclosure, and a
  // stale match left an unclosed inner <details> that swallowed "All scores (for analysts)".
  await visit(`#/uc/${UC}/output/${SCORE_RISK}`);
  const outer = $("details.tech[data-tech]");
  assert.ok(outer, "the Output page has its Technical details disclosure");
  assert.equal(outer.querySelector("details"), null, "no disclosure nested inside it");
  const link = outer.querySelector('a[data-download="scores"]');
  assert.ok(link, "the scored list is offered inside it");
  assert.equal(link.closest("details"), outer, "one click on Technical details reaches it");
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
