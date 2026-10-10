/* Plan J M108: the cost line beside Run, the confirmation and the Cost page (`ui/cost.js`).
   Run by tests/unit/decide/test_cost_card_js.py, or directly: `node --test tests/unit/decide/cost_card.test.mjs`.
   No npm install is needed: ui/cost.js is pure. Every figure below is one a test handed in as the server's. */
import { test } from "node:test";
import assert from "node:assert/strict";

const cost = await import(new URL("../../../ui/cost.js", import.meta.url));
const { costLineHtml, costConfirmHtml, costPageHtml, usd, inr, COST_NEEDS_CONFIRMATION } = cost;

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

const estimate = {
  backend: "sagemaker",
  estimated_usd: 3.4,
  reason: null,
  basis: "An estimate at AWS's published list price, in US dollars.",
  inr: null,
  inr_reason: "No exchange rate has been set by an administrator, so the amount is shown in US dollars only.",
  cap_usd: null,
  needs_confirmation: false,
  lines: [
    { kind: "training", label: "Training the model", usd: 3.4, detail: "Up to 2 hours on 1 x ml.m5.2xlarge.", reason: null, in_total: true },
    { kind: "scoring", label: "Scoring new data with this model later (a separate run)", usd: 1.2, detail: "Up to 2 hours.", reason: null, in_total: false },
  ],
};

test("the code the screen waits for is the server's", () => {
  assert.equal(COST_NEEDS_CONFIRMATION, "RUN_COST_NEEDS_CONFIRMATION");
});

test("a priced run says what it could cost, and how it is worked out", () => {
  const plain = text(costLineHtml(estimate));
  assert.match(plain, /could cost up to \$3\.40/);
  assert.match(plain, /How this is worked out/);
  assert.match(plain, /Training the model\s*: \$3\.40/);
  assert.match(plain, /\(not counted here\)/);
  assert.doesNotMatch(plain, /INR|₹/);
});

test("rupees are drawn only when the server sent them, with the rate and where it came from", () => {
  const withInr = {
    ...estimate,
    inr: { amount: 287.3, inr_per_usd: 84.5, source: "Finance sheet", as_of: "2026-10-01" },
    inr_reason: null,
  };
  const plain = text(costLineHtml(withInr));
  assert.match(plain, /₹287\.30/);
  assert.match(plain, /84\.5 rupees to the dollar/);
  assert.match(plain, /Finance sheet, 2026-10-01/);
});

test("a cost the server could not state is its reason, never a number", () => {
  const html = costLineHtml({
    ...estimate,
    estimated_usd: null,
    reason: "No AWS price list is installed on this deployment, so the cost cannot be estimated.",
    lines: [{ kind: "training", label: "Training the model", usd: null, detail: "", reason: "No AWS price list is installed.", in_total: true }],
  });
  const plain = text(html);
  assert.match(plain, /cannot be estimated/);
  assert.match(plain, /Training the model\s*: not known/);
  assert.doesNotMatch(plain, /\$0|\$ ?0\.00/);
});

test("a deployment on its own machine gets no line at all", () => {
  assert.equal(costLineHtml({ ...estimate, backend: "local", estimated_usd: null, reason: "x" }), "");
  assert.equal(costLineHtml(null), "");
});

test("a cap is named, and so is the confirmation it will ask for", () => {
  const plain = text(costLineHtml({ ...estimate, cap_usd: 2, needs_confirmation: true }));
  assert.match(plain, /limit set for one run is \$2\.00, so you will be asked to confirm/);
  const quiet = text(costLineHtml({ ...estimate, cap_usd: 10, needs_confirmation: false }));
  assert.match(quiet, /limit set for one run is \$10\.00\./);
  assert.doesNotMatch(quiet, /asked to confirm/);
});

test("words from the server are escaped", () => {
  const html = costLineHtml({ ...estimate, estimated_usd: null, reason: "<img src=x onerror=alert(1)>", lines: [] });
  assert.doesNotMatch(html, /<img/);
  assert.match(costConfirmHtml("<script>x</script> Not started."), /&lt;script&gt;/);
});

test("the confirmation carries the server's sentence and the two ways out", () => {
  const html = costConfirmHtml("This run was not started. It could cost up to USD 3.40, more than the USD 2.00 limit.");
  assert.match(text(html), /This run was not started\. It could cost up to USD 3\.40/);
  assert.match(html, /id="f-cost-confirm"/);
  assert.match(html, /id="f-cost-decline"/);
  assert.equal(costConfirmHtml(null), "");
});

test("a very small amount does not read as zero", () => {
  assert.match(usd(0.004), /under \$0\.01/);
  assert.equal(usd(0), "$0.00");
  assert.match(inr(1234.5), /₹1,234\.50/);
});

const spend = {
  basis: "Estimates at AWS's published list price, in US dollars.",
  months: [
    { month: "2026-10", runs: 3, priced_runs: 2, unpriced_runs: 1, estimated_usd: 7.5, inr: null },
    { month: "2026-09", runs: 0, priced_runs: 0, unpriced_runs: 0, estimated_usd: null, inr: null },
    { month: "2026-08", runs: 2, priced_runs: 0, unpriced_runs: 2, estimated_usd: null, inr: null },
  ],
};

test("the Cost page shows each month, and no figure where the server had none", () => {
  const plain = text(costPageHtml({ spend, fx: null }));
  assert.match(plain, /2026-10 3 runs, 1 with no figure \$7\.50/);
  assert.match(plain, /2026-09 no runs no figure/);
  assert.match(plain, /2026-08 2 runs, 2 with no figure no figure/);
  assert.match(plain, /No exchange rate is set/);
});

test("rupee amounts per month appear only with a saved rate", () => {
  const rated = {
    ...spend,
    months: [{ ...spend.months[0], inr: { amount: 633.75, inr_per_usd: 84.5, source: "Finance", as_of: "2026-10-01" } }],
  };
  const fx = { inr_per_usd: 84.5, source: "Finance", as_of: "2026-10-01" };
  const plain = text(costPageHtml({ spend: rated, fx }));
  assert.match(plain, /₹633\.75/);
  assert.match(plain, /84\.5 rupees to the dollar, from Finance \(2026-10-01\)/);
});

test("only a person who may save the rate gets the form", () => {
  assert.doesNotMatch(costPageHtml({ spend, fx: null, canEdit: false }), /data-fx-form/);
  const editable = costPageHtml({ spend, fx: { inr_per_usd: 84, source: "x", as_of: "2026-10-01" }, canEdit: true });
  assert.match(editable, /data-fx-form/);
  assert.match(editable, /data-fx-clear/);
  assert.doesNotMatch(costPageHtml({ spend, fx: null, canEdit: true }), /data-fx-clear/);
});
