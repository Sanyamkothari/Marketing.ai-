// The Data page (Plan M, M179, DEC-1609): `#/data[/<tab>]`.
//
//   #/data          Sources: the saved connections and the uploaded files in one list, each with its health and when
//                   it was last refreshed or uploaded. A connection keeps Test, Edit and Delete, and "+ Add a source"
//                   opens the kinds of source to set up.
//   #/data/check    Data Doctor: the one Data Doctor screen itself (`#/data/check/pick` opens it on "Pick from a
//                   connection").
//
// Nothing here is new engine behaviour. The list, the test and the delete are the Connections module's own calls
// (`../connections/api.js`) and its own badge and test report (`statusBadge`, `stepsHtml`); the set-up and edit forms
// stay at `#/connections/new/<kind>` and `#/connections/<id>`; `#/doctor` redirects to the Data Doctor tab. The one
// addition is the list of uploaded files, `GET /uploads`.
//
// The Data Doctor tab draws the Data Doctor component itself (it draws no H1 of its own).
//
// This page does not import `ui/layout.js`: it draws the same in the classic and the campaigns layout.

import { button, emptyState, icon, pageHeader, statusChip, tabs } from "../../components.js";
import { announceStatus, errorBox, safeDecode, esc, fmtInt, fmtSize, fmtStamp, skeleton } from "../../dom.js";
import { deleteConnection, getConnections, getKinds, testConnection } from "../connections/api.js";
import { stepsHtml } from "../connections/page.js";
import { revealSelectedTab } from "../reports/page.js";
import { injectConnectionStyles } from "../connections/styles.js";
import { userText } from "../../labels.js";
import { getUploads } from "./api.js";
import { getCases } from "../datadoctor/api.js";
import { injectDataStyles } from "./styles.js";
// Plan M M192 (DEC-1622): the note under the sources opens Settings › AI service itself, not Settings (which used to
// send the AI service back to Connections, which the campaigns layout redirects here: a loop).
import { AI_SETTINGS_HREF, aiServiceStatus } from "../../availability.js";
// Plan O M247 (DEC-1817): the Data Doctor tab is the one Data Doctor screen itself, drawn in the tab (a component that
// draws no H1 of its own).
import { mountDataDoctor } from "../datadoctor/screen.js";
import { TABLE_FILE_TYPES } from "../intake/notes.js";

export const DATA_HOME = "#/data";
export const TABS = Object.freeze([
  { id: "sources", label: "Sources", href: DATA_HOME },
  { id: "check", label: "Data Doctor", href: `${DATA_HOME}/check` },
]);
/** How many uploaded files the list shows before "Show all". */
export const FILES_SHOWN = 25;
/** How many Data Doctor checks (newest first, each holding its own uploads) the list shows before "Show older checks". */
export const CHECKS_SHOWN = 3;

const state = {
  kinds: [],
  connections: [],
  uploads: [],
  checks: [], // the Data Doctor checks (newest first): the files they hold are listed once, under the check
  ai: null, // whether the Product AI is connected: true | false | null (not known)
  uploadsTotal: 0, // how many uploaded files there are in all (the list holds only the newest)
  loadError: null,
  uploadsError: null,
  busy: {}, // connection id -> "test" | "delete"
  reports: {}, // connection id -> the last test report
  errors: {}, // connection id -> an error from a row action
  confirming: null, // the connection id whose Delete asks "Are you sure?"
  chooserOpen: false,
  showAllFiles: false,
  showOlderChecks: false,
};

let view = { app: null, tab: "sources", hash: "" };

/** The files chosen on "Connect your data": kept while the page goes to the Data Doctor tab, which reads them. */
let handedFiles = null;

/** True while the person is still on the screen this page drew: an answer that arrives after they left must not paint over it. */
const stillHere = () => window.location.hash === view.hash;

// --- small pieces ----------------------------------------------------------------------------------

const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;

const connectionName = (id) => {
  const found = state.connections.find((c) => c.connection_id === id);
  return found ? found.name : null;
};

/** When a file last came in through this connection: the newest file imported or pulled from it, among the files listed. */
function lastImport(connection) {
  const latest = state.uploads.find((u) => u.from_connection_id === connection.connection_id);
  return latest ? latest.created_at : null;
}

/** The files the list does not hold (older than the newest ones, or not listed at all). */
const filesCut = () => state.uploadsTotal > state.uploads.length;

/**
 * What the "last import" cell says. It claims only what the file list shows: a file imported from here, or that no
 * file is listed as coming from here. Data can also reach a source without a file (Setup's "add from a connection",
 * the scheduled refresh), so the empty answer is "No file imported yet", never "nothing came in".
 */
function whenText(connection) {
  const imported = lastImport(connection);
  if (imported) return `Last import ${fmtStamp(imported)}`;
  if (state.uploadsError) return "";
  if (filesCut()) return `No import in the newest ${fmtInt(state.uploads.length)} files`;
  return "No file imported yet";
}

/** How an uploaded file came to be: its second line (HTML: a source's name is the person's, `userText`) and the word
    before its date. */
function fileOrigin(u) {
  const from = u.from_connection_id ? connectionName(u.from_connection_id) : null;
  switch (u.made_by) {
    case "import":
      return { kind: from ? `Imported from ${userText(from)}` : "Imported from a saved source", verb: "Imported" };
    case "pull":
      return { kind: from ? `Pulled from ${userText(from)}` : "Pulled from a saved source", verb: "Pulled" };
    case "renamed_copy":
      return { kind: "Copy with renamed columns. The file you sent is unchanged.", verb: "Made" };
    case "recipe":
      return { kind: "Built from another file by the steps you approved", verb: "Built" };
    default:
      return { kind: "File", verb: "Added" };
  }
}

/** The header: the page's one action is "Connect your data" below, so the header carries no button. */
function header() {
  return pageHeader({ title: "Data", subtitle: "Where your customer data comes from, and whether it is ready to use." });
}

/** The underline tabs: a link to each route; the current one is also `aria-current="page"`, as a link to a page is. */
function tabsHtml(tab) {
  // The second tab keeps the `check` id and address.
  return tabs({
    label: "Data sections",
    current: tab,
    items: TABS.map((t) => ({
      id: t.id,
      label: t.label,
      href: t.href,
      attrs: `data-dt-tab="${t.id}"${t.id === tab ? ' aria-current="page"' : ""}`,
    })),
  });
}

/** What kind of source a row is, drawn: a document for a file, a database for a saved connection (the words are beside it). */
const FILE_ICON =
  '<svg class="ds-ico" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" focusable="false" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
const typeIcon = (kind) => `<span class="dt-ico" data-dt-type="${kind}">${kind === "file" ? FILE_ICON : icon("data", { size: 20 })}</span>`;

/**
 * A saved connection's health: a short status chip (an icon and one or two words) and, on a line under it, the rest of
 * what the Connections page says - when it was tested, or the server's reason it failed - in the same order and words
 * (M188 review: a long sentence inside a pill wraps into a blob).
 */
function connectionChip(c) {
  const status = ["connected", "failed", "needs_addon"].includes(c.status) ? c.status : "not_tested";
  const attrs = `data-cn-status="${status}"`;
  const sub = (text) => (text ? `<span class="dt-sub" data-dt-health-detail>${esc(text)}</span>` : "");
  if (status === "connected") {
    return `${statusChip("health", "connected", { attrs })}${sub(c.last_test ? `Tested ${fmtStamp(c.last_test.tested_at)}` : "")}`;
  }
  if (status === "failed") return `${statusChip("health", "failed", { attrs })}${sub(c.failure || "See the test.")}`;
  // `needs_addon` is the mapping's own neutral status (added to components.js STATUS_MAP.health at integration).
  if (status === "needs_addon") return statusChip("health", "needs_addon", { word: "Needs the add-on", attrs });
  return statusChip("health", "not_tested", { attrs });
}

// --- the Sources tab -------------------------------------------------------------------------------

/** The question that replaces a connection's actions while Delete asks "Are you sure?" (drawn full width below the row). */
function deleteQuestion(c) {
  return `<div class="cn-confirm" role="group" aria-label="Delete ${esc(c.name)}?"><span>Delete ${userText(c.name)}? Files already imported stay.</span><button type="button" class="btn danger confirm sm" data-dt-delete-yes="${esc(
    c.connection_id,
  )}">Delete</button><button type="button" class="btn quiet sm" data-dt-delete-no>Keep it</button></div>`;
}

function connectionActions(c) {
  const busy = state.busy[c.connection_id];
  const id = esc(c.connection_id);
  return `${button({
    label: busy === "test" ? "Testing…" : "Test",
    kind: "secondary",
    size: "sm",
    attrs: `data-dt-test="${id}" aria-label="Test ${esc(c.name)}"`,
    disabled: Boolean(busy || !c.available),
  })}${button({ label: "Edit", href: `#/connections/${c.connection_id}`, kind: "tertiary", size: "sm", attrs: `data-dt-edit="${id}" aria-label="Edit ${esc(c.name)}"` })}${button({
    label: "Delete",
    kind: "tertiary",
    size: "sm",
    attrs: `data-dt-delete="${id}" aria-label="Delete ${esc(c.name)}"`,
    disabled: Boolean(busy),
  })}`;
}

function connectionRow(c) {
  const when = whenText(c);
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
    ${typeIcon("connection")}<div><h3 class="dt-name">${userText(c.name)}<span class="dt-sub">${esc(c.kind_label)}</span></h3></div>
    <div class="dt-cell" data-dt-health>${connectionChip(c)}${addon}${unreadable}</div>
    <div class="dt-cell" data-dt-when>${esc(when)}</div>
    <div class="dt-actions">${confirming ? "" : connectionActions(c)}</div>${detail ? `<div class="dt-wide">${detail}</div>` : ""}
  </li>`;
}

function fileRow(u) {
  const { kind, verb } = fileOrigin(u);
  const sample = u.synthetic ? " · Sample data" : "";
  return `<li class="dt-row" data-dt-source="file" data-dt-id="${esc(u.upload_id)}">
    ${typeIcon("file")}<div><h3 class="dt-name">${userText(u.file_name)}<span class="dt-sub">${kind}${esc(sample)}</span></h3></div>
    <div class="dt-cell" data-dt-health>${statusChip("health", "read", { attrs: 'data-dt-status="read"' })}<span class="dt-sub">${esc(
      `${plural(u.row_count, "row", "rows")} · ${plural(u.column_count, "column", "columns")} · ${fmtSize(u.file_size_bytes)}`,
    )}</span></div>
    <div class="dt-cell" data-dt-when>${esc(`${verb} ${fmtStamp(u.created_at)}`)}</div>
    <div class="dt-actions"></div>
  </li>`;
}

/** The uploads a Data Doctor check holds, and the prepared copies made from them: they are listed under the check,
 *  not a second time as files. */
const inChecks = () => {
  const held = new Set(state.checks.flatMap((c) => c.upload_ids || []));
  for (let grew = true; grew; ) {
    grew = false;
    for (const u of state.uploads) {
      if (u.source_upload_id && held.has(u.source_upload_id) && !held.has(u.upload_id)) {
        held.add(u.upload_id);
        grew = true;
      }
    }
  }
  return held;
};

/** One row for a check: its files by name, and the way back into it. */
function checkRow(c) {
  const names = (c.files || []).map((n) => userText(n)).join(", ");
  return `<li class="dt-row" data-dt-source="check" data-dt-id="${esc(c.case_id)}">
    ${typeIcon("file")}<div><h3 class="dt-name">${userText(c.title || "Data check")}<span class="dt-sub">${esc(
      `Data Doctor check · ${plural((c.files || []).length, "file", "files")}`,
    )}: ${names}</span></h3></div>
    <div class="dt-cell" data-dt-health><span class="dt-sub">In a Data Doctor check</span></div>
    <div class="dt-cell" data-dt-when>${esc(c.updated_at ? `Changed ${fmtStamp(c.updated_at)}` : "")}</div>
    <div class="dt-actions">${button({ label: "Open the check", href: `${DATA_HOME}/check/case/${encodeURIComponent(c.case_id)}`, kind: "secondary", size: "sm", attrs: `aria-label="Open the check ${esc(c.title || "")}"` })}</div>
  </li>`;
}

function chooserHtml() {
  const kinds = state.kinds.filter((k) => k.creatable);
  const cards = kinds
    .map((k) => {
      const used = state.connections.some((c) => c.kind === k.kind);
      const action = k.available
        ? `<div class="btn-row">${button({
            label: used ? "Set up another" : "Set up",
            href: `#/connections/new/${k.kind}`,
            kind: "secondary",
            size: "sm",
            attrs: `data-dt-new="${esc(k.kind)}"`,
          })}</div>`
        : `<p class="dt-note">Needs an add-on. Ask your administrator to install it.</p>`;
      return `<article class="dt-kind" data-dt-kind="${esc(k.kind)}"><div class="dt-kind-h">${typeIcon("connection")}<h3>${esc(k.label)}</h3></div>${action}</article>`;
    })
    .join("");
  // A file or a pick from a connection is brought in on the Data Doctor tab.
  const pick = state.connections.some((c) => c.available)
    ? `<article class="dt-kind" data-dt-kind="pick"><div class="dt-kind-h">${typeIcon("connection")}<h3>Pick from a connection</h3></div><p class="dt-note">Choose a file or table in a source you have set up. Data Doctor reads it first.</p><div class="btn-row">${button(
        { label: "Pick from a connection", href: `${DATA_HOME}/check/pick`, kind: "secondary", size: "sm", attrs: 'data-dt-new="pick"' },
      )}</div></article>`
    : "";
  return `<section class="dt-chooser" id="dt-chooser" aria-labelledby="dt-chooser-h"${state.chooserOpen ? "" : " hidden"}><h2 id="dt-chooser-h">Other sources</h2><p>Marketing AI only reads from a source. It never changes anything there.</p><div class="dt-kinds">${cards}${pick}</div></section>`;
}

/**
 * The page starts with one action. Files are chosen here (several at once) and go straight
 * into the Data Doctor; the other kinds of source are a quiet link that opens the chooser below.
 */
function connectHtml() {
  const other = state.kinds.some((k) => k.creatable) || state.connections.some((c) => c.available);
  return `<section class="card dt-connect" data-dt-connect aria-labelledby="dt-connect-h"><h2 id="dt-connect-h">Connect your data</h2>
    <p>Choose your customer files, CSV, Excel or Parquet, several at once if you have them. Data Doctor reads them, asks only what it cannot tell, and gets them ready. It never changes your files.</p>
    <div class="btn-row"><label class="btn primary dt-files-label">${icon("upload")}<span>Connect your data</span><input type="file" class="sr" id="dt-files" multiple accept="${esc(
      TABLE_FILE_TYPES,
    )}" data-dt-files></label></div>${
      other
        ? `<p class="dt-other">${button({
            label: "Use a database, cloud storage or a saved connection instead",
            kind: "tertiary",
            size: "sm",
            attrs: `data-dt-open-chooser aria-controls="dt-chooser" aria-expanded="${state.chooserOpen}"`,
          })}</p>`
        : ""
    }</section>`;
}

function sourcesHtml() {
  if (state.loadError) return errorBox(state.loadError, { retry: true });
  const connections = state.connections.map(connectionRow).join("");
  const held = inChecks();
  // Uploads are grouped under the check they belong to (one row per check, newest first); the older checks are folded.
  const allChecks = state.checks.filter((c) => (c.files || []).length);
  const shownChecks = state.showOlderChecks ? allChecks : allChecks.slice(0, CHECKS_SHOWN);
  const checks = shownChecks.map(checkRow).join("");
  const olderChecks =
    allChecks.length > shownChecks.length
      ? button({
          label: `Show ${fmtInt(allChecks.length - shownChecks.length)} older ${allChecks.length - shownChecks.length === 1 ? "check" : "checks"}`,
          kind: "tertiary",
          size: "sm",
          attrs: "data-dt-show-older",
        })
      : "";
  const loose = state.uploads.filter((u) => !held.has(u.upload_id));
  const shown = state.showAllFiles ? loose : loose.slice(0, FILES_SHOWN);
  const files = checks + shown.map(fileRow).join("");
  const hidden = loose.length > shown.length;
  // The list holds the newest files the API sent. When there are more files than that, the button must not say "all".
  const showAll = hidden
    ? button({
        label: filesCut() ? `Show the newest ${fmtInt(loose.length)} files` : `Show all ${fmtInt(loose.length)} files`,
        kind: "tertiary",
        size: "sm",
        attrs: "data-dt-show-all",
      })
    : "";
  const cut =
    !hidden && filesCut()
      ? `<span class="dt-sub" data-dt-cut>${esc(
          `Showing the newest ${fmtInt(state.uploads.length)} of ${fmtInt(state.uploadsTotal)} files.`,
        )}</span>`
      : "";
  const more = olderChecks || showAll || cut ? `<div class="dt-more">${olderChecks}${showAll}${cut}</div>` : "";
  const list =
    connections || files
      ? `<ul class="dt-list" aria-label="Your data sources">${connections}${files}</ul>${more}`
      : `<div class="card">${emptyState({
          icon: "data",
          title: "No data sources yet",
          text: "Connect your data above to get started.",
        })}</div>`;
  const problem = state.uploadsError
    ? errorBox(state.uploadsError, { title: "The uploaded files could not be listed." })
    : "";
  return `${aiStatusHtml()}${connectHtml()}${chooserHtml()}<section class="dt-sec" aria-labelledby="dt-sources-h"><h2 id="dt-sources-h">Your sources</h2><p>Saved connections and uploaded files, with their health and when they last brought in data.</p>${list}${problem}</section>
    <p class="dt-foot">Connect an AI service in <a href="${AI_SETTINGS_HREF}" data-dt-settings>Settings › AI service</a>.</p>`;
}

/** One line: whether the AI that helps your team (Data Doctor's chat) is connected, and where to connect it. */
function aiStatusHtml() {
  if (state.ai === null) return "";
  return `<p class="dt-ai" data-dt-ai="${state.ai ? "connected" : "not-connected"}">${
    state.ai
      ? "Product AI connected."
      : `Product AI not connected. <a href="${AI_SETTINGS_HREF}" data-dt-ai-link>Connect it in Settings</a>.`
  }</p>`;
}

// --- the Data Doctor tab ---------------------------------------------------------------------------

/** Plan O M247: the tab holds the Data Doctor screen: the case on the left, the chat on the right. */
const DOCTOR_SLOTS = `<section class="dt-doctor" data-dt-doctor-screen aria-label="Data Doctor"><div class="dds-layout"><div class="dds-main" data-dds-main></div><aside class="dds-aside" data-dds-aside></aside></div></section>`;

const checkHtml = () => DOCTOR_SLOTS;

// --- drawing and behaviour -------------------------------------------------------------------------

function draw() {
  const { app, tab } = view;
  if (!app || !stillHere()) return;
  injectDataStyles();
  injectConnectionStyles();
  app.innerHTML = `<main class="screen dt" data-dt-tab-shown="${tab}">${header()}${tabsHtml(tab)}${
    tab === "check" ? checkHtml() : sourcesHtml()
  }</main>`;
  bind(app);
  if (tab === "check") {
    const main = app.querySelector("[data-dds-main]");
    const aside = app.querySelector("[data-dds-aside]");
    if (main) mountDataDoctor({ main, aside }, { place: "data", pick: view.pick, caseId: view.caseId, files: view.files });
    view.pick = false; // `#/data/check/pick` opens the picker once, not on every redraw
    view.files = null; // the files are read once, not on every redraw
  }
  revealSelectedTab(app);
  document.title = `${tab === "check" ? "Data Doctor" : "Data"} · Marketing AI`;
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
  const opener = app.querySelector("[data-dt-open-chooser]");
  if (opener) {
    opener.addEventListener("click", () => {
      state.chooserOpen = true;
      const chooser = app.querySelector("#dt-chooser");
      if (chooser) chooser.hidden = false;
      opener.setAttribute("aria-expanded", "true");
      const first = chooser && chooser.querySelector("a, button");
      if (first) first.focus();
    });
  }
  const files = app.querySelector("[data-dt-files]");
  if (files) {
    files.addEventListener("change", () => {
      if (!files.files || !files.files.length) return;
      handedFiles = [...files.files];
      window.location.hash = `${DATA_HOME}/check`; // the Data Doctor tab reads them
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
  const older = app.querySelector("[data-dt-show-older]");
  if (older) {
    older.addEventListener("click", () => {
      state.showOlderChecks = true;
      draw();
    });
  }
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
  // `#/data/check/case/<id>` opens the Data Doctor on that case ("What the Doctor did" from a finished campaign).
  const caseId = parts[2] === "case" && parts[3] ? safeDecode(parts[3]) : null;
  view = { app, tab, hash: window.location.hash, pick: parts[2] === "pick", caseId };
  injectDataStyles();
  if (tab === "check") {
    view.files = handedFiles;
    handedFiles = null;
    draw();
    return;
  }
  app.innerHTML = skeleton("list", { title: "Data" });
  state.loadError = null;
  state.uploadsError = null;
  state.confirming = null;
  state.showAllFiles = false;
  state.showOlderChecks = false;
  const [sources, files, ai, checks] = await Promise.allSettled([
    Promise.all([getKinds(), getConnections()]),
    getUploads(),
    aiServiceStatus(),
    getCases(),
  ]);
  // Without the list of checks (Data Doctor not answering) every file is listed on its own, as before.
  state.checks = checks.status === "fulfilled" ? checks.value.cases || [] : [];
  const product = ai.status === "fulfilled" && ai.value && ai.value.slots && ai.value.slots.product;
  state.ai = product ? Boolean(product.connected) : null;
  if (sources.status === "fulfilled") {
    state.kinds = sources.value[0].kinds || [];
    state.connections = sources.value[1].connections || [];
  } else {
    state.loadError = sources.reason;
  }
  if (files.status === "fulfilled") {
    state.uploads = files.value.uploads || [];
    state.uploadsTotal = Number.isInteger(files.value.total) ? files.value.total : state.uploads.length;
  } else {
    state.uploads = [];
    state.uploadsTotal = 0;
    state.uploadsError = files.reason;
  }
  if (view.app !== app || view.tab !== tab) return; // the person moved on
  draw(); // (which also stays quiet when the hash has changed meanwhile)
}
