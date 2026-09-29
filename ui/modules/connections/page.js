// The Connections page (Plan H M80): every place the data lives, in one list, set up and tested here.
//
//   #/connections              the saved connections (status, Test, Edit, Delete) and the services to add
//   #/connections/new/<kind>   the set-up form of one kind
//   #/connections/<id>         a saved connection's form, with its last test
//   #/connections/ai/<slot>    the AI service screens (`ai.js`): "product" and "deliverable"
//
// The list ends with the two AI service cards, drawn from `GET /ai-service` whatever the kinds say.
//
// Plain words, few fields: a form shows what nearly everyone needs and tucks ports, encryption and the
// like under "More options". A secret field is a password input that is never filled in - the API never
// sends a secret back, only which ones are saved - so editing says "Saved: leave blank to keep it".
// "Save and test" is the one primary button: saving always runs the test, and the test shows each step
// with what happened and, when it failed, exactly what to fix. Every string from the API is escaped.

import { announceStatus, crumbs, emptyState, errorBox, esc, fmtStamp, pageHead, skeleton } from "../../dom.js";
import {
  createConnection,
  deleteConnection,
  getAiService,
  getConnections,
  getKinds,
  testConnection,
  updateConnection,
} from "./api.js";
import { SLOTS, aiStatusBadge, renderAiService, slotHref } from "./ai.js";
import { injectConnectionStyles } from "./styles.js";

const LIST = "#/connections";

const state = {
  kinds: [],
  connections: [],
  ai: null,
  loadError: null,
  busy: {}, // id -> "test" | "delete"
  reports: {}, // id -> the last test report shown on the list
  errors: {}, // id -> an error from a list action
  confirming: null, // the id whose Delete asks "Are you sure?"
  form: null,
};

let view = { app: null, parts: [] };

// --- small pieces ----------------------------------------------------------------------------------

const kindOf = (kind) => state.kinds.find((k) => k.kind === kind) || null;

/** The badge a connection's card wears, in plain words. */
export function statusBadge(connection) {
  const status = connection.status;
  if (status === "connected") {
    const when = connection.last_test ? ` · tested ${fmtStamp(connection.last_test.tested_at)}` : "";
    return `<span class="pill ok cn-badge" data-cn-status="connected">${esc(`Connected${when}`)}</span>`;
  }
  if (status === "failed") {
    return `<span class="pill bad cn-badge" data-cn-status="failed">${esc(`Failed: ${connection.failure || "see the test"}`)}</span>`;
  }
  if (status === "needs_addon") {
    return `<span class="chip neutral cn-badge" data-cn-status="needs_addon">Needs the add-on</span>`;
  }
  return `<span class="pill warn cn-badge" data-cn-status="not_tested">Not tested yet</span>`;
}

const ICONS = { ok: "✓", failed: "✕", warning: "!", skipped: "–" };

/** A test report: one verdict line and one row per named step, with its fix when there is one. */
export function stepsHtml(report) {
  if (!report) return "";
  const warned = (report.steps || []).some((s) => s.status === "warning");
  const verdict = report.ok
    ? `<p class="cn-verdict ok" role="status">${esc(
        warned ? "Connected, with a safety tip below." : "Connected. Marketing AI can read from here.",
      )}</p>`
    : `<p class="cn-verdict bad" role="status">Not connected yet. Fix the first problem below, then test again.</p>`;
  const rows = (report.steps || [])
    .map(
      (s) =>
        `<li class="${esc(s.status)}" data-cn-step="${esc(s.name)}"><span class="cn-ico" aria-hidden="true">${esc(
          ICONS[s.status] || "·",
        )}</span><span><span class="cn-sl">${esc(s.label)}</span> <span class="sr">(${esc(s.status)})</span><div class="cn-sm">${esc(
          s.message,
        )}</div>${s.fix ? `<div class="cn-fix">${esc(s.fix)}</div>` : ""}</span></li>`,
    )
    .join("");
  return `${verdict}<ul class="cn-steps" data-cn-report>${rows}</ul>`;
}

// --- the list --------------------------------------------------------------------------------------

function connectionCard(c) {
  const busy = state.busy[c.connection_id];
  const confirming = state.confirming === c.connection_id;
  const actions = confirming
    ? `<div class="cn-confirm" role="group" aria-label="Delete ${esc(c.name)}?"><span>${esc(
        `Delete ${c.name}? Files already imported stay.`,
      )}</span><button type="button" class="btn danger confirm sm" data-cn-delete-yes="${esc(
        c.connection_id,
      )}">Delete</button><button type="button" class="btn quiet sm" data-cn-delete-no>Keep it</button></div>`
    : `<div class="btn-row"><button type="button" class="btn secondary sm" data-cn-test="${esc(c.connection_id)}"${
        busy || !c.available ? " disabled" : ""
      }>${busy === "test" ? "Testing…" : "Test connection"}</button><a class="btn quiet sm" href="${esc(
        `${LIST}/${c.connection_id}`,
      )}">Edit</a><button type="button" class="btn quiet sm" data-cn-delete="${esc(c.connection_id)}"${
        busy ? " disabled" : ""
      }>Delete</button></div>`;
  const kind = kindOf(c.kind);
  const addon =
    c.status === "needs_addon" && kind && kind.addon
      ? `<p class="cn-note">${esc(`Ask whoever runs Marketing AI to install it: pip install 'marketing-ai[${kind.addon}]'.`)}</p>`
      : "";
  const unreadable = c.secrets_readable === false
    ? `<p class="cn-note">The saved password or key can no longer be read. Edit the connection and enter it again.</p>`
    : "";
  return `<article class="cn-card" data-cn-connection="${esc(c.connection_id)}"><h3>${esc(c.name)}</h3><span class="cn-kind">${esc(
    c.kind_label,
  )}</span>${statusBadge(c)}${addon}${unreadable}${state.errors[c.connection_id] ? errorBox(state.errors[c.connection_id]) : ""}${stepsHtml(
    state.reports[c.connection_id],
  )}${actions}</article>`;
}

function kindCard(k) {
  const used = state.connections.some((c) => c.kind === k.kind);
  const badge = !k.available
    ? `<span class="chip neutral cn-badge" data-cn-status="needs_addon">Needs the add-on</span>`
    : used
      ? ""
      : `<span class="chip neutral cn-badge" data-cn-status="not_set_up">Not set up</span>`;
  const action = !k.available
    ? `<p class="cn-note">${esc(`Ask whoever runs Marketing AI to install it: pip install 'marketing-ai[${k.addon}]'.`)}</p>`
    : `<div class="btn-row"><a class="btn secondary sm" href="${esc(`${LIST}/new/${k.kind}`)}">${used ? "Set up another" : "Set up"}</a></div>`;
  return `<article class="cn-card" data-cn-kind="${esc(k.kind)}"><h3>${esc(k.label)}</h3>${badge}<p class="cn-text">${esc(
    k.description,
  )}</p>${action}</article>`;
}

/** One of the two AI service cards: its own badge and one button to its screen. */
function aiCard(slot) {
  const info = SLOTS[slot];
  const st = state.ai && state.ai.slots ? state.ai.slots[slot] : null;
  const note =
    st && st.source === "inherited"
      ? "Uses the Product AI unless you set its own."
      : st && st.source === "config"
        ? "Set in this installation's configuration."
        : "";
  const set = st && st.source && st.source !== "none";
  return `<article class="cn-card" data-cn-ai="${esc(slot)}"><h3>${esc(info.title)}</h3>${aiStatusBadge(st)}<p class="cn-text">${esc(
    info.blurb,
  )}</p>${note ? `<p class="cn-note">${esc(note)}</p>` : ""}<div class="btn-row"><a class="btn secondary sm" href="${esc(
    slotHref(slot),
  )}">${set ? "Change" : "Set up"}</a></div></article>`;
}

function listHtml() {
  const head = pageHead(
    `${crumbs([{ label: "Connections" }])}<h1 class="h1">Connections</h1><p class="desc">The places your data lives. Marketing AI only reads from them and never changes anything there.</p>`,
  );
  if (state.loadError) {
    return `<main class="screen cn">${head}${errorBox(state.loadError, { retry: true })}</main>`;
  }
  const saved = state.connections.length
    ? `<div class="cn-grid">${state.connections.map(connectionCard).join("")}</div>`
    : emptyState({
        title: "No connections yet",
        text: "Pick a service below to connect it. You can also upload files directly in each use case.",
      });
  const data = state.kinds.filter((k) => k.creatable);
  return `<main class="screen cn">${head}
    <section class="cn-sec" aria-labelledby="cn-yours"><h2 id="cn-yours">Your connections</h2>${saved}</section>
    <section class="cn-sec" aria-labelledby="cn-add"><h2 id="cn-add">Add a connection</h2><p>Files in cloud storage, or tables in a database.</p><div class="cn-grid">${data
      .map(kindCard)
      .join("")}</div></section>
    <section class="cn-sec" aria-labelledby="cn-ai"><h2 id="cn-ai">AI service</h2><p>Two settings: one that helps your team, one for what your customer gets. Neither trains or scores your models.</p><div class="cn-grid">${Object.keys(SLOTS)
      .map(aiCard)
      .join("")}</div></section>
  </main>`;
}

// --- the form --------------------------------------------------------------------------------------

function freshForm(kind, connection) {
  const values = {};
  for (const field of kind.fields || []) {
    if (field.secret) continue;
    const saved = connection && connection.config ? connection.config[field.name] : undefined;
    values[field.name] = saved !== undefined && saved !== null ? saved : field.default !== null && field.default !== undefined ? field.default : "";
  }
  return {
    kind: kind.kind,
    id: connection ? connection.connection_id : null,
    name: connection ? connection.name : kind.label,
    values,
    saved: connection ? connection.secrets_saved || [] : [],
    readable: connection ? connection.secrets_readable !== false : true,
    report: connection ? connection.last_test : null,
    busy: "",
    error: null,
  };
}

function fieldHtml(field, form) {
  const id = `cn-f-${field.name}`;
  const help = field.help ? `<span class="sub">${esc(field.help)}</span>` : "";
  const need = field.required ? "" : ` <span class="sub">(optional)</span>`;
  const disabled = form.busy ? " disabled" : "";
  if (field.type === "checkbox") {
    return `<label class="cn-check"><input type="checkbox" id="${id}" data-cn-field="${esc(field.name)}"${
      form.values[field.name] === true ? " checked" : ""
    }${disabled}><span>${esc(field.label)}</span></label>${help}`;
  }
  let control;
  if (field.secret) {
    const kept = form.saved.includes(field.name) && form.readable;
    const placeholder = kept ? "Saved. Leave blank to keep it." : field.placeholder || "";
    control =
      field.type === "textarea"
        ? `<div class="control area"><textarea id="${id}" data-cn-secret="${esc(field.name)}" placeholder="${esc(
            placeholder,
          )}" autocomplete="off" spellcheck="false"${disabled}></textarea></div>`
        : `<div class="control"><input type="password" id="${id}" data-cn-secret="${esc(field.name)}" placeholder="${esc(
            placeholder,
          )}" autocomplete="new-password"${disabled}></div>`;
  } else if (field.type === "select") {
    control = `<div class="control sel"><select id="${id}" data-cn-field="${esc(field.name)}"${disabled}>${(field.options || [])
      .map((o) => `<option value="${esc(o)}"${String(form.values[field.name]) === o ? " selected" : ""}>${esc(o)}</option>`)
      .join("")}</select></div>`;
  } else {
    const type = field.type === "number" ? "number" : "text";
    control = `<div class="control"><input type="${type}" id="${id}" data-cn-field="${esc(field.name)}" value="${esc(
      form.values[field.name] ?? "",
    )}" placeholder="${esc(field.placeholder || "")}" autocomplete="off" spellcheck="false"${disabled}></div>`;
  }
  return `<label class="field" for="${id}"><span>${esc(field.label)}${need}</span>${control}${help}</label>`;
}

function formHtml() {
  const form = state.form;
  const kind = kindOf(form.kind);
  const title = form.id ? form.name : `Set up ${kind.label}`;
  const head = pageHead(
    `${crumbs([{ label: "Connections", href: LIST }, { label: title }])}<h1 class="h1">${esc(title)}</h1><p class="desc">${esc(
      kind.description,
    )}</p>`,
  );
  if (!kind.available) {
    return `<main class="screen cn">${head}<section class="card notice-card" role="status"><h3>This service needs an add-on</h3><p>${esc(
      `Ask whoever runs Marketing AI to install it: pip install 'marketing-ai[${kind.addon}]'.`,
    )}</p><div class="notice-actions"><a class="btn secondary" href="${LIST}">Back to Connections</a></div></section></main>`;
  }
  const basic = (kind.fields || []).filter((f) => !f.advanced).map((f) => fieldHtml(f, form)).join("");
  const advanced = (kind.fields || []).filter((f) => f.advanced);
  const more = advanced.length
    ? `<details class="adv"><summary>More options</summary><div class="cn-advbody">${advanced.map((f) => fieldHtml(f, form)).join("")}</div></details>`
    : "";
  const disabled = form.busy ? " disabled" : "";
  const label = form.busy === "save" ? "Saving…" : form.busy === "test" ? "Testing…" : "Save and test";
  return `<main class="screen cn">${head}
    <form class="cn-form" id="cn-form" novalidate>
      <label class="field" for="cn-name"><span>Name</span><div class="control"><input type="text" id="cn-name" value="${esc(
        form.name,
      )}" maxlength="80" autocomplete="off"${disabled}></div><span class="sub">What you will see in the list, such as “Sales database”.</span></label>
      ${basic}${more}
      ${form.error ? errorBox(form.error) : ""}
      <div class="actions"><button type="submit" class="btn primary" id="cn-save"${disabled}>${esc(label)}</button><a class="btn secondary" href="${LIST}">${
        form.id ? "Back to Connections" : "Cancel"
      }</a></div>
    </form>
    <section aria-live="polite" id="cn-result">${stepsHtml(form.report)}</section>
  </main>`;
}

// --- drawing and behaviour -------------------------------------------------------------------------

function draw() {
  const { app, parts } = view;
  if (!app) return;
  injectConnectionStyles();
  if (parts[1] && !state.loadError && state.form) app.innerHTML = formHtml();
  else if (parts[1] && !state.loadError) app.innerHTML = notFoundHtml();
  else app.innerHTML = listHtml();
  bind(app);
}

function notFoundHtml() {
  return `<main class="screen cn">${pageHead(
    `${crumbs([{ label: "Connections", href: LIST }, { label: "Not found" }])}<h1 class="h1">Connections</h1>`,
  )}<section class="card notice-card" role="status" data-not-found><h3>This connection could not be found</h3><p>It may have been deleted.</p><div class="notice-actions"><a class="btn secondary" href="${LIST}">Back to Connections</a></div></section></main>`;
}

function replaceConnection(connection) {
  const at = state.connections.findIndex((c) => c.connection_id === connection.connection_id);
  if (at >= 0) state.connections[at] = connection;
  else state.connections.push(connection);
}

async function testFromList(id) {
  state.busy[id] = "test";
  delete state.errors[id];
  draw();
  try {
    const answer = await testConnection(id);
    replaceConnection(answer.connection);
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
    announceStatus("Connection deleted");
  } catch (error) {
    state.errors[id] = error;
  }
  delete state.busy[id];
  draw();
}

/** An input's value as the API takes it: a tick as true/false, a number as a number, the rest as text. */
function fieldValue(input) {
  if (input.type === "checkbox") return input.checked;
  const raw = input.value;
  if (input.type === "number" && raw.trim() !== "" && Number.isFinite(Number(raw))) return Number(raw);
  return raw;
}

/** Read the form: the non-secret values into the form's state, the typed secrets into a local only. */
function readForm(app) {
  const form = state.form;
  const name = app.querySelector("#cn-name");
  if (name) form.name = name.value;
  app.querySelectorAll("[data-cn-field]").forEach((input) => {
    form.values[input.dataset.cnField] = fieldValue(input);
  });
  const secrets = {};
  app.querySelectorAll("[data-cn-secret]").forEach((input) => {
    secrets[input.dataset.cnSecret] = input.value;
  });
  return secrets;
}

async function saveAndTest(app) {
  const form = state.form;
  const secrets = readForm(app);
  const kind = kindOf(form.kind);
  const config = {};
  for (const field of kind.fields || []) {
    if (field.secret) continue;
    const value = form.values[field.name];
    if (value === "" || value === undefined || value === null) continue;
    config[field.name] = value;
  }
  form.busy = "save";
  form.error = null;
  draw();
  let saved;
  try {
    saved = form.id
      ? await updateConnection(form.id, { name: form.name, config, secrets })
      : await createConnection({ kind: form.kind, name: form.name, config, secrets });
  } catch (error) {
    form.busy = "";
    form.error = error;
    draw();
    return;
  }
  replaceConnection(saved);
  Object.assign(form, { id: saved.connection_id, saved: saved.secrets_saved || [], readable: saved.secrets_readable !== false, name: saved.name, busy: "test" });
  draw();
  try {
    const answer = await testConnection(saved.connection_id);
    replaceConnection(answer.connection);
    form.report = answer.report;
    announceStatus(answer.report.ok ? "Connected" : "The test found a problem");
  } catch (error) {
    form.error = error;
  }
  form.busy = "";
  const target = `${LIST}/${saved.connection_id}`;
  if (window.location.hash !== target) {
    view.parts = ["connections", saved.connection_id];
    if (window.history && window.history.replaceState) window.history.replaceState(null, "", target);
  }
  draw();
}

function bind(app) {
  app.querySelectorAll("[data-cn-test]").forEach((b) => b.addEventListener("click", () => testFromList(b.dataset.cnTest)));
  app.querySelectorAll("[data-cn-delete]").forEach((b) =>
    b.addEventListener("click", () => {
      state.confirming = b.dataset.cnDelete;
      draw();
    }),
  );
  app.querySelectorAll("[data-cn-delete-yes]").forEach((b) => b.addEventListener("click", () => remove(b.dataset.cnDeleteYes)));
  app.querySelectorAll("[data-cn-delete-no]").forEach((b) =>
    b.addEventListener("click", () => {
      state.confirming = null;
      draw();
    }),
  );
  const form = app.querySelector("#cn-form");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!state.form.busy) saveAndTest(app);
    });
    // Keep typed (non-secret) values across a repaint; secrets are only read when saving.
    form.querySelectorAll("[data-cn-field]").forEach((input) =>
      input.addEventListener("change", () => {
        state.form.values[input.dataset.cnField] = fieldValue(input);
      }),
    );
  }
}

// --- the route -------------------------------------------------------------------------------------

/** `registerModule`'s render for `#/connections...`: paint a skeleton, load, draw. */
export async function renderConnections(app, parts) {
  injectConnectionStyles();
  if (parts[1] === "ai") return renderAiService(app, parts[2]); // the AI service screens load their own state
  view = { app, parts: [...parts] };
  const editing = parts[1];
  app.innerHTML = skeleton(editing ? "form" : "list", { title: "Connections" });
  state.loadError = null;
  try {
    const [kinds, list] = await Promise.all([getKinds(), getConnections()]);
    state.kinds = kinds.kinds || [];
    state.connections = list.connections || [];
  } catch (error) {
    state.loadError = error;
  }
  if (!editing) {
    state.form = null;
    try {
      state.ai = await getAiService();
    } catch {
      state.ai = null; // the card still links to its screen
    }
  } else if (editing === "new") {
    const kind = kindOf(parts[2]);
    state.form = kind && kind.creatable ? freshForm(kind, null) : null;
  } else {
    const connection = state.connections.find((c) => c.connection_id === editing);
    const kind = connection ? kindOf(connection.kind) : null;
    state.form = connection && kind ? freshForm(kind, connection) : null;
  }
  if (view.app !== app || view.parts.join("/") !== parts.join("/")) return; // the person moved on
  draw();
  document.title = "Connections · Marketing AI";
}
