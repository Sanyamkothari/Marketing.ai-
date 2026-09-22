// The calls the generative screens make, in the shape `ui/api.js` already established: a thin
// `request` wrapper, one exported function per endpoint, and nothing here ever invents a field the
// server did not send - an artefact this job has not produced yet stays `null` and the screen shows
// an em dash for it, exactly as `getArtefact` already does for a predictive run.
//
// `ui/api.js` is not imported for its private `request`/`url` helpers - it does not export them -
// but its `API_BASE` and `ApiError` are the two pieces of shared state that matter (one origin, one
// error shape across the whole app), so both are reused rather than redeclared. Every endpoint below
// is new surface docs/generative-ui-endpoints.md documents in full; nothing here is served yet.

import { API_BASE, ApiError } from "../../api.js";

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

const json = (payload) => ({ headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

// --- knowledge base / RAG assistant (GenerativeJobKind.INDEX_BUILD, .REFERENCE_EVAL) -----------

/** Profiles an uploaded reference-answer file, the way `POST /uploads` profiles a dataset. */
export function postReferenceSet(useCaseId, file) {
  const form = new FormData();
  form.append("file", file);
  return request(`/use-cases/${encodeURIComponent(useCaseId)}/reference-sets`, { method: "POST", body: form });
}

/**
 * Starts an index build: the Build-assistant tab's `documents` plus an optional reference set,
 * model choice and advanced-setting overrides. `documents` is a `File[]`; every other field is a
 * plain value, sent as multipart because the documents are binary (DEC-207's PDFs among them).
 */
export function postIndexBuild(useCaseId, fields) {
  const form = new FormData();
  for (const file of fields.documents || []) form.append("documents", file);
  if (fields.useSampleDocuments) form.append("use_sample_documents", "true");
  if (fields.referenceSetId) form.append("reference_set_id", fields.referenceSetId);
  if (fields.useSampleQuestions) form.append("use_sample_questions", "true");
  if (fields.primaryKey) form.append("primary_key", fields.primaryKey);
  if (fields.referenceColumn) form.append("reference_column", fields.referenceColumn);
  form.append("model_choice", fields.modelChoice);
  form.append("overrides", JSON.stringify(fields.overrides || {}));
  return request(`/use-cases/${encodeURIComponent(useCaseId)}/indexes`, { method: "POST", body: form });
}

/** Every index this use case has built, newest first - the Setup screen's "Previous runs" card. */
export const getIndexes = (useCaseId) => request(`/use-cases/${encodeURIComponent(useCaseId)}/indexes`);

/** One index: its status while building, and every artefact it has produced once it is done. */
export const getIndex = (indexId) => request(`/indexes/${encodeURIComponent(indexId)}`);

/** Re-grades an existing index against a (possibly new) reference set: the Evaluate-assistant tab. */
export function postEvaluate(indexId, fields) {
  const form = new FormData();
  if (fields.referenceSetId) form.append("reference_set_id", fields.referenceSetId);
  if (fields.useSampleQuestions) form.append("use_sample_questions", "true");
  if (fields.primaryKey) form.append("primary_key", fields.primaryKey);
  if (fields.referenceColumn) form.append("reference_column", fields.referenceColumn);
  return request(`/indexes/${encodeURIComponent(indexId)}/evaluate`, { method: "POST", body: form });
}

/**
 * One question, answered live. The response body *is* an `AssistantAnswer` - `contracts.py` names
 * this exact route in the artefact's own docstring, so the shape is not this file's to invent.
 */
export const postAsk = (indexId, question) =>
  request(`/indexes/${encodeURIComponent(indexId)}/ask`, { method: "POST", ...json({ question }) });

// --- root-cause summaries (GenerativeJobKind.ROOT_CAUSE) ----------------------------------------

/** Starts a root-cause job over a finished run; polled through the run's own artefact route. */
export const postRootCause = (runId, overrides = {}) =>
  request(`/runs/${encodeURIComponent(runId)}/root-cause`, { method: "POST", ...json({ overrides }) });

// --- campaign copy (GenerativeJobKind.CAMPAIGN_COPY) --------------------------------------------

/** Starts a campaign-copy job over a finished scoring run. */
export const postCampaignCopy = (runId, overrides = {}) =>
  request(`/runs/${encodeURIComponent(runId)}/campaign-copy`, { method: "POST", ...json({ overrides }) });

/** Records that a person approved one template - never that it was sent (`CopyBatch` is read-only). */
export const postApproveTemplate = (runId, templateId, approvedBy) =>
  request(
    `/runs/${encodeURIComponent(runId)}/campaign-copy/templates/${encodeURIComponent(templateId)}/approve`,
    { method: "POST", ...json({ approved_by: approvedBy }) },
  );

/** Re-generates one template in place; the response is the replacement `CopyTemplate`. */
export const postRegenerateTemplate = (runId, templateId) =>
  request(
    `/runs/${encodeURIComponent(runId)}/campaign-copy/templates/${encodeURIComponent(templateId)}/regenerate`,
    { method: "POST" },
  );

export const copyMessagesUrl = (runId) => url(`/runs/${encodeURIComponent(runId)}/copy_messages.csv`);

// --- AWS connection (engine/aws_connection.py): a source, never a secret -----------------------

/** Where Bedrock is called from right now, and whether this caller may change it. */
export const getAwsConnection = () => request("/connection/aws");

/** Saves the chosen source. `403 CONNECTION_LOCKED` outside a local, loopback caller. */
export const putAwsConnection = (connection) =>
  request("/connection/aws", { method: "PUT", ...json(connection) });

/** Forgets the chosen profile and returns to the default credential chain. */
export const deleteAwsConnection = () => request("/connection/aws", { method: "DELETE" });

/**
 * Checks an identity for free - `sts:GetCallerIdentity` plus a Bedrock availability check per
 * configured model, never a model call. `body` is `{}` to test what is saved, or
 * `{connection}` to try a selection before it is saved.
 */
export const postTestAwsConnection = (body = {}) =>
  request("/connection/aws/test", { method: "POST", ...json(body) });
