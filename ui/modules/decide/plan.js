// The "Plan the test" card on a campaign's page (Plan J M94, DEC-1304 (g), (k)).
//
// A registered plan is drawn from the server's own values only: the metric, the window, the analysis
// date, the share held back and the customers behind it, the detectable effect, the expected rate and
// the power the server computed for them (or its sentence saying why it could not), any warning, the
// version and the first characters of `plan_hash`. Before a plan exists, a person who may register one
// gets a form of their own decisions - what is measured, when it is read, the smallest effect worth
// finding, the rate expected without the campaign - and no number of ours until the server answers.
//
// Above the plan sits the preview slider (M93's planner, wired at M94's integration; DEC-1204, DEC-1304
// (k)): a range input over the indices of `GET /campaigns/{id}/plan-preview`'s `points` and nothing
// else - one stop per point the server computed, never an interpolated value - starting on the
// campaign's own split (`current_index`). Each stop reads out the share held back, the customers on
// each side, the smallest change that test is sure to see and what holding them back costs, or the
// planner's sentence saying why a figure is missing. The stop does not change the plan: the campaign's
// split is already drawn, and the plan's power is the server's for that split.
//
// Pure: `planCardHtml` and `previewReadoutHtml` draw from their arguments, so the jsdom tests can call
// them with real answers.

import { EM_DASH, esc, fmtDate, fmtInt, fmtMoney, fmtNum, fmtPct, glossaryCode, present } from "../../dom.js";

const row = (label, value, attrs = "") =>
  `<div class="dc-kv"${attrs ? ` ${attrs}` : ""}><span class="dc-k">${esc(label)}</span><span class="dc-v">${value}</span></div>`;

const UNDERPOWERED = "This test is smaller than the effect needs: it may miss an effect of that size.";

function warningText(code) {
  const entry = glossaryCode(code);
  if (entry && entry.title) return entry.title;
  return code === "PLAN_UNDERPOWERED" ? UNDERPOWERED : code;
}

/** The registered plan, as the server froze it. */
function planRows(plan) {
  const window = present(plan.outcome_window_days) ? `${fmtInt(plan.outcome_window_days)} days` : EM_DASH;
  const later = (plan.secondary_analysis_dates || []).map((d) => esc(fmtDate(d))).join(", ");
  const power = present(plan.achieved_power)
    ? esc(fmtPct(plan.achieved_power, 0))
    : esc(plan.power_note || EM_DASH);
  return [
    row("What is measured", esc(plan.metric)),
    row("Outcome column", `<span class="mono">${esc(plan.outcome_column)}</span>`),
    row("Outcome window", esc(window)),
    row("Analysis date", esc(fmtDate(plan.analysis_date)), "data-plan-date"),
    later ? row("Later readings", later) : "",
    row(
      "Held back",
      `${esc(fmtPct(plan.holdout_fraction, 1))} <span class="dc-note">(${esc(fmtInt(plan.n_holdout))} of ${esc(
        fmtInt(plan.population_rows),
      )} customers)</span>`,
      "data-plan-holdout",
    ),
    row("Detectable effect planned for", present(plan.mde_pp) ? `${esc(fmtNum(plan.mde_pp, 1))} points` : EM_DASH),
    row("Rate expected without the campaign", present(plan.base_rate) ? esc(fmtPct(plan.base_rate, 1)) : EM_DASH),
    row("Chance of finding that effect", power, "data-plan-power"),
    row(
      "Version",
      `${esc(fmtInt(plan.version))}${plan.amends ? ` <span class="dc-note">(amended: ${esc(plan.amendment_reason || "")})</span>` : ""}`,
    ),
    row("Fixed as", `<span class="mono" title="${esc(plan.plan_hash)}">${esc(String(plan.plan_hash).slice(0, 12))}</span>`, "data-plan-hash"),
  ].join("");
}

const money = (range) =>
  !range ? null : range.low === range.high ? fmtMoney(range.low) : `${fmtMoney(range.low)} to ${fmtMoney(range.high)}`;

/** What one computed point says; `preview` is the whole answer (for the campaign's own split). */
export function previewReadoutHtml(preview, index) {
  const point = preview && preview.points ? preview.points[index] : null;
  if (!point) return "";
  const own = index === preview.current_index ? ` <span class="dc-note">(this campaign's split)</span>` : "";
  // Plan J M102: a plan on an amount gives the change in the average amount per customer instead.
  const change = present(point.mde_pp)
    ? `${esc(fmtNum(point.mde_pp, 1))} points`
    : present(point.mde_amount) ? `${esc(fmtNum(point.mde_amount, 2))} per customer` : EM_DASH;
  const holding = money(point.cost_of_holdout);
  return [
    row("Held back", `${esc(fmtPct(point.holdout_share, 1))}${own}`, "data-preview-share"),
    row("Customers contacted / held back", `${esc(fmtInt(point.n_treat))} / ${esc(fmtInt(point.n_control))}`, "data-preview-arms"),
    row("Detectable effect at this split", change, "data-preview-mde"),
    row("Cost of holding them back", holding ? esc(holding) : EM_DASH, "data-preview-cost"),
    point.reason ? `<p class="dc-note" data-preview-reason>${esc(point.reason)}</p>` : "",
  ].join("");
}

/** The slider over the server's points: one stop per point, starting on `index`. */
export function previewHtml(preview, index) {
  if (!preview) return "";
  const points = preview.points || [];
  if (!points.length) {
    return `<div class="dc-preview" data-plan-preview><p class="dc-note" data-preview-reason>${esc(preview.reason || EM_DASH)}</p></div>`;
  }
  const at = Math.min(Math.max(0, Number(index) || 0), points.length - 1);
  return `<div class="dc-preview" data-plan-preview>
    <p class="dc-text">How small a change a test of the ${esc(fmtInt(preview.eligible))} customers measured can see, by the share held back.</p>
    <input type="range" min="0" max="${points.length - 1}" step="1" value="${at}" data-preview-slider aria-label="Share held back">
    <div class="dc-kvs" data-preview-readout>${previewReadoutHtml(preview, at)}</div>
    ${preview.basis ? `<p class="dc-note" data-preview-basis>${esc(preview.basis)}</p>` : ""}
  </div>`;
}

/** The form a person fills before a plan exists: their decisions, never a number of ours. */
function planForm(campaign) {
  const outcome = (campaign.outcomes && campaign.outcomes.outcome_column) || "";
  return `<form class="dc-form" data-plan-form>
    <label>What is measured<input name="metric" type="text" required maxlength="200" placeholder="for example: came back within 90 days"></label>
    <label>Outcome column<input name="outcome_column" type="text" required maxlength="200" value="${esc(outcome)}"></label>
    <label>Analysis date<input name="analysis_date" type="date" required></label>
    <label>Detectable effect to plan for, in points<input name="mde_pp" type="number" min="0.1" max="100" step="0.1"></label>
    <label>Rate expected without the campaign, in %<input name="base_rate_pct" type="number" min="0" max="100" step="0.1"></label>
    <label>Expectation, in your words<input name="expectation" type="text" maxlength="500"></label>
    <div class="dc-actions"><button type="submit" class="btn primary" data-plan-submit>Fix the plan</button></div>
  </form>`;
}

/** What the form says, as the body of `POST /campaigns/{id}/plan`. */
export function planBody(form) {
  const value = (name) => {
    const field = form.elements.namedItem(name);
    return field && field.value !== "" ? field.value : null;
  };
  const body = {
    metric: value("metric"),
    outcome_column: value("outcome_column"),
    analysis_date: value("analysis_date"),
    expectation: value("expectation") || "",
  };
  if (value("mde_pp") !== null) body.mde_pp = Number(value("mde_pp"));
  if (value("base_rate_pct") !== null) body.base_rate = Number(value("base_rate_pct")) / 100;
  return body;
}

/**
 * The card. `campaign` is the record, `plan` the plan in force (or null), `versions` every version,
 * `canRegister` whether this person may register one, `error` the last refusal, `preview` the answer of
 * `GET /campaigns/{id}/plan-preview` (or null) and `previewIndex` the slider's stop.
 */
export function planCardHtml({
  campaign,
  plan = null,
  versions = [],
  canRegister = false,
  error = null,
  preview = null,
  previewIndex = null,
} = {}) {
  let body;
  if (plan) {
    const warnings = (plan.warnings || [])
      .map((code) => `<p class="dc-warn" data-plan-warning="${esc(code)}">${esc(warningText(code))}</p>`)
      .join("");
    const history =
      versions.length > 1
        ? `<p class="dc-note" data-plan-versions>${esc(fmtInt(versions.length))} versions are kept; this is version ${esc(
            fmtInt(plan.version),
          )}.</p>`
        : "";
    body = `<div class="dc-kvs">${planRows(plan)}</div>${warnings}${history}`;
  } else if (canRegister) {
    body = `<p class="dc-text">Decide how this campaign will be judged before its outcomes are read. Once fixed, the plan changes only through an amendment with a reason.</p>${planForm(campaign)}`;
  } else {
    body = `<p class="dc-text" data-plan-none>No test plan has been registered for this campaign yet.</p>`;
  }
  const problem = error ? `<p class="dc-error" role="alert" data-plan-error>${esc(error.message || String(error))}</p>` : "";
  const start = present(previewIndex) ? previewIndex : preview && present(preview.current_index) ? preview.current_index : 0;
  return `<section class="card dc-card" data-plan-card><h3>Plan the test</h3>${previewHtml(preview, start)}${body}${problem}</section>`;
}
