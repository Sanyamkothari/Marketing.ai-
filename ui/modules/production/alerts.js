// Alerts and missed runs (Phase 4b M49): what went wrong on its own, and who is dealing with it.
//
// Alerts are `GET /monitoring/alerts` - drift above its threshold, a performance drop beyond
// `monitoring.performance_alert_drop_pct`, a failed scheduled job, missed slots - newest first, with
// the server's own message (it holds ids, codes and counts only, DEC-770). "Acknowledge" is Analyst
// work (DEC-784): it says someone is dealing with it, the first acknowledgement stands, and a Viewer
// sees the button refused in place. The list opens on the open alerts, because an acknowledged alert
// is history, not work; one click shows everything.
//
// Missed runs are `GET /monitoring/missed-firings`: every due slot nobody fired (the scheduler was
// down, the machine asleep), across all schedules. A schedule's own history (`schedules.js`) shows
// its missed slots among its firings; this is the one place to see all of them at once.

import { EM_DASH, errorBox, esc, fmtStamp } from "../../dom.js";
import { getAlerts, getMissedFirings, postAlertAcknowledge } from "./api.js";
import { actionButton, fieldValue, mono, statusPill } from "./controls.js";
import { KIND_LABEL, monitoringScreen, runHref } from "./schedules.js";

export const ALERT_KIND_LABEL = {
  drift_above_threshold: "Drift above threshold",
  performance_drop: "Performance drop",
  scheduled_job_failed: "Scheduled job failed",
  schedule_missed: "Schedule missed",
};

export const ALERTS_SHOWN = 200;

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

const scheduleLink = (id) =>
  id ? `<a class="pb-mono" href="#/monitoring/schedules/${encodeURIComponent(id)}">${esc(id)}</a>` : EM_DASH;

const runLink = (id) => (id ? `<a class="pb-mono" href="${runHref(id)}">${esc(id)}</a>` : EM_DASH);

export async function loadAlerts() {
  try {
    const body = await getAlerts({ ...state.filters, limit: ALERTS_SHOWN });
    state.alerts = body.alerts || [];
    state.alertsError = null;
  } catch (error) {
    state.alertsError = error;
  }
}

export async function loadMissed() {
  try {
    state.missed = (await getMissedFirings({ limit: 500 })).firings || [];
    state.missedError = null;
  } catch (error) {
    state.missedError = error;
  }
}

function acknowledgement(alert) {
  if (alert.acknowledged_at) {
    return `<span class="pb-small">Acknowledged by ${esc(alert.acknowledged_by || EM_DASH)}<br>${esc(
      fmtStamp(alert.acknowledged_at),
    )}</span>`;
  }
  const error =
    state.ackError && state.ackError.alertId === alert.alert_id ? errorBox(state.ackError.error) : "";
  return `${actionButton("POST", "/monitoring/alerts/{alert_id}/acknowledge", {
    cls: "linkbtn",
    attrs: `data-ack="${esc(alert.alert_id)}"`,
    label: state.acking === alert.alert_id ? "Acknowledging…" : "Acknowledge",
    busy: state.acking === alert.alert_id,
  })}${error}`;
}

function alertRow(alert) {
  return `<tr data-alert="${esc(alert.alert_id)}"><td>${esc(fmtStamp(alert.created_at))}</td><td>${statusPill(alert.severity)}</td>
    <td>${esc(ALERT_KIND_LABEL[alert.kind] || alert.kind)}</td><td>${esc(alert.use_case_id)}${
      alert.client_id ? `<div class="pb-small">${esc(alert.client_id)}</div>` : ""
    }</td>
    <td>${esc(alert.message)}</td><td>${scheduleLink(alert.schedule_id)}</td><td>${runLink(alert.run_id)}</td>
    <td>${acknowledgement(alert)}</td></tr>`;
}

function filterCard() {
  const f = state.filters;
  return `<form id="pb-alert-filters" class="pb-filters" novalidate>
    <label class="pb-field"><span class="sub">Kind</span><select class="pb-input" name="kind"><option value="">Any</option>${Object.entries(
      ALERT_KIND_LABEL,
    )
      .map(([kind, label]) => `<option value="${kind}"${f.kind === kind ? " selected" : ""}>${esc(label)}</option>`)
      .join("")}</select></label>
    <label class="check" style="align-self:end"><input type="checkbox" name="unacknowledged_only"${
      f.unacknowledged_only ? " checked" : ""
    }> Open alerts only</label>
  </form>`;
}

function alertsCard() {
  if (state.alertsError) return `<section class="card"><h3>Alerts</h3>${filterCard()}${errorBox(state.alertsError)}</section>`;
  if (!state.alerts) return `<section class="card"><h3>Alerts</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  const table = state.alerts.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Raised</th><th>Severity</th><th>What</th><th>Use case</th><th>Message</th><th>Schedule</th><th>Run</th><th></th></tr></thead>
        <tbody>${state.alerts.map(alertRow).join("")}</tbody></table></div>`
    : `<p class="empty">${state.filters.unacknowledged_only ? "No open alert." : "No alert has been raised."}</p>`;
  return `<section class="card"><h3>Alerts · ${esc(state.alerts.length)}${
    state.alerts.length >= ALERTS_SHOWN ? ` (newest ${ALERTS_SHOWN})` : ""
  }</h3>${filterCard()}${
    state.acked
      ? `<div style="padding:0 20px 12px"><div class="pb-ok" role="status">Acknowledged by ${esc(
          state.acked.acknowledged_by || EM_DASH,
        )}: ${esc(state.acked.message)}</div></div>`
      : ""
  }${table}</section>`;
}

export const alertsHtml = () =>
  monitoringScreen(
    "alerts",
    "Alerts",
    "Drift, performance drops, failed and missed scheduled work. Acknowledge one to say it is being dealt with.",
    ["GET", "/monitoring/alerts"],
    alertsCard,
  );

function missedRow(f) {
  return `<tr><td>${esc(fmtStamp(f.scheduled_for))}</td><td>${scheduleLink(f.schedule_id)}</td><td>${esc(
    KIND_LABEL[f.kind] || f.kind,
  )}</td><td>${esc(f.use_case_id)}</td><td>${f.client_id ? mono(f.client_id) : EM_DASH}</td><td>${statusPill(
    f.status,
  )} ${mono(f.error_code)}</td><td>${esc(fmtStamp(f.fired_at))}</td></tr>`;
}

function missedCard() {
  if (state.missedError) return `<section class="card"><h3>Missed runs</h3>${errorBox(state.missedError)}</section>`;
  if (!state.missed) return `<section class="card"><h3>Missed runs</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  const table = state.missed.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Due</th><th>Schedule</th><th>Work</th><th>Use case</th><th>Client</th><th>Status</th><th>Recorded</th></tr></thead>
        <tbody>${state.missed.map(missedRow).join("")}</tbody></table></div>`
    : `<p class="empty">No scheduled slot has been missed.</p>`;
  return `<section class="card"><h3>Missed runs · ${esc(state.missed.length)}, newest first</h3>${table}</section>`;
}

export const missedHtml = () =>
  monitoringScreen(
    "missed",
    "Missed runs",
    "Due slots nobody fired - the scheduler was stopped or the machine asleep. One catch-up run follows, never one per slot.",
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
