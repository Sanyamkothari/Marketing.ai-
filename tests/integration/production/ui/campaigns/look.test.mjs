/* Plan M M186 (DEC-1616): the look of the Campaigns list and the campaign page, in jsdom.

   The builders' markup is parsed into a real document and read the way a browser and a screen reader read it:
   - the list: no breadcrumb (a top-level place), the status chip of components.js in each state's tone with its
     icon hidden and its word read, the goal as a quiet tag, the contacted count in a number column, and the result as
     the net value with its likely range (a bar hidden from screen readers, the words beside it) once measured;
   - the campaign page: the name, the status chip and the view's ONE primary action in the header, underline tabs
     (role=tablist, aria-selected, the Technical tab marked for M180) over a tab panel named by its tab, the KPI row
     of stat tiles, "What was sent" as a definition list;
   - the Results tab: the results file's drop zone is the file input's label (one file button, CSV or Parquet), the
     form's "Add results" is the only primary; once measured, the evidence chip and the two figures with their ranges.
   Run through tests/integration/test_campaigns_page_ui.py with M176's suites. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>`, {
  url: "http://localhost/ui/#/",
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { STATUS } = await import("../../../../../ui/modules/campaigns/status.js");
const views = await import("../../../../../ui/modules/campaigns/views.js");
const app = document.getElementById("app");
const draw = (html) => {
  app.innerHTML = html;
  return app;
};
const $ = (sel) => app.querySelector(sel);
const $$ = (sel) => [...app.querySelectorAll(sel)];

const base = {
  campaign_id: "c_20261001_aaaaaaaa",
  kind: "scored",
  name: "Win-back, October list",
  use_case_id: "win-back-campaign",
  run_ids: ["r_20261001_00000001"],
  treatment_start: "2026-10-01T09:00:00Z",
  outcome_window_days: 30,
  status: "live",
  outcomes: null,
  test_plan_hash: "abc",
  measured_at: null,
  counts: { intended_treated: 900, intended_holdout: 100 },
};
const plan = { analysis_date: "2026-11-12", outcome_column: "reactivated", positive_label: "1" };
const fig = (value, text, format = "inr") => ({ value, text, format, sources: [], formula: null });
const proof = {
  claim: "proven",
  headline: "Extra outcomes because of the campaign: +26 (likely +9 to +43). Net value ₹88,187 to ₹4,18,481 (4.18 lakh).",
  sections: [
    {
      key: "incremental",
      lines: [
        { label: "Difference", value: fig(0.006, "+0.6 points", "points"), low: fig(0.002, "+0.2"), high: fig(0.01, "+1.0") },
        { label: "Extra outcomes because of the campaign", value: fig(26, "+26", "signed_count"), low: fig(9, "+9", "signed_count"), high: fig(43, "+43", "signed_count") },
      ],
    },
    {
      key: "net_value",
      lines: [
        { label: "Value of what the campaign changed", value: fig(249913, "₹2,49,913"), low: fig(88830, "₹88,830"), high: fig(419124, "₹4,19,124") },
        { label: "Net value", value: fig(249269.49, "₹2,49,269 (2.49 lakh)"), low: fig(88187.1, "₹88,187"), high: fig(418481.2, "₹4,18,481 (4.18 lakh)") },
      ],
    },
  ],
};
const entry = (campaign, status, extra = {}) => ({ campaign, view: { campaign, plan }, proof: null, goal: "Win back lapsed customers", status, ...extra });
const measured = { ...base, campaign_id: "c_20261001_bbbbbbbb", status: "measured", measured_at: "2026-11-13T00:00:00Z" };

test("the list: no breadcrumb, a status chip of the one mapping per state, the goal as a tag, numbers in a number column", () => {
  draw(
    views.campaignsListHtml({
      entries: [
        entry({ ...base, test_plan_hash: null, campaign_id: "c_1" }, STATUS.draft),
        entry({ ...base, campaign_id: "c_2" }, STATUS.live),
        entry({ ...base, campaign_id: "c_3" }, STATUS.waiting),
        entry(measured, STATUS.measured, { proof }),
      ],
    }),
  );
  assert.equal($(".crumbs"), null, "Campaigns is a top-level place");
  assert.equal($$(".head-actions .btn.primary").length, 1);
  const chips = $$("tbody [data-status]");
  assert.deepEqual(
    chips.map((c) => [c.dataset.status, c.dataset.tone, c.textContent]),
    [
      ["draft", "neutral", "Draft"],
      ["live", "info", "Live"],
      ["waiting", "warning", "Waiting for results"],
      ["measured", "good", "Measured"],
    ],
  );
  for (const c of chips) {
    assert.ok(c.classList.contains("badge"), "the components.js chip, not the old black badge");
    assert.equal(c.querySelector("svg").getAttribute("aria-hidden"), "true", "an icon beside the word, never colour alone");
  }
  for (const tr of $$("tbody tr")) {
    assert.equal(tr.querySelector("td:nth-child(2) .tag").textContent, "Win back lapsed customers");
    assert.ok(tr.querySelector("td:nth-child(4)").classList.contains("num"), "right-aligned, tabular figures");
  }
  assert.equal($("thead th:nth-child(4)").className, "num");
});

test("the list: a measured campaign's result is its net value and likely range, not a sentence", () => {
  draw(views.campaignsListHtml({ entries: [entry(measured, STATUS.measured, { proof })] }));
  const cell = $("[data-result]");
  assert.equal(cell.querySelector(".cp-result-value .num").textContent, "₹2.49 lakh");
  assert.equal(cell.querySelector(".cp-result-value .num").getAttribute("title"), "₹2,49,269 (2.49 lakh)");
  assert.equal(cell.querySelector(".vr-text").textContent, "Likely ₹88,187 to ₹4.18 lakh", "the words a screen reader reads");
  assert.equal(cell.querySelector(".vr-track").getAttribute("aria-hidden"), "true");
  assert.ok(cell.querySelector(".vr-dot"), "the dot marks the net value inside its range");
  assert.ok(!cell.textContent.includes(proof.headline), "the Pack's sentence stays on the Results tab");
  // No Pack (it could not be read): the verdict's short sentence; nothing at all: "Measured".
  const verdict = { headline: "The campaign added about 26 conversions" };
  draw(views.campaignsListHtml({ entries: [entry(measured, STATUS.measured, { view: { campaign: measured, verdict } })] }));
  assert.equal($("[data-result]").textContent, verdict.headline);
  // The long recorded name is one secondary line, whole in its tooltip.
  const sub = $(".cp-sub");
  assert.equal(sub.getAttribute("title"), base.name);
  assert.ok(sub.hasAttribute("data-user-text"));
});

test("the list: no campaigns is an empty state with an icon, one sentence and the one primary action", () => {
  draw(views.campaignsListHtml({ entries: [] }));
  const empty = $("[data-campaigns-empty] .empty-state");
  assert.ok(empty.querySelector(".es-icon svg"), "a line icon");
  assert.equal(empty.querySelectorAll("a, button").length, 1);
  assert.equal($$(".btn.primary").length, 1, "the empty state's action is the page's one primary");
});

const pageModel = (over = {}) => ({
  campaign: base,
  view: { campaign: base, plan },
  plan,
  status: STATUS.live,
  goal: "Win back lapsed customers",
  tab: "overview",
  results: {},
  refresh: {},
  summary: { treat_rows: 900, total_rows: 1000, offer_counts: { "Discount 10%": 900 } },
  contactListHref: "/runs/r_1/treat_list.csv",
  canAdd: true,
  canRefresh: true,
  ...over,
});

test("the campaign page: the name, its status chip and the view's one primary action in the header, over underline tabs", () => {
  draw(views.campaignPageHtml(pageModel()));
  assert.equal($$("h1").length, 1);
  assert.equal($(".head .cp-title [data-status]").textContent, "Live");
  assert.equal($(".head .cp-title [data-status]").dataset.tone, "info");
  const list = $('[role="tablist"]');
  assert.equal(list.getAttribute("aria-label"), "Campaign");
  const tabs = $$('[role="tab"]');
  assert.deepEqual(tabs.map((t) => t.dataset.tab), ["overview", "audience", "results", "technical"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("aria-selected")), ["true", "false", "false", "false"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("tabindex")), ["0", "-1", "-1", "-1"], "a roving tabindex: arrows move");
  assert.ok(tabs[3].hasAttribute("data-technical"), "M180's switch still hides Technical details");
  const panel = $('[role="tabpanel"]');
  assert.equal(panel.getAttribute("aria-labelledby"), "cp-tab-overview");
  assert.ok(document.getElementById("cp-tab-overview"), "the panel is named by its tab");
  // One primary per view, in the header, by tab.
  const primaryOf = (over) => {
    draw(views.campaignPageHtml(pageModel(over)));
    const all = $$(".btn.primary");
    return all.map((b) => [b.closest(".head") ? "head" : "body", b.textContent.trim()]);
  };
  assert.deepEqual(primaryOf({}), [["head", "Download contact list"]]);
  assert.deepEqual(primaryOf({ tab: "audience" }), [["head", "Download contact list"]]);
  assert.deepEqual(primaryOf({ tab: "results" }), [["body", "Add results"]], "the form's own button");
  assert.deepEqual(primaryOf({ tab: "technical" }), []);
  const done = { ...measured };
  assert.deepEqual(primaryOf({ campaign: done, view: { campaign: done, plan }, status: STATUS.measured, proof, tab: "results" }), [
    ["head", "Open the full report"],
  ]);
  assert.deepEqual(primaryOf({ listHold: "unknown", tab: "audience" }), [], "a held list offers no download");
});

test("the campaign page: where it stands, a KPI row of stat tiles with the net value's range, and what was sent as a definition list", () => {
  const done = { ...measured };
  draw(views.campaignPageHtml(pageModel({ campaign: done, view: { campaign: done, plan }, status: STATUS.measured, proof })));
  assert.equal($("[data-overview] [data-next-step]").textContent, "Measured on 13 Nov 2026. The full report is ready.");
  assert.ok($("[data-overview] .cp-stand-icon.tone-good svg"), "the status's own icon and tone");
  assert.ok($("[data-overview] [data-refresh-ask].btn.secondary"), "Refresh the list stays a secondary action");
  const tiles = $$("[data-kpis] > .stat");
  assert.deepEqual(
    tiles.map((t) => [t.querySelector(".stat-label").textContent, t.querySelector(".stat-value").textContent]),
    [
      ["Contacted", "900"],
      ["Comparison group", "100"],
      ["Net value", "₹2.49 lakh"],
    ],
  );
  assert.equal($("[data-tile-value] .vr-text").textContent, "Likely ₹88,187 to ₹4.18 lakh");
  assert.equal($$("dl.cp-dl > .cp-kv-row > dt").length, 3);
  assert.deepEqual(
    $$("dl.cp-dl dd").map((d) => d.textContent),
    ["1 Oct 2026", "900 customers", "900 customers"],
  );
});

test("the Results tab: the results file's drop zone is the file input's label, with one file button", () => {
  draw(views.campaignPageHtml(pageModel({ tab: "results", status: STATUS.waiting })));
  const zone = $("[data-results-drop]");
  assert.equal(zone.tagName, "LABEL");
  const input = $("[data-results-file-input]");
  assert.equal(zone.getAttribute("for"), input.id, "a click anywhere on the zone opens the file chooser");
  assert.ok(zone.contains(input));
  assert.equal(input.getAttribute("accept"), ".csv,.parquet");
  assert.ok(input.required);
  assert.match(zone.textContent, /Drop the results file here, or choose a file/);
  assert.equal($("[data-results-chosen]").hidden, true, "nothing is named until a file is chosen");
  assert.equal($$("form .btn.primary").length, 1);
  assert.equal($("form .btn.primary").getAttribute("type"), "submit");
});

test("the Results tab once measured: the evidence chip, the Pack's sentence and two figures with their ranges", () => {
  const done = { ...measured };
  draw(views.campaignPageHtml(pageModel({ campaign: done, view: { campaign: done, plan }, status: STATUS.measured, proof, tab: "results" })));
  const chip = $("[data-evidence] [data-status]");
  assert.deepEqual([chip.textContent, chip.dataset.tone], ["Proven", "good"]);
  assert.equal($("[data-headline]").textContent, proof.headline);
  const tiles = $$("[data-result-tiles] > .stat");
  assert.deepEqual(
    tiles.map((t) => [t.querySelector(".stat-value").textContent, t.querySelector(".vr-text").textContent]),
    [
      ["+26", "Likely +9 to +43"],
      ["₹2.49 lakh", "Likely ₹88,187 to ₹4.18 lakh"],
    ],
  );
  // An estimated result says so with its own tone.
  draw(
    views.campaignPageHtml(
      pageModel({ campaign: done, view: { campaign: done, plan }, status: STATUS.measured, proof: { ...proof, claim: "stated_random" }, tab: "results" }),
    ),
  );
  assert.deepEqual([$("[data-evidence] [data-status]").textContent, $("[data-evidence] [data-status]").dataset.tone], ["Estimated", "info"]);
});

test("figureText writes each server figure in ui/format.js's one format", () => {
  assert.equal(views.figureText(fig(418481.2, "₹4,18,481")), "₹4.18 lakh");
  assert.equal(views.figureText(fig(26, "+26", "signed_count")), "+26");
  assert.equal(views.figureText(fig(-3580.84, "-3,580.84", "signed_amount")), "−3,581");
  assert.equal(views.figureText(fig(0.53, "53.2%", "share")), "53.0%");
  assert.equal(views.figureText(fig(null, "Not measured", "text")), "Not measured");
  assert.equal(views.figureText(null), "—");
});
