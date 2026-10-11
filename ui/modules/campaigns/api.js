// The calls the Campaigns list and the campaign page make (Plan M M176, DEC-1606). Every one is an existing route;
// this milestone adds none. The campaign record, plan and measurement are Plan J's (`ui/modules/decide/api.js`,
// reused as they are); the run, its upload and a new scoring run are Phase 1's (`ui/api.js`).

import {
  API_BASE,
  ApiError,
  getIndustries,
  getRun,
  loadContactList,
  postRun,
  postUpload,
  treatListUrl,
} from "../../api.js";
import { getCampaign, getCampaigns, postMeasure } from "../decide/api.js";
import { ContactListReader } from "./audience.js";

export { getCampaign, getCampaigns, getIndustries, getRun, postMeasure, postRun, postUpload, treatListUrl };

async function send(path, options = {}) {
  try {
    return await fetch(API_BASE + path, options);
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
}

async function request(path, options = {}, { text = false } = {}) {
  return answer(await send(path, options), { text });
}

/** An answer's body (JSON, or the text when asked), or its refusal as an `ApiError` in the API's own words. */
async function answer(response, { text = false } = {}) {
  const raw = await response.text();
  if (response.ok && text) return raw;
  let body = null;
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = raw;
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

const enc = encodeURIComponent;

/** The campaigns made from one scoring run's list, newest first (`GET /campaigns?run_id=`). */
export const getCampaignsForRun = (runId) => request(`/campaigns?run_id=${enc(runId)}`);

/** Every campaign's Value Proof Pack, ready or not, with its headline (`GET /pilot/proof`). */
export const getProofs = () => request("/pilot/proof");

/** One campaign's Value Proof Pack as data (`GET /pilot/proof/{id}?format=json`). */
export const getProof = (campaignId) => request(`/pilot/proof/${enc(campaignId)}?format=json`);

/** The contact list's counts, asked once per run and shared with the run's own pages (M171's `loadContactList`). */
export { loadContactList };

/**
 * The contact list, read from the CSV a sending tool is given (`GET /runs/{id}/treat_list.csv`) as it arrives (`audience.js` `ContactListReader`): the file is decoded
 * and read piece by piece, so neither the whole text nor a table of it is ever held, and the page stays responsive
 * between pieces. A browser (or test) whose answer has no readable body is given the text in one piece.
 */
export async function readContactList(runId, primaryKey) {
  const response = await send(`/runs/${enc(runId)}/treat_list.csv`);
  const reader = new ContactListReader(primaryKey);
  if (!response.ok || !response.body || typeof response.body.getReader !== "function") {
    return reader.push(await answer(response, { text: true })).end();
  }
  const pieces = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  for (;;) {
    const { done, value } = await pieces.read();
    if (done) break;
    reader.push(decoder.decode(value, { stream: true }));
  }
  return reader.push(decoder.decode()).end();
}

/** Give a campaign its results file (`POST /campaigns/{id}/outcomes`, Plan J M94). */
export const postOutcomes = (campaignId, payload) => request(`/campaigns/${enc(campaignId)}/outcomes`, post(payload));
