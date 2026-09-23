// The calls the uplift screens make, in the shape `ui/api.js` established: a thin `request`
// wrapper and one exported function per endpoint. Nothing here invents a field the server did not
// send - an artefact a run has not produced comes back as `null`, and the screens render "—".
//
// `ui/api.js` keeps `request` private, so it is repeated here (as `modules/generative/api.js` does);
// `API_BASE` and `ApiError` are reused, because one origin and one error shape across the whole app
// is what lets `errorBox` render a failure from any screen the same way. The Phase 1 calls the uplift
// screens also need (`POST /uploads`, `GET /runs/{id}`, `GET /models`, ...) are imported from
// `ui/api.js` by the controllers rather than duplicated.
//
// Every path below is the Stage G contract in UPLIFT_INTERFACES.md ("Stage F - API it talks to").

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

/** `null` for a 404 - "this run did not produce it" - and a thrown `ApiError` for anything else. */
async function orNull(promise) {
  try {
    return await promise;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

const json = (payload) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

const run = (runId) => `/runs/${encodeURIComponent(runId)}`;

/** The 0/1 columns of an upload that could be the treatment, and the one the config points at. */
export const getTreatmentCandidates = (uploadId, useCaseId) =>
  request(
    `/uploads/${encodeURIComponent(uploadId)}/treatment-candidates?use_case=${encodeURIComponent(useCaseId)}`,
  );

/**
 * Starts an uplift training run: `202 {run_id}`, or `409` whose body carries both
 * `validation.json` and `uplift_validation.json` for the Setup screen to render inline.
 */
export const postUpliftRun = (payload) => request("/uplift/runs", json(payload));

/** One uplift artefact of a run, or `null` when the run did not produce it. */
export const getUpliftArtefact = (runId, name) =>
  orNull(request(`${run(runId)}/uplift/${encodeURIComponent(name)}`));

/** Several uplift artefacts at once, keyed by file name; each missing one is `null`. */
export async function getUpliftArtefacts(runId, names) {
  const loaded = await Promise.all(names.map((name) => getUpliftArtefact(runId, name)));
  return Object.fromEntries(names.map((name, index) => [name, loaded[index]]));
}

/** Measures a finished campaign from an uploaded outcomes file; the body is the report itself. */
export const postCampaignResults = (runId, payload) => request(`${run(runId)}/campaign-results`, json(payload));

/** The stored campaign report of a run, or `null` before any outcomes were uploaded. */
export const getCampaignResults = (runId) => orNull(request(`${run(runId)}/campaign-results`));

/** Off-policy estimate of a targeting rule on a training run's hold-out: `{top_share}` or `{min_uplift}`. */
export const postOpe = (runId, rule) => request(`${run(runId)}/uplift/ope`, json(rule));
