// The calls the Reports page makes (Plan M, M178, DEC-1608). Nothing here is new: every call is one an existing
// screen already makes, and is re-exported so this module has one place that names them.
//
//   Value Proof Packs       GET /pilot/proof (+ the pack and its PDF at /pilot/proof/<campaign>)   pilot/api.js
//   Programme summary       GET /campaigns/summary                                                 api.js (readSummary)
//   Check a past campaign   POST /uploads, POST /campaigns/audit, POST /campaigns/programme        api.js, decide/api.js
//   Model reports           GET /models, /runs?mode=score, /datasets, /pilot/demo                  pilot/api.js

export { getIndustries, postUpload } from "../../api.js";
export { postAudit, postProgramme } from "../decide/api.js";
export {
  getDemo,
  listDatasets,
  listModels,
  listProofs,
  listScoringRuns,
  proofUrl,
  readinessUrl,
  resultsUrl,
  roiUrl,
} from "../pilot/api.js";

import { API_BASE, ApiError } from "../../api.js";

/**
 * `GET /campaigns/summary`, and an error when it cannot be read. Results' own read (`simple/api.js getSummary`) turns every
 * failure into null, which is right for a card that may simply be missing, but this tab is the page that says what is
 * proven: it must tell "nothing proven" (a 200 with no totals) from "could not be read" (a 403, a 500, no network).
 */
export async function readSummary() {
  let response;
  try {
    response = await fetch(`${API_BASE}/campaigns/summary`);
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
  throw new ApiError(
    response.status,
    (detail && detail.code) || `HTTP_${response.status}`,
    (detail && detail.message) || `The API answered ${response.status}.`,
    body,
  );
}
