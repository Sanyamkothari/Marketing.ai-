// Node tests for the Live screen's promises (ui/v2/screens/live.js), run by test_live.py.
//
// The screen is drawn in a tiny fake DOM against the real API client (ui/v2/api.js) in mock mode, so every number
// it shows comes from the sample files in ui/v2/mock/, the same shapes the /v2 API serves. Each test checks what a
// business owner would read: the pitch's daily sentence, the left-alone reasons, the money with its range or "not
// sure yet", the delivery labels, the person's name, and the customer panel in words.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// ---- a tiny fake DOM --------------------------------------------------------------------------------------------
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
  async fire(type) {
    const ev = { type, target: this, preventDefault() {} };
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
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
  getElementById(id) {
    return [...all(this.head), ...all(this.body)].find((n) => n.id === id) || null;
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

function all(node) {
  const out = [];
  for (const c of node.childNodes) if (c instanceof FakeElement) out.push(c, ...all(c));
  return out;
}
const byTag = (root, tag) => all(root).filter((n) => n.tagName === tag);
const byClass = (root, cls) => all(root).filter((n) => (n.getAttribute("class") || "").split(/\s+/).includes(cls));
const settle = () => new Promise((r) => setTimeout(r, 0));
const noJunk = (text) => !/\b(undefined|null|false|NaN)\b/.test(text);

// ---- the code under test and the real API client on the sample files -------------------------------------------
const UI = new URL("../../../ui/v2/", import.meta.url);
const live = await import(new URL("screens/live.js", UI).href);
const { createApi } = await import(new URL("api.js", UI).href);
const mock = async (name) => JSON.parse(await readFile(fileURLToPath(new URL(`mock/${name}`, UI)), "utf8"));

/** A fetch that serves the mock folder from disk (the client only fetches mock files in mock mode). */
async function fileFetch(url) {
  try {
    const text = await readFile(fileURLToPath(url), "utf8");
    return { ok: true, status: 200, text: async () => text, headers: { get: () => "application/json" } };
  } catch {
    return { ok: false, status: 404, text: async () => "", headers: { get: () => "" } };
  }
}

function mockApi(overrides = {}) {
  const api = createApi({ mock: true, fetch: fileFetch, mockBase: new URL("mock/", UI).href, now: () => new Date("2026-10-20T14:00:00Z") });
  return Object.assign(api, overrides);
}

async function draw(overrides = {}, extra = {}) {
  const root = new FakeElement("main");
  fakeDocument.body.replaceChildren(root);
  const toasts = [];
  const ctx = { api: mockApi(overrides), goalId: "goal_winback", navigate: () => {}, toast: (m, k) => toasts.push([m, k]), pauseAllInTopBar: true, ...extra };
  await live.render(root, ctx);
  await settle();
  return { root, toasts, ctx };
}

// ---- pure words --------------------------------------------------------------------------------------------------
test("the daily sentence is the pitch's own, and true to the autonomy level", () => {
  const base = { date: "2026-10-19", as_of: "2026-10-20T13:20:00Z", contacted: 341, left_alone: 1200 };
  assert.equal(live.briefingSentence({ ...base, autonomy: "autopilot" }), "Yesterday I contacted 341 customers, left 1,200 alone, and here's why.");
  assert.match(live.briefingSentence({ ...base, autonomy: "shadow", sends_nothing: true }), /^Yesterday I would have contacted 341 customers \(Shadow mode, so nothing was sent\)/);
  assert.match(live.briefingSentence({ ...base, autonomy: "approve" }), /picked 341 customers to contact \(each batch waits for your approval\)/);
  assert.equal(live.dayName("2026-10-20", "2026-10-20T13:00:00Z").title, "Today so far");
  assert.equal(live.dayName("2026-10-15", "2026-10-20T13:00:00Z").title, "On 15 Oct");
});

test("extra profit is shown with its range, and 'not sure yet' when the range includes zero or is missing", () => {
  const sure = live.profitWords({ incremental_profit: { point: 570, low: 310, high: 830 } }, "USD");
  assert.equal(sure.sure, true);
  assert.equal(sure.value, "$570 (range $310 to $830)");
  const unsure = live.profitWords({ incremental_profit: { point: 120, low: -80, high: 330 } }, "USD");
  assert.equal(unsure.value, "Not sure yet");
  assert.match(unsure.note, /\$120 \(range -\$80 to \$330\).*includes zero/);
  assert.equal(live.profitWords(null, "USD").value, "Not sure yet");
  assert.equal(live.profitWords({ incremental_profit: null }, "USD").sure, false);
});

test("the panel's facts are words, not jargon", () => {
  assert.equal(live.upliftWords(0.052), "raises the chance they buy by about 5.2 in 100");
  assert.equal(live.upliftWords(-0.004), "lowers the chance they buy by about 0.4 in 100");
  assert.equal(live.ruleWords("no_consent:whatsapp"), "WhatsApp: has not agreed to messages");
  assert.equal(live.ruleWords("preference:no_discounts"), "Asked for no discounts");
  assert.equal(live.ruleWords("max_contacts_per_week"), "Already contacted as often as the weekly limit allows");
  assert.equal(live.refillWords({ item_name: "Vitamin C serum", days_until_due: 3.2, window_open: false }), "Due for a refill of Vitamin C serum in about 3 days");
  assert.equal(live.refillWords({ item_name: "Serum", days_until_due: -4, window_open: true }), "Overdue for a refill of Serum, by about 4 days");
  assert.equal(live.fineMoney(0.001, "USD"), "under $0.01");
  assert.equal(live.subjectWords("We miss you, {{first_name}}"), "We miss you, [first name]");
  assert.equal(live.deliveryLabel(null, "shadow").word, "Would send (Shadow)");
  assert.equal(live.deliveryLabel(null, "autopilot"), null, "no label is guessed outside Shadow");
  assert.equal(live.deliveryLabel({ state: "held" }, "approve").word, "Waiting for your approval");
});

test("each option it weighed says why it won or lost", () => {
  const action = { channel: "email", offer_id: "none", content_id: "c1", send_at: "t" };
  const d = { kind: "act", action, expected_net: 1.5, attention_price: 0.15 };
  assert.equal(live.bidVerdict({ action, expected_net: 1.5 }, d), "Chosen");
  assert.match(live.bidVerdict({ action: { ...action, offer_id: "pct10" }, expected_net: -1.2 }, d), /would lose money/);
  assert.match(live.bidVerdict({ action: { ...action, content_id: "c2" }, expected_net: 0.1 }, d), /cost of bothering them again/);
});

// ---- the screen on the sample data -------------------------------------------------------------------------------
test("Live leads with yesterday: contacted, left alone and why, from GET /v2/today", async () => {
  const today = await mock("today.json");
  const { root } = await draw();
  const text = root.textContent;
  assert.ok(text.includes(live.briefingSentence(today)), "the pitch's sentence");
  assert.ok(text.includes("412") && text.includes("1,688"));
  for (const r of today.reasons) {
    assert.ok(text.includes(r.words), r.code);
    assert.ok(text.includes(r.n.toLocaleString("en-US")), `${r.code} count`);
  }
  assert.ok(text.includes("Made a decision for each of 2,100 customers yesterday"), "always-on: how many it looked at");
  assert.ok(text.includes("Next check: 21 Oct"), "when it checks next");
  assert.ok(text.includes("371 chosen by Autonomy, 41 by the simple rule"));
  // Extra profit is set large with its range on the line right under it, in the same cell (never a bare point).
  const profit = byClass(root, "lv-profit")[0];
  assert.ok(profit, "the extra profit figure");
  assert.ok(byClass(profit, "kpi-value")[0].textContent === "$4,300", "extra profit, from /proof");
  assert.ok(byClass(profit, "kpi-range")[0].textContent === "Range $900 to $7,700", "with its range");
  assert.ok(text.includes("$1,840 of $5,000") && text.includes("$3,160 left"), "spend against the budget");
  assert.ok(text.includes("$2,960 in sales"));
  // How it is measured is a description on the label, opened by hover, focus or tap (DESIGN.md section 6).
  const tipFor = (node) => all(root).find((n) => n.getAttribute("role") === "tooltip" && (node.getAttribute("aria-describedby") || "").split(" ").includes(n.id));
  const purchases = all(root).find((n) => n.textContent === "Purchases" && n.getAttribute("aria-describedby"));
  assert.ok(tipFor(purchases).textContent.includes("Every purchase by these customers, not only extra ones"));
  const profitLabel = all(profit).find((n) => n.textContent === "Extra profit so far" && n.getAttribute("aria-describedby"));
  assert.match(tipFor(profitLabel).textContent, /small random group of customers it never contacts/);
  assert.ok(text.includes(today.learned[0]), "what it learned");
  assert.ok(noJunk(text), "no stray false/null/undefined text");
});

test("the feed says what really happened, to whom, with the message", async () => {
  const activity = await mock("activity.json");
  const { root } = await draw();
  const text = root.textContent;
  assert.ok(text.includes("Waiting for your approval"), "held in a batch");
  assert.ok(text.includes("Scheduled"), "queued");
  assert.ok(text.includes("Message: “We miss you, [first name]”"), "the message subject, with its fill-ins in words");
  const maya = byTag(root, "button").find((b) => b.getAttribute("aria-label") === "Open Maya (customer cust_1042)");
  assert.ok(maya, "a person, not an id");
  assert.ok(maya.textContent.includes("Maya") && maya.textContent.includes("cust_1042"));
  for (const r of activity) assert.ok(text.includes(r.decision.reason), r.decision.decision_id);
  for (const r of activity.filter((x) => x.left_alone)) assert.ok(text.includes(r.left_alone.words), r.left_alone.code);
  const nothing = activity.filter((r) => r.decision.kind !== "act").length;
  assert.ok(byClass(root, "ap-left-alone").some((n) => n.textContent.includes(`Left alone (${nothing})`)), "doing nothing is listed as decisions");
});

test("batches are worded by channel and offer, and auto-pauses show at the top", async () => {
  const breakdowns = await mock("batch_breakdowns.json");
  const batches = await mock("batches.json");
  const { root } = await draw();
  const text = root.textContent;
  for (const b of batches.filter((x) => x.status === "pending")) {
    const words = breakdowns.find((x) => x.batch_id === b.batch_id).words;
    assert.ok(text.includes(words), b.batch_id);
  }
  assert.ok(!text.includes("pct10") && !text.includes("freeship"), "no raw offer ids");
  const banner = byClass(root, "lv-selfpause")[0];
  assert.ok(banner, "the self-pause banner");
  assert.ok(banner.textContent.includes("WhatsApp quality rating turned yellow"));
  assert.ok(text.includes("Pauses the goal at 3× your usual rate (usual 0.11%), that is 0.33%"), "the unsubscribe stop in words");
});

test("a goal that paused itself gets a red banner naming why", async () => {
  const detail = await mock("goal_detail.json");
  const alert = { alert_id: "a1", at: "2026-10-20T12:00:00Z", goal_id: "goal_winback", severity: "critical", code: "complaint_rate", message: "Complaints reached 0.12%, above your 0.08% limit, so this goal paused itself.", auto_paused: true };
  const { root } = await draw({
    getGoal: async () => ({ ...detail, goal: { ...detail.goal, status: "paused" } }),
    listAlerts: async () => [alert],
  });
  const banner = byClass(root, "lv-selfpause-goal")[0];
  assert.ok(banner, "red banner");
  assert.equal(banner.getAttribute("role"), "alert");
  assert.ok(banner.textContent.includes("This goal paused itself"));
  assert.ok(banner.textContent.includes(alert.message));
});

test("Shadow says it would have, and 'not sure yet' when the range includes zero", async () => {
  const today = await mock("today.json");
  const proof = await mock("proof.json");
  const detail = await mock("goal_detail.json");
  const { root } = await draw({
    getGoal: async () => ({ ...detail, goal: { ...detail.goal, autonomy: "shadow" } }),
    getToday: async () => ({ ...today, autonomy: "shadow", sends_nothing: true, messages_sent: 0, delivered: 0 }),
    getProof: async () => ({ ...proof, incremental_profit: { point: 300, low: -200, high: 800 } }),
    listActivity: async () => (await mock("activity.json")).map((r) => ({ ...r, delivery: null })),
  });
  const text = root.textContent;
  assert.ok(text.includes("Yesterday I would have contacted 412 customers (Shadow mode, so nothing was sent)"));
  assert.ok(text.includes("Would send (Shadow)"));
  assert.ok(text.includes("Not sure yet"));
  assert.ok(text.includes("The range includes zero"));
  assert.ok(text.includes("Shadow: it decides and shows you what it would do, but sends nothing."));
});

test("tapping a customer shows what it knows in words, every decision, and the options it weighed", async () => {
  const insight = await mock("customer_insight.json");
  const { root } = await draw();
  const maya = byTag(root, "button").find((b) => b.getAttribute("aria-label") === "Open Maya (customer cust_1042)");
  await maya.fire("click");
  await settle();
  await settle();
  const panel = byClass(root, "ap-panel")[0];
  assert.ok(panel, "panel open");
  const text = panel.textContent;
  assert.ok(byTag(panel, "h2")[0].textContent.startsWith("Maya"), "the person's name");
  assert.ok(text.includes(insight.best_hour_words), "reading hours");
  assert.ok(text.includes(insight.offer_habit_words), "offer habit");
  assert.ok(text.includes("Due for a refill of House blend coffee, 1 kg in about 84 days"), "refill due");
  assert.ok(text.includes(insight.refill.basis));
  assert.ok(text.includes("Options it weighed (1)"));
  assert.ok(text.includes("Raises the chance they buy by about 5.2 in 100"));
  assert.ok(text.includes("Chosen"));
  assert.ok(text.includes("The bar a message had to clear: $0.15 (the cost of bothering them again)."));
  assert.ok(text.includes("Ruled out: WhatsApp: has not agreed to messages; Asked for no discounts."));
  assert.ok(text.includes("Delivered") && text.includes("Scheduled"), "what happened to each decision");
  assert.ok(text.includes(insight.left_alone.dec_20261017_031.words), "the left-alone decision's reason");
  assert.ok(text.includes("Bought · $58 · House blend coffee, 1 kg"), "products by name");
  assert.ok(!text.includes("churn_risk") && !text.includes("chance of this pick"), "no model jargon");
  assert.ok(noJunk(text));
});

test("any customer can be looked up by id", async () => {
  const { root } = await draw();
  const form = byClass(root, "lv-find")[0];
  byTag(form, "input")[0].value = "cust_1042";
  await form.fire("submit");
  // The panel reads two mock files from disk; wait until it has drawn (up to about 2 s).
  const panelText = () => byClass(root, "ap-panel")[0]?.textContent ?? "";
  for (let i = 0; i < 200 && !panelText().includes("Spent with you"); i += 1) {
    await new Promise((r) => setTimeout(r, 10));
  }
  assert.ok(panelText().includes("Spent with you"));
});
