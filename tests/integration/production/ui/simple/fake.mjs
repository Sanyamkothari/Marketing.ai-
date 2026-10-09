/* The page as `ui/index.html` loads it - app.js, then every module in the order of its <script> tags -
   against a fake API answering with the bodies `tests/integration/simple/test_simple_ui.py` captured
   from the real app ($SP_FIXTURES): sign-in off, demo mode off, an empty installation. */
import fs from "node:fs";
import path from "node:path";
import { installPage } from "../harness.mjs";

const FIXTURES = process.env.SP_FIXTURES;

export function fixture(name) {
  if (!FIXTURES) throw new Error("SP_FIXTURES is not set: run these through tests/integration/simple/test_simple_ui.py");
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
}

/** The module scripts of `ui/index.html`, in page order, as the browser would load them. */
export function pageScripts() {
  const html = fs.readFileSync(new URL("../../../../../ui/index.html", import.meta.url), "utf8");
  const body = html.slice(html.indexOf("<body"));
  const scripts = [...body.matchAll(/<script type="module" src="\.\/([^"]+)"><\/script>/g)].map((m) => m[1]);
  return [...new Set(scripts)];
}

/**
 * Install the page. `world` holds what can change between tests: `runs` (the `GET /runs` body for the
 * whole history), `waiting` (the approvals body, or null for a refusal), and optionally `models` (the
 * `GET /models` body; the empty app's by default) and `roi` (run id to its `GET /pilot/roi/{run}` body).
 */
export async function installWholePage({ hash = "#/", world }) {
  const clients = { list: fixture("clients_empty") };
  const server = (request) => {
    const { method, path: p, query } = request;
    const ok = (body) => ({ status: 200, body });
    if (method === "GET" && p === "/industries") return ok(fixture("industries"));
    if (method === "GET" && p.startsWith("/use-cases/")) return ok(fixture("use_case"));
    if (method === "GET" && p === "/models") return ok(world.models || fixture("models"));
    if (method === "GET" && p === "/runs") {
      if (query.use_case) return ok(fixture("runs_empty"));
      return ok(query.mode ? { runs: world.runs.runs.filter((r) => r.mode === query.mode) } : world.runs);
    }
    if (method === "GET" && p === "/datasets") return ok(fixture("datasets_empty"));
    if (method === "GET" && p === "/pilot/data-request") return ok(fixture("data_request"));
    if (method === "GET" && p.startsWith("/pilot/roi/")) {
      const body = (world.roi || {})[decodeURIComponent(p.slice("/pilot/roi/".length))];
      return body ? ok(body) : { status: 404, body: { detail: { code: "RUN_NOT_FOUND", message: "No such run." } } };
    }
    if (method === "GET" && p === "/auth/me") return ok(fixture("me_off"));
    if (method === "GET" && p === "/pilot/demo") return ok(fixture("demo"));
    if (method === "GET" && p === "/pilot/help") return ok(fixture("help"));
    if (method === "GET" && p === "/clients") return ok(clients.list);
    if (method === "POST" && p === "/clients/default") {
      clients.list = fixture("clients");
      return ok(fixture("client_default"));
    }
    if (method === "GET" && p === "/healthz") return ok(fixture("healthz"));
    if (method === "GET" && p === "/approvals") {
      return world.waiting ? ok(world.waiting) : { status: 403, body: { detail: { code: "ROLE_REQUIRED", message: "No." } } };
    }
    // Plan J M104: the Value Proof Packs. `world.proofs` is the list; `world.packs` maps a campaign id to
    // `{json, html, after, refused}` (the real app's answers, captured by test_simple_ui.py).
    if (method === "GET" && p === "/pilot/proof") return ok(world.proofs || { proofs: [] });
    if (p.startsWith("/pilot/proof/")) {
      const [id, action] = p.slice("/pilot/proof/".length).split("/").map(decodeURIComponent);
      const pack = (world.packs || {})[id];
      if (!pack) return { status: 404, body: { detail: { code: "CAMPAIGN_NOT_FOUND", message: "No campaign." } } };
      if (method === "POST" && action === "suppressions") {
        world.approved = (world.approved || []).concat([request.body]);
        pack.approved = true;
        return { status: 201, body: fixture("proof_approval") };
      }
      if (method === "GET" && !action) {
        if (pack.refused) return { status: 409, body: pack.refused };
        if (query.format === "json") return ok(pack.approved ? pack.after : pack.json);
        return { status: 200, body: pack.html };
      }
    }
    return null;
  };
  const page = installPage(server, { hash });
  for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
  return page;
}
