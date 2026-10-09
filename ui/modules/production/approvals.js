// The Approver's screen (Plan D M54): models waiting for approval - a new model that beats the one in
// use, or the first model of a use case - each against the model in use.
//
// Everything drawn is `GET /approvals` (`api/routes/approvals.py`), and every rule shown is the
// server's:
// * The head-to-head is on the new model's own test split - the rows both models were scored on by
//   the train flow - with every metric that run recorded for both, the difference, and which model
//   each metric favours (DEC-864). A metric the run did not record for the model in use is shown with
//   a dash, never estimated; the server's note says why.
// * Separation of duties (DEC-862): the person who started the training run cannot approve its model.
//   The server says so per model (`can_decide`, `blocked_reason`), so the buttons are drawn disabled
//   with that sentence in their place before anybody clicks - and the server refuses anyway
//   (`403 SEPARATION_OF_DUTIES`). With sign-in off the screen says, in one line, that the rule cannot
//   be enforced.
// * Approve and reject both need a reason, which is kept in the decision record with who decided and
//   against which model. Rejecting archives the new model; approving puts it in use. A stale
//   comparison (the model in use changed since) is flagged: approval will be refused (CHAMPION_CHANGED).
//
// v1: people by name ("Trained by Priya on 24 Sept 2026", looked up from the user list when this
// person may read it, else "another user"); the deciding measure first, by its `help.yaml` name, the
// rest behind "Show all measures"; 3 decimals and Better / Worse; ids under Details. The count of
// models this person may decide is offered to the top bar's "Waiting for approval" badge.
//
// Plan J M106: a model learned from the last campaign also carries `live_calibration`: how the model
// that chose that campaign's list did, predicted against measured change per tenth of its ranking,
// with the server's 95% ranges and sentence. It is advice beside the decision, like the M96 checks.

import { EM_DASH, RESULTS_CRUMB, RUN_FINISHED_EVENT, errorBox, esc, fmtDate, fmtInt, fmtMetric, glossaryMetric, techDetails } from "../../dom.js";
import { refreshTopBar, registerNavSlot } from "../router.js";
import { getApprovals, postApprove, postReject } from "./api.js";
import {
  actionButton,
  fieldError,
  isLocal,
  loadPeople,
  loadUseCaseNames,
  personName,
  refusal,
  rowsTable,
  screenHead,
  statusPill,
  useCaseName,
} from "./controls.js";
import { can, currentMe, onSession, reasonFor, sessionStatus } from "./session.js";

const state = {
  items: null,
  separationEnforced: true,
  loadError: null,
  deciding: null, // model id being decided
  decideError: null, // { modelId, error }
  decided: null, // { modelId, decision, title }
};

// --- the count for the top bar's badge ------------------------------------------------------------

let waiting = null; // how many models this person may decide, or null when unknown

/** The number shown on "Waiting for approval" in the top bar ("" when unknown or none). */
export const approvalsCount = () => (waiting ? String(waiting) : "");

function countFrom(items) {
  waiting = (items || []).filter((item) => item.can_decide).length;
  refreshTopBar();
}

registerNavSlot("badge:approvals", { html: approvalsCount });

/** Whether this person may decide models (an Approver, or anyone with sign-in off): only they ask. */
function mayDecide(me, status) {
  const approver = status === "off" || (me && me.principal && (me.principal.roles || []).includes("approver"));
  return Boolean(approver) && !reasonFor("GET", "/approvals") && can("POST", "/models/{model_id}/approve");
}

/**
 * Ask `GET /approvals` again and redraw the badge, for someone who may decide; nobody else asks. The
 * count used to be read once per sign-in, so it went stale in the tab that trained the model
 * (docs/UI_AUDIT.md §8.4 item 1). It is read again when Results opens, when a training run finishes
 * (`RUN_FINISHED_EVENT`, from the use-case screen), and after a decision here (`loadApprovals`).
 */
export function recountApprovals() {
  if (!mayDecide(currentMe(), sessionStatus())) return Promise.resolve();
  return getApprovals()
    .then((body) => countFrom(body.items))
    .catch(() => {});
}

/** An Approver (or anyone, with sign-in off) learns the count once per sign-in, then on the events above. */
let countedFor = null;
onSession((me, status) => {
  const who = me && me.principal ? me.principal.user_id : status;
  if (who === countedFor) return;
  countedFor = who;
  waiting = null;
  if (!mayDecide(me, status)) {
    refreshTopBar();
    return;
  }
  getApprovals()
    .then((body) => countFrom(body.items))
    .catch(() => {});
});

if (typeof window !== "undefined") {
  window.addEventListener("hashchange", () => {
    if (/^#\/results\/?$/.test(window.location.hash)) recountApprovals();
  });
  window.addEventListener(RUN_FINISHED_EVENT, (event) => {
    const run = event && event.detail;
    if (run && run.mode === "train" && run.state === "done") recountApprovals();
  });
}

export async function loadApprovals() {
  try {
    const [body] = await Promise.all([getApprovals(), loadPeople(), loadUseCaseNames()]);
    state.items = body.items || [];
    state.separationEnforced = body.separation_enforced !== false;
    state.loadError = null;
    countFrom(state.items);
  } catch (error) {
    state.loadError = error;
  }
}

// --- the head-to-head -----------------------------------------------------------------------------

const metricValue = (value) => (value === null || value === undefined ? EM_DASH : fmtMetric(value));

function fmtDifference(row) {
  if (row.difference === null || row.difference === undefined) return EM_DASH;
  const sign = row.difference > 0 ? "+" : "";
  return `${sign}${fmtMetric(row.difference)}`;
}

/** From the new model's side: Better, Worse, Same. */
const BETTER_LABEL = { challenger: "Better", champion: "Worse", equal: "Same" };
const BETTER_CLASS = { challenger: "ok", champion: "bad", equal: "warn" };

/** The usual short names, for when the catalogue is not loaded and the server sent only the key. */
const METRIC_SHORT = {
  roc_auc: "ROC-AUC",
  pr_auc: "PR-AUC",
  f1: "F1",
  recall: "Recall",
  precision: "Precision",
  accuracy: "Accuracy",
  rmse: "RMSE",
  mae: "MAE",
  auuc: "AUUC",
  qini: "Qini",
};

const shortName = (row) => (row.label && row.label !== row.metric ? row.label : METRIC_SHORT[row.metric] || row.label || row.metric);

/** A metric's plain name from `configs/pilot/help.yaml`, else its usual short name. */
function metricName(row) {
  const entry = glossaryMetric(row.metric);
  return entry && entry.name ? entry.name : shortName(row);
}

function metricRow(row, firstModel) {
  const verdict = row.better
    ? `<span class="pill ${BETTER_CLASS[row.better]}" data-better="${esc(row.better)}">${esc(BETTER_LABEL[row.better])}</span>`
    : EM_DASH;
  const plain = metricName(row);
  const short = shortName(row);
  const name = `${esc(plain)}${row.primary ? ` <span class="chip neutral">Main measure</span>` : ""}${
    plain !== short ? `<div class="pb-small">${esc(short)}</div>` : ""
  }`;
  return {
    attrs: `data-metric="${esc(row.metric)}"${row.better === "champion" ? ' class="pb-worse"' : ""}`,
    cells: firstModel
      ? [name, esc(metricValue(row.challenger))]
      : [name, esc(metricValue(row.challenger)), esc(metricValue(row.champion)), esc(fmtDifference(row)), verdict],
  };
}

function measuresTable(rows, firstModel) {
  const cols = firstModel
    ? [{ label: "Measure" }, { label: "New model", num: true }]
    : [
        { label: "Measure" },
        { label: "New model", num: true },
        { label: "Model in use", num: true },
        { label: "Difference", num: true },
        { label: "New model is" },
      ];
  return rowsTable(cols, rows.map((row) => metricRow(row, firstModel)), { cls: "pb-h2h" });
}

function headToHead(h) {
  const firstModel = !h.measured_against_champion_id;
  const rows = h.rows_evaluated === null || h.rows_evaluated === undefined ? null : fmtInt(h.rows_evaluated);
  const lead = firstModel
    ? "This is the first model for this use case: there is no model in use to compare it with."
    : `Compared with the model in use on the same ${rows ? `${rows} customers` : "customers"}, which neither model saw while learning.${
        h.improvement_pct === null || h.improvement_pct === undefined
          ? ""
          : ` It is ${Number(h.improvement_pct).toFixed(1)}% better on the main measure.`
      }`;
  const metrics = h.metrics || [];
  const main = metrics.filter((row) => row.primary);
  const rest = metrics.filter((row) => !row.primary);
  const shown = main.length ? main : metrics.slice(0, 1);
  const hidden = main.length ? rest : metrics.slice(1);
  return `<p class="pb-meta">${esc(lead)}</p>
  ${
    h.stale
      ? `<div class="apierr" role="alert" data-code="CHAMPION_CHANGED"><b>The model in use has changed since this comparison, so approving it will be refused.</b><p class="apierr-fix">Retrain to compare it with the new model in use.</p>${techDetails(
          [
            ["Code", "CHAMPION_CHANGED"],
            ["Model in use now", h.current_champion_id],
          ],
          "Details",
        )}</div>`
      : ""
  }
  ${shown.length ? measuresTable(shown, firstModel) : ""}
  ${
    hidden.length
      ? `<details class="pb-measures"><summary>Show all measures (${hidden.length})</summary>${measuresTable(hidden, firstModel)}</details>`
      : ""
  }
  ${h.note ? `<p class="pb-small" role="note">${esc(h.note)}</p>` : ""}`;
}

// --- advisory checks (Plan J M96) -----------------------------------------------------------------

/** What each check asks, in words; the verdict and its numbers are the server's `message`. */
const CHECK_TITLE = {
  UPLIFT_NOT_BETTER_THAN_RISK: "Beats plain risk ranking",
  UPLIFT_UNSTABLE_ACROSS_FOLDS: "Stable when refitted on other customers",
  UPLIFT_MISCALIBRATED: "Predicted uplift matches what was measured",
};
const CHECK_PILL = {
  true: ["ok", "Passed"],
  false: ["warn", "Check"],
  null: ["pb-pill-none", "Not measured"],
};

/**
 * `item.checks` from `GET /approvals` (`engine.model_gates`): advisory, so they never disable the
 * buttons. Each row is the server's code, verdict and sentence; nothing is computed here.
 */
function checksList(checks) {
  if (!checks || !checks.length) return "";
  const rows = checks
    .map((check) => {
      const verdict = check.passed === true || check.passed === false ? String(check.passed) : "null";
      const [cls, label] = CHECK_PILL[verdict];
      const title = CHECK_TITLE[check.code] || check.code;
      return `<li class="pb-check" data-check="${esc(check.code)}" data-passed="${esc(verdict)}"><span class="pill ${cls}">${esc(
        label,
      )}</span> <b>${esc(title)}</b><div class="pb-small">${esc(check.message)}</div></li>`;
    })
    .join("");
  return `<div class="pb-checks" role="note"><p class="pb-meta">Checks for the Approver (advice only: they do not change the decision rule)</p><ul class="pb-stack pb-checklist">${rows}</ul></div>`;
}

// --- predicted against measured on the last campaign (Plan J M106) --------------------------------

/** A share as signed points ("+21.9 points"); the value is the server's, only its unit changes. */
const points = (value) =>
  value === null || value === undefined ? EM_DASH : `${value > 0 ? "+" : ""}${(Number(value) * 100).toFixed(1)} points`;

const INSIDE_PILL = {
  true: ["ok", "Holds the prediction"],
  false: ["warn", "Does not hold it"],
  null: ["pb-pill-none", "Not measured"],
};

/**
 * `item.live_calibration` from `GET /approvals` (`engine.measurement.learn`): for a model learned from
 * a campaign, how the model that chose that campaign's list did, tenth by tenth. Every number and the
 * sentence are the server's; nothing is computed here but the unit. Absent for any other model.
 */
function liveCalibration(block) {
  if (!block) return "";
  const rows = (block.deciles || []).map((d) => {
    const m = d.measured;
    const range =
      m && m.ci_low !== null && m.ci_low !== undefined && m.ci_high !== null && m.ci_high !== undefined
        ? `${points(m.ci_low)} to ${points(m.ci_high)}`
        : EM_DASH;
    const verdict = d.inside_range === true || d.inside_range === false ? String(d.inside_range) : "null";
    const [cls, label] = INSIDE_PILL[verdict];
    return {
      attrs: `data-decile="${esc(d.decile)}" data-inside="${esc(verdict)}"`,
      cells: [
        esc(d.decile),
        esc(fmtInt(d.rows)),
        esc(points(d.predicted)),
        esc(m ? points(m.value) : EM_DASH),
        esc(range),
        `<span class="pill ${cls}">${esc(label)}</span>`,
      ],
    };
  });
  const cols = [
    { label: "Tenth" },
    { label: "Customers", num: true },
    { label: "Predicted change", num: true },
    { label: "Measured change", num: true },
    { label: "95% range" },
    { label: "Prediction inside the range" },
  ];
  const verdict =
    block.matches === true || block.matches === false ? String(block.matches) : "null";
  return `<div class="pb-live" data-live="${esc(block.source_run_id)}" data-matches="${esc(verdict)}" role="note">
    <p class="pb-meta">How the model in use did on the last campaign (predicted against measured, by tenth of its ranking)</p>
    <p class="pb-small">${esc(block.summary)}</p>
    <details class="pb-measures"><summary>Show the ten tenths</summary>${rowsTable(cols, rows, { cls: "pb-live-table" })}</details>
  </div>`;
}

function decisionsList(decisions) {
  if (!decisions || !decisions.length) return "";
  const verb = { approve: "Approved", reject: "Rejected" };
  return `<ul class="pb-small">${decisions
    .map(
      (d) =>
        `<li>${esc(verb[d.decision] || d.decision)} by ${esc(personName(d.decided_by))} on ${esc(fmtDate(d.decided_at))}${
          d.reason ? `: ${esc(d.reason)}` : ""
        }</li>`,
    )
    .join("")}</ul>`;
}

function decisionForm(item) {
  const id = item.version.model_id;
  const failed = state.decideError && state.decideError.modelId === id ? state.decideError.error : null;
  if (!item.can_decide) {
    return `<div class="pb-why" role="note" data-blocked="${esc(id)}">${esc(item.blocked_reason || "You cannot decide on this model.")}</div>`;
  }
  const busy = state.deciding === id;
  return `<form class="pb-stack pb-decide" data-model="${esc(id)}" novalidate autocomplete="off">
    <label class="field wide pb-field"><span class="sub">Reason (required)</span><span class="control area"><textarea name="reason" rows="2" maxlength="2000" aria-describedby="pb-reason-${esc(
      id,
    )}"></textarea></span><span class="pb-hint">Kept in the decision record with your name.</span></label>
    ${failed && isLocal(failed) ? fieldError(failed, `pb-reason-${id}`) : ""}
    <div class="pb-form-actions">${actionButton("POST", "/models/{model_id}/approve", {
      type: "submit",
      attrs: `data-decision="approve" data-model="${esc(id)}"`,
      label: busy ? "Working…" : "Approve",
      busy,
    })}<span class="spacer"></span><span class="reason">Rejecting archives this model.</span>${actionButton("POST", "/models/{model_id}/reject", {
      cls: "btn danger",
      type: "submit",
      attrs: `data-decision="reject" data-model="${esc(id)}"`,
      label: "Reject",
      busy,
    })}</div>${failed && !isLocal(failed) ? errorBox(failed) : ""}
  </form>`;
}

/** "Telco Customer Churn · version 2". */
const itemTitle = (v) => `${useCaseName(v.use_case_id)} · version ${v.version}`;

function trainedBy(item) {
  const who = item.trained_by ? personName(item.trained_by) : null;
  const when = fmtDate(item.version.created_at);
  if (!who) return `Trained on ${when} (who started it was not recorded).`;
  return `Trained by ${who} on ${when}.`;
}

function itemCard(item) {
  const v = item.version;
  return `<section class="card" data-approval="${esc(v.model_id)}"><h3>${esc(itemTitle(v))} ${statusPill(v.status)}</h3><div class="card-body">
    <p class="pb-meta">${esc(trainedBy(item))}</p>
    ${headToHead(item.head_to_head || { metrics: [] })}
    ${checksList(item.checks)}
    ${liveCalibration(item.live_calibration)}
    ${decisionsList(item.decisions)}
    ${decisionForm(item)}
    ${techDetails([
      ["Model", v.model_id],
      ["Kind of model", v.model_display_name],
      ["Training run", v.run_id],
      ["Trained by (id)", item.trained_by],
      ["Compared with", (item.head_to_head || {}).measured_against_champion_id],
      ["Customers compared on", (item.head_to_head || {}).rows_evaluated],
      ...(item.live_calibration
        ? [
            ["Learned from run", item.live_calibration.source_run_id],
            ["List chosen by model", item.live_calibration.model_id],
          ]
        : []),
    ])}
  </div></section>`;
}

function listHtml() {
  if (state.loadError) return errorBox(state.loadError, { retry: true });
  if (!state.items) return `<p class="loading">Loading…</p>`;
  const done = state.decided
    ? `<div class="pb-ok" role="status">${esc(state.decided.title)} ${esc(
        state.decided.decision === "approve" ? "was approved: it is now the model in use." : "was rejected and archived.",
      )}</div>`
    : "";
  const note =
    state.separationEnforced && sessionStatus() !== "off"
      ? ""
      : `<p class="pb-note" role="note">Sign-in is off, so the rule that whoever trained a model cannot approve it is not enforced here.</p>`;
  if (!state.items.length) {
    return `${done}${note}<section class="card"><div class="empty-state"><p class="es-t">No model is waiting for approval.</p><p>When an Analyst trains a model that beats the current one, or the first model of a use case, it appears here.</p></div></section>`;
  }
  return `${done}${note}${state.items.map(itemCard).join("")}`;
}

export function approvalsHtml() {
  const head = screenHead({
    trail: [RESULTS_CRUMB],
    title: "Waiting for approval",
    desc: "Models waiting for your approval: new models that beat the one in use, and the first model of a use case.",
  });
  const refused = reasonFor("GET", "/approvals");
  if (refused) return `<main class="screen pb-screen">${head}${refusal(refused)}</main>`;
  return `<main class="screen pb-screen">${head}<div class="stack">${listHtml()}</div></main>`;
}

function signedInName() {
  const who = currentMe();
  return (who && who.principal && (who.principal.username || who.principal.user_id)) || "local operator";
}

export function bindApprovals(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  let pressed = "approve";
  main.addEventListener("click", (event) => {
    const button = event.target && event.target.closest ? event.target.closest("[data-decision]") : null;
    if (button) pressed = button.getAttribute("data-decision");
  });
  main.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form || !form.classList || !form.classList.contains("pb-decide")) return;
    event.preventDefault();
    if (state.deciding) return;
    const modelId = form.getAttribute("data-model");
    const decision = (event.submitter && event.submitter.getAttribute("data-decision")) || pressed;
    const reason = form.elements.namedItem("reason").value.trim();
    state.decided = null;
    if (reason.length < 3) {
      state.decideError = { modelId, error: { code: "REASON_REQUIRED", message: "Give a reason for the decision." } };
      repaint();
      return;
    }
    const item = (state.items || []).find((i) => i.version.model_id === modelId);
    state.deciding = modelId;
    state.decideError = null;
    repaint();
    try {
      if (decision === "reject") await postReject(modelId, { reason });
      else await postApprove(modelId, { approved_by: signedInName(), reason });
      state.decided = { modelId, decision, title: item ? itemTitle(item.version) : modelId };
      await loadApprovals();
    } catch (error) {
      state.decideError = { modelId, error };
    }
    state.deciding = null;
    repaint();
  });
}

/** Test seam: forget screen state between cases. */
export function _resetApprovalsForTests() {
  waiting = null;
  countedFor = null;
  Object.assign(state, {
    items: null,
    separationEnforced: true,
    loadError: null,
    deciding: null,
    decideError: null,
    decided: null,
  });
}
