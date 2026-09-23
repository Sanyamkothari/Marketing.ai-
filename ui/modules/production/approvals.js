// The Approver's screen (Plan D M54): challengers waiting for approval, each against its champion.
//
// Everything drawn is `GET /approvals` (`api/routes/approvals.py`), and every rule shown is the
// server's:
// * The head-to-head is on the challenger's own test split - the rows both models were scored on by
//   the train flow - with every metric that run recorded for both, the difference, and which model
//   each metric favours (DEC-864). A metric the run did not record for the champion is shown with a
//   dash, never estimated; the server's note says why.
// * Separation of duties (DEC-862): the person who started the training run cannot approve its model.
//   The server says so per challenger (`can_decide`, `blocked_reason`), so the buttons are drawn
//   disabled with that sentence beside them before anybody clicks - and the server refuses anyway
//   (`403 SEPARATION_OF_DUTIES`). With sign-in off the screen says the rule cannot be enforced.
// * Approve and reject both need a reason, which is kept in the decision record with who decided and
//   against which champion. Rejecting archives the challenger; approving makes it champion. A stale
//   comparison (the champion changed since) is flagged: approval will be refused (CHAMPION_CHANGED).

import { EM_DASH, errorBox, esc, fmtStamp, pageHead } from "../../dom.js";
import { getApprovals, postApprove, postReject } from "./api.js";
import { actionButton, mono, refusal, statusPill } from "./controls.js";
import { currentMe, reasonFor } from "./session.js";

const state = {
  items: null,
  separationEnforced: true,
  loadError: null,
  deciding: null, // model id being decided
  decideError: null, // { modelId, error }
  decided: null, // { modelId, decision, version }
};

export async function loadApprovals() {
  try {
    const body = await getApprovals();
    state.items = body.items || [];
    state.separationEnforced = body.separation_enforced !== false;
    state.loadError = null;
  } catch (error) {
    state.loadError = error;
  }
}

const fmtMetric = (value) => (value === null || value === undefined ? EM_DASH : Number(value).toFixed(4));

function fmtDifference(row) {
  if (row.difference === null || row.difference === undefined) return EM_DASH;
  const sign = row.difference > 0 ? "+" : "";
  return `${sign}${Number(row.difference).toFixed(4)}`;
}

const BETTER_LABEL = { challenger: "Challenger better", champion: "Champion better", equal: "Equal" };
const BETTER_CLASS = { challenger: "ok", champion: "bad", equal: "warn" };

function metricRow(row) {
  const verdict = row.better
    ? `<span class="pill ${BETTER_CLASS[row.better]}" data-better="${esc(row.better)}">${esc(BETTER_LABEL[row.better])}</span>`
    : EM_DASH;
  return `<tr data-metric="${esc(row.metric)}"${row.better === "champion" ? ' class="pb-worse"' : ""}><td>${esc(row.label)}${
    row.primary ? ' <span class="pb-small">(decides)</span>' : ""
  }</td><td>${esc(fmtMetric(row.challenger))}</td><td>${esc(fmtMetric(row.champion))}</td><td>${esc(
    fmtDifference(row),
  )}</td><td>${verdict}</td></tr>`;
}

function headToHead(h) {
  const against = h.measured_against_champion_id ? mono(h.measured_against_champion_id) : "no champion";
  const rows = h.rows_evaluated === null || h.rows_evaluated === undefined ? "" : ` · ${esc(String(h.rows_evaluated))} held-out rows, both models`;
  return `<p class="pb-small">Against ${against}${rows}${
    h.improvement_pct === null || h.improvement_pct === undefined
      ? ""
      : ` · improvement on the deciding metric ${esc(Number(h.improvement_pct).toFixed(2))}%`
  }</p>
  ${h.stale ? `<div class="apierr" role="alert"><b>CHAMPION_CHANGED</b>The champion is now ${mono(h.current_champion_id)}; this comparison is stale and approving will be refused.</div>` : ""}
  <div class="tbl-wrap"><table class="pb-h2h"><thead><tr><th>Metric</th><th>Challenger</th><th>Champion</th><th>Difference</th><th></th></tr></thead>
  <tbody>${h.metrics.map(metricRow).join("")}</tbody></table></div>
  ${h.note ? `<p class="pb-small" role="note">${esc(h.note)}</p>` : ""}`;
}

function decisionsList(decisions) {
  if (!decisions || !decisions.length) return "";
  return `<ul class="pb-small">${decisions
    .map(
      (d) =>
        `<li>${esc(d.decision)} by ${mono(d.decided_by)} on ${esc(fmtStamp(d.decided_at))}${d.reason ? `: ${esc(d.reason)}` : ""}</li>`,
    )
    .join("")}</ul>`;
}

function decisionForm(item) {
  const id = item.version.model_id;
  const error = state.decideError && state.decideError.modelId === id ? errorBox(state.decideError.error) : "";
  if (!item.can_decide) {
    return `<div class="pb-why" role="note" data-blocked="${esc(id)}">${esc(item.blocked_reason || "You cannot decide on this model.")}</div>`;
  }
  const busy = state.deciding === id;
  return `<form class="pb-form wide pb-decide" data-model="${esc(id)}" novalidate autocomplete="off">
    <label class="pb-field field"><span class="sub">Reason (required)</span><textarea class="pb-input" name="reason" rows="2" maxlength="2000"></textarea></label>
    <div class="actions">${actionButton("POST", "/models/{model_id}/approve", {
      type: "submit",
      attrs: `data-decision="approve" data-model="${esc(id)}"`,
      label: busy ? "Working…" : "Approve",
      busy,
    })}${actionButton("POST", "/models/{model_id}/reject", {
      cls: "linkbtn",
      type: "submit",
      attrs: `data-decision="reject" data-model="${esc(id)}"`,
      label: "Reject",
      busy,
    })}</div>${error}
  </form>`;
}

function itemCard(item) {
  const v = item.version;
  return `<section class="card" data-approval="${esc(v.model_id)}"><h3>${esc(v.use_case_id)} · version ${esc(
    String(v.version),
  )} ${statusPill(v.status)}</h3><div class="form-body">
    <p class="pb-small">${mono(v.model_id)} · ${esc(v.model_display_name)} · trained by run ${mono(v.run_id)} · registered ${esc(
      fmtStamp(v.created_at),
    )} · started by ${item.trained_by ? mono(item.trained_by) : "(not recorded for this run)"}</p>
    ${headToHead(item.head_to_head)}
    ${decisionsList(item.decisions)}
    ${decisionForm(item)}
  </div></section>`;
}

function listCard() {
  if (state.loadError) return errorBox(state.loadError);
  if (!state.items) return `<p class="loading" style="padding:16px 20px">Loading…</p>`;
  const done = state.decided
    ? `<div class="pb-ok" role="status">${esc(state.decided.modelId)} ${esc(
        state.decided.decision === "approve" ? "approved: it is now the champion." : "rejected and archived.",
      )}</div>`
    : "";
  const note = state.separationEnforced
    ? ""
    : `<div class="pb-why" role="note">Sign-in is off: everyone acts as one local operator, so the rule that the person who trained a model cannot approve it is not enforced here.</div>`;
  if (!state.items.length) return `${done}${note}<section class="card"><p class="empty">No challenger is waiting for approval.</p></section>`;
  return `${done}${note}${state.items.map(itemCard).join("")}`;
}

export function approvalsHtml() {
  const head = pageHead(
    `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">Approvals</h1><p class="desc">Challengers that beat their champion and wait for an Approver. Both models were scored on the same held-out rows.</p>`,
  );
  const refused = reasonFor("GET", "/approvals");
  if (refused) return `<main class="screen">${head}${refusal(refused)}</main>`;
  return `<main class="screen">${head}<div class="stack">${listCard()}</div></main>`;
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
    state.deciding = modelId;
    state.decideError = null;
    repaint();
    try {
      const version =
        decision === "reject"
          ? await postReject(modelId, { reason })
          : await postApprove(modelId, { approved_by: signedInName(), reason });
      state.decided = { modelId, decision, version };
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
  Object.assign(state, {
    items: null,
    separationEnforced: true,
    loadError: null,
    deciding: null,
    decideError: null,
    decided: null,
  });
}
