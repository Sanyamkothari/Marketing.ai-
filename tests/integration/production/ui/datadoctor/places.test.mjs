/* Plan O M247 (DEC-1817): ONE Data Doctor screen opened from three places, on ONE case, in the real app (jsdom).

   The Data page with no goal (`#/data/check`) -> answer -> "Choose this goal" -> the "New campaign" wizard's
   Customers step on the same case, inside the card's mount points, with nothing asked twice -> the same case in the
   goal's setup (`#/uc/<goal>`, where Guided setup mounts). The fake API serves one case through the states the real
   visit gave (`common.mjs`). */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the Data Doctor places' jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  const { $, $$, installPage, settle, until } = await import("../harness.mjs");
  const { CASE, GOAL, makeWorld, screen, serverFor } = await import("./common.mjs");

  const world = makeWorld({ at: "question" });
  installPage(serverFor(world), { hash: "#/data/check" });
  await import("../../../../../ui/app.js");
  await import("../../../../../ui/modules/production/index.js");
  await import("../../../../../ui/modules/agent/index.js");
  await import("../../../../../ui/modules/decide/index.js");
  await import("../../../../../ui/modules/data/index.js");

  const go = async (hash) => {
    window.location.hash = hash;
    await settle(4);
  };
  const keyQuestion = screen("question").case.questions.find((q) => q.code === "KEY_AMBIGUOUS");
  const answersTo = (qid) => world.posted.filter((p) => p.path === `/cases/${CASE}/answers` && p.body.question_id === qid);

  test("the Data page: the screen is the Check my data tab, with no goal", async () => {
    await until(() => $('[data-dt-doctor-screen] [data-dds-place="data"] [data-dds-verdict]'), 4000, "the screen on the Data page");
    assert.equal($$("h1").length, 1, "the page keeps its one H1");
    assert.equal($("[data-dds-verdict]").dataset.ddsVerdict, "needs_answers");
    assert.ok($("[data-dds-goals]"), "with no goal: what this data can be used for");
    assert.ok(!$(".dt-check"), "today's description card is replaced by the screen itself");
  });

  test("answer on the Data page, then choose a goal: the wizard continues on the same case", async () => {
    world.next.answers = "approve";
    $(`[data-dds-question="${keyQuestion.question_id}"] [data-dds-answer="c0"]`).click();
    await until(() => $("[data-dds-approve-sentence]"), 3000, "the answer taken");
    assert.equal(answersTo(keyQuestion.question_id).length, 1);
    world.next.goal = "approve_class";
    $(`[data-dds-goal="${GOAL}"]`).click();
    await until(() => window.location.hash === `#/campaigns/new/customers/${GOAL}`, 3000, "the wizard");
    const goalPost = world.posted.find((p) => p.path === `/cases/${CASE}/goal`);
    assert.equal(goalPost.body.goal_id, GOAL);
    await until(() => $('[data-cw-customers] [data-cw-guided-main] [data-dds-place="wizard"]'), 5000, "the screen in the wizard");
    const here = $('[data-dds-place="wizard"]');
    assert.equal(here.dataset.ddsCase, CASE, "the same case");
    assert.ok($('[data-cw-customers] [data-cw-guided-main] [data-dds-composer]'), "the chat's box, in the card");
    assert.ok($('[data-cw-customers] [data-cw-guided-main] [data-dds-fit]'), "the goal was chosen first: whether the data fits it");
  });

  test("nothing is asked twice: the Data page's answer stands in the wizard", async () => {
    const card = $(`[data-dds-place="wizard"] [data-dds-question="${keyQuestion.question_id}"]`);
    assert.ok(card.querySelector("[data-dds-answered]"), "answered, not asked");
    const blocking = $$('[data-dds-place="wizard"] .dds-q.is-open').filter((q) => q.textContent.includes("Needed to go on"));
    assert.deepEqual(blocking, [], "no question waits again");
    assert.equal(answersTo(keyQuestion.question_id).length, 1, "answered once, in one place");
    assert.ok($('[data-dds-place="wizard"] [data-dds-confirm-row]'), "the goal's own yeses are in the one approval card");
    assert.equal($$('[data-dds-place="wizard"] [data-dds-approve-start]').length, 1, "and there is one approval");
  });

  test("a goal's setup opens the same case where Guided setup mounts", async () => {
    await go(`#/uc/${GOAL}`);
    await until(() => $('[data-dds-place="setup"] [data-dds-verdict]'), 6000, "the screen in the goal's setup");
    assert.equal($('[data-dds-place="setup"]').dataset.ddsCase, CASE);
    assert.ok(!$(".ag-chat"), "Guided setup itself is not drawn as well");
  });
}
