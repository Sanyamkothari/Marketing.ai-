/* Plan J M100 part B: the uplift Output page shows which offer each customer gets
   (`ui/modules/uplift/views.js` `offerChoiceCard`, from `offer_choice.json`), and the treat list card
   counts its rows per offer and per channel (`ui/modules/decide/views.js` `treatListCardHtml`). Every
   number is the server's. Run by tests/unit/decide/test_offer_choice_card_js.py, or directly:
   `node --test tests/unit/decide/offer_choice_card.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const uplift = await import(new URL("../../../ui/modules/uplift/views.js", import.meta.url));
const decide = await import(new URL("../../../ui/modules/decide/views.js", import.meta.url));

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

const arm = (position, level, label, channels, offered, net, offerCost, contactCost) => ({
  schema_version: 1,
  position,
  level,
  action_id: `${level}_action`,
  label,
  channels,
  offer_cost: offerCost,
  contact_cost: contactCost,
  cost_source: "catalogue",
  eligible_rows: 4000,
  sleeping_dog_rows: 300,
  offered_rows: offered,
  net_value_total: net,
  cost_total: 1234.5,
});

// The shape of `OfferChoiceSummary` (engine/decide/offer_run.py), as the API sends it.
const choice = {
  schema_version: 1,
  run_id: "r_20261008_0e100002",
  use_case_id: "win-back-campaign",
  chosen: true,
  code: null,
  note: null,
  rows: 9000,
  levels: ["none", "offer_a", "offer_b"],
  arms: [
    arm(1, "offer_a", "Offer A by SMS", ["sms"], 2917, 701234.4, 10, 0.5),
    arm(2, "offer_b", "Offer B by email", ["email"], 1388, 160007.2, 300, 0.25),
  ],
  no_offer_rows: 4695,
  reasons: { offer: 4305, no_eligible_offer: 1500, sleeping_dog: 1200, below_cost: 1100, over_budget: 0 },
  budget: null,
  budget_contacts: null,
  spent: 238224.1,
  net_value_total: 861241.6,
  value_per_conversion: 1000,
  value_column: null,
  catalogue_sha256: "ab".repeat(32),
  created_at: "2026-10-08T10:00:00Z",
};

test("the card counts the customers given each offer and no offer, from the server", () => {
  const plain = text(uplift.offerChoiceCard(choice));
  assert.match(plain, /Which offer each customer gets/);
  assert.match(plain, /Offer A by SMS 2,917 sms/);
  assert.match(plain, /Offer B by email 1,388 email/);
  assert.match(plain, /No offer 4,695/);
  assert.match(plain, /Cannot be reached on a channel of any offer 1,500/);
  assert.match(plain, /Every offer would make them less likely to respond 1,200/);
  assert.match(plain, /Spent/);
  assert.doesNotMatch(plain, /Budget/, "no budget was set");
  assert.doesNotMatch(plain.toLowerCase(), /uplift|feature/);
});

test("a budget is shown when the run had one", () => {
  const plain = text(uplift.offerChoiceCard({ ...choice, budget: 95289.65, spent: 95200 }));
  assert.match(plain, /Budget/);
});

test("a run that could not choose says why in the server's words, and a run of one offer shows nothing", () => {
  const note = "No offer was chosen per customer: set a value per response.";
  const plain = text(uplift.offerChoiceCard({ ...choice, chosen: false, code: "OFFER_CHOICE_NOT_MADE", note }));
  assert.match(plain, /set a value per response/);
  assert.doesNotMatch(plain, /Offer A by SMS/);
  assert.equal(uplift.offerChoiceCard(null), "");
});

test("the Output page of a scoring run of several offers draws the card", () => {
  assert.ok(uplift.OUTPUT_ARTEFACTS.includes("offer_choice.json"));
  const uc = { id: "win-back", name: "Win-back", marker: "P", stars: "★", type_label: "Predictive" };
  const run = {
    run_id: "r_20261008_0e100002",
    mode: "score",
    state: "done",
    created_at: "2026-10-08T10:00:00Z",
    file_name: "offers.csv",
    problem_type: "uplift",
  };
  const html = uplift.outputPageHtml(uc, run, { "offer_choice.json": choice });
  assert.match(html, /data-offer-choice/);
  const none = uplift.outputPageHtml(uc, run, {});
  assert.doesNotMatch(none, /data-offer-choice/);
});

const summary = {
  schema_version: 1,
  run_id: "r_20261008_0e100002",
  use_case_id: "win-back-campaign",
  total_rows: 9000,
  treat_rows: 4305,
  holdout_rows: null,
  explore_rows: null,
  suppressed_rows: 0,
  net_value_total: 861241.6,
  net_value_unit: "rupees",
  expected_gross_value_total: null,
  holdout_note: null,
  net_value_note: null,
  expected_gross_value_note: null,
  created_at: "2026-10-08T10:00:00Z",
  catalogue_sha256: "ab".repeat(32),
  offer_counts: { "Offer A by SMS": 2917, "Offer B by email": 1388 },
  channel_rows: { email: 1388, sms: 2917 },
};

test("the treat list card counts the treat rows per offer and per channel", () => {
  const plain = text(decide.treatListCardHtml(summary, { treatListHref: "/x/treat_list.csv" }));
  assert.match(plain, /By offer Offer A by SMS 2,917 Offer B by email 1,388/);
  assert.match(plain, /By channel email 1,388 sms 2,917/);
});

test("a treat list without offers or channels draws no such rows", () => {
  const { offer_counts: _o, channel_rows: _c, ...plainSummary } = summary;
  const html = decide.treatListCardHtml(plainSummary, { treatListHref: "/x/treat_list.csv" });
  assert.doesNotMatch(html, /data-treat-list-offers|data-treat-list-channels/);
});
