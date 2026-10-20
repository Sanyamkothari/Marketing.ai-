// Node tests for ui/v2/screens/live.js, proof.js and operator.js, run by test_live_proof_operator_js.py.
//
// The screens only use a small part of the DOM (createElement, attributes, listeners, children), so a
// tiny fake document stands in for a browser: no jsdom, no npm install. Each test renders a screen
// against a fake API that serves the real mock JSON files and checks what a person would read.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// ---- a tiny fake DOM ----------------------------------------------------------------------------------

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
    // Like the real DOM: anything that is not a node (null and false included) becomes its text.
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
  constructor(tag, ns = null) {
    super();
    this.tagName = tag.toLowerCase();
    this.namespaceURI = ns;
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.dataset = {};
  }
  // components.js (the page head and the descriptions) sets classes through className.
  get className() {
    return this.attributes.class || "";
  }
  set className(v) {
    this.attributes.class = String(v);
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "id") this.id = String(v);
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
  focus() {
    fakeDocument.activeElement = this;
  }
  select() {}
  /** Fire an event and wait for async handlers. */
  async fire(type, extra = {}) {
    const ev = { type, target: this, preventDefault() {}, ...extra };
    await Promise.all((this.listeners[type] || []).map((f) => f(ev)));
    await settle();
  }
}

const fakeDocument = {
  activeElement: null,
  listeners: {},
  head: new FakeElement("head"),
  body: new FakeElement("body"),
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (ns, tag) => new FakeElement(tag, ns),
  createTextNode: (text) => new FakeText(text),
  getElementById(id) {
    return all(this.head).find((n) => n.id === id) || all(this.body).find((n) => n.id === id) || null;
  },
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  },
  execCommand: () => true,
};
globalThis.document = fakeDocument;
globalThis.Node = FakeNode;

function all(node) {
  const out = [];
  const walk = (n) => {
    for (const c of n.childNodes) {
      if (c instanceof FakeElement) {
        out.push(c);
        walk(c);
      }
    }
  };
  walk(node);
  return out;
}
const byTag = (root, tag) => all(root).filter((n) => n.tagName === tag);
const byClass = (root, cls) => all(root).filter((n) => (n.getAttribute("class") || "").split(/\s+/).includes(cls));
const button = (root, text) => byTag(root, "button").find((b) => b.textContent.trim() === text);
const settle = () => new Promise((r) => setTimeout(r, 0));
/** A real DOM turns a stray `false`/`null`/`undefined` child into that word, so none may be rendered. */
const noJunk = (text) => !/\b(undefined|null|false|NaN)\b/.test(text);

// ---- the screens and a fake API on the mock files --------------------------------------------------------

const mockDir = new URL("../../../ui/v2/mock/", import.meta.url);
const mock = (name) => JSON.parse(readFileSync(new URL(name, mockDir), "utf8"));
const live = await import(new URL("../../../ui/v2/screens/live.js", import.meta.url).href);
const proofScreen = await import(new URL("../../../ui/v2/screens/proof.js", import.meta.url).href);
const operator = await import(new URL("../../../ui/v2/screens/operator.js", import.meta.url).href);

function fakeApi(overrides = {}) {
  const calls = [];
  const detail = mock("goal_detail.json");
  let goal = { ...detail.goal };
  const api = {
    calls,
    listGoals: async () => [goal],
    getGoal: async (id) => {
      calls.push(["getGoal", id]);
      return { ...detail, goal };
    },
    listDecisions: async (q) => {
      calls.push(["listDecisions", q]);
      const rows = mock("decisions.json").filter((d) => !q.customer_id || d.customer_id === q.customer_id);
      return rows.slice(0, q.limit || rows.length);
    },
    getCustomer: async (id) => {
      calls.push(["getCustomer", id]);
      const c = mock("customer.json");
      if (c.state.customer_id !== id) throw Object.assign(new Error(`Customer ${id} not found`), { status: 404 });
      return c;
    },
    getBriefing: async () => mock("briefing.json"),
    getProof: async () => mock("proof.json"),
    getHealth: async () => mock("health.json"),
    listAlerts: async () => mock("alerts.json"),
    pauseGoal: async (id) => {
      calls.push(["pauseGoal", id]);
      goal = { ...goal, status: "paused" };
      return goal;
    },
    resumeGoal: async (id) => {
      calls.push(["resumeGoal", id]);
      goal = { ...goal, status: "active" };
      return goal;
    },
    ...overrides,
  };
  return api;
}

function makeCtx(api, extra = {}) {
  const toasts = [];
  return { ctx: { api, goalId: "goal_winback", navigate: () => {}, toast: (m, k) => toasts.push([m, k]), ...extra }, toasts };
}

// ---- pure helpers ----------------------------------------------------------------------------------------

test("money uses the goal's currency and the em dash for nothing", () => {
  assert.equal(live.money(4300, "USD"), "$4,300");
  assert.equal(live.money(0.38, "USD"), "$0.38");
  assert.equal(live.money(4300, "EUR"), "€4,300");
  assert.equal(live.money(null, "USD"), "—");
  assert.equal(live.money(12, "XYZ").includes("XYZ"), true);
  assert.equal(live.pct(0.0002), "0.02%");
  assert.equal(live.pct(0.987), "98.7%");
  assert.equal(live.pct(undefined), "—");
});

test("an estimate is never shown as a bare point", () => {
  const usd = (v) => live.money(v, "USD");
  assert.equal(live.range({ point: 4300, low: 900, high: 7700 }, usd), "$4,300 (range $900 to $7,700)");
  assert.equal(live.range(null, usd), "—");
  assert.equal(live.range({ point: 1, low: null, high: 2 }, usd), "—");
});

test("decisions split into contacted and left alone, newest first", () => {
  const { acted, leftAlone } = live.splitDecisions(mock("decisions.json"));
  assert.equal(acted.length + leftAlone.length, mock("decisions.json").length);
  assert.ok(acted.every((d) => d.kind === "act"));
  assert.ok(leftAlone.every((d) => d.kind === "nothing"));
  assert.ok(acted[0].made_at >= acted[acted.length - 1].made_at);
});

test("alerts from /alerts and /health appear once each, newest first", () => {
  const alerts = mock("alerts.json");
  const merged = live.mergeAlerts([alerts, mock("health.json").alerts], "goal_winback");
  assert.equal(merged.length, alerts.length);
  assert.deepEqual(merged.map((a) => a.at), [...merged.map((a) => a.at)].sort().reverse());
  assert.equal(live.mergeAlerts([[{ alert_id: "x", goal_id: "other", at: "1" }]], "goal_winback").length, 0);
});

test("the proof summary gives every estimate with its range", () => {
  const goal = mock("goals.json")[0];
  const text = proofScreen.proofSummaryText(mock("proof.json"), goal);
  assert.match(text, /\$4,300 \(range \$900 to \$7,700\) in extra profit/);
  assert.match(text, /\$0\.38 \(range \$0\.05 to \$0\.71\) more profit per customer/);
  assert.match(text, /2\.3× \(range 0\.5× to 4\.2×\)/);
  assert.match(text, /8,120 customers decided by the agent/);
  assert.ok(!text.includes("undefined") && !text.includes("NaN"));
});

test("the proof summary says when there is not enough data", () => {
  assert.match(proofScreen.proofSummaryText(null, null), /not enough data yet/);
  const thin = { ...mock("proof.json"), incremental_profit: null, incremental_revenue: null, lift_vs_baseline: null, roi: null };
  const text = proofScreen.proofSummaryText(thin, mock("goals.json")[0]);
  assert.match(text, /not enough data yet to compare with leaving customers alone/);
  assert.match(text, /not enough data yet to compare with the simple rule/);
  const unsure = { ...mock("proof.json"), lift_vs_baseline: { point: 0.1, low: -0.2, high: 0.4 } };
  assert.match(proofScreen.proofSummaryText(unsure, mock("goals.json")[0]), /includes zero/);
});

test("ordering compares instants, not text, so mixed offsets sort right", () => {
  const rows = [
    { decision_id: "a", kind: "act", action: {}, made_at: "2026-10-20T12:00:00+02:00" }, // 10:00Z
    { decision_id: "b", kind: "act", action: {}, made_at: "2026-10-20T11:00:00Z" },
  ];
  assert.deepEqual(live.splitDecisions(rows).acted.map((d) => d.decision_id), ["b", "a"]);
  assert.deepEqual(operator.filterDecisions(rows).map((d) => d.decision_id), ["b", "a"]);
  const tl = live.timeline({ events: [{ ts: "2026-10-20T12:30:00+02:00" }], decisions: [{ made_at: "2026-10-20T10:45:00Z" }] });
  assert.deepEqual(tl.map((x) => x.kind), ["decision", "event"]);
});

test("a decision row shows the start of the reason's why, never words that are not in it", () => {
  const r = "Sent a quality message by email with no discount: the top option's estimate is uncertain, so a close alternative won this draw.";
  assert.equal(operator.reasonPreview(r), "The top option's estimate is uncertain…");
  assert.equal(operator.reasonPreview("Left alone: no consent."), "No consent");
  assert.equal(operator.reasonPreview(""), "—");
  for (const d of mock("decisions.json")) {
    const words = operator.reasonPreview(d.reason).replace(/…$/, "").toLowerCase();
    assert.ok(String(d.reason).toLowerCase().includes(words), d.decision_id);
  }
});

test("the operator filters narrow the decisions", () => {
  const rows = mock("decisions.json");
  const acts = operator.filterDecisions(rows, { kind: "act" });
  assert.ok(acts.length > 0 && acts.every((d) => d.kind === "act"));
  assert.ok(operator.filterDecisions(rows, { group: "holdout" }).every((d) => d.group === "holdout"));
  assert.ok(operator.filterDecisions(rows, { channel: "whatsapp" }).every((d) => d.action.channel === "whatsapp"));
  assert.equal(operator.filterDecisions(rows, { text: "cust_1042" }).length, 1);
  assert.equal(operator.filterDecisions(rows, {}).length, rows.length);
});

// ---- rendering -------------------------------------------------------------------------------------------

test("Live shows the briefing, the feed with reasons, the inbox and health", async () => {
  const root = new FakeElement("main");
  const api = fakeApi();
  const { ctx } = makeCtx(api);
  await live.render(root, ctx);
  const text = root.textContent;
  assert.ok(text.includes(mock("briefing.json").headline));
  for (const d of mock("decisions.json")) assert.ok(text.includes(d.reason), `reason of ${d.decision_id}`);
  const nothing = mock("decisions.json").filter((d) => d.kind === "nothing").length;
  const details = byClass(root, "ap-left-alone")[0];
  assert.ok(details.textContent.includes(`Left alone (${nothing})`));
  assert.equal(details.getAttribute("open"), null, "left-alone decisions start collapsed");
  assert.ok(text.includes(`Inbox (${mock("alerts.json").length})`));
  assert.ok(text.includes("Auto-paused"));
  assert.ok(text.includes("98.7%"));
  assert.ok(text.includes("Hard cap 2 per customer"));
  assert.ok(text.includes("Email · No offer"));
  assert.ok(noJunk(text), "no stray false/null/undefined text");
  assert.ok(fakeDocument.getElementById("v2-feed-css"), "the screen loads its stylesheet");
  // The api was asked for this goal's decisions.
  assert.deepEqual(api.calls.find((c) => c[0] === "listDecisions")[1], { goal_id: "goal_winback", limit: 100 });
});

test("Live pauses and resumes with one click", async () => {
  const root = new FakeElement("main");
  const api = fakeApi();
  const { ctx, toasts } = makeCtx(api);
  await live.render(root, ctx);
  await button(root, "Pause everything").fire("click");
  assert.deepEqual(api.calls.filter((c) => c[0] === "pauseGoal"), [["pauseGoal", "goal_winback"]]);
  const resume = button(root, "Resume");
  assert.ok(resume, "the button now resumes");
  assert.ok(root.textContent.includes("Paused"));
  assert.equal(toasts.at(-1)[1], "warning");
  await resume.fire("click");
  assert.ok(button(root, "Pause everything"));
});

test("Live follows a pause made elsewhere (the app's top bar)", async () => {
  const root = new FakeElement("main");
  const subscribers = [];
  const api = fakeApi({ onGoalChange: (fn) => (subscribers.push(fn), () => {}) });
  await live.render(root, makeCtx(api).ctx);
  assert.equal(subscribers.length, 1);
  const goal = mock("goal_detail.json").goal;
  subscribers[0]({ ...goal, goal_id: "some_other_goal", status: "paused" });
  assert.ok(button(root, "Pause everything"), "another goal's change is ignored");
  subscribers[0]({ ...goal, status: "paused" });
  assert.ok(button(root, "Resume"), "the header shows the new status");
});

test("Live keeps working when one endpoint fails", async () => {
  const root = new FakeElement("main");
  const api = fakeApi({
    getBriefing: async () => {
      throw new Error("server error");
    },
    getHealth: async () => ({ as_of: "2026-10-20T13:15:00Z", alerts: [] }),
  });
  await live.render(root, makeCtx(api).ctx);
  const text = root.textContent;
  assert.ok(text.includes("Couldn't load today's briefing: server error"));
  assert.ok(text.includes(mock("decisions.json")[0].reason), "the feed still shows");
  const tiles = byClass(root, "ap-tile-value").map((n) => n.textContent);
  assert.ok(tiles.length >= 4 && tiles.every((t) => t.startsWith("—")), "missing health numbers are dashes");
});

test("clicking a customer opens the side panel with the why of each decision", async () => {
  const root = new FakeElement("main");
  await live.render(root, makeCtx(fakeApi()).ctx);
  const who = byTag(root, "button").find((b) => b.textContent === "cust_1042");
  await who.fire("click");
  await settle();
  const panel = byClass(root, "ap-panel")[0];
  assert.ok(panel, "panel open");
  assert.equal(panel.getAttribute("role"), "dialog");
  const text = panel.textContent;
  assert.ok(text.includes("$412.50"), "revenue in the goal's currency");
  assert.ok(text.includes("Why: "));
  for (const d of mock("customer.json").decisions) assert.ok(text.includes(d.reason));
  assert.ok(text.includes("Bought"));
  // Escape closes it and focus goes back to the customer button.
  for (const fn of fakeDocument.listeners.keydown || []) fn({ key: "Escape" });
  assert.equal(byClass(root, "ap-panel").length, 0);
  assert.equal(fakeDocument.activeElement, who);
});

test("a customer with no record shows an empty state, not an error", async () => {
  const root = new FakeElement("main");
  await live.render(root, makeCtx(fakeApi()).ctx);
  await byTag(root, "button").find((b) => b.textContent === "cust_2210").fire("click");
  await settle();
  const text = byClass(root, "ap-panel")[0].textContent;
  assert.ok(text.includes("No details for customer cust_2210 yet."));
  assert.ok(!text.includes("Couldn't load"));
});

test("Proof shows ranges, group sizes, a chart and copies a summary", async () => {
  const root = new FakeElement("main");
  const { ctx, toasts } = makeCtx(fakeApi());
  await proofScreen.render(root, ctx);
  const text = root.textContent;
  assert.ok(text.includes("(likely $900 to $7,700)"));
  assert.ok(text.includes("$0.05 to $0.71"));
  assert.ok(text.includes("8,120") && text.includes("1,015"));
  assert.ok(text.includes("$1,840"));
  assert.ok(text.includes("0.5× to 4.2×"));
  const charts = byTag(root, "svg");
  assert.equal(charts.length, 1, "one chart: the simple rule's single figure needs none");
  assert.equal(byClass(charts[0], "ap-bar").length, 2);
  assert.match(charts[0].getAttribute("aria-label"), /range/);
  const field = byTag(root, "textarea")[0];
  assert.equal(field.value, proofScreen.proofSummaryText(mock("proof.json"), mock("goal_detail.json").goal));
  await button(root, "Copy summary").fire("click");
  assert.equal(toasts.at(-1)[1], "good");
});

test("Proof with no notes renders no stray text", async () => {
  const root = new FakeElement("main");
  await proofScreen.render(root, makeCtx(fakeApi({ getProof: async () => ({ ...mock("proof.json"), notes: [] }) })).ctx);
  assert.ok(root.textContent.includes("(likely $900 to $7,700)"));
  assert.ok(noJunk(root.textContent));
});

test("Proof says the request failed rather than 'not enough data' when it cannot load", async () => {
  const root = new FakeElement("main");
  const api = fakeApi({
    getGoal: async () => ({ ...mock("goal_detail.json"), proof: null }),
    getProof: async () => {
      throw new Error("server error");
    },
  });
  await proofScreen.render(root, makeCtx(api).ctx);
  assert.ok(root.textContent.includes("Couldn't load proof: server error"));
  assert.ok(!root.textContent.includes("Not enough data yet"));
});

test("Proof with no summary yet says so", async () => {
  const root = new FakeElement("main");
  await proofScreen.render(root, makeCtx(fakeApi({ getProof: async () => null })).ctx);
  assert.ok(root.textContent.includes("Not enough data yet"));
  assert.equal(byTag(root, "svg").length, 0);
});

test("Operator shows raw JSON, a filterable decisions table and customer lookup", async () => {
  const root = new FakeElement("main");
  const api = fakeApi();
  await operator.render(root, makeCtx(api).ctx);
  const total = mock("decisions.json").length;
  const rowsNow = () => byTag(byTag(root, "tbody")[0], "tr").length;
  assert.equal(rowsNow(), total);
  assert.ok(byTag(root, "pre").some((p) => p.textContent.includes('"goal_id": "goal_winback"')));
  const kind = byTag(root, "select")[1];
  kind.value = "act";
  await kind.fire("change");
  const acts = mock("decisions.json").filter((d) => d.kind === "act").length;
  assert.equal(rowsNow(), acts);
  assert.ok(root.textContent.includes(`Showing ${acts} of ${total} fetched`));
  // Each row shows the start of the reason; the whole reason is that cell's description (hover, focus or tap).
  const row = byTag(byTag(root, "tbody")[0], "tr")[0];
  const id = byTag(row, "button").find((b) => (b.getAttribute("aria-label") || "").startsWith("Show JSON of ")).getAttribute("aria-label").slice(13);
  const cell = byClass(row, "op-reason")[0];
  const tip = all(cell).find((n) => n.getAttribute("role") === "tooltip");
  assert.equal(tip.textContent, mock("decisions.json").find((d) => d.decision_id === id).reason);
  assert.equal(tip.hidden, true);
  // Customer lookup by id: the second form, above the long decisions list.
  const forms = byTag(root, "form");
  assert.ok(byTag(forms[1], "button").some((b) => b.textContent === "Look up"));
  const lookupInput = byTag(forms[1], "input")[0];
  lookupInput.value = "cust_1042";
  await forms[1].fire("submit");
  assert.ok(root.textContent.includes("cust_1042 · Agent · 6 purchases · $412.50 revenue"));
  lookupInput.value = "cust_nobody";
  await forms[1].fire("submit");
  assert.ok(root.textContent.includes("No customer cust_nobody."));
});
