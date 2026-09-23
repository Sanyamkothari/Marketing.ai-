// The Data, Model and Output pages, rendered from a run's artefacts (plan §7's table, §9.3).
//
// Every figure on these three screens comes out of an artefact this run wrote. Nothing is filled
// in from the prototype's illustrative figures: an artefact this run did not produce renders as an
// em dash or as an empty card that says what is missing.

import {
  EM_DASH,
  barTrack,
  columnBar,
  dash,
  errorBox,
  esc,
  fmtDate,
  fmtInt,
  fmtN,
  fmtNum,
  fmtPct,
  fmtStamp,
  fmtSize,
  journeyCrumb,
  kpis,
  kvs,
  pageHead,
  present,
  stageChip,
  steps,
  table,
  typeChip,
} from "./dom.js";
import { indexSchema, readPath } from "./settings.js";

export const PAGE_ARTEFACTS = {
  data: ["profile.json", "prepare.json", "split.json", "validation.json", "run_config.json"],
  model: [
    "run_config.json",
    "best_model.json",
    "evaluation.json",
    "baseline.json",
    "feature_importance.json",
    "confusion_matrix.json",
    "leaderboard.json",
    "fairness.json",
  ],
  output: ["decile_lift.json", "scoring_summary.json", "drift.json", "run_config.json", "prepare.json"],
};

/**
 * What an uplift run's pages read instead (DEC-651: `run.problem_type === "uplift"`). An uplift run
 * writes no `prepare.json`, `drift.json`, `decile_lift.json` or ROC-style evaluation, so asking for
 * them would only log 404s; it writes the uplift artefacts, which `GET /runs/{id}/artefacts/{name}`
 * serves since M53.
 */
export const UPLIFT_PAGE_ARTEFACTS = {
  data: [
    "profile.json",
    "split.json",
    "validation.json",
    "uplift_validation.json",
    "uplift_drift.json",
    "run_config.json",
  ],
  model: ["run_config.json", "uplift_evaluation.json", "qini_curve.json", "feature_importance.json"],
  output: [
    "scoring_summary.json",
    "segments.json",
    "policy_recommendation.json",
    "uplift_drift.json",
    "run_config.json",
  ],
};

export const isUplift = (run) => !!run && run.problem_type === "uplift";

/** The artefacts one page of this run reads: Phase 1's list, or the uplift one. */
export const pageArtefacts = (kind, run) => (isUplift(run) ? UPLIFT_PAGE_ARTEFACTS : PAGE_ARTEFACTS)[kind];

const TAB_LABEL = { data: "Data", model: "Model", output: "Output" };

/** The key as the user named it: one column, or its columns joined by " + " for a two-column key. */
const keyText = (pk) => (Array.isArray(pk) ? pk.join(" + ") : dash(pk));

/** A choice label from the advanced-settings schema, so enum wording matches the Setup screen. */
function choiceLabel(byPath, path, value) {
  const field = byPath.get(path);
  if (field && Array.isArray(field.choices)) {
    const choice = field.choices.find((c) => c.value === value);
    if (choice) return choice.label;
  }
  return present(value) ? humanise(value) : EM_DASH;
}

/** A vocabulary word the schema does not carry a label for: `clip_percentile` → `Clip percentile`. */
function humanise(value) {
  const text = String(value).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function shell(uc, kind, run, body) {
  const label = TAB_LABEL[kind];
  const tabs = ["data", "model", "output"]
    .map(
      (t, i) =>
        `${i ? '<span class="arr" aria-hidden="true">→</span>' : ""}<a class="tab ${
          t === kind ? "on" : ""
        }" href="#/uc/${esc(uc.id)}/${t}/${esc(run.run_id)}" ${
          t === kind ? 'aria-current="page"' : ""
        }><span class="n">0${i + 1}</span>${TAB_LABEL[t]}</a>`,
    )
    .join("");
  const note = `${run.mode === "train" ? "Training run" : "Scoring run"} ${run.run_id} · ${fmtStamp(
    run.created_at,
  )}`;
  return `<main class="screen t-${esc(uc.marker)}">
    ${pageHead(`<nav class="crumbs" aria-label="Breadcrumb">${journeyCrumb(uc)}<span class="sep">›</span><a href="#/uc/${esc(
      uc.id,
    )}">${esc(uc.name)}</a><span class="sep">›</span><span class="cur">${label}</span></nav>
      <span class="over" style="color:var(--c)">${label}</span><h1 class="h1">${esc(
        uc.pages[kind],
      )}</h1><div class="chips">${stageChip(uc.lifecycle_stage)}${typeChip(uc)}</div>`)}
    <div class="tabs-bar"><nav class="tabs" aria-label="Pipeline">${tabs}</nav><span class="note">${esc(
      note,
    )}</span></div>
    <div class="stack">${body}</div>
  </main>`;
}

// --- Data --------------------------------------------------------------------------------------

function dateRange(split) {
  const parts = (split && split.parts) || [];
  const starts = parts.map((p) => p.start_date).filter(present);
  const ends = parts.map((p) => p.end_date).filter(present);
  if (!starts.length || !ends.length) return EM_DASH;
  const from = starts.slice().sort()[0];
  const to = ends.slice().sort().slice(-1)[0];
  return `${fmtDate(from)} – ${fmtDate(to)}`;
}

function leakageLine(validation, config) {
  const checks = ((validation && validation.checks) || []).filter((c) => c.code.startsWith("LEAKAGE"));
  if (checks.length) {
    return checks
      .map((c) => `${c.severity === "error" ? "Flagged" : "Warned"} on ${c.column || EM_DASH}`)
      .join(" · ");
  }
  const enabled = readPath(config || {}, "validation.leakage_check");
  if (enabled === false) return "Not run";
  if (!validation) return EM_DASH;
  return "Passed";
}

/** One column of the lineage block: a heading and the cards under it, worded by the engine. */
function lineageColumn(title, cards) {
  return `<div class="lin"><div class="lt">${esc(title)}</div>${cards
    .map(
      (card) =>
        `<div class="li">${esc(card.id)}</div><div class="ld">${esc(card.label)}${
          card.detail ? ` · ${esc(card.detail)}` : ""
        }</div>`,
    )
    .join("")}</div>`;
}

/**
 * "Where this dataset came from" (Plan A M35; prototype screenshot `15-data-lineage`): sources ->
 * mapping -> recipe -> dataset -> this run, left to right. Every card but the last is the engine's
 * own `GET /datasets/{id}/lineage` node, already worded; the last is this run's record. A run that
 * read an uploaded file has no lineage and shows no card.
 */
function lineageCard(run, extra) {
  if (extra.lineageError) {
    return `<section class="card"><h3>Where this dataset came from</h3><div style="padding:0 20px 20px">${errorBox(
      extra.lineageError,
    )}</div></section>`;
  }
  const lineage = extra.lineage;
  if (!lineage) return "";
  const runCard = {
    id: run.run_id,
    label: run.mode === "train" ? "Training run" : "Scoring run",
    detail: fmtStamp(run.created_at),
  };
  return `<section class="card"><h3>Where this dataset came from</h3><div class="lineage">${[
    lineageColumn("Sources", lineage.sources),
    lineageColumn("Mapping", lineage.mappings),
    lineageColumn("Recipe", [lineage.spec]),
    lineageColumn("Dataset", [lineage.dataset]),
    lineageColumn("Run", [runCard]),
  ].join("")}</div><p class="caption">Built from raw tables. Every id is stored with the run, so the same recipe can be replayed on next month's tables.</p></section>`;
}

function dataPage(uc, run, art, byPath, extra) {
  const profile = art["profile.json"];
  const prepare = art["prepare.json"];
  const split = art["split.json"];
  const validation = art["validation.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;

  const tiles = kpis([
    ["Rows", dash(profile && profile.row_count, fmtN)],
    ["Features", dash(prepare && prepare.feature_columns && prepare.feature_columns.length, String)],
    ["Date range", dateRange(split)],
    ["Missing values", dash(profile && profile.missing_value_rate_pct, (v) => `${fmtNum(v, 2)}%`)],
  ]);

  const fileRows = profile
    ? [
        ["File", profile.file_name],
        ["Format", String(profile.file_format).toUpperCase()],
        ["Size", fmtSize(profile.file_size_bytes)],
        ["Rows", fmtInt(profile.row_count)],
        ["Columns", String(profile.column_count)],
        ["Encoding", profile.encoding],
        ["Fingerprint", profile.fingerprint ? profile.fingerprint.hash.slice(0, 26) + "…" : EM_DASH],
      ]
    : [["File", EM_DASH]];

  const targetRows = [
    [uc.target.label || "Target column", dash(run.target)],
    ["Definition", dash(uc.target.definition)],
    ["Label source", dash(uc.target.label_source)],
  ];

  const transformSteps = ((prepare && prepare.transforms) || []).map((t) => [
    humanise(t.kind),
    t.columns && t.columns.length ? t.columns.join(", ") : describeParameters(t.parameters),
  ]);

  const trainPart = ((split && split.parts) || []).find((p) => p.name === "train");
  const qualityRows = [
    [
      "Class balance",
      trainPart && present(trainPart.positive_rate) ? `${fmtPct(trainPart.positive_rate)} positive` : EM_DASH,
    ],
    [
      "Train / val / test",
      split ? (split.parts || []).map((p) => fmtNum(p.share * 100, 0)).join(" / ") : EM_DASH,
    ],
    ["Leakage check", leakageLine(validation, config)],
  ];

  const byName = new Map(((profile && profile.columns) || []).map((c) => [c.name, c]));
  const transformsFor = (name) =>
    ((prepare && prepare.transforms) || [])
      .filter((t) => (t.columns || []).includes(name))
      .map((t) => humanise(t.kind))
      .join(", ") || EM_DASH;
  const features = (prepare && prepare.feature_columns) || [];
  const featureRows = features.map((name) => {
    const column = byName.get(name);
    return [
      name,
      column ? humanise(column.inferred_type) : EM_DASH,
      column ? `${fmtNum(column.null_rate * 100, 2)}%` : EM_DASH,
      transformsFor(name),
    ];
  });

  const dropped = (prepare && prepare.dropped_columns) || [];
  const removals = (prepare && prepare.row_removals) || [];
  // Kept with the rows but never trained on - the as-of date (DEC-092). Not a dropped column, but
  // a column the user uploaded that is not a feature has to say why somewhere.
  const carried = (prepare && prepare.carried_columns) || [];
  const carriedCard = carried.length
    ? `<section class="card"><h3>Columns kept, not trained on</h3>${table(
        ["column", "reason", "detail"],
        carried.map((c) => [c.name, humanise(c.reason), c.detail]),
      )}</section>`
    : "";
  const droppedCard = dropped.length
    ? `<section class="card"><h3>Columns dropped</h3>${table(
        ["column", "reason", "detail"],
        dropped.map((d) => [d.name, humanise(d.reason), d.detail]),
      )}</section>`
    : "";
  const removalCard = removals.length
    ? `<section class="card"><h3>Rows removed</h3>${table(
        ["reason", "rows"],
        removals.map((r) => [humanise(r.reason), fmtInt(r.rows)]),
      )}</section>`
    : "";

  return `${tiles}
    ${lineageCard(run, extra)}
    <div class="row">
      <section class="card"><h3>Input dataset</h3>${kvs(fileRows).replace(
        'class="kv"',
        'class="kv" style="border-top:0"',
      )}<h4>Target &amp; labelling</h4>${kvs(targetRows).replace(
        'class="kv"',
        'class="kv" style="border-top:0"',
      )}</section>
      <section class="card"><h3>Data pipeline</h3>${
        transformSteps.length
          ? steps(transformSteps)
          : `<div class="empty">This run applied no transforms.</div>`
      }<h4>Data quality</h4>${kvs(qualityRows).replace(
        'class="kv"',
        'class="kv" style="border-top:0"',
      )}</section>
    </div>
    <section class="card"><h3>Feature set (${features.length} of ${dash(
      profile && profile.column_count,
      String,
    )} columns)</h3>${
      featureRows.length
        ? table(["feature", "type", "missing", "transform"], featureRows)
        : `<div class="empty">This run has not produced ${esc("prepare.json")} yet.</div>`
    }</section>
    ${droppedCard}${carriedCard}${removalCard}`;
}

function describeParameters(parameters) {
  const entries = Object.entries(parameters || {});
  if (!entries.length) return EM_DASH;
  return entries.map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ");
}

// --- Model -------------------------------------------------------------------------------------

function modelPage(uc, run, art, byPath) {
  const best = art["best_model.json"];
  const evaluation = art["evaluation.json"];
  const baseline = art["baseline.json"];
  const importance = art["feature_importance.json"];
  const matrix = art["confusion_matrix.json"];
  const board = art["leaderboard.json"];
  const fairness = art["fairness.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;

  const metrics = (evaluation && evaluation.metrics) || [];
  const second = metrics.find((m) => m.id !== (evaluation && evaluation.primary_metric));
  const tiles = kpis([
    ["Algorithm", dash(best && best.display_name)],
    [
      dash(evaluation && evaluation.primary_metric_label),
      dash(evaluation && evaluation.headline_score, (v) => fmtNum(v, 4)),
    ],
    [dash(second && second.label), dash(second && second.value, (v) => fmtNum(v, 4))],
    ["Last trained", dash(run.finished_at, fmtStamp)],
  ]);

  const setupRows = [
    ["Problem type", dash(uc.problem_type_label)],
    ["Target", dash(run.target)],
    ["Algorithm", dash(best && best.display_name)],
    ["Hyperparameters", dash(best && best.hyperparameters_summary)],
    ["Validation", config ? `${readPath(config, "model_search.folds")}-fold cross-validation` : EM_DASH],
    [
      "Class imbalance",
      config
        ? choiceLabel(byPath, "model_search.imbalance", readPath(config, "model_search.imbalance"))
        : EM_DASH,
    ],
    [
      "Search strategy",
      config
        ? choiceLabel(byPath, "model_search.strategy", readPath(config, "model_search.strategy"))
        : EM_DASH,
    ],
    ["Calibration", dash(evaluation && evaluation.calibration && evaluation.calibration.method, humanise)],
    ["Decision threshold", dash(evaluation && evaluation.threshold_detail)],
    ["Training rows", dash(best && best.training_rows, fmtInt)],
    ["Engine", `Marketing AI ${run.engine_version}`],
  ];

  const evalTitle = "Evaluation (hold-out test set)";
  const evalCard = baseline
    ? table(
        ["metric", "model", baseline.baseline_name],
        (baseline.rows || []).map((r) => [r.label, fmtNum(r.model_value, 4), fmtNum(r.baseline_value, 4)]),
        "eval",
      )
    : evaluation
      ? table(
          ["metric", "model", "baseline"],
          metrics.map((m) => [m.label, fmtNum(m.value, 4), EM_DASH]),
          "eval",
        )
      : `<div class="empty">This run has not produced evaluation.json yet.</div>`;

  const bars = importanceBars(importance);
  const importanceTitle = importance ? `Feature importance (${importance.method})` : "Feature importance";

  const matrixCard = matrix
    ? `<div class="cm">
          <span></span><span class="hd">Predicted +</span><span class="hd">Predicted −</span>
          <span class="rl">Actual +</span><div class="cell good"><b>${fmtInt(
            matrix.true_positive,
          )}</b><small>True positive</small></div><div class="cell"><b>${fmtInt(
            matrix.false_negative,
          )}</b><small>False negative</small></div>
          <span class="rl">Actual −</span><div class="cell"><b>${fmtInt(
            matrix.false_positive,
          )}</b><small>False positive</small></div><div class="cell good"><b>${fmtInt(
            matrix.true_negative,
          )}</b><small>True negative</small></div>
        </div><p class="caption">Test set, decision threshold ${fmtNum(matrix.threshold, 4)}</p>`
    : `<div class="empty">This run has not produced confusion_matrix.json yet.</div>`;

  const boardCard = board
    ? `<section class="card"><h3>Model search (${board.models_trained} models, ${
        board.metric_label
      })</h3>${table(
        ["rank", "model", "validation", "test", "fit (s)"],
        (board.entries || []).map((e) => [
          String(e.rank),
          e.family_label || e.model_name,
          fmtNum(e.validation_score, 4),
          dash(e.test_score, (v) => fmtNum(v, 4)),
          fmtNum(e.fit_time_seconds, 2),
        ]),
      )}<p class="caption">Time limit ${board.time_limit_seconds}s · presets ${esc(
        board.presets,
      )}</p></section>`
    : "";

  const fairnessCard =
    fairness && fairness.evaluated
      ? `<section class="card"><h3>Fairness by ${esc(fairness.column)}</h3>${table(
          ["group", "rows", "positive rate", "recall", "precision"],
          (fairness.groups || []).map((g) => [
            g.value,
            fmtInt(g.rows),
            fmtPct(g.positive_rate),
            dash(g.recall, (v) => fmtNum(v, 3)),
            dash(g.precision, (v) => fmtNum(v, 3)),
          ]),
        )}<p class="caption">${esc(fairness.note)}</p></section>`
      : fairness
        ? `<section class="card"><h3>Fairness</h3><div class="empty">${esc(
            fairness.reason_not_evaluated || fairness.note,
          )}</div></section>`
        : "";

  return `${tiles}
    <div class="row rev">
      <section class="card"><h3>Training setup</h3>${kvs(setupRows)}</section>
      <section class="card"><h3>${evalTitle}</h3>${evalCard}</section>
    </div>
    <div class="row">
      <section class="card"><h3>${esc(importanceTitle)}</h3>${bars}</section>
      <section class="card"><h3>Confusion matrix</h3>${matrixCard}</section>
    </div>
    ${boardCard}${fairnessCard}`;
}

/** Feature importance as the Model page draws it, for both kinds of run. */
function importanceBars(importance) {
  const items = (importance && importance.items) || [];
  const maxShare = items.length ? Math.max(...items.map((i) => i.share_pct)) : 0;
  return items.length
    ? `<div class="bars">${items
        .map(
          (item) =>
            `<div class="brow"><span class="lab">${esc(item.feature)}</span>${barTrack(
              maxShare ? (100 * item.share_pct) / maxShare : 0,
              `${item.feature} ${fmtNum(item.share_pct, 1)} percent`,
            )}<span class="pct">${fmtNum(item.share_pct, 1)}%</span></div>`,
        )
        .join("")}</div><p class="caption">${esc(importance.caption)}</p>`
    : `<div class="empty">This run has not produced feature_importance.json yet.</div>`;
}

// --- Output ------------------------------------------------------------------------------------

function outputPage(uc, run, art, byPath, scoresHref) {
  const lift = art["decile_lift.json"];
  const summary = art["scoring_summary.json"];
  const drift = art["drift.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;

  const tiles = kpis([
    [uc.output.kpi.label, dash(summary && summary.kpi && summary.kpi.display)],
    [
      "Lift (top decile)",
      lift && lift.values && lift.values.length ? `${fmtNum(lift.values[0], 2)}${lift.unit}` : EM_DASH,
    ],
    ["Rows scored", dash(summary && summary.rows_scored, fmtInt)],
    ["Control group", dash(summary && summary.control_group_rows, fmtInt)],
  ]);

  const bins = (lift && lift.bins) || [];
  const values = (lift && lift.values) || [];
  const maxValue = values.length ? Math.max(...values) : 0;
  const chart = values.length
    ? `<div class="vchart">${values
        .map((v, i) => {
          const bin = bins[i] || {};
          const top = i < 3;
          return `<div class="vcol ${top ? "top" : ""}"><span class="bv">${fmtNum(v, 2)}${esc(
            lift.unit,
          )}</span>${columnBar(
            maxValue ? (160 * v) / maxValue : 0,
            top,
            `${bin.label || ""} ${fmtNum(v, 2)}${lift.unit}`,
          )}<span class="bl">${esc(bin.label || "")}</span></div>`;
        })
        .join("")}</div><p class="caption">${esc(lift.caption)}</p>`
    : `<div class="empty">This run has not produced decile_lift.json yet.</div>`;
  const chartTitle = lift
    ? `${lift.unit === "x" ? "Lift" : "Actual rate"} by score decile`
    : "Lift by score decile";

  const bandRows = ((config && readPath(config, "actions.bands")) || []).map(
    (b) => `${b.name} ≥ ${fmtNum(b.min_score, 2)} → ${b.action}`,
  );
  const monitoringRows = config
    ? [
        ["Score field", readPath(config, "actions.score_field")],
        ["Bands", bandRows.length ? bandRows.join(" · ") : EM_DASH],
        ["Control group", `${fmtNum(readPath(config, "actions.control_group_fraction") * 100, 2)}%`],
        [
          "Suppression",
          [
            readPath(config, "actions.suppression.suppress_opted_out") ? "opted out" : null,
            readPath(config, "actions.suppression.suppress_recently_contacted")
              ? `contacted < ${readPath(config, "actions.suppression.recently_contacted_days")}d`
              : null,
          ]
            .filter(Boolean)
            .join(" · ") || "None",
        ],
        [
          "Reasons",
          // A summary written before DEC-056 carries no such field. Absent is unknown, not zero:
          // reporting "measured on every row" for it would be a claim nothing measured.
          !summary || summary.rows_with_fallback_reasons === undefined
            ? EM_DASH
            : summary.rows_with_fallback_reasons
              ? `${fmtInt(summary.rows_with_fallback_reasons)} of ${fmtInt(
                  summary.rows_scored,
                )} rows on fallback reasons`
              : "measured on every row",
        ],
        ["Drift alert", `PSI above ${readPath(config, "monitoring.drift_psi_threshold")}`],
        [
          "Drift measured",
          drift ? `${humanise(drift.status)} · max PSI ${fmtNum(drift.max_psi, 3)}` : EM_DASH,
        ],
        [
          "Retraining",
          choiceLabel(byPath, "monitoring.retraining", readPath(config, "monitoring.retraining")),
        ],
        ["Performance alert", `${readPath(config, "monitoring.performance_alert_drop_pct")}% drop`],
        ["Data retention", `${readPath(config, "governance.retention_days")} days`],
      ]
    : [["Score field", EM_DASH]];

  const suppressionCard = summary
    ? `<section class="card"><h3>Bands &amp; actions</h3>${table(
        ["band", "action", "rows", "share"],
        (summary.bands || []).map((b) => [b.name, b.action, fmtInt(b.rows), `${fmtNum(b.share_pct, 1)}%`]),
        "",
        0,
      )}<h4>Suppressed</h4>${
        (summary.suppressed || []).length
          ? table(
              ["reason", "rows"],
              summary.suppressed.map((s) => [humanise(s.reason), fmtInt(s.rows)]),
            )
          : `<div class="empty">No rows were suppressed.</div>`
      }</section>`
    : "";

  const sample = (summary && summary.sample_rows) || [];
  const sampleTable = sample.length
    ? table(
        [keyText(run.primary_key), summary.score_field, "band", "top reason", "action"],
        sample.map((row) => [
          row.primary_key,
          fmtNum(row.score, 4),
          row.band,
          (row.reasons || []).length ? row.reasons[0].text || row.reasons[0].feature : EM_DASH,
          row.suppressed_reason ? `Suppressed (${humanise(row.suppressed_reason)})` : row.action,
        ]),
        "",
        2,
      )
    : `<div class="empty">${
        run.mode === "train"
          ? "Scored rows come from a scoring run. Switch this use case to “Score new data” and run it against this model."
          : "This run has not produced scoring_summary.json yet."
      }</div>`;
  const downloadLink = sample.length
    ? `<p class="caption"><a class="linkbtn" href="${esc(scoresHref)}">Download all scored rows (CSV)</a></p>`
    : "";

  return `${tiles}
    <div class="row">
      <section class="card"><h3>${esc(chartTitle)}</h3>${chart}</section>
      <section class="card"><h3>Actions &amp; monitoring</h3>${kvs(
        monitoringRows.map(([k, v]) => [k, present(v) ? String(v) : EM_DASH]),
      )}</section>
    </div>
    ${suppressionCard}
    <section class="card"><h3>Prediction output (sample rows)</h3>${sampleTable}${downloadLink}</section>`;
}

// --- Uplift runs (M53) ----------------------------------------------------------------------------
//
// The same three tabs for a run whose problem type is uplift: treatment and control instead of the
// feature pipeline, the Qini curve and AUUC instead of ROC and lift, segments instead of bands. Every
// figure is read from an uplift artefact; a missing one is an em dash or a card that says so.

const NOT_CAUSAL =
  "Not causal: the treatment was not randomly assigned, so these numbers describe who was contacted, not what contacting them changed.";

const fmtShare = (v) => dash(v, (x) => fmtPct(x, 1));

/** A `ConfidenceValue` as "0.0123 (95% CI 0.0041 to 0.0205)"; a missing interval stays visible. */
function fmtConfidence(cv, places = 4) {
  if (!cv || !present(cv.value)) return EM_DASH;
  const interval =
    present(cv.ci_low) && present(cv.ci_high)
      ? `${fmtNum(cv.ci_low, places)} to ${fmtNum(cv.ci_high, places)}`
      : EM_DASH;
  const level = present(cv.confidence_level) ? `${fmtNum(cv.confidence_level * 100, 0)}% CI` : "CI";
  return `${fmtNum(cv.value, places)} (${level} ${interval})`;
}

/** Treated or control: in customers and rows for a two-column key, in rows otherwise. */
function armLine(report, arm) {
  if (!report || !present(report[`${arm}_rows`])) return EM_DASH;
  const rows = fmtInt(report[`${arm}_rows`]);
  const entities = report[`${arm}_entities`];
  return present(entities) ? `${fmtInt(entities)} customers (${rows} rows)` : `${rows} rows`;
}

/** The arm count a tile shows: customers for a two-column key, rows otherwise. */
function armCount(report, arm) {
  if (!report) return EM_DASH;
  return present(report[`${arm}_entities`])
    ? fmtInt(report[`${arm}_entities`])
    : dash(report[`${arm}_rows`], fmtInt);
}

function treatedShare(report) {
  if (!report || !present(report.treated_rows) || !present(report.control_rows)) return EM_DASH;
  const total = report.treated_rows + report.control_rows;
  return total ? fmtPct(report.treated_rows / total) : EM_DASH;
}

/** The randomness check in words: its AUC against the limit, and whether a failure was acknowledged. */
function randomnessLine(report, config) {
  if (!report) return EM_DASH;
  const limit = config ? readPath(config, "uplift.randomness_auc_max") : null;
  const finding = (report.checks || []).find((c) => c.code === "TREATMENT_NOT_RANDOM");
  if (!present(report.randomness_auc)) return finding ? "Failed" : "Not measured";
  const auc = `AUC ${fmtNum(report.randomness_auc, 2)}${present(limit) ? `, limit ${fmtNum(limit, 2)}` : ""}`;
  if (!finding) return `Passed (${auc})`;
  return finding.acknowledged ? `Targeted, acknowledged (${auc})` : `Targeted (${auc})`;
}

function driftRows(drift) {
  const t = drift.treatment || {};
  const features = drift.features
    ? `${humanise(drift.features.status)} · max PSI ${fmtNum(drift.features.max_psi, 3)}`
    : dash(drift.features_reason);
  const share =
    t.status === "not_applicable"
      ? dash(t.reason)
      : `${fmtShare(t.current_treated_share)} now, ${fmtShare(t.training_treated_share)} in training · ${humanise(
          t.status,
        )} (tolerance ${fmtNum(t.tolerance, 2)})`;
  return [
    ["Feature drift", features],
    ["Treated share", share],
  ];
}

/** `uplift_drift.json`, on a scoring run's Data and Output pages; nothing on a training run. */
function driftCard(drift, scoring) {
  if (!scoring && !drift) return "";
  const drifted = drift && drift.features ? drift.features.drifted_features || [] : [];
  const body = drift
    ? `${kvs(driftRows(drift))}${
        drifted.length ? `<p class="caption">Drifted: ${esc(drifted.join(", "))}</p>` : ""
      }`
    : `<div class="empty">This run has not produced uplift_drift.json yet.</div>`;
  return `<section class="card"><h3>Drift against the training data</h3>${body}</section>`;
}

function upliftDataPage(uc, run, art, byPath, extra) {
  const profile = art["profile.json"];
  const split = art["split.json"];
  const uv = art["uplift_validation.json"];
  const drift = art["uplift_drift.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;
  const scoring = run.mode !== "train";

  const tiles = kpis([
    ["Rows", dash(profile && profile.row_count, fmtN)],
    ["Treated", armCount(uv, "treated")],
    ["Control", armCount(uv, "control")],
    ["Randomness", uv && present(uv.randomness_auc) ? `AUC ${fmtNum(uv.randomness_auc, 2)}` : EM_DASH],
  ]);

  const fileRows = profile
    ? [
        ["File", profile.file_name],
        ["Rows", fmtInt(profile.row_count)],
        ["Columns", String(profile.column_count)],
        ["Key", keyText(run.primary_key)],
        [uc.target.label || "Outcome column", dash(run.target)],
      ]
    : [["File", EM_DASH]];

  const parts = ((split && split.parts) || []).filter((p) => p.rows);
  const experimentRows = [
    ["Treatment column", dash(uv && uv.treatment_column)],
    ["Treated", armLine(uv, "treated")],
    ["Control", armLine(uv, "control")],
    ["Treated share", treatedShare(uv)],
    ["Randomness check", randomnessLine(uv, config)],
    ["Causal", uv ? (uv.causal ? "Yes" : "No") : EM_DASH],
    ["Rows set aside (outcome not final)", dash(uv && uv.rows_immature, fmtInt)],
    [
      "Train / test",
      parts.length
        ? `${parts.map((p) => `${p.name} ${fmtInt(p.rows)}`).join(" · ")}${
            split.group_column ? ` · grouped by ${split.group_column}` : ""
          }`
        : EM_DASH,
    ],
  ];

  const checks = (uv && uv.checks) || [];
  const checksCard = uv
    ? checks.length
      ? table(
          ["check", "severity", "message"],
          checks.map((c) => [c.code, `${c.severity}${c.acknowledged ? " (acknowledged)" : ""}`, c.message]),
        )
      : `<div class="empty">Every uplift check passed.</div>`
    : `<div class="empty">${
        scoring
          ? "A scoring run is not checked as an experiment; its treated share is compared with training's in the drift card."
          : "This run has not produced uplift_validation.json yet."
      }</div>`;

  return `${tiles}
    ${lineageCard(run, extra)}
    <div class="row">
      <section class="card"><h3>Input dataset</h3>${kvs(fileRows)}</section>
      <section class="card"><h3>Treatment &amp; control</h3>${kvs(experimentRows)}${
        uv && uv.causal === false ? `<p class="caption">${esc(NOT_CAUSAL)}</p>` : ""
      }</section>
    </div>
    <section class="card"><h3>Uplift checks</h3>${checksCard}</section>
    ${driftCard(drift, scoring)}`;
}

/**
 * The Qini curve. The chart is the uplift module's own (`extra.qiniChart`, which `app.js` loads), so
 * both screens draw one picture; without that module the curve's points are listed instead.
 */
function qiniBlock(curve, extra) {
  if (!curve || !(curve.points || []).length) {
    return `<div class="empty">This run has not produced qini_curve.json yet.</div>`;
  }
  if (typeof extra.qiniChart === "function") return extra.qiniChart(curve);
  const tenths = curve.points.filter((p) => Math.abs(p.fraction * 10 - Math.round(p.fraction * 10)) < 1e-9);
  return table(
    ["share targeted", "model", "random"],
    tenths.map((p) => [fmtPct(p.fraction, 0), fmtPct(p.qini, 2), fmtPct(p.random, 2)]),
  );
}

function upliftModelPage(uc, run, art, byPath, extra) {
  const evaluation = art["uplift_evaluation.json"];
  const curve = art["qini_curve.json"];
  const importance = art["feature_importance.json"];
  const cv = (name) => (evaluation && evaluation[name]) || null;

  const tiles = kpis([
    ["Algorithm", dash(run.best_model)],
    ["AUUC", dash(cv("auuc") && cv("auuc").value, (v) => fmtNum(v, 4))],
    ["Qini coefficient", dash(cv("qini_coefficient") && cv("qini_coefficient").value, (v) => fmtNum(v, 4))],
    ["Last trained", dash(run.finished_at, fmtStamp)],
  ]);

  const evalCard = evaluation
    ? `${kvs([
        ["AUUC", fmtConfidence(evaluation.auuc)],
        ["Qini coefficient", fmtConfidence(evaluation.qini_coefficient)],
        ["Average treatment effect", fmtConfidence(evaluation.average_treatment_effect)],
        ...(evaluation.uplift_at || []).map((u) => [`Uplift in the top ${fmtPct(u.fraction, 0)}`, fmtConfidence(u.uplift)]),
        ["Treated (hold-out)", `${fmtInt(evaluation.treated_rows)} rows · rate ${fmtPct(evaluation.treated_rate)}`],
        ["Control (hold-out)", `${fmtInt(evaluation.control_rows)} rows · rate ${fmtPct(evaluation.control_rate)}`],
        ["Measurable uplift", evaluation.measurable_uplift ? "Yes" : "No"],
      ])}<p class="caption">${esc(evaluation.summary)}${evaluation.causal === false ? ` ${esc(NOT_CAUSAL)}` : ""}</p>`
    : `<div class="empty">${
        run.mode === "train"
          ? "This run has not produced uplift_evaluation.json yet."
          : "A scoring run measures nothing on a hold-out; open this model's training run for its Qini curve and AUUC."
      }</div>`;

  const setupRows = [
    ["Problem type", problemTypeLabel(uc, run.problem_type)],
    ["Outcome", dash(run.target)],
    ["Algorithm", dash(run.best_model)],
    ["Bootstrap resamples", dash(evaluation && evaluation.bootstrap_samples, fmtInt)],
    ["Hold-out rows", dash(evaluation && evaluation.rows_evaluated, fmtInt)],
    ["Engine", `Marketing AI ${run.engine_version}`],
  ];
  const importanceTitle = importance ? `Feature importance (${importance.method})` : "Feature importance";

  return `${tiles}
    <div class="row rev">
      <section class="card"><h3>Training setup</h3>${kvs(setupRows)}</section>
      <section class="card"><h3>Evaluation (hold-out test set)</h3>${evalCard}</section>
    </div>
    <div class="row">
      <section class="card"><h3>Qini curve</h3>${qiniBlock(curve, extra)}</section>
      <section class="card"><h3>${esc(importanceTitle)}</h3>${importanceBars(importance)}</section>
    </div>`;
}

function upliftOutputPage(uc, run, art, byPath, scoresHref) {
  const summary = art["scoring_summary.json"];
  const segments = art["segments.json"];
  const policy = art["policy_recommendation.json"];
  const drift = art["uplift_drift.json"];
  const scoring = run.mode !== "train";
  const expected = policy ? policy.expected_incremental_conversions : null;
  const kpi = summary && summary.kpi;

  const tiles = kpis([
    [dash(kpi && kpi.label), dash(kpi && kpi.display)],
    ["Rows scored", dash(summary && summary.rows_scored, fmtInt)],
    ["Control group", dash(summary && summary.control_group_rows, fmtInt)],
    ["Expected extra conversions", dash(expected && expected.value, (v) => fmtNum(v, 1))],
  ]);

  const segmentRows = summary
    ? (summary.bands || []).map((b) => [b.name, b.action, fmtInt(b.rows), `${fmtNum(b.share_pct, 1)}%`])
    : ((segments && segments.segments) || []).map((g) => [g.label, g.action, fmtInt(g.rows), `${fmtNum(g.share_pct, 1)}%`]);
  const segmentNote = segments
    ? `<p class="caption">Computed on the ${segments.computed_on === "test" ? "hold-out" : "scored rows"}.${
        segments.causal === false ? ` ${esc(NOT_CAUSAL)}` : ""
      }</p>`
    : "";
  const segmentCard = segmentRows.length
    ? `${table(["segment", "action", "rows", "share"], segmentRows)}${segmentNote}`
    : `<div class="empty">This run has not produced segments.json yet.</div>`;

  const policyRows = policy
    ? [
        ["Contacts recommended", fmtInt(policy.contacts_recommended)],
        ["Eligible persuadables", fmtInt(policy.eligible_persuadables)],
        ["Why not more", humanise(policy.stop_reason)],
        ["Budget", dash(policy.budget_contacts, fmtInt)],
        ["Predicted extra conversions", fmtNum(policy.predicted_incremental_conversions, 1)],
        ["Expected extra conversions", fmtConfidence(expected, 1)],
      ]
    : [["Contacts recommended", EM_DASH]];

  const sample = (summary && summary.sample_rows) || [];
  const sampleTable = sample.length
    ? table(
        [keyText(run.primary_key), summary.score_field, "segment", "top reason", "action"],
        sample.map((row) => [
          row.primary_key,
          fmtNum(row.score, 4),
          row.band,
          (row.reasons || []).length ? row.reasons[0].text || row.reasons[0].feature : EM_DASH,
          row.suppressed_reason ? `Suppressed (${humanise(row.suppressed_reason)})` : row.action,
        ]),
      )
    : `<div class="empty">${
        scoring
          ? "This run has not produced scoring_summary.json yet."
          : "Scored rows come from a scoring run. Score new data with this model to get a treat list."
      }</div>`;
  const downloadLink = sample.length
    ? `<p class="caption"><a class="linkbtn" href="${esc(scoresHref)}">Download all scored rows (CSV)</a></p>`
    : "";

  return `${tiles}
    <div class="row">
      <section class="card"><h3>Segments &amp; actions</h3>${segmentCard}</section>
      <section class="card"><h3>Targeting recommendation</h3>${kvs(policyRows)}</section>
    </div>
    ${driftCard(drift, scoring)}
    <section class="card"><h3>Treat list (sample rows)</h3>${sampleTable}${downloadLink}</section>`;
}

/** A problem type's label: the use case's own, a Setup choice's, or the id in words. */
export function problemTypeLabel(uc, value) {
  if (!present(value)) return EM_DASH;
  if (uc && value === uc.problem_type && uc.problem_type_label) return uc.problem_type_label;
  const choices = (uc && uc.setup && uc.setup.problem_type_choices) || [];
  const choice = choices.find((c) => c.value === value);
  return choice && choice.label ? choice.label : humanise(value);
}

// --- entry point --------------------------------------------------------------------------------

/**
 * `extra` carries what is not a run artefact: the Data page's `lineage` (or `lineageError`), and for
 * an uplift run's Model page the uplift module's `qiniChart`.
 */
export function renderPage(kind, uc, run, art, scoresHref, extra = {}) {
  const byPath = indexSchema(uc.advanced_settings || { stages: [] });
  const uplift = isUplift(run);
  const body =
    kind === "data"
      ? (uplift ? upliftDataPage : dataPage)(uc, run, art, byPath, extra)
      : kind === "model"
        ? (uplift ? upliftModelPage : modelPage)(uc, run, art, byPath, extra)
        : (uplift ? upliftOutputPage : outputPage)(uc, run, art, byPath, scoresHref);
  return shell(uc, kind, run, body);
}
