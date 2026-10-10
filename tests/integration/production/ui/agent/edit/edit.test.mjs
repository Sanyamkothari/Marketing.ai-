/* Editing by hand (ui/modules/agent/edit.js, Plan L M154) in Guided setup, in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/agent/test_edit_ui.py). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../../harness.mjs";

const uc = fixture("use_case");
const sent = { edits: [], rows: [] };
let nextEdit = "edit_everywhere";

const server = (request) => {
  const { method, path, body } = request;
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture("runs_empty") };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture("models_empty") };
  if (method === "POST" && path === "/uploads") return { status: 201, body: fixture("upload") };
  if (method === "POST" && path.endsWith("/agent-session")) return { status: 201, body: fixture("start") };
  if (method === "POST" && path.endsWith("/agent-session/rows")) {
    sent.rows.push(body);
    return { status: 200, body: fixture("rows") };
  }
  if (method === "POST" && path.endsWith("/agent-session/edits")) {
    sent.edits.push(body);
    const answer = fixture(nextEdit);
    return { status: nextEdit === "edit_refused" ? 422 : 200, body: answer };
  }
  return null;
};

const { w } = installPage(server, { hash: "#/uc/x" });
await import("../../../../../../ui/modules/agent/index.js");
const { createController, useCaseHtml } = await import("../../../../../../ui/usecase.js");
const app = $("#app");
const controller = createController(uc, () => rerender());
function rerender() {
  app.innerHTML = useCaseHtml(uc, controller.state);
  controller.bind(app);
}
await controller.refreshLists();
controller.sync();
rerender();

const input = $("#ag-file");
Object.defineProperty(input, "files", { value: [new w.File(["a"], "history.csv")], configurable: true });
input.dispatchEvent(new w.Event("change", { bubbles: true }));
await until(() => $("[data-ag-group]") && !$(".ag .loading"), 3000, "the session");

const rows = fixture("rows");
const city = rows.columns.indexOf("city");

test("the table view opens on the first rows, personal data hidden", async () => {
  $("[data-ag-editor='open']").click();
  await until(() => $(".ag-table"), 3000, "the rows");
  assert.deepEqual(sent.rows[0], { offset: 0, limit: 10, where_column: null, equals: null });
  assert.equal($$(".ag-table tbody tr").length, 10);
  for (const name of rows.personal) {
    const index = rows.columns.indexOf(name);
    assert.equal($$(".ag-table tbody tr")[0].children[index + 1].textContent, "[personal data]");
  }
});

test("a value changed everywhere becomes a ticked suggestion of the person's", async () => {
  $(`[data-ag-cell='0:${city}']`).click();
  const form = $("[data-ag-editform]");
  assert.ok(form, "the edit form opens");
  form.querySelector("input").value = "Mumbai";
  form.querySelector("[data-ag-edit='everywhere']").click();
  await until(() => sent.edits.length === 1 && $("[data-ag-source='person']"), 3000, "the edit");
  const old = rows.rows[0][city];
  assert.deepEqual(sent.edits[0], { kind: "map_values", column: "city", params: { mapping: { [old]: "Mumbai" } } });
  const box = $("[data-ag-source='person']").closest("label").querySelector("input");
  assert.equal(box.checked, true);
});

test("one cell is corrected by the row's ID", async () => {
  nextEdit = "edit_cell";
  $(`[data-ag-cell='1:${city}']`).click();
  const form = $("[data-ag-editform]");
  form.querySelector("input").value = "Agra";
  form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => sent.edits.length === 2, 3000, "the correction");
  const key = rows.rows[1][rows.columns.indexOf(rows.key_column)];
  assert.deepEqual(sent.edits[1], {
    kind: "correct_cells",
    column: "city",
    params: { cells: [{ key, old: rows.rows[1][city], new: "Agra" }] },
  });
});

test("a refused edit says why and changes nothing", async () => {
  nextEdit = "edit_refused";
  $(`[data-ag-cell='2:${city}']`).click();
  $("[data-ag-editform] [data-ag-edit='remove']").click();
  await until(() => sent.edits.length === 3 && $(".ag-editor .err, .ag-editor [role='alert'], .ag-editor .errbox"), 3000, "the refusal");
  assert.match($(".ag-editor").textContent, /ID or the outcome|outcome/);
});
