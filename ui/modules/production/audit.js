// The audit viewer (Phase 4b M47, Admin): who did what, when, to which object, and how it ended.
//
// Filters are exactly `GET /audit/events`'s - actor, action (exact, or a family ending in `.` such as
// `models.`), object type and id, outcome, and a date range - so what is on screen, what the CSV
// holds and what a retained export covers are one query, never three that drift (DEC-794).
//
// v1: three filters a person understands come first - Who (people by name; it sends `actor_id`),
// What (plain families, each the same action prefix the API filters by) and the date range - and the
// rest sit behind "More filters". The query sent is unchanged. The table reads When / Who / What /
// Result. "What" is plain words only - every action the API audits has its own, and one it does not
// know yet reads as a verb and a thing ("Created a …"), never as a code; the raw action (`auth.login`)
// and everything else - object, request id, details - is in the row's own Details. Runs of identical
// failures collapse into one row.
//
// Dates are whole UTC days: "from" is that day's 00:00Z (inclusive) and "to" is the *next* day's
// 00:00Z, because the API's `until` is exclusive and a person who picks "to 23 Sep" means to include
// the 23rd. UTC, not the browser's zone, because the log is stored and exported in UTC and an Admin
// comparing this screen with an export or an S3 object must see the same window.
//
// Pages of 50, newest first, with the total the API counts across every page; the CSV is a link the
// signed-in download listener fetches with the token (DEC-793), so the audited read is attributed to
// the Admin who took it. "Save a tamper-proof copy" is `POST /audit/exports` over the same window
// (S3 Object Lock in COMPLIANCE mode, or JSON lines under the data directory locally, DEC-715): the
// screen shows where it was written and its SHA-256, which is what a reviewer checks it against.
//
// Nothing here renders a data value: the log never holds one (DEC-705), and `details` has a closed
// set of keys, each shown as `key=value` exactly as recorded.

import { EM_DASH, errorBox, esc, fmtInt, fmtStamp, present, techDetails } from "../../dom.js";
import { auditCsvUrl, getAuditEvents, postAuditExport } from "./api.js";
import {
  knownPeople,
  loadPeople,
  personName,
  refusal,
  rowsTable,
  selectField,
  statusPill,
  textField,
} from "./controls.js";
import { reasonFor } from "./session.js";
import { adminHead } from "./users.js";

export const PAGE_SIZE = 50;

const OUTCOMES = ["success", "denied", "failed"];
const OUTCOME_WORDS = { success: "Done", denied: "Refused", failed: "Failed" };

/** "What": plain families, each exactly the action prefix `GET /audit/events` filters by. */
export const FAMILIES = [
  ["auth.", "Sign-ins and sign-outs"],
  ["users.", "People and roles"],
  ["models.", "Model approvals"],
  ["runs.", "Training, scoring and downloads"],
  ["uplift.", "Uplift models and campaign results"],
  ["datasets.", "Datasets"],
  ["sources.", "Source tables"],
  ["mappings.", "Column mappings"],
  ["onboarding_specs.", "Recipes"],
  ["clients.", "Clients"],
  ["schedules.", "Schedules"],
  ["monitoring.", "Alerts and outcomes"],
  ["privacy.", "Privacy requests"],
  ["copy.", "Campaign copy"],
  ["indexes.", "AI assistant"],
  ["pilot.", "Reports and feedback"],
  ["settings.", "Settings"],
  ["audit.", "Use of the audit log"],
];

/** Plain words for every action the API audits (`api/access_policy.py`, the routes' own tables). */
const ACTION_WORDS = {
  "auth.login": "Signed in",
  "auth.logout": "Signed out",
  "auth.me": "Checked who is signed in",
  "users.create": "Added a person",
  "users.update": "Changed a person",
  "users.list": "Looked at the people list",
  "users.roles_change": "Changed a person's roles",
  "users.password_change": "Changed a password",
  "users.password_reset": "Reset a password",
  "approvals.list": "Looked at models waiting for approval",
  "models.approve": "Approved a model",
  "models.reject": "Rejected a model",
  "models.promote": "Put a model in use",
  "models.list": "Looked at the models",
  "runs.create": "Started a run",
  "runs.cancel": "Cancelled a run",
  "runs.list": "Looked at the runs",
  "runs.read": "Looked at a run",
  "runs.root_cause": "Asked why a run's results changed",
  "runs.scores_download": "Downloaded scored customers",
  "runs.artefact_download": "Opened a run file",
  "uplift.runs_create": "Started an uplift run",
  "uplift.artefact_download": "Opened an uplift run file",
  "uplift.campaign_results": "Measured a campaign's results",
  "uplift.campaign_results_read": "Looked at a campaign's results",
  "uplift.ope": "Tried out a targeting rule",
  "uploads.create": "Uploaded a file",
  "uploads.profile": "Checked an uploaded file",
  "uploads.treatment_candidates": "Checked an upload for campaign columns",
  "datasets.create": "Built a dataset",
  "datasets.list": "Looked at the datasets",
  "datasets.read": "Looked at a dataset",
  "datasets.report": "Looked at a dataset's report",
  "datasets.sample": "Looked at a dataset's sample rows",
  "datasets.features_sql": "Downloaded a dataset's SQL",
  "datasets.lineage": "Looked at where a dataset came from",
  "sources.create": "Added a source table",
  "sources.update": "Changed a source table",
  "sources.delete": "Removed a source table",
  "sources.list": "Looked at the source tables",
  "mappings.save": "Saved a column mapping",
  "mappings.suggest": "Asked for suggested column mappings",
  "mappings.list": "Looked at the column mappings",
  "onboarding_specs.create": "Saved a recipe",
  "onboarding_specs.list": "Looked at the recipes",
  "onboarding_specs.preview": "Previewed a recipe",
  "onboarding_specs.replay": "Re-ran a recipe",
  "clients.create": "Added a client",
  "clients.create_default": "Set up the first client",
  "clients.list": "Looked at the clients",
  "clients.read": "Looked at a client",
  "industries.list": "Looked at the use cases",
  "use_cases.read": "Looked at a use case",
  "use_cases.standard_schema": "Looked at a use case's standard columns",
  "use_cases.template": "Downloaded a data template",
  "use_cases.template_readme": "Downloaded a data template's notes",
  "reference_sets.create": "Added reference material",
  "schedules.create": "Created a schedule",
  "schedules.update": "Changed a schedule",
  "schedules.delete": "Deleted a schedule",
  "schedules.enable": "Resumed a schedule",
  "schedules.disable": "Paused a schedule",
  "schedules.fire": "Ran a schedule",
  "schedules.missed": "Recorded missed runs",
  "schedules.list": "Looked at the schedules",
  "schedules.read": "Looked at a schedule",
  "schedules.firings": "Looked at a schedule's history",
  "schedules.retraining_sync": "Updated the retraining schedules",
  "monitoring.alert_acknowledge": "Took on an alert",
  "monitoring.alerts_list": "Looked at the alerts",
  "monitoring.missed_firings": "Looked at missed runs",
  "monitoring.outcomes_upload": "Added campaign outcomes",
  "monitoring.outcomes_read": "Looked at a campaign's outcomes",
  "monitoring.incrementality_read": "Looked at a campaign's effect",
  "privacy.erasure": "Erased a customer",
  "privacy.erasure.complete": "Finished erasing a customer",
  "privacy.erasure.retry": "Retried erasing a customer",
  "privacy.erasure.list": "Looked at the erasure requests",
  "privacy.erasure.read": "Looked at an erasure request",
  "privacy.erasure.progress": "Checked an erasure's progress",
  "privacy.access_request": "Exported a customer's data",
  "privacy.consent.import": "Imported consent records",
  "privacy.consent.record": "Recorded a customer's consent",
  "privacy.consent.lookup": "Looked up a customer's consent",
  "privacy.consent_report.read": "Looked at who a run left out",
  "privacy.policy.read": "Looked at the privacy rules",
  "privacy.retention.plan": "Checked what is past its keep-until date",
  "privacy.retention.apply": "Deleted data past its keep-until date",
  "privacy.retrain_flags.list": "Looked at models to retrain after an erasure",
  "copy.approve": "Approved campaign copy",
  "copy.generate": "Wrote campaign copy",
  "copy.regenerate": "Rewrote campaign copy",
  "copy.messages_download": "Downloaded campaign messages",
  "indexes.ask": "Asked the AI assistant",
  "indexes.create": "Built a knowledge index",
  "indexes.evaluate": "Tested a knowledge index",
  "indexes.list": "Looked at the knowledge indexes",
  "indexes.read": "Looked at a knowledge index",
  "pilot.feedback_create": "Sent feedback",
  "pilot.feedback_export": "Downloaded all feedback",
  "pilot.data_request_read": "Looked at the data request",
  "pilot.demo_read": "Looked at the demo",
  "pilot.demo_raw_download": "Downloaded the demo data",
  "pilot.help_read": "Opened the help",
  "pilot.readiness_read": "Looked at the pilot readiness check",
  "pilot.results_read": "Looked at the pilot results",
  "pilot.roi_read": "Looked at a campaign's value",
  "pilot.roi_inputs_save": "Saved a campaign's value inputs",
  "pilot.template_read": "Looked at a report template",
  "settings.aws_connection.read": "Looked at the AI service connection",
  "settings.aws_connection.update": "Changed the AI service connection",
  "settings.aws_connection.test": "Tested the AI service connection",
  "settings.aws_connection.reset": "Reset the AI service connection",
  "audit.query": "Searched the audit log",
  "audit.download": "Downloaded the audit log",
  "audit.export": "Saved a copy of the audit log",
  "health.read": "Checked the service is up",
  "request.unmatched": "Asked for an address that does not exist",
  "request.no_policy": "Made a request with no access rule",
};

/** An action not listed above: its last verb in the past tense … */
const VERB_WORDS = {
  create: "Created",
  update: "Changed",
  delete: "Deleted",
  save: "Saved",
  list: "Looked at",
  read: "Looked at",
  preview: "Previewed",
  replay: "Re-ran",
  approve: "Approved",
  reject: "Rejected",
  generate: "Wrote",
  regenerate: "Rewrote",
  download: "Downloaded",
  export: "Exported",
  import: "Imported",
  enable: "Turned on",
  disable: "Turned off",
  test: "Tested",
  reset: "Reset",
  upload: "Uploaded",
  cancel: "Cancelled",
  retry: "Retried",
};

/** … and the thing its family names. */
const FAMILY_NOUNS = {
  "auth.": "sign-in",
  "users.": "a person",
  "approvals.": "approvals",
  "models.": "a model",
  "runs.": "a run",
  "uplift.": "an uplift run",
  "uploads.": "an upload",
  "datasets.": "a dataset",
  "sources.": "a source table",
  "mappings.": "a column mapping",
  "onboarding_specs.": "a recipe",
  "clients.": "a client",
  "use_cases.": "a use case",
  "reference_sets.": "reference material",
  "schedules.": "a schedule",
  "monitoring.": "monitoring",
  "privacy.": "a privacy request",
  "copy.": "campaign copy",
  "indexes.": "a knowledge index",
  "pilot.": "the pilot",
  "settings.": "the settings",
  "audit.": "the audit log",
};

const words = (text) => String(text).replace(/[._]+/g, " ").trim();
const sentence = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

/**
 * The plain label of an action, e.g. `users.update` → "Changed a person". One not listed above reads
 * as its verb and the thing its family names - `datasets.create` → "Created a dataset", `feeds.archive`
 * → "Archive feeds" - never as the bare code, which is in the row's Details.
 */
export function actionWords(action) {
  if (!action) return EM_DASH;
  if (ACTION_WORDS[action]) return ACTION_WORDS[action];
  const cut = action.indexOf(".");
  const prefix = cut > 0 ? action.slice(0, cut + 1) : "";
  const parts = (cut > 0 ? action.slice(cut + 1) : action).split(/[._]+/).filter(Boolean);
  const last = parts[parts.length - 1] || "";
  const middle = parts.slice(0, -1).join(" ");
  const noun = middle || FAMILY_NOUNS[prefix] || words(prefix) || "something";
  if (VERB_WORDS[last]) return `${VERB_WORDS[last]} ${noun}`;
  return sentence(`${parts.join(" ")}${prefix || middle ? ` ${FAMILY_NOUNS[prefix] || words(prefix)}` : ""}`.trim());
}

/** The form's fields; `from`/`to` are `YYYY-MM-DD` as a date input gives them. */
const emptyForm = () => ({
  actor_id: "",
  family: "",
  action: "",
  object_type: "",
  object_id: "",
  outcome: "",
  from: "",
  to: "",
});

const state = {
  form: emptyForm(),
  offset: 0,
  page: null,
  loadError: null,
  loading: false,
  exporting: false,
  exportResult: null,
  exportError: null,
};

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** `2026-09-23` → `2026-09-23T00:00:00Z`; anything else → null. */
export function dayStart(day) {
  if (!DAY.test(day || "")) return null;
  const at = new Date(`${day}T00:00:00Z`);
  return Number.isNaN(at.getTime()) ? null : at.toISOString().replace(".000Z", "Z");
}

/** `2026-09-23` → `2026-09-24T00:00:00Z`: the exclusive end that includes the whole chosen day. */
export function dayAfter(day) {
  const start = dayStart(day);
  if (!start) return null;
  const at = new Date(start);
  at.setUTCDate(at.getUTCDate() + 1);
  return at.toISOString().replace(".000Z", "Z");
}

/**
 * The screen's form as `GET /audit/events` filters: trimmed, empty ones dropped, days made instants.
 * The typed action (More filters) wins over the "What" family, which is the same prefix filter.
 */
export function filtersOf(form) {
  const filters = {};
  const merged = { ...form, action: (form.action || "").trim() || (form.family || "").trim() };
  for (const key of ["actor_id", "action", "object_type", "object_id", "outcome"]) {
    const value = (merged[key] || "").trim();
    if (value) filters[key] = value;
  }
  const since = dayStart(form.from);
  const until = dayAfter(form.to);
  if (since) filters.since = since;
  if (until) filters.until = until;
  return filters;
}

const detailsText = (details) =>
  Object.entries(details || {})
    .map(([key, value]) => `${key}=${value === null ? EM_DASH : value}`)
    .join(" · ");

/** Who did it, as a name: a signed-in person, "someone not signed in", or Marketing AI itself. */
function who(event) {
  if (event.actor_kind === "anonymous") return "Someone not signed in";
  const name = personName(event.actor_id);
  return name === "you" ? "You" : name;
}

/** Collapse runs of the same failure by the same actor (a script retrying a bad password, say). */
export function collapseRepeats(events) {
  const out = [];
  for (const event of events) {
    const prev = out[out.length - 1];
    const same =
      prev &&
      event.outcome !== "success" &&
      prev.event.outcome === event.outcome &&
      prev.event.action === event.action &&
      prev.event.actor_id === event.actor_id &&
      detailsText(prev.event.details) === detailsText(event.details);
    if (same) prev.count += 1;
    else out.push({ event, count: 1 });
  }
  return out;
}

function eventRow({ event, count }) {
  const object = [event.object_type, event.object_id].filter(Boolean).join(" · ");
  const more = techDetails(
    [
      ["Action", event.action],
      ["Object", object],
      ["Who (id)", `${event.actor_id} (${event.actor_kind})`],
      ["Request", event.request_id],
      ["Details", Object.keys(event.details || {}).length ? detailsText(event.details) : null],
      ["Event", event.event_id],
    ],
    "Details",
  );
  const repeated = count > 1 ? ` <span class="pb-small">× ${esc(fmtInt(count))} in a row</span>` : "";
  return {
    attrs: `data-event="${esc(event.event_id || "")}"`,
    cells: [
      esc(fmtStamp(event.occurred_at)),
      esc(who(event)),
      `${esc(actionWords(event.action))}${repeated}${more}`,
      statusPill(event.outcome),
    ],
  };
}

function whoField() {
  const people = knownPeople();
  if (!people) return textField("actor_id", "Who", { value: state.form.actor_id, placeholder: "a user id" });
  const options = [["", "Anyone"], ...people];
  if (state.form.actor_id && !people.some(([id]) => id === state.form.actor_id)) {
    options.push([state.form.actor_id, state.form.actor_id]);
  }
  return selectField("actor_id", "Who", options, { value: state.form.actor_id });
}

function filterCard() {
  const f = state.form;
  const moreOpen = Boolean(f.action || f.object_type || f.object_id || f.outcome);
  return `<section class="card"><h3>Find events</h3><div class="card-body"><form id="pb-audit-filters" class="pb-stack" novalidate>
    <div class="pb-filters">
      ${whoField()}
      ${selectField("family", "What", [["", "Anything"], ...FAMILIES], { value: f.family })}
      <label class="field pb-field"><span class="sub">From (UTC day)</span><span class="control"><input type="date" name="from" value="${esc(
        f.from,
      )}"></span></label>
      <label class="field pb-field"><span class="sub">To (UTC day, included)</span><span class="control"><input type="date" name="to" value="${esc(
        f.to,
      )}"></span></label>
    </div>
    <details class="adv"${moreOpen ? " open" : ""}><summary>More filters</summary><div class="pb-adv-body"><div class="pb-filters">
      ${textField("action", "Exact action", { value: f.action, placeholder: "e.g. models.approve, or models. for all" })}
      ${textField("object_type", "Object type", { value: f.object_type, placeholder: "e.g. model, run, user" })}
      ${textField("object_id", "Object id", { value: f.object_id })}
      ${selectField("outcome", "Result", [["", "Any"], ...OUTCOMES.map((o) => [o, OUTCOME_WORDS[o]])], { value: f.outcome })}
    </div></div></details>
    <div class="pb-form-actions"><button type="submit" class="btn primary" id="pb-audit-apply">Search</button><button type="button" class="btn quiet" id="pb-audit-clear">Clear</button></div>
  </form></div></section>`;
}

function exportActions(filters) {
  const csvRefused = reasonFor("GET", "/audit/events.csv");
  const exportRefused = reasonFor("POST", "/audit/exports");
  const csv = csvRefused
    ? `<span class="pb-small">${esc(csvRefused)}</span>`
    : `<a class="btn secondary sm" id="pb-audit-csv" href="${esc(auditCsvUrl(filters))}" download="audit_events.csv"><span aria-hidden="true">⤓</span> Download CSV</a>`;
  const retained = exportRefused
    ? `<span class="pb-small">${esc(exportRefused)}</span>`
    : `<button type="button" class="btn secondary sm" id="pb-audit-export"${state.exporting ? " disabled" : ""}>${
        state.exporting ? "Saving…" : "Save a tamper-proof copy"
      }</button><span class="pb-hint">Saves these dates and this "What" as a copy nobody can change, for compliance.</span>`;
  const open = Boolean(state.exportResult || state.exportError || state.exporting);
  return `<div class="pb-row-actions">${csv}<details class="pb-more-actions"${open ? " open" : ""}><summary class="btn quiet sm">More actions</summary><div class="pb-row-actions">${retained}</div></details></div>`;
}

function exportOutcome() {
  if (state.exportError) return errorBox(state.exportError);
  const r = state.exportResult;
  if (!r) return "";
  return `<div class="pb-ok" role="status">Saved a copy of ${esc(fmtInt(r.event_count))} event${r.event_count === 1 ? "" : "s"}${
    r.retain_until ? `, kept until ${esc(fmtStamp(r.retain_until))}` : ""
  }.${techDetails([
    ["Where", r.location],
    ["SHA-256", r.sha256],
  ])}</div>`;
}

function resultsCard(filters) {
  if (state.loadError) {
    return `<section class="card"><h3>Events</h3><div class="card-body">${errorBox(state.loadError, {
      title: "We could not load the audit log.",
      retry: true,
    })}</div></section>`;
  }
  if (!state.page) return `<section class="card"><h3>Events</h3><p class="loading pb-pad">Loading…</p></section>`;
  const { events, total, offset } = state.page;
  const first = total ? offset + 1 : 0;
  const last = offset + events.length;
  const table = events.length
    ? rowsTable(
        [{ label: "When" }, { label: "Who" }, { label: "What" }, { label: "Result" }],
        collapseRepeats(events).map(eventRow),
        { cls: "pb-audit" },
      )
    : `<div class="empty-state"><p class="es-t">No event matches these filters.</p><p>Widen the dates, or choose "Anyone" and "Anything".</p></div>`;
  const outcome = exportOutcome();
  return `<section class="card"><h3>Events · ${esc(fmtInt(total))} <span class="sort-note">(newest first)</span></h3>
    <div class="card-body">${exportActions(filters)}${outcome}</div>
    ${table}
    <div class="pb-pager"><span>${total ? `${esc(fmtInt(first))}–${esc(fmtInt(last))} of ${esc(fmtInt(total))}, newest first` : EM_DASH}</span>
      <span class="pb-row-actions"><button type="button" class="btn secondary sm" id="pb-audit-prev"${
        offset <= 0 || state.loading ? " disabled" : ""
      }>‹ Newer</button><button type="button" class="btn secondary sm" id="pb-audit-next"${
        last >= total || state.loading ? " disabled" : ""
      }>Older ›</button></span></div>
  </section>`;
}

export function auditHtml() {
  const refused = reasonFor("GET", "/audit/events");
  const head = adminHead(
    "Audit log",
    "A permanent record of every sign-in, change and download. It cannot be edited. Times show in your own zone.",
  );
  if (refused) return `<main class="screen pb-screen">${head}${refusal(refused)}</main>`;
  const filters = filtersOf(state.form);
  return `<main class="screen pb-screen">${head}<div class="stack">${filterCard()}${resultsCard(filters)}</div></main>`;
}

export async function loadEvents() {
  state.loading = true;
  try {
    const [page] = await Promise.all([
      getAuditEvents(filtersOf(state.form), PAGE_SIZE, state.offset),
      knownPeople() ? null : loadPeople(),
    ]);
    state.page = page;
    state.loadError = null;
  } catch (error) {
    state.loadError = error;
  }
  state.loading = false;
}

function readForm(form) {
  const next = emptyForm();
  for (const key of Object.keys(next)) {
    const field = form.elements.namedItem(key);
    if (field && present(field.value)) next[key] = field.value;
  }
  return next;
}

export function bindAudit(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  const reload = async () => {
    repaint();
    await loadEvents();
    repaint();
  };
  const form = main.querySelector("#pb-audit-filters");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      state.form = readForm(form);
      state.offset = 0;
      state.exportResult = null;
      state.exportError = null;
      reload();
    });
  }
  main.addEventListener("click", async (event) => {
    const target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("#pb-audit-clear")) {
      state.form = emptyForm();
      state.offset = 0;
      reload();
    } else if (target.closest("#pb-audit-prev") && state.page) {
      state.offset = Math.max(0, state.offset - PAGE_SIZE);
      reload();
    } else if (target.closest("#pb-audit-next") && state.page) {
      state.offset += PAGE_SIZE;
      reload();
    } else if (target.closest("#pb-audit-export") && !state.exporting) {
      const filters = filtersOf(state.form);
      const payload = {};
      for (const key of ["since", "until", "action"]) if (filters[key]) payload[key] = filters[key];
      state.exporting = true;
      state.exportError = null;
      state.exportResult = null;
      repaint();
      try {
        state.exportResult = await postAuditExport(payload);
      } catch (error) {
        state.exportError = error;
      }
      state.exporting = false;
      repaint();
    }
  });
}

/**
 * Open the viewer already filtered to one change - the erasure, access export or retention run the
 * privacy screens just made (M48): its action and object id, every date, the first page.
 */
export function openAuditFor(action, objectId, win = window) {
  state.form = { ...emptyForm(), action: action || "", object_id: objectId || "" };
  state.offset = 0;
  state.page = null;
  state.exportResult = null;
  state.exportError = null;
  win.location.hash = "#/admin/audit";
}

/** Route clicks on `controls.auditReference`'s "Open in the audit log" under `root` to `openAuditFor`. */
export function bindAuditLinks(root) {
  root.addEventListener("click", (event) => {
    const link = event.target && event.target.closest ? event.target.closest("[data-audit-action]") : null;
    if (link) openAuditFor(link.dataset.auditAction, link.dataset.auditObject);
  });
}

/** Test seam: forget screen state between cases. */
export function _resetAuditForTests() {
  Object.assign(state, {
    form: emptyForm(),
    offset: 0,
    page: null,
    loadError: null,
    loading: false,
    exporting: false,
    exportResult: null,
    exportError: null,
  });
}
