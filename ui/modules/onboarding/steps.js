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
//   vocabularyError - the ApiError loading featureSchema or snapshotSchema raised, or null
//   label         - editable copy of schema.label (only horizon_days differs from it), or null
//   snapshotSchema- {mode, frequency, maxSnapshots: {choices|min|max|default}} from openapi.json
//   snapshot      - editable SnapshotDefinition-shaped object
//   specChecks, previewLoading, previewError, preview (the preview response, or null)
//   datasetId, building, dataset ({manifest, status}), buildError, buildChecks (409 checks), buildReport
//   inUse         - the host has taken this dataset into Step 2 ("Use this dataset" was clicked)
//   replay        - score mode only (Plan A M35), else null: {spec, uploadedIds, settledIds,
//                   response (the POST .../replay answer, or null), running, error}

import { EM_DASH, barTrack, dash, errorBox, esc, fmtInt, fmtPct, present, table } from "../../dom.js";

/** The two standard types `StandardColumn` allows `value_aliases` on; any other type has no value
 * map to edit (`engine.config.StandardColumn._shape` refuses one outright). */
const VALUE_MAPPABLE = ["categorical", "boolean"];

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

/** The one threshold this file draws in: at or above it a suggestion is shown green, below it amber
 * (plan §10's mapping screen). It decides a colour, never a value - the number beside the pill is
 * always the suggester's own, and a pair nobody suggested gets no number at all. */
const CONFIDENT = 0.85;

/**
 * The confidence pill for one row: the suggester's measurement, or grey when there is none.
 *
 * `measured` is looked up in `state.mappingSuggested` - the untouched `MappingSpec` the API
 * returned - and never in the working copy the table edits. That is the whole point: a column the
 * user picked by hand was never scored by anything, and `MappingColumn.confidence` is a required
 * float, so the working copy has to carry *some* number for the save request to be accepted at all.
 * Printing that number as though it were a measurement is exactly the fabrication house rule 2
 * forbids, so a hand-picked pair says who decided it instead, and says nothing about how sure
 * anyone is.
 */
function confidencePill(decided, suggested) {
  if (!decided) return `<span class="pill">${EM_DASH}</span>`;
  const measured =
    suggested &&
    (suggested.columns || []).find(
      (c) => c.source === decided.source && c.standard === decided.standard,
    );
  if (!measured || !present(measured.confidence)) return `<span class="pill">Your choice</span>`;
  const cls = measured.confidence >= CONFIDENT ? "ok" : "warn";
  return `<span class="pill ${cls}">${fmtPct(measured.confidence, 0)}</span>`;
}

/**
 * `checks` rendered the same way the Phase 1 Setup form renders a validation report (`.vlist`).
 *
 * `counts` is `{error_count, warning_count}` when the response carrying these checks also carried
 * its own tally - a `BuildReport` does, and its numbers are not the same as counting the list here:
 * `error_count` is documented as "blocking errors, *excluding acknowledged ones*". Where the API
 * counted, that count is shown; where it did not (a mapping save, a spec create - neither returns a
 * tally), the length of the list the API sent is the only number in play and is shown as such.
 */
function checksList(checks, counts) {
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
  const counted = counts && present(counts.error_count);
  const errors = counted ? counts.error_count : list.filter((c) => c.severity === "error").length;
  const warnings = counted
    ? counts.warning_count
    : list.filter((c) => c.severity === "warning").length;
  const also = warnings ? `, and ${warnings} warning${warnings === 1 ? "" : "s"} were found` : "";
  const head = errors
    ? `${errors} ${errors === 1 ? "problem" : "problems"} must be fixed before this can be built${also}.`
    : "Warnings were found - review before building.";
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${items}</div>`;
}

// ---------------------------------------------------------------------------------------------
// Step 1: Sources
// ---------------------------------------------------------------------------------------------

/**
 * The role `<select>`, preselected to the detector's top candidate, plus a Confirm button while the
 * role is still only a proposal.
 *
 * The button is not decoration. `change` fires only when the user picks a *different* option, so a
 * user who agrees with the detection - the common case, and the one the ranking exists to produce -
 * has no way to say so: the row would sit showing "Bills" while `SourceSpec.role` stayed null, and
 * every later step would keep refusing a source the screen appeared to have settled. Detection
 * proposes and the user confirms (plan §6.1), which needs something to confirm *with*.
 */
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
  const confirm = currentRole
    ? ""
    : `<button type="button" class="linkbtn" data-act="confirm-role" data-source="${esc(
        sourceId,
      )}">Confirm</button>`;
  return `<div class="control sel"><select data-act="set-role" data-source="${esc(
    sourceId,
  )}" aria-label="Role">${options}</select></div>${confirm}`;
}

function roleHintLine(state) {
  const roles = state.schema.roles.roles;
  const line = Object.keys(roles)
    .sort()
    .map((id) => `${humanizeId(id)}: ${roles[id].description}`)
    .join(" · ");
  return `<p class="fhint">${esc(line)}</p>`;
}

/** The share of this table's keys found in the entity table - measured server-side on every role
 * change (`api.routes.sources.resync_join_coverage`) and an em dash until it has been. There is
 * nothing to measure against before an entity source is confirmed, and nothing to measure for the
 * entity source itself. */
function coverageBadge(entityConfirmed, isEntityRole, keyCandidates) {
  if (isEntityRole || !entityConfirmed) return EM_DASH;
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
    <td>${esc(dash(keyTop && keyTop.column))}</td>
    <td>${esc(dash(timeTop && timeTop.column))}</td>
    <td>${coverageBadge(entityConfirmed, isEntityRole, profile.key_candidates)}</td>
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
  if (state.replay) return replaySourcesStep(state);
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

/** The replay's row for one of this month's files, or `null` while it has not answered. */
function replayedEntry(state, sourceId) {
  const response = state.replay && state.replay.response;
  return (response && response.sources.find((entry) => entry.source_id === sourceId)) || null;
}

/**
 * Score mode's Sources step (prototype `08`): this month's files, each with the role the saved
 * recipe gives it. There is no role to choose - the recipe made that decision last month - so the
 * role is shown, not offered; a file the recipe had and this month's upload does not is listed with
 * the API's own words about it.
 */
function replaySourcesStep(state) {
  const replay = state.replay;
  const entityRole = entityRoleId(state.schema);
  const entityConfirmed = state.sources.some((entry) => entry.source.role === entityRole);
  const response = replay.response;
  const rows = state.sources
    .map((entry) => {
      const { source, profile } = entry;
      const replayed = replayedEntry(state, source.source_id);
      const keyTop = (profile.key_candidates || [])[0];
      const timeTop = (profile.time_candidates || [])[0];
      return `<tr>
        <td>${esc(source.file_name)}</td>
        <td>${dash(source.rows, fmtInt)}</td>
        <td>${replayed ? esc(humanizeId(replayed.role)) : EM_DASH}</td>
        <td>${esc(dash(keyTop && keyTop.column))}</td>
        <td>${esc(dash(timeTop && timeTop.column))}</td>
        <td>${coverageBadge(entityConfirmed, source.role === entityRole, profile.key_candidates)}</td>
        <td><button type="button" class="linkbtn" data-act="delete-source" data-source="${esc(
          source.source_id,
        )}">Remove</button></td>
      </tr>`;
    })
    .join("");
  const unmatched = response ? response.unmatched : [];
  const unused = response
    ? state.sources.filter((entry) => response.unused_source_ids.includes(entry.source.source_id))
    : [];
  const body = `
    <div class="orline">
      <label class="control file"><input type="file" class="sr" multiple accept=".csv,.parquet" data-act="pick-files"><span class="fname">Add this month's files</span><span class="ico" aria-hidden="true">⤒</span></label>
      <span>CSV or Parquet, one file per table</span>
    </div>
    ${state.uploading.map((name) => `<div class="loading">Reading ${esc(name)}…</div>`).join("")}
    ${state.sourcesError ? errorBox(state.sourcesError) : ""}
    ${replay.error ? errorBox(replay.error) : ""}
    ${replay.running ? `<div class="loading">Matching this month's tables to the saved recipe…</div>` : ""}
    ${
      state.sources.length
        ? `<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Rows</th><th>Role</th><th>Key candidate</th><th>Time candidate</th><th>Coverage</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="empty">${state.sourcesLoading ? "Loading sources…" : "No tables yet - add this month's files above."}</div>`
    }
    ${
      unmatched.length
        ? `<div class="vlist" role="alert">${unmatched
            .map((entry) => `<div class="vitem"><span class="pill bad">${esc(entry.file_name)}</span><div><div class="vmsg">${esc(entry.message)}</div></div></div>`)
            .join("")}</div>`
        : ""
    }
    ${
      unused.length
        ? `<p class="fhint">${esc(
            `Not part of the saved recipe, so not read: ${unused.map((entry) => entry.source.file_name).join(", ")}.`,
          )}</p>`
        : ""
    }
    <p class="fhint">These are this month's tables. The roles come from the saved recipe.</p>
  `;
  const done = !!response && !unmatched.length && response.sources.length > 0;
  return stepShell("sources", 1, "Sources", "This month's tables, read the way the saved recipe reads them.", done, state.open === "sources", body);
}

// ---------------------------------------------------------------------------------------------
// Step 2: Mapping
// ---------------------------------------------------------------------------------------------

/**
 * The standard names a source in this role may fill, in the order `engine.onboarding.mapping`'s own
 * `_targets()` builds them: the role's required columns, then the use case's columns for an entity
 * table or the role's typical columns for an event log, then the role's optional ones.
 *
 * Mirroring that order matters twice over. An event log's real targets are `entity_key`,
 * `event_time` and its own `typical_columns` (`amount`, `status`, ...) - offering it the use case's
 * one-row-per-entity columns instead, as an earlier version did, left `bills` with no way to name
 * its own amount column and no way to reach the names the build actually reads. And the list is
 * deduplicated, because `MappingSpec` refuses a standard column claimed twice: a `<select>` that
 * offered one name under two options would let the user build a mapping the API can only reject.
 */
function mappingTargets(state, role) {
  const spec = state.schema.roles.roles[role];
  if (!spec) return [];
  const columns = state.schema.standard_schema.columns || [];
  const defined = new Map(columns.map((column) => [column.name, column]));
  const required = requiredTargetNames(state, spec);
  const ordered = [
    ...(spec.required_columns || []),
    ...(spec.kind === "event" ? spec.typical_columns || [] : columns.map((column) => column.name)),
    ...(spec.optional_columns || []),
  ];
  const targets = [];
  const seen = new Set();
  for (const name of ordered) {
    if (seen.has(name)) continue;
    seen.add(name);
    const column = defined.get(name) || null;
    const label = column && column.description ? column.description : humanizeId(name);
    targets.push({ value: name, label: required.has(name) ? `${label} (required)` : label });
  }
  return targets;
}

/** `engine.onboarding.mapping._required()`: the role's own required columns, plus - for an entity
 * table only - the use case's required columns that the build cannot derive for itself. A derivable
 * column is not missing when nobody maps it, so marking it required would send the user looking for
 * a column their file is not expected to have. */
function requiredTargetNames(state, spec) {
  const names = new Set(spec.required_columns || []);
  if (spec.kind === "event") return names;
  for (const column of state.schema.standard_schema.columns || []) {
    if (column.required && !column.derivable) names.add(column.name);
  }
  return names;
}

function columnTargetOf(mapping, rawName) {
  const found = (mapping.columns || []).find((c) => c.source === rawName);
  return found ? found.standard : "";
}

/** `mapped` says whether this raw value has a decision at all; `current` is that decision, which may
 * legitimately be `null` ("blank it out"). The two are separate arguments because they are separate
 * facts: "nobody has decided" and "someone decided to blank it" are both falsy, and collapsing them
 * marks two of these options selected at once. */
function valueMapRow(sourceId, standardName, standardColumn, rawValue, mapped, current) {
  const aliasTargets = Object.keys(standardColumn.value_aliases || {});
  const options = [
    `<option value="__keep__"${!mapped ? " selected" : ""}>Keep as-is</option>`,
    `<option value="__null__"${mapped && current === null ? " selected" : ""}>Set null</option>`,
    ...aliasTargets.map(
      (target) =>
        `<option value="${esc(target)}"${
          mapped && current === target ? " selected" : ""
        }>${esc(target)}</option>`,
    ),
  ].join("");
  return `<div class="frow"><span class="colchip">${esc(rawValue)}</span><div class="control sel"><select data-act="set-value-map" data-source="${esc(
    sourceId,
  )}" data-standard="${esc(standardName)}" data-value="${esc(rawValue)}" aria-label="Map ${esc(
    rawValue,
  )}">${options}</select></div></div>`;
}

function valueMapEditor(sourceId, standardName, standardColumn, mapping, column) {
  if (!VALUE_MAPPABLE.includes(standardColumn.type)) return "";
  const known = mapping.value_maps && mapping.value_maps[standardName] ? mapping.value_maps[standardName] : {};
  const values = new Set([...(column.top_categories || []).map((c) => c.value), ...Object.keys(known)]);
  if (!values.size) return "";
  const rows = [...values]
    .map((value) =>
      valueMapRow(
        sourceId,
        standardName,
        standardColumn,
        value,
        Object.prototype.hasOwnProperty.call(known, value),
        known[value],
      ),
    )
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
    <td>${esc(dash(column.inferred_type))}</td>
    <td>${esc(dash(sample))}</td>
    <td>${confidencePill(decided, state.mappingSuggested[sourceId])}</td>
    <td>${standardColumn ? valueMapEditor(sourceId, current, standardColumn, mapping, column) : ""}</td>
  </tr>`;
}

function mappingTable(state, entry) {
  const { source, profile } = entry;
  const mapping = state.mappings[source.source_id];
  if (state.mappingLoading[source.source_id]) return `<div class="loading">Suggesting a mapping…</div>`;
  if (state.mappingError[source.source_id]) return errorBox(state.mappingError[source.source_id]);
  if (!mapping) return `<div class="empty">Not analysed yet.</div>`;
  const rows = profile.profile.columns.map((column) => mappingRow(state, source.source_id, mapping, source.role, column)).join("");
  return `<div class="card"><h3>${esc(source.file_name)}<span style="float:right"><button type="button" class="linkbtn" data-act="accept-all" data-source="${esc(
    source.source_id,
  )}">Accept all suggestions</button></span></h3>
    <div class="tbl-wrap"><table><thead><tr><th>Your column</th><th>Our column</th><th>Type</th><th>Sample values</th><th>Confidence</th><th>Value map</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${missingRequiredBlock(state, source.source_id, mapping)}
    ${checksList(state.mappingChecks[source.source_id])}
    <div class="kv"><span class="k">${state.mappingSaved[source.source_id] ? "Saved" : "Not saved yet"}</span><span class="v"><button type="button" class="linkbtn" data-act="save-mapping" data-source="${esc(
      source.source_id,
    )}"${state.mappingSaving[source.source_id] ? " disabled" : ""}>${
      state.mappingSaving[source.source_id] ? "Saving…" : "Save mapping"
    }</button></span></div>
  </div>`;
}

/**
 * The required standard columns still unmapped, and the API's own words about each one.
 *
 * `missing_required` is the server's list, not a recount of the working copy: the engine decides
 * what "required" means for a role (a derivable column is not missing; an event log's requirements
 * are not an entity table's), and a second opinion computed here would eventually disagree with the
 * one that actually blocks the build. The message beside each name is likewise the
 * `REQUIRED_STANDARD_COLUMN_UNMAPPED` check's, or - before a save has been made and checks exist -
 * the standard column's own description from the schema. The UI does not phrase validation
 * failures; it shows the ones the API returned (house rule 3).
 *
 * The one thing done locally is *narrowing*: a name the user has since given a column to drops off
 * the list before the next save confirms it. That direction is safe - it can only stop showing a
 * warning the API raised, never raise one it did not - whereas recomputing the list from scratch
 * would be this screen inventing a verdict the build does not share.
 */
function missingRequiredBlock(state, sourceId, mapping) {
  const claimed = new Set((mapping.columns || []).map((column) => column.standard));
  const names = (mapping.missing_required || []).filter((name) => !claimed.has(name));
  if (!names.length) return "";
  const checks = state.mappingChecks[sourceId] || [];
  const defined = state.schema.standard_schema.columns || [];
  const items = names
    .map((name) => {
      const check = checks.find((c) => c.column === name) || null;
      const column = defined.find((c) => c.name === name) || null;
      const message = check ? check.message : column && column.description ? column.description : "";
      const suggestion = check && check.suggestion ? check.suggestion : "";
      return `<div class="vitem"><span class="pill bad">${esc(name)}</span><div>${
        message ? `<div class="vmsg">${esc(message)}</div>` : ""
      }${suggestion ? `<div class="vsug">${esc(suggestion)}</div>` : ""}</div></div>`;
    })
    .join("");
  const head = `${names.length} required column${names.length === 1 ? "" : "s"} not yet mapped`;
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${items}</div>`;
}

/**
 * Score mode's Mapping step: closed and ticked when every file has the columns last month's mapping
 * read, and reopened on exactly the files that do not - each under the API's own sentence about what
 * went missing. The mapping table itself is the train-mode one, over the replayed copy.
 */
function replayMappingStep(state) {
  const response = state.replay.response;
  const reopened = response ? response.sources.filter((entry) => entry.missing_columns.length) : [];
  let body;
  if (!response) {
    body = `<div class="empty">Add this month's tables in step 1; their mapping is replayed from the saved recipe.</div>`;
  } else if (!reopened.length) {
    body = `<div class="empty">Every table has the columns last month's mapping read, so the mapping is replayed exactly as it was.</div>`;
  } else {
    body = reopened
      .map((replayed) => {
        const entry = state.sources.find((candidate) => candidate.source.source_id === replayed.source_id);
        return `<div class="vlist" role="alert"><div class="vhead">${esc(replayed.message)}</div></div>${
          entry ? mappingTable(state, entry) : ""
        }`;
      })
      .join("");
  }
  const done = !!response && !reopened.length && !response.unmatched.length;
  return stepShell("mapping", 2, "Mapping", "Replayed from the saved recipe.", done, state.open === "mapping", body);
}

export function mappingStep(state) {
  const pending = schemaPending(state, "mapping", 2, "Mapping", "Your columns, matched to ours.");
  if (pending) return pending;
  if (state.replay) return replayMappingStep(state);
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
  if (!fs) {
    return state.vocabularyError
      ? errorBox(state.vocabularyError)
      : `<div class="loading">Loading the feature vocabulary…</div>`;
  }
  const entityRole = entityRoleId(state.schema);
  const roles = [...new Set(state.sources.filter((e) => e.source.role && e.source.role !== entityRole).map((e) => e.source.role))];
  const draft = state.featureForm;
  const fnOptions = fs.function.map((f) => `<option value="${esc(f)}"${draft.function === f ? " selected" : ""}>${esc(f)}</option>`).join("");
  const roleOptions = roles.map((r) => `<option value="${esc(r)}"${draft.role === r ? " selected" : ""}>${esc(humanizeId(r))}</option>`).join("");
  const opOptions = fs.where
    .map((op) => `<option value="${esc(op)}"${draft.whereOp === op ? " selected" : ""}>${esc(op)}</option>`)
    .join("");
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
    <details class="adv-wrap"><summary>Filter (optional)<span class="n">only count events that match</span></summary>
      <div class="frow">
        <div class="field"><span class="sub">Column</span><div class="control"><input data-act="feature-field" data-field="whereColumn" value="${esc(
          draft.whereColumn || "",
        )}" aria-label="Filter column"></div></div>
        <div class="field"><span class="sub">Is</span><div class="control sel"><select data-act="feature-field" data-field="whereOp" aria-label="Filter operator"><option value=""${
          draft.whereOp ? "" : " selected"
        }>No filter</option>${opOptions}</select></div></div>
        <div class="field"><span class="sub">Value</span><div class="control"><input data-act="feature-field" data-field="whereValue" value="${esc(
          draft.whereValue || "",
        )}" aria-label="Filter value"></div></div>
      </div>
    </details>
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

/** `min`/`max` attributes for a number input, written only where the API's own schema declared a
 * bound. A fallback bound would be this screen inventing a limit nobody set - and silently refusing
 * a value the API would have accepted, or accepting one it will not. */
function numericBounds(bounds) {
  const parts = [];
  if (present(bounds && bounds.min)) parts.push(` min="${esc(bounds.min)}"`);
  if (present(bounds && bounds.max)) parts.push(` max="${esc(bounds.max)}"`);
  return parts.join("");
}

function snapshotSettings(state) {
  const ss = state.snapshotSchema;
  const snap = state.snapshot;
  if (!ss || !snap) {
    return state.vocabularyError
      ? errorBox(state.vocabularyError)
      : `<div class="loading">Loading snapshot settings…</div>`;
  }
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
    <div class="field xs"><span class="sub">Max snapshots</span><div class="control"><input type="number" data-act="set-snapshot" data-field="max_snapshots"${numericBounds(
      ss.maxSnapshots,
    )} value="${esc(present(snap.max_snapshots) ? snap.max_snapshots : "")}" aria-label="Max snapshots"></div></div>`
        : ""
    }
  </div>`;
}

/** A 0-100 track for a measured fraction, and nothing at all for one that was not measured: a
 * zero-length bar beside an em dash reads as "we measured this and it came out at nought", which
 * is precisely the sentence house rule 2 exists to prevent. */
function rateBar(fraction, label) {
  if (!present(fraction)) return "";
  return barTrack(fraction * 100, `${label} ${fmtPct(fraction)}`);
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
    dash(rate, fmtPct),
    rateBar(rate, `${name} null rate`),
  ]);
  const snapshots = (preview.per_snapshot || []).map((s) => [
    dash(s.date),
    dash(s.entities, fmtInt),
    dash(s.positive_rate, fmtPct),
  ]);
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

/**
 * What still stands between this recipe and a spec the API will accept, in business language.
 *
 * `POST /clients/{id}/onboarding-specs` requires a real `entity_source_id` and mapping ids it can
 * load; sending it a null entity source, or the id of a suggestion nobody saved, answers with
 * FastAPI's own field-level `422` (or a `404` on the mapping), neither of which is a message a user
 * can act on. House rule 3 says a failure is a code, a business-language message and a suggestion -
 * so the Preview and Build buttons refuse locally, and say what to do, rather than firing a request
 * whose only possible answer is one this panel would have to apologise for.
 */
function recipeBlockers(state) {
  const reasons = [];
  if (!state.schema) return ["The standard schema has not loaded yet."];
  const entityRole = entityRoleId(state.schema);
  if (!state.sources.some((entry) => entry.source.role === entityRole)) {
    reasons.push("Confirm one source as the entity table in step 1.");
  }
  const unsaved = state.sources.filter(
    (entry) => entry.source.role && !state.mappingSaved[entry.source.source_id],
  );
  if (unsaved.length) {
    // Every source with a confirmed role goes into the recipe, so every one of them needs a saved
    // mapping - `OnboardingSpec.mapping_ids` is "one per source", and a spec that names a source
    // with no mapping is one `POST /datasets` can only refuse.
    reasons.push(
      `Save the mapping for ${unsaved.map((entry) => entry.source.file_name).join(", ")} in step 2.`,
    );
  }
  if (!state.snapshot) reasons.push("The snapshot settings have not loaded yet.");
  return reasons;
}

/** What still stands between this month's files and a score-mode build: the same idea as
 * `recipeBlockers`, answered from the replay - whose own words are used wherever it gave any. */
function replayBlockers(state) {
  const replay = state.replay;
  if (!state.schema) return ["The standard schema has not loaded yet."];
  if (!replay.uploadedIds.length) return ["Add this month's tables in step 1."];
  if (replay.running) return ["Matching this month's tables to the saved recipe…"];
  const response = replay.response;
  if (!response) return ["The saved recipe has not been replayed onto this month's tables yet."];
  const reasons = response.unmatched.map((entry) => entry.message);
  const reopened = response.sources.filter((entry) => entry.missing_columns.length);
  if (reopened.length) {
    reasons.push(`Review the mapping for ${reopened.map((entry) => entry.file_name).join(", ")} in step 2.`);
  }
  return reasons;
}

/** A disabled action's reason line, in the same `.reason` the Build step already uses. */
const blockerLine = (reasons) =>
  reasons.length ? `<span class="reason">${esc(reasons.join(" "))}</span>` : "";

/**
 * Score mode's Features & label step: the saved recipe's features, applied unchanged, and the one
 * sentence that explains why no outcome is built when scoring (`docs/ONBOARDING.md` section 8). Read
 * only: a feature that differed from the ones the model was fitted on would score nothing it knows.
 */
function replayFeaturesStep(state) {
  const spec = state.replay.spec;
  const features = (spec.feature_spec && spec.feature_spec.features) || [];
  const rows = features
    .map(
      (feature) =>
        `<div class="kv"><span class="k">${esc(feature.name)}</span><span class="v">${esc(
          feature.description || windowLabel(feature),
        )}</span></div>`,
    )
    .join("");
  const label = spec.label_spec;
  const body = `
    <div class="card"><h4>Features, from the saved recipe</h4>${rows || `<div class="empty">The saved recipe builds no features.</div>`}</div>
    <div class="card"><h4>Label</h4><p class="fhint" style="padding:12px 16px 0">${esc(
      label
        ? `${label.name} is not built when scoring: there is nothing yet to look forward to.`
        : "The saved recipe builds no outcome.",
    )}</p></div>
    <div class="card"><h4>Snapshots</h4><p class="fhint" style="padding:12px 16px 0">Scoring stands at a single date: the end of this month's data.</p></div>
  `;
  return stepShell("features", 3, "Features & label", "The saved recipe's, unchanged.", true, state.open === "features", body);
}

export function featuresStep(state) {
  const pending = schemaPending(state, "features", 3, "Features & label", "What the model reads, and what it predicts.");
  if (pending) return pending;
  if (state.replay) return replayFeaturesStep(state);
  // A preview that came back carrying a blocking check did not succeed, whatever its HTTP status:
  // `POST .../preview` answers 200 with empty rows and the refusal in `checks` when the recipe is
  // structurally wrong. A green tick there would be this screen telling the user something the API
  // did not.
  const blocked = (checks) => (checks || []).some((c) => c.severity === "error");
  const blockers = recipeBlockers(state);
  const done =
    !!state.preview &&
    !state.previewError &&
    !blocked(state.specChecks) &&
    !blocked(state.preview.checks);
  const body = `
    ${state.features.length ? featuresByRole(state) : `<div class="empty">No suggested features for the mapped sources yet.</div>`}
    ${addFeatureForm(state)}
    <div class="card"><h4>Label</h4>${labelSentence(state)}</div>
    <div class="card"><h4>Snapshots</h4>${snapshotSettings(state)}</div>
    ${checksList(state.specChecks)}
    <div class="actions" style="border-top:0"><button type="button" class="run" data-act="preview"${
      state.previewLoading || blockers.length ? " disabled" : ""
    }>${state.previewLoading ? "Building preview…" : "Preview"}</button>${blockerLine(blockers)}</div>
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
  const rows = (report.sources || []).map((s) => [
    dash(s.source_id),
    dash(s.role, humanizeId),
    dash(s.rows, fmtInt),
    dash(s.join_coverage, (v) => fmtPct(v)),
  ]);
  return table(["source", "role", "rows", "coverage"], rows);
}

function snapshotBars(report) {
  const snaps = report.snapshots || [];
  if (!snaps.length) return `<div class="empty">No snapshots produced.</div>`;
  return `<div class="bars">${snaps
    .map(
      (s) =>
        `<div class="brow"><span class="lab">${esc(dash(s.date))}${
          s.censored ? " (censored)" : ""
        }</span>${rateBar(s.positive_rate, `${s.date} positive rate`)}<span class="pct">${dash(
          s.positive_rate,
          (v) => fmtPct(v),
        )}</span></div>`,
    )
    .join("")}</div>`;
}

function droppedFeatures(report) {
  const dropped = (report.features || []).filter((f) => f.dropped);
  if (!dropped.length) return `<div class="empty">No features were dropped.</div>`;
  return table(
    ["feature", "null rate", "reason"],
    dropped.map((f) => [dash(f.name), dash(f.null_fraction, fmtPct), dash(f.reason)]),
  );
}

/**
 * A build that stopped, as a coded, business-language message with something to do about it
 * (house rule 3). Without this a failure shows only as red dots on the progress list: the engine
 * writes its reason into `BuildStatus.error`, and a screen that polls that document and then drops
 * the one field explaining what happened leaves the user with nothing to act on.
 */
function buildFailure(state) {
  const status = state.dataset && state.dataset.status;
  if (!status || (status.state !== "failed" && status.state !== "cancelled")) return "";
  const cancelled = status.state === "cancelled";
  const reason = status.error || status.detail || "";
  return `${errorBox({
    code: cancelled ? "DATASET_BUILD_CANCELLED" : "DATASET_BUILD_FAILED",
    message:
      reason ||
      (cancelled
        ? "This build was stopped before it finished, so no dataset was produced."
        : "This build stopped before it finished, so no dataset was produced."),
  })}<p class="fhint">${esc(
    cancelled
      ? "Start the build again when you are ready; nothing about the recipe was changed."
      : "Review the checks below, fix what they name, then build again.",
  )}</p>`;
}

function buildReview(state) {
  const report = state.buildReport;
  if (!report) return "";
  return `<div class="card"><h3>Sources &amp; coverage</h3>${coverageTable(report)}</div>
    <div class="card"><h3>Snapshots</h3>${snapshotBars(report)}</div>
    <div class="card"><h3>Dropped features</h3>${droppedFeatures(report)}</div>
    ${checksList(report.checks, report)}
    <div class="actions" style="border-top:0">
      <button type="button" class="run" data-act="use-dataset"${report.passed ? "" : " disabled"}>Use this dataset</button>
      ${!report.passed ? `<span class="reason">Resolve the errors above first.</span>` : ""}
      ${state.inUse ? `<span class="reason">In use: the Columns step below now reads this dataset.</span>` : ""}
    </div>`;
}

export function buildStep(state) {
  const done = !!(state.buildReport && state.buildReport.passed);
  const blockers = state.replay ? replayBlockers(state) : recipeBlockers(state);
  const body = `
    ${
      !state.datasetId
        ? `<div class="actions" style="border-top:0"><button type="button" class="run" data-act="build"${
            state.building || blockers.length ? " disabled" : ""
          }>${state.building ? "Starting…" : "Build dataset"}</button>${blockerLine(blockers)}</div>`
        : ""
    }
    ${state.buildError ? errorBox(state.buildError) : ""}
    ${checksList(state.buildChecks)}
    ${buildProgress(state)}
    ${buildFailure(state)}
    ${buildReview(state)}
  `;
  return stepShell("build", 4, "Build & review", "Run the recipe and check what it produced.", done, state.open === "build", body);
}
