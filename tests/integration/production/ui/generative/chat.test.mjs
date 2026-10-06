/* The "Try it" chat as a conversation, in jsdom, against REAL bodies (PB_FIXTURES, written by
   tests/integration/test_assistant_manage_ui.py): starter questions from the graded reference set,
   a follow-up sent with the conversation so far, "Searched for", the confidence label and
   "New conversation" (DEC-1280 … DEC-1289). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const detail = fixture("detail");
const answer = fixture("answer");
const followup = fixture("answer_followup");
const indexes = fixture("indexes");

let nextAnswer = answer;
const server = ({ method, path }) => {
  let m;
  if (method === "GET" && path === `/use-cases/${uc.id}/indexes`) return { status: 200, body: indexes };
  if (path.match(/^\/indexes\/([^/]+)\/feedback$/)) return { status: 200, body: fixture("feedback_empty") };
  if (path.match(/^\/indexes\/([^/]+)\/ask$/)) return { status: 200, body: nextAnswer };
  if ((m = path.match(/^\/indexes\/([^/]+)$/)) && m[1] === ids.first) return { status: 200, body: detail };
  return null;
};

const { w, calls } = installPage(server, { hash: `#/generative/assistant/${uc.id}/${ids.first}` });
const { assistantHtml, createAssistantController } = await import("../../../../../ui/modules/generative/assistant.js");
const app = $("#app");
const controller = createAssistantController(uc, () => {
  app.innerHTML = assistantHtml(uc, controller.state);
  controller.bind(app);
});
const paint = () => {
  app.innerHTML = assistantHtml(uc, controller.state);
  controller.bind(app);
};
controller.stop();
await controller.loadIndex(ids.first);
controller.stop();
paint();

const asks = () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/ask"));
const click = (el) => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
async function type(question) {
  $("#g-question").value = question;
  $("#g-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  $("#g-ask").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => !controller.state.asking && $$(".gmsg.bot .loading").length === 0, 2000, "the answer");
}

test("an empty chat offers the starter questions the index passed, and no reset button", () => {
  const buttons = $$("[data-suggest]").map((b) => b.textContent);
  assert.deepEqual(buttons, detail.suggested_questions);
  assert.ok(buttons.length > 0 && buttons.length <= 4);
  const passed = new Set(detail.rag_eval.questions.filter((q) => q.passed && !q.expect_refusal).map((q) => q.question));
  for (const text of buttons) assert.ok(passed.has(text), `${text} is a reference question that passed`);
  assert.equal($("#g-new-chat"), null);
});

test("clicking a starter question asks it, with no history, and shows the confidence label", async () => {
  nextAnswer = answer;
  click($("[data-suggest]"));
  await until(() => asks().length === 1 && !controller.state.asking, 2000, "the first ask");
  assert.deepEqual(asks()[0].body, { question: detail.suggested_questions[0] }, "a first question sends no history");
  await until(() => $(".gmsg.bot .pill"), 2000, "the answer");
  const pill = $(".gconf .pill");
  assert.ok(pill, "the answer carries a confidence pill");
  assert.equal(pill.textContent, `${answer.confidence.level} confidence`);
  assert.ok($(".gconf").getAttribute("title").length > 0, "the pill says why");
  assert.equal($$("[data-suggest]").length, 0, "starters only show on an empty chat");
  assert.ok($("#g-new-chat"), "a conversation can be reset");
});

test("a follow-up is sent with the conversation so far and says what was searched for", async () => {
  nextAnswer = followup;
  await type(followup.question);
  const body = asks().at(-1).body;
  assert.equal(body.question, followup.question);
  assert.deepEqual(body.history, [{ question: detail.suggested_questions[0], answer: answer.answer }]);
  const meta = $$(".gmsg.bot .gmeta").at(-1).textContent;
  assert.ok(meta.includes(`Searched for: ${followup.searched_for}`), meta);
});

test("only the last three exchanges are sent", async () => {
  nextAnswer = answer;
  for (const q of ["one?", "two?", "three?"]) await type(q);
  const body = asks().at(-1).body;
  assert.equal(body.history.length, 3);
  assert.deepEqual(
    body.history.map((h) => h.question),
    [followup.question, "one?", "two?"],
  );
});

test("a retried answer and a failed rewrite say so; a refusal has no confidence pill", async () => {
  nextAnswer = { ...answer, attempts: 2, condense_error: "LLM_UNAVAILABLE", searched_for: null };
  await type("retried?");
  const meta = $$(".gmsg.bot .gmeta").at(-1).textContent;
  assert.ok(meta.includes("Written again once"), meta);
  assert.ok(meta.includes("Searched with your words as typed"), meta);
  nextAnswer = { ...answer, refused: true, citations: [], confidence: null };
  await type("refused?");
  const last = $$(".gmsg.bot").at(-1);
  assert.equal(last.querySelector(".gconf"), null);
});

test("New conversation clears the chat, brings the starters back and sends no history after it", async () => {
  click($("#g-new-chat"));
  assert.equal(controller.state.conversation.length, 0);
  assert.equal($$(".gmsg").length, 0);
  assert.ok($$("[data-suggest]").length > 0);
  nextAnswer = answer;
  await type("fresh start?");
  assert.deepEqual(asks().at(-1).body, { question: "fresh start?" });
});
