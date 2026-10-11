/* Plan J M103's old audit page (`#/audit`, ui/modules/decide/index.js) in jsdom: choosing a file draws the form
   again, and what the person set before must survive that, an unticked box included. FormData leaves an unticked
   box out, so restoring from it alone would re-tick "More of the outcome is better" and the audit would be sent
   with the direction reversed. `POST /uploads` and `POST /campaigns/audit` are answered here (the rest by the
   fake's captured answers) and the audit request is recorded. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const COLUMNS = {
  assignment: ["customer_id", "group"],
  outcomes: ["customer_id", "converted", "treatment_date"],
  contact: ["customer_id", "contacted"],
};
const queue = [];
const sent = [];

const world = { campaigns: fixture("campaigns_empty"), views: {}, plans: {}, previews: {}, ratedPreviews: {} };
const { w } = await installWholePage({ hash: "#/audit", world });
globalThis.FormData = w.FormData; // a form is read in the page's own DOM, so its FormData is the page's
const inner = w.fetch;
w.fetch = async (input, init = {}) => {
  const target = new URL(typeof input === "string" ? input : input.url, w.location.href);
  const method = (init.method || "GET").toUpperCase();
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  if (method === "POST" && target.pathname === "/uploads") {
    const kind = queue.shift();
    return json(201, {
      upload_id: `u_${kind}`,
      profile: { file_name: `${kind}.csv`, row_count: 100, columns: COLUMNS[kind].map((name) => ({ name })) },
    });
  }
  if (method === "POST" && target.pathname === "/campaigns/audit") {
    sent.push(JSON.parse(init.body));
    return json(201, { campaign: { campaign_id: "c_audited_1" } });
  }
  return inner(input, init);
};

async function choose(kind, columnField) {
  queue.push(kind);
  const input = $(`[data-audit-file="${kind}"]`);
  assert.ok(input, `the ${kind} file control`);
  Object.defineProperty(input, "files", { configurable: true, value: [new w.File(["id\n1\n"], `${kind}.csv`, { type: "text/csv" })] });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await until(() => $(`[name="${columnField}"]`), 3000, `the form offering ${kind}'s columns`);
  await settle(2);
}

function fill(values) {
  for (const [name, value] of Object.entries(values)) {
    const inputs = $$(`[name="${name}"]`);
    assert.ok(inputs.length, `no field named ${name}`);
    for (const input of inputs) {
      if (input.type === "radio") input.checked = input.value === value;
      else input.value = value;
    }
  }
}

test("an unticked 'More of the outcome is better' stays unticked when a file is chosen afterwards, and is sent as false", async () => {
  await until(() => $("[data-audit-form]"), 4000, "the audit form");
  await choose("assignment", "arm_column");
  await choose("outcomes", "outcome_column");
  const box = () => $('[name="outcome_is_good"]');
  assert.equal(box().checked, true, "ticked until the person changes it");
  box().checked = false;
  fill({ treatment_start: "2026-04-01" });
  await choose("contact", "contacted_column"); // draws the form again
  assert.equal(box().checked, false, "still unticked after the form was drawn again");
  assert.equal($('[name="treatment_start"]').value, "2026-04-01", "and what was typed stays typed");
  fill({
    assignment_key: "customer_id",
    arm_column: "group",
    assignment_basis: "random",
    outcomes_key: "customer_id",
    outcome_column: "converted",
    outcome_window_days: "30",
    contact_key: "customer_id",
    contacted_column: "contacted",
  });
  $("[data-audit-form]").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => sent.length === 1, 3000, "the audit request");
  assert.equal(sent[0].outcome_is_good, false, "the audit is sent in the direction the person chose");
});

test("a ticked box stays ticked when a file is chosen afterwards", async () => {
  w.location.hash = "#/";
  await settle(3);
  w.location.hash = "#/audit";
  await until(() => $("[data-audit-form]"), 4000, "the audit form");
  await settle(3);
  await choose("assignment", "arm_column");
  await choose("outcomes", "outcome_column");
  assert.equal($('[name="outcome_is_good"]').checked, true);
  await choose("contact", "contacted_column");
  assert.equal($('[name="outcome_is_good"]').checked, true, "the default is kept");
});
