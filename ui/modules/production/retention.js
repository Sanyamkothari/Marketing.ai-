// Retention (Phase 4b M48.2, Admin): the dry run, then the real run of exactly that dry run.
//
// The plan asks for "a dry-run mode that shows what would be deleted", and the API makes the real run
// provably the dry run (DEC-748): `GET /privacy/retention/plan` answers the plan with its `plan_hash`,
// and `POST /privacy/retention/apply` re-plans as of the same `planned_at` and deletes only when the
// hash still matches. So this screen never builds an apply request of its own - it sends back the
// `plan_id`, `planned_at` and `plan_hash` of the plan that is on screen, and nothing else. What the
// Admin reviewed is what is deleted, or nothing is (`409 RETENTION_PLAN_CHANGED`: a run started, a
// file arrived, or someone else applied it first). After a refusal the plan on screen is marked out
// of date and its apply button is withdrawn: the only way forward is a fresh dry run to review.
//
// The dry run is loaded when the tab opens (it is a plain read and deletes nothing), and "Plan again"
// refreshes it. Applying needs the "I have reviewed" box ticked, because the plan can be long and the
// deletion cannot be undone; models and aggregate reports are kept by the job itself (`strip_samples`
// only removes the row samples inside a kept report), and the screen says which is which per item.

import { EM_DASH, errorBox, esc, fmtInt, fmtStamp } from "../../dom.js";
import { getRetentionPlan, postRetentionApply } from "./api.js";
import { bindAuditLinks } from "./audit.js";
import { actionButton, auditReference, mono } from "./controls.js";
import { privacyScreen } from "./privacy.js";

/** Items drawn at once; the counts above the table always cover the whole plan. */
export const ITEMS_SHOWN = 300;

const CATEGORY = {
  upload: "Uploads",
  source: "Onboarding sources",
  dataset: "Built datasets",
  run_row_level: "Row-level run files",
};

const ACTION = { delete: "Delete", strip_samples: "Keep, remove row samples" };

const state = {
  plan: null, // RetentionPlanResponse
  planError: null,
  planning: false,
  stale: false,
  applying: false,
  applied: null, // RetentionApplyResponse
  applyError: null,
};

export async function loadPlan() {
  state.planning = true;
  try {
    state.plan = await getRetentionPlan();
    state.planError = null;
    state.stale = false;
  } catch (error) {
    state.planError = error;
  }
  state.planning = false;
}

function countsLine(response) {
  const items = response.plan.items || [];
  const deletes = items.filter((item) => item.action === "delete").length;
  const cards = Object.entries(response.counts || {}).map(
    ([category, count]) =>
      `<div class="kpi"><div class="l">${esc(CATEGORY[category] || category)}</div><div class="v">${esc(fmtInt(count))}</div></div>`,
  );
  cards.push(
    `<div class="kpi"><div class="l">Files deleted · kept and stripped</div><div class="v">${esc(fmtInt(deletes))} · ${esc(
      fmtInt(items.length - deletes),
    )}</div></div>`,
  );
  return `<div class="kpis" style="padding:0 20px 16px">${cards.join("")}</div>`;
}

function itemsTable(items) {
  const shown = items.slice(0, ITEMS_SHOWN);
  return `<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Kind</th><th>What happens</th><th>Belongs to</th><th>Use case</th><th>Created</th><th>Kept for</th></tr></thead><tbody>${shown
    .map(
      (item) => `<tr><td>${mono(item.key)}</td><td>${esc(CATEGORY[item.category] || item.category)}</td>
        <td>${item.action === "delete" ? `<span class="pill bad">${esc(ACTION.delete)}</span>` : `<span class="pill warn">${esc(ACTION.strip_samples)}</span>`}</td>
        <td>${mono(item.owner_id)}</td><td>${esc(item.use_case_id || EM_DASH)}</td><td>${esc(fmtStamp(item.created_at))}</td>
        <td>${esc(fmtInt(item.retention_days))} days</td></tr>`,
    )
    .join("")}</tbody></table></div>${
    items.length > shown.length
      ? `<p class="pb-small" style="padding:8px 20px">…and ${esc(fmtInt(items.length - shown.length))} more files, all counted above and all in the apply.</p>`
      : ""
  }`;
}

function skippedList(skipped) {
  if (!skipped || !skipped.length) return "";
  return `<h4>Not touched</h4><div class="tbl-wrap"><table><thead><tr><th>Belongs to</th><th>Kind</th><th>Why</th></tr></thead><tbody>${skipped
    .map((s) => `<tr><td>${mono(s.owner_id)}</td><td>${esc(CATEGORY[s.category] || s.category)}</td><td>${mono(s.reason_code)}</td></tr>`)
    .join("")}</tbody></table></div>`;
}

function applyBlock(items) {
  if (state.applied) return `<p class="pb-small" style="padding:0 20px 16px">This plan has been applied. Plan again to see what is due now.</p>`;
  if (state.stale) {
    return `<p class="pb-small" style="padding:0 20px 16px">This plan is out of date. Plan again, review the new list, then apply that one.</p>`;
  }
  if (!items.length) return "";
  return `<form id="pb-retention-apply" class="pb-form wide" novalidate style="padding:0 20px 18px">
    <label class="check"><input type="checkbox" name="confirm" id="pb-retention-confirm"> I have reviewed this list; delete these ${esc(
      fmtInt(items.length),
    )} files now</label>
    <div class="actions">${actionButton("POST", "/privacy/retention/apply", {
      type: "submit",
      attrs: 'id="pb-retention-apply-submit"',
      label: state.applying ? "Applying…" : "Apply this plan",
      busy: state.applying,
    })}<span class="reason">Only exactly this plan is applied; if anything changed since, nothing is deleted.</span></div>
  </form>`;
}

function planCard() {
  const again = actionButton("GET", "/privacy/retention/plan", {
    cls: "linkbtn",
    attrs: 'id="pb-retention-replan"',
    label: state.planning ? "Planning…" : "Plan again",
    busy: state.planning,
  });
  if (state.planError) return `<section class="card"><h3>Dry run</h3>${errorBox(state.planError)}<div style="padding:0 20px 16px">${again}</div></section>`;
  if (!state.plan) return `<section class="card"><h3>Dry run</h3><p class="loading" style="padding:16px 20px">Planning…</p></section>`;
  const { plan, plan_hash: hash } = state.plan;
  const items = plan.items || [];
  const meta = `<div class="kv"><span class="k">Planned</span><span class="v">${esc(fmtStamp(plan.planned_at))} · ${mono(
    plan.plan_id,
  )}</span></div><div class="kv"><span class="k">Plan hash</span><span class="v">${mono(hash)}</span></div>`;
  const body = items.length
    ? `${countsLine(state.plan)}${itemsTable(items)}`
    : `<p class="empty">Nothing is past its retention period. Models and aggregate reports are always kept.</p>`;
  return `<section class="card"><h3>Dry run · what would be deleted now</h3>${meta}
    <div class="pb-row-actions" style="padding:10px 20px">${again}</div>
    ${body}${skippedList(plan.skipped)}${applyBlock(items)}</section>`;
}

function resultCard() {
  if (state.applyError) return `<section class="card"><h3>Apply</h3><div style="padding:0 20px 16px">${errorBox(state.applyError)}</div></section>`;
  const r = state.applied;
  if (!r) return "";
  const result = r.result;
  return `<section class="card"><h3>Applied</h3><div style="padding:0 20px 16px"><div class="pb-ok" role="status">Deleted ${esc(
    fmtInt(result.deleted.length),
  )} file(s), removed the row samples from ${esc(fmtInt(result.stripped.length))}, ${esc(
    fmtInt(result.already_gone.length),
  )} already gone; ${esc(fmtInt(result.registry_rows_deleted))} registry row(s) deleted. Applied ${esc(
    fmtStamp(result.applied_at),
  )}.</div>${auditReference("privacy.retention.apply", r.plan_id)}</div></section>`;
}

export const retentionHtml = () => privacyScreen("retention", "Retention", () => `${resultCard()}${planCard()}`);

async function apply(form, repaint) {
  if (!form.elements.namedItem("confirm").checked) {
    state.applyError = { code: "CONFIRMATION_REQUIRED", message: "Tick the box to confirm you reviewed the list." };
    repaint();
    return;
  }
  const reviewed = state.plan;
  state.applying = true;
  state.applyError = null;
  state.applied = null;
  repaint();
  try {
    state.applied = await postRetentionApply(reviewed);
    state.stale = true; // applied: the same plan cannot be applied twice
  } catch (error) {
    state.applyError = error;
    if (error && error.code === "RETENTION_PLAN_CHANGED") state.stale = true;
  }
  state.applying = false;
  repaint();
}

export function bindRetention(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindAuditLinks(main);
  main.addEventListener("submit", (event) => {
    if (!event.target || event.target.id !== "pb-retention-apply") return;
    event.preventDefault();
    if (!state.applying && state.plan) apply(event.target, repaint);
  });
  main.addEventListener("click", async (event) => {
    if (!event.target || !event.target.closest || !event.target.closest("#pb-retention-replan") || state.planning) return;
    state.applied = null;
    state.applyError = null;
    state.plan = null;
    repaint();
    await loadPlan();
    repaint();
  });
}

/** Test seam: forget screen state between cases. */
export function _resetRetentionForTests() {
  Object.assign(state, {
    plan: null,
    planError: null,
    planning: false,
    stale: false,
    applying: false,
    applied: null,
    applyError: null,
  });
}
