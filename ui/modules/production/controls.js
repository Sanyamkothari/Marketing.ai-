// Small pieces the Phase 4b screens share (users, audit, privacy, monitoring, campaigns, approvals):
// a control drawn already refused, tab strips, status words, the two-step danger confirm, inline field
// errors, plain words for the codes a scheduled job or a store answers with, people and use cases by
// name, and a table with row attributes.
//
// A control on one of *this module's* screens is refused as it is drawn, not afterwards by
// `gate.js`'s table (DEC-792). The table exists for other workstreams' screens, which this branch may
// not edit; these screens are ours, so they ask `reasonFor(method, path)` - the very `GET /auth/me`
// permission list the table reads - while painting. What a person sees is identical either way: the
// control stays on screen, disabled, with the server's own sentence beside it and as its title, and
// it carries the same `data-pb-gate` marker, so `gate.js`'s capture-phase listener swallows a click or
// submit on it exactly as it does for a gated control anywhere else. Disabled and explained, never
// hidden: a Viewer should learn why the Run now button does nothing for them (plan M46).
//
// v1 (docs/ui/FOUNDATION.md): codes, ids and hashes are never the headline. A status is a word in a
// pill (the raw value stays in `data-status`); a code is a plain sentence (the raw code stays in
// `data-code` and under Details); a person is a name, never a user id.

import { getIndustries } from "../../api.js";
import { EM_DASH, crumbs, esc, glossaryCode, pageHead, present, techDetails } from "../../dom.js";
import { getUsers } from "./api.js";
import { can, currentMe, reasonFor } from "./session.js";

/**
 * Every screen's header in the one v1 order: breadcrumb (Home first), H1, one sentence, then the
 * header actions (already built with `headActions`). `trail` are the crumbs between Home and this
 * screen, each `{ label, href }`.
 */
export const screenHead = ({ trail = [], title, crumb = null, desc = "", actions = "" }) =>
  pageHead(
    `${crumbs([...trail, { label: crumb || title }])}<h1 class="h1">${esc(title)}</h1>${
      desc ? `<p class="desc">${esc(desc)}</p>` : ""
    }${actions}`,
  );

/** `attrs` is raw HTML attributes; `label` is text. The button is refused when the route is. */
export function actionButton(
  method,
  path,
  { attrs = "", label, cls = "btn primary", type = "button", busy = false, explain = true } = {},
) {
  const reason = reasonFor(method, path);
  if (reason) {
    return `<button type="${type}" class="${cls} pb-off" ${attrs} disabled data-pb-gate="${esc(reason)}" title="${esc(
      reason,
    )}">${esc(label)}</button>${explain ? `<span class="pb-why" role="note">${esc(reason)}</span>` : ""}`;
  }
  return `<button type="${type}" class="${cls}" ${attrs}${busy ? " disabled" : ""}>${esc(label)}</button>`;
}

/** A tab strip: `[key, label, href]` rows, the current one marked. */
export const tabStrip = (current, entries, label) =>
  `<div class="tabs-bar pb-tabs"><nav class="tabs" aria-label="${esc(label)}">${entries
    .map(
      ([key, text, href]) =>
        `<a class="tab${key === current ? " on" : ""}" href="${href}"${key === current ? ' aria-current="page"' : ""}>${esc(
          text,
        )}</a>`,
    )
    .join("")}</nav></div>`;

/**
 * The whole screen refused: the server's own sentence where the content would be, and a way home.
 * No code on screen (the refusal is not a fault); `data-code` keeps it for anyone who needs it.
 */
export const refusal = (reason) =>
  `<div class="apierr pb-refused" role="alert" data-code="ROLE_REQUIRED"><b>${esc(
    reason,
  )}</b><div class="btn-row"><a class="btn secondary sm" href="#/">Go to Home</a></div></div>`;

// --- status words -----------------------------------------------------------------------------------

const PILL = {
  // firings and runs
  succeeded: "ok",
  done: "ok",
  running: "warn",
  queued: "warn",
  pending: "warn",
  failed: "bad",
  missed: "bad",
  cancelled: "warn",
  // erasure register
  completed: "ok",
  completed_with_exceptions: "warn",
  // audit outcomes
  success: "ok",
  denied: "warn",
  // alert severities
  info: "ok",
  warning: "warn",
  critical: "bad",
  // consent states
  valid: "ok",
  granted: "ok",
  withdrawn: "bad",
  expired: "warn",
  none: "none",
  // model versions
  pending_approval: "warn",
  champion: "ok",
  approved: "ok",
  rejected: "bad",
  archived: "none",
};

/** The word a person reads for each status the API sends. */
export const STATUS_WORDS = {
  succeeded: "Done",
  done: "Done",
  running: "Running",
  queued: "Waiting",
  pending: "Waiting",
  failed: "Failed",
  missed: "Missed",
  cancelled: "Cancelled",
  completed: "Done",
  completed_with_exceptions: "Done, with problems",
  success: "Done",
  denied: "Refused",
  info: "Info",
  warning: "Warning",
  critical: "Critical",
  valid: "Given",
  granted: "Given",
  withdrawn: "Withdrawn",
  expired: "Expired",
  none: "No record",
  pending_approval: "Waiting for approval",
  champion: "In use",
  approved: "Approved",
  rejected: "Rejected",
  archived: "Archived",
};

/** `completed_with_exceptions` → "Completed with exceptions": the fallback for a status not listed. */
const sentenceCase = (value) => {
  const text = String(value).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

export const statusWord = (status) => (present(status) ? STATUS_WORDS[status] || sentenceCase(status) : EM_DASH);

/**
 * A status as the house pill, in words; the raw value stays in `data-status`. An unknown status reads
 * neutral (`warn`) rather than green; "no record" and "archived" read grey.
 */
export const statusPill = (status, words = null) => {
  if (!present(status)) return EM_DASH;
  const tone = PILL[status] || "warn";
  const cls = tone === "none" ? "pill pb-pill-none" : `pill ${tone}`;
  return `<span class="${cls}" data-status="${esc(status)}">${esc(words || statusWord(status))}</span>`;
};

// --- codes in plain words ---------------------------------------------------------------------------

/**
 * Plain words for the codes a scheduled job, a firing or a store answers with, for the ones
 * `configs/pilot/help.yaml` does not (yet) catalogue. The catalogue wins when it has the code.
 */
export const CODE_WORDS = {
  // what a firing did
  SCORING_STARTED: "Scoring started",
  TRAINING_STARTED: "Retraining started",
  RETRAIN_FOR_ERASURE: "Retraining started, because customer data was erased",
  DRIFT_OK: "New customers look like the ones the model learned from",
  DRIFT_ABOVE_THRESHOLD: "New customers look very different from the ones the model learned from",
  DRIFT_NOT_MEASURED: "Could not measure whether new customers look different",
  // why a firing failed
  DATASET_COMPOSITE_KEY_NOT_WIRED:
    "The dataset has more than one row per customer, so it cannot be scheduled yet. Save the recipe with a single snapshot.",
  DATASET_NOT_BUILT: "The dataset has not finished building",
  DATASET_NOT_FOUND: "The dataset could not be found",
  DATASET_USE_CASE_MISMATCH: "The dataset was built for a different use case",
  DATASET_CLIENT_MISMATCH: "The dataset belongs to another client",
  DATASET_CHECKS_FAILED: "The data did not pass its checks",
  VALIDATION_FAILED: "The data did not pass its checks",
  SCHEDULE_MISSED: "Missed while Marketing AI was switched off",
  SCHEDULE_NO_DATA: "No recipe or dataset is set for this schedule",
  FIRING_ABANDONED: "Stopped because it took too long",
  FIRING_FAILED: "The job failed",
  RUN_NOT_FOUND: "The run could not be found",
  NO_CHAMPION: "No approved model to use yet",
  CHAMPION_NOT_FOUND: "No approved model to use yet",
  NO_TRAINING_DATA: "There is no data to train on",
  NO_SCORED_DATA: "There is no scored data to compare with",
  ONBOARDING_INPUT_MISSING: "This month's tables have not been uploaded",
  ONBOARDING_SPEC_NOT_FOUND: "The recipe could not be found",
  ONBOARDING_SPEC_USE_CASE_MISMATCH: "The recipe was made for a different use case",
  ONBOARDING_SPEC_HAS_NO_LABEL: "The recipe does not say what outcome to learn",
  CLIENT_STORE_UNAVAILABLE: "The client's tables could not be read",
  USE_CASE_UNAVAILABLE: "The use case is not available",
  MODEL_NOT_REGISTERED: "The model could not be found",
  // erasure stores
  STORE_WRITE_FAILED: "A file could not be changed",
  ERASURE_STORE_FAILED: "Some files could not be changed",
  // retention
  RUN_IN_PROGRESS: "A run is still using it",
  // outcomes
  PERFORMANCE_DROP: "The model did worse on real outcomes than in testing",
};

/** The plain sentence for a code, or `null` when neither the catalogue nor this file knows it. */
export function plainCode(code) {
  if (!present(code)) return null;
  const entry = glossaryCode(code);
  if (entry && entry.title) return entry.title;
  return CODE_WORDS[code] || null;
}

/** A code as a person reads it: its plain words (or `fallback`), the raw code kept in `data-code`. */
export function codeWords(code, fallback = "") {
  if (!present(code)) return fallback ? esc(fallback) : EM_DASH;
  const words = plainCode(code) || fallback || sentenceCase(String(code).toLowerCase());
  return `<span class="pb-code" data-code="${esc(code)}">${esc(words)}</span>`;
}

/** A raw code in monospace, or the em dash: for a "Show more columns" cell or a Details list. */
export const mono = (value) => (present(value) ? `<span class="mono">${esc(value)}</span>` : EM_DASH);

// --- forms --------------------------------------------------------------------------------------------

/**
 * A check the screen makes before sending anything ("type the id first"): one line under the form in
 * `--bad`, never a box with a code. `data-code` keeps the reason for tests and support.
 */
export const fieldError = (error, id = "") =>
  error
    ? `<p class="pb-invalid" role="alert"${id ? ` id="${esc(id)}"` : ""} data-code="${esc(error.code || "INVALID")}">${esc(
        error.message || String(error),
      )}</p>`
    : "";

/** Codes the screens raise themselves, before a request: shown with `fieldError`, not `errorBox`. */
export const LOCAL_CODES = new Set([
  "PRINCIPAL_ID_REQUIRED",
  "CONFIRMATION_REQUIRED",
  "CONSENT_FILE_REQUIRED",
  "OUTCOME_FILE_REQUIRED",
  "REASON_REQUIRED",
]);

export const isLocal = (error) => Boolean(error && LOCAL_CODES.has(error.code));

/** A labelled text input, label above, an optional helper line below. */
export const textField = (name, label, { value = "", placeholder = "", attrs = "", hint = "" } = {}) =>
  `<label class="field pb-field"><span class="sub">${esc(label)}</span><span class="control"><input name="${esc(
    name,
  )}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false" ${attrs}></span>${
    hint ? `<span class="pb-hint">${esc(hint)}</span>` : ""
  }</label>`;

/** A labelled select: `options` are `[value, label]`. */
export const selectField = (name, label, options, { value = "", attrs = "", hint = "" } = {}) =>
  `<label class="field pb-field"><span class="sub">${esc(label)}</span><span class="control sel"><select name="${esc(
    name,
  )}" ${attrs}>${options
    .map(([v, text]) => `<option value="${esc(v)}"${String(v) === String(value) ? " selected" : ""}>${esc(text)}</option>`)
    .join("")}</select></span>${hint ? `<span class="pb-hint">${esc(hint)}</span>` : ""}</label>`;

/** A file picker in the house `.control.file` look; the input keeps its `name`. */
export const fileField = (name, label, { accept = "", attrs = "" } = {}) =>
  `<label class="control file pb-file"><input type="file" class="sr" name="${esc(name)}"${
    accept ? ` accept="${esc(accept)}"` : ""
  } ${attrs}><span class="fname">${esc(label)}</span><span class="ico" aria-hidden="true">⤒</span></label>`;

/** A form's trimmed text value by name, or "" when the field is absent. */
export function fieldValue(form, name) {
  const field = form.elements.namedItem(name);
  return field && typeof field.value === "string" ? field.value.trim() : "";
}

/** Show a chosen file's name in its `.control.file` label (the input itself is visually hidden). */
export function bindFileNames(root) {
  root.addEventListener("change", (event) => {
    const input = event.target;
    if (!input || input.type !== "file") return;
    const label = input.closest(".control.file");
    const name = label && label.querySelector(".fname");
    const file = input.files && input.files[0];
    if (name && file) {
      name.textContent = file.name;
      label.classList.add("has");
    }
  });
}

// --- the two-step danger confirm --------------------------------------------------------------------

/**
 * The second step of a destructive action, where the first button stood: the question, a filled
 * danger "Yes, …" and a quiet "Keep". `yes` is already-built button HTML (so it can be refused);
 * `noAttrs` marks the Keep button for the screen's click handler.
 */
export const dangerConfirm = (question, yes, noAttrs, noLabel = "Keep") =>
  `<span class="pb-confirm" role="group" aria-label="${esc(question)}"><span class="pb-confirm-q">${esc(
    question,
  )}</span>${yes}<button type="button" class="btn quiet sm" ${noAttrs}>${esc(noLabel)}</button></span>`;

// --- tables -----------------------------------------------------------------------------------------

/**
 * `dataTable` (dom.js) with row attributes and extra rows: `rows` are `{ attrs, cells, after }` -
 * `cells` already-escaped HTML, `after` more `<tr>` markup (a message under the row). Columns as in
 * `dataTable`: `{ label, num, more }`; `more` columns sit behind "Show more columns"; stacked below 700px.
 */
export function rowsTable(cols, rows, { cls = "", moreLabel = "Show more columns" } = {}) {
  const classes = (c) => [c.num ? "num" : "", c.more ? "more" : "", c.cls || ""].filter(Boolean).join(" ");
  const attr = (c) => (classes(c) ? ` class="${classes(c)}"` : "");
  const hidden = cols.filter((c) => c.more).length;
  const toggle = hidden
    ? `<details class="tbl-more"><summary>${esc(moreLabel)} (${hidden})</summary></details>`
    : "";
  return `${toggle}<div class="tbl-wrap"><table class="tbl tbl-stack${cls ? ` ${esc(cls)}` : ""}"><thead><tr>${cols
    .map((c) => `<th${attr(c)} scope="col">${c.label ? esc(c.label) : `<span class="sr">${esc(c.sr || "Actions")}</span>`}</th>`)
    .join("")}</tr></thead><tbody>${rows
    .map(
      (r) =>
        `<tr${r.attrs ? ` ${r.attrs}` : ""}>${r.cells
          .map((v, j) => {
            const col = cols[j] || {};
            return `<td${attr(col)} data-label="${esc(col.label || "")}"><div class="pb-cell">${v}</div></td>`;
          })
          .join("")}</tr>${r.after || ""}`,
    )
    .join("")}</tbody></table></div>`;
}

/** A message row under a table row, across every column. */
export const spanRow = (span, html) => `<tr class="pb-msg-row"><td colspan="${span}">${html}</td></tr>`;

// --- people by name ---------------------------------------------------------------------------------

let people = null; // Map user_id → display name, when `GET /users` may be read

/**
 * Learn everyone's name, when this person may read the user list (an Admin); otherwise nothing is
 * asked and people are named "you" or `fallback`. Failure is quiet: a name is a courtesy.
 */
export async function loadPeople() {
  if (!can("GET", "/users")) return;
  try {
    const body = await getUsers();
    rememberPeople(body.users || []);
  } catch {
    // an older API, or sign-in off with no user store: ids stay unnamed
  }
}

/** Keep names already fetched (the Users screen passes its own list). */
export function rememberPeople(users) {
  people = new Map((users || []).map((u) => [u.user_id, u.display_name || u.username || u.user_id]));
}

/** The people list as `[user_id, name]`, sorted by name, or `null` when it was not readable. */
export const knownPeople = () =>
  people ? [...people].sort((a, b) => a[1].localeCompare(b[1])) : null;

const SYSTEM_NAMES = {
  "system:bootstrap": "Set up at install",
  "system:scheduler": "the scheduler",
  "local-operator": "the local operator",
  anonymous: "someone not signed in",
};

/**
 * A person as a name: "you" for the one signed in, the name from the user list, a plain name for
 * the system's own actors - and `fallback` ("another user") for an id nobody here may resolve.
 * A value that is not an id at all (an older record's name) is shown as it is.
 */
export function personName(id, fallback = "another user") {
  if (!present(id)) return EM_DASH;
  const me = currentMe();
  if (me && me.principal && me.principal.user_id === id) return "you";
  if (SYSTEM_NAMES[id]) return SYSTEM_NAMES[id];
  if (String(id).startsWith("system:")) return "Marketing AI";
  if (people && people.has(id)) return people.get(id);
  if (/^u-[0-9a-f]{6,}$/i.test(String(id))) return fallback;
  return String(id);
}

// --- use cases by name ------------------------------------------------------------------------------

let useCases = null; // Map id → name from GET /industries
let useCasesAsked = null;

/** Every configured use case's name, once per page. Failure leaves ids as they are. */
export function loadUseCaseNames() {
  if (!useCasesAsked) {
    useCasesAsked = getIndustries()
      .then((payload) => {
        const seen = new Map();
        for (const industry of payload.industries || []) {
          for (const stage of industry.stages || []) {
            for (const uc of stage.use_cases || []) if (!seen.has(uc.id)) seen.set(uc.id, uc.name);
          }
        }
        useCases = seen;
      })
      .catch(() => {
        useCasesAsked = null; // ask again next time
      });
  }
  return useCasesAsked;
}

/** `[{id, name}]` of every use case, or `null` while unknown. */
export const useCaseList = () => (useCases ? [...useCases].map(([id, name]) => ({ id, name })) : null);

/** A use case's name, or its id when the catalogue could not be read. */
export const useCaseName = (id, name = null) => name || (useCases && useCases.get(id)) || id || EM_DASH;

// --- the audit reference ----------------------------------------------------------------------------

/**
 * Where to find the one audit event a change wrote: a quiet "Open in the audit log" for an Admin
 * (`data-audit-*`, handled by the screen), and the action and object id under Technical details.
 * `extra` are more `[label, value]` pairs for the same disclosure.
 */
export function auditReference(action, objectId, extra = []) {
  const link = can("GET", "/audit/events")
    ? `<button type="button" class="btn quiet sm" data-audit-action="${esc(action)}" data-audit-object="${esc(
        objectId,
      )}">Open in the audit log</button>`
    : "";
  const line = `<p class="mono pb-audit-line">Audit: ${esc(action)} on ${esc(objectId)}.</p>`;
  const tech = techDetails(extra);
  const details = tech
    ? tech.replace(/<\/details>$/, `${line}</details>`)
    : `<details class="tech"><summary>Technical details</summary>${line}</details>`;
  return `<div class="pb-audit-ref"><span class="pb-small">This is recorded in the audit log.</span>${link}</div>${details}`;
}

/** Test seam: forget the names learnt. */
export function _resetNamesForTests() {
  people = null;
  useCases = null;
  useCasesAsked = null;
}
