/* The whole page (app.js and every module, in the order of ui/index.html) for the shell's tests (Plan M M174):
   the API answers recorded from the validated Hillstrom demo (M170's replay file), except two that a test sets -
   `GET /auth/me` (a real answer the Python side wrote into $SH_FIXTURES, which carries `ui_layout`) and
   `GET /approvals` (the list the bell counts). */
import fs from "node:fs";
import path from "node:path";
import { installPage, settle } from "../harness.mjs";
import { recorded } from "../glossary/replay.mjs";
import { pageScripts } from "../simple/fake.mjs";

const FIXTURES = process.env.SH_FIXTURES;

/** One `GET /auth/me` body the Python side captured from the real app. */
export function me(name) {
  if (!FIXTURES) throw new Error("SH_FIXTURES is not set: run these through tests/integration/test_shell_ui.py");
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
}

const keyOf = (request) => {
  const query = new URLSearchParams(request.query);
  query.sort();
  const q = query.toString();
  return `${request.method} ${request.path}${q ? `?${q}` : ""}`;
};

const SIGNED_OUT = { status: 401, body: { detail: { code: "AUTH_REQUIRED", message: "Sign in to do this.", path: null } } };

/** `world.me` is the `/auth/me` body (null answers 401); `world.approvals` the `/approvals` body (null: recorded). */
export function installShellPage({ hash = "#/", world }) {
  const state = { inFlight: 0 };
  const server = async (request) => {
    state.inFlight += 1;
    try {
      if (request.path === "/auth/me") return world.me ? { status: 200, body: world.me } : SIGNED_OUT;
      if (request.path === "/approvals" && world.approvals) return { status: 200, body: world.approvals };
      const hit = recorded[keyOf(request)];
      if (hit) return { status: hit.status, body: hit.body, headers: { "Content-Type": hit.content_type || "application/json" } };
      return { status: 404, body: { detail: { code: "NOT_RECORDED", message: "Not found." } } };
    } finally {
      state.inFlight -= 1;
    }
  };
  return { ...installPage(server, { hash }), state };
}

export async function loadModules() {
  for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
}

/** Wait until the page has stopped changing: nothing in flight and the same markup for a few ticks. */
export async function untilStable(state, ms = 10000) {
  const started = Date.now();
  let last = null;
  let same = 0;
  while (Date.now() - started < ms) {
    await new Promise((resolve) => setTimeout(resolve, 20));
    const now = `${window.location.hash}:${document.body.innerHTML.length}`;
    if (state.inFlight === 0 && now === last) same += 1;
    else same = 0;
    last = now;
    if (same >= 5) return;
  }
}

/** Go to `hash` and wait for the page (and any redirect) to settle. */
export async function visit(state, hash) {
  window.location.hash = hash;
  await settle(2);
  await untilStable(state);
}
