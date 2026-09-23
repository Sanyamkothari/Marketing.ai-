// Every fetch the onboarding panel makes (Phase 2 plan §9/§10, M13).
//
// Self-contained on purpose: `ui/api.js` is a Phase 1 file this branch may not edit
// (`PARALLEL_WORK_PROTOCOL.md` §3), so rather than import its private `request()` helper this module
// mirrors its tiny-wrapper shape exactly - same `ApiError`, same envelope parsing, same "network
// failure is a coded error too" rule - so a screen that already knows how to unwrap `ui/api.js`'s
// errors unwraps these identically. The two never need to agree on more than that shape.
//
// Every endpoint below is declared by a router in this repository today - `api/routes/clients.py`,
// `sources.py`, `mappings.py`, `datasets.py` - and `tests/unit/test_onboarding_ui.py` checks each
// path here against those routers' own OpenAPI surface rather than against a transcript of them.
// `api.main.create_app` mounts all four (its PHASE-2 block); an installation without them answers
// 404 like any other unknown route, and the panel renders that the same way it renders every other
// `ApiError`, so there is nothing here to special-case.

const API_BASE = (window.MARKETING_AI_API || window.location.origin).replace(/\/+$/, "");

/** An API response that was not a success, carrying the server's own `{code, message}` envelope. */
export class ApiError extends Error {
  constructor(status, code, message, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

function url(path) {
  return path.startsWith("http") ? path : API_BASE + path;
}

async function parse(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(url(path), options);
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const body = await parse(response);
  if (response.ok) return body;
  const detail = body && body.detail;
  if (Array.isArray(detail)) throw fieldError(response.status, detail, body);
  const code = (detail && detail.code) || `HTTP_${response.status}`;
  const message = (detail && detail.message) || `The API answered ${response.status}.`;
  throw new ApiError(response.status, code, message, body);
}

/**
 * FastAPI's own request-validation answer, turned into a message a person can act on.
 *
 * Every route in this API hand-writes the house envelope (`{detail: {code, message}}`), but the one
 * failure no route gets to write is a body that never reached it: a request FastAPI rejects answers
 * `422` with `detail` as a *list* of field errors, and the generic unwrapping above would render
 * that as "The API answered 422." - a status code read aloud, which is neither business language
 * nor something to do about it (house rule 3).
 *
 * Only `loc` is read - the field path, which is schema vocabulary - and never `input`, which is the
 * rejected value itself and may be a client's own data (house rule 4 in the browser: a data value
 * has no business in a message either).
 */
function fieldError(status, problems, body) {
  const fields = [
    ...new Set(
      problems
        .map((problem) => (problem.loc || []).filter((part) => typeof part === "string").pop())
        .filter(Boolean),
    ),
  ];
  const named = fields.length ? `: ${fields.join(", ")}` : "";
  return new ApiError(
    status,
    "REQUEST_REJECTED",
    `The engine could not accept this request - ${problems.length} setting(s) were missing or of ` +
      `the wrong kind${named}. Check those settings on this screen and try again.`,
    body,
  );
}

const json = (path, method, payload) =>
  request(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

// --- what the mapping and feature forms are generated from -------------------------------------

export const getStandardSchema = (useCaseId) =>
  request(`/use-cases/${encodeURIComponent(useCaseId)}/standard-schema`);

let openApiPromise = null;

/**
 * The app's own OpenAPI document, fetched once and cached for the session.
 *
 * The "Add feature" form and the snapshot settings are built from this, not from a hand-typed list
 * of function names or operators: `AggFunction`/`WhereOp`/`SnapshotMode`/`SnapshotFrequency` are
 * real pydantic enums, and their choices - and a numeric field's `minimum`/`maximum`/`default` -
 * show up in `components.schemas` exactly because `FeatureSpec`/`SnapshotSpec` are real request or
 * response models on the mapping/dataset routes. Nothing about the vocabulary is typed twice.
 */
export function getOpenApiSchema() {
  if (!openApiPromise) openApiPromise = request("/openapi.json");
  return openApiPromise;
}

function componentOf(schema, name) {
  return (schema.components && schema.components.schemas && schema.components.schemas[name]) || null;
}

/** The `enum` list of a named component (`AggFunction`, `WhereOp`, ...), or `[]` while it is unknown. */
export async function schemaEnum(name) {
  const component = componentOf(await getOpenApiSchema(), name);
  return (component && component.enum) || [];
}

/** One property's own JSON-schema node, unwrapping the `anyOf: [T, null]` shape an optional field gets. */
export async function schemaProperty(model, property) {
  const component = componentOf(await getOpenApiSchema(), model);
  const prop = component && component.properties && component.properties[property];
  if (!prop) return null;
  if (Array.isArray(prop.anyOf)) {
    const typed = prop.anyOf.find((option) => option.type && option.type !== "null");
    if (typed) return { ...typed, default: prop.default, title: prop.title };
  }
  return prop;
}

// --- clients (the header's picker, Plan A M35) ---------------------------------------------------

/** The client every installation starts with ("Demo"), created by the API the first time. */
export const ensureDefaultClient = () => request("/clients/default", { method: "POST" });

export const listClients = () => request("/clients");

export const createClient = (name, industry) => json("/clients", "POST", { name, industry });

// --- sources -------------------------------------------------------------------------------------

export const listSources = (clientId) => request(`/clients/${encodeURIComponent(clientId)}/sources`);

/** `role` is left out until the user confirms one; an unconfirmed role is `None`, never guessed. */
export function createSource(clientId, file, role) {
  const form = new FormData();
  form.append("file", file);
  if (role) form.append("role", role);
  return request(`/clients/${encodeURIComponent(clientId)}/sources`, { method: "POST", body: form });
}

export const setSourceRole = (clientId, sourceId, role) =>
  json(`/clients/${encodeURIComponent(clientId)}/sources/${encodeURIComponent(sourceId)}`, "PATCH", { role });

export const deleteSource = (clientId, sourceId) =>
  request(`/clients/${encodeURIComponent(clientId)}/sources/${encodeURIComponent(sourceId)}`, {
    method: "DELETE",
  });

// --- mapping ---------------------------------------------------------------------------------------

export const suggestMapping = (clientId, sourceId, useCaseId) =>
  json(`/clients/${encodeURIComponent(clientId)}/mappings/suggest`, "POST", {
    source_id: sourceId,
    use_case: useCaseId,
  });

/**
 * `mapping` is the (possibly user-edited) `MappingSpec` the suggestion started from; the request
 * body is narrowed to exactly `MappingSaveRequest`'s fields before it is sent. `mapping_id` is the
 * URL's to own (the route reads it from there, never the body - two people editing the same
 * suggestion must save to one id, not fork it into two), and `created_at`/`hash` are the store's to
 * stamp on write - sending any of the three would be an unknown field against a route that forbids
 * them, not a value the API would have used anyway.
 */
export const saveMapping = (clientId, mappingId, mapping) =>
  json(`/clients/${encodeURIComponent(clientId)}/mappings/${encodeURIComponent(mappingId)}`, "PUT", {
    client_id: mapping.client_id,
    source_id: mapping.source_id,
    use_case: mapping.use_case,
    role: mapping.role,
    columns: mapping.columns,
    unmapped_source: mapping.unmapped_source,
    missing_required: mapping.missing_required,
    value_maps: mapping.value_maps,
  });

/** One client's saved mappings for one use case - how a replayed mapping is read back for review. */
export const listMappings = (clientId, useCaseId) =>
  request(
    `/clients/${encodeURIComponent(clientId)}/mappings?use_case=${encodeURIComponent(useCaseId)}`,
  );

// --- the onboarding spec and its preview ------------------------------------------------------------

export const createOnboardingSpec = (clientId, body) =>
  json(`/clients/${encodeURIComponent(clientId)}/onboarding-specs`, "POST", body);

export const listOnboardingSpecs = (clientId, useCaseId) =>
  request(
    `/clients/${encodeURIComponent(clientId)}/onboarding-specs?use_case=${encodeURIComponent(useCaseId)}`,
  );

/**
 * Point a saved recipe at this month's files (score mode). `sourceIds` are the files just uploaded;
 * `mappingIds` are mappings the user saved for some of them after the replay reopened their mapping
 * step - the replay then uses those as they stand. `spec_id` in the answer is null until nothing is
 * missing.
 */
export const replayOnboardingSpec = (clientId, specId, sourceIds, mappingIds) =>
  json(
    `/clients/${encodeURIComponent(clientId)}/onboarding-specs/${encodeURIComponent(specId)}/replay`,
    "POST",
    { source_ids: sourceIds, mapping_ids: mappingIds },
  );

/** Synchronous by contract (<=10s, a 200-entity sample) - no polling, unlike a dataset build. */
export const previewOnboardingSpec = (clientId, specId) =>
  request(
    `/clients/${encodeURIComponent(clientId)}/onboarding-specs/${encodeURIComponent(specId)}/preview`,
    { method: "POST" },
  );

// --- the build itself --------------------------------------------------------------------------

/** `202` with `{dataset_id}` on success; `409` with `{detail, checks}` when the spec is not buildable. */
export const createDataset = (body) => json("/datasets", "POST", body);

export const getDataset = (datasetId) => request(`/datasets/${encodeURIComponent(datasetId)}`);

export const getDatasetReport = (datasetId) => request(`/datasets/${encodeURIComponent(datasetId)}/report`);

/** The stringified, PII-redacted rows `sample.json` holds - Step 1's preview of a built dataset. */
export const getDatasetSample = (datasetId) => request(`/datasets/${encodeURIComponent(datasetId)}/sample`);
