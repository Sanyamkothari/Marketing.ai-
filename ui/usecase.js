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

import {
  cancelRun,
  getArtefact,
  getModels,
  getRun,
  getRuns,
  postRun,
  postUpload,
  templateUrl,
  ApiError,
} from "./api.js";
import {
  EM_DASH,
  backLink,
  dash,
  errorBox,
  esc,
  fmtInt,
  fmtN,
  fmtNum,
  fmtSize,
  fmtStamp,
  pageHead,
  stageChip,
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
import { setupSource } from "./modules/router.js";
import { problemTypeLabel } from "./pages.js";

const AUTOML = "__automl__";
const FILE = "file";
const RAW = "raw";
const POLL_MS = 2000;

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
      validation: null,
      acknowledged: [],
      extraOverrides: {},
      submitError: null,
      submitting: false,
      runs: [],
      models: [],
      clientRuns: [],
      view: "setup",
      runId: null,
      detail: null,
    });
  }
  return STATE.get(uc.id);
}

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

function blocker(uc, s) {
  if (s.source === RAW && !s.dataset) {
    return s.mode === "score"
      ? "Build the dataset from this month's tables to continue"
      : "Build the dataset from your tables to continue";
  }
  if (!dataOf(s)) return "Upload a dataset to continue";
  if (!keyColumns(s.pk).length) return "Choose the primary key column";
  if (s.mode === "train" && !s.target) return `Choose the ${uc.setup.target_label.toLowerCase()}`;
  if (s.mode === "score" && !trainedVersions(s).length) return "Train a model first";
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

function previewHtml(s) {
  const profile = profileOf(s);
  if (!profile) return "";
  const names = profile.columns.map((c) => c.name);
  const shown = names.slice(0, 6);
  const more = names.length - shown.length;
  const rows = profile.preview_rows || [];
  const tbl = rows.length
    ? `<div class="tbl-wrap"><table><thead><tr>${shown
        .map((c) => `<th>${esc(c)}</th>`)
        .join("")}${more > 0 ? `<th>+${more} more</th>` : ""}</tr></thead><tbody>${rows
        .map(
          (r) =>
            `<tr>${shown.map((_, j) => `<td>${esc(r[j] ?? "")}</td>`).join("")}${
              more > 0 ? "<td>…</td>" : ""
            }</tr>`,
        )
        .join("")}</tbody></table></div>`
    : "";
  const chips = `<div class="colchips">${names
    .slice(0, 10)
    .map((c) => `<span class="colchip ${c === s.target || c === s.pk ? "t" : ""}">${esc(c)}</span>`)
    .join("")}${names.length > 10 ? `<span class="colchip">+${names.length - 10} more</span>` : ""}</div>`;
  return `<div class="preview"><div class="pv-head"><span><b>${fmtInt(
    profile.row_count,
  )}</b> rows · <b>${profile.column_count}</b> columns</span><span>${esc(
    fmtSize(profile.file_size_bytes),
  )}</span></div>${tbl}${chips}</div>`;
}

/**
 * The built dataset's preview, in the upload preview's shape (prototype `07`): the manifest's own
 * row count and columns, and the first rows of its redacted `sample.json` - never rows made up here.
 */
function datasetPreviewHtml(s) {
  const dataset = datasetOf(s);
  if (!dataset) return "";
  const names = dataset.manifest.columns.map((c) => c.name);
  const shown = names.slice(0, 6);
  const more = names.length - shown.length;
  const rows = (dataset.sample || []).slice(0, 5);
  const tbl = rows.length
    ? `<div class="tbl-wrap"><table><thead><tr>${shown
        .map((c) => `<th>${esc(c)}</th>`)
        .join("")}${more > 0 ? `<th>+${more} more</th>` : ""}</tr></thead><tbody>${rows
        .map(
          (r) =>
            `<tr>${shown.map((c) => `<td>${esc(dash(r[c]))}</td>`).join("")}${more > 0 ? "<td>…</td>" : ""}</tr>`,
        )
        .join("")}</tbody></table></div>`
    : "";
  return `<div class="preview"><div class="pv-head"><span><b>${fmtInt(
    dataset.manifest.n_rows,
  )}</b> rows · <b>${names.length}</b> columns</span><span>${esc(dataset.datasetId)}</span></div>${tbl}</div>`;
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
    }"><span class="pt1">${esc(title)}</span><span class="pt2">${esc(text)}</span>${
      s.source === value ? '<span class="ptag">Selected</span>' : ""
    }</button>`;
  return `<div class="pick" role="group" aria-label="Where the data comes from">${option(
    FILE,
    "Upload a prepared file",
    prepared,
  )}${option(RAW, card.title, card.text)}</div>`;
}

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
      ? `<span style="flex-basis:100%;color:var(--warn)">${esc(
          help ||
            `Metrics and models will switch to ${(chosen ? chosen.label : current).split(" (")[0].toLowerCase()} defaults.`,
        )}</span>`
      : "";
  return `<div class="ptype"><span>Problem type</span><span class="pill">${esc(
    chosen ? chosen.label : current,
  )}</span><span>${manual ? "set manually" : "detected from the target column"}</span>
    <select id="f-ptype" aria-label="Change problem type"><option value="">change…</option>${choices
      .map(
        (c) =>
          `<option value="${esc(c.value)}"${c.value === current || c.enabled === false ? " disabled" : ""}>${esc(
            c.label,
          )}</option>`,
      )
      .join("")}</select>${warn}</div>`;
}

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
          `<button type="button" class="linkbtn" data-fix="open" data-fix-path="${esc(
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
      return `<div class="vitem"><span class="pill ${severity}">${esc(check.code)}</span><div>
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

function runsCard(uc, s) {
  const rows = s.runs.map((run) => {
    const train = run.mode === "train";
    const headline = train
      ? run.state === "done"
        ? dash(run.best_model)
        : stateLabel(run.state)
      : run.state === "done"
        ? `Scored ${dash(run.row_count, fmtInt)} rows`
        : stateLabel(run.state);
    const right = train
      ? run.state === "done"
        ? `${dash(run.headline_metric_label)} ${dash(run.headline_score, (v) => fmtNum(v, 4))}`
        : EM_DASH
      : dash(run.best_model);
    return `<a class="runrow" href="#/uc/${esc(uc.id)}/run/${esc(run.run_id)}">
      <div><div class="r1">${esc(headline)}${run.champion ? '<span class="champ">Champion</span>' : ""}</div>
      <div class="r2">${esc(run.file_name)} · ${esc(fmtStamp(run.created_at))}</div></div>
      <div class="r3"><b>${esc(right)}</b><span style="font-size:12px;color:var(--muted)">${esc(
        `${run.mode} · ${problemTypeLabel(uc, run.problem_type)}`,
      )}</span></div></a>`;
  });
  return `<section class="card"><h3>Previous runs</h3><div class="runs-list">${
    rows.length ? rows.join("") : `<div class="empty">No runs yet.</div>`
  }</div></section>`;
}

const stateLabel = (state) =>
  ({ pending: "Queued", running: "Running…", done: "Done", failed: "Failed", cancelled: "Cancelled" })[
    state
  ] || state;

function setupHtml(uc, s) {
  const copy = modeCopy(uc, s);
  const train = s.mode === "train";
  const why = blocker(uc, s);
  const profile = profileOf(s);
  const dataset = datasetOf(s);
  const columns = columnsFor(uc, s);
  const names = columns.all;
  const extension = setupSource();
  const card = extension ? extension.card(uc, s.mode, extension.context()) : null;
  const raw = s.source === RAW && card;
  const hasData = !!dataOf(s);

  const uploadControl = `<div class="orline"><label class="control file ${
    s.upload ? "has" : ""
  }"><input type="file" id="f-file" class="sr" accept=".csv,.parquet"><span class="fname">${esc(
    profile ? profile.file_name : "Upload CSV or Parquet",
  )}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <a class="linkbtn" href="${esc(
    templateUrl(uc.setup.template_url),
  )}" download>Download template</a></span></div>
    ${s.uploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${s.uploadError ? errorBox(s.uploadError) : ""}
    ${previewHtml(s)}`;
  // The raw-tables panel is mounted into this element after every paint (`bind`), by the source.
  const rawSlot = `<div id="f-onboarding"></div>${datasetPreviewHtml(s)}`;

  const step1 = `<div class="fstep ${hasData ? "done" : ""}"><div class="stepno">1</div><div>
    <div class="flabel">Dataset</div><div class="fhint">${esc(copy.dataset_hint)}</div>
    ${sourcePickHtml(uc, s, card)}
    ${raw ? rawSlot : uploadControl}</div></div>`;

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
  const step2 = `<div class="fstep ${hasData ? "" : "locked"} ${
    keyColumns(s.pk).length && (!train || s.target) ? "done" : ""
  }"><div class="stepno">2</div><div>
    <div class="flabel">Columns</div><div class="fhint">${esc(copy.columns_hint)}</div>
    <div class="frow"><div class="field"><span class="sub">Primary key</span><div class="control sel"><select id="f-pk">${pkOptions}</select></div></div>
    ${
      train
        ? `<div class="field"><span class="sub">${esc(
            uc.setup.target_label,
          )}</span><div class="control sel"><select id="f-target">${targetOptions}</select></div></div>`
        : ""
    }</div>${problemTypeHtml(uc, s)}</div></div>`;

  const versions = trainedVersions(s);
  const step3 = train
    ? `<div class="fstep ${hasData ? "" : "locked"} done"><div class="stepno">3</div><div>
        <div class="flabel">Model</div><div class="fhint">AutoML tries every selected algorithm and keeps the best. Pick one only if you need to.</div>
        <div class="frow"><div class="field"><div class="control sel"><select id="f-model" aria-label="Model">${optionsOf(
          uc.setup.model_choices,
          s.model,
          null,
        )}</select></div></div></div></div></div>`
    : // Never locked, unlike training's Step 3: the raw-tables card replays the chosen model's
      // recipe, so the model is chosen before there is any data, and a locked select left a model
      // trained on another client's tables with no way to pick a different one (DEC-959).
      `<div class="fstep done"><div class="stepno">3</div><div>
        <div class="flabel">Trained model</div><div class="fhint">The saved model that will score the uploaded rows.</div>
        <div class="frow"><div class="field" style="width:360px"><div class="control sel"><select id="f-scorerun" aria-label="Trained model">${
          versions.length
            ? versions
                .map(
                  (v) =>
                    `<option value="${esc(v.version.model_id)}"${
                      v.version.model_id === s.modelVersionId ? " selected" : ""
                    }>${esc(v.version.model_display_name)} · ${esc(
                      v.version.metric_label,
                    )} ${fmtNum(v.version.test_score, 4)} · ${esc(
                      fmtStamp(v.version.created_at),
                    )}${v.is_champion ? " · Champion" : ""}</option>`,
                )
                .join("")
            : `<option value="">No trained model yet</option>`
        }</select></div></div></div></div></div>`;

  const advanced = train
    ? `<details class="adv-wrap" id="f-adv"${s.advOpen ? " open" : ""}><summary>Advanced settings<span class="n">Each AutoML stage can be configured · defaults work for most data</span></summary>${stagesHtml(
        uc.advanced_settings || { stages: [] },
        s.values,
        columns,
      )}</details>`
    : "";

  return `<div class="setup-grid">
    <section class="card"><div class="form-body">
      <div class="seg" role="group" aria-label="Mode">${uc.setup.modes
        .map(
          (m) =>
            `<button type="button" data-mode="${esc(m.value)}" class="${
              m.value === s.mode ? "on" : ""
            }">${esc(m.label)}</button>`,
        )
        .join("")}</div>
      <p class="seg-help">${esc(copy.help)}</p>
      <form id="f-setup" novalidate style="margin-top:18px">
        ${step1}${step2}${step3}${advanced}
        ${validationHtml(uc, s)}
        ${s.submitError ? errorBox(s.submitError) : ""}
        <div class="actions"><button type="submit" class="run" id="f-run"${
          why || s.submitting ? " disabled" : ""
        }>${esc(s.submitting ? "Starting…" : copy.run_button)}</button><span class="reason">${esc(
          why,
        )}</span></div>
      </form></div></section>
    ${runsCard(uc, s)}
  </div>
  <p class="next">After the run, this page shows the pipeline: Data → Model → Output.</p>`;
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

function runningHtml(uc, s) {
  const status = s.detail && s.detail.status;
  const groups = status ? groupStages(status.stages || []) : [];
  const cls = { running: "active", done: "done", failed: "failed", cancelled: "cancelled", pending: "" };
  const rows = groups.length
    ? groups
        .map(
          (group, i) =>
            `<li class="${cls[group.state]}"><span class="dot">${i + 1}</span><div><div class="pt">${esc(
              group.label,
            )}</div><div class="pd">${esc(group.detail)}</div></div></li>`,
        )
        .join("")
    : (uc.running_rows[s.mode] || [])
        .map(
          (label, i) =>
            `<li><span class="dot">${i + 1}</span><div><div class="pt">${esc(
              label,
            )}</div><div class="pd"></div></div></li>`,
        )
        .join("");
  return `<div class="setup-grid"><section class="card"><h3>Running… <button type="button" class="cancel" id="f-cancel">Cancel</button></h3><ol class="progress" id="prog">${rows}</ol></section>${runsCard(
    uc,
    s,
  )}</div>`;
}

// --- Results -------------------------------------------------------------------------------------

function flowBlocks(uc, s, run) {
  const train = run.mode === "train";
  const kpiLabel = uc.output.kpi.label;
  const items = [
    [
      "data",
      "Data",
      run.file_name,
      `${dash(run.row_count, fmtN)} rows · key ${dash(keyLabel(run.primary_key))}${
        train ? ` · target ${dash(run.target)} · ${problemTypeLabel(uc, run.problem_type).toLowerCase()}` : ""
      }`,
    ],
    [
      "model",
      "Model",
      dash(run.best_model),
      train
        ? `${run.model_choice === AUTOML ? "Selected by AutoML · " : ""}${dash(
            run.headline_metric_label,
          )} ${dash(run.headline_score, (v) => fmtNum(v, 4))}`
        : `Champion model · ${dash(run.headline_metric_label)} ${dash(run.headline_score, (v) =>
            fmtNum(v, 4),
          )}`,
    ],
    [
      "output",
      "Output",
      train ? uc.pages.output : `${dash(run.row_count, fmtN)} rows scored`,
      `${kpiLabel}: ${dash(s.kpiDisplay)}`,
    ],
  ];
  const failedBlock =
    run.state === "failed" && run.error && run.error.stage ? STAGE_BLOCK[run.error.stage] : -1;
  return items
    .map(([slug, label, value, meta], i) => {
      let state = '<span class="bstate">✓ Done</span>';
      let extra = "";
      if (failedBlock > -1) {
        if (i === failedBlock) state = '<span class="bstate failed">Failed</span>';
        else if (i > failedBlock) {
          state = '<span class="bstate waiting">Not reached</span>';
          extra = " pending";
        }
      } else if (run.state === "cancelled") {
        state = '<span class="bstate waiting">Cancelled</span>';
      }
      return `${i ? '<div class="arrow" aria-hidden="true">→</div>' : ""}<a class="block${extra}" href="#/uc/${esc(
        uc.id,
      )}/${slug}/${esc(run.run_id)}"><div><div class="lab"><span>${String(i + 1).padStart(
        2,
        "0",
      )}&nbsp;&nbsp;${label}</span>${state}</div><div class="val">${esc(
        value,
      )}</div><div class="meta">${esc(meta)}</div></div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`;
    })
    .join("");
}

function resultsHtml(uc, s) {
  const run = s.detail && s.detail.run;
  if (!run) return `<div class="loading">Loading the run…</div>`;
  const train = run.mode === "train";
  const headline =
    run.state === "done"
      ? `<span class="ok">✓ ${train ? "Training complete" : "Scoring complete"}</span>`
      : run.state === "cancelled"
        ? `<span class="muted">Run cancelled</span>`
        : `<span class="bad">✕ Run failed</span>`;
  const detailLine =
    run.state === "done"
      ? train
        ? `<span>Best model <b>${esc(dash(run.best_model))}</b> · ${esc(
            dash(run.headline_metric_label),
          )} ${esc(dash(run.headline_score, (v) => fmtNum(v, 4)))}</span>`
        : `<span><b>${esc(dash(run.row_count, fmtInt))}</b> rows scored with ${esc(
            dash(run.best_model),
          )}</span>`
      : run.error
        ? `<span>${esc(run.error.message)}</span>`
        : "";
  const version = s.models.find((v) => v.version.model_id === run.model_version_id);
  const championLine =
    train && run.state === "done"
      ? run.champion
        ? `<span class="muted">Set as champion${
            run.beat_previous_champion ? " · beat previous champion" : ""
          }</span>`
        : version
          ? `<span class="muted">${esc(
              version.version.status === "pending_approval"
                ? "Awaiting approval as champion"
                : version.version.status,
            )}</span>`
          : ""
      : "";
  return `<div class="results"><div class="summary">${headline}${detailLine}${championLine}
    <span class="muted">${esc(run.file_name)} · ${esc(
      fmtStamp(run.created_at),
    )}</span><button class="again" id="f-again">Run again / change settings</button></div>
    <span class="cap">AI pipeline</span><div class="flow">${flowBlocks(uc, s, run)}</div>
    <div class="runs-below">${runsCard(uc, s)}</div></div>`;
}

// --- the screen ------------------------------------------------------------------------------------

export function useCaseHtml(uc, s) {
  const body =
    s.view === "running" ? runningHtml(uc, s) : s.view === "results" ? resultsHtml(uc, s) : setupHtml(uc, s);
  return `<main class="screen t-${esc(uc.marker)}">
    ${pageHead(
      `${backLink(uc)}<h1 class="h1">${esc(
        uc.name,
      )}</h1><p class="desc">${esc(uc.description)}</p><div class="chips">${stageChip(
        uc.lifecycle_stage,
      )}${typeChip(uc)}</div>`,
    )}
    ${body}
  </main>`;
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

  /** The Output block's KPI line, read from the run's own scoring summary or left as an em dash. */
  async function loadKpi(run) {
    s.kpiDisplay = null;
    if (!run.artefacts || !run.artefacts["scoring_summary.json"]) return;
    const summary = await getArtefact(run.run_id, "scoring_summary.json");
    s.kpiDisplay = summary && summary.kpi ? summary.kpi.display : null;
  }

  async function loadRun(runId) {
    s.runId = runId;
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
      s.view = "results";
      await loadKpi(s.detail.run);
      await refreshLists();
      rerender();
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

  /**
   * Bring this screen's state in line with the setup source's, before it is painted: a dataset built
   * for one client is not the one to run once the header names another. Called on every route paint.
   */
  function sync() {
    const extension = setupSource();
    if (s.dataset && s.dataset.clientId !== clientId()) {
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
        s.mode = button.dataset.mode;
        s.upload = null;
        // A dataset built for training is not a scoring input, nor the other way round.
        s.source = FILE;
        s.dataset = null;
        s.pk = "";
        s.target = "";
        s.problemType = "";
        s.validation = null;
        s.submitError = null;
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
      s.advOpen = event.target.open;
    });
    root.querySelectorAll(".stage-d").forEach((details) =>
      details.addEventListener("toggle", () => {
        const id = details.dataset.stage;
        s.openStages = details.open
          ? [...new Set([...s.openStages, id])]
          : s.openStages.filter((x) => x !== id);
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
        if (stage) s.openStages = [...new Set([...s.openStages, stage.id])];
        rerender();
      }),
    );
    on("f-setup", "submit", (event) => {
      event.preventDefault();
      if (blocker(uc, s) || s.submitting) return;
      submit();
    });
    on("f-again", "click", () => {
      stop();
      s.view = "setup";
      s.validation = null;
      rerender();
    });
    on("f-cancel", "click", async () => {
      if (!s.runId) return;
      try {
        await cancelRun(s.runId);
        s.detail = await getRun(s.runId);
      } catch (error) {
        s.submitError = error;
      }
      rerender();
    });

    // A rerender replaces the DOM, so reopen whatever the user had expanded.
    root.querySelectorAll(".stage-d").forEach((details) => {
      if (s.openStages.includes(details.dataset.stage)) details.open = true;
    });

    // Last, so none of the queries above reaches into the panel: it binds its own events.
    if (s.view === "setup") mountSource(root);
  }

  return { state: s, bind, refreshLists, loadRun, poll, stop, sync };
}
