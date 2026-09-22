// Every fetch the onboarding panel makes (Phase 2 plan §9/§10, M13).
//
// Self-contained on purpose: `ui/api.js` is a Phase 1 file this branch may not edit
// (`PARALLEL_WORK_PROTOCOL.md` §3), so rather than import its private `request()` helper this module
// mirrors its tiny-wrapper shape exactly - same `ApiError`, same envelope parsing, same "network
// failure is a coded error too" rule - so a screen that already knows how to unwrap `ui/api.js`'s
// errors unwraps these identically. The two never need to agree on more than that shape.
//
// The client/source endpoints below are live in this API today (`api/routes/clients.py`,
// `api/routes/sources.py`). The mapping/spec/dataset endpoints are the sibling M12 routers built in
// the same parallel batch as this panel (Phase 2 plan §9): written against the paths and shapes that
// spec documents, not against a running server. Until those routers are mounted a call to one of them
// answers 404 like any other unknown route, and the panel renders that the same way it renders every
// other `ApiError` - there is nothing here to special-case.

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
  const code = (detail && detail.code) || `HTTP_${response.status}`;
  const message = (detail && detail.message) || `The API answered ${response.status}.`;
  throw new ApiError(response.status, code, message, body);
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

/** `mapping` is the (possibly user-edited) `MappingSpec` the suggestion started from. */
export const saveMapping = (clientId, mappingId, mapping) =>
  json(`/clients/${encodeURIComponent(clientId)}/mappings/${encodeURIComponent(mappingId)}`, "PUT", mapping);

// --- the onboarding spec and its preview ------------------------------------------------------------

export const createOnboardingSpec = (clientId, body) =>
  json(`/clients/${encodeURIComponent(clientId)}/onboarding-specs`, "POST", body);

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
