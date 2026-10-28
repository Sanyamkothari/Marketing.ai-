// "Ask your data" calls (Plan I, `/uploads/{id}/ask`). Every route hangs off the upload it is about.
//
// Errors are the shared `ApiError` from `ui/api.js`, carrying the server's own envelope: a `409` from
// `apply` whose checks would fail keeps its `validation` report in `error.body`, exactly as a `409`
// from `POST /runs` does, so the screen reads both the same way.

import { API_BASE, ApiError } from "../../api.js";

const enc = encodeURIComponent;

async function call(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const text = await response.text();
  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (response.ok) return parsed;
  const detail = parsed && parsed.detail;
  throw new ApiError(
    response.status,
    (detail && detail.code) || `HTTP_${response.status}`,
    (detail && detail.message) || `The API answered ${response.status}.`,
    parsed,
  );
}

// Ask your data (Plan I, DEC-1250): the helper's read-only chat on an upload, outside Guided setup.
const ask = (uploadId, tail = "") => `/uploads/${enc(uploadId)}/ask${tail}`;

/** The upload's Ask-your-data chat: `{ session | null, chat, charts }`. */
export const getAsk = (uploadId) => call(ask(uploadId));

/** One question; the reply (and any chart under it) comes back in the same envelope. */
export const postAsk = (uploadId, text) => call(ask(uploadId, "/messages"), { method: "POST", body: { text } });

/** Start a new chat: the old one is deleted. */
export const clearAsk = (uploadId) => call(ask(uploadId), { method: "DELETE" });
