/* The whole page (app.js and every module, in the order of ui/index.html) against a replayed API: the answers
   that `tests/fixtures/capture_ui_demo.py` recorded from the app running on the validated Hillstrom demo.
   A request with no recorded answer gets the 404 the real API gives for a thing that does not exist. */
import fs from "node:fs";
import { installPage } from "../harness.mjs";
import { pageScripts } from "../simple/fake.mjs";

const API = new URL("../../../../fixtures/ui_demo/api.json", import.meta.url);

export const recorded = JSON.parse(fs.readFileSync(API, "utf8"));

const keyOf = (request) => {
  const query = new URLSearchParams(request.query);
  query.sort();
  const q = query.toString();
  return `${request.method} ${request.path}${q ? `?${q}` : ""}`;
};

export function installDemoPage({ hash = "#/" } = {}) {
  const missing = new Set();
  const state = { inFlight: 0 };
  const server = async (request) => {
    state.inFlight += 1;
    try {
      const hit = recorded[keyOf(request)];
      if (hit) {
        return { status: hit.status, body: hit.body, headers: { "Content-Type": hit.content_type || "application/json" } };
      }
      if (request.method === "GET") missing.add(keyOf(request));
      return { status: 404, body: { detail: { code: "NOT_RECORDED", message: "Not found." } } };
    } finally {
      state.inFlight -= 1;
    }
  };
  const page = installPage(server, { hash });
  return { ...page, state, missing };
}

export async function loadModules() {
  for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
}
