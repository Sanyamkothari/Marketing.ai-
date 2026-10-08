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
