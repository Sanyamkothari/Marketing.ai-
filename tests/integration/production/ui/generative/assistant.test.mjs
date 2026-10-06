/* The AI Onboarding Assistant's Results view in jsdom, against REAL bodies (PB_FIXTURES, written by
   tests/integration/test_assistant_manage_ui.py): a citation opens its passage with the quote marked,
   an answer takes a thumbs up or down, the thumbs-down questions download as a test-question file,
   "Update documents" builds a new version, the best version cannot be deleted, and two graded
   versions compare side by side (DEC-1270 … DEC-1279). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const detail = fixture("detail");
const updated = fixture("detail_updated");
const answer = fixture("answer");
const chunk = fixture("chunk");
const neighbour = fixture("chunk_neighbour");
const indexes = fixture("indexes");
const csvText = fixture("test_questions_csv").text;

let feedbackBody = fixture("feedback_empty");
const details = { [ids.first]: detail, [ids.second]: updated };
const chunks = { [chunk.chunk_id]: chunk, [neighbour.chunk_id]: neighbour };

const server = ({ method, path }) => {
  let m;
  if (method === "GET" && path === `/use-cases/${uc.id}/indexes`) return { status: 200, body: indexes };
  if ((m = path.match(/^\/indexes\/([^/]+)\/chunks\/([^/]+)$/))) {
    const body = chunks[decodeURIComponent(m[2])];
    return body ? { status: 200, body } : null;
  }
  if ((m = path.match(/^\/indexes\/([^/]+)\/feedback$/))) {
    if (method === "POST") {
      feedbackBody = fixture("feedback_list");
      return { status: 201, body: fixture("feedback_saved") };
    }
    return { status: 200, body: feedbackBody };
  }
  if (path.endsWith("/feedback/test-questions.csv")) return { status: 200, body: csvText };
  if ((m = path.match(/^\/indexes\/([^/]+)\/ask$/))) return { status: 200, body: answer };
  if ((m = path.match(/^\/indexes\/([^/]+)\/update$/))) return { status: 202, body: fixture("update_started") };
  if ((m = path.match(/^\/indexes\/([^/]+)\/compare\/([^/]+)$/))) return { status: 200, body: fixture("comparison") };
  if ((m = path.match(/^\/indexes\/([^/]+)$/))) {
    if (method === "DELETE") {
      return m[1] === ids.champion ? { status: 409, body: fixture("delete_refused") } : { status: 204, body: null };
    }
    return details[m[1]] ? { status: 200, body: details[m[1]] } : null;
  }
  return null;
};

const { w, calls } = installPage(server, { hash: `#/generative/assistant/${uc.id}/${ids.first}` });
const forms = [];
{
  const inner = w.fetch;
  w.fetch = async (input, init = {}) => {
    if (init && init.body instanceof w.FormData) forms.push({ url: String(input), form: init.body });
    return inner(input, init);
  };
}
globalThis.FormData = w.FormData;
const saved = [];
URL.createObjectURL = (blob) => {
  saved.push(blob);
  return "blob:saved";
};
URL.revokeObjectURL = () => {};

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

async function open(indexId) {
  controller.stop();
  await controller.refreshIndexes();
  await controller.loadIndex(indexId);
  paint();
}

async function ask() {
  $("#g-question").value = answer.question;
  $("#g-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  $("#g-ask").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $$(".gcite").length > 0, 2000, "the answer's citations");
}

const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : "");

test("the grade card shows every figure the grading measured, and a dash for one it did not", async () => {
  await open(ids.first);
  const grade = detail.grade;
  const metrics = $$(".gmetric").map((el) => `${el.querySelector(".k").textContent} ${el.querySelector(".v").textContent}`);
  const pct = (v) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);
  assert.deepEqual(metrics, [
    `Found the right document ${pct(grade.retrieval_hit_rate)}`,
    `Stayed with the documents ${pct(grade.mean_faithfulness)}`,
    `Agreed with the accepted answer ${pct(grade.mean_correctness)}`,
    `Refused or answered rightly ${grade.refusal_correct} of ${grade.questions}`,
  ]);
  details[ids.first] = { ...detail, grade: { ...grade, mean_correctness: null } };
  await controller.loadIndex(ids.first);
  paint();
  assert.equal($$(".gmetric .v")[2].textContent, "—", "a mean that was never measured is a dash, never 0%");
  details[ids.first] = detail;
  await controller.loadIndex(ids.first);
  paint();
});

test("a citation opens its passage with the quoted words marked, and steps to the next passage", async () => {
  await ask();
  const first = $(".gcite");
  assert.equal(first.getAttribute("role"), "button");
  assert.ok(text(".gcite").startsWith(`${answer.citations[0].document} · ${answer.citations[0].section}`));
  assert.ok(!text(".gcite figcaption").includes("page"), "an unpaged document shows no page");
  first.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await until(() => $("#g-passage .gpassage-text"), 2000, "the passage panel");
  assert.ok(calls.some((c) => c.path === `/indexes/${ids.first}/chunks/${encodeURIComponent(chunk.chunk_id)}`));
  assert.equal(text("#g-passage-title"), `${chunk.document} · ${chunk.section}`);
  assert.equal($(".gpassage-text").textContent, chunk.text, "the passage is shown exactly as indexed");
  const quote = answer.citations[0].quote;
  if (quote) {
    const mark = $(".gpassage-text mark");
    assert.ok(mark, "the quote is marked");
    assert.equal(mark.textContent.replace(/\s+/g, " ").toLowerCase(), quote.replace(/\s+/g, " ").toLowerCase());
  }
  const direction = chunk.next_chunk_id ? "next" : "previous";
  $(`[data-passage-go="${direction}"]`).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await until(() => $(".gpassage-text") && $(".gpassage-text").textContent === neighbour.text, 2000, "the neighbour");
  assert.equal($(".gpassage-text mark"), null, "another passage carries no mark: the quote is not in it");
  $("#g-passage-close").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  assert.equal($("#g-passage"), null);
});

test("a citation with a page says so, and hostile passage text is shown as text", async () => {
  const paged = { ...answer.citations[0], page: 3 };
  const { citationCard, highlightQuote } = await import("../../../../../ui/modules/generative/gdom.js");
  assert.ok(citationCard(paged).includes("· page 3"));
  const html = highlightQuote('<img src=x onerror="window.pwned=1"> the fee is Rs 100', "the FEE is");
  assert.ok(html.startsWith("&lt;img"), html);
  assert.ok(html.includes("<mark>the fee is</mark>"), html);
  assert.equal(highlightQuote("nothing to see", "absent words"), "nothing to see");
});

test("thumbs down with a comment is sent once, says what was masked, and fills the feedback card", async () => {
  assert.ok(text("#g-feedback").includes("0 not helpful"));
  $('[data-fb$=":down"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  const area = $("[data-fb-comment]");
  assert.ok(area, "a comment box appears after a rating");
  area.value = "Ask asha.verma@example.com, she knows the fee.";
  area.dispatchEvent(new w.Event("input", { bubbles: true }));
  $("[data-fb-send]").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await until(() => $("[data-fb-done]"), 2000, "the thanks line");
  const post = calls.find((c) => c.method === "POST" && c.path.endsWith("/feedback"));
  assert.equal(post.body.rating, "down");
  assert.equal(post.body.question, answer.question);
  assert.deepEqual(post.body.cited_chunk_ids, answer.citations.map((c) => c.chunk_id));
  assert.ok(text("[data-fb-done]").includes("Contact details in it were masked."));
  assert.ok(text("#g-feedback").includes("1 not helpful"));
  assert.ok(!text("#g-feedback").includes("asha.verma@example.com"), "the card shows the stored, masked text");
});

test("Add to test questions saves the server's file and invents nothing", async () => {
  $("#g-export").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await until(() => saved.length === 1, 2000, "the saved file");
  assert.equal(await saved[0].text(), csvText);
  assert.ok(csvText.split("\n")[1].endsWith(",,,"), "the accepted answer is left for a person");
});

test("the best version cannot be deleted, and says why", async () => {
  if (ids.champion !== ids.first) await open(ids.champion);
  assert.equal($("#g-delete").disabled, true);
  assert.ok(text("#g-manage").includes("This is the best assistant, so it is kept."));
});

test("Update documents posts the removal and opens the new version with what it reused", async () => {
  await open(ids.first);
  const box = $(`[data-remove-doc="${ids.removed}"]`);
  assert.ok(box, "every document can be picked for removal");
  assert.equal($("#g-update-go").disabled, true, "nothing chosen, nothing to build");
  box.checked = true;
  box.dispatchEvent(new w.Event("change", { bubbles: true }));
  assert.equal($("#g-update-go").disabled, false);
  $("#g-update").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => controller.state.indexId === ids.second, 2000, "the new version");
  controller.stop();
  await settle();
  const sent = forms.find((f) => f.url.endsWith(`/indexes/${ids.first}/update`));
  assert.deepEqual(sent.form.getAll("remove"), [ids.removed]);
  paint();
  const reuse = updated.update;
  assert.ok(text("#g-reuse").includes(`${reuse.reused.length} documents (${reuse.chunks_reused} passages)`));
  assert.ok(text("#g-index").includes(`removed ${ids.removed}`));
});

test("a version that is not the best can be deleted after a confirmation", async () => {
  const loser = ids.champion === ids.first ? ids.second : ids.first;
  await open(loser);
  w.confirm = () => false;
  $("#g-delete").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.ok(!calls.some((c) => c.method === "DELETE"), "cancelled means nothing is sent");
  w.confirm = () => true;
  $("#g-delete").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await until(() => calls.some((c) => c.method === "DELETE"), 2000, "the delete");
  await until(() => w.location.hash === `#/generative/assistant/${uc.id}`, 2000, "back to the assistant");
});

test("two graded versions compare side by side, with the questions that changed verdict", async () => {
  await open(ids.first);
  const select = $("#g-compare-with");
  assert.ok(select, "another graded version is offered");
  select.value = ids.second;
  select.dispatchEvent(new w.Event("change", { bubbles: true }));
  $("#g-compare-go").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await until(() => $("#g-compare-table"), 2000, "the comparison");
  const comparison = fixture("comparison");
  const rows = $$("#g-compare-table tbody tr").map((tr) => [...tr.cells].map((td) => td.textContent));
  assert.equal(rows[0][0], "Passed");
  assert.ok(rows[0][1].startsWith(`${(comparison.left.grade.pass_rate * 100).toFixed(0)}%`));
  assert.ok(rows[0][2].startsWith(`${(comparison.right.grade.pass_rate * 100).toFixed(0)}%`));
  const changed = $$("#g-compare-changed tbody tr");
  assert.equal(changed.length, comparison.changed.length);
  if (!comparison.changed.length) assert.ok(text("#g-compare").includes("No question changed its verdict."));
});
