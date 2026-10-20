// Node tests for the live wiring of the marketer app (ui/v2): the batch approval flow, the API client's handling of
// the real /v2 answers (422 lists, a route that is not there yet), the mock customer drill-down, and the one pause
// control. Run by tests/v2/app/test_live_wiring.py, or directly: `node --test tests/v2/app/live_wiring.test.mjs`.
//
// The screens only use a small part of the DOM, so a tiny fake document stands in for a browser (no jsdom, no npm).

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// ---- a tiny fake DOM, enough for components.js (Node, className, style) and live.js (attributes) ------------------
class FakeNode {
  constructor() {
    this.childNodes = [];
    this.parentNode = null;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  append(...kids) {
    for (const k of kids) this.appendChild(k instanceof FakeNode ? k : new FakeText(String(k)));
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i >= 0) this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceChildren(...kids) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    this.append(...kids);
  }
  get textContent() {
    return this.childNodes.map((c) => c.textContent).join("");
  }
  set textContent(v) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    if (String(v) !== "") this.appendChild(new FakeText(String(v)));
  }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === fakeDocument.body;
  }
}

class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.data = text;
  }
  get textContent() {
    return this.data;
  }
  set textContent(v) {
    this.data = String(v);
  }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tagName = tag.toLowerCase();
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
  }
  get className() {
    return this.attributes.class || "";
  }
  set className(v) {
    this.attributes.class = String(v);
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "disabled") this.disabled = true;
  }
  getAttribute(k) {
    return k in this.attributes ? this.attributes[k] : null;
  }
  removeAttribute(k) {
    delete this.attributes[k];
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  focus() {}
  querySelector() {
    return null;
  }
  async fire(type) {
    const ev = { type, target: this, preventDefault() {} };
    await Promise.all((this.listeners[type] || []).map((f) => f(ev)));
    await settle();
  }
}

const fakeDocument = {
  head: new FakeElement("head"),
  body: new FakeElement("body"),
  listeners: {},
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
  getElementById(id) {
    return [...walk(this.head), ...walk(this.body)].find((n) => n.attributes.id === id) || null;
  },
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  },
};
globalThis.document = fakeDocument;
globalThis.Node = FakeNode;

function walk(node) {
  const out = [];
  for (const c of node.childNodes) {
    if (c instanceof FakeElement) out.push(c, ...walk(c));
  }
  return out;
}
const byTag = (root, tag) => walk(root).filter((n) => n.tagName === tag);
const button = (root, text) => byTag(root, "button").find((b) => b.textContent.trim() === text);
const settle = () => new Promise((r) => setTimeout(r, 0));
const noJunk = (text) => !/\b(undefined|null|false|NaN)\b/.test(text);

// ---- the code under test, the mock files, and fetch stand-ins ------------------------------------------------------
const UI = new URL("../../../ui/v2/", import.meta.url);
const { createApi, errorMessage, routeMissing, ApiError } = await import(new URL("api.js", UI).href);
const live = await import(new URL("screens/live.js", UI).href);
const planScreen = await import(new URL("screens/plan.js", UI).href);
const proofScreen = await import(new URL("screens/proof.js", UI).href);
const mock = async (name) => JSON.parse(await readFile(fileURLToPath(new URL(`mock/${name}`, UI)), "utf8"));

/** fetch for the mock folder (file: URLs) and, optionally, a fake /v2 server. */
function fakeFetch(server) {
  return async (url, init = {}) => {
    const u = String(url);
    if (u.startsWith("file:")) {
      try {
        const text = await readFile(fileURLToPath(u), "utf8");
        return { ok: true, status: 200, text: async () => text, headers: { get: () => "application/json" } };
      } catch {
        return { ok: false, status: 404, text: async () => "", headers: { get: () => "text/plain" } };
      }
    }
    if (!server) throw new TypeError("fetch failed");
    return server(u, init);
  };
}
const json = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body), headers: { get: () => "application/json" } });
const mockApi = () => createApi({ mock: true, fetch: fakeFetch(), now: () => new Date("2026-10-20T14:00:00Z") });

function ctxFor(api, extra = {}) {
  const toasts = [];
  return { toasts, ctx: { api, goalId: "goal_winback", navigate: () => {}, toast: (m, k) => toasts.push([m, k]), ...extra } };
}

function mount() {
  const root = new FakeElement("main");
  fakeDocument.body.replaceChildren(root);
  return root;
}

// ---- api.js --------------------------------------------------------------------------------------------------------
test("a 422 list names the field to fix", () => {
  const body = {
    detail: [
      { type: "string_too_short", loc: ["body", "text"], msg: "String should have at least 1 character" },
      { type: "greater_than_equal", loc: ["body", "budget"], msg: "Input should be greater than or equal to 0" },
    ],
  };
  assert.equal(errorMessage(body, 422), "Text: String should have at least 1 character; Budget: Input should be greater than or equal to 0");
  assert.equal(errorMessage({ detail: "no goal 'x'" }, 404), "no goal 'x'");
  assert.equal(errorMessage(null, 500), "The Autonomy API answered 500.");
});

test("only FastAPI's own 404 means the route is not there", () => {
  assert.equal(routeMissing(new ApiError(404, "HTTP_404", "Not Found", { detail: "Not Found" })), true);
  assert.equal(routeMissing(new ApiError(405, "HTTP_405", "Method Not Allowed", { detail: "Method Not Allowed" })), true);
  assert.equal(routeMissing(new ApiError(404, "HTTP_404", "no batch 'b'", { detail: "no batch 'b'" })), false);
  assert.equal(routeMissing(new ApiError(500, "HTTP_500", "boom", null)), false);
});

test("live: batches are listed, approved and rejected on the contract's paths", async () => {
  const seen = [];
  const batch = (await mock("batches.json"))[0];
  const server = (url, init) => {
    seen.push([init.method || "GET", url, init.body]);
    if (url.endsWith("/v2/health")) return json(200, { as_of: "2026-10-20T00:00:00Z", alerts: [] });
    if (url === "/v2/batches?goal_id=goal_winback") return json(200, [batch]);
    if (url === `/v2/batches/${batch.batch_id}/approve`) return json(200, { ...batch, status: "approved", decided_by: "founder" });
    if (url === `/v2/batches/${batch.batch_id}/reject`) return json(409, { detail: "This batch was already approved." });
    return json(404, { detail: "Not Found" });
  };
  const api = createApi({ base: "", fetch: fakeFetch(server), mock: false });
  const changed = [];
  api.onBatchChange((b) => changed.push(b.status));
  assert.deepEqual(await api.listBatches("goal_winback"), [batch]);
  const approved = await api.approveBatch(batch.batch_id);
  assert.equal(approved.status, "approved");
  assert.deepEqual(seen.at(-1), ["POST", `/v2/batches/${batch.batch_id}/approve`, "{}"]);
  assert.deepEqual(changed, ["approved"], "the top bar hears about the decision");
  await assert.rejects(api.rejectBatch(batch.batch_id), (e) => e.status === 409 && /already approved/.test(e.message));
  assert.equal(api.mode, "live");
});

test("live: an API without the batch route answers null, not an error and not sample data", async () => {
  const server = (url) => (url.endsWith("/v2/health") ? json(200, { alerts: [] }) : json(404, { detail: "Not Found" }));
  const api = createApi({ base: "", fetch: fakeFetch(server), mock: false });
  assert.equal(await api.listBatches("goal_winback"), null);
  assert.equal(api.mode, "live");
});

test("mock: the sample goal's batches, decided once each, kept for the page load", async () => {
  const api = mockApi();
  const rows = await api.listBatches("goal_winback");
  assert.equal(rows.length, (await mock("batches.json")).length);
  assert.deepEqual(await api.listBatches("goal_other"), []);
  const pending = rows.find((b) => b.status === "pending");
  const decided = await api.approveBatch(pending.batch_id);
  assert.equal(decided.status, "approved", "approved, never pretended to be sent");
  assert.equal(decided.decided_by, "you");
  assert.equal(decided.decided_at, "2026-10-20T14:00:00.000Z");
  await assert.rejects(api.rejectBatch(pending.batch_id), (e) => e.status === 409);
  await assert.rejects(api.approveBatch("batch_nope"), (e) => e.status === 404);
  assert.equal((await api.listBatches("goal_winback")).find((b) => b.batch_id === pending.batch_id).status, "approved");
});

test("mock: any customer in the sample decisions can be opened", async () => {
  const api = mockApi();
  const sample = await mock("customer.json");
  assert.deepEqual(await api.getCustomer(sample.state.customer_id), sample, "the full sample customer is unchanged");
  const decisions = await mock("decisions.json");
  for (const id of new Set(decisions.map((d) => d.customer_id))) {
    const bundle = await api.getCustomer(id);
    assert.equal(bundle.state.customer_id, id);
    if (id === sample.state.customer_id) continue;
    const mine = decisions.filter((d) => d.customer_id === id);
    assert.equal(bundle.decisions.length, mine.length);
    assert.equal(bundle.state.group, mine[0].group);
    assert.deepEqual(bundle.events, [], "no event is made up");
    assert.equal(bundle.state.purchases, undefined, "what a decision cannot tell stays missing");
    for (const d of mine.filter((x) => x.kind === "act")) assert.ok(bundle.state.channels[d.action.channel].consent);
  }
  await assert.rejects(api.getCustomer("cust_nobody"), (e) => e.status === 404);
});

// ---- the Live screen -----------------------------------------------------------------------------------------------
async function fakeScreenApi(overrides = {}) {
  const detail = await mock("goal_detail.json");
  const batches = await mock("batches.json");
  const calls = [];
  const subscribers = [];
  let goal = { ...detail.goal, autonomy: "approve" };
  const api = {
    calls,
    subscribers,
    getGoal: async () => ({ ...detail, goal }),
    listGoals: async () => [goal],
    listDecisions: async () => mock("decisions.json"),
    getBriefing: async () => mock("briefing.json"),
    getHealth: async () => mock("health.json"),
    listAlerts: async () => mock("alerts.json"),
    getProof: async () => mock("proof.json"),
    listBatches: async (id) => {
      calls.push(["listBatches", id]);
      return batches;
    },
    approveBatch: async (id) => {
      calls.push(["approveBatch", id]);
      return { ...batches.find((b) => b.batch_id === id), status: "approved", decided_by: "you", decided_at: "2026-10-20T14:00:00Z" };
    },
    rejectBatch: async (id) => {
      calls.push(["rejectBatch", id]);
      return { ...batches.find((b) => b.batch_id === id), status: "rejected", decided_by: "you", decided_at: "2026-10-20T14:00:00Z" };
    },
    pauseGoal: async () => {
      throw new Error("Live must not pause by itself inside the app");
    },
    onGoalChange: (fn) => (subscribers.push(fn), () => {}),
    ...overrides,
  };
  return { api, goal, batches };
}

test("Live lists pending batches with their numbers, and approving one takes it off the list", async () => {
  const root = mount();
  const { api, batches } = await fakeScreenApi();
  const { ctx, toasts } = ctxFor(api, { pauseInTopBar: true });
  await live.render(root, ctx);
  const pending = batches.filter((b) => b.status === "pending");
  const text = root.textContent;
  assert.ok(text.includes(`Waiting for your approval (${pending.length})`));
  for (const b of pending) {
    assert.ok(text.includes(b.summary));
    assert.ok(text.includes(live.batchFacts(b, "USD")));
  }
  assert.ok(text.includes("3 contacts · expected net profit $6.34 · expected spend $0.10"));
  assert.ok(text.includes("Decided (1)"));
  assert.ok(noJunk(text), "no stray false/null/undefined text");

  await button(root, "Approve").fire("click");
  assert.deepEqual(api.calls.filter((c) => c[0] === "approveBatch"), [["approveBatch", pending[0].batch_id]]);
  assert.ok(root.textContent.includes(`Waiting for your approval (${pending.length - 1})`));
  assert.ok(root.textContent.includes(`Decided (2)`));
  assert.equal(toasts.at(-1)[1], "good");

  await button(root, "Reject").fire("click");
  assert.deepEqual(api.calls.filter((c) => c[0] === "rejectBatch"), [["rejectBatch", pending[1].batch_id]]);
  assert.ok(root.textContent.includes("Nothing is waiting for you"));
});

test("Live says when the API does not list batches yet, and shows none below 'approve'", async () => {
  let root = mount();
  const missing = await fakeScreenApi({ listBatches: async () => null });
  await live.render(root, ctxFor(missing.api, { pauseInTopBar: true }).ctx);
  assert.ok(root.textContent.includes("This server does not list them yet"));
  assert.ok(!root.textContent.includes("Couldn't load"));

  root = mount();
  const detail = await mock("goal_detail.json");
  const auto = await fakeScreenApi({ getGoal: async () => ({ ...detail, goal: { ...detail.goal, autonomy: "autopilot" } }) });
  await live.render(root, ctxFor(auto.api, { pauseInTopBar: true }).ctx);
  assert.ok(!root.textContent.includes("Waiting for your approval"));
  assert.deepEqual(auto.api.calls, [], "no batch request for a goal on autopilot");
  // The autonomy moves to "approve" elsewhere (the Plan screen): the batches appear.
  auto.api.subscribers[0]({ ...detail.goal, autonomy: "approve" });
  await settle();
  assert.ok(root.textContent.includes("Waiting for your approval (2)"));
});

test("inside the app, Live has no pause button of its own and follows the top bar", async () => {
  const root = mount();
  const { api, goal } = await fakeScreenApi();
  await live.render(root, ctxFor(api, { pauseInTopBar: true }).ctx);
  assert.equal(byTag(root, "button").filter((b) => /Pause|Resume/.test(b.textContent)).length, 0);
  assert.ok(root.textContent.includes("Pause, at the top, stops every send"));
  api.subscribers[0]({ ...goal, status: "paused" });
  assert.ok(root.textContent.includes("until you press Resume at the top"));
  assert.ok(root.textContent.includes("Paused"));
});

test("a goal that does not exist is 'not found' on Live, Plan and Proof, not an error", async () => {
  const gone = Object.assign(new Error("no goal 'nope'"), { status: 404 });
  const { api } = await fakeScreenApi({ getGoal: async () => Promise.reject(gone), getProof: async () => null });
  let root = mount();
  await live.render(root, ctxFor(api, { goalId: "nope" }).ctx);
  assert.ok(root.textContent.includes("There is no goal nope."));
  root = mount();
  await proofScreen.render(root, ctxFor(api, { goalId: "nope" }).ctx);
  assert.ok(root.textContent.includes("There is no goal nope."));
  assert.ok(!root.textContent.includes("Not enough data yet"));
  root = mount();
  await planScreen.render(root, ctxFor(api, { goalId: "nope" }).ctx);
  assert.ok(root.textContent.includes("This goal was not found"));
});

test("an estimate keeps one precision across its point and range", () => {
  const usd = (v) => live.money(v, "USD");
  const est = { point: 1417.68, low: 766.92, high: 2068.44 };
  assert.equal(live.range(est, proofScreen.estimateFormat(est, usd)), "$1,418 (range $767 to $2,068)");
  const small = { point: 0.38, low: 0.05, high: 0.71 };
  assert.equal(live.range(small, proofScreen.estimateFormat(small, usd)), "$0.38 (range $0.05 to $0.71)");
});
