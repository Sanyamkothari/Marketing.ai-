/* The cases of chat.test.mjs: the chat-first screen mounted on its own, as the Data page mounts it. */
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
const chat = await import("../../../../../ui/modules/datadoctor/chat.js");
const { CSS } = await import("../../../../../ui/modules/datadoctor/styles.js");
const { forgetAiServiceStatus } = await import("../../../../../ui/availability.js");

const app = $("#app");

/** Mount the screen on a fresh page in scenario `at` (the chat is what opens); `patch` changes the answer on its way out. */
async function show(at, options = {}) {
  world = makeWorld({ at, ...options });
  if (options.offer) world.offer = options.offer;
  world.patch = options.patch || null;
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
const order = () => world.posted.map((p) => p.path.split("/").pop()).filter((tail) => tail !== "visit"); // (opening the case resumes the visit)
const card = (screenAnswer, type) => screenAnswer.view.conversation.find((c) => c.type === type);
const tick = (el) => {
  el.checked = true;
  el.dispatchEvent(new window.Event("change", { bubbles: true }));
};
const connected = () => {
  const ai = structuredClone(fx("ai_service"));
  ai.slots.product.connected = true;
  return ai;
};

// --- the conversation -------------------------------------------------------------------------------------------

test("the chat is what opens: what was looked at, the questions as chips, the goals, a box to type in, the data beside it", async () => {
  const s = await show("question");
  assert.ok($("[data-dds-chat-view]"), "the chat view");
  assert.ok(!$("[data-dds-plan]") && !$("[data-dds-approve-button]"), "today's plan view is behind See details");
  assert.equal($("[data-dds-first-look]").textContent.trim(), card(s, "first_look").sentence);
  assert.match($("[data-dds-first-look]").textContent, /I looked at 1 file: customers_oct\.csv \(1,200 rows\)/);
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  const chips = [...$(`[data-dds-question="${q.question_id}"]`).querySelectorAll("[data-dds-answer]")].map((b) => b.textContent.trim());
  assert.deepEqual(chips, ["Yes: haa is yes, nahi is no", "Leave them as written", "I don't know"], "options, then the safe I don't know");
  assert.match($(`[data-dds-question="${q.question_id}"]`).textContent, /Changes 600 rows/);
  assert.ok($("[data-dds-goals] [data-dds-goal]"), "what the data is good for, as cards");
  assert.equal($("[data-dds-goal-best]").closest("[data-dds-goal-row]").querySelector(".btn").classList.contains("primary"), true);
  assert.ok($("[data-dds-doubt]") && $("[data-dds-request]"), "doubts and requests are cards in the conversation");
  assert.equal($$("[data-dds-prompt]").length, 3, "two or three suggested prompts");
  assert.ok($("#dds-chat-input") && $("[data-dds-panel] [data-dds-data]"), "the box and the data panel");
  assert.equal($(".dds-aside-root").hidden, true, "nothing is drawn in the side mount: the box and the panel are in the screen");
});

test("a question chip answers the question; I don't know takes the safe choice at once, never a guess", async () => {
  const s = await show("question");
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  world.next.answers = "question";
  $(`[data-dds-question="${q.question_id}"] [data-dds-dk-chip]`).click();
  await until(() => posts("answers").length === 1, 2000, "the answer");
  assert.deepEqual(
    { question_id: posts("answers")[0].body.question_id, option_id: posts("answers")[0].body.option_id, version: posts("answers")[0].body.version },
    { question_id: q.question_id, option_id: "c1", version: s.case.version },
  );
  assert.ok(posts("answers")[0].body.op_id);
  assert.ok(!$("[data-dds-who]"), "no panel in between: the click is the person's answer");
});

test("an answered question shows the answer and can still be changed by tapping another", async () => {
  const s = await show("approve_class");
  const key = s.case.questions.find((x) => x.code === "KEY_AMBIGUOUS");
  const el = $(`[data-dds-question="${key.question_id}"]`);
  assert.match(el.querySelector("[data-dds-answered]").textContent, /Your answer: customer_id\. Tap another answer to change it\./);
  assert.equal(el.querySelector('[data-dds-answer="c0"]').disabled, true, "the chosen one is pressed");
  assert.equal(el.querySelector('[data-dds-answer="c1"]').disabled, false);
});

test("a fix is a one-line change card with its counts and Undo; undo posts the untick", async () => {
  const s = await show("approve_class", {
    patch: (a) => {
      const change = a.view.conversation.find((c) => c.type === "change");
      change.counts = "Whole file: 4,000 rows, 3,812 kept, 188 left out";
      return a;
    },
  });
  const change = card(s, "change");
  const el = $(`[data-dds-change-card="${change.item_id}"]`);
  assert.equal(el.dataset.ddsChangeState, "accepted");
  assert.ok(el.textContent.includes(change.title.replace(/[“”]/g, "").slice(0, 10)) || el.textContent.length > 10);
  assert.match(el.textContent, /Whole file: 4,000 rows, 3,812 kept, 188 left out/);
  assert.match(el.textContent, /Done by|Your choice/);
  const undo = el.querySelector("[data-dds-undo]");
  assert.equal(undo.textContent.trim(), "Undo");
  undo.click();
  await until(() => posts("decisions").length === 1, 2000, "the untick");
  assert.deepEqual(
    { item_id: posts("decisions")[0].body.item_id, accept: posts("decisions")[0].body.accept },
    { item_id: change.item_id, accept: false },
  );
  assert.ok($(".ddc-change .btn").classList.contains("btn"));
});

test("a change waiting for a yes says so and can be used or skipped; an undone one can be done again", async () => {
  const s = await show("approve_class", {
    patch: (a) => {
      const at = a.view.conversation.findIndex((c) => c.type === "change");
      a.view.conversation.splice(at + 1, 0, { ...a.view.conversation[at], item_id: "ai.second_change", title: "Another change" }); // (the answers are on their questions, so one fix is all the fixture has)
      const [first, second] = a.view.conversation.filter((c) => c.type === "change");
      Object.assign(first, { state: "pending", undo: false, by: null, source: "ai" });
      Object.assign(second, { state: "rejected", undo: false, by: null });
      return a;
    },
  });
  const first = s.view.conversation.find((c) => c.type === "change");
  const second = { item_id: "ai.second_change" }; // (added by the patch)
  const waiting = $(`[data-dds-change-card="${first.item_id}"]`);
  assert.match(waiting.textContent, /Suggested by the AI\. Waiting for your yes\./);
  assert.equal(waiting.querySelector("[data-dds-use]").textContent.trim(), "Use this");
  waiting.querySelector("[data-dds-use]").click();
  await until(() => posts("decisions").length === 1, 2000, "use");
  assert.equal(posts("decisions")[0].body.accept, true);
  await until(() => !$("[data-dds-use]").disabled, 2000, "idle");
  $(`[data-dds-change-card="${first.item_id}"] [data-dds-undo]`).click();
  await until(() => posts("decisions").length === 2, 2000, "skip");
  assert.equal(posts("decisions")[1].body.accept, false);
  const undone = $(`[data-dds-change-card="${second.item_id}"]`);
  assert.equal(undone.querySelector("[data-dds-use]").textContent.trim(), "Do it again");
  assert.match(undone.textContent, /Undone\. Not in the plan\./);
});

test("with no goal the goals are cards; a goal chosen first is a fit, not a list", async () => {
  const s = await show("question");
  world.next.goal = "approve_class";
  const best = $("[data-dds-goal-best]").closest("[data-dds-goal-row]");
  best.querySelector("[data-dds-goal]").click();
  await until(() => posts("goal").length === 1, 2000, "the goal");
  assert.equal(posts("goal")[0].body.goal_id, best.dataset.ddsGoalRow);
  const chosen = await show("approve_class", { host: { place: "wizard", goalId: GOAL } });
  const fit = $("[data-dds-fit]");
  assert.equal(fit.dataset.ddsFit, GOAL);
  assert.equal(fit.dataset.ddsFitOk, "true");
  assert.match(fit.textContent, /A good fit for “Find customers likely to buy”/);
  assert.ok(!$("[data-dds-goals]"), "no list to pick from");
  assert.equal(s.view.mode, "generic");
  assert.equal(chosen.view.mode, "goal");
});

// --- the composer -----------------------------------------------------------------------------------------------

test("with no AI service the box says so in plain words and the buttons still work", async () => {
  const s = await show("question");
  assert.match($("[data-ai-not-connected]").textContent, /^Chat needs the AI service\. Connect it in Settings\.$/, "one short line");
  assert.ok($('[data-ai-settings-link][href="#/settings/ai"]'), "the way to Settings › AI service");
  assert.equal($("#dds-chat-input").disabled, true);
  assert.ok($$("[data-dds-prompt]").every((b) => b.disabled), "the prompts wait for the AI service");
  assert.ok($$("[data-dds-answer]").every((b) => !b.disabled), "questions still work");
  assert.ok($$("[data-dds-goal]").every((b) => !b.disabled), "goals still work");
  assert.equal(s.case.questions.length > 0, true);
});

test("with an AI service the box takes a message and a suggested prompt sends itself", async () => {
  await show("question", { ai: connected() });
  const input = $("#dds-chat-input");
  assert.equal(input.disabled, false);
  input.value = "use only this year's orders";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  $("#dds-chat-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => posts("chat").length === 1, 2000, "the message");
  assert.equal(posts("chat")[0].body.message, "use only this year's orders");
  await until(() => !$("#dds-chat-input").disabled, 2000, "idle");
  const prompt = $("[data-dds-prompt]");
  prompt.click();
  await until(() => posts("chat").length === 2, 2000, "the prompt");
  assert.equal(posts("chat")[1].body.message, prompt.dataset.ddsPrompt);
});

test("the agent's reply: what it did folded, what it took back, and an answer it suggests that only a click records", async () => {
  const s = await show("question", {
    ai: connected(),
    patch: (a) => {
      const q = a.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
      const ready = a.view.conversation.findIndex((c) => c.type === "ready");
      const messages = [
        { type: "message", role: "user", text: "haa means yes", item_ids: [], withdrawn: [], tools: [], suggestion: null, sent: [] },
        {
          type: "message",
          role: "agent",
          text: "I understood: haa is yes. Use this?",
          item_ids: [],
          withdrawn: ["Drop the test customers"],
          tools: ["get_profile", "propose_step", "some_new_tool"],
          suggestion: { question_id: q.question_id, option_id: "c0", label: "Yes: haa is yes, nahi is no", question: q.text, used: false },
          sent: [],
        },
      ];
      a.view.conversation.splice(ready === -1 ? a.view.conversation.length : ready, 0, ...messages);
      return a;
    },
  });
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  assert.equal($('[data-dds-msg="user"]').textContent, "haa means yes");
  assert.match($('[data-dds-msg="agent"]').textContent, /I understood: haa is yes\. Use this\?/);
  assert.match($("[data-dds-withdrawn]").textContent, /I took back: Drop the test customers/);
  const trace = [...$(".ddc-trace").querySelectorAll("li")].map((li) => li.textContent);
  assert.deepEqual(trace, ["Read the file's overview", "Suggested a change"], "plain phrases; a tool with none is not shown");
  assert.equal(posts("answers").length, 0, "a suggestion records nothing");
  const use = $(`[data-dds-suggestion="${q.question_id}"] [data-dds-use-answer]`);
  assert.equal(use.textContent.trim(), "Use this answer");
  use.click();
  await until(() => posts("answers").length === 1, 2000, "the click");
  assert.deepEqual({ q: posts("answers")[0].body.question_id, o: posts("answers")[0].body.option_id }, { q: q.question_id, o: "c0" });
});

test("a suggested answer is offered once, in its reply: the button, the other answers a tap away, no box that says it again", async () => {
  const s = await show("question", {
    ai: connected(),
    patch: (a) => {
      const q = a.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
      const ready = a.view.conversation.findIndex((c) => c.type === "ready");
      const at = a.view.conversation.findIndex((c) => c.type === "question" && c.question_id === q.question_id);
      const [own] = a.view.conversation.splice(at, 1); // (the server leaves the open question's card out while its reply offers an answer)
      const others = [...own.options.filter((o) => o.id !== "c0"), own.dont_know];
      a.view.conversation.splice(ready === -1 ? a.view.conversation.length : ready - 1, 0, {
        type: "message",
        role: "agent",
        text: "I understood: haa is yes. Use this?",
        item_ids: [],
        withdrawn: [],
        tools: [],
        sent: [],
        suggestion: { question_id: q.question_id, option_id: "c0", label: "Yes: haa is yes, nahi is no", question: q.text, used: false, others },
      });
      return a;
    },
  });
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  assert.equal($$(`[data-dds-question="${q.question_id}"]`).length, 0, "not also a question card");
  assert.equal($$("[data-dds-suggestion]").length, 1, "one place");
  assert.equal($$("[data-dds-use-answer]").length, 1, "one Use this answer");
  assert.doesNotMatch($("[data-dds-suggestion]").textContent, /I understood/, "the reply already says what was understood");
  assert.match($("[data-dds-suggestion]").textContent, /Yes: haa is yes, nahi is no/);
  const other = $(`[data-dds-suggestion="${q.question_id}"] .ddc-others [data-dds-dk-chip]`);
  assert.ok(other, "I don't know is one of the other answers");
  world.next.answers = "question";
  other.click();
  await until(() => posts("answers").length === 1, 2000, "the other answer");
  assert.equal(posts("answers")[0].body.question_id, q.question_id);
});

test("in the New campaign wizard the card around the chat names the customers: the chat adds no second heading", async () => {
  await show("approve_class", { host: { place: "wizard", goalId: GOAL } });
  assert.ok($("[data-dds-chat-view]") && $("[data-dds-verdict]"), "the verdict and the next step are still there");
  assert.equal($("[data-dds-chat-view] .ddc-head .dds-title"), null, "no heading of its own");
  assert.ok($("[data-dds-next]").textContent.length > 0);
  await show("approve_class");
  assert.match($("[data-dds-chat-view] .ddc-head .dds-title").textContent, /^Your data for/, "on the Data page the chat names itself");
});

test("one count of changes: the checklist, the first look and the Ready sentence say the same number", async () => {
  const s = await show("approve_class");
  const first = card(s, "first_look");
  const said = /with (\d+) changes?/.exec(card(s, "ready").sentence);
  assert.equal(first.fixes_count, Number(said[1]));
  assert.equal(chat.checklistOf(s).find((r) => r.id === "fixes").text, `${first.fixes_count} changes made`);
  assert.match($("[data-dds-first-look]").textContent, new RegExp(`I made ${first.fixes_count} changes on a copy`));
  assert.equal($('[data-dds-check-item="fixes"]').textContent.trim(), `${first.fixes_count} changes made`);
  const open = chat.checklistOf(s).find((r) => r.id === "questions");
  assert.equal(open.done, first.questions_count === 0, "and the questions left are the server's count too");
});

// --- the data panel ---------------------------------------------------------------------------------------------

const PREVIEW = {
  files: [
    {
      upload_id: "up1",
      file_name: "customers_oct.csv",
      total_rows: 1200,
      columns: [
        { name: "customer_id", changed: false, personal: false },
        { name: "email", changed: false, personal: true },
        { name: "lifetime_spend", changed: true, personal: false },
      ],
      before: [["c1", "[personal data]", "₹1,200"], ["c2", "[personal data]", "₹ 90"]],
      after: [["c1", "[personal data]", "1200"], ["c2", "[personal data]", "90"]],
      note: null,
    },
    { upload_id: "up2", file_name: "orders.csv", total_rows: 6029, columns: [{ name: "order_id", changed: false, personal: false }], before: [["o1"]], after: [["o1"]], note: null },
  ],
  step_samples: {},
};

test("the data panel: tabs per file, the first rows after the fixes or as in the file, fixed columns marked, personal ones hidden", async () => {
  await show("approve_class", { patch: (a) => ((a.view.preview = structuredClone(PREVIEW)), a) });
  const tabs = $$("[data-dds-file-tab]").map((b) => b.textContent.trim());
  assert.deepEqual(tabs, ["customers_oct.csv", "orders.csv"]);
  const after = $('[data-dds-preview-table="after"]');
  assert.deepEqual([...after.querySelectorAll("tbody tr:first-child td")].map((td) => td.textContent), ["c1", "[personal data]", "1200"]);
  assert.ok(after.querySelector("th.is-changed .ddc-mark"), "the fixed column is marked in words");
  assert.ok(after.querySelector("td.dds-hidden-cell"), "a personal column is drawn as hidden");
  $('[data-dds-side="before"]').click();
  await settle(1);
  assert.equal($('[data-dds-preview-table="before"] tbody tr:first-child td:last-child').textContent, "₹1,200");
  $('[data-dds-file-tab="up2"]').click();
  await settle(1);
  assert.equal($('[data-dds-preview-table="before"] th').textContent.trim(), "order_id");
});

test("without a preview the panel says rows appear once read; the checklist is live", async () => {
  await show("question");
  assert.ok($("[data-dds-no-rows]"));
  const list = (id) => $(`[data-dds-check-item="${id}"]`);
  assert.equal(list("looked").textContent.trim(), "Looked at 1 file");
  assert.equal(list("questions").textContent.trim(), "2 questions left");
  assert.ok(!list("questions").classList.contains("is-done"));
  assert.equal(list("goal").textContent.trim(), "Goal: not chosen yet");
  assert.equal(list("ready").textContent.trim(), "Not ready to start yet");
  await show("ready");
  assert.equal(list("goal").textContent.trim(), "Goal: Find customers likely to buy");
  assert.ok(list("ready").classList.contains("is-done") && list("questions").classList.contains("is-done"));
  assert.equal(list("ready").textContent.trim(), "Ready to start");
});

test("See details opens today's plan view, and Back to the chat returns", async () => {
  await show("approve_class");
  $("[data-dds-see-details]").click();
  await settle(1);
  assert.ok($("[data-dds-plan]") && $("[data-dds-approve-button]"), "the full plan view");
  assert.ok(!$("[data-dds-chat-view]"));
  $("[data-dds-back-to-chat]").click();
  await settle(1);
  assert.ok($("[data-dds-chat-view]") && !$("[data-dds-plan]"));
});

test("an answer without a conversation, or a card this screen does not know, never breaks it", async () => {
  await show("approve_class", { patch: (a) => ((a.view.conversation = []), a) });
  assert.ok($("[data-dds-plan]") && !$("[data-dds-chat-view]"), "an older server: the plan view is drawn");
  await show("approve_class", { patch: (a) => (a.view.conversation.splice(1, 0, { type: "from_the_future", text: "x" }), a) });
  assert.ok($("[data-dds-chat-view]") && $("[data-dds-ready]"), "the card is left out");
});

// --- the one approval -------------------------------------------------------------------------------------------

test("Approve and start: the own yes, Approve with the sentence read, then the start - in that order, ONE click and no boxes to tick first", async () => {
  const s = await show("approve_class");
  const ready = card(s, "ready");
  const rows = ready.confirmations.find((c) => c.required);
  const consent = ready.confirmations.find((c) => !c.required);
  const go = $("[data-dds-approve-start]");
  assert.equal($$("[data-dds-approve-start]").length, 1, "one approval");
  assert.equal(go.textContent.trim(), "Approve and start");
  assert.equal($("[data-dds-approve-sentence]").textContent, ready.sentence, "the sentence, word for word");
  assert.equal(go.disabled, false, "the change that needs its own yes is agreed above: nothing to tick before the click");
  assert.ok(!$(`input[data-dds-confirm="${rows.item_id}"]`), "an answer already given is not asked again as a box");
  assert.match($(`[data-dds-agreed="${rows.item_id}"]`).textContent, /Needs your yes: .*Approve and start gives your yes\./, "this goal asks for a separate yes: said in a line, given by the click");
  assert.match($(`[data-dds-confirm-row="${consent.item_id}"]`).textContent, /Also include: .*Left out unless you tick it/);
  assert.deepEqual(
    $$("[data-dds-speeds] .btn").map((b) => b.textContent.trim()),
    ["Quick: about 8 minutes", "Balanced: about 20 minutes (recommended)", "Thorough: about 45 minutes"],
  );
  $('[data-dds-speed="quick"]').click();
  await settle(1);
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 4000, "the start");
  assert.deepEqual(order(), ["confirmations", "approval", "campaign-runs"], "the yes, Approve and the start from the one click");
  assert.deepEqual({ item: posts("confirmations")[0].body.item_id, sentence: posts("confirmations")[0].body.sentence }, { item: rows.item_id, sentence: rows.sentence });
  assert.equal(posts("approval")[0].body.sentence, ready.sentence, "Approve carries the sentence read");
  const run = world.posted.find((p) => p.path === "/campaign-runs").body;
  assert.equal(run.speed, "quick", "the learning time chosen in the card reaches the start");
  assert.equal(run.target, screen("ready").view.start.outcome);
  assert.ok(!posts("confirmations").some((p) => p.body.item_id === consent.item_id), "what was not ticked is left out");
});

test("a left-out change the person includes is agreed in the same click; the sentence moving by just that is approved at once", async () => {
  const bump = (a, delta) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (ready && ready.stage === "approve" && world.posted.some((p) => p.path.endsWith("/confirmations"))) {
      a.view.approval_sentence = chat.sentenceWithMore(a.view.approval_sentence, delta);
      ready.sentence = a.view.approval_sentence;
    }
    return a;
  };
  const s = await show("approve_class", { patch: (a) => bump(a, 1) });
  const ready = card(s, "ready");
  const consent = ready.confirmations.find((c) => !c.required);
  tick($(`input[data-dds-confirm="${consent.item_id}"]`));
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 4000, "the start");
  assert.equal(posts("confirmations").length, 2, "the answer's yes and the included change's yes");
  assert.equal(posts("approval").length, 1, "one click, one approval: no second click for a sentence that only counted the yes");
  assert.equal(posts("approval")[0].body.sentence, chat.sentenceWithMore(ready.sentence, 1), "the sentence as it reads now");
  assert.ok(!$("[data-dds-notice]"), "nothing to read again");
});

test("the sentence only counts the changes the person just agreed to", () => {
  const sentence = "Prepare 1 file with 4 changes for the goal “Find customers likely to buy”. Nothing else changes.";
  assert.equal(chat.sentenceWithMore(sentence, 0), sentence);
  assert.equal(chat.sentenceWithMore(sentence, 1), "Prepare 1 file with 5 changes for the goal “Find customers likely to buy”. Nothing else changes.");
  assert.equal(chat.sentenceWithMore("Prepare 2 files with 0 changes. Nothing else changes.", 1), "Prepare 2 files with 1 change. Nothing else changes.");
  assert.equal(chat.sentenceWithMore("Something else entirely.", 1), "Something else entirely.");
});

test("the approval card says what the click leaves out and what happens after it", async () => {
  const s = await show("approve_class");
  const ready = card(s, "ready");
  const consent = ready.confirmations.find((c) => !c.required);
  assert.match($("[data-dds-approve-leaves-out]").textContent, /^Approve and start leaves out:/);
  assert.ok($("[data-dds-approve-leaves-out]").textContent.includes(consent.sentence.slice(0, 20)), "the unticked change is named next to the button");
  assert.match($("[data-dds-approve-start-sentence]").textContent, /^We will learn from .+ and build your contact list\.$/, "the minutes are the buttons' only");
  assert.doesNotMatch($("[data-dds-approve-start-sentence]").textContent, /minutes?/);
  assert.match($("[data-dds-ready] [data-dds-start-leave]").textContent, /You can close this page/);
  tick($(`input[data-dds-confirm="${consent.item_id}"]`));
  assert.ok(!$("[data-dds-approve-leaves-out]"), "ticked: nothing is left out");
});

test("a change a teammate made is not called the reader's choice; an answer's counts are told on its question", async () => {
  await show("approve_class", {
    patch: (a) => {
      const change = a.view.conversation.find((c) => c.type === "change");
      change.by = "someone";
      a.view.conversation.find((c) => c.type === "question" && c.answer).counts = "600 values changed";
      return a;
    },
  });
  assert.match($(".ddc-change").textContent, /Chosen by a teammate/);
  assert.doesNotMatch($(".ddc-change").textContent, /Your choice/);
  assert.match($("[data-dds-answered]").textContent, /Your answer: .*\. 600 values changed\. Tap another answer to change it\./);
});

test("after a reply the page follows the reply, not the approval card that always stays last", async () => {
  const seen = [];
  const original = window.Element.prototype.scrollIntoView;
  window.Element.prototype.scrollIntoView = function () {
    seen.push(this);
  };
  try {
    await show("approve_class", {
      ai: connected(),
      patch: (a) => {
        if (!posts("chat").length) return a;
        const ready = a.view.conversation.findIndex((c) => c.type === "ready");
        const m = { type: "message", item_ids: [], withdrawn: [], tools: [], suggestion: null, sent: [] };
        a.view.conversation.splice(ready, 0, { ...m, role: "user", text: "why?" }, { ...m, role: "agent", text: "Because of the age column." });
        return a;
      },
    });
    const input = $("#dds-chat-input");
    input.value = "why?";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    $("#dds-chat-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => $('[data-dds-msg="agent"]'), 3000, "the reply");
    await settle(2);
    assert.ok(seen.length >= 1);
    const last = seen[seen.length - 1];
    assert.ok(last.querySelector('[data-dds-msg="agent"]'), "the reply is what is scrolled to");
    assert.ok(!last.querySelector("[data-dds-ready]"));
  } finally {
    window.Element.prototype.scrollIntoView = original;
  }
});

test("a stop is a checkbox in the card: nothing goes on until it is ticked, then it is said yes to before Approve", async () => {
  const stop = { key: "k-stop", text: "One step leaves no customers.", item_id: null };
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (ready && ready.stage === "approve" && !world.posted.some((p) => p.path.endsWith("/acknowledgements"))) ready.stops = [stop];
    return a;
  };
  await show("approve_class", { patch });
  assert.equal($("[data-dds-approve-start]").disabled, true, "a stop is open");
  assert.match($("[data-dds-stop-row]").textContent, /^One step leaves no customers\./);
  assert.match($("[data-dds-stop-row]").textContent, /A safety check stops this plan until you say yes/);
  tick($('input[data-dds-stop="k-stop"]'));
  assert.equal($("[data-dds-approve-start]").disabled, false);
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 4000, "the start");
  assert.deepEqual(order(), ["confirmations", "acknowledgements", "approval", "campaign-runs"], "the yeses first, then Approve");
  assert.equal(posts("acknowledgements")[0].body.key, "k-stop");
});

test("if acknowledging a stop changes the sentence, nothing is approved: the new sentence is shown to read first", async () => {
  const stop = { key: "k-stop", text: "One step leaves no customers.", item_id: null };
  const next = "Prepare 3 files with 9 changes for the goal “Something else”. Nothing else changes.";
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (!ready || ready.stage !== "approve") return a;
    if (world.posted.some((p) => p.path.endsWith("/acknowledgements"))) {
      a.view.approval_sentence = next;
      ready.sentence = next;
      ready.stops = [];
    } else ready.stops = [stop];
    return a;
  };
  await show("approve_class", { patch });
  tick($('input[data-dds-stop="k-stop"]'));
  $("[data-dds-approve-start]").click();
  await until(() => $("[data-dds-notice]"), 3000, "the notice");
  assert.match($("[data-dds-notice]").textContent, /What you approve has changed/);
  assert.equal($("[data-dds-approve-sentence]").textContent, next);
  assert.equal(posts("approval").length, 0, "the sentence the person did not read is not approved");
});

test("a stop that appears after the own yeses ends the chain: nothing is approved until the person ticks it", async () => {
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (ready && ready.stage === "approve" && world.posted.some((p) => p.path.endsWith("/confirmations"))) {
      ready.stops = [{ key: "k-new", text: "Counting again found a new stop.", item_id: null }];
    }
    return a;
  };
  await show("approve_class", { patch });
  $("[data-dds-approve-start]").click();
  await until(() => $("[data-dds-notice]"), 3000, "the notice");
  assert.match($("[data-dds-notice]").textContent, /A safety check changed/);
  assert.ok($("[data-dds-ready] [data-dds-notice]"), "said in the approval card, next to the button, not at the top of a long page");
  assert.equal($$("[data-dds-notice]").length, 1, "and said once");
  assert.deepEqual(order(), ["confirmations"], "no yes to a stop the person did not tick, and no approval");
  assert.ok($("[data-dds-stop-row]"), "the new stop is in the card to read");
});

test("if agreeing changed the sentence by more than the yeses, nothing is approved: the new sentence is shown to read first", async () => {
  const next = "Prepare 1 file with 5 changes for the goal “Find customers likely to buy”. Nothing else changes.";
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (ready && ready.stage === "approve" && world.posted.some((p) => p.path.endsWith("/confirmations"))) {
      a.view.approval_sentence = next;
      ready.sentence = next;
      ready.confirmations = ready.confirmations.filter((c) => !posts("confirmations").some((p) => p.body.item_id === c.item_id));
    }
    return a;
  };
  await show("approve_class", { patch });
  $("[data-dds-approve-start]").click();
  await until(() => $("[data-dds-notice]"), 3000, "the notice");
  assert.match($("[data-dds-notice]").textContent, /Your yes is recorded\. What you approve has changed/);
  assert.ok($("[data-dds-ready] [data-dds-notice]"), "said in the approval card, next to the button");
  assert.equal($("[data-dds-approve-sentence]").textContent, next);
  assert.deepEqual(order(), ["confirmations"], "no approval of a sentence the person did not read");
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => posts("approval").length === 1, 3000, "the second click");
  assert.equal(posts("approval")[0].body.sentence, next);
});

test("a change someone else must agree to blocks the card and says whom to ask", async () => {
  await show("approve_class_own");
  const go = $("[data-dds-approve-start]");
  assert.equal(go.disabled, true);
  assert.ok($("[data-dds-approve-hold]"));
  assert.match($("[data-dds-ask-other]").textContent, /ask (another )?(an? )?(analyst|approver) on your team to agree/i);
  assert.equal($$("input[data-dds-confirm]").length, 0, "no checkbox that can only be refused");
  go.click();
  await settle(2);
  assert.deepEqual(order(), []);
});

test("approved already: the card is the start - the sentence, what was left out, the learning time, one Start", async () => {
  const s = await show("ready");
  const el = $('[data-dds-ready="start"]');
  assert.ok(el && !$("[data-dds-approve-start]"));
  assert.equal(el.querySelector("[data-dds-approved-sentence]").textContent, card(s, "ready").sentence);
  assert.match(el.querySelector("[data-dds-left-out]").textContent, /Left out, because nobody agreed:.*says who agreed to be contacted/);
  assert.match($("[data-dds-start-sentence]").textContent, /^We will learn from .+ Balanced: about 20 minutes\./);
  $("[data-dds-continue]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 3000, "the start");
});

test("next month the card still offers last month's model or learning again; Approve and start then waits for the choice", async () => {
  const offer = { model_version_id: "m_last", usable: true, reason: null, suggest: "reuse", hint: "This data looks like last month's." };
  await show("approve_class", { offer });
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => $('[data-dds-ready="start"]'), 4000, "the start stage");
  await settle(2);
  assert.ok(!world.posted.some((p) => p.path === "/campaign-runs"), "the choice is the person's, not made by the click");
  assert.match($("[data-dds-notice]").textContent, /Approved\. Choose how to start below\./);
  assert.deepEqual($$("[data-dds-mode]").map((b) => b.textContent.trim()), ["Use last month's model (fast)", "Learn again on the new data (slower)"]);
});

test("in a goal's setup the one click approves and fills the form (nothing runs)", async () => {
  const filled = [];
  await show("approve_class", { host: { place: "setup", goalId: GOAL, approved: (fill) => filled.push(fill) } });
  assert.equal($("[data-dds-approve-start]").textContent.trim(), "Approve and continue", "nothing starts here, so the button does not say it does");
  assert.ok(!$("[data-dds-approve-start-sentence]"), "no start sentence where nothing starts");
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => filled.length === 1, 4000, "the form filled");
  assert.ok(!world.posted.some((p) => p.path === "/campaign-runs"));
  assert.equal(filled[0].caseId, CASE);
});

// --- look, accessibility, words ---------------------------------------------------------------------------------

test("phone width: the data panel is one line above the chat; every control is the theme's 40px button", async () => {
  assert.ok(CSS.includes("@media (max-width:900px){.ddc-layout{grid-template-columns:minmax(0,1fr)}.ddc-panel{order:-1;position:static;max-height:none;overflow:visible}"));
  assert.ok(
    CSS.includes("position:sticky;top:calc(var(--bar-h,56px) + var(--s4));max-height:calc(100vh - var(--bar-h,56px) - 2*var(--s4));overflow:auto}"),
    "the panel sits below the top bar and scrolls inside itself, so its foot is reachable",
  );
  assert.ok(CSS.includes(".ddc-panel>*{flex:none}"), "a card of the panel is never squeezed (its last line clipped) by the others");
  assert.ok(CSS.includes(".ddc-form .btn{min-height:40px}") && CSS.includes(".ddc-prompts .btn{min-height:40px"));
  assert.ok(!/#[0-9a-fA-F]{3,6}\b/.test(CSS), "colours are the theme's tokens");
  await show("approve_class", { ai: connected() });
  for (const b of $$("[data-dds-chat-view] button")) assert.ok(b.classList.contains("btn"), `a theme button: ${b.outerHTML.slice(0, 80)}`);
  const html = app.innerHTML;
  assert.ok(html.indexOf("data-dds-conversation") < html.indexOf("data-dds-panel"), "the conversation comes first in reading order");
});

const ALL = ["question", "assumed", "approve", "approve_class", "approve_class_own", "ready", "ready_with_warnings", "not_ready", "standing", "revoked"];

test("accessibility (Plan M M181): every shape of the chat passes the structural checks", async () => {
  for (const at of ALL) {
    await show(at, { ai: connected() });
    assert.ok($("[data-dds-chat-view]"), `${at}: the chat`);
    const found = check(app).violations;
    assert.deepEqual(found, [], `${at}: ${JSON.stringify(found)}`);
  }
});

test("visible text: no word the glossary keeps from a marketer, in any shape of the chat", async () => {
  for (const at of ALL) {
    await show(at);
    const v = violations(visibleText(app));
    assert.ok(v.ok, describe(`chat ${at}`, v));
  }
});

test("the checklist and the prompts are plain functions of the answer", () => {
  const s = screen("approve_class");
  assert.deepEqual(chat.checklistOf(s).map((r) => r.id), ["looked", "fixes", "questions", "goal", "ready"]);
  assert.equal(chat.promptsOf(s).length, 3);
  assert.equal(chat.cardOf(s, "ready").stage, "approve");
  assert.equal(chat.cardOf({ view: {} }, "ready"), null);
});

test("the learning times are the card's own, in one place and one set of numbers; one minute is not 'minutes'", async () => {
  const rules = await import("../../../../../ui/modules/campaigns/run_rules.js");
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (ready) ready.speeds = [{ id: "quick", name: "Quick", minutes: 1, recommended: false }, { id: "balanced", name: "Balanced", minutes: 7, recommended: true }, { id: "thorough", name: "Thorough", minutes: 20, recommended: false }];
    return a;
  };
  try {
    await show("approve_class", { patch });
    assert.deepEqual(
      $$("[data-dds-speeds] .btn").map((b) => b.textContent.trim()),
      ["Quick: about 1 minute", "Balanced: about 7 minutes (recommended)", "Thorough: about 20 minutes"],
    );
    assert.doesNotMatch($("[data-dds-approve-start-sentence]").textContent, /minutes?/, "the sentence does not say the minutes again: the buttons are the one place");
    assert.match($('[data-dds-speed="balanced"]').textContent, /about 7 minutes/, "not the screen's built-in 15");
  } finally {
    rules.applySpeedMinutes(null);
  }
});

test("Approve waits until a goal is chosen: the card says so and the button is off", async () => {
  const s = await show("approve");
  assert.equal(card(s, "ready").hold, "Choose what the campaign is for first.");
  const go = $("[data-dds-approve-start]");
  assert.equal(go.disabled, true);
  assert.match($("[data-dds-approve-hold]").textContent, /Choose what the campaign is for first\./);
  go.click();
  await settle(2);
  assert.deepEqual(order(), [], "nothing is approved before there is a goal");
});

test("while the last check found something to fix, Approve is off and the card says to fix it first", async () => {
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    ready.hold = "Fix what the last check found first.";
    ready.notes = ["Say which values of 'marketing_opt_in' mean yes."];
    return a;
  };
  await show("approve_class", { patch });
  assert.equal($("[data-dds-approve-start]").disabled, true);
  assert.match($("[data-dds-approve-hold]").textContent, /Fix what the last check found first\./);
  assert.match($("[data-dds-ready-notes]").textContent, /Say which values/, "the reasons stay in the card");
});

test("a change that needs its own yes is said once in the card, as agreed (an answer) or shown above with Undo; never a box", async () => {
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    const required = ready.confirmations.find((c) => c.required);
    required.separate = false;
    required.answered = false;
    required.shown = true;
    return a;
  };
  const s = await show("approve_class", { patch });
  const ready = card(s, "ready");
  const rows = ready.confirmations.find((c) => c.required);
  const consent = ready.confirmations.find((c) => !c.required);
  assert.match($(`[data-dds-agreed="${rows.item_id}"]`).textContent, /^Shown above, with Undo: /);
  assert.equal($$("input[data-dds-confirm]").length, 1, "only the left-out change has a box");
  assert.ok($(`input[data-dds-confirm="${consent.item_id}"]`));
  await show("approve_class", { patch: (a) => (a.view.conversation.find((c) => c.type === "ready").confirmations.find((c) => c.required).separate = false, a) });
  assert.match($(`[data-dds-agreed="${rows.item_id}"]`).textContent, /^Already agreed: .*Recorded when you click Approve and start\.$/);
});

test("when a separate yes is the rule the card says why in one line, and the one click gives it", async () => {
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    ready.confirmations.find((c) => c.required).separate = true;
    return a;
  };
  const s = await show("approve_class", { patch });
  const rows = card(s, "ready").confirmations.find((c) => c.required);
  const line = $(`[data-dds-agreed="${rows.item_id}"]`).textContent;
  assert.match(line, /^Needs your yes: .*Someone other than whoever suggested it must agree \(.+\)\. Approve and start gives your yes\.$/);
  assert.equal($(`[data-dds-agreed="${rows.item_id}"]`).querySelectorAll("br").length, 1, "one line of why");
  assert.equal($("[data-dds-approve-start]").disabled, false);
});

test("a refused start is told in the card with the Start button, not at the top of the page", async () => {
  await show("ready");
  world.refuseRun = { status: 409, body: { detail: { code: "RUN_REFUSED", message: "A campaign is running already." } } };
  $("[data-dds-continue]").click();
  await until(() => $("[data-dds-ready] [role=alert]"), 3000, "the refusal in the card");
  assert.equal($$("[role=alert]").length, 1, "and said once");
  assert.ok($("[data-dds-ready] [data-dds-continue]"), "next to the button that can try again");
});

// --- what a real browser walk of the whole Online Retail file found (U1) --------------------------------------------

test("the progress line 'Ready to start' is the ready card's own word: one source of truth", () => {
  const ready = chat.checklistOf(screen("approve_class")).find((r) => r.id === "ready");
  assert.equal(ready.text, "Ready to start", "the card says Ready to start, so does the list");
  assert.equal(ready.done, true);
  const asked = chat.checklistOf(screen("question")).find((r) => r.id === "ready");
  assert.deepEqual([asked.text, asked.done], ["Not ready to start yet", false], "no card while a question is open");
  const held = structuredClone(screen("approve_class"));
  Object.assign(chat.cardOf(held, "ready"), { hold: "Choose what the campaign is for first." });
  assert.equal(chat.checklistOf(held).find((r) => r.id === "ready").done, false, "a card that holds Approve is not ready");
});

test("the goal is named by its display name in the progress list, never by its id", () => {
  const s = structuredClone(screen("approve_class"));
  s.view.goal_id = "retail-win-back";
  s.view.conversation = s.view.conversation.filter((c) => c.type !== "fit");
  assert.equal(chat.checklistOf(s).find((r) => r.id === "goal").text, "Goal: Retail win back", "never the raw id");
  s.view.conversation.push({ type: "goals", cards: [{ goal_id: "retail-win-back", name: "Retail Win-back", why: "", best: true, status: "fits" }], more_count: 0 });
  assert.equal(chat.checklistOf(s).find((r) => r.id === "goal").text, "Goal: Retail Win-back", "the goal card's name");
  s.view.conversation.push({ type: "fit", goal_id: "retail-win-back", name: "Win back lapsed shoppers", ok: true, sentence: "x" });
  assert.equal(chat.checklistOf(s).find((r) => r.id === "goal").text, "Goal: Win back lapsed shoppers", "the server's own name when it gave one");
});

test("the goal button says 'Choose this goal', as every goal button of the Data Doctor does", async () => {
  await show("question");
  assert.ok($$("[data-dds-goal]").length > 0);
  assert.ok($$("[data-dds-goal-row] [data-dds-goal]").every((b) => b.textContent.trim() === "Choose this goal"));
});

test("a change the goal needs is part of the plan the one approval gives, not a box left unticked", () => {
  const html = chat.readyHtml(
    {
      type: "ready",
      stage: "approve",
      sentence: "Prepare 1 file with 3 changes for the goal “Retail Win-back”. Nothing else changes.",
      stops: [],
      speeds: [],
      confirmations: [
        { item_id: "build", sentence: "Training table: one row per shopper.", why: "it makes a new table with a query", role: "analyst", required: true, can_agree: true, separate: true, shown: true, answered: false },
      ],
    },
    { stops: new Set(), confirms: new Set(), names: [] },
  );
  assert.match(html, /data-dds-agreed="build"/);
  assert.doesNotMatch(html, /Left out unless you tick it/, "no 'left out unless you tick it' for what the goal needs");
  assert.doesNotMatch(html, /type="checkbox"/);
  assert.match(html, /Approve and start gives your yes/);
});

test("the consent prompt says what the AI will and will not see, and a line confirms once it is given", () => {
  const ask = chat.consentCardHtml({ ai: true, use: { consent: false }, canConsent: true });
  assert.match(ask, /data-dds-consent-card/);
  assert.match(ask, /masked summaries/);
  assert.match(ask, /never see a cell value/);
  assert.match(ask, /rules still run/);
  assert.match(ask, /data-dds-ai-consent/);
  assert.equal(chat.consentCardHtml({ ai: false, use: { consent: false } }), "", "no AI service: nothing to allow");
  assert.equal(chat.consentCardHtml({ ai: true, use: { consent: true } }), "", "allowed earlier: no prompt");
  const done = chat.consentCardHtml({ ai: true, use: { consent: true }, consented: true });
  assert.match(done, /data-dds-consent-done/);
  assert.match(done, /Allowed\./);
  assert.match(done, /never cell values/);
  assert.doesNotMatch(done, /data-dds-ai-consent/);
  assert.match(chat.consentCardHtml({ ai: true, use: { consent: false }, canConsent: false }), /Ask someone who can change this check/);
});

test("the consent prompt follows the first look, and allowing it replaces the prompt with a confirmation", async () => {
  world = makeWorld({ at: "question" });
  const open = {
    ...(await show("question", { ai: connected() })),
  };
  assert.ok(open);
  world.use = { consent: false, spent: false };
  world.ai = connected();
  _resetDataDoctorForTests();
  forgetAiServiceStatus();
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data" });
  await until(() => $("[data-dds-consent-card]"), 3000, "the consent prompt");
  const prompt = $("[data-dds-consent-card]");
  assert.equal(prompt.closest(".ddc-row").previousElementSibling.querySelector("[data-dds-first-look]") !== null, true, "right after the first look");
  assert.match(prompt.textContent, /masked summaries/);
  assert.match(prompt.textContent, /never see a cell value/);
  prompt.querySelector("[data-dds-ai-consent]").click();
  await until(() => $("[data-dds-consent-done]"), 3000, "the confirmation");
  assert.equal(posts("ai-consent").length, 1);
  assert.match($("[data-dds-consent-done]").textContent, /Allowed\..*never cell values/);
  assert.ok(!$("[data-dds-consent-card]"), "the prompt is gone once it is given");
});

test("the data panel says which table the rows are: the file, or the table a build makes, with its rows", () => {
  const s = structuredClone(screen("approve_class"));
  const file = { upload_id: "u1", file_name: "orders.csv", total_rows: 541909, columns: [{ name: "InvoiceNo", changed: false, personal: false }, { name: "n", changed: true, personal: false }], before: [["1", "2"]], after: [["", "5"]], left_out_columns: 0, after_table: "training_retail_win_back", after_rows: 1402 };
  s.view.preview = { files: [file] };
  s.view.ledger = { ...(s.view.ledger || {}), rows_out: 9999, sampled_from: 1000 }; // a sampled plan check, as on any real file
  const built = chat.panelHtml(s, { tab: "u1", side: "after" });
  assert.match(built, /First rows of the table the plan builds from this file \(training_retail_win_back, 1,402 rows; the file has 541,909 rows\)/);
  assert.doesNotMatch(built, /after the fixes \(of 541,909\)/i);
  assert.doesNotMatch(built, /ddc-mark">fixed|is-changed/, "the columns of a new table are not the file's columns fixed");
  const before = chat.panelHtml(s, { tab: "u1", side: "before" });
  assert.match(before, /First rows of your file, as they are \(of 541,909 rows\)/);
  s.view.preview = { files: [{ ...file, after_table: null }] };
  assert.match(chat.panelHtml(s, { tab: "u1", side: "after" }), /First rows after the fixes \(of 541,909 rows\), 1 column fixed/);
});


// --- coming back to an approved case (the walk's revisit) ------------------------------------------------------------

test("the preview caption is said once, and a built table shows its own columns while the file keeps its own", async () => {
  const built = structuredClone(PREVIEW);
  built.files[0].columns = [{ name: "customer_id", changed: false, personal: false }, { name: "n", changed: false, personal: false }];
  built.files[0].before_columns = [{ name: "order_id", changed: false, personal: false }, { name: "when", changed: false, personal: false }, { name: "amount", changed: false, personal: false }];
  built.files[0].before = [["o1", "2026-01-01", "5"]];
  built.files[0].after = [["c1", "3"]];
  built.files[0].after_table = "out";
  await show("approve_class", { patch: (a) => ((a.view.preview = built), a) });
  assert.deepEqual($$('[data-dds-preview-table="after"] th').map((th) => th.textContent.trim()), ["customer_id", "n"]);
  const captions = $$("[data-dds-data] p.dds-why").filter((p) => /^First rows/.test(p.textContent));
  assert.equal(captions.length, 1, "one caption");
  assert.ok(!$("[data-dds-data] caption"), "no second copy of it in a hidden caption");
  assert.equal(visibleText($("[data-dds-data]")).split("First rows").length - 1, 1, "said once in everything a reader gets");
  assert.equal($('[data-dds-preview-table="after"]').getAttribute("aria-describedby"), captions[0].id, "the table is described by that one caption");
  $('[data-dds-side="before"]').click();
  await settle(1);
  assert.deepEqual($$('[data-dds-preview-table="before"] th').map((th) => th.textContent.trim()), ["order_id", "when", "amount"], "the file as it is");
  assert.deepEqual($$('[data-dds-preview-table="before"] tbody tr td').map((td) => td.textContent), ["o1", "2026-01-01", "5"]);
});

test("while a question waits the chat box rests at the end of the conversation instead of floating over its choices", async () => {
  await show("question");
  assert.ok($("[data-dds-composer]").classList.contains("is-asking"));
  assert.match(CSS, /\.ddc-composer\.is-asking\{position:static\}/);
  await show("ready");
  assert.ok(!$("[data-dds-composer]").classList.contains("is-asking"), "nothing is waiting: the box stays in reach");
});

test("coming back to a screen drawn before reads the case again: an approved case is not shown as new", async () => {
  await show("question");
  assert.ok($("[data-dds-question]"), "a question is open");
  world.at = "ready"; // approved meanwhile (another page, a teammate)
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  const kept = mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data" }); // the Data page mounts it again
  kept.refreshedAt = 0; // (the screen was read a moment ago; a revisit is later)
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data" });
  await until(() => $("[data-dds-ready=\"start\"]"), 3000, "the approved card after the screen is read again");
  assert.ok(!$$("[data-dds-question]").some((q) => !q.querySelector("[data-dds-answered]")), "nothing answered is asked again");
});

test("an approved case with a run going says it is approved and going, with the way to the run, and offers no second start", async () => {
  await show("ready", { runOfCase: null });
  assert.ok($("[data-dds-continue]"), "no run yet: Start is offered");
  world.runOfCase = { record: { campaign_run_id: "cr_dds", state: "running" }, pct: 40, summary: "" };
  _resetDataDoctorForTests();
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data" });
  await until(() => $("[data-dds-run-link]"), 3000, "the run link");
  assert.equal($("[data-dds-ready=\"start\"] h3").textContent.trim(), "Approved");
  assert.match($("[data-dds-run-says]").textContent, /being prepared now.*40% done/);
  assert.ok($("[data-dds-approved-sentence]"), "what was approved is shown");
  assert.ok(!$("[data-dds-continue]"), "no second start");
  assert.equal($("[data-dds-run-link]").getAttribute("href"), "#/campaigns/run/cr_dds");
});

test("a finished run, or a run seen from the wizard, keeps Start: the run says nothing of the plan now approved", async () => {
  const remount = async (run, place, ready) => {
    world.runOfCase = run;
    _resetDataDoctorForTests();
    app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
    mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place });
    await until(() => $(ready), 3000, ready);
    await settle(2);
  };
  await show("ready");
  await remount({ record: { campaign_run_id: "cr_dds", state: "done" }, pct: 100, summary: "" }, "data", "[data-dds-continue]");
  assert.ok(!$("[data-dds-run-says]"), "a done run on the Data page: Start stays, nothing is claimed");
  await remount({ record: { campaign_run_id: "cr_dds", state: "running" }, pct: 40, summary: "" }, "wizard", "[data-dds-continue]");
  assert.ok(!$("[data-dds-run-link]"), "the wizard does not read the run");
});
