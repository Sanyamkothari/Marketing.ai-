// Retention (Phase 4b M48.2, Admin): what is past its keep-until date, then deleting exactly that.
//
// The plan asks for "a dry-run mode that shows what would be deleted", and the API makes the real run
// provably the dry run (DEC-748): `GET /privacy/retention/plan` answers the plan with its `plan_hash`,
// and `POST /privacy/retention/apply` re-plans as of the same `planned_at` and deletes only when the
// hash still matches. So this screen never builds an apply request of its own - it sends back the
// `plan_id`, `planned_at` and `plan_hash` of the plan that is on screen, and nothing else. What the
// Admin reviewed is what is deleted, or nothing is (`409 RETENTION_PLAN_CHANGED`: a run started, a
// file arrived, or someone else applied it first). After a refusal the plan on screen is marked out
// of date and its delete button is withdrawn: the only way forward is a fresh check to review.
//
// The check is loaded when the tab opens (it is a plain read and deletes nothing), and "Check again"
// refreshes it. Deleting needs the "I have reviewed" box ticked, because the list can be long and the
// deletion cannot be undone; models and aggregate reports are kept by the job itself (`strip_samples`
// only removes the row samples inside a kept report), and the screen says which is which per item.
//
// v1: the answer comes first ("12 files are past their keep-until date"), with the setting's own
// meaning from `configs/pilot/help.yaml`; the plan id and hash are under Technical details.

import { EM_DASH, errorBox, esc, fmtDate, fmtInt, fmtStamp, glossarySetting, techDetails } from "../../dom.js";
import { getRetentionPlan, postRetentionApply } from "./api.js";
import { bindAuditLinks } from "./audit.js";
import { actionButton, auditReference, codeWords, fieldError, isLocal, mono, rowsTable, useCaseName } from "./controls.js";
import { privacyScreen } from "./privacy.js";

/** Items drawn at once; the counts above the table always cover the whole plan. */
export const ITEMS_SHOWN = 300;

const CATEGORY = {
  upload: "Uploaded file",
  source: "Source table",
  dataset: "Built dataset",
  run_row_level: "Customer rows from a run",
  run_samples: "Report with sample rows",
};

const ACTION = { delete: "Deleted", strip_samples: "Kept; its sample rows removed" };

const RETENTION_MEANING = "How long uploaded data and results are kept before they are deleted.";

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

const lastPart = (key) => String(key || "").split("/").filter(Boolean).pop() || key;

function itemsTable(items) {
  const shown = items.slice(0, ITEMS_SHOWN);
  const table = rowsTable(
    [
      { label: "What" },
      { label: "Created" },
      { label: "Kept for" },
      { label: "What happens" },
      { label: "File", more: true },
      { label: "Use case", more: true },
      { label: "Belongs to", more: true },
    ],
    shown.map((item) => ({
      attrs: `data-key="${esc(item.key)}"`,
      cells: [
        `${esc(CATEGORY[item.category] || item.category)}<div class="pb-small">${esc(lastPart(item.key))}</div>`,
        esc(fmtDate(item.created_at)),
        `${esc(fmtInt(item.retention_days))} days`,
        item.action === "delete"
          ? `<span class="pill bad" data-status="delete">${esc(ACTION.delete)}</span>`
          : `<span class="pill warn" data-status="strip_samples">${esc(ACTION.strip_samples)}</span>`,
        mono(item.key),
        esc(item.use_case_id ? useCaseName(item.use_case_id) : EM_DASH),
        mono(item.owner_id),
      ],
    })),
  );
  return `${table}${
    items.length > shown.length
      ? `<p class="pb-small pb-pad">…and ${esc(fmtInt(items.length - shown.length))} more files, all counted above and all deleted together.</p>`
      : ""
  }`;
}

function skippedList(skipped) {
  if (!skipped || !skipped.length) return "";
  return `<div class="card-body"><details class="tech"><summary>Not touched (${esc(fmtInt(skipped.length))})</summary>${rowsTable(
    [{ label: "What" }, { label: "Why" }, { label: "Belongs to", more: true }, { label: "Code", more: true }],
    skipped.map((s) => ({
      cells: [esc(CATEGORY[s.category] || s.category), codeWords(s.reason_code, "Kept for now"), mono(s.owner_id), mono(s.reason_code)],
    })),
  )}</details></div>`;
}

function applyBlock(items) {
  if (state.applied) return `<p class="pb-note pb-pad">This plan has been applied. Check again to see what is due now.</p>`;
  if (state.stale) {
    return `<p class="pb-note pb-pad">This plan is out of date. Check again, review the new list, then delete that one.</p>`;
  }
  if (!items.length) return "";
  const deletes = items.filter((item) => item.action === "delete").length;
  return `<div class="card-body"><form id="pb-retention-apply" class="pb-stack" novalidate>
    <label class="check"><input type="checkbox" name="confirm" id="pb-retention-confirm"> I have reviewed this list</label>
    <div class="pb-form-actions"><span class="reason">Only exactly this list is deleted; if anything changed since, nothing is.</span><span class="spacer"></span>${actionButton(
      "POST",
      "/privacy/retention/apply",
      {
        cls: "btn danger",
        type: "submit",
        attrs: 'id="pb-retention-apply-submit" data-needs-tick="pb-retention-confirm"',
        label: state.applying ? "Deleting…" : `Delete these ${fmtInt(deletes || items.length)} files`,
        busy: state.applying,
      },
    )}</div>
    ${isLocal(state.applyError) ? fieldError(state.applyError) : ""}
  </form></div>`;
}

function planCard() {
  const again = actionButton("GET", "/privacy/retention/plan", {
    cls: "btn quiet sm",
    attrs: 'id="pb-retention-replan"',
    label: state.planning ? "Checking…" : "Check again",
    busy: state.planning,
  });
  if (state.planError) {
    return `<section class="card"><h3>What is due</h3><div class="card-body">${errorBox(state.planError)}<div class="pb-row-actions">${again}</div></div></section>`;
  }
  if (!state.plan) return `<section class="card"><h3>What is due</h3><p class="loading pb-pad">Checking…</p></section>`;
  const { plan, plan_hash: hash } = state.plan;
  const items = plan.items || [];
  const deletes = items.filter((item) => item.action === "delete").length;
  const stripped = items.length - deletes;
  const meaning = (glossarySetting("governance.retention_days") || {}).meaning || RETENTION_MEANING;
  const answer = items.length
    ? `${fmtInt(items.length)} file${items.length === 1 ? " is" : "s are"} past ${items.length === 1 ? "its" : "their"} keep-until date.`
    : "Nothing is past its keep-until date.";
  const detail = items.length
    ? `${fmtInt(deletes)} will be deleted${stripped ? `; ${fmtInt(stripped)} report${stripped === 1 ? " is" : "s are"} kept with ${stripped === 1 ? "its" : "their"} sample rows removed` : ""}. Models and summary reports are always kept.`
    : "Models and summary reports are always kept.";
  const tech = techDetails([
    ["Checked", fmtStamp(plan.planned_at)],
    ["Plan", plan.plan_id],
    ["Plan hash", hash],
  ]);
  return `<section class="card"><h3>What is due <span class="sort-note">(checked ${esc(fmtStamp(plan.planned_at))})</span></h3>
    <div class="card-body"><p class="pb-lead">${esc(answer)}</p><p class="pb-desc">${esc(detail)} Each use case's retention setting decides the date: ${esc(
      meaning.charAt(0).toLowerCase() + meaning.slice(1),
    )}</p><div class="pb-row-actions">${again}</div>${tech}</div>
    ${items.length ? itemsTable(items) : ""}${skippedList(plan.skipped)}${applyBlock(items)}</section>`;
}

function resultCard() {
  if (state.applyError && !isLocal(state.applyError)) {
    return `<section class="card"><h3>Delete</h3><div class="card-body">${errorBox(state.applyError)}</div></section>`;
  }
  const r = state.applied;
  if (!r) return "";
  const result = r.result;
  const n = result.deleted.length;
  return `<section class="card"><h3>Deleted</h3><div class="card-body"><div class="pb-ok" role="status">Deleted ${esc(fmtInt(n))} file${
    n === 1 ? "" : "s"
  } and removed the sample rows from ${esc(fmtInt(result.stripped.length))} report${
    result.stripped.length === 1 ? "" : "s"
  }; ${esc(fmtInt(result.already_gone.length))} ${result.already_gone.length === 1 ? "was" : "were"} already gone. Done ${esc(
    fmtStamp(result.applied_at),
  )}.</div>${auditReference("privacy.retention.apply", r.plan_id, [["Registry rows deleted", result.registry_rows_deleted]])}</div></section>`;
}

export const retentionHtml = () =>
  privacyScreen(
    "retention",
    "Data past its keep-until date",
    () => `${resultCard()}${planCard()}`,
    "Uploaded files and customer rows are kept for a set time. This lists what is past it, so you can delete it.",
  );

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

/** The delete button waits for the review tick (the form's own check still refuses an unticked submit). */
function bindTick(main) {
  const sync = () => {
    const button = main.querySelector("#pb-retention-apply-submit");
    const box = main.querySelector("#pb-retention-confirm");
    if (button && !button.hasAttribute("data-pb-gate")) button.disabled = !(box && box.checked) || state.applying;
  };
  sync();
  main.addEventListener("change", (event) => {
    if (event.target && event.target.id === "pb-retention-confirm") sync();
  });
}

export function bindRetention(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindAuditLinks(main);
  bindTick(main);
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
