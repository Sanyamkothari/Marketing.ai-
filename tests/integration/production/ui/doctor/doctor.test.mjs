/* Data Doctor's page (ui/modules/doctor/index.js, Plan L M155, M157) in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/test_doctor_ui.py): how the files fit together, and the
   outcome from events agreed as a sentence, word for word. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, until } from "../harness.mjs";

const diagnosis = fixture("diagnosis");
const agreement = fixture("agreement");
const uploads = fixture("uploads");
const sent = [];

const server = (request) => {
  if (request.method === "POST" && request.path === "/doctor/outcome") {
    sent.push(request.body);
    return { status: 201, body: agreement };
  }
  return null;
};

installPage(server, { hash: "#/doctor" });
const { showDiagnosis } = await import("../../../../../ui/modules/doctor/index.js");
const { resolveRoute } = await import("../../../../../ui/modules/router.js");
await resolveRoute(["doctor"]).render($("#app"), ["doctor"]);
showDiagnosis(structuredClone(diagnosis), uploads);
await settle();

test("how the files fit: a log whose IDs need a fix, and two months of one report", () => {
  const fit = $("[data-dd-fit]");
  assert.ok(fit, "the card is drawn");
  const tickets = diagnosis.links.find((link) => link.file_name === "tickets.csv");
  const row = $(`[data-dd-link="${tickets.upload_id}"]`);
  assert.match(row.textContent, /after the fix/);
  assert.ok(row.textContent.includes(tickets.summary));
  const stack = $$("[data-dd-stack]").find((el) => el.textContent.includes("bills_2025_04.csv"));
  assert.ok(stack, "the monthly bills are one report");
  assert.match(stack.textContent, /renamed|new:|missing:/);
});

test("the outcome is offered as a sentence with counted choices, the default picked", () => {
  const offer = diagnosis.use_cases.find((item) => item.use_case_id === "rca").outcome_offer;
  const box = $('[data-dd-outcome="rca"]');
  assert.ok(box.textContent.includes(offer.question));
  const radios = $$('[data-dd-pick="rca"]');
  assert.equal(radios.length, offer.choices.length);
  const fallback = offer.choices.find((c) => c.default);
  assert.equal(radios.find((r) => r.checked).value, String(fallback.days));
  for (const choice of offer.choices) assert.ok(box.textContent.includes(choice.sentence));
});

test("nothing is sent until the person presses the button; then the chosen sentence word for word", async () => {
  const offer = diagnosis.use_cases.find((item) => item.use_case_id === "rca").outcome_offer;
  assert.equal(sent.length, 0);
  const other = offer.choices.find((c) => !c.default && c.customers);
  const radio = $$('[data-dd-pick="rca"]').find((r) => r.value === String(other.days));
  radio.checked = true;
  radio.dispatchEvent(new window.Event("change"));
  await settle();
  $('[data-dd-agree="rca"]').click();
  await until(() => $('[data-dd-agreed="rca"]'), 2000, "the agreement");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].days, other.days);
  assert.equal(sent[0].sentence, other.sentence);
  assert.equal(sent[0].agreed, true);
  assert.equal(sent[0].event_upload_id, offer.event_upload_id);
  assert.ok($('[data-dd-agreed="rca"]').textContent.includes(agreement.next_steps[0]));
});

test("file names and sentences are text, never markup", async () => {
  const hostile = structuredClone(diagnosis);
  hostile.links[0].file_name = `<img src=x onerror="window.__pwned=1">`;
  hostile.use_cases.find((item) => item.use_case_id === "rca").outcome_offer.question = "<b>When?</b>";
  showDiagnosis(hostile, uploads);
  await settle();
  assert.equal($$("[data-dd-fit] img").length, 0);
  assert.equal($$('[data-dd-outcome="rca"] legend b').length, 0);
  assert.equal(window.__pwned, undefined);
});

// DEC-1596: what the file already says, counted with no AI, always labelled.
const firstLook = fixture("first_look");
const LABEL = "Describes your file. Not a cause and not a prediction.";

test("a ready use case shows its file's rate by the most informative columns, labelled", async () => {
  showDiagnosis(structuredClone(firstLook), []);
  await settle();
  const item = firstLook.use_cases.find((u) => u.use_case_id === "targeted-advertisement");
  const box = $('[data-dd-usecase="targeted-advertisement"] [data-dd-look="outcome"]');
  assert.ok(box, "the first look is drawn under the use case");
  assert.equal(box.querySelector("[data-dd-look-label]").textContent, LABEL);
  assert.ok(box.textContent.includes(item.first_look.summary));
  assert.ok(item.first_look.columns.length > 0);
  for (const column of item.first_look.columns) {
    const col = [...box.querySelectorAll("[data-dd-look-col]")].find((el) => el.dataset.ddLookCol === column.column);
    assert.ok(col, `column ${column.column} is drawn`);
    assert.equal(col.querySelectorAll("[data-dd-look-group]").length, column.groups.length);
    for (const group of column.groups) assert.ok(col.textContent.includes(group.group));
  }
  if (item.first_look.left_out.length) assert.ok(box.querySelector("[data-dd-look-left]"));
});

test("a log with no outcome shows when its customers last appear, labelled", async () => {
  showDiagnosis(structuredClone(diagnosis), uploads);
  await settle();
  const file = diagnosis.files.find((f) => f.file_name === "recharges.csv");
  const box = $(`[data-dd-file="${file.upload_id}"] [data-dd-look="recency"]`);
  assert.ok(box, "the recency picture is drawn on the file");
  assert.equal(box.querySelector("[data-dd-look-label]").textContent, LABEL);
  const look = file.first_look;
  const groups = look.recency.groups.length + (look.frequency ? look.frequency.groups.length : 0);
  assert.equal(box.querySelectorAll("[data-dd-look-group]").length, groups);
  // A use case that is not ready shows no first look.
  for (const item of diagnosis.use_cases.filter((u) => u.status !== "ready" && u.status !== "check")) {
    assert.equal($$(`[data-dd-usecase="${item.use_case_id}"] [data-dd-look]`).length, 0);
  }
});

test("first-look groups and sentences are text, never markup", async () => {
  const hostile = structuredClone(firstLook);
  const look = hostile.use_cases.find((u) => u.use_case_id === "targeted-advertisement").first_look;
  look.columns[0].groups[0].group = `<img src=x onerror="window.__pwned2=1">`;
  look.columns[0].summary = "<b>bold</b>";
  showDiagnosis(hostile, []);
  await settle();
  assert.equal($$("[data-dd-look] img").length, 0);
  assert.equal($$("[data-dd-look] b").length, 0);
  assert.equal(window.__pwned2, undefined);
});
