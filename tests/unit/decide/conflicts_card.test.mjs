/* Plan J M101: the arbitration conflicts card on Results (`ui/modules/decide/views.js`
   `conflictsCardHtml`). Run by tests/unit/decide/test_conflicts_card_js.py, or directly:
   `node --test tests/unit/decide/conflicts_card.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const views = await import(new URL("../../../ui/modules/decide/views.js", import.meta.url));
const { conflictsCardHtml } = views;

const summary = {
  total_customers: 100,
  customers_with_actions: 80,
  customers_with_conflicts: 30,
  treated_customers: 50,
  dropped_actions_count: 35,
  channel_capped_count: 5,
  winning_by_use_case: {
    "win-back-campaign": 30,
    "targeted-advertisement": 20,
  },
  dropped_by_use_case: {
    "win-back-campaign": 10,
    "targeted-advertisement": 25,
  },
};

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

test("returns empty string when no summary or zero actions", () => {
  assert.equal(conflictsCardHtml(null), "");
  assert.equal(conflictsCardHtml({ total_customers: 0, customers_with_actions: 0 }), "");
});

test("renders conflict metrics and counts", () => {
  const html = conflictsCardHtml(summary);
  const plain = text(html);
  assert.match(plain, /Arbitration & Conflicts/);
  assert.match(plain, /Total customers evaluated 100/);
  assert.match(plain, /Customers qualifying for actions 80/);
  assert.match(plain, /Customers with conflicts \((>|&gt;)1 action\) 30/);
  assert.match(plain, /Treated customers \(winners\) 50/);
  assert.match(plain, /Dropped actions \(conflict suppressed\) 35/);
  assert.match(plain, /Channel cap suppressed 5/);
});

test("renders use case breakdown table", () => {
  const html = conflictsCardHtml(summary);
  const plain = text(html);
  assert.match(plain, /win-back-campaign/);
  assert.match(plain, /targeted-advertisement/);
  assert.match(plain, /Winning actions/);
  assert.match(plain, /Dropped actions/);
});

test("says how conflicts were settled, only for the ways that happened", () => {
  const plain = text(
    conflictsCardHtml({
      ...summary,
      customers_decided_by_value: 12,
      customers_decided_by_priority: 7,
      customers_decided_by_request_order: 3,
      customers_decided_by_explore: 2,
      holdout_blocked_actions: 4,
    }),
  );
  assert.match(plain, /Settled by priority times value 12/);
  assert.match(plain, /Settled by priority alone \(value missing or of a different kind\) 7/);
  assert.match(plain, /Settled by use case order \(a tie\) 3/);
  assert.match(plain, /Kept because chosen at random \(explore\) 2/);
  assert.match(plain, /Actions blocked by a hold-out 4/);
  const none = text(conflictsCardHtml(summary));
  assert.doesNotMatch(none, /Settled by/);
  assert.doesNotMatch(none, /hold-out/);
});

test("names the contested customers a channel cap left with no action, only when there are some", () => {
  const plain = text(conflictsCardHtml({ ...summary, contested_customers_channel_capped: 6 }));
  assert.match(plain, /Wanted by several use cases, left with no action by a channel cap 6/);
  assert.doesNotMatch(text(conflictsCardHtml(summary)), /left with no action/);
});

test("names the actions another use case's control group blocked, only when there are some", () => {
  const plain = text(conflictsCardHtml({ ...summary, control_blocked_actions: 9 }));
  assert.match(plain, /Actions blocked by another use case's control group 9/);
  assert.doesNotMatch(text(conflictsCardHtml(summary)), /control group/);
});
