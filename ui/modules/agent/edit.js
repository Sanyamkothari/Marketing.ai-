// Editing by hand (Plan L M154, DEC-1541): a table view of the file inside Guided setup's "Data fixes".
//
// Every edit becomes a step the person can see and untick, never a change to the file: a cell is
// corrected for that customer only (`correct_cells`, found by the ID column), or a value is changed
// everywhere it appears (`map_values`), or the rows holding a value are left out (`remove_rows`). The
// API checks each one with the same rules as every other step; a refusal is shown under the table.
// Personal-data columns are shown as `[personal data]` and cannot be edited.

import { errorBox, esc, fmtInt } from "../../dom.js";

export const PAGE_ROWS = 10;

export function freshEditor() {
  return { open: false, page: null, offset: 0, busy: false, error: null, editing: null, search: null };
}

function cellHtml(editor, rowIndex, columnIndex, value) {
  const page = editor.page;
  const column = page.columns[columnIndex];
  const personal = (page.personal || []).includes(column);
  const shown = value === null || value === undefined ? "" : String(value);
  if (personal) return `<td class="ag-pd">${esc(shown)}</td>`;
  return `<td><button type="button" class="ag-cell" data-ag-cell="${rowIndex}:${columnIndex}"${
    editor.busy ? " disabled" : ""
  } title="Edit this value">${shown ? esc(shown) : '<span class="ag-empty">empty</span>'}</button></td>`;
}

function formHtml(editor) {
  const e = editor.editing;
  if (!e) return "";
  const keyed = Boolean(editor.page.key_column) && e.key !== null && e.key !== "[personal data]";
  return `<form class="ag-editform" data-ag-editform><p><b>${esc(e.column)}</b> on row ${esc(e.line)}: ${
    e.old === "" ? "<i>empty</i>" : `“${esc(e.old)}”`
  }</p><label>New value <input type="text" name="value" maxlength="100" value="${esc(e.old)}"></label><div class="btn-row">${
    keyed ? `<button type="submit" class="btn primary sm" data-ag-edit="cell">Change this cell only</button>` : ""
  }${
    e.old !== "" ? `<button type="button" class="btn secondary sm" data-ag-edit="everywhere">Change “${esc(e.old)}” everywhere</button>` : ""
  }${
    e.old !== "" ? `<button type="button" class="btn quiet sm" data-ag-edit="remove">Leave out rows with “${esc(e.old)}”</button>` : ""
  }<button type="button" class="btn quiet sm" data-ag-edit="cancel">Cancel</button></div>${
    keyed ? "" : `<p class="fhint">Choose the ID column above to correct one customer's cell.</p>`
  }</form>`;
}

export function editorHtml(editor, { disabled = false } = {}) {
  if (!editor.open) {
    return `<div class="ag-editbar"><button type="button" class="btn quiet sm" data-ag-editor="open"${
      disabled ? " disabled" : ""
    }>Edit by hand</button><span class="fhint">See your rows and correct a value, merge spellings or leave rows out. Each edit is a step you can untick.</span></div>`;
  }
  const page = editor.page;
  const table = page
    ? `<div class="ag-tablewrap"><table class="ag-table"><thead><tr><th>Row</th>${page.columns
        .map((c) => `<th>${esc(c)}</th>`)
        .join("")}</tr></thead><tbody>${page.rows
        .map(
          (row, r) =>
            `<tr><td class="ag-ln">${esc(page.row_numbers[r])}</td>${row
              .map((value, c) => cellHtml(editor, r, c, value))
              .join("")}</tr>`,
        )
        .join("")}</tbody></table></div><div class="btn-row ag-pager"><button type="button" class="btn quiet sm" data-ag-editor="prev"${
        editor.offset === 0 || editor.busy ? " disabled" : ""
      }>Previous</button><span class="fhint">Rows ${fmtInt(editor.offset + 1)}–${fmtInt(
        Math.min(editor.offset + page.rows.length, page.total),
      )} of ${fmtInt(page.total)}</span><button type="button" class="btn quiet sm" data-ag-editor="next"${
        editor.offset + PAGE_ROWS >= page.total || editor.busy ? " disabled" : ""
      }>Next</button><button type="button" class="btn quiet sm" data-ag-editor="close">Close</button></div>`
    : `<div class="loading" role="status">Reading your rows…</div>`;
  return `<section class="ag-group ag-editor" data-ag-editor-panel><h4>Edit by hand</h4><p class="ag-cap">Click a value to change it. Your edits are added to the list above, ticked; untick one to undo it. Your file is never changed.</p>${table}${formHtml(
    editor,
  )}${editor.error ? errorBox(editor.error) : ""}</section>`;
}

/** The edit request for the open form's choice, or null for "cancel". */
export function editRequest(editor, choice, value) {
  const e = editor.editing;
  if (!e || choice === "cancel") return null;
  if (choice === "cell") {
    return { kind: "correct_cells", column: e.column, params: { cells: [{ key: e.key, old: e.old, new: value }] } };
  }
  if (choice === "everywhere") return { kind: "map_values", column: e.column, params: { mapping: { [e.old]: value } } };
  return { kind: "remove_rows", column: e.column, params: { operator: "equals", values: [e.old] } };
}

/** What the person clicked: `{ line, column, old, key }` for row `r`, column `c` of the page. */
export function cellTarget(editor, r, c) {
  const page = editor.page;
  const keyIndex = page.key_column ? page.columns.indexOf(page.key_column) : -1;
  const value = page.rows[r][c];
  return {
    line: page.row_numbers[r],
    column: page.columns[c],
    old: value === null || value === undefined ? "" : String(value),
    key: keyIndex >= 0 ? page.rows[r][keyIndex] : null,
  };
}
