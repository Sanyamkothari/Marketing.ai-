// The Data page (Plan M, M179, DEC-1609): `#/data[/<tab>]`.
//
//   #/data          Sources: the saved connections and the uploaded files in one list, each with its health and when
//                   it was last refreshed or uploaded. A connection keeps Test, Edit and Delete, and "+ Add a source"
//                   opens the kinds of source to set up.
//   #/data/check    Check my data: what Data Doctor does, with the way in to it.
//
// Nothing here is new engine behaviour. The list, the test and the delete are the Connections module's own calls
// (`../connections/api.js`) and its own badge and test report (`statusBadge`, `stepsHtml`); the set-up and edit forms
// stay at `#/connections/new/<kind>` and `#/connections/<id>`; Data Doctor stays at `#/doctor`. The one addition is the
// list of uploaded files, `GET /uploads`.
//
// Data Doctor is reached by a link, not drawn inside the tab: its module draws a whole page of its own (its own H1 and
// `<main>`) and exports no render function to reuse, so embedding it here would give this page two H1s. The link is at
// the top of the Sources tab, so it is one click from Data.
//
// This page does not import `ui/layout.js`: it draws the same in the classic and the campaigns layout.

import { announceStatus, emptyState, errorBox, esc, fmtInt, fmtSize, fmtStamp, headActions, pageHead, skeleton } from "../../dom.js";
import { deleteConnection, getConnections, getKinds, testConnection } from "../connections/api.js";
import { statusBadge, stepsHtml } from "../connections/page.js";
import { injectConnectionStyles } from "../connections/styles.js";
import { getUploads } from "./api.js";
import { injectDataStyles } from "./styles.js";

export const DATA_HOME = "#/data";
export const DOCTOR_HREF = "#/doctor";
export const TABS = Object.freeze([
  { id: "sources", label: "Sources", href: DATA_HOME },
  { id: "check", label: "Check my data", href: `${DATA_HOME}/check` },
]);
/** How many uploaded files the list shows before "Show all". */
export const FILES_SHOWN = 25;

const state = {
  kinds: [],
  connections: [],
  uploads: [],
  loadError: null,
  uploadsError: null,
  busy: {}, // connection id -> "test" | "delete"
  reports: {}, // connection id -> the last test report
  errors: {}, // connection id -> an error from a row action
  confirming: null, // the connection id whose Delete asks "Are you sure?"
  chooserOpen: false,
  showAllFiles: false,
};

let view = { app: null, tab: "sources" };

// --- small pieces ----------------------------------------------------------------------------------

const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;

const connectionName = (id) => {
  const found = state.connections.find((c) => c.connection_id === id);
  return found ? found.name : null;
};

/** When data last came in through this connection: the newest file imported from it. */
function lastImport(connection) {
  const latest = state.uploads.find((u) => u.from_connection_id === connection.connection_id);
  return latest ? latest.created_at : null;
}

function header(tab) {
  const primary =
    tab === "sources"
      ? { label: "+ Add a source", id: "dt-add", attrs: `aria-expanded="${state.chooserOpen}" aria-controls="dt-chooser"` }
      : { label: "Open Data Doctor", href: DOCTOR_HREF };
  const secondary = tab === "sources" ? [{ label: "Check my data", href: DOCTOR_HREF, attrs: "data-dt-doctor" }] : [];
  return pageHead(
    `<h1 class="h1">Data</h1><p class="desc">Where your customer data comes from, and whether it is ready to use.</p>${headActions({
      primary,
      secondary,
    })}`,
  );
}

function tabs(tab) {
  return `<nav class="dt-tabs" aria-label="Data sections">${TABS.map(
    (t) => `<a href="${t.href}" data-dt-tab="${t.id}"${t.id === tab ? ' aria-current="page"' : ""}>${esc(t.label)}</a>`,
  ).join("")}</nav>`;
}

// --- the Sources tab -------------------------------------------------------------------------------

/** The question that replaces a connection's actions while Delete asks "Are you sure?" (drawn full width below the row). */
function deleteQuestion(c) {
  return `<div class="cn-confirm" role="group" aria-label="Delete ${esc(c.name)}?"><span>${esc(
    `Delete ${c.name}? Files already imported stay.`,
  )}</span><button type="button" class="btn danger confirm sm" data-dt-delete-yes="${esc(
    c.connection_id,
  )}">Delete</button><button type="button" class="btn quiet sm" data-dt-delete-no>Keep it</button></div>`;
}

function connectionActions(c) {
  const busy = state.busy[c.connection_id];
  return `<button type="button" class="btn secondary sm" data-dt-test="${esc(c.connection_id)}" aria-label="Test ${esc(c.name)}"${
    busy || !c.available ? " disabled" : ""
  }>${busy === "test" ? "Testing…" : "Test"}</button><a class="btn quiet sm" href="${esc(
    `#/connections/${c.connection_id}`,
  )}" data-dt-edit="${esc(c.connection_id)}" aria-label="Edit ${esc(c.name)}">Edit</a><button type="button" class="btn quiet sm" data-dt-delete="${esc(
    c.connection_id,
  )}" aria-label="Delete ${esc(c.name)}"${busy ? " disabled" : ""}>Delete</button>`;
}

function connectionRow(c) {
  const imported = lastImport(c);
  const addon = c.status === "needs_addon" ? `<span class="dt-sub">Ask your administrator to turn this on.</span>` : "";
  const unreadable =
    c.secrets_readable === false
      ? `<span class="dt-sub">The saved password or key can no longer be read. Edit the source and enter it again.</span>`
      : "";
  const confirming = state.confirming === c.connection_id;
  const detail = `${confirming ? deleteQuestion(c) : ""}${state.errors[c.connection_id] ? errorBox(state.errors[c.connection_id]) : ""}${stepsHtml(
    state.reports[c.connection_id],
  )}`;
  return `<li class="dt-row" data-dt-source="connection" data-dt-id="${esc(c.connection_id)}">
    <div><h3 class="dt-name">${esc(c.name)}<span class="dt-sub">${esc(c.kind_label)}</span></h3></div>
    <div class="dt-cell" data-dt-health>${statusBadge(c)}${addon}${unreadable}</div>
    <div class="dt-cell" data-dt-when>${imported ? esc(`Last import ${fmtStamp(imported)}`) : "Nothing imported yet"}</div>
    <div class="dt-actions">${confirming ? "" : connectionActions(c)}</div>${detail ? `<div class="dt-wide">${detail}</div>` : ""}
  </li>`;
}

function fileRow(u) {
  const from = u.from_connection_id ? connectionName(u.from_connection_id) : null;
  const kind = u.from_connection_id ? (from ? `Imported from ${from}` : "Imported from a saved source") : "Uploaded file";
  const sample = u.synthetic ? " · Sample data" : "";
  return `<li class="dt-row" data-dt-source="file" data-dt-id="${esc(u.upload_id)}">
    <div><h3 class="dt-name">${esc(u.file_name)}<span class="dt-sub">${esc(`${kind}${sample}`)}</span></h3></div>
    <div class="dt-cell" data-dt-health><span class="pill ok cn-badge" data-dt-status="read">Read correctly</span><span class="dt-sub">${esc(
      `${plural(u.row_count, "row", "rows")} · ${plural(u.column_count, "column", "columns")} · ${fmtSize(u.file_size_bytes)}`,
    )}</span></div>
    <div class="dt-cell" data-dt-when>${esc(`${u.from_connection_id ? "Imported" : "Uploaded"} ${fmtStamp(u.created_at)}`)}</div>
    <div class="dt-actions"></div>
  </li>`;
}

function chooserHtml() {
  const kinds = state.kinds.filter((k) => k.creatable);
  const cards = kinds
    .map((k) => {
      const used = state.connections.some((c) => c.kind === k.kind);
      const action = k.available
        ? `<div class="btn-row"><a class="btn secondary sm" href="${esc(`#/connections/new/${k.kind}`)}" data-dt-new="${esc(k.kind)}">${
            used ? "Set up another" : "Set up"
          }</a></div>`
        : `<p class="dt-note">Needs an add-on. Ask your administrator to install it.</p>`;
      return `<article class="dt-kind" data-dt-kind="${esc(k.kind)}"><h3>${esc(k.label)}</h3>${action}</article>`;
    })
    .join("");
  const file = `<article class="dt-kind" data-dt-kind="file"><h3>A file from your computer</h3><p class="dt-note">CSV, Excel or Parquet. Data Doctor reads it first and says what it can be used for.</p><div class="btn-row"><a class="btn secondary sm" href="${DOCTOR_HREF}" data-dt-new="file">Choose a file</a></div></article>`;
  return `<section class="dt-chooser" id="dt-chooser" aria-labelledby="dt-chooser-h"${state.chooserOpen ? "" : " hidden"}><h2 id="dt-chooser-h">Add a source</h2><p>Marketing AI only reads from a source. It never changes anything there.</p><div class="dt-kinds">${cards}${file}</div></section>`;
}

function sourcesHtml() {
  if (state.loadError) return errorBox(state.loadError, { retry: true });
  const connections = state.connections.map(connectionRow).join("");
  const shown = state.showAllFiles ? state.uploads : state.uploads.slice(0, FILES_SHOWN);
  const files = shown.map(fileRow).join("");
  const more =
    state.uploads.length > shown.length
      ? `<div class="dt-more"><button type="button" class="btn quiet sm" data-dt-show-all>${esc(`Show all ${fmtInt(state.uploads.length)} files`)}</button></div>`
      : "";
  const list =
    connections || files
      ? `<ul class="dt-list" aria-label="Your data sources">${connections}${files}</ul>${more}`
      : emptyState({ title: "No data sources yet", text: "Add a source or choose a file to get started." });
  const problem = state.uploadsError
    ? errorBox(state.uploadsError, { title: "The uploaded files could not be listed." })
    : "";
  return `${chooserHtml()}<section class="dt-sec" aria-labelledby="dt-sources-h"><h2 id="dt-sources-h">Your sources</h2><p>Saved connections and uploaded files, with their health and when they last brought in data.</p>${list}${problem}</section>
    <p class="dt-foot">The AI service has its own place under <a href="#/settings" data-dt-settings>Settings</a>.</p>`;
}

// --- the Check my data tab -------------------------------------------------------------------------

function checkHtml() {
  return `<section class="dt-check" aria-labelledby="dt-check-h"><h2 id="dt-check-h">Check my data</h2>
    <p>Data Doctor looks at your files before you use them. It never changes them.</p>
    <ul>
      <li>It says what each file is and what it can be used for.</li>
      <li>It says what is missing, so you know what to ask your data team for.</li>
      <li>It shows how several files fit together.</li>
    </ul>
    <p>Choose your files there, or open it from here at any time.</p></section>`;
}

// --- drawing and behaviour -------------------------------------------------------------------------

function draw() {
  const { app, tab } = view;
  if (!app) return;
  injectDataStyles();
  injectConnectionStyles();
  app.innerHTML = `<main class="screen dt" data-dt-tab-shown="${tab}">${header(tab)}${tabs(tab)}${
    tab === "check" ? checkHtml() : sourcesHtml()
  }</main>`;
  bind(app);
  document.title = `${tab === "check" ? "Check my data" : "Data"} · Marketing AI`;
}

async function testFromList(id) {
  state.busy[id] = "test";
  delete state.errors[id];
  draw();
  try {
    const answer = await testConnection(id);
    const at = state.connections.findIndex((c) => c.connection_id === id);
    if (at >= 0) state.connections[at] = answer.connection;
    state.reports[id] = answer.report;
    announceStatus(answer.report.ok ? "Connected" : "The test found a problem");
  } catch (error) {
    state.errors[id] = error;
  }
  delete state.busy[id];
  draw();
}

async function remove(id) {
  state.busy[id] = "delete";
  state.confirming = null;
  draw();
  try {
    await deleteConnection(id);
    state.connections = state.connections.filter((c) => c.connection_id !== id);
    delete state.reports[id];
    announceStatus("Source deleted");
  } catch (error) {
    state.errors[id] = error;
  }
  delete state.busy[id];
  draw();
}

function bind(app) {
  const add = app.querySelector("#dt-add");
  if (add) {
    add.addEventListener("click", () => {
      state.chooserOpen = !state.chooserOpen;
      const chooser = app.querySelector("#dt-chooser");
      if (chooser) chooser.hidden = !state.chooserOpen;
      add.setAttribute("aria-expanded", String(state.chooserOpen));
    });
  }
  app.querySelectorAll("[data-dt-test]").forEach((b) => b.addEventListener("click", () => testFromList(b.dataset.dtTest)));
  app.querySelectorAll("[data-dt-delete]").forEach((b) =>
    b.addEventListener("click", () => {
      state.confirming = b.dataset.dtDelete;
      draw();
    }),
  );
  app.querySelectorAll("[data-dt-delete-yes]").forEach((b) => b.addEventListener("click", () => remove(b.dataset.dtDeleteYes)));
  app.querySelectorAll("[data-dt-delete-no]").forEach((b) =>
    b.addEventListener("click", () => {
      state.confirming = null;
      draw();
    }),
  );
  const all = app.querySelector("[data-dt-show-all]");
  if (all) {
    all.addEventListener("click", () => {
      state.showAllFiles = true;
      draw();
    });
  }
}

// --- the route -------------------------------------------------------------------------------------

/** The tab a hash names: `#/data/check` is "check"; `#/data`, `#/data/sources` and anything else is "sources". */
export const tabOf = (parts) => (parts[1] === "check" ? "check" : "sources");

/** `registerModule`'s render for `#/data[/<tab>]`: paint a skeleton, load, draw. */
export async function renderData(app, parts) {
  const tab = tabOf(parts);
  view = { app, tab };
  injectDataStyles();
  if (tab === "check") {
    draw();
    return;
  }
  app.innerHTML = skeleton("list", { title: "Data" });
  state.loadError = null;
  state.uploadsError = null;
  state.confirming = null;
  state.showAllFiles = false;
  const [sources, files] = await Promise.allSettled([
    Promise.all([getKinds(), getConnections()]),
    getUploads(),
  ]);
  if (sources.status === "fulfilled") {
    state.kinds = sources.value[0].kinds || [];
    state.connections = sources.value[1].connections || [];
  } else {
    state.loadError = sources.reason;
  }
  if (files.status === "fulfilled") {
    state.uploads = files.value.uploads || [];
  } else {
    state.uploads = [];
    state.uploadsError = files.reason;
  }
  if (view.app !== app || view.tab !== tab) return; // the person moved on
  draw();
}
