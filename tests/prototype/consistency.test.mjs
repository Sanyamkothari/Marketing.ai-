/* House rules for the prototype: the illustrative numbers must agree with
   each other, every new setting must carry a one-line hint, and every new
   panel must have a mobile layout. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, go, ev, $, $$, body, click, HTML } from "./harness.mjs";

const CSS = HTML.slice(HTML.indexOf("<style>"), HTML.indexOf("</style>"));

test("the raw-table numbers add up", () => {
  const dom = load("#/uc/rca");
  assert.equal(ev(dom, "RAW_TABLES.length"), 6);
  assert.equal(ev(dom, "RAW_TABLES[0].rows"), "20,000", "20,000 customers");
  assert.deepEqual(ev(dom, "JSON.stringify(RAW_TABLES.slice(1).map(t=>t.cov))"), "[96,96,96,96,96]");
  assert.equal(ev(dom, "RAW_TABLES[0].cov"), null, "the spine table has no coverage of itself");
  assert.equal(ev(dom, "SNAPS.length"), 6, "six snapshots");
  const mean = ev(dom, "SNAPS.reduce((a,x)=>a+x[1],0)/SNAPS.length");
  assert.equal(Math.round(mean * 10) / 10, 11.8, "the snapshot rates average the headline 11.8%");
  // 20,000 customers × 6 snapshots = 120,000 rows; 11.8% of those = 14,160 positives
  const rows = 20000 * 6;
  assert.equal(rows, 120000);
  assert.equal(Math.round(rows * mean) / 100, 14160);
  assert.ok(ev(dom, "CHECKS.map(c=>c[0]).join(' ')").includes("14,160 positive examples"));
  assert.ok(ev(dom, "CHECKS.map(c=>c[0]).join(' ')").includes("96% of event rows"));
  // 16 suggested features, 2 dropped, 14 built
  assert.equal(ev(dom, "FEATS.length"), 16);
  assert.equal(ev(dom, "DROPPED.length"), 2);
  assert.ok(ev(dom, "DROPPED.every(d=>FEATS.some(f=>f[1]===d[0]))"),
    "a dropped feature must be one that was suggested");
  // the lineage repeats the same totals
  assert.match(ev(dom, "JSON.stringify(LINEAGE)"), /14 features · 6 snapshots/);
  assert.match(ev(dom, "JSON.stringify(LINEAGE)"), /120,000 rows/);
});

test("the document numbers add up", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  assert.equal(ev(dom, "DOCS.reduce((a,d)=>a+d[1],0)"), ev(dom, "DOC_PAGES"));
  assert.equal(ev(dom, "DOCS.reduce((a,d)=>a+d[2],0)"), 1600);
  assert.equal(ev(dom, "DOC_CHUNKS"), "1,600");
  assert.equal(ev(dom, "WORST10.length"), 10);
  assert.ok(ev(dom, "EVAL.pass") > ev(dom, "EVAL.threshold"), "the run passes its threshold");
  assert.equal(ev(dom, "QA_FILE.pairs"), 300, "the same 300-question set the Model page names");
});

test("the win-back copy counts match the cards", () => {
  const dom = load("#/uc/win-back-campaign/output");
  assert.equal(ev(dom, "COPY_SEED.length"), 12);
  assert.equal(ev(dom, "new Set(COPY_SEED.map(c=>c.ch)).size"), 3);
  assert.equal(ev(dom, "new Set(COPY_SEED.map(c=>c.band)).size"), 2);
  assert.ok(ev(dom, "COPY_SEED.every(c=>c.why===''||c.status==='Blocked')"),
    "only a blocked message carries a reason");
  assert.ok(ev(dom, "COPY_SEED.every(c=>c.alt&&c.alt!==c.text)"),
    "every card has different copy to regenerate into");
  // the SMS blocked for length really is over the limit; the others are not
  assert.ok(ev(dom, "COPY_SEED.find(c=>c.ch==='SMS'&&c.v==='B'&&c.band==='High').text.length") > 160);
  assert.ok(ev(dom, "COPY_SEED.filter(c=>c.ch==='SMS'&&c.status!=='Blocked').every(c=>c.text.length<=160)"));
});

test("every new setting carries a one-line hint", () => {
  const dom = load("#/uc/rca");
  click(dom, '.pickcard[data-src="raw"]');
  click(dom, "#ob-sample");
  // sources: a hint line for every row's role
  assert.equal($$(dom, ".srchint").length, $$(dom, ".srcrow").length);
  assert.ok(ev(dom, "Object.keys(ROLE_HINT).length") === ev(dom, "ROLES.length"),
    "one hint per role, including the roles not in the sample");
  assert.ok(ev(dom, "Object.values(ROLE_HINT).every(h=>h.length>20&&h.endsWith('.'))"));
  // every required column has a message written for it
  assert.ok(ev(dom, "['customer_id','event_date'].every(k=>REQ_MSG[k])"));
  // each step body opens with a hint
  for (const n of [2, 3, 4]) {
    click(dom, `[data-ob-open="${n}"]`);
    assert.ok($(dom, ".obbody .obhint, .obbody .sechd"), `step ${n} explains itself`);
  }
  // the assistant's refusal message is explained
  go(dom, "#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  assert.match($(dom, '[data-adv="refusal"]').closest(".field").textContent, /Sent instead of a guess/);
});

test("every new panel has a mobile layout", () => {
  for (const rule of [
    ".pick{grid-template-columns:1fr}",      // step 1 choice
    ".srcrow{grid-template-columns:1fr 1fr}", // source rows
    ".maprow{grid-template-columns:1fr 1fr}", // mapping rows
    ".featrow{grid-template-columns:18px 1fr}", // feature checklist
    ".report .rrow{grid-template-columns:1fr 1fr}", // build report
    ".ccgrid{grid-template-columns:1fr}",    // campaign copy
    ".lineage{flex-direction:column}",       // lineage flow
    ".headtools{order:-1;width:100%;",       // client picker stacks above the title
  ]) {
    assert.ok(CSS.includes(rule), `no mobile rule for ${rule}`);
  }
  // and every one of those sits inside a max-width media query
  const mobile = CSS.split("@media (max-width:").slice(1).join(" ");
  for (const sel of [".pick{", ".srcrow{", ".maprow{", ".featrow{", ".ccgrid{", ".lineage{"]) {
    assert.ok(mobile.includes(sel), `${sel} mobile rule is outside a media query`);
  }
});

test("nothing new invents a number the built product would have to fake", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  // an uploaded document has no page or chunk count until it is read
  $(dom, "#f-docs").files = undefined;
  assert.equal(ev(dom, "DOCS[0][1]"), 48, "sample documents carry sample numbers");
  // the cost of a run is never guessed
  assert.ok(HTML.includes("LLM cost this run: —"));
});
