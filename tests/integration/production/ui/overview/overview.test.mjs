/* Plan M M175 (DEC-1605): the Overview page (`#/overview`) in jsdom, as the browser draws it (app.js and every module
   in the order of ui/index.html), against the API answers recorded from the app on the validated Hillstrom demo
   (tests/fixtures/ui_demo/api.json, whose `GET /overview` is built from that recording by
   tests/fixtures/build_overview_fixture.py) and against answers shaped for the cases the demo does not have.

   What a person sees, in order: needs your attention (each card with ONE action), the value proven to date (a floor,
   rupees rounded, with "How this is counted"), live campaigns (short name, status, next step), the one primary
   button, and the first-visit checklist (ticked from real state, hidden once done or dismissed). No engine word
   outside technical details, with an EMPTY allow-list. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { $, $$, installPage, settle, until } from "../harness.mjs";
import { findNeverShown } from "../../../../../ui/labels.js";
import { visibleText } from "../glossary/collect.mjs";
import { recorded } from "../glossary/replay.mjs";
import { pageScripts } from "../simple/fake.mjs";

const key = (request) => {
  const query = new URLSearchParams(request.query);
  query.sort();
  const q = query.toString();
  return `${request.method} ${request.path}${q ? `?${q}` : ""}`;
};

/** What `GET /overview` answers now; each test sets it. */
const world = { overview: recorded["GET /overview"] };

const server = async (request) => {
  if (request.method === "GET" && request.path === "/overview") {
    return { status: world.overview.status, body: world.overview.body };
  }
  const hit = recorded[key(request)];
  if (hit) return { status: hit.status, body: hit.body, headers: { "Content-Type": hit.content_type || "application/json" } };
  return { status: 404, body: { detail: { code: "NOT_RECORDED", message: "Not found." } } };
};

const { w } = installPage(server, { hash: "#/__start" });
for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
const page = await import("../../../../../ui/modules/overview/page.js");
const overviewModule = await import("../../../../../ui/modules/overview/index.js");
const router = await import("../../../../../ui/modules/router.js");

const demo = recorded["GET /overview"].body;
const answer = (body) => ({ status: 200, body });

async function visit(overview = { status: 200, body: demo }) {
  world.overview = overview;
  w.location.hash = "#/__between";
  await settle(4);
  w.location.hash = "#/overview";
  await until(() => $('[data-module="overview"] .ov-grid, [data-module="overview"] .apierr'), 4000, "the Overview");
  await settle(4);
}

const text = () => visibleText($('[data-module="overview"]'));
const clear = () => {
  try {
    w.localStorage.clear();
  } catch {
    /* none */
  }
};

// --- shapes the demo does not have ---------------------------------------------------------------------------

const figure = (value, textValue, format = "inr") => ({
  value,
  text: textValue,
  format,
  sources: [{ artefact: "campaigns/c_1/incrementality_report.json", field: "x" }],
  formula: null,
});

const card = (over) => ({
  code: "X",
  kind: "no_control",
  severity: "warning",
  title: "A title",
  text: "A sentence about it.",
  next_step: "Do the thing.",
  campaign_id: "c_1",
  campaign_name: figure("Win back lapsed customers - March", "Win back lapsed customers - March", "text"),
  use_case_id: "win-back-campaign",
  facts: [],
  read: [],
  ...over,
});

const row = (n, over = {}) => ({
  campaign_id: `c_${n}`,
  name: `Win back lapsed customers - month ${n}, a name that is far too long to sit on one line of a list`,
  use_case_id: "win-back-campaign",
  status: "live",
  next_step: "wait",
  results_date: "2026-11-12",
  ...over,
});

const busy = () => ({
  ...demo,
  cards: [
    card({ code: "CAMPAIGN_NO_CONTROL", kind: "no_control", campaign_id: "c_1" }),
    card({ code: "GROUP_BACKFIRED", kind: "backfire", campaign_id: "c_2" }),
    card({ code: "CHALLENGER_READY", kind: "challenger_ready", campaign_id: null, campaign_name: null }),
    card({ code: "DRIFT_DRIFTED", kind: "drift", campaign_id: null, campaign_name: null }),
    card({ code: "EFFECT_FADING", kind: "uplift_fading", campaign_id: null, campaign_name: null, use_case_id: null }),
  ],
  cards_total: 8,
  headline: { ...demo.headline, total: figure(1234567.8, "₹12,34,568") },
  campaigns: [
    row(1),
    row(2, { status: "waiting_for_results", next_step: "add_results" }),
    row(3, { status: "waiting_for_results", next_step: "measure" }),
    row(4, { next_step: "plan", results_date: null }),
    row(5),
  ],
  campaigns_total: 7,
  checklist: { connected: true, campaign: true, results: false },
});

const fresh = () => ({
  schema_version: 1,
  cards: [],
  cards_total: 0,
  headline: null,
  summary_checked: true,
  campaigns: [],
  campaigns_total: 0,
  checklist: { connected: false, campaign: false, results: false },
  built_at: "2026-10-11T12:00:00Z",
});

// --- the demo ----------------------------------------------------------------------------------------------------

test("the demo: one H1, one sentence, the one primary button in the header, then attention, value and live in the plan's order", async () => {
  clear();
  await visit();
  assert.equal($$("main h1").length, 1);
  assert.equal($("main h1").textContent, "Overview");
  assert.ok($("main .desc").textContent.trim().length > 20, "one sentence under the title");
  const primary = $$('[data-module="overview"] .btn.primary');
  assert.equal(primary.length, 1, "ONE primary button on the page");
  assert.equal(primary[0].textContent.trim(), "+ New campaign");
  assert.equal(primary[0].getAttribute("href"), "#/campaigns/new");
  const order = $$('[data-module="overview"] section[data-attention], [data-module="overview"] section[data-value], [data-module="overview"] section[data-live]').map(
    (s) => s.getAttribute("aria-labelledby"),
  );
  assert.deepEqual(order, ["ov-h-attention", "ov-h-value", "ov-h-live"], "attention, then value, then live campaigns");
  // DEC-1605: the button sits in the page header, before the sections (the plan lists it fourth), as on every page.
  const first = $('[data-module="overview"] section');
  assert.ok(primary[0].compareDocumentPosition(first) & 4, "the button is in the header, above attention, value and live");
  assert.equal(document.title, "Overview · Marketing AI");
});

test("the demo: the server's card, with exactly one action that opens its campaign", async () => {
  await visit();
  const cards = $$("[data-attention] [data-card]");
  assert.equal(cards.length, demo.cards.length);
  assert.equal(cards.length, 1, "the demo has one card");
  for (const c of cards) {
    assert.equal(c.querySelectorAll("a, button").length, 1, "every card has exactly one action");
    assert.equal(c.querySelectorAll("[data-card-action]").length, 1);
  }
  const [server] = demo.cards;
  const shown = cards[0].textContent;
  for (const part of [server.title, server.text]) assert.ok(shown.includes(part), part);
  // The server's "what to do" sentence names a "use case" and says "train": until M172 rewords it at the source, the
  // card keeps its title, its sentence and its one action, and leaves that sentence out.
  assert.ok(!shown.includes(server.next_step));
  assert.equal(cards[0].querySelector(".ov-card-next"), null);
  assert.equal(cards[0].querySelector("[data-card-action]").getAttribute("href"), `#/campaigns/${server.campaign_id}/overview`);
  assert.equal(cards[0].querySelector("[data-card-action]").textContent.trim(), "Open the campaign");
});

test("the demo: value proven to date is the server's rupee floor, with one sentence and how it is counted", async () => {
  await visit();
  const value = $("[data-value]");
  assert.match(value.querySelector("h2").textContent, /^Value proven to date$/);
  const amount = value.querySelector("[data-value-amount]");
  assert.match(amount.textContent, /At least\s+₹81,034/);
  assert.equal(amount.getAttribute("title"), demo.headline.total.text, "the exact figure is in the tooltip");
  assert.match(value.querySelector("[data-value-meaning]").textContent, /^A floor, not a forecast:/);
  assert.match(value.querySelector("[data-value-meaning]").textContent, /Based on 1 measured campaign\./);
  const link = value.querySelector("[data-how-counted]");
  assert.equal(link.textContent, "How this is counted");
  assert.match(link.getAttribute("href"), /^#\/(reports|pilot)$/, "Reports (M178), or today's reports hub until it has merged");
});

test("the demo: nothing is live, so the list says so and points to all campaigns", async () => {
  await visit();
  assert.equal($$("[data-live-row]").length, 0);
  assert.match($("[data-live]").textContent, /No campaign is live right now/);
  assert.equal($('[data-live] a[href="#/campaigns"]').textContent.trim(), "See all campaigns");
  assert.equal($$('[data-module="overview"] .btn.primary').length, 1, "the empty state adds no second primary button");
});

test("the demo: all three first steps are done, so the checklist is not drawn", async () => {
  clear();
  await visit();
  assert.deepEqual(demo.checklist, { connected: true, campaign: true, results: true });
  assert.equal($("[data-checklist]"), null);
});

// --- cards and rows ---------------------------------------------------------------------------------------------------

test("a busy day: five cards with one action each, five live rows, and how many more there are", async () => {
  clear();
  await visit(answer(busy()));
  const cards = $$("[data-attention] [data-card]");
  assert.equal(cards.length, 5);
  for (const c of cards) assert.equal(c.querySelectorAll("a, button").length, 1, `${c.dataset.card}: exactly one action`);
  assert.deepEqual(
    cards.map((c) => [c.dataset.card, c.querySelector("[data-card-action]").getAttribute("href"), c.querySelector("[data-card-action]").textContent.trim()]),
    [
      ["CAMPAIGN_NO_CONTROL", "#/campaigns/c_1/overview", "Open the campaign"],
      ["GROUP_BACKFIRED", "#/pilot/proof/c_2", "Open the Value Proof Pack"],
      ["CHALLENGER_READY", "#/approvals", "Review the new candidate"],
      ["DRIFT_DRIFTED", "#/uc/win-back-campaign", "Open the goal"],
      ["EFFECT_FADING", "#/results", "See all results"],
    ],
  );
  assert.match($("[data-more-cards]").textContent, /3 more items need your attention/);
  assert.equal($('[data-more-cards] a').getAttribute("href"), "#/results");

  const rows = $$("[data-live-row]");
  assert.equal(rows.length, 5);
  const cells = rows.map((r) => [
    r.querySelector(".ov-row-name").textContent,
    r.querySelector(".pill").textContent,
    r.querySelector(".ov-row-next").textContent,
    r.querySelector(".ov-row-name").getAttribute("href"),
  ]);
  assert.deepEqual(cells[0], ["Win back lapsed customers", "Live", "Add its results after 12 Nov 2026", "#/campaigns/c_1/overview"]);
  assert.deepEqual(cells[1].slice(1, 3), ["Waiting for results", "Add its results"]);
  assert.deepEqual(cells[2].slice(1, 3), ["Waiting for results", "Measure its results"]);
  assert.deepEqual(cells[3].slice(1, 3), ["Live", "Set the date its results are due"]);
  for (const r of rows) assert.ok(r.querySelector(".ov-row-name").textContent.length <= 40, "a short name");
  assert.equal(rows[0].querySelector(".ov-row-name").getAttribute("title"), busy().campaigns[0].name, "the long name is the tooltip");
  assert.match($("[data-more-campaigns]").textContent, /2 more live campaigns/);
  assert.equal($("[data-more-campaigns] a").getAttribute("href"), "#/campaigns");
  assert.match($("[data-value-amount]").textContent, /At least\s+₹12\.35 lakh/, "rupees rounded for display");
  assert.equal($("[data-value-amount]").getAttribute("title"), "₹12,34,568");
  assert.equal($$('[data-module="overview"] .btn.primary').length, 1, "still ONE primary button");
});

test("every kind of card has exactly one action, and a card about nothing still leads somewhere", () => {
  const kinds = ["no_control", "early_look", "underpowered", "contamination", "drift", "challenger_ready", "backfire", "uplift_not_better_than_risk", "uplift_fading", "harm_alarm"];
  for (const kind of kinds) {
    for (const over of [{}, { campaign_id: null, campaign_name: null }, { campaign_id: null, use_case_id: null }]) {
      const action = page.cardAction(card({ kind, ...over }));
      assert.ok(action.label && /^#\//.test(action.href), `${kind} ${JSON.stringify(over)} -> ${JSON.stringify(action)}`);
    }
  }
});

test("a server sentence gets the glossary's word for a noun; one that still holds an engine word is left out", async () => {
  assert.equal(page.plainWords("Some customers in the control group were contacted, in this use case."), "Some customers in the comparison group were contacted, in this goal.");
  assert.equal(page.plainWords("Use cases and Control groups"), "Goals and Comparison groups");
  assert.equal(page.showable("Keep a random group out of the next campaign."), "Keep a random group out of the next campaign.");
  assert.equal(page.showable("Keep the holdout out."), "Keep the comparison group out.");
  assert.equal(page.showable("Approve a risk model for this use case, or train the model on more customers."), "");
  await visit(answer({ ...demo, cards: [card({ code: "CLEAN", text: "A clean sentence.", next_step: "Keep the control group out of every channel." })], cards_total: 1 }));
  assert.match($("[data-card]").textContent, /A clean sentence\./);
  assert.match($(".ov-card-next").textContent, /^Keep the comparison group out of every channel\.$/);
});

test("a number that could not be traced: the cards and the value are not shown, the campaigns are", async () => {
  await visit(answer({ ...busy(), summary_checked: false, cards: [], cards_total: 0, headline: null }));
  assert.ok($("[data-unchecked]"), "says why the list is missing");
  assert.equal($$("[data-card]").length, 0);
  assert.equal($("[data-value-amount]"), null);
  assert.match($("[data-value]").textContent, /could not be checked/);
  assert.equal($$("[data-live-row]").length, 5);
});

test("a total that is not in rupees carries its unit and the server's own sentence", async () => {
  const label = "at least 984.06 extra reactivated_90d outcomes, the sum of each campaign's lower bound";
  const unit_words = "extra reactivated_90d outcomes";
  await visit(answer({ ...demo, headline: { ...demo.headline, unit: "outcomes:reactivated_90d", unit_words, total: figure(984.06, "984.06", "number"), label } }));
  assert.equal($("[data-value-amount]").textContent.replace(/\s+/g, " ").trim(), "At least 984.06 extra reactivated_90d outcomes");
  assert.match($("[data-value-meaning]").textContent, /^At least 984\.06 extra reactivated_90d outcomes, the sum of each campaign's lower bound\./);
});

test("a unit sentence that holds an engine word is replaced by the kind of unit alone", async () => {
  const label = "at least 984.06 in a holdout column, the sum of each campaign's lower bound";
  await visit(answer({ ...demo, headline: { ...demo.headline, unit: "amount:holdout_flag", unit_words: "in a holdout column and treat = 1", total: figure(984.06, "984.06", "number"), label: `${label} treat = 1` } }));
  assert.equal($("[data-value-amount]").textContent.replace(/\s+/g, " ").trim(), "At least 984.06 in total");
  assert.match($("[data-value-meaning]").textContent, /^A floor, not a forecast: the least your measured campaigns achieved/);
  assert.deepEqual(findNeverShown(text()), {});
});

test("no total while campaigns have results says so, instead of asking for results", async () => {
  await visit(answer({ ...demo, headline: null, checklist: { connected: true, campaign: true, results: true } }));
  assert.ok($("[data-value-empty]"));
  assert.match($("[data-value-meaning]").textContent, /None of your measured campaigns is counted as proven yet/);
  assert.doesNotMatch($("[data-value]").textContent, /Once a campaign's results are added/);
});

// --- a fresh install and the checklist ------------------------------------------------------------------------

test("a fresh install: empty states that say what to do, and three unticked steps with a way into each", async () => {
  clear();
  await visit(answer(fresh()));
  assert.match($("[data-attention]").textContent, /Nothing needs your attention/);
  assert.ok($("[data-value-empty]"), "no number, not a zero");
  assert.match($("[data-value]").textContent, /Nothing is proven yet/);
  assert.match($("[data-live]").textContent, /You have no campaigns yet/);
  assert.match($("[data-live]").textContent, /\+ New campaign/);
  assert.equal($$("[data-live] a, [data-live] button").length, 0, "the header button is the only way to start");
  const items = $$("[data-checklist] [data-check]");
  assert.deepEqual(items.map((i) => [i.dataset.check, i.dataset.done]), [["connected", "false"], ["campaign", "false"], ["results", "false"]]);
  assert.deepEqual(items.map((i) => i.textContent.replace(/\s+/g, " ").trim()), [
    "○Connect or upload your dataTo do",
    "○Start your first campaignTo do",
    "○Add its resultsTo do",
  ]);
  assert.match(items[0].querySelector("a").getAttribute("href"), /^#\/(data|connections)$/);
  assert.equal(items[1].querySelector("a").getAttribute("href"), "#/campaigns/new");
  assert.equal($$('[data-module="overview"] .btn.primary').length, 1);
});

test("the checklist ticks each step from real state and leaves the rest", async () => {
  clear();
  await visit(answer({ ...fresh(), checklist: { connected: true, campaign: true, results: false } }));
  const state = Object.fromEntries($$("[data-check]").map((i) => [i.dataset.check, i.dataset.done]));
  assert.deepEqual(state, { connected: "true", campaign: "true", results: "false" });
  assert.equal($('[data-check="connected"] a'), null, "a finished step is not a link");
  assert.match($('[data-check="connected"]').textContent, /✓/);
  assert.ok($('[data-check="results"] a'));
});

test("hiding the checklist is remembered in this browser and the list stays gone", async () => {
  clear();
  await visit(answer(fresh()));
  assert.ok($("[data-checklist]"));
  $("[data-checklist-hide]").click();
  await settle(2);
  assert.equal($("[data-checklist]"), null);
  assert.equal(w.localStorage.getItem("marketing-ai.overview.checklist-hidden"), "1");
  assert.ok($("[data-attention]"), "the rest of the page is still there");
  await visit(answer(fresh()));
  assert.equal($("[data-checklist]"), null, "still hidden on the next visit");
  clear();
  await visit(answer(fresh()));
  assert.ok($("[data-checklist]"), "and back when the choice is cleared");
});

test("blocked browser storage never breaks the page", async () => {
  const real = Object.getOwnPropertyDescriptor(w, "localStorage");
  Object.defineProperty(w, "localStorage", {
    configurable: true,
    get() {
      throw new Error("blocked");
    },
  });
  try {
    await visit(answer(fresh()));
    assert.ok($("[data-checklist]"), "drawn when the choice cannot be read");
    $("[data-checklist-hide]").click();
    await settle(2);
    assert.equal($("[data-checklist]"), null, "hidden for this visit even when it cannot be remembered");
  } finally {
    if (real) Object.defineProperty(w, "localStorage", real);
    else delete w.localStorage;
  }
});

// --- failure ------------------------------------------------------------------------------------------------------------

test("a failed call shows a plain error with a way to try again", async () => {
  clear();
  await visit({ status: 500, body: { detail: { code: "SOMETHING", message: "It broke." } } });
  const box = $('[data-module="overview"] .apierr');
  assert.ok(box, "an error box, not a blank page");
  assert.ok(box.querySelector("[data-retry]"));
  assert.doesNotMatch(box.textContent.replace(/Details[\s\S]*/, ""), /\b500\b/, "never a status code up front");
});

// --- words, numbers and the module's seams ---------------------------------------------------------------------------

test("no engine word is shown on any of its states, with an empty allow-list", async () => {
  clear();
  for (const [name, overview] of [
    ["demo", { status: 200, body: demo }],
    ["busy", answer(busy())],
    ["fresh", answer(fresh())],
    ["unchecked", answer({ ...busy(), summary_checked: false, cards: [], headline: null })],
  ]) {
    await visit(overview);
    assert.deepEqual(findNeverShown(text()), {}, `${name}: ${text().slice(0, 300)}`);
  }
});

test("rupees are rounded for display (lakh, crore) and exact in the tooltip's own text", () => {
  const inr = (value, textValue) => figure(value, textValue, "inr");
  assert.equal(page.fmtRupees(inr(81033.97, "₹81,034")), "₹81,034", "under a lakh the server's text");
  assert.equal(page.fmtRupees(inr(100000, "₹1,00,000")), "₹1 lakh");
  assert.equal(page.fmtRupees(inr(1234567.8, "₹12,34,568")), "₹12.35 lakh");
  assert.equal(page.fmtRupees(inr(25000000, "₹2,50,00,000")), "₹2.5 crore");
  assert.equal(page.fmtRupees(figure(984.06, "984.06", "amount")), "984.06", "other units are the server's text");
});

test("a campaign's short name is at most 40 characters and keeps its meaning", () => {
  assert.equal(page.shortName("Win back lapsed customers - March 2026, e-mail only"), "Win back lapsed customers");
  assert.equal(page.shortName("A".repeat(60)).length, 40);
  assert.ok(page.shortName("A".repeat(60)).endsWith("…"));
  assert.equal(page.shortName(""), "—");
  assert.equal(page.shortName("Hillstrom e-mail (spend) - public dataset, retrospective replay"), "Hillstrom e-mail (spend)");
});

test("dates are written as people write them", () => {
  assert.equal(page.fmtDay("2026-11-12"), "12 Nov 2026");
  assert.equal(page.fmtDay("2026-01-05"), "5 Jan 2026");
  assert.equal(page.fmtDay("not a date"), "not a date");
});

test("the page names no layout setting and imports no layout module, so it merges in any order", () => {
  for (const file of ["index.js", "page.js", "api.js"]) {
    const source = fs.readFileSync(new URL(`../../../../../ui/modules/overview/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /layout\.js|uiLayout|isCampaignLayout/, file);
  }
});

test("it claims only #/overview and is registered once", () => {
  const { phaseModules } = router;
  assert.deepEqual(phaseModules().find((m) => m.name === "overview").routes, ["overview"]);
  assert.deepEqual(overviewModule.ROUTES, ["overview"]);
});
