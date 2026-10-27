/* The cases of review.test.mjs: the chat opens, "Review everything first" opens the approval screen. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage, settle, until } from "../harness.mjs";
import { CASE, makeWorld, screen, serverFor } from "./common.mjs";

let world = makeWorld();
installPage((request) => serverFor(world)(request), { hash: "#/data/check" });
const { mountDataDoctor, _resetDataDoctorForTests } = await import("../../../../../ui/modules/datadoctor/screen.js");
const review = await import("../../../../../ui/modules/datadoctor/review.js");
const { forgetAiServiceStatus } = await import("../../../../../ui/availability.js");

const app = $("#app");

async function show(at, patch = null) {
  world = makeWorld({ at });
  world.patch = patch;
  _resetDataDoctorForTests();
  forgetAiServiceStatus();
  await settle(2);
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data" });
  await until(() => $("[data-dds-verdict]") || $("[data-dds-start]"), 3000, `the ${at} screen`);
  await settle(3);
  $("[data-dds-open-review]").click();
  await settle(2);
  assert.ok($("[data-rv]"), "the approval screen is open");
  return screen(at);
}

const posts = (tail) => world.posted.filter((p) => p.path === `/cases/${CASE}/${tail}`);
const approve = () => $("[data-rv-approve]");
const option = (id, label, extra = {}) => ({ id, label, effect: "", recommended: false, safe: false, value_kind: null, ...extra });
const base = (r) => ({ plan: [], decisions: [], problems: [], agreements: [], swaps: [], ...r, approve: { ...r.approve } });

test("everything answered: the plan, the decisions and ONE Approve that records the sentence read", async () => {
  const s = await show("approve_class", (a) => {
    a.view.review.decisions = [
      { kind: "outcome", title: "What counts as the outcome", why: "", current: "bought", decided_by: "person", question_id: null, item_id: "i-out", options: [], answer: null, answer_value: null, needs_yes: false, answered: true, blocking: false },
    ];
    return a;
  });
  const r = s.view.review;
  assert.equal($$("[data-rv-step]").length, r.plan.length, "one line per change");
  assert.equal($$("[data-rv-approve]").length, 1, "one Approve");
  assert.equal($("[data-rv-sentence]").textContent, r.approve.sentence, "the sentence, word for word");
  assert.equal(approve().disabled, false);
  world.next.approval = "ready";
  approve().click();
  await until(() => posts("approval").length === 1, 4000, "the approval");
  assert.equal(posts("approval")[0].body.sentence, r.approve.sentence);
});

test("an assumption needs a yes (or a change) before Approve is on; a typed answer shows the right input", async () => {
  await show("approve_class", (a) => {
    a.view.review.decisions = [
      {
        kind: "date", title: "Which date is the data as of?", why: "Both readings are valid.", current: "2011-12-09", decided_by: "rules",
        question_id: "q-date", item_id: null, answer: "latest", answer_value: null, needs_yes: true, answered: true, blocking: false,
        options: [option("latest", "The latest date in the file"), option("typed", "I will type a date", { value_kind: "date" }), option("n", "A number", { value_kind: "number" })],
      },
    ];
    return a;
  });
  assert.equal(approve().disabled, true, "the assumption is not said yes to");
  assert.match($("[data-rv-waiting]").textContent, /Say yes or change: Which date/);
  $("[data-rv-change]").click();
  await settle(1);
  assert.equal($('input[type="date"][data-rv-value="typed"]').getAttribute("type"), "date", "a date asks for a date");
  assert.equal($('input[type="number"][data-rv-value="n"]').getAttribute("type"), "number");
  const input = $('input[data-rv-value="typed"]');
  $('[data-rv-answer="typed"]').click();
  await settle(1);
  assert.equal(posts("answers").length, 0, "no typed value: nothing is sent");
  input.value = "2011-12-01";
  world.next.answers = "approve_class";
  $('[data-rv-answer="typed"]').click();
  await until(() => posts("answers").length === 1, 3000, "the answer");
  assert.deepEqual(
    { q: posts("answers")[0].body.question_id, o: posts("answers")[0].body.option_id, v: posts("answers")[0].body.value },
    { q: "q-date", o: "typed", v: "2011-12-01" },
  );
});

test("saying yes to an assumption turns Approve on", async () => {
  await show("approve_class", (a) => {
    a.view.review.decisions = [{ kind: "consent", title: "Who may be contacted", why: "", current: "marketing opt in", decided_by: "rules", question_id: null, item_id: "i-c", options: [], answer: null, answer_value: null, needs_yes: true, answered: true, blocking: false }];
    return a;
  });
  assert.equal(approve().disabled, true);
  $('[data-rv-yes="i-c"]').click();
  await settle(1);
  assert.equal(approve().disabled, false);
});

test("a hard stop is drawn apart with its reason and fix, and no yes; a stop needs yes or no, and no keeps Approve off", async () => {
  await show("approve_class", (a) => {
    a.view.review.problems = [
      { key: "a".repeat(16), text: "Nobody agreed to be contacted in 40 rows.", detail: null, hard: true, acknowledgeable: false, acknowledged: false, fix: "Change the plan or the file: Remove the 40 rows", fix_item_ids: [] },
      { key: "b".repeat(16), text: "The plan leaves out 30% of the rows.", detail: null, hard: false, acknowledgeable: true, acknowledged: false, fix: "Change the plan or the file", fix_item_ids: [] },
    ];
    a.view.review.approve.waiting = ["1 rule is not met."];
    a.view.review.approve.enabled = false;
    return a;
  });
  const hard = $(`[data-rv-problem="${"a".repeat(16)}"]`);
  assert.ok(hard.hasAttribute("data-rv-hard") && hard.classList.contains("is-hard"));
  assert.match(hard.querySelector("[data-rv-reason]").textContent, /No one can say yes/);
  assert.match(hard.querySelector("[data-rv-fix]").textContent, /What would fix it/);
  assert.equal(hard.querySelectorAll("[data-rv-stop-yes]").length, 0, "nobody can say yes to a hard stop");
  const stop = "b".repeat(16);
  $(`[data-rv-stop-no="${stop}"]`).click();
  await settle(1);
  assert.match($("[data-rv-waiting]").textContent, /You said no/);
  assert.equal(approve().disabled, true);
});

test("a swap shows the old step beside the new and asks which to use", async () => {
  await show("approve_class", (a) => {
    a.view.review.swaps = [{ new_item_id: "swap-1", old_item_id: "old-1", old_title: "Drop duplicates", old_counts: "1,200 rows in, 1,100 out", new_title: "Keep the latest copy", new_counts: "1,200 rows in, 1,150 out", by: "agent", state: "pending" }];
    a.view.review.approve.waiting = ["Choose between the old and the new step (1)."];
    return a;
  });
  const swap = $('[data-rv-swap="swap-1"]');
  assert.match(swap.querySelector("[data-rv-swap-old]").textContent, /Old.*Drop duplicates.*1,100 out/);
  assert.match(swap.querySelector("[data-rv-swap-new]").textContent, /New.*Keep the latest copy.*1,150 out/);
  assert.equal(approve().disabled, true);
  world.next.decisions = "approve_class";
  $('[data-rv-swap-keep="swap-1"]').click();
  await until(() => posts("decisions").length === 1, 3000, "the decision");
  assert.deepEqual({ id: posts("decisions")[0].body.item_id, accept: posts("decisions")[0].body.accept }, { id: "swap-1", accept: false });
});

test("waitingFor reads the server's reasons first, then the person's", () => {
  const r = base({ decisions: [{ title: "T", question_id: "q", answered: true, needs_yes: true }], problems: [{ key: "k", text: "X", hard: false, acknowledged: false }], approve: { stage: "approve", waiting: ["Choose what the campaign is for."], enabled: false } });
  const ctx = { yes: new Set(), nos: new Set(), stops: new Set() };
  assert.deepEqual(review.waitingFor(r, ctx), ["Choose what the campaign is for.", "Say yes or change: T", "Answer yes or no: X"]);
  r.approve.waiting = [];
  ctx.yes.add("q");
  ctx.stops.add("k");
  assert.equal(review.reviewAllowed(r, ctx), true);
  assert.equal(review.reviewAllowed({ ...r, approve: { ...r.approve, stage: "start" } }, ctx), false);
});
