// The calls the pilot screens make (Plan E), in the shape `ui/api.js` established: a private
// `request` and one exported function per endpoint. `API_BASE` and `ApiError` are reused so an error
// from here renders like one from any other screen. Nothing here invents a value: a 404 comes back
// as `null` where the screens then say what is missing.

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

async function orNull(promise) {
  try {
    return await promise;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

const send = (method, payload) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

const q = (params) => {
  const parts = Object.entries(params)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
  return parts.length ? `?${parts.join("&")}` : "";
};

export const getHelp = () => request("/pilot/help");
export const getDemo = () => request("/pilot/demo");
export const postFeedback = (payload) => request("/pilot/feedback", send("POST", payload));

export const dataRequestUrl = () => url("/pilot/data-request");
export const templateUrl = (role) => url(`/pilot/templates/${encodeURIComponent(role)}`);
export const demoRawUrl = (variant) => url(`/pilot/demo/raw/${encodeURIComponent(variant)}`);
export const feedbackExportUrl = () => url("/pilot/feedback/export");

export const readinessUrl = (datasetId, format) =>
  url(`/pilot/readiness/${encodeURIComponent(datasetId)}${q({ format })}`);
export const resultsUrl = (useCaseId, format) => url(`/pilot/results${q({ use_case: useCaseId, format })}`);
export const roiUrl = (runId, format) => url(`/pilot/roi/${encodeURIComponent(runId)}${q({ format })}`);

/** A report's HTML, fetched through the wrapped `fetch` so a sign-in is carried. */
export const getReportHtml = (href) => orNull(request(href));

export const getRoi = (runId) => orNull(request(`/pilot/roi/${encodeURIComponent(runId)}`));
export const putRoi = (runId, inputs) => request(`/pilot/roi/${encodeURIComponent(runId)}`, send("PUT", inputs));

export const listDatasets = () => request("/datasets");
export const listScoringRuns = () => request(`/runs${q({ mode: "score", limit: 50 })}`);
export const listModels = () => request("/models");
export const getDataRequest = () => request("/pilot/data-request?format=json");
