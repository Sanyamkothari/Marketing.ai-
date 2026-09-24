// Every call the Phase 4b screens make (M46 sign-in and users, M47 audit log, M48 privacy, M49
// schedules, monitoring and outcomes), in the shape
// `ui/api.js` established: a thin `request` wrapper, one exported function per endpoint, and the
// server's own `{detail: {code, message}}` envelope turned into an `ApiError` the screens already
// know how to render with `errorBox`.
//
// `API_BASE` and `ApiError` are imported from `ui/api.js` - one origin and one error shape across the
// whole app - but its private `request` is not exported, so it is mirrored here, as the generative
// and onboarding modules also do. No header is added here: `session.js`'s fetch wrapper puts the
// bearer token on every API call, these included (DEC-790), so this file cannot forget it.
//
// Every path below is served by `api/routes/{auth,audit,privacy,schedules,monitoring}.py` (the run
// list, Phase 1's `GET /runs`, excepted);
// `tests/integration/production/test_production_ui.py` checks each against the app's OpenAPI surface.

import { API_BASE, ApiError } from "../../api.js";

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

/**
 * FastAPI's own 422 (a body the route never saw) carries `detail` as a list of field problems; only
 * the field names are read - never `input`, which would echo a typed password back onto the screen.
 */
function fieldProblem(status, detail, body) {
  const fields = [
    ...new Set(
      detail.map((problem) => (problem.loc || []).filter((part) => typeof part === "string").pop()).filter(Boolean),
    ),
  ];
  const named = fields.length ? `: ${fields.join(", ")}` : "";
  return new ApiError(status, "INVALID_REQUEST", `Some fields are missing or not valid${named}.`, body);
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(url(path), options);
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const body = await parse(response);
  if (response.ok) return body;
  const detail = body && body.detail;
  if (Array.isArray(detail)) throw fieldProblem(response.status, detail, body);
  const code = (detail && detail.code) || `HTTP_${response.status}`;
  const message = (detail && detail.message) || `The API answered ${response.status}.`;
  throw new ApiError(response.status, code, message, body);
}

const json = (method, payload) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

// --- signing in (M46) ---------------------------------------------------------------------------

/** `LoginResponse`: the token (shown once), when it lapses, and who signed in. */
export const postLogin = (username, password) => request("/auth/login", json("POST", { username, password }));

/** Revokes this browser's session on the server. */
export const postLogout = () => request("/auth/logout", { method: "POST" });

/** `MeResponse`: the principal, `auth_mode` and one `PermissionView` per declared route. */
export const getMe = () => request("/auth/me");

// --- users (M46, Admin) -------------------------------------------------------------------------

export const getUsers = () => request("/users");

/** `{username, password, display_name?, roles}`; answers the new `UserView`. */
export const postUser = (payload) => request("/users", json("POST", payload));

/** `{display_name?, roles?, disabled?}`; `409 LAST_ADMIN` when it would leave nobody to administer. */
export const patchUser = (userId, payload) => request(`/users/${encodeURIComponent(userId)}`, json("PATCH", payload));

/** An Admin sets anyone's; a person changes their own with `current_password`. Answers 204. */
export const postPassword = (userId, payload) =>
  request(`/users/${encodeURIComponent(userId)}/password`, json("POST", payload));

// --- the audit log (M47, Admin) -----------------------------------------------------------------

export const AUDIT_FILTERS = ["actor_id", "action", "object_type", "object_id", "outcome", "since", "until"];

/** The query string of an audit filter, without its `?`: empty values are left out, never sent as `""`. */
export function auditQuery(filters, extra = {}) {
  const params = new URLSearchParams();
  for (const key of AUDIT_FILTERS) {
    const value = filters[key];
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  for (const [key, value] of Object.entries(extra)) params.set(key, String(value));
  return params.toString();
}

/** `AuditEventPage`: newest first, with the total that match across every page. */
export function getAuditEvents(filters, limit, offset) {
  const query = auditQuery(filters, { limit, offset });
  return request(`/audit/events?${query}`);
}

/** The same filters as a CSV download; a link, fetched with the token by `downloads.js` (DEC-793). */
export function auditCsvUrl(filters) {
  const query = auditQuery(filters);
  return url(query ? `/audit/events.csv?${query}` : "/audit/events.csv");
}

/** Writes a retained copy (S3 Object Lock, or local JSON lines); answers `AuditExportResult`. */
export const postAuditExport = (payload) => request("/audit/exports", json("POST", payload));

// --- DPDP controls (M48, Admin; the consent report is Viewer) ------------------------------------
//
// Every call that names a data principal is a `POST` with the id in the JSON body, never in the path
// or the query string (DEC-746): a URL is written to access logs, proxies and the browser's history,
// and none of those can be erased. `api/routes/privacy.py` enforces the same rule on its side.

export const getPrivacyPolicy = () => request("/privacy/purposes");

/**
 * `ConsentImportReport`. The API answers a refused file with `422` and the *same* report (every
 * problem by row and column, DEC-751), not the error envelope, so that answer is returned as a report
 * with `imported: false` rather than thrown; any other failure is thrown as usual.
 */
export async function postConsentImport(file, { clientId = "", partial = false } = {}) {
  const form = new FormData();
  form.append("file", file, file.name);
  if (clientId) form.append("client_id", clientId);
  form.append("partial", partial ? "true" : "false");
  try {
    return await request("/privacy/consent/imports", { method: "POST", body: form });
  } catch (error) {
    if (error instanceof ApiError && error.status === 422 && error.body && Array.isArray(error.body.errors)) {
      return error.body;
    }
    throw error;
  }
}

/** `{principal_id, client_id?, as_of?}` → `ConsentLookupResponse` (states per purpose, the rows). */
export const postConsentLookup = (payload) => request("/privacy/consent/lookup", json("POST", payload));

/** `RetentionPlanResponse`: the dry run - `plan`, `plan_hash` and `counts`. Deletes nothing. */
export const getRetentionPlan = () => request("/privacy/retention/plan");

/** Apply a reviewed dry run, named by what it returned; `409 RETENTION_PLAN_CHANGED` when it moved. */
export const postRetentionApply = (planResponse) =>
  request(
    "/privacy/retention/apply",
    json("POST", {
      plan_id: planResponse.plan.plan_id,
      planned_at: planResponse.plan.planned_at,
      plan_hash: planResponse.plan_hash,
    }),
  );

// --- approvals (Plan D M54, DEC-862, DEC-864) ---------------------------------------------------

/** `ApprovalListResponse`: the challengers waiting, each with its head-to-head and whether you may decide. */
export const getApprovals = () => request("/approvals");

/** `{approved_by, reason}` → the version, now champion. `403 SEPARATION_OF_DUTIES` for its trainer. */
export const postApprove = (modelId, payload) =>
  request(`/models/${encodeURIComponent(modelId)}/approve`, json("POST", payload));

/** `{reason}` → `{version, is_champion, decision}`: the challenger archived, the reason recorded. */
export const postReject = (modelId, payload) =>
  request(`/models/${encodeURIComponent(modelId)}/reject`, json("POST", payload));

/** `{principal_id, client_id?}` → 202 `ErasureAccepted`: a background job erases (Plan D, DEC-863). */
export const postErasure = (payload) => request("/privacy/erasure", json("POST", payload));

/** `{principal_id}` again (it is never stored) → 202 `ErasureAccepted`; only a failed request. */
export const postErasureRetry = (requestId, payload) =>
  request(`/privacy/erasure/${encodeURIComponent(requestId)}/retry`, json("POST", payload));

/** Status and per-store progress of one request; a plain read, so it can be polled. */
export const getErasureProgress = (requestId) =>
  request(`/privacy/erasure/${encodeURIComponent(requestId)}/progress`);

/** One request's full record - the completion report. An audited read: asked once, at the end. */
export const getErasure = (requestId) => request(`/privacy/erasure/${encodeURIComponent(requestId)}`);

/** The erasure register, newest first. */
export const getErasures = () => request("/privacy/erasure");

/** Model versions due for retraining because an erasure removed rows they were trained on. */
export const getRetrainFlags = () => request("/privacy/retrain-flags");

/**
 * `{principal_id, client_id?}` → the access export as `{blob, filename}`. A `POST` answering a zip,
 * so it cannot be a link `downloads.js` fetches; the wrapped `fetch` still adds the token (DEC-790).
 */
export async function postAccessRequest(payload) {
  let response;
  try {
    response = await fetch(url("/privacy/access-requests"), json("POST", payload));
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  if (!response.ok) {
    const body = await parse(response);
    const detail = body && body.detail;
    if (Array.isArray(detail)) throw fieldProblem(response.status, detail, body);
    throw new ApiError(
      response.status,
      (detail && detail.code) || `HTTP_${response.status}`,
      (detail && detail.message) || `The API answered ${response.status}.`,
      body,
    );
  }
  return { blob: await response.blob(), disposition: response.headers.get("Content-Disposition") };
}

/** `ConsentReport` of one scoring run, or null when the run was not gated (`CONSENT_REPORT_NOT_FOUND`). */
export const getConsentReport = (runId) =>
  orNull(request(`/privacy/runs/${encodeURIComponent(runId)}/consent-report`), [
    "CONSENT_REPORT_NOT_FOUND",
    "PRIVACY_NOT_CONFIGURED",
  ]);

// --- schedules, monitoring and outcomes (M49; reads Viewer, changes Analyst) --------------------

/** A read that may legitimately not exist yet: null for those codes, thrown for anything else. */
async function orNull(pending, codes) {
  try {
    return await pending;
  } catch (error) {
    if (error instanceof ApiError && codes.includes(error.code)) return null;
    throw error;
  }
}

/**
 * `path` with a query string from `params`; empty values (and `false`, every flag's default) are left
 * out, never sent as `""`. The path stays a literal at each call site, so the static test can check it.
 */
function withQuery(path, params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== null && value !== undefined && value !== "" && value !== false) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `${path}?${text}` : path;
}

const sid = (scheduleId) => encodeURIComponent(scheduleId);

/** `{client_id?, use_case_id?, kind?}` → `ScheduleListResponse`. */
export const getSchedules = (filters = {}) => request(withQuery("/schedules", filters));

/** `ScheduleCreateRequest` → 201 `Schedule`. */
export const postSchedule = (payload) => request("/schedules", json("POST", payload));

export const getSchedule = (scheduleId) => request(`/schedules/${sid(scheduleId)}`);

/** `ScheduleUpdateRequest` (omitted fields unchanged) → `Schedule`; `409 SCHEDULE_MANAGED` for a managed one. */
export const patchSchedule = (scheduleId, payload) =>
  request(`/schedules/${sid(scheduleId)}`, json("PATCH", payload));

export const deleteSchedule = (scheduleId) => request(`/schedules/${sid(scheduleId)}`, { method: "DELETE" });

export const postScheduleEnable = (scheduleId) =>
  request(`/schedules/${sid(scheduleId)}/enable`, { method: "POST" });

export const postScheduleDisable = (scheduleId) =>
  request(`/schedules/${sid(scheduleId)}/disable`, { method: "POST" });

/** "Run now": 201 with the firing as recorded, which may itself be `failed` with an `error_code`. */
export const postScheduleFire = (scheduleId) => request(`/schedules/${sid(scheduleId)}/fire`, { method: "POST" });

/** `{status?, limit?}` → `FiringListResponse`, newest first (missed slots included). */
export const getFirings = (scheduleId, filters = {}) => request(withQuery(`/schedules/${sid(scheduleId)}/firings`, filters));

/** `RetrainingSyncResponse`: what `monitoring.retraining` now implies was created, updated, removed. */
export const postRetrainingSync = () => request("/schedules/retraining/sync", { method: "POST" });

/** `{client_id?, use_case_id?, kind?, unacknowledged_only?, limit?}` → `AlertListResponse`. */
export const getAlerts = (filters = {}) => request(withQuery("/monitoring/alerts", filters));

export const postAlertAcknowledge = (alertId) =>
  request(`/monitoring/alerts/${encodeURIComponent(alertId)}/acknowledge`, { method: "POST" });

/** `{schedule_id?, client_id?, use_case_id?, limit?}` → `FiringListResponse` of missed slots. */
export const getMissedFirings = (filters = {}) => request(withQuery("/monitoring/missed-firings", filters));

/** The finished-or-not scoring runs, newest first (Phase 1's `GET /runs`, filtered to `mode=score`). */
export const getScoringRuns = (limit = 50) => request(withQuery("/runs", { mode: "score", limit }));

/** Upload a scoring run's real outcomes (`.csv`, `.parquet`, `.pq`) → 201 `OutcomeReport`. */
export function postOutcomes(runId, file, outcomeColumn = "") {
  const form = new FormData();
  form.append("file", file, file.name);
  if (outcomeColumn) form.append("outcome_column", outcomeColumn);
  return request(`/runs/${encodeURIComponent(runId)}/outcomes`, { method: "POST", body: form });
}

/** `OutcomeReport`, or null before any outcomes were uploaded. */
export const getOutcomes = (runId) =>
  orNull(request(`/runs/${encodeURIComponent(runId)}/outcomes`), ["OUTCOME_REPORT_NOT_FOUND"]);

/**
 * Was this scoring run's campaign measured on its Campaign results page (`#/campaign/<uc>/<run>`)?
 * `GET /runs/{run_id}/campaign-results` answers 200 with the report once it was, 404 before; only
 * that answer is read here, never the report. Anything else is thrown, so "unknown" stays unknown.
 */
export async function hasCampaignResults(runId) {
  try {
    await request(`/runs/${encodeURIComponent(runId)}/campaign-results`);
    return true;
  } catch (error) {
    if (error instanceof ApiError && Number(error.status) === 404) return false;
    throw error;
  }
}

/** `IncrementalityInput`, or null when the run held out no control group (or has no outcomes yet). */
export const getIncrementalityInput = (runId) =>
  orNull(request(`/runs/${encodeURIComponent(runId)}/incrementality-input`), ["INCREMENTALITY_INPUT_NOT_FOUND"]);
