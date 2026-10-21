// Node checks for the shell's promise parts and the Getting started screen (#/start): the autonomy badge, the
// "Paused itself" badge, the inbox count (same set as Live's Inbox), the one big Pause button that pauses every
// running goal, the Shadow note, and each onboarding step's state read from data. Same tiny DOM shim as
// tests/v2/ui/shell_goal_plan.test.mjs (no jsdom). Run by test_shell_onboarding.py, or directly with node.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

// ---- a tiny DOM ------------------------------------------------------------------------------------------
class Node {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
  }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this);
    this.parentNode = null;
  }
}

class Text extends Node {
  constructor(text) {
    super();
    this.data = String(text);
  }
  get textContent() {
    return this.data;
  }
}

const BOOL_PROPS = ["disabled", "hidden", "selected", "required", "checked"];

class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.value = "";
    const self = this;
    this.classList = {
      add: (...c) => self.setAttribute("class", [...new Set([...self.classes(), ...c])].join(" ")),
      remove: (...c) => self.setAttribute("class", self.classes().filter((x) => !c.includes(x)).join(" ")),
      toggle: (c, on) => ((on ?? !self.classes().includes(c)) ? self.classList.add(c) : self.classList.remove(c)),
      contains: (c) => self.classes().includes(c),
    };
    for (const p of BOOL_PROPS) {
      Object.defineProperty(this, p, {
        get: () => this.attrs.has(p),
        set: (v) => (v ? this.attrs.set(p, "") : this.attrs.delete(p)),
      });
    }
  }
  classes() {
    return (this.attrs.get("class") || "").split(/\s+/).filter(Boolean);
  }
  get className() {
    return this.attrs.get("class") || "";
  }
  set className(v) {
    this.attrs.set("class", String(v));
  }
  get id() {
    return this.attrs.get("id") || "";
  }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
    if (k === "value") this.value = String(v);
  }
  getAttribute(k) {
    return this.attrs.has(k) ? this.attrs.get(k) : null;
  }
  removeAttribute(k) {
    this.attrs.delete(k);
  }
  hasAttribute(k) {
    return this.attrs.has(k);
  }
  append(...nodes) {
    for (const n of nodes) {
      const node = n instanceof Node ? n : new Text(n);
      node.remove();
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const n of this.childNodes) n.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(v) {
    this.replaceChildren(new Text(v));
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  dispatch(type, extra = {}) {
    const event = { type, target: this, preventDefault() {}, ...extra };
    return Promise.all((this.listeners[type] || []).map((fn) => fn(event)));
  }
  click() {
    return this.dispatch("click");
  }
  focus() {}
  matches(sel) {
    const m = sel.match(/^([a-z0-9]*)((?:\.[\w-]+)*)(?:#([\w-]+))?(:checked)?$/i);
    if (!m) throw new Error(`shim cannot match ${sel}`);
    const [, tag, cls, id, checked] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (cls && !cls.split(".").filter(Boolean).every((c) => this.classes().includes(c))) return false;
    if (id && this.id !== id) return false;
    if (checked && !this.checked) return false;
    return true;
  }
  *walk() {
    for (const n of this.childNodes) {
      if (n instanceof Element) {
        yield n;
        yield* n.walk();
      }
    }
  }
  querySelectorAll(sel) {
    return [...this.walk()].filter((n) => n.matches(sel));
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
}

globalThis.Node = Node;
globalThis.document = {
  body: new Element("body"),
  createElement: (t) => new Element(t),
  createTextNode: (t) => new Text(t),
  getElementById(id) {
    return [...this.body.walk()].find((n) => n.id === id) || null;
  },
};

// ---- the code under test -----------------------------------------------------------------------------------
const UI = new URL("../../../ui/v2/", import.meta.url);
const { createApi } = await import(new URL("api.js", UI).href);
const C = await import(new URL("components.js", UI).href);
const ob = await import(new URL("screens/onboarding.js", UI).href);
const { autonomyBadge, inboxAlerts, inboxItems, START, SCREENS } = await import(new URL("app.js", UI).href);

function fakeFetch() {
  return async (url) => {
    const u = String(url);
    if (u.startsWith("file:")) {
      try {
        const text = await readFile(fileURLToPath(u), "utf8");
        return { ok: true, status: 200, text: async () => text, headers: { get: () => "application/json" } };
      } catch {
        return { ok: false, status: 404, text: async () => "", headers: { get: () => "text/plain" } };
      }
    }
    throw new TypeError("fetch failed");
  };
}
const mockApi = () => createApi({ mock: true, fetch: fakeFetch(), now: () => new Date("2026-10-20T08:00:00Z") });

function ctxFor(api, extra = {}) {
  const toasts = [];
  return { toasts, ctx: { api, goalId: null, goal: null, navigate() {}, toast: (m, t) => toasts.push([m, t]), refreshGoals: async () => [], ...extra } };
}

const days = (n) => new Date(Date.now() - n * 86400000).toISOString();
const text = (n) => n.textContent.replace(/\s+/g, " ");

// ---- helpers --------------------------------------------------------------------------------------------------
test("the Shadow trial counts days from the approval, and ranges are judged honestly", () => {
  assert.equal(ob.trialDay(null), null);
  assert.equal(ob.trialDay("not a date"), null);
  assert.equal(ob.trialDay("2026-10-01T09:00:00Z", new Date("2026-10-01T10:00:00Z")), 1);
  assert.equal(ob.trialDay("2026-10-01T09:00:00Z", new Date("2026-10-05T10:00:00Z")), 5);
  assert.equal(ob.trialDay("2026-10-05T09:00:00Z", new Date("2026-10-01T10:00:00Z")), 1, "never day 0 or less");
  assert.equal(ob.trialWords(5), "day 5 of 14");
  assert.match(ob.trialWords(20), /two weeks are done/);
  assert.equal(ob.rangeVerdict({ point: 1, low: 0.5, high: 2 }), "sure");
  assert.equal(ob.rangeVerdict({ point: 1, low: -1, high: 2 }), "unsure");
  assert.equal(ob.rangeVerdict({ point: -1, low: -3, high: -0.5 }), "below");
  assert.equal(ob.rangeVerdict({ point: 1 }), null, "a bare point is never judged");
});

test("the autonomy badge says the level in words", () => {
  const g = { goal_id: "g", status: "active", autonomy: "shadow" };
  assert.match(autonomyBadge(g).text, /^Shadow: sends nothing$/);
  assert.match(autonomyBadge(g, 5).text, /Shadow: sends nothing · day 5 of 14/);
  assert.match(autonomyBadge(g, 5).long, /Shadow trial: day 5 of 14/);
  assert.equal(autonomyBadge({ ...g, autonomy: "approve" }).text, "You approve each batch");
  assert.equal(autonomyBadge({ ...g, autonomy: "autopilot" }).text, "Autopilot");
  assert.match(autonomyBadge({ ...g, autonomy: "autopilot", status: "paused" }).text, /^Paused · Autopilot/);
  assert.match(autonomyBadge({ ...g, status: "draft" }).text, /nothing runs yet/);
  assert.equal(autonomyBadge(null), null);
  // The five numbered screens stay five; Getting started sits outside them.
  assert.deepEqual(SCREENS.map((s) => s.id), ["connect", "goal", "plan", "live", "proof"]);
  assert.equal(START.id, "start");
});

test("the inbox counts what Live's Inbox lists: both endpoints, once each, info too, this goal and account-wide", () => {
  const a = { alert_id: "a", goal_id: "g1", severity: "info", at: "2026-10-20T10:00:00Z" };
  const b = { alert_id: "b", goal_id: null, severity: "critical", at: "2026-10-20T12:00:00Z" };
  const other = { alert_id: "c", goal_id: "g2", severity: "warning", at: "2026-10-20T11:00:00Z" };
  const out = inboxAlerts([[a, b, other], [a]], "g1");
  assert.deepEqual(out.map((x) => x.alert_id), ["b", "a"]);
  // With the exceptions-only inbox the count is its items: this goal's and the account-wide ones, nothing else.
  const items = { items: [{ item_id: "i1", goal_id: "g1" }, { item_id: "i2", goal_id: null }, { item_id: "i3", goal_id: "g2" }] };
  assert.deepEqual(inboxItems(items, "g1").map((x) => x.item_id), ["i1", "i2"]);
});

// ---- Getting started ------------------------------------------------------------------------------------------
function fakeApi({ goals = [], data = null, today = null, expected = null, proof = null, detail = null } = {}) {
  const calls = [];
  return {
    calls,
    listGoals: async () => goals,
    getDataReport: async () => data,
    getToday: async (id) => (calls.push(["today", id]), today),
    getExpected: async (id) => (calls.push(["expected", id]), expected),
    getProof: async () => proof,
    getGoal: async () => detail,
    approveGoal: async (id, autonomy) => (calls.push(["approve", id, autonomy]), { goal: { ...goals[0], autonomy } }),
    updateGoalBudget: async (id, budget) => (calls.push(["budget", id, budget]), { goal: { ...goals[0], budget } }),
  };
}

test("first run, no goal: only the ask's two steps, with the honest status", async () => {
  const { ctx } = ctxFor(fakeApi({ data: { customers: 0, orders: 0, sources: [] } }));
  const root = C.el("div");
  await ob.render(root, ctx);
  const t = text(root);
  assert.equal(root.querySelectorAll("li.ob-step").length, 2);
  assert.match(t, /What we ask: your customer data and one goal/);
  assert.match(t, /Two steps to start/);
  assert.match(t, /Upload your customer and order files, or connect your database/);
  assert.match(t, /asks you before it changes anything/);
  assert.match(t, /not yet run with a real business/);
  assert.match(t, /start(s)? in Shadow mode/);
  assert.doesNotMatch(t, /Doing now.*Doing now/, "only one step is the one to do now");
});

test("a server that cannot report on data says so instead of guessing", async () => {
  const { ctx } = ctxFor(fakeApi({ data: null }));
  const root = C.el("div");
  await ob.render(root, ctx);
  assert.match(text(root), /Can't tell yet/);
});

test("in Shadow: the trial day, what it would have done and its forecast, labelled, with 'not sure yet'", async () => {
  const goal = { goal_id: "g1", text: "Win back lapsed buyers", status: "active", autonomy: "shadow", budget: 800, currency: "USD" };
  const api = fakeApi({
    goals: [goal],
    data: { customers: 400, orders: 570, sources: [{ name: "csv" }], questions: [{ question_id: "q", text: "?" }] },
    today: {
      date: "2026-10-19", autonomy_since: days(4), customers_considered: 300, contacted: 41, left_alone: 259,
      reasons: [{ code: "weekly_cap", words: "Already contacted as often as the weekly limit allows", n: 200 }],
    },
    expected: { with_forecast: 41, currency: "USD", expected_net: { point: 30, low: -12, high: 72 } },
  });
  const { ctx } = ctxFor(api, { goalId: "g1", goal });
  const root = C.el("div");
  await ob.render(root, ctx);
  const t = text(root);
  assert.match(t, /Shadow trial: day 5 of 14/);
  assert.match(t, /would have contacted 41 customers and left 259 alone\. Nothing was sent\./);
  assert.match(t, /Already contacted as often as the weekly limit allows: 200/);
  assert.match(t, /-\$12\.00 – \$72\.00|−\$12\.00 – \$72\.00/);
  assert.match(t, /not sure yet/);
  assert.match(t, /forecast, not proof/);
  assert.match(t, /Data Doctor has 1 question/);
  assert.match(t, /9 more days/);
  assert.match(t, /General guidance|in general/i);
  assert.match(t, /not a number about your shop/);

  // Lowering the budget there, then turning on Approve mode.
  const form = root.querySelector("form.ob-budget");
  form.querySelector("input").value = "300";
  await form.dispatch("submit");
  assert.deepEqual(api.calls.find((c) => c[0] === "budget"), ["budget", "g1", 300]);
  const turnOn = root.querySelectorAll("button").find((b) => /Turn on Approve mode/.test(b.textContent));
  await turnOn.click();
  assert.deepEqual(api.calls.find((c) => c[0] === "approve"), ["approve", "g1", "approve"]);
});

test("the trial day falls back to the plan's approval date", async () => {
  const goal = { goal_id: "g1", text: "x", status: "active", autonomy: "shadow", budget: 800, currency: "USD" };
  const { ctx } = ctxFor(fakeApi({ goals: [goal], data: null, detail: { brief: { approved_at: days(15) } } }), { goalId: "g1" });
  const root = C.el("div");
  await ob.render(root, ctx);
  assert.match(text(root), /the two weeks are done \(day 16\)/);
  assert.match(text(root), /The two weeks are up/);
});

test("on the sample data (Approve): proof above zero suggests Autopilot; the budget can only be lowered", async () => {
  const api = mockApi();
  const { ctx, toasts } = ctxFor(api, { goalId: "goal_winback" });
  const root = C.el("div");
  await ob.render(root, ctx);
  const t = text(root);
  assert.equal(root.querySelectorAll("li.ob-step").length, 5);
  assert.match(t, /This goal is past Shadow/);
  assert.match(t, /Proof shows real extra profit compared with people we never contact: \$900 – \$7,700/);
  const proof = JSON.parse(await readFile(fileURLToPath(new URL("mock/proof.json", UI)), "utf8"));
  assert.equal(proof.incremental_profit.low, 900, "the number on screen is the sample file's");
  let form = root.querySelector("form.ob-budget");
  form.querySelector("input").value = "9000";
  await form.dispatch("submit");
  assert.match(text(form), /Couldn't change the budget/);
  form.querySelector("input").value = "3000";
  await form.dispatch("submit");
  assert.ok(toasts.some(([m]) => /now \$3,000/.test(m)));
  form = root.querySelector("form.ob-budget");
  assert.match(text(form), /Now \$3,000/);
});

// ---- the shell ---------------------------------------------------------------------------------------------------
async function until(check, what) {
  for (let i = 0; i < 300; i += 1) {
    if (check()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.fail(`timed out waiting for ${what}`);
}

test("the shell: autonomy badge, Paused itself, inbox count, Pause all sending, notes, Getting started", async () => {
  const ids = [
    "skip-link:a", "goal-select:select", "autonomy-badge:a", "selfpause-badge:a", "pause-button:button", "pause-scope:span",
    "inbox-link:a", "inbox-count:span", "nav-list:ol", "start-link:a", "operator-link:a", "goal-note:div", "mode-banner:div", "main:main",
  ];
  document.body.replaceChildren(...ids.map((x) => C.el(x.split(":")[1], { id: x.split(":")[0] })));
  const hashListeners = [];
  globalThis.location = { hash: "#/live/goal_winback", search: "?mock=1" };
  globalThis.history = { replaceState: (_s, _t, h) => (globalThis.location.hash = h) };
  globalThis.window = globalThis;
  globalThis.addEventListener = (type, fn) => type === "hashchange" && hashListeners.push(fn);
  globalThis.fetch = fakeFetch();
  const go = async (hash) => {
    globalThis.location.hash = hash;
    await Promise.all(hashListeners.map((fn) => fn()));
  };
  const $ = (id) => document.getElementById(id);
  try {
    await import(new URL("app.js?shell-onboarding-test", UI).href);
    await until(() => $("autonomy-badge").textContent === "You approve each batch", "the autonomy badge");
    assert.equal($("autonomy-badge").getAttribute("href"), "#/plan/goal_winback");
    // The count is what Live's Inbox lists: with the exceptions-only inbox (GET /v2/inbox), this goal's items and the
    // account-wide ones. The "Paused itself" badge still reads the alerts (health.json repeats one, counted once).
    const box = JSON.parse(await readFile(fileURLToPath(new URL("mock/inbox.json", UI)), "utf8"));
    const listed = box.items.filter((i) => !i.goal_id || i.goal_id === "goal_winback");
    await until(() => $("inbox-count").textContent === String(listed.length), "the inbox count");
    const alerts = JSON.parse(await readFile(fileURLToPath(new URL("mock/alerts.json", UI)), "utf8"));
    const expected = alerts.filter((a) => !a.goal_id || a.goal_id === "goal_winback");
    const selfPaused = expected.filter((a) => a.auto_paused).length;
    assert.equal($("selfpause-badge").hidden, false);
    assert.equal($("selfpause-badge").textContent, `Paused itself (${selfPaused})`);
    assert.match($("selfpause-badge").getAttribute("aria-label"), /paused sending on its own/);

    const pause = $("pause-button");
    assert.equal(pause.textContent, "Pause");
    assert.match(pause.getAttribute("aria-label"), /Pause all sending/);
    // "Stops all sending" is the Pause button's description now (shown on hover and focus), linked for screen readers.
    assert.match($("pause-scope").textContent, /^Stops all sending, for every goal, at once/);
    assert.equal($("pause-scope").getAttribute("role"), "tooltip");
    assert.ok(pause.getAttribute("aria-describedby").split(" ").includes("pause-scope"));
    await pause.click();
    await until(() => pause.textContent === "Resume", "Resume after pausing all");
    const toasts = document.getElementById("toasts");
    assert.match(toasts.textContent, /Paused all sending: 1 goal stopped/);
    assert.match($("goal-note").textContent, /Paused: nothing is being sent/);
    assert.match($("autonomy-badge").textContent, /^Paused · /);
    await pause.click();
    await until(() => pause.textContent === "Pause", "Pause again after resuming");
    assert.match(toasts.textContent, /Resumed: 1 goal/);

    // The draft goal (Shadow, plan not approved) and Getting started.
    await go("#/plan/goal_repeat");
    await until(() => /nothing runs yet/.test($("autonomy-badge").textContent), "the draft badge");
    await go("#/start");
    await until(() => $("main").textContent.includes("Getting started is safe"), "the Getting started screen");
    assert.equal($("start-link").getAttribute("aria-current"), "page");
    assert.equal(document.title, "Getting started · Autonomy");
  } finally {
    delete globalThis.window;
    delete globalThis.location;
    delete globalThis.history;
  }
});
