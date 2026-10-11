/* Plan M M172 (DEC-1602): the one number and money format (`ui/format.js`). Run by tests/unit/ux/test_format_js.py,
   or directly: `node --test tests/unit/ux/format.test.mjs`. No npm install is needed: the module is pure. These
   are the snapshots of the money format the plan asks for: lakh and crore boundaries, negatives, the exact figure
   in the tooltip, and the em dash (with its reason) for a value nobody measured. */
import { test } from "node:test";
import assert from "node:assert/strict";

const f = await import(new URL("../../../ui/format.js", import.meta.url));

const R = "₹";
const M = "−";

test("rupees below a lakh: Indian grouping, whole rupees from 100, paise below", () => {
  assert.equal(f.rupees(0), `${R}0`);
  assert.equal(f.rupees(0.4), `${R}0.40`);
  assert.equal(f.rupees(12), `${R}12`);
  assert.equal(f.rupees(99.5), `${R}99.50`);
  assert.equal(f.rupees(100.4), `${R}100`);
  assert.equal(f.rupees(45300), `${R}45,300`);
  assert.equal(f.rupees(99999), `${R}99,999`);
  assert.equal(f.rupees("3578.2"), `${R}3,578`, "a numeric string from a CSV reads as a number");
});

test("the lakh boundary: 1,00,000 is the first lakh, and an amount that rounds up to it is one", () => {
  assert.equal(f.rupees(100000), `${R}1 lakh`);
  assert.equal(f.rupees(99999.6), `${R}1 lakh`, "₹99,999.60 rounds to ₹1,00,000");
  assert.equal(f.rupees(418250), `${R}4.18 lakh`);
  assert.equal(f.rupees(450000), `${R}4.5 lakh`, "a trailing zero is dropped");
  assert.equal(f.rupees(1250000), `${R}12.5 lakh`);
  assert.equal(f.rupees(9949999), `${R}99.5 lakh`);
});

test("the crore boundary: 99.995 lakh and up is a crore, never '100 lakh'", () => {
  assert.equal(f.rupees(9999999), `${R}1 crore`);
  assert.equal(f.rupees(9999500), `${R}1 crore`);
  assert.equal(f.rupees(10000000), `${R}1 crore`);
  assert.equal(f.rupees(12000000), `${R}1.2 crore`);
  assert.equal(f.rupees(123456789), `${R}12.35 crore`);
  assert.equal(f.rupees(123456789000), `${R}12,345.68 crore`, "crores are grouped too");
});

test("negatives and signed amounts use the real minus sign; zero takes no sign", () => {
  assert.equal(f.rupees(-230000), `${M}${R}2.3 lakh`);
  assert.equal(f.rupees(-450), `${M}${R}450`);
  assert.equal(f.rupees(-0.4), `${M}${R}0.40`);
  assert.equal(f.rupees(418250, { signed: true }), `+${R}4.18 lakh`);
  assert.equal(f.rupees(-12000000, { signed: true }), `${M}${R}1.2 crore`);
  assert.equal(f.rupees(0, { signed: true }), `${R}0`);
  assert.equal(f.rupees(-0.001), `${R}0`);
});

test("the exact figure for the tooltip: Indian grouping, paise only when there are some", () => {
  assert.equal(f.rupeesExact(418250), `${R}4,18,250`);
  assert.equal(f.rupeesExact(12345678.9), `${R}1,23,45,678.90`);
  assert.equal(f.rupeesExact(-230000), `${M}${R}2,30,000`);
  assert.equal(f.rupeesExact(999), `${R}999`);
  assert.equal(f.groupIndian("41825000"), "4,18,25,000");
  assert.equal(f.groupIndian("100"), "100");
});

test("a missing value is the em dash, never zero", () => {
  for (const v of [null, undefined, "", NaN, "n/a", Infinity, true]) {
    assert.equal(f.rupees(v), "—", String(v));
    assert.equal(f.money(v, "USD"), "—", String(v));
    assert.equal(f.count(v), "—", String(v));
    assert.equal(f.percent(v), "—", String(v));
  }
});

test("money keeps the unit its source names: rupees, another currency, or none at all", () => {
  assert.equal(f.money(418250), `${R}4.18 lakh`, "rupees are the default");
  assert.equal(f.money(3578.4, "USD"), "$3,578");
  assert.equal(f.money(1.39, "USD"), "$1.39");
  assert.equal(f.money(-0.84, "USD", { signed: true }), `${M}$0.84`);
  assert.equal(f.money(3578.4, null), "3,578", "no currency in the source: a plain number, never rupees");
  assert.equal(f.money(0.84, null, { signed: true }), "+0.84");
  assert.ok(!f.money(3578.4, null).includes(R));
  assert.equal(f.moneyExact(3578.4, null), "3,578.4");
});

test("percentages have one decimal, counts are whole and grouped, signed numbers line up", () => {
  assert.equal(f.percent(0.39), "39.0%");
  assert.equal(f.percent(0.0123), "1.2%");
  assert.equal(f.percent(-0.0123, { signed: true }), `${M}1.2%`);
  assert.equal(f.percent(0.006, { signed: true }), "+0.6%");
  assert.equal(f.percent(0.00001, { signed: true }), "0.0%", "a value that rounds to zero takes no sign");
  assert.equal(f.percentExact(0.01234), "1.234%");
  assert.equal(f.count(32000), "32,000");
  assert.equal(f.count(1645.4), "1,645");
  assert.equal(f.count(-3), `${M}3`);
  assert.equal(f.count(26, { signed: true }), "+26");
  assert.equal(f.signedNumber(0.84), "+0.84");
  assert.equal(f.signedNumber(-3), `${M}3`);
});

test("the HTML forms: the exact figure in the title, the reason on a missing value", () => {
  assert.equal(f.moneyHtml(418250), `<span class="fmt" title="${R}4,18,250">${R}4.18 lakh</span>`);
  assert.equal(f.moneyHtml(450), `<span class="fmt">${R}450</span>`, "no tooltip that repeats the figure");
  assert.equal(f.moneyHtml(null, { reason: "No results yet" }), '<span class="fmt" title="No results yet">—</span>');
  assert.equal(f.moneyHtml(null), "—");
  assert.equal(f.percentHtml(0.01234), '<span class="fmt" title="1.234%">1.2%</span>');
  assert.equal(f.percentHtml(0.39), '<span class="fmt">39.0%</span>');
  assert.equal(f.countHtml(32000), '<span class="fmt">32,000</span>');
  assert.equal(f.numberHtml("<b>", "a\"b"), '<span class="fmt" title="a&quot;b">&lt;b&gt;</span>', "both are escaped");
});

test("a per-unit amount below half a paisa keeps the source's own text, never Rs 0 (M172 review)", () => {
  // engine/pilot/proof.py `_unit_inr` keeps the decimals so a tiny cost never prints as zero; the page keeps them too.
  assert.equal(f.figureHtml({ value: 0.004, text: `${R}0.0040`, format: "inr_unit" }), `<span class="fmt">${R}0.0040</span>`);
  assert.equal(f.figureHtml({ value: -0.004, text: `-${R}0.0040`, format: "inr_unit" }), `<span class="fmt">−${R}0.0040</span>`);
  // From half a paisa the one format applies as before, and a real zero is still Rs 0.
  assert.equal(f.figureHtml({ value: 0.3, text: `${R}0.30`, format: "inr_unit" }), `<span class="fmt">${R}0.30</span>`);
  assert.equal(f.figureHtml({ value: 0, text: `${R}0`, format: "inr_unit" }), `<span class="fmt">${R}0</span>`);
  assert.equal(f.figureHtml({ value: 0.004, text: `${R}0.0040`, format: "inr" }), `<span class="fmt">${R}0.0040</span>`);
});

test("a day: '12 Nov 2026' in UTC from a date or an instant, the em dash for nothing (Plan M final integration)", () => {
  assert.equal(f.day("2026-11-12"), "12 Nov 2026");
  assert.equal(f.day("2026-01-05"), "5 Jan 2026", "no leading zero");
  assert.equal(f.day("2026-10-11T23:30:00Z"), "11 Oct 2026", "an instant is read in UTC");
  assert.equal(f.day("2026-10-11T23:30:00-05:00"), "12 Oct 2026", "an offset is converted to UTC first");
  for (const v of [null, undefined, "", "not a date"]) assert.equal(f.day(v), "—", String(v));
});
