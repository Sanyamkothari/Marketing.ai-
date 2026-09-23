/* The M48 screens - consent import and lookup, erasure, access export, retention's dry run and apply -
   in the whole page as index.html loads it, signed in as an Admin, then a Viewer. Every body the fake
   API answers with is one the real app answered (PB_FIXTURES, written by
   test_production_ops_ui_js.py). The principal ids typed here are the ones the Python side really
   sent, and the rule checked throughout is DEC-746's: an id goes in a POST body and nowhere else -
   not a URL, not the hash, not the page after the answer is drawn. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, settle, until } from "../harness.mjs";
import { chooseFile, created, installOps, ok, refused, setField, submit } from "./fake.mjs";

const typed = fixture("typed");
const erasure = fixture("erasure");
const plan = fixture("retention_plan");
const access = fixture("access_export");
let privacyConfigured = true;
let planChanged = false;

const { w, calls, forms, saved } = installOps({
  tokens: { "tok-admin": fixture("me_admin"), "tok-viewer": fixture("me_viewer") },
  token: "tok-admin",
  hash: "#/privacy/consent",
  table: [
    [
      "GET",
      "/privacy/purposes",
      () => (privacyConfigured ? ok(fixture("purposes")) : refused(409, fixture("privacy_not_configured"))),
    ],
    [
      "POST",
      "/privacy/consent/imports",
      (r) =>
        forms.at(-1).form.get("file").name === "broken.csv"
          ? refused(422, fixture("import_refused"))
          : created(fixture("import_ok")),
    ],
    ["POST", "/privacy/consent/lookup", () => ok(fixture("lookup"))],
    ["POST", "/privacy/erasure", () => created(erasure)],
    ["GET", "/privacy/erasure", () => ok(fixture("erasures"))],
    ["GET", "/privacy/retrain-flags", () => ok(fixture("retrain_flags"))],
    [
      "POST",
      "/privacy/access-requests",
      () => ({
        status: 200,
        body: "PK\u0003\u0004 not really a zip",
        headers: { "Content-Type": "application/zip", "Content-Disposition": access.disposition },
      }),
    ],
    ["GET", "/privacy/retention/plan", () => ok(plan)],
    [
      "POST",
      "/privacy/retention/apply",
      () => (planChanged ? refused(409, fixture("retention_changed")) : ok(fixture("retention_applied"))),
    ],
    ["GET", "/audit/events", () => ok(fixture("audit_erasure"))],
  ],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");
const session = await import("../../../../../ui/modules/production/session.js");

const text = () => ($("#app") || {}).textContent || "";
const last = (method, path) => calls.filter((c) => c.method === method && c.path === path).pop();
const count = (method, path) => calls.filter((c) => c.method === method && c.path === path).length;
const cardTitles = () => $$("main .card h3").map((h) => h.textContent).join(" | ");

/** DEC-746: the id reached the API in a body, and is in no URL, no hash and no drawn page. */
function nowhereBut(id, body) {
  assert.equal(body.principal_id, id);
  assert.equal(
    calls.some((c) => c.path.includes(id) || Object.values(c.query).some((v) => v.includes(id))),
    false,
    "never in a URL",
  );
  assert.equal(w.location.hash.includes(id), false, "never in the hash");
  assert.equal(w.document.body.innerHTML.includes(id), false, "not on the page once answered");
}

test("the Admin's consent screen lists the purposes and which use cases they gate", async () => {
  await until(() => $("#pb-consent-import"), 3000, "the consent screen");
  assert.ok($('#pb-bar a[href="#/privacy/consent"]'), "Privacy in the user bar");
  for (const purpose of fixture("purposes").purposes) assert.match(text(), new RegExp(purpose.label));
  assert.match(text(), /not legal advice/);
  assert.equal(last("GET", "/privacy/purposes").auth, "Bearer tok-admin");
});

test("a clean consent file is imported; a refused one lists every problem by row and column", async () => {
  const form = $("#pb-consent-import");
  chooseFile(form.elements.namedItem("file"), "consent.csv", "principal_id,purpose,status,recorded_at\n");
  submit(w, form);
  await until(() => $(".pb-ok"), 2000, "the report");
  const ok = fixture("import_ok");
  assert.match($(".pb-ok").textContent, new RegExp(`Imported ${ok.rows_imported} of ${ok.rows_read} rows`));
  assert.match($(".pb-ok").textContent, /Columns ignored: notes/);
  assert.equal(forms.at(-1).form.get("partial"), "false");
  assert.equal(forms.at(-1).form.get("client_id"), null, "no client typed: the deployment's");

  const again = $("#pb-consent-import");
  chooseFile(again.elements.namedItem("file"), "broken.csv", "x\n");
  setField(w, again, "client_id", "acme");
  submit(w, again);
  await until(() => /CONSENT_FILE_REFUSED/.test(text()), 2000, "the refusal");
  const problem = fixture("import_refused").errors[0];
  const row = $$("tbody tr").find((tr) => tr.textContent.includes(problem.code));
  assert.ok(row, "the problem's row");
  assert.match(row.textContent, new RegExp(`^${problem.row}${problem.column}`));
  assert.equal(forms.at(-1).form.get("client_id"), "acme");

  setField(w, $("#pb-consent-import"), "partial", true);
  chooseFile($("#pb-consent-import").elements.namedItem("file"), "consent.csv", "x\n");
  submit(w, $("#pb-consent-import"));
  await until(() => $(".pb-ok"), 2000, "the partial import");
  assert.equal(forms.at(-1).form.get("partial"), "true");
});

test("a lookup sends the id in the body only and answers by hash, per purpose, with every row", async () => {
  const form = $("#pb-consent-lookup");
  setField(w, form, "principal_id", typed.looked_up);
  submit(w, form);
  await until(() => /Ledger rows, oldest first/.test(text()), 2000, "the answer");
  const lookup = fixture("lookup");
  assert.match(text(), new RegExp(lookup.principal_hash));
  const states = Object.fromEntries(lookup.purposes.map((p) => [p.purpose, p.state]));
  assert.equal(states.marketing_communication, "withdrawn");
  assert.ok($$(".pill.bad").some((p) => p.textContent === "withdrawn"));
  assert.ok($$(".pill").some((p) => p.textContent === "no record"));
  assert.match(text(), /privacy\.consent\.lookup/);
  nowhereBut(typed.looked_up, last("POST", "/privacy/consent/lookup").body);
});

test("an erasure is sent only once confirmed, and its outcome is shown per store with the audit reference", async () => {
  w.location.hash = "#/privacy/erasure";
  await until(() => $("#pb-erasure") && /Erasure register ·/.test(cardTitles()), 3000, "the erasure screen");
  const form = $("#pb-erasure");
  setField(w, form, "principal_id", typed.erased);
  setField(w, form, "client_id", "cl_1");
  submit(w, form);
  await until(() => /CONFIRMATION_REQUIRED/.test(text()), 2000, "the confirmation request");
  assert.equal(count("POST", "/privacy/erasure"), 0, "nothing sent unconfirmed");

  const confirmed = $("#pb-erasure");
  setField(w, confirmed, "principal_id", typed.erased);
  setField(w, confirmed, "client_id", "cl_1");
  setField(w, confirmed, "confirm", true);
  submit(w, confirmed);
  await until(() => /Per store/.test(text()), 3000, "the outcome");
  assert.deepEqual(last("POST", "/privacy/erasure").body, { principal_id: typed.erased, client_id: "cl_1" });
  nowhereBut(typed.erased, last("POST", "/privacy/erasure").body);
  for (const store of Object.keys(erasure.store_counts)) assert.match(text(), new RegExp(store));
  for (const model of erasure.models_flagged) assert.match(text(), new RegExp(model));
  assert.match(text(), /flagged for retraining at the next scheduled cycle \(not now\)/);
  assert.match(text(), new RegExp(`Audit: privacy\\.erasure on ${erasure.request_id}`));
  assert.ok(count("GET", "/privacy/erasure") >= 2, "the register re-reads after an erasure");
});

test("the audit reference opens the audit viewer filtered to that erasure", async () => {
  $("[data-audit-action]").click();
  await until(() => w.location.hash === "#/admin/audit" && last("GET", "/audit/events"), 3000, "the audit viewer");
  const query = last("GET", "/audit/events").query;
  assert.equal(query.action, "privacy.erasure");
  assert.equal(query.object_id, erasure.request_id);
  await until(() => /Events · 1/.test(text()), 2000, "the event");
});

test("an access request downloads the zip under the server's name and shows its audit reference", async () => {
  w.location.hash = "#/privacy/access";
  await until(() => $("#pb-access"), 3000, "the access screen");
  const form = $("#pb-access");
  setField(w, form, "principal_id", typed.erased);
  submit(w, form);
  await until(() => /Saved access_export_ar_/.test(text()), 2000, "the download");
  assert.equal(saved.length, 1, "handed to the browser as a file");
  const name = /filename="([^"]+)"/.exec(access.disposition)[1];
  assert.match(text(), new RegExp(`Saved ${name.replace(".", "\\.")}`));
  assert.match(text(), new RegExp(`privacy\\.access_request on ${name.replace(/^access_export_|\.zip$/g, "")}`));
  nowhereBut(typed.erased, last("POST", "/privacy/access-requests").body);
});

test("retention shows the dry run, and applies exactly that plan once confirmed", async () => {
  w.location.hash = "#/privacy/retention";
  await until(() => $("#pb-retention-apply"), 3000, "the dry run");
  const keys = new Set(plan.plan.items.map((item) => item.key));
  const drawn = $$("tbody tr").filter((tr) => keys.has(tr.querySelector("td").textContent));
  assert.equal(drawn.length, plan.plan.items.length, "every file that would go");
  for (const skip of plan.plan.skipped) assert.match(text(), new RegExp(skip.reason_code));
  assert.match(text(), new RegExp(plan.plan_hash));
  submit(w, $("#pb-retention-apply"));
  await until(() => /CONFIRMATION_REQUIRED/.test(text()), 2000, "the confirmation request");
  assert.equal(count("POST", "/privacy/retention/apply"), 0);

  setField(w, $("#pb-retention-apply"), "confirm", true);
  submit(w, $("#pb-retention-apply"));
  await until(() => /Deleted \d+ file\(s\)/.test(text()), 2000, "the result");
  assert.deepEqual(last("POST", "/privacy/retention/apply").body, {
    plan_id: plan.plan.plan_id,
    planned_at: plan.plan.planned_at,
    plan_hash: plan.plan_hash,
  });
  const result = fixture("retention_applied").result;
  assert.match(text(), new RegExp(`Deleted ${result.deleted.length} file\\(s\\)`));
  assert.equal($("#pb-retention-apply"), null, "an applied plan cannot be applied again");
  assert.match(text(), /This plan has been applied/);
});

test("a plan that changed since the dry run is refused, and only a new dry run can be applied", async () => {
  planChanged = true;
  $("#pb-retention-replan").click();
  await until(() => $("#pb-retention-apply"), 3000, "a fresh dry run");
  assert.equal(count("GET", "/privacy/retention/plan"), 2);
  setField(w, $("#pb-retention-apply"), "confirm", true);
  submit(w, $("#pb-retention-apply"));
  await until(() => /RETENTION_PLAN_CHANGED/.test(text()), 2000, "the refusal");
  assert.match(text(), new RegExp(fixture("retention_changed").detail.message.slice(0, 30)));
  assert.equal($("#pb-retention-apply"), null, "the stale plan's apply is withdrawn");
  assert.match(text(), /This plan is out of date/);
});

test("without a privacy policy the server's sentence replaces the forms", async () => {
  privacyConfigured = false;
  w.location.hash = "#/privacy/consent";
  await until(() => /PRIVACY_NOT_CONFIGURED/.test(text()), 3000, "the refusal");
  assert.match(text(), new RegExp(fixture("privacy_not_configured").detail.message.slice(0, 40).replace(/[()]/g, ".")));
  assert.equal($("#pb-consent-import"), null);
  privacyConfigured = true;
});

test("a Viewer is told why, and the privacy API is not even asked", async () => {
  session.storeToken("tok-viewer", null);
  await session.loadMe();
  assert.equal($('#pb-bar a[href="#/privacy/consent"]'), null, "no Privacy link for a Viewer");
  const before = [count("GET", "/privacy/purposes"), count("GET", "/privacy/erasure")];
  w.location.hash = "#/privacy/erasure";
  await until(() => /ROLE_REQUIRED/.test(text()), 3000, "the refusal");
  assert.match(text(), /Only an Admin can see the privacy policy\./);
  await settle(2);
  assert.deepEqual([count("GET", "/privacy/purposes"), count("GET", "/privacy/erasure")], before, "nothing was read");
});
