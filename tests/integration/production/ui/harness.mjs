/* The Phase 4b UI modules (ui/modules/production/) in jsdom, against a fake API that answers with
   REAL response bodies: `test_production_ui_js.py` builds the app with sign-in on, signs in a Viewer,
   an Analyst, an Approver and an Admin, and writes what `GET /auth/me`, `POST /auth/login`,
   `GET /users` and `GET /audit/events` really answered into $PB_FIXTURES. So a test here cannot pass
   against a permission list or a message this file made up.

   The UI is plain ES modules that read the browser's globals (`window`, `document`, `fetch`) when
   they run, so one jsdom window is installed as the globals *before* any module is imported, and
   each test file is its own process (`node --test`), which gives each file a fresh module graph. */
import fs from "node:fs";
import path from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

export const ROOT = new URL("../../../../", import.meta.url);
export const UI = new URL("ui/", ROOT);

const FIXTURES = process.env.PB_FIXTURES;

/** One fixture the Python side wrote from a real API response. */
export function fixture(name) {
  if (!FIXTURES) throw new Error("PB_FIXTURES is not set: run these through test_production_ui_js.py");
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
}

/**
 * Install a jsdom window as the page's globals. `server(request)` answers every API call:
 * `request` is `{method, path, query, auth, body}`; it returns `{status, body, headers}`.
 * Every call is also recorded in `calls`, with the Authorization header it carried.
 */
export function installPage(server, { hash = "#/" } = {}) {
  const vc = new VirtualConsole();
  vc.on("jsdomError", () => {});
  const dom = new JSDOM(
    `<!DOCTYPE html><html><head></head><body><div id="app" aria-live="polite"></div></body></html>`,
    { url: `http://localhost/ui/${hash}`, pretendToBeVisual: true, virtualConsole: vc },
  );
  const w = dom.window;
  w.scrollTo = () => {};
  const calls = [];
  w.fetch = async (input, init = {}) => {
    const target = new URL(typeof input === "string" ? input : input.url, w.location.href);
    const headers = new Headers(init.headers || undefined);
    const request = {
      method: (init.method || "GET").toUpperCase(),
      path: target.pathname,
      query: Object.fromEntries(target.searchParams),
      auth: headers.get("Authorization"),
      body: typeof init.body === "string" ? JSON.parse(init.body) : null,
    };
    calls.push(request);
    const answer = (await server(request)) || { status: 404, body: { detail: { code: "NOT_FOUND", message: "No route." } } };
    const isText = typeof answer.body === "string";
    return new Response(answer.status === 204 ? null : isText ? answer.body : JSON.stringify(answer.body), {
      status: answer.status || 200,
      headers: { "Content-Type": isText ? "text/csv" : "application/json", ...(answer.headers || {}) },
    });
  };
  Object.assign(globalThis, {
    window: w,
    document: w.document,
    MutationObserver: w.MutationObserver,
    HashChangeEvent: w.HashChangeEvent,
    Event: w.Event,
  });
  // A module's bare `fetch` is the page's `window.fetch`, whatever wrapped it since.
  globalThis.fetch = (...args) => w.fetch(...args);
  return { dom, w, calls };
}

/** Let promises, hashchange events and MutationObserver callbacks run. */
export const settle = async (rounds = 6) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
};

/** Wait until `predicate()` holds, or fail after `ms`. */
export async function until(predicate, ms = 2000, what = "condition") {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > ms) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

export const $ = (sel) => document.querySelector(sel);
export const $$ = (sel) => [...document.querySelectorAll(sel)];

/** An API that answers like the real one with sign-in on: 401 AUTH_REQUIRED without a known token. */
export function signedInServer({ tokens, routes = {} }) {
  return async (request) => {
    if (request.path === "/auth/login" && request.method === "POST") {
      return routes.login ? routes.login(request) : { status: 401, body: fixture("login_refused") };
    }
    const token = (request.auth || "").replace(/^Bearer /, "");
    const me = tokens[token];
    if (!me) {
      return {
        status: 401,
        body: { detail: { code: "AUTH_REQUIRED", message: "Sign in to do this.", path: null } },
        headers: { "WWW-Authenticate": "Bearer" },
      };
    }
    if (request.path === "/auth/me") return { status: 200, body: me };
    const key = `${request.method} ${request.path}`;
    if (routes[key]) return routes[key](request, me);
    return { status: 404, body: { detail: { code: "NOT_FOUND", message: "No route in this fake.", path: null } } };
  };
}
