// The onboarding steps (Phase 2 plan §10, M13; v1 UI WP3): Sources, Mapping, Features & label,
// Build & review. Every function here is pure - `(state) => html` - exactly like `ui/pages.js`'s
// screens: `panel.js` owns the fetches, the working edits and the event wiring; this file only draws
// whatever is already sitting in `state`. Nothing in this file is data - every number, name and
// confidence reaches the page through `dash()`/`esc()`/`fmtInt()`/`fmtPct()`, and the one place a set
// of choices comes from is the schema the API just returned, never a list typed here.
//
// v1 layout (docs/UI_AUDIT.md "build-raw-*"): one step open at a time; a finished step folds to a
// one-line summary with "Edit"; titled steps without numbers; plain words first and codes, ids and
// extra columns behind "Show more columns" / "Details".
//
// `state` (built and owned by `panel.js`) that this module reads:
//   schema        - GET /use-cases/{id}/standard-schema response, or null before it loads
//   schemaError   - the ApiError that loading it raised, or null
//   entity        - the use case's own word for one row ("subscriber"), from `uc.entity`
//   open          - id of the one step currently expanded: "sources"|"mapping"|"features"|"build"
//   sources       - [{source: SourceSpec, profile: SourceProfile}], newest last
//   sourcesLoading, sourcesError, uploading (filenames mid-upload), rolesBusy
//   rowMenu, confirmDelete - the source id whose row menu / remove confirmation is open, or null
//   mappings      - sourceId -> MappingSpec (suggested or saved, with the user's live edits)
//   mappingLoading, mappingError, mappingSaving, mappingSaved, mappingChecks (sourceId -> ...)
//   mapOpen       - sourceId -> bool, a mapping card the user opened or closed
//   touched       - sourceId -> {column: true}, columns the user re-pointed by hand
//   saveAll       - {running, saved, stopped: [sourceId]} after "Accept suggestions and save all"
//   features      - working FeatureDef[] (suggested, filtered to mapped roles, plus any added)
//   featureChecked- name -> bool, which features are ticked into the build
//   featureForm   - the "Add a feature" draft the dialog is editing, or null when closed
//   featureSchema - {function: string[], where: string[], namePattern: string} from openapi.json,
//                   or null before it loads; the Add-feature form stays disabled until it has this
//   vocabularyError - the ApiError loading featureSchema or snapshotSchema raised, or null
//   label         - editable copy of schema.label (only horizon_days differs from it), or null
//   snapshotSchema- {mode, frequency, maxSnapshots: {min|max}} from openapi.json
//   snapshot      - editable SnapshotDefinition-shaped object
//   specChecks, previewLoading, previewError, preview (the preview response, or null)
//   datasetId, building, dataset ({manifest, status}), buildError, buildChecks (409 checks), buildReport
//   inUse, collapsed - the host has taken this dataset into Step 2; the panel is folded to one line
//   replay        - score mode only (Plan A M35), else null: {spec, uploadedIds, settledIds,
//                   response (the POST .../replay answer, or null), running, error, trainingKeys}

import {
  EM_DASH,
  barTrack,
  dash,
  dataTable,
  errorBox,
  esc,
  fmtInt,
  fmtPct,
  glossaryCode,
  plainError,
  present,
  table,
  toggletip,
} from "../../dom.js";

/** The two standard types `StandardColumn` allows `value_aliases` on; any other type has no value
 * map to edit (`engine.config.StandardColumn._shape` refuses one outright). */
const VALUE_MAPPABLE = ["categorical", "boolean"];

/** The label sentence, one entry per `LabelType`: "A subscriber counts as <outcome> if they <rule>
 * [60] days after the prediction date." `share` heads the per-date chart of the build report. A
 * `column` label reads an outcome the client already records, so it has no rule and no horizon. */
const LABEL_TYPE_PHRASE = {
  event_presence: { outcome: "a yes", rule: "have matching activity within", share: "Share counted as a yes" },
  event_absence: { outcome: "having left", rule: "do nothing for", share: "Share who left" },
  value_threshold: { outcome: "a yes", rule: "cross the measured threshold within", share: "Share counted as a yes" },
  column: { outcome: "a yes", rule: "", share: "Share counted as a yes" },
};

/** What one periodic snapshot is called in a sentence ("the last 12 month-ends"). */
const SNAPSHOT_NOUN = { monthly: "month-ends", weekly: "week-ends" };

/** The one threshold this file draws in: at or above it a suggestion is "Sure", below it "Check"
 * (plan §10's mapping screen). It decides a word and a colour, never a value. */
const CONFIDENT = 0.85;

/** Share of empty values above which a measure counts as "often empty" in the preview sentence. */
const OFTEN_EMPTY = 0.5;

/** A check that goes stale the moment the client has an entity source (the save that raised it ran
 * before the customer table was confirmed); it is not repeated on cards once one exists. */
const NO_ENTITY_SOURCE = "NO_ENTITY_SOURCE";

/** How many groups of look-alike files the duplicate note names before "and N more groups". */
const MAX_TWIN_GROUPS = 3;

// --- small shared pieces -------------------------------------------------------------------------

function humanizeId(id) {
  return String(id)
    .split("_")
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
}

const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");

/** The use case's own word for one row: "subscriber", "asset", "order". */
const entityNoun = (state) => state.entity || "customer";

/** "a and b", "a, b and c". */
function listJoin(items) {
  if (items.length < 2) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

const plural = (count, one, many) => `${fmtInt(count)} ${count === 1 ? one : many}`;

/** The first sentence of a description, so a select option stays one readable line. */
function firstSentence(text) {
  const match = /^(.+?[.!?])(\s|$)/.exec(String(text || ""));
  return match ? match[1] : String(text || "");
}

/** The one role whose `kind` is `entity`. Not a wire field: `RoleCatalogue.entity_role` is a Python
 * `@property` and never reaches the JSON the API sends, so every screen that needs it recomputes it
 * from the `roles` dict the same way the server does, rather than guessing at a field that is not
 * there. */
function entityRoleId(schema) {
  const roles = schema.roles.roles;
  return Object.keys(roles).find((id) => roles[id].kind === "entity") || null;
}

/** A role in words: the entity role is "<Entity> table", every event log its own name. */
function roleLabel(state, id) {
  if (!id) return EM_DASH;
  const spec = state.schema && state.schema.roles.roles[id];
  if (spec && spec.kind === "entity") return `${cap(entityNoun(state))} table`;
  return humanizeId(id);
}

/** A standard column name in words, with the generic "entity" read as the use case's own noun. */
const plainName = (state, name) => humanizeId(name).replace(/\bEntity\b/, cap(entityNoun(state)));

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

/**
 * One step of the panel: a `<details>` whose summary is the step's title and one line - a hint while
 * it is to do, the result once it is done ("6 tables · roles confirmed ✓"). A finished step shows
 * "Edit" and folds; `panel.js` keeps exactly one step open.
 */
function stepShell(id, title, line, done, open, body) {
  return `<details class="stage-d ob-step${done ? " done" : ""}" data-step="${esc(id)}"${open ? " open" : ""}>
    <summary><span class="sn" aria-hidden="true">${done ? "✓" : ""}</span><span><div class="st">${esc(
      title,
    )}</div><div class="ss">${esc(line)}</div></span>${done ? `<span class="sc ob-edit">Edit</span>` : ""}</summary>
    <div class="ob-body">${body}</div>
  </details>`;
}

/** A quiet button that opens another step ("Go to Mapping"). */
const stepLink = (step, label, kind = "quiet sm") =>
  `<button type="button" class="btn ${kind}" data-act="open-step" data-step="${esc(step)}">${esc(label)}</button>`;

/**
 * The confidence for one row: the suggester's measurement, read as "Sure" or "Check", or "Your
 * choice" / an em dash when nothing measured it.
 *
 * `measured` is looked up in `suggested` - the untouched `MappingSpec` the API returned - and never in
 * the working copy the table edits. That is the whole point: a column the user picked by hand was
 * never scored by anything, and `MappingColumn.confidence` is a required float, so the working copy
 * has to carry *some* number for the save request to be accepted at all. Printing that number as
 * though it were a measurement is exactly the fabrication house rule 2 forbids, so a hand-picked pair
 * says who decided it instead, and says nothing about how sure anyone is.
 */
function confidencePill(decided, suggested) {
  if (!decided) return `<span class="pill">${EM_DASH}</span>`;
  const measured =
    suggested &&
    (suggested.columns || []).find(
      (c) => c.source === decided.source && c.standard === decided.standard,
    );
  if (!measured || !present(measured.confidence)) return `<span class="pill">Your choice</span>`;
  const sure = measured.confidence >= CONFIDENT;
  return `<span class="pill ${sure ? "ok" : "warn"}" title="${esc(
    `Match confidence ${fmtPct(measured.confidence, 0)}`,
  )}">${sure ? "Sure" : "Check"}</span>`;
}

/**
 * The columns of one source whose current match is the suggester's own, below `CONFIDENT`, and not
 * re-pointed by the user since: the matches "Accept suggestions and save all" stops on and marks
 * "Check this match". `panel.js` reads the same list, so the screen and the action never disagree.
 */
export function flaggedColumns(state, sourceId) {
  const mapping = state.mappings[sourceId];
  const suggested = state.mappingSuggested[sourceId];
  if (!mapping || !suggested) return [];
  const touched = (state.touched && state.touched[sourceId]) || {};
  return (mapping.columns || [])
    .filter((decided) => {
      if (touched[decided.source]) return false;
      const measured = (suggested.columns || []).find(
        (c) => c.source === decided.source && c.standard === decided.standard,
      );
      return !!measured && present(measured.confidence) && measured.confidence < CONFIDENT;
    })
    .map((decided) => decided.source);
}

/** Checks as they stand now: a NO_ENTITY_SOURCE raised before the entity table was confirmed is
 * dropped once it has been. */
function liveChecks(state, checks) {
  const list = checks || [];
  if (!state.schema) return list;
  const entityRole = entityRoleId(state.schema);
  const hasEntity = state.sources.some((entry) => entry.source.role === entityRole);
  return hasEntity ? list.filter((check) => check.code !== NO_ENTITY_SOURCE) : list;
}

/** One check in plain words: the glossary's title first, the API's own message and suggestion (or
 * the glossary's fix) under it, the code behind "Details" and in `data-code`. */
function checkItem(check) {
  const entry = glossaryCode(check.code);
  const lead = entry && entry.title ? entry.title : check.message;
  const said = entry && entry.title ? check.message : "";
  const fix = check.suggestion || (entry && entry.fix) || "";
  const pill =
    check.severity === "error"
      ? `<span class="pill bad">Must fix</span>`
      : check.severity === "warning"
        ? `<span class="pill warn">Check</span>`
        : `<span class="pill">Note</span>`;
  return `<div class="vitem" data-code="${esc(check.code)}">${pill}<div><div class="vmsg">${esc(lead)}</div>${
    said ? `<div class="vsug">${esc(said)}</div>` : ""
  }${fix ? `<div class="vsug">${esc(fix)}</div>` : ""}<details class="tech"><summary>Details</summary><p class="mono"><code>${esc(
    check.code,
  )}</code></p></details></div></div>`;
}

/**
 * `checks` rendered the same way the Phase 1 Setup form renders a validation report (`.vlist`).
 *
 * `counts` is `{error_count, warning_count}` when the response carrying these checks also carried
 * its own tally - a `BuildReport` does, and its numbers are not the same as counting the list here:
 * `error_count` is documented as "blocking errors, *excluding acknowledged ones*". Where the API
 * counted, that count is shown; where it did not (a mapping save, a spec create - neither returns a
 * tally), the length of the list the API sent is the only number in play and is shown as such.
 * Without a blocking error the list is headed "Things to review".
 */
function checksList(checks, counts) {
  const list = checks || [];
  if (!list.length) return "";
  const counted = counts && present(counts.error_count);
  const errors = counted ? counts.error_count : list.filter((c) => c.severity === "error").length;
  const warnings = counted
    ? counts.warning_count
    : list.filter((c) => c.severity === "warning").length;
  const also = warnings ? `, and ${plural(warnings, "thing", "things")} to review` : "";
  const head = errors
    ? `${plural(errors, "problem", "problems")} must be fixed before this can be built${also}.`
    : "Things to review";
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${list.map(checkItem).join("")}</div>`;
}

/**
 * Checks as the step shows them: blocking ones open (they are what to do next), the rest folded
 * under "Things to review (N)" - still one click away, never a wall of pills.
 */
function checksFolded(checks, key, counts) {
  const list = checks || [];
  const blocking = list.filter((c) => c.severity === "error");
  const rest = list.filter((c) => c.severity !== "error");
  return `${blocking.length ? checksList(blocking, counts) : ""}${
    rest.length
      ? `<details class="ob-fold" data-keep="${esc(key)}"><summary>${esc(
          `Things to review (${fmtInt(rest.length)})`,
        )}</summary>${checksList(rest)}</details>`
      : ""
  }`;
}

/** The file input as a button: the empty state's one action, or "Add more files" beside a table. */
const uploadLabel = (text, kind) =>
  `<label class="btn ${kind} ob-upload"><input type="file" class="sr" multiple accept=".csv,.parquet" data-act="pick-files"><span>${esc(
    text,
  )}</span></label>`;

/** Files being read, then the files the API refused - one box per distinct reason, naming them. */
function uploadingLines(state) {
  const reading = state.uploading.map((name) => `<div class="loading">Reading ${esc(name)}…</div>`).join("");
  const groups = new Map();
  for (const { name, error } of state.uploadErrors || []) {
    const key = `${(error && error.code) || ""}|${(error && error.message) || ""}`;
    if (!groups.has(key)) groups.set(key, { error, names: [] });
    groups.get(key).names.push(name);
  }
  const refused = [...groups.values()]
    .map(({ error, names }) =>
      errorBox(error, {
        title: `${listJoin(names)} could not be added. ${plainError(error)}`,
      }),
    )
    .join("");
  return reading + refused;
}

// ---------------------------------------------------------------------------------------------
// Step 1: Sources
// ---------------------------------------------------------------------------------------------

const topCandidate = (entry) => ((entry.profile && entry.profile.role_candidates) || [])[0] || null;

/**
 * The role `<select>`, behind "Change", preselected to the detector's top candidate.
 *
 * The row also carries a Confirm button while the role is only a proposal, and the step a
 * "Confirm roles" that confirms them all. Neither is decoration: `change` fires only when the user
 * picks a *different* option, so a user who agrees with the detection - the common case - would
 * otherwise have no way to say so, and `SourceSpec.role` would stay null while the row appeared
 * settled. Detection proposes and the user confirms (plan §6.1).
 */
function roleSelect(state, source, currentRole, candidates) {
  const roles = state.schema.roles.roles;
  const top = (candidates || [])[0];
  const options = Object.keys(roles)
    .map((id) => ({ id, label: roleLabel(state, id) }))
    .sort((a, b) => a.label.localeCompare(b.label))
    .map(({ id, label }) => {
      const selected = currentRole ? currentRole === id : top ? top.role === id : false;
      return `<option value="${esc(id)}" title="${esc(roles[id].description)}"${selected ? " selected" : ""}>${esc(
        label,
      )}</option>`;
    })
    .join("");
  return `<div class="control sel ob-sel"><select data-act="set-role" data-source="${esc(
    source.source_id,
  )}" aria-label="${esc(`Kind of table for ${source.file_name}`)}">${options}</select></div>`;
}

function roleCell(state, entry) {
  const { source, profile } = entry;
  const top = topCandidate(entry);
  const shown = source.role || (top ? top.role : "");
  const spec = shown ? state.schema.roles.roles[shown] : null;
  const confirm = source.role
    ? ""
    : `<button type="button" class="btn secondary sm" data-act="confirm-role" data-source="${esc(
        source.source_id,
      )}">Confirm</button>`;
  return `<div class="ob-role"><span class="ob-role-name">${esc(roleLabel(state, shown))}</span>${
    source.role ? "" : `<span class="ob-tag">Suggested</span>`
  }${confirm}<details class="ob-change" data-keep="${esc(`role:${source.source_id}`)}"><summary>Change</summary>${roleSelect(
    state,
    source,
    source.role,
    profile.role_candidates,
  )}${spec ? `<p class="fhint">${esc(spec.description)}</p>` : ""}</details></div>`;
}

/** The glossary of roles, for the "?" on the Role header. */
function roleGlossary(state) {
  const roles = state.schema.roles.roles;
  return Object.keys(roles)
    .map((id) => `${roleLabel(state, id)}: ${roles[id].description}`)
    .sort()
    .join(" · ");
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

/** The row menu: "Remove" lives here, and asks before it deletes (`removeDialog`). */
function rowMenu(state, source) {
  const open = state.rowMenu === source.source_id;
  return `<div class="menu ob-rowmenu"><button type="button" class="btn quiet sm" data-act="row-menu" data-source="${esc(
    source.source_id,
  )}" aria-haspopup="true" aria-expanded="${open}" aria-label="${esc(
    `More actions for ${source.file_name}`,
  )}">More</button><div class="menu-pop right" role="menu"${open ? "" : " hidden"}><button type="button" role="menuitem" data-act="ask-delete" data-source="${esc(
    source.source_id,
  )}">Remove from this client</button></div></div>`;
}

function removeDialog(state) {
  const entry = state.confirmDelete
    ? state.sources.find((candidate) => candidate.source.source_id === state.confirmDelete)
    : null;
  if (!entry) return "";
  const { source } = entry;
  return `<div class="dialog ob-dialog" role="alertdialog" aria-labelledby="ob-del-t" aria-describedby="ob-del-d"><p class="ob-dialog-t" id="ob-del-t">${esc(
    `Remove ${source.file_name} from this client?`,
  )}</p><p id="ob-del-d">This cannot be undone. Upload the file again if you need it later.</p><div class="btn-row"><button type="button" class="btn danger confirm" data-act="delete-source" data-source="${esc(
    source.source_id,
  )}">Remove</button><button type="button" class="btn quiet" data-act="cancel-delete">Cancel</button></div></div>`;
}

/** A calm note when two files look like the same table: the same role and the same columns. */
function duplicateNote(state) {
  const groups = new Map();
  for (const entry of state.sources) {
    const top = topCandidate(entry);
    const role = entry.source.role || (top ? top.role : "");
    const columns =
      entry.profile && entry.profile.profile
        ? entry.profile.profile.columns.map((column) => column.name).sort().join("|")
        : "";
    if (!role || !columns) continue;
    const key = `${role}::${columns}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry.source.file_name);
  }
  const twins = [...groups.values()].filter((names) => names.length > 1);
  if (!twins.length) return "";
  const named = (names) => {
    const counts = new Map();
    for (const name of names) counts.set(name, (counts.get(name) || 0) + 1);
    return listJoin([...counts].map(([name, n]) => (n > 1 ? `${name} (${fmtInt(n)} copies)` : name)));
  };
  const shown = twins.slice(0, MAX_TWIN_GROUPS).map(named);
  const more = twins.length - shown.length;
  const groupsText = `${shown.join("; ")}${more > 0 ? `; and ${plural(more, "more group", "more groups")}` : ""}`;
  return `<div class="ob-note" role="note"><p class="ob-note-t">Some files look like the same table</p><p>${esc(
    `${groupsText}. Each group has the same kind of table and the same columns. If one is an older or newer extract, remove the one you do not need.`,
  )}</p></div>`;
}

function sourceRows(state, entityConfirmed, entityRole, { replay = false } = {}) {
  return state.sources.map((entry) => {
    const { source, profile } = entry;
    const keyTop = (profile.key_candidates || [])[0];
    const timeTop = (profile.time_candidates || [])[0];
    const replayed = replay ? replayedEntry(state, source.source_id) : null;
    return [
      esc(source.file_name),
      dash(source.rows, fmtInt),
      replay ? (replayed ? esc(roleLabel(state, replayed.role)) : EM_DASH) : roleCell(state, entry),
      esc(dash(keyTop && keyTop.column)),
      esc(dash(timeTop && timeTop.column)),
      coverageBadge(entityConfirmed, source.role === entityRole, profile.key_candidates),
      rowMenu(state, source),
    ];
  });
}

function sourceColumns(state) {
  return [
    { label: "File" },
    { label: "Rows", num: true },
    { label: "Role" },
    { label: "ID column", more: true },
    { label: "Date column", more: true },
    { label: `${cap(entityNoun(state))}s matched`, more: true },
    { label: "" },
  ];
}

/** Every step past this point reads `state.schema`; none may run before it has loaded, so each one
 * starts with this same guard rather than letting a null dereference reach the page. */
function schemaPending(state, id, title, line) {
  if (state.schema) return null;
  const body = state.schemaError
    ? errorBox(state.schemaError, { title: "The table catalogue could not be loaded." })
    : `<div class="loading">Loading…</div>`;
  return stepShell(id, title, line, false, state.open === id, body);
}

export function sourcesStep(state) {
  const pending = schemaPending(state, "sources", "Your tables", "One file per table.");
  if (pending) return pending;
  if (state.replay) return replaySourcesStep(state);
  const entityRole = entityRoleId(state.schema);
  const entityConfirmed = state.sources.some((entry) => entry.source.role === entityRole);
  const proposed = state.sources.filter((entry) => !entry.source.role && topCandidate(entry));
  const done = entityConfirmed && !proposed.length;
  const entity = entityNoun(state);
  if (!state.sources.length && !state.uploading.length) {
    const body = state.sourcesLoading
      ? `<div class="loading">Loading this client's tables…</div>`
      : `${uploadingLines(state)}${state.sourcesError ? errorBox(state.sourcesError) : ""}<div class="empty-state"><p class="es-t">Add your raw tables</p><p>${esc(
          `One CSV or Parquet file per table (${entity}s, activity, bills…). We will suggest what each one is.`,
        )}</p>${uploadLabel("Add files", "primary")}</div>`;
    return stepShell("sources", "Your tables", "One file per table.", false, state.open === "sources", body);
  }
  const tableHtml = dataTable(sourceColumns(state), sourceRows(state, entityConfirmed, entityRole)).replace(
    ">Role</th>",
    `>Role ${toggletip(roleGlossary(state), "Role")}</th>`,
  );
  const primary = proposed.length
    ? `<button type="button" class="btn primary" data-act="confirm-roles"${state.rolesBusy ? " disabled" : ""}>${
        state.rolesBusy ? "Confirming…" : `Confirm roles (${fmtInt(proposed.length)})`
      }</button>`
    : "";
  const body = `
    <div class="btn-row ob-toolbar">${primary}${uploadLabel("Add more files", "secondary")}<span class="fhint">CSV or Parquet, one file per table</span>${
      // Once every table has its role, moving on is this step's one primary action.
      done ? `<span class="spacer"></span>${stepLink("mapping", "Continue to Mapping", "primary")}` : ""
    }</div>
    ${uploadingLines(state)}
    ${state.sourcesError ? errorBox(state.sourcesError) : ""}
    ${duplicateNote(state)}
    ${removeDialog(state)}
    <div data-keep-scope="sources">${tableHtml}</div>
    ${
      !entityConfirmed && state.sources.length
        ? `<p class="fhint">${esc(
            `Confirm one file as the ${cap(entity)} table (one row per ${entity}) before matching columns.`,
          )}</p>`
        : ""
    }
  `;
  const line = done
    ? `${plural(state.sources.length, "table", "tables")} · roles confirmed ✓`
    : proposed.length
      ? `${plural(proposed.length, "suggested role", "suggested roles")} to confirm`
      : "One file per table.";
  return stepShell("sources", "Your tables", line, done, state.open === "sources", body);
}

/** The replay's row for one of this month's files, or `null` while it has not answered. */
function replayedEntry(state, sourceId) {
  const response = state.replay && state.replay.response;
  return (response && response.sources.find((entry) => entry.source_id === sourceId)) || null;
}

/**
 * What the saved setup builds, folded: its features and why no outcome is built when scoring
 * (`docs/ONBOARDING.md` section 8). Read only: a feature that differed from the ones the model was
 * fitted on would score nothing it knows.
 */
function replayRecipe(state) {
  const spec = state.replay.spec;
  const features = (spec.feature_spec && spec.feature_spec.features) || [];
  const rows = features
    .map(
      (feature) =>
        `<li><span class="ob-fdesc">${esc(feature.description || windowLabel(feature))}</span><span class="ob-fname">${esc(
          feature.name,
        )}</span></li>`,
    )
    .join("");
  const label = spec.label_spec;
  return `<details class="ob-fold" data-keep="recipe"><summary>What this setup builds</summary>
    ${rows ? `<ul class="ob-flist">${rows}</ul>` : `<div class="empty">The saved setup builds no measures.</div>`}
    <p class="fhint">${esc(
      label
        ? `${label.name} is not built when scoring: there is nothing yet to look forward to. Scoring stands at one date, the end of this month's data.`
        : "The saved setup builds no outcome.",
    )}</p></details>`;
}

/**
 * Score mode's one visible step (prototype `08`): this month's files, each with the role the saved
 * setup gives it. There is no role to choose - the setup made that decision last month - so the role
 * is shown, not offered; a file the setup had and this month's upload does not is listed with the
 * API's own words about it.
 */
function replaySourcesStep(state) {
  const replay = state.replay;
  const entityRole = entityRoleId(state.schema);
  const entityConfirmed = state.sources.some((entry) => entry.source.role === entityRole);
  const response = replay.response;
  const unmatched = response ? response.unmatched : [];
  const unused = response
    ? state.sources.filter((entry) => response.unused_source_ids.includes(entry.source.source_id))
    : [];
  const hasTables = state.sources.length || state.uploading.length;
  const body = `
    ${
      hasTables
        ? `<div class="btn-row ob-toolbar">${uploadLabel("Add more files", "secondary")}<span class="fhint">The same tables as last month, as CSV or Parquet</span></div>`
        : `<div class="empty-state"><p class="es-t">Upload this month's extract</p><p>The same tables you trained with, one CSV or Parquet file per table. They are read exactly the way last month's were.</p>${uploadLabel(
            "Add files",
            "secondary",
          )}</div>`
    }
    ${uploadingLines(state)}
    ${state.sourcesError ? errorBox(state.sourcesError) : ""}
    ${replay.error ? errorBox(replay.error) : ""}
    ${replay.running ? `<div class="loading">Matching this month's tables to last month's setup…</div>` : ""}
    ${removeDialog(state)}
    ${
      state.sources.length
        ? `<div data-keep-scope="replay-sources">${dataTable(
            sourceColumns(state),
            sourceRows(state, entityConfirmed, entityRole, { replay: true }),
          )}</div>`
        : ""
    }
    ${
      unmatched.length
        ? `<div class="vlist" role="alert">${unmatched
            .map(
              (entry) =>
                `<div class="vitem"><span class="pill bad">${esc(entry.file_name)}</span><div><div class="vmsg">${esc(
                  entry.message,
                )}</div></div></div>`,
            )
            .join("")}</div>`
        : ""
    }
    ${
      unused.length
        ? `<p class="fhint">${esc(
            `Not part of last month's setup, so not read: ${unused.map((entry) => entry.source.file_name).join(", ")}.`,
          )}</p>`
        : ""
    }
    ${replayRecipe(state)}
  `;
  const done = !!response && !unmatched.length && response.sources.length > 0;
  const line = done
    ? `${plural(state.sources.length, "table", "tables")} added ✓`
    : "The same tables as last month, this month's extract.";
  return stepShell("sources", "Add this month's files", line, done, state.open === "sources", body);
}

// ---------------------------------------------------------------------------------------------
// Step 2: Mapping
// ---------------------------------------------------------------------------------------------

/**
 * The standard names a source in this role may fill, in the order `engine.onboarding.mapping`'s own
 * `_targets()` builds them: the role's required columns, then the use case's columns for an entity
 * table or the role's typical columns for an event log, then the role's optional ones.
 *
 * A role's `typical_columns` are `StandardColumn` objects, not names: each one's `name` is the option
 * value and its description the label (reading the objects as names drew "[object Object]"). The
 * list is deduplicated, because `MappingSpec` refuses a standard column claimed twice.
 */
function mappingTargets(state, role) {
  const spec = state.schema.roles.roles[role];
  if (!spec) return [];
  const columns = state.schema.standard_schema.columns || [];
  const defined = new Map(columns.map((column) => [column.name, column]));
  for (const column of spec.typical_columns || []) {
    if (column && column.name && !defined.has(column.name)) defined.set(column.name, column);
  }
  const required = requiredTargetNames(state, spec);
  const nameOf = (item) => (item && typeof item === "object" ? item.name : item);
  const ordered = [
    ...(spec.required_columns || []),
    ...(spec.kind === "event" ? (spec.typical_columns || []).map(nameOf) : columns.map(nameOf)),
    ...(spec.optional_columns || []),
  ];
  const targets = [];
  const seen = new Set();
  for (const name of ordered) {
    if (!name || seen.has(name)) continue;
    seen.add(name);
    const column = defined.get(name) || null;
    const label = column && column.description ? firstSentence(column.description) : plainName(state, name);
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

/** The standard column definition a target name refers to: the use case's, or the role's typical. */
function standardColumnOf(state, role, name) {
  if (!name) return null;
  const own = (state.schema.standard_schema.columns || []).find((c) => c.name === name);
  if (own) return own;
  const spec = state.schema.roles.roles[role];
  return ((spec && spec.typical_columns) || []).find((c) => c && c.name === name) || null;
}

function columnTargetOf(mapping, rawName) {
  const found = (mapping.columns || []).find((c) => c.source === rawName);
  return found ? found.standard : "";
}

/** `mapped` says whether this raw value has a decision at all; `current` is that decision, which may
 * legitimately be `null` ("leave it blank"). The two are separate arguments because they are separate
 * facts: "nobody has decided" and "someone decided to blank it" are both falsy, and collapsing them
 * marks two of these options selected at once. */
function valueMapRow(sourceId, standardName, standardColumn, rawValue, mapped, current) {
  const aliasTargets = Object.keys(standardColumn.value_aliases || {});
  const options = [
    `<option value="__keep__"${!mapped ? " selected" : ""}>Keep</option>`,
    `<option value="__null__"${mapped && current === null ? " selected" : ""}>Leave blank</option>`,
    ...aliasTargets.map(
      (target) =>
        `<option value="${esc(target)}"${
          mapped && current === target ? " selected" : ""
        }>${esc(target)}</option>`,
    ),
  ].join("");
  return `<div class="ob-vmap"><span class="colchip">${esc(rawValue)}</span><div class="control sel"><select data-act="set-value-map" data-source="${esc(
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
  return `<details class="adv-wrap" data-keep="${esc(`vmap:${sourceId}:${column.name}`)}"><summary>Values<span class="n">${esc(
    plural(values.size, "value", "values"),
  )}</span></summary>${rows}</details>`;
}

/** Up to three example values on one line; a column whose samples were all masked says so. */
function exampleValues(column) {
  const samples = (column.sample_values || []).filter(present).map(String);
  if (!samples.length) return EM_DASH;
  if (samples.every((value) => /^\[[A-Z_ ]+\]$/.test(value))) return `<span class="ob-muted">Hidden (personal data)</span>`;
  const shown = samples.slice(0, 3).map((value) => (value.length > 40 ? `${value.slice(0, 40)}…` : value));
  return `<span class="ob-samples">${esc(shown.join(", "))}</span>`;
}

function mappingRow(state, sourceId, mapping, role, column, flagged) {
  const targets = mappingTargets(state, role);
  const current = columnTargetOf(mapping, column.name);
  const options = [
    `<option value=""${current === "" ? " selected" : ""}>Not used</option>`,
    ...targets.map(
      (t) => `<option value="${esc(t.value)}"${current === t.value ? " selected" : ""}>${esc(t.label)}</option>`,
    ),
  ].join("");
  const decided = (mapping.columns || []).find((c) => c.source === column.name);
  const standardColumn = standardColumnOf(state, role, current);
  const check = flagged.includes(column.name) ? `<span class="pill warn ob-checkme">Check this match</span>` : "";
  return [
    `<span class="ob-colname">${esc(column.name)}</span>`,
    `<div class="ob-mapto"><div class="control sel ob-sel"><select data-act="set-mapping" data-source="${esc(
      sourceId,
    )}" data-column="${esc(column.name)}" aria-label="${esc(`${column.name} maps to`)}">${options}</select></div>${check}</div>`,
    exampleValues(column),
    esc(dash(column.inferred_type, humanizeId)),
    confidencePill(decided, state.mappingSuggested[sourceId]),
    standardColumn ? valueMapEditor(sourceId, current, standardColumn, mapping, column) || EM_DASH : EM_DASH,
  ];
}

const MAPPING_COLUMNS = [
  { label: "Your column" },
  { label: "Maps to" },
  { label: "Example values" },
  { label: "Type", more: true },
  { label: "Confidence", more: true },
  { label: "Values", more: true },
];

/**
 * One file's mapping, folded to a line ("complaints.csv · 6 columns · 5 matched · 1 needs your
 * check") until the user opens it or a match needs them. The per-card "Accept suggestions and save"
 * (`[data-act="accept-all"]`) and the saved state stay in the always-visible head.
 */
function mappingCard(state, entry) {
  const { source, profile } = entry;
  const id = source.source_id;
  const mapping = state.mappings[id];
  const saved = !!state.mappingSaved[id];
  const saving = !!state.mappingSaving[id];
  const error = state.mappingError[id];
  const columns = (profile && profile.profile && profile.profile.columns) || [];
  const flagged = flaggedColumns(state, id);
  const missing = mapping ? missingRequiredNames(mapping) : [];
  const defaultOpen = !saved && (flagged.length > 0 || missing.length > 0 || !!error);
  const open = id in (state.mapOpen || {}) ? state.mapOpen[id] : defaultOpen;
  const parts = [roleLabel(state, source.role)];
  if (mapping) {
    parts.push(plural(columns.length, "column", "columns"), `${fmtInt((mapping.columns || []).length)} matched`);
    if (flagged.length) parts.push(`${fmtInt(flagged.length)} ${flagged.length === 1 ? "needs" : "need"} your check`);
  }
  let inner;
  if (state.mappingLoading[id]) inner = `<div class="loading">Suggesting matches…</div>`;
  else if (error) inner = errorBox(error);
  else if (!mapping) inner = `<div class="empty">Not analysed yet.</div>`;
  else {
    const rows = columns.map((column) => mappingRow(state, id, mapping, source.role, column, flagged));
    inner = `<div data-keep-scope="${esc(`map:${id}`)}">${dataTable(MAPPING_COLUMNS, rows)}</div>
      ${missingRequiredBlock(state, id, mapping)}
      ${checksList(liveChecks(state, state.mappingChecks[id]))}
      <div class="btn-row ob-card-foot"><button type="button" class="btn secondary sm" data-act="save-mapping" data-source="${esc(
        id,
      )}"${saving ? " disabled" : ""}>${saving ? "Saving…" : "Save"}</button>${
        saved ? "" : `<span class="fhint">Saves the matches as shown.</span>`
      }</div>`;
  }
  return `<section class="card ob-map${flagged.length && !saved ? " flag" : ""}">
    <div class="ob-map-head">
      <div class="ob-map-t"><h3>${esc(source.file_name)}</h3><span class="ob-map-sum">${esc(parts.join(" · "))}</span></div>
      <div class="kv ob-map-state"><span class="k">${saving ? "Saving…" : saved ? "Saved" : "Not saved yet"}</span>${
        saved ? `<span class="v ob-ok" aria-hidden="true">✓</span>` : ""
      }</div>
      <button type="button" class="btn quiet sm" data-act="toggle-map" data-source="${esc(id)}" aria-expanded="${open}">${
        open ? "Hide columns" : "Review"
      }</button>
      <button type="button" class="btn secondary sm" data-act="accept-all" data-source="${esc(id)}"${
        saving ? " disabled" : ""
      }>Accept suggestions and save</button>
    </div>
    ${open ? `<div class="ob-map-body">${inner}</div>` : ""}
  </section>`;
}

/** The server's `missing_required`, narrowed by what the user has since mapped (never widened). */
function missingRequiredNames(mapping) {
  const claimed = new Set((mapping.columns || []).map((column) => column.standard));
  return (mapping.missing_required || []).filter((name) => !claimed.has(name));
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
 * warning the API raised, never raise one it did not.
 */
function missingRequiredBlock(state, sourceId, mapping) {
  const names = missingRequiredNames(mapping);
  if (!names.length) return "";
  const checks = state.mappingChecks[sourceId] || [];
  const defined = state.schema.standard_schema.columns || [];
  const items = names
    .map((name) => {
      const check = checks.find((c) => c.column === name) || null;
      const column = defined.find((c) => c.name === name) || null;
      const message = check ? check.message : column && column.description ? column.description : "";
      const suggestion = check && check.suggestion ? check.suggestion : "";
      return `<div class="vitem"><span class="pill bad">${esc(plainName(state, name))}</span><div>${
        message ? `<div class="vmsg">${esc(message)}</div>` : ""
      }${suggestion ? `<div class="vsug">${esc(suggestion)}</div>` : ""}</div></div>`;
    })
    .join("");
  const head = `${plural(names.length, "required column", "required columns")} not matched yet`;
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${items}</div>`;
}

/** The note "Accept suggestions and save all" leaves when it stopped on matches to check. */
function saveAllNote(state) {
  const run = state.saveAll;
  if (!run || run.running || !run.stopped.length) return "";
  const names = run.stopped
    .map((id) => state.sources.find((entry) => entry.source.source_id === id))
    .filter(Boolean)
    .map((entry) => entry.source.file_name);
  return `<div class="ob-note" role="status"><p class="ob-note-t">${esc(
    `Saved ${plural(run.saved, "table", "tables")}. ${plural(names.length, "table needs", "tables need")} you to check a match first.`,
  )}</p><p>${esc(
    `Look at the matches marked "Check this match" in ${listJoin(names)}. Change any that are wrong, then press Save on that table.`,
  )}</p></div>`;
}

/**
 * Score mode's Mapping step: one summary line when every file has the columns last month's mapping
 * read, and reopened on exactly the files that do not - each under the API's own sentence about what
 * went missing. The mapping table itself is the train-mode one, over the replayed copy.
 */
function replayMappingStep(state) {
  const response = state.replay.response;
  if (!state.replay.uploadedIds.length) return "";
  const reopened = response ? response.sources.filter((entry) => entry.missing_columns.length) : [];
  let body;
  let line;
  if (!response) {
    line = "Matched automatically once this month's files are in.";
    body = `<div class="empty">Add this month's files first; they are matched to last month's setup automatically.</div>`;
  } else if (!reopened.length) {
    line = response.unmatched.length
      ? `${plural(response.sources.length, "table", "tables")} matched so far`
      : `${plural(response.sources.length, "table", "tables")} matched last month's setup ✓`;
    body = `<div class="empty">Every table has the columns last month's mapping read, so the mapping is replayed exactly as it was.</div>`;
  } else {
    line = `${plural(reopened.length, "table needs", "tables need")} a look`;
    body = reopened
      .map((replayed) => {
        const entry = state.sources.find((candidate) => candidate.source.source_id === replayed.source_id);
        return `<div class="vlist" role="alert"><div class="vhead">${esc(replayed.message)}</div></div>${
          entry ? mappingCard(state, entry) : ""
        }`;
      })
      .join("");
  }
  const done = !!response && !reopened.length && !response.unmatched.length;
  return stepShell("mapping", "Match to last month's setup", line, done, state.open === "mapping", body);
}

export function mappingStep(state) {
  const pending = schemaPending(state, "mapping", "Match your columns", "Your columns, matched to ours.");
  if (pending) return pending;
  if (state.replay) return replayMappingStep(state);
  if (!state.sources.length) return "";
  const mappable = state.sources.filter((entry) => entry.source.role);
  const savedCount = mappable.filter((entry) => state.mappingSaved[entry.source.source_id]).length;
  const done = mappable.length > 0 && savedCount === mappable.length;
  const busy =
    (state.saveAll && state.saveAll.running) ||
    mappable.some((entry) => state.mappingLoading[entry.source.source_id] || state.mappingSaving[entry.source.source_id]);
  const body = mappable.length
    ? `<div class="btn-row ob-toolbar">${
        done
          ? stepLink("features", "Continue to Measures")
          : `<button type="button" class="btn primary" data-act="save-all-mappings"${busy ? " disabled" : ""}>${
              state.saveAll && state.saveAll.running ? "Saving…" : "Accept suggestions and save all"
            }</button><span class="fhint">${esc(
              "Matches we are less sure of (below 85%) are left for you to check.",
            )}</span>`
      }</div>
      ${saveAllNote(state)}
      ${mappable.map((entry) => mappingCard(state, entry)).join("")}`
    : `<div class="empty">Confirm what each file is first.</div>${stepLink("sources", "Go to Your tables")}`;
  const line = done
    ? `${plural(mappable.length, "table", "tables")} matched and saved ✓`
    : mappable.length
      ? `${fmtInt(savedCount)} of ${plural(mappable.length, "table", "tables")} saved`
      : "Your columns, matched to ours.";
  return stepShell("mapping", "Match your columns", line, done, state.open === "mapping", body);
}

// ---------------------------------------------------------------------------------------------
// Step 3: Features & label
// ---------------------------------------------------------------------------------------------

function windowLabel(feature) {
  return present(feature.window_days) ? `last ${fmtInt(feature.window_days)} days` : "all history";
}

function featureRow(state, feature) {
  const checked = state.featureChecked[feature.name] !== false;
  return `<label class="check ob-feature"><input type="checkbox" data-act="toggle-feature" data-feature="${esc(
    feature.name,
  )}"${checked ? " checked" : ""}><span class="ob-ftext"><span class="ob-fdesc">${esc(
    feature.description || `${humanizeId(feature.function)} of ${roleLabel(state, feature.role)}`,
  )}</span><span class="ob-fname">${esc(feature.name)}</span></span><span class="pill">${esc(
    windowLabel(feature),
  )}</span></label>`;
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
        `<div class="ob-fgroup"><p class="ob-fgroup-t">${esc(`From ${roleLabel(state, role)}`)}</p>${features
          .map((f) => featureRow(state, f))
          .join("")}</div>`,
    )
    .join("");
}

function addFeatureDialog(state) {
  if (!state.featureForm) return "";
  const fs = state.featureSchema;
  const shell = (inner) =>
    `<div class="dialog ob-dialog ob-wide" role="dialog" aria-labelledby="ob-add-t"><p class="ob-dialog-t" id="ob-add-t">Add a measure</p>${inner}</div>`;
  if (!fs) {
    return shell(
      state.vocabularyError
        ? errorBox(state.vocabularyError)
        : `<div class="loading">Loading the choices…</div>`,
    );
  }
  const entityRole = entityRoleId(state.schema);
  const roles = [...new Set(state.sources.filter((e) => e.source.role && e.source.role !== entityRole).map((e) => e.source.role))];
  const draft = state.featureForm;
  const blank = (value) => `<option value=""${value ? "" : " selected"}>Choose…</option>`;
  const fnOptions = fs.function.map((f) => `<option value="${esc(f)}"${draft.function === f ? " selected" : ""}>${esc(humanizeId(f))}</option>`).join("");
  const roleOptions = roles.map((r) => `<option value="${esc(r)}"${draft.role === r ? " selected" : ""}>${esc(roleLabel(state, r))}</option>`).join("");
  const opOptions = fs.where
    .map((op) => `<option value="${esc(op)}"${draft.whereOp === op ? " selected" : ""}>${esc(humanizeId(op))}</option>`)
    .join("");
  const input = (field, value, label, extra = "") =>
    `<div class="field"><label class="sub" for="ob-f-${esc(field)}">${esc(label)}</label><div class="control"><input id="ob-f-${esc(
      field,
    )}" data-act="feature-field" data-field="${esc(field)}" value="${esc(value)}"${extra}></div></div>`;
  const select = (field, options, label) =>
    `<div class="field"><label class="sub" for="ob-f-${esc(field)}">${esc(label)}</label><div class="control sel"><select id="ob-f-${esc(
      field,
    )}" data-act="feature-field" data-field="${esc(field)}">${options}</select></div></div>`;
  const ready = draft.name && draft.role && draft.function;
  return shell(`<fieldset class="ob-fields"><legend class="sr">The measure</legend>
      ${input("description", draft.description || "", "What it measures")}
      ${input("name", draft.name, "Short name (letters, digits and _)", ` pattern="${esc(fs.namePattern)}"`)}
      ${select("role", blank(draft.role) + roleOptions, "From which table")}
      ${select("function", blank(draft.function) + fnOptions, "How to count it")}
      ${input("column", draft.column || "", "Column (leave blank to count rows)")}
      <div class="field xs"><label class="sub" for="ob-f-window_days">Last N days</label><div class="control"><input id="ob-f-window_days" type="number" data-act="feature-field" data-field="window_days" min="1" value="${esc(
        present(draft.window_days) ? draft.window_days : "",
      )}" aria-describedby="ob-f-window-help"></div><span class="fhint" id="ob-f-window-help">Blank for all history</span></div>
    </fieldset>
    <details class="adv" data-keep="feature-filter"><summary>Advanced: only count some rows</summary>
      <div class="frow">
        ${input("whereColumn", draft.whereColumn || "", "Column")}
        ${select("whereOp", `<option value=""${draft.whereOp ? "" : " selected"}>No filter</option>${opOptions}`, "Is")}
        ${input("whereValue", draft.whereValue || "", "Value")}
      </div>
    </details>
    <div class="btn-row"><button type="button" class="btn primary" data-act="save-feature"${ready ? "" : " disabled"}>Add measure</button><button type="button" class="btn quiet" data-act="cancel-add-feature">Cancel</button>${
      ready ? "" : `<span class="reason">Fill in the name, the table and how to count it.</span>`
    }</div>`);
}

function labelSentence(state) {
  const label = state.label;
  if (!label) return `<div class="empty">This use case has no default outcome - the dataset will be built for scoring only.</div>`;
  const phrase = LABEL_TYPE_PHRASE[label.type] || { outcome: humanizeId(label.type), rule: "" };
  const entity = entityNoun(state);
  const sentence =
    phrase.rule && present(label.horizon_days)
      ? `<p class="ob-line">${esc(`A ${entity} counts as ${phrase.outcome} if they ${phrase.rule}`)} <span class="control ob-num"><input type="number" min="1" data-act="set-horizon" value="${esc(
          label.horizon_days,
        )}" aria-label="Days after the prediction date"></span> ${esc("days after the prediction date.")}</p>`
      : `<p class="ob-line">${esc("The outcome is read from a column your data already records.")}</p>`;
  return `${sentence}${label.description ? `<p class="fhint">${esc(label.description)}</p>` : ""}`;
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

const snapshotNoun = (frequency) =>
  SNAPSHOT_NOUN[frequency] || `${humanizeId(frequency || "snapshot").toLowerCase()} dates`;

/** "Learn from the last 12 month-ends", with the three settings behind "Change". */
function snapshotSettings(state) {
  const ss = state.snapshotSchema;
  const snap = state.snapshot;
  if (!ss || !snap) {
    return state.vocabularyError
      ? errorBox(state.vocabularyError)
      : `<div class="loading">Loading…</div>`;
  }
  const periodic = snap.mode === "periodic";
  const line = periodic
    ? present(snap.max_snapshots)
      ? `Learn from the last ${fmtInt(snap.max_snapshots)} ${snapshotNoun(snap.frequency)}.`
      : `Learn from the most recent ${snapshotNoun(snap.frequency)}.`
    : `One row per ${entityNoun(state)}, as of the end of your data.`;
  const modeOptions = ss.mode
    .map((m) => `<option value="${esc(m)}"${snap.mode === m ? " selected" : ""}>${esc(humanizeId(m))}</option>`)
    .join("");
  const freqOptions = ss.frequency
    .map((f) => `<option value="${esc(f)}"${snap.frequency === f ? " selected" : ""}>${esc(humanizeId(f))}</option>`)
    .join("");
  return `<p class="ob-line">${esc(line)}</p><details class="ob-change" data-keep="snapshots"><summary>Change</summary><div class="frow ob-fields">
    <div class="field"><label class="sub" for="ob-snap-mode">Dates</label><div class="control sel"><select id="ob-snap-mode" data-act="set-snapshot" data-field="mode">${modeOptions}</select></div></div>
    ${
      periodic
        ? `<div class="field"><label class="sub" for="ob-snap-freq">How often</label><div class="control sel"><select id="ob-snap-freq" data-act="set-snapshot" data-field="frequency">${freqOptions}</select></div></div>
    <div class="field xs"><label class="sub" for="ob-snap-max">How many</label><div class="control"><input id="ob-snap-max" type="number" data-act="set-snapshot" data-field="max_snapshots"${numericBounds(
      ss.maxSnapshots,
    )} value="${esc(present(snap.max_snapshots) ? snap.max_snapshots : "")}"></div></div>`
        : ""
    }
  </div></details>`;
}

/** A 0-100 track for a measured fraction, and nothing at all for one that was not measured: a
 * zero-length bar beside an em dash reads as "we measured this and it came out at nought", which
 * is precisely the sentence house rule 2 exists to prevent. */
function rateBar(fraction, label) {
  if (!present(fraction)) return "";
  return barTrack(fraction * 100, `${label} ${fmtPct(fraction)}`);
}

/** The preview in one sentence: how big the sample is, how many had the outcome, how many measures
 * are often empty. Every number is the preview's own. */
function previewSentence(state, preview) {
  const snaps = preview.per_snapshot || [];
  const sized = snaps.filter((s) => present(s.entities));
  const size = sized.length ? Math.max(...sized.map((s) => s.entities)) : (preview.rows || []).length;
  const rated = snaps.filter((s) => present(s.positive_rate) && present(s.entities) && s.entities > 0);
  const weight = rated.reduce((sum, s) => sum + s.entities, 0);
  const rate = weight ? rated.reduce((sum, s) => sum + s.positive_rate * s.entities, 0) / weight : null;
  const empty = Object.values(preview.feature_null_rates || {}).filter((r) => present(r) && r >= OFTEN_EMPTY).length;
  const phrase = state.label ? LABEL_TYPE_PHRASE[state.label.type] : null;
  const parts = [`Sample of ${plural(size, entityNoun(state), `${entityNoun(state)}s`)}`];
  const tail = [];
  if (present(rate) && phrase) tail.push(`${fmtPct(rate)} counted as ${phrase.outcome}`);
  tail.push(empty ? `${plural(empty, "measure is", "measures are")} often empty` : "no measure is often empty");
  return `${parts.join("")}: ${tail.join("; ")}.`;
}

function previewResults(state) {
  if (state.previewLoading) return `<div class="loading">Building a small sample…</div>`;
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
    rateBar(rate, `${name} empty`),
  ]);
  const snapshots = (preview.per_snapshot || []).map((s) => [
    dash(s.date),
    dash(s.entities, fmtInt),
    dash(s.positive_rate, fmtPct),
  ]);
  const blocked = (preview.checks || []).some((c) => c.severity === "error");
  return `<div class="ob-preview">
    <p class="ob-lead-s">${esc(previewSentence(state, preview))}</p>
    ${checksFolded(preview.checks, "preview-review")}
    ${blocked ? "" : `<div class="btn-row">${stepLink("build", "Continue to Build")}</div>`}
    <details class="ob-fold" data-keep="preview"><summary>Show preview details</summary>
      <p class="ob-sub-t">First rows</p>${rowsTable}
      <p class="ob-sub-t">How often each measure is empty</p>${
        nullRates.length
          ? `<div class="bars">${nullRates
              .map(([name, pct, bar]) => `<div class="brow"><span class="lab">${esc(name)}</span>${bar}<span class="pct">${esc(pct)}</span></div>`)
              .join("")}</div>`
          : `<div class="empty">No measures to check yet.</div>`
      }
      <p class="ob-sub-t">By date</p>${
        snapshots.length ? table(["Date", "Rows", "Outcome rate"], snapshots) : `<div class="empty">One date only.</div>`
      }
    </details>
  </div>`;
}

/**
 * What still stands between this recipe and a spec the API will accept, in business language, each
 * with the step that fixes it.
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
  if (!state.schema) return [{ text: "The table catalogue has not loaded yet." }];
  const entityRole = entityRoleId(state.schema);
  if (!state.sources.some((entry) => entry.source.role === entityRole)) {
    reasons.push({ text: `Confirm one file as the ${cap(entityNoun(state))} table first.`, step: "sources", go: "Go to Your tables" });
  }
  const unsaved = state.sources.filter(
    (entry) => entry.source.role && !state.mappingSaved[entry.source.source_id],
  );
  if (unsaved.length) {
    // Every source with a confirmed role goes into the recipe, so every one of them needs a saved
    // mapping - `OnboardingSpec.mapping_ids` is "one per source", and a spec that names a source
    // with no mapping is one `POST /datasets` can only refuse.
    reasons.push({
      text: `Save the mapping for ${plural(unsaved.length, "table", "tables")} first.`,
      step: "mapping",
      go: "Go to Match your columns",
    });
  }
  if (!state.snapshot) reasons.push({ text: "The date settings have not loaded yet." });
  return reasons;
}

/** What still stands between this month's files and a score-mode build: the same idea as
 * `recipeBlockers`, answered from the replay - whose own words are used wherever it gave any. */
function replayBlockers(state) {
  const replay = state.replay;
  if (!state.schema) return [{ text: "The table catalogue has not loaded yet." }];
  if (!replay.uploadedIds.length) return [{ text: "Add this month's files first.", step: "sources", go: "Add files" }];
  if (replay.running) return [{ text: "Matching this month's tables to last month's setup…" }];
  const response = replay.response;
  if (!response) return [{ text: "This month's tables have not been matched to last month's setup yet." }];
  const reasons = response.unmatched.map((entry) => ({ text: entry.message, step: "sources", go: "Go to files" }));
  const reopened = response.sources.filter((entry) => entry.missing_columns.length);
  if (reopened.length) {
    reasons.push({
      text: `Review the matches for ${listJoin(reopened.map((entry) => entry.file_name))}.`,
      step: "mapping",
      go: "Review",
    });
  }
  return reasons;
}

/** A disabled action's reasons, in the `.reason` the Setup form uses, each with its way there. */
const blockerLine = (reasons) =>
  reasons.length
    ? `<span class="reason">${esc(reasons.map((r) => r.text).join(" "))}</span>${reasons
        .filter((r) => r.step)
        .map((r) => stepLink(r.step, r.go))
        .join("")}`
    : "";

export function featuresStep(state) {
  const pending = schemaPending(state, "features", "Measures and outcome", "What the model learns from, and what it predicts.");
  if (pending) return pending;
  if (state.replay || !state.sources.length) return "";
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
  const ticked = state.features.filter((f) => state.featureChecked[f.name] !== false).length;
  const list = state.features.length
    ? `<details class="ob-fold" data-keep="features"><summary><span>${esc(
        `${plural(ticked, "measure", "measures")} will be built from your tables ✓`,
      )}</span><span class="ob-link">Review</span></summary>${featuresByRole(state)}</details>`
    : `<div class="empty">No suggested measures for the matched tables yet.</div>`;
  const body = `
    <div class="ob-sub"><p class="ob-sub-t">Measures</p>${list}
      ${state.featureForm ? addFeatureDialog(state) : `<button type="button" class="btn secondary sm" data-act="open-add-feature">Add a measure</button>`}
    </div>
    <div class="ob-sub"><p class="ob-sub-t">Outcome</p>${labelSentence(state)}</div>
    <div class="ob-sub"><p class="ob-sub-t">Dates to learn from</p>${snapshotSettings(state)}</div>
    ${state.preview ? "" : checksFolded(state.specChecks, "spec-review")}
    <div class="btn-row ob-actions"><button type="button" class="btn primary" data-act="preview"${
      state.previewLoading || blockers.length ? " disabled" : ""
    }>${state.previewLoading ? "Building preview…" : "Preview"}</button>${blockerLine(blockers)}</div>
    ${previewResults(state)}
  `;
  const line = done
    ? `${plural(ticked, "measure", "measures")} · preview looks right ✓`
    : ticked
      ? `${plural(ticked, "measure", "measures")} · what the model learns from and predicts`
      : "What the model learns from, and what it predicts.";
  return stepShell("features", "Measures and outcome", line, done, state.open === "features", body);
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

function fileNameOf(state, sourceId) {
  const entry = state.sources.find((candidate) => candidate.source.source_id === sourceId);
  return entry ? entry.source.file_name : sourceId;
}

function coverageTable(state, report) {
  const rows = (report.sources || []).map((s) => [
    dash(fileNameOf(state, s.source_id)),
    dash(s.role, (role) => roleLabel(state, role)),
    dash(s.rows, fmtInt),
    dash(s.join_coverage, (v) => fmtPct(v)),
  ]);
  return table(["File", "Role", "Rows", `${cap(entityNoun(state))}s matched`], rows);
}

function outcomeShare(state) {
  const type = (state.label && state.label.type) || (state.replay && state.replay.spec.label_spec && state.replay.spec.label_spec.type);
  const phrase = type ? LABEL_TYPE_PHRASE[type] : null;
  return phrase ? phrase.share : "Share with the outcome";
}

function snapshotBars(state, report) {
  const snaps = report.snapshots || [];
  if (!snaps.length) return "";
  return `<div class="card"><h3>${esc(`${outcomeShare(state)}, by date`)}</h3><div class="bars">${snaps
    .map(
      (s) =>
        `<div class="brow"><span class="lab">${esc(dash(s.date))}${
          s.censored ? `<span class="ob-muted"> · left out: too recent to know the outcome</span>` : ""
        }</span>${rateBar(s.positive_rate, `${s.date} share`)}<span class="pct">${dash(
          s.positive_rate,
          (v) => fmtPct(v),
        )}</span></div>`,
    )
    .join("")}</div></div>`;
}

function droppedFeatures(report) {
  const dropped = (report.features || []).filter((f) => f.dropped);
  if (!dropped.length) return `<div class="empty">No measures were dropped.</div>`;
  return table(
    ["Measure", "Empty", "Why"],
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
  return `${errorBox(
    {
      code: cancelled ? "DATASET_BUILD_CANCELLED" : "DATASET_BUILD_FAILED",
      message:
        reason ||
        (cancelled
          ? "This build was stopped before it finished, so no dataset was produced."
          : "This build stopped before it finished, so no dataset was produced."),
    },
    { title: cancelled ? "The build was stopped before it finished." : "The build stopped before it finished." },
  )}<p class="fhint">${esc(
    cancelled
      ? "Start the build again when you are ready; nothing about the setup was changed."
      : "Look at what it names, fix it, then build again.",
  )}</p>`;
}

/**
 * Which future-data leak check the build ran - the full one or the narrowed one - and why (ruling
 * R1, DEC-870). The sentence is the engine's own (`LeakCheckRecord.summary`); this only places it.
 * A report from a build that stopped before the check, or from before M55, has none and shows none.
 */
function leakCheck(report) {
  const check = report.leak_check;
  if (!check) return "";
  return `<div class="card" data-leak-check="${esc(check.scope)}"><h3>Future-data check</h3><p class="fhint">${esc(
    check.summary,
  )}</p></div>`;
}

/** Score mode shows only the warnings that differ from the build the model was trained on. */
const checkKey = (check) => `${check.code}|${check.column || ""}`;

function splitChecks(state, checks) {
  const known = state.replay && state.replay.trainingKeys;
  if (!known) return { fresh: checks, same: [] };
  const fresh = [];
  const same = [];
  for (const check of checks) {
    if (check.severity !== "error" && known.has(checkKey(check))) same.push(check);
    else fresh.push(check);
  }
  return { fresh, same };
}

function datesInReport(state, report) {
  const manifest = state.dataset && state.dataset.manifest;
  if (manifest && manifest.snapshot_dates) return manifest.snapshot_dates.length;
  return (report.snapshots || []).filter((s) => !s.censored).length;
}

function buildReview(state) {
  const report = state.buildReport;
  if (!report) return "";
  const { fresh, same } = splitChecks(state, report.checks || []);
  const toReview = fresh.filter((c) => c.severity !== "error").length;
  const errors = present(report.error_count) ? report.error_count : fresh.filter((c) => c.severity === "error").length;
  const dates = datesInReport(state, report);
  const frequency = state.replay ? state.replay.spec.snapshot_spec && state.replay.spec.snapshot_spec.frequency : state.snapshot && state.snapshot.frequency;
  const across = dates > 1 ? ` across ${fmtInt(dates)} ${snapshotNoun(frequency)}` : "";
  const review = toReview ? `, ${plural(toReview, "thing", "things")} to review` : "";
  const verdict = report.passed
    ? `${state.replay ? "This month's dataset is ready" : "Dataset ready"}: ${plural(report.rows_out, "row", "rows")}${across}${review}.`
    : `This dataset cannot be used yet: ${plural(errors, "problem", "problems")} must be fixed.`;
  const warnings = fresh.filter((c) => c.severity !== "error");
  const blocking = fresh.filter((c) => c.severity === "error");
  return `<div class="ob-verdict${report.passed ? "" : " bad"}">
      <p class="ob-lead" role="status">${esc(verdict)}</p>
      <div class="btn-row"><button type="button" class="btn primary" data-act="use-dataset"${report.passed ? "" : " disabled"}>Use this dataset</button>${
        !report.passed ? `<span class="reason">Fix the problems below first.</span>` : ""
      }${state.inUse ? `<span class="reason">In use: the Columns step below now reads this dataset.</span>` : ""}</div>
    </div>
    ${blocking.length ? checksList(blocking, report) : ""}
    ${checksFolded(warnings, "review")}
    ${
      same.length
        ? `<p class="fhint">${esc(
            `${plural(same.length, "warning is", "warnings are")} the same as when the model was trained, so ${
              same.length === 1 ? "it is" : "they are"
            } listed under build details only.`,
          )}</p>`
        : ""
    }
    <details class="ob-fold" data-keep="build-details"><summary>Show build details</summary>
      ${buildProgress(state)}
      <div class="card"><h3>Tables read</h3>${coverageTable(state, report)}</div>
      ${snapshotBars(state, report)}
      <div class="card"><h3>Dropped measures</h3>${droppedFeatures(report)}</div>
      ${leakCheck(report)}
      ${same.length ? checksList(same) : ""}
    </details>`;
}

export function buildStep(state) {
  if (state.replay ? !state.replay.uploadedIds.length : !state.sources.length) return "";
  const done = !!(state.buildReport && state.buildReport.passed);
  const blockers = state.replay ? replayBlockers(state) : recipeBlockers(state);
  const running = state.dataset && state.dataset.status && !state.buildReport && !buildFailure(state);
  const label = state.replay ? "Build this month's dataset" : "Build dataset";
  const body = `
    ${
      !state.datasetId
        ? `<div class="btn-row ob-actions"><button type="button" class="btn primary" data-act="build"${
            state.building || blockers.length ? " disabled" : ""
          }>${state.building ? "Starting…" : esc(label)}</button>${blockerLine(blockers)}</div>`
        : ""
    }
    ${state.buildError ? errorBox(state.buildError) : ""}
    ${checksList(state.buildChecks)}
    ${running ? `<p class="ob-lead-s" role="status">Building the dataset…</p>${buildProgress(state)}` : ""}
    ${buildFailure(state) ? `${buildFailure(state)}${buildProgress(state)}` : ""}
    ${buildReview(state)}
  `;
  const line = done
    ? `${plural(state.buildReport.rows_out, "row", "rows")} built ✓`
    : state.replay
      ? "Read this month's tables with last month's setup."
      : "Build the dataset and check it.";
  return stepShell("build", state.replay ? "Build this month's dataset" : "Build and review", line, done, state.open === "build", body);
}

/** The panel after "Use this dataset": one line and a way back in. */
export function foldedLine(state) {
  const report = state.buildReport;
  const rows = report ? ` · ${plural(report.rows_out, "row", "rows")}` : "";
  return `<div class="ob-folded" role="status"><span class="ob-ok" aria-hidden="true">✓</span><span>${esc(
    `Built dataset${rows}`,
  )}</span><button type="button" class="btn quiet sm" data-act="unfold">Change</button></div>`;
}
