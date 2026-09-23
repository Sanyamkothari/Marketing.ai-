// Outcomes of a scoring run (Phase 4b M49): what really happened to the people it scored, and how the
// model did on it compared with its test score.
//
// Two screens. `#/monitoring/runs` lists the scoring runs (Phase 1's `GET /runs?mode=score`), newest
// first; `#/monitoring/runs/<run_id>` is one run: its outcomes upload, the outcome report, the
// incrementality input and - when the consent ledger gated the run - who it left out and why.
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
// * Incrementality input: treated against the held-out control group, overall and by band, and the
//   observed difference Plan B's report starts from. Absent when the run held out no control group.
// * Consent: `GET /privacy/runs/{run_id}/consent-report` (Viewer, DEC-750); `404 CONSENT_REPORT_NOT_FOUND`
//   means the run was not gated, and the card is simply left out.

import { getRun } from "../../api.js";
import { EM_DASH, errorBox, esc, fmtInt, fmtNum, fmtPct, fmtStamp, present } from "../../dom.js";
import { getConsentReport, getIncrementalityInput, getOutcomes, getScoringRuns, postOutcomes } from "./api.js";
import { actionButton, fieldValue, mono, statusPill } from "./controls.js";
import { monitoringScreen, runHref } from "./schedules.js";

export const OUTCOME_FILE_TYPES = ".csv,.parquet,.pq";

const state = {
  runs: null,
  runsError: null,
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

export async function loadRuns() {
  try {
    state.runs = (await getScoringRuns(100)).runs || [];
    state.runsError = null;
  } catch (error) {
    state.runsError = error;
  }
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

// --- the run list -------------------------------------------------------------------------------

function runRow(run) {
  const done = run.state === "done";
  return `<tr><td>${done ? `<a class="pb-mono" href="${runHref(run.run_id)}">${esc(run.run_id)}</a>` : mono(run.run_id)}</td>
    <td>${esc(run.use_case_name || run.use_case_id)}</td><td>${run.client_id ? mono(run.client_id) : EM_DASH}</td>
    <td>${statusPill(run.state)}</td><td>${esc(fmtStamp(run.created_at))}</td><td>${esc(fmtStamp(run.finished_at))}</td>
    <td>${done ? `<a class="linkbtn" href="${runHref(run.run_id)}">Outcomes</a>` : EM_DASH}</td></tr>`;
}

function runsCard() {
  if (state.runsError) return `<section class="card"><h3>Scoring runs</h3>${errorBox(state.runsError)}</section>`;
  if (!state.runs) return `<section class="card"><h3>Scoring runs</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  const table = state.runs.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Run</th><th>Use case</th><th>Client</th><th>State</th><th>Started</th><th>Finished</th><th></th></tr></thead>
        <tbody>${state.runs.map(runRow).join("")}</tbody></table></div>`
    : `<p class="empty">No scoring run yet. Once a model scores new data, its outcomes can be added here when they are known.</p>`;
  return `<section class="card"><h3>Scoring runs · ${esc(state.runs.length)}, newest first</h3>${table}</section>`;
}

export const runsHtml = () =>
  monitoringScreen(
    "runs",
    "Outcomes",
    "Add what really happened to a finished scoring run's customers, and see how the model did on it.",
    ["GET", "/runs"],
    runsCard,
  );

// --- one run ------------------------------------------------------------------------------------

const score = (value) => (present(value) ? fmtNum(value, 3) : EM_DASH);

function uploadCard(run) {
  if (run.mode !== "score") {
    return `<section class="card"><h3>Add outcomes</h3><p class="empty">Outcomes are measured on scoring runs; this run trained a model.</p></section>`;
  }
  if (run.state !== "done") {
    return `<section class="card"><h3>Add outcomes</h3><p class="empty">This run has not finished (${esc(
      run.state,
    )}); outcomes can be added once it has.</p></section>`;
  }
  return `<section class="card"><h3>${state.report ? "Replace the outcomes" : "Add outcomes"}</h3><div class="form-body">
    <form id="pb-outcomes" class="pb-form wide" novalidate>
      <p class="fhint" style="margin:0">A CSV or Parquet file with the customer key and what happened, once the outcome window has passed. The API reads it in memory and keeps only the aggregates below; the file itself is not stored.${
        state.report ? " A new file replaces this report; the audit log keeps every upload's hash." : ""
      }</p>
      <div class="frow">
        <label class="pb-field field"><span class="sub">Outcomes file (.csv, .parquet)</span><input class="pb-input" type="file" name="file" accept="${OUTCOME_FILE_TYPES}" required></label>
        <label class="pb-field field"><span class="sub">Outcome column (optional)</span><input class="pb-input" name="outcome_column" autocomplete="off" spellcheck="false" placeholder="the use case's label when empty"></label>
      </div>
      <div class="actions">${actionButton("POST", "/runs/{run_id}/outcomes", {
        type: "submit",
        attrs: 'id="pb-outcomes-submit"',
        label: state.uploading ? "Measuring…" : "Upload and measure",
        busy: state.uploading,
      })}</div>
    </form>
    ${state.uploadError ? errorBox(state.uploadError) : ""}
    ${state.uploaded ? `<div class="pb-ok" role="status">Measured. The report below is this upload's.</div>` : ""}
  </div></section>`;
}

const kpi = (label, value) =>
  `<div class="kpi"><div class="l">${esc(label)}</div><div class="v">${esc(value)}</div></div>`;

function reportCard() {
  const r = state.report;
  if (!r) {
    return `<section class="card"><h3>Performance on real outcomes</h3><p class="empty">No outcomes have been added for this run yet.</p></section>`;
  }
  const drop = present(r.relative_drop_pct) ? `${fmtNum(r.relative_drop_pct, 1)}%` : EM_DASH;
  const alert = r.alert_raised
    ? `<div class="apierr" role="alert"><b>PERFORMANCE_DROP</b>The real-world ${esc(r.metric_label)} is ${esc(
        drop,
      )} worse than the test score, beyond the ${esc(r.alert_threshold_pct)}% alert level. An alert was raised: <a href="#/monitoring/alerts">see Alerts</a>.</div>`
    : `<div class="pb-ok" role="status">Within the ${esc(r.alert_threshold_pct)}% alert level; no alert was raised.</div>`;
  const basis =
    r.metric_basis === "control_group"
      ? "Measured on the held-out control group only, whom no campaign reached."
      : "Measured on every matched row; the run held out no control group, so campaigns may have changed outcomes.";
  const rows = [
    ["Scored", r.rows_scored],
    ["In the file", r.rows_in_file],
    ["Matched", r.rows_matched],
    ["In the file, not scored", r.rows_unmatched_in_file],
    ["Evaluated", r.rows_evaluated],
    ["Control rows matched", r.control_rows_matched],
  ];
  const w = r.window || {};
  return `<section class="card"><h3>Performance on real outcomes · ${esc(r.metric_label)}</h3>
    <div class="kpis" style="padding:0 20px 16px">${kpi("Test score", score(r.test_score))}${kpi(
      "Real-world score",
      score(r.real_world_score),
    )}${kpi("Worse than test by", drop)}${kpi("Alert level", `${r.alert_threshold_pct}%`)}</div>
    <div style="padding:0 20px 12px">${alert}${
      r.metric_undefined_reason
        ? `<p class="pb-small">No real-world score: ${esc(r.metric_undefined_reason)}</p>`
        : ""
    }<p class="pb-small">${esc(basis)}</p></div>
    <div class="tbl-wrap"><table><thead><tr>${rows.map(([l]) => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody><tr>${rows
      .map(([, v]) => `<td>${esc(fmtInt(v))}</td>`)
      .join("")}</tr></tbody></table></div>
    <div class="kv"><span class="k">Outcome</span><span class="v">${mono(r.outcome_name)}${
      present(r.decision_threshold) ? ` · decision threshold ${esc(fmtNum(r.decision_threshold, 3))}` : ""
    }</span></div>
    <div class="kv"><span class="k">Window</span><span class="v">${esc(w.horizon_days)} days from ${esc(fmtStamp(w.anchor))}, matured ${esc(
      fmtStamp(w.matures_at),
    )}</span></div>
    <div class="kv"><span class="k">Model version</span><span class="v">${mono(r.model_version_id)}</span></div>
    <div class="kv"><span class="k">Measured</span><span class="v">${esc(fmtStamp(r.computed_at))}</span></div>
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
  return `<td>${esc(fmtInt(group.rows))}</td><td>${present(group.positives) ? esc(fmtInt(group.positives)) : EM_DASH}</td><td>${esc(value)}</td>`;
}

function incrementalityCard() {
  const inc = state.incrementality;
  if (!inc) {
    return `<section class="card"><h3>Incrementality input</h3><p class="empty">${
      state.report
        ? "This run held out no control group, so there is nothing to compare the campaign against."
        : "Written once outcomes are added, when the run held out a control group."
    }</p></section>`;
  }
  const measure = inc.outcome_kind === "binary" ? "Outcome rate" : "Mean outcome";
  const diff = present(inc.observed_difference)
    ? inc.outcome_kind === "binary"
      ? `${fmtNum(inc.observed_difference * 100, 2)} points`
      : fmtNum(inc.observed_difference, 3)
    : EM_DASH;
  const head = `<thead><tr><th>Group</th><th>Rows</th><th>Positives</th><th>${esc(measure)}</th></tr></thead>`;
  const bands = (inc.by_band || [])
    .map(
      (b) =>
        `<tr><td>${esc(b.band)} · treated</td>${groupCells(b.treated, inc.outcome_kind)}</tr><tr><td>${esc(
          b.band,
        )} · control</td>${groupCells(b.control, inc.outcome_kind)}</tr>`,
    )
    .join("");
  return `<section class="card"><h3>Incrementality input</h3>
    <div class="kpis" style="padding:0 20px 16px">${kpi("Treated minus control", diff)}${kpi(
      "Control group",
      fmtPct(inc.control_group_fraction),
    )}${kpi("Suppressed, left out", fmtInt(inc.suppressed_rows_excluded))}${kpi("Scored, not in the file", fmtInt(inc.unmatched_scored_rows))}</div>
    <div class="tbl-wrap"><table>${head}<tbody><tr><td>Treated</td>${groupCells(inc.treated, inc.outcome_kind)}</tr><tr><td>Control</td>${groupCells(
      inc.control,
      inc.outcome_kind,
    )}</tr>${bands}</tbody></table></div>
    <p class="pb-small" style="padding:8px 20px 16px">The observed difference, before any statistical test: what Plan B's incrementality report starts from. Written ${esc(
      fmtStamp(inc.created_at),
    )}.</p></section>`;
}

function consentCard() {
  const c = state.consent;
  if (!c) return "";
  return `<section class="card"><h3>Consent · ${mono(c.purpose)}</h3>
    <div class="kpis" style="padding:0 20px 16px">${kpi("Checked", fmtInt(c.principals_checked))}${kpi(
      "Valid consent",
      fmtInt(c.principals_with_valid_consent),
    )}${kpi("Left out", fmtInt(c.excluded_total))}${kpi("No record · withdrawn · expired", `${fmtInt(c.excluded_no_consent)} · ${fmtInt(
      c.excluded_withdrawn,
    )} · ${fmtInt(c.excluded_expired)}`)}</div>
    <p class="pb-small" style="padding:0 20px 16px">Scored against client ${mono(c.client_id)}'s consent ledger as of ${esc(
      fmtStamp(c.ledger_as_of),
    )}.</p></section>`;
}

function runSummary(run) {
  return `<section class="card"><h3>${esc(run.use_case_name || run.use_case_id)} · ${mono(run.run_id)}</h3>
    <div class="kv"><span class="k">State</span><span class="v">${statusPill(run.state)} ${esc(run.mode)}</span></div>
    <div class="kv"><span class="k">Finished</span><span class="v">${esc(fmtStamp(run.finished_at))}</span></div>
    <div class="kv"><span class="k">Client</span><span class="v">${run.client_id ? mono(run.client_id) : EM_DASH}</span></div>
    <div class="kv"><span class="k">Results</span><span class="v"><a href="#/uc/${encodeURIComponent(run.use_case_id)}/output/${encodeURIComponent(
      run.run_id,
    )}">Open the Output page</a></span></div></section>`;
}

export function runOutcomesHtml(runId) {
  return monitoringScreen(
    "runs",
    "Outcomes",
    "What really happened to the people this run scored, and how the model did on it.",
    ["GET", "/runs/{run_id}/outcomes"],
    () => {
      const back = `<p><a class="linkbtn" href="#/monitoring/runs">‹ All scoring runs</a></p>`;
      if (state.runError) return `${back}${errorBox(state.runError)}`;
      if (!state.run || state.runId !== runId) return `${back}<p class="loading">Loading…</p>`;
      return `${back}${runSummary(state.run)}${state.partsError ? errorBox(state.partsError) : ""}${uploadCard(
        state.run,
      )}${reportCard()}${incrementalityCard()}${consentCard()}`;
    },
  );
}

export function bindRunOutcomes(root, repaint) {
  const main = root.querySelector("main");
  if (!main) return;
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
