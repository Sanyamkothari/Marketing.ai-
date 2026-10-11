/* Plan M M171 (review fix): what the pages ask the server once for the one contact list (`loadContactList`,
   `contactListNow` in `ui/api.js`). A page never waits for it; only a 409 or 404 means "this run cannot have a treat
   list", and the server's own error is kept. Run by tests/unit/decide/test_contact_list_js.py, or directly:
   `node --test tests/unit/decide/contact_list_api.test.mjs`. No npm install is needed: `fetch` is a stub. */
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = { location: { origin: "http://localhost" } };
const api = await import(new URL("../../../ui/api.js", import.meta.url));

const respond = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
let calls = [];
const serve = (handler) => {
  globalThis.fetch = async (url) => {
    calls.push(url);
    return handler(url);
  };
};

test("before the server answers the page assumes the treat list, with the two file addresses", () => {
  const now = api.contactListNow("run-a");
  assert.deepEqual(now, {
    summary: null,
    error: null,
    refused: false,
    treatHref: "http://localhost/runs/run-a/treat_list.csv",
    scoresHref: "http://localhost/runs/run-a/scores.csv",
  });
});

test("one request per run, however many parts of the page ask, and the answer is then known without asking", async () => {
  calls = [];
  serve(() => respond(200, { treat_rows: 5 }));
  const [x, y] = await Promise.all([api.loadContactList("run-b"), api.loadContactList("run-b")]);
  assert.equal(calls.length, 1);
  assert.equal(x, y);
  assert.equal(x.summary.treat_rows, 5);
  assert.equal(x.refused, false);
  assert.equal(api.contactListNow("run-b").summary.treat_rows, 5);
});

test("a 409 or a 404 refuses the list in the server's own words", async () => {
  for (const [status, run] of [[409, "run-c"], [404, "run-d"]]) {
    serve(() => respond(status, { detail: { code: "TREAT_LIST_ERROR", message: "No offers are configured." } }));
    const answer = await api.loadContactList(run);
    assert.equal(answer.refused, true, String(status));
    assert.equal(answer.error.message, "No offers are configured.");
    assert.equal(answer.summary, null);
  }
});

test("a failure that says nothing about the run (500, no network) is not a refusal", async () => {
  serve(() => respond(500, {}));
  const server = await api.loadContactList("run-e");
  assert.equal(server.refused, false);
  assert.ok(server.error);
  serve(() => {
    throw new Error("offline");
  });
  const offline = await api.loadContactList("run-f");
  assert.equal(offline.refused, false);
  assert.equal(offline.error.code, "NETWORK_ERROR");
});
