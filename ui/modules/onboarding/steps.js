// The four onboarding steps (Phase 2 plan §10, M13): Sources, Mapping, Features & label, Build &
// review. Every function here is pure - `(state) => html` - exactly like `ui/pages.js`'s screens:
// `panel.js` owns the fetches, the working edits and the event wiring; this file only draws whatever
// is already sitting in `state`. Nothing in this file is data - every number, name and confidence
// reaches the page through `dash()`/`esc()`/`fmtInt()`/`fmtPct()`, and the one place a set of
// choices comes from is the schema the API just returned, never a list typed here.
//
// `state` (built and owned by `panel.js`) that this module reads:
//   schema        - GET /use-cases/{id}/standard-schema response, or null before it loads
//   schemaError   - the ApiError that loading it raised, or null
//   open          - id of the one step currently expanded: "sources"|"mapping"|"features"|"build"
//   sources       - [{source: SourceSpec, profile: SourceProfile}], newest last
//   sourcesLoading, sourcesError, uploading (filenames mid-upload)
//   mappings      - sourceId -> MappingSpec (suggested or saved, with the user's live edits)
//   mappingLoading, mappingError, mappingSaving, mappingSaved, mappingChecks (sourceId -> ...)
//   features      - working FeatureDef[] (suggested, filtered to mapped roles, plus any added)
//   featureChecked- name -> bool, which features are ticked into the build
//   featureForm   - the "Add feature" draft the generated form is editing, or null when closed
//   featureSchema - {function: string[], where: string[], namePattern: string} from openapi.json,
//                   or null before it loads; the Add-feature form stays disabled until it has this
//   label         - editable copy of schema.label (only horizon_days differs from it), or null
//   snapshotSchema- {mode, frequency, maxSnapshots: {choices|min|max|default}} from openapi.json
//   snapshot      - editable SnapshotDefinition-shaped object
//   specChecks, previewLoading, previewError, preview (the preview response, or null)
//   datasetId, building, dataset ({manifest, status}), buildError, buildChecks (409 checks), buildReport

import { EM_DASH, barTrack, dash, errorBox, esc, fmtInt, fmtPct, present, table } from "../../dom.js";

const LABEL_TYPE_PHRASE = {
  event_presence: "any activity",
  event_absence: "no activity",
  value_threshold: "a measured value",
  column: "the recorded outcome",
};

// --- small shared pieces -------------------------------------------------------------------------

function humanizeId(id) {
  return String(id)
    .split("_")
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
}

/** The one role whose `kind` is `entity`. Not a wire field: `RoleCatalogue.entity_role` is a Python
 * `@property` and never reaches the JSON the API sends, so every screen that needs it recomputes it
 * from the `roles` dict the same way the server does, rather than guessing at a field that is not
 * there. */
function entityRoleId(schema) {
  const roles = schema.roles.roles;
  return Object.keys(roles).find((id) => roles[id].kind === "entity") || null;
}

/** `usecase.js`'s `groupStages`, recomputed here rather than imported: this module owns nothing
 * outside `ui/modules/onboarding/` and a `BuildStage` is not a Phase 1 run stage - it carries no
 * `error` object, only its own pre-formatted `detail` - so the two are close but not the same shape. */
function groupBuildStages(stages) {
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
    const withDetail = group.stages.filter((s) => s.detail);
    const detail = withDetail.length ? withDetail[withDetail.length - 1].detail : "";
    return { ...group, state, detail };
  });
}

/** The green-tick summary badge every finished step carries; unfinished steps show nothing here. */
const doneBadge = (done) => (done ? '<span class="pill ok">✓ Done</span>' : '<span class="sc">Configure</span>');

function stepShell(id, number, title, hint, done, open, body) {
  return `<details class="stage-d" data-step="${esc(id)}"${open ? " open" : ""}>
    <summary><span class="sn">${number}</span><span><div class="st">${esc(title)}</div><div class="ss">${esc(
      hint,
    )}</div></span>${doneBadge(done)}</summary>
    <div class="frow" style="display:block;padding:4px 16px 20px">${body}</div>
  </details>`;
}

function confidencePill(confidence) {
  if (!present(confidence)) return `<span class="pill">${EM_DASH}</span>`;
  const cls = confidence >= 0.85 ? "ok" : confidence >= 0.5 ? "warn" : "";
  return `<span class="pill ${cls}">${fmtPct(confidence, 0)}</span>`;
}

/** `checks` rendered the same way the Phase 1 Setup form renders a validation report (`.vlist`). */
function checksList(checks) {
  const list = checks || [];
  if (!list.length) return "";
  const items = list
    .map(
      (check) =>
        `<div class="vitem"><span class="pill ${
          check.severity === "error" ? "bad" : check.severity === "warning" ? "warn" : ""
        }">${esc(check.code)}</span><div><div class="vmsg">${esc(check.message)}</div>${
          check.suggestion ? `<div class="vsug">${esc(check.suggestion)}</div>` : ""
        }</div></div>`,
    )
    .join("");
  const errors = list.filter((c) => c.severity === "error").length;
  const head = errors
    ? `${errors} ${errors === 1 ? "problem" : "problems"} must be fixed before this can be built.`
    : "Warnings were found - review before building.";
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${items}</div>`;
}

// ---------------------------------------------------------------------------------------------
// Step 1: Sources
// ---------------------------------------------------------------------------------------------

function roleSelect(state, sourceId, currentRole, candidates) {
  const roles = state.schema.roles.roles;
  const topCandidate = (candidates || [])[0];
  const options = Object.keys(roles)
    .sort()
    .map((id) => {
      const selected = currentRole ? currentRole === id : topCandidate ? topCandidate.role === id : false;
      return `<option value="${esc(id)}"${selected ? " selected" : ""}>${esc(humanizeId(id))}</option>`;
    })
    .join("");
  return `<div class="control sel"><select data-act="set-role" data-source="${esc(
    sourceId,
  )}" aria-label="Role">${options}</select></div>`;
}

function roleHintLine(state) {
  const roles = state.schema.roles.roles;
  const line = Object.keys(roles)
    .sort()
    .map((id) => `${humanizeId(id)}: ${roles[id].description}`)
    .join(" · ");
  return `<p class="fhint">${esc(line)}</p>`;
}

function coverageBadge(entityConfirmed, role, isEntityRole, keyCandidates) {
  if (isEntityRole) return EM_DASH;
  if (!entityConfirmed) return EM_DASH;
  const top = (keyCandidates || [])[0];
  return dash(top && top.coverage, (v) => fmtPct(v, 0));
}

function sourceRow(state, entry, entityConfirmed, entityRole) {
  const { source, profile } = entry;
  const isEntityRole = source.role === entityRole;
  const keyTop = (profile.key_candidates || [])[0];
  const timeTop = (profile.time_candidates || [])[0];
  return `<tr>
    <td>${esc(source.file_name)}</td>
    <td>${dash(source.rows, fmtInt)}</td>
    <td>${roleSelect(state, source.source_id, source.role, profile.role_candidates)}</td>
    <td>${dash(keyTop && keyTop.column)}</td>
    <td>${dash(timeTop && timeTop.column)}</td>
    <td>${coverageBadge(entityConfirmed, source.role, isEntityRole, profile.key_candidates)}</td>
    <td><button type="button" class="linkbtn" data-act="delete-source" data-source="${esc(
      source.source_id,
    )}">Remove</button></td>
  </tr>`;
}

/** Every step past this point reads `state.schema`; none may run before it has loaded, so each one
 * starts with this same guard rather than letting a null dereference reach the page. */
function schemaPending(state, id, number, title, hint) {
  if (state.schema) return null;
  const body = state.schemaError ? errorBox(state.schemaError) : `<div class="loading">Loading the standard schema…</div>`;
  return stepShell(id, number, title, hint, false, state.open === id, body);
}

export function sourcesStep(state) {
  const pending = schemaPending(state, "sources", 1, "Sources", "The client's raw tables, one row per file.");
  if (pending) return pending;
  const entityRole = state.schema && entityRoleId(state.schema);
  const entityConfirmed = state.sources.some((entry) => entry.source.role === entityRole);
  const done = entityConfirmed;
  const rows = state.sources.map((entry) => sourceRow(state, entry, entityConfirmed, entityRole)).join("");
  const uploadingRows = state.uploading
    .map((name) => `<div class="loading">Reading ${esc(name)}…</div>`)
    .join("");
  const body = `
    <div class="orline">
      <label class="control file"><input type="file" class="sr" multiple accept=".csv,.parquet" data-act="pick-files"><span class="fname">Add source files</span><span class="ico" aria-hidden="true">⤒</span></label>
      <span>CSV or Parquet, one file per table</span>
    </div>
    ${uploadingRows}
    ${state.sourcesError ? errorBox(state.sourcesError) : ""}
    ${
      state.sources.length
        ? `<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Rows</th><th>Role</th><th>Key candidate</th><th>Time candidate</th><th>Coverage</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="empty">${state.sourcesLoading ? "Loading sources…" : "No sources yet - add a file above."}</div>`
    }
    ${state.schema ? roleHintLine(state) : ""}
    ${!entityConfirmed && state.sources.length ? `<p class="fhint">One source must be set to the entity role before mapping can start.</p>` : ""}
  `;
  return stepShell("sources", 1, "Sources", "The client's raw tables, one row per file.", done, state.open === "sources", body);
}

// ---------------------------------------------------------------------------------------------
// Step 2: Mapping
// ---------------------------------------------------------------------------------------------

/** Every standard name a source's role may map a column to: the role's required names first. */
function mappingTargets(state, role) {
  const roles = state.schema.roles.roles[role];
  const required = (roles ? roles.required_columns : []).map((name) => ({
    value: name,
    label: `${humanizeId(name)} (required)`,
  }));
  const columns = (state.schema.standard_schema.columns || []).map((column) => ({
    value: column.name,
    label: column.required ? `${column.description || column.name} (required)` : column.description || column.name,
  }));
  return [...required, ...columns];
}

function columnTargetOf(mapping, rawName) {
  const found = (mapping.columns || []).find((c) => c.source === rawName);
  return found ? found.standard : "";
}

function valueMapRow(state, sourceId, standardName, standardColumn, rawValue, current) {
  const aliasTargets = Object.keys(standardColumn.value_aliases || {});
  const options = [
    `<option value="__keep__"${!present(current) ? " selected" : ""}>Keep as-is</option>`,
    `<option value="__null__"${current === null ? " selected" : ""}>Set null</option>`,
    ...aliasTargets.map(
      (target) =>
        `<option value="${esc(target)}"${current === target ? " selected" : ""}>${esc(target)}</option>`,
    ),
  ].join("");
  return `<div class="frow"><span class="colchip">${esc(rawValue)}</span><div class="control sel"><select data-act="set-value-map" data-source="${esc(
    sourceId,
  )}" data-standard="${esc(standardName)}" data-value="${esc(rawValue)}" aria-label="Map ${esc(
    rawValue,
  )}">${options}</select></div></div>`;
}

function valueMapEditor(state, sourceId, standardName, standardColumn, mapping, column) {
  if (!["categorical", "boolean"].includes(standardColumn.type)) return "";
  const known = mapping.value_maps && mapping.value_maps[standardName] ? mapping.value_maps[standardName] : {};
  const values = new Set([...(column.top_categories || []).map((c) => c.value), ...Object.keys(known)]);
  if (!values.size) return "";
  const rows = [...values]
    .map((value) => valueMapRow(state, sourceId, standardName, standardColumn, value, known[value]))
    .join("");
  return `<details class="adv-wrap"><summary>Value map<span class="n">${values.size} value${
    values.size === 1 ? "" : "s"
  }</span></summary>${rows}</details>`;
}

function mappingRow(state, sourceId, mapping, role, column) {
  const targets = mappingTargets(state, role);
  const current = columnTargetOf(mapping, column.name);
  const options = [
    `<option value=""${current === "" ? " selected" : ""}>Not used</option>`,
    ...targets.map(
      (t) => `<option value="${esc(t.value)}"${current === t.value ? " selected" : ""}>${esc(t.label)}</option>`,
    ),
  ].join("");
  const decided = (mapping.columns || []).find((c) => c.source === column.name);
  const standardColumn = (state.schema.standard_schema.columns || []).find((c) => c.name === current);
  const sample = (column.sample_values || []).join(", ");
  return `<tr>
    <td>${esc(column.name)}</td>
    <td><div class="control sel"><select data-act="set-mapping" data-source="${esc(
      sourceId,
    )}" data-column="${esc(column.name)}" aria-label="Standard column for ${esc(column.name)}">${options}</select></div></td>
    <td>${esc(column.inferred_type)}</td>
    <td>${esc(sample) || EM_DASH}</td>
    <td>${confidencePill(decided ? decided.confidence : null)}</td>
    <td>${standardColumn ? valueMapEditor(state, sourceId, current, standardColumn, mapping, column) : ""}</td>
  </tr>`;
}

function mappingTable(state, entry) {
  const { source, profile } = entry;
  const mapping = state.mappings[source.source_id];
  if (state.mappingLoading[source.source_id]) return `<div class="loading">Suggesting a mapping…</div>`;
  if (state.mappingError[source.source_id]) return errorBox(state.mappingError[source.source_id]);
  if (!mapping) return `<div class="empty">Not analysed yet.</div>`;
  const rows = profile.profile.columns.map((column) => mappingRow(state, source.source_id, mapping, source.role, column)).join("");
  const missing = requiredMissing(state, mapping, source.role);
  return `<div class="card"><h3>${esc(source.file_name)}<span style="float:right"><button type="button" class="linkbtn" data-act="accept-all" data-source="${esc(
    source.source_id,
  )}">Accept all suggestions</button></span></h3>
    <div class="tbl-wrap"><table><thead><tr><th>Your column</th><th>Our column</th><th>Type</th><th>Sample values</th><th>Confidence</th><th>Value map</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${
      missing.length
        ? `<div class="vlist" role="alert"><div class="vhead">${missing.length} required column${
            missing.length === 1 ? "" : "s"
          } not yet mapped</div>${missing
            .map(
              (name) =>
                `<div class="vitem"><span class="pill bad">${esc(name)}</span><div class="vmsg">${esc(
                  humanizeId(name),
                )} has no source column yet.</div></div>`,
            )
            .join("")}</div>`
        : ""
    }
    ${checksList(state.mappingChecks[source.source_id])}
    <div class="kv"><span class="k">${state.mappingSaved[source.source_id] ? "Saved" : "Not saved yet"}</span><span class="v"><button type="button" class="linkbtn" data-act="save-mapping" data-source="${esc(
      source.source_id,
    )}"${state.mappingSaving[source.source_id] ? " disabled" : ""}>${
      state.mappingSaving[source.source_id] ? "Saving…" : "Save mapping"
    }</button></span></div>
  </div>`;
}

function columnMissing(mapping, standardName) {
  return !(mapping.columns || []).some((c) => c.standard === standardName);
}

/** Required standard columns still unmapped, recomputed live from the schema on every render so an
 * edit the user just made (not yet saved) updates this the moment it happens, rather than waiting on
 * a round trip to see `mapping.missing_required` catch up. */
function requiredMissing(state, mapping, role) {
  const required = mappingTargets(state, role).filter((t) => t.label.endsWith("(required)"));
  return required.filter((t) => columnMissing(mapping, t.value)).map((t) => t.value);
}

export function mappingStep(state) {
  const pending = schemaPending(state, "mapping", 2, "Mapping", "Your columns, matched to ours.");
  if (pending) return pending;
  const mappable = state.sources.filter((entry) => entry.source.role);
  const done = mappable.length > 0 && mappable.every((entry) => state.mappingSaved[entry.source.source_id]);
  const body = mappable.length
    ? mappable.map((entry) => mappingTable(state, entry)).join("")
    : `<div class="empty">Confirm a role for at least one source first.</div>`;
  return stepShell(
    "mapping",
    2,
    "Mapping",
    "Your columns, matched to ours.",
    done,
    state.open === "mapping",
    body,
  );
}

// ---------------------------------------------------------------------------------------------
// Step 3: Features & label
// ---------------------------------------------------------------------------------------------

function windowLabel(feature) {
  return present(feature.window_days) ? `last ${fmtInt(feature.window_days)} days` : "all history";
}

function featureRow(state, feature) {
  const checked = state.featureChecked[feature.name] !== false;
  return `<label class="check" style="height:auto;align-items:flex-start;padding:8px 0"><input type="checkbox" data-act="toggle-feature" data-feature="${esc(
    feature.name,
  )}"${checked ? " checked" : ""}> <span><b>${esc(feature.name)}</b> — ${esc(
    feature.description || `${feature.function} of ${feature.role}`,
  )} <span class="colchip">${esc(windowLabel(feature))}</span></span></label>`;
}

function featuresByRole(state) {
  const groups = new Map();
  for (const feature of state.features) {
    if (!groups.has(feature.role)) groups.set(feature.role, []);
    groups.get(feature.role).push(feature);
  }
  return [...groups.entries()]
    .map(
      ([role, features]) =>
        `<div class="card"><h4>${esc(humanizeId(role))}</h4>${features
          .map((f) => featureRow(state, f))
          .join("")}</div>`,
    )
    .join("");
}

function addFeatureForm(state) {
  if (!state.featureForm) {
    return `<button type="button" class="linkbtn" data-act="open-add-feature">+ Add a feature</button>`;
  }
  const fs = state.featureSchema;
  if (!fs) return `<div class="loading">Loading the feature vocabulary…</div>`;
  const entityRole = entityRoleId(state.schema);
  const roles = [...new Set(state.sources.filter((e) => e.source.role && e.source.role !== entityRole).map((e) => e.source.role))];
  const draft = state.featureForm;
  const fnOptions = fs.function.map((f) => `<option value="${esc(f)}"${draft.function === f ? " selected" : ""}>${esc(f)}</option>`).join("");
  const roleOptions = roles.map((r) => `<option value="${esc(r)}"${draft.role === r ? " selected" : ""}>${esc(humanizeId(r))}</option>`).join("");
  return `<div class="card"><h4>New feature</h4><div style="padding:16px">
    <div class="frow">
      <div class="field"><span class="sub">Name</span><div class="control"><input data-act="feature-field" data-field="name" value="${esc(
        draft.name,
      )}" pattern="${esc(fs.namePattern)}" aria-label="Feature name"></div></div>
      <div class="field"><span class="sub">Role</span><div class="control sel"><select data-act="feature-field" data-field="role" aria-label="Role">${roleOptions}</select></div></div>
      <div class="field"><span class="sub">Function</span><div class="control sel"><select data-act="feature-field" data-field="function" aria-label="Function">${fnOptions}</select></div></div>
      <div class="field"><span class="sub">Column</span><div class="control"><input data-act="feature-field" data-field="column" value="${esc(
        draft.column || "",
      )}" aria-label="Column"></div></div>
      <div class="field xs"><span class="sub">Window (days)</span><div class="control"><input type="number" data-act="feature-field" data-field="window_days" min="1" value="${esc(
        present(draft.window_days) ? draft.window_days : "",
      )}" aria-label="Window in days, blank for all history"></div></div>
      <div class="field"><span class="sub">Description</span><div class="control"><input data-act="feature-field" data-field="description" value="${esc(
        draft.description || "",
      )}" aria-label="Description"></div></div>
    </div>
    <div class="actions" style="border-top:0;padding-top:10px">
      <button type="button" class="run" data-act="save-feature">Add feature</button>
      <button type="button" class="linkbtn" data-act="cancel-add-feature">Cancel</button>
    </div>
  </div></div>`;
}

function labelSentence(state) {
  const label = state.label;
  if (!label) return `<div class="empty">This use case has no default label - the dataset will be built for scoring only.</div>`;
  const phrase = LABEL_TYPE_PHRASE[label.type] || label.type;
  return `<p class="fhint" style="font-size:13px;color:var(--ink)">An entity counts as <b>${esc(
    label.name,
  )}</b> if there is <b>${esc(phrase)}</b> in the <b><input type="number" min="1" style="width:64px" data-act="set-horizon" value="${esc(
    present(label.horizon_days) ? label.horizon_days : "",
  )}" aria-label="Horizon in days"></b> days after the snapshot.</p>`;
}

function snapshotSettings(state) {
  const ss = state.snapshotSchema;
  const snap = state.snapshot;
  if (!ss || !snap) return `<div class="loading">Loading snapshot settings…</div>`;
  const modeOptions = ss.mode
    .map((m) => `<option value="${esc(m)}"${snap.mode === m ? " selected" : ""}>${esc(humanizeId(m))}</option>`)
    .join("");
  const freqOptions = ss.frequency
    .map((f) => `<option value="${esc(f)}"${snap.frequency === f ? " selected" : ""}>${esc(humanizeId(f))}</option>`)
    .join("");
  return `<div class="frow">
    <div class="field"><span class="sub">Snapshots</span><div class="control sel"><select data-act="set-snapshot" data-field="mode" aria-label="Snapshot mode">${modeOptions}</select></div></div>
    ${
      snap.mode === "periodic"
        ? `<div class="field"><span class="sub">Frequency</span><div class="control sel"><select data-act="set-snapshot" data-field="frequency" aria-label="Frequency">${freqOptions}</select></div></div>
    <div class="field xs"><span class="sub">Max snapshots</span><div class="control"><input type="number" data-act="set-snapshot" data-field="max_snapshots" min="${
      ss.maxSnapshots.min || 1
    }" max="${ss.maxSnapshots.max || 120}" value="${esc(snap.max_snapshots)}" aria-label="Max snapshots"></div></div>`
        : ""
    }
  </div>`;
}

function previewResults(state) {
  if (state.previewLoading) return `<div class="loading">Building a 200-entity sample…</div>`;
  if (state.previewError) return errorBox(state.previewError);
  const preview = state.preview;
  if (!preview) return "";
  const rows = (preview.rows || []).slice(0, 5);
  const columns = rows.length ? Object.keys(rows[0]) : [];
  const rowsTable = rows.length
    ? table(columns, rows.map((r) => columns.map((c) => dash(r[c]))))
    : `<div class="empty">The sample produced no rows.</div>`;
  const nullRates = Object.entries(preview.feature_null_rates || {}).map(([name, rate]) => [
    name,
    fmtPct(rate),
    barTrack(rate * 100, `${name} ${fmtPct(rate)} null`),
  ]);
  const snapshots = (preview.per_snapshot || []).map((s) => [s.date, dash(s.entities, fmtInt), dash(s.positive_rate, fmtPct)]);
  return `<div class="card"><h3>Preview</h3>${rowsTable}
    <h4>Feature null rates</h4>${
      nullRates.length
        ? `<div class="bars">${nullRates
            .map(([name, pct, bar]) => `<div class="brow"><span class="lab">${esc(name)}</span>${bar}<span class="pct">${esc(pct)}</span></div>`)
            .join("")}</div>`
        : `<div class="empty">No features to measure yet.</div>`
    }
    <h4>Positive rate per snapshot</h4>${
      snapshots.length ? table(["snapshot", "entities", "positive rate"], snapshots) : `<div class="empty">Single-snapshot dataset.</div>`
    }
    ${checksList(preview.checks)}
  </div>`;
}

export function featuresStep(state) {
  const pending = schemaPending(state, "features", 3, "Features & label", "What the model reads, and what it predicts.");
  if (pending) return pending;
  const done = !!state.preview && !state.previewError;
  const body = `
    ${state.features.length ? featuresByRole(state) : `<div class="empty">No suggested features for the mapped sources yet.</div>`}
    ${addFeatureForm(state)}
    <h4 style="margin-top:16px">Label</h4>
    ${labelSentence(state)}
    <h4 style="margin-top:16px">Snapshots</h4>
    ${snapshotSettings(state)}
    ${checksList(state.specChecks)}
    <div class="actions" style="border-top:0"><button type="button" class="run" data-act="preview"${
      state.previewLoading ? " disabled" : ""
    }>${state.previewLoading ? "Building preview…" : "Preview"}</button></div>
    ${previewResults(state)}
  `;
  return stepShell(
    "features",
    3,
    "Features & label",
    "What the model reads, and what it predicts.",
    done,
    state.open === "features",
    body,
  );
}

// ---------------------------------------------------------------------------------------------
// Step 4: Build & review
// ---------------------------------------------------------------------------------------------

function buildProgress(state) {
  const status = state.dataset && state.dataset.status;
  if (!status) return "";
  const groups = groupBuildStages(status.stages || []);
  const cls = { running: "active", done: "done", failed: "failed", cancelled: "cancelled", pending: "" };
  const rows = groups
    .map(
      (group, i) =>
        `<li class="${cls[group.state]}"><span class="dot">${i + 1}</span><div><div class="pt">${esc(
          group.label,
        )}</div><div class="pd">${esc(group.detail)}</div></div></li>`,
    )
    .join("");
  return `<ol class="progress">${rows}</ol>`;
}

function coverageTable(report) {
  const rows = (report.sources || []).map((s) => [s.source_id, humanizeId(s.role), fmtInt(s.rows), dash(s.join_coverage, (v) => fmtPct(v))]);
  return table(["source", "role", "rows", "coverage"], rows);
}

function snapshotBars(report) {
  const snaps = report.snapshots || [];
  if (!snaps.length) return `<div class="empty">No snapshots produced.</div>`;
  return `<div class="bars">${snaps
    .map((s) => {
      const pct = present(s.positive_rate) ? s.positive_rate * 100 : 0;
      return `<div class="brow"><span class="lab">${esc(s.date)}${s.censored ? " (censored)" : ""}</span>${barTrack(
        pct,
        `${s.date} ${dash(s.positive_rate, (v) => fmtPct(v))} positive`,
      )}<span class="pct">${dash(s.positive_rate, (v) => fmtPct(v))}</span></div>`;
    })
    .join("")}</div>`;
}

function droppedFeatures(report) {
  const dropped = (report.features || []).filter((f) => f.dropped);
  if (!dropped.length) return `<div class="empty">No features were dropped.</div>`;
  return table(
    ["feature", "null rate", "reason"],
    dropped.map((f) => [f.name, fmtPct(f.null_fraction), f.reason || EM_DASH]),
  );
}

function buildReview(state) {
  const report = state.buildReport;
  if (!report) return "";
  return `<div class="card"><h3>Sources &amp; coverage</h3>${coverageTable(report)}</div>
    <div class="card"><h3>Snapshots</h3>${snapshotBars(report)}</div>
    <div class="card"><h3>Dropped features</h3>${droppedFeatures(report)}</div>
    ${checksList(report.checks)}
    <div class="actions" style="border-top:0">
      <button type="button" class="run" data-act="use-dataset"${report.passed ? "" : " disabled"}>Use this dataset</button>
      ${!report.passed ? `<span class="reason">Resolve the errors above first.</span>` : ""}
    </div>`;
}

export function buildStep(state) {
  const done = !!(state.buildReport && state.buildReport.passed);
  const body = `
    ${!state.datasetId ? `<div class="actions" style="border-top:0"><button type="button" class="run" data-act="build"${state.building ? " disabled" : ""}>${state.building ? "Starting…" : "Build dataset"}</button></div>` : ""}
    ${state.buildError ? errorBox(state.buildError) : ""}
    ${checksList(state.buildChecks)}
    ${buildProgress(state)}
    ${buildReview(state)}
  `;
  return stepShell("build", 4, "Build & review", "Run the recipe and check what it produced.", done, state.open === "build", body);
}
