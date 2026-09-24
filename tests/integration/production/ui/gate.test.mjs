/* DEC-792: role-aware controls. Every action a role may not take is disabled and explained in place
   with the server's own sentence, after every paint, for every screen - and nothing at all changes
   when access control is off. The markup below uses the real ids of the real screens;
   test_production_ui.py checks each id in ACTION_CONTROLS is still drawn by its screen. */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle } from "./harness.mjs";

let me = fixture("me_viewer");
const { w } = installPage((request) => (request.path === "/auth/me" ? { status: 200, body: me } : null));

const session = await import("../../../../ui/modules/production/session.js");
const gate = await import("../../../../ui/modules/production/gate.js");
session.installFetch(w);
gate.installGates(w.document);

/** What the Setup form, the copy screen and the connection screen draw, reduced to their controls. */
const SCREEN = `<main class="screen">
  <form id="f-setup"><div class="orline"><label class="control file"><input type="file" id="f-file" class="sr"><span class="fname">Choose file</span></label></div>
    <div class="actions"><button type="submit" class="run" id="f-run">Run</button></div></form>
  <h3>Running… <button type="button" class="cancel" id="f-cancel">Cancel</button></h3>
  <div class="gtpl-actions"><button type="button" class="g-approve" data-approve="t1">Approve</button><button type="button" class="g-regen" data-regen="t1">Regenerate</button></div>
  <div class="gtpl-actions"><button type="button" class="g-approve" data-approve="t2">Approve</button></div>
  <button type="button" data-action="models.approve">Approve champion</button>
  <form id="c-form"><label class="check"><input type="radio" name="c-source" value="default_chain"> Default</label>
    <div class="actions"><button type="submit" class="run" id="c-save">Save</button></div></form>
  <button type="button" class="run" id="c-test">Test connection</button>
</main>`;

async function as(name) {
  me = fixture(name);
  await session.loadMe();
}

async function paint(html = SCREEN) {
  $("#app").innerHTML = html;
  await settle(2);
}

const notes = () => $$(".pb-why").map((n) => n.textContent);

beforeEach(async () => {
  await as("me_viewer");
  await paint();
});

test("a Viewer can start nothing, and each control says who can", () => {
  for (const sel of ["#f-file", "#f-run", "#f-cancel", '[data-approve="t1"]', "[data-regen]", '[data-action="models.approve"]', "#c-save", "#c-test"]) {
    assert.equal($(sel).disabled, true, sel);
  }
  assert.equal($("#f-run").title, "Only an Analyst can start a run.");
  assert.equal($("#f-file").closest("label").classList.contains("pb-off"), true);
  assert.ok(notes().includes("Only an Analyst can upload data."));
  assert.ok(notes().includes("Only an Approver can approve a champion."));
  assert.ok(notes().includes("Only an Approver can approve campaign copy."));
  assert.ok(notes().includes("Only an Admin can change the AWS connection."));
  // The radio is gated without repeating the Save button's sentence.
  assert.equal($('input[name="c-source"]').disabled, true);
  assert.equal(notes().filter((n) => n === "Only an Admin can change the AWS connection.").length, 1);
});

test("an Analyst runs but cannot approve (separation of duties)", async () => {
  await as("me_analyst");
  await paint();
  assert.equal($("#f-run").disabled, false);
  assert.equal($("#f-file").disabled, false);
  assert.equal($("[data-regen]").disabled, false);
  assert.equal($('[data-approve="t1"]').disabled, true);
  assert.equal($('[data-action="models.approve"]').title, "Only an Approver can approve a champion.");
});

test("an Approver approves but cannot run", async () => {
  await as("me_approver");
  await paint();
  assert.equal($('[data-approve="t1"]').disabled, false);
  assert.equal($('[data-action="models.approve"]').disabled, false);
  assert.equal($("#f-run").disabled, true);
  assert.equal($("#f-run").title, "Only an Analyst can start a run.");
});

test("an Admin changes settings but approves nothing (DEC-703)", async () => {
  await as("me_admin");
  await paint();
  assert.equal($("#c-save").disabled, false);
  assert.equal($("#c-test").disabled, false);
  assert.equal($('[data-approve="t2"]').disabled, true);
  assert.equal($("#f-run").disabled, true);
});

test("with access control off nothing is touched", async () => {
  await as("me_off");
  await paint();
  assert.equal($$("[data-pb-gate]").length, 0);
  assert.equal($$(".pb-why").length, 0);
  assert.equal($$("[disabled]").length, 0);
  assert.equal($("#f-run").hasAttribute("title"), false);
});

test("a gated submit or click never reaches the screen's own handler", async () => {
  let submitted = 0;
  let approved = 0;
  $("#f-setup").addEventListener("submit", (e) => {
    e.preventDefault();
    submitted += 1;
  });
  $('[data-approve="t1"]').addEventListener("click", () => {
    approved += 1;
  });
  $("#f-setup").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  $('[data-approve="t1"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true }));
  assert.equal(submitted, 0);
  assert.equal(approved, 0);
});

test("every repaint is gated again, and a screen re-enabling a control is overruled", async () => {
  await paint(SCREEN.replace("Run</button>", "Run again</button>"));
  assert.equal($("#f-run").disabled, true);
  $("#f-run").disabled = false; // what usecase.js does once a file is chosen
  await settle(2);
  assert.equal($("#f-run").disabled, true);
});

test("signing in as someone who may ungates in place, keeping the screen's own disabled state", async () => {
  await paint(SCREEN.replace('id="f-run">', 'id="f-run" disabled>'));
  assert.equal($("#f-cancel").disabled, true);
  await as("me_analyst");
  await settle(2);
  assert.equal($("#f-cancel").disabled, false);
  assert.equal($("#f-run").disabled, true, "the Setup form had it disabled itself: that stays");
  assert.equal($("#f-run").hasAttribute("data-pb-gate"), false);
  assert.ok(!notes().includes("Only an Analyst can start a run."));
});

/** The uplift Setup, campaign results, value view, feedback export, client picker and raw-tables panel
 * (v1, WP7), reduced to their controls - the real ids and data attributes those screens draw. */
const MORE = `<main class="screen">
  <form id="u-form"><label class="control file"><input type="file" id="u-file" class="sr"><span class="fname">Choose file</span></label>
    <button type="submit" class="run" id="u-run">Train</button></form>
  <button type="button" class="cancel" id="u-cancel">Cancel</button>
  <form id="u-camp"><label class="control file"><input type="file" id="u-camp-file" class="sr"></label>
    <button type="submit" class="run" id="u-camp-run">Measure</button></form>
  <form class="pe-form" data-pe-roi><input name="cost_per_contact" value="10"><button type="submit" class="pe-btn primary">Save values</button></form>
  <p><a href="http://localhost/pilot/feedback/export">export all feedback</a></p>
  <div class="control sel"><select id="f-client" aria-label="Client"><option value="c1">Demo</option><option value="__new__">+ New client</option></select></div>
  <button type="button" class="linkbtn" id="f-client-add">Add</button>
  <label class="control file"><input type="file" class="sr" multiple data-act="pick-files"><span class="fname">Add source files</span></label>
  <table><tr><td><button type="button" class="linkbtn" data-act="delete-source" data-source="s1">Remove</button></td></tr>
    <tr><td><button type="button" class="linkbtn" data-act="delete-source" data-source="s2">Remove</button></td></tr></table>
  <button type="button" class="linkbtn" data-act="confirm-role" data-source="s1">Confirm</button>
  <div class="control sel"><select data-act="set-role" data-source="s1"><option>entity</option></select></div>
  <button type="button" class="linkbtn" data-act="accept-all" data-source="s1">Accept all</button>
  <button type="button" class="linkbtn" data-act="save-mapping" data-source="s1">Save mapping</button>
  <button type="button" class="run" data-act="preview">Preview</button>
  <button type="button" class="run" data-act="build">Build</button>
  <button type="button" class="run" data-act="use-dataset">Use this dataset</button>
</main>`;

const reasonOf = (method, path) => me.permissions.find((p) => p.method === method && p.path === path).reason;

test("a Viewer is refused the uplift, campaign, value, feedback, new-client and raw-tables controls too", async () => {
  await paint(MORE);
  const refused = {
    "#u-file": ["POST", "/uploads"],
    "#u-run": ["POST", "/runs"],
    "#u-cancel": ["POST", "/runs/{run_id}/cancel"],
    "#u-camp-file": ["POST", "/uploads"],
    "#u-camp-run": ["POST", "/runs/{run_id}/campaign-results"],
    '[data-pe-roi] button[type="submit"]': ["PUT", "/pilot/roi/{run_id}"],
    "[data-pe-roi] input": ["PUT", "/pilot/roi/{run_id}"],
    '#f-client option[value="__new__"]': ["POST", "/clients"],
    "#f-client-add": ["POST", "/clients"],
    '[data-act="pick-files"]': ["POST", "/clients/{client_id}/sources"],
    '[data-act="confirm-role"]': ["PATCH", "/clients/{client_id}/sources/{source_id}"],
    '[data-act="set-role"]': ["PATCH", "/clients/{client_id}/sources/{source_id}"],
    '[data-act="accept-all"]': ["PUT", "/clients/{client_id}/mappings/{mapping_id}"],
    '[data-act="save-mapping"]': ["PUT", "/clients/{client_id}/mappings/{mapping_id}"],
    '[data-act="preview"]': ["POST", "/clients/{client_id}/onboarding-specs/{spec_id}/preview"],
    '[data-act="build"]': ["POST", "/datasets"],
    '[data-act="use-dataset"]': ["POST", "/runs"],
  };
  for (const [sel, [method, path]] of Object.entries(refused)) {
    assert.equal($(sel).disabled, true, sel);
    assert.equal($(sel).title, reasonOf(method, path), sel);
  }
  for (const remove of $$('[data-act="delete-source"]')) {
    assert.equal(remove.disabled, true);
    assert.equal(remove.title, reasonOf("DELETE", "/clients/{client_id}/sources/{source_id}"));
  }
  assert.equal($('#f-client option[value="c1"]').disabled, false, "choosing a client is not refused");
  const feedback = $('a[href$="/pilot/feedback/export"]');
  assert.equal(feedback.getAttribute("aria-disabled"), "true");
  assert.equal(feedback.title, reasonOf("GET", "/pilot/feedback/export"));
  for (const [method, path] of [
    ["POST", "/uploads"],
    ["PUT", "/pilot/roi/{run_id}"],
    ["GET", "/pilot/feedback/export"],
    ["POST", "/clients"],
    ["POST", "/datasets"],
  ]) {
    assert.ok(notes().includes(reasonOf(method, path)), `${method} ${path} is explained in place`);
  }
  // One sentence per kind of control, not one per row: the Remove buttons carry theirs as a title.
  assert.equal(notes().filter((n) => n === reasonOf("DELETE", "/clients/{client_id}/sources/{source_id}")).length, 0);
});

test("an Analyst may use all of them but the feedback export; an Admin only the export", async () => {
  await as("me_analyst");
  await paint(MORE);
  for (const sel of ["#u-file", "#u-run", "#u-camp-run", '[data-pe-roi] button[type="submit"]', "#f-client-add", '[data-act="build"]']) {
    assert.equal($(sel).disabled, false, sel);
  }
  assert.equal($('#f-client option[value="__new__"]').disabled, false);
  assert.equal($('a[href$="/pilot/feedback/export"]').hasAttribute("aria-disabled"), true);
  await as("me_admin");
  await paint(MORE);
  assert.equal($('a[href$="/pilot/feedback/export"]').hasAttribute("aria-disabled"), false);
  assert.equal($("#u-run").disabled, true);
});

test("a gate is idempotent: applying twice adds no second note", async () => {
  const before = $$(".pb-why").length;
  gate.applyGates($("#app"));
  gate.applyGates($("#app"));
  assert.equal($$(".pb-why").length, before);
});
