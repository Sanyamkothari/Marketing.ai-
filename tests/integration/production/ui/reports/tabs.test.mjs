/* Plan M, M178 (DEC-1608): Reports, `#/reports[/<tab>]`, in jsdom - the page as ui/index.html loads it, against the
   API answers recorded from the validated Hillstrom demo.

   Value proof: the Value Proof Packs with the server's headline, a short name, what the result can claim in words
   (Proven / Estimated / Not yet proven) and the old pack and PDF links. Programme summary: the block Results
   draws, drawn here too from the same source. Check a past campaign: the audit form in three steps (the equality of its request with the old
   form's is audit_equal.test.mjs). Model reports: today's `#/pilot` lists, in a closed technical section.
   The acceptance test lists every report reachable from `#/pilot` today and finds each of them here. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle } from "../harness.mjs";
import { findNeverShown } from "../../../../../ui/labels.js";
import { visibleText } from "../glossary/collect.mjs";
import { installReports, open, recorded, text } from "./fake.mjs";

const proofs = recorded["GET /pilot/proof"].body.proofs;
const [spend, conversion] = proofs;

const { w, overlay } = await installReports({ hash: "#/reports" });

const proofRows = () => $$('[data-proofs] tbody tr');
const proofsReady = () => proofRows().length > 0 || $("[data-proofs-empty]");

test("the page has one H1, one sentence, four tabs and one primary button", async () => {
  await open(w, "#/reports", proofsReady);
  assert.equal($$("h1").length, 1);
  assert.equal(text($("h1")), "Reports");
  assert.equal($$(".head .desc").length, 1);
  assert.deepEqual($$("[data-reports-tab]").map((a) => text(a)), ["Value proof", "Programme summary", "Check a past campaign", "Model reports"]);
  assert.equal($$("main .btn.primary").length, 1, "one primary button on the page");
  assert.equal(text($("main .btn.primary")), "Open the newest Value Proof Pack");
  assert.equal($("[data-reports-tab][aria-current]").dataset.reportsTab, "value-proof");
  assert.equal($("[data-reports-page]").dataset.tab, "value-proof");
});

test("value proof: a short name, the server's one line, what it can claim in words, Open and PDF", () => {
  assert.equal(proofRows().length, proofs.length);
  const row = proofRows().find((tr) => tr.querySelector(`[data-proof-campaign="${conversion.campaign_id}"]`));
  assert.equal(text(row.querySelector(".rp-name")), "Hillstrom e-mail (conversion)", "the description after the dash is dropped");
  assert.equal(row.querySelector(".rp-name").getAttribute("title"), conversion.name, "the full name stays one hover away");
  assert.equal(text(row.querySelector('td[data-label="What it shows"]')), conversion.headline, "the server's sentence, unchanged");
  assert.match(conversion.headline, /₹/, "the recorded sentence carries rupees");
  assert.equal(text(row.querySelector("[data-claim]")), "Proven");
  assert.equal(row.querySelector("[data-claim]").dataset.claim, "proven");
  const open = row.querySelector("[data-proof-open]");
  assert.equal(open.getAttribute("href"), `#/pilot/proof/${conversion.campaign_id}`, "the existing Value Proof Pack page");
  const pdf = row.querySelector("[data-proof-pdf]");
  assert.match(pdf.getAttribute("href"), new RegExp(`/pilot/proof/${conversion.campaign_id}\\?format=pdf$`));
  assert.ok(pdf.hasAttribute("download"));
  assert.equal($("main .btn.primary").getAttribute("href"), `#/pilot/proof/${proofs[0].campaign_id}`, "the newest pack");
});

test("value proof: the three claims and a refused pack, in words and never as the engine's label", async () => {
  overlay["GET /pilot/proof"] = {
    status: 200,
    body: {
      proofs: [
        { ...spend, campaign_id: "c_a", name: "Spring win-back - sent by another tool", claim_label: "Random by your statement, not verified" },
        { ...spend, campaign_id: "c_b", name: "Summer offer", claim_label: "Descriptive only" },
        { ...spend, campaign_id: "c_c", name: "Autumn offer", status: "refused", claim_label: null, headline: null, message: "The final result is not in yet." },
        { ...spend, campaign_id: "c_d", name: "Winter offer", claim_label: "Causal: the customers held back were chosen at random" },
      ],
    },
  };
  await open(w, "#/reports/value-proof", () => proofRows().length === 4);
  const claims = Object.fromEntries(proofRows().map((tr) => [tr.querySelector("[data-proof-campaign]").dataset.proofCampaign, text(tr.querySelector("[data-claim]"))]));
  assert.deepEqual(claims, { c_a: "Estimated", c_b: "Not yet proven", c_c: "", c_d: "Proven" });
  const refused = proofRows().find((tr) => tr.querySelector('[data-proof-campaign="c_c"]'));
  assert.match(text(refused), /The final result is not in yet\./);
  assert.equal(refused.querySelector("[data-proof-open]"), null, "a refused pack has nothing to open");
  assert.equal(text(proofRows()[0].querySelector(".rp-name")), "Spring win-back");
  assert.equal(findNeverShown(visibleText($("[data-reports-body]")))["causal"], undefined, "'Causal' is the engine's label, not a word shown");
  delete overlay["GET /pilot/proof"];
});

test("value proof: no pack yet is one calm sentence and a way to check a past campaign", async () => {
  overlay["GET /pilot/proof"] = { status: 200, body: { proofs: [] } };
  await open(w, "#/reports", () => $("[data-proofs-empty]"));
  assert.match(text($("[data-proofs-empty]")), /No Value Proof Pack yet/);
  assert.equal($("[data-proofs-empty] a").getAttribute("href"), "#/reports/check");
  assert.equal($("main .btn.primary"), null, "nothing to open, so no primary button");
  delete overlay["GET /pilot/proof"];
});

test("value proof: a list the API refuses is an error with a retry, not a blank page", async () => {
  overlay["GET /pilot/proof"] = { status: 403, body: { detail: { code: "ROLE_REQUIRED", message: "Ask an administrator." } } };
  await open(w, "#/reports", () => $("[data-reports-body] [role=alert], [data-reports-body] .apierr"));
  assert.match(text($("[data-reports-body]")), /administrator|Retry|wrong/i);
  delete overlay["GET /pilot/proof"];
});

test("programme summary: the block Results draws, the same structure and figures, in the glossary's words", async () => {
  const { plainWords } = await import("../../../../../ui/modules/reports/programme.js");
  await open(w, "#/results", () => $("[data-proven]"));
  const onResults = $("[data-proven]");
  const shape = (el) => el.outerHTML.replace(/>[^<]*</g, "><");
  const resultsShape = shape(onResults);
  const pieces = (el) => {
    const out = [];
    const walk = (n) => (n.nodeType === 3 ? n.nodeValue.trim() && out.push(n.nodeValue.trim()) : n.childNodes.forEach(walk));
    walk(el);
    return out;
  };
  const resultsText = pieces(onResults);
  await open(w, "#/reports/programme", () => $("[data-proven]"));
  assert.equal(shape($("[data-proven]")), resultsShape, "the same cards, lines and lists");
  assert.deepEqual(pieces($("[data-proven]")), resultsText.map(plainWords), "the same sentences and figures; only the engine's words are changed");
  assert.notDeepEqual(pieces($("[data-proven]")), resultsText, "and the recording does have engine words to change");
  assert.equal($("[data-attention]"), null, "the cards that need attention stay on Results");
  assert.match(text($("[data-proven]")), /Value proven to date/);
  const rule = plainWords(recorded["GET /campaigns/summary"].body.proven.rule);
  assert.ok(text($("[data-proven]")).includes(rule), "the rules it is counted by");
  assert.match(rule, /comparison group/);
  assert.equal($("[data-reports-tab][aria-current]").dataset.reportsTab, "programme");
});

test("programme summary: nothing proven yet says so, only for an answer that has nothing proven", async () => {
  overlay["GET /campaigns/summary"] = { status: 200, body: { cards: [], proven: { rule: "r", totals: [], apart: [], excluded: [], unpriced: [] } } };
  await open(w, "#/reports/programme", () => $("[data-programme-empty]"));
  assert.match(text($("[data-programme-empty]")), /Nothing is proven yet/);
  delete overlay["GET /campaigns/summary"];
});

test("programme summary: a summary that cannot be read is an error with a retry, never 'nothing is proven yet'", async () => {
  const refused = [
    [403, { detail: { code: "ROLE_REQUIRED", message: "Ask an administrator." } }],
    [500, { detail: { code: "INTERNAL", message: "Something broke." } }],
  ];
  for (const [status, body] of refused) {
    overlay["GET /campaigns/summary"] = { status, body };
    await open(w, "#/reports/programme", () => $("[data-reports-body] .apierr"));
    assert.ok($("[data-reports-body] [data-retry]"), `${status}: a Try again button`);
    assert.equal($("[data-programme-empty]"), null, `${status}: no claim that nothing is proven`);
    assert.doesNotMatch(text($("[data-reports-body]")), /Nothing is proven yet/, String(status));
  }
  overlay["GET /campaigns/summary"] = () => {
    throw new Error("network down");
  };
  await open(w, "#/reports/programme", () => $("[data-reports-body] .apierr") || $("[data-programme-empty]"));
  assert.equal($("[data-programme-empty]"), null, "a network failure is not 'nothing is proven' either");
  delete overlay["GET /campaigns/summary"];
});

test("a malformed escape in the tab lands on Value proof instead of throwing", async () => {
  for (const hash of ["#/reports/%", "#/reports/%E0", "#/reports/%E0%A4%A"]) {
    await open(w, hash, () => $("[data-reports-page]"));
    assert.equal($("[data-reports-page]").dataset.tab, "value-proof", hash);
  }
});

test("the classic top bar marks Results on #/reports", async () => {
  await open(w, "#/reports", () => $("[data-reports-page]"));
  const on = $$("nav a[aria-current], .tn-item.on").map((a) => text(a));
  assert.ok(on.includes("Results"), `marked: ${JSON.stringify(on)}`);
});

test("model reports: a closed technical section holding today's three lists", async () => {
  await open(w, "#/reports/models", () => $("[data-rp-models]") && $$("[data-models-part]").length === 3 && !$("[data-models-part] .skel"));
  const section = $("[data-rp-models]");
  assert.equal(section.tagName, "DETAILS");
  assert.equal(section.hasAttribute("open"), false, "closed until M180's switch");
  assert.ok(section.hasAttribute("data-technical") && section.classList.contains("tech"));
  assert.ok($('[data-reports-tab="models"]').hasAttribute("data-technical"), "the tab is marked for the switch too");
  assert.deepEqual($$("[data-models-part]").map((p) => p.dataset.modelsPart), ["results", "value", "readiness"]);
  // The recording has an approved model and two scored campaigns.
  assert.ok($$('[data-models-part="results"] tbody tr').length >= 1);
  assert.equal($$('[data-models-part="value"] tbody tr').length, 2);
});

test("check a past campaign: three steps and one primary button", async () => {
  await open(w, "#/reports/check", () => $("[data-rp-check-form]"));
  assert.deepEqual($$("[data-rp-check-form] .rp-step-h").map((h) => text(h)), [
    "1Who was in which group",
    "2What happened",
    "3Who was actually contacted (optional)",
  ]);
  assert.equal($$("main .btn.primary").length, 1);
  assert.equal($("[data-rp-submit]").disabled, true, "nothing to check until the two files are chosen");
  assert.equal($("[data-rp-basis] input[type=radio]:checked"), null, "how the groups were chosen is never assumed");
  assert.equal($$("[data-rp-file]").length, 3 + 1, "three files, and the programme's");
  assert.equal($("[data-rp-programme-card]").hasAttribute("open"), false);
  assert.equal($$("[data-audit-file], [data-audit-form]").length, 0, "none of the old page's hooks: its handlers must not also fire");
});

test("the tab names the plan's words and the old ones", async () => {
  for (const [hash, tab] of [
    ["#/reports/proof", "value-proof"],
    ["#/reports/summary", "programme"],
    ["#/reports/audit", "check"],
    ["#/reports/pilot", "models"],
    ["#/reports/no-such-tab", "value-proof"],
  ]) {
    await open(w, hash, () => $("[data-reports-page]"));
    assert.equal($("[data-reports-page]").dataset.tab, tab, hash);
  }
});

test("every report reachable from today's #/pilot (and its Build data page) is reachable from Reports", async () => {
  // Make today's lists non-empty: an approved model, a built dataset, and the recorded scored campaigns and packs.
  const champion = structuredClone(recorded["GET /models"].body);
  overlay["GET /models"] = { status: 200, body: champion };
  overlay["GET /datasets"] = {
    status: 200,
    body: { datasets: [{ dataset_id: "d_20261011_aaaa", use_case: "hillstrom-email", target: "converted", n_rows: 640, built_at: "2026-10-11T10:00:00Z" }] },
  };
  const hrefs = () =>
    $$('a[href^="#/pilot/"], a[href*="/pilot/"][download]')
      .map((a) => a.getAttribute("href"))
      .filter((h) => /#\/pilot\/(view|value|proof)\/|\/pilot\/(results|roi|proof|readiness)/.test(h));

  await open(w, "#/pilot", () => $('[data-pe-card="proof"] tbody tr') && $('[data-pe-card="value"] tbody tr') && $('[data-pe-card="results"] tbody tr'));
  const today = new Set(hrefs());
  await open(w, "#/pilot/kit", () => $('[data-pe-card="readiness"] tbody tr'));
  for (const h of hrefs()) today.add(h);
  assert.ok([...today].some((h) => h.startsWith("#/pilot/view/results/")), "today: a model results report");
  assert.ok([...today].some((h) => h.startsWith("#/pilot/value/")), "today: a campaign value report");
  assert.ok([...today].some((h) => h.startsWith("#/pilot/proof/")), "today: a Value Proof Pack");
  assert.ok([...today].some((h) => h.startsWith("#/pilot/view/readiness/")), "today: a readiness report");
  assert.ok([...today].some((h) => /format=pdf$/.test(h)), "today: PDFs");

  const here = new Set();
  await open(w, "#/reports/value-proof", proofsReady);
  for (const h of hrefs()) here.add(h);
  await open(w, "#/reports/models", () => $$('[data-models-part] tbody tr').length >= 4);
  for (const h of hrefs()) here.add(h);
  const missing = [...today].filter((h) => !here.has(h));
  assert.deepEqual(missing, [], "a report reachable today that Reports does not link");
  delete overlay["GET /models"];
  delete overlay["GET /datasets"];
});

test("no tab shows an engine word outside technical details, and the model reports are all technical", async () => {
  for (const hash of ["#/reports/value-proof", "#/reports/programme", "#/reports/check", "#/reports/models"]) {
    await open(w, hash, () => $("[data-reports-page]") && !$(".skel-screen"));
    await settle(4);
    assert.deepEqual(findNeverShown(visibleText($("main"))), {}, hash);
  }
});
