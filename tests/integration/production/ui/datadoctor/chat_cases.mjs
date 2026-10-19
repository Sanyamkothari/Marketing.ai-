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
      change.counts = "4,000 rows in, 3,812 out (188 fewer)";
      return a;
    },
  });
  const change = card(s, "change");
  const el = $(`[data-dds-change-card="${change.item_id}"]`);
  assert.equal(el.dataset.ddsChangeState, "accepted");
  assert.ok(el.textContent.includes(change.title.replace(/[“”]/g, "").slice(0, 10)) || el.textContent.length > 10);
  assert.match(el.textContent, /4,000 rows in, 3,812 out/);
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
  assert.match($("[data-ai-not-connected]").textContent, /Chat needs the AI service\. No AI service is connected\./);
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

test("Approve and start: the own yes, Approve with the sentence read, then the start - in that order, one click", async () => {
  const s = await show("approve_class");
  const ready = card(s, "ready");
  const rows = ready.confirmations.find((c) => c.required);
  const consent = ready.confirmations.find((c) => !c.required);
  const go = $("[data-dds-approve-start]");
  assert.equal($$("[data-dds-approve-start]").length, 1, "one approval");
  assert.equal(go.textContent.trim(), "Approve and start");
  assert.equal($("[data-dds-approve-sentence]").textContent, ready.sentence, "the sentence, word for word");
  assert.equal(go.disabled, true, "the change that needs its own yes is not ticked");
  assert.match($(`[data-dds-confirm-row="${rows.item_id}"]`).textContent, /Needed to go on/);
  assert.match($(`[data-dds-confirm-row="${consent.item_id}"]`).textContent, /Left out unless you agree/);
  assert.deepEqual(
    $$("[data-dds-speeds] .btn").map((b) => b.textContent.trim()),
    ["Quick: about 5 minutes", "Balanced: about 15 minutes (recommended)", "Thorough: about 40 minutes"],
  );
  $('[data-dds-speed="quick"]').click();
  await settle(1);
  tick($(`input[data-dds-confirm="${rows.item_id}"]`));
  assert.equal($("[data-dds-approve-start]").disabled, false, "ticked: Approve and start turns on, in place");
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 4000, "the start");
  assert.deepEqual(order(), ["confirmations", "approval", "campaign-runs"]);
  assert.deepEqual({ item: posts("confirmations")[0].body.item_id, sentence: posts("confirmations")[0].body.sentence }, { item: rows.item_id, sentence: rows.sentence });
  assert.equal(posts("approval")[0].body.sentence, ready.sentence, "Approve carries the sentence read");
  const run = world.posted.find((p) => p.path === "/campaign-runs").body;
  assert.equal(run.speed, "quick", "the learning time chosen in the card reaches the start");
  assert.equal(run.target, screen("ready").view.start.outcome);
  assert.ok(!posts("confirmations").some((p) => p.body.item_id === consent.item_id), "what was not ticked is left out");
});

test("the approval card says what the click leaves out and what happens after it", async () => {
  const s = await show("approve_class");
  const ready = card(s, "ready");
  const rows = ready.confirmations.find((c) => c.required);
  const consent = ready.confirmations.find((c) => !c.required);
  tick($(`input[data-dds-confirm="${rows.item_id}"]`));
  assert.match($("[data-dds-approve-leaves-out]").textContent, /^Approve and start leaves out:/);
  assert.ok($("[data-dds-approve-leaves-out]").textContent.includes(consent.sentence.slice(0, 20)), "the unticked change is named next to the button");
  assert.match($("[data-dds-approve-start-sentence]").textContent, /build your contact list\. \w+: about \d+ minutes\./);
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
  const s = await show("approve_class", { patch });
  const rows = card(s, "ready").confirmations.find((c) => c.required);
  tick($(`input[data-dds-confirm="${rows.item_id}"]`));
  assert.equal($("[data-dds-approve-start]").disabled, true, "a stop is open");
  assert.match($("[data-dds-stop-row]").textContent, /A safety check stops this plan: One step leaves no customers\./);
  tick($('input[data-dds-stop="k-stop"]'));
  assert.equal($("[data-dds-approve-start]").disabled, false);
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 4000, "the start");
  assert.deepEqual(order(), ["confirmations", "acknowledgements", "approval", "campaign-runs"], "the yeses first, then Approve");
  assert.equal(posts("acknowledgements")[0].body.key, "k-stop");
});

test("a stop that appears after the own yeses ends the chain: nothing is approved until the person ticks it", async () => {
  const patch = (a) => {
    const ready = a.view.conversation.find((c) => c.type === "ready");
    if (ready && ready.stage === "approve" && world.posted.some((p) => p.path.endsWith("/confirmations"))) {
      ready.stops = [{ key: "k-new", text: "Counting again found a new stop.", item_id: null }];
    }
    return a;
  };
  const s = await show("approve_class", { patch });
  tick($(`input[data-dds-confirm="${card(s, "ready").confirmations.find((c) => c.required).item_id}"]`));
  $("[data-dds-approve-start]").click();
  await until(() => $("[data-dds-notice]"), 3000, "the notice");
  assert.match($("[data-dds-notice]").textContent, /A safety check changed/);
  assert.ok($("[data-dds-ready] [data-dds-notice]"), "said in the approval card, next to the button, not at the top of a long page");
  assert.equal($$("[data-dds-notice]").length, 1, "and said once");
  assert.deepEqual(order(), ["confirmations"], "no yes to a stop the person did not tick, and no approval");
  assert.ok($("[data-dds-stop-row]"), "the new stop is in the card to read");
});

test("if agreeing changed the sentence, nothing is approved: the new sentence is shown to read first", async () => {
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
  const s = await show("approve_class", { patch });
  tick($(`input[data-dds-confirm="${card(s, "ready").confirmations.find((c) => c.required).item_id}"]`));
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
  assert.match($("[data-dds-start-sentence]").textContent, /^We will learn from .+ Balanced: about 15 minutes\./);
  $("[data-dds-continue]").click();
  await until(() => world.posted.some((p) => p.path === "/campaign-runs"), 3000, "the start");
});

test("next month the card still offers last month's model or learning again; Approve and start then waits for the choice", async () => {
  const offer = { model_version_id: "m_last", usable: true, reason: null, suggest: "reuse", hint: "This data looks like last month's." };
  await show("approve_class", { offer });
  tick($(`input[data-dds-confirm="${card(screen("approve_class"), "ready").confirmations.find((c) => c.required).item_id}"]`));
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
  const s = await show("approve_class", { host: { place: "setup", goalId: GOAL, approved: (fill) => filled.push(fill) } });
  assert.equal($("[data-dds-approve-start]").textContent.trim(), "Approve and continue", "nothing starts here, so the button does not say it does");
  assert.ok(!$("[data-dds-approve-start-sentence]"), "no start sentence where nothing starts");
  tick($(`input[data-dds-confirm="${card(s, "ready").confirmations.find((c) => c.required).item_id}"]`));
  world.next.approval = "ready";
  $("[data-dds-approve-start]").click();
  await until(() => filled.length === 1, 4000, "the form filled");
  assert.ok(!world.posted.some((p) => p.path === "/campaign-runs"));
  assert.equal(filled[0].caseId, CASE);
});

// --- look, accessibility, words ---------------------------------------------------------------------------------

test("phone width: the data panel is above the chat; every control is the theme's 40px button", async () => {
  assert.ok(CSS.includes("@media (max-width:900px){.ddc-layout{grid-template-columns:minmax(0,1fr)}.ddc-panel{order:-1;position:static;max-height:none;overflow:visible}}"));
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

test("the learning times are the card's own, one minute is not 'minutes', and the sentence says what the buttons say", async () => {
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
    assert.match($("[data-dds-approve-start-sentence]").textContent, /Balanced: about 7 minutes\./, "not the screen's built-in 15");
  } finally {
    rules.applySpeedMinutes(null);
  }
});
