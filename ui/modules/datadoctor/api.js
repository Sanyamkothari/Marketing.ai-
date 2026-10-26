// The Data Doctor screen's calls (Plan O M247, DEC-1817): the case routes of M231, M235 and M246, and M247's own
// two reads (`api/routes/case_screen.py`). Every change names the case version it was made against and carries an
// idempotency key, so a click sent twice changes the case once. Errors are the shared `ApiError` of `ui/api.js`.

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

let ops = 0;
/** An idempotency key for one click: a retried request with the same key changes nothing. */
export const opId = () => `dd-${Date.now().toString(36)}-${(ops += 1).toString(36)}`;

const one = (caseId, tail = "") => `/cases/${enc(caseId)}${tail}`;

/** `{ case, timeline, view }` (`engine/doctor/screen.py` `CaseScreen`). */
export const getScreen = (caseId) => call(one(caseId, "/screen"));

/** `{ cases: [{ case_id, title, goal_id, state, files, upload_ids, updated_at }] }`, newest change first. */
export const getCases = (goalId = null) =>
  call(`/data-doctor/cases${goalId ? `?goal_id=${enc(goalId)}` : ""}`);

/** Open a case, optionally with uploads; 201 with the case. */
export const createCase = (uploadIds = [], title = "") =>
  call("/cases", { method: "POST", body: { upload_ids: uploadIds, title } });

/** Add one upload to the case. */
export const addFile = (c, uploadId) =>
  call(one(c.case_id, "/files"), { method: "POST", body: { version: c.version, upload_id: uploadId } });

/** Continue the visit to its next rest. */
export const resumeVisit = (caseId) => call(one(caseId, "/visit"), { method: "POST", body: {} });

/** Choose, switch or clear (`null`) the goal: nothing decided is asked again. */
export const chooseGoal = (c, goalId) =>
  call(one(c.case_id, "/goal"), { method: "POST", body: { version: c.version, goal_id: goalId, op_id: opId() } });

/** Answer one question with one of its options. */
export const answerQuestion = (c, questionId, optionId, value) =>
  call(one(c.case_id, "/answers"), {
    method: "POST",
    body: {
      version: c.version,
      question_id: questionId,
      option_id: optionId,
      ...(value ? { value } : {}),
      op_id: opId(),
    },
  });

/** Tick (`accept: true`) or untick a suggestion. */
export const decideItem = (c, itemId, accept) =>
  call(one(c.case_id, "/decisions"), {
    method: "POST",
    body: { version: c.version, item_id: itemId, accept, op_id: opId() },
  });

/** Park a step for the one who would know (`crm_admin`, `database_admin`, `compliance`, `finance`, `campaign_manager`): it is left out until resumed. */
export const parkItem = (c, itemId, role) =>
  call(one(c.case_id, "/decisions"), {
    method: "POST",
    body: { version: c.version, item_id: itemId, accept: false, park_for: role, op_id: opId() },
  });

/** The own yes of one change that needs it, with its sentence word for word. */
export const confirmItem = (c, itemId, sentence) =>
  call(one(c.case_id, "/confirmations"), {
    method: "POST",
    body: { version: c.version, item_id: itemId, sentence, op_id: opId() },
  });

/** Approve, with the sentence read word for word. */
export const approveCase = (c, sentence) =>
  call(one(c.case_id, "/approval"), { method: "POST", body: { version: c.version, sentence, op_id: opId() } });

/** Say yes to one stop of the plan, by its `key` (`view.ledger.findings[].key`), with a reason that is logged. */
export const acknowledgeStop = (c, key, reason = "") =>
  call(one(c.case_id, "/acknowledgements"), {
    method: "POST",
    body: { version: c.version, key, reason, op_id: opId() },
  });

/** Revoke a remembered answer: its question is asked again here, and next time. */
export const revokeStanding = (c, answerId) =>
  call(one(c.case_id, `/standing-answers/${enc(answerId)}/revoke`), {
    method: "POST",
    body: { version: c.version, op_id: opId() },
  });

/** Write to Data Doctor; with no AI service the reply says so and nothing else changes. */
export const sendChat = (c, message) =>
  call(one(c.case_id, "/chat"), { method: "POST", body: { version: c.version, message, op_id: opId() } });

/** Whether the client let the Doctor's AI read masked summaries, and what this case has used of its AI limit. */
export const getAiStatus = (caseId) => call(one(caseId, "/ai"));

/** Say once, for this client, that the Doctor's AI may read masked summaries of its data. */
export const giveAiConsent = (caseId) => call(one(caseId, "/ai-consent"), { method: "POST", body: {} });

/** The list of what to ask the data team for, for these goals (the data request kit, `GET /pilot/data-request`). */
export const dataRequestUrl = (goalIds = []) =>
  `${API_BASE}/pilot/data-request${goalIds.length ? `?${goalIds.map((g) => `use_case=${enc(g)}`).join("&")}` : ""}`;
