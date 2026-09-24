// The use-case screen and its three states: Setup, Running, Results (plan §9.2).
//
// The Setup form is generated: the mode switch, both hints, the target label, the problem-type
// choices, the model list and every one of the advanced settings come from `GET /use-cases/{id}`.
// The only numbers on the Running and Results screens are the ones `GET /runs/{id}` reported.
//
// Step 1 has two sources of data when a phase module registers a second one (Plan A M35): the
// prepared file this screen always took, and whatever the registered setup source offers - today,
// "Build from raw tables". This module never learns what that source is. It asks the registry for a
// card to show, mounts the source into an element when the card is chosen, and takes back one
// payload - `{datasetId, clientId, primaryKey, target, problemType, timeColumn, manifest, sample}` -
// which fills Step 2 exactly as an upload's profile does. Run then posts `dataset_id` instead of
// `upload_id`, and nothing after the `202` knows the difference.
//
// v1 (docs/ui/FOUNDATION.md, WP2): one step at a time - before there is data only Step 1 is drawn,
// the later steps are one line each; Score mode asks for the model first. Results lead with one
// verdict and one primary action; ids, codes and engine wording sit behind "Technical details".

import {
  cancelRun,
  getArtefact,
  getModels,
  getRun,
  getRuns,
  postRun,
  postUpload,
  scoresUrl,
  templateUrl,
  ApiError,
} from "./api.js";
import {
  announceStatus,
  backLink,
  dash,
  emptyState,
  errorBox,
  esc,
  fmtDate,
  fmtInt,
  fmtMetric,
  fmtSize,
  fmtStamp,
  glossaryCode,
  glossaryMetric,
  headActions,
  noticeCard,
  pageHead,
  present,
  sortNote,
  techDetails,
  typeChip,
} from "./dom.js";
import {
  applyControl,
  indexSchema,
  initialValues,
  overridesFrom,
  readPath,
  stagesHtml,
  writePath,
} from "./settings.js";
import { runActionsHtml, setupSource } from "./modules/router.js";
import * as seams from "./modules/router.js";

const AUTOML = "__automl__";
const FILE = "file";
const RAW = "raw";
const POLL_MS = 2000;
const RUNS_SHOWN = 5;

const STATE = new Map();

/** Which block of the Results flow a stage belongs to, using the pipeline's own stage keys. */
const STAGE_BLOCK = {
  ingest: 0,
  validate: 0,
  validate_against_schema: 0,
  prepare: 0,
  split: 0,
  train: 1,
  evaluate: 1,
  explain: 1,
  register: 1,
  predict: 2,
  explain_rows: 2,
  actions: 2,
  export: 2,
};

export function useCaseState(uc) {
  if (!STATE.has(uc.id)) {
    const schema = uc.advanced_settings || { stages: [] };
    const values = initialValues(schema, uc.config);
    values.__ui = { model: AUTOML };
    STATE.set(uc.id, {
      mode: (uc.setup.modes[0] || {}).value || "train",
      source: FILE,
      dataset: null,
      upload: null,
      uploadError: null,
      uploading: false,
      pk: "",
      target: "",
      problemType: "",
      model: AUTOML,
      modelVersionId: "",
      // Score mode's model choice (DEC-959): what the user picked, and for which header client, and
      // the training run they started from this screen - both kept only for this page's lifetime.
      modelPick: null,
      trained: null,
      values,
      datasetSplit: null,
      advOpen: false,
      openStages: [],
      editColumns: false,
      validation: null,
      acknowledged: [],
      extraOverrides: {},
      submitError: null,
      submitting: false,
      cancelAsk: false,
      runGated: false,
      runs: [],
      models: [],
      clientRuns: [],
      clientId: null,
      view: "setup",
      runId: null,
      detail: null,
    });
  }
  return STATE.get(uc.id);
}

// --- words -----------------------------------------------------------------------------------------

const capital = (text) => (text ? `${text[0].toUpperCase()}${text.slice(1)}` : "");

/** "subscribers": the use case's own entity, made plural the way every configured one is. */
const people = (uc) => `${(uc && uc.entity) || "customer"}s`;

/** The Step 2 label for the key: "Subscriber ID column". */
const idLabel = (uc) => `${capital((uc && uc.entity) || "row")} ID column`;

/** The label a problem type has in this use case's own choices ("Classification (yes / no)"). */
function problemTypeLabel(uc, value) {
  if (!present(value)) return "";
  if (value === uc.problem_type && uc.problem_type_label) return uc.problem_type_label;
  const choice = (uc.setup.problem_type_choices || []).find((c) => c.value === value);
  return choice && choice.label ? choice.label : String(value).replace(/_/g, " ");
}

/** The plain half of that label, the part in brackets: "Yes / no", "A number". */
function problemTypePlain(uc, value) {
  const label = problemTypeLabel(uc, value);
  const inner = /\(([^)]+)\)/.exec(label);
  return capital(inner ? inner[1] : label);
}

/** "ROC-AUC 0.955": a run's headline metric, three decimals at most; "" when it has none. */
const metricText = (label, score) => (present(score) && label ? `${label} ${fmtMetric(score)}` : "");

/** The catalogue's plain name of a metric (help.yaml `metrics.<key>.name`), or "". */
function metricName(key) {
  const entry = glossaryMetric(key);
  return (entry && (typeof entry === "string" ? entry : entry.name)) || "";
}

/**
 * The one sentence a finished training run leads with. ROC-AUC reads as what it measures - how often
 * the model puts someone with the outcome above someone without it; any other metric by its value.
 */
function trainVerdict(uc, run) {
  const model = run.best_model || "The model";
  const metric = metricText(run.headline_metric_label, run.headline_score);
  if (!metric) return `${model} is trained.`;
  if (run.headline_metric === "roc_auc") {
    return `${model} ranks ${people(uc)} correctly ${Math.round(Number(run.headline_score) * 100)}% of the time (${metric}).`;
  }
  return `${model} scored ${metric}.`;
}

/** A model version's standing, in words: what it means for the next scores. */
function standing(uc, status, run) {
  switch (status) {
    case "champion":
      return {
        text: `Approved and in use: new ${people(uc)} are scored with this model.${
          run && run.beat_previous_champion ? " It did better than the model used before." : ""
        }`,
      };
    case "pending_approval":
      return {
        text: "Waiting for an Approver: another person must approve this model before it is the one in use. You can already score with it.",
        link: { href: "#/approvals", label: "See what is waiting for approval" },
      };
    case "candidate":
      return {
        text: "Not in use: it was not better than the model in use by enough to replace it. You can still pick it when scoring.",
      };
    case "archived":
      return { text: "Replaced: a newer model is in use." };
    default:
      return null;
  }
}

/** The short tag a model carries in the Score mode list and in Previous runs. */
const STATUS_TAG = { champion: "in use", pending_approval: "waiting for approval" };

/** How a run's data is named for a person: the file, or the dataset built from raw tables. */
const isBuilt = (run) => Boolean(run.dataset_id) && !run.upload_id;
const dataName = (run) => (isBuilt(run) ? "Built dataset" : run.file_name || "Uploaded file");

const stateLabel = (state) =>
  ({ pending: "queued", running: "running", done: "done", failed: "failed", cancelled: "cancelled" })[state] ||
  state;

// --- derived -------------------------------------------------------------------------------------

const profileOf = (s) => (s.source === FILE && s.upload ? s.upload.profile : null);

/** The built dataset Step 2 reads, when Step 1 is on the raw-tables card and one was taken. */
const datasetOf = (s) => (s.source === RAW ? s.dataset : null);

/** What Step 1 has produced for the chosen card: an upload, a built dataset, or nothing yet. */
const dataOf = (s) => (s.source === RAW ? s.dataset : s.upload);

/** The key as a list of columns: an upload's is one, a periodic dataset's is two (DEC-083). */
const keyColumns = (pk) => (Array.isArray(pk) ? pk : pk ? [pk] : []);

/** The key as the engine writes it for a person (`engine.keys.key_label`): `a + b`. */
const keyLabel = (pk) => keyColumns(pk).join(" + ");

function columnsFor(uc, s) {
  const profile = profileOf(s);
  const dataset = datasetOf(s);
  const all = dataset
    ? dataset.manifest.columns.map((c) => c.name)
    : profile
      ? profile.columns.map((c) => c.name)
      : [];
  const keys = keyColumns(s.pk);
  const features = all.filter((c) => !keys.includes(c) && c !== s.target);
  const candidates = dataset
    ? [dataset.timeColumn].filter(Boolean)
    : (profile && profile.time_column_candidates) || [];
  let timeLike = candidates.filter((c) => all.includes(c));
  if (!timeLike.length && uc.time_like_pattern) {
    const pattern = new RegExp(uc.time_like_pattern, "i");
    timeLike = all.filter((c) => pattern.test(c));
  }
  return { all, features, time_like: timeLike.length ? timeLike : all };
}

/** The problem type the uploaded column itself implies; `""` until a target is chosen. */
function detectProblemType(s) {
  const dataset = datasetOf(s);
  if (dataset) return (s.target && dataset.problemType) || "";
  const profile = profileOf(s);
  if (!s.target || !profile) return "";
  const column = profile.columns.find((c) => c.name === s.target);
  if (!column) return "";
  if (column.distinct_count <= 2) return "binary_classification";
  if (column.inferred_type === "integer" || column.inferred_type === "float") return "regression";
  return "binary_classification";
}

const chosenProblemType = (uc, s) => s.problemType || detectProblemType(s) || uc.problem_type;

const modeCopy = (uc, s) => uc.setup.modes.find((m) => m.value === s.mode) || uc.setup.modes[0];

const trainedVersions = (s) => s.models;

/** The newest finished run of this use case, for "See latest results". */
const latestDone = (s) => s.runs.find((run) => run.state === "done") || null;

/**
 * Whether this person may start a run. The top bar's access provider (`registerAccess`) answers when
 * the router offers `canAccess(method, path)`; until it does, the role gate's own verdict is read off
 * the painted Run button (`production/gate.js` marks a control it disabled with `data-pb-gate`, from
 * the same permission table), remembered in `s.runGated`. With neither, everyone may (sign-in off).
 */
function mayRun(s) {
  const can = seams.canAccess;
  if (typeof can !== "function") return !s.runGated;
  try {
    return can("POST", "/runs") !== false;
  } catch {
    return true;
  }
}

/** Why Run is disabled, in the order the steps are drawn: in Score mode the model comes first. */
function blocker(uc, s) {
  if (s.mode === "score" && !trainedVersions(s).length) return "Train a model first";
  if (s.source === RAW && !s.dataset) {
    return s.mode === "score"
      ? "Build the dataset from this month's tables to continue"
      : "Build the dataset from your tables to continue";
  }
  if (!dataOf(s)) return "Upload a dataset to continue";
  if (!keyColumns(s.pk).length) return `Choose the ${idLabel(uc).toLowerCase()}`;
  if (s.mode === "train" && !s.target) return `Choose the ${uc.setup.target_label.toLowerCase()}`;
  return "";
}

/** The `overrides` body: the advanced settings the user moved, plus the fixes they confirmed. */
function overridesFor(uc, s) {
  const overrides = overridesFrom(uc.advanced_settings || { stages: [] }, s.values);
  Object.assign(overrides, s.extraOverrides);
  const chosen = chosenProblemType(uc, s);
  if (chosen && chosen !== uc.problem_type) overrides.problem_type = chosen;
  if (s.acknowledged.length) overrides["validation.acknowledged"] = s.acknowledged.slice();
  return overrides;
}

// --- Setup ---------------------------------------------------------------------------------------

const optionsOf = (choices, current, emptyLabel) =>
  `${emptyLabel ? `<option value="">${esc(emptyLabel)}</option>` : ""}${choices
    .map(
      (c) =>
        `<option value="${esc(c.value)}"${String(c.value) === String(current) ? " selected" : ""}${
          c.enabled === false ? " disabled" : ""
        }>${esc(c.label)}</option>`,
    )
    .join("")}`;

const columnOptions = (names, current, emptyLabel) =>
  optionsOf(
    names.map((n) => ({ value: n, label: n, enabled: true })),
    current,
    emptyLabel,
  );

/** One preview of the rows: at most six columns, every column name behind "Show all N columns". */
function previewTable(names, rows, cell, keys) {
  const shown = names.slice(0, 6);
  const table = rows.length
    ? `<div class="tbl-wrap"><table><thead><tr>${shown.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows
        .map((r) => `<tr>${shown.map((c, j) => `<td>${esc(cell(r, c, j))}</td>`).join("")}</tr>`)
        .join("")}</tbody></table></div>`
    : "";
  const all =
    names.length > shown.length
      ? `<details class="tbl-more pv-cols"><summary>Show all ${fmtInt(names.length)} columns</summary><div class="colchips">${names
          .map((c) => `<span class="colchip${keys.includes(c) ? " t" : ""}">${esc(c)}</span>`)
          .join("")}</div></details>`
      : "";
  return `${table}${all}`;
}

function previewHtml(s) {
  const profile = profileOf(s);
  if (!profile) return "";
  const names = profile.columns.map((c) => c.name);
  const keys = [...keyColumns(s.pk), s.target].filter(Boolean);
  return `<div class="preview"><div class="pv-head"><span><b>${fmtInt(profile.row_count)}</b> rows · <b>${fmtInt(
    profile.column_count,
  )}</b> columns</span><span>File size ${esc(fmtSize(profile.file_size_bytes))}</span></div>${previewTable(
    names,
    profile.preview_rows || [],
    (r, _c, j) => r[j] ?? "",
    keys,
  )}</div>`;
}

/**
 * The built dataset's preview, in the upload preview's shape (prototype `07`): the manifest's own
 * row count and columns, and the first rows of its redacted `sample.json` - never rows made up here.
 */
function datasetPreviewHtml(s) {
  const dataset = datasetOf(s);
  if (!dataset) return "";
  const names = dataset.manifest.columns.map((c) => c.name);
  const keys = [...keyColumns(s.pk), s.target].filter(Boolean);
  return `<div class="preview"><div class="pv-head"><span><b>${fmtInt(dataset.manifest.n_rows)}</b> rows · <b>${fmtInt(
    names.length,
  )}</b> columns</span><span>Built from your tables</span></div>${previewTable(
    names,
    (dataset.sample || []).slice(0, 5),
    (r, c) => dash(r[c]),
    keys,
  )}<div class="pv-tech">${techDetails([["Dataset ID", dataset.datasetId]])}</div></div>`;
}

/** Step 1's two cards, when a setup source is registered; `""` when it is not (the Phase 1 form). */
function sourcePickHtml(uc, s, card) {
  if (!card) return "";
  const entity = uc.entity || "row";
  const prepared =
    s.mode === "train"
      ? `One row per ${entity}, including the outcome column. CSV or Parquet.`
      : `One row per ${entity}. CSV or Parquet.`;
  const option = (value, title, text) =>
    `<button type="button" class="pickcard${s.source === value ? " on" : ""}" data-source="${value}" aria-pressed="${
      s.source === value
    }"><span class="pt1">${esc(title)}</span><span class="pt2">${esc(text)}</span></button>`;
  return `<div class="pick" role="group" aria-label="Where the data comes from">${option(
    FILE,
    "Upload a prepared file",
    prepared,
  )}${option(RAW, card.title, card.text)}</div>`;
}

/**
 * The problem type, inside Advanced settings: what was detected, and a labelled select to change it.
 * Its warning (a later-phase type, or metrics switching) stays next to it.
 */
function problemTypeHtml(uc, s) {
  if (s.mode !== "train" || !s.target) return "";
  const choices = uc.setup.problem_type_choices || [];
  const current = chosenProblemType(uc, s);
  const chosen = choices.find((c) => c.value === current);
  const detected = detectProblemType(s);
  const manual = s.problemType && s.problemType !== detected;
  const help = chosen && chosen.help ? chosen.help : "";
  const changed = current !== uc.problem_type;
  const warn =
    help || changed
      ? `<span class="ptype-warn">${esc(
          help ||
            `Metrics and models will switch to ${(chosen ? chosen.label : current).split(" (")[0].toLowerCase()} defaults.`,
        )}</span>`
      : "";
  const canChange = !datasetOf(s);
  return `<div class="ptype-adv"><div class="ptype"><span>Kind of prediction</span><span class="pill">${esc(
    chosen ? chosen.label : current,
  )}</span><span>${manual ? "set by you" : "worked out from the outcome column"}</span>${
    canChange
      ? `<div class="control sel"><select id="f-ptype" aria-label="Change the kind of prediction"><option value="">Change…</option>${choices
          .map(
            (c) =>
              `<option value="${esc(c.value)}"${c.value === current || c.enabled === false ? " disabled" : ""}>${esc(
                c.label,
              )}</option>`,
          )
          .join("")}</select></div>`
      : ""
  }${warn}</div></div>`;
}

/** The code's plain title from the catalogue (help.yaml `codes`), else the code itself. */
const checkTitle = (code) => {
  const entry = glossaryCode(code);
  return (entry && entry.title) || code;
};

function validationHtml(uc, s) {
  const report = s.validation;
  if (!report) return "";
  const byPath = indexSchema(uc.advanced_settings || { stages: [] });
  const items = (report.checks || [])
    .map((check) => {
      const details = check.details || {};
      const severity = check.severity === "error" ? "bad" : check.severity === "warning" ? "warn" : "ok";
      const controls = [];
      if (details.override_path && details.override_value !== undefined) {
        const field = byPath.get(details.override_path);
        const value = details.override_value;
        const shown = Array.isArray(value) ? value.join(", ") : String(value);
        const on = fixApplied(uc, s, details.override_path, value);
        controls.push(
          `<label><input type="checkbox" data-fix="override" data-fix-path="${esc(
            details.override_path,
          )}" data-fix-value="${esc(JSON.stringify(value))}"${on ? " checked" : ""}> ${esc(
            field ? `${field.label}: ${shown}` : `Set ${details.override_path} to ${shown}`,
          )}</label>`,
        );
      } else if (details.override_path && byPath.has(details.override_path)) {
        const field = byPath.get(details.override_path);
        controls.push(
          `<button type="button" class="btn quiet sm" data-fix="open" data-fix-path="${esc(
            details.override_path,
          )}">Change “${esc(field.label)}” in Advanced settings</button>`,
        );
      }
      if (check.acknowledgeable && details.acknowledge) {
        const on = s.acknowledged.includes(details.acknowledge);
        controls.push(
          `<label><input type="checkbox" data-fix="ack" data-fix-token="${esc(
            details.acknowledge,
          )}"${on ? " checked" : ""}> I confirm this is expected — run anyway</label>`,
        );
      }
      return `<div class="vitem"><span class="pill ${severity}" data-code="${esc(check.code)}">${esc(
        checkTitle(check.code),
      )}</span><div>
        <div class="vmsg">${esc(check.message)}</div>
        ${check.suggestion ? `<div class="vsug">${esc(check.suggestion)}</div>` : ""}
        ${controls.length ? `<div class="vfix">${controls.join(" ")}</div>` : ""}
      </div></div>`;
    })
    .join("");
  const head = `${report.error_count} ${
    report.error_count === 1 ? "problem" : "problems"
  } must be fixed before this data can be used${
    report.warning_count
      ? `, and ${report.warning_count} warning${report.warning_count === 1 ? "" : "s"} were found`
      : ""
  }.`;
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${items}</div>`;
}

function fixApplied(uc, s, path, value) {
  if (path === "problem_type") return s.problemType === value;
  const byPath = indexSchema(uc.advanced_settings || { stages: [] });
  if (byPath.has(path)) {
    const current = readPath(s.values, path);
    if (Array.isArray(value)) return value.every((v) => (current || []).includes(v));
    return current === value;
  }
  const current = s.extraOverrides[path];
  if (Array.isArray(value)) return Array.isArray(current) && value.every((v) => current.includes(v));
  return current === value;
}

function applyFix(uc, s, path, value, on) {
  if (path === "problem_type") {
    s.problemType = on ? value : "";
    return;
  }
  const byPath = indexSchema(uc.advanced_settings || { stages: [] });
  if (byPath.has(path)) {
    if (Array.isArray(value)) {
      const current = readPath(s.values, path) || [];
      const next = on ? [...new Set([...current, ...value])] : current.filter((v) => !value.includes(v));
      writePath(s.values, path, next);
    } else {
      writePath(s.values, path, on ? value : byPath.get(path).default);
    }
    return;
  }
  if (on) s.extraOverrides[path] = value;
  else delete s.extraOverrides[path];
}

// --- Previous runs ---------------------------------------------------------------------------------

/** The runs of the client the top bar names (and those from a prepared file, which name none). */
function runsOfClient(s) {
  if (!s.clientId) return { mine: s.runs, others: 0 };
  const mine = s.runs.filter((run) => !run.client_id || run.client_id === s.clientId);
  return { mine, others: s.runs.length - mine.length };
}

function runRow(uc, s, run) {
  const train = run.mode === "train";
  const done = run.state === "done";
  const version = s.models.find((v) => v.version.model_id === run.model_version_id);
  const headline = done
    ? train
      ? `Trained ${run.best_model || "a model"}`
      : `Scored ${present(run.row_count) ? fmtInt(run.row_count) : ""} rows`.replace("  ", " ")
    : `${train ? "Training" : "Scoring"} ${stateLabel(run.state)}`;
  const inUse = train && done && version && version.version.status === "champion";
  const rows = present(run.row_count) ? ` · ${fmtInt(run.row_count)} rows` : "";
  const data = isBuilt(run) ? `Built dataset${train ? rows : ""}` : `${run.file_name || "Uploaded file"}${train ? rows : ""}`;
  const metric = train && done ? metricText(run.headline_metric_label, run.headline_score) : "";
  const named = metricName(run.headline_metric);
  const right = metric
    ? `<b${named ? ` title="${esc(named)}"` : ""}>${esc(metric)}</b>`
    : !train && done && run.best_model
      ? `<span class="r4">with ${esc(run.best_model)}</span>`
      : "";
  return `<a class="runrow" href="#/uc/${esc(uc.id)}/run/${esc(run.run_id)}">
      <div><div class="r1">${esc(headline)}${inUse ? '<span class="champ">In use</span>' : ""}</div>
      <div class="r2">${esc(data)} · ${esc(fmtStamp(run.created_at))}</div></div>
      <div class="r3">${right}</div></a>`;
}

/** Previous runs: this client's, newest first, five and then "Show all runs"; folded while running. */
function runsCard(uc, s, { folded = false } = {}) {
  const { mine, others } = runsOfClient(s);
  const rows = mine.map((run) => runRow(uc, s, run));
  const list = rows.length
    ? `<div class="runs-list">${rows.slice(0, RUNS_SHOWN).join("")}</div>${
        rows.length > RUNS_SHOWN
          ? `<details class="more-runs"><summary>Show all ${fmtInt(rows.length)} runs</summary><div class="runs-list">${rows
              .slice(RUNS_SHOWN)
              .join("")}</div></details>`
          : ""
      }`
    : `<div class="empty">No runs yet. Your first run will appear here.</div>`;
  const note = others
    ? `<p class="runs-note">${fmtInt(others)} ${others === 1 ? "run" : "runs"} for other clients ${
        others === 1 ? "is" : "are"
      } not shown.</p>`
    : "";
  if (folded) {
    return `<details class="card runs-fold"><summary>Previous runs (${fmtInt(mine.length)})</summary>${list}${note}</details>`;
  }
  return `<section class="card"><h3>Previous runs${rows.length ? sortNote("newest first") : ""}</h3>${list}${note}</section>`;
}

// --- Setup steps -----------------------------------------------------------------------------------

/** A step that has nothing to show yet: its number, its name and what will appear, on one line. */
const laterStep = (n, label, hint) =>
  `<div class="fstep todo"><div class="stepno">${n}</div><div><div class="flabel">${esc(
    label,
  )}</div><div class="fhint">${esc(hint)}</div></div></div>`;

function uploadStep(uc, s, n, card) {
  const copy = modeCopy(uc, s);
  const profile = profileOf(s);
  const raw = s.source === RAW && card;
  const uploadControl = `<div class="orline"><label class="control file ${
    s.upload ? "has" : ""
  }"><input type="file" id="f-file" class="sr" accept=".csv,.parquet"><span class="fname">${esc(
    profile ? profile.file_name : "Upload CSV or Parquet",
  )}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <a class="btn quiet sm" href="${esc(
    templateUrl(uc.setup.template_url),
  )}" download>Download template</a></span></div>
    ${s.uploading ? `<div class="loading" role="status">Reading the file…</div>` : ""}
    ${s.uploadError ? errorBox(s.uploadError) : ""}
    ${previewHtml(s)}`;
  // The raw-tables panel is mounted into this element after every paint (`bind`), by the source.
  const rawSlot = `<div id="f-onboarding"></div>${datasetPreviewHtml(s)}`;
  // With the two cards shown, the prepared-file card already says what the file holds.
  const hint = card ? "" : `<div class="fhint">${esc(copy.dataset_hint)}</div>`;
  return `<div class="fstep ${dataOf(s) ? "done" : ""}"><div class="stepno">${n}</div><div>
    <div class="flabel">Your data</div>${hint}
    ${sourcePickHtml(uc, s, card)}
    ${raw ? rawSlot : uploadControl}</div></div>`;
}

/**
 * Step "Columns" once there is data: what detection matched, in one sentence with "Change"; the two
 * selects behind it (drawn either way - they hold the values). Without a match they show at once.
 */
function columnsStep(uc, s, n) {
  const train = s.mode === "train";
  const dataset = datasetOf(s);
  const names = columnsFor(uc, s).all;
  const matched = keyColumns(s.pk).length && (!train || s.target);
  // A built dataset's key and outcome are the manifest's: its key is the entity key and the
  // snapshot date together, and its outcome is the label the recipe defined. They are shown as the
  // one choice there is, not offered as columns to pick from.
  const pkOptions = dataset
    ? `<option value="${esc(keyLabel(s.pk))}" selected>${esc(keyLabel(s.pk))}</option>`
    : columnOptions(names, s.pk, "Select a column…");
  const targetOptions = dataset
    ? columnOptions([dataset.target].filter(Boolean), s.target, dataset.target ? null : "Select a column…")
    : columnOptions(
        names.filter((c) => c !== s.pk),
        s.target,
        "Select a column…",
      );
  const selects = `<div class="frow"><div class="field"><span class="sub">${esc(
    idLabel(uc),
  )}</span><div class="control sel"><select id="f-pk">${pkOptions}</select></div></div>${
    train
      ? `<div class="field"><span class="sub">${esc(
          uc.setup.target_label,
        )}</span><div class="control sel"><select id="f-target">${targetOptions}</select></div></div>`
      : ""
  }</div>`;
  const open = !matched || s.editColumns;
  const sentence = matched
    ? `<p class="match">We matched <b>${esc(idLabel(uc).replace(/ column$/, ""))}</b> → <code>${esc(
        keyLabel(s.pk),
      )}</code>${
        train ? ` and <b>what to predict</b> → <code>${esc(s.target)}</code>` : ""
      }${dataset ? " from the built dataset." : "."}${
        dataset
          ? ""
          : ` <button type="button" class="btn quiet sm" id="f-cols-change" aria-expanded="${open}" aria-controls="f-colpick">${
              open ? "Done" : "Change"
            }</button>`
      }</p>`
    : `<p class="match">Choose the columns below.</p>`;
  const predicting =
    train && s.target ? `<p class="predicting">Predicting: <b>${esc(problemTypePlain(uc, chosenProblemType(uc, s)))}</b></p>` : "";
  return `<div class="fstep ${matched ? "done" : ""}"><div class="stepno">${n}</div><div>
    <div class="flabel">Columns</div>${sentence}<div class="colpick" id="f-colpick"${open ? "" : " hidden"}>${selects}</div>${predicting}</div></div>`;
}

function modelStep(uc, s, n) {
  return `<div class="fstep done"><div class="stepno">${n}</div><div>
    <div class="flabel">Model</div><div class="fhint">We try every model type and keep the best one. Pick one only if you need to.</div>
    <div class="frow wide"><div class="field"><div class="control sel"><select id="f-model" aria-label="Model">${optionsOf(
      uc.setup.model_choices,
      s.model,
      null,
    )}</select></div></div></div></div></div>`;
}

/** "<model> · trained 24 Sept 2026 · in use": one option of Score mode's model list. */
const modelOption = (v, current) =>
  `<option value="${esc(v.version.model_id)}"${v.version.model_id === current ? " selected" : ""}>${esc(
    [
      v.version.model_display_name,
      `trained ${fmtDate(v.version.created_at)}`,
      v.is_champion ? STATUS_TAG.champion : STATUS_TAG[v.version.status],
    ]
      .filter(Boolean)
      .join(" · "),
  )}</option>`;

/**
 * Score mode's first step. Never locked: the raw-tables card replays the chosen model's recipe, so
 * the model is chosen before there is any data, and a locked select left a model trained on another
 * client's tables with no way to pick a different one (DEC-959).
 */
function scoreModelStep(uc, s, n) {
  const versions = trainedVersions(s);
  return `<div class="fstep done"><div class="stepno">${n}</div><div>
    <div class="flabel">Model</div><div class="fhint">The trained model that scores your ${esc(
      people(uc),
    )}. We start on the one in use, or the newest one trained on this client's data.</div>
    <div class="frow wide"><div class="field"><div class="control sel"><select id="f-scorerun" aria-label="Trained model">${versions
      .map((v) => modelOption(v, s.modelVersionId))
      .join("")}</select></div></div></div></div></div>`;
}

/** Score mode with nothing trained yet: one card and the one way forward. */
const noModelHtml = (uc) =>
  `<div class="nomodel">${emptyState({
    title: "No trained model yet",
    text: `Train a model on past data first, then come back to score new ${people(uc)}.`,
    action: { label: "Train a model", attrs: 'data-goto-mode="train"' },
  })}</div>`;

function setupForm(uc, s) {
  const copy = modeCopy(uc, s);
  const train = s.mode === "train";
  const why = blocker(uc, s);
  const extension = setupSource();
  const card = extension ? extension.card(uc, s.mode, extension.context()) : null;
  const hasData = !!dataOf(s);
  const seg = `<div class="seg" role="group" aria-label="Mode">${uc.setup.modes
    .map(
      (m) =>
        `<button type="button" data-mode="${esc(m.value)}" class="${m.value === s.mode ? "on" : ""}" aria-pressed="${
          m.value === s.mode
        }">${esc(m.label)}</button>`,
    )
    .join("")}</div><p class="seg-help">${esc(copy.help)}</p>`;

  if (!train && !trainedVersions(s).length) {
    return `<section class="card"><div class="form-body">${seg}${noModelHtml(uc)}</div></section>`;
  }

  const steps = train
    ? [
        uploadStep(uc, s, 1, card),
        hasData ? columnsStep(uc, s, 2) : laterStep(2, "Columns", "After upload: we find the ID column and what to predict for you."),
        hasData ? modelStep(uc, s, 3) : laterStep(3, "Model", "The best model is picked automatically. You can choose one after upload."),
      ]
    : [
        scoreModelStep(uc, s, 1),
        uploadStep(uc, s, 2, card),
        hasData ? columnsStep(uc, s, 3) : laterStep(3, "Columns", "After upload: we find the ID column for you."),
      ];

  const advanced = train
    ? `<details class="adv-wrap" id="f-adv"${s.advOpen ? " open" : ""}><summary>Advanced settings<span class="n">Optional · the recommended settings suit most data</span></summary>${problemTypeHtml(
        uc,
        s,
      )}${stagesHtml(uc.advanced_settings || { stages: [] }, s.values, columnsFor(uc, s))}</details>`
    : "";

  return `<section class="card"><div class="form-body">${seg}
      <form id="f-setup" novalidate class="f-setup">
        ${steps.join("")}${advanced}
        ${validationHtml(uc, s)}
        ${s.submitError ? errorBox(s.submitError) : ""}
        <div class="actions"><button type="submit" class="btn primary run" id="f-run"${
          why || s.submitting ? " disabled" : ""
        }>${esc(s.submitting ? "Starting…" : copy.run_button)}</button><span class="reason">${esc(why)}</span></div>
      </form></div></section>`;
}

function setupHtml(uc, s) {
  if (mayRun(s)) return `<div class="setup-grid">${setupForm(uc, s)}${runsCard(uc, s)}</div>`;
  // A role that may not start runs: what it can do first, the (gated) form folded away below.
  const latest = latestDone(s);
  return `<div class="uc-viewer">${noticeCard({
    title: "You can view this use case",
    text: `See this use case's runs and results below. Training and scoring are not available with your role.`,
    action: latest
      ? { label: "See latest results", href: `#/uc/${uc.id}/run/${latest.run_id}`, kind: "primary" }
      : null,
  })}${runsCard(uc, s)}<details class="adv-wrap viewer-form"><summary>Show the setup form</summary>${setupForm(
    uc,
    s,
  )}</details></div>`;
}

// --- Running -------------------------------------------------------------------------------------

/** status.json's stages, collapsed onto the prototype's five rows by their `group_label`. */
export function groupStages(stages) {
  const groups = [];
  const index = new Map();
  for (const stage of stages) {
    if (!index.has(stage.group_label)) {
      index.set(stage.group_label, groups.length);
      groups.push({ label: stage.group_label, stages: [] });
    }
    groups[index.get(stage.group_label)].stages.push(stage);
  }
  return groups.map((group) => {
    const states = group.stages.map((s) => s.state);
    const state = states.includes("failed")
      ? "failed"
      : states.includes("cancelled")
        ? "cancelled"
        : states.includes("running")
          ? "running"
          : states.every((s) => s === "done" || s === "skipped")
            ? "done"
            : "pending";
    const failed = group.stages.find((s) => s.state === "failed");
    const withDetail = group.stages.filter((s) => s.detail);
    const detail =
      failed && failed.error
        ? failed.error.message
        : withDetail.length
          ? withDetail[withDetail.length - 1].detail
          : "";
    return { ...group, state, detail };
  });
}

/** "2 warnings" in a stage's own detail: the one number a Running row shows, in the warning colour. */
function warningsOf(group) {
  return (group.stages || []).reduce((n, stage) => {
    const found = /\b(\d+) warnings?\b/.exec(stage.detail || "");
    return n + (found ? Number(found[1]) : 0);
  }, 0);
}

/** A group's line in words: the step under way, the warnings found, or what went wrong. */
function groupLine(group) {
  if (group.state === "failed") return { text: group.detail || "This step did not finish.", cls: "" };
  if (group.state === "running") {
    const current = (group.stages || []).find((stage) => stage.state === "running");
    return { text: current && current.title ? `${current.title}…` : "Working…", cls: "" };
  }
  const warnings = group.state === "done" ? warningsOf(group) : 0;
  if (warnings) return { text: `${fmtInt(warnings)} warning${warnings === 1 ? "" : "s"} to review`, cls: " warn" };
  return { text: group.state === "done" ? "Done" : "", cls: "" };
}

/**
 * The Running card, shared with the uplift screens (WP5): a title and one plain sentence, the steps
 * with their state, "Cancel run" set apart with a confirm step, and the engine's own wording of each
 * step behind "Technical details".
 *
 * `groups` are `groupStages(status.stages)`; `placeholder` names the rows to draw before the first
 * status arrives. `cancel` is the Cancel control's HTML (`cancelControls`); `listId` is the `<ol>`'s
 * id; `error` is HTML (an errorBox).
 */
export function runningCard({
  title,
  intro = "",
  groups = [],
  placeholder = [],
  cancel = "",
  listId = "",
  error = "",
}) {
  const cls = { running: "active", done: "done", failed: "failed", cancelled: "cancelled", pending: "" };
  const rows = (groups.length ? groups : placeholder.map((label) => ({ label, state: "pending", stages: [] })))
    .map((group, i) => {
      const line = groupLine(group);
      return `<li class="${cls[group.state] || ""}"><span class="dot">${i + 1}</span><div><div class="pt">${esc(
        group.label,
      )}</div><div class="pd${line.cls}">${esc(line.text)}</div></div></li>`;
    })
    .join("");
  const stages = groups.flatMap((group) => group.stages || []).filter((stage) => stage.detail);
  const tech = stages.length
    ? `<details class="tech run-tech"><summary>Technical details</summary><dl class="tech-list">${stages
        .map((stage) => `<div><dt>${esc(stage.title || stage.key)}</dt><dd>${esc(stage.detail)}</dd></div>`)
        .join("")}</dl></details>`
    : "";
  return `<section class="card run-card" aria-busy="true"><div class="run-head"><div><h3>${esc(title)}</h3>${
    intro ? `<p class="run-intro">${esc(intro)}</p>` : ""
  }</div>${cancel ? `<div class="run-cancel">${cancel}</div>` : ""}</div><ol class="progress"${
    listId ? ` id="${esc(listId)}"` : ""
  }>${rows}</ol>${tech}${error}</section>`;
}

/**
 * "Cancel run", and after one click its confirm step: the same id on the confirming button (so a role
 * gate follows it), "Keep running" as `[data-cancel-keep]`. Each screen writes its own id literally.
 */
export const cancelControls = (button, confirm, confirming) =>
  confirming
    ? `<span class="cancel-ask">Stop this run?</span>${confirm}<button type="button" class="btn quiet sm" data-cancel-keep>Keep running</button>`
    : button;

function runningHtml(uc, s) {
  const status = s.detail && s.detail.status;
  const train = s.mode === "train";
  return `<div class="uc-running">${runningCard({
    title: train ? "Training your model…" : `Scoring your ${people(uc)}…`,
    intro: "This usually takes a few minutes. You can leave this page: the run keeps going.",
    groups: status ? groupStages(status.stages || []) : [],
    placeholder: uc.running_rows[s.mode] || [],
    cancel: cancelControls(
      `<button type="button" class="btn danger sm" id="f-cancel">Cancel run</button>`,
      `<button type="button" class="btn danger confirm sm" id="f-cancel" data-confirm>Yes, cancel run</button>`,
      s.cancelAsk,
    ),
    listId: "prog",
    error: s.submitError ? errorBox(s.submitError) : "",
  })}${runsCard(uc, s, { folded: true })}</div>`;
}

// --- Results -------------------------------------------------------------------------------------

function flowBlocks(uc, s, run) {
  const train = run.mode === "train";
  const done = run.state === "done";
  const version = s.models.find((v) => v.version.model_id === run.model_version_id);
  const rows = present(run.row_count) ? fmtInt(run.row_count) : "";
  const trainedOn = version ? `trained ${fmtDate(version.version.created_at)}` : "";
  const items = [
    [
      "data",
      "Data",
      dataName(run),
      [
        rows ? `${rows} rows` : "",
        keyColumns(run.primary_key).length ? `ID ${keyLabel(run.primary_key)}` : "",
        train && run.target ? `predicting ${run.target}` : "",
      ]
        .filter(Boolean)
        .join(" · "),
    ],
    [
      "model",
      "Model",
      // A run that did not finish has no model to name, whatever its record carried over.
      done ? run.best_model || "Model" : train ? "Not trained" : run.best_model || "Model",
      train
        ? done
          ? [metricText(run.headline_metric_label, run.headline_score), run.model_choice === AUTOML ? "picked automatically" : ""]
              .filter(Boolean)
              .join(" · ")
          : ""
        : [version && version.is_champion ? "Approved model" : "Trained model", trainedOn].filter(Boolean).join(" · "),
    ],
    [
      "output",
      "Output",
      train ? uc.pages.output : rows ? `${rows} rows scored` : uc.pages.output,
      train
        ? done
          ? `Who to contact: available after scoring new ${people(uc)} with this model.`
          : ""
        : present(s.kpiDisplay)
          ? `${uc.output.kpi.label}: ${s.kpiDisplay}`
          : "The list of who to contact, with reasons and actions.",
    ],
  ];
  // The block whose stage failed; with no stage named, none of them is known to have finished.
  const failedStage = run.error && run.error.stage in STAGE_BLOCK ? STAGE_BLOCK[run.error.stage] : -1;
  const blocks = items.map(([slug, label, value, meta], i) => {
    let state = "";
    let extra = "";
    if (run.state === "failed") {
      if (i === failedStage) state = '<span class="bstate failed">Failed</span>';
      else if (i > failedStage) {
        state = '<span class="bstate waiting">Not completed</span>';
        extra = " pending";
      }
    } else if (run.state === "cancelled") {
      state = '<span class="bstate waiting">Not completed</span>';
      extra = " pending";
    }
    return `<a class="block${extra}" href="#/uc/${esc(uc.id)}/${slug}/${esc(run.run_id)}"><div><div class="lab"><span>${esc(
      label,
    )}</span>${state}</div><div class="val">${esc(value)}</div>${
      meta ? `<div class="meta">${esc(meta)}</div>` : ""
    }</div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`;
  });
  // After a scoring run: what the phase modules offer next (campaign results, AI copy), as a fourth block.
  const next = !train && done ? runActionsHtml(uc, run) : "";
  if (next) {
    blocks.push(
      `<div class="block next-step"><div><div class="lab"><span>After the campaign</span></div><div class="val">Measure the results</div><div class="meta">Once the campaign has run, add who responded to see what it changed.</div></div><div class="go next-actions">${next}</div></div>`,
    );
  }
  const arrow = '<div class="arrow" aria-hidden="true">→</div>';
  return { html: blocks.join(arrow), count: blocks.length };
}

/** The summary's lines and actions for each way a run can end. */
function outcome(uc, s, run) {
  const train = run.mode === "train";
  const version = s.models.find((v) => v.version.model_id === run.model_version_id);
  if (run.state === "done" && train) {
    const named = metricName(run.headline_metric);
    const standingOf = standing(uc, version ? version.version.status : run.champion ? "champion" : null, run);
    return {
      head: `<span class="ok">✓ Training complete</span> <span class="vt">${esc(trainVerdict(uc, run))}</span>`,
      lines: [
        named ? `<p class="vsub muted">${esc(`${run.headline_metric_label}: ${named}.`)}</p>` : "",
        standingOf
          ? `<p class="vsub">${esc(standingOf.text)}${
              standingOf.link
                ? ` <a class="btn quiet sm" href="${esc(standingOf.link.href)}">${esc(standingOf.link.label)}</a>`
                : ""
            }</p>`
          : "",
      ],
      actions: `<button type="button" class="btn primary" id="f-score-with">Score new ${esc(
        people(uc),
      )} with this model</button><button type="button" class="btn quiet again" id="f-again">Change settings and train again</button>`,
    };
  }
  if (run.state === "done") {
    const kpi = present(s.kpiDisplay) ? ` ${uc.output.kpi.label}: ${s.kpiDisplay}.` : "";
    return {
      head: `<span class="ok">✓ Scoring complete</span> <span class="vt">${esc(
        `${present(run.row_count) ? fmtInt(run.row_count) : "All"} rows scored.${kpi}`,
      )}</span>`,
      lines: [
        run.best_model
          ? `<p class="vsub">${esc(
              `Scored with ${run.best_model}${version ? `, trained ${fmtDate(version.version.created_at)}` : ""}${
                version && version.is_champion ? " (the approved model)" : ""
              }.`,
            )}</p>`
          : "",
      ],
      actions: `<a class="btn primary" href="${esc(scoresUrl(run.run_id))}" download>Download contact list (CSV)</a><a class="btn secondary" href="#/uc/${esc(
        uc.id,
      )}/output/${esc(run.run_id)}">See who to contact</a><button type="button" class="btn quiet again" id="f-again">Score another file</button>`,
    };
  }
  if (run.state === "cancelled") {
    return {
      head: `<span class="muted">Run cancelled</span>`,
      lines: [`<p class="vsub">This run was stopped before it finished. Nothing it started was saved as a model or a list.</p>`],
      actions: `<button type="button" class="btn primary" id="f-again">Back to Setup</button>`,
    };
  }
  const error = run.error || {};
  const entry = error.code ? glossaryCode(error.code) : null;
  return {
    head: `<span class="bad">✕ Run failed</span> <span class="vt">${esc(
      (entry && entry.title) || error.message || "The run did not finish.",
    )}</span>`,
    lines: [
      entry && entry.fix ? `<p class="vsub">${esc(entry.fix)}</p>` : "",
      entry && error.message ? `<p class="vsub muted">${esc(error.message)}</p>` : "",
    ],
    actions: `<button type="button" class="btn primary" id="f-again">Try again</button>`,
  };
}

function resultsHtml(uc, s) {
  const run = s.detail && s.detail.run;
  if (!run) return `<div class="loading" role="status">Loading the run…</div>`;
  const told = outcome(uc, s, run);
  const tech = techDetails([
    ["Run ID", run.run_id],
    [isBuilt(run) ? "Dataset ID" : "File", isBuilt(run) ? run.dataset_id : run.file_name],
    ["Model version", run.model_version_id],
    ["Started", run.created_at ? fmtStamp(run.created_at) : null],
    ["Error code", run.error && run.error.code],
  ]);
  const flow = flowBlocks(uc, s, run);
  return `<div class="results"><section class="summary rsum"><div class="vline">${told.head}</div>${told.lines.join(
    "",
  )}<div class="btn-row">${told.actions}</div>${tech}</section>
    <div class="flow${flow.count > 3 ? " four" : ""}">${flow.html}</div>
    <div class="runs-below">${runsCard(uc, s)}</div></div>`;
}

// --- the screen ------------------------------------------------------------------------------------

export function useCaseHtml(uc, s) {
  ensureStyles();
  const body =
    s.view === "running" ? runningHtml(uc, s) : s.view === "results" ? resultsHtml(uc, s) : setupHtml(uc, s);
  // A related link the phase modules offer for the use case itself (`registerRunAction`, asked with
  // no run): uplift's "target with uplift", for one. Drawn quietly, after the description.
  const related = runActionsHtml(uc, null);
  return `<main class="screen t-${esc(uc.marker)}">
    ${pageHead(
      `${backLink(uc)}<h1 class="h1">${esc(uc.name)}</h1><p class="desc">${esc(
        uc.description,
      )}</p><div class="chips">${typeChip(uc)}</div>${related ? headActions({ related }) : ""}`,
    )}
    ${body}
  </main>`;
}

// --- styles ----------------------------------------------------------------------------------------
// The few rules this screen adds to the shared components, tokens only. They belong in index.html's
// v1 block (docs/ui/FOUNDATION.md); until that file is edited they are added once, here.

const STYLE_ID = "uc-v1-styles";
const STYLES = `
.f-setup{margin-top:16px}
.fstep.todo .stepno{background:var(--soft);color:var(--muted);border:1px solid var(--line)}
.fstep.todo .fhint{margin-bottom:0}
.match,.predicting{margin:4px 0 8px;font-size:13px;color:var(--ink2);line-height:1.5}
.predicting{margin:12px 0 0}
.match code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--ink);overflow-wrap:anywhere}
.colpick{margin-top:8px}
.preview details.tbl-more{padding:8px 12px;border-top:1px solid var(--line)}
.preview .colchips{padding:8px 0 0}
.preview .pv-tech{padding:0 12px 8px}
.ptype-adv{padding:4px 0 16px}
.ptype-adv .ptype{margin-top:0}
.ptype .control{height:32px;width:220px}
.ptype .ptype-warn{flex-basis:100%;color:var(--warn)}
.fstep .frow.wide .field{width:420px;max-width:100%}
.ptype .control select{color:var(--ink);text-decoration:none;font-size:13px;padding:0 32px 0 12px}
.rsum .again{margin-left:0}
.runrow .r3 b{white-space:nowrap}
.nomodel{margin-top:16px;border:1px solid var(--line);border-radius:10px}
.stages.plain .stage-d>summary{grid-template-columns:minmax(0,1fr) auto}
.stages.plain .stage-d>summary .ss{white-space:normal}
.stages.plain .stage-d>summary .sc{display:inline-flex;align-items:center;gap:8px;min-height:24px;color:var(--brand-blue);font-weight:500}
.stages.plain .stage-d>summary .sc::after{content:"";width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:translateY(-2px) rotate(45deg);transition:transform .15s}
.stages.plain .stage-d[open]>summary .sc::after{transform:translateY(2px) rotate(-135deg)}
.stages.plain .stage-cap{margin:0;padding:12px 16px 0;background:var(--soft);font-size:12px;color:var(--muted);line-height:1.45}
.stages.plain .sfields{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px 24px;padding:12px 16px 16px;background:var(--soft)}
.stages.plain .sfields .field,.stages.plain .sfields .field.xs{width:auto;min-width:0}
.stages.plain .sfields .algos{grid-column:1/-1;padding:0;background:none}
.stages.plain .sfields .algos-head{width:100%;font-size:12px;color:var(--muted)}
.stages.plain .sfields:empty{display:none}
.stages.plain .sfields + .stage-cap{padding:0 16px 16px}
.fcap{font-size:12px;color:var(--muted);line-height:1.45}
.algos .fcap{width:100%}
.field.fcheck .check{height:auto;min-height:38px}
details.planned{margin:0 0 16px}
details.planned>summary,details.runs-fold>summary,details.more-runs>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;min-height:24px}
details.planned>summary::-webkit-details-marker,details.runs-fold>summary::-webkit-details-marker,details.more-runs>summary::-webkit-details-marker{display:none}
details.planned>summary{font-size:13px;font-weight:500;color:var(--brand-blue)}
details.planned>summary::before,details.runs-fold>summary::before{content:"";width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(-45deg);transition:transform .15s}
details.planned[open]>summary::before,details.runs-fold[open]>summary::before{transform:rotate(45deg)}
details.planned>.stage-cap{margin:8px 0;font-size:12px;color:var(--muted)}
.run-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:16px 20px;border-bottom:1px solid var(--line)}
.card .run-head h3{padding:0;border:0}
.run-intro{margin:4px 0 0;font-size:13px;color:var(--muted);line-height:1.5}
.run-cancel{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cancel-ask{font-size:13px;color:var(--ink2)}
.progress .pd.warn,.progress li.done .pd.warn{color:var(--warn)}
.run-card details.tech{margin:0 20px 16px}
.uc-running{display:flex;flex-direction:column;gap:24px;margin-top:8px}
details.runs-fold>summary{padding:16px 20px;font-size:14px;font-weight:600;color:var(--ink)}
details.more-runs>summary{padding:12px 20px;border-top:1px solid var(--line);font-size:13px;font-weight:500;color:var(--brand-blue)}
.runs-note{margin:0;padding:12px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.runrow .r3{white-space:normal;flex:none;max-width:40%}
.runrow .r3 .r4{font-size:12px;color:var(--muted)}
.summary.rsum{display:flex;flex-direction:column;align-items:stretch;gap:8px;padding:16px 20px;background:var(--surface)}
.rsum .vline{font-size:20px;font-weight:600;line-height:1.35;color:var(--ink)}
.rsum .vline .ok,.rsum .vline .bad,.rsum .vline .muted{font-weight:600}
.rsum .vline .bad{color:var(--bad)}
.rsum .vsub{margin:0;font-size:14px;color:var(--ink2);line-height:1.5}
.rsum .vsub.muted{font-size:13px;color:var(--muted)}
.rsum .btn-row{margin-top:8px}
.flow.four{grid-template-columns:1fr 40px 1fr 40px 1fr 40px 1fr}
.block.next-step{background:var(--soft)}
.block .next-actions{flex-wrap:wrap;gap:8px 16px;justify-content:flex-start}
.uc-viewer{display:flex;flex-direction:column;gap:24px;margin-top:8px}
.uc-viewer .adv-wrap{border-top:0}
@media (max-width:1100px){.flow.four{grid-template-columns:1fr}.flow.four .arrow{transform:rotate(90deg);height:40px}}
@media (max-width:700px){.run-head{flex-direction:column}.stages.plain .sfields{grid-template-columns:1fr}.ptype .control{width:100%}.runrow{flex-direction:column;align-items:flex-start}.runrow .r3{text-align:left;max-width:none}.rsum .vline{font-size:18px}}
`;

function ensureStyles() {
  if (typeof document === "undefined" || !document.head || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = STYLES;
  document.head.appendChild(style);
}

// --- behaviour -------------------------------------------------------------------------------------

export function createController(uc, rerender) {
  const s = useCaseState(uc);
  let timer = null;

  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
  };

  /** The client picked in the header, when a setup source is registered to have one. */
  function clientId() {
    const extension = setupSource();
    return extension ? extension.context().clientId : null;
  }

  async function refreshLists() {
    const client = clientId();
    s.clientId = client;
    try {
      const [runs, models, clientRuns] = await Promise.all([
        getRuns(uc.id),
        getModels(uc.id),
        client ? getRuns(uc.id, { mode: "train", clientId: client }) : { runs: [] },
      ]);
      s.runs = runs.runs || [];
      s.models = models.versions || [];
      s.clientRuns = clientRuns.runs || [];
      settleModel();
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
  }

  /**
   * The model Score mode starts on (DEC-959), in this order: the one the user just trained here -
   * unless it was built from another client's tables than the header names - then the newest one
   * trained on the header client's tables, then the champion. The champion alone was wrong twice
   * over: a model trained a minute ago on a prepared file waits for approval, so the champion -
   * trained on other columns - refused that same file with SCHEMA_MISMATCH; and a newly created
   * client was offered another client's model, whose recipe the raw-tables card cannot replay.
   * `GET /models` names each version's training run, and `GET /runs?client_id=` that client's runs.
   */
  function defaultModelId(client) {
    const trainedBy = (runIds) => s.models.find((v) => runIds.includes(v.version.run_id));
    const trained =
      s.trained && (!s.trained.clientId || s.trained.clientId === client) && trainedBy([s.trained.runId]);
    const own = client && trainedBy(s.clientRuns.map((run) => run.run_id));
    const chosen = trained || own || s.models.find((v) => v.is_champion) || s.models[0];
    return chosen ? chosen.version.model_id : "";
  }

  /** Keep the user's own pick while it exists and the header client is the one it was made for;
   * otherwise follow the default, which moves as models are trained and clients change. */
  function settleModel() {
    const client = clientId();
    const pick = s.modelPick;
    const kept =
      pick && pick.clientId === client && s.models.some((v) => v.version.model_id === pick.id) ? pick.id : "";
    const next = kept || defaultModelId(client);
    if (next === s.modelVersionId) return;
    s.modelVersionId = next;
    modelChanged();
  }

  /**
   * A different score model makes what the form showed for the last one stale: its validation
   * report (a SCHEMA_MISMATCH box named the old model's columns) and its error. The raw-tables panel
   * replays the chosen model's own recipe, so a different model is a different panel too.
   */
  function modelChanged() {
    if (s.mode !== "score") return;
    s.validation = null;
    s.submitError = null;
    if (s.source === RAW) {
      s.dataset = null;
      resetColumns();
    }
  }

  /** The Output block's KPI line, read from the run's own scoring summary or left out. */
  async function loadKpi(run) {
    s.kpiDisplay = null;
    if (!run.artefacts || !run.artefacts["scoring_summary.json"]) return;
    const summary = await getArtefact(run.run_id, "scoring_summary.json");
    s.kpiDisplay = summary && summary.kpi ? summary.kpi.display : null;
  }

  async function loadRun(runId) {
    s.runId = runId;
    s.cancelAsk = false;
    s.detail = await getRun(runId);
    const run = s.detail.run;
    s.mode = run.mode;
    s.view = run.state === "pending" || run.state === "running" ? "running" : "results";
    await loadKpi(run);
  }

  function poll() {
    stop();
    timer = setInterval(async () => {
      if (!s.runId) return stop();
      try {
        s.detail = await getRun(s.runId);
      } catch (error) {
        stop();
        return;
      }
      const state = s.detail.run.state;
      if (state === "pending" || state === "running") {
        rerender();
        return;
      }
      stop();
      s.cancelAsk = false;
      s.view = "results";
      await loadKpi(s.detail.run);
      await refreshLists();
      rerender();
      const run = s.detail.run;
      announceStatus(
        run.state === "done"
          ? run.mode === "train"
            ? "Training complete"
            : "Scoring complete"
          : run.state === "cancelled"
            ? "Run cancelled"
            : "Run failed",
      );
    }, POLL_MS);
  }

  async function submit() {
    s.submitting = true;
    s.submitError = null;
    rerender();
    const body = {
      use_case: uc.id,
      mode: s.mode,
      primary_key: s.pk,
      overrides: overridesFor(uc, s),
    };
    if (s.source === RAW) {
      // A built dataset carries its own lineage; the run records which one, and whose (DEC-107).
      body.dataset_id = s.dataset.datasetId;
      body.client_id = s.dataset.clientId;
    } else {
      body.upload_id = s.upload.upload_id;
    }
    if (s.mode === "train") {
      body.target = s.target;
      body.model_choice = s.model;
    } else {
      body.model_version_id = s.modelVersionId;
    }
    try {
      const created = await postRun(body);
      if (s.mode === "train") {
        // The model this run registers is the one to score with next, over any earlier pick.
        s.trained = { runId: created.run_id, clientId: body.client_id || null };
        s.modelPick = null;
      }
      s.validation = null;
      s.submitting = false;
      s.cancelAsk = false;
      s.runId = created.run_id;
      s.view = "running";
      s.detail = await getRun(created.run_id);
      s.kpiDisplay = null;
      await refreshLists();
      rerender();
      poll();
    } catch (error) {
      s.submitting = false;
      if (error instanceof ApiError && error.status === 409 && error.body && error.body.validation) {
        s.validation = error.body.validation;
      } else {
        s.submitError = error;
      }
      rerender();
    }
  }

  async function upload(file) {
    s.uploading = true;
    s.uploadError = null;
    s.upload = null;
    s.pk = "";
    s.target = "";
    s.problemType = "";
    s.editColumns = false;
    s.validation = null;
    rerender();
    try {
      const result = await postUpload(file, uc.id, s.mode);
      s.upload = result;
      adoptUpload();
    } catch (error) {
      s.uploadError = error;
    }
    s.uploading = false;
    rerender();
  }

  /** The advanced setting the schema says reads a time column; `undefined` when none does. */
  function timeField() {
    return (uc.advanced_settings.stages || [])
      .flatMap((stage) => stage.fields || [])
      .find((f) => f.column_source === "time_like" && f.widget === "column-select");
  }

  /** The time column Step 2 fills, into whichever advanced setting the schema says reads one. */
  function fillTimeColumn(candidate) {
    const field = timeField();
    if (field && candidate && !readPath(s.values, field.path)) {
      writePath(s.values, field.path, candidate);
    }
  }

  /**
   * A built dataset's time column, and for a periodic one in training the split it needs: made on
   * the snapshot date, as the reference prototype's `useDataset()` sets it. Without the split type
   * the column would be read by nothing, and the rows split at random. The type and its value are
   * the ones the schema shows the time column under (`visible_when`), so no path or value is spelled
   * here. What Step 2 held before is overwritten - a time column left from an earlier upload names a
   * column this dataset does not have - and what was written is remembered for `releaseTimeSplit`.
   */
  function adoptTimeSplit(column) {
    const field = timeField();
    if (!field) return;
    releaseTimeSplit();
    const written = [];
    if (column && s.mode === "train" && field.visible_when) {
      written.push([field.visible_when.path, field.visible_when.equals]);
    }
    written.push([field.path, column || null]);
    for (const [path, value] of written) writePath(s.values, path, value);
    s.datasetSplit = written;
  }

  /** Back to a prepared file: the use case's own starting values, wherever the user left what a
   * dataset wrote untouched. */
  function releaseTimeSplit() {
    const byPath = indexSchema(uc.advanced_settings || { stages: [] });
    for (const [path, value] of s.datasetSplit || []) {
      if (byPath.has(path) && readPath(s.values, path) === value) {
        writePath(s.values, path, byPath.get(path).value);
      }
    }
    s.datasetSplit = null;
  }

  /** Step 2 from an upload's profile: detection proposes the key, the target and the time column. */
  function adoptUpload() {
    const profile = s.upload.profile;
    releaseTimeSplit();
    s.pk = (profile.primary_key_candidates || [])[0] || "";
    s.target = s.mode === "train" ? profile.target_candidate || "" : "";
    fillTimeColumn((profile.time_column_candidates || [])[0]);
  }

  /** Step 2 from a built dataset: its manifest settles the key (both columns), the outcome, the
   * problem type, the time column and a periodic dataset's split - "Use this dataset" fills them all
   * (Plan A M35). */
  function adoptDataset() {
    const dataset = s.dataset;
    const key = keyColumns(dataset.primaryKey);
    s.pk = key.length === 1 ? key[0] : key;
    s.target = s.mode === "train" ? dataset.target || "" : "";
    adoptTimeSplit(dataset.timeColumn);
  }

  /** Step 2 starts again whenever what Step 1 holds changes. */
  function resetColumns() {
    s.pk = "";
    s.target = "";
    s.problemType = "";
    s.editColumns = false;
    s.validation = null;
    s.submitError = null;
  }

  function chooseSource(source) {
    if (s.source === source) return;
    s.source = source;
    resetColumns();
    const data = dataOf(s);
    if (data && source === RAW) adoptDataset();
    else if (data) adoptUpload();
    rerender();
  }

  function datasetReady(payload) {
    s.dataset = payload;
    resetColumns();
    adoptDataset();
    rerender();
  }

  /** Switch Setup to another mode: a file or dataset read for one mode is not the other's input. */
  function switchMode(mode) {
    if (s.mode === mode) return;
    s.mode = mode;
    s.upload = null;
    // A dataset built for training is not a scoring input, nor the other way round.
    s.source = FILE;
    s.dataset = null;
    s.pk = "";
    s.target = "";
    s.problemType = "";
    s.editColumns = false;
    s.validation = null;
    s.submitError = null;
  }

  /**
   * Back to Setup from a run's results. From a run's own address the route is changed too, so a
   * repaint of that address (a client change, a module loading) does not bring the results back.
   */
  function backToSetup() {
    stop();
    s.view = "setup";
    s.validation = null;
    s.cancelAsk = false;
    const setupHash = `#/uc/${encodeURIComponent(uc.id)}`;
    if (typeof window !== "undefined" && window.location.hash !== setupHash && /\/run\//.test(window.location.hash)) {
      window.location.hash = setupHash;
      return;
    }
    rerender();
  }

  /**
   * Bring this screen's state in line with the setup source's, before it is painted: a dataset built
   * for one client is not the one to run once the header names another. Called on every route paint.
   */
  function sync() {
    const extension = setupSource();
    s.clientId = clientId();
    if (s.dataset && s.dataset.clientId !== s.clientId) {
      s.dataset = null;
      if (s.source === RAW) resetColumns();
    }
    if (!extension && s.source === RAW) {
      s.source = FILE;
      resetColumns();
    }
    settleModel();
  }

  /** Mount the setup source's panel into this paint's placeholder, when the raw card is chosen. */
  function mountSource(root) {
    const slot = root.querySelector("#f-onboarding");
    const extension = setupSource();
    if (!slot || !extension) return;
    const modelVersion = (s.models.find((v) => v.version.model_id === s.modelVersionId) || {}).version || null;
    extension.mount(slot, { uc, mode: s.mode, modelVersion, onDatasetReady: datasetReady });
  }

  function bind(root) {
    const $ = (id) => root.querySelector(`#${id}`);
    const on = (id, event, fn) => {
      const el = $(id);
      if (el) el.addEventListener(event, fn);
    };

    root.querySelectorAll(".seg button").forEach((button) =>
      button.addEventListener("click", () => {
        if (s.mode === button.dataset.mode) return;
        switchMode(button.dataset.mode);
        rerender();
      }),
    );
    root.querySelectorAll("[data-goto-mode]").forEach((button) =>
      button.addEventListener("click", () => {
        switchMode(button.dataset.gotoMode);
        rerender();
      }),
    );

    root.querySelectorAll(".pickcard[data-source]").forEach((button) =>
      button.addEventListener("click", () => chooseSource(button.dataset.source)),
    );
    on("f-file", "change", (event) => {
      const file = event.target.files[0];
      if (file) upload(file);
    });
    on("f-cols-change", "click", () => {
      s.editColumns = !s.editColumns;
      rerender();
    });
    on("f-pk", "change", (event) => {
      if (datasetOf(s)) return; // a built dataset's key is its manifest's; there is no other to pick
      s.pk = event.target.value;
      if (s.target === s.pk) s.target = "";
      rerender();
    });
    on("f-target", "change", (event) => {
      s.target = event.target.value;
      s.problemType = "";
      rerender();
    });
    on("f-ptype", "change", (event) => {
      if (event.target.value) s.problemType = event.target.value;
      rerender();
    });
    on("f-model", "change", (event) => {
      s.model = event.target.value;
      s.values.__ui = { model: s.model };
      rerender();
    });
    on("f-scorerun", "change", (event) => {
      s.modelVersionId = event.target.value;
      s.modelPick = { id: s.modelVersionId, clientId: clientId() };
      modelChanged();
      rerender();
    });
    on("f-adv", "toggle", (event) => {
      if (event.target.id === "f-adv") s.advOpen = event.target.open;
    });
    // One stage open at a time: opening one folds the others of its list.
    root.querySelectorAll(".stage-d[data-stage]").forEach((details) =>
      details.addEventListener("toggle", () => {
        const id = details.dataset.stage;
        if (!details.closest(".stages.plain")) return;
        if (details.open) {
          details
            .closest(".stages.plain")
            .querySelectorAll(":scope > .stage-d[open]")
            .forEach((other) => {
              if (other !== details) other.open = false;
            });
          s.openStages = [id];
        } else {
          s.openStages = s.openStages.filter((x) => x !== id);
        }
      }),
    );
    root.querySelectorAll("[data-path]").forEach((element) =>
      element.addEventListener("change", () => {
        applyControl(element, s.values);
        rerender();
      }),
    );
    root.querySelectorAll('[data-fix="override"]').forEach((element) =>
      element.addEventListener("change", () => {
        applyFix(uc, s, element.dataset.fixPath, JSON.parse(element.dataset.fixValue), element.checked);
        rerender();
      }),
    );
    root.querySelectorAll('[data-fix="ack"]').forEach((element) =>
      element.addEventListener("change", () => {
        const token = element.dataset.fixToken;
        s.acknowledged = element.checked
          ? [...new Set([...s.acknowledged, token])]
          : s.acknowledged.filter((x) => x !== token);
        rerender();
      }),
    );
    root.querySelectorAll('[data-fix="open"]').forEach((element) =>
      element.addEventListener("click", () => {
        s.advOpen = true;
        const stage = (uc.advanced_settings.stages || []).find((st) =>
          (st.fields || []).some((f) => f.path === element.dataset.fixPath),
        );
        if (stage) s.openStages = [stage.id];
        rerender();
      }),
    );
    on("f-setup", "submit", (event) => {
      event.preventDefault();
      if (blocker(uc, s) || s.submitting) return;
      submit();
    });
    on("f-again", "click", () => backToSetup());
    on("f-score-with", "click", () => {
      const run = s.detail && s.detail.run;
      switchMode("score");
      if (run && run.model_version_id && s.models.some((v) => v.version.model_id === run.model_version_id)) {
        s.modelVersionId = run.model_version_id;
        s.modelPick = { id: run.model_version_id, clientId: clientId() };
      }
      backToSetup();
    });
    on("f-cancel", "click", async () => {
      if (!s.runId) return;
      if (!s.cancelAsk) {
        s.cancelAsk = true;
        rerender();
        const confirm = document.getElementById("f-cancel");
        if (confirm) confirm.focus();
        return;
      }
      s.cancelAsk = false;
      try {
        await cancelRun(s.runId);
        s.detail = await getRun(s.runId);
      } catch (error) {
        s.submitError = error;
      }
      rerender();
    });
    root.querySelectorAll("[data-cancel-keep]").forEach((button) =>
      button.addEventListener("click", () => {
        s.cancelAsk = false;
        rerender();
      }),
    );

    // A rerender replaces the DOM, so reopen whatever the user had expanded.
    root.querySelectorAll(".stage-d[data-stage]").forEach((details) => {
      if (s.openStages.includes(details.dataset.stage)) details.open = true;
    });

    // Last, so none of the queries above reaches into the panel: it binds its own events.
    if (s.view === "setup") mountSource(root);

    // The role gate disables Run in a microtask after this paint; once it has, a role that may not
    // start runs gets the read-only layout (and the full form again after a sign-in as one that may).
    if (s.view === "setup" && typeof seams.canAccess !== "function") {
      setTimeout(() => {
        const run = root.querySelector("#f-run");
        const gated = Boolean(run && run.dataset.pbGate);
        if (run && gated !== s.runGated && s.view === "setup") {
          s.runGated = gated;
          rerender();
        }
      }, 0);
    }
  }

  return { state: s, bind, refreshLists, loadRun, poll, stop, sync };
}
