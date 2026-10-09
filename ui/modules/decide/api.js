// The calls the campaign screens make (Plan J M94), in the shape `ui/api.js` established: a private
// `request` and one exported function per endpoint. Every number the screens draw comes from these
// answers (`api/routes/campaigns.py`); nothing here computes one.

import { API_BASE, ApiError } from "../../api.js";

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(API_BASE + path, options);
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

const post = (payload) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

const campaignPath = (id) => `/campaigns/${encodeURIComponent(id)}`;

/** Every campaign, newest first (`GET /campaigns`). */
export const getCampaigns = () => request("/campaigns");

/** One campaign: its record, stored report, verdict and test plan (`GET /campaigns/{id}`). */
export const getCampaign = (id) => request(campaignPath(id));

/** The plan in force and every version (`GET /campaigns/{id}/plan`). */
export const getPlan = (id) => request(`${campaignPath(id)}/plan`);

/**
 * The planner's points on this campaign's population (`GET /campaigns/{id}/plan-preview`): the slider
 * steps through these only. `baseRate` (0 to 1) is the rate the person expects; the plan's when null.
 */
export const getPlanPreview = (id, baseRate = null) =>
  request(`${campaignPath(id)}/plan-preview${baseRate === null ? "" : `?base_rate=${encodeURIComponent(baseRate)}`}`);

/** Register the test plan (`POST /campaigns/{id}/plan`). */
export const postPlan = (id, payload) => request(`${campaignPath(id)}/plan`, post(payload));

/** Measure the campaign now (`POST /campaigns/{id}/measure`). */
export const postMeasure = (id) => request(`${campaignPath(id)}/measure`, post({}));

/** The treat list summary of a scoring run (`GET /runs/{id}/artefacts/treat_list_summary.json`). */
export const getTreatListSummary = (runId) => request(`/runs/${encodeURIComponent(runId)}/artefacts/treat_list_summary.json`);

/** The conflicts summary across arbitrated use cases (`GET /decide/conflicts`). */
export const getArbitrationConflicts = () => request("/decide/conflicts");

/** Audit a campaign another tool ran (`POST /campaigns/audit`, Plan J M103). */
export const postAudit = (payload) => request("/campaigns/audit", post(payload));

/** The whole programme against the universal holdout (`POST /campaigns/programme`, Plan J M103). */
export const postProgramme = (payload) => request("/campaigns/programme", post(payload));

/** Say who a campaign actually contacted (`POST /campaigns/{id}/contacts`, Plan J M103). */
export const postContacts = (id, payload) => request(`${campaignPath(id)}/contacts`, post(payload));

const driftEventsPath = (runId) => `/runs/${encodeURIComponent(runId)}/drift-events`;

/** The events noted against a scoring run's change report, with the server's reading (Plan J M109). */
export const getDriftEvents = (runId) => request(driftEventsPath(runId));

/** Note an event (`POST /runs/{id}/drift-events`, Analyst); the answer is the whole view. */
export const postDriftEvent = (runId, payload) => request(driftEventsPath(runId), post(payload));

/** Remove a noted event (`DELETE /runs/{id}/drift-events/{annotation_id}`, Analyst); the answer is the whole view. */
export const deleteDriftEvent = (runId, annotationId) =>
  request(`${driftEventsPath(runId)}/${encodeURIComponent(annotationId)}`, { method: "DELETE" });
