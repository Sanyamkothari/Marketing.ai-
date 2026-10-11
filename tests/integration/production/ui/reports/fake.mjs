/* The whole page (app.js and every module, in the order of ui/index.html) against the replayed demo API
   (tests/fixtures/ui_demo/api.json, recorded from the app running on the validated Hillstrom demo), with
   per-test overlays: an answer a test replaces, or a write (POST) it answers. Plan M, M178. */
import { recorded } from "../glossary/replay.mjs";
import { installPage, settle, until } from "../harness.mjs";
import { pageScripts } from "../simple/fake.mjs";

export { recorded };

const keyOf = (request) => {
  const query = new URLSearchParams(request.query);
  query.sort();
  const q = query.toString();
  return `${request.method} ${request.path}${q ? `?${q}` : ""}`;
};

/** `overlay` maps "METHOD /path?query" to `{status, body}` or a function `(request) => {status, body}`. */
export async function installReports({ hash = "#/reports", overlay = {} } = {}) {
  const calls = [];
  const server = async (request) => {
    calls.push(request);
    const key = keyOf(request);
    const mine = overlay[key];
    if (mine) return typeof mine === "function" ? mine(request) : mine;
    const hit = recorded[key];
    if (hit) return { status: hit.status, body: hit.body, headers: { "Content-Type": hit.content_type || "application/json" } };
    return { status: 404, body: { detail: { code: "NOT_RECORDED", message: "Not found." } } };
  };
  const page = installPage(server, { hash });
  globalThis.FormData = page.w.FormData; // a form is read in the page's own DOM, so its FormData is the page's
  for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
  return { ...page, calls, overlay };
}

/** Go to a route and wait until `ready()` holds, then let the page settle. */
export async function open(w, hash, ready, ms = 4000) {
  w.location.hash = "#/__between";
  await settle(3);
  w.location.hash = hash;
  await until(ready, ms, hash);
  await settle(4);
}

export const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

/** A file input given a file, as a person's choice does, and the `change` the page listens for. */
export function chooseFile(w, input, name, content = "id\n1\n") {
  Object.defineProperty(input, "files", { configurable: true, value: [new w.File([content], name, { type: "text/csv" })] });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
}

/** The answer `POST /uploads` gives for a file with these columns (the shape of the real answer's `profile`). */
export const uploaded = (id, fileName, columns, rows = 100) => ({
  status: 201,
  body: { upload_id: id, profile: { file_name: fileName, row_count: rows, columns: columns.map((name) => ({ name })) } },
});
