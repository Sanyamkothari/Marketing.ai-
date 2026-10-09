// The audit screens' pure builders (Plan J M103), run by node alone against REAL API answers.
//
// `CP_FIXTURES` is a directory of JSON the Python half (test_audit_view.py) wrote from the running app:
// `verified`, `declared`, `descriptive` (audited campaigns), `contacts` (one with a contact file) and
// `programme`. What a person reads on a campaign's page - the label, why, the rates, the effect on the
// contacted, and every reason a value is missing - must be the server's words and numbers, drawn and not
// recomputed.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { auditBody, auditCardHtml, auditPageHtml, contactsCardHtml, fileOf, programmeBody, programmeCardHtml } from "../../../ui/modules/decide/audit.js";
import { campaignPageHtml } from "../../../ui/modules/decide/views.js";

const dir = process.env.CP_FIXTURES;
const read = (name) => JSON.parse(fs.readFileSync(path.join(dir, `${name}.json`), "utf8"));
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, " ");

test("the label, the reason and the check are the server's own words", () => {
  for (const name of ["verified", "declared", "descriptive"]) {
    const view = read(name);
    const html = auditCardHtml(view.audit);
    assert.ok(html.includes(`>${view.audit.label}<`), `${name}: the label`);
    assert.ok(text(html).includes(view.audit.explanation), `${name}: the explanation`);
    assert.ok(html.includes(`data-causal="${view.audit.causal}"`));
  }
  assert.equal(read("verified").audit.label, "Causal");
  assert.equal(read("declared").audit.label, "Random by your statement, not verified");
  assert.equal(read("descriptive").audit.label, "Descriptive only");
  assert.ok(auditCardHtml(read("verified").audit).includes('class="chip ok"'), "only a verified assignment is drawn as proven");
  for (const name of ["declared", "descriptive"]) {
    assert.ok(auditCardHtml(read(name).audit).includes('class="chip neutral"'), `${name} is not drawn as proven`);
  }
});

test("a campaign's page shows the result, the claim and nothing else invented", () => {
  const verified = campaignPageHtml({ view: read("verified") });
  assert.ok(verified.includes("data-audit-card") && verified.includes("data-verdict"));
  const descriptive = read("descriptive");
  const page = campaignPageHtml({ view: descriptive });
  assert.equal(descriptive.verdict, null);
  assert.ok(!page.includes("data-verdict="), "no verdict is drawn for a descriptive-only campaign");
  assert.ok(text(page).includes(descriptive.report.summary), "its own sentence is shown");
  assert.ok(!/campaign added|caused/i.test(text(page).replace(descriptive.audit.explanation, "")), "no causal wording");
  const scored = { ...read("verified"), audit: undefined, contacts: undefined, programme: undefined };
  const plain = campaignPageHtml({ view: scored });
  assert.ok(!plain.includes("data-audit-card") && !plain.includes("data-contacts-card"), "a scored campaign is drawn as before");
});

test("the contact card shows the rates as counted and the effect on the contacted as secondary", () => {
  const readout = read("contacts").contacts;
  const html = contactsCardHtml(readout);
  assert.ok(html.includes("data-contact-rate") && html.includes("data-contamination"));
  const shown = text(html);
  assert.ok(shown.includes(`${readout.treated_contacted.toLocaleString("en-US")} of ${readout.treated_listed.toLocaleString("en-US")}`));
  assert.ok(shown.includes(`${readout.holdout_contacted.toLocaleString("en-US")} of ${readout.holdout_listed.toLocaleString("en-US")}`));
  assert.ok(readout.complier, "the fixture has an effect on the contacted");
  assert.ok(html.includes("data-complier-value") && shown.includes("Secondary: the effect on the customers who were contacted"));
  assert.ok(shown.includes(readout.complier.label));
  const low = readout.complier.effect;
  assert.ok(shown.includes((low.ci_low * 100).toFixed(1)) && shown.includes((low.ci_high * 100).toFixed(1)));
  for (const note of readout.notes) assert.ok(shown.includes(note), "the server's notes are shown");
});

test("a value the server could not give is its reason, never a number", () => {
  const readout = { ...read("contacts").contacts, contact_rate: null, contact_rate_reason: "No contacted-group customer is listed.", complier: null, complier_reason: "The contact rates are too close." };
  const html = contactsCardHtml(readout);
  assert.ok(text(html).includes("No contacted-group customer is listed."));
  assert.ok(html.includes("data-complier-reason") && text(html).includes("The contact rates are too close."));
  assert.ok(!html.includes("data-complier-value"));
  assert.equal(contactsCardHtml(null), "");
  assert.equal(auditCardHtml(null), "");
  assert.equal(programmeCardHtml(undefined), "");
});

test("the programme card says what was compared and shows the server's counts", () => {
  const programme = read("programme").programme;
  const html = programmeCardHtml(programme);
  const shown = text(html);
  assert.ok(shown.includes(programme.label) && shown.includes(programme.explanation));
  assert.ok(shown.includes(programme.holdout_members.toLocaleString("en-US")) && shown.includes(programme.other_customers.toLocaleString("en-US")));
  assert.ok(shown.includes("not of one message"), "it is the whole programme, not a message");
  assert.ok(campaignPageHtml({ view: read("programme") }).includes("data-programme-card"));
});

const upload = (name, columns, rows = 100) => ({ upload_id: `u_${name}`, profile: { file_name: `${name}.csv`, row_count: rows, columns: columns.map((c) => ({ name: c })) } });

test("the form offers each file's own columns and never assumes how the groups were chosen", () => {
  const files = { assignment: fileOf(upload("a", ["id", "grp", "age"])), outcomes: null, contact: null, programme: null };
  const html = auditPageHtml({ state: { files, busy: false, error: null } });
  assert.ok(html.includes('<option value="grp">grp</option>') && html.includes('<option value="age">age</option>'));
  assert.ok(!/name="assignment_basis"[^>]*checked/.test(html), "neither choice is ticked");
  assert.ok(html.includes("Never assumed"));
  assert.match(html, /data-audit-submit disabled/, "not until both files are in");
  const both = { ...files, outcomes: fileOf(upload("o", ["id", "won", "day"])) };
  assert.doesNotMatch(auditPageHtml({ state: { files: both, busy: false, error: null } }), /data-audit-submit disabled/);
  const refused = auditPageHtml({ can: () => false, state: { files, busy: false, error: null } });
  assert.ok(refused.includes("data-audit-refused") && !refused.includes("data-audit-form"));
  const failed = auditPageHtml({ state: { files: both, busy: false, error: { message: "Measure again on or after 1 Oct 2026." } } });
  assert.ok(failed.includes("data-audit-error") && failed.includes("Measure again on or after 1 Oct 2026."));
});

test("the audit request is built from what the person chose", () => {
  const files = { assignment: fileOf(upload("a", ["cust", "grp"])), outcomes: fileOf(upload("o", ["customer", "won"])), contact: fileOf(upload("c", ["cust", "sent"])) };
  const body = auditBody(
    {
      assignment_key: "cust",
      arm_column: "grp",
      control_value: " none ",
      treated_values: "gold, silver",
      sent_date_column: "",
      intended_column: "",
      assignment_basis: "random",
      outcomes_key: "customer",
      outcome_column: "won",
      outcome_kind: "continuous",
      positive_label: "",
      outcome_date_column: "",
      treatment_start: "2026-04-01",
      outcome_window_days: "30",
      contacted_column: "sent",
      contact_key: "cust",
      unlisted_customers: "not_contacted",
      name: "",
    },
    files,
  );
  assert.equal(body.primary_key, "cust");
  assert.deepEqual(body.assignment, { upload_id: "u_a", key_columns: null, arm_column: "grp", control_value: "none", treated_values: ["gold", "silver"], sent_date_column: null, intended_column: null });
  assert.deepEqual(body.outcomes.key_columns, ["customer"], "named only when it differs");
  assert.equal(body.outcomes.outcome_kind, "continuous");
  assert.equal(body.assignment_basis, "random");
  assert.equal(body.treatment_start, "2026-04-01T00:00:00Z");
  assert.equal(body.outcome_window_days, 30);
  assert.equal(body.outcome_is_good, false, "an unticked box says the opposite");
  assert.deepEqual(body.contact, { upload_id: "u_c", key_columns: null, contacted_column: "sent", unlisted_customers: "not_contacted" });
  const none = auditBody({ assignment_key: "cust", arm_column: "grp", assignment_basis: "", outcome_is_good: "on" }, { assignment: files.assignment, outcomes: files.outcomes });
  assert.equal(none.assignment_basis, null, "never assumed: the server refuses it");
  assert.equal(none.outcome_is_good, true);
  assert.equal("contact" in none, false);
});

test("the programme request names the period, the file and the columns", () => {
  const body = programmeBody(
    { programme_key: "id", programme_outcome: "spend", programme_kind: "continuous", period_start: "2026-01-01", period_end: "2026-03-31", programme_name: "Q1" },
    { programme: fileOf(upload("p", ["id", "spend"])) },
  );
  assert.deepEqual(body, { name: "Q1", primary_key: "id", period: { start: "2026-01-01", end: "2026-03-31" }, outcome: { upload_id: "u_p", outcome_column: "spend", positive_label: null, outcome_kind: "continuous" } });
});
