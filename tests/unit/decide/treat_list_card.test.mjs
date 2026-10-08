/* Plan J M98: the treat list card on a scoring run's Output page (`ui/modules/decide/views.js`
   `treatListCardHtml`). Run by tests/unit/decide/test_treat_list_card_js.py, or directly:
   `node --test tests/unit/decide/treat_list_card.test.mjs`. No npm install is needed.

   The card draws `treat_list_summary.json` in the server's own words. The first review found it read
   fields the summary does not have (`rows`, `treated`, `held_out`), so it drew zeros, and that a failed
   request drew nothing at all. */
import { test } from "node:test";
import assert from "node:assert/strict";

const views = await import(new URL("../../../ui/modules/decide/views.js", import.meta.url));
const { treatListCardHtml } = views;

// The shape of `TreatListSummary` (engine/decide/treat_list.py), as the API sends it.
const summary = {
  schema_version: 1,
  run_id: "r_20261001_0a000002",
  use_case_id: "win-back-campaign",
  total_rows: 24,
  treat_rows: 9,
  holdout_rows: 5,
  explore_rows: 1,
  suppressed_rows: 3,
  net_value_total: null,
  net_value_unit: null,
  expected_gross_value_total: 1234.5,
  holdout_note: null,
  net_value_note: "Net value is incremental and needs an uplift run; see expected gross value for a propensity run.",
  expected_gross_value_note: "Expected gross value is ... It is not incremental: it counts customers who would have responded.",
  created_at: "2026-10-01T09:00:00Z",
};

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

test("the card shows the summary's own counts, not zeros", () => {
  const html = treatListCardHtml(summary, { treatListHref: "/runs/r/treat_list.csv" });
  const plain = text(html);
  assert.match(plain, /Customers 24/);
  assert.match(plain, /To treat \(treat = 1\) 9/);
  assert.match(plain, /Held back \(holdout = 1\) 5/);
  assert.match(plain, /Explore \(explore = 1\) 1/);
  assert.match(plain, /Not to be contacted 3/);
});

test("it says to include treat = 1 and exclude holdout = 1", () => {
  const plain = text(treatListCardHtml(summary, { treatListHref: "/x/treat_list.csv" }));
  assert.match(plain, /Include treat = 1, exclude holdout = 1\./);
});

test("gross value is labelled not incremental and net value is left out when null", () => {
  const plain = text(treatListCardHtml(summary, { treatListHref: "/x/treat_list.csv" }));
  assert.match(plain, /Expected gross value of those treated \(not incremental\)/);
  assert.doesNotMatch(plain, /Predicted net value of those treated/);
  assert.match(plain, /It is not incremental/, "the server's note is shown");
  const uplift = { ...summary, net_value_total: 77, expected_gross_value_total: null };
  const upliftPlain = text(treatListCardHtml(uplift, { treatListHref: "/x/treat_list.csv" }));
  assert.match(upliftPlain, /Predicted net value of those treated/);
  assert.doesNotMatch(upliftPlain, /Expected gross value of those treated/);
});

test("an unknown holdout count is a dash, never zero, with the server's note", () => {
  const unknown = { ...summary, holdout_rows: null, holdout_note: "The run has no holdout assignment file, so the holdout flag is not known." };
  const plain = text(treatListCardHtml(unknown, { treatListHref: "/x/treat_list.csv" }));
  assert.match(plain, /Held back \(holdout = 1\) —/);
  assert.match(plain, /holdout flag is not known/);
});

test("the download is labelled as the treat list, and the contact list is another file", () => {
  const html = treatListCardHtml(summary, { treatListHref: "/runs/r/treat_list.csv" });
  assert.match(html, /<a [^>]*href="\/runs\/r\/treat_list\.csv"[^>]*>Download treat list \(CSV\)<\/a>/);
  assert.doesNotMatch(html, /Download contact list/);
  assert.match(text(html), /The contact list above is the scored list before that choice/);
});

test("a failed request shows the server's message instead of nothing", () => {
  const error = { code: "RUN_NOT_SCORED", message: "This run did not save the settings it was scored with (run_config.json), so its treat list cannot be built from them." };
  const html = treatListCardHtml(null, { treatListHref: "/x/treat_list.csv", error });
  assert.match(html, /data-treat-list-error/);
  assert.match(text(html), /The treat list could not be loaded\. This run did not save the settings/);
  assert.doesNotMatch(html, /Download treat list/, "no link to a file that cannot be built");
});

test("the message is escaped, and a missing summary with no error draws nothing yet", () => {
  const html = treatListCardHtml(null, { error: { message: "<img src=x onerror=alert(1)>" } });
  assert.doesNotMatch(html, /<img/);
  assert.equal(treatListCardHtml(null, { treatListHref: "/x" }), "");
});
