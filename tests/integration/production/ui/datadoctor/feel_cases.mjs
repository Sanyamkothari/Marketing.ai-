/* The cases of feel.test.mjs: the chat-first screen in plain words (a safety stop, column names), on a phone (the data
   panel is one line), and answering at once (progress text, the clicked control busy, nothing moves). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage, settle, until } from "../harness.mjs";
import { CASE, makeWorld, screen, serverFor } from "./common.mjs";
import { visibleText } from "../glossary/collect.mjs";
import { violations, describe } from "../glossary/check.mjs";

let world = makeWorld();
let hold = null; // { tail, promise }: the answer to a POST of that kind waits until `release()`
installPage(
  async (request) => {
    if (hold && request.method === "POST" && (request.path === `/cases/${CASE}/${hold.tail}` || request.path === hold.tail)) await hold.promise;
    return serverFor(world)(request);
  },
  { hash: "#/data/check" },
);
const { mountDataDoctor, _resetDataDoctorForTests } = await import("../../../../../ui/modules/datadoctor/screen.js");
const { CSS } = await import("../../../../../ui/modules/datadoctor/styles.js");
const { forgetAiServiceStatus } = await import("../../../../../ui/availability.js");

const app = $("#app");

async function show(at, options = {}) {
  world = makeWorld({ at, ...options });
  world.patch = options.patch || null;
  hold = null;
  _resetDataDoctorForTests();
  forgetAiServiceStatus();
  await settle(2);
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data", ...(options.host || {}) });
  await until(() => $("[data-dds-verdict]") || $("[data-dds-start]"), 3000, `the ${at} screen`);
  await settle(3);
  return screen(at);
}

/** Make the next POST of this kind wait; `release()` lets the server answer. */
function holdPost(tail) {
  let release = () => {};
  hold = { tail, promise: new Promise((resolve) => (release = resolve)) };
  return release;
}
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
const posts = (tail) => world.posted.filter((p) => p.path === `/cases/${CASE}/${tail}`);
const tick = (el) => {
  el.checked = true;
  el.dispatchEvent(new window.Event("change", { bubbles: true }));
};

// --- (1) a safety stop in plain words ---------------------------------------------------------------------------

const STOP = {
  key: "k-loss",
  text: "This step leaves out 87 of 1,000 customers (8.7%), more than the 5% we allow without asking. Most of them were not a yes.",
  detail: "Most from step 5, step 4. By outcome, 0: 76, 1: 11.",
  item_id: null,
};

test("a safety stop is one plain sentence; the step numbers and the split by outcome are behind See details", async () => {
  await show("approve_class", {
    patch: (a) => {
      const ready = a.view.conversation.find((c) => c.type === "ready");
      if (ready && ready.stage === "approve") ready.stops = [STOP];
      return a;
    },
  });
  const row = $("[data-dds-stop-row]");
  assert.match(text(row), /^This step leaves out 87 of 1,000 customers \(8\.7%\), more than the 5% we allow without asking\. Most of them were not a yes\./);
  assert.ok(!/step 5|By outcome|0: 76/.test(text(row)), "no expert words in the sentence the person ticks");
  const more = $("[data-dds-stop-block] details");
  assert.equal(more.querySelector("summary").textContent.trim(), "See details");
  assert.equal(more.open, false, "folded until asked for");
  assert.match(more.querySelector("[data-dds-stop-detail]").textContent, /Most from step 5, step 4\. By outcome, 0: 76, 1: 11\./);
  more.open = true;
  tick($('input[data-dds-stop="k-loss"]'));
  assert.equal(more.open, true, "ticking does not fold it");
  const v = violations(visibleText(app));
  assert.ok(v.ok, describe("a safety stop", v));
});

// --- (2) column names in words ----------------------------------------------------------------------------------

test("a column is said in words; its exact name is small and only where the person must recognise it", async () => {
  const s = await show("approve_class", {
    patch: (a) => {
      const fit = a.view.conversation.find((c) => c.type === "fit");
      fit.sentence = "customers_oct.csv has 1,200 customers; 602 of them are a yes in marketing_opt_in.";
      a.case.questions.find((q) => q.code === "KEY_AMBIGUOUS").column = "customer_id";
      return a;
    },
  });
  const fit = $("[data-dds-fit]");
  assert.match(text(fit), /are a yes in marketing opt in\./);
  assert.ok(!text(fit).includes("marketing_opt_in"), "not as the file spells it");
  assert.ok(fit.querySelector("[data-user-text]"), "still the person's own text for the glossary check");
  assert.match(text($("[data-dds-fit] [data-user-text]:last-of-type")), /^marketing opt in$/);
  const change = $(`[data-dds-change-card="${s.view.conversation.find((c) => c.type === "change").item_id}"]`);
  assert.match(text(change), /“snapshot date” is the date each row describes\./);
  assert.match(text(change.querySelector(".dds-item-sub")), /In your file: snapshot_date/, "the exact name, small, on the card that changes it");
  const key = $('[data-dds-question][data-dds-code="KEY_AMBIGUOUS"]');
  assert.match(text(key.querySelector("[data-dds-column-note]")), /^In your file: customer_id$/);
  const plain = $('[data-dds-question][data-dds-code="IMPOSSIBLE_AGE"]');
  assert.ok(!plain.querySelector("[data-dds-column-note]"), "a plain name needs no note");
  // The exact sentence a person agrees to is never reworded.
  for (const yes of s.view.conversation.find((c) => c.type === "ready").confirmations) {
    const row = $(`[data-dds-confirm-row="${yes.item_id}"]`) || $(`[data-dds-agreed="${yes.item_id}"]`);
    assert.ok(row && text(row).includes(yes.sentence), `the sentence agreed to is word for word: ${yes.sentence}`);
  }
  const v = violations(visibleText(app));
  assert.ok(v.ok, describe("column names in words", v));
});

// --- (3) a phone ------------------------------------------------------------------------------------------------

test("on a phone the data panel is one line, Your data · 4,000 customers · Show, until it is opened", async () => {
  await show("approve_class", {
    patch: (a) => {
      const look = a.view.conversation.find((c) => c.type === "first_look");
      look.files = [{ upload_id: "u1", name: "customers.csv", rows: 4000, columns: 12, kind: "a customer list" }, { upload_id: "u2", name: "orders.csv", rows: 6029, columns: 5, kind: "a log of orders" }];
      return a;
    },
  });
  const panel = $("[data-dds-panel]");
  const toggle = panel.querySelector("[data-dds-panel-toggle]");
  assert.equal(text(toggle), "Your data · 4,000 customers · Show");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.ok(!panel.classList.contains("is-open"));
  assert.ok(CSS.includes(".ddc-panel:not(.is-open) .ddc-panel-body{display:none}"), "closed: only the line shows on a phone");
  assert.ok(CSS.includes(".ddc-panel-toggle{display:none}"), "on a wide screen there is no toggle: the panel is always open");
  assert.ok(panel.querySelector("#ddc-panel-body [data-dds-checklist-panel]") && panel.querySelector("#ddc-panel-body [data-dds-data]"), "the checklist and the data are in the folded part");
  toggle.click();
  assert.equal(text(toggle), "Your data · 4,000 customers · Hide");
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.ok(panel.classList.contains("is-open"));
  $("[data-dds-approve-start]"); // a repaint keeps it open
  $('[data-dds-conversation] [data-dds-undo]').click();
  await until(() => posts("decisions").length === 1, 2000, "the click");
  await until(() => !$("[data-dds-progress]"), 2000, "the answer");
  assert.ok($("[data-dds-panel]").classList.contains("is-open"), "still open after the screen is drawn again");
  $("[data-dds-panel-toggle]").click();
  assert.equal(text($("[data-dds-panel-toggle]")), "Your data · 4,000 customers · Show");
});

test("with no AI service the note under the chat is one short line, and on a phone the disabled prompts are left out", async () => {
  await show("question");
  const note = $("[data-dds-composer] .dds-nochat");
  assert.equal(note.children.length, 1, "one line, no extra paragraphs or buttons");
  assert.equal(text(note), "Chat needs the AI service. Connect it in Settings.");
  assert.equal(note.querySelector("a[data-ai-settings-link]").getAttribute("href"), "#/settings/ai");
  assert.ok($("[data-dds-composer]").classList.contains("is-off"));
  assert.ok(CSS.includes(".ddc-composer.is-off .ddc-prompts{display:none}"));
});

// --- (4) feedback -----------------------------------------------------------------------------------------------

test("a chip answer says at once what is happening, the clicked chip is busy and everything waits, then it is over", async () => {
  const s = await show("question");
  const q = s.case.questions.find((x) => x.code === "UNKNOWN_BOOLEAN");
  const children = () => $("[data-dds-conversation]").children.length;
  const before = children();
  world.next.answers = "question";
  const release = holdPost("answers");
  const chip = $(`[data-dds-question="${q.question_id}"] [data-dds-answer]`);
  const id = chip.getAttribute("data-dds-answer");
  chip.click();
  await until(() => $("[data-dds-progress]"), 1000, "the progress text");
  assert.equal(text($("[data-dds-progress]")), "Applying your answer to 1,200 rows…");
  assert.equal($("[data-dds-progress]").getAttribute("role"), "status");
  const clicked = $(`[data-dds-question="${q.question_id}"] [data-dds-answer="${id}"]`);
  assert.equal(clicked.getAttribute("aria-busy"), "true", "the clicked chip shows it is working");
  assert.equal(clicked.disabled, true);
  assert.equal($$("[data-dds-answer]").filter((b) => b !== clicked && b.getAttribute("aria-busy") === "true").length, 0, "only that one");
  assert.ok($$("[data-dds-answer], [data-dds-goal], [data-dds-undo], [data-dds-use]").every((b) => b.disabled), "nothing else can be clicked while the server answers");
  assert.equal(children(), before, "no card was added or moved: the line floats over the page");
  assert.ok(CSS.includes(".ddc-progress{position:fixed"), "the progress line floats at the foot of the window, so it is seen wherever the click was and nothing jumps");
  release();
  await until(() => !$("[data-dds-progress]"), 3000, "the answer");
  assert.equal(posts("answers").length, 1, "one click, one answer sent");
  assert.ok($$("[data-dds-answer]").some((b) => !b.disabled), "the chips work again");
});

test("choosing a goal, Undo and Approve each say what is happening and mark the clicked control", async () => {
  const s = await show("approve_class");
  const change = s.view.conversation.find((c) => c.type === "change");
  let release = holdPost("decisions");
  $(`[data-dds-change-card="${change.item_id}"] [data-dds-undo]`).click();
  await until(() => $("[data-dds-progress]"), 1000, "the progress text");
  assert.equal(text($("[data-dds-progress]")), "Updating the plan on 1,200 rows…");
  assert.equal($(`[data-dds-change-card="${change.item_id}"] [data-dds-undo]`).getAttribute("aria-busy"), "true");
  release();
  await until(() => !$("[data-dds-progress]"), 3000, "the answer");

  const ready = s.view.conversation.find((c) => c.type === "ready");
  // A required yes is said once as a line and given by the one click (no box to tick); an optional one is a box.
  for (const yes of ready.confirmations.filter((c) => c.required)) {
    const box = $(`input[data-dds-confirm="${yes.item_id}"]`);
    if (box) tick(box);
  }
  release = holdPost("confirmations");
  $("[data-dds-approve-start]").click();
  await until(() => $("[data-dds-progress]"), 1000, "the progress text");
  assert.equal(text($("[data-dds-progress]")), "Checking everything before we start…");
  assert.equal($("[data-dds-approve-start]").getAttribute("aria-busy"), "true", "the one approval shows it is working");
  assert.equal($("[data-dds-approve-start]").disabled, true);
  release();
  await until(() => !$("[data-dds-progress]"), 4000, "the answer");
});

test("files just chosen: the chat opens at once with Reading your files…, and the screen replaces it when they are read", async () => {
  world = makeWorld({ at: "question" });
  world.cases = { cases: [] };
  hold = null;
  _resetDataDoctorForTests();
  forgetAiServiceStatus();
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  const release = holdPost("/uploads");
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data", files: [new File(["a,b\n1,2\n"], "customers.csv"), new File(["a,b\n1,2\n"], "orders.csv")] });
  await until(() => $("[data-dds-reading]"), 2000, "the reading line");
  assert.match(text($("[data-dds-reading] [data-dds-progress]")), /^Reading your files… customers\.csv and 1 more$/);
  assert.ok(!$("#dds-file") && !$("[data-dds-start]"), "no second way to choose a file while they are read");
  release();
  await until(() => $("[data-dds-verdict]"), 4000, "the check");
  assert.ok(!$("[data-dds-reading]"), "the line is gone once the check is drawn");
  assert.equal(world.uploads, 2);
});
