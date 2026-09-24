// The "Build from raw tables" panel (Phase 2 plan §9/§10, M13): the entry point `onboardingPanel`
// mounts the four collapsible steps (`steps.js`) inline wherever the host places `container`, and
// owns everything the pure render functions do not - the fetches, the working edits, the polling and
// the event wiring.
//
// This module never claims a hash route. Step 1 of the Phase 1 Setup form is a choice between
// "Upload a prepared file" (unchanged) and "Build from raw tables" (this panel, rendered inline, not
// a separate page - see plan §10). `onboardingPanel(container, options)` is a plain mount function;
// `setup.js` calls it when `ui/usecase.js` asks the registered setup source to mount (Plan A M35),
// and keeps the panel alive across the Setup form's repaints.
//
// One departure from `ui/usecase.js`'s own `bind(root)` pattern is deliberate: `usecase.js` re-binds
// after every render because `app.js` tears its whole `<main>` down and rebuilds it on each route
// change, so `root` itself is a new element each time. `container` here is never replaced - only its
// `innerHTML` is - so the two delegated listeners below are attached exactly once, at mount, and
// every click or change inside the panel is read off `event.target.closest("[data-act]")` the same
// way `usecase.js` reads `data-path`/`data-fix`. Fewer listeners to reattach, same event-delegation
// idea `usecase.js` already uses for its checkbox and fix controls.

import {
  ApiError,
  createDataset,
  createOnboardingSpec,
  createSource,
  deleteSource,
  getDataset,
  getDatasetReport,
  getDatasetSample,
  getStandardSchema,
  listMappings,
  listSources,
  previewOnboardingSpec,
  replayOnboardingSpec,
  saveMapping,
  schemaEnum,
  schemaProperty,
  setSourceRole,
  suggestMapping,
} from "./api.js";
import { buildStep, featuresStep, flaggedColumns, foldedLine, mappingStep, sourcesStep } from "./steps.js";
import { present } from "../../dom.js";

const POLL_MS = 2000;

/** `CSS.escape` where the browser has it (jsdom may not); only ids and data values reach it. */
function cssEscape(value) {
  const text = String(value);
  if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(text);
  return text.replace(/["\\\]\[]/g, (ch) => `\\${ch}`);
}

function initialState(clientId, useCaseId, replay, entity) {
  return {
    clientId,
    useCaseId,
    entity,
    open: "sources",

    // v1 UI: which folded pieces are open (`details[data-keep]`), the row menu and the remove
    // confirmation, the mapping cards opened or closed by hand, the columns the user re-pointed, and
    // the last "Accept suggestions and save all" run.
    keep: {},
    rowMenu: null,
    confirmDelete: null,
    rolesBusy: false,
    mapOpen: {},
    touched: {},
    saveAll: null,
    collapsed: false,

    // Score mode (Plan A M35): this month's files through a saved recipe. `null` in train mode.
    // `spec` is the saved `OnboardingSpec`; `uploadedIds` the files uploaded on this panel;
    // `settledIds` the mappings the user saved after a replay reopened them; `response` the last
    // `POST .../replay` answer, whose `spec_id` is what the build runs once nothing is missing.
    replay: replay
      ? {
          spec: replay.spec,
          uploadedIds: [],
          settledIds: [],
          response: null,
          running: false,
          error: null,
          // `code|column` of the checks the model's own training build raised, or null when that
          // report could not be read: score mode then shows only the warnings that differ.
          trainingKeys: replay.trainingKeys || null,
        }
      : null,

    schema: null,
    schemaError: null,

    sources: [],
    sourcesLoading: true,
    sourcesError: null,
    uploading: [],
    uploadErrors: [], // [{name, error}] - files the last upload could not add

    mappingSuggested: {}, // sourceId -> the untouched MappingSpec `suggestMapping` returned
    mappings: {}, // sourceId -> the working copy the mapping table edits
    mappingLoading: {},
    mappingError: {},
    mappingSaving: {},
    mappingSaved: {},
    mappingChecks: {},

    features: [],
    featureChecked: {},
    featureForm: null,
    featureSchema: null,
    vocabularyError: null,

    label: null,

    snapshotSchema: null,
    snapshot: null,

    specId: null,
    specChecks: [],
    preview: null,
    previewLoading: false,
    previewError: null,

    datasetId: null,
    building: false,
    dataset: null,
    buildError: null,
    buildChecks: [],
    buildReport: null,
    inUse: false,
  };
}

/**
 * Mount the panel into `container`. `clientId` and `useCaseId` name what is being onboarded;
 * `onDatasetReady(payload)` fires once the user clicks "Use this dataset" on a passed build, with
 * `{datasetId, primaryKey, target, problemType, timeColumn, manifest}` - everything the Phase 1
 * Setup form's Step 2 needs to fill itself in and hand control back to the ordinary upload flow.
 * `problemType` is a best-effort read of the target column's standard type (`boolean` -> binary
 * classification, `numeric` -> regression); it is `null`, never guessed further, when the manifest
 * carries no target or a type this panel cannot map with confidence - Step 2 already lets the host
 * choose a problem type by hand, exactly as it does today for an uploaded file. The payload also
 * carries `sample` (the dataset's own redacted `sample.json` rows) for the host's preview.
 *
 * `replay` (score mode, Plan A M35) is `{spec}`, a saved `OnboardingSpec`: the panel then takes this
 * month's files, replays the recipe onto them (`POST .../replay`), reopens the mapping step only for
 * a file missing a column last month's mapping read, and builds for scoring. Nothing else about the
 * recipe is asked again.
 */
export function onboardingPanel(
  container,
  { clientId, useCaseId, onDatasetReady, replay = null, entity = null },
) {
  const state = initialState(clientId, useCaseId, replay, entity);
  let pollTimer = null;
  let focusNext = null;
  let firstRead = true;

  /** A selector that finds "the same control" in the next paint, so a repaint never drops focus. */
  function focusKey(el) {
    if (!el || !container.contains(el) || el === container) return null;
    if (el.id) return `#${cssEscape(el.id)}`;
    const attrs = ["act", "source", "column", "field", "feature", "step", "value", "standard"]
      .filter((name) => el.dataset && el.dataset[name] !== undefined)
      .map((name) => `[data-${name}="${cssEscape(el.dataset[name])}"]`)
      .join("");
    if (attrs) return `${el.tagName.toLowerCase()}${attrs}`;
    if (el.tagName !== "SUMMARY") return null;
    const keep = el.parentElement && el.parentElement.dataset ? el.parentElement.dataset.keep : null;
    if (keep) return `details[data-keep="${cssEscape(keep)}"] > summary`;
    const step = el.parentElement && el.parentElement.dataset ? el.parentElement.dataset.step : null;
    if (step) return `.stage-d[data-step="${cssEscape(step)}"] > summary`;
    return null;
  }

  /** The key a folded piece is remembered by: its `data-keep`, or its table's scope for "Show more
   * columns" (drawn by `dom.js`'s `dataTable`, which takes no attributes). */
  function keepKeyOf(details) {
    if (details.dataset.keep) return details.dataset.keep;
    if (!details.classList.contains("tbl-more")) return null;
    const scope = details.closest("[data-keep-scope]");
    return scope ? `${scope.dataset.keepScope}:more` : null;
  }

  function rerender() {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    const refocus = focusNext || focusKey(active);
    focusNext = null;
    container.innerHTML = panelHtml(state);
    container.querySelectorAll(".stage-d").forEach((details) => {
      details.open = state.open === details.dataset.step;
      details.addEventListener("toggle", () => {
        // Both directions: collapsing the open step has to clear `state.open`, or the next render
        // reopens the step the user just closed. Opening one step closes the others (one at a time).
        // A toggle queued on an element an earlier paint already replaced is stale: ignored.
        if (!details.isConnected) return;
        if (details.open) {
          if (state.open !== details.dataset.step) {
            state.open = details.dataset.step;
            container.querySelectorAll(".stage-d").forEach((other) => {
              if (other !== details && other.open) other.open = false;
            });
          }
        } else if (state.open === details.dataset.step) state.open = null;
      });
    });
    container.querySelectorAll("details").forEach((details) => {
      const key = keepKeyOf(details);
      if (!key) return;
      if (key in state.keep) details.open = state.keep[key];
      details.addEventListener("toggle", () => {
        if (details.isConnected) state.keep[key] = details.open;
      });
    });
    if (refocus) {
      const target = container.querySelector(refocus);
      if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
    }
  }

  function panelHtml(s) {
    if (s.collapsed) return foldedLine(s);
    return `<div class="stages">${sourcesStep(s)}${mappingStep(s)}${featuresStep(s)}${buildStep(s)}</div>`;
  }

  /** Open one step (and only it), as a "Go to …" / "Continue" button asks. */
  function openStep(step) {
    state.open = step;
    focusNext = `.stage-d[data-step="${cssEscape(step)}"] > summary`;
    rerender();
  }

  // --- schema and the two generated-form vocabularies ------------------------------------------

  async function loadSchema() {
    try {
      state.schema = await getStandardSchema(useCaseId);
      state.label = state.schema.label ? { ...state.schema.label } : null;
      syncFeatures();
    } catch (error) {
      state.schemaError = error;
    }
    rerender();
  }

  /** Both loaders below share one error surface (`state.vocabularyError`): each fetches several
   * `openapi.json` properties for the same "Features & label" step, so one screen answers for both
   * rather than the mapping/window/where vocabulary and the snapshot vocabulary failing separately
   * and silently in two different corners of the form. Either one failing must not throw past this
   * function - an uncaught rejection here would crash the mount before the user ever sees a screen. */
  /** An empty `enum` means the API's OpenAPI document has no such component - the onboarding
   * routers are not mounted, or a model was renamed - and an empty `<select>` would leave the user
   * staring at a form with no choices and no reason given. A code, a business-language message and
   * a suggestion instead (house rule 3), raised as the same `ApiError` every other failure on this
   * screen is rendered from. */
  function requireChoices(name, choices) {
    if (choices && choices.length) return choices;
    throw new ApiError(
      0,
      "SCHEMA_VOCABULARY_MISSING",
      `This engine did not describe its ${name} choices, so this form cannot be filled in safely. ` +
        "Ask whoever installed it to finish enabling the onboarding endpoints, then reload the page.",
      null,
    );
  }

  async function loadFeatureSchema() {
    try {
      const [functionChoices, whereChoices, nameProp] = await Promise.all([
        schemaEnum("AggFunction"),
        schemaEnum("WhereOp"),
        schemaProperty("FeatureDef", "name"),
      ]);
      state.featureSchema = {
        function: requireChoices("feature function", functionChoices),
        where: requireChoices("filter operator", whereChoices),
        namePattern: (nameProp && nameProp.pattern) || "",
      };
    } catch (error) {
      state.vocabularyError = error;
    }
  }

  /**
   * The snapshot form's choices, bounds and starting values, all read off the API's own schema.
   *
   * `default` is the value `SnapshotDefinition` itself would have used had the request left the
   * field out, so starting the form there is showing the user what the engine is configured to do -
   * not a number chosen here. When the schema has no default there is nothing true to start from,
   * so the field starts empty and the API supplies its own on save; the earlier fallback chain
   * ending in a literal `1` was a max-snapshots setting this file invented and then presented as
   * the engine's.
   */
  async function loadSnapshotSchema() {
    try {
      const [modeChoices, freqChoices, modeProp, freqProp, maxProp] = await Promise.all([
        schemaEnum("SnapshotMode"),
        schemaEnum("SnapshotFrequency"),
        schemaProperty("SnapshotDefinition", "mode"),
        schemaProperty("SnapshotDefinition", "frequency"),
        schemaProperty("SnapshotDefinition", "max_snapshots"),
      ]);
      state.snapshotSchema = {
        mode: requireChoices("snapshot mode", modeChoices),
        frequency: requireChoices("snapshot frequency", freqChoices),
        maxSnapshots: {
          min: maxProp ? maxProp.minimum : null,
          max: maxProp ? maxProp.maximum : null,
        },
      };
      state.snapshot = {
        mode: modeProp && present(modeProp.default) ? modeProp.default : modeChoices[0],
        frequency: freqProp && present(freqProp.default) ? freqProp.default : freqChoices[0],
        max_snapshots: maxProp && present(maxProp.default) ? maxProp.default : null,
      };
    } catch (error) {
      state.vocabularyError = error;
      state.snapshotSchema = null;
      state.snapshot = null;
    }
    rerender();
  }

  // --- sources -------------------------------------------------------------------------------

  async function loadSources() {
    state.sourcesLoading = true;
    rerender();
    try {
      const result = await listSources(clientId);
      // In score mode the panel is about this month's files only; last month's are the recipe's,
      // and showing them here would offer to map again what the replay exists to reuse.
      const shown = state.replay
        ? result.sources.filter((source) => state.replay.uploadedIds.includes(source.source_id))
        : result.sources;
      state.sources = shown.map((source) => ({
        source,
        profile: result.profiles[source.source_id],
      }));
      state.sourcesError = null; // a read that succeeded answers the failure the last one reported
      if (!state.replay) syncFeatures();
      // A client whose tables are already confirmed opens on the first step still to do.
      if (firstRead && !state.replay && state.open === "sources" && allRolesConfirmed()) state.open = "mapping";
    } catch (error) {
      state.sourcesError = error;
    }
    firstRead = false;
    state.sourcesLoading = false;
    rerender();
    await ensureMappingsLoaded();
  }

  /**
   * The feature checklist, kept in step with which roles the client actually has a source for.
   *
   * It grows when a role is confirmed and shrinks when its last source goes away, because a spec
   * naming features for a role no source fills is one `POST /datasets` can only refuse. `featureChecked`
   * is deliberately *not* pruned alongside: a tick the user removed survives the source being
   * re-added, which is the one piece of this the user, rather than the data, decided.
   */
  function syncFeatures() {
    if (!state.schema) return;
    const mappedRoles = new Set(state.sources.filter((e) => e.source.role).map((e) => e.source.role));
    state.features = state.features.filter((feature) => mappedRoles.has(feature.role));
    const known = new Set(state.features.map((f) => f.name));
    for (const feature of state.schema.suggested_features || []) {
      if (mappedRoles.has(feature.role) && !known.has(feature.name)) {
        state.features.push(feature);
        if (!(feature.name in state.featureChecked)) state.featureChecked[feature.name] = true;
      }
    }
  }

  async function uploadFiles(fileList) {
    state.sourcesError = null;
    state.uploadErrors = [];
    for (const file of Array.from(fileList)) {
      state.uploading = [...state.uploading, file.name];
      rerender();
      try {
        const created = await createSource(clientId, file);
        if (state.replay) state.replay.uploadedIds = [...state.replay.uploadedIds, created.source_id];
      } catch (error) {
        // Kept apart from `sourcesError`: the re-read that follows clears that one, and a file the
        // API refused has to stay on screen until the next upload.
        state.uploadErrors = [...state.uploadErrors, { name: file.name, error }];
      }
      state.uploading = state.uploading.filter((name) => name !== file.name);
    }
    if (state.replay) await runReplay();
    else await loadSources();
  }

  // --- score mode: replaying the saved recipe ------------------------------------------------------

  /**
   * Ask the API to point the saved recipe at this month's files, then show what it decided.
   *
   * The replay confirms each file's role from the recipe, so the sources are re-read afterwards for
   * the same reason `setRoleFor` re-reads them. A file the replay reports as missing a column gets
   * its replayed mapping loaded into the mapping step, which opens on it; when nothing is missing the
   * panel goes straight to Build, because the recipe has already answered every other question.
   */
  async function runReplay() {
    const replay = state.replay;
    replay.running = true;
    replay.error = null;
    rerender();
    try {
      replay.response = replay.uploadedIds.length
        ? await replayOnboardingSpec(clientId, replay.spec.spec_id, replay.uploadedIds, replay.settledIds)
        : null;
    } catch (error) {
      replay.response = null;
      replay.error = error;
    }
    replay.running = false;
    await loadSources();
    await loadReopenedMappings();
    const response = replay.response;
    if (response && response.spec_id) state.open = "build";
    else if (response && reopened().length) state.open = "mapping";
    rerender();
  }

  /** This month's files whose replayed mapping is missing a column - the only ones to review. */
  function reopened() {
    const response = state.replay && state.replay.response;
    return response ? response.sources.filter((entry) => entry.missing_columns.length) : [];
  }

  async function loadReopenedMappings() {
    const entries = reopened();
    if (!entries.length) return;
    try {
      const { mappings } = await listMappings(clientId, useCaseId);
      for (const entry of entries) {
        const saved = mappings.find((mapping) => mapping.mapping_id === entry.mapping_id);
        if (!saved) continue;
        // The replayed copy is both what the table edits and what its confidence pills read: its
        // numbers are last month's decisions, carried across unchanged (never re-measured here).
        state.mappingSuggested[entry.source_id] = saved;
        state.mappings[entry.source_id] = saved;
        state.mappingSaved[entry.source_id] = false;
        state.mappingError[entry.source_id] = null;
      }
    } catch (error) {
      for (const entry of entries) state.mappingError[entry.source_id] = error;
    }
  }

  /**
   * Confirm (or change) a source's role, then re-read every source.
   *
   * The re-read is the point, not housekeeping: `PATCH /clients/{id}/sources/{sid}` answers with
   * the one `SourceSpec` it changed, but it also re-derives `KeyCandidate.coverage` on *every* one
   * of this client's stored profiles (`api.routes.sources.resync_join_coverage`) - confirming the
   * entity table is exactly what makes every other table's coverage measurable, and moving that
   * role away is what clears it again. Patching the single row in place would leave the coverage
   * badges showing the em dash for a number the server has just measured, which is the mirror image
   * of house rule 2: not a fabricated value, but a measured one the screen refuses to show.
   */
  async function setRoleFor(sourceId, role) {
    try {
      await setSourceRole(clientId, sourceId, role);
      delete state.mappings[sourceId];
      delete state.mappingSuggested[sourceId];
      delete state.mappingSaved[sourceId];
      delete state.mappingChecks[sourceId];
    } catch (error) {
      state.sourcesError = error;
      rerender();
      return;
    }
    await loadSources();
  }

  /** Confirm the role the `<select>` is already showing. The select's own `change` never fires for
   * a user who agrees with the detector's top candidate, so without this the preselected proposal
   * could never become a confirmed fact - see `steps.js`'s `roleSelect`. */
  function confirmRoleFor(button) {
    const row = button.closest("tr");
    const select = row && row.querySelector('select[data-act="set-role"]');
    if (!select || !select.value) return;
    return setRoleFor(button.dataset.source, select.value).then(() => {
      if (!state.sourcesError && allRolesConfirmed()) openStep("mapping");
    });
  }

  /**
   * "Confirm roles": every proposed role, confirmed the way the row's own Confirm does it (the same
   * `PATCH` per source), then one re-read. Moves on to Mapping once every table has its role.
   */
  async function confirmAllRoles() {
    const proposed = state.sources.filter(
      (entry) => !entry.source.role && ((entry.profile && entry.profile.role_candidates) || []).length,
    );
    if (!proposed.length) return;
    state.rolesBusy = true;
    rerender();
    for (const entry of proposed) {
      const sourceId = entry.source.source_id;
      try {
        await setSourceRole(clientId, sourceId, entry.profile.role_candidates[0].role);
        delete state.mappings[sourceId];
        delete state.mappingSuggested[sourceId];
        delete state.mappingSaved[sourceId];
        delete state.mappingChecks[sourceId];
      } catch (error) {
        state.sourcesError = error;
        break;
      }
    }
    state.rolesBusy = false;
    await loadSources();
    if (!state.sourcesError && allRolesConfirmed()) openStep("mapping");
  }

  function allRolesConfirmed() {
    if (!state.schema || !state.sources.length) return false;
    const roles = state.schema.roles.roles;
    const entityRole = Object.keys(roles).find((id) => roles[id].kind === "entity");
    return state.sources.every((e) => e.source.role) && state.sources.some((e) => e.source.role === entityRole);
  }

  function toggleRowMenu(sourceId) {
    state.rowMenu = state.rowMenu === sourceId ? null : sourceId;
    state.confirmDelete = null;
    if (state.rowMenu) focusNext = `[data-act="ask-delete"][data-source="${cssEscape(sourceId)}"]`;
    rerender();
  }

  function askDelete(sourceId) {
    state.rowMenu = null;
    state.confirmDelete = sourceId;
    focusNext = `[data-act="cancel-delete"]`;
    rerender();
  }

  function closeMenus(returnTo) {
    const sourceId = returnTo || state.rowMenu || state.confirmDelete;
    state.rowMenu = null;
    state.confirmDelete = null;
    if (sourceId) focusNext = `[data-act="row-menu"][data-source="${cssEscape(sourceId)}"]`;
    rerender();
  }

  /** Deleting a source re-derives the same coverage numbers a role change does, so this re-reads
   * the client's sources for the same reason `setRoleFor` does. */
  async function deleteSourceFor(sourceId) {
    state.confirmDelete = null;
    state.rowMenu = null;
    try {
      await deleteSource(clientId, sourceId);
      delete state.mappings[sourceId];
      delete state.mappingSuggested[sourceId];
      delete state.mappingSaved[sourceId];
      delete state.mappingChecks[sourceId];
    } catch (error) {
      state.sourcesError = error;
      rerender();
      return;
    }
    if (state.replay) {
      state.replay.uploadedIds = state.replay.uploadedIds.filter((id) => id !== sourceId);
      await runReplay();
      return;
    }
    await loadSources();
  }

  // --- mapping ---------------------------------------------------------------------------------

  async function ensureMapping(entry) {
    const id = entry.source.source_id;
    // Score mode never suggests: a file's mapping is last month's, replayed (`loadReopenedMappings`).
    if (state.replay) return;
    if (!entry.source.role || state.mappings[id] || state.mappingLoading[id]) return;
    state.mappingLoading[id] = true;
    state.mappingError[id] = null;
    rerender();
    try {
      const mapping = await suggestMapping(clientId, id, useCaseId);
      state.mappingSuggested[id] = mapping;
      state.mappings[id] = mapping;
    } catch (error) {
      state.mappingError[id] = error;
    }
    state.mappingLoading[id] = false;
    rerender();
  }

  function ensureMappingsLoaded() {
    return Promise.all(state.sources.map(ensureMapping));
  }

  /**
   * Point one of the client's columns at one of ours, or at nothing.
   *
   * Two invariants `MappingSpec`'s own validator enforces are kept here rather than discovered as a
   * `422` on save: one source column claims one standard name, and one standard name is claimed by
   * one source column. Re-pointing a standard name therefore *displaces* whichever column held it,
   * back onto the unmapped list, instead of leaving two columns claiming it; and a column that
   * stops being mapped joins that list rather than vanishing from both.
   *
   * `confidence` is the suggester's own measurement whenever this pair is one it proposed. When it
   * is not, nothing measured it: `MappingColumn.confidence` is a required float, so the request
   * body carries the settled-by-a-human `1` alongside `decided_by: "user"` (which is what stops the
   * engine asking for a review of a decision a human just made), and `steps.js` reads the pill off
   * the untouched suggestion so that number is never shown as though it were a measurement.
   */
  function handleSetMapping(el) {
    const sourceId = el.dataset.source;
    const columnName = el.dataset.column;
    const standard = el.value;
    const mapping = state.mappings[sourceId];
    if (!mapping) return;
    const displaced = standard
      ? (mapping.columns || []).filter((c) => c.standard === standard && c.source !== columnName)
      : [];
    const columns = (mapping.columns || []).filter(
      (c) => c.source !== columnName && !displaced.includes(c),
    );
    const freed = displaced.map((c) => c.source);
    let unmapped = (mapping.unmapped_source || []).filter(
      (c) => c !== columnName && !freed.includes(c),
    );
    if (standard) {
      columns.push({
        source: columnName,
        standard,
        confidence: suggestedConfidence(sourceId, columnName, standard),
        decided_by: "user",
        transform: null,
      });
      unmapped = [...unmapped, ...freed];
    } else {
      unmapped = [...unmapped, columnName];
    }
    state.mappings[sourceId] = { ...mapping, columns, unmapped_source: unmapped };
    state.mappingSaved[sourceId] = false;
    const touched = { ...(state.touched[sourceId] || {}), [columnName]: true };
    for (const other of freed) touched[other] = true;
    state.touched[sourceId] = touched;
    rerender();
  }

  /** The suggester's score for exactly this (source column -> standard column) pair, or `1` when it
   * never proposed this pair - see `handleSetMapping` for why that number is a wire value and not a
   * displayed one. */
  function suggestedConfidence(sourceId, columnName, standard) {
    const suggested = state.mappingSuggested[sourceId];
    const match =
      suggested &&
      (suggested.columns || []).find((c) => c.source === columnName && c.standard === standard);
    return match && present(match.confidence) ? match.confidence : 1;
  }

  function handleSetValueMap(el) {
    const sourceId = el.dataset.source;
    const standard = el.dataset.standard;
    const rawValue = el.dataset.value;
    const mapping = state.mappings[sourceId];
    if (!mapping) return;
    const valueMaps = { ...(mapping.value_maps || {}) };
    const forStandard = { ...(valueMaps[standard] || {}) };
    if (el.value === "__keep__") delete forStandard[rawValue];
    else if (el.value === "__null__") forStandard[rawValue] = null;
    else forStandard[rawValue] = el.value;
    valueMaps[standard] = forStandard;
    state.mappings[sourceId] = { ...mapping, value_maps: valueMaps };
    state.mappingSaved[sourceId] = false;
    rerender();
  }

  async function saveMappingFor(sourceId, { advance = true } = {}) {
    const mapping = state.mappings[sourceId];
    if (!mapping) return;
    state.mappingSaving[sourceId] = true;
    rerender();
    try {
      const result = await saveMapping(clientId, mapping.mapping_id, mapping);
      state.mappingSaved[sourceId] = true;
      state.mappingError[sourceId] = null;
      // In score mode these checks would be about every mapping the client ever saved - last
      // month's included - so they are not this file's to show; the replay answers instead.
      state.mappingChecks[sourceId] = state.replay ? [] : result.checks || [];
    } catch (error) {
      state.mappingError[sourceId] = error;
    }
    state.mappingSaving[sourceId] = false;
    if (state.replay && state.mappingSaved[sourceId]) {
      // A saved answer to a reopened mapping is this month's decision for that file: the replay is
      // asked again with it, and uses it as it stands.
      if (!state.replay.settledIds.includes(mapping.mapping_id)) {
        state.replay.settledIds = [...state.replay.settledIds, mapping.mapping_id];
      }
      await runReplay();
      return;
    }
    if (state.mappingSaved[sourceId]) state.mapOpen[sourceId] = false;
    if (advance && allMappingsSaved() && state.open === "mapping") state.open = "features";
    rerender();
  }

  function allMappingsSaved() {
    const mappable = state.sources.filter((entry) => entry.source.role);
    return mappable.length > 0 && mappable.every((entry) => state.mappingSaved[entry.source.source_id]);
  }

  function acceptAllFor(sourceId) {
    if (state.mappingSuggested[sourceId]) state.mappings[sourceId] = state.mappingSuggested[sourceId];
    delete state.touched[sourceId];
    void saveMappingFor(sourceId);
  }

  /**
   * "Accept suggestions and save all": every unsaved table's suggestion, saved one after another -
   * except a table with a match below the confident threshold, which is left open with that match
   * marked "Check this match" for the user. A table the user already edited is saved as edited.
   */
  async function saveAllMappings() {
    const pending = state.sources.filter(
      (entry) =>
        entry.source.role && state.mappings[entry.source.source_id] && !state.mappingSaved[entry.source.source_id],
    );
    const run = { running: true, saved: 0, stopped: [] };
    state.saveAll = run;
    rerender();
    for (const entry of pending) {
      const sourceId = entry.source.source_id;
      const edited = state.touched[sourceId] && Object.keys(state.touched[sourceId]).length;
      if (!edited && state.mappingSuggested[sourceId]) state.mappings[sourceId] = state.mappingSuggested[sourceId];
      if (flaggedColumns(state, sourceId).length) {
        run.stopped.push(sourceId);
        state.mapOpen[sourceId] = true;
        continue;
      }
      await saveMappingFor(sourceId, { advance: false });
      if (state.mappingSaved[sourceId]) run.saved += 1;
      else state.mapOpen[sourceId] = true;
    }
    run.running = false;
    if (allMappingsSaved()) state.open = "features";
    rerender();
  }

  // --- features & label --------------------------------------------------------------------------

  function toggleFeature(el) {
    state.featureChecked[el.dataset.feature] = el.checked;
    rerender();
  }

  async function openAddFeature() {
    if (!state.featureSchema) await loadFeatureSchema();
    state.featureForm = {
      name: "",
      role: "",
      function: "",
      column: "",
      window_days: null,
      description: "",
      whereColumn: "",
      whereOp: "",
      whereValue: "",
    };
    focusNext = "#ob-f-description";
    rerender();
  }

  function handleFeatureField(el) {
    if (!state.featureForm) return;
    const field = el.dataset.field;
    const value = el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value;
    state.featureForm = { ...state.featureForm, [field]: value };
    rerender();
  }

  function saveFeatureFromDraft() {
    const draft = state.featureForm;
    if (!draft || !draft.name || !draft.role || !draft.function) return;
    const feature = {
      name: draft.name,
      role: draft.role,
      function: draft.function,
      column: draft.column || null,
      window_days: present(draft.window_days) ? draft.window_days : null,
      where:
        draft.whereOp && draft.whereColumn
          ? { column: draft.whereColumn, op: draft.whereOp, value: draft.whereValue || null }
          : null,
      description: draft.description || "",
    };
    state.features = [...state.features, feature];
    state.featureChecked[feature.name] = true;
    state.featureForm = null;
    rerender();
  }

  function setHorizon(el) {
    if (!state.label) return;
    state.label = { ...state.label, horizon_days: el.value === "" ? null : Number(el.value) };
    rerender();
  }

  function setSnapshotField(el) {
    if (!state.snapshot) return;
    const field = el.dataset.field;
    const value = field === "max_snapshots" ? Number(el.value) : el.value;
    state.snapshot = { ...state.snapshot, [field]: value };
    rerender();
  }

  // --- the spec, the preview and the build ------------------------------------------------------

  function entitySourceEntry() {
    if (!state.schema) return null;
    const roles = state.schema.roles.roles;
    const entityRole = Object.keys(roles).find((id) => roles[id].kind === "entity");
    return state.sources.find((e) => e.source.role === entityRole) || null;
  }

  function specBody() {
    const entity = entitySourceEntry();
    const eventSources = state.sources.filter(
      (e) => e.source.role && (!entity || e.source.source_id !== entity.source.source_id),
    );
    // Only a SAVED mapping has a row `api/routes/onboarding-specs` can load by id; a suggestion the
    // user has not saved yet exists only in this tab's memory, and naming it here would 404 the
    // instant the server tried to load it back.
    const mappingIds = state.sources
      .filter((e) => e.source.role && state.mappingSaved[e.source.source_id])
      .map((e) => state.mappings[e.source.source_id])
      .filter(Boolean)
      .map((m) => m.mapping_id)
      .sort(); // `OnboardingSpec.mapping_ids` is documented sorted, and its hash is order-sensitive.
    const tickedFeatures = state.features.filter((f) => state.featureChecked[f.name] !== false);
    return {
      use_case: useCaseId,
      entity_source_id: entity ? entity.source.source_id : null,
      event_source_ids: eventSources.map((e) => e.source.source_id),
      mapping_ids: mappingIds,
      feature_spec: { features: tickedFeatures },
      label_spec: state.label,
      snapshot_spec: withoutBlanks(state.snapshot),
    };
  }

  /** A settings object with the fields the user left blank removed rather than sent as `null`.
   * `SnapshotDefinition.max_snapshots` is a plain `int` with its own default: leaving it out means
   * "whatever this engine is configured to use", while sending `null` means "no value", which the
   * API is right to refuse. */
  function withoutBlanks(settings) {
    if (!settings) return settings;
    return Object.fromEntries(Object.entries(settings).filter(([, value]) => present(value)));
  }

  async function runPreview() {
    state.previewLoading = true;
    state.previewError = null;
    state.preview = null;
    rerender();
    try {
      const spec = await createOnboardingSpec(clientId, specBody());
      state.specId = spec.spec_id;
      state.specChecks = spec.checks || [];
      if (!state.specChecks.some((c) => c.severity === "error")) {
        state.preview = await previewOnboardingSpec(clientId, spec.spec_id);
      }
    } catch (error) {
      state.previewError = error;
    }
    state.previewLoading = false;
    rerender();
  }

  /**
   * Save the recipe and start the build.
   *
   * `409` is not an error on this screen: `POST /datasets` answers it with the whole check list when
   * a blocking problem remains, and that list is the screen's content, rendered exactly like the
   * checks any other step shows. Anything else is an error box.
   *
   * The first `GET /datasets/{id}` is inside its own `try`: by then the build has really started
   * (`POST /datasets` writes `build_status.json` before it returns), so a failed first poll is a
   * reading problem, not a starting one, and reporting it as "the build could not be started" would
   * be telling the user something untrue about a job that is running.
   */
  async function startBuild() {
    state.building = true;
    state.buildError = null;
    state.buildChecks = [];
    rerender();
    try {
      // Score mode builds the recipe the replay already saved; train mode saves this screen's.
      const specId = state.replay ? state.replay.response.spec_id : await saveRecipe();
      const created = await createDataset({
        client_id: clientId,
        spec_id: specId,
        mode: state.replay ? "score" : "train",
      });
      state.datasetId = created.dataset_id;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.body && error.body.checks) {
        state.buildChecks = error.body.checks;
      } else {
        state.buildError = error;
      }
      state.building = false;
      rerender();
      return;
    }
    state.building = false;
    await pollOnce();
    scheduleNextPoll();
  }

  /** Save this screen's recipe (`POST .../onboarding-specs`) and return its id. */
  async function saveRecipe() {
    const spec = await createOnboardingSpec(clientId, specBody());
    state.specId = spec.spec_id;
    state.specChecks = spec.checks || [];
    return spec.spec_id;
  }

  /** One poll, and whatever it settles. Returns `true` while the build is still going. */
  async function pollOnce() {
    let status;
    try {
      state.dataset = await getDataset(state.datasetId);
      status = state.dataset.status;
    } catch (error) {
      state.buildError = error;
      rerender();
      return false;
    }
    if (status.state === "pending" || status.state === "running") {
      rerender();
      return true;
    }
    if (status.state === "done") {
      try {
        state.buildReport = await getDatasetReport(state.datasetId);
      } catch (error) {
        state.buildError = error;
      }
    }
    rerender();
    return false;
  }

  /** `setTimeout` rather than `setInterval`: the next poll is scheduled once the previous one has
   * answered, so a slow API is never given a queue of overlapping requests to answer, and a build
   * that ends between ticks is never polled again after its report has been read. */
  function scheduleNextPoll() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (await pollOnce()) scheduleNextPoll();
    }, POLL_MS);
  }

  /** A boolean standard column reads as a binary target and a numeric one as a regression target -
   * the same rule `ui/usecase.js`'s own `detectProblemType` applies to an uploaded file's target
   * column, read here off the manifest's declared standard type instead of a freshly profiled one.
   * Anything else (categorical, date, text, or no target at all) is left `null`: Step 2 already asks
   * the user to choose when detection cannot, and guessing further would be exactly the fabricated
   * value house rule 2 forbids. */
  function inferProblemType(manifest) {
    if (!manifest.target) return null;
    const column = manifest.columns.find((c) => c.name === manifest.target);
    if (!column) return null;
    if (column.type === "boolean") return "binary_classification";
    if (column.type === "numeric") return "regression";
    return null;
  }

  /**
   * Hand the built dataset to the Setup form, and fold the four steps away (prototype `07`).
   *
   * The sample is read first so the host can preview the rows it is about to run on; a sample that
   * cannot be read is an empty preview, never a reason to withhold a dataset that built and passed.
   */
  async function useDataset() {
    const report = state.buildReport;
    const manifest = state.dataset && state.dataset.manifest;
    if (!report || !report.passed || !manifest || typeof onDatasetReady !== "function") return;
    let sample = [];
    try {
      sample = (await getDatasetSample(state.datasetId)).rows || [];
    } catch {
      sample = [];
    }
    state.inUse = true;
    state.collapsed = true;
    state.open = null;
    rerender();
    onDatasetReady({
      datasetId: state.datasetId,
      primaryKey: manifest.primary_key,
      target: manifest.target,
      problemType: inferProblemType(manifest),
      timeColumn: manifest.snapshot_mode === "periodic" ? state.schema.standard_schema.snapshot_column : null,
      manifest,
      sample,
    });
  }

  // --- event wiring, attached once (see the module docstring) --------------------------------------

  function onClick(event) {
    const el = event.target.closest("[data-act]");
    if (state.rowMenu && !event.target.closest(".ob-rowmenu")) {
      state.rowMenu = null;
      if (!el) rerender();
    }
    if (!el) return;
    const act = el.dataset.act;
    if (act === "delete-source") deleteSourceFor(el.dataset.source);
    else if (act === "row-menu") toggleRowMenu(el.dataset.source);
    else if (act === "ask-delete") askDelete(el.dataset.source);
    else if (act === "cancel-delete") closeMenus(state.confirmDelete);
    else if (act === "confirm-role") confirmRoleFor(el);
    else if (act === "confirm-roles") confirmAllRoles();
    else if (act === "open-step") openStep(el.dataset.step);
    else if (act === "toggle-map") {
      state.mapOpen[el.dataset.source] = el.getAttribute("aria-expanded") !== "true";
      rerender();
    } else if (act === "save-all-mappings") saveAllMappings();
    else if (act === "unfold") {
      state.collapsed = false;
      state.open = "build";
      rerender();
    } else if (act === "accept-all") acceptAllFor(el.dataset.source);
    else if (act === "save-mapping") saveMappingFor(el.dataset.source);
    else if (act === "open-add-feature") openAddFeature();
    else if (act === "cancel-add-feature") {
      state.featureForm = null;
      focusNext = `[data-act="open-add-feature"]`;
      rerender();
    } else if (act === "save-feature") saveFeatureFromDraft();
    else if (act === "preview") runPreview();
    else if (act === "build") startBuild();
    else if (act === "use-dataset") useDataset();
  }

  function onChange(event) {
    const el = event.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "pick-files") {
      if (el.files && el.files.length) uploadFiles(el.files);
    } else if (act === "set-role") setRoleFor(el.dataset.source, el.value);
    else if (act === "set-mapping") handleSetMapping(el);
    else if (act === "set-value-map") handleSetValueMap(el);
    else if (act === "toggle-feature") toggleFeature(el);
    else if (act === "feature-field") handleFeatureField(el);
    else if (act === "set-horizon") setHorizon(el);
    else if (act === "set-snapshot") setSnapshotField(el);
  }

  /** Escape closes the row menu, the remove confirmation and the add-a-measure dialog, and hands
   * focus back to what opened it. */
  function onKeydown(event) {
    if (event.key !== "Escape") return;
    if (state.rowMenu || state.confirmDelete) {
      event.stopPropagation();
      closeMenus();
    } else if (state.featureForm) {
      event.stopPropagation();
      state.featureForm = null;
      focusNext = `[data-act="open-add-feature"]`;
      rerender();
    }
  }

  container.addEventListener("click", onClick);
  container.addEventListener("change", onChange);
  container.addEventListener("keydown", onKeydown);

  rerender();
  loadSchema();
  loadSources();
  loadSnapshotSchema();

  /** `stop()` is the host's way out: `ui/usecase.js` replaces its whole `<main>` on a route change,
   * and a poll still running against a container nobody can see is a request nobody reads. */
  return {
    getState: () => state,
    rerender,
    stop: () => {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = null;
    },
  };
}
