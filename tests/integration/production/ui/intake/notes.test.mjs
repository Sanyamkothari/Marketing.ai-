/* Data Doctor's intake notes (ui/modules/intake/notes.js, Plan L M150) in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/test_intake_ui.py): the box that says how a workbook was read,
   its two ways back, and that a note is text, never markup. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage } from "../harness.mjs";

const { w } = installPage(() => null, { hash: "#/data/check" });
const { TABLE_FILE_TYPES, bindIntakeNotes, intakeNotesHtml } = await import("../../../../../ui/modules/intake/notes.js");

const app = $("#app");
const calls = [];
const draw = (intake, options = { undo: true }) => {
  calls.length = 0;
  app.innerHTML = intakeNotesHtml(intake, options);
  bindIntakeNotes(app, { onAsIs: () => calls.push(["asis"]), onSheet: (name) => calls.push(["sheet", name]) });
};
const notes = () => ($("[data-intake-notes]") ? $("[data-intake-notes]").textContent.replace(/\s+/g, " ").trim() : "");

test("the file picker takes Excel workbooks", () => {
  assert.match(TABLE_FILE_TYPES, /\.xlsx/);
});

test("the notes say how the workbook was read and offer both ways back", () => {
  const report = fixture("upload_report").intake;
  draw(report);
  for (const note of report.notes) assert.ok(notes().includes(note), `shows: ${note}`);
  assert.ok($("[data-intake-asis]"), "offers to read the file as it is");
  assert.deepEqual(
    $$("[data-intake-sheet]").map((button) => button.dataset.intakeSheet),
    ["Summary"],
  );
});

test("Read it as it is asks for the same file again with intake off", () => {
  draw(fixture("upload_report").intake);
  $("[data-intake-asis]").click();
  assert.deepEqual(calls, [["asis"]]);
  draw(fixture("upload_asis").intake);
  assert.match(notes(), /turned off/);
  assert.equal($("[data-intake-asis]"), null, "a file read as it is offers no second 'as it is'");
});

test("choosing another sheet asks for the same file with that sheet", () => {
  draw(fixture("upload_report").intake);
  $("[data-intake-sheet]").click();
  assert.deepEqual(calls, [["sheet", "Summary"]]);
  draw(fixture("upload_sheet").intake);
  assert.match(notes(), /"Summary"/);
});

test("a screen that cannot upload again draws the notes without buttons", () => {
  draw(fixture("upload_report").intake, {});
  assert.ok($("[data-intake-notes]"));
  assert.equal($("[data-intake-asis]"), null);
  assert.equal($("[data-intake-sheet]"), null);
});

test("a note is text, never markup", () => {
  const intake = structuredClone(fixture("upload_report").intake);
  intake.notes = [`<img src=x onerror="window.__pwned=1">Skipped <b>3</b> rows`];
  draw(intake);
  assert.equal($("[data-intake-notes] img"), null);
  assert.equal($("[data-intake-notes] b"), null);
  assert.equal(w.__pwned, undefined);
  assert.match(notes(), /<img src=x/);
});
