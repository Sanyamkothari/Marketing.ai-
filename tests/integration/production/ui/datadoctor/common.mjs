/* Shared by the Data Doctor screen's jsdom suites (Plan O M247): the recorded answers ($DDS_FIXTURES, written by
   tests/integration/test_data_doctor_ui.py) and a fake API that serves one case through its states.

   Every scenario was built on its own store, so each has its own case id: the fake serves them all as ONE case
   (`CASE`), the way the real API serves one case moving from state to state. */
import fs from "node:fs";
import path from "node:path";

export const FIXTURES = process.env.DDS_FIXTURES;
export const fx = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
export const CASE = "case_00000000000000aa";
export const GOAL = "targeted-advertisement";

/** A scenario's screen answer, its case id replaced by `CASE` everywhere it appears. */
export function screen(name) {
  const raw = fx(`screen_${name}`);
  const id = raw.case.case_id;
  return JSON.parse(JSON.stringify(raw).split(id).join(CASE));
}

/**
 * A world the fake API answers from: `world.at` is the scenario the case is in now; a change (any POST to the
 * case) is recorded in `world.posted` and moves the case to `world.next[path tail]` when one is set.
 */
export function makeWorld({ at = "question", me = "me_on", ai = null } = {}) {
  return {
    at,
    me: fx(me),
    ai: ai || fx("ai_service"),
    posted: [],
    next: {},
    patch: null, // (screen) => screen: changes the screen answer on its way out (fields the API sends later)
    cases: null, // the answer of GET /data-doctor/cases; default: the one case
    uploads: 0,
  };
}

const ok = (body, status = 200) => ({ status, body });

/** The fake API: the case routes, the screen read, `/auth/me`, `/ai-service`, and what the wizard and setup read. */
export function serverFor(world) {
  return (request) => {
    const { method, path: p } = request;
    if (method === "GET" && p === "/auth/me") return ok(world.me);
    if (method === "GET" && p === "/ai-service") return ok(world.ai);
    if (method === "GET" && p === "/industries") return ok(fx("industries"));
    if (method === "GET" && p === `/use-cases/${GOAL}`) return ok(fx("use_case"));
    if (method === "GET" && p === "/models") return ok(fx("models"));
    if (method === "GET" && p === "/runs") return ok(fx("runs"));
    if (method === "GET" && p === "/data-doctor/cases") {
      if (world.cases) return ok(world.cases);
      const c = screen(world.at).case;
      const goal = (c.goals || []).find((g) => g.active);
      return ok({ cases: [{ case_id: CASE, title: c.title, goal_id: goal ? goal.goal_id : null, state: c.state, files: [], updated_at: c.updated_at }] });
    }
    if (method === "GET" && p === `/cases/${CASE}/screen`) return ok(world.patch ? world.patch(screen(world.at)) : screen(world.at));
    if (method === "GET" && p === `/cases/${CASE}_other/screen`) {
      // a second case of the same shape, for the tests that switch or reopen a case
      const other = JSON.parse(JSON.stringify(screen(world.at)));
      other.case.case_id = `${CASE}_other`;
      return ok(other);
    }
    if (method === "GET" && /^\/uploads\/[^/]+\/profile$/.test(p)) return ok({ rows: 1200, row_count: 1200, columns: [] });
    if (method === "GET" && p === "/campaign-runs/model-offer") {
      return world.offer ? ok(world.offer) : { status: 404, body: { detail: { code: "NOT_FOUND", message: "No offer." } } };
    }
    if (method === "POST" && p === "/campaign-runs") {
      world.posted.push({ path: p, body: request.body });
      if (world.refuseRun) return world.refuseRun;
      return ok({ record: { campaign_run_id: "cr_dds", state: "running" }, pct: 0, eta_seconds: null, summary: "" }, 202);
    }
    if (method === "POST" && p === "/uploads") {
      world.uploads += 1;
      world.posted.push({ path: p, body: null, form: request.form || null });
      return ok(world.uploadAnswer ? world.uploadAnswer(request) : { upload_id: "u_dds_upload", profile: null }, 201);
    }
    if (method === "POST" && (p === "/cases" || p.startsWith(`/cases/${CASE}`))) {
      world.posted.push({ path: p, body: request.body });
      if (world.refuse && world.refuse.path === p) {
        const answer = world.refuse.answer;
        world.refuse = null;
        return answer;
      }
      const tail = p === "/cases" ? "create" : p.slice(`/cases/${CASE}/`.length).split("/")[0];
      if (world.next[tail]) world.at = world.next[tail];
      return ok(screen(world.at).case, p === "/cases" ? 201 : 200);
    }
    return { status: 404, body: { detail: { code: "NOT_FOUND", message: "Not in this fake." } } };
  };
}
