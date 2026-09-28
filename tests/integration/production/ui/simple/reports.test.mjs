/* Plan H (UI_AUDIT §8.4 items 13 and 14): the screens left under Results and Settings - the Reports
   hub (`#/pilot`) and the data request kit (`#/pilot/kit`) - speak of one company and one journey:
   no Client or Industry column, "your data team" instead of "your client", plain "Feedback" instead
   of "the pilot team"; and the hub offers "Open the latest report" only once a report exists. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const world = { runs: fixture("runs"), waiting: null, models: null, roi: {} };
const { w } = await installWholePage({ hash: "#/pilot", world });

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
const headers = (card) => $$(`[data-pe-card="${card}"] thead th`).map((th) => text(th));
const [, scoreRun] = fixture("runs").runs;

async function open(hash, ready) {
  w.location.hash = "#/";
  await settle(3);
  w.location.hash = hash;
  await until(ready, 3000, hash);
  await settle(6);
}

const hubReady = () => $('[data-pe-card="value"] tbody tr') && $("[data-pe-export]");

test("no approved model and a campaign not measured yet: no 'Open the latest report'", async () => {
  world.roi = { [scoreRun.run_id]: fixture("roi_not_measured") };
  await open("#/pilot", () => hubReady() && $('[data-pe-card="results"] .empty-state'));
  assert.equal($("main h1").textContent, "Reports");
  assert.match(text($('[data-pe-card="results"]')), /No approved model yet/);
  assert.equal($$('[data-pe-card="value"] tbody tr').length, 1, "the scored campaign is listed");
  assert.doesNotMatch(text($("main")), /Open the latest report/, "no report exists yet");
  assert.ok($("[data-pe-head-actions]"), "the slot stays empty");
});

test("the hub has no Client column and no pilot-team words", () => {
  assert.deepEqual(headers("value"), ["Campaign", "Scored on", "Report"]);
  assert.doesNotMatch(text($("main")), /Client|pilot team/i);
  const foot = text($("[data-pe-export]"));
  if (foot) assert.match(foot, /^Feedback: Export all feedback$/);
});

test("once the campaign is measured, its value is the latest report", async () => {
  world.roi = { [scoreRun.run_id]: fixture("roi_measured") };
  await open("#/pilot", () => hubReady() && $("main .btn.primary"));
  const primary = $("main .btn.primary");
  assert.equal(text(primary), "Open the latest report");
  assert.equal(primary.getAttribute("href"), `#/pilot/value/${scoreRun.run_id}`);
});

test("with an approved model, its results report is offered", async () => {
  world.models = fixture("models_champion");
  world.roi = { [scoreRun.run_id]: fixture("roi_not_measured") };
  await open("#/pilot", () => hubReady() && $("main .btn.primary"));
  const primary = $("main .btn.primary");
  assert.equal(text(primary), "Open the latest report");
  const useCase = fixture("models_champion").versions[0].version.use_case_id;
  assert.equal(primary.getAttribute("href"), `#/pilot/view/results/${useCase}`);
  world.models = null;
});

test("the data request kit asks the data team, and lists use cases without an Industry column", async () => {
  await open("#/pilot/kit", () => $('[data-pe-card="build"] tbody tr') && $('[data-pe-card="kit"] .pe-body'));
  const page = text($("main"));
  assert.match(page, /Ask your data team for the right tables/);
  assert.match(page, /What to ask your data team for/);
  assert.doesNotMatch(page, /your client|Industry|Every business/i);
  assert.deepEqual(headers("build"), ["Use case", "Set up"]);
  assert.doesNotMatch(text($('[data-pe-card="readiness"]')), /client/i);
});
