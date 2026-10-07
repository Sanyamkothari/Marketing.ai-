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

/** Register the test plan (`POST /campaigns/{id}/plan`). */
export const postPlan = (id, payload) => request(`${campaignPath(id)}/plan`, post(payload));

/** Measure the campaign now (`POST /campaigns/{id}/measure`). */
export const postMeasure = (id) => request(`${campaignPath(id)}/measure`, post({}));
