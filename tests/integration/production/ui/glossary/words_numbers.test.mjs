/* Plan M, M172 (DEC-1602): words and numbers, on the real pages.

   The same replayed demo as the visible-text check (replay.mjs, tests/fixtures/ui_demo/api.json): every page is drawn
   in jsdom from the API answers the validated Hillstrom demo gave. This suite pins what M172 changed beyond the words
   the visible-text check forbids:
     - goals carry their Plan M names (section 4.2, ui/labels.js GOAL_NAMES), with ids and API answers unchanged;
     - Home shows each goal's lifecycle stage as a small label, folds the operational goals under a closed
       "More goals", and has no colour legend;
     - money and numbers use ui/format.js: rupees in lakh and crore, the exact figure in a tooltip, an amount whose
       source names no currency kept without one;
     - campaigns on lists have a short name (goal and month, at most 40 characters) with the recorded label under it;
     - a name the person supplied (a file name, a campaign label) is marked `data-user`. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle } from "../harness.mjs";
import { installDemoPage, loadModules, recorded } from "./replay.mjs";

const UC = "hillstrom-email";
const TRAIN_RISK = "r_20261010_11100001";
const SCORE_RISK = "r_20261010_11100004";
const C_CONVERSION = "c_20261011_c9276210";
const C_SPEND = "c_20261011_5ee8a5d4";
const R = "₹";
const SHORT = `Best e-mail for each customer — Oct 2026`;

const { w, state } = installDemoPage({ hash: "#/__start" });
await loadModules();

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
  w.location.hash = "#/__between";
  await untilStable();
  w.location.hash = hash;
  await settle(2);
  await untilStable();
}

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

test("Home: goals in business words, the stage as a label on each card, no colour legend", async () => {
  await visit("#/");
  const cards = $$("[data-goals] a.uc");
  const byId = Object.fromEntries(cards.map((a) => [a.getAttribute("href").replace("#/uc/", ""), a]));
  assert.deepEqual(Object.keys(byId), [
    "targeted-advertisement",
    "ai-onboarding-assistant",
    "payment-propensity",
    "rca",
    "win-back-campaign",
  ]);
  assert.equal(text(byId["targeted-advertisement"].querySelector(".uc-name")), "Find customers likely to buy");
  assert.equal(text(byId["ai-onboarding-assistant"].querySelector(".uc-name")), "Answer customer questions (AI assistant)");
  assert.equal(text(byId["payment-propensity"].querySelector(".uc-name")), "Get customers to pay on time");
  assert.equal(text(byId.rca.querySelector(".uc-name")), "Understand why customers leave");
  assert.equal(text(byId["win-back-campaign"].querySelector(".uc-name")), "Win back lapsed customers");
  // The stage each card belongs to is a small label on it, from the journey's own stage names.
  const generic = recorded["GET /industries"].body.industries[0];
  for (const stage of generic.stages) {
    for (const u of stage.use_cases) {
      const card = byId[u.id] || $(`[data-more-goals] a.uc[href="#/uc/${u.id}"]`);
      assert.ok(card, `${u.id} has a card`);
      assert.equal(text(card.querySelector(".uc-stage")), stage.name, `${u.id} carries its stage`);
    }
  }
  assert.doesNotMatch(text($("#app")), /What do the colours mean/);
  assert.equal($(".ov-colours"), null);
});

test("Home: the operational goals fold under a closed 'More goals', keeping their names", async () => {
  await visit("#/");
  const more = $("[data-more-goals]");
  assert.ok(more, "a More goals section");
  assert.equal(more.tagName, "DETAILS");
  assert.equal(more.open, false, "closed");
  assert.match(text(more.querySelector("summary")), /^More goals/);
  assert.deepEqual(
    $$("[data-more-goals] a.uc").map((a) => [a.getAttribute("href"), text(a.querySelector(".uc-name"))]),
    [
      ["#/uc/order-fulfillment", "Order Fulfillment"],
      ["#/uc/fault-prediction", "Fault Prediction"],
    ],
  );
  assert.equal($('[data-goals] a[href="#/uc/order-fulfillment"]'), null);
  // The other journeys' goals are named in business words too.
  const other = text($("[data-other-goals]"));
  assert.match(other, /Choose the best e-mail for each customer/);
  assert.match(other, /Spot card accounts likely to miss a payment/);
  assert.doesNotMatch(other, /Propensity|E-mail Offer Choice/);
});

test("a goal's own page is titled by its goal name; the API's name and id are unchanged", async () => {
  await visit("#/uc/win-back-campaign");
  assert.equal(text($("main h1")), "Win back lapsed customers");
  assert.equal(recorded["GET /use-cases/win-back-campaign"].body.name, "Win-back Campaign", "the API answer is not rewritten");
  assert.deepEqual(
    $$('.seg button[data-mode]').map((b) => text(b)),
    ["Learn from past results", "Make a contact list"],
    "the mode labels the configuration writes, in a marketer's words",
  );
});

test("Results: value proven to date in the one number format, exact figures in the tooltip", async () => {
  await visit("#/results");
  const spend = $('[data-total="amount:spend"] [data-line]');
  assert.ok(spend, "the spend total's campaign line");
  const figure = spend.querySelector(".fmt");
  assert.equal(text(figure), "+984", "an amount with no currency in its source stays a plain number");
  assert.equal(figure.getAttribute("title"), "+984.06", "the exact figure, in the server's words, in the tooltip");
  assert.doesNotMatch(text(spend), new RegExp(`${R}\\s*984`), "never an invented rupee sign");
  const rupees = $('[data-total="rupees"] [data-line] .fmt');
  assert.equal(text(rupees), `${R}81,034`);
});

test("Results: every campaign on a list has a short name (goal and month) and its recorded label under it", async () => {
  await visit("#/results");
  const names = $$("[data-campaign-name]");
  assert.ok(names.length >= 4, "the attention card, value lines, campaigns list and packs list name campaigns");
  for (const name of names) {
    const sub = name.querySelector("[data-user]");
    const shortText = text(name).slice(0, text(name).length - text(sub).length).trim();
    assert.equal(shortText, SHORT);
    assert.ok(shortText.length <= 40, `${shortText} is at most 40 characters`);
    assert.ok(sub, "the recorded label is the subtitle, marked as the person's own words");
    assert.match(text(sub), /^Hillstrom e-mail \((spend|conversion)\)/);
  }
  const listed = $$("[data-campaigns] a[data-campaign]").map((a) => [a.getAttribute("data-campaign"), text(a)]);
  assert.deepEqual(listed, [
    [C_SPEND, SHORT],
    [C_CONVERSION, SHORT],
  ]);
});

test("a campaign's own page shows both names: the short one as its title, the recorded label under it", async () => {
  await visit(`#/campaigns/${C_CONVERSION}`);
  assert.equal(text($("main h1")), SHORT);
  assert.match(text($(".dc-label [data-user]")), /^Hillstrom e-mail \(conversion\)/);
  assert.match(text($("main")), /from this contact list\./);
  assert.ok($(`a[data-campaign-list][href="#/uc/${UC}/run/r_20261010_11100003"]`), "the list it was sent from, by link");
});

test("the Value Proof Pack: headline tiles in lakh and crore, the pack's exact text in the tooltip", async () => {
  await visit(`#/pilot/proof/${C_CONVERSION}`);
  const tiles = $$("[data-pe-proof-tiles] .kpi");
  assert.equal(tiles.length, 3);
  const [extra, value, net] = tiles;
  assert.match(text(extra.querySelector(".l")), /^Extra outcomes because of the campaign$/);
  assert.equal(text(extra.querySelector(".v")), "+26");
  assert.equal(text(net.querySelector(".l")), "Net value");
  const netValue = net.querySelector(".v .fmt");
  assert.equal(text(netValue), `${R}2.49 lakh`);
  assert.equal(netValue.getAttribute("title"), `${R}2,49,269 (2.49 lakh)`);
  assert.equal(text(net.querySelector(".s")), `likely ${R}88,187 to ${R}4.18 lakh`);
  assert.equal(text(value.querySelector(".v")), `${R}2.5 lakh`);
  assert.match(text($("main .sub")), new RegExp(`^${SHORT} · Proven: the customers held back were chosen at random$`));
});

test("a figure the pack could not measure is the em dash, with the pack's reason as its tooltip", async () => {
  const { proofTilesHtml } = await import("../../../../../ui/modules/pilot/screen.js");
  const pack = recorded[`GET /pilot/proof/${C_SPEND}?format=json`].body;
  const reason = "No value inputs were entered for this campaign, so its net value in rupees is not measured.";
  const sections = pack.sections.map((s) => (s.key === "net_value" ? { ...s, status: "not_measured", reason, lines: [] } : s));
  const box = document.createElement("div");
  box.innerHTML = proofTilesHtml({ ...pack, sections });
  const missing = box.querySelector("[data-pe-tile-missing]");
  assert.ok(missing, "the unmeasured section still has its tile");
  assert.equal(text(missing.querySelector(".l")), "Net value");
  assert.equal(text(missing.querySelector(".v")), "—", "never a zero");
  assert.equal(missing.querySelector(".v .fmt").getAttribute("title"), reason);
  // The measured amount with no currency in its source stays a plain number, its exact text in the tooltip.
  const extra = box.querySelector(".kpi .v .fmt");
  assert.equal(text(extra), "+3,581");
  assert.equal(extra.getAttribute("title"), "+3,580.84");
  assert.doesNotMatch(text(box.querySelector(".kpi")), new RegExp(R));
});

test("Campaigns waiting for results: each list by its short campaign name", async () => {
  await visit("#/monitoring/runs");
  const first = $$("[data-run] td:first-child").map((td) => text(td));
  assert.ok(first.length >= 2);
  for (const name of first) assert.ok(name.length <= 40 && / — Oct 2026$/.test(name), name);
});

test("a name the person supplied is marked as theirs; the words around it stay ours", async () => {
  await visit(`#/uc/${UC}/data/${TRAIN_RISK}`);
  const file = $$("[data-user]").find((el) => text(el) === "training.csv");
  assert.ok(file, "the uploaded file's name is marked data-user");
  assert.match(text(file.parentElement.parentElement), /Uploaded file training\.csv\./);
  await visit(`#/uc/${UC}/run/${SCORE_RISK}`);
  assert.ok($$("[data-user]").some((el) => text(el) === "training.csv"), "on the history of models and lists too");
});

test("the run's own tiles: counts and shares in the one number format", async () => {
  await visit(`#/uc/${UC}/output/${SCORE_RISK}`);
  const tiles = $$(".kpis .kpi").map((k) => [text(k.querySelector(".l")), text(k.querySelector(".v"))]);
  const held = tiles.find(([l]) => l === "Held back to measure results");
  assert.ok(held, `the held-back tile (${JSON.stringify(tiles)})`);
  assert.match(held[1], /^[\d,]+ \(\d+\.\d%\)$/, "a share with one decimal");
  assert.ok(tiles.some(([l, v]) => l === "Customers ranked" && /^[\d,]+$/.test(v)));
});
