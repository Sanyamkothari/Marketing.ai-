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

import { button, emptyState, icon, pageHeader, statusChip, tabs } from "../../components.js";
import { announceStatus, errorBox, esc, fmtInt, fmtSize, fmtStamp, skeleton } from "../../dom.js";
import { deleteConnection, getConnections, getKinds, testConnection } from "../connections/api.js";
import { stepsHtml } from "../connections/page.js";
import { revealSelectedTab } from "../reports/page.js";
import { injectConnectionStyles } from "../connections/styles.js";
import { userText } from "../../labels.js";
import { getUploads } from "./api.js";
import { injectDataStyles } from "./styles.js";
// Plan M M192 (DEC-1622): the note under the sources opens Settings › AI service itself, not Settings (which used to
// send the AI service back to Connections, which the campaigns layout redirects here: a loop).
import { AI_SETTINGS_HREF, aiServiceStatus } from "../../availability.js";
// Plan O M247 (DEC-1817): with `data_doctor.unified` on, "Check my data" is the one Data Doctor screen itself, drawn
// in the tab (a component that draws no H1 of its own), and the header's link goes to the tab. Off: unchanged.
import { dataDoctorOn, dataDoctorOnNow } from "../datadoctor/switch.js";
import { mountDataDoctor } from "../datadoctor/screen.js";

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
};

let view = { app: null, tab: "sources", hash: "", doctor: false };

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
      return { kind: "Built by Guided setup from another file", verb: "Built" };
    default:
      return { kind: "File", verb: "Added" };
  }
}

/** The header: "+ Add a source" is the Sources tab's one primary action; the Check tab keeps its one action in its card. */
function header(tab) {
  const subtitle = "Where your customer data comes from, and whether it is ready to use.";
  if (tab !== "sources") return pageHeader({ title: "Data", subtitle });
  return pageHeader({
    title: "Data",
    subtitle,
    primary: { label: "+ Add a source", id: "dt-add", attrs: `aria-expanded="${state.chooserOpen}" aria-controls="dt-chooser"` },
    secondary: [
      dataDoctorOnNow()
        ? { label: "Data Doctor", href: `${DATA_HOME}/check`, attrs: "data-dt-doctor" }
        : { label: "Check my data", href: DOCTOR_HREF, attrs: "data-dt-doctor" },
    ],
  });
}

/** The underline tabs: a link to each route; the current one is also `aria-current="page"`, as a link to a page is. */
function tabsHtml(tab) {
  // With the one Data Doctor on, the second tab is called what it is (the `check` id and address stay).
  return tabs({
    label: "Data sections",
    current: tab,
    items: TABS.map((t) => ({
      id: t.id,
      label: t.id === "check" && (view.doctor || dataDoctorOnNow()) ? "Data Doctor" : t.label,
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
  // With the one Data Doctor on, a file or a pick from a connection is brought in on its tab; off, the old page.
  const doctor = dataDoctorOnNow();
  const file = `<article class="dt-kind" data-dt-kind="file"><div class="dt-kind-h">${typeIcon("file")}<h3>A file from your computer</h3></div><p class="dt-note">CSV, Excel or Parquet. Data Doctor reads it first and says what it can be used for.</p><div class="btn-row">${button(
    { label: "Choose a file", href: doctor ? `${DATA_HOME}/check` : DOCTOR_HREF, kind: "secondary", size: "sm", attrs: 'data-dt-new="file"' },
  )}</div></article>`;
  const pick =
    doctor && state.connections.some((c) => c.available)
      ? `<article class="dt-kind" data-dt-kind="pick"><div class="dt-kind-h">${typeIcon("connection")}<h3>Pick from a connection</h3></div><p class="dt-note">Choose a file or table in a source you have set up. Data Doctor reads it first.</p><div class="btn-row">${button(
          { label: "Pick from a connection", href: `${DATA_HOME}/check/pick`, kind: "secondary", size: "sm", attrs: 'data-dt-new="pick"' },
        )}</div></article>`
      : "";
  return `<section class="dt-chooser" id="dt-chooser" aria-labelledby="dt-chooser-h"${state.chooserOpen ? "" : " hidden"}><h2 id="dt-chooser-h">Add a source</h2><p>Marketing AI only reads from a source. It never changes anything there.</p><div class="dt-kinds">${cards}${file}${pick}</div></section>`;
}

function sourcesHtml() {
  if (state.loadError) return errorBox(state.loadError, { retry: true });
  const connections = state.connections.map(connectionRow).join("");
  const shown = state.showAllFiles ? state.uploads : state.uploads.slice(0, FILES_SHOWN);
  const files = shown.map(fileRow).join("");
  const hidden = state.uploads.length > shown.length;
  // The list holds the newest files the API sent. When there are more files than that, the button must not say "all".
  const showAll = hidden
    ? button({
        label: filesCut() ? `Show the newest ${fmtInt(state.uploads.length)} files` : `Show all ${fmtInt(state.uploads.length)} files`,
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
  const more = showAll || cut ? `<div class="dt-more">${showAll}${cut}</div>` : "";
  const list =
    connections || files
      ? `<ul class="dt-list" aria-label="Your data sources">${connections}${files}</ul>${more}`
      : `<div class="card">${emptyState({
          icon: "data",
          title: "No data sources yet",
          text: "Add a source or choose a file to get started.",
          // The one way forward is the header's: this opens the same "Add a source" chooser (M188 review).
          action: { label: "Add a source", kind: "secondary", attrs: 'data-dt-open-chooser aria-controls="dt-chooser"' },
        })}</div>`;
  const problem = state.uploadsError
    ? errorBox(state.uploadsError, { title: "The uploaded files could not be listed." })
    : "";
  return `${aiStatusHtml()}${chooserHtml()}<section class="dt-sec" aria-labelledby="dt-sources-h"><h2 id="dt-sources-h">Your sources</h2><p>Saved connections and uploaded files, with their health and when they last brought in data.</p>${list}${problem}</section>
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

// --- the Check my data tab -------------------------------------------------------------------------

/** Plan O M247: the tab holds the Data Doctor screen: the case on the left, the chat on the right. */
const DOCTOR_SLOTS = `<section class="dt-doctor" data-dt-doctor-screen aria-label="Data Doctor"><div class="dds-layout"><div class="dds-main" data-dds-main></div><aside class="dds-aside" data-dds-aside></aside></div></section>`;

function checkHtml() {
  if (view.doctor) return DOCTOR_SLOTS;
  const point = (text) => `<li>${icon("checkCircle", { size: 18 })}<span>${esc(text)}</span></li>`;
  return `<section class="dt-check card" aria-labelledby="dt-check-h"><div class="dt-check-h"><span class="dt-ico lg">${icon("checkCircle", { size: 24 })}</span><div><h2 id="dt-check-h">Check my data</h2>
    <p>Data Doctor looks at your files before you use them. It never changes them.</p></div></div>
    <ul class="dt-points">${point("It says what each file is and what it can be used for.")}${point("It says what is missing, so you know what to ask your data team for.")}${point("It shows how several files fit together.")}</ul>
    <p class="dt-after">Choose your files there, or open it from here at any time.</p>
    <div class="dt-go">${button({ label: "Open Data Doctor", href: DOCTOR_HREF })}</div></section>`;
}

// --- drawing and behaviour -------------------------------------------------------------------------

function draw() {
  const { app, tab } = view;
  if (!app || !stillHere()) return;
  injectDataStyles();
  injectConnectionStyles();
  app.innerHTML = `<main class="screen dt" data-dt-tab-shown="${tab}">${header(tab)}${tabsHtml(tab)}${
    tab === "check" ? checkHtml() : sourcesHtml()
  }</main>`;
  bind(app);
  if (tab === "check" && view.doctor) {
    const main = app.querySelector("[data-dds-main]");
    const aside = app.querySelector("[data-dds-aside]");
    if (main) mountDataDoctor({ main, aside }, { place: "data", pick: view.pick });
  }
  revealSelectedTab(app);
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
  const opener = app.querySelector("[data-dt-open-chooser]");
  if (opener) {
    opener.addEventListener("click", () => {
      state.chooserOpen = true;
      const chooser = app.querySelector("#dt-chooser");
      if (chooser) chooser.hidden = false;
      if (add) add.setAttribute("aria-expanded", "true");
      const first = chooser && chooser.querySelector("a, button");
      if (first) first.focus();
    });
  }
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
  view = { app, tab, hash: window.location.hash, doctor: false, pick: parts[2] === "pick" };
  injectDataStyles();
  if (tab === "check") {
    // Plan O M247: only this tab waits for `/auth/me` (the call the app already made at start-up).
    const doctor = await dataDoctorOn();
    if (view.app !== app || view.tab !== tab) return;
    view.doctor = doctor;
    draw();
    return;
  }
  app.innerHTML = skeleton("list", { title: "Data" });
  state.loadError = null;
  state.uploadsError = null;
  state.confirming = null;
  state.showAllFiles = false;
  const [sources, files, ai] = await Promise.allSettled([
    Promise.all([getKinds(), getConnections()]),
    getUploads(),
    aiServiceStatus(),
  ]);
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
