// Every uplift screen as a pure function: state and artefacts in, an HTML string out.
//
// Nothing in this file calls the API, touches `window` or keeps state, so each screen can be
// rendered from a fixture in node (tests/unit/uplift/uplift_ui.test.mjs) exactly as it renders in
// the browser. The controllers in `controller.js` own the fetching; this file owns what a response
// looks like on screen.
//
// Two rules run through every function here:
//  * a number is shown only when an artefact carries it - anything else is `EM_DASH`, never a
//    sample figure (plan §13.3); and
//  * any artefact with `causal: false` puts the not-causal banner at the top of the screen, because
//    a number from a non-random assignment describes who was contacted, not what contact changed.

import {
  EM_DASH,
  dash,
  errorBox,
  esc,
  fmtInt,
  fmtN,
  fmtNum,
  fmtSize,
  fmtStamp,
  kpis,
  kvs,
  pageHead,
  present,
  stageChip,
  table,
  typeChip,
} from "../../dom.js";
import { decileChart, qiniChart, segmentChart } from "./charts.js";
import { fmtCi, fmtCount, fmtDay, fmtInterval, fmtP, fmtPts, fmtRate, fmtVal, signed } from "./format.js";

/** The one-line explanation plan B §8 gives the problem type, shown wherever it is chosen. */
export const UPLIFT_EXPLANATION = "predicts who changes behaviour because of your action";

/** `engine.uplift.contracts.NOT_CAUSAL_NOTE`, the label every output of a non-random run carries. */
export const NOT_CAUSAL_NOTE =
  "Not causal: the treatment was not randomly assigned, so these numbers describe who was " +
  "contacted, not what contacting them changed.";

/** The acknowledgement token of the one uplift check a user may accept (plan B §4). */
export const NOT_RANDOM = "TREATMENT_NOT_RANDOM";

/** What each screen reads from `GET /runs/{id}/uplift/{name}`. */
export const MODEL_ARTEFACTS = [
  "uplift_validation.json",
  "uplift_evaluation.json",
  "qini_curve.json",
  "segments.json",
  "policy_recommendation.json",
  "ope_report.json",
];
export const OUTPUT_ARTEFACTS = ["uplift_validation.json", "segments.json", "policy_recommendation.json"];

const LEARNER_LABEL = { s_learner: "S-learner", t_learner: "T-learner", x_learner: "X-learner" };
const BASE_MODEL_LABEL = { lightgbm: "LightGBM", autogluon_fast: "AutoGluon (fast preset)" };

const STOP_REASON = {
  all_persuadables: "Every eligible persuadable fits within the budget.",
  budget: "The contact budget is reached.",
  value_below_cost: "The next customer's expected value is below the cost of contacting them.",
  no_persuadables: "No customer is predicted to be persuadable.",
};

const STATE_LABEL = {
  pending: "Queued",
  running: "Running…",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

const humanise = (value) => {
  const text = String(value).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

const labelOf = (map, value) => (present(value) ? map[value] || humanise(value) : EM_DASH);

// --- routes ------------------------------------------------------------------------------------

export const routes = {
  index: () => "#/uplift",
  setup: (ucId) => `#/uplift/${encodeURIComponent(ucId)}`,
  run: (ucId, runId) => `#/uplift/${encodeURIComponent(ucId)}/run/${encodeURIComponent(runId)}`,
  model: (ucId, runId) => `#/uplift/${encodeURIComponent(ucId)}/model/${encodeURIComponent(runId)}`,
  output: (ucId, runId) => `#/uplift/${encodeURIComponent(ucId)}/output/${encodeURIComponent(runId)}`,
  campaign: (ucId, runId) => `#/campaign/${encodeURIComponent(ucId)}/${encodeURIComponent(runId)}`,
  data: (ucId, runId) => `#/uc/${encodeURIComponent(ucId)}/data/${encodeURIComponent(runId)}`,
  useCase: (ucId) => `#/uc/${encodeURIComponent(ucId)}`,
};

// --- shared pieces -----------------------------------------------------------------------------

/**
 * The not-causal banner, when any of the given artefacts says `causal: false`. `null` artefacts
 * are skipped: a report the run did not produce says nothing either way.
 */
export function notCausalBanner(...artefacts) {
  const flagged = artefacts.some((a) => a && a.causal === false);
  if (!flagged) return "";
  return `<div class="unotcausal" role="note"><b>Not causal</b><span>${esc(
    NOT_CAUSAL_NOTE.replace(/^Not causal: /, ""),
  )} TREATMENT_NOT_RANDOM was acknowledged for this run.</span></div>`;
}

const upliftChip = () => `<span class="chip type">Uplift</span>`;

/** A screen: the `data-module` marker is how `index.js` knows the page on screen is its own. */
const screenOf = (uc, head, body) =>
  `<main class="screen t-${esc((uc && uc.marker) || "P")}" data-module="uplift">${pageHead(head)}${body}</main>`;

function crumbs(uc, current) {
  return `<nav class="crumbs" aria-label="Breadcrumb"><a href="#/">Customer Lifecycle</a><span class="sep">›</span><a href="${esc(
    routes.useCase(uc.id),
  )}">${esc(uc.name)}</a><span class="sep">›</span><a href="${esc(
    routes.setup(uc.id),
  )}">Uplift</a><span class="sep">›</span><span class="cur">${esc(current)}</span></nav>`;
}

// --- index: every use case, one link each ------------------------------------------------------

/** `#/uplift`: the module's own entry page, drawn from `GET /industries`. */
export function upliftIndexHtml(payload) {
  const industry = ((payload && payload.industries) || [])[0];
  const links = industry
    ? (industry.stages || []).flatMap((stage) =>
        (stage.use_cases || []).map(
          (u) =>
            `<a href="${esc(routes.setup(u.id))}"><span>${esc(u.name)}</span><span class="s">${esc(
              stage.name,
            )} ›</span></a>`,
        ),
      )
    : [];
  return `<main class="screen" data-module="uplift">${pageHead(
    `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">Uplift modelling</h1><p class="desc">Uplift ${esc(
      UPLIFT_EXPLANATION,
    )}: it compares customers who were treated with a randomly held-out control group, so you contact the persuadable and leave alone the ones who would convert anyway, would never convert, or react badly.</p>`,
  )}${
    links.length
      ? `<div class="uindex">${links.join("")}</div>`
      : `<p class="hint">No industry template is configured.</p>`
  }<p class="hint">Pick a use case to train an uplift model on a campaign with a random control group.</p></main>`;
}

// --- Setup -------------------------------------------------------------------------------------

const profileOf = (s) => (s.upload ? s.upload.profile : null);

/** Model versions an uplift run registered: the only metric they are ranked on is AUUC. */
export const upliftVersions = (models) => (models || []).filter((m) => m.version && m.version.metric === "auuc");

/** Why the Run button is disabled, or `""` when it is not. */
export function setupBlocker(s) {
  if (!s.upload) return "Upload a dataset to continue";
  if (!s.pk) return "Choose the primary key column";
  if (s.mode === "train") {
    if (!s.target) return "Choose the outcome column";
    if (!s.treatment) return "Choose the treatment column";
    if (s.treatment === s.target) return "The treatment and the outcome must be different columns";
  } else if (!s.modelVersionId) {
    return "Train an uplift model first";
  }
  return "";
}

const option = (value, label, current) =>
  `<option value="${esc(value)}"${String(value) === String(current) ? " selected" : ""}>${esc(label)}</option>`;

function treatmentOptions(s, names) {
  const candidates = (s.candidates && s.candidates.candidates) || [];
  const listed = new Set(candidates.map((c) => c.column));
  const rest = names.filter((n) => !listed.has(n) && n !== s.pk && n !== s.target);
  const first = candidates
    .map((c) =>
      option(
        c.column,
        `${c.column} · ${present(c.treated_share) ? `${fmtNum(c.treated_share * 100, 1)}% treated` : EM_DASH}${
          c.hinted ? " · named like a treatment" : ""
        }`,
        s.treatment,
      ),
    )
    .join("");
  return `<option value="">Select a column…</option>${
    first ? `<optgroup label="0/1 columns">${first}</optgroup>` : ""
  }${rest.length ? `<optgroup label="Other columns">${rest.map((n) => option(n, n, s.treatment)).join("")}</optgroup>` : ""}`;
}

/** Every check of both 409 reports, uplift checks first, each with its acknowledge control. */
export function validationHtml(s) {
  const uplift = (s.upliftValidation && s.upliftValidation.checks) || [];
  const phase1 = (s.validation && s.validation.checks) || [];
  if (!uplift.length && !phase1.length) return "";
  const item = (check, token) => {
    const severity = check.severity === "error" ? "bad" : check.severity === "warning" ? "warn" : "ok";
    const on = token && s.acknowledged.includes(token);
    const control =
      check.acknowledgeable && token
        ? `<div class="vfix"><label><input type="checkbox" data-uack="${esc(token)}"${
            on ? " checked" : ""
          }> I confirm this is expected — run anyway${
            token === NOT_RANDOM ? " (every result will be labelled not causal)" : ""
          }</label></div>`
        : "";
    return `<div class="vitem"><span class="pill ${severity}">${esc(check.code)}</span><div>
      <div class="vmsg">${esc(check.message)}</div>
      ${check.suggestion ? `<div class="vsug">${esc(check.suggestion)}</div>` : ""}
      ${control}</div></div>`;
  };
  const upliftTokenOf = (c) => (c.details && c.details.acknowledge) || c.code;
  const phase1TokenOf = (c) => (c.details && c.details.acknowledge) || null;
  const blocking = [
    ...uplift.filter((c) => c.severity === "error" && !c.acknowledged && !s.acknowledged.includes(upliftTokenOf(c))),
    ...phase1.filter(
      (c) => c.severity === "error" && !c.acknowledged && !s.acknowledged.includes(phase1TokenOf(c)),
    ),
  ].length;
  const warnings = [...uplift, ...phase1].filter((c) => c.severity === "warning").length;
  const head = `${blocking} ${blocking === 1 ? "problem" : "problems"} must be fixed or acknowledged before this data can be used${
    warnings ? `, and ${warnings} warning${warnings === 1 ? "" : "s"} were found` : ""
  }.`;
  const auc =
    s.upliftValidation && present(s.upliftValidation.randomness_auc)
      ? `<div class="vitem"><span class="pill ok">RANDOMNESS</span><div><div class="vsug">A classifier predicting treatment from the features reached AUC ${esc(
          fmtNum(s.upliftValidation.randomness_auc, 3),
        )} (0.5 is a coin toss).</div></div></div>`
      : "";
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${uplift
    .map((c) => item(c, upliftTokenOf(c)))
    .join("")}${phase1.map((c) => item(c, phase1TokenOf(c))).join("")}${auc}</div>`;
}

function upliftRunsCard(uc, runs) {
  const rows = (runs || []).map((run) => {
    const train = run.mode === "train";
    const headline =
      run.state === "done"
        ? train
          ? dash(run.best_model)
          : `Scored ${dash(run.row_count, fmtInt)} rows`
        : STATE_LABEL[run.state] || run.state;
    const right =
      train && run.state === "done"
        ? `${dash(run.headline_metric_label)} ${fmtVal(run.headline_score)}`
        : train
          ? EM_DASH
          : dash(run.best_model);
    return `<a class="runrow" href="${esc(routes.run(uc.id, run.run_id))}">
      <div><div class="r1">${esc(headline)}${run.champion ? '<span class="champ">Champion</span>' : ""}</div>
      <div class="r2">${esc(run.file_name)} · ${esc(fmtStamp(run.created_at))}</div></div>
      <div class="r3"><b>${esc(right)}</b><span style="font-size:12px;color:var(--muted)">${esc(
        run.mode,
      )}</span></div></a>`;
  });
  return `<section class="card"><h3>Previous uplift runs</h3><div class="runs-list">${
    rows.length ? rows.join("") : `<div class="empty">No uplift runs yet.</div>`
  }</div></section>`;
}

function setupBody(uc, s) {
  const train = s.mode === "train";
  const profile = profileOf(s);
  const names = profile ? profile.columns.map((c) => c.name) : [];
  const why = setupBlocker(s);

  const step1 = `<div class="fstep ${s.upload ? "done" : ""}"><div class="stepno">1</div><div>
    <div class="flabel">Dataset</div><div class="fhint">${esc(
      train
        ? "One row per customer of a past campaign: the features, whether they were treated (0/1, assigned at random) and the outcome."
        : "The customers to score. The trained uplift model ranks them by how much the action changes their outcome.",
    )}</div>
    <div class="orline"><label class="control file ${
      s.upload ? "has" : ""
    }"><input type="file" id="u-file" class="sr" accept=".csv,.parquet"><span class="fname">${esc(
      profile ? profile.file_name : "Upload CSV or Parquet",
    )}</span><span class="ico" aria-hidden="true">⤒</span></label>${
      profile
        ? `<span>${esc(fmtInt(profile.row_count))} rows · ${esc(String(profile.column_count))} columns · ${esc(
            fmtSize(profile.file_size_bytes),
          )}</span>`
        : ""
    }</div>
    ${s.uploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${s.uploadError ? errorBox(s.uploadError) : ""}</div></div>`;

  const columnSelect = (id, label, current, choices) =>
    `<div class="field"><span class="sub">${esc(label)}</span><div class="control sel"><select id="${id}">${option(
      "",
      "Select a column…",
      current,
    )}${choices.map((n) => option(n, n, current)).join("")}</select></div></div>`;

  const candidates = (s.candidates && s.candidates.candidates) || [];
  const treatmentField = train
    ? `<div class="field"><span class="sub">Treatment column</span><div class="control sel"><select id="u-treatment">${treatmentOptions(
        s,
        names,
      )}</select></div></div>`
    : "";
  const candidateNote = train
    ? s.candidatesLoading
      ? `<div class="loading">Looking for 0/1 treatment columns…</div>`
      : s.candidatesError
        ? errorBox(s.candidatesError)
        : s.upload && !candidates.length && s.candidates
          ? `<p class="seg-help">No 0/1 column in this file looks like a treatment. Uplift needs one: 1 for customers who got the action, 0 for the randomly held-out control group.</p>`
          : ""
    : "";
  const ptype =
    train && s.treatment
      ? `<div class="ptype"><span>Problem type</span><span class="pill">Uplift</span><span>${esc(
          UPLIFT_EXPLANATION,
        )}</span>${
          s.acknowledged.includes(NOT_RANDOM)
            ? `<span class="uwarn">${esc(NOT_CAUSAL_NOTE)}</span>`
            : ""
        }</div>`
      : "";
  const step2 = `<div class="fstep ${s.upload ? "" : "locked"} ${
    !why || (s.pk && !train) ? "done" : ""
  }"><div class="stepno">2</div><div>
    <div class="flabel">Columns</div><div class="fhint">${esc(
      train
        ? "Which column identifies a customer, which one says whether they were treated, and which one is the outcome."
        : "Which column identifies a customer.",
    )}</div>
    <div class="frow">${columnSelect("u-pk", "Primary key", s.pk, names)}${
      train
        ? `${treatmentField}${columnSelect(
            "u-target",
            "Outcome column",
            s.target,
            names.filter((n) => n !== s.pk && n !== s.treatment),
          )}`
        : ""
    }</div>${candidateNote}${ptype}</div></div>`;

  const versions = upliftVersions(s.models);
  const step3 = train
    ? ""
    : `<div class="fstep ${s.upload ? "" : "locked"} done"><div class="stepno">3</div><div>
        <div class="flabel">Trained uplift model</div><div class="fhint">The saved uplift model that will rank the uploaded customers.</div>
        <div class="frow"><div class="field" style="width:360px"><div class="control sel"><select id="u-model" aria-label="Trained uplift model">${
          versions.length
            ? versions
                .map((v) =>
                  option(
                    v.version.model_id,
                    `${v.version.model_display_name} · ${v.version.metric_label} ${fmtVal(v.version.test_score)} · ${fmtStamp(
                      v.version.created_at,
                    )}${v.is_champion ? " · Champion" : ""}`,
                    s.modelVersionId,
                  ),
                )
                .join("")
            : `<option value="">No trained uplift model yet</option>`
        }</select></div></div></div></div></div>`;

  return `<div class="setup-grid">
    <section class="card"><div class="form-body">
      <div class="seg" role="group" aria-label="Mode"><button type="button" data-umode="train" class="${
        train ? "on" : ""
      }">Train uplift model</button><button type="button" data-umode="score" class="${
        train ? "" : "on"
      }">Score new data</button></div>
      <p class="seg-help">${esc(
        train
          ? `Uplift ${UPLIFT_EXPLANATION}. It needs a past campaign where the action was given at random.`
          : "Scoring writes the treat list: persuadables within budget are Treat, sleeping dogs never are, and a control group is held out.",
      )}</p>
      <form id="u-setup" novalidate style="margin-top:18px">
        ${step1}${step2}${step3}
        ${validationHtml(s)}
        ${s.submitError ? errorBox(s.submitError) : ""}
        <div class="actions"><button type="submit" class="run" id="u-run"${why || s.submitting ? " disabled" : ""}>${esc(
          s.submitting ? "Starting…" : train ? "Train uplift model" : "Score customers",
        )}</button><span class="reason">${esc(why)}</span></div>
      </form></div></section>
    ${upliftRunsCard(uc, s.runs)}
  </div>
  <p class="next">After the run: Model (Qini curve, AUUC) → Output (segments, treat list) → Campaign results.</p>`;
}

// --- Running -----------------------------------------------------------------------------------

/** `status.json`'s stages collapsed onto their `group_label` rows, as the Phase 1 Running screen does. */
export function groupStages(stages) {
  const groups = [];
  const index = new Map();
  for (const stage of stages || []) {
    if (!index.has(stage.group_label)) {
      index.set(stage.group_label, groups.length);
      groups.push({ label: stage.group_label, stages: [] });
    }
    groups[index.get(stage.group_label)].stages.push(stage);
  }
  return groups.map((group) => {
    const states = group.stages.map((st) => st.state);
    const state = states.includes("failed")
      ? "failed"
      : states.includes("cancelled")
        ? "cancelled"
        : states.includes("running")
          ? "running"
          : states.every((st) => st === "done" || st === "skipped")
            ? "done"
            : "pending";
    const failed = group.stages.find((st) => st.state === "failed");
    const withDetail = group.stages.filter((st) => st.detail);
    const detail =
      failed && failed.error
        ? failed.error.message
        : withDetail.length
          ? withDetail[withDetail.length - 1].detail
          : "";
    return { label: group.label, state, detail };
  });
}

function runningBody(uc, s) {
  const status = s.detail && s.detail.status;
  const groups = status ? groupStages(status.stages) : [];
  const cls = { running: "active", done: "done", failed: "failed", cancelled: "cancelled", pending: "" };
  const rows = groups.length
    ? groups
        .map(
          (g, i) =>
            `<li class="${cls[g.state]}"><span class="dot">${i + 1}</span><div><div class="pt">${esc(
              g.label,
            )}</div><div class="pd">${esc(g.detail)}</div></div></li>`,
        )
        .join("")
    : `<li><span class="dot">1</span><div><div class="pt">Waiting for the run to start</div><div class="pd"></div></div></li>`;
  return `<div class="setup-grid"><section class="card"><h3>Running… <button type="button" class="cancel" id="u-cancel">Cancel</button></h3><ol class="progress">${rows}</ol>${
    s.submitError ? errorBox(s.submitError) : ""
  }</section>${upliftRunsCard(uc, s.runs)}</div>`;
}

// --- Results -----------------------------------------------------------------------------------

function resultsBody(uc, s) {
  const run = s.detail && s.detail.run;
  if (!run) return `<div class="loading">Loading the run…</div>`;
  const train = run.mode === "train";
  const headline =
    run.state === "done"
      ? `<span class="ok">✓ ${train ? "Uplift model trained" : "Scoring complete"}</span>`
      : run.state === "cancelled"
        ? `<span class="muted">Run cancelled</span>`
        : `<span class="bad">✕ Run failed</span>`;
  const detailLine =
    run.state === "done"
      ? train
        ? `<span>${esc(dash(run.best_model))} · ${esc(dash(run.headline_metric_label))} ${esc(
            fmtVal(run.headline_score),
          )}</span>`
        : `<span><b>${esc(dash(run.row_count, fmtInt))}</b> customers scored</span>`
      : run.error
        ? `<span>${esc(run.error.message)}</span>`
        : "";
  const blocks = train
    ? [
        ["Data", routes.data(uc.id, run.run_id), run.file_name, `${dash(run.row_count, fmtN)} rows · outcome ${dash(run.target)}`],
        [
          "Model",
          routes.model(uc.id, run.run_id),
          dash(run.best_model),
          `Qini curve · ${dash(run.headline_metric_label)} ${fmtVal(run.headline_score)}`,
        ],
        ["Output", routes.output(uc.id, run.run_id), "Segments & targeting", "Four segments on the hold-out, recommended contacts"],
      ]
    : [
        ["Data", routes.data(uc.id, run.run_id), run.file_name, `${dash(run.row_count, fmtN)} rows`],
        ["Output", routes.output(uc.id, run.run_id), "Treat list", "Segments, recommended contacts, download"],
        ["Campaign results", routes.campaign(uc.id, run.run_id), "Treated vs control", "Upload outcomes once the campaign has run"],
      ];
  const flow = blocks
    .map(
      ([label, href, value, meta], i) =>
        `${i ? '<div class="arrow" aria-hidden="true">→</div>' : ""}<a class="block${
          run.state === "done" ? "" : " pending"
        }" href="${esc(href)}"><div><div class="lab"><span>${String(i + 1).padStart(2, "0")}&nbsp;&nbsp;${esc(
          label,
        )}</span>${
          run.state === "done" ? '<span class="bstate">✓ Done</span>' : '<span class="bstate waiting">Not reached</span>'
        }</div><div class="val">${esc(value)}</div><div class="meta">${esc(
          meta,
        )}</div></div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`,
    )
    .join("");
  return `<div class="results"><div class="summary">${headline}${detailLine}<span class="muted">${esc(
    run.file_name,
  )} · ${esc(fmtStamp(run.created_at))}</span><a class="again" href="${esc(
    routes.setup(uc.id),
  )}">Run again / change settings</a></div>
    <span class="cap">Uplift pipeline</span><div class="flow">${flow}</div>
    <div class="runs-below">${upliftRunsCard(uc, s.runs)}</div></div>`;
}

/** `#/uplift/<use case>[/run/<run>]`: Setup, Running or Results, like the Phase 1 use-case screen. */
export function upliftScreenHtml(uc, s) {
  const body = s.view === "running" ? runningBody(uc, s) : s.view === "results" ? resultsBody(uc, s) : setupBody(uc, s);
  return screenOf(
    uc,
    `<a class="back" href="${esc(routes.useCase(uc.id))}">‹&nbsp; ${esc(uc.name)}</a><h1 class="h1">${esc(
      uc.name,
    )} · Uplift</h1><p class="desc">Uplift ${esc(UPLIFT_EXPLANATION)}.</p><div class="chips">${stageChip(
      uc.lifecycle_stage,
    )}${typeChip(uc)}${upliftChip()}</div>`,
    body,
  );
}

// --- Results pages: shell ----------------------------------------------------------------------

function pageShell(uc, run, kind, body) {
  const train = run.mode === "train";
  const tabs = (
    train
      ? [
          ["data", "Data", routes.data(uc.id, run.run_id)],
          ["model", "Model", routes.model(uc.id, run.run_id)],
          ["output", "Output", routes.output(uc.id, run.run_id)],
        ]
      : [
          ["data", "Data", routes.data(uc.id, run.run_id)],
          ["output", "Output", routes.output(uc.id, run.run_id)],
          ["campaign", "Campaign results", routes.campaign(uc.id, run.run_id)],
        ]
  )
    .map(
      ([key, label, href], i) =>
        `${i ? '<span class="arr" aria-hidden="true">→</span>' : ""}<a class="tab ${key === kind ? "on" : ""}" href="${esc(
          href,
        )}"${key === kind ? ' aria-current="page"' : ""}><span class="n">0${i + 1}</span>${esc(label)}</a>`,
    )
    .join("");
  const label = { model: "Model", output: "Output", campaign: "Campaign results" }[kind];
  const note = `${train ? "Training run" : "Scoring run"} ${run.run_id} · ${fmtStamp(run.created_at)}`;
  return screenOf(
    uc,
    `${crumbs(uc, label)}<span class="over" style="color:var(--c)">${esc(label)}</span><h1 class="h1">${esc(
      {
        model: "How well the model finds the persuadable",
        output: "Who to contact",
        campaign: "Did the campaign work?",
      }[kind],
    )}</h1><div class="chips">${stageChip(uc.lifecycle_stage)}${typeChip(uc)}${upliftChip()}</div>`,
    `<div class="tabs-bar"><nav class="tabs" aria-label="Pipeline">${tabs}</nav><span class="note">${esc(
      note,
    )}</span></div><div class="stack">${body}</div>`,
  );
}

// --- Model -------------------------------------------------------------------------------------

function opeCard(o) {
  const report = o && o.report;
  const estimates = (report && report.estimates) || [];
  const methodLabel = { ips: "IPS", snips: "Self-normalised IPS", dr: "Doubly robust" };
  const result = report
    ? `${notCausalBanner(report)}${kvs([
        ["Policy", dash(report.policy_description)],
        ["Customers the policy treats", fmtRate(report.policy_treat_share)],
        ["Logged outcome rate", fmtRate(report.logged_value, 2)],
        ["Treat everyone (DR)", fmtCi(report.treat_all_value, (v) => fmtRate(v, 2))],
        ["Treat no one (DR)", fmtCi(report.treat_none_value, (v) => fmtRate(v, 2))],
        ["Rows", dash(report.rows, fmtInt)],
      ])}${table(
        ["estimator", "outcome rate if followed"],
        estimates.map((e) => [methodLabel[e.method] || e.method, fmtCi(e.value, (v) => fmtRate(v, 2))]),
      )}<p class="caption">Estimated on the hold-out, which the model never saw · ${esc(
        fmtStamp(report.computed_at),
      )}</p>`
    : `<div class="empty">No targeting rule has been evaluated on this run yet.</div>`;
  const share = o && present(o.topSharePct) ? o.topSharePct : "";
  return `<section class="card"><h3>What if we targeted only the top customers? (off-policy estimate)</h3>
    <form id="u-ope" class="uform" novalidate><div class="frow"><div class="field xs"><span class="sub">Top share (%)</span><div class="control"><input type="number" id="u-ope-share" min="1" max="100" step="1" value="${esc(
      share,
    )}" aria-label="Top share of customers, percent"></div></div></div>
    <div class="actions"><button type="submit" class="run"${o && o.submitting ? " disabled" : ""}>${
      o && o.submitting ? "Estimating…" : "Estimate"
    }</button><span class="reason">Treat the customers with the highest predicted uplift, then estimate the outcome rate.</span></div>
    ${o && o.error ? errorBox(o.error) : ""}</form>${result}</section>`;
}

/** `#/uplift/<use case>/model/<run>`: Qini curve, AUUC with its interval, uplift by decile. */
export function modelPageHtml(uc, run, art, ope) {
  const validation = art["uplift_validation.json"];
  const evaluation = art["uplift_evaluation.json"];
  const curve = art["qini_curve.json"];
  const e = evaluation || {};
  const tiles = kpis([
    ["AUUC", fmtVal(e.auuc && e.auuc.value)],
    ["AUUC interval", fmtInterval(e.auuc)],
    ["Qini coefficient", fmtVal(e.qini_coefficient && e.qini_coefficient.value)],
    ["Treating everyone (ATE)", fmtPts(e.average_treatment_effect && e.average_treatment_effect.value)],
  ]);
  const verdict = evaluation
    ? `<section class="card"><div class="usummary"><span class="pill ${
        evaluation.measurable_uplift ? "ok" : "warn"
      }">${evaluation.measurable_uplift ? "Measurable uplift" : "No measurable uplift"}</span> ${esc(
        evaluation.summary,
      )}</div></section>`
    : `<section class="card"><div class="empty">This run has not produced uplift_evaluation.json yet.</div></section>`;
  const setupRows = [
    ["Learner", labelOf(LEARNER_LABEL, e.learner)],
    ["Base model", labelOf(BASE_MODEL_LABEL, e.base_model)],
    ["Hold-out rows", dash(e.rows_evaluated, fmtInt)],
    ["Treated", `${dash(e.treated_rows, fmtInt)} · ${fmtRate(e.treated_rate, 2)} converted`],
    ["Control", `${dash(e.control_rows, fmtInt)} · ${fmtRate(e.control_rate, 2)} converted`],
    ["Treating everyone (ATE)", fmtCi(e.average_treatment_effect, (v) => fmtPts(v, 2))],
    ["Qini coefficient", fmtCi(e.qini_coefficient)],
    ["Bootstrap resamples", dash(e.bootstrap_samples, fmtInt)],
    [
      "Randomness check (AUC)",
      validation && present(validation.randomness_auc) ? fmtNum(validation.randomness_auc, 3) : EM_DASH,
    ],
    ["Evaluated", fmtStamp(e.evaluated_at)],
  ];
  const upliftAt = (e.uplift_at || []).length
    ? table(
        ["customers targeted", "observed uplift", "interval"],
        e.uplift_at.map((u) => [
          `Top ${fmtNum(u.fraction * 100, 0)}%`,
          fmtPts(u.uplift && u.uplift.value),
          fmtInterval(u.uplift, (v) => fmtPts(v)),
        ]),
        "eval",
      )
    : `<div class="empty">This run has not produced uplift_evaluation.json yet.</div>`;
  const deciles = e.deciles || [];
  const decileTable = deciles.length
    ? table(
        ["decile", "rows", "treated", "control", "treated rate", "control rate", "observed uplift", "predicted uplift"],
        deciles.map((d) => [
          String(d.decile),
          fmtInt(d.rows),
          fmtInt(d.treated_rows),
          fmtInt(d.control_rows),
          fmtRate(d.treated_rate),
          fmtRate(d.control_rate),
          fmtPts(d.observed_uplift),
          fmtPts(d.predicted_uplift),
        ]),
      )
    : `<div class="empty">This run has not produced uplift_evaluation.json yet.</div>`;
  const body = `${notCausalBanner(validation, evaluation, art["segments.json"], art["policy_recommendation.json"])}
    ${tiles}${verdict}
    <div class="row">
      <section class="card"><h3>Qini curve (hold-out)</h3>${qiniChart(curve)}<p class="caption">${esc(
        `The further the curve sits above the dashed line, the better targeting by predicted uplift beats random targeting${
          curve ? ` · ${fmtInt(curve.rows_evaluated)} hold-out rows` : ""
        }.`,
      )}</p></section>
      <section class="card"><h3>Training setup</h3>${kvs(setupRows)}</section>
    </div>
    <div class="row">
      <section class="card"><h3>Uplift by decile</h3>${decileChart(deciles)}</section>
      <section class="card"><h3>Uplift in the top customers</h3>${upliftAt}<p class="caption">Treated conversion rate minus control conversion rate among the customers ranked highest.</p></section>
    </div>
    <section class="card"><h3>Decile table</h3>${decileTable}</section>
    ${opeCard(ope)}`;
  return pageShell(uc, run, "model", body);
}

// --- Output ------------------------------------------------------------------------------------

/** `#/uplift/<use case>/output/<run>`: the four segments, the targeting recommendation, the treat list. */
export function outputPageHtml(uc, run, art, extra = {}) {
  const validation = art["uplift_validation.json"];
  const segments = art["segments.json"];
  const policy = art["policy_recommendation.json"];
  const p = policy || {};
  const expected = p.expected_incremental_conversions;
  const tiles = kpis([
    ["Recommended to contact", dash(p.contacts_recommended, fmtInt)],
    ["Expected incremental conversions", fmtCount(expected && expected.value)],
    ["Eligible persuadables", dash(p.eligible_persuadables, fmtInt)],
    ["Expected net value", fmtCount(p.expected_net_value, 2)],
  ]);
  const scope = policy
    ? policy.computed_on === "test"
      ? "Measured on the hold-out split of the training run."
      : "Computed over every customer this run scored."
    : "";
  const t = (segments && segments.thresholds) || {};
  const thresholdRows = segments
    ? kvs([
        ["Persuadable", `predicted uplift ≥ ${fmtPts(t.persuadable_min_uplift, 2)}`],
        ["Sleeping dog", `predicted uplift ≤ ${fmtPts(t.sleeping_dog_max_uplift, 2)}`],
        [
          "Sure thing vs lost cause",
          `P(outcome if not treated) ${present(t.sure_thing_min_probability) ? `≥ ${fmtRate(t.sure_thing_min_probability)}` : EM_DASH}${
            t.sure_thing_from_base_rate ? " (the training base rate)" : ""
          }`,
        ],
        ["Customers segmented", dash(segments.rows, fmtInt)],
      ])
    : "";
  const policyRows = policy
    ? kvs([
        [
          "Recommended to contact",
          `${fmtInt(policy.contacts_recommended)} of ${fmtInt(policy.eligible_persuadables)} persuadables`,
        ],
        ["Contact budget", present(policy.budget_contacts) ? fmtInt(policy.budget_contacts) : "No budget set"],
        ["Why not more", STOP_REASON[policy.stop_reason] || humanise(policy.stop_reason)],
        ["Expected incremental conversions", fmtCi(expected, (v) => fmtCount(v))],
        ["Model's own prediction", fmtCount(policy.predicted_incremental_conversions)],
        ["Cost per contact", fmtCount(policy.cost_per_contact, 2)],
        ["Value per conversion", fmtCount(policy.value_per_conversion, 2)],
        ["Expected cost", fmtCount(policy.expected_cost, 2)],
        ["Expected value", fmtCount(policy.expected_value, 2)],
        ["Expected net value", fmtCount(policy.expected_net_value, 2)],
      ])
    : `<div class="empty">This run has not produced policy_recommendation.json yet.</div>`;

  let treatList;
  if (run.mode === "score") {
    treatList = `<div class="usummary">${esc(
      "Every scored customer with their segment and action. Treat rows are the list to contact; control-group rows are held out at random so the campaign can be measured; sleeping dogs are never Treat.",
    )}</div><p class="caption" style="padding-top:14px"><a class="linkbtn" href="${esc(
      extra.scoresHref || "#",
    )}">Download treat list (CSV)</a> · <a class="linkbtn" href="${esc(
      routes.campaign(uc.id, run.run_id),
    )}">Campaign results for this run</a></p>`;
  } else {
    const scoreRuns = (extra.scoreRuns || []).filter((r) => r.state === "done");
    treatList = `<div class="empty">${esc(
      "The treat list comes from a scoring run: switch to “Score new data” on the uplift setup screen and run it against this model.",
    )} <a class="linkbtn" href="${esc(routes.setup(uc.id))}">Score new data</a></div>${
      scoreRuns.length
        ? `<div class="runs-list">${scoreRuns
            .map(
              (r) =>
                `<a class="runrow" href="${esc(routes.output(uc.id, r.run_id))}"><div><div class="r1">Scored ${esc(
                  dash(r.row_count, fmtInt),
                )} rows</div><div class="r2">${esc(r.file_name)} · ${esc(
                  fmtStamp(r.created_at),
                )}</div></div><div class="r3"><b>Treat list ›</b></div></a>`,
            )
            .join("")}</div>`
        : ""
    }`;
  }

  const body = `${notCausalBanner(validation, segments, policy)}
    ${tiles}${scope ? `<p class="note" style="margin:-8px 0 0">${esc(scope)}</p>` : ""}
    <div class="row">
      <section class="card"><h3>Four segments</h3>${segmentChart(segments)}${
        segments ? `<h4>How the segments are cut</h4>${thresholdRows}` : ""
      }</section>
      <section class="card"><h3>Targeting recommendation</h3>${policyRows}</section>
    </div>
    <section class="card"><h3>Treat list</h3>${treatList}</section>`;
  return pageShell(uc, run, "output", body);
}

// --- Campaign results --------------------------------------------------------------------------

function reportSection(report) {
  if (!report) {
    return `<section class="card"><h3>Campaign results</h3><div class="empty">No outcomes have been uploaded for this run yet. Upload them below once the campaign has run.</div></section>`;
  }
  const banner = notCausalBanner(report);
  if (report.status === "immature") {
    return `${banner}<section class="card"><h3>Campaign results</h3><div class="uwait"><b>Results available on ${esc(
      fmtDay(report.results_available_on),
    )}</b><span>${esc(
      `${fmtInt(report.rows_immature)} customers are still inside their ${
        present(report.outcome_window_days) ? `${report.outcome_window_days}-day ` : ""
      }outcome window (as of ${fmtStamp(report.as_of)}). Nothing is estimated before it has elapsed: an early read would count conversions that have not happened yet.`,
    )}</span></div></section>`;
  }
  const lift = report.absolute_lift;
  const inc = report.incremental_conversions;
  const tiles = kpis([
    ["Incremental conversions", fmtCount(inc && inc.value)],
    ["Absolute lift", fmtPts(lift && lift.value, 2)],
    ["Relative lift", present(report.relative_lift) ? `${signed(report.relative_lift * 100, 1)}%` : EM_DASH],
    ["p-value", fmtP(report.p_value)],
  ]);
  const partial = present(report.results_available_on)
    ? `<p class="note" style="margin:0">${esc(
        `${fmtInt(report.rows_immature)} customers were still inside their outcome window and are excluded; every customer's window has elapsed on ${fmtDay(
          report.results_available_on,
        )}.`,
      )}</p>`
    : "";
  const arms = table(
    ["group", "customers", "conversions", "conversion rate"],
    [
      ["Treated", fmtInt(report.treated_rows), fmtInt(report.treated_conversions), fmtRate(report.treated_rate, 2)],
      ["Control (held out)", fmtInt(report.control_rows), fmtInt(report.control_conversions), fmtRate(report.control_rate, 2)],
    ],
  );
  const details = kvs([
    ["Absolute lift", fmtCi(lift, (v) => fmtPts(v, 2))],
    ["Incremental conversions", fmtCi(inc, (v) => fmtCount(v))],
    ["Outcome column", dash(report.outcome_column)],
    ["Outcome window", present(report.outcome_window_days) ? `${report.outcome_window_days} days` : EM_DASH],
    ["Judged as of", fmtStamp(report.as_of)],
    ["Excluded: window not elapsed", fmtInt(report.rows_immature)],
    ["Excluded: no outcome row", fmtInt(report.rows_without_outcome)],
    ["Not in either group", fmtInt(report.rows_suppressed_or_untreated)],
    ["Computed", fmtStamp(report.computed_at)],
  ]);
  return `${banner}${tiles}${partial}<section class="card"><div class="usummary">${esc(report.summary)}</div>${arms}</section>
    <section class="card"><h3>How it was measured</h3>${details}</section>`;
}

function outcomesForm(c) {
  const profile = c.upload ? c.upload.profile : null;
  const names = profile ? profile.columns.map((col) => col.name) : [];
  const f = c.form || {};
  const select = (id, label, current, emptyLabel) =>
    `<div class="field"><span class="sub">${esc(label)}</span><div class="control sel"><select id="${id}">${option(
      "",
      emptyLabel,
      current,
    )}${names.map((n) => option(n, n, current)).join("")}</select></div></div>`;
  const ready = profile && f.outcome_column;
  return `<section class="card"><h3>Upload campaign outcomes</h3><form id="u-camp" class="uform" novalidate>
    <p class="seg-help" style="margin:0">One row per customer of this run: its primary key and whether they converted. Treated and control customers are compared; suppressed customers are left out.</p>
    <div class="orline"><label class="control file ${profile ? "has" : ""}"><input type="file" id="u-camp-file" class="sr" accept=".csv,.parquet"><span class="fname">${esc(
      profile ? profile.file_name : "Upload outcomes CSV or Parquet",
    )}</span><span class="ico" aria-hidden="true">⤒</span></label>${
      profile ? `<span>${esc(fmtInt(profile.row_count))} rows</span>` : ""
    }</div>
    ${c.uploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${c.uploadError ? errorBox(c.uploadError) : ""}
    ${
      profile
        ? `<div class="frow">${select("u-camp-outcome", "Outcome column", f.outcome_column, "Select a column…")}${select(
            "u-camp-date",
            "Treatment date column",
            f.treatment_date_column,
            "None: use the scoring time",
          )}</div>
    <div class="frow"><div class="field"><span class="sub">Outcome window (days)</span><div class="control"><input type="number" id="u-camp-window" min="1" max="3650" step="1" value="${esc(
      present(f.outcome_window_days) ? f.outcome_window_days : "",
    )}"></div></div><div class="field"><span class="sub">Converted value (optional)</span><div class="control"><input type="text" id="u-camp-label" value="${esc(
      f.positive_label || "",
    )}" placeholder="1, true or yes"></div></div><div class="field"><span class="sub">Judge maturity as of (optional)</span><div class="control"><input type="date" id="u-camp-asof" value="${esc(
      f.as_of || "",
    )}"></div></div></div>`
        : ""
    }
    ${c.submitError ? errorBox(c.submitError) : ""}
    <div class="actions"><button type="submit" class="run" id="u-camp-run"${!ready || c.submitting ? " disabled" : ""}>${
      c.submitting ? "Measuring…" : "Measure the campaign"
    }</button><span class="reason">${esc(
      !profile ? "Upload the outcomes file to continue" : !f.outcome_column ? "Choose the outcome column" : "",
    )}</span></div></form></section>`;
}

/** `#/campaign/<use case>/<run>`: the incrementality report of a scoring run, or when it will exist. */
export function campaignPageHtml(uc, run, c) {
  if (run.mode !== "score") {
    const body = `<section class="card"><h3>Campaign results</h3><div class="empty">${esc(
      "Campaign results are measured on a scoring run: its control group was held out at random when the treat list was written. This is a training run.",
    )} <a class="linkbtn" href="${esc(routes.setup(uc.id))}">Score new data</a></div></section>`;
    return pageShell(uc, run, "campaign", body);
  }
  const body = `${c.loadError ? errorBox(c.loadError) : ""}${reportSection(c.report)}${outcomesForm(c)}`;
  return pageShell(uc, run, "campaign", body);
}
