/* Plan M M189 (DEC-1619): the running bar reads the time-weighted estimate, says how long is left, says
   "Finishing up…" when a run takes longer than expected, and falls back to progress_pct for a status
   without an estimate. The statuses are REAL: tests/integration/test_run_progress_ui.py writes them into
   PB_FIXTURES from a training run through the real pipeline (its train stage held on a fake clock), and
   these tests draw the goal's Running card, the uplift workbench's running screen and the New campaign
   wizard's learning step from them, all through the one helper (ui/run_progress.js). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { fixture, installPage } from "../harness.mjs";

installPage(() => null);

const { etaText, runProgress } = await import("../../../../../ui/run_progress.js");
const { runningCard } = await import("../../../../../ui/usecase.js");
const { upliftScreenHtml } = await import("../../../../../ui/modules/uplift/views.js");
const { runPageHtml } = await import("../../../../../ui/modules/campaigns/run_views.js");
const { findNeverShown } = await import("../../../../../ui/labels.js");

const mid = fixture("status_mid"); // three quarters through the train stage's time budget
const overrun = fixture("status_overrun"); // twice the train stage's time budget
const legacy = fixture("status_legacy"); // the same moment, as a status.json written before M189
const done = fixture("status_done");
const midEta = etaText(mid.eta_seconds);

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const card = (status) => runningCard({ title: "Building your model…", groups: [], progress: runProgress(status) });
const uc = { id: "win-back", name: "Win back", marker: "P", stars: "", type_label: "", lifecycle_stage: "", ai_type: "predictive" };
const upliftRunning = (status) =>
  upliftScreenHtml(uc, { view: "running", detail: { run: { run_id: "r", mode: "train", state: "running" }, status }, runs: [] });

test("the fixtures are the real thing: progress_pct sits at 50 while the estimate has moved on", () => {
  assert.equal(mid.progress_pct, 50);
  assert.match(midEta, /^About \d+ minutes left$/);
  assert.ok(mid.estimated_pct > 60, "three quarters through the longest stage is well past half");
  assert.equal(typeof mid.eta_seconds, "number");
  assert.equal(overrun.eta_seconds, null);
  assert.equal(legacy.estimated_pct, undefined);
});

test("etaText rounds sensibly", () => {
  assert.equal(etaText(null), "");
  assert.equal(etaText(12), "Less than a minute left");
  assert.equal(etaText(70), "About a minute left");
  assert.equal(etaText(185), "About 3 minutes left");
  assert.equal(etaText(9 * 60 + 20), "About 9 minutes left");
  assert.equal(etaText(23 * 60), "About 25 minutes left");
  assert.equal(etaText(59 * 60), "About 1 hour left");
  assert.equal(etaText(80 * 60), "About 1 hour 20 minutes left");
  assert.equal(etaText(2 * 3600 + 120), "About 2 hours left");
});

test("the Running card shows the estimate, its bar and the time left", () => {
  const html = card(mid);
  assert.match(html, new RegExp(`<span class="run-pct">${mid.estimated_pct}%</span>`));
  assert.match(html, new RegExp(`aria-valuenow="${mid.estimated_pct}"`));
  assert.match(html, new RegExp(`style="width:${mid.estimated_pct}%"`));
  assert.ok(!html.includes(`${mid.progress_pct}%`), "not the done-stages count");
  assert.match(text(html), new RegExp(midEta));
  assert.ok(!html.includes("Finishing up"));
});

test("a run past its expected time says Finishing up and its bar moves on its own, with no number", () => {
  const html = card(overrun);
  assert.match(html, /<p class="run-eta" data-run-eta>Finishing up…<\/p>/);
  assert.match(html, /class="run-meter run-meter-busy"[^>]*aria-valuetext="Finishing up"/);
  assert.match(html, /<div class="run-meter-fill run-busy-fill"><\/div>/);
  assert.ok(!/aria-valuenow/.test(html), "an indeterminate bar has no value");
  assert.ok(!html.includes('class="run-pct"'), "no frozen number in the pill");
  assert.ok(!/left/.test(text(html)), "no time left is promised");
});

test("a status without an estimate falls back to progress_pct, as before, with no time left", () => {
  const html = card(legacy);
  assert.match(html, /<span class="run-pct">50%<\/span>/);
  assert.match(html, /aria-valuenow="50"/);
  assert.ok(!html.includes("data-run-eta"));
  // and a bare number, the card's old argument, still draws the same bar
  const bare = runningCard({ title: "t", groups: [], progressPct: 50 });
  assert.match(bare, /<span class="run-pct">50%<\/span>/);
  assert.match(bare, /aria-valuenow="50"/);
});

test("a finished run reads 100 and promises nothing", () => {
  const reading = runProgress(done);
  assert.equal(reading.pct, 100);
  assert.equal(reading.eta, "");
  assert.equal(reading.finishing, false);
});

test("the uplift workbench's running screen uses the same reading", () => {
  const html = upliftRunning(mid);
  assert.match(html, new RegExp(`<span class="run-pct">${mid.estimated_pct}%</span>`));
  assert.match(text(html), new RegExp(midEta));
  assert.match(upliftRunning(overrun), /Finishing up…/);
  assert.match(upliftRunning(legacy), /<span class="run-pct">50%<\/span>/);
});

const running = (pct, eta) =>
  runPageHtml({
    record: { state: "running", use_case_id: "win-back-campaign", steps: [{ key: "train", title: "Learning from your customers", state: "running" }] },
    pct,
    eta_seconds: eta,
    summary: "Learning from your customers.",
  });

test("the campaign progress page uses the same bar and time-left line", () => {
  const html = running(75, 300);
  assert.match(html, /class="run-meter cw-meter"[^>]*aria-valuenow="75"/);
  assert.match(text(html), new RegExp(etaText(300)));
  assert.ok(!running(75, null).includes("data-run-eta"), "no time left, no line");
});

test("every new word passes the visible-text check (no engine words)", () => {
  for (const status of [mid, overrun, legacy]) {
    // The stage rows are drawn from the stub stages' own detail lines, which this milestone does not change
    // (the uplift screen's rows have their own test); what is checked here is what the progress adds.
    const bare = { ...status, stages: [] };
    for (const html of [card(status), upliftRunning(bare), running(status.estimated_pct, status.eta_seconds)]) {
      assert.deepEqual(findNeverShown(text(html.replace(/ title="[^"]*"/g, ""))), {});
    }
  }
  assert.match(card(mid), /Building your model…/);
});

test("the helper brings its own two rules, once, with no new colour and a still bar under reduced motion", () => {
  card(overrun);
  card(mid);
  const sheets = [...document.head.querySelectorAll("style#run-progress-styles")];
  assert.equal(sheets.length, 1, "injected once, however many bars are drawn");
  const css = sheets[0].textContent;
  assert.match(css, /\.run-eta\{[^}]*color:var\(--muted\)/);
  assert.match(css, /@media \(prefers-reduced-motion:no-preference\)\{[^]*animation:run-busy/);
  assert.ok(!/\.run-meter-fill/.test(css), "does not restyle the shared fill");
  assert.ok(!/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i.test(css), "no colour value of its own");
});
