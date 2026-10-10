/* Data Doctor's intake notes (ui/modules/intake/notes.js, Plan L M150) under Guided setup's file picker,
   in jsdom, against REAL bodies (PB_FIXTURES, written by tests/integration/test_intake_ui.py). The real
   Setup form (ui/usecase.js) is drawn the way app.js draws it, with the agent module registered as
   index.html's PLAN-G block loads it. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const byId = Object.fromEntries(Object.entries(ids).map(([name, id]) => [id, name]));

/** The form fields of every `POST /uploads`, in order: the harness only records JSON bodies. */
const forms = [];
let hostile = false;

const server = (request) => {
  const { method, path } = request;
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture("runs_empty") };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture("models_empty") };
  if (method === "POST" && path === "/uploads") {
    const form = forms[forms.length - 1] || {};
    const name = form.intake === "false" ? "asis" : form.sheet === "Summary" ? "sheet" : "report";
    const body = structuredClone(fixture(`upload_${name}`));
    if (hostile) body.intake.notes = [`<img src=x onerror="window.__pwned=1">Skipped <b>3</b> rows`];
    return { status: 201, body };
  }
  const start = /^\/uploads\/([^/]+)\/agent-session$/.exec(path);
  if (start && method === "POST") return { status: 201, body: fixture(`start_${byId[start[1]]}`) };
  return null;
};

const { w } = installPage(server, { hash: "#/uc/x" });
const pageFetch = w.fetch;
w.fetch = async (input, init = {}) => {
  if (init.body && typeof init.body.get === "function") {
    forms.push({ intake: init.body.get("intake"), sheet: init.body.get("sheet") });
  }
  return pageFetch(input, init);
};
await import("../../../../../ui/modules/agent/index.js");
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");

const app = $("#app");
const controller = createController(uc, () => rerender());
function rerender() {
  app.innerHTML = useCaseHtml(uc, controller.state);
  controller.bind(app);
}
await controller.refreshLists();
controller.sync();
rerender();

const notes = () => ($("[data-intake-notes]") ? $("[data-intake-notes]").textContent.replace(/\s+/g, " ").trim() : "");

async function waitForSession(count) {
  await until(() => forms.length >= count && !$(".ag .loading") && $("[data-intake-notes]"), 3000, "the upload and its notes");
}

test("the file picker takes Excel workbooks", () => {
  const input = $("#ag-file");
  assert.ok(input, "Guided setup draws its upload control");
  assert.match(input.getAttribute("accept"), /\.xlsx/);
  assert.match($(".ag .fname").textContent, /Excel/);
});

test("after an upload, the notes say how the workbook was read and offer both ways back", async () => {
  const input = $("#ag-file");
  const file = new w.File(["PK"], "customers.xlsx");
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await waitForSession(1);
  assert.deepEqual(forms[0], { intake: null, sheet: null });
  for (const note of fixture("upload_report").intake.notes) assert.ok(notes().includes(note), `shows: ${note}`);
  assert.ok($("[data-intake-asis]"), "offers to read the file as it is");
  assert.deepEqual(
    $$("[data-intake-sheet]").map((button) => button.dataset.intakeSheet),
    ["Summary"],
  );
});

test("Read it as it is sends the same file again with intake off", async () => {
  $("[data-intake-asis]").click();
  await waitForSession(2);
  assert.deepEqual(forms[1], { intake: "false", sheet: null });
  assert.match(notes(), /turned off/);
  assert.equal($("[data-intake-asis]"), null, "a file read as it is offers no second 'as it is'");
});

test("choosing another sheet sends the same file with that sheet", async () => {
  $("[data-intake-sheet]").click();
  await waitForSession(3);
  assert.deepEqual(forms[2], { intake: null, sheet: "Summary" });
  assert.match(notes(), /"Summary"/);
});

test("a note is text, never markup", async () => {
  hostile = true;
  const input = $("#ag-file");
  Object.defineProperty(input, "files", { value: [new w.File(["PK"], "customers.xlsx")], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await waitForSession(4);
  assert.equal($("[data-intake-notes] img"), null);
  assert.equal($("[data-intake-notes] b"), null);
  assert.equal(w.__pwned, undefined);
  assert.match(notes(), /<img src=x/);
});
