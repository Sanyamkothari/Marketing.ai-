/* Manual setup's "Pick from a connection" (UI audit §8.4 item 10; DEC-1109 follow-up) in jsdom, against
   REAL bodies (PB_FIXTURES, written by tests/integration/connections/test_connections_ui.py). The real
   Setup form (ui/usecase.js) is drawn the way app.js draws it, with the agent and connections modules
   registered as index.html's PLAN-G block does; the fake fetch answers what the API answered. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const imported = fixture("imported");
const sent = { imports: [], runs: [], lists: 0 };
let connections = fixture("list_empty");

const server = (request) => {
  const { method, path, body } = request;
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture("runs_empty") };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture("models_empty") };
  if (method === "GET" && path === "/connections") {
    sent.lists += 1;
    return { status: 200, body: connections };
  }
  if (method === "GET" && path === `/connections/${ids.s3}/browse`) return { status: 200, body: fixture("browse") };
  if (method === "POST" && path === `/connections/${ids.s3}/preview`) return { status: 200, body: fixture("preview") };
  if (method === "POST" && path === `/connections/${ids.s3}/import`) {
    sent.imports.push(body);
    return { status: 201, body: imported };
  }
  if (method === "POST" && path === "/runs") {
    sent.runs.push(body);
    return { status: 503, body: { detail: { code: "TEST_STOP", message: "Stopped by the test." } } };
  }
  return null;
};

const { w, calls } = installPage(server, { hash: "#/uc/x" });
await import("../../../../../ui/modules/router.js");
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");
const { fmtInt } = await import("../../../../../ui/dom.js");
await import("../../../../../ui/modules/agent/index.js");
await import("../../../../../ui/modules/connections/index.js");

const app = $("#app");
let controller;
const rerender = () => {
  app.innerHTML = useCaseHtml(uc, controller.state);
  controller.bind(app);
};
controller = createController(uc, rerender);
await controller.refreshLists();
controller.sync();
rerender();

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

test("with no connection, Manual Step 1 says so in one line and links to the Connections page", async () => {
  $('[data-setup-tab="manual"]').click();
  assert.ok($("#f-file"), "the file upload is Step 1's control");
  await until(() => $(".up-src .cp-none"), 2000, "the no-connection line");
  const line = $(".up-src .cp-none");
  assert.match(text(line), /^No connections yet: add one to pick a table or file from it\.$/);
  assert.equal(line.querySelector("a").getAttribute("href"), "#/connections");
  assert.equal($("[data-upload-conn]"), null, "nothing to pick from, so no button");
});

test("once a connection exists (back from #/connections), Manual Step 1 offers Pick from a connection", async () => {
  connections = fixture("list");
  const before = sent.lists;
  w.dispatchEvent(new w.HashChangeEvent("hashchange")); // the person went to Connections and came back
  rerender();
  await until(() => $("[data-upload-conn]"), 2000, "the offer");
  assert.equal(sent.lists, before + 1, "the list is read again after a route change");
  assert.equal(text($("[data-upload-conn]")), "Pick from a connection");
  assert.equal($(".up-src .cp-none"), null);
  assert.ok($("#f-file"), "the file upload is still offered beside it");
  rerender();
  assert.ok($("[data-upload-conn]"), "a repaint of the form keeps the offer");
  assert.equal(sent.lists, before + 1, "a repaint does not read the list again");
});

test("picking and importing fills Step 1 with the upload, exactly as a file upload does", async () => {
  $("[data-upload-conn]").click();
  await until(() => $("[data-cp-connection]"), 2000, "the connections");
  assert.equal($("#f-file"), null, "the picker takes the file control's place");
  assert.ok($("#f-upload-source [data-cp]"));
  assert.equal($$("[data-cp] img").length, 0, "a hostile connection name stays text");
  $(`[data-cp-connection="${ids.s3}"]`).click();
  await until(() => $("[data-cp-pick]"), 2000, "the listing");
  rerender(); // the Setup form repaints (a module loading, a client change): the picker survives it
  assert.ok($("[data-cp-pick]"), "the listing is still there after a repaint");
  $$("[data-cp-pick]").find((b) => text(b).startsWith("history.csv")).click();
  await until(() => $("[data-cp-import]") && !$("[data-cp-import]").disabled, 2000, "the preview");
  $("[data-cp-import]").click();
  await until(() => $("#f-pk"), 3000, "Step 2 after the import");

  assert.deepEqual(sent.imports, [{ use_case: uc.id, mode: "train", path: "exports/history.csv" }]);
  assert.ok(!calls.some((c) => c.path === "/uploads" && c.method === "POST"), "no second upload was sent");
  assert.equal($("[data-cp]"), null, "the picker is gone once the data is in");
  assert.ok($("#f-file"), "the file control is back");
  assert.equal(text($(".fname")), "history.csv (from Exports)");

  const profile = imported.profile;
  assert.equal($(".preview .pv-head b").textContent, fmtInt(profile.row_count));
  assert.equal($("#f-pk").value, profile.primary_key_candidates[0], "the ID column is detected as for a file");
  assert.equal($("#f-target").value, profile.target_candidate, "and so is the outcome");
  const names = profile.columns.map((c) => c.name);
  assert.deepEqual(
    $$("#f-pk option").map((o) => o.value).filter(Boolean),
    names,
    "the ID picker lists the upload's own columns",
  );
  assert.ok(!$("#f-run").disabled, "Run is ready");
  assert.equal(text($(".actions .reason")), "");
});

test("Run posts the imported upload, as for a file", async () => {
  $("#f-setup").dispatchEvent(new w.Event("submit", { cancelable: true }));
  await until(() => sent.runs.length === 1, 2000, "the run request");
  const body = sent.runs[0];
  assert.equal(body.upload_id, imported.upload_id);
  assert.equal(body.dataset_id, undefined);
  assert.equal(body.primary_key, imported.profile.primary_key_candidates[0]);
  assert.equal(body.target, imported.profile.target_candidate);
});

test("the picker can be left for a plain upload, keeping the data already in", async () => {
  $("[data-upload-conn]").click();
  await until(() => $("[data-cp-cancel]"), 2000, "the picker");
  $("[data-cp-cancel]").click();
  assert.ok($("#f-file"));
  assert.equal(text($(".fname")), "history.csv (from Exports)", "leaving the picker keeps the import");
  assert.ok($("#f-pk"));
  assert.equal(w.__pwned, undefined);
});
