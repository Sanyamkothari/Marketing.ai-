// The one call the Overview page makes (Plan M M175, DEC-1605): `GET /overview`, which holds the page's four parts
// (what needs attention, the value proven to date, the live campaigns and the first-visit checklist). In the shape
// `ui/api.js` established: a private `request`, `API_BASE` and `ApiError` reused so an error from here renders like one
// from any other screen. Nothing here invents a value.

import { API_BASE, ApiError } from "../../api.js";

async function request(path) {
  let response;
  try {
    response = await fetch(API_BASE + path);
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

/** The Overview's four parts in one answer (`GET /overview`). */
export const getOverview = () => request("/overview");
