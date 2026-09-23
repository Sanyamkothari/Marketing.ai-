// The DPDP screens (Phase 4b M48, Admin): the consent ledger, erasure requests and access requests.
// The retention dry run and apply are in `retention.js`; both share this file's head and tabs.
//
// Every call is `api/routes/privacy.py`, and every rule shown is the server's:
// * A data principal's id is typed, sent in a `POST` body and forgotten (DEC-746). It is read from its
//   input on submit, never kept in screen state, never put in the hash (the browser history, and any
//   proxy that logs URLs, would keep it for good), and never drawn again: the answers name the person
//   by `principal_hash` only, as the server does. After a repaint the input is empty, which is the
//   point - the same rule the sign-in form follows for a password (DEC-791).
// * Erasure cannot be undone, so nothing is sent until the "I understand" box is ticked; the
//   answer is shown in full - rows and cells per store, files rewritten or deleted, the models flagged
//   for retraining at their next scheduled cycle (never at once, plan M48.3) - with the `er_` id that
//   is the audit event's object, and a link that opens the audit viewer filtered to it.
// * A failed erasure is `500` with the engine's code; the message names the `er_` id, and the
//   register below re-reads, where the row reads `failed` (DEC-752).
// * A consent file is all or nothing unless "import the valid rows" is ticked (DEC-751): a refused
//   file answers `422` with the same report, so every problem is listed by row and column at once.
// * The access export is a zip the server returns and never stores (DEC-753); the screen saves it at
//   once under the server's file name and shows the `ar_` id from that name as the audit reference.
// * Without `configs/privacy.yaml` every route answers `409 PRIVACY_NOT_CONFIGURED`; that sentence is
//   shown once, in place of the forms, rather than as the same error under each of them.

import { EM_DASH, errorBox, esc, fmtInt, fmtSize, fmtStamp } from "../../dom.js";
import {
  getErasures,
  getPrivacyPolicy,
  getRetrainFlags,
  postAccessRequest,
  postConsentImport,
  postConsentLookup,
  postErasure,
} from "./api.js";
import { bindAuditLinks } from "./audit.js";
import { actionButton, auditReference, fieldValue, mono, refusal, statusPill, tabStrip, textField } from "./controls.js";
import { filenameFrom, saveBlob } from "./downloads.js";
import { reasonFor } from "./session.js";
import { adminHead } from "./users.js";

export const PRIVACY_TABS = [
  ["consent", "Consent", "#/privacy/consent"],
  ["erasure", "Erasure", "#/privacy/erasure"],
  ["access", "Access requests", "#/privacy/access"],
  ["retention", "Retention", "#/privacy/retention"],
];

/** Shown under every privacy screen's title. */
const LEGAL =
  "Engineering controls that support India's DPDP Act. They are not legal advice; your compliance team decides how they are used.";

export const privacyHead = (title) => adminHead(title, esc(LEGAL));

const state = {
  policy: null,
  policyError: null,
  // consent
  importing: false,
  importReport: null,
  importError: null,
  looking: false,
  lookup: null,
  lookupError: null,
  // erasure
  erasing: false,
  erasure: null,
  erasureError: null,
  register: null,
  registerError: null,
  flags: null,
  // access
  exporting: false,
  exported: null, // { filename, size, requestId }
  exportError: null,
};

/** The `ar_…` id inside `access_export_ar_….zip`, the audit event's object id (DEC-753). */
export function accessRequestId(filename) {
  const match = /(?:^|[^A-Za-z0-9])(ar_[A-Za-z0-9]+)/.exec(filename || ""); // "_" before it: no \b
  return match ? match[1] : null;
}

/** Load what every privacy tab needs: the policy, which also says whether privacy is configured. */
export async function loadPolicy() {
  try {
    state.policy = await getPrivacyPolicy();
    state.policyError = null;
  } catch (error) {
    state.policy = null;
    state.policyError = error;
  }
}

/** The erasure register and the retrain flags, for the Erasure tab. */
export async function loadRegister() {
  try {
    const [register, flags] = await Promise.all([getErasures(), getRetrainFlags()]);
    state.register = register.requests || [];
    state.flags = flags.flags || [];
    state.registerError = null;
  } catch (error) {
    state.registerError = error;
  }
}

/** The screen as a whole, or the reason it cannot be used. `body` draws the tab's own cards. */
export function privacyScreen(tab, title, body) {
  const head = privacyHead(title);
  const tabs = tabStrip(tab, PRIVACY_TABS, "Privacy");
  const refused = reasonFor("GET", "/privacy/purposes");
  if (refused) return `<main class="screen">${head}${tabs}${refusal(refused)}</main>`;
  if (state.policyError) return `<main class="screen">${head}${tabs}${errorBox(state.policyError)}</main>`;
  if (!state.policy) return `<main class="screen">${head}${tabs}<p class="loading">Loading…</p></main>`;
  return `<main class="screen">${head}${tabs}<div class="stack">${body()}</div></main>`;
}

const principalField = (name) =>
  `<label class="pb-field field"><span class="sub">Data principal id · as your files spell it</span><input class="pb-input" name="${name}" autocomplete="off" spellcheck="false" required></label>`;

const clientField = () =>
  textField("client_id", "Client id (optional)", { placeholder: "the deployment's client when empty" });

// --- consent ------------------------------------------------------------------------------------

function purposesCard(policy) {
  const rows = (policy.purposes || [])
    .map(
      (p) =>
        `<tr><td>${mono(p.purpose_id)}</td><td>${esc(p.label)}</td><td>${esc(p.description)}</td></tr>`,
    )
    .join("");
  const gated = Object.entries(policy.use_case_purposes || {});
  return `<section class="card"><h3>Purposes</h3><div class="tbl-wrap"><table>
    <thead><tr><th>Purpose</th><th>Name</th><th>What it covers</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="kv"><span class="k">Use cases gated by consent</span><span class="v">${
      gated.length ? gated.map(([uc, purpose]) => `${esc(uc)} → ${esc(purpose)}`).join(" · ") : "none"
    }</span></div>
    <div class="kv"><span class="k">Erasure</span><span class="v">${esc(policy.erasure_mode)}; consent history is ${esc(
      policy.consent_history === "keep" ? "kept (hashed)" : "deleted",
    )}</span></div></section>`;
}

function problemsTable(errors) {
  const shown = errors.slice(0, 200);
  return `<div class="tbl-wrap"><table><thead><tr><th>Row</th><th>Column</th><th>Problem</th><th></th></tr></thead><tbody>${shown
    .map(
      (e) =>
        `<tr><td>${esc(fmtInt(e.row))}</td><td>${e.column ? mono(e.column) : EM_DASH}</td><td>${mono(e.code)}</td><td>${esc(
          e.message,
        )}</td></tr>`,
    )
    .join("")}</tbody></table></div>${
    errors.length > shown.length ? `<p class="pb-small">…and ${esc(fmtInt(errors.length - shown.length))} more.</p>` : ""
  }`;
}

function importOutcome() {
  if (state.importError) return errorBox(state.importError);
  const r = state.importReport;
  if (!r) return "";
  const ignored = (r.ignored_columns || []).length
    ? ` Columns ignored: ${r.ignored_columns.map((c) => `<span class="pb-mono">${esc(c)}</span>`).join(", ")}.`
    : "";
  const errors = r.errors || [];
  const head = r.imported
    ? `<div class="pb-ok" role="status">Imported ${esc(fmtInt(r.rows_imported))} of ${esc(fmtInt(r.rows_read))} rows into client <span class="pb-mono">${esc(
        r.client_id,
      )}</span>'s ledger.${ignored}</div>`
    : `<div class="apierr" role="alert"><b>CONSENT_FILE_REFUSED</b>Nothing was imported: ${esc(fmtInt(errors.length))} problem${
        errors.length === 1 ? "" : "s"
      } in ${esc(fmtInt(r.rows_read))} rows. Correct the file, or tick "import the valid rows".${ignored}</div>`;
  return `${head}${errors.length ? problemsTable(errors) : ""}`;
}

function importCard() {
  return `<section class="card"><h3>Import a consent file</h3><div class="form-body">
    <form id="pb-consent-import" class="pb-form wide" novalidate>
      <p class="fhint" style="margin:0">A UTF-8 CSV with <span class="pb-mono">principal_id, purpose, status, recorded_at</span> and optionally <span class="pb-mono">expires_at, source</span>. Ids are hashed on arrival; the file is not kept.</p>
      <div class="frow">
        <label class="pb-field field"><span class="sub">Consent file (.csv)</span><input class="pb-input" type="file" name="file" accept=".csv,text/csv" required></label>
        ${clientField()}
      </div>
      <label class="check"><input type="checkbox" name="partial"> Import the valid rows even if some are wrong</label>
      <div class="actions">${actionButton("POST", "/privacy/consent/imports", {
        type: "submit",
        attrs: 'id="pb-consent-import-submit"',
        label: state.importing ? "Importing…" : "Import",
        busy: state.importing,
      })}</div>
    </form>
    <div aria-live="polite">${importOutcome()}</div>
  </div></section>`;
}

function lookupOutcome() {
  if (state.lookupError) return errorBox(state.lookupError);
  const r = state.lookup;
  if (!r) return "";
  const purposes = (r.purposes || [])
    .map((p) => `<tr><td>${mono(p.purpose)}</td><td>${p.state === "none" ? `<span class="pill pb-pill-none">no record</span>` : statusPill(p.state)}</td></tr>`)
    .join("");
  const records = (r.records || [])
    .map(
      (c) =>
        `<tr><td>${esc(fmtInt(c.seq))}</td><td>${mono(c.purpose)}</td><td>${statusPill(c.status)}</td><td>${esc(
          c.source,
        )}</td><td>${esc(fmtStamp(c.recorded_at))}</td><td>${esc(fmtStamp(c.expires_at))}</td></tr>`,
    )
    .join("");
  return `<div class="kv"><span class="k">Principal</span><span class="v">${mono(r.principal_hash)}</span></div>
    <div class="kv"><span class="k">Client · as of</span><span class="v">${mono(r.client_id)} · ${esc(fmtStamp(r.as_of))}</span></div>
    <div class="tbl-wrap"><table><thead><tr><th>Purpose</th><th>May be processed?</th></tr></thead><tbody>${purposes}</tbody></table></div>
    ${
      records
        ? `<h4>Ledger rows, oldest first</h4><div class="tbl-wrap"><table><thead><tr><th>#</th><th>Purpose</th><th>Status</th><th>Source</th><th>Recorded</th><th>Expires</th></tr></thead><tbody>${records}</tbody></table></div>`
        : `<p class="empty">The ledger holds no row for this person.</p>`
    }
    ${auditReference("privacy.consent.lookup", r.lookup_id)}`;
}

function lookupCard() {
  return `<section class="card"><h3>Look up one person</h3><div class="form-body">
    <form id="pb-consent-lookup" class="pb-form wide" novalidate autocomplete="off">
      <div class="frow">${principalField("principal_id")}${clientField()}</div>
      <div class="actions">${actionButton("POST", "/privacy/consent/lookup", {
        type: "submit",
        attrs: 'id="pb-consent-lookup-submit"',
        label: state.looking ? "Looking up…" : "Look up",
        busy: state.looking,
      })}<span class="reason">The id is sent in the request body and never shown again; the answer names the person by hash.</span></div>
    </form>
    <div aria-live="polite">${lookupOutcome()}</div>
  </div></section>`;
}

export const consentHtml = () =>
  privacyScreen("consent", "Consent", () => `${purposesCard(state.policy)}${importCard()}${lookupCard()}`);

// --- erasure ------------------------------------------------------------------------------------

function storeTable(counts) {
  const rows = Object.entries(counts || {});
  if (!rows.length) return `<p class="empty">The id was found in no store.</p>`;
  return `<div class="tbl-wrap"><table><thead><tr><th>Store</th><th>Files</th><th>Rows</th><th>Cells</th></tr></thead><tbody>${rows
    .map(
      ([store, c]) =>
        `<tr><td>${esc(store)}</td><td>${esc(fmtInt(c.files))}</td><td>${esc(fmtInt(c.rows))}</td><td>${esc(fmtInt(c.cells))}</td></tr>`,
    )
    .join("")}</tbody></table></div>`;
}

function modelsFlagged(models) {
  if (!models || !models.length) return `<p class="pb-small">No model was trained on data holding this person.</p>`;
  return `<p class="pb-small">${esc(fmtInt(models.length))} model version${
    models.length === 1 ? " was" : "s were"
  } trained on data that held this person and ${models.length === 1 ? "is" : "are"} flagged for retraining at the next scheduled cycle (not now): ${models
    .map((m) => mono(m))
    .join(", ")}</p>`;
}

function erasureOutcome() {
  if (state.erasureError) return errorBox(state.erasureError);
  const r = state.erasure;
  if (!r) return "";
  const totals = [
    ["Rows deleted", r.rows_deleted],
    ["Rows tombstoned", r.rows_tombstoned],
    ["Cells masked", r.cells_masked],
    ["Files rewritten", r.files_rewritten],
    ["Files deleted", r.files_deleted],
    ["Consent rows deleted", r.consent_records_deleted],
  ];
  return `<div class="pb-ok" role="status">Erasure ${mono(r.request_id)} ${statusPill(r.status)} · mode ${esc(r.mode)} · principal ${mono(
    r.principal_hash,
  )}</div>
    <div class="kpis">${totals
      .map(([l, v]) => `<div class="kpi"><div class="l">${esc(l)}</div><div class="v">${esc(fmtInt(v))}</div></div>`)
      .join("")}</div>
    <h4>Per store</h4>${storeTable(r.store_counts)}
    ${modelsFlagged(r.models_flagged)}
    ${
      (r.unrewritable_keys || []).length
        ? `<div class="apierr" role="alert"><b>NOT_REWRITTEN</b>${esc(fmtInt(r.unrewritable_keys.length))} file(s) could not be rewritten and need a person: ${r.unrewritable_keys
            .map((k) => esc(k))
            .join(", ")}</div>`
        : ""
    }
    ${auditReference("privacy.erasure", r.request_id)}`;
}

function erasureCard() {
  return `<section class="card"><h3>Erase one person</h3><div class="form-body">
    <form id="pb-erasure" class="pb-form wide" novalidate autocomplete="off">
      <p class="fhint" style="margin:0">Searches every store - uploads, sources, datasets, scores, explanations, campaign messages, the LLM cache and the consent ledger - and deletes or tombstones every row holding the id. This cannot be undone.</p>
      <div class="frow">${principalField("principal_id")}${clientField()}</div>
      <label class="check"><input type="checkbox" name="confirm" id="pb-erasure-confirm"> I understand this permanently removes this person's data from every store</label>
      <div class="actions">${actionButton("POST", "/privacy/erasure", {
        type: "submit",
        attrs: 'id="pb-erasure-submit"',
        label: state.erasing ? "Erasing…" : "Erase",
        busy: state.erasing,
      })}</div>
    </form>
    <div aria-live="polite">${erasureOutcome()}</div>
  </div></section>`;
}

function registerCard() {
  if (state.registerError) return `<section class="card"><h3>Erasure register</h3>${errorBox(state.registerError)}</section>`;
  if (!state.register) return `<section class="card"><h3>Erasure register</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  const rows = state.register
    .map(
      (r) => `<tr><td>${mono(r.request_id)}</td><td>${statusPill(r.status)}${r.error_code ? ` ${mono(r.error_code)}` : ""}</td>
        <td>${esc(fmtStamp(r.requested_at))}<div class="pb-small">by ${esc(r.requested_by)}</div></td><td>${esc(r.mode)}</td>
        <td>${esc(fmtInt(r.rows_deleted + r.rows_tombstoned))}</td><td>${esc(fmtInt(r.files_rewritten + r.files_deleted))}</td>
        <td>${(r.models_flagged || []).length ? r.models_flagged.map((m) => mono(m)).join(" ") : EM_DASH}</td></tr>`,
    )
    .join("");
  const flags = (state.flags || [])
    .map(
      (f) =>
        `<tr><td>${mono(f.model_id)}</td><td>${mono(f.request_id)}</td><td>${esc(f.reason)}</td><td>${esc(fmtStamp(f.created_at))}</td></tr>`,
    )
    .join("");
  return `<section class="card"><h3>Erasure register · ${esc(fmtInt(state.register.length))}</h3>${
    rows
      ? `<div class="tbl-wrap"><table><thead><tr><th>Request</th><th>Status</th><th>Requested</th><th>Mode</th><th>Rows</th><th>Files</th><th>Models flagged</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="empty">No erasure has been requested.</p>`
  }</section>
  <section class="card"><h3>Models due for retraining</h3>${
    flags
      ? `<div class="tbl-wrap"><table><thead><tr><th>Model version</th><th>Erasure</th><th>Why</th><th>Flagged</th></tr></thead><tbody>${flags}</tbody></table></div>`
      : `<p class="empty">No model is waiting to be retrained because of an erasure.</p>`
  }</section>`;
}

export const erasureHtml = () => privacyScreen("erasure", "Erasure requests", () => `${erasureCard()}${registerCard()}`);

// --- access requests ----------------------------------------------------------------------------

function accessOutcome() {
  if (state.exportError) return errorBox(state.exportError);
  const r = state.exported;
  if (!r) return "";
  return `<div class="pb-ok" role="status">Saved <span class="pb-mono">${esc(r.filename)}</span> (${esc(
    fmtSize(r.size),
  )}). Hand it to the client to share with the person; the server kept no copy.</div>${
    r.requestId ? auditReference("privacy.access_request", r.requestId) : ""
  }`;
}

function accessCard() {
  return `<section class="card"><h3>Export everything held about one person</h3><div class="form-body">
    <form id="pb-access" class="pb-form wide" novalidate autocomplete="off">
      <p class="fhint" style="margin:0">A zip of every row holding the id, their consent and erasure history, and a manifest of where each came from. The server returns it and keeps no copy; the audit log records its SHA-256.</p>
      <div class="frow">${principalField("principal_id")}${textField("client_id", "Client id (optional)", {
        placeholder: "limits the consent history included",
      })}</div>
      <div class="actions">${actionButton("POST", "/privacy/access-requests", {
        type: "submit",
        attrs: 'id="pb-access-submit"',
        label: state.exporting ? "Exporting…" : "Export and download",
        busy: state.exporting,
      })}</div>
    </form>
    <div aria-live="polite">${accessOutcome()}</div>
  </div></section>`;
}

export const accessHtml = () => privacyScreen("access", "Access requests", accessCard);

// --- binding ------------------------------------------------------------------------------------

/** `{principal_id, client_id?}` from a form; null when the id is empty. The id is not kept anywhere. */
function principalPayload(form) {
  const principal = form.elements.namedItem("principal_id").value.trim();
  if (!principal) return null;
  const payload = { principal_id: principal };
  const client = fieldValue(form, "client_id");
  if (client) payload.client_id = client;
  return payload;
}

const missingId = () => ({ code: "PRINCIPAL_ID_REQUIRED", message: "Type the data principal's id first." });

async function submitImport(form, repaint) {
  const input = form.elements.namedItem("file");
  const file = input && input.files && input.files[0];
  state.importReport = null;
  if (!file) {
    state.importError = { code: "CONSENT_FILE_REQUIRED", message: "Choose a consent CSV first." };
    repaint();
    return;
  }
  state.importing = true;
  state.importError = null;
  repaint();
  try {
    state.importReport = await postConsentImport(file, {
      clientId: fieldValue(form, "client_id"),
      partial: form.elements.namedItem("partial").checked,
    });
  } catch (error) {
    state.importError = error;
  }
  state.importing = false;
  repaint();
}

async function submitLookup(form, repaint) {
  const payload = principalPayload(form);
  state.lookup = null;
  if (!payload) {
    state.lookupError = missingId();
    repaint();
    return;
  }
  state.looking = true;
  state.lookupError = null;
  repaint();
  try {
    state.lookup = await postConsentLookup(payload);
  } catch (error) {
    state.lookupError = error;
  }
  state.looking = false;
  repaint();
}

async function submitErasure(form, repaint) {
  const payload = principalPayload(form);
  state.erasure = null;
  if (!payload) {
    state.erasureError = missingId();
    repaint();
    return;
  }
  if (!form.elements.namedItem("confirm").checked) {
    state.erasureError = {
      code: "CONFIRMATION_REQUIRED",
      message: "Tick the box to confirm: an erasure cannot be undone.",
    };
    repaint();
    return;
  }
  state.erasing = true;
  state.erasureError = null;
  repaint();
  try {
    state.erasure = await postErasure(payload);
  } catch (error) {
    state.erasureError = error;
  }
  state.erasing = false;
  await loadRegister(); // a failed erasure is in the register too, as `failed` (DEC-752)
  repaint();
}

async function submitAccess(form, repaint) {
  const payload = principalPayload(form);
  state.exported = null;
  if (!payload) {
    state.exportError = missingId();
    repaint();
    return;
  }
  state.exporting = true;
  state.exportError = null;
  repaint();
  try {
    const { blob, disposition } = await postAccessRequest(payload);
    const filename = filenameFrom(disposition) || "access_export.zip";
    saveBlob(blob, filename);
    state.exported = { filename, size: blob.size, requestId: accessRequestId(filename) };
  } catch (error) {
    state.exportError = error;
  }
  state.exporting = false;
  repaint();
}

const SUBMITS = {
  "pb-consent-import": submitImport,
  "pb-consent-lookup": submitLookup,
  "pb-erasure": submitErasure,
  "pb-access": submitAccess,
};

/** One delegated submit listener on `<main>` (replaced by every paint, so listeners never pile up). */
export function bindPrivacy(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindAuditLinks(main);
  main.addEventListener("submit", (event) => {
    const form = event.target;
    const handler = form && SUBMITS[form.id];
    if (!handler) return;
    event.preventDefault();
    if (state.importing || state.looking || state.erasing || state.exporting) return;
    handler(form, repaint);
  });
}

/** Test seam: forget screen state between cases. */
export function _resetPrivacyForTests() {
  Object.assign(state, {
    policy: null,
    policyError: null,
    importing: false,
    importReport: null,
    importError: null,
    looking: false,
    lookup: null,
    lookupError: null,
    erasing: false,
    erasure: null,
    erasureError: null,
    register: null,
    registerError: null,
    flags: null,
    exporting: false,
    exported: null,
    exportError: null,
  });
}
