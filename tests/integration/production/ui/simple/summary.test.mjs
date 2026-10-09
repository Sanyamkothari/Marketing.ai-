/* Plan J M105: what needs attention and the value proven to date on Results, against the real app's answer
   (`test_simple_ui.py` builds a real measured campaign with a planted harmful band and captures
   `GET /campaigns/summary`, and the same answer of an installation with no campaign). What a person sees:
   * the card for the harmed group, in the server's words, with the figures it sent and a way to the pack;
   * the total labelled as the server labelled it, with each campaign's lower bound under it;
   * nothing at all of either without a server value: no answer, an empty answer, a refused role;
   * jargon-free words, and no number that the server did not send. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const summary = fixture("campaign_summary");
const world = { runs: fixture("runs_empty"), waiting: null, summary };
const { w } = await installWholePage({ hash: "#/results", world });
const { resultsHtml, summaryCardHtml } = await import("../../../../../ui/modules/simple/pages.js");

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

async function open(hash, ready) {
  w.location.hash = "#/";
  await settle(3);
  w.location.hash = hash;
  await until(ready, 3000, hash);
  await settle(6);
}

test("Results shows the server's cards, each with its words, its figures and the way to the pack", async () => {
  await open("#/results", () => $("[data-attention]"));
  const cards = $$("[data-attention] [data-card]");
  assert.deepEqual(
    cards.map((c) => c.getAttribute("data-card")),
    summary.cards.map((c) => c.code),
    "one card for each the server sent, in its order",
  );
  const [card] = summary.cards;
  const shown = text(cards[0]);
  for (const part of [card.title, card.text, card.next_step, card.campaign_name.text]) assert.ok(shown.includes(part), part);
  for (const fact of card.facts) assert.ok(shown.includes(fact.value.text), "a figure the server sent");
  assert.equal(
    cards[0].querySelector("[data-card-pack]").getAttribute("href"),
    `#/pilot/proof/${card.campaign_id}`,
  );
});

test("the total is labelled exactly as the server labelled it, with each campaign's lower bound", async () => {
  await open("#/results", () => $("[data-proven]"));
  const [total] = summary.proven.totals;
  const row = $(`[data-proven] [data-total="${total.unit}"]`);
  assert.ok(row, "the total of its unit");
  assert.match(total.label, /^at least .*, the sum of each campaign's lower bound$/);
  assert.ok(text(row).includes(total.label));
  for (const line of total.campaigns) {
    const li = row.querySelector(`[data-line="${line.campaign_id}"]`);
    assert.ok(li && text(li).includes(line.lower_bound.text), "each lower bound that was added");
  }
  assert.ok(text($("[data-proven]")).includes(summary.proven.rule));
});

test("every number on the two cards is one the server sent, and the words are plain", () => {
  const html = summaryCardHtml(summary);
  const page = html.replace(/<[^>]+>/g, " ");
  const sent = JSON.stringify(summary);
  const digits = page.match(/\d(?:[\d,]*\d)?(?:\.\d+)?/g) || [];
  assert.ok(digits.length, "there are numbers to check");
  for (const digit of digits) assert.ok(sent.includes(digit), `${digit} comes from the server's answer`);
  for (const jargon of [/uplift/i, /confidence/i, /p-value/i, /\bCI\b/, /interval/i, /feature/i, /covariate/i, /incrementality/i]) {
    assert.doesNotMatch(page, jargon, `plain words: ${jargon}`);
  }
});

test("nothing renders without a server value", () => {
  for (const nothing of [null, undefined, {}, "x", { cards: [], proven: null }, fixture("campaign_summary_empty")]) {
    assert.equal(summaryCardHtml(nothing), "", JSON.stringify(nothing));
  }
  const without = resultsHtml({ runs: [], summary: null });
  assert.doesNotMatch(without, /data-attention|data-proven/);
  assert.match(resultsHtml({ runs: [], summary }), /data-attention/);
  // a summary with cards but no totals draws no total, and one with totals but no cards draws no card
  assert.doesNotMatch(summaryCardHtml({ ...summary, proven: { ...summary.proven, totals: [], apart: [], excluded: [] } }), /data-proven/);
  assert.doesNotMatch(summaryCardHtml({ ...summary, cards: [] }), /data-attention/);
});

test("a role without the summary sees Results as before, with no card and no error", async () => {
  world.summary = null; // the fake answers 403, as the API does for a role that may not read it
  await open("#/results", () => $("[data-results-empty]"));
  assert.equal($("[data-attention]"), null);
  assert.equal($("[data-proven]"), null);
  assert.equal($(".error-box"), null);
  world.summary = summary;
});
