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

const runPath = (id) => `/runs/${encodeURIComponent(id)}`;

/** Record the campaign sent from a finished scoring run's list (`POST /campaigns`, Plan J M94). */
export const postCampaign = (payload) =>
  request("/campaigns", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

/** The campaigns made from one scoring run (`GET /campaigns?run_id=`): a refresh finds the one it made. */
export const getCampaignsOfRun = (runId) => request(`/campaigns?run_id=${encodeURIComponent(runId)}`);

/** A scoring run's summary (`scoring_summary.json`): its priorities, counts and average likelihood. */
export const getScoringSummary = (runId) => request(`${runPath(runId)}/artefacts/scoring_summary.json`);

/**
 * The compliance gate's summary of a list (`compliance_decisions.json`, Plan K M134). Asked for only when the
 * list's summary says the gate decided it (`compliance_rule_version`), so a deployment with the switch off
 * never asks for a file that cannot exist.
 */
export const getComplianceSummary = (runId) => request(`${runPath(runId)}/artefacts/compliance_decisions.json`);
