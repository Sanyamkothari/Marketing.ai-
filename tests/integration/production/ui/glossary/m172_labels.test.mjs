/* Plan M, M172 (DEC-1602): what ui/labels.js gained for the sweep - goal names (section 4.2), engine sentences in a
   marketer's words (`plainText`), the mark on names the person supplied (`userText`) - and the short campaign names
   ui/format.js builds from them. Pure data and functions: no page, so no jsdom. */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ENGINE_TEXT,
  GOAL_NAMES,
  GOAL_SHORT_NAMES,
  MORE_GOALS,
  USER_TEXT_ATTRIBUTE,
  findNeverShown,
  goalName,
  goalShortName,
  plainText,
  userText,
} from "../../../../../ui/labels.js";
import { shortCampaignName } from "../../../../../ui/format.js";

test("goal names follow Plan M section 4.2, by id; a goal not listed keeps its configured name", () => {
  const plan = {
    "targeted-advertisement": "Find customers likely to buy",
    "win-back-campaign": "Win back lapsed customers",
    "telco-churn": "Keep customers likely to leave",
    "payment-propensity": "Get customers to pay on time",
    rca: "Understand why customers leave",
    "hillstrom-email": "Choose the best e-mail for each customer",
    "ai-onboarding-assistant": "Answer customer questions (AI assistant)",
  };
  for (const [id, name] of Object.entries(plan)) {
    assert.equal(GOAL_NAMES[id], name, id);
    assert.equal(goalName(id, "whatever the configuration says"), name, id);
  }
  assert.equal(goalName("order-fulfillment", "Order Fulfillment"), "Order Fulfillment", "the operational goals are unchanged");
  assert.equal(goalName("fault-prediction", "Fault Prediction"), "Fault Prediction");
  assert.equal(goalName("bank-term-deposit", "Term Deposit Conversion"), "Term Deposit Conversion");
  assert.equal(goalName("no-such-goal"), "no-such-goal", "with no name at all, the id");
  assert.deepEqual([...MORE_GOALS], ["order-fulfillment", "fault-prediction"]);
});

test("every goal name, short name and engine-sentence replacement is itself clean", () => {
  for (const [id, name] of Object.entries(GOAL_NAMES)) assert.deepEqual(findNeverShown(name), {}, `${id}: ${name}`);
  for (const [id, name] of Object.entries(GOAL_SHORT_NAMES)) assert.deepEqual(findNeverShown(name), {}, `${id}: ${name}`);
  for (const [, to] of ENGINE_TEXT) assert.deepEqual(findNeverShown(to), {}, `"${to}"`);
});

test("a campaign's short name fits in 40 characters for every goal, with the month", () => {
  for (const id of Object.keys(GOAL_NAMES)) {
    const name = shortCampaignName(goalShortName(id), "2026-10-11T11:56:14Z");
    assert.ok(name.length <= 40, `${name} (${name.length})`);
    assert.ok(name.endsWith(" — Oct 2026"), name);
    assert.ok(!name.includes("…"), `${id}'s short name fits without being cut: ${name}`);
  }
  assert.equal(shortCampaignName("Win back lapsed customers", "2026-10-11T00:00:00Z"), "Win back lapsed customers — Oct 2026");
  assert.equal(
    shortCampaignName("A goal named at much greater length than any list has room for", "2026-10-11T00:00:00Z"),
    "A goal named at much greater… — Oct 2026",
    "too long: cut at a word, with an ellipsis",
  );
  assert.equal(shortCampaignName("Win back lapsed customers", null), "Win back lapsed customers", "no date, no month");
});

test("plainText: the engine's and the configuration's sentences in a marketer's words; anything else unchanged", () => {
  assert.equal(plainText("Train a model"), "Learn from past results");
  assert.equal(plainText("Score new data"), "Make a contact list");
  assert.equal(plainText("Run training"), "Build the model");
  assert.equal(plainText("Excluded in the run settings."), "You left it out in Advanced settings.");
  assert.equal(
    plainText("Causal: the customers held back were chosen at random"),
    "Proven: the customers held back were chosen at random",
  );
  assert.equal(
    plainText("Treated customers converted at 1.1% against 0.5% for the control group."),
    "Treated customers converted at 1.1% against 0.5% for the comparison group.",
  );
  assert.equal(
    plainText(
      "So this contact list is ranked by the approved propensity model (Ensemble (LightGBM), version 1), contacting the same number.",
    ),
    "So this contact list is ranked by the approved likelihood model, contacting the same number.",
    "an algorithm's name in brackets is left out",
  );
  assert.equal(
    plainText("It does not beat a simple yardstick (baseline (logistic regression)) on its main measure."),
    "It does not beat a simple yardstick on its main measure.",
  );
  assert.equal(plainText("Train a model, then check it"), "Train a model, then check it", "a whole-text rule matches only the whole text");
  assert.equal(plainText("Nothing to change here."), "Nothing to change here.");
  assert.equal(plainText(null), null);
  assert.equal(plainText(""), "");
});

test("userText marks a name the person supplied, escaped, and nothing around it", () => {
  assert.equal(USER_TEXT_ATTRIBUTE, "data-user");
  assert.equal(userText("training.csv"), "<span data-user>training.csv</span>");
  assert.equal(userText('a "run" <b>'), '<span data-user>a &quot;run&quot; &lt;b&gt;</span>');
  assert.equal(userText("x", "p"), "<p data-user>x</p>");
});
