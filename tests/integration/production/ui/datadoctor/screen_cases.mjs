/* The cases of screen.test.mjs (Plan O M247): the screen mounted on its own, as the Data page mounts it. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage, settle, until } from "../harness.mjs";
import { CASE, GOAL, fx, makeWorld, screen, serverFor } from "./common.mjs";
import { check } from "../a11y/checks.mjs";
import { visibleText } from "../glossary/collect.mjs";
import { violations, describe } from "../glossary/check.mjs";

let world = makeWorld();
installPage((request) => serverFor(world)(request), { hash: "#/data/check" });
const { mountDataDoctor, _resetDataDoctorForTests } = await import("../../../../../ui/modules/datadoctor/screen.js");
const view = await import("../../../../../ui/modules/datadoctor/view.js");
const { CSS } = await import("../../../../../ui/modules/datadoctor/styles.js");
const { forgetAiServiceStatus } = await import("../../../../../ui/availability.js");

const app = $("#app");

/** Mount the screen on a fresh page in scenario `at`; resolves once the verdict is drawn. */
async function show(at, options = {}) {
  world = makeWorld({ at, ...options });
  if (options.cases) world.cases = options.cases;
  _resetDataDoctorForTests();
  forgetAiServiceStatus();
  await settle(2);
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data", ...(options.host || {}) });
  await until(() => $("[data-dds-verdict]") || $("[data-dds-start]"), 3000, `the ${at} screen`);
  await settle(3);
  return screen(at);
}

const posts = (tail) => world.posted.filter((p) => p.path === `/cases/${CASE}/${tail}`);

// --- questions -----------------------------------------------------------------------------------------------

test("a question: its text, why it is asked, each answer with what it changes, and I don't know", async () => {
  const s = await show("question");
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  const card = $(`[data-dds-question="${q.question_id}"]`);
  assert.ok(card, "the question is drawn");
  assert.ok(card.textContent.includes("do “haa” and “nahi” mean yes and no?"));
  assert.match(card.textContent, /Why we ask: 600 of 1,200 values are written that way\./);
  const yes = card.querySelector('[data-dds-answer="c0"]:not([data-dds-dont-know])');
  assert.equal(yes.textContent.trim(), "Yes: haa is yes, nahi is no");
  assert.match(yes.closest("li").textContent, /Changes 600 rows/);
  const dontKnow = card.querySelector("[data-dds-dont-know]");
  assert.equal(dontKnow.textContent.trim(), "I don't know");
  assert.equal(dontKnow.dataset.ddsAnswer, "c1", "I don't know is the safe choice, never a guess");
  assert.match(dontKnow.closest("li").textContent, /safe choice: Leave them as written/);
  dontKnow.click();
  await until(() => posts("answers").length === 1, 2000, "the answer");
  const sent = posts("answers")[0].body;
  assert.deepEqual(
    { question_id: sent.question_id, option_id: sent.option_id, version: sent.version },
    { question_id: q.question_id, option_id: "c1", version: s.case.version },
  );
  assert.ok(sent.op_id, "every change carries an idempotency key");
});

test("a question that offers its own I don't know draws it once, as the last answer", async () => {
  const s = await show("question");
  const q = s.case.questions.find((x) => x.code === "KEY_AMBIGUOUS");
  const card = $(`[data-dds-question="${q.question_id}"]`);
  const labels = [...card.querySelectorAll("[data-dds-answer]")].map((b) => b.textContent.trim());
  assert.deepEqual(labels, ["customer_id", "account_no", "monthly_spend", "I don't know"]);
  assert.equal(card.querySelector("[data-dds-dont-know]").dataset.ddsAnswer, "none");
  assert.ok(card.textContent.includes("Needed to go on"), "a blocking question says so");
  assert.equal($("[data-dds-verdict]").dataset.ddsVerdict, "needs_answers");
  assert.ok($("[data-dds-next]").textContent.includes("Answer 1 question below to go on."));
});

test("a small choice decided for the person says what was assumed, and can be changed", async () => {
  const s = await show("assumed");
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  const card = $(`[data-dds-question="${q.question_id}"]`);
  assert.match(card.querySelector("[data-dds-assumed]").textContent, /Decided for you: Leave them as written\. The choice changes too little to ask/);
  const other = card.querySelector('[data-dds-answer="c0"]');
  assert.equal(other.disabled, false, "another answer changes the assumption");
});

test("an answered question shows the answer and is not asked again", async () => {
  const s = await show("approve");
  const q = s.case.questions.find((x) => x.code === "KEY_AMBIGUOUS");
  const card = $(`[data-dds-question="${q.question_id}"]`);
  assert.match(card.querySelector("[data-dds-answered]").textContent, /Your answer: customer_id/);
  assert.ok(!card.classList.contains("is-open"));
  assert.equal(card.querySelectorAll("[data-dds-answer]").length, 0, "an answer given is not offered again");
});

// --- approvals -----------------------------------------------------------------------------------------------

test("a change that needs its own yes: the exact sentence, why, who, and I agree to this sentence", async () => {
  const s = await show("approve_class");
  for (const entry of s.view.approve_class) {
    const card = $(`[data-dds-approval="${entry.item_id}"]`);
    assert.ok(card, entry.item_id);
    assert.equal(card.querySelector("[data-dds-sentence]").textContent, entry.sentence, "the sentence, word for word");
    assert.ok(card.textContent.includes(`Why: ${entry.why.charAt(0).toUpperCase()}${entry.why.slice(1)}.`), "why it needs its own yes");
    const who = entry.role === "approver" ? "someone with the Approver role" : "someone with the Analyst role";
    assert.ok(card.textContent.includes(who));
    assert.ok(card.textContent.includes("other than whoever suggested or answered it"), "separation of duties is said");
    assert.equal(card.querySelector("[data-dds-agree]").textContent.trim(), "I agree to this sentence");
  }
  const approve = $("[data-dds-approve-button]");
  assert.ok(approve.disabled, "Approve waits for the own yes");
  const rows = s.view.approve_class.find((a) => a.role === "analyst");
  world.next.confirmations = "approve_class";
  $(`[data-dds-agree="${rows.item_id}"]`).click();
  await until(() => posts("confirmations").length === 1, 2000, "the yes");
  const sent = posts("confirmations")[0].body;
  assert.equal(sent.item_id, rows.item_id);
  assert.equal(sent.sentence, rows.sentence, "the sentence the person read goes back word for word");
});

test("a refused yes shows the server's reason and records nothing", async () => {
  const s = await show("approve_class");
  const consent = s.view.approve_class.find((a) => a.role === "approver");
  world.refuse = {
    path: `/cases/${CASE}/confirmations`,
    answer: { status: 403, body: { detail: { code: "TOOL_ROLE_REQUIRED", message: "Only someone with the Approver role can agree to this." } } },
  };
  $(`[data-dds-agree="${consent.item_id}"]`).click();
  await until(() => $("[data-dds] .apierr"), 2000, "the refusal");
  assert.match($("[data-dds] .apierr").textContent, /Approver role/);
});

test("Approve sends the sentence it shows, word for word", async () => {
  const s = await show("approve");
  const shown = $("[data-dds-approve-sentence]").textContent;
  assert.equal(shown, s.view.approval_sentence);
  assert.equal(shown, "Prepare 1 file with 2 changes. Nothing else changes.");
  world.next.approval = "approve";
  $("[data-dds-approve-button]").click();
  await until(() => posts("approval").length === 1, 2000, "the approval");
  assert.equal(posts("approval")[0].body.sentence, s.view.approval_sentence);
});

test("a sentence that changed meanwhile records nothing and is shown again", async () => {
  await show("approve");
  world.refuse = {
    path: `/cases/${CASE}/approval`,
    answer: { status: 409, body: { detail: { code: "APPROVAL_SENTENCE_CHANGED", message: "It now reads: …" } } },
  };
  $("[data-dds-approve-button]").click();
  await until(() => $("[data-dds-notice]"), 2000, "the notice");
  assert.match($("[data-dds-notice]").textContent, /has changed\. Read it again/);
  assert.ok($("[data-dds-approve-sentence]"), "the sentence is drawn again to read");
});

// --- doubts and requests -------------------------------------------------------------------------------------

test("a doubt says why, what Data Doctor would do, and what changes if not", async () => {
  const s = await show("question");
  const d = s.case.doubts[0];
  const card = $(`[data-dds-doubt="${d.doubt_id}"]`);
  const part = (key) => card.querySelector(`[data-dds-doubt-part="${key}"]`).textContent;
  assert.ok(part("because").startsWith("I am not sure because"));
  assert.ok(part("because").includes("near 2 for 312 rows"));
  assert.ok(part("would_do").startsWith("Here is what I would do:") && part("would_do").includes(d.would_do));
  assert.ok(part("if_not").startsWith("Here is what changes if not:") && part("if_not").includes(d.if_not));
});

test("a request for more data: what to ask for, the file wanted, the list to send, and the two answers", async () => {
  const s = await show("question");
  const q = s.case.questions.find((x) => x.code === "DATA_REQUESTED");
  const card = $(`[data-dds-request="${q.question_id}"]`);
  assert.ok(card.textContent.includes("Ask your billing team for the date of each customer's last order"));
  assert.ok(card.textContent.includes("The file needed: one row per customer"));
  const kit = card.querySelector("[data-dds-request-kit]");
  assert.match(kit.getAttribute("href"), /\/pilot\/data-request/);
  const labels = [...card.querySelectorAll("[data-dds-answer]")].map((b) => b.textContent.trim());
  assert.deepEqual(labels, ["I will add it", "I cannot get it"]);
  assert.ok(!$(`[data-dds-question="${q.question_id}"]`), "a request is not drawn as a second question");
  assert.ok($("[data-dds-more] input[type=file]"), "the file asked for can be added here, in the same check");
});

// --- remembered answers --------------------------------------------------------------------------------------

test("a remembered answer is shown with its date and a Revoke that asks the question again", async () => {
  const s = await show("standing");
  const used = s.view.standing[0];
  const row = $(`[data-dds-standing="${used.answer_id}"]`);
  assert.equal(row.querySelector("[data-dds-standing-date]").textContent, "12 Oct 2026");
  assert.ok(row.textContent.includes("Yes: haa is yes, nahi is no"));
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  assert.match($(`[data-dds-question="${q.question_id}"] [data-dds-from-standing]`).textContent, /Using your answer from 12 Oct 2026/);
  world.next["standing-answers"] = "revoked";
  row.querySelector(`[data-dds-revoke="${used.answer_id}"]`).click();
  await until(() => $("[data-dds-revoked]"), 2000, "the revoked answer");
  const sent = world.posted.find((p) => p.path === `/cases/${CASE}/standing-answers/${used.answer_id}/revoke`);
  assert.ok(sent && sent.body.version === s.case.version);
  assert.ok(!$(`[data-dds-question="${q.question_id}"] [data-dds-from-standing]`), "the question is open again");
  assert.ok($(`[data-dds-question="${q.question_id}"]`).classList.contains("is-open"));
});

// --- verdicts, the checklist and the hand-off ----------------------------------------------------------------

test("the checklist: found by our checks, ticked as the case says; unticking sends the decision", async () => {
  const s = await show("approve");
  const group = $('[data-dds-group="rules"]');
  assert.ok(group.textContent.includes("Found by our checks"));
  const item = s.case.items.find((i) => i.kind === "key");
  const box = $(`input[data-dds-tick="${item.item_id}"]`);
  assert.equal(box.checked, true);
  box.checked = false;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  await until(() => posts("decisions").length === 1, 2000, "the decision");
  assert.deepEqual([posts("decisions")[0].body.item_id, posts("decisions")[0].body.accept], [item.item_id, false]);
});

test("ready on the Data page continues in the wizard on the same case", async () => {
  await show("ready");
  assert.equal($("[data-dds-verdict]").dataset.ddsVerdict, "ready");
  const button = $("[data-dds-continue]");
  assert.equal(button.textContent.trim(), "Continue to the campaign");
  button.click();
  await settle(3);
  assert.equal(window.location.hash, `#/campaigns/new/customers/${GOAL}`);
});

test("not ready says so, and starts nothing", async () => {
  await show("not_ready");
  assert.equal($("[data-dds-verdict]").dataset.ddsVerdict, "not_ready");
  assert.ok(!$("[data-dds-continue]"));
});

test("with no file yet: one sentence and Choose a file", async () => {
  await show("empty", { cases: { cases: [] } });
  assert.ok($("[data-dds-start] label[for=dds-file]").textContent.includes("Choose a file"));
  assert.ok($("#dds-file").getAttribute("accept").includes(".csv"));
  assert.ok(!$("[data-dds-chat]"), "nothing to chat about before a file");
});

// --- the chat --------------------------------------------------------------------------------------------------

test("with no AI service the chat links to Settings › AI service, not Connections, and the rest works", async () => {
  await show("question");
  const chat = $("[data-dds-chat]");
  const link = chat.querySelector("[data-ai-settings-link]");
  assert.equal(link.getAttribute("href"), "#/settings/ai");
  assert.ok(!chat.querySelector('a[href^="#/connections"]'), "never Connections");
  assert.ok(!chat.querySelector("form"), "no input that cannot be answered");
  assert.match(chat.textContent, /Everything else on this screen works without it\./);
  assert.ok($$("[data-dds-answer]").every((b) => !b.disabled), "questions still work");
});

test("with an AI service connected the chat takes a message; What the AI looked at stays as built", async () => {
  const ai = structuredClone(fx("ai_service"));
  ai.slots.product.connected = true;
  await show("question", { ai });
  const input = $("#dds-chat-input");
  assert.ok(input, "the chat input");
  input.value = "Why is account_no not the ID?";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  $("#dds-chat-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => posts("chat").length === 1, 2000, "the message");
  assert.equal(posts("chat")[0].body.message, "Why is account_no not the ID?");
  const { sentHtml } = await import("../../../../../ui/modules/agent/sent.js");
  const html = view.chatHtml(
    { case: { chat: [{ role: "agent", text: "Here.", at: "2026-10-12T09:00:00Z", sent: [{ tool: "value_counts", args: { column: "x" }, preview: "a", chars: 1 }] }] } },
    { ai: true },
  );
  assert.ok(html.includes(sentHtml([{ tool: "value_counts", args: { column: "x" }, preview: "a", chars: 1 }], { id: 0 })));
});

// --- the timeline ------------------------------------------------------------------------------------------------

test("the timeline lists what happened, newest last, with the receipt of an approval", async () => {
  await show("ready");
  const events = $$("[data-dds-event]").map((li) => li.dataset.ddsEvent);
  assert.equal(events[0], "case.created");
  assert.ok(events.includes("approval") && events.includes("readiness.checked"));
  assert.ok(events.indexOf("approval") < events.lastIndexOf("hand_off"), "newest last");
  assert.ok($$('[data-dds-event="approval"] [data-dds-receipt]').length >= 1);
  assert.ok(!events.includes("state.changed"), "the visit's own steps are technical");
});

// --- accessibility, touch targets and words ---------------------------------------------------------------------

const ALL = ["question", "assumed", "approve", "approve_class", "ready", "not_ready", "standing", "revoked"];

test("accessibility (Plan M M181): every shape passes the structural checks", async () => {
  for (const at of ALL) {
    await show(at);
    const found = check(app).violations;
    assert.deepEqual(found, [], `${at}: ${JSON.stringify(found)}`);
  }
});

test("every control is a 40px target: buttons are the theme's, rows and links have 40px", async () => {
  await show("approve_class");
  for (const b of $$("[data-dds] button, [data-dds-chat] button")) {
    assert.ok(b.classList.contains("btn"), `a theme button: ${b.outerHTML.slice(0, 80)}`);
  }
  for (const rule of [".dds-tick{", "min-height:40px", ".dds-standing .btn.sm,.dds-goal .btn{min-height:40px}", ".dds-opt .btn{min-height:40px}", ".dds-nochat .btn{min-height:40px}"]) {
    assert.ok(CSS.includes(rule), rule);
  }
  assert.ok(!/#[0-9a-fA-F]{3,6}\b/.test(CSS), "colours are the theme's tokens");
});

test("visible text: no word the glossary keeps from a marketer, in any shape", async () => {
  for (const at of [...ALL, "empty"]) {
    await show(at, at === "empty" ? { cases: { cases: [] } } : {});
    const v = violations(visibleText(app));
    assert.ok(v.ok, describe(`data doctor ${at}`, v));
  }
});
