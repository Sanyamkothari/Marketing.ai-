// Schedules (Phase 4b M49): the list, a new schedule, one schedule with its run history, and the
// actions on it - edit, pause and resume, run now, delete. Alerts and missed runs are `alerts.js`; the
// scored customer lists and their outcomes are `outcomes.js` (Campaigns). Alerts, Schedules and Missed
// runs share this file's header and tabs, under "Model health".
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
//   be `failed` with an `error_code` - shown as the firing's result, in words, not as a failed request.
// * A schedule `managed_by` the retraining setting cannot be edited here (`409 SCHEDULE_MANAGED`): the
//   edit form is replaced by that explanation. "Update retraining schedules" asks the server to bring
//   the managed ones in line with the recipes now (DEC-783), instead of at the next restart.
// * The run history is the server's, newest first, missed slots included (`status=missed`), and
//   each run links to that run's campaign screen.
//
// v1: the list comes first with one primary: "New schedule" in the header, or - once the form is open,
// which it always is while nothing is scheduled - the form's own "Create schedule", the header button
// then secondary. The empty state is the sentence and that form, no "More actions": the retraining
// sync sits on its own quiet line there, so it stays reachable with nothing scheduled. The form asks
// for the use case, what it does, how often and whether to start now, and keeps the ids and the cron
// line under Advanced (the payload is unchanged). Delete is a quiet red button set apart that asks
// twice - a second, red-filled "Yes, delete" in the row -
// because `window.confirm` is a browser dialog no test (and no screen reader flow) handles well, and
// a schedule's history goes with it. Codes such as `DATASET_COMPOSITE_KEY_NOT_WIRED` read as plain
// words; the code itself is under "Show more columns" or Details.

import {
  EM_DASH,
  errorBox,
  esc,
  fmtDate,
  fmtStamp,
  headActions,
  noticeCard,
  notFound,
  techDetails,
} from "../../dom.js";
import {
  deleteSchedule,
  getFirings,
  getMissedFirings,
  getSchedule,
  getSchedules,
  patchSchedule,
  postSchedule,
  postScheduleDisable,
  postScheduleEnable,
  postScheduleFire,
  postRetrainingSync,
} from "./api.js";
import {
  actionButton,
  codeWords,
  dangerConfirm,
  fieldValue,
  loadPeople,
  loadUseCaseNames,
  mono,
  personName,
  refusal,
  rowsTable,
  screenHead,
  selectField,
  spanRow,
  statusPill,
  statusWord,
  tabStrip,
  textField,
  useCaseList,
  useCaseName,
} from "./controls.js";
import { reasonFor } from "./session.js";

export const MONITORING_TABS = [
  ["alerts", "Alerts", "#/monitoring/alerts"],
  ["schedules", "Schedules", "#/monitoring/schedules"],
  ["missed", "Missed runs", "#/monitoring/missed"],
];

export const KIND_LABEL = {
  score: "Score new data",
  drift_check: "Check if new customers look different",
  retrain: "Retrain the model",
};
const KIND_HELP = {
  drift_check:
    "Compares the newest customers with the ones the model learned from. If they look very different, its scores may be less reliable and an alert is raised.",
  retrain: "Trains a new model on recent data. It is used only after an Approver approves it.",
};
export const PRESETS = ["daily", "weekly", "monthly"];
const PRESET_LABEL = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
export const DEFAULT_TIMEZONE = "Asia/Kolkata";
export const FIRING_STATUSES = ["queued", "running", "succeeded", "failed", "missed"];
const TRIGGER_WORDS = { scheduled: "On schedule", manual: "Run now", catch_up: "Catch-up", missed: "Missed" };

// --- the Model health header and tabs --------------------------------------------------------------

let missedCount = 0;

/** How many missed runs are on record: the Missed runs tab is offered only when there are some. */
export const missedRuns = () => missedCount;

/** Learn the missed runs' count (a plain read); a failure leaves the tab hidden. */
export async function loadMissedCount() {
  if (reasonFor("GET", "/monitoring/missed-firings")) return;
  try {
    missedCount = ((await getMissedFirings({ limit: 500 })).firings || []).length;
  } catch {
    // the count is a courtesy: the route stays reachable
  }
}

export function setMissedCount(n) {
  missedCount = n;
}

export const monitoringHead = (title, desc, actions = "") =>
  screenHead({ trail: [{ label: "Model health", href: "#/monitoring/alerts" }], title, desc, actions });

/** A Model health screen, or the server's reason it may not be read. */
export function monitoringScreen(tab, title, desc, readRoute, body, actions = "") {
  const refused = reasonFor(readRoute[0], readRoute[1]);
  const head = monitoringHead(title, desc, refused ? "" : actions);
  const tabs = tabStrip(
    tab,
    MONITORING_TABS.filter(([key]) => key !== "missed" || tab === "missed" || missedCount > 0),
    "Model health",
  );
  if (refused) return `<main class="screen pb-screen">${head}${tabs}${refusal(refused)}</main>`;
  return `<main class="screen pb-screen">${head}${tabs}<div class="stack">${body()}</div></main>`;
}

/** "Monthly · 02:00 Asia/Kolkata", or "Custom: <cron line> · <zone>". */
export function cadenceText(schedule) {
  const zone = schedule.timezone || DEFAULT_TIMEZONE;
  if (schedule.preset) return `${PRESET_LABEL[schedule.preset] || schedule.preset} · 02:00 ${zone}`;
  return `Custom: ${schedule.cron} · ${zone}`;
}

/** "monthly at 02:00 (Asia/Kolkata)", for a sentence. */
function cadenceSentence(schedule) {
  const zone = schedule.timezone || DEFAULT_TIMEZONE;
  if (schedule.preset) return `${schedule.preset} at 02:00 (${zone})`;
  return `on the cron line ${schedule.cron} (${zone})`;
}

/** A next or last run in the reader's own clock, said so. */
const yourTime = (iso) => (iso ? `${esc(fmtStamp(iso))}<div class="pb-small">your time</div>` : EM_DASH);

const scheduleHref = (id) => `#/monitoring/schedules/${encodeURIComponent(id)}`;
export const runHref = (id) => `#/monitoring/runs/${encodeURIComponent(id)}`;

const state = {
  schedules: null,
  loadError: null,
  useCasesLoading: false,
  newOpen: false,
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

export async function loadSchedules() {
  state.useCasesLoading = !useCaseList();
  try {
    const [body] = await Promise.all([getSchedules(), loadUseCaseNames(), loadMissedCount()]);
    state.schedules = body.schedules || [];
    state.loadError = null;
  } catch (error) {
    state.loadError = error;
  }
  state.useCasesLoading = false;
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
      loadUseCaseNames(),
      loadPeople(),
      loadMissedCount(),
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

// --- the form -----------------------------------------------------------------------------------

function useCaseField() {
  const list = useCaseList();
  if (!list && (state.useCasesLoading || !state.schedules)) {
    return `<label class="field pb-field"><span class="sub">Use case</span><span class="control sel"><select name="use_case_id" disabled><option value="">Loading use cases…</option></select></span></label>`;
  }
  if (!list || !list.length) return textField("use_case_id", "Use case id", { attrs: "required" });
  return selectField(
    "use_case_id",
    "Use case",
    list.map((uc) => [uc.id, uc.name]),
  );
}

function cadenceField(current = null) {
  const preset = current ? current.preset || "custom" : "monthly";
  return selectField(
    "preset",
    "How often",
    [...PRESETS, "custom"].map((p) => [p, p === "custom" ? "Custom (a cron line, under Advanced)" : `${PRESET_LABEL[p]} at 02:00`]),
    { value: preset },
  );
}

function advancedFields(current = null, parameters = {}) {
  const cron = current && !current.preset ? current.cron : "";
  return `${textField("cron", "Cron line, for Custom", {
    value: cron,
    placeholder: "minute hour day month weekday, e.g. 0 6 1 * *",
    hint: "Used only when How often is Custom.",
  })}${textField("timezone", "Time zone", { value: current ? current.timezone : DEFAULT_TIMEZONE })}${textField(
    "onboarding_spec_id",
    "Recipe id",
    { value: parameters.onboarding_spec_id || "", hint: "Rebuilds the dataset from the client's newest tables." },
  )}${textField("dataset_id", "Or a fixed dataset id", { value: parameters.dataset_id || "" })}${textField(
    "model_version_id",
    "Model version id",
    { value: parameters.model_version_id || "", hint: "Leave empty to use the model in use." },
  )}`;
}

const newOpen = () =>
  Boolean(state.newOpen || state.creating || state.createError || (state.schedules && !state.schedules.length));

function newCard() {
  const open = newOpen();
  return `<section class="card" id="pb-schedule-new"${open ? "" : " hidden"}><h3>New schedule</h3><div class="card-body">
    <form id="pb-schedule-create" class="pb-stack" novalidate autocomplete="off">
      <div class="frow">${useCaseField()}
        ${selectField(
          "kind",
          "What it does",
          Object.entries(KIND_LABEL).map(([kind, label]) => [kind, label]),
          { hint: "Scoring reads a recipe or a dataset: name one under Advanced." },
        )}
        ${cadenceField()}
      </div>
      <label class="check"><input type="checkbox" name="enabled" checked> Start it now (untick to create it paused)</label>
      <details class="adv"><summary>Advanced</summary><div class="pb-adv-body"><div class="frow">
        ${textField("client_id", "Client id", { hint: "Leave empty to use the recipe's client." })}
        ${advancedFields()}
      </div></div></details>
      <div class="pb-form-actions">${actionButton("POST", "/schedules", {
        type: "submit",
        attrs: 'id="pb-schedule-create-submit"',
        label: state.creating ? "Saving…" : "Create schedule",
        busy: state.creating,
      })}<button type="button" class="btn quiet" data-new-close>Cancel</button><span class="reason">A retrain makes a new model; an Approver still decides whether it is used.</span></div>
    </form>
    ${state.createError ? errorBox(state.createError) : ""}
  </div></section>`;
}

// --- the list -----------------------------------------------------------------------------------

function rowActions(s) {
  const id = esc(s.schedule_id);
  if (state.confirmDelete === s.schedule_id) {
    return `<div class="pb-row-actions">${dangerConfirm(
      "Delete it and its history?",
      actionButton("DELETE", "/schedules/{schedule_id}", {
        cls: "btn danger confirm sm",
        attrs: `data-delete-yes="${id}"`,
        label: "Yes, delete",
      }),
      "data-delete-no",
    )}</div>`;
  }
  const toggle = s.enabled
    ? actionButton("POST", "/schedules/{schedule_id}/disable", { cls: "btn quiet sm", attrs: `data-disable="${id}"`, label: "Pause", explain: false })
    : actionButton("POST", "/schedules/{schedule_id}/enable", { cls: "btn quiet sm", attrs: `data-enable="${id}"`, label: "Resume", explain: false });
  return `<div class="pb-row-actions">${actionButton("POST", "/schedules/{schedule_id}/fire", {
    cls: "btn quiet sm",
    attrs: `data-fire="${id}"`,
    label: "Run now",
  })}${toggle}<span class="spacer"></span>${actionButton("DELETE", "/schedules/{schedule_id}", {
    cls: "btn quiet sm pb-quiet-bad",
    attrs: `data-delete="${id}"`,
    explain: false,
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

const LIST_COLUMNS = [
  { label: "What" },
  { label: "Use case" },
  { label: "How often" },
  { label: "Next run" },
  { label: "Status" },
  { label: "", sr: "Actions" },
  { label: "Client", more: true },
  { label: "Last run", more: true },
];

const activePill = (s) =>
  s.enabled ? `<span class="pill ok" data-status="active">Active</span>` : `<span class="pill warn" data-status="paused">Paused</span>`;

function scheduleRow(s) {
  const message = messageFor(s);
  return {
    attrs: `data-schedule="${esc(s.schedule_id)}"`,
    cells: [
      `<a href="${scheduleHref(s.schedule_id)}">${esc(KIND_LABEL[s.kind] || s.kind)}</a>${
        s.managed_by ? `<div class="pb-small">Follows the use case's retraining setting</div>` : ""
      }`,
      esc(useCaseName(s.use_case_id)),
      esc(cadenceText(s)),
      yourTime(s.next_due_at),
      activePill(s),
      rowActions(s),
      s.client_id ? mono(s.client_id) : `<span class="pb-small">The recipe's</span>`,
      yourTime(s.last_fired_at),
    ],
    after: message ? spanRow(LIST_COLUMNS.length, message) : "",
  };
}

/** "Update retraining schedules now": under More actions in the list, and on its own quiet line in
 * the empty state - a use case set to retrain on its own has no schedule until it runs. */
function moreActions({ inline = false } = {}) {
  const button = actionButton("POST", "/schedules/retraining/sync", {
    cls: inline ? "btn quiet sm" : "btn secondary sm",
    attrs: 'id="pb-retraining-sync"',
    label: state.syncing ? "Updating…" : "Update retraining schedules now",
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
  const hint = `<span class="pb-hint">Brings the retraining schedules in line with each use case's settings now, instead of at the next restart.</span>`;
  if (inline) return `<div class="card-body"><div class="pb-row-actions">${button}${hint}${result}</div></div>`;
  const open = state.syncing || r || state.syncError;
  return `<div class="card-body"><details class="pb-more-actions"${open ? " open" : ""}><summary class="btn quiet sm">More actions</summary><div class="pb-row-actions">${button}<span class="pb-hint">Brings the retraining schedules in line with each use case's settings now, instead of at the next restart.</span>${result}</div></details></div>`;
}

function missedNotice() {
  if (!missedCount) return "";
  return noticeCard({
    title: `${missedCount} scheduled run${missedCount === 1 ? " was" : "s were"} missed`,
    text: "Runs that should have happened while Marketing AI was switched off. Each schedule ran once to catch up.",
    action: { label: "See missed runs", href: "#/monitoring/missed" },
  });
}

function listCard() {
  const title = (n) => `<h3>Schedules${n === null ? "" : ` · ${esc(n)}`} <span class="sort-note">(next run first)</span></h3>`;
  if (state.loadError) {
    return `<section class="card">${title(null)}<div class="card-body">${errorBox(state.loadError, { retry: true })}</div></section>`;
  }
  if (!state.schedules) return `<section class="card">${title(null)}<p class="loading pb-pad">Loading…</p></section>`;
  const created = state.created
    ? `<div class="card-body"><div class="pb-ok" role="status">Created <a href="${scheduleHref(state.created.schedule_id)}">${esc(
        KIND_LABEL[state.created.kind] || state.created.kind,
      )} · ${esc(useCaseName(state.created.use_case_id))}</a>; next run ${esc(fmtStamp(state.created.next_due_at))} your time.</div></div>`
    : "";
  if (!state.schedules.length) {
    return `<section class="card">${title(0)}${created}<div class="empty-state"><p class="es-t">Nothing runs on its own yet.</p><p>Schedule monthly scoring or a check on new customers, so you don't have to remember. Use the form below.</p></div>${moreActions({ inline: true })}</section>`;
  }
  const sorted = [...state.schedules].sort(
    (a, b) => Number(!a.enabled) - Number(!b.enabled) || String(a.next_due_at).localeCompare(String(b.next_due_at)),
  );
  return `<section class="card">${title(state.schedules.length)}${created}${rowsTable(LIST_COLUMNS, sorted.map(scheduleRow), {
    cls: "pb-schedules",
  })}${moreActions()}</section>`;
}

export const schedulesHtml = () =>
  monitoringScreen(
    "schedules",
    "Schedules",
    "Scoring, checks on new customers and retraining that run on their own. Every change and every run is in the audit log.",
    ["GET", "/schedules"],
    () => `${missedNotice()}${listCard()}${newCard()}`,
    headActions({
      primary: actionButton("POST", "/schedules", {
        cls: newOpen() ? "btn secondary" : "btn primary", // the form's own button is the primary once open
        attrs: `id="pb-schedule-new-open" aria-controls="pb-schedule-new" aria-expanded="${newOpen()}"`,
        label: "New schedule",
      }),
    }),
  );

// --- one schedule -------------------------------------------------------------------------------

function firingRow(f) {
  const code = f.error_code || f.result_code;
  return {
    attrs: `data-firing="${esc(f.firing_id || "")}"`,
    cells: [
      esc(fmtStamp(f.fired_at)),
      `${statusPill(f.status)}${code ? `<div class="pb-small">${codeWords(code)}</div>` : ""}`,
      f.run_id ? `<a href="${runHref(f.run_id)}">See this run</a>` : EM_DASH,
      esc(TRIGGER_WORDS[f.trigger] || String(f.trigger || EM_DASH).replace(/_/g, " ")),
      esc(fmtStamp(f.scheduled_for)),
      [mono(f.result_code), mono(f.error_code)].filter((v) => v !== EM_DASH).join(" ") || EM_DASH,
      f.run_id ? mono(f.run_id) : EM_DASH,
      (f.flagged_models || []).length ? f.flagged_models.map((m) => mono(m)).join(" ") : EM_DASH,
    ],
  };
}

function historyCard() {
  const filter = `<form id="pb-firings-filter" class="pb-filters">${selectField(
    "status",
    "Show",
    [["", "Every run"], ...FIRING_STATUSES.map((s) => [s, statusWord(s)])],
    { value: state.firingStatus },
  )}</form>`;
  const title = (n) => `<h3>Run history${n === null ? "" : ` · ${esc(n)}`} <span class="sort-note">(newest first)</span></h3>`;
  if (state.firingsError) return `<section class="card">${title(null)}<div class="card-body">${filter}${errorBox(state.firingsError)}</div></section>`;
  const firings = state.firings || [];
  const table = firings.length
    ? rowsTable(
        [
          { label: "When" },
          { label: "Result" },
          { label: "Run" },
          { label: "How it started", more: true },
          { label: "Due", more: true },
          { label: "Code", more: true },
          { label: "Run id", more: true },
          { label: "Models to retrain", more: true },
        ],
        firings.map(firingRow),
      )
    : `<div class="empty-state"><p class="es-t">${state.firingStatus ? "No run has this result." : "This schedule has not run yet."}</p></div>`;
  return `<section class="card">${title(firings.length)}<div class="card-body">${filter}</div>${table}</section>`;
}

function editCard(s) {
  if (s.managed_by) {
    return `<section class="card"><h3>Change this schedule</h3><div class="card-body"><p class="pb-note">This schedule follows the use case's retraining setting, so it is changed there. After changing the setting, use "Update retraining schedules now" on the Schedules page.</p>${techDetails(
      [
        ["Managed by", s.managed_by],
        ["Setting", "monitoring.retraining"],
      ],
    )}</div></section>`;
  }
  const open = state.saving || state.saved || Boolean(state.saveError);
  return `<section class="card"><div class="card-body"><details class="adv" id="pb-schedule-edit-panel"${open ? " open" : ""}><summary>Change how often it runs or what it reads</summary><div class="pb-adv-body">
    <form id="pb-schedule-edit" class="pb-stack" novalidate autocomplete="off">
      <div class="frow">${cadenceField(s)}${advancedFields(s, s.parameters || {})}</div>
      <div class="pb-form-actions">${actionButton("PATCH", "/schedules/{schedule_id}", {
        cls: "btn secondary",
        type: "submit",
        attrs: 'id="pb-schedule-save"',
        label: state.saving ? "Saving…" : "Save changes",
        busy: state.saving,
      })}</div>
    </form>
    ${state.saveError ? errorBox(state.saveError) : ""}
    ${state.saved ? `<div class="pb-ok" role="status">Saved. Next run ${esc(fmtStamp(s.next_due_at))} your time.</div>` : ""}
  </div></details></div></section>`;
}

function factsCard(s) {
  const p = s.parameters || {};
  const pairs = [
    ["What it does", esc(KIND_LABEL[s.kind] || s.kind)],
    ["Use case", esc(useCaseName(s.use_case_id))],
    ["How often", esc(cadenceText(s))],
    ["Status", activePill(s)],
    ["Next run", `${esc(fmtStamp(s.next_due_at))} your time`],
    ["Last run", s.last_fired_at ? `${esc(fmtStamp(s.last_fired_at))} your time` : "Not yet"],
    ["Created", `${esc(fmtDate(s.created_at))} by ${esc(personName(s.created_by))}`],
  ];
  const deleteArea =
    state.confirmDelete === s.schedule_id
      ? dangerConfirm(
          "Delete it and its history?",
          actionButton("DELETE", "/schedules/{schedule_id}", {
            cls: "btn danger confirm sm",
            attrs: `data-delete-yes="${esc(s.schedule_id)}"`,
            label: "Yes, delete",
          }),
          "data-delete-no",
        )
      : actionButton("DELETE", "/schedules/{schedule_id}", {
          cls: "btn quiet sm pb-quiet-bad",
          attrs: `data-delete="${esc(s.schedule_id)}"`,
          label: "Delete this schedule",
        });
  return `<section class="card"><h3>About this schedule</h3>${pairs
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`)
    .join("")}<div class="card-body">${techDetails([
    ["Schedule", s.schedule_id],
    ["Client", s.client_id],
    ["Recipe", p.onboarding_spec_id],
    ["Dataset", p.dataset_id],
    ["Model version", p.model_version_id],
    ["Created by (id)", s.created_by],
  ])}<div class="pb-row-actions pb-danger-row"><span class="spacer"></span>${deleteArea}</div></div></section>`;
}

export function scheduleHtml(scheduleId) {
  const s = state.schedule && state.schedule.schedule_id === scheduleId ? state.schedule : null;
  const title = s ? `${KIND_LABEL[s.kind] || s.kind} · ${useCaseName(s.use_case_id)}` : "Schedule";
  const actions = s
    ? headActions({
        primary: actionButton("POST", "/schedules/{schedule_id}/fire", {
          cls: "btn primary",
          attrs: `data-fire="${esc(s.schedule_id)}"`,
          label: "Run now",
        }),
        secondary: [
          s.enabled
            ? actionButton("POST", "/schedules/{schedule_id}/disable", {
                cls: "btn secondary",
                attrs: `data-disable="${esc(s.schedule_id)}"`,
                label: "Pause",
              })
            : actionButton("POST", "/schedules/{schedule_id}/enable", {
                cls: "btn secondary",
                attrs: `data-enable="${esc(s.schedule_id)}"`,
                label: "Resume",
              }),
        ],
        related: { label: "All schedules", href: "#/monitoring/schedules" },
      })
    : "";
  const desc = s ? `Runs ${cadenceSentence(s)}.` : "One schedule and every time it ran.";
  return monitoringScreen(
    "schedules",
    title,
    desc,
    ["GET", "/schedules/{schedule_id}"],
    () => {
      if (state.scheduleError) {
        return Number(state.scheduleError.status) === 404
          ? notFound("schedule", { href: "#/monitoring/schedules", label: "Schedules" }, state.scheduleError)
          : errorBox(state.scheduleError, { retry: true });
      }
      if (!s) return `<p class="loading">Loading…</p>`;
      const message = messageFor(s);
      return `${KIND_HELP[s.kind] ? `<p class="pb-desc">${esc(KIND_HELP[s.kind])}</p>` : ""}${message}${factsCard(s)}${historyCard()}${editCard(s)}`;
    },
    actions,
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
  const code = firing.error_code || firing.result_code;
  const run = firing.run_id ? ` <a href="${runHref(firing.run_id)}">See this run</a>.` : "";
  return `Ran now: ${statusPill(firing.status)}${code ? ` ${codeWords(code)}` : ""}.${run}${techDetails([
    ["Result", firing.status],
    ["Code", code],
    ["Run", firing.run_id],
  ])}`;
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
      rowAction(id, () => postScheduleEnable(id), repaint, reload, (s) => `Resumed; next run ${esc(fmtStamp(s.next_due_at))} your time.`);
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

/** Choosing "Custom" opens Advanced, where the cron line is typed. */
function bindCustomCadence(main) {
  main.addEventListener("change", (event) => {
    const select = event.target;
    if (!select || select.name !== "preset" || select.value !== "custom") return;
    const form = select.closest("form");
    const advanced = form && form.querySelector("details.adv");
    if (advanced) advanced.open = true;
    const cron = form && form.elements.namedItem("cron");
    if (cron) cron.focus();
  });
}

export function bindSchedules(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindRowActions(main, repaint, loadSchedules, loadSchedules);
  bindCustomCadence(main);
  main.addEventListener("click", async (event) => {
    const target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("#pb-schedule-new-open")) {
      state.newOpen = !state.newOpen;
      repaint();
      if (state.newOpen) {
        const first = document.querySelector('#pb-schedule-create [name="use_case_id"]');
        if (first) first.focus();
      }
      return;
    }
    if (target.closest("[data-new-close]")) {
      state.newOpen = false;
      state.createError = null;
      repaint();
      return;
    }
    if (!target.closest("#pb-retraining-sync") || state.syncing) return;
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
      state.newOpen = false;
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
  bindCustomCadence(main);
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
  missedCount = 0;
  Object.assign(state, {
    schedules: null,
    loadError: null,
    useCasesLoading: false,
    newOpen: false,
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
