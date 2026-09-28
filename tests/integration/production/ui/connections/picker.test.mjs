/* Guided setup's "Pick from a connection" (Plan H M80) in jsdom, against REAL bodies (PB_FIXTURES,
   written by tests/integration/connections/test_connections_ui.py). The real Setup form (ui/usecase.js)
   is drawn the way app.js draws it, with the agent module registered as index.html's PLAN-G block does. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const imported = fixture("imported");
const sent = { imports: [], previews: [], starts: [], browses: [] };

const server = (request) => {
  const { method, path, body, query } = request;
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture("runs_empty") };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture("models_empty") };
  if (method === "GET" && path === "/connections") return { status: 200, body: fixture("list") };
  if (method === "GET" && path === `/connections/${ids.s3}/browse`) {
    sent.browses.push(query.path);
    return { status: 200, body: fixture("browse") };
  }
  if (method === "POST" && path === `/connections/${ids.s3}/preview`) {
    sent.previews.push(body);
    return { status: 200, body: fixture("preview") };
  }
  if (method === "POST" && path === `/connections/${ids.s3}/import`) {
    sent.imports.push(body);
    return { status: 201, body: imported };
  }
  if (method === "POST" && path === `/uploads/${ids.upload}/agent-session`) {
    sent.starts.push(body);
    return { status: 201, body: fixture("imported_start") };
  }
  return null;
};

const { w, calls } = installPage(server, { hash: "#/uc/x" });
await import("../../../../../ui/modules/router.js");
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");
await import("../../../../../ui/modules/agent/index.js");

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

test("step 1 of Guided setup offers a connection beside the file upload", async () => {
  $('[data-setup-tab="guided"]').click();
  assert.ok($("#ag-file"), "the file upload is still there");
  assert.equal(text($("#ag-from-conn")), "Pick from a connection");
});

test("choose a connection, open it, pick a file: its first rows are shown, masked", async () => {
  $("#ag-from-conn").click();
  await until(() => $("[data-cp-connection]"), 2000, "the connections");
  assert.equal($("#ag-file"), null, "the picker takes the upload's place");
  const choices = $$("[data-cp-connection]");
  assert.deepEqual(
    choices.map((b) => b.dataset.cpConnection),
    fixture("list").connections.map((c) => c.connection_id),
  );
  assert.equal($$("[data-cp] img").length, 0, "a hostile connection name stays text");
  $(`[data-cp-connection="${ids.s3}"]`).click();
  await until(() => $("[data-cp-pick]"), 2000, "the listing");
  assert.deepEqual(sent.browses, [""]);
  const picks = $$("[data-cp-pick]");
  const pdf = picks.find((b) => text(b).startsWith("notes.pdf"));
  assert.ok(pdf.disabled, "a file that is not a table cannot be picked");
  picks.find((b) => text(b).startsWith("history.csv")).click();
  await until(() => $("[data-cp-import]") && !$("[data-cp-import]").disabled, 2000, "the preview");
  assert.deepEqual(sent.previews, [{ path: "exports/history.csv" }]);
  const preview = fixture("preview");
  assert.equal($$("[data-cp] tbody tr").length, preview.rows.length);
  assert.ok(preview.rows.length <= 20);
});

test("Import yields an upload and the helper starts on it, exactly as after a file upload", async () => {
  $("[data-cp-import]").click();
  await until(() => sent.starts.length === 1 && $(".ag [data-ag-group]"), 3000, "the helper's session");
  assert.deepEqual(sent.imports, [{ use_case: uc.id, mode: "train", path: "exports/history.csv" }]);
  assert.equal(sent.starts[0].use_case, uc.id);
  assert.equal($("[data-cp]"), null, "the picker is gone once the file is in");
  assert.match(text($(".ag .fname")), /history\.csv \(from Exports\)/);
  assert.ok(!calls.some((c) => c.path === "/uploads" && c.method === "POST"), "no second upload was sent");
});

test("the picker can be left for a plain upload", async () => {
  const again = $("#ag-from-conn");
  assert.ok(again && !again.disabled, "another table can be picked before approving");
  again.click();
  await until(() => $("[data-cp-cancel]"), 2000, "the picker");
  $("[data-cp-cancel]").click();
  assert.ok($("#ag-file"));
  assert.equal(w.__pwned, undefined);
});
