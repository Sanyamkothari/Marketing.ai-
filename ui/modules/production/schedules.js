// Schedules (Phase 4b M49): the list, a new schedule, one schedule with its firing history, and the
// actions on it - edit, pause and resume, run now, delete. Alerts and missed runs are `alerts.js`;
// outcomes of a scoring run are `outcomes.js`; all three share this file's head and tabs.
//
// Every call is `api/routes/schedules.py`, and the rules shown are the server's (DEC-780…786):
// * Reading is Viewer; every change is Analyst, because a schedule only starts work an Analyst could
//   start by hand (`POST /runs`) and fires as `system:scheduler`, which can never approve. A Viewer
//   sees every control, refused in place with the server's sentence (`controls.actionButton`).
// * A cadence is `daily`, `weekly` or `monthly` (02:00 in the schedule's zone) or a five-field cron
//   line. The form offers the three words and "custom"; the cron line is only read for "custom", so a
//   line typed and then abandoned is never sent by accident.
// * A schedule's client defaults to its recipe's (DEC-786); the form says so rather than offering the
//   deployment's `client_id`, which is a different namespace.
// * "Run now" fires inside the request (DEC-781) and answers the firing as recorded, which may itself
//   be `failed` with an `error_code` - shown as the firing's result, not as a failed request.
// * A schedule `managed_by` the retraining setting cannot be edited here (`409 SCHEDULE_MANAGED`): the
//   edit form is replaced by that explanation. "Sync retraining schedules" asks the server to bring the
//   managed ones in line with the recipes now (DEC-783), instead of at the next restart.
// * The firing history is the server's, newest first, missed slots included (`status=missed`), and
//   each firing's run links to that run's outcomes screen.
//
// Delete asks twice - a second "Yes, delete" button in the row - because `window.confirm` is a
// browser dialog no test (and no screen reader flow) handles well, and a schedule's firing history
// goes with it.

import { getIndustries } from "../../api.js";
import { EM_DASH, errorBox, esc, fmtStamp, pageHead } from "../../dom.js";
import {
  deleteSchedule,
  getFirings,
  getSchedule,
  getSchedules,
  patchSchedule,
  postSchedule,
  postScheduleDisable,
  postScheduleEnable,
  postScheduleFire,
  postRetrainingSync,
} from "./api.js";
import { actionButton, fieldValue, mono, refusal, statusPill, tabStrip, textField } from "./controls.js";
import { reasonFor } from "./session.js";

export const MONITORING_TABS = [
  ["schedules", "Schedules", "#/monitoring/schedules"],
  ["alerts", "Alerts", "#/monitoring/alerts"],
  ["missed", "Missed runs", "#/monitoring/missed"],
  ["runs", "Outcomes", "#/monitoring/runs"],
];

export const KIND_LABEL = { score: "Score new data", drift_check: "Check drift", retrain: "Retrain (a challenger)" };
export const PRESETS = ["daily", "weekly", "monthly"];
const PRESET_LABEL = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
export const DEFAULT_TIMEZONE = "Asia/Kolkata";
export const FIRING_STATUSES = ["queued", "running", "succeeded", "failed", "missed"];

export const monitoringHead = (title, desc) =>
  pageHead(
    `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">${esc(title)}</h1><p class="desc">${esc(desc)}</p>`,
  );

/** A monitoring screen, or the server's reason it may not be read. */
export function monitoringScreen(tab, title, desc, readRoute, body) {
  const head = monitoringHead(title, desc);
  const tabs = tabStrip(tab, MONITORING_TABS, "Monitoring");
  const refused = reasonFor(readRoute[0], readRoute[1]);
  if (refused) return `<main class="screen">${head}${tabs}${refusal(refused)}</main>`;
  return `<main class="screen">${head}${tabs}<div class="stack">${body()}</div></main>`;
}

/** "Monthly · 02:00 Asia/Kolkata", or the cron line itself for a custom cadence. */
export function cadenceText(schedule) {
  const zone = schedule.timezone || DEFAULT_TIMEZONE;
  if (schedule.preset) return `${PRESET_LABEL[schedule.preset] || schedule.preset} · 02:00 ${zone}`;
  return `${schedule.cron} · ${zone}`;
}

const scheduleHref = (id) => `#/monitoring/schedules/${encodeURIComponent(id)}`;
export const runHref = (id) => `#/monitoring/runs/${encodeURIComponent(id)}`;

const state = {
  schedules: null,
  loadError: null,
  useCases: null, // [{id, name}] from GET /industries, or null when unavailable
  creating: false,
  createError: null,
  created: null,
  syncing: false,
  synced: null,
  syncError: null,
  confirmDelete: null, // schedule id awaiting "Yes, delete"
  rowError: null, // { scheduleId, error }
  rowNotice: null, // { scheduleId, html }
  busy: false,
  // one schedule
  schedule: null,
  scheduleError: null,
  firings: null,
  firingsError: null,
  firingStatus: "",
  saving: false,
  saveError: null,
  saved: false,
};

// --- loading ------------------------------------------------------------------------------------

/** Every use case of the configured industry, for the form's picker; null when it cannot be read. */
async function loadUseCases() {
  if (state.useCases) return;
  try {
    const payload = await getIndustries();
    const seen = new Map();
    for (const industry of payload.industries || []) {
      for (const stage of industry.stages || []) {
        for (const uc of stage.use_cases || []) if (!seen.has(uc.id)) seen.set(uc.id, uc.name);
      }
    }
    state.useCases = [...seen].map(([id, name]) => ({ id, name }));
  } catch {
    state.useCases = null; // the form falls back to a text field
  }
}

export async function loadSchedules() {
  try {
    const [body] = await Promise.all([getSchedules(), loadUseCases()]);
    state.schedules = body.schedules || [];
    state.loadError = null;
  } catch (error) {
    state.loadError = error;
  }
}

export async function loadSchedule(scheduleId) {
  if (!state.schedule || state.schedule.schedule_id !== scheduleId) {
    state.schedule = null;
    state.firings = null;
    state.firingStatus = "";
    state.saveError = null;
    state.saved = false;
    state.rowError = null;
    state.rowNotice = null;
    state.confirmDelete = null;
  }
  try {
    const [schedule, firings] = await Promise.all([
      getSchedule(scheduleId),
      getFirings(scheduleId, { status: state.firingStatus, limit: 200 }),
    ]);
    state.schedule = schedule;
    state.firings = firings.firings || [];
    state.scheduleError = null;
    state.firingsError = null;
  } catch (error) {
    state.scheduleError = error;
  }
}

async function loadFirings() {
  const id = state.schedule && state.schedule.schedule_id;
  if (!id) return;
  try {
    state.firings = (await getFirings(id, { status: state.firingStatus, limit: 200 })).firings || [];
    state.firingsError = null;
  } catch (error) {
    state.firingsError = error;
  }
}

// --- the list -----------------------------------------------------------------------------------

function useCaseField() {
  if (!state.useCases || !state.useCases.length) return textField("use_case_id", "Use case id", { attrs: "required" });
  return `<label class="pb-field field"><span class="sub">Use case</span><select class="pb-input" name="use_case_id">${state.useCases
    .map((uc) => `<option value="${esc(uc.id)}">${esc(uc.name)}</option>`)
    .join("")}</select></label>`;
}

function cadenceFields(current = null) {
  const preset = current ? current.preset || "custom" : "monthly";
  const cron = current && !current.preset ? current.cron : "";
  return `<label class="pb-field field"><span class="sub">Cadence</span><select class="pb-input" name="preset">${[...PRESETS, "custom"]
    .map(
      (p) =>
        `<option value="${p}"${p === preset ? " selected" : ""}>${p === "custom" ? "Custom (cron line)" : `${PRESET_LABEL[p]} at 02:00`}</option>`,
    )
    .join("")}</select></label>
    ${textField("cron", "Cron line, for Custom", { value: cron, placeholder: "minute hour day month weekday, e.g. 0 6 1 * *" })}
    ${textField("timezone", "Time zone", { value: current ? current.timezone : DEFAULT_TIMEZONE })}`;
}

function parameterFields(current = {}) {
  return `${textField("onboarding_spec_id", "Onboarding recipe id", {
    value: current.onboarding_spec_id || "",
    placeholder: "rebuilds the dataset from the client's tables",
  })}${textField("dataset_id", "Or a fixed dataset id", { value: current.dataset_id || "" })}${textField(
    "model_version_id",
    "Model version id (optional)",
    { value: current.model_version_id || "", placeholder: "the champion when empty" },
  )}`;
}

function createCard() {
  return `<section class="card"><h3>New schedule</h3><div class="form-body">
    <form id="pb-schedule-create" class="pb-form wide" novalidate autocomplete="off">
      <div class="frow">${useCaseField()}
        <label class="pb-field field"><span class="sub">What it does</span><select class="pb-input" name="kind">${Object.entries(KIND_LABEL)
          .map(([kind, label]) => `<option value="${kind}">${esc(label)}</option>`)
          .join("")}</select></label>
        ${textField("client_id", "Client id (optional)", { placeholder: "the recipe's client when empty" })}
      </div>
      <div class="frow">${cadenceFields()}</div>
      <div class="frow">${parameterFields()}</div>
      <label class="check"><input type="checkbox" name="enabled" checked> Start it now (untick to create it paused)</label>
      <div class="actions">${actionButton("POST", "/schedules", {
        type: "submit",
        attrs: 'id="pb-schedule-create-submit"',
        label: state.creating ? "Saving…" : "Create schedule",
        busy: state.creating,
      })}<span class="reason">A retrain produces a challenger; an Approver still decides the champion.</span></div>
    </form>
    ${state.createError ? errorBox(state.createError) : ""}
    ${
      state.created
        ? `<div class="pb-ok" role="status">Created <a href="${scheduleHref(state.created.schedule_id)}">${esc(
            KIND_LABEL[state.created.kind] || state.created.kind,
          )} · ${esc(state.created.use_case_id)}</a>; next due ${esc(fmtStamp(state.created.next_due_at))}.</div>`
        : ""
    }
  </div></section>`;
}

function rowActions(s, withOpen = true) {
  const id = esc(s.schedule_id);
  if (state.confirmDelete === s.schedule_id) {
    return `<div class="pb-row-actions"><span class="pb-small">Delete it and its history?</span>${actionButton(
      "DELETE",
      "/schedules/{schedule_id}",
      { cls: "linkbtn", attrs: `data-delete-yes="${id}"`, label: "Yes, delete" },
    )}<button type="button" class="linkbtn" data-delete-no>Keep</button></div>`;
  }
  const toggle = s.enabled
    ? actionButton("POST", "/schedules/{schedule_id}/disable", { cls: "linkbtn", attrs: `data-disable="${id}"`, label: "Pause" })
    : actionButton("POST", "/schedules/{schedule_id}/enable", { cls: "linkbtn", attrs: `data-enable="${id}"`, label: "Resume" });
  const open = withOpen ? `<a class="linkbtn" href="${scheduleHref(s.schedule_id)}">Open</a>` : "";
  return `<div class="pb-row-actions">${open}${actionButton(
    "POST",
    "/schedules/{schedule_id}/fire",
    { cls: "linkbtn", attrs: `data-fire="${id}"`, label: "Run now" },
  )}${toggle}${actionButton("DELETE", "/schedules/{schedule_id}", {
    cls: "linkbtn",
    attrs: `data-delete="${id}"`,
    label: "Delete",
  })}</div>`;
}

/** The outcome of the last action on this schedule (an error box or a confirmation), or "". */
function messageFor(s) {
  const error = state.rowError && state.rowError.scheduleId === s.schedule_id ? state.rowError.error : null;
  const notice = state.rowNotice && state.rowNotice.scheduleId === s.schedule_id ? state.rowNotice.html : null;
  if (error) return errorBox(error);
  return notice ? `<div class="pb-ok" role="status">${notice}</div>` : "";
}

function rowMessage(s, span) {
  const message = messageFor(s);
  return message ? `<tr><td colspan="${span}">${message}</td></tr>` : "";
}

function scheduleRow(s) {
  return `<tr data-schedule="${esc(s.schedule_id)}">
    <td><a href="${scheduleHref(s.schedule_id)}">${esc(KIND_LABEL[s.kind] || s.kind)}</a>${
      s.managed_by ? `<div class="pb-small">managed by ${esc(s.managed_by)}</div>` : ""
    }</td>
    <td>${esc(s.use_case_id)}</td><td>${s.client_id ? mono(s.client_id) : EM_DASH}</td>
    <td>${esc(cadenceText(s))}</td>
    <td>${s.enabled ? `<span class="pill ok">Active</span>` : `<span class="pill warn">Paused</span>`}</td>
    <td>${esc(fmtStamp(s.next_due_at))}</td><td>${esc(fmtStamp(s.last_fired_at))}</td>
    <td>${rowActions(s)}</td></tr>${rowMessage(s, 8)}`;
}

function syncLine() {
  const button = actionButton("POST", "/schedules/retraining/sync", {
    cls: "linkbtn",
    attrs: 'id="pb-retraining-sync"',
    label: state.syncing ? "Syncing…" : "Sync retraining schedules",
    busy: state.syncing,
  });
  const r = state.synced;
  const result = state.syncError
    ? errorBox(state.syncError)
    : r
      ? `<span class="pb-small" role="status">Checked ${esc(r.targets)} recipe(s): ${esc(r.created.length)} created, ${esc(
          r.updated.length,
        )} updated, ${esc(r.removed.length)} removed.</span>`
      : "";
  return `<div class="pb-row-actions" style="padding:12px 20px">${button}${result}</div>`;
}

function listCard() {
  if (state.loadError) return `<section class="card"><h3>Schedules</h3>${errorBox(state.loadError)}</section>`;
  if (!state.schedules) return `<section class="card"><h3>Schedules</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  const table = state.schedules.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Work</th><th>Use case</th><th>Client</th><th>Cadence</th><th>State</th><th>Next due</th><th>Last fired</th><th></th></tr></thead>
        <tbody>${state.schedules.map(scheduleRow).join("")}</tbody></table></div>`
    : `<p class="empty">No schedule yet. Create one below, or sync the retraining schedules your recipes ask for.</p>`;
  return `<section class="card"><h3>Schedules · ${esc(state.schedules.length)}</h3>${syncLine()}${table}</section>`;
}

export const schedulesHtml = () =>
  monitoringScreen(
    "schedules",
    "Schedules",
    "Scoring, drift checks and retraining that run on their own. Every change and every firing is in the audit log.",
    ["GET", "/schedules"],
    () => `${listCard()}${createCard()}`,
  );

// --- one schedule -------------------------------------------------------------------------------

function firingRow(f) {
  return `<tr><td>${esc(fmtStamp(f.fired_at))}</td><td>${esc(f.trigger.replace(/_/g, " "))}</td><td>${statusPill(f.status)}</td>
    <td>${esc(fmtStamp(f.scheduled_for))}</td><td>${f.run_id ? `<a href="${runHref(f.run_id)}" class="pb-mono">${esc(f.run_id)}</a>` : EM_DASH}</td>
    <td>${mono(f.result_code)}</td><td>${mono(f.error_code)}</td>
    <td>${(f.flagged_models || []).length ? f.flagged_models.map((m) => mono(m)).join(" ") : EM_DASH}</td></tr>`;
}

function firingsCard() {
  const filter = `<form id="pb-firings-filter" class="pb-row-actions" style="padding:12px 20px"><label class="pb-field"><span class="sub">Status</span><select class="pb-input" name="status"><option value="">Any</option>${FIRING_STATUSES.map(
    (s) => `<option value="${s}"${state.firingStatus === s ? " selected" : ""}>${s}</option>`,
  ).join("")}</select></label></form>`;
  if (state.firingsError) return `<section class="card"><h3>Firings</h3>${filter}${errorBox(state.firingsError)}</section>`;
  const firings = state.firings || [];
  const table = firings.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Fired</th><th>How</th><th>Status</th><th>Due slot</th><th>Run</th><th>Result</th><th>Error</th><th>Models flagged</th></tr></thead><tbody>${firings
        .map(firingRow)
        .join("")}</tbody></table></div>`
    : `<p class="empty">${state.firingStatus ? "No firing with this status." : "This schedule has not fired yet."}</p>`;
  return `<section class="card"><h3>Firings · ${esc(firings.length)}, newest first</h3>${filter}${table}</section>`;
}

function editCard(s) {
  if (s.managed_by) {
    return `<section class="card"><h3>Edit</h3><p class="empty">Managed by ${esc(
      s.managed_by,
    )}: its cadence follows the use case's <span class="pb-mono">monitoring.retraining</span> setting. Change that, then sync the retraining schedules.</p></section>`;
  }
  return `<section class="card"><h3>Edit</h3><div class="form-body">
    <form id="pb-schedule-edit" class="pb-form wide" novalidate autocomplete="off">
      <div class="frow">${cadenceFields(s)}</div>
      <div class="frow">${parameterFields(s.parameters || {})}</div>
      <div class="actions">${actionButton("PATCH", "/schedules/{schedule_id}", {
        type: "submit",
        attrs: 'id="pb-schedule-save"',
        label: state.saving ? "Saving…" : "Save changes",
        busy: state.saving,
      })}</div>
    </form>
    ${state.saveError ? errorBox(state.saveError) : ""}
    ${state.saved ? `<div class="pb-ok" role="status">Saved; next due ${esc(fmtStamp(s.next_due_at))}.</div>` : ""}
  </div></section>`;
}

function detailCard(s) {
  const p = s.parameters || {};
  const pairs = [
    ["What it does", esc(KIND_LABEL[s.kind] || s.kind)],
    ["Use case", esc(s.use_case_id)],
    ["Client", s.client_id ? mono(s.client_id) : EM_DASH],
    ["Cadence", esc(cadenceText(s))],
    ["State", s.enabled ? `<span class="pill ok">Active</span>` : `<span class="pill warn">Paused</span>`],
    ["Next due", esc(fmtStamp(s.next_due_at))],
    ["Last fired", esc(fmtStamp(s.last_fired_at))],
    ["Recipe · dataset · model", `${mono(p.onboarding_spec_id)} · ${mono(p.dataset_id)} · ${mono(p.model_version_id)}`],
    ["Created", `${esc(fmtStamp(s.created_at))} by ${esc(s.created_by)}`],
  ];
  return `<section class="card"><h3>${esc(KIND_LABEL[s.kind] || s.kind)} · ${esc(s.use_case_id)}</h3>${pairs
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`)
    .join("")}<div style="padding:12px 20px">${rowActions(s, false)}${messageFor(s)}</div></section>`;
}

export function scheduleHtml(scheduleId) {
  return monitoringScreen(
    "schedules",
    "Schedule",
    "One schedule, its actions and every time it fired, missed slots included.",
    ["GET", "/schedules/{schedule_id}"],
    () => {
      if (state.scheduleError) return errorBox(state.scheduleError);
      const s = state.schedule;
      if (!s || s.schedule_id !== scheduleId) return `<p class="loading">Loading…</p>`;
      return `<p><a class="linkbtn" href="#/monitoring/schedules">‹ All schedules</a></p>${detailCard(s)}${firingsCard()}${editCard(s)}`;
    },
  );
}

// --- binding ------------------------------------------------------------------------------------

/** The cadence a form asks for: a preset word, or the cron line when "custom" is chosen. */
export function cadenceOf(form) {
  const preset = fieldValue(form, "preset");
  return preset === "custom" ? fieldValue(form, "cron") : preset;
}

/** `ScheduleParameters` from a form: only the ids typed, so an empty field is never sent as `""`. */
export function parametersOf(form) {
  const parameters = {};
  for (const key of ["onboarding_spec_id", "dataset_id", "model_version_id"]) {
    const value = fieldValue(form, key);
    if (value) parameters[key] = value;
  }
  return parameters;
}

/** `ScheduleCreateRequest` from the New schedule form. */
export function createPayload(form) {
  const payload = {
    use_case_id: fieldValue(form, "use_case_id"),
    kind: fieldValue(form, "kind"),
    cadence: cadenceOf(form),
    timezone: fieldValue(form, "timezone") || DEFAULT_TIMEZONE,
    parameters: parametersOf(form),
    enabled: form.elements.namedItem("enabled").checked,
  };
  const client = fieldValue(form, "client_id");
  if (client) payload.client_id = client;
  return payload;
}

function firedNotice(firing) {
  const run = firing.run_id ? ` Run <a href="${runHref(firing.run_id)}" class="pb-mono">${esc(firing.run_id)}</a>.` : "";
  const code = firing.error_code || firing.result_code;
  return `Fired: ${statusPill(firing.status)}${code ? ` ${mono(code)}` : ""}.${run}`;
}

/** Run one row action, show its outcome under the row, and reload what it changed. */
async function rowAction(scheduleId, action, repaint, reload, notice) {
  if (state.busy) return;
  state.busy = true;
  state.rowError = null;
  state.rowNotice = null;
  state.confirmDelete = null;
  repaint();
  try {
    const result = await action();
    state.rowNotice = notice ? { scheduleId, html: notice(result) } : null;
  } catch (error) {
    state.rowError = { scheduleId, error };
  }
  state.busy = false;
  await reload();
  repaint();
}

function bindRowActions(main, repaint, reload, afterDelete) {
  main.addEventListener("click", (event) => {
    const target = event.target;
    if (!target || !target.closest) return;
    const hit = (attr) => target.closest(`[${attr}]`);
    let el;
    if ((el = hit("data-fire"))) {
      const id = el.getAttribute("data-fire");
      rowAction(id, () => postScheduleFire(id), repaint, reload, firedNotice);
    } else if ((el = hit("data-disable"))) {
      const id = el.getAttribute("data-disable");
      rowAction(id, () => postScheduleDisable(id), repaint, reload, () => "Paused: it will not fire until resumed.");
    } else if ((el = hit("data-enable"))) {
      const id = el.getAttribute("data-enable");
      rowAction(id, () => postScheduleEnable(id), repaint, reload, (s) => `Resumed; next due ${esc(fmtStamp(s.next_due_at))}.`);
    } else if ((el = hit("data-delete-yes"))) {
      const id = el.getAttribute("data-delete-yes");
      rowAction(id, () => deleteSchedule(id), repaint, afterDelete, null);
    } else if ((el = hit("data-delete"))) {
      state.confirmDelete = el.getAttribute("data-delete");
      repaint();
    } else if (hit("data-delete-no")) {
      state.confirmDelete = null;
      repaint();
    }
  });
}

export function bindSchedules(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindRowActions(main, repaint, loadSchedules, loadSchedules);
  main.addEventListener("click", async (event) => {
    if (!event.target || !event.target.closest || !event.target.closest("#pb-retraining-sync") || state.syncing) return;
    state.syncing = true;
    state.synced = null;
    state.syncError = null;
    repaint();
    try {
      state.synced = await postRetrainingSync();
      await loadSchedules();
    } catch (error) {
      state.syncError = error;
    }
    state.syncing = false;
    repaint();
  });
  main.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form || form.id !== "pb-schedule-create") return;
    event.preventDefault();
    if (state.creating) return;
    state.creating = true;
    state.createError = null;
    state.created = null;
    repaint();
    try {
      state.created = await postSchedule(createPayload(form));
      await loadSchedules();
    } catch (error) {
      state.createError = error;
    }
    state.creating = false;
    repaint();
  });
}

export function bindSchedule(root, repaint) {
  const main = root.querySelector("main");
  if (!main || !state.schedule) return;
  const id = state.schedule.schedule_id;
  const reload = () => loadSchedule(id);
  bindRowActions(main, repaint, reload, async () => {
    window.location.hash = "#/monitoring/schedules";
  });
  const filter = main.querySelector("#pb-firings-filter");
  if (filter) {
    filter.addEventListener("change", async () => {
      state.firingStatus = fieldValue(filter, "status");
      await loadFirings();
      repaint();
    });
    filter.addEventListener("submit", (event) => event.preventDefault());
  }
  main.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form || form.id !== "pb-schedule-edit") return;
    event.preventDefault();
    if (state.saving) return;
    state.saving = true;
    state.saveError = null;
    state.saved = false;
    repaint();
    try {
      state.schedule = await patchSchedule(id, {
        cadence: cadenceOf(form),
        timezone: fieldValue(form, "timezone") || DEFAULT_TIMEZONE,
        parameters: parametersOf(form),
      });
      state.saved = true;
    } catch (error) {
      state.saveError = error;
    }
    state.saving = false;
    repaint();
  });
}

/** Test seam: forget screen state between cases. */
export function _resetSchedulesForTests() {
  Object.assign(state, {
    schedules: null,
    loadError: null,
    useCases: null,
    creating: false,
    createError: null,
    created: null,
    syncing: false,
    synced: null,
    syncError: null,
    confirmDelete: null,
    rowError: null,
    rowNotice: null,
    busy: false,
    schedule: null,
    scheduleError: null,
    firings: null,
    firingsError: null,
    firingStatus: "",
    saving: false,
    saveError: null,
    saved: false,
  });
}
