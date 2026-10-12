/* Plan M M187 (DEC-1617): the "New campaign" wizard's look, in jsdom.

   The wizard's screens are drawn by its own builders (wizard_views.js) and read the way a browser and a screen reader
   read them: the design system's helpers (ui/components.js) draw the header, the stepper, the chips, the stat tiles
   and the value range, the attention items and the buttons; each goal has a line icon and its stage as a quiet tag;
   the Review screen opens with a summary of three tiles; Send is a success state. Words and hooks are M177's and
   M181's. Run through tests/integration/test_campaign_wizard_look.py (no API fixtures needed). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>`, {
  url: "http://localhost/ui/#/campaigns/new",
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const V = await import("../../../../../../ui/modules/campaigns/wizard_views.js");
const R = await import("../../../../../../ui/modules/campaigns/wizard_rules.js");
const S = await import("../../../../../../ui/modules/campaigns/wizard_styles.js");
const app = document.getElementById("app");
const $ = (sel) => app.querySelector(sel);
const $$ = (sel) => [...app.querySelectorAll(sel)];
const draw = (html) => {
  app.innerHTML = html;
  return app;
};
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);

const GOALS = [
  { id: "targeted-advertisement", name: "Find customers likely to buy", stage: "Awareness" },
  { id: "win-back-campaign", name: "Win back lapsed customers", stage: "Win-back" },
  { id: "payment-propensity", name: "Get customers to pay on time", stage: "Service" },
  { id: "rca", name: "Understand why customers leave", stage: "Churn" },
  { id: "hillstrom-email", name: "Choose the best e-mail for each customer", stage: "Retention" },
  { id: "ai-onboarding-assistant", name: "Answer customer questions (AI assistant)", stage: "Onboarding" },
];
const MORE = [
  { id: "order-fulfillment", name: "Order Fulfillment", stage: "Service" },
  { id: "fault-prediction", name: "Fault Prediction", stage: "Service" },
];
const campaign = { campaign_id: "c_1", use_case_id: "win-back-campaign", run_ids: ["r_1"], status: "live" };
const scoring = {
  bands: [
    { name: "Persuadables", rows: 14998, action: "Treat" },
    { name: "Sure things", rows: 57, action: "Don't treat (converts anyway)" },
  ],
};
const list = { treat_rows: 12862, net_value_total: 2518304.28, offer_counts: { a: 7597, b: 5265 }, channel_rows: { email: 9100 } };
const preview = { n_holdout: 12737, holdout_fraction: 0.498 };
const policy = { net_value_low: 88187, net_value_high: 418000 };

test("the page: the design system's header (crumbs, one H1, one sentence) and its stepper, not pills", () => {
  draw(V.pageHtml("customers", "<p>body</p>"));
  assert.equal($$("h1").length, 1);
  assert.ok($("header.page-header .ph-main nav.crumbs"), "breadcrumbs: the wizard sits below Campaigns");
  assert.equal(text($("header.page-header h1.h1")), "New campaign");
  assert.equal(text($("header.page-header p.desc")), V.LEAD);
  assert.equal($(".cw-step"), null, "the M177 pill steps are gone");
  const steps = $$("ol.stepper > li.st-step");
  assert.deepEqual(
    steps.map((li) => [li.getAttribute("data-cw-stepper"), li.className.replace("st-step ", "")]),
    [
      ["goal", "done"],
      ["customers", "current"],
      ["audience", "upcoming"],
      ["review", "upcoming"],
      ["send", "upcoming"],
    ],
  );
  // done: a brand circle with a check and "(done)" for a screen reader; current: the ring with its number
  assert.ok(steps[0].querySelector(".st-dot svg[aria-hidden=true]"));
  assert.equal(text(steps[0].querySelector(".st-label .sr")), "(done)");
  assert.equal(text(steps[1].querySelector(".st-dot")), "2");
  assert.equal(steps[1].getAttribute("aria-current"), "step");
  assert.equal(text($(".stepper-phone")), "Step 2 of 5 · Customers");
  assert.equal($(".stepper-phone").getAttribute("aria-hidden"), "true");
});

test("the Review screen's stepper: Audience and Review are both current, Review is the step", () => {
  draw(V.pageHtml("review", ""));
  assert.deepEqual(
    $$("ol.stepper > li").map((li) => li.className.replace("st-step ", "")),
    ["done", "done", "current", "current", "upcoming"],
  );
  assert.equal($$("[aria-current=step]").length, 1);
  assert.equal($("[data-cw-stepper=review]").getAttribute("aria-current"), "step");
  assert.equal(text($("[data-cw-stepper=audience] .st-dot")), "3", "a current step shows its number, not a check");
  assert.equal(text($(".stepper-phone")), "Step 4 of 5 · Review");
});

test("Goal: each card has a line icon, its name and its stage as a quiet tag; no card inside a card", () => {
  draw(V.goalHtml({ goals: GOALS, more: MORE, selected: "win-back-campaign" }));
  const section = $("[data-cw-goal-card]");
  assert.ok(!section.classList.contains("card"), "the goal cards sit on the canvas, not in a bordered card");
  assert.equal(text(section.querySelector("h2")), "What is the campaign for?");
  const cards = $$("[data-cw-goals] > button.cw-goal");
  assert.equal(cards.length, GOALS.length);
  const icons = new Set();
  for (const card of cards) {
    const svg = card.querySelector(".cw-goal-icon svg");
    assert.ok(svg, `${card.dataset.cwGoal} has its icon`);
    assert.equal(svg.getAttribute("aria-hidden"), "true");
    assert.equal(svg.getAttribute("stroke-width"), "1.75", "the design system's line weight");
    assert.equal(svg.getAttribute("viewBox"), "0 0 24 24");
    icons.add(svg.innerHTML);
    const stage = card.querySelector(".cw-goal-stage > span.tag");
    assert.ok(stage, "the stage is a tag chip");
    assert.ok(!/tone-/.test(stage.className), "a stage is a label, not a status");
  }
  assert.equal(icons.size, GOALS.length, "every named goal has its own icon");
  assert.deepEqual(
    cards.map((c) => text(c.querySelector(".cw-goal-name"))),
    GOALS.map((g) => g.name),
    "the button's name is the goal's name",
  );
  // The icons add no words; a space keeps the name and the stage apart for a screen reader.
  assert.equal(cards[0].textContent, `${GOALS[0].name} ${GOALS[0].stage}`);
  assert.deepEqual(
    $$(".cw-goal.is-selected").map((c) => c.dataset.cwGoal),
    ["win-back-campaign"],
    "the goal chosen last is drawn chosen",
  );
  const more = $("details[data-cw-more]");
  assert.equal(more.open, false);
  assert.equal(text(more.querySelector("summary")), "More goals 2");
  assert.ok(more.querySelector("summary .cw-more-chev svg[aria-hidden=true]"));
  assert.deepEqual(
    [...more.querySelectorAll("[data-cw-goal]")].map((c) => c.dataset.cwGoal),
    ["order-fulfillment", "fault-prediction"],
  );
  assert.equal($$(".btn.primary").length, 0);
});

test("Customers: a calm card around Guided setup, with the goal's icon", () => {
  draw(V.customersHtml({ uc: { id: "win-back-campaign", name: "Win-back" }, first: true }));
  const card = $("[data-cw-customers=first]");
  assert.ok(card.classList.contains("card"));
  assert.ok(card.querySelector(".cw-card-head .cw-card-icon svg[aria-hidden=true]"));
  assert.equal(text(card.querySelector(".cw-card-head h2")), 'Customers for "Win back lapsed customers"');
  assert.ok(card.querySelector("[data-cw-guided] [data-cw-guided-main]"), "Guided setup's place is unchanged");
  draw(
    V.customersHtml({
      uc: { id: "win-back-campaign" },
      first: false,
      problem: { validation: { checks: [{ severity: "error", message: "No key column." }] } },
    }),
  );
  const checks = $("[data-cw-checks]");
  assert.ok(checks.classList.contains("tone-critical"));
  assert.equal(checks.getAttribute("role"), "alert");
  assert.equal(checks.querySelector(".cw-problem-icon svg").getAttribute("aria-label"), "Warning", "an icon with a name, not colour alone");
});

test("notices are the design system's attention items, the action a primary button", () => {
  draw(V.waitingHtml());
  const wait = $("[data-cw-waiting=wait]");
  assert.ok(wait.querySelector(".attn.tone-warning .attn-icon svg[role=img][aria-label=Warning]"));
  assert.equal(text(wait.querySelector(".attn-title")), "Waiting for an approval");
  assert.equal(wait.querySelector(".attn-action a.btn.primary").getAttribute("href"), "#/approvals");
  draw(V.stoppedHtml({ ucId: "u", detail: null, reason: "It stopped." }));
  assert.ok($("[data-cw-stopped] .attn.tone-critical"));
  assert.ok($("[data-cw-stopped] .attn-action a.btn.primary[data-cw-restart]"));
  draw(V.goalHtml({ canStart: false }));
  assert.ok($("[data-cw-no-role] .attn.tone-info"));
  draw(V.progressHtml({ learning: true, share: 0.4 }));
  assert.ok($("[data-cw-working=learning] .cw-card-icon svg"));
  assert.equal($("[role=progressbar]").getAttribute("aria-valuenow"), "40");
});

test("Review: a summary of three tiles - contacted, comparison group, expected value with its range bar", () => {
  draw(V.reviewHtml({ campaign, scoring, list, policy, preview, cost: null, compliance: null, resultsOn: "2026-11-10" }));
  const row = $(".stat-row[data-cw-summary=value]");
  assert.ok(row, "a KPI row");
  const tiles = [...row.children];
  assert.deepEqual(
    tiles.map((t) => t.getAttribute("data-cw-tile")),
    ["contacted", "comparison", "value"],
  );
  assert.ok(tiles.every((t) => t.classList.contains("stat")));
  assert.equal(text($("[data-cw-tile=contacted] .stat-value")), "12,862");
  assert.equal(text($("[data-cw-tile=comparison] .stat-caption")), "Customers not contacted (49.8%)");
  const value = R.expectedValue(list, policy);
  assert.equal(text($("[data-cw-tile=value] .stat-value")), R.valueFigure(value));
  const range = $("[data-cw-tile=value] .vrange[data-cw-review-value]");
  assert.equal(range.querySelector(".vr-track").getAttribute("aria-hidden"), "true", "the bar is drawn for the eye");
  assert.ok(range.querySelector(".vr-bar"));
  assert.equal(text(range.querySelector(".vr-text")), R.valueSentence(value), "the words are beside it");
  assert.equal($("[data-cw-summary] ~ * .kpi"), null, "no old KPI tiles");
  assert.equal($$(".kpi").length, 0);
  // no range: the words alone, no bar
  draw(V.reviewHtml({ campaign, scoring, list, policy: null, preview, cost: null, compliance: null, resultsOn: null }));
  assert.equal($("[data-cw-review-value] .vr-track"), null);
  assert.match(text($("[data-cw-review-value]")), /A range cannot be given for this list\./);
});

test("Review: small tables with share bars; what happens to a priority is a second line, not a column", () => {
  draw(V.reviewHtml({ campaign, scoring, list, policy, preview, cost: null, compliance: null, resultsOn: null }));
  const priority = $("[data-cw-priority]");
  assert.deepEqual(
    [...priority.querySelectorAll("th")].map((th) => text(th)),
    ["Priority", "Customers"],
  );
  const first = priority.querySelector("tbody tr");
  assert.equal(text(first.querySelector(".cw-pri-name")), "Persuadables");
  assert.equal(text(first.querySelector(".cw-pri-action")), "Treat");
  assert.equal(text(first.querySelector("td.num")), "14,998");
  const bars = $$("[data-cw-priority] .cw-bar");
  assert.equal(bars.length, 2);
  assert.ok(bars.every((b) => b.getAttribute("aria-hidden") === "true"));
  assert.equal(bars[0].firstElementChild.style.width, "100%", "the largest count fills its bar");
  assert.ok($("[data-cw-offers] .cw-bar") && $("[data-cw-channels] .cw-bar"));
  assert.equal($$(".cw-breakdowns > .cw-breakdown").length, 3);
});

test("Review: the compliance result is a status chip with its sentence; one primary button and secondary links", () => {
  draw(
    V.reviewHtml({
      campaign,
      scoring,
      list,
      policy,
      preview,
      cost: null,
      compliance: { customers: 100, withheld_rows: 4, rerouted_rows: 0 },
      resultsOn: null,
    }),
  );
  const chip = $("[data-cw-review-compliance] dd .badge");
  assert.ok(chip.classList.contains("tone-good"));
  assert.equal(chip.querySelector("svg").getAttribute("aria-hidden"), "true");
  assert.equal(text(chip), "Checked");
  assert.equal(text($("[data-cw-review-compliance] .cw-kv-text")), "96 of 100 customers may be contacted; 4 were taken off the list.");
  assert.equal($$(".btn.primary").length, 1);
  assert.equal(text($("[data-cw-approve]")), "Approve and download");
  assert.ok($("[data-cw-approve] svg"), "the download icon");
  assert.ok($("[data-cw-again]").classList.contains("tertiary"));
  assert.ok($("[data-cw-review] .cw-decide [data-cw-approve]"), "the decision bar ends the card");
  draw(
    V.reviewHtml({ campaign, scoring, list, policy, preview, cost: null, compliance: { customers: 5, withheld_rows: 5 }, resultsOn: null }),
  );
  assert.ok($("[data-cw-review-compliance] .badge.tone-critical"));
  // the Approver's one button and a busy one
  draw(V.reviewHtml({ campaign, list, preview, cost: null, compliance: null, resultsOn: null, model: { state: "pending", canApprove: true } }));
  assert.equal($$(".btn.primary").length, 1);
  assert.equal(text($("[data-cw-approve]")), V.APPROVE_BOTH_LABEL);
  assert.ok($("[data-cw-new-model].tone-info"));
  draw(V.reviewHtml({ campaign, list, preview, cost: null, compliance: null, resultsOn: null, busy: "Approving…" }));
  assert.equal($("[data-cw-approve]").getAttribute("aria-busy"), "true");
  assert.ok($("[data-cw-approve]").disabled);
});

test("Send: a success state - a check, the status chip, one sentence, the campaign page first", () => {
  draw(V.sendHtml({ campaign, plan: null, downloadHref: "/runs/r_1/treat_list.csv", resultsOn: null, downloaded: true }));
  const send = $("[data-cw-send].cw-done");
  assert.ok(send.querySelector(".cw-done-icon svg[aria-hidden=true]"));
  assert.equal(text(send.querySelector("h2")), "Your campaign is live");
  const status = $("[data-cw-status]");
  assert.ok(status.classList.contains("badge") && status.classList.contains("tone-info"), "Live is the brand's tone");
  assert.equal(status.textContent, "Live");
  assert.equal(text($("[data-cw-live]")), "Your contact list is downloading. Send it from your e-mail or messaging tool.");
  assert.deepEqual(
    $$(".cw-done-actions > .btn").map((b) => [b.className, text(b)]),
    [
      ["btn primary", "Open the campaign"],
      ["btn secondary", "Download again"],
    ],
  );
  draw(V.sendHtml({ campaign: { ...campaign, status: "measured" }, plan: null, downloadHref: "/x.csv", resultsOn: null }));
  assert.ok($("[data-cw-status]").classList.contains("tone-good"));
  assert.equal(text($(".btn.primary")), "Download contact list");
  draw(V.sendHtml({ campaign, plan: null, downloadHref: "/x.csv", resultsOn: null, held: "pending" }));
  assert.ok($("[data-cw-send-held=pending].tone-warning"));
  assert.equal($("[download]"), null, "a held list offers no download");
});

test("the wizard's style sheet lays out the helpers and reads the brand token, never the default blue", () => {
  S.injectWizardStyles(document);
  const css = document.getElementById("cw-styles").textContent;
  assert.doesNotMatch(css, /\.cw-step\b/, "the pill steps' rules are gone");
  assert.doesNotMatch(css, /#1E57BD/i, "no rule names the default brand colour");
  for (const rule of [".cw .cw-goal.is-selected{", ".cw .cw-goal-icon{", ".cw .cw-bar>span{", ".st-step.current + .st-step.current::before{"]) {
    const at = css.indexOf(rule);
    assert.ok(at >= 0, rule);
    assert.match(css.slice(at, css.indexOf("}", at)), /var\(--brand-(blue|50)\)/, `${rule} reads the brand`);
  }
  assert.match(css, /@media \(max-width:700px\)\{[^]*\.cw \.cw-actions \.btn[^{]*\{width:100%/, "full-width buttons on a phone");
  assert.match(css, /prefers-reduced-motion/);
});
