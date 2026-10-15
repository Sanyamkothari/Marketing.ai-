// The calls the "New campaign" wizard makes (M177, DEC-1607). Existing routes only: nothing here is new to
// the API, and every call that `ui/api.js` or the campaign screens' `decide/api.js` already make is reused
// from there rather than written twice.

import {
  API_BASE,
  ApiError,
  getCostEstimate,
  getIndustries,
  getModels,
  getRun,
  getUseCase,
  postRun,
  postUpload,
  treatListUrl,
} from "../../api.js";
import { getCampaign, getPlanPreview, getTreatListSummary, postPlan } from "../decide/api.js";
import { getMe, postApprove } from "../production/api.js";

export {
  getCampaign,
  getCostEstimate,
  getIndustries,
  getMe,
  getModels,
  getPlanPreview,
  getRun,
  getTreatListSummary,
  getUseCase,
  postApprove,
  postPlan,
  postRun,
  postUpload,
  treatListUrl,
};

export async function request(path, options = {}) {
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

const runPath = (id) => `/runs/${encodeURIComponent(id)}`;

const post = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const runsPath = (id) => `/campaign-runs/${encodeURIComponent(id)}`;

/** Start a campaign on the server (`POST /campaign-runs`): learn, score, build the list. It goes on without the browser. */
export const postCampaignRun = (payload) => request("/campaign-runs", post(payload));

/** Where one campaign run is (`GET /campaign-runs/{id}`): `{ record, pct, eta_seconds, summary }`. */
export const getCampaignRun = (id) => request(runsPath(id));

/** The campaign runs, newest first (`GET /campaign-runs`): `{ campaign_runs: [...] }`. */
export const getCampaignRuns = () => request("/campaign-runs");

/** Start a stopped or failed campaign run again as a new one (`POST /campaign-runs/{id}/restart`). */
export const restartCampaignRun = (id, { confirmCost = false } = {}) =>
  request(`${runsPath(id)}/restart`, post({ confirm_cost: confirmCost }));

/**
 * What the Review page's plain summary needs besides the list's own numbers: the Data Doctor case the file came
 * from (read off the campaign run that made this contact list) and the first customers of the list, best first.
 * Never rejects: a part that cannot be read is `{ error }` (or `{ denied: true }` for the names without the Analyst
 * role) and the summary says so in words. A list made without a campaign run has no case, and its first customers
 * are pointed to on the full results page (`{ elsewhere: true }`).
 */
export async function getResultsSummary(scoreRunId, { canSeeNames = true } = {}) {
  let found = null;
  try {
    const listed = await request(`/campaign-runs?score_run_id=${encodeURIComponent(scoreRunId)}&limit=1`);
    found = ((listed && listed.campaign_runs) || [])[0] || null;
  } catch (error) {
    return { caseId: null, top: { error } };
  }
  // A list made before campaign runs (or by hand) has no run to read its first customers through: say where they are.
  if (!found) return { caseId: null, top: { elsewhere: true } };
  const record = found.record;
  const caseId = record.case_id || null;
  if (!canSeeNames) return { caseId, top: { denied: true } };
  try {
    return { caseId, top: await request(`${runsPath(record.campaign_run_id)}/top-customers?limit=10`) };
  } catch (error) {
    return { caseId, top: { error } };
  }
}

/** A scoring run's summary (`scoring_summary.json`): its priorities, counts and average likelihood. */
export const getScoringSummary = (runId) => request(`${runPath(runId)}/artefacts/scoring_summary.json`);

/**
 * The compliance gate's summary of a list (`compliance_decisions.json`, Plan K M134). Asked for only when the
 * list's summary says the gate decided it (`compliance_rule_version`), so a deployment with the switch off
 * never asks for a file that cannot exist.
 */
export const getComplianceSummary = (runId) => request(`${runPath(runId)}/artefacts/compliance_decisions.json`);
