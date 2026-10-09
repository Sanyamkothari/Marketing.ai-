// The calls the Results and Settings pages make (Plan H M82), in the shape `ui/api.js` established:
// a private `request` and one exported function per endpoint. `API_BASE` and `ApiError` are reused so
// an error from here renders like one from any other screen. Nothing here invents a value.

import { API_BASE, ApiError } from "../../api.js";

/** The most runs `GET /runs` returns in one answer (`api/routes/runs.py` `MAX_LIMIT`). */
export const RUNS_SHOWN = 100;

async function request(path) {
  let response;
  try {
    response = await fetch(API_BASE + path);
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (response.ok) return body;
  const detail = body && body.detail;
  const code = (detail && detail.code) || `HTTP_${response.status}`;
  const message = (detail && detail.message) || `The API answered ${response.status}.`;
  throw new ApiError(response.status, code, message, body);
}

/** Every run of every use case, newest first (`GET /runs`, at most `RUNS_SHOWN`). */
export const getAllRuns = () => request(`/runs?limit=${RUNS_SHOWN}`);

/** How many models wait for approval (`GET /approvals`), or null when this person may not ask. */
export async function waitingForApproval() {
  try {
    const body = await request("/approvals");
    return ((body && body.items) || []).length;
  } catch {
    return null; // refused (a role without approvals) or unreachable: no badge, never an error here
  }
}

/**
 * Plan J M104: the newest campaigns' Value Proof Packs (`GET /pilot/proof`), or null when they cannot be
 * read (a role without them, or an API without the route): Results then shows no card, never an error.
 */
export async function getProofs() {
  try {
    return await request("/pilot/proof");
  } catch {
    return null;
  }
}

/** The engine version this API serves (`GET /healthz`), or null. */
export async function engineVersion() {
  try {
    const body = await request("/healthz");
    return (body && body.version) || null;
  } catch {
    return null;
  }
}
