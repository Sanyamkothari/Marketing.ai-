// Every call the screens make. Nothing here invents a value: a field the API did not send stays
// undefined and the renderers show an em dash for it (plan §9.4).

import { goalName, plainText } from "./labels.js";

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

/**
 * Plan M §4.2 (M172, DEC-1602): a goal is shown under its Plan M name (`ui/labels.js` GOAL_NAMES). The API's
 * own name is kept as `config_name`; ids and every request are unchanged. Done here, where every screen reads a
 * use case, so no page shows the configured name by accident.
 */
export function withGoalName(useCase) {
  if (useCase && typeof useCase === "object" && useCase.id && typeof useCase.name === "string") {
    if (!("config_name" in useCase)) useCase.config_name = useCase.name;
    useCase.name = goalName(useCase.id, useCase.config_name);
  }
  return useCase;
}

export const getIndustries = () =>
  request("/industries").then((payload) => {
    for (const industry of (payload && payload.industries) || []) {
      for (const stage of industry.stages || []) (stage.use_cases || []).forEach(withGoalName);
    }
    return payload;
  });
/** The Setup modes' copy (configs/engine.yaml `ui.mode_labels` and the rest) in the page's words (`plainText`). */
function withPlainModes(useCase) {
  const modes = useCase && useCase.setup && useCase.setup.modes;
  for (const mode of Array.isArray(modes) ? modes : []) {
    for (const key of ["label", "help", "dataset_hint", "columns_hint", "run_button"]) {
      if (typeof mode[key] === "string") mode[key] = plainText(mode[key]);
    }
  }
  // The Data, Model and Output pages' titles (`ui.pages`), which a use case may name in engine words.
  const pages = useCase && useCase.pages;
  if (pages && typeof pages === "object") {
    for (const key of Object.keys(pages)) if (typeof pages[key] === "string") pages[key] = plainText(pages[key]);
  }
  return useCase;
}

export const getUseCase = (id) =>
  request(`/use-cases/${encodeURIComponent(id)}`).then((useCase) => withPlainModes(withGoalName(useCase)));
export const templateUrl = (path) => url(path);

/**
 * `mode` is not optional here even though the API defaults it to "train": an upload is validated
 * against the mode it was made for, and `POST /runs` refuses a mismatch with UPLOAD_MODE_MISMATCH.
 * Leaving it out registered every scoring upload as a training one, so "Score new data" failed on
 * the Run click with advice the screen could not act on — it offered to upload the file again,
 * which is what had just gone wrong (DEC-081).
 */
export function postUpload(file, useCaseId, mode, { sheet = null, intake = true } = {}) {
  const form = new FormData();
  form.append("file", file);
  form.append("use_case", useCaseId);
  form.append("mode", mode);
  // Plan L M150 (DEC-1505): a workbook's sheet, and "read it as it is" (intake off).
  if (sheet) form.append("sheet", sheet);
  if (!intake) form.append("intake", "false");
  return request("/uploads", { method: "POST", body: form });
}

export const getProfile = (uploadId) => request(`/uploads/${encodeURIComponent(uploadId)}/profile`);

export const postRun = (payload) =>
  request("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

/** `mode` and `clientId` narrow the history with the filters `GET /runs` already takes. */
export function getRuns(useCaseId, { mode, clientId } = {}) {
  const query = new URLSearchParams({ use_case: useCaseId });
  if (mode) query.set("mode", mode);
  if (clientId) query.set("client_id", clientId);
  return request(`/runs?${query}`);
}

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
export const treatListUrl = (runId) => url(`/runs/${encodeURIComponent(runId)}/treat_list.csv`);

// Plan M M171 (DEC-1601): what a scoring run's pages ask the server once for the one contact list - the treat
// list's summary (the route the Output page's own card has always read) and the two file addresses. The answer
// is shared by every part of a page that needs it (the header's download, the sentence under the tiles, the
// "Treat list" counts under Technical details), so a page makes one request. The first request builds the list
// on the server, so a page never waits for it: it draws with `contactListNow` and draws the answer in when
// `loadContactList` resolves. A refusal (the list cannot be built for this run) is kept for a few seconds for
// the same reason and then forgotten, so the next visit asks again.
const KEEP_REFUSAL_MS = 5000;
const contactListAnswers = new Map();
const TREAT_SUMMARY = "treat_list_summary.json";

const hrefsOf = (runId) => ({ treatHref: treatListUrl(runId), scoresHref: scoresUrl(runId) });

// The server says "this run cannot have a treat list" with a 409 (TreatListError) or a 404; any other failure
// (the network, a 500, a sign-in that expired) says nothing about the run.
const cannotHaveTreatList = (error) => error instanceof ApiError && (error.status === 409 || error.status === 404);

const keepable = (entry) => !entry.settled || !entry.refused || Date.now() - entry.settled < KEEP_REFUSAL_MS;

/**
 * What is known about the contact list right now, without asking: `{ summary, error, refused, treatHref,
 * scoresHref }` once the server has answered, else the same with nothing in it (the treat list is assumed).
 */
export function contactListNow(runId) {
  const known = contactListAnswers.get(runId);
  const answer = known && known.answer && keepable(known) ? known.answer : null;
  return answer || { summary: null, error: null, refused: false, ...hrefsOf(runId) };
}

/** The same shape, once the server has answered. Never rejects; the server's own error is kept in `error`. */
export function loadContactList(runId) {
  const known = contactListAnswers.get(runId);
  if (known && keepable(known)) return known.promise;
  const hrefs = hrefsOf(runId);
  const entry = { settled: 0, refused: false, answer: null, promise: null };
  entry.promise = request(`/runs/${encodeURIComponent(runId)}/artefacts/${encodeURIComponent(TREAT_SUMMARY)}`).then(
    (summary) => ({ summary: summary || null, error: null, refused: false, ...hrefs }),
    (error) => ({ summary: null, error, refused: cannotHaveTreatList(error), ...hrefs }),
  );
  contactListAnswers.set(runId, entry);
  entry.promise.then((answer) => {
    entry.answer = answer;
    entry.refused = answer.refused;
    entry.settled = Date.now();
  });
  return entry.promise;
}

/**
 * Where a built dataset came from - its sources, mappings and recipe, each card already worded by
 * the engine (Plan A M35). A run that read an uploaded file has no dataset and never asks.
 */
export const getDatasetLineage = (datasetId) => request(`/datasets/${encodeURIComponent(datasetId)}/lineage`);

export const getModels = (useCaseId) => request(`/models?use_case=${encodeURIComponent(useCaseId)}`);

/** Start a training run on recent data (a challenger pending approval); `NO_TRAINING_DATA` when nothing is labelled. */
export const postRetrainNow = (payload) =>
  request("/schedules/retrain-now", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

// --- Plan J M108 (DEC-1318): the cost of a run before it starts, and what runs have cost ------------

/** What one run of this use case could cost at list price, the cap on it, and whether it needs confirming. */
export const getCostEstimate = (useCaseId, mode) =>
  request(`/use-cases/${encodeURIComponent(useCaseId)}/cost-estimate?mode=${encodeURIComponent(mode)}`);

/** What finished runs cost each month (`GET /cost/spend`). */
export const getSpend = (months = 6) => request(`/cost/spend?months=${encodeURIComponent(months)}`);

/** The saved exchange rate, or `{ fx_rate: null }` (`GET /cost/fx-rate`). */
export const getFxRate = () => request("/cost/fx-rate");

/** Save the exchange rate rupee amounts use (`PUT /cost/fx-rate`, Admin). */
export const putFxRate = (payload) =>
  request("/cost/fx-rate", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

/** Forget the exchange rate (`DELETE /cost/fx-rate`, Admin). */
export const deleteFxRate = () => request("/cost/fx-rate", { method: "DELETE" });
