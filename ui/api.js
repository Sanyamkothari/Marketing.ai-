// Every call the screens make. Nothing here invents a value: a field the API did not send stays
// undefined and the renderers show an em dash for it (plan §9.4).

export const API_BASE = (window.MARKETING_AI_API || window.location.origin).replace(/\/+$/, "");

/** An API response that was not a success, carrying the server's own error envelope. */
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
  } catch (cause) {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const body = await parse(response);
  if (response.ok) return body;
  const detail = body && body.detail;
  const code = (detail && detail.code) || `HTTP_${response.status}`;
  const message = (detail && detail.message) || `The API answered ${response.status}.`;
  throw new ApiError(response.status, code, message, body);
}

export const getIndustries = () => request("/industries");
export const getUseCase = (id) => request(`/use-cases/${encodeURIComponent(id)}`);
export const templateUrl = (path) => url(path);

/**
 * `mode` is not optional here even though the API defaults it to "train": an upload is validated
 * against the mode it was made for, and `POST /runs` refuses a mismatch with UPLOAD_MODE_MISMATCH.
 * Leaving it out registered every scoring upload as a training one, so "Score new data" failed on
 * the Run click with advice the screen could not act on — it offered to upload the file again,
 * which is what had just gone wrong (DEC-076).
 */
export function postUpload(file, useCaseId, mode) {
  const form = new FormData();
  form.append("file", file);
  form.append("use_case", useCaseId);
  form.append("mode", mode);
  return request("/uploads", { method: "POST", body: form });
}

export const getProfile = (uploadId) => request(`/uploads/${encodeURIComponent(uploadId)}/profile`);

export const postRun = (payload) =>
  request("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const getRuns = (useCaseId) => request(`/runs?use_case=${encodeURIComponent(useCaseId)}`);

export const getRun = (runId) => request(`/runs/${encodeURIComponent(runId)}`);

export const cancelRun = (runId) => request(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });

/** One artefact, or `null` when this run has not produced it — which the pages render as an em dash. */
export async function getArtefact(runId, name) {
  try {
    return await request(`/runs/${encodeURIComponent(runId)}/artefacts/${encodeURIComponent(name)}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

/** Several artefacts at once; each missing one comes back as `null`. */
export async function getArtefacts(runId, names) {
  const loaded = await Promise.all(names.map((name) => getArtefact(runId, name)));
  return Object.fromEntries(names.map((name, index) => [name, loaded[index]]));
}

export const scoresUrl = (runId) => url(`/runs/${encodeURIComponent(runId)}/scores.csv`);

export const getModels = (useCaseId) => request(`/models?use_case=${encodeURIComponent(useCaseId)}`);
