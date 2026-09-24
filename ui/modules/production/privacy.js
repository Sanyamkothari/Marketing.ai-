// The DPDP screens (Phase 4b M48, Admin): customer consent, erasure requests and access requests.
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
// * Erasure is a background job (Plan D M54, DEC-863): the `POST` answers `202` with the `er_` id, the
//   screen polls `GET /privacy/erasure/{id}/progress` (a plain read) and draws each store's files done
//   and attempts, then reads the full record once (an audited read) as the completion report. A request
//   that ends `failed` - a store that kept failing after its retries - offers "Retry", which needs the
//   id typed again: it was never kept, and the server checks it against the request's hash.
// * A consent file is all or nothing unless "import the valid rows" is ticked (DEC-751): a refused
//   file answers `422` with the same report, so every problem is listed by row and column at once.
// * The access export is a zip the server returns and never stores (DEC-753); the screen saves it at
//   once under the server's file name and shows the `ar_` id from that name as the audit reference.
// * Without `configs/privacy.yaml` every route answers `409 PRIVACY_NOT_CONFIGURED`; that sentence is
//   shown once, in place of the forms, rather than as the same error under each of them.
//
// v1: the client every form names is the one chosen in the top bar ("For client: Demo Telecom"),
// read through the router's setup-source seam, so a deployment with no client id of its own no longer
// answers `CLIENT_ID_REQUIRED`; a different client can be typed under Advanced. Hashes, request ids
// and codes sit under Details; purposes, stores and statuses are words.

import { EM_DASH, errorBox, esc, fmtDate, fmtInt, fmtSize, fmtStamp, techDetails } from "../../dom.js";
import { setupSource } from "../router.js";
import {
  getErasure,
  getErasureProgress,
  getErasures,
  getPrivacyPolicy,
  getRetrainFlags,
  postAccessRequest,
  postConsentImport,
  postConsentLookup,
  postErasure,
  postErasureRetry,
} from "./api.js";
import { bindAuditLinks } from "./audit.js";
import {
  actionButton,
  auditReference,
  bindFileNames,
  codeWords,
  fieldError,
  fieldValue,
  fileField,
  isLocal,
  loadPeople,
  loadUseCaseNames,
  mono,
  personName,
  refusal,
  rowsTable,
  screenHead,
  statusPill,
  tabStrip,
  textField,
  useCaseName,
} from "./controls.js";
import { filenameFrom, saveBlob } from "./downloads.js";
import { reasonFor } from "./session.js";

export const PRIVACY_TABS = [
  ["consent", "Consent", "#/privacy/consent"],
  ["erasure", "Erase a customer", "#/privacy/erasure"],
  ["access", "Export a customer's data", "#/privacy/access"],
  ["retention", "Old data", "#/privacy/retention"],
];

/** One quiet line at the foot of every privacy screen. */
const LEGAL =
  "These are engineering controls that support India's DPDP Act. They are not legal advice; your compliance team decides how they are used.";

/** Home › Admin › Privacy › <title>. */
export const privacyHead = (title, desc = "") =>
  screenHead({
    trail: [
      { label: "Admin", href: "#/admin/users" },
      { label: "Privacy", href: "#/privacy/consent" },
    ],
    title,
    desc,
  });

/** What each store holds, in words; the store's key stays in a "Show more columns" cell. */
const STORE_WORDS = {
  uploads: "Uploaded files",
  sources: "Source tables",
  datasets: "Built datasets",
  scores: "Scored customers",
  row_explanations: "Reasons behind scores",
  copy_messages: "Campaign messages",
  llm_cache: "Saved AI answers",
  indexes: "AI assistant documents",
  run_artefacts: "Other run files",
  consent: "Consent records",
  consent_ledger: "Consent records",
};
const storeWords = (store) => STORE_WORDS[store] || String(store).replace(/_/g, " ");

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
  erasure: null, // the completion report (the request's record)
  erasureJob: null, // { request_id, principal_hash, status, error_code, progress[] } while it runs
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
    const [policy] = await Promise.all([getPrivacyPolicy(), loadUseCaseNames()]);
    state.policy = policy;
    state.policyError = null;
  } catch (error) {
    state.policy = null;
    state.policyError = error;
  }
}

/** The erasure register and the retrain flags, for the Erasure tab. */
export async function loadRegister() {
  try {
    const [register, flags] = await Promise.all([getErasures(), getRetrainFlags(), loadPeople()]);
    state.register = register.requests || [];
    state.flags = flags.flags || [];
    state.registerError = null;
  } catch (error) {
    state.registerError = error;
  }
}

/** The screen as a whole, or the reason it cannot be used. `body` draws the tab's own cards. */
export function privacyScreen(tab, title, body, desc = "") {
  const head = privacyHead(title, desc);
  const tabs = tabStrip(tab, PRIVACY_TABS, "Privacy");
  const legal = `<p class="pb-legal">${esc(LEGAL)}</p>`;
  const refused = reasonFor("GET", "/privacy/purposes");
  const main = (inner) => `<main class="screen pb-screen">${head}${tabs}${inner}${legal}</main>`;
  if (refused) return main(refusal(refused));
  if (state.policyError) {
    return main(errorBox(state.policyError, { retry: state.policyError.code !== "PRIVACY_NOT_CONFIGURED" }));
  }
  if (!state.policy) return main(`<p class="loading">Loading…</p>`);
  return main(`<div class="stack">${body()}</div>`);
}

// --- the client each form names -------------------------------------------------------------------

/** The client chosen in the top bar, `{ id, name }`, or null (the picker is not loaded yet, or absent). */
export function selectedClient() {
  const source = setupSource();
  if (!source || typeof source.context !== "function") return null;
  try {
    const context = source.context() || {};
    return context.clientId ? { id: context.clientId, name: context.clientName || context.clientId } : null;
  } catch {
    return null;
  }
}

/** "For client: Demo Telecom", and a different client id under Advanced. */
function clientBlock(purpose = "") {
  const client = selectedClient();
  const line = client
    ? `<p class="pb-for-client">For client: <b>${esc(client.name)}</b> <span class="pb-small">(chosen in the top bar)</span></p>`
    : `<p class="pb-for-client">For this installation's own client.</p>`;
  return `${line}<details class="adv"><summary>Advanced</summary><div class="pb-adv-body">${textField(
    "client_id",
    "A different client id",
    {
      placeholder: client ? client.id : "this installation's client",
      hint: `Leave empty to use the client above.${purpose ? ` ${purpose}` : ""}`,
    },
  )}</div></details>`;
}

/** The client a form's request names: the one typed under Advanced, else the top bar's, else none. */
function clientOf(form) {
  const typed = fieldValue(form, "client_id");
  if (typed) return typed;
  const client = selectedClient();
  return client ? client.id : "";
}

const principalField = (name) =>
  `<label class="field wide pb-field"><span class="sub">Customer ID (as in your files)</span><span class="control"><input name="${name}" autocomplete="off" spellcheck="false" required></span><span class="pb-hint">Sent once and not kept: the answer names the customer by a code, never by this ID.</span></label>`;

// --- consent ------------------------------------------------------------------------------------

const purposeLabel = (id) => {
  const purpose = ((state.policy && state.policy.purposes) || []).find((p) => p.purpose_id === id);
  return purpose ? purpose.label : String(id || EM_DASH).replace(/_/g, " ");
};

function purposesCard(policy) {
  const gated = Object.entries(policy.use_case_purposes || {});
  const rows = (policy.purposes || []).map((p) => {
    const uses = gated.filter(([, purpose]) => purpose === p.purpose_id).map(([uc]) => useCaseName(uc));
    return {
      attrs: `data-purpose="${esc(p.purpose_id)}"`,
      cells: [
        `<b>${esc(p.label)}</b><div class="pb-small">${esc(p.description)}</div>`,
        uses.length ? esc(uses.join(", ")) : `<span class="pb-small">No use case</span>`,
        mono(p.purpose_id),
      ],
    };
  });
  const history = policy.consent_history === "keep" ? "kept, with customer IDs replaced by codes" : "deleted";
  return `<section class="card"><h3>How consent is set up</h3>${rowsTable(
    [{ label: "Purpose" }, { label: "Needed before contacting customers for" }, { label: "Purpose id", more: true }],
    rows,
  )}<div class="kv"><span class="k">When a customer is erased</span><span class="v">Their rows are ${esc(
    policy.erasure_mode === "delete" ? "deleted" : policy.erasure_mode,
  )}; their consent history is ${esc(history)}</span></div></section>`;
}

function problemsTable(errors) {
  const shown = errors.slice(0, 200);
  return `${rowsTable(
    [{ label: "Row", num: true }, { label: "Column" }, { label: "Problem" }, { label: "Code", more: true }],
    shown.map((e) => ({
      cells: [esc(fmtInt(e.row)), e.column ? mono(e.column) : EM_DASH, esc(e.message), mono(e.code)],
    })),
  )}${errors.length > shown.length ? `<p class="pb-small">…and ${esc(fmtInt(errors.length - shown.length))} more.</p>` : ""}`;
}

function importOutcome() {
  if (state.importError) return isLocal(state.importError) ? fieldError(state.importError) : errorBox(state.importError);
  const r = state.importReport;
  if (!r) return "";
  const ignored = (r.ignored_columns || []).length ? ` Columns ignored: ${r.ignored_columns.map((c) => esc(c)).join(", ")}.` : "";
  const errors = r.errors || [];
  const head = r.imported
    ? `<div class="pb-ok" role="status">Imported ${esc(fmtInt(r.rows_imported))} of ${esc(fmtInt(r.rows_read))} rows.${ignored}${techDetails([
        ["Client", r.client_id],
      ])}</div>`
    : `<div class="apierr" role="alert" data-code="CONSENT_FILE_REFUSED"><b>Nothing was imported: ${esc(fmtInt(errors.length))} problem${
        errors.length === 1 ? "" : "s"
      } in ${esc(fmtInt(r.rows_read))} rows.</b><p class="apierr-fix">Correct the file, or tick "Import the valid rows" to load the rest.${ignored}</p>${techDetails(
        [
          ["Code", "CONSENT_FILE_REFUSED"],
          ["Client", r.client_id],
        ],
        "Details",
      )}</div>`;
  return `${head}${errors.length ? problemsTable(errors) : ""}`;
}

function importCard() {
  const open = state.importing || state.importReport || state.importError;
  return `<section class="card"><div class="card-body"><details class="pb-reveal" id="pb-consent-import-panel"${open ? " open" : ""}>
    <summary class="btn secondary">Import consent records</summary>
    <form id="pb-consent-import" class="pb-stack" novalidate>
      <p class="pb-desc">A CSV file from your CRM with one row per consent given or withdrawn. Customer IDs are replaced by codes on arrival, and the file itself is not kept.</p>
      ${fileField("file", "Choose the consent file (.csv)", { accept: ".csv,text/csv", attrs: "required" })}
      <label class="check"><input type="checkbox" name="partial"> Import the valid rows even if some are wrong</label>
      ${clientBlock()}
      <details class="tech"><summary>File format</summary><p>Columns <code>principal_id, purpose, status, recorded_at</code>, and optionally <code>expires_at, source</code>. UTF-8.</p></details>
      <div class="pb-form-actions">${actionButton("POST", "/privacy/consent/imports", {
        cls: "btn secondary",
        type: "submit",
        attrs: 'id="pb-consent-import-submit"',
        label: state.importing ? "Importing…" : "Import",
        busy: state.importing,
      })}</div>
    </form>
    <div aria-live="polite">${importOutcome()}</div>
  </details></div></section>`;
}

/** "May we contact them?" in words; the ledger's own state stays in `data-status`. */
const CONTACT_WORDS = {
  valid: "Yes",
  granted: "Yes",
  withdrawn: "No, they withdrew consent",
  expired: "No, their consent expired",
  none: "No, there is no consent on record",
};

function lookupOutcome() {
  if (state.lookupError) return isLocal(state.lookupError) ? fieldError(state.lookupError) : errorBox(state.lookupError);
  const r = state.lookup;
  if (!r) return "";
  const answers = rowsTable(
    [{ label: "Purpose" }, { label: "May we contact them?" }],
    (r.purposes || []).map((p) => ({
      attrs: `data-purpose="${esc(p.purpose)}"`,
      cells: [esc(purposeLabel(p.purpose)), statusPill(p.state, CONTACT_WORDS[p.state] || null)],
    })),
    { cls: "pb-consent-answer" },
  );
  const records = (r.records || []).map((c) => ({
    cells: [
      esc(fmtStamp(c.recorded_at)),
      esc(purposeLabel(c.purpose)),
      statusPill(c.status),
      esc(c.source || EM_DASH),
      esc(fmtStamp(c.expires_at)),
      esc(fmtInt(c.seq)),
    ],
  }));
  const history = records.length
    ? `<h4>Consent history, oldest first</h4>${rowsTable(
        [{ label: "Recorded" }, { label: "Purpose" }, { label: "Status" }, { label: "From" }, { label: "Expires" }, { label: "#", more: true }],
        records,
      )}`
    : `<p class="pb-note">There is no consent record for this customer.</p>`;
  return `<div class="pb-lookup">${answers}<details class="tech pb-history"><summary>Consent history</summary>${history}</details>${auditReference(
    "privacy.consent.lookup",
    r.lookup_id,
    [
      ["Customer code", r.principal_hash],
      ["Client", r.client_id],
      ["As of", fmtStamp(r.as_of)],
    ],
  )}</div>`;
}

function lookupCard() {
  return `<section class="card"><h3>Look up a customer</h3><div class="card-body">
    <form id="pb-consent-lookup" class="pb-stack" novalidate autocomplete="off">
      ${principalField("principal_id")}
      ${clientBlock()}
      <div class="pb-form-actions">${actionButton("POST", "/privacy/consent/lookup", {
        type: "submit",
        attrs: 'id="pb-consent-lookup-submit"',
        label: state.looking ? "Looking up…" : "Look up",
        busy: state.looking,
      })}</div>
    </form>
    <div aria-live="polite">${lookupOutcome()}</div>
  </div></section>`;
}

export const consentHtml = () =>
  privacyScreen(
    "consent",
    "Customer consent",
    () => `${lookupCard()}${importCard()}${purposesCard(state.policy)}`,
    "Check whether you may contact a customer, and load the consent records from your own systems.",
  );

// --- erasure ------------------------------------------------------------------------------------

function storeTable(counts) {
  const rows = Object.entries(counts || {});
  if (!rows.length) return `<p class="pb-note">This customer was not found in any file.</p>`;
  return rowsTable(
    [
      { label: "What" },
      { label: "Files", num: true },
      { label: "Rows removed", num: true },
      { label: "Values blanked", num: true },
      { label: "Store", more: true },
    ],
    rows.map(([store, c]) => ({
      attrs: `data-store-count="${esc(store)}"`,
      cells: [esc(storeWords(store)), esc(fmtInt(c.files)), esc(fmtInt(c.rows)), esc(fmtInt(c.cells)), mono(store)],
    })),
  );
}

function modelsFlagged(models) {
  if (!models || !models.length) return `<p class="pb-note">No model learned from data that held this customer.</p>`;
  return `<p class="pb-note">${esc(fmtInt(models.length))} model${
    models.length === 1 ? " was" : "s were"
  } trained on data that held this customer and ${models.length === 1 ? "is" : "are"} flagged for retraining at the next scheduled cycle (not now).</p>`;
}

/** How often a running erasure is asked how far it has got. A test sets it lower. */
let erasurePollMs = 1500;
const TERMINAL = new Set(["completed", "completed_with_exceptions", "failed"]);

function progressTable(progress) {
  if (!progress || !progress.length) return `<p class="pb-note">Searching every file for this customer…</p>`;
  return rowsTable(
    [
      { label: "What" },
      { label: "Status" },
      { label: "Files done", num: true },
      { label: "Code", more: true },
      { label: "Attempts", num: true },
    ],
    progress.map((p) => ({
      attrs: `data-store="${esc(p.store)}"`,
      cells: [
        `${esc(storeWords(p.store))}<div class="pb-small mono">${esc(p.store)}</div>`,
        `${statusPill(p.status)}${p.error_code ? `<div class="pb-small">${codeWords(p.error_code)}</div>` : ""}`,
        `${esc(fmtInt(p.files_done))} of ${esc(fmtInt(p.files_total))}`,
        mono(p.error_code),
        esc(fmtInt(p.attempts)),
      ],
    })),
  );
}

function retryForm(job) {
  return `<form id="pb-erasure-retry" class="pb-stack" novalidate autocomplete="off" data-request="${esc(job.request_id)}">
    <p class="pb-desc">Some files could not be changed after every retry. Type the same customer ID again to try once more: it was never stored, and Marketing AI checks it is the same customer.</p>
    ${principalField("principal_id")}
    <div class="pb-form-actions">${actionButton("POST", "/privacy/erasure/{request_id}/retry", {
      cls: "btn danger",
      type: "submit",
      attrs: 'id="pb-erasure-retry-submit"',
      label: state.erasing ? "Retrying…" : "Retry the erasure",
      busy: state.erasing,
    })}</div>
  </form>`;
}

function erasureRunning(job) {
  const progress = job.progress || [];
  const done = progress.filter((p) => TERMINAL.has(p.status) || p.status === "done").length;
  return `<div class="pb-ok" role="status">Erasing this customer: ${esc(fmtInt(done))} of ${esc(
    fmtInt(progress.length || 0),
  )} kinds of data done. ${statusPill(job.status)}${techDetails([
    ["Request", job.request_id],
    ["Customer code", job.principal_hash],
  ])}</div><h4>Progress</h4>${progressTable(progress)}`;
}

const sum = (...values) => values.reduce((total, v) => total + (Number(v) || 0), 0);

function erasureOutcome() {
  if (state.erasureError && !state.erasureJob) {
    return isLocal(state.erasureError) ? fieldError(state.erasureError) : errorBox(state.erasureError);
  }
  const job = state.erasureJob;
  const laterError = state.erasureError
    ? isLocal(state.erasureError)
      ? fieldError(state.erasureError)
      : errorBox(state.erasureError)
    : "";
  if (job && !state.erasure) return `${erasureRunning(job)}${laterError}`;
  const r = state.erasure;
  if (!r) return "";
  if (r.status === "failed") {
    return `${errorBox(
      {
        code: r.error_code || "ERASURE_FAILED",
        message: `Erasure request ${r.request_id} did not complete and is recorded as failed.`,
      },
      { title: "The erasure did not finish: some files could not be changed." },
    )}<h4>Progress</h4>${progressTable(r.progress)}${
      r.error_code === "ERASURE_STORE_FAILED" ? retryForm(r) : ""
    }${laterError}${auditReference("privacy.erasure.complete", r.request_id)}`;
  }
  const rows = sum(r.rows_deleted, r.rows_tombstoned);
  const files = sum(r.files_rewritten, r.files_deleted);
  const partly = r.status === "completed_with_exceptions" || (r.unrewritable_keys || []).length;
  const lead = rows || r.cells_masked
    ? `${partly ? "Mostly done" : "Done"}: this customer was removed from ${fmtInt(files)} file${files === 1 ? "" : "s"} (${fmtInt(
        rows,
      )} row${rows === 1 ? "" : "s"} removed${r.cells_masked ? `, ${fmtInt(r.cells_masked)} value${r.cells_masked === 1 ? "" : "s"} blanked` : ""}).`
    : "Done: this customer was not found in any file Marketing AI holds.";
  return `<div class="pb-report" role="status"><p class="pb-lead">${esc(lead)}</p>
    ${modelsFlagged(r.models_flagged)}
    ${
      (r.unrewritable_keys || []).length
        ? `<div class="apierr" role="alert" data-code="NOT_REWRITTEN"><b>${esc(fmtInt(r.unrewritable_keys.length))} file${
            r.unrewritable_keys.length === 1 ? "" : "s"
          } could not be changed and need a person to look at them.</b>${techDetails(
            [
              ["Code", "NOT_REWRITTEN"],
              ["Files", r.unrewritable_keys.join(", ")],
            ],
            "Details",
          )}</div>`
        : ""
    }
    <details class="tech"><summary>Where they were found</summary>${storeTable(r.store_counts)}${
      (r.progress || []).length ? `<h4>Progress</h4>${progressTable(r.progress)}` : ""
    }</details>
    ${auditReference("privacy.erasure", r.request_id, [
      ["Status", r.status],
      ["How", r.mode],
      ["Customer code", r.principal_hash],
      ["Models to retrain", (r.models_flagged || []).join(", ")],
      ["Rows deleted", r.rows_deleted],
      ["Rows kept as a marker", r.rows_tombstoned],
      ["Files rewritten", r.files_rewritten],
      ["Files deleted", r.files_deleted],
      ["Consent rows deleted", r.consent_records_deleted],
    ])}</div>`;
}

function erasureCard() {
  return `<section class="card"><h3>Erase a customer</h3><div class="card-body">
    <form id="pb-erasure" class="pb-stack" novalidate autocomplete="off">
      ${principalField("principal_id")}
      ${clientBlock()}
      <label class="check"><input type="checkbox" name="confirm" id="pb-erasure-confirm"> I understand this permanently removes this customer's data from every file</label>
      <div class="pb-form-actions"><span class="spacer"></span>${actionButton("POST", "/privacy/erasure", {
        cls: "btn danger",
        type: "submit",
        attrs: 'id="pb-erasure-submit" data-needs-tick="pb-erasure-confirm"',
        label: state.erasing ? "Erasing…" : "Erase this customer",
        busy: state.erasing,
      })}</div>
    </form>
    <div aria-live="polite">${erasureOutcome()}</div>
  </div></section>`;
}

function registerCard() {
  const title = (n) => `<h3>Erasure requests${n === null ? "" : ` · ${esc(fmtInt(n))}`} <span class="sort-note">(newest first)</span></h3>`;
  if (state.registerError) {
    return `<section class="card">${title(null)}<div class="card-body">${errorBox(state.registerError, { retry: true })}</div></section>`;
  }
  if (!state.register) return `<section class="card">${title(null)}<p class="loading pb-pad">Loading…</p></section>`;
  const rows = [...state.register]
    .sort((a, b) => String(b.requested_at).localeCompare(String(a.requested_at)))
    .map((r) => ({
      attrs: `data-request="${esc(r.request_id)}"`,
      cells: [
        `${esc(fmtDate(r.requested_at))}<div class="pb-small">by ${esc(personName(r.requested_by))}</div>`,
        `${statusPill(r.status)}${r.error_code ? `<div class="pb-small">${codeWords(r.error_code)}</div>` : ""}`,
        esc(fmtInt(sum(r.rows_deleted, r.rows_tombstoned))),
        esc(fmtInt((r.models_flagged || []).length)),
        mono(r.request_id),
        mono(r.client_id),
        esc(r.mode || EM_DASH),
        esc(fmtInt(sum(r.files_rewritten, r.files_deleted))),
      ],
    }));
  const table = rows.length
    ? rowsTable(
        [
          { label: "Requested" },
          { label: "Status" },
          { label: "Rows removed", num: true },
          { label: "Models to retrain", num: true },
          { label: "Request", more: true },
          { label: "Client", more: true },
          { label: "How", more: true },
          { label: "Files changed", more: true },
        ],
        rows,
      )
    : `<div class="empty-state"><p class="es-t">No customer has been erased yet.</p><p>Each request you make above is listed here, with how much was removed.</p></div>`;
  const flags = state.flags || [];
  const models = [...new Set(flags.map((f) => f.model_id))];
  const retrain = models.length
    ? `<div class="card-body"><p class="pb-note">${esc(fmtInt(models.length))} model${
        models.length === 1 ? " is" : "s are"
      } flagged for retraining at the next scheduled cycle (not now), because the data ${
        models.length === 1 ? "it" : "they"
      } learned from held an erased customer.</p><details class="tech"><summary>Models due for retraining</summary>${rowsTable(
        [{ label: "Model" }, { label: "Erasure" }, { label: "Why" }, { label: "Flagged" }],
        flags.map((f) => ({ cells: [mono(f.model_id), mono(f.request_id), mono(f.reason), esc(fmtStamp(f.created_at))] })),
      )}</details></div>`
    : "";
  return `<section class="card">${title(state.register.length)}${table}${retrain}</section>`;
}

export const erasureHtml = () =>
  privacyScreen(
    "erasure",
    "Erase a customer's data",
    () => `${erasureCard()}${registerCard()}`,
    "Finds this customer in every file Marketing AI holds and permanently removes them. This cannot be undone.",
  );

// --- access requests ----------------------------------------------------------------------------

function accessOutcome() {
  if (state.exportError) return isLocal(state.exportError) ? fieldError(state.exportError) : errorBox(state.exportError);
  const r = state.exported;
  if (!r) return "";
  return `<div class="pb-ok" role="status">Saved ${esc(r.filename)} (${esc(
    fmtSize(r.size),
  )}). Give it to the client to share with the customer; nothing was kept on the server.</div>${
    r.requestId ? auditReference("privacy.access_request", r.requestId, [["File", r.filename]]) : ""
  }`;
}

function accessCard() {
  return `<section class="card"><h3>Export a customer's data</h3><div class="card-body">
    <form id="pb-access" class="pb-stack" novalidate autocomplete="off">
      ${principalField("principal_id")}
      ${clientBlock("It limits the consent history included.")}
      <details class="tech"><summary>What the file holds</summary><p>A zip of every row that holds this customer, their consent and erasure history, and a manifest of where each came from. The audit log records the file's SHA-256 fingerprint.</p></details>
      <div class="pb-form-actions">${actionButton("POST", "/privacy/access-requests", {
        type: "submit",
        attrs: 'id="pb-access-submit"',
        label: state.exporting ? "Exporting…" : "Export and download",
        busy: state.exporting,
      })}</div>
    </form>
    <div aria-live="polite">${accessOutcome()}</div>
  </div></section>`;
}

export const accessHtml = () =>
  privacyScreen(
    "access",
    "Export a customer's data",
    accessCard,
    "Download everything Marketing AI holds about one customer, to answer their request for a copy.",
  );

// --- binding ------------------------------------------------------------------------------------

/** `{principal_id, client_id?}` from a form; null when the id is empty. The id is not kept anywhere. */
function principalPayload(form) {
  const principal = form.elements.namedItem("principal_id").value.trim();
  if (!principal) return null;
  const payload = { principal_id: principal };
  const client = clientOf(form);
  if (client) payload.client_id = client;
  return payload;
}

const missingId = () => ({ code: "PRINCIPAL_ID_REQUIRED", message: "Type the customer's ID first." });

async function submitImport(form, repaint) {
  const input = form.elements.namedItem("file");
  const file = input && input.files && input.files[0];
  state.importReport = null;
  if (!file) {
    state.importError = { code: "CONSENT_FILE_REQUIRED", message: "Choose a consent file first." };
    repaint();
    return;
  }
  state.importing = true;
  state.importError = null;
  repaint();
  try {
    state.importReport = await postConsentImport(file, {
      clientId: clientOf(form),
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
  state.erasureJob = null;
  repaint();
  let accepted = null;
  try {
    accepted = await postErasure(payload);
  } catch (error) {
    state.erasureError = error;
  }
  state.erasing = false;
  if (accepted) await followErasure(accepted, repaint);
  else repaint();
}

async function submitRetry(form, repaint) {
  const requestId = form.getAttribute("data-request");
  const principal = form.elements.namedItem("principal_id").value.trim();
  if (!principal) {
    state.erasureError = missingId();
    repaint();
    return;
  }
  state.erasing = true;
  state.erasureError = null;
  repaint();
  let accepted = null;
  try {
    accepted = await postErasureRetry(requestId, { principal_id: principal });
  } catch (error) {
    state.erasureError = error;
  }
  state.erasing = false;
  if (accepted) {
    state.erasure = null;
    await followErasure(accepted, repaint);
  } else repaint();
}

/** Poll the progress of an accepted request until it ends, then read its record once (DEC-863). */
async function followErasure(accepted, repaint) {
  state.erasureJob = { ...accepted, progress: [] };
  repaint();
  for (;;) {
    try {
      const progress = await getErasureProgress(accepted.request_id);
      state.erasureJob = { ...state.erasureJob, ...progress };
    } catch (error) {
      state.erasureError = error;
      repaint();
      return;
    }
    repaint();
    if (TERMINAL.has(state.erasureJob.status)) break;
    await new Promise((resolve) => setTimeout(resolve, erasurePollMs));
  }
  try {
    state.erasure = await getErasure(accepted.request_id);
  } catch (error) {
    state.erasureError = error;
  }
  await loadRegister(); // the register and the flags carry the finished request too
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
  "pb-erasure-retry": submitRetry,
  "pb-access": submitAccess,
};

/**
 * A destructive button stays disabled until its "I understand" box is ticked (`data-needs-tick`
 * names the box). The form's own check still refuses an unticked submit.
 */
function bindTicks(main) {
  const sync = () => {
    for (const button of main.querySelectorAll("[data-needs-tick]")) {
      if (button.hasAttribute("data-pb-gate")) continue; // refused by role: gate.js owns it
      const box = main.querySelector(`#${button.getAttribute("data-needs-tick")}`);
      button.disabled = !(box && box.checked) || state.erasing;
    }
  };
  sync();
  main.addEventListener("change", (event) => {
    if (event.target && event.target.type === "checkbox") sync();
  });
}

/** One delegated submit listener on `<main>` (replaced by every paint, so listeners never pile up). */
export function bindPrivacy(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindAuditLinks(main);
  bindFileNames(main);
  bindTicks(main);
  main.addEventListener("submit", (event) => {
    const form = event.target;
    const handler = form && SUBMITS[form.id];
    if (!handler) return;
    event.preventDefault();
    if (state.importing || state.looking || state.erasing || state.exporting) return;
    handler(form, repaint);
  });
}

/** Test seam: how long the erasure screen waits between progress reads. */
export function _setErasurePollForTests(ms) {
  erasurePollMs = ms;
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
    erasureJob: null,
    erasureError: null,
    register: null,
    registerError: null,
    flags: null,
    exporting: false,
    exported: null,
    exportError: null,
  });
}
