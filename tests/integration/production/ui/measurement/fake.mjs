/* The page as `ui/index.html` loads it - app.js, then every module in the order of its <script> tags -
   against a fake API answering with the bodies `tests/integration/measurement/test_campaign_ui.py`
   captured from the real app ($CP_FIXTURES): sign-in off, one Phase 1 scoring run, two campaigns. */
import fs from "node:fs";
import path from "node:path";
import { installPage } from "../harness.mjs";

const FIXTURES = process.env.CP_FIXTURES;

export function fixture(name) {
  if (!FIXTURES) throw new Error("CP_FIXTURES is not set: run these through tests/integration/measurement/test_campaign_ui.py");
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
}

/** The module scripts of `ui/index.html`, in page order, as the browser would load them. */
function pageScripts() {
  const html = fs.readFileSync(new URL("../../../../../ui/index.html", import.meta.url), "utf8");
  const body = html.slice(html.indexOf("<body"));
  const scripts = [...body.matchAll(/<script type="module" src="\.\/([^"]+)"><\/script>/g)].map((m) => m[1]);
  return [...new Set(scripts)];
}

/**
 * Install the page. `world` holds what can change between tests: `campaigns` (the `GET /campaigns`
 * body), `views` (campaign id to its `GET /campaigns/{id}` body), `plans` (to its `GET .../plan`
 * body), and `onPlan(id, body)` / `onMeasure(id)` answering the two writes.
 */
export async function installWholePage({ hash = "#/", world }) {
  const server = (request) => {
    const { method, path: p } = request;
    const ok = (body) => ({ status: 200, body });
    if (method === "GET" && p === "/industries") return ok(fixture("industries"));
    if (method === "GET" && p.startsWith("/use-cases/")) return ok(fixture("use_case"));
    if (method === "GET" && p === "/models") return ok(fixture("models"));
    if (method === "GET" && p === "/runs") return ok(fixture("runs"));
    if (method === "GET" && p === "/datasets") return ok(fixture("datasets"));
    if (method === "GET" && p === "/auth/me") return ok(fixture("me_off"));
    if (method === "GET" && p === "/pilot/demo") return ok(fixture("demo"));
    if (method === "GET" && p === "/pilot/help") return ok(fixture("help"));
    if (method === "GET" && p === "/clients") return ok(fixture("clients"));
    if (method === "GET" && p === "/healthz") return ok(fixture("healthz"));
    if (method === "GET" && p === "/campaigns") return ok(world.campaigns);
    const match = p.match(/^\/campaigns\/([^/]+)(\/plan|\/measure)?$/);
    if (match) {
      const id = decodeURIComponent(match[1]);
      if (method === "GET" && !match[2] && world.views[id]) return ok(world.views[id]);
      if (method === "GET" && match[2] === "/plan" && world.plans[id]) return ok(world.plans[id]);
      if (method === "POST" && match[2] === "/plan" && world.onPlan) return world.onPlan(id, request.body);
      if (method === "POST" && match[2] === "/measure" && world.onMeasure) return world.onMeasure(id);
      return { status: 404, body: { detail: { code: "CAMPAIGN_NOT_FOUND", message: "No such campaign.", path: null } } };
    }
    return null;
  };
  const page = installPage(server, { hash });
  for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
  return page;
}
