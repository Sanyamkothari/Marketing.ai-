// Alerts and missed runs (Phase 4b M49): what went wrong on its own, and who is dealing with it.
//
// Alerts are `GET /monitoring/alerts` - drift above its threshold, a performance drop beyond
// `monitoring.performance_alert_drop_pct`, a failed scheduled job, missed slots - with the server's own
// message (it holds ids, codes and counts only, DEC-770). "I'm on it" acknowledges one: Analyst work
// (DEC-784); it says someone is dealing with it, the first acknowledgement stands, and a Viewer sees
// the button refused in place. The list opens on the open alerts, because an acknowledged alert is
// history, not work; one click shows everything.
//
// v1: five columns - severity, what happened in words, use case, when, and the action - with the
// server's message, the schedule and the run behind each row's own disclosure; open critical alerts
// sort first. A code inside a message (`DATASET_COMPOSITE_KEY_NOT_WIRED`) reads as plain words; the
// message as the server wrote it stays under Details.
//
// Missed runs are `GET /monitoring/missed-firings`: every due slot nobody fired (the scheduler was
// down, the machine asleep), across all schedules. The Schedules screen offers this view only when
// there are some; the route itself always answers.

import { EM_DASH, errorBox, esc, fmtStamp, glossaryCode, techDetails } from "../../dom.js";
import { getAlerts, getMissedFirings, postAlertAcknowledge } from "./api.js";
import {
  actionButton,
  codeWords,
  fieldValue,
  loadPeople,
  loadUseCaseNames,
  mono,
  personName,
  plainCode,
  rowsTable,
  selectField,
  statusPill,
  useCaseName,
} from "./controls.js";
import { KIND_LABEL, monitoringScreen, runHref, setMissedCount } from "./schedules.js";

/** What happened, in words; drift uses the catalogue's own title when it has one. */
export const ALERT_KIND_LABEL = {
  drift_above_threshold: "New customers look very different",
  performance_drop: "The model did worse on real outcomes",
  scheduled_job_failed: "A scheduled job failed",
  schedule_missed: "Scheduled runs were missed",
};

const kindWords = (kind) => {
  if (kind === "drift_above_threshold") {
    const entry = glossaryCode("DRIFT_DRIFTED");
    if (entry && entry.title) return entry.title;
  }
  return ALERT_KIND_LABEL[kind] || String(kind || EM_DASH).replace(/_/g, " ");
};

export const ALERTS_SHOWN = 200;

const SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };

const state = {
  alerts: null,
  alertsError: null,
  filters: { kind: "", unacknowledged_only: true },
  ackError: null, // { alertId, error }
  acking: null,
  acked: null, // the alert as the server answered it, for the confirmation line
  missed: null,
  missedError: null,
};

export async function loadAlerts() {
  try {
    const [body] = await Promise.all([
      getAlerts({ ...state.filters, limit: ALERTS_SHOWN }),
      loadUseCaseNames(),
      loadPeople(),
    ]);
    state.alerts = body.alerts || [];
    state.alertsError = null;
  } catch (error) {
    state.alertsError = error;
  }
}

export async function loadMissed() {
  try {
    const [body] = await Promise.all([getMissedFirings({ limit: 500 }), loadUseCaseNames()]);
    state.missed = body.firings || [];
    setMissedCount(state.missed.length);
    state.missedError = null;
  } catch (error) {
    state.missedError = error;
  }
}

const CODE_IN_TEXT = /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g;

/** A server message with any known code replaced by its plain words (the original goes under Details). */
export function plainMessage(message) {
  return String(message || "").replace(CODE_IN_TEXT, (code) => {
    const words = plainCode(code);
    return words ? (words.charAt(0).toLowerCase() + words.slice(1)).replace(/\.$/, "") : code;
  });
}

/** Open first, then critical before warning before info, then newest. */
const byUrgency = (a, b) =>
  Number(Boolean(a.acknowledged_at)) - Number(Boolean(b.acknowledged_at)) ||
  (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3) ||
  String(b.created_at).localeCompare(String(a.created_at));

function acknowledgement(alert) {
  if (alert.acknowledged_at) {
    return `<span class="pb-small">Taken on by ${esc(personName(alert.acknowledged_by))}<br>${esc(fmtStamp(alert.acknowledged_at))}</span>`;
  }
  const error = state.ackError && state.ackError.alertId === alert.alert_id ? errorBox(state.ackError.error) : "";
  return `${actionButton("POST", "/monitoring/alerts/{alert_id}/acknowledge", {
    cls: "btn secondary sm",
    attrs: `data-ack="${esc(alert.alert_id)}"`,
    label: state.acking === alert.alert_id ? "Saving…" : "I'm on it",
    busy: state.acking === alert.alert_id,
  })}${error}`;
}

function whatHappened(alert) {
  const links = [
    alert.run_id ? `<a href="${runHref(alert.run_id)}">See the run</a>` : "",
    alert.schedule_id ? `<a href="#/monitoring/schedules/${encodeURIComponent(alert.schedule_id)}">Open the schedule</a>` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const plain = plainMessage(alert.message);
  return `<details class="pb-rowdetails"><summary>${esc(kindWords(alert.kind))}</summary><p>${esc(plain)}</p>${
    links ? `<p>${links}</p>` : ""
  }${techDetails(
    [
      ["Message", plain !== alert.message ? alert.message : null],
      ["Alert", alert.alert_id],
      ["Kind", alert.kind],
      ["Schedule", alert.schedule_id],
      ["Run", alert.run_id],
      ["Model", alert.model_id],
      ["Client", alert.client_id],
    ],
    "Details",
  )}</details>`;
}

function alertRow(alert) {
  return {
    attrs: `data-alert="${esc(alert.alert_id)}"`,
    cells: [
      statusPill(alert.severity),
      whatHappened(alert),
      esc(useCaseName(alert.use_case_id)),
      esc(fmtStamp(alert.created_at)),
      acknowledgement(alert),
    ],
  };
}

function filterForm() {
  const f = state.filters;
  return `<form id="pb-alert-filters" class="pb-filters" novalidate>
    ${selectField("kind", "What happened", [["", "Anything"], ...Object.keys(ALERT_KIND_LABEL).map((kind) => [kind, kindWords(kind)])], {
      value: f.kind,
    })}
    <label class="check"><input type="checkbox" name="unacknowledged_only"${f.unacknowledged_only ? " checked" : ""}> Open alerts only</label>
  </form>`;
}

function alertsCard() {
  const title = (n) =>
    `<h3>${n === null ? "Alerts" : `${esc(n)} alert${n === 1 ? "" : "s"}${n >= ALERTS_SHOWN ? ` (newest ${ALERTS_SHOWN})` : ""}`} <span class="sort-note">(open and most serious first)</span></h3>`;
  if (state.alertsError) {
    return `<section class="card">${title(null)}<div class="card-body">${filterForm()}${errorBox(state.alertsError, { retry: true })}</div></section>`;
  }
  if (!state.alerts) return `<section class="card">${title(null)}<p class="loading pb-pad">Loading…</p></section>`;
  const table = state.alerts.length
    ? rowsTable(
        [{ label: "Severity" }, { label: "What happened" }, { label: "Use case" }, { label: "Raised" }, { label: "", sr: "Action" }],
        [...state.alerts].sort(byUrgency).map(alertRow),
        { cls: "pb-alerts" },
      )
    : state.filters.unacknowledged_only
      ? `<div class="empty-state"><p class="es-t">No open alerts.</p><p>You'll see one here if a scheduled job fails, results drop, or new customers look very different.</p></div>`
      : `<div class="empty-state"><p class="es-t">No alert has been raised yet.</p><p>You'll see one here if a scheduled job fails, results drop, or new customers look very different.</p></div>`;
  const acked = state.acked
    ? `<div class="pb-ok" role="status">Marked as being dealt with by ${esc(personName(state.acked.acknowledged_by))}: ${esc(
        kindWords(state.acked.kind),
      )}.${techDetails([["Acknowledged by", state.acked.acknowledged_by]])}</div>`
    : "";
  return `<section class="card">${title(state.alerts.length)}<div class="card-body">${filterForm()}${acked}</div>${table}</section>`;
}

export const alertsHtml = () =>
  monitoringScreen(
    "alerts",
    "Alerts",
    "Problems Marketing AI found on its own: results that dropped, new customers that look different, and scheduled work that failed or was missed.",
    ["GET", "/monitoring/alerts"],
    alertsCard,
  );

function missedRow(f) {
  return {
    cells: [
      esc(fmtStamp(f.scheduled_for)),
      esc(KIND_LABEL[f.kind] || f.kind),
      esc(useCaseName(f.use_case_id)),
      `<a href="#/monitoring/schedules/${encodeURIComponent(f.schedule_id)}">Open the schedule</a>`,
      `${statusPill(f.status)}${f.error_code ? `<div class="pb-small">${codeWords(f.error_code)}</div>` : ""}`,
      mono(f.error_code),
      f.client_id ? mono(f.client_id) : EM_DASH,
      esc(fmtStamp(f.fired_at)),
    ],
  };
}

function missedCard() {
  const title = (n) => `<h3>Missed runs${n === null ? "" : ` · ${esc(n)}`} <span class="sort-note">(newest first)</span></h3>`;
  if (state.missedError) return `<section class="card">${title(null)}<div class="card-body">${errorBox(state.missedError, { retry: true })}</div></section>`;
  if (!state.missed) return `<section class="card">${title(null)}<p class="loading pb-pad">Loading…</p></section>`;
  const table = state.missed.length
    ? rowsTable(
        [
          { label: "Due" },
          { label: "What" },
          { label: "Use case" },
          { label: "Schedule" },
          { label: "Result" },
          { label: "Code", more: true },
          { label: "Client", more: true },
          { label: "Recorded", more: true },
        ],
        state.missed.map(missedRow),
      )
    : `<div class="empty-state"><p class="es-t">No scheduled run has been missed.</p></div>`;
  return `<section class="card">${title(state.missed.length)}${table}</section>`;
}

export const missedHtml = () =>
  monitoringScreen(
    "missed",
    "Missed runs",
    "Runs that should have happened while Marketing AI was switched off. It runs each schedule once to catch up, never once per missed run.",
    ["GET", "/monitoring/missed-firings"],
    missedCard,
  );

export function bindAlerts(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  const filters = main.querySelector("#pb-alert-filters");
  if (filters) {
    filters.addEventListener("change", async () => {
      state.filters = {
        kind: fieldValue(filters, "kind"),
        unacknowledged_only: filters.elements.namedItem("unacknowledged_only").checked,
      };
      await loadAlerts();
      repaint();
    });
    filters.addEventListener("submit", (event) => event.preventDefault());
  }
  main.addEventListener("click", async (event) => {
    const button = event.target && event.target.closest ? event.target.closest("[data-ack]") : null;
    if (!button || state.acking) return;
    const alertId = button.getAttribute("data-ack");
    state.acking = alertId;
    state.ackError = null;
    state.acked = null;
    repaint();
    try {
      state.acked = await postAlertAcknowledge(alertId);
      await loadAlerts();
    } catch (error) {
      state.ackError = { alertId, error };
    }
    state.acking = null;
    repaint();
  });
}

/** Test seam: forget screen state between cases. */
export function _resetAlertsForTests() {
  Object.assign(state, {
    alerts: null,
    alertsError: null,
    filters: { kind: "", unacknowledged_only: true },
    ackError: null,
    acking: null,
    acked: null,
    missed: null,
    missedError: null,
  });
}
