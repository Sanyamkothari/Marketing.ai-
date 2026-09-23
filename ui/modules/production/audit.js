// The audit viewer (Phase 4b M47, Admin): who did what, when, to which object, and how it ended.
//
// Filters are exactly `GET /audit/events`'s - actor, action (exact, or a family ending in `.` such as
// `models.`), object type and id, outcome, and a date range - so what is on screen, what the CSV
// holds and what a retained export covers are one query, never three that drift (DEC-794).
//
// Dates are whole UTC days: "from" is that day's 00:00Z (inclusive) and "to" is the *next* day's
// 00:00Z, because the API's `until` is exclusive and a person who picks "to 23 Sep" means to include
// the 23rd. UTC, not the browser's zone, because the log is stored and exported in UTC and an Admin
// comparing this screen with an export or an S3 object must see the same window.
//
// Pages of 50, newest first, with the total the API counts across every page; the CSV is a link the
// signed-in download listener fetches with the token (DEC-793), so the audited read is attributed to
// the Admin who took it. "Write a retained export" is `POST /audit/exports` over the same window
// (S3 Object Lock in COMPLIANCE mode, or JSON lines under the data directory locally, DEC-715): the
// screen shows where it was written and its SHA-256, which is what a reviewer checks it against.
//
// Nothing here renders a data value: the log never holds one (DEC-705), and `details` has a closed
// set of keys, each shown as `key=value` exactly as recorded.

import { EM_DASH, errorBox, esc, fmtInt, fmtStamp } from "../../dom.js";
import { auditCsvUrl, getAuditEvents, postAuditExport } from "./api.js";
import { reasonFor } from "./session.js";
import { adminHead, tabsHtml } from "./users.js";

export const PAGE_SIZE = 50;

const OUTCOMES = ["success", "denied", "failed"];
const OUTCOME_CLASS = { success: "ok", denied: "warn", failed: "bad" };

/** The form's fields; `from`/`to` are `YYYY-MM-DD` as a date input gives them. */
const emptyForm = () => ({ actor_id: "", action: "", object_type: "", object_id: "", outcome: "", from: "", to: "" });

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

/** The screen's form as `GET /audit/events` filters: trimmed, empty ones dropped, days made instants. */
export function filtersOf(form) {
  const filters = {};
  for (const key of ["actor_id", "action", "object_type", "object_id", "outcome"]) {
    const value = (form[key] || "").trim();
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

function eventRow(event) {
  const object = [event.object_type, event.object_id].filter(Boolean).join(" · ");
  return `<tr>
    <td>${esc(fmtStamp(event.occurred_at))}</td>
    <td><span class="pb-mono">${esc(event.actor_id)}</span><div class="pb-small">${esc(event.actor_kind)}</div></td>
    <td><span class="pb-mono">${esc(event.action)}</span></td>
    <td>${object ? `<span class="pb-mono">${esc(object)}</span>` : EM_DASH}</td>
    <td><span class="pill ${OUTCOME_CLASS[event.outcome] || "warn"}">${esc(event.outcome)}</span></td>
    <td>${event.request_id ? `<span class="pb-mono">${esc(event.request_id)}</span>` : EM_DASH}</td>
    <td>${Object.keys(event.details || {}).length ? `<span class="pb-mono">${esc(detailsText(event.details))}</span>` : EM_DASH}</td>
  </tr>`;
}

const input = (key, label, placeholder = "") =>
  `<label class="pb-field"><span class="sub">${esc(label)}</span><input class="pb-input" name="${key}" value="${esc(
    state.form[key],
  )}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false"></label>`;

function filterCard() {
  const f = state.form;
  return `<section class="card"><h3>Filters</h3><form id="pb-audit-filters" novalidate>
    <div class="pb-filters">
      ${input("actor_id", "Actor (user id)")}
      ${input("action", "Action", "e.g. models.approve, or models. for all")}
      ${input("object_type", "Object type", "e.g. model, run, user")}
      ${input("object_id", "Object id")}
      <label class="pb-field"><span class="sub">Outcome</span><select class="pb-input" name="outcome"><option value="">Any</option>${OUTCOMES.map(
        (o) => `<option value="${o}"${f.outcome === o ? " selected" : ""}>${o}</option>`,
      ).join("")}</select></label>
      <label class="pb-field"><span class="sub">From (UTC day)</span><input class="pb-input" type="date" name="from" value="${esc(f.from)}"></label>
      <label class="pb-field"><span class="sub">To (UTC day, included)</span><input class="pb-input" type="date" name="to" value="${esc(f.to)}"></label>
    </div>
    <div class="actions" style="margin:0 20px 18px"><button type="submit" class="run" id="pb-audit-apply">Apply filters</button><button type="button" class="linkbtn" id="pb-audit-clear">Clear</button></div>
  </form></section>`;
}

function exportLine(filters) {
  const csvRefused = reasonFor("GET", "/audit/events.csv");
  const exportRefused = reasonFor("POST", "/audit/exports");
  const csv = csvRefused
    ? `<span class="pb-small">${esc(csvRefused)}</span>`
    : `<a class="linkbtn" id="pb-audit-csv" href="${esc(auditCsvUrl(filters))}" download="audit_events.csv">Download these events (CSV)</a>`;
  const retained = exportRefused
    ? `<span class="pb-small">${esc(exportRefused)}</span>`
    : `<button type="button" class="linkbtn" id="pb-audit-export" title="Covers the date range and the action filter: what POST /audit/exports accepts."${state.exporting ? " disabled" : ""}>${
        state.exporting ? "Writing export…" : "Write a retained export"
      }</button>`;
  return `<div class="pb-row-actions">${csv}${retained}</div>`;
}

function exportOutcome() {
  if (state.exportError) return errorBox(state.exportError);
  const r = state.exportResult;
  if (!r) return "";
  return `<div class="pb-ok" role="status">Wrote ${esc(fmtInt(r.event_count))} event${r.event_count === 1 ? "" : "s"} to <span class="pb-mono">${esc(
    r.location,
  )}</span>${r.retain_until ? `, retained until ${esc(fmtStamp(r.retain_until))}` : ""}. SHA-256 <span class="pb-mono">${esc(r.sha256)}</span></div>`;
}

function resultsCard(filters) {
  if (state.loadError) return `<section class="card"><h3>Events</h3>${errorBox(state.loadError)}</section>`;
  if (!state.page) return `<section class="card"><h3>Events</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  const { events, total, offset } = state.page;
  const first = total ? offset + 1 : 0;
  const last = offset + events.length;
  const table = events.length
    ? `<div class="tbl-wrap"><table>
        <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Object</th><th>Outcome</th><th>Request</th><th>Details</th></tr></thead>
        <tbody>${events.map(eventRow).join("")}</tbody></table></div>`
    : `<p class="empty">No event matches these filters.</p>`;
  return `<section class="card"><h3>Events · ${esc(fmtInt(total))}</h3>
    <div class="kv" style="border-top:0;flex-wrap:wrap">${exportLine(filters)}</div>
    ${exportOutcome() ? `<div style="padding:0 20px 12px">${exportOutcome()}</div>` : ""}
    ${table}
    <div class="pb-pager"><span>${total ? `${esc(fmtInt(first))}–${esc(fmtInt(last))} of ${esc(fmtInt(total))}, newest first` : EM_DASH}</span>
      <span class="pb-row-actions"><button type="button" id="pb-audit-prev"${offset <= 0 || state.loading ? " disabled" : ""}>‹ Newer</button><button type="button" id="pb-audit-next"${
        last >= total || state.loading ? " disabled" : ""
      }>Older ›</button></span></div>
  </section>`;
}

export function auditHtml() {
  const head = adminHead(
    "Audit log",
    "Every change, every sign-in and every download of customer rows, append-only. Filter dates are UTC days; times show in your own zone.",
  );
  const refused = reasonFor("GET", "/audit/events");
  if (refused) {
    return `<main class="screen">${head}${tabsHtml("audit")}<div class="apierr" role="alert"><b>ROLE_REQUIRED</b>${esc(refused)}</div></main>`;
  }
  const filters = filtersOf(state.form);
  return `<main class="screen">${head}${tabsHtml("audit")}<div class="stack">${filterCard()}${resultsCard(filters)}</div></main>`;
}

export async function loadEvents() {
  state.loading = true;
  try {
    state.page = await getAuditEvents(filtersOf(state.form), PAGE_SIZE, state.offset);
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
    if (field) next[key] = field.value;
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

