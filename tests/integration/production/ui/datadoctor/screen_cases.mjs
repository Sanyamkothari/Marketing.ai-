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

/** Tick "I have read this sentence" for a change that needs its own yes. */
function readIt(itemId) {
  const box = $$("input[data-dds-read]").find((b) => b.dataset.ddsRead === itemId);
  assert.ok(box, `the read box of ${itemId}`);
  box.checked = true;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
}
/** Tick "I have checked this section" in every section that asks for it. */
function checkSections() {
  const next = () => $$("input[data-dds-sec-ok]").find((b) => !b.checked);
  for (let box = next(); box; box = next()) {
    box.checked = true;
    box.dispatchEvent(new window.Event("change", { bubbles: true })); // the screen draws again: look the next one up
  }
}
const agreeOf = (itemId) => $$("[data-dds-agree]").find((b) => b.dataset.ddsAgree === itemId);

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
  await settle(1);
  assert.equal(posts("answers").length, 0, "I don't know first asks who would know; nothing is guessed");
  const safe = $(`[data-dds-question="${q.question_id}"] [data-dds-who] [data-dds-answer="c1"]`);
  assert.match(safe.textContent, /Use the safe choice for now: Leave them as written/);
  safe.click();
  await until(() => posts("answers").length === 1, 2000, "the answer");
  const sent = posts("answers")[0].body;
  assert.deepEqual(
    { question_id: sent.question_id, option_id: sent.option_id, version: sent.version },
    { question_id: q.question_id, option_id: "c1", version: s.case.version },
  );
  assert.ok(sent.op_id, "every change carries an idempotency key");
});

test("an answer says what it does in words, then the counts code measured; an answer with no sentence shows only the counts", async () => {
  const s = await show("question");
  const q = {
    question_id: "q.sentence",
    code: "Q11_WHICH_WINS",
    text: "Which copy wins?",
    blocking: false,
    options: [
      { option_id: "c0", label: "The later one", effect: "Keeps the later copy <b>of each</b> customer.", rows_changed: 12 },
      { option_id: "c1", label: "The earlier one", rows_changed: 0 },
      { option_id: "c2", label: "I don't know", safe: true },
    ],
  };
  const host = document.createElement("div");
  host.innerHTML = view.questionHtml(q, s.case);
  const [first, second] = [...host.querySelectorAll("li.dds-opt")];
  assert.match(first.textContent, /Keeps the later copy <b>of each<\/b> customer\. · Changes 12 rows/, "escaped text, sentence first");
  assert.equal(first.querySelector("b"), null, "the sentence is never markup");
  assert.match(second.textContent, /Changes no rows/);
});

test("a question that offers its own I don't know draws it once, as the last answer", async () => {
  const s = await show("question");
  const q = s.case.questions.find((x) => x.code === "KEY_AMBIGUOUS");
  const card = $(`[data-dds-question="${q.question_id}"]`);
  // a unique spend column no longer competes with the ID-named ones for "names each row" (visit._id_named)
  const labels = [...card.querySelectorAll("[data-dds-answer]")].map((b) => b.textContent.trim());
  assert.deepEqual(labels, ["customer_id", "account_no", "I don't know"]);
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
  readIt(rows.item_id);
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
  readIt(consent.item_id);
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

// --- M247 review: the goal's name, a deliberate yes, who may agree, what is left out -------------------------

test("the sentence approved names the goal as the screens do, never its id", async () => {
  const s = await show("approve_class");
  const shown = $("[data-dds-approve-sentence]").textContent;
  assert.equal(shown, s.view.approval_sentence);
  assert.equal(shown, "Prepare 1 file with 4 changes for the goal “Find customers likely to buy”. Nothing else changes.");
  assert.ok(!shown.includes(GOAL), "not the goal's id");
  assert.ok(visibleText(app).includes("Find customers likely to buy"), "it is visible text, checked by the glossary below");
  assert.equal($("[data-dds-approve-button]").getAttribute("aria-describedby"), "dds-approve-sentence");
  assert.equal($("#dds-approve-sentence").textContent, shown);
});

test("I agree to this sentence is a deliberate step: off until I have read this sentence is ticked", async () => {
  const s = await show("approve_class");
  const consent = s.view.approve_class.find((a) => a.role === "approver");
  const agree = agreeOf(consent.item_id);
  assert.ok(agree.disabled, "one stray click records nothing");
  const described = document.getElementById(agree.getAttribute("aria-describedby"));
  assert.ok(described, "the button is described by the sentence");
  assert.equal(described.textContent, consent.sentence, "a screen reader hears the sentence agreed to");
  const box = $$("input[data-dds-read]").find((b) => b.dataset.ddsRead === consent.item_id);
  assert.equal(box.closest("label").textContent.trim(), "I have read this sentence");
  assert.equal(box.checked, false);
  agree.click();
  await settle(2);
  assert.equal(posts("confirmations").length, 0, "nothing sent before the sentence is marked as read");
  readIt(consent.item_id);
  assert.equal(agreeOf(consent.item_id).disabled, false, "ticked: the yes can be given");
  box.checked = false;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.ok(agreeOf(consent.item_id).disabled, "unticked again: off again");
  readIt(consent.item_id);
  agreeOf(consent.item_id).click();
  await until(() => posts("confirmations").length === 1, 2000, "the yes");
  assert.equal(posts("confirmations")[0].body.sentence, consent.sentence);
});

test("someone the server would refuse is told whom to ask, with no I agree button", async () => {
  const s = await show("approve_class_own");
  for (const entry of s.view.approve_class) {
    assert.equal(entry.can_agree, false, entry.item_id);
    const card = $(`[data-dds-approval="${entry.item_id}"]`);
    assert.ok(!card.querySelector("[data-dds-agree]"), "no button that can only fail");
    assert.ok(!card.querySelector("input[data-dds-read]"));
    const ask = card.querySelector("[data-dds-ask-other]");
    assert.equal(ask.dataset.ddsAskOther, entry.cannot_agree);
    if (entry.cannot_agree === "role") {
      assert.equal(ask.textContent.trim(), "Ask someone with the Approver role to agree to this.");
    } else {
      assert.equal(ask.textContent.trim(), "You suggested or answered this, so ask someone else with the Analyst role to agree to this.");
    }
  }
  assert.deepEqual(new Set(s.view.approve_class.map((a) => a.cannot_agree)), new Set(["role", "separation"]));
});

test("Approve lists every change left out until someone agrees, in plain words", async () => {
  const s = await show("not_ready");
  const consent = s.view.approve_class.find((a) => a.role === "approver");
  const left = $('[data-dds-left-out="approve"]');
  assert.ok(left, "the left-out list beside Approve");
  assert.match(left.textContent, /Left out until someone agrees:/);
  const row = left.querySelector(`[data-dds-left-out-item="${consent.item_id}"]`);
  assert.equal(row.textContent, consent.sentence);
  assert.ok(row.textContent.includes("says who agreed to be contacted"));
  assert.match($("[data-dds-left-out-choice]").textContent, /After you approve, you choose whether to continue without it\./);
  assert.equal($("[data-dds-approve-button]").disabled, true, "the final yes waits for each section to be checked");
  assert.match($("[data-dds-approve-hold]").textContent, /Check each section above first/);
  checkSections();
  assert.equal($("[data-dds-approve-button]").disabled, false, "a change left out does not block Approve once the sections are checked");
});

test("in the wizard, Approve does not start the list while a change is left out; continuing is its own choice", async () => {
  const started = [];
  await show("not_ready", { host: { place: "wizard", goalId: GOAL, approved: (fill) => started.push(fill) } });
  world.next.approval = "ready";
  checkSections();
  $("[data-dds-approve-button]").click();
  await until(() => $("[data-dds-continue]"), 3000, "the case approved and ready");
  await settle(3);
  assert.deepEqual(started, [], "the run did not start on Approve");
  const button = $("[data-dds-continue]");
  assert.equal(button.textContent.trim(), "Start the campaign");
  assert.match($("[data-dds-continue-without]").textContent, /Continuing uses the data without the change left out above\./);
  const head = $('[data-dds-header] [data-dds-left-out="head"]');
  assert.ok(head && head.textContent.includes("says who agreed to be contacted"), "the verdict says what is left out");
  button.click();
  await until(() => started.length === 1, 3000, "the list started by the person's own choice");
  const s = screen("ready");
  assert.equal(started[0].target, s.view.start.outcome);
  assert.equal(started[0].upload.upload_id, s.view.start.upload_id);
  assert.equal(started[0].speed, "balanced", "the training time the person left as it is reaches the start");
});

test("in the wizard, approving a plan that leaves nothing out still starts nothing: the one Start button does", async () => {
  const started = [];
  await show("approve", { host: { place: "wizard", goalId: GOAL, approved: (fill) => started.push(fill) } });
  world.next.approval = "ready";
  $("[data-dds-approve-button]").click();
  await until(() => $("[data-dds-continue]"), 3000, "the case approved and ready");
  await settle(3);
  assert.deepEqual(started, [], "Approve did not start the run");
  assert.match($("[data-dds-start-sentence]").textContent, /^We will learn from .+ Balanced: about 15 minutes\./);
  assert.ok($("[data-dds-speed-open]"), "the Quick/Balanced choice sits behind Change");
  $("[data-dds-continue]").click();
  await until(() => started.length === 1, 3000, "the one Start");
});

test("Ready, with notes always shows a plain note", async () => {
  const s = await show("ready_with_warnings");
  assert.equal($("[data-dds-verdict]").dataset.ddsVerdict, "ready_with_warnings");
  assert.ok($("[data-dds-header]").textContent.includes("Ready, with notes"));
  assert.equal(s.view.verdict.reasons.length, 0, "this check's warnings have no sentence of their own to show");
  assert.equal($$("[data-dds-reasons] li").length, 0);
  assert.equal($("[data-dds-note]").textContent, s.view.verdict.note, "the plain note stands in for them");
  assert.equal(s.view.verdict.note, "Data Doctor found a few small things to know about this data. None of them stops this goal.");
});

// --- answers: change one, and what is remembered -------------------------------------------------------------

test("an answered question can be changed: Change answer shows its answers again", async () => {
  const s = await show("approve");
  const q = s.case.questions.find((x) => x.code === "KEY_AMBIGUOUS");
  const card = () => $(`[data-dds-question="${q.question_id}"]`);
  const change = card().querySelector("[data-dds-change]");
  assert.equal(change.textContent.trim(), "Change answer");
  assert.equal(change.getAttribute("aria-expanded"), "false");
  assert.equal(card().querySelectorAll("[data-dds-answer]").length, 0);
  change.click();
  await settle(1);
  const open = card().querySelector("[data-dds-change]");
  assert.equal(open.getAttribute("aria-expanded"), "true");
  assert.equal(document.activeElement, open, "the focus stays on the control");
  const list = document.getElementById(open.getAttribute("aria-controls"));
  assert.ok(list && card().contains(list), "the control names the answers it shows");
  const chosen = card().querySelector('[data-dds-answer="c0"]:not([data-dds-dont-know])');
  assert.equal(chosen.getAttribute("aria-pressed"), "true", "the answer given is marked");
  const other = card().querySelector('[data-dds-answer="c1"]');
  assert.equal(other.disabled, false);
  other.click();
  await until(() => posts("answers").length === 1, 2000, "the changed answer");
  assert.deepEqual([posts("answers")[0].body.question_id, posts("answers")[0].body.option_id], [q.question_id, "c1"]);
  await settle(2);
  assert.equal(card().querySelector("[data-dds-change]").getAttribute("aria-expanded"), "false", "closed once saved");
});

test("an answer remembered for next time says so where it was given, with its date and Revoke", async () => {
  const s = await show("approve");
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  const mine = s.view.remembered.find((r) => r.question_id === q.question_id);
  assert.ok(mine, "the server names the remembered answer by its question");
  const card = $(`[data-dds-question="${q.question_id}"]`);
  const memory = card.querySelector(`[data-dds-remembered="${mine.answer_id}"]`);
  assert.match(memory.textContent, /Remembered for next time, 12 Oct 2026\./);
  const key = s.case.questions.find((x) => x.code === "KEY_AMBIGUOUS");
  assert.ok(!$(`[data-dds-question="${key.question_id}"] [data-dds-remembered]`), "only what is remembered says so");
  memory.querySelector(`[data-dds-revoke="${mine.answer_id}"]`).click();
  await until(() => world.posted.some((p) => p.path === `/cases/${CASE}/standing-answers/${mine.answer_id}/revoke`), 2000, "the revoke");
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

test("the plan: sure fixes sit folded in their section, ticked as the case says; unticking sends the decision", async () => {
  const s = await show("approve");
  const section = $('[data-dds-plan-section="read"]');
  assert.ok(section.querySelector("h3, h4, h5").textContent.includes("Read the file"));
  const fold = section.querySelector("details[data-dds-sure]");
  assert.ok(fold && !fold.open, "a sure fix is folded away");
  assert.match(fold.querySelector("summary").textContent, /1 sure fix/);
  const item = s.case.items.find((i) => i.kind === "key");
  const box = $(`input[data-dds-tick="${item.item_id}"]`);
  assert.equal(box.checked, true);
  box.checked = false;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  await until(() => posts("decisions").length === 1, 2000, "the decision");
  assert.deepEqual([posts("decisions")[0].body.item_id, posts("decisions")[0].body.accept], [item.item_id, false]);
});

test("ready on the Data page: one sentence with the time, Change picks another, and Start runs the campaign on the server", async () => {
  const s = await show("ready");
  assert.equal($("[data-dds-verdict]").dataset.ddsVerdict, "ready");
  const sentence = $("[data-dds-start-sentence]").textContent;
  assert.match(sentence, /^We will learn from .+, 1,200 customers, and build your contact list\. Balanced: about 15 minutes\./);
  assert.match($("[data-dds-start-leave]").textContent, /You can close this page\./);
  assert.ok(!$("[data-dds-speeds]"), "the choices wait behind Change");
  $("[data-dds-speed-open]").click();
  await settle(1);
  assert.deepEqual(
    $$("[data-dds-speed]").map((b) => b.textContent.trim()),
    ["Quick: about 5 minutes", "Balanced: about 15 minutes (recommended)", "Thorough: about 40 minutes"],
  );
  $('[data-dds-speed="thorough"]').click();
  await settle(1);
  assert.match($("[data-dds-start-sentence]").textContent, /Thorough: about 40 minutes\./);
  const button = $("[data-dds-continue]");
  assert.equal(button.textContent.trim(), "Start the campaign");
  button.click();
  await until(() => window.location.hash === "#/campaigns/run/cr_dds", 3000, "the campaign's progress page");
  const sent = world.posted.find((p) => p.path === "/campaign-runs");
  assert.deepEqual(sent.body, {
    use_case: s.view.start.goal_id,
    upload_id: s.view.start.upload_id,
    primary_key: s.view.start.primary_key,
    target: s.view.start.outcome,
    speed: "thorough",
    case_id: CASE,
  });
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

const ALL = [
  "question",
  "assumed",
  "approve",
  "approve_class",
  "approve_class_own",
  "ready",
  "ready_with_warnings",
  "not_ready",
  "standing",
  "revoked",
];

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

test("the goals list shows the why once, best first, with the first ready or check goal's button primary", () => {
  const draw = (found) => {
    const host = document.createElement("div");
    host.innerHTML = view.goalsHtml({ case: { diagnosis: { use_cases: found } }, view: { mode: "generic" } });
    return [...host.querySelectorAll("[data-dds-goal-row]")];
  };
  const rows = draw([
    { use_case_id: "c_goal", name: "Third", status: "too_small", why: "c.csv has 5 customers.", customers: 5, yes_count: 0, first_fix: "Add more customers." },
    { use_case_id: "a_goal", name: "Second", status: "check", why: "b.csv has 10 customers.", customers: 10, yes_count: null, first_fix: "Open Guided setup and answer 1 question." },
    { use_case_id: "b_goal", name: "First", status: "ready", why: "a.csv has 4,000 customers; 276 of them are a yes in bought.", customers: 4000, yes_count: 276 },
  ]);
  assert.deepEqual(rows.map((r) => r.dataset.ddsGoalRow), ["b_goal", "a_goal", "c_goal"], "the ready goal comes first");
  assert.match(rows[0].textContent, /276 of them are a yes in bought/);
  assert.doesNotMatch(rows[0].textContent, /already did it/, "the counts are not said twice");
  assert.ok(rows[0].querySelector("[data-dds-goal-best]"));
  assert.ok(rows[0].querySelector(".btn.primary[data-dds-goal]"), "the first goal's button goes on");
  assert.ok(rows[1].querySelector(".btn.secondary[data-dds-goal]"));
  assert.equal(rows[1].querySelector("[data-dds-goal-fix]"), null, "a goal to check shows no 'Open Guided setup' step");
  assert.match(rows[2].querySelector("[data-dds-goal-fix]").textContent, /To do first: Add more customers\./);
  assert.equal(rows[0].querySelector("[data-dds-goal-fix]"), null, "a ready goal has nothing to do first");
  // No goal is ready or to check: nothing is called the best fit.
  const none = draw([{ use_case_id: "x_goal", name: "X", status: "too_small", why: "x.csv has 5 customers.", customers: 5 }]);
  assert.equal(none[0].querySelector("[data-dds-goal-best]"), null);
  assert.ok(none[0].querySelector(".btn.secondary[data-dds-goal]"));
  // With no why, the counts are said instead.
  const bare = draw([{ use_case_id: "y_goal", name: "Y", status: "ready", customers: 4000, yes_count: 276 }]);
  assert.match(bare[0].textContent, /4,000 customers, 276 already did it/);
});
