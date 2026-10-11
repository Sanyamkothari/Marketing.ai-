/* Plan M, M178 (DEC-1608): "Check a past campaign" asks the server exactly what the old audit form (`#/audit`) asks.

   The same files and the same choices are put through both forms in jsdom, with a fake API that records what
   `POST /campaigns/audit` and `POST /campaigns/programme` receive. The two requests must be identical, field for
   field, and each form must send one request per click (the old page's handlers listen on the whole document, so a
   shared hook would send two). Because the server's answer is a function of the request, an identical request
   gets the identical result; tests/integration/test_reports_ui.py proves that last step against the REAL API by
   posting the old and the new bodies written here ($RP_OUT) and comparing the answers.

   Environment:
     RP_OUT=<dir>      write each captured request to <dir>/<name>.json
     RP_UPLOADS=<file> answer POST /uploads with these real answers (a JSON object keyed assignment, outcomes,
                       contact, programme) instead of made-up ones */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { $, $$, settle, until as waitUntil } from "../harness.mjs";
import { chooseFile, installReports, open, uploaded } from "./fake.mjs";

const OUT = process.env.RP_OUT || "";
const REAL = process.env.RP_UPLOADS ? JSON.parse(fs.readFileSync(process.env.RP_UPLOADS, "utf8")) : null;

const COLUMNS = {
  assignment: ["customer_id", "group", "age", "region"],
  outcomes: ["customer_id", "converted", "treatment_date"],
  contact: ["customer_id", "contacted"],
  programme: ["customer_id", "converted"],
};

const queue = [];
const sent = { audit: [], programme: [] };
let uploads = 0;

const { w, overlay } = await installReports({
  hash: "#/audit",
  overlay: {
    "POST /uploads": () => {
      uploads += 1;
      const kind = queue.shift();
      if (REAL) return { status: 201, body: REAL[kind] };
      return uploaded(`u_${kind}`, `${kind}.csv`, COLUMNS[kind]);
    },
    "POST /campaigns/audit": (request) => {
      sent.audit.push(request.body);
      return { status: 201, body: { campaign: { campaign_id: "c_checked_1" } } };
    },
    "POST /campaigns/programme": (request) => {
      sent.programme.push(request.body);
      return { status: 201, body: { campaign: { campaign_id: "c_programme_1" } } };
    },
  },
});

/** Put values into the fields named, as a person would: selects and inputs by value, radios and checkboxes by state. */
function fill(values) {
  for (const [name, value] of Object.entries(values)) {
    const inputs = $$(`[name="${name}"]`);
    assert.ok(inputs.length, `no field named ${name}`);
    for (const input of inputs) {
      if (input.type === "radio") input.checked = input.value === value;
      else if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value;
    }
  }
}

const auditValues = {
  name: "Spring win-back",
  assignment_key: "customer_id",
  arm_column: "group",
  control_value: "0",
  treated_values: "",
  sent_date_column: "",
  intended_column: "",
  assignment_basis: "random",
  outcomes_key: "customer_id",
  outcome_column: "converted",
  outcome_kind: "binary",
  positive_label: "",
  outcome_date_column: "treatment_date",
  treatment_start: "2026-04-01",
  outcome_window_days: "30",
  outcome_is_good: true,
};
const contactValues = { contact_key: "customer_id", contacted_column: "contacted", unlisted_customers: "not_contacted" };
const programmeValues = {
  period_start: "2026-04-01",
  period_end: "2026-06-30",
  programme_key: "customer_id",
  programme_outcome: "converted",
  programme_kind: "binary",
  programme_positive: "",
  programme_name: "Q2",
};

/** Choose a file in the control `selector` and wait for the form to offer its columns. */
async function choose(selector, kind, columnField) {
  queue.push(kind);
  chooseFile(w, $(selector), `${kind}.csv`);
  const until = Date.now() + 3000;
  while (!$(`[name="${columnField}"]`) && Date.now() < until) await settle(2);
  assert.ok($(`[name="${columnField}"]`), `the form did not offer ${kind}'s columns`);
}

const submit = async (formSelector) => {
  const form = $(formSelector);
  assert.ok(form, formSelector);
  form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  const until = Date.now() + 3000;
  while (!/#\/campaigns\//.test(w.location.hash) && Date.now() < until) await settle(2);
};

async function throughOldForm({ withContact }) {
  await open(w, "#/audit", () => $("[data-audit-form]"));
  await choose('[data-audit-file="assignment"]', "assignment", "arm_column");
  await choose('[data-audit-file="outcomes"]', "outcomes", "outcome_column");
  if (withContact) await choose('[data-audit-file="contact"]', "contact", "contacted_column");
  fill({ ...auditValues, ...(withContact ? contactValues : {}) });
  await submit("[data-audit-form]");
  return w.location.hash;
}

async function throughNewForm({ withContact }) {
  await open(w, "#/reports/check", () => $("[data-rp-check-form]"));
  await choose('[data-rp-file="assignment"]', "assignment", "arm_column");
  await choose('[data-rp-file="outcomes"]', "outcomes", "outcome_column");
  if (withContact) await choose('[data-rp-file="contact"]', "contact", "contacted_column");
  fill({ ...auditValues, ...(withContact ? contactValues : {}) });
  await submit("[data-rp-check-form]");
  return w.location.hash;
}

const keep = (name, body) => {
  if (!OUT) return;
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, `${name}.json`), JSON.stringify(body, null, 1));
};

for (const withContact of [false, true]) {
  const label = withContact ? "with a contact file" : "without one";
  test(`the audit request through the new form equals the old form's, ${label}`, async () => {
    sent.audit.length = 0;
    uploads = 0;
    const oldHash = await throughOldForm({ withContact });
    const oldUploads = uploads;
    uploads = 0;
    const newHash = await throughNewForm({ withContact });
    assert.equal(sent.audit.length, 2, "one request from each form, and none doubled");
    assert.equal(oldUploads, withContact ? 3 : 2, "one upload per file");
    assert.equal(uploads, oldUploads, "the same files, uploaded the same way");
    const [old, now] = sent.audit;
    assert.deepEqual(now, old, "the same body, field for field");
    assert.equal(old.assignment_basis, "random");
    assert.equal(old.assignment.arm_column, "group");
    assert.equal(Boolean(old.contact), withContact);
    assert.equal(oldHash, "#/campaigns/c_checked_1");
    assert.equal(newHash, oldHash, "both land on the same campaign page, where the result is read");
    keep(withContact ? "audit_with_contact_old" : "audit_old", old);
    keep(withContact ? "audit_with_contact_new" : "audit_new", now);
  });
}

test("an unticked 'More of the outcome is better' stays unticked when a file is chosen afterwards, and is sent as false", async () => {
  sent.audit.length = 0;
  await open(w, "#/reports/check", () => $("[data-rp-check-form]"));
  await choose('[data-rp-file="assignment"]', "assignment", "arm_column");
  await choose('[data-rp-file="outcomes"]', "outcomes", "outcome_column");
  const box = () => $('[name="outcome_is_good"]');
  assert.equal(box().checked, true, "ticked until the person changes it");
  box().checked = false;
  await choose('[data-rp-file="contact"]', "contact", "contacted_column"); // draws the form again
  assert.equal(box().checked, false, "still unticked after the form was drawn again");
  const { outcome_is_good: _kept, ...others } = auditValues; // the box is left as the person left it
  fill({ ...others, ...contactValues });
  await submit("[data-rp-check-form]");
  assert.equal(sent.audit.length, 1);
  assert.equal(sent.audit[0].outcome_is_good, false, "the audit is sent in the direction the person chose");
});

test("a basis the person did not choose is not sent (never assumed), through either form", async () => {
  await open(w, "#/reports/check", () => $("[data-rp-check-form]"));
  await choose('[data-rp-file="assignment"]', "assignment", "arm_column");
  await choose('[data-rp-file="outcomes"]', "outcomes", "outcome_column");
  fill({ ...auditValues, assignment_basis: "" });
  assert.equal($("[data-rp-basis] input:checked"), null);
  assert.equal($('[name="assignment_basis"]').required, true, "the browser will not submit without a choice");
});

test("the whole-programme readout through the new tab equals the old page's", async () => {
  sent.programme.length = 0;
  await open(w, "#/audit", () => $("[data-programme-form]"));
  queue.push("programme");
  chooseFile(w, $('[data-audit-file="programme"]'), "programme.csv");
  await waitUntil(() => $('[name="programme_key"]'), 3000, "the programme's columns");
  fill(programmeValues);
  await submit("[data-programme-form]");
  await open(w, "#/reports/check", () => $("[data-rp-programme-form]"));
  queue.push("programme");
  chooseFile(w, $('[data-rp-file="programme"]'), "programme.csv");
  await waitUntil(() => $('[name="programme_key"]'), 3000, "the programme's columns");
  fill(programmeValues);
  await submit("[data-rp-programme-form]");
  assert.equal(sent.programme.length, 2);
  assert.deepEqual(sent.programme[1], sent.programme[0]);
  assert.equal(sent.programme[0].period.start, "2026-04-01");
  assert.equal(w.location.hash, "#/campaigns/c_programme_1");
  keep("programme_old", sent.programme[0]);
  keep("programme_new", sent.programme[1]);
});

test("an answer the server refuses stays on the form with its message, and the files stay chosen", async () => {
  const answer = overlay["POST /campaigns/audit"];
  overlay["POST /campaigns/audit"] = {
    status: 409,
    body: { detail: { code: "CAMPAIGN_NOT_MATURED", message: "Some outcomes are still being counted. Come back on 12 November." } },
  };
  await open(w, "#/reports/check", () => $("[data-rp-check-form]"));
  await choose('[data-rp-file="assignment"]', "assignment", "arm_column");
  await choose('[data-rp-file="outcomes"]', "outcomes", "outcome_column");
  fill(auditValues);
  $("[data-rp-check-form]").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  const until = Date.now() + 3000;
  while (!$("[data-rp-error]") && Date.now() < until) await settle(2);
  assert.match($("[data-rp-error]").textContent, /Come back on 12 November/);
  assert.equal(w.location.hash, "#/reports/check", "no navigation");
  assert.match($('[data-file-name="assignment"]').textContent, /assignment\.csv · [\d,]+ rows/, "the files stay chosen");
  assert.equal($('[name="treatment_start"]').value, "2026-04-01", "and what was typed stays typed");
  assert.equal($("[data-rp-submit]").disabled, false, "ready to try again");
  overlay["POST /campaigns/audit"] = answer;
});
