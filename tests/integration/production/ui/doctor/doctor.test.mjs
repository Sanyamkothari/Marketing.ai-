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
