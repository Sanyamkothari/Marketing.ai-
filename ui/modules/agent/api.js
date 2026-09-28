// Guided setup's calls (Plan G, `api/routes/agent.py`, DEC-1018). Every session route hangs off the
// upload it is about, so the only id this file ever needs is the upload's.
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

const session = (uploadId, tail = "") => `/uploads/${enc(uploadId)}/agent-session${tail}`;

/** Start (or restart) Guided setup for an upload: `{ session, chat: { backend, generation_model_id } }`. */
export const startSession = (uploadId, useCaseId, modelVersionId = null) =>
  call(session(uploadId), { method: "POST", body: { use_case: useCaseId, model_version_id: modelVersionId } });

/** The session as it stands, in the same envelope. */
export const getSession = (uploadId) => call(session(uploadId));

/** Accept or reject suggestions: `decisions` is `[{ proposal_id, state, value? }]`. */
export const postDecisions = (uploadId, decisions, { acceptRecommended = false } = {}) =>
  call(session(uploadId, "/decisions"), {
    method: "POST",
    body: { decisions, accept_recommended: acceptRecommended },
  });

/** Answer one question with one of its options. */
export const postAnswer = (uploadId, questionId, optionId) =>
  call(session(uploadId, "/answers"), {
    method: "POST",
    body: { question_id: questionId, option_id: optionId },
  });

/** One chat turn; the reply comes back in the session's transcript. */
export const postMessage = (uploadId, text) => call(session(uploadId, "/messages"), { method: "POST", body: { text } });

/** The first rows before and after the accepted steps: `{ columns_before, rows_before, columns_after, rows_after, receipt }`. */
export const postPreview = (uploadId) => call(session(uploadId, "/preview"), { method: "POST" });

/** Approve: `{ upload_id, mode, primary_key, target, overrides, summary, receipt }`, or a 409. */
export const postApply = (uploadId) => call(session(uploadId, "/apply"), { method: "POST" });
