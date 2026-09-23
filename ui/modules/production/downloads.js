// Downloads that carry the sign-in (DEC-793).
//
// Several screens hand out files as plain links: the scored rows (`pages.js`), a use case's template
// (`usecase.js`), campaign messages (`generative/copy.js`), and this module's own audit CSV. A link
// the browser follows by itself cannot carry an `Authorization` header, so once sign-in is on
// (`auth_mode=local`) every one of those links would answer `401` - and the scored rows and campaign
// messages are exactly the `audit_reads` downloads the audit log exists to attribute (DEC-716).
//
// Rather than edit three other workstreams' screens, one capture-phase click listener catches a
// click on any link to the API origin *while a token is held*, fetches the same URL through the
// wrapped `fetch` (so the header is added and the server audits the read against the real person),
// and saves the answer under the server's own `Content-Disposition` file name. With no token - every
// click when `auth_mode=off` - the listener does nothing and the browser follows the link exactly as
// before. A refusal (a Viewer asking for something only an Analyst may take) is shown beside the link
// in the house error box, rather than as a browser page of raw JSON.

import { ApiError } from "../../api.js";
import { errorBox } from "../../dom.js";
import { isApiUrl, readToken } from "./session.js";

const INSTALLED = Symbol.for("marketing-ai.production.downloads");

/** `attachment; filename="scores.csv"` → `scores.csv`; null when the header names none. */
export function filenameFrom(disposition) {
  if (!disposition) return null;
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      // fall through to the plain parameter
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : null;
}

function lastSegment(href) {
  try {
    const path = new URL(href, window.location.href).pathname;
    return decodeURIComponent(path.split("/").filter(Boolean).pop() || "download");
  } catch {
    return "download";
  }
}

async function failureOf(response) {
  try {
    const body = await response.json();
    const detail = body && body.detail;
    return new ApiError(
      response.status,
      (detail && detail.code) || `HTTP_${response.status}`,
      (detail && detail.message) || `The API answered ${response.status}.`,
      body,
    );
  } catch {
    return new ApiError(response.status, `HTTP_${response.status}`, `The API answered ${response.status}.`, null);
  }
}

function showBeside(link, error) {
  const next = link.nextElementSibling;
  if (next && next.classList.contains("pb-download-error")) next.remove();
  link.insertAdjacentHTML("afterend", `<div class="pb-download-error">${errorBox(error)}</div>`);
}

/** Fetch `href` with the sign-in and hand the body to the browser as a file. */
export async function saveWithToken(link) {
  let response;
  try {
    response = await window.fetch(link.href);
  } catch {
    showBeside(link, new ApiError(0, "NETWORK_ERROR", "Could not reach the API for this download.", null));
    return false;
  }
  if (!response.ok) {
    showBeside(link, await failureOf(response));
    return false;
  }
  const blob = await response.blob();
  const name =
    filenameFrom(response.headers.get("Content-Disposition")) || link.getAttribute("download") || lastSegment(link.href);
  saveBlob(blob, name);
  return true;
}

/**
 * Hand a body already fetched to the browser as a file named `name`. Also used for an answer no link
 * can fetch - the access export is a `POST` answering a zip (M48, `privacy.js`).
 */
export function saveBlob(blob, name) {
  const objectUrl = URL.createObjectURL(blob);
  const save = document.createElement("a");
  save.href = objectUrl;
  save.download = name;
  save.style.display = "none";
  document.body.appendChild(save);
  save.click();
  save.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000); // after the browser has taken the file
}

function onClick(event) {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
  if (!link) return;
  const href = link.getAttribute("href") || "";
  if (!href || href.startsWith("#")) return;
  if (!isApiUrl(link.href) || !readToken()) return;
  event.preventDefault();
  saveWithToken(link);
}

/** Listen once, in the capture phase, so the browser is stopped before it follows the link itself. */
export function installDownloads(doc = document) {
  if (doc[INSTALLED]) return;
  doc[INSTALLED] = true;
  doc.addEventListener("click", onClick, true);
}
