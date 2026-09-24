/* The screens that were already in the prototype must not move.
   Phase 2 and Phase 3a add screens; they do not redraw the old ones. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, go, ev, settle, $, $$, HTML } from "./harness.mjs";

test("the lifecycle overview is unchanged", () => {
  const dom = load("#/");
  assert.deepEqual(
    $$(dom, ".stage-pill").map((e) => e.textContent.replace(/^\d+/, "")),
    ["Awareness", "Onboarding", "Service / Payments", "Churn", "Win-back"],
  );
  assert.deepEqual(
    $$(dom, ".uc").map((e) => e.getAttribute("href")),
    ["#/uc/targeted-advertisement", "#/uc/ai-onboarding-assistant",
     "#/uc/order-fulfillment", "#/uc/fault-prediction", "#/uc/payment-propensity",
     "#/uc/rca", "#/uc/win-back-campaign"],
  );
  // v1: the breadcrumb root is Home, the H1 is the journey, one sentence says what to do, and one
  // toggletip about the colours replaces the legend and the per-stage type chips
  assert.equal($(dom, ".crumbs").textContent, "Home");
  assert.equal($(dom, ".h1").textContent, "Customer Lifecycle");
  assert.match($(dom, ".desc").textContent, /^Pick what you want to predict or improve\./);
  assert.match($(dom, ".desc .tt-pop").textContent, /Predictive AI.*Generative AI.*Hybrid/);
  assert.equal($(dom, ".legend"), null);
  assert.equal($(dom, ".stage-col .chip.type"), null);
  assert.ok($(dom, ".foryou"), "a 'For you' line with one next step");
});

test("every use-case definition text is unchanged", () => {
  const dom = load("#/");
  assert.equal(
    ev(dom, "UC.find(u=>u.id==='rca').desc"),
    "Flags customers likely to churn and uses an LLM to summarise the root cause behind each one, with a recommended retention action.",
  );
  assert.equal(
    ev(dom, "UC.find(u=>u.id==='ai-onboarding-assistant').desc"),
    "A chat assistant grounded in product documentation that guides new customers through setup and answers their questions.",
  );
  assert.equal(ev(dom, "UC.length"), 7);
  assert.deepEqual(ev(dom, "JSON.stringify(UC.map(u=>u.name))"), JSON.stringify(
    ["Targeted Advertisement", "AI Onboarding Assistant", "Order Fulfillment",
     "Fault Prediction", "Payment Propensity", "RCA (Root Cause Analysis)", "Win-back Campaign"]));
});

test("the eight advanced-settings stages are unchanged", () => {
  const dom = load("#/uc/targeted-advertisement");
  $(dom, "#f-sample").click();
  $(dom, "#f-adv").open = true;
  $(dom, "#f-adv").dispatchEvent(new dom.window.Event("toggle"));
  dom.window.render();
  $(dom, "#f-adv").open = true;
  const titles = $$(dom, ".stage-d .st").map((e) => e.textContent);
  assert.deepEqual(titles, [
    "Data preparation", "Data split", "Feature engineering", "Model search",
    "Evaluation & explainability", "Actions & output", "Monitoring & retraining",
    "Governance & privacy",
  ]);
});

test("the generative pipeline still has four stages", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  $(dom, "#f-sampledocs").click();
  const titles = $$(dom, ".stage-d .st").map((e) => e.textContent);
  assert.deepEqual(titles, ["Documents", "Retrieval", "Generation", "Evaluation"]);
});

test("the three setup steps and the run states still exist", async () => {
  const dom = load("#/uc/payment-propensity");
  assert.equal($$(dom, ".fstep").length, 3);
  assert.deepEqual($$(dom, ".flabel").map((e) => e.textContent.trim()),
    ["Dataset", "Columns", "Model"]);
  $(dom, "#f-sample").click();
  assert.equal($(dom, "#f-run").disabled, false);
  $(dom, "#f-setup").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  assert.ok($(dom, ".progress"), "running state renders a progress list");
  await settle(dom);
  assert.ok($(dom, ".results"), "results state renders");
  assert.equal($$(dom, ".flow .block").length, 3);
});

test("the colour tokens and dark mode are unchanged", () => {
  for (const token of ["--p:#2F6FDB", "--g:#7A55D3", "--h:#12957F",
                       "--brand-blue:#1E57BD", "--brand-yellow:#FFD500"]) {
    assert.ok(HTML.includes(token), `missing light token ${token}`);
  }
  assert.ok(HTML.includes('@media (prefers-color-scheme: dark)'));
  assert.ok(HTML.includes(':root[data-theme="dark"]'));
  // new panels must not introduce a hard-coded colour of their own
  const newCss = HTML.slice(HTML.indexOf("/* --- header client picker"), HTML.indexOf("</style>"));
  const hardCoded = newCss.match(/#[0-9a-fA-F]{3,6}\b/g) || [];
  assert.deepEqual(hardCoded, [], `new CSS must use tokens, found ${hardCoded}`);
});

test("every Data / Model / Output page still renders for every use case", () => {
  const dom = load("#/");
  for (const id of ev(dom, "JSON.stringify(UC.map(u=>u.id))").split('","').map((s) => s.replace(/[[\]"]/g, ""))) {
    for (const page of ["data", "model", "output"]) {
      go(dom, `#/uc/${id}/${page}`);
      assert.ok($$(dom, ".kpi").length >= 4, `${id}/${page} lost its KPIs`);
      assert.ok($(dom, ".tabs .tab.on"), `${id}/${page} lost its tab bar`);
    }
  }
});
