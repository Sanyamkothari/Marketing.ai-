/* "Ask your data" (ui/modules/agent/ask.js, Plan I DEC-1255) on a run's Data page, in jsdom, against REAL
   bodies (PB_FIXTURES, written by tests/integration/agent/test_ask_ui.py). The agent module registers the
   panel exactly as index.html's PLAN-G block loads it; the Data page is ui/pages.js's own, given the panels
   the way app.js gives them. Two bodies are derived here from the recorded ones, never invented: a reply
   whose text claims other numbers (the chart must keep the tool's), and labels carrying markup (escaped). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const empty = fixture("ask_empty");
const rated = fixture("ask_rate");
const suppressed = fixture("ask_suppressed");
const off = fixture("ask_off");

const LIAR = "u_liar";
const HOSTILE = "u_hostile";
const liar = (() => {
  const copy = structuredClone(rated);
  copy.session.transcript[copy.session.transcript.length - 1].text = "North converts at 99.9% and South at 0%.";
  return copy;
})();
const hostile = (() => {
  const copy = structuredClone(rated);
  copy.charts[0].bars[0].label = `<img src=x onerror="window.__pwned=1">North`;
  copy.charts[0].column = "<b>region</b>";
  return copy;
})();

const answers = {
  [ids.upload]: { get: empty, post: rated, del: empty },
  [ids.gappy]: { get: empty, post: suppressed },
  [ids.off]: { get: off },
  [LIAR]: { get: empty, post: liar },
  [HOSTILE]: { get: empty, post: hostile },
};
const sent = [];

const server = ({ method, path, body }) => {
  const match = /^\/uploads\/([^/]+)\/ask(\/messages)?$/.exec(path);
  if (!match) return null;
  const flow = answers[match[1]];
  if (!flow) return { status: 404, body: { detail: { code: "UPLOAD_NOT_FOUND", message: "No such upload." } } };
  if (method === "GET") return { status: 200, body: flow.get };
  if (method === "POST" && match[2]) {
    sent.push({ upload: match[1], body });
    return { status: 200, body: flow.post };
  }
  if (method === "DELETE") return { status: 200, body: flow.del };
  return null;
};

const { w } = installPage(server, { hash: "#/uc/x/data/r1" });
const router = await import("../../../../../../ui/modules/router.js");
await import("../../../../../../ui/modules/agent/index.js");
const ask = await import("../../../../../../ui/modules/agent/ask.js");
const { renderPage } = await import("../../../../../../ui/pages.js");
const { fmtPct, fmtInt } = await import("../../../../../../ui/dom.js");

const app = $("#app");
const run = (uploadId) => ({ run_id: "r1", mode: "train", state: "done", upload_id: uploadId, use_case_id: uc.id });

/** The Data page as app.js draws it: the page's own cards, then the registered panels. */
function showDataPage(uploadId) {
  ask.resetAskState();
  const r = run(uploadId);
  app.innerHTML = renderPage("data", uc, r, {}, "", { panelsHtml: router.pagePanelsHtml("data", uc, r) });
}

const panel = () => $("[data-ask-panel]");
const loaded = () => panel() && !panel().querySelector('[role="status"].ask-lead');

async function askQuestion(text) {
  const input = $("[data-ask-input]");
  input.value = text;
  input.dispatchEvent(new w.Event("input", { bubbles: true }));
  $("[data-ask-form]").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $$("[data-ask-chat] .gmsg.bot").length && !$(".gmsg .loading"), 2000, "the reply");
}

test("the panel is on a run's Data page only, for a run made from an upload", () => {
  assert.equal(ask.askApplies("data", uc, run("u1")), true);
  assert.equal(ask.askApplies("model", uc, run("u1")), false);
  assert.equal(ask.askApplies("data", uc, { ...run("u1"), upload_id: null, dataset_id: "d1" }), false);
  assert.match(router.pagePanelsHtml("data", uc, run(ids.upload)), /data-ask-panel/);
  assert.equal(router.pagePanelsHtml("output", uc, run(ids.upload)), "");
});

test("before the first question: the box with the example prompts, and nothing else", async () => {
  showDataPage(ids.upload);
  await until(loaded, 2000, "the panel to load");
  assert.equal(panel().querySelector("h3").textContent, "Ask your data");
  const input = $("[data-ask-input]");
  assert.equal(input.getAttribute("placeholder"), "Try “Churn rate by state” or “Who are the top 10% spenders?”");
  assert.equal($$("[data-ask-chart]").length, 0);
  assert.equal($("[data-ask-clear]"), null, "no New chat before there is a chat");
  assert.equal(panel().closest(".stack") !== null, true, "drawn inside the Data page's own column");
});

test("a question answers with the reply and a bar chart drawn from the tool result", async () => {
  showDataPage(ids.upload);
  await until(loaded);
  await askQuestion("Show 'converted_30d' by 'region'");
  assert.deepEqual(sent.at(-1), { upload: ids.upload, body: { text: "Show 'converted_30d' by 'region'" } });
  const reply = rated.session.transcript.at(-1).text;
  assert.equal($$("[data-ask-chat] .gmsg.bot")[0].firstChild.textContent, reply);
  const chart = rated.charts[0];
  const figure = $(`[data-ask-chart="${chart.evidence_id}"]`);
  assert.ok(figure, "the chart is under the reply");
  assert.equal(figure.querySelector("figcaption").textContent, `Share “${chart.positive_label}” in converted_30d, by region`);
  const bars = [...figure.querySelectorAll("[data-ask-bar]")];
  assert.equal(bars.length, chart.bars.length);
  assert.equal(figure.querySelectorAll("svg.track").length, chart.bars.length, "every bar is an inline SVG track");
  bars.forEach((bar, i) => {
    assert.equal(bar.querySelector(".pct").textContent, fmtPct(chart.bars[i].value));
    assert.ok(bar.querySelector(".lab").textContent.startsWith(chart.bars[i].label));
    assert.ok(bar.querySelector(".lab").textContent.includes(`${fmtInt(chart.bars[i].rows)} rows`));
  });
  // The longest bar is the largest rate.
  const widths = bars.map((bar) => Number(bar.querySelectorAll("rect")[1].getAttribute("width").replace("%", "")));
  const largest = chart.bars.reduce((best, b, i) => (b.value > chart.bars[best].value ? i : best), 0);
  assert.equal(widths[largest], 100);
  assert.ok($("[data-ask-clear]"), "New chat is offered once there is a chat");
});

test("the chart's numbers are the tool's even when the reply claims others", async () => {
  showDataPage(LIAR);
  await until(loaded);
  await askQuestion("Conversion by region?");
  const pcts = $$("[data-ask-bar] .pct").map((el) => el.textContent);
  assert.deepEqual(pcts, liar.charts[0].bars.map((b) => fmtPct(b.value)));
  assert.ok(!pcts.includes("99.9%"));
});

test("a group too small to show reads 'fewer than N rows' and draws no bar", async () => {
  showDataPage(ids.gappy);
  await until(loaded);
  await askQuestion("Show 'converted_30d' by 'monthly_spend'");
  const chart = suppressed.charts[0];
  const rows = $$("[data-ask-bar]");
  const hidden = rows.filter((row) => row.classList.contains("is-suppressed"));
  assert.equal(hidden.length, chart.bars.filter((b) => b.suppressed).length);
  for (const row of hidden) {
    assert.equal(row.querySelector(".pct").textContent, `fewer than ${chart.min_group_rows} rows`);
    assert.equal(row.querySelectorAll("rect")[1].getAttribute("width"), "0.00%");
  }
  assert.match($("[data-ask-chart] .caption").textContent, /fewer than 10 rows show no figures/);
});

test("markup in a label or a column name is shown as text", async () => {
  showDataPage(HOSTILE);
  await until(loaded);
  await askQuestion("Rates?");
  assert.equal(w.__pwned, undefined);
  assert.equal($$("[data-ask-chart] img").length, 0);
  assert.match($("[data-ask-chart] figcaption").textContent, /<b>region<\/b>/);
});

test("New chat empties the panel", async () => {
  showDataPage(ids.upload);
  await until(loaded);
  await askQuestion("Show 'converted_30d' by 'region'");
  $("[data-ask-clear]").dispatchEvent(new w.Event("click", { bubbles: true }));
  await until(() => !$("[data-ask-chat]"), 2000, "the chat to clear");
  assert.equal($$("[data-ask-chart]").length, 0);
});

// Near the end: it installs an access provider for every test after it (the next one never asks).
test("a role that may not ask reads the chat without a box", async () => {
  router.registerAccess({ can: (method, path) => !(method === "POST" && path === "/uploads/{upload_id}/ask/messages") });
  showDataPage(ids.upload);
  await until(loaded);
  assert.ok($("[data-ask-readonly]"));
  assert.equal($("[data-ask-input]"), null);
  assert.equal($("[data-ask-clear]"), null);
});

test("without an AI service: the same 'Connect an AI service' line as Guided setup, and no box", async () => {
  showDataPage(ids.off);
  await until(loaded);
  const line = $("[data-ask-nochat]");
  assert.ok(line);
  assert.match(line.textContent, /Connect an AI service to chat with the helper\./);
  assert.equal(line.querySelector("a").getAttribute("href"), "#/connections/ai/product");
  assert.equal($("[data-ask-input]"), null);
});
