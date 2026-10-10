// Data Doctor's intake notes (Plan L M150, DEC-1505): what intake did to a file on the way in.
//
// Every upload screen draws the same box under its file picker from the `intake` the API answered
// (`POST /uploads`, `POST /connections/{id}/import`, `POST /clients/{id}/sources`). The sentences are
// the API's own (`IntakeReport.notes`); this module only lays them out and offers the two ways back:
// "Read it as it is" (the same file, uploaded again with `intake=false`) and, for a workbook, one
// button per other sheet that holds a table (`sheet=<name>`). A screen without the file in hand (an
// import from a connection) passes no handlers and gets the notes alone.
//
// Tokens only, classes prefixed `di-`, injected once, as the other modules' stylesheets are.

import { esc } from "../../dom.js";

/** What every table-file picker accepts: CSV, Excel workbooks and Parquet. */
export const TABLE_FILE_TYPES = ".csv,.xlsx,.xlsm,.parquet";

const CSS = `
.di-notes{margin-top:10px;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--soft);font-size:13px;color:var(--ink2)}
.di-notes h4{margin:0 0 6px;font-size:12px;font-weight:600;color:var(--ink2)}
.di-notes ul{margin:0;padding-left:18px;line-height:1.5}
.di-notes li{overflow-wrap:anywhere}
.di-notes .di-acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
`;

let injected = false;

function injectStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "intake-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}

/** The other sheets of a workbook that hold a table, by name. */
export function otherTableSheets(intake) {
  if (!intake || !Array.isArray(intake.sheets)) return [];
  return intake.sheets.filter((sheet) => !sheet.used && sheet.looks_like_data && !sheet.hidden).map((sheet) => sheet.name);
}

/**
 * The box, or "" when intake has nothing to say. `undo` draws the buttons: pass it only when the
 * screen can upload the same file again.
 */
export function intakeNotesHtml(intake, { undo = false, busy = false, title = "How Data Doctor read your file" } = {}) {
  const notes = intake && Array.isArray(intake.notes) ? intake.notes : [];
  if (!notes.length) return "";
  injectStyles();
  const disabled = busy ? " disabled" : "";
  const buttons = [];
  if (undo && intake.applied && !intake.turned_off) {
    buttons.push(`<button type="button" class="btn quiet sm" data-intake-asis${disabled}>Read it as it is instead</button>`);
  }
  if (undo) {
    for (const name of otherTableSheets(intake)) {
      buttons.push(
        `<button type="button" class="btn quiet sm" data-intake-sheet="${esc(name)}"${disabled}>Read the sheet “${esc(
          name,
        )}” instead</button>`,
      );
    }
  }
  return `<div class="di-notes" role="status" data-intake-notes><h4>${esc(title)}</h4><ul>${notes
    .map((note) => `<li>${esc(note)}</li>`)
    .join("")}</ul>${buttons.length ? `<div class="di-acts">${buttons.join("")}</div>` : ""}</div>`;
}

/** Wire the box's buttons: `onAsIs()` and `onSheet(name)` upload the same file again. */
export function bindIntakeNotes(root, { onAsIs, onSheet } = {}) {
  if (!root) return;
  root.querySelectorAll("[data-intake-asis]").forEach((button) =>
    button.addEventListener("click", () => onAsIs && onAsIs()),
  );
  root.querySelectorAll("[data-intake-sheet]").forEach((button) =>
    button.addEventListener("click", () => onSheet && onSheet(button.dataset.intakeSheet)),
  );
}
