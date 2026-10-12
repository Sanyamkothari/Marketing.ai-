/* Plan O M242 (DEC-1812): the workspace currency in `ui/format.js`. Run by tests/unit/ux/test_workspace_currency_js.py,
   or directly: `node --test tests/unit/ux/workspace_currency.test.mjs`. An INR workspace's figures are exactly what
   they were; a dollar workspace shows dollars; a figure that names its currency keeps it; none named, none invented. */
import { test } from "node:test";
import assert from "node:assert/strict";

const f = await import(new URL("../../../ui/format.js", import.meta.url));

const R = "₹";

function inWorkspace(code, body) {
  f.setWorkspaceCurrency(code);
  try {
    body();
  } finally {
    f.setWorkspaceCurrency("INR");
  }
}

test("an INR workspace is the default, and its money is exactly what it was", () => {
  assert.equal(f.getWorkspaceCurrency(), "INR");
  assert.equal(f.money(418250), `${R}4.18 lakh`);
  assert.equal(f.moneyExact(418250), `${R}4,18,250`);
  assert.equal(f.moneyHtml(418250), `<span class="fmt" title="${R}4,18,250">${R}4.18 lakh</span>`);
  inWorkspace("INR", () => {
    assert.equal(f.money(418250), f.rupees(418250));
    assert.equal(f.moneyHtml(450), `<span class="fmt">${R}450</span>`);
  });
});

test("a dollar workspace shows dollars where no currency is given, and rupees where rupees are named", () => {
  inWorkspace("USD", () => {
    assert.equal(f.getWorkspaceCurrency(), "USD");
    assert.equal(f.money(3578.4), "$3,578");
    assert.equal(f.moneyExact(3578.4), "$3,578.40");
    assert.equal(f.moneyHtml(3578.4), '<span class="fmt" title="$3,578.40">$3,578</span>');
    assert.ok(!f.money(3578.4).includes(R), "never a rupee sign on a dollar amount");
    assert.equal(f.money(418250, "INR"), `${R}4.18 lakh`, "an amount that names rupees keeps them");
    assert.equal(f.money(3578.4, null), "3,578", "an amount whose source names no currency stays a plain number");
  });
  assert.equal(f.money(3578.4), `${R}3,578`, "back in an INR workspace");
});

test("anything but a currency code leaves the workspace in rupees", () => {
  for (const bad of [undefined, null, "", "usd", "US$", "DOLLARS", 7]) {
    assert.equal(f.setWorkspaceCurrency(bad), "INR", String(bad));
  }
  f.setWorkspaceCurrency("INR");
});

test("a server figure of format money keeps its currency; none named, a plain number", () => {
  assert.equal(f.figureHtml({ value: 3578.4, text: "$3,578.40", format: "money", currency: "USD" }),
    '<span class="fmt" title="$3,578.40">$3,578</span>');
  assert.equal(f.figureHtml({ value: 3578.4, format: "money" }), '<span class="fmt" title="3,578.4">3,578</span>');
  assert.equal(f.figureHtml({ value: 3578.4, format: "money", currency: "<b>" }),
    '<span class="fmt" title="3,578.4">3,578</span>', "a currency that is not a code is not used");
});

test("a currency in words, for labels", () => {
  assert.equal(f.currencyName("INR"), "rupees");
  assert.equal(f.currencyName("USD"), "US dollars");
  assert.equal(f.currencyName("XYZ"), "XYZ");
});
