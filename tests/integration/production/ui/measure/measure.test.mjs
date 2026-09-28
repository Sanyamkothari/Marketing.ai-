/* Step 4, "Measure the campaign" (Plan H M83), in jsdom against REAL bodies (PB_FIXTURES, written by
   tests/integration/test_measure_ui.py): the Results view of a finished scoring run (ui/usecase.js)
   with the measure module loaded, before and after an outcomes file is uploaded, a campaign whose
   outcome window is not over, one too small to learn from, learning who to contact next time, and an
   operational use case that has no step 4 at all. */
import fs from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, ROOT, fixture, installPage, until } from "../harness.mjs";

const uc = fixture("use_case");
const ops = fixture("use_case_ops");
const run = fixture("run");
const small = fixture("run_small");
const learned = fixture("learn");
const early = { ...run, run_id: "r_20260928_3b0000e1" };
const hostile = { ...run, run_id: "r_20260928_3b0000e2" };

const answers = {
  [run.run_id]: "measure_before",
  [small.run_id]: "measure_small",
  [early.run_id]: "measure_early",
};
const hostileView = (() => {
  const view = fixture("measure_after");
  view.outcomes.file_name = '<img src=x onerror="window.pwned=1">.csv';
  view.verdict.detail = "<b>bold</b>";
  return view;
})();

const server = ({ method, path }) => {
  const m = path.match(/^\/runs\/([^/]+)\/measure(\/learn)?$/);
  if (m && method === "GET") {
    if (m[1] === hostile.run_id) return { status: 200, body: hostileView };
    return { status: 200, body: fixture(answers[m[1]]) };
  }
  if (m && method === "POST" && m[2]) {
    answers[m[1]] = "measure_learned";
    return { status: 202, body: learned };
  }
  if (m && method === "POST") {
    answers[m[1]] = "measure_after";
    return { status: 200, body: fixture("measure_after") };
  }
  if (method === "POST" && path === "/uploads") return { status: 201, body: fixture("upload_outcomes") };
  return null;
};
const { w, calls } = installPage(server, { hash: `#/uc/${uc.id}/run/${run.run_id}` });
// index.html's own stylesheets, first in <head> as on the page (before any module injects its rules),
// so computed styles see the shared `.card h3` / `.block .lab` rules with the modules' rules after them.
{
  const html = fs.readFileSync(new URL("ui/index.html", ROOT), "utf8");
  const style = w.document.createElement("style");
  style.id = "page-styles";
  style.textContent = [...html.matchAll(/<style>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n");
  w.document.head.appendChild(style);
}

await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/uplift/index.js");
await import("../../../../../ui/modules/measure/index.js");
const { useCaseHtml, useCaseState } = await import("../../../../../ui/usecase.js");

const app = $("#app");
/** app.js's paint of a run's Results: the whole screen from the use case and the run. */
function show(useCase, scoring) {
  const s = useCaseState(useCase);
  s.view = "results";
  s.detail = { run: scoring };
  app.innerHTML = useCaseHtml(useCase, s);
}
const panel = () => $("[data-measure-panel]");
const pct = (fraction) => `${(fraction * 100).toFixed(1).replace(/0+$/, "").replace(/\.$/, "")}%`;
const mainText = () => {
  const copy = panel().cloneNode(true);
  for (const details of copy.querySelectorAll("details")) details.remove();
  return copy.textContent;
};

test("a scoring run's Results end with step 4: a fourth block and the step, with one upload button", async () => {
  show(uc, run);
  const blocks = $$(".flow .block");
  assert.equal(blocks.length, 4);
  assert.equal(blocks[3].dataset.action, "measure");
  assert.match(blocks[3].textContent, /^MeasureAfter the campaign/);
  assert.equal($('[data-action="campaign-results"]'), null, "step 4 replaces the old Campaign results block");
  assert.ok(panel(), "the step is drawn under the flow");
  await until(() => $(".measure-lead"), 2000, "the step's first answer");
  assert.equal($("#measure-h").textContent, "Measure the campaign");
  const held = fixture("measure_before").held_back.toLocaleString("en-US");
  assert.equal(
    $(".measure-lead").textContent,
    `After the campaign, upload who responded to see what contacting them changed, compared with the ${held} customers held back.`,
  );
  assert.equal($$("[data-measure-file]").length, 1, "one upload button");
  assert.equal($(".measure-upload").textContent, "Upload outcomes");
  assert.equal($(".measure-result"), null, "no result before any outcome, and no number invented");
});

test("the use-case page's rough edges (UI_AUDIT §8.4 items 5-8)", async () => {
  show(uc, run);
  await until(() => $(".measure-lead"), 2000, "the step's first answer");
  const css = (el, prop) => w.getComputedStyle(el).getPropertyValue(prop);
  // 8: no "Also: target with uplift ›" in the use case's header; uplift is this step and a Settings entry
  assert.equal($(".head .head-actions a.related"), null, "no related link in the header");
  assert.doesNotMatch($(".head").textContent, /target with uplift/i);
  assert.equal($('.head a[href^="#/uplift"]'), null, "nothing in the header opens the uplift workbench");
  // 5: the Measure block and panel carry no stray number (Guided setup's 1-3 are its own)
  const block = $('[data-action="measure"]');
  assert.doesNotMatch(block.querySelector(".lab").textContent, /\d/, "the block's label has no number");
  assert.doesNotMatch(block.querySelector(".go").textContent, /\d/, "nor its link");
  assert.doesNotMatch($("#measure-h").textContent, /\d/, "the panel's heading has no number");
  // 6: the block's state sits under its label, not beside it
  const lab = block.querySelector(".lab");
  assert.deepEqual([...lab.children].map((c) => c.textContent), ["Measure", "After the campaign"]);
  assert.equal(css(lab, "flex-direction"), "column", "the state goes under the label");
  assert.equal(css(lab, "align-items"), "flex-start");
  // 6: the DATA block's file name is cut with an ellipsis on one line, whole in its tooltip
  const name = $$(".flow .block")[0].querySelector(".val");
  assert.ok(run.file_name, "the fixture run names its file");
  assert.equal(name.textContent, run.file_name);
  assert.equal(name.getAttribute("title"), run.file_name);
  assert.equal(css(name, "white-space"), "nowrap");
  assert.equal(css(name, "overflow"), "hidden");
  assert.equal(css(name, "text-overflow"), "ellipsis");
  assert.equal($$(".flow .block")[1].querySelector(".val").getAttribute("title"), null, "only the file name");
  // 7: the panel's heading starts where its text does: the card pads both, the heading adds no more
  const heading = $("#measure-h");
  // (jsdom leaves a property no rule sets empty: that is the initial 0)
  const left = (el) => ["padding-left", "margin-left"].map((p) => css(el, p) || "0px");
  assert.equal(css(panel(), "padding-left"), "24px");
  assert.deepEqual(left(heading), ["0px", "0px"], "no extra left padding on the heading");
  assert.deepEqual(left(heading), left($(".measure-lead")), "the heading and the text share one left edge");
});

test("uploading outcomes shows one big plain result from the engine's report, the statistics folded", async () => {
  const input = $("[data-measure-file]");
  const file = new w.File(["customer_id,reactivated_90d\n1,1\n"], "outcomes.csv", { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await until(() => $(".measure-big"), 2000, "the measured result");
  const posted = calls.filter((c) => c.method === "POST").map((c) => c.path);
  assert.deepEqual(posted, ["/uploads", `/runs/${run.run_id}/measure`]);
  const upload = fixture("upload_outcomes");
  assert.deepEqual(calls.at(-1).body, { upload_id: upload.upload_id }, "only the file: the column is found");

  const after = fixture("measure_after");
  assert.equal($(".measure-big").textContent, after.verdict.headline);
  assert.match(after.verdict.headline, /^The campaign added about [\d,]+ conversions$/);
  assert.equal($(".measure-sub").textContent, after.verdict.detail);
  const rates = $$(".measure-rates li").map((li) => li.textContent);
  assert.deepEqual(rates, [
    `Contacted${pct(after.report.treated_rate)} responded${after.report.treated_rows.toLocaleString("en-US")} customers`,
    `Held back${pct(after.report.control_rate)} responded${after.report.control_rows.toLocaleString("en-US")} customers`,
  ]);
  for (const jargon of [/confidence/i, /\bCI\b/, /p-value/i, /p = /, /interval/i, /incrementality/i, /champion/i]) {
    assert.doesNotMatch(mainText(), jargon, `no statistics jargon on the main view: ${jargon}`);
  }
  const details = $(".measure-more");
  assert.equal(details.open, false, "the statistics are folded away");
  assert.ok(details.textContent.includes(after.report.summary), "the engine's own sentence is under Details");
  assert.equal($('[data-measure-jump] .bstate').textContent, "Measured");
  assert.equal($("[data-measure-learn]").textContent, "Learn who to contact next time");
});

test("a repaint of the whole screen keeps the measured result", () => {
  show(uc, run);
  assert.equal($(".measure-big").textContent, fixture("measure_after").verdict.headline);
});

test("Learn who to contact next time starts an uplift model and links to its contact list", async () => {
  $("[data-measure-learn]").click();
  await until(() => $("[data-measure-treat]"), 2000, "the learned model's link");
  assert.ok(calls.some((c) => c.method === "POST" && c.path === `/runs/${run.run_id}/measure/learn`));
  assert.equal($("[data-measure-treat]").getAttribute("href"), `#/uplift/${uc.id}/output/${learned.run_id}`);
  assert.equal($("[data-measure-treat]").textContent, "See who to contact next time ›");
});

test("an outcome window that is not over says so, with no rate", async () => {
  show(uc, early);
  await until(() => $(".measure-big"), 2000, "the early result");
  assert.equal($(".measure-big").textContent, "Outcome window not over yet");
  assert.equal($("[data-measure-verdict]").dataset.measureVerdict, "too_early");
  assert.equal($(".measure-rates"), null);
  assert.equal($("[data-measure-learn]"), null);
});

test("a campaign too small to learn from says how much more it needs", async () => {
  show(uc, small);
  await until(() => $("[data-measure-not-enough]"), 2000, "the not-enough message");
  assert.equal($("[data-measure-not-enough]").textContent, fixture("measure_small").learn.reason);
  assert.match($("[data-measure-not-enough]").textContent, /^To learn who to contact next time, each group needs at least 1,000 customers/);
  assert.equal($("[data-measure-learn]"), null);
});

test("every string from the API is escaped", async () => {
  show(uc, hostile);
  await until(() => $(".measure-big"), 2000, "the hostile result");
  assert.equal($(".measure-sub").textContent, "<b>bold</b>");
  assert.equal($(".measure-sub b"), null);
  assert.equal($(".measure-more img"), null);
  assert.equal(w.pwned, undefined);
});

test("an operational use case has no step 4 and no campaign block", () => {
  show(ops, { ...run, use_case_id: ops.id, use_case_name: ops.name });
  assert.equal($$(".flow .block").length, 3);
  assert.equal(panel(), null);
  assert.equal($('[data-action="measure"]'), null);
  assert.equal($('[data-action="campaign-results"]'), null);
});
