// Campaigns (Phase 4b M49 outcomes): every scored customer list, and what really happened to the
// customers on it - how the campaign did against its control group, and how the model did compared
// with its test score.
//
// Two screens. `#/monitoring/runs` lists the scoring runs (Phase 1's `GET /runs?mode=score`), newest
// first, each with whether its results were added; `#/monitoring/runs/<run_id>` is one run: its
// outcomes upload, the report, the campaign effect and - when the consent ledger gated the run - who
// it left out and why.
//
// Why a screen of this module, not a card on Phase 1's Output page: that page is `ui/pages.js`, which
// this branch may not edit (PARALLEL_WORK_PROTOCOL.md §3). A scoring run's firing links here from the
// schedule history, and a performance-drop alert links here by its run id, so the report is one
// click from wherever a person learns about it.
//
// What each part shows, all of it the server's (DEC-771/772, DEC-785):
// * The upload is offered only on a finished scoring run - the only run the API measures - and is
//   Analyst work (DEC-780). The file is read in memory by the API and never stored; the screen says so,
//   because the file is customer ids paired with what happened to them.
// * Performance: the metric the model was chosen on, its test score beside the real-world score, the
//   relative drop against the use case's alert level, whether an alert was raised (with a link to the
//   Alerts screen), and how many rows matched. A metric the data cannot define (one class only, say)
//   shows the server's reason instead of a number, never a zero.
// * Campaign effect: contacted against the held-out control group, overall and by band, and the
//   observed difference Plan B's report starts from. Absent when the run held out no control group.
// * Consent: `GET /privacy/runs/{run_id}/consent-report` (Viewer, DEC-750); `404 CONSENT_REPORT_NOT_FOUND`
//   means the run was not gated, and the card is simply left out.
//
// v1: the list reads Use case / Scored on / Customers scored / Results, with the run id and client
// behind "Show more columns"; a card that has nothing to show yet is not drawn. The use case links to
// the run's Campaign results page (`#/campaign/<use case>/<run>`, ui/modules/uplift), and Results reads
// "Measured" when either screen measured the run: its campaign results exist (`GET
// /runs/{run_id}/campaign-results` answers 200, 404 before) or outcomes were added here.

import { getRun } from "../../api.js";
import {
  EM_DASH,
  errorBox,
  esc,
  fmtDate,
  fmtInt,
  fmtNum,
  fmtPct,
  fmtStamp,
  glossaryMetric,
  glossaryTerm,
  headActions,
  notFound,
  present,
  techDetails,
} from "../../dom.js";
import {
  getConsentReport,
  getIncrementalityInput,
  getOutcomes,
  getScoringRuns,
  hasCampaignResults,
  postOutcomes,
} from "./api.js";
import {
  actionButton,
  bindFileNames,
  fieldError,
  fieldValue,
  fileField,
  isLocal,
  mono,
  refusal,
  rowsTable,
  screenHead,
  statusPill,
  textField,
} from "./controls.js";
import { reasonFor } from "./session.js";
import { runHref } from "./schedules.js";

export const OUTCOME_FILE_TYPES = ".csv,.parquet,.pq";

/** How many of the newest finished runs are asked whether they were measured (two plain reads each). */
export const RESULTS_CHECKED = 20;

const state = {
  runs: null,
  runsError: null,
  measured: {}, // run id → true (campaign results or outcomes added) | false (neither yet)
  outcomesAdded: {}, // run id → true | false: outcomes added on this screen (`GET /runs/{id}/outcomes`)
  runId: null,
  run: null,
  runError: null,
  report: null,
  incrementality: null,
  consent: null,
  partsError: null,
  uploading: false,
  uploadError: null,
  uploaded: false,
};

/** The Campaign results page of a scoring run (ui/modules/uplift's `#/campaign/<use case>/<run>`). */
export const campaignHref = (useCaseId, runId) =>
  `#/campaign/${encodeURIComponent(useCaseId)}/${encodeURIComponent(runId)}`;

/**
 * Was this run measured? `{ outcomes, campaign }`, each true, false or `null` for "unknown" (a read
 * that failed): outcomes added here (`getOutcomes` is null until they are), and campaign results
 * measured on the Campaign results page (`hasCampaignResults`: 200 yes, 404 no).
 */
async function measuredOf(runId) {
  const [outcomes, campaign] = await Promise.all([
    getOutcomes(runId).then(Boolean, () => null),
    hasCampaignResults(runId).catch(() => null),
  ]);
  return { outcomes, campaign };
}

export async function loadRuns() {
  try {
    state.runs = (await getScoringRuns(100)).runs || [];
    state.runsError = null;
  } catch (error) {
    state.runsError = error;
    return;
  }
  const done = state.runs.filter((run) => run.state === "done").slice(0, RESULTS_CHECKED);
  const answers = await Promise.all(done.map((run) => measuredOf(run.run_id)));
  done.forEach((run, i) => {
    const { outcomes, campaign } = answers[i];
    if (outcomes !== null) state.outcomesAdded[run.run_id] = outcomes;
    if (outcomes === true || campaign === true) state.measured[run.run_id] = true;
    else if (outcomes === false && campaign === false) state.measured[run.run_id] = false;
  });
}

/** One run and everything measured about it. A report not written yet is `null`, not an error. */
export async function loadRunOutcomes(runId) {
  if (state.runId !== runId) {
    Object.assign(state, {
      runId,
      run: null,
      runError: null,
      report: null,
      incrementality: null,
      consent: null,
      partsError: null,
      uploadError: null,
      uploaded: false,
    });
  }
  try {
    state.run = (await getRun(runId)).run;
    state.runError = null;
  } catch (error) {
    state.runError = error;
    return;
  }
  try {
    const [report, incrementality, consent] = await Promise.all([
      getOutcomes(runId),
      getIncrementalityInput(runId),
      getConsentReport(runId),
    ]);
    Object.assign(state, { report, incrementality, consent, partsError: null });
  } catch (error) {
    state.partsError = error;
  }
}

/** Home › Campaigns › …, with no Model health tabs: a campaign is not a monitoring job. */
function campaignScreen(title, desc, readRoute, body, { crumb = null, actions = "" } = {}) {
  const trail = crumb ? [{ label: "Campaigns", href: "#/monitoring/runs" }] : [];
  const refused = reasonFor(readRoute[0], readRoute[1]);
  const head = screenHead({ trail, title, crumb, desc, actions: refused ? "" : actions });
  if (refused) return `<main class="screen pb-screen">${head}${refusal(refused)}</main>`;
  return `<main class="screen pb-screen">${head}<div class="stack">${body()}</div></main>`;
}

// --- the list -------------------------------------------------------------------------------------

function resultsPill(run) {
  if (run.state !== "done") return statusPill(run.state);
  const measured = state.measured[run.run_id];
  if (measured === true) return `<span class="pill ok" data-status="measured">Measured</span>`;
  if (measured === false) return `<span class="pill pb-pill-none" data-status="not_added">Not added yet</span>`;
  return EM_DASH;
}

function runRow(run) {
  const done = run.state === "done";
  const toAdd = state.outcomesAdded[run.run_id] === false;
  const action = done
    ? `<a class="btn ${toAdd ? "secondary" : "quiet"} sm" href="${runHref(run.run_id)}">${toAdd ? "Add outcomes" : "View results"}</a>`
    : EM_DASH;
  const name = esc(run.use_case_name || run.use_case_id);
  return {
    attrs: `data-run="${esc(run.run_id)}"`,
    cells: [
      done && run.use_case_id ? `<a href="${campaignHref(run.use_case_id, run.run_id)}" data-campaign>${name}</a>` : name,
      esc(fmtDate(run.finished_at || run.created_at)),
      present(run.row_count) ? esc(fmtInt(run.row_count)) : EM_DASH,
      resultsPill(run),
      action,
      mono(run.run_id),
      run.client_id ? mono(run.client_id) : EM_DASH,
    ],
  };
}

function runsCard() {
  const title = (n) => `<h3>Campaigns${n === null ? "" : ` · ${esc(n)}`} <span class="sort-note">(newest first)</span></h3>`;
  if (state.runsError) return `<section class="card">${title(null)}<div class="card-body">${errorBox(state.runsError, { retry: true })}</div></section>`;
  if (!state.runs) return `<section class="card">${title(null)}<p class="loading pb-pad">Loading…</p></section>`;
  if (!state.runs.length) {
    return `<section class="card">${title(0)}<div class="empty-state"><p class="es-t">No customers have been scored yet.</p><p>Score new customers with a trained model; the list of who to contact appears here, ready for you to add what happened.</p><a class="btn primary" href="#/">Go to use cases</a></div></section>`;
  }
  return `<section class="card">${title(state.runs.length)}${rowsTable(
    [
      { label: "Use case" },
      { label: "Scored on" },
      { label: "Customers scored", num: true },
      { label: "Results" },
      { label: "", sr: "Action" },
      { label: "Run", more: true },
      { label: "Client", more: true },
    ],
    state.runs.map(runRow),
    { cls: "pb-campaigns" },
  )}</section>`;
}

export const runsHtml = () =>
  campaignScreen(
    "Campaigns",
    "Every list of customers a model scored. Add what happened to them afterwards to see how the campaign and the model did.",
    ["GET", "/runs"],
    runsCard,
  );

// --- one run ------------------------------------------------------------------------------------

const score = (value) => (present(value) ? fmtNum(value, 3) : EM_DASH);

function uploadCard(run) {
  if (run.mode !== "score") {
    return `<section class="card"><h3>Add what happened to these customers</h3><p class="pb-note pb-pad">Results are added to scored customer lists; this run trained a model.</p></section>`;
  }
  if (run.state !== "done") {
    return `<section class="card"><h3>Add what happened to these customers</h3><p class="pb-note pb-pad">This run has not finished (${esc(
      run.state,
    )}); results can be added once it has.</p></section>`;
  }
  const localError = isLocal(state.uploadError) ? fieldError(state.uploadError) : "";
  return `<section class="card"><h3>${state.report ? "Replace what happened to these customers" : "Add what happened to these customers"}</h3><div class="card-body">
    ${state.report ? "" : `<p class="pb-note">No outcomes have been added for this run yet.</p>`}
    <form id="pb-outcomes" class="pb-stack" novalidate>
      <p class="pb-desc">Once the campaign's outcome window has passed, upload a CSV or Parquet file with each customer's ID and what happened. Only the totals below are kept; the file itself is not stored.${
        state.report ? " A new file replaces these results; the audit log keeps every upload's fingerprint." : ""
      }</p>
      ${fileField("file", "Choose the outcomes file", { accept: OUTCOME_FILE_TYPES, attrs: "required" })}
      <details class="adv"><summary>Advanced</summary><div class="pb-adv-body">${textField("outcome_column", "Outcome column", {
        hint: "Leave empty to use the use case's own outcome column.",
      })}</div></details>
      <div class="pb-form-actions">${actionButton("POST", "/runs/{run_id}/outcomes", {
        type: "submit",
        attrs: 'id="pb-outcomes-submit"',
        label: state.uploading ? "Measuring…" : "Upload and measure",
        busy: state.uploading,
      })}</div>
      ${localError}
    </form>
    ${state.uploadError && !localError ? errorBox(state.uploadError) : ""}
    ${state.uploaded ? `<div class="pb-ok" role="status">Measured. The results below are this upload's.</div>` : ""}
  </div></section>`;
}

const kpi = (label, value) => `<div class="kpi"><div class="l">${esc(label)}</div><div class="v">${esc(value)}</div></div>`;

function reportCard() {
  const r = state.report;
  if (!r) return "";
  const metric = glossaryMetric(r.metric);
  const drop = present(r.relative_drop_pct) ? `${fmtNum(r.relative_drop_pct, 1)}%` : EM_DASH;
  const alert = r.alert_raised
    ? `<div class="apierr" role="alert" data-code="PERFORMANCE_DROP"><b>The model did worse on real outcomes than in testing.</b><p class="apierr-fix">Its ${esc(
        r.metric_label,
      )} is ${esc(drop)} lower than its test score, beyond the ${esc(
        r.alert_threshold_pct,
      )}% alert level. Consider retraining it on recent data. <a href="#/monitoring/alerts">See the alert</a></p>${techDetails(
        [
          ["Code", "PERFORMANCE_DROP"],
          ["Alert", r.alert_id],
        ],
        "Details",
      )}</div>`
    : `<div class="pb-ok" role="status">Within the ${esc(r.alert_threshold_pct)}% alert level; no alert was raised.</div>`;
  const basis =
    r.metric_basis === "control_group"
      ? "Measured on the control group only, whom no campaign reached."
      : "Measured on every matched customer; the run held out no control group, so campaigns may have changed outcomes.";
  const rows = [
    ["Scored", r.rows_scored],
    ["In the file", r.rows_in_file],
    ["Matched", r.rows_matched],
    ["In the file, not scored", r.rows_unmatched_in_file],
    ["Measured on", r.rows_evaluated],
    ["Control group matched", r.control_rows_matched],
  ];
  const w = r.window || {};
  return `<section class="card"><h3>Performance on real outcomes · ${esc(r.metric_label)}</h3><div class="card-body">
    ${metric && metric.name ? `<p class="pb-note">${esc(r.metric_label)}: ${esc(metric.name)}.</p>` : ""}
    <div class="kpis pb-kpis">${kpi("Test score", score(r.test_score))}${kpi("Real-world score", score(r.real_world_score))}${kpi(
      "Drop since testing",
      drop,
    )}${kpi("Alert when the drop is over", `${r.alert_threshold_pct}%`)}</div>
    ${alert}${r.metric_undefined_reason ? `<p class="pb-small">No real-world score: ${esc(r.metric_undefined_reason)}</p>` : ""}
    <p class="pb-small">${esc(basis)}</p></div>
    ${rowsTable(
      rows.map(([label]) => ({ label, num: true })),
      [{ cells: rows.map(([, v]) => esc(fmtInt(v))) }],
    )}
    <div class="kv"><span class="k">Outcome window</span><span class="v">${esc(w.horizon_days)} days from ${esc(fmtDate(w.anchor))}, complete on ${esc(
      fmtDate(w.matures_at),
    )}</span></div>
    <div class="kv"><span class="k">Measured</span><span class="v">${esc(fmtStamp(r.computed_at))}</span></div>
    <div class="card-body">${techDetails([
      ["Outcome column", r.outcome_name],
      ["Decision threshold", present(r.decision_threshold) ? fmtNum(r.decision_threshold, 3) : null],
      ["Metric", r.metric],
      ["Model version", r.model_version_id],
    ])}</div>
  </section>`;
}

function groupCells(group, kind) {
  const value =
    kind === "binary"
      ? present(group.outcome_rate)
        ? fmtPct(group.outcome_rate)
        : EM_DASH
      : present(group.outcome_mean)
        ? fmtNum(group.outcome_mean, 3)
        : EM_DASH;
  return [esc(fmtInt(group.rows)), present(group.positives) ? esc(fmtInt(group.positives)) : EM_DASH, esc(value)];
}

function incrementalityCard() {
  const inc = state.incrementality;
  if (!inc) {
    if (!state.report) return ""; // nothing to say before any outcome is added
    return `<section class="card"><h3>Campaign effect (contacted vs control group)</h3><p class="pb-note pb-pad">This run held out no control group, so there is nothing to compare the campaign against.</p></section>`;
  }
  const measure = inc.outcome_kind === "binary" ? "Outcome rate" : "Average outcome";
  const diff = present(inc.observed_difference)
    ? inc.outcome_kind === "binary"
      ? `${fmtNum(inc.observed_difference * 100, 1)} points`
      : fmtNum(inc.observed_difference, 3)
    : EM_DASH;
  const rows = [
    { cells: ["Contacted", ...groupCells(inc.treated, inc.outcome_kind)] },
    { cells: ["Control group (not contacted)", ...groupCells(inc.control, inc.outcome_kind)] },
    ...(inc.by_band || []).flatMap((b) => [
      { cells: [`${esc(b.band)} · contacted`, ...groupCells(b.treated, inc.outcome_kind)] },
      { cells: [`${esc(b.band)} · control group`, ...groupCells(b.control, inc.outcome_kind)] },
    ]),
  ];
  const control = glossaryTerm("control group");
  return `<section class="card"><h3>Campaign effect (contacted vs control group)</h3><div class="card-body">
    ${control ? `<p class="pb-note">Control group: ${esc(control)}</p>` : ""}
    <div class="kpis pb-kpis">${kpi("Difference vs control group", diff)}${kpi("Control group", fmtPct(inc.control_group_fraction))}${kpi(
      "Left out (no contact allowed)",
      fmtInt(inc.suppressed_rows_excluded),
    )}${kpi("Scored, not in the file", fmtInt(inc.unmatched_scored_rows))}</div></div>
    ${rowsTable(
      [{ label: "Group" }, { label: "Customers", num: true }, { label: "With the outcome", num: true }, { label: measure, num: true }],
      rows,
    )}
    <p class="pb-small pb-pad">The difference as observed, before any statistical test. Written ${esc(fmtStamp(inc.created_at))}.</p></section>`;
}

function consentCard() {
  const c = state.consent;
  if (!c) return "";
  const purpose = String(c.purpose || "").replace(/_/g, " ");
  return `<section class="card"><h3>Consent: ${esc(purpose.charAt(0).toUpperCase() + purpose.slice(1))}</h3><div class="card-body">
    <div class="kpis pb-kpis">${kpi("Checked", fmtInt(c.principals_checked))}${kpi("Consent given", fmtInt(c.principals_with_valid_consent))}${kpi(
      "Left out",
      fmtInt(c.excluded_total),
    )}${kpi("No record · withdrawn · expired", `${fmtInt(c.excluded_no_consent)} · ${fmtInt(c.excluded_withdrawn)} · ${fmtInt(c.excluded_expired)}`)}</div>
    <p class="pb-small">Checked against the client's consent records as of ${esc(fmtStamp(c.ledger_as_of))}.</p>${techDetails([
      ["Client", c.client_id],
      ["Purpose", c.purpose],
    ])}</div></section>`;
}

function runFacts(run) {
  return `<section class="card"><h3>This list</h3>
    <div class="kv"><span class="k">Scored on</span><span class="v">${esc(fmtStamp(run.finished_at))}</span></div>
    <div class="kv"><span class="k">Customers scored</span><span class="v">${present(run.row_count) ? esc(fmtInt(run.row_count)) : EM_DASH}</span></div>
    <div class="kv"><span class="k">Status</span><span class="v">${statusPill(run.state)}</span></div>
    <div class="card-body">${techDetails([
      ["Run", run.run_id],
      ["Mode", run.mode],
      ["Client", run.client_id],
      ["Model version", run.model_version_id],
    ])}</div></section>`;
}

export function runOutcomesHtml(runId) {
  const run = state.run && state.runId === runId ? state.run : null;
  const name = run ? run.use_case_name || run.use_case_id : "Campaign";
  const title = run ? `${name} · scored ${fmtDate(run.finished_at || run.created_at)}` : "Campaign";
  const actions = run
    ? headActions({
        secondary: [
          {
            label: "See this run's results",
            href: `#/uc/${encodeURIComponent(run.use_case_id)}/output/${encodeURIComponent(run.run_id)}`,
          },
        ],
      })
    : "";
  return campaignScreen(
    title,
    "What really happened to the customers this run scored: how the campaign did against the control group, and how the model did.",
    ["GET", "/runs/{run_id}/outcomes"],
    () => {
      if (state.runError && state.runId === runId) {
        return Number(state.runError.status) === 404
          ? notFound("run", { href: "#/monitoring/runs", label: "Campaigns" }, state.runError)
          : errorBox(state.runError, { retry: true });
      }
      if (!run) return `<p class="loading">Loading…</p>`;
      return `${state.partsError ? errorBox(state.partsError, { retry: true }) : ""}${uploadCard(run)}${reportCard()}${incrementalityCard()}${consentCard()}${runFacts(run)}`;
    },
    { crumb: run ? name : "Campaign", actions },
  );
}

export function bindRunOutcomes(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
  bindFileNames(main);
  main.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form || form.id !== "pb-outcomes") return;
    event.preventDefault();
    if (state.uploading || !state.run) return;
    const input = form.elements.namedItem("file");
    const file = input && input.files && input.files[0];
    state.uploaded = false;
    if (!file) {
      state.uploadError = { code: "OUTCOME_FILE_REQUIRED", message: "Choose the outcomes file first." };
      repaint();
      return;
    }
    const runId = state.run.run_id;
    state.uploading = true;
    state.uploadError = null;
    repaint();
    try {
      state.report = await postOutcomes(runId, file, fieldValue(form, "outcome_column"));
      state.uploaded = true;
      state.measured[runId] = true;
      state.outcomesAdded[runId] = true;
      state.incrementality = await getIncrementalityInput(runId);
    } catch (error) {
      state.uploadError = error;
    }
    state.uploading = false;
    repaint();
  });
}

/** Test seam: forget screen state between cases. */
export function _resetOutcomesForTests() {
  Object.assign(state, {
    runs: null,
    runsError: null,
    measured: {},
    outcomesAdded: {},
    runId: null,
    run: null,
    runError: null,
    report: null,
    incrementality: null,
    consent: null,
    partsError: null,
    uploading: false,
    uploadError: null,
    uploaded: false,
  });
}
