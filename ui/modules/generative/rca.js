// The root-cause notes screen (RCA, prototypes 12-13): a finished churn run, then - on request -
// plain-language causes for each risk group.
//
// `RootCauseSummary` is written *over* a finished run, never inside it (DEC-211), so this screen has
// two honest states rather than one screen that guesses: before generation, the notes card is the
// only thing that says nothing has been written yet. After generation, every `RootCause` on screen
// carries the evidence-pack ids it was built from (rule 1: grounded or nothing), rendered as the
// reason or the complaint quote itself rather than a bare id, because "trust me" is not what an
// evidence reference is for. The prototype's "Deployment & monitoring" card (a reference cloud
// stack, marked as a sample on the mock itself) is not backed by `contracts.py`, so it is left out
// here rather than invented (plan §13.3: no value nobody measured).
//
// v1 UI: the churn model's own tiles and decile chart belong to the run's Output page, linked as "See
// the scores"; this screen no longer repeats them, so it reads only what the root-cause job writes.
// Cost and the guardrail totals sit in one closed "Cost and safety checks"; the run id under
// Technical details.

import { ApiError, getArtefacts, getRun } from "../../api.js";
import {
  emptyState,
  errorBox,
  esc,
  fmtInt,
  fmtNum,
  fmtStamp,
  headActions,
  journeyCrumb,
  pageHead,
  skeleton,
  techDetails,
} from "../../dom.js";
import { backendBadge, confidencePill, costAndChecks, evidenceChip, gTypeChip, guardrailCounts, progressList } from "./gdom.js";
import { postRootCause } from "./api.js";
import { readPath } from "../../settings.js";

const POLL_MS = 2000;
const STATE = new Map();

const ARTEFACTS = [
  "run_config.json",
  "root_cause_status.json",
  "root_cause_summary.json",
  "guardrail_report.json",
  "llm_usage.json",
];

function stateFor(runId) {
  if (!STATE.has(runId)) {
    STATE.set(runId, { runId, run: null, art: {}, generating: false, generateError: null });
  }
  return STATE.get(runId);
}

// --- root causes -----------------------------------------------------------------------------

function evidenceLookup(pack) {
  const map = new Map();
  for (const reason of pack.reasons) map.set(reason.id, reason);
  for (const complaint of pack.complaints) map.set(complaint.id, complaint);
  return map;
}

function causeHtml(cause, lookup) {
  const refs = cause.evidence_refs.map((id) => lookup.get(id)).filter(Boolean).map(evidenceChip).join("");
  return `<div class="gcause"><div class="gcause-h"><b>${esc(cause.cause)}</b>${confidencePill(cause.confidence)}</div>${
    refs ? `<div class="gcause-refs">${refs}</div>` : ""
  }</div>`;
}

function segmentCard(seg) {
  const pack = seg.evidence_pack;
  const stats = pack.stats;
  const complaintNote = pack.complaints.length
    ? `Built from ${fmtInt(pack.complaints.length)} complaint note${pack.complaints.length === 1 ? "" : "s"} across ${fmtInt(
        stats.rows,
      )} customers.`
    : `No complaint notes were available for this group, so the reasons rest on the model's drivers alone.`;
  if (!seg.summary) {
    return `<div class="gsegment"><div class="gsegment-h"><h4>${esc(seg.segment)}</h4><span class="tnum">${fmtInt(
      stats.rows,
    )} customers</span></div><div class="gsegment-body"><div class="empty">${esc(
      seg.blocked_reason || "No notes were stored for this group.",
    )}</div>${seg.guardrails.length ? `<div>${guardrailCounts(seg.guardrails)}</div>` : ""}</div></div>`;
  }
  const lookup = evidenceLookup(pack);
  return `<div class="gsegment"><div class="gsegment-h"><h4>${esc(seg.segment)}</h4><span class="tnum">${fmtInt(
    stats.rows,
  )} customers · ${fmtNum(stats.share_pct, 1)}% of those scored</span></div>
    <div class="gsegment-body">
      <p class="gheadline">${esc(seg.summary.headline)}</p>
      ${seg.summary.root_causes.map((c) => causeHtml(c, lookup)).join("")}
      ${
        seg.summary.recommended_actions.length
          ? `<div><span class="gactions-h">What to do</span><ul class="gactions-list">${seg.summary.recommended_actions
              .map((a) => `<li>${esc(a)}</li>`)
              .join("")}</ul></div>`
          : ""
      }
      ${seg.summary.caveats.length ? `<div class="gcaveat">${seg.summary.caveats.map(esc).join(" · ")}</div>` : ""}
      <div class="gseg-row"><span>${esc(complaintNote)}</span>${guardrailCounts(seg.guardrails)}</div>
    </div></div>`;
}

// Written out, not built by `emptyState`: the gate (`production/gate.js`) and its test find the id.
const generateAction = (s) =>
  `<button type="button" class="btn primary run" id="g-generate-rca"${s.generating ? " disabled" : ""}>${
    s.generating ? "Starting…" : "Generate root causes"
  }</button>`;

const generateErrorHtml = (s) => (s.generateError ? `<div class="gbody">${errorBox(s.generateError)}</div>` : "");

function rootCausesCard(s) {
  const rca = s.art["root_cause_summary.json"];
  const status = s.art["root_cause_status.json"];
  const running = status && (status.state === "pending" || status.state === "running");
  const details = costAndChecks(s.art["llm_usage.json"], s.art["guardrail_report.json"]);
  if (rca) {
    return `<section class="card"><h3>Reasons by risk group</h3><div class="gsegments">${rca.segments
      .map(segmentCard)
      .join("")}<p class="caption">Written from the model's top reasons and the complaint notes, ${esc(
      fmtStamp(rca.generated_at),
    )}. Read each group's caveats before acting on it.</p></div>${details}</section>`;
  }
  if (running) {
    return `<section class="card"><h3>Writing root-cause notes…</h3>${progressList(status)}</section>`;
  }
  if (status && status.state === "failed") {
    return `<section class="card"><h3>Root-cause notes</h3>${emptyState({
      title: "The last attempt did not finish.",
      text: status.error_message || "",
      action: generateAction(s),
    })}${generateErrorHtml(s)}${details}</section>`;
  }
  return `<section class="card"><h3>Root-cause notes</h3>${emptyState({
    title: "No root-cause notes have been written for this run yet.",
    text: "We take the top reasons behind each risk group's scores and the complaint notes behind them, and write the likely causes per group. Nothing is sent to anyone.",
    action: generateAction(s),
  })}${generateErrorHtml(s)}</section>`;
}

// --- shell -------------------------------------------------------------------------------------

function crumbTrail(uc) {
  return `<nav class="crumbs" aria-label="Breadcrumb">${journeyCrumb(uc)}<span class="sep" aria-hidden="true">›</span><a href="#/uc/${esc(
    uc.id,
  )}">${esc(uc.name)}</a><span class="sep" aria-hidden="true">›</span><span class="cur" aria-current="page">Root-cause notes</span></nav>`;
}

export function rcaHtml(uc, s) {
  const run = s.run;
  if (!run) return skeleton("page", { title: "Root-cause notes" });
  const config = s.art["run_config.json"] && s.art["run_config.json"].config;
  const llm = config && readPath(config, "generative.llm");
  const scores = `#/uc/${encodeURIComponent(uc.id)}/output/${encodeURIComponent(run.run_id)}`;
  return `<main class="screen gscreen t-${esc(uc.marker)}">
    ${pageHead(
      `${crumbTrail(uc)}<h1 class="h1">Root-cause notes</h1><p class="desc">Plain-language reasons why customers in each risk group may leave, and what to do about it, written from this run's scores and complaint notes.</p><div class="chips">${gTypeChip(
        uc,
      )}</div>${headActions({ related: { label: "See the scores", href: scores } })}`,
    )}
    ${backendBadge(llm)}
    <div class="stack">
      ${rootCausesCard(s)}
      ${techDetails([
        ["Run", run.run_id],
        ["Run created", fmtStamp(run.created_at)],
      ])}
    </div>
  </main>`;
}

// --- controller ----------------------------------------------------------------------------------

export function createRcaController(uc, runId, rerender) {
  const s = stateFor(runId);
  let timer = null;
  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
  };

  async function load() {
    const [detail, art] = await Promise.all([getRun(runId), getArtefacts(runId, ARTEFACTS)]);
    s.run = detail.run;
    s.art = art;
    const status = art["root_cause_status.json"];
    if (status && (status.state === "pending" || status.state === "running")) poll();
  }

  function poll() {
    stop();
    timer = setInterval(async () => {
      try {
        s.art = await getArtefacts(runId, ARTEFACTS);
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        return stop();
      }
      const status = s.art["root_cause_status.json"];
      if (status && (status.state === "pending" || status.state === "running")) return rerender();
      stop();
      rerender();
    }, POLL_MS);
  }

  async function generate() {
    s.generating = true;
    s.generateError = null;
    rerender();
    try {
      await postRootCause(runId);
      s.art = await getArtefacts(runId, ARTEFACTS);
      s.generating = false;
      rerender();
      poll();
    } catch (error) {
      s.generating = false;
      s.generateError = error;
      rerender();
    }
  }

  function bind(root) {
    const button = root.querySelector("#g-generate-rca");
    if (button) button.addEventListener("click", () => generate());
  }

  return { state: s, load, bind, stop };
}
