// The "Build from raw tables" panel (Phase 2 plan §9/§10, M13): the entry point `onboardingPanel`
// mounts the four collapsible steps (`steps.js`) inline wherever the host places `container`, and
// owns everything the pure render functions do not - the fetches, the working edits, the polling and
// the event wiring.
//
// This module never claims a hash route. Step 1 of the Phase 1 Setup form is meant to become a
// choice between "Upload a prepared file" (unchanged) and "Build from raw tables" (this panel,
// rendered inline, not a separate page - see plan §10), and that choice lives in `ui/usecase.js`,
// which this branch may not edit (`PARALLEL_WORK_PROTOCOL.md` §3: not in this milestone's file list).
// So `onboardingPanel(container, options)` is a plain mount function a host calls once it has decided
// to show this path, exactly the shape the task names; the exact call the orchestrator needs to add
// to `usecase.js` is written out in this branch's final report, not in code here.
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
  getStandardSchema,
  listSources,
  previewOnboardingSpec,
  saveMapping,
  schemaEnum,
  schemaProperty,
  setSourceRole,
  suggestMapping,
} from "./api.js";
import { buildStep, featuresStep, mappingStep, sourcesStep } from "./steps.js";
import { present } from "../../dom.js";

const POLL_MS = 2000;

function initialState(clientId, useCaseId) {
  return {
    clientId,
    useCaseId,
    open: "sources",

    schema: null,
    schemaError: null,

    sources: [],
    sourcesLoading: true,
    sourcesError: null,
    uploading: [],

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
 * choose a problem type by hand, exactly as it does today for an uploaded file.
 */
export function onboardingPanel(container, { clientId, useCaseId, onDatasetReady }) {
  const state = initialState(clientId, useCaseId);
  let pollTimer = null;

  function rerender() {
    container.innerHTML = panelHtml(state);
    container.querySelectorAll(".stage-d").forEach((details) => {
      details.open = state.open === details.dataset.step;
      details.addEventListener("toggle", () => {
        if (details.open) state.open = details.dataset.step;
      });
    });
  }

  function panelHtml(s) {
    return `<div class="stages">${sourcesStep(s)}${mappingStep(s)}${featuresStep(s)}${buildStep(s)}</div>`;
  }

  // --- schema and the two generated-form vocabularies ------------------------------------------

  async function loadSchema() {
    try {
      state.schema = await getStandardSchema(useCaseId);
      state.label = state.schema.label ? { ...state.schema.label } : null;
      seedFeatures();
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
  async function loadFeatureSchema() {
    try {
      const [functionChoices, whereChoices, nameProp] = await Promise.all([
        schemaEnum("AggFunction"),
        schemaEnum("WhereOp"),
        schemaProperty("FeatureDef", "name"),
      ]);
      state.featureSchema = {
        function: functionChoices,
        where: whereChoices,
        namePattern: (nameProp && nameProp.pattern) || "",
      };
    } catch (error) {
      state.vocabularyError = error;
    }
  }

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
        mode: modeChoices,
        frequency: freqChoices,
        maxSnapshots: { min: maxProp && maxProp.minimum, max: maxProp && maxProp.maximum },
      };
      state.snapshot = {
        mode: (modeProp && modeProp.default) || modeChoices[0],
        frequency: (freqProp && freqProp.default) || freqChoices[0],
        max_snapshots: (maxProp && maxProp.default) || (maxProp && maxProp.minimum) || 1,
      };
    } catch (error) {
      state.vocabularyError = error;
    }
    rerender();
  }

  // --- sources -------------------------------------------------------------------------------

  async function loadSources() {
    state.sourcesLoading = true;
    rerender();
    try {
      const result = await listSources(clientId);
      state.sources = result.sources.map((source) => ({ source, profile: result.profiles[source.source_id] }));
      seedFeatures();
    } catch (error) {
      state.sourcesError = error;
    }
    state.sourcesLoading = false;
    rerender();
    await ensureMappingsLoaded();
  }

  /** Every suggested feature whose role the client has now mapped, added once and never removed:
   * a role confirmed later only grows the checklist, it never throws away a tick the user made. */
  function seedFeatures() {
    if (!state.schema) return;
    const mappedRoles = new Set(state.sources.filter((e) => e.source.role).map((e) => e.source.role));
    const known = new Set(state.features.map((f) => f.name));
    for (const feature of state.schema.suggested_features || []) {
      if (mappedRoles.has(feature.role) && !known.has(feature.name)) {
        state.features.push(feature);
        state.featureChecked[feature.name] = true;
      }
    }
  }

  async function uploadFiles(fileList) {
    for (const file of Array.from(fileList)) {
      state.uploading = [...state.uploading, file.name];
      rerender();
      try {
        await createSource(clientId, file);
      } catch (error) {
        state.sourcesError = error;
      }
      state.uploading = state.uploading.filter((name) => name !== file.name);
    }
    await loadSources();
  }

  async function setRoleFor(sourceId, role) {
    try {
      const updated = await setSourceRole(clientId, sourceId, role);
      state.sources = state.sources.map((e) => (e.source.source_id === sourceId ? { ...e, source: updated } : e));
      delete state.mappings[sourceId];
      delete state.mappingSuggested[sourceId];
      delete state.mappingSaved[sourceId];
      seedFeatures();
    } catch (error) {
      state.sourcesError = error;
    }
    rerender();
    await ensureMappingsLoaded();
  }

  async function deleteSourceFor(sourceId) {
    try {
      await deleteSource(clientId, sourceId);
      state.sources = state.sources.filter((e) => e.source.source_id !== sourceId);
      delete state.mappings[sourceId];
      delete state.mappingSuggested[sourceId];
    } catch (error) {
      state.sourcesError = error;
    }
    rerender();
  }

  // --- mapping ---------------------------------------------------------------------------------

  async function ensureMapping(entry) {
    const id = entry.source.source_id;
    if (!entry.source.role || state.mappings[id] || state.mappingLoading[id]) return;
    state.mappingLoading[id] = true;
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

  function handleSetMapping(el) {
    const sourceId = el.dataset.source;
    const columnName = el.dataset.column;
    const standard = el.value;
    const mapping = state.mappings[sourceId];
    if (!mapping) return;
    const columns = (mapping.columns || []).filter((c) => c.source !== columnName);
    let unmapped = (mapping.unmapped_source || []).filter((c) => c !== columnName);
    if (standard) {
      columns.push({ source: columnName, standard, confidence: 1, decided_by: "user", transform: null });
    } else {
      unmapped = [...unmapped, columnName];
    }
    state.mappings[sourceId] = { ...mapping, columns, unmapped_source: unmapped };
    state.mappingSaved[sourceId] = false;
    rerender();
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

  async function saveMappingFor(sourceId) {
    const mapping = state.mappings[sourceId];
    if (!mapping) return;
    state.mappingSaving[sourceId] = true;
    rerender();
    try {
      const result = await saveMapping(clientId, mapping.mapping_id, mapping);
      state.mappingSaved[sourceId] = true;
      state.mappingChecks[sourceId] = result.checks || [];
    } catch (error) {
      state.mappingError[sourceId] = error;
    }
    state.mappingSaving[sourceId] = false;
    rerender();
  }

  function acceptAllFor(sourceId) {
    if (state.mappingSuggested[sourceId]) state.mappings[sourceId] = state.mappingSuggested[sourceId];
    void saveMappingFor(sourceId);
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
    const eventSources = state.sources.filter((e) => e.source.role && (!entity || e.source.source_id !== entity.source.source_id));
    // Only a SAVED mapping has a row `api/routes/onboarding-specs` can load by id; a suggestion the
    // user has not saved yet exists only in this tab's memory, and naming it here would 404 the
    // instant the server tried to load it back.
    const mappingIds = state.sources
      .filter((e) => e.source.role && state.mappingSaved[e.source.source_id])
      .map((e) => state.mappings[e.source.source_id])
      .filter(Boolean)
      .map((m) => m.mapping_id);
    const tickedFeatures = state.features.filter((f) => state.featureChecked[f.name] !== false);
    return {
      use_case: useCaseId,
      entity_source_id: entity ? entity.source.source_id : null,
      event_source_ids: eventSources.map((e) => e.source.source_id),
      mapping_ids: mappingIds,
      feature_spec: { features: tickedFeatures },
      label_spec: state.label,
      snapshot_spec: state.snapshot,
    };
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

  async function startBuild() {
    state.building = true;
    state.buildError = null;
    state.buildChecks = [];
    rerender();
    try {
      const spec = await createOnboardingSpec(clientId, specBody());
      state.specId = spec.spec_id;
      state.specChecks = spec.checks || [];
      const created = await createDataset({ client_id: clientId, spec_id: spec.spec_id, mode: "train" });
      state.datasetId = created.dataset_id;
      state.dataset = await getDataset(state.datasetId);
      pollBuild();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.body && error.body.checks) {
        state.buildChecks = error.body.checks;
      } else {
        state.buildError = error;
      }
    }
    state.building = false;
    rerender();
  }

  function pollBuild() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      let status;
      try {
        state.dataset = await getDataset(state.datasetId);
        status = state.dataset.status;
      } catch (error) {
        clearInterval(pollTimer);
        pollTimer = null;
        state.buildError = error;
        rerender();
        return;
      }
      if (status.state === "pending" || status.state === "running") {
        rerender();
        return;
      }
      clearInterval(pollTimer);
      pollTimer = null;
      if (status.state === "done") {
        try {
          state.buildReport = await getDatasetReport(state.datasetId);
        } catch (error) {
          state.buildError = error;
        }
      }
      rerender();
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

  function useDataset() {
    const report = state.buildReport;
    const manifest = state.dataset && state.dataset.manifest;
    if (!report || !report.passed || !manifest || typeof onDatasetReady !== "function") return;
    onDatasetReady({
      datasetId: state.datasetId,
      primaryKey: manifest.primary_key,
      target: manifest.target,
      problemType: inferProblemType(manifest),
      timeColumn: manifest.snapshot_mode === "periodic" ? state.schema.standard_schema.snapshot_column : null,
      manifest,
    });
  }

  // --- event wiring, attached once (see the module docstring) --------------------------------------

  function onClick(event) {
    const el = event.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "delete-source") deleteSourceFor(el.dataset.source);
    else if (act === "accept-all") acceptAllFor(el.dataset.source);
    else if (act === "save-mapping") saveMappingFor(el.dataset.source);
    else if (act === "open-add-feature") openAddFeature();
    else if (act === "cancel-add-feature") {
      state.featureForm = null;
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

  container.addEventListener("click", onClick);
  container.addEventListener("change", onChange);

  rerender();
  loadSchema();
  loadSources();
  loadSnapshotSchema();

  return { getState: () => state, rerender, stop: () => pollTimer && clearInterval(pollTimer) };
}
