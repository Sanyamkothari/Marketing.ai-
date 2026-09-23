// The RCA (Root Cause Analysis) screen (prototypes 12-13): a finished churn run, then - on request -
// plain-language causes for each risk segment.
//
// `RootCauseSummary` is written *over* a finished run, never inside it (DEC-211), so this screen has
// two honest states rather than one screen that guesses: before generation, the Root causes card is
// the only thing that says nothing has been written yet, and every number around it still comes from
// the run's own `decile_lift.json` and `scoring_summary.json` - the churn model's output, not the
// LLM's. After generation, every `RootCause` on screen carries the evidence-pack ids it was built
// from (rule 1: grounded or nothing), rendered as the reason or the complaint quote itself rather
// than a bare id, because "trust me" is not what an evidence reference is for. The prototype's
// "Deployment & monitoring" card (SageMaker, Amazon SES, a retraining cadence) is Phase 4a's AWS
// reference stack, marked "Illustrative sample" on the mock itself - nothing in `contracts.py` backs
// it yet, so it is left out here rather than invented (plan §13.3: no value nobody measured).

import { ApiError, getArtefacts, getRun } from "../../api.js";
import {
  EM_DASH,
  columnBar,
  dash,
  errorBox,
  esc,
  fmtInt,
  fmtN,
  fmtNum,
  fmtStamp,
  journeyCrumb,
  pageHead,
  stageChip,
  typeChip,
} from "../../dom.js";
import { backendBadge, confidencePill, evidenceChip, guardrailCounts, usageLine } from "./gdom.js";
import { postRootCause } from "./api.js";
import { readPath } from "../../settings.js";

const POLL_MS = 2000;
const STATE = new Map();

const ARTEFACTS = [
  "decile_lift.json",
  "scoring_summary.json",
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

// --- churn context: what the model already measured, before any LLM ran -------------------------

function kpiTiles(s) {
  const summary = s.art["scoring_summary.json"];
  const rca = s.art["root_cause_summary.json"];
  const topSegment = rca && rca.segments[0];
  return `<div class="kpis">
    <div class="kpi"><div class="l">Rows scored</div><div class="v">${dash(
      summary && summary.rows_scored,
      fmtN,
    )}</div></div>
    <div class="kpi"><div class="l">Control group</div><div class="v">${dash(
      summary && summary.control_group_rows,
      fmtInt,
    )}</div></div>
    <div class="kpi"><div class="l">Segments</div><div class="v">${dash(
      rca && rca.segments.length,
      String,
    )}</div></div>
    <div class="kpi"><div class="l">Largest segment</div><div class="v">${
      topSegment ? esc(topSegment.segment) : EM_DASH
    }</div></div>
  </div>`;
}

function decileChart(s) {
  const lift = s.art["decile_lift.json"];
  const bins = (lift && lift.bins) || [];
  const values = (lift && lift.values) || [];
  if (!values.length) {
    return `<section class="card"><h3>Actual churn rate by risk decile</h3><div class="empty">This run has not produced decile_lift.json yet.</div></section>`;
  }
  const max = Math.max(...values);
  const chart = `<div class="vchart">${values
    .map((v, i) => {
      const bin = bins[i] || {};
      const top = i < 3;
      return `<div class="vcol ${top ? "top" : ""}"><span class="bv">${fmtNum(v, 2)}${esc(
        lift.unit,
      )}</span>${columnBar(max ? (160 * v) / max : 0, top, `${bin.label || ""} ${fmtNum(v, 2)}${lift.unit}`)}<span class="bl">${esc(
        bin.label || "",
      )}</span></div>`;
    })
    .join("")}</div>`;
  return `<section class="card"><h3>Actual churn rate by risk decile</h3>${chart}<p class="caption">${esc(
    lift.caption,
  )}</p></section>`;
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
  return `<div class="gcause"><div style="display:flex;justify-content:space-between;gap:10px;align-items:baseline">
    <b>${esc(cause.cause)}</b>${confidencePill(cause.confidence)}</div>
    <div style="margin-top:6px">${refs}</div></div>`;
}

function segmentCard(seg) {
  const pack = seg.evidence_pack;
  const stats = pack.stats;
  const complaintNote = pack.complaints.length
    ? `Built from ${fmtInt(pack.complaints.length)} complaint note${pack.complaints.length === 1 ? "" : "s"} across ${fmtInt(
        stats.rows,
      )} customers.`
    : `No complaint text was available for this segment; causes rest on the model's drivers alone.`;
  if (!seg.summary) {
    return `<div class="gsegment"><div class="gsegment-h"><h4>${esc(seg.segment)}</h4><span>${fmtInt(
      stats.rows,
    )} customers</span></div><div class="gsegment-body"><div class="empty">${esc(
      seg.blocked_reason || "No summary was stored for this segment.",
    )}</div>${
      seg.guardrails.length ? `<div>${guardrailCounts(seg.guardrails)}</div>` : ""
    }</div></div>`;
  }
  const lookup = evidenceLookup(pack);
  return `<div class="gsegment"><div class="gsegment-h"><h4>${esc(seg.segment)}</h4><span>${fmtInt(
    stats.rows,
  )} customers · ${fmtNum(stats.share_pct, 1)}% of scored rows</span></div>
    <div class="gsegment-body">
      <p style="margin:0;font-weight:600">${esc(seg.summary.headline)}</p>
      ${seg.summary.root_causes.map((c) => causeHtml(c, lookup)).join("")}
      ${
        seg.summary.recommended_actions.length
          ? `<div><b style="font-size:12px">What to do</b><ul class="gactions-list">${seg.summary.recommended_actions
              .map((a) => `<li>${esc(a)}</li>`)
              .join("")}</ul></div>`
          : ""
      }
      ${
        seg.summary.caveats.length
          ? `<div class="gcaveat">${seg.summary.caveats.map(esc).join(" · ")}</div>`
          : ""
      }
      <div class="gseg-row"><span>${esc(complaintNote)}</span>${guardrailCounts(seg.guardrails)}</div>
    </div></div>`;
}

function rootCausesCard(uc, s) {
  const rca = s.art["root_cause_summary.json"];
  const status = s.art["root_cause_status.json"];
  const running = status && (status.state === "pending" || status.state === "running");
  if (rca) {
    return `<section class="card"><h3>Root causes</h3><div style="padding:18px 20px 0">${rca.segments
      .map(segmentCard)
      .join("")}<p class="caption" style="padding:0 0 4px">Written from the model drivers and the complaint notes, ${fmtStamp(
      rca.generated_at,
    )}. Read each segment's caveats before acting on it.</p></div></section>`;
  }
  if (running) {
    const stages = status.stages || [];
    const rows = stages.length
      ? stages
          .map(
            (st, i) =>
              `<li class="${st.state === "running" ? "active" : st.state === "done" ? "done" : st.state === "failed" ? "failed" : ""}"><span class="dot">${
                i + 1
              }</span><div><div class="pt">${esc(st.title)}</div><div class="pd">${esc(st.detail)}</div></div></li>`,
          )
          .join("")
      : `<li class="active"><span class="dot">1</span><div><div class="pt">Starting…</div><div class="pd"></div></div></li>`;
    return `<section class="card"><h3>Root causes</h3><ol class="progress">${rows}</ol></section>`;
  }
  if (status && status.state === "failed") {
    return `<section class="card"><h3>Root causes</h3><div class="empty">${esc(
      status.error_message || "The last attempt failed.",
    )}</div><div style="padding:0 20px 20px">${generateButton(s)}</div></section>`;
  }
  return `<section class="card"><h3>Root causes</h3><div style="padding:18px 20px">
    <p class="desc" style="margin:0 0 14px">Root causes have not been written for this run yet.</p>
    <p class="desc" style="margin:0 0 14px;color:var(--muted)">We take the top drivers for each risk segment and the complaint notes behind them, and write causes per segment. Nothing is sent to anyone.</p>
    ${s.generateError ? errorBox(s.generateError) : ""}
    ${generateButton(s)}
  </div></section>`;
}

const generateButton = (s) =>
  `<button type="button" class="run" id="g-generate-rca"${s.generating ? " disabled" : ""}>${
    s.generating ? "Starting…" : "Generate root causes"
  }</button>`;

function usageCard(s) {
  const usage = s.art["llm_usage.json"];
  const guardrails = s.art["guardrail_report.json"];
  if (!usage && !guardrails) return "";
  return `<section class="card"><h3>Cost &amp; guardrails</h3><div style="padding:16px 20px">${usageLine(
    usage,
  )}${
    guardrails
      ? `<div style="margin-top:6px">${guardrailCounts(guardrails.checks)} <span style="font-size:12px;color:var(--muted)">across ${fmtInt(
          guardrails.summary.checked,
        )} generated text${guardrails.summary.checked === 1 ? "" : "s"}</span></div>`
      : ""
  }</div></section>`;
}

// --- shell -------------------------------------------------------------------------------------

export function rcaHtml(uc, s) {
  const run = s.run;
  const config = s.art["run_config.json"] && s.art["run_config.json"].config;
  const llm = config && readPath(config, "generative.llm");
  if (!run) return `<main class="screen t-${esc(uc.marker)}">${pageHead(`<h1 class="h1">${esc(uc.name)}</h1>`)}<p class="loading">Loading the run…</p></main>`;
  return `<main class="screen t-${esc(uc.marker)}">
    ${pageHead(
      `<nav class="crumbs" aria-label="Breadcrumb">${journeyCrumb(uc)}<span class="sep">›</span><a href="#/uc/${esc(
        uc.id,
      )}">${esc(uc.name)}</a><span class="sep">›</span><span class="cur">Root causes</span></nav>
      <span class="over" style="color:var(--c)">Root cause analysis</span><h1 class="h1">${esc(
        uc.pages.output,
      )}</h1><div class="chips">${stageChip(uc.lifecycle_stage)}${typeChip(uc)}</div>`,
    )}
    ${backendBadge(llm)}
    <p class="note" style="margin:-8px 0 16px">Scoring run ${esc(run.run_id)} · ${esc(fmtStamp(run.created_at))}</p>
    <div class="stack">
      ${kpiTiles(s)}
      ${decileChart(s)}
      ${rootCausesCard(uc, s)}
      ${usageCard(s)}
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
