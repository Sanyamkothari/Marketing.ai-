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

test("a gate is idempotent: applying twice adds no second note", async () => {
  const before = $$(".pb-why").length;
  gate.applyGates($("#app"));
  gate.applyGates($("#app"));
  assert.equal($$(".pb-why").length, before);
});
