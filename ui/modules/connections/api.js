// The Connections calls (Plan H M80, `api/routes/connections.py`). Errors are the shared `ApiError`
// carrying the server's own envelope, whose message already ends with the plain fix.
//
// A secret goes to the server in the body of a create or an update and nowhere else: never in a URL,
// never kept by this module after the call, and never read back - the API sends only the names of the
// secrets a connection has saved.

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

const one = (id, tail = "") => `/connections/${enc(id)}${tail}`;

/** What was picked, as the API takes it: a store's `path`, or a database's `schema_name` and `table`. */
function pickBody(pick) {
  const body = {};
  for (const key of ["path", "schema_name", "table"]) {
    if (pick[key] !== null && pick[key] !== undefined && pick[key] !== "") body[key] = pick[key];
  }
  return body;
}

/** `{ kinds: [KindInfo] }`: every card, with its form's fields. */
export const getKinds = () => call("/connections/kinds");

/** `{ connections: [ConnectionView] }`, oldest first. */
export const getConnections = () => call("/connections");

export const getConnection = (id) => call(one(id));

/** Save a new connection: `{ kind, name, config, secrets }`. */
export const createConnection = (body) => call("/connections", { method: "POST", body });

/** Change one: `{ name, config, secrets, clear_secrets }`; a blank secret keeps the saved one. */
export const updateConnection = (id, body) => call(one(id), { method: "PUT", body });

export const deleteConnection = (id) => call(one(id), { method: "DELETE" });

/** `{ connection, report: { ok, steps: [{ name, label, status, message, fix }], tested_at } }`. */
export const testConnection = (id) => call(one(id, "/test"), { method: "POST" });

/** `{ path, parent, items: [{ name, kind, path, schema_name, table, size_bytes, importable }], truncated }`. */
export const browseConnection = (id, path = "") => call(`${one(id, "/browse")}?path=${enc(path)}`);

/** `{ columns, rows, note }`: at most 20 rows, personal data masked by the server. */
export const previewFrom = (id, pick) =>
  call(one(id, "/preview"), { method: "POST", body: pickBody(pick) });

/** Import a table or file as an ordinary upload: the same `{ upload_id, profile }` as `POST /uploads`. */
export const importFrom = (id, pick, useCaseId, mode) =>
  call(one(id, "/import"), {
    method: "POST",
    body: { use_case: useCaseId, mode, ...pickBody(pick) },
  });

/** The AI service's own state (`GET /connection/aws`, Phase 3a), for its card. */
export const getAiService = () => call("/connection/aws");
