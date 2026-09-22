/* Phase 2 §10 A — the client selector.
   Phase 3a §9 C — the AI Onboarding Assistant.
   Phase 3a §9 D — RCA root causes.
   Phase 3a §9 E — win-back campaign copy. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, go, ev, $, $$, body, click, set, wait } from "./harness.mjs";

/* ---------- A. client selector ---------- */

test("the client selector sits in the header of every screen", () => {
  const dom = load("#/");
  for (const hash of ["#/", "#/uc/rca", "#/uc/rca/data", "#/uc/win-back-campaign/output"]) {
    go(dom, hash);
    const sel = $(dom, ".headtools #f-client");
    assert.ok(sel, `no client selector on ${hash}`);
    assert.equal(sel.value, "Demo Telecom");
    assert.equal([...sel.options].at(-1).textContent, "+ New client");
    assert.equal($(dom, ".chint").textContent, "Tables, recipes and models are kept per client.");
  }
});

test("switching client changes nothing else on the page", () => {
  const dom = load("#/uc/rca");
  const before = $(dom, "#app").innerHTML;
  set(dom, "#f-client", "Acme Broadband");
  assert.equal(ev(dom, "CLIENT"), "Acme Broadband");
  assert.equal($(dom, "#app").innerHTML, before, "the selector is visual only");
  set(dom, "#f-client", "__new");
  assert.equal(ev(dom, "CLIENT"), "Acme Broadband", "+ New client does not switch client");
});

/* ---------- C. AI Onboarding Assistant ---------- */

test("assistant setup: documents, then optional reference questions", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  assert.equal($(dom, ".pickcard"), null, "the raw-table path is for tabular use cases");
  assert.deepEqual($$(dom, ".flabel").map((e) => e.textContent.trim().replace(/\s+/g, " ")),
    ["Documents", "Reference questions · optional", "Model"]);
  assert.equal($(dom, ".reason").textContent, "Upload the documents to continue");
  click(dom, "#f-sampledocs");
  const rows = $$(dom, ".docrow");
  assert.equal(rows.length, 5);
  assert.deepEqual(rows.map((r) => [...r.children].map((c) => c.textContent)), [
    ["broadband_setup_guide.pdf", "48 pages", "612 chunks"],
    ["router_troubleshooting.pdf", "32 pages", "410 chunks"],
    ["sim_activation_guide.pdf", "21 pages", "264 chunks"],
    ["plans_and_pricing.html", "12 pages", "188 chunks"],
    ["billing_faq.html", "9 pages", "126 chunks"],
  ]);
  assert.match(body(dom), /5 documents · 122 pages · 1,600 chunks at 512 tokens each\./);
  assert.equal($(dom, ".reason").textContent, "", "questions are optional, so the run is unblocked");
  click(dom, "#f-sampleqa");
  assert.equal($(dom, "#f-pk").value, "question_id");
  assert.equal($(dom, "#f-target").value, "reference_answer");
});

test("assistant advanced settings carry the RAG settings including the refusal message", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  assert.equal($(dom, '[data-adv="chunk"]').value, "512");
  assert.equal($(dom, '[data-adv="topk"]').value, "5");
  const refusal = $(dom, '[data-adv="refusal"]');
  assert.ok(refusal, "the refusal message is configurable");
  assert.equal(refusal.value,
    "I could not find this in the product documents. I will pass you to a person.");
  assert.match(refusal.closest(".field").textContent, /When nothing relevant is found, reply/);
  assert.match(refusal.closest(".field").textContent, /Sent instead of a guess/);
});

test("assistant running screen builds the index stage by stage", () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  click(dom, "#f-sampleqa");
  $(dom, "#f-setup").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  assert.deepEqual($$(dom, ".progress .pt").map((e) => e.textContent), [
    "Reading documents", "Splitting into chunks", "Redacting personal data",
    "Creating embeddings", "Building the retrieval index",
    "Answering the reference questions", "Grading the answers", "Saving assistant version",
  ]);
});

test("assistant results: index summary, pass rate, worst ten, try it, cost line", async () => {
  const dom = load("#/uc/ai-onboarding-assistant");
  click(dom, "#f-sampledocs");
  click(dom, "#f-sampleqa");
  $(dom, "#f-setup").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await wait(1600);
  assert.ok($(dom, ".results"), "the assistant reaches the results state");
  // eval pass rate against the threshold, with a bar
  assert.equal($(dom, ".evalbar .ebv").textContent, "91% passed");
  assert.equal($(dom, ".evalbar .eb i").style.width, "91%");
  assert.equal($(dom, ".evalbar .ebm").style.left, "85%");
  assert.match($(dom, ".evalbar .ebl").textContent, /300 reference questions/);
  assert.match($(dom, ".evalbar .ebl").textContent, /Threshold 85%/);
  // the worst ten answers
  const worst = $$(dom, ".evalbar ~ .tbl-wrap tbody tr");
  assert.equal(worst.length, 10);
  assert.deepEqual([...$$(dom, ".evalbar ~ .tbl-wrap th")].map((e) => e.textContent),
    ["question", "score", "what went wrong"]);
  assert.equal(worst[0].children[1].textContent, "0.31");
  // index summary
  const idx = $$(dom, ".card .kv").map((e) => e.textContent);
  assert.ok(idx.some((t) => t.startsWith("Text chunks1,600")), idx.join(" | "));
  assert.ok(idx.some((t) => t.startsWith("Retrievaltop-5 · hybrid")));
  assert.ok(idx.some((t) => t.includes("pass you to a person")));
  // try it, with citations that expand to the quoted chunk
  assert.equal($$(dom, ".chatpanel .bubble.q").length, 3);
  const cites = $$(dom, "details.cite");
  assert.equal(cites.length, 3);
  assert.equal(cites[0].querySelector("summary").textContent, "router_troubleshooting.pdf · p. 11");
  assert.match(cites[0].querySelector(".quote").textContent, /steady red WAN light/);
  // the refusal answer cites nothing
  assert.match($$(dom, ".bubble.a").at(-1).textContent, /could not find this in the product documents/);
  assert.match($$(dom, ".bubble.a").at(-1).textContent, /no document matched/);
  assert.equal($(dom, ".costline").textContent, "LLM cost this run: —");
});

/* ---------- D. RCA root causes ---------- */

test("RCA output offers to generate root causes before it has any", () => {
  const dom = load("#/uc/rca/output");
  assert.ok($(dom, "#rca-gen"), "the button state comes first");
  assert.equal($(dom, ".rcseg"), null);
  assert.match($(dom, ".genbox").textContent, /Root causes have not been written for this run yet\./);
});

test("RCA root causes: two segments, three causes each, evidence, actions, caveats", () => {
  const dom = load("#/uc/rca/output");
  click(dom, "#rca-gen");
  const segs = $$(dom, ".rcseg");
  assert.equal(segs.length, 2);
  assert.deepEqual(segs.map((s) => s.querySelector(".rsh").textContent), ["High risk", "Medium risk"]);
  assert.deepEqual(segs.map((s) => s.querySelector(".rsn").textContent),
    ["2,180 customers", "3,410 customers"]);
  assert.equal(segs[0].querySelector(".rline").textContent,
    "Network problems first, then a bill nobody explained.");
  for (const seg of segs) {
    const causes = [...seg.querySelectorAll(".cause")];
    assert.equal(causes.length, 3);
    for (const c of causes) {
      const chips = [...c.querySelectorAll(".evchip")];
      assert.equal(chips.length, 2, "one reason chip and one complaint snippet");
      assert.ok(chips[0].classList.contains("reason"));
      assert.ok(chips[1].classList.contains("quote"));
      assert.match(chips[1].textContent, /^“.*”$/, "the snippet is quoted");
    }
    assert.ok(seg.querySelectorAll(".rcfoot ul li").length >= 3, "recommended actions");
    assert.match(seg.querySelector(".cav").textContent, /complaint notes/, "caveat");
  }
  assert.equal(segs[0].querySelector(".evchip.reason").textContent, "outages_90d ↑ 3x");
});

/* ---------- E. win-back campaign copy ---------- */

test("win-back output carries the campaign copy block and its banner", () => {
  const dom = load("#/uc/win-back-campaign/output");
  assert.equal($(dom, ".banner").textContent.replace("⚑", "").trim(),
    "Nothing is sent from here. Approved messages are exported for your campaign tool.");
  const cards = $$(dom, ".cccard");
  assert.equal(cards.length, 12, "2 bands × 3 channels × 2 variants");
  assert.deepEqual($$(dom, ".ccgrouphd").map((e) => e.textContent),
    ["High band · 30% off for 3 months", "Medium band · Free upgrade"]);
  assert.deepEqual(cards.slice(0, 6).map((c) => c.querySelector(".cct").textContent),
    ["Email · Variant A", "Email · Variant B", "SMS · Variant A", "SMS · Variant B",
     "WhatsApp · Variant A", "WhatsApp · Variant B"]);
  // a status pill and judge scores on every card
  for (const c of cards) {
    assert.match(c.querySelector(".pill").textContent, /^(Approved|Pending review|Blocked)$/);
    assert.match(c.querySelector(".ccm").textContent, /Brand 0\.\d\d · Clarity 0\.\d\d · Compliance \d\.\d\d · \d+ characters/);
  }
  assert.ok($(dom, "#cc-dl"), "download messages");
  assert.match($(dom, ".ccbar").textContent, /7 approved · 3 pending review · 2 blocked/);
});

test("a blocked message says why, in plain words", () => {
  const dom = load("#/uc/win-back-campaign/output");
  const blocked = $$(dom, ".cccard.blocked");
  assert.equal(blocked.length, 2);
  // the character count in the reason is read off the message, so it cannot drift
  const over = blocked[0].querySelector(".ccb").textContent;
  assert.ok(over.length > 160);
  assert.deepEqual(blocked.map((c) => c.querySelector(".ccwhy").textContent),
    [`Exceeds 160 characters (${over.length}).`, "Promises a 50% discount that is not in this offer."]);
  assert.match(blocked[0].querySelector(".ccm").textContent, new RegExp(`${over.length} characters`));
});

test("Approve and Regenerate change one card and nothing else", () => {
  const dom = load("#/uc/win-back-campaign/output");
  const pending = $$(dom, ".cccard").find((c) => c.querySelector(".pill").textContent === "Pending review");
  const before = pending.querySelector(".ccb").textContent;
  pending.querySelector("[data-cc-ok]").click();
  assert.match($(dom, ".ccbar").textContent, /8 approved · 2 pending review · 2 blocked/);
  // regenerating a blocked card offers different copy and sends it back for review
  const blocked = $(dom, ".cccard.blocked");
  const blockedBefore = blocked.querySelector(".ccb").textContent;
  blocked.querySelector("[data-cc-re]").click();
  const again = $$(dom, ".cccard")[3];
  assert.equal(again.querySelector(".pill").textContent, "Pending review");
  assert.notEqual(again.querySelector(".ccb").textContent, blockedBefore);
  assert.equal(again.querySelector(".ccwhy"), null, "the block reason is cleared");
  assert.ok(again.querySelector(".ccb").textContent.length <= 160);
  assert.equal($$(dom, ".cccard")[1].querySelector(".ccb").textContent, before,
    "the other cards are untouched");
});

test("the root-cause and copy blocks only appear on their own use case", () => {
  const dom = load("#/");
  for (const id of ["targeted-advertisement", "order-fulfillment", "fault-prediction",
                    "payment-propensity", "ai-onboarding-assistant"]) {
    go(dom, `#/uc/${id}/output`);
    assert.equal($(dom, ".rcseg"), null, `${id} has no root causes`);
    assert.equal($(dom, "#rca-gen"), null, `${id} has no root-cause button`);
    assert.equal($(dom, ".cccard"), null, `${id} has no campaign copy`);
  }
});
