// Step 4 of a use case, "Measure the campaign" (Plan H M83), on a finished scoring run's Results.
//
// WHAT THE PERSON SEES
//   * In the Results flow, a fourth block "Measure" (the router's run-action seam) that jumps to
//     the step below the flow; its state ("After the campaign", "Measured") sits under the label.
//     Neither the block nor the panel carries a number: the page's other steps (Guided setup's 1-3,
//     the unnumbered Data / Model / Output blocks) would not add up to it (UI_AUDIT §8.4 item 5).
//   * Below the flow, the step itself (the router's run-panel seam): one sentence, one upload button;
//     after the upload, one big plain result ("The campaign added about 180 conversions", "No clear
//     effect yet", "Outcome window not over yet"), the contacted and held-back response rates, the
//     statistics folded under "Details", and - when the campaign is large enough - "Learn who to
//     contact next time", which trains an uplift model on the server and links to its contact list.
//
// Which use cases have the step is `rule.js` (configuration, never an id). Every number and sentence
// comes from `GET/POST /runs/{id}/measure` (`api/routes/measure.py`), whose report is the existing
// campaign-results engine's; nothing here computes an effect. The panel keeps its state per run in
// this module and redraws itself in place, so a repaint of the whole screen (the Results view polls)
// shows the same state with no re-binding: every event is delegated from `document`.

import { API_BASE, ApiError, postUpload } from "../../api.js";
import { announceStatus, errorBox, esc, fmtInt, fmtPct, present } from "../../dom.js";
import { registerRunAction, registerRunPanel } from "../router.js";
import { campaignStep, measureApplies } from "./rule.js";
import { injectMeasureStyles } from "./styles.js";

export { campaignStep, measureApplies };

const POLL_MS = 4000;
const states = new Map();

// --- the three calls -----------------------------------------------------------------------------

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(API_BASE + path, options);
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (response.ok) return body;
  const detail = body && body.detail;
  const code = (detail && detail.code) || `HTTP_${response.status}`;
  const message = (detail && detail.message) || `The API answered ${response.status}.`;
  throw new ApiError(response.status, code, message, body);
}

const post = (payload) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});
const measurePath = (runId) => `/runs/${encodeURIComponent(runId)}/measure`;
export const getMeasure = (runId) => request(measurePath(runId));
export const postMeasure = (runId, payload) => request(measurePath(runId), post(payload));
export const postLearn = (runId) => request(`${measurePath(runId)}/learn`, post({}));

// --- state ---------------------------------------------------------------------------------------

function stateFor(uc, run) {
  let st = states.get(run.run_id);
  if (!st) {
    st = { uc, run, view: null, loading: true, error: null, busy: "", actionError: null, timer: null };
    states.set(run.run_id, st);
    load(st);
  }
  st.uc = uc;
  st.run = run;
  return st;
}

async function load(st) {
  try {
    st.view = await getMeasure(st.run.run_id);
    st.error = null;
  } catch (error) {
    st.error = error;
  }
  st.loading = false;
  repaint(st);
  schedulePoll(st);
}

/** While the uplift model is learning, ask again now and then - only while the step is on screen. */
function schedulePoll(st) {
  clearTimeout(st.timer);
  const learning = st.view && st.view.uplift_run && ["pending", "running"].includes(st.view.uplift_run.state);
  if (!learning) return;
  st.timer = setTimeout(() => {
    if (panelElement(st.run.run_id)) load(st);
  }, POLL_MS);
}

function panelElement(runId) {
  if (typeof document === "undefined") return null;
  return [...document.querySelectorAll("[data-measure-panel]")].find((el) => el.dataset.measurePanel === runId) || null;
}

function repaint(st) {
  const el = panelElement(st.run.run_id);
  if (el) el.outerHTML = panelHtml(st);
  const block = typeof document !== "undefined"
    ? [...document.querySelectorAll("[data-measure-jump]")].find((b) => b.dataset.measureJump === st.run.run_id)
    : null;
  if (block) block.outerHTML = blockHtml(st.uc, st.run);
}

// --- markup --------------------------------------------------------------------------------------

const routes = {
  run: (uc, run) => `#/uc/${encodeURIComponent(uc.id)}/run/${encodeURIComponent(run.run_id)}`,
  campaign: (uc, run) => `#/campaign/${encodeURIComponent(uc.id)}/${encodeURIComponent(run.run_id)}`,
  upliftRun: (uc, id) => `#/uplift/${encodeURIComponent(uc.id)}/run/${encodeURIComponent(id)}`,
  treatList: (uc, id) => `#/uplift/${encodeURIComponent(uc.id)}/output/${encodeURIComponent(id)}`,
};

/** The flow's fourth block: it says whether the campaign was measured, and jumps to the step. */
function blockHtml(uc, run) {
  const st = states.get(run.run_id);
  const measured = !!(st && st.view && st.view.report);
  const state = measured
    ? '<span class="bstate">Measured</span>'
    : '<span class="bstate waiting">After the campaign</span>';
  return `<a class="block" href="${esc(routes.run(uc, run))}" data-measure-jump="${esc(
    run.run_id,
  )}" data-action="measure"><div><div class="lab measure-lab"><span>Measure</span>${state}</div><div class="val">Did contacting them change anything?</div><div class="meta">${
    measured ? "See the result below" : "Upload who responded, below"
  }</div></div><div class="go"><span>Go to the step</span><span aria-hidden="true">↓</span></div></a>`;
}

function uploadButton(st, label, kind) {
  const busy = !!st.busy;
  return `<label class="btn ${kind} measure-upload${busy ? " is-busy" : ""}"><input type="file" class="measure-file" accept=".csv,.parquet,text/csv" data-measure-file="${esc(
    st.run.run_id,
  )}"${busy ? " disabled" : ""}><span>${esc(label)}</span></label>`;
}

const TONE = {
  added: "ok",
  prevented: "ok",
  harmed: "bad",
  no_clear_effect: "warn",
  not_enough: "warn",
  too_early: "warn",
  nothing_matched: "bad",
  no_control: "bad",
};

function ratesHtml(report) {
  if (!present(report.treated_rate) || !present(report.control_rate)) return "";
  const row = (label, rate, rows) =>
    `<li><span class="measure-arm">${esc(label)}</span><span class="measure-rate">${esc(fmtPct(rate))} responded</span><span class="muted">${esc(
      `${fmtInt(rows)} customers`,
    )}</span></li>`;
  return `<ul class="measure-rates">${row("Contacted", report.treated_rate, report.treated_rows)}${row(
    "Held back",
    report.control_rate,
    report.control_rows,
  )}</ul>`;
}

function learnHtml(st) {
  const { uc, view } = st;
  const learned = view.uplift_run;
  if (learned) {
    if (learned.state === "done") {
      return `<div class="measure-learn"><p>The model has learned which customers this campaign really changes.</p><a class="btn secondary" href="${esc(
        routes.treatList(uc, learned.run_id),
      )}" data-measure-treat>See who to contact next time ›</a></div>`;
    }
    if (learned.state === "failed" || learned.state === "cancelled") {
      return `<div class="measure-learn"><p>Learning who to contact did not finish. <a href="${esc(
        routes.upliftRun(uc, learned.run_id),
      )}">See why ›</a></p></div>`;
    }
    return `<div class="measure-learn"><p role="status">Learning who to contact next time… <a href="${esc(
      routes.upliftRun(uc, learned.run_id),
    )}">See progress ›</a></p></div>`;
  }
  if (view.learn && view.learn.ready) {
    return `<div class="measure-learn"><p>Use this campaign to find the customers contact really changes, so the next list skips the ones who would respond anyway.</p><button type="button" class="btn secondary" data-measure-learn="${esc(
      st.run.run_id,
    )}"${st.busy ? " disabled" : ""}>Learn who to contact next time</button></div>`;
  }
  return view.learn ? `<p class="muted measure-learn-no" data-measure-not-enough>${esc(view.learn.reason)}</p>` : "";
}

function detailsHtml(st) {
  const { uc, run, view } = st;
  const r = view.report;
  const outcomes = view.outcomes || {};
  const incremental = r.incremental_conversions;
  const pairs = [
    ["Outcomes file", outcomes.file_name],
    ["Outcome column", r.outcome_column],
    ["Counted over", present(r.outcome_window_days) ? `${fmtInt(r.outcome_window_days)} days after the list` : null],
    ["What counts", view.outcome_label],
    [
      "Extra outcomes, 95% range",
      incremental && present(incremental.ci_low) && present(incremental.ci_high)
        ? `${fmtInt(Math.round(incremental.value))} (${fmtInt(Math.round(incremental.ci_low))} to ${fmtInt(
            Math.round(incremental.ci_high),
          )})`
        : null,
    ],
    ["p-value", present(r.p_value) ? (r.p_value < 0.001 ? "below 0.001" : r.p_value.toFixed(3)) : null],
    ["Still inside the window", r.rows_immature ? `${fmtInt(r.rows_immature)} customers, not counted` : null],
    ["No outcome in the file", r.rows_without_outcome ? `${fmtInt(r.rows_without_outcome)} customers, not counted` : null],
  ].filter(([, value]) => present(value));
  return `<details class="tech measure-more"><summary>Details</summary><p class="measure-summary">${esc(
    r.summary,
  )}</p><dl class="measure-kv">${pairs
    .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`)
    .join("")}</dl><p><a href="${esc(routes.campaign(uc, run))}">Open the full campaign report ›</a></p></details>`;
}

function busyHtml(st) {
  const busy = st.busy ? `<p class="measure-busy" role="status">${esc(st.busy)}</p>` : "";
  const failed = st.actionError ? errorBox(st.actionError) : "";
  return busy + failed;
}

function askHtml(st) {
  const held = present(st.view.held_back)
    ? `the ${fmtInt(st.view.held_back)} customers held back`
    : "the customers held back";
  return `<p class="measure-lead">After the campaign, upload who responded to see what contacting them changed, compared with ${esc(
    held,
  )}.</p><p class="muted measure-hint">A CSV with the customer id and one column: 1 if they responded, 0 if not.</p><div class="btn-row">${uploadButton(
    st,
    "Upload outcomes",
    "secondary",
  )}</div>${busyHtml(st)}`;
}

function resultHtml(st) {
  const { view } = st;
  const verdict = view.verdict || { kind: "not_enough", headline: "", detail: "" };
  return `<div class="measure-result ${TONE[verdict.kind] || "warn"}" data-measure-verdict="${esc(
    verdict.kind,
  )}"><p class="measure-big">${esc(verdict.headline)}</p><p class="measure-sub">${esc(verdict.detail)}</p></div>${ratesHtml(
    view.report,
  )}${learnHtml(st)}${busyHtml(st)}${detailsHtml(st)}<div class="btn-row measure-again">${uploadButton(
    st,
    "Upload new outcomes",
    "quiet",
  )}</div>`;
}

function panelHtml(st) {
  const id = esc(st.run.run_id);
  const open = `<section class="card measure" id="measure-step" data-measure-panel="${id}" aria-labelledby="measure-h"`;
  if (st.view && !st.view.offered) return `${open} hidden></section>`;
  let body;
  if (!st.view && st.loading) body = `<p class="muted" role="status">Loading…</p>`;
  else if (!st.view) body = errorBox(st.error, { title: "This step could not be loaded." });
  else body = st.view.report ? resultHtml(st) : askHtml(st);
  return `${open}><h3 id="measure-h">Measure the campaign</h3>${body}</section>`;
}

// --- events (delegated once) -----------------------------------------------------------------------

async function measureFile(st, file) {
  st.busy = "Measuring the campaign…";
  st.actionError = null;
  repaint(st);
  try {
    // Outcomes are rows to join onto the scored list, not training data: profiled as a scoring file.
    const upload = await postUpload(file, st.uc.id, "score");
    st.view = await postMeasure(st.run.run_id, { upload_id: upload.upload_id });
    announceStatus(st.view.verdict ? st.view.verdict.headline : "Campaign measured.");
  } catch (error) {
    st.actionError = error;
  }
  st.busy = "";
  repaint(st);
}

async function learn(st) {
  st.busy = "Starting to learn who to contact…";
  st.actionError = null;
  repaint(st);
  try {
    await postLearn(st.run.run_id);
    st.view = await getMeasure(st.run.run_id);
    announceStatus("Learning who to contact next time.");
  } catch (error) {
    st.actionError = error;
  }
  st.busy = "";
  repaint(st);
  schedulePoll(st);
}

function bindOnce() {
  if (typeof document === "undefined" || bindOnce.done) return;
  bindOnce.done = true;
  document.addEventListener("change", (event) => {
    const input = event.target && event.target.closest ? event.target.closest("[data-measure-file]") : null;
    if (!input) return;
    const st = states.get(input.dataset.measureFile);
    const file = input.files && input.files[0];
    if (st && file && !st.busy) measureFile(st, file);
  });
  document.addEventListener("click", (event) => {
    const target = event.target && event.target.closest ? event.target : null;
    if (!target) return;
    const learnButton = target.closest("[data-measure-learn]");
    if (learnButton) {
      const st = states.get(learnButton.dataset.measureLearn);
      if (st && !st.busy) learn(st);
      return;
    }
    const jump = target.closest("[data-measure-jump]");
    if (jump) {
      event.preventDefault();
      const panel = panelElement(jump.dataset.measureJump);
      if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: "smooth", block: "start" });
      const focusable = panel && panel.querySelector("[data-measure-file], [data-measure-learn]");
      if (focusable && focusable.focus) focusable.focus({ preventScroll: true });
    }
  });
}

// --- registration --------------------------------------------------------------------------------

injectMeasureStyles();
bindOnce();

registerRunAction({
  name: "measure",
  applies: (uc, run) => measureApplies(uc, run),
  html: (uc, run) => {
    stateFor(uc, run);
    return `<div class="arrow" aria-hidden="true">→</div>${blockHtml(uc, run)}`;
  },
});

registerRunPanel({
  name: "measure",
  applies: (uc, run) => measureApplies(uc, run),
  html: (uc, run) => panelHtml(stateFor(uc, run)),
});

/** For tests: forget every run's state (a fresh page). */
export function resetMeasureState() {
  for (const st of states.values()) clearTimeout(st.timer);
  states.clear();
}
