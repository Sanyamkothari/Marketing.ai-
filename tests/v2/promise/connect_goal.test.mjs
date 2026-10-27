// Node checks for the Connect and Goal screens against the promises they carry (docs/v2/PROMISE_MAP.md rows
// 6-14): what is connected, what the Data Doctor fixed and asks, how messages go out, sending health in words,
// the goal's words read back as a budget and an end date, and opportunities sized in customers. Every number
// checked here is one the sample files or the fake server sent. Run by tests/v2/promise/test_connect_goal.py,
// or directly: `node tests/v2/promise/connect_goal.test.mjs`.

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
const MOCK = new URL("mock/", UI);
const { createApi } = await import(new URL("api.js", UI).href);
const C = await import(new URL("components.js", UI).href);
const connect = await import(new URL("screens/connect.js", UI).href);
const goal = await import(new URL("screens/goal.js", UI).href);

const sample = async (name) => JSON.parse(await readFile(fileURLToPath(new URL(name, MOCK)), "utf8"));

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
const NOW = new Date("2026-10-20T08:00:00Z");
const mockApi = () => createApi({ mock: true, fetch: fakeFetch(), now: () => NOW });
/** A live /v2 with only `routes` ("GET /v2/health" -> body); anything else is FastAPI's own 404. */
const liveApi = (routes, seen = []) =>
  createApi({
    base: "",
    mock: undefined,
    fetch: fakeFetch((url, init) => {
      const key = `${init.method || "GET"} ${url.split("?")[0]}`;
      seen.push([key, init.body]);
      if (key in routes) {
        const r = routes[key];
        return typeof r === "function" ? r(init) : json(200, r);
      }
      return json(404, { detail: "Not Found" });
    }),
  });

function makeCtx(api, goalId = null) {
  const calls = { navigate: [], toast: [] };
  return { calls, ctx: { api, goalId, goal: null, navigate: (...a) => calls.navigate.push(a), toast: (m, t) => calls.toast.push([m, t]), refreshGoals: async () => {} } };
}
const byText = (root, tag, text) => root.querySelectorAll(tag).filter((n) => n.textContent.trim() === text);
const byClass = (root, cls) => root.querySelectorAll(`.${cls}`);
const settle = () => new Promise((r) => setTimeout(r, 0));

// ---- Connect ------------------------------------------------------------------------------------------------
test("Connect (sample data): says where and what to upload, and shows what is connected", async () => {
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await connect.render(root, ctx);
  const text = root.textContent;
  const data = await sample("data_status.json");
  for (const s of ["Upload your files", "CSV, Excel", "Or connect your database", "Read-only access", "Where: "]) assert.ok(text.includes(s), s);
  // What to bring sits in the descriptions of "customers" and "orders" (hover, focus or tap), still in the page.
  const tipOf = (word) => {
    const target = root.querySelectorAll("span").find((n) => n.classes().includes("has-info") && n.textContent === word);
    assert.ok(target, `"${word}" has a description`);
    return root.querySelectorAll("span").find((n) => n.id === target.getAttribute("aria-describedby")).textContent;
  };
  assert.match(tipOf("customers"), /^One row per customer, with their id, email or phone number, and whether they agreed to hear from you\.$/);
  assert.match(tipOf("orders"), /^One row per order, with the order id, the customer id, the date, the amount and what they bought\.$/);
  assert.match(tipOf("Read-only access"), /never writes to your database/);
  const hrefs = root.querySelectorAll("a").map((a) => a.getAttribute("href"));
  assert.ok(hrefs.includes("/") && hrefs.includes("/#/connections"), "links to the Data Doctor and to database connections");
  const totals = byClass(root, "cn-total").map((n) => n.textContent);
  assert.deepEqual(totals, [`Customers${C.int(data.customers)}`, `Orders${C.int(data.orders)}`, `Products in those orders${C.int(data.products)}`]);
  assert.ok(text.includes("Uploaded file (csv)"));
  // One source: its counts are the totals above, so it is one line, not a table repeating them.
  assert.equal(root.querySelectorAll("table").length, 0);
  assert.match(byClass(root, "cn-from")[0].textContent, /^From Uploaded file \(csv\) · latest data /);
});

test("Connect (sample data): what the Data Doctor fixed, and what it is waiting for you to answer", async () => {
  const { ctx } = makeCtx(mockApi());
  const root = C.el("div");
  await connect.render(root, ctx);
  const text = root.textContent;
  const data = await sample("data_status.json");
  const fixes = byClass(root, "cn-fix-list")[0].querySelectorAll("li").map((li) => li.textContent);
  assert.deepEqual(fixes, data.fixes.map((f) => `${f.what}${C.int(f.rows)} rows`));
  for (const p of data.problems) assert.ok(text.includes(p), p);
  assert.ok(text.includes(`Waiting for your answer (${data.questions.length})`));
  assert.ok(text.includes("It asks before changing anything."));
  // How it asks is the promise's description; each question says how many rows it would change.
  const promise = byClass(root, "cn-promise")[0];
  assert.match(promise.querySelector("strong").getAttribute("aria-describedby") || "", /\S/);
  assert.match(promise.textContent, /shows you the change and how many rows it touches, then waits for your yes\./);
  for (const q of data.questions) {
    assert.ok(text.includes(q.text), q.text);
    assert.ok(text.includes(`Would change ${C.int(q.rows_affected)} rows.`));
  }
  assert.equal(byText(root, "a", "Answer in the Data Doctor").length, 1);
});

test("Connect (sample data): how messages go out, and sending health explained line by line", async () => {
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await connect.render(root, ctx);
  const text = root.textContent;
  const channels = await sample("channels.json");
  const lines = byClass(root, "cn-channel").map((n) => n.textContent);
  assert.equal(lines.length, channels.length);
  assert.ok(lines.includes("EmailReady"));
  const wa = channels.find((c) => c.channel === "whatsapp");
  assert.ok(lines.includes(`WhatsAppPaused${wa.paused_reason}`), "a paused channel says why, WhatsApp included");
  assert.ok(!lines.some((l) => l.startsWith("WhatsAppReady")), "WhatsApp is under Coming next above, so it is never Ready here");
  assert.ok(lines.includes("SMSNot set up yet") && lines.includes("Your own toolsNot set up yet"));
  assert.ok(text.includes("By hand-off") && text.includes("Sent by Autonomy"));
  // health: each line in words, and the goal's safety stops from /v2/health/limits
  const health = await sample("health.json");
  const limits = await sample("health_limits.json");
  for (const l of connect.HEALTH_LINES) assert.ok(text.includes(l.words), l.key);
  assert.ok(text.includes(`Messages per customer per week${C.num(health.contacts_per_customer_week, 2)}`));
  for (const r of limits.rules.filter((r) => ["complaint_rate", "unsubscribe_multiple", "delivery_rate_below"].includes(r.metric))) assert.ok(text.includes(`Safety stop: ${r.words}`), r.metric);
  assert.ok(text.includes("It paused itself"), "an automatic pause is shown");
});

const HEALTH = { as_of: "2026-10-20T00:00:00Z", delivery_rate: 1, complaint_rate: 0, unsubscribe_rate: 0.0089, block_rate: 0, contacts_per_customer_week: 0.2817, alerts: [] };

test("WhatsApp is 'Coming next' in both places, even where a sender is ready, and a paused one says why", async () => {
  const channels = [
    { channel: "email", mode: "direct", ready: true, paused: false, paused_reason: null, words: "Email: Autonomy sends these itself." },
    { channel: "whatsapp", mode: "direct", ready: true, paused: false, paused_reason: null, words: "WhatsApp: Autonomy sends these itself." },
  ];
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/channels": channels }));
  const root = C.el("div");
  await connect.render(root, ctx);
  const lines = byClass(root, "cn-channel").map((n) => n.textContent);
  assert.ok(lines.includes("EmailReady"));
  assert.ok(lines.some((l) => l.startsWith("WhatsAppComing next")), lines.join(" | "));
  assert.ok(!lines.some((l) => l.startsWith("WhatsAppReady")));
  assert.ok(connect.COMING_NEXT.includes("WhatsApp"), "and the platforms card lists it under Coming next");
  assert.ok(root.textContent.includes("WhatsApp is not ready for production yet"));
  assert.ok(!root.textContent.includes("nothing goes out through a real account"), "a set-up sender does send");
  // paused (for example by delivery failures): Paused and its reason, not Coming next
  const reason = "Paused: too many WhatsApp messages failed to deliver.";
  const paused = [channels[0], { ...channels[1], paused: true, paused_reason: reason }];
  const p = C.el("div");
  await connect.render(p, makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/channels": paused })).ctx);
  const pausedLines = byClass(p, "cn-channel").map((n) => n.textContent);
  assert.ok(pausedLines.includes(`WhatsAppPaused${reason}`), pausedLines.join(" | "));
});

test("Connect and Goal say Simulated shop when the shop is simulated, and only then", async () => {
  const label = (root) => byClass(root, "simulated-label")[0];
  // the sample files
  const a = C.el("div");
  await connect.render(a, makeCtx(mockApi(), "goal_winback").ctx);
  assert.ok(label(a) && label(a).textContent.startsWith("Simulated shop."));
  const b = C.el("div");
  await goal.render(b, makeCtx(mockApi()).ctx);
  assert.ok(label(b) && label(b).textContent.startsWith("Simulated shop."));
  assert.ok(b.textContent.includes(goal.BUDGET_ABOUT), "a suggested budget says what it covers (messages, no discounts)");
  // a server whose only source is the Gym
  const gym = { as_of: "2026-10-20T00:00:00Z", customers: 400, orders: 1200, products: null, sources: [{ name: "gym", kind: "simulation", customers: 400, orders: 1200, events: 5000, updated_at: "2026-10-19T00:00:00Z" }], fixes: [], problems: [], questions: [], doctor_connected: false };
  const c = C.el("div");
  await connect.render(c, makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/data/status": gym, "GET /v2/channels": [] })).ctx);
  assert.ok(label(c));
  // a real shop
  const shop = { ...gym, sources: [{ name: "shopify", kind: "shop", customers: 400, orders: 1200, events: 5000, updated_at: "2026-10-19T00:00:00Z" }] };
  const d = C.el("div");
  await connect.render(d, makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/data/status": shop, "GET /v2/channels": [] })).ctx);
  assert.equal(byClass(d, "simulated-label").length, 0);
  assert.ok(!d.textContent.includes("Simulated shop"));
});

test("Connect on an API without the new routes: says what is not available, never invents it", async () => {
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH }));
  const root = C.el("div");
  await connect.render(root, ctx);
  const text = root.textContent;
  assert.ok(text.includes("Data status is not available here"));
  assert.ok(text.includes("Channel status is not available here"));
  assert.ok(!text.includes("What the Data Doctor fixed") && !text.includes("Waiting for your answer"));
  assert.ok(text.includes("Messages per customer per week0.28"), "rounded to 2 places");
  assert.ok(!text.includes("0.2817"));
  assert.ok(!text.includes("Safety stop"), "no limits without a goal and a limits route");
});

test("Connect when the Data Doctor's report is not linked, on a simulated shop", async () => {
  const report = {
    as_of: "2026-10-20T00:00:00Z",
    customers: 400,
    orders: 1200,
    products: null,
    sources: [{ name: "gym", kind: "simulation", customers: 400, orders: 1200, events: 5000, updated_at: "2026-10-19T00:00:00Z" }],
    fixes: [],
    problems: [],
    questions: [],
    doctor_connected: false,
  };
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/data/status": report, "GET /v2/channels": [] }));
  const root = C.el("div");
  await connect.render(root, ctx);
  const text = root.textContent;
  assert.ok(text.includes("simulated sample shop, not real customers"));
  assert.ok(text.includes("Its report is not linked to Autonomy yet"));
  assert.ok(text.includes("It asks before changing anything"));
  assert.ok(!text.includes("Waiting for your answer"), "no count of questions it cannot see");
  assert.ok(!text.includes("Products in those orders"), "a missing total is left out, not shown as zero");
});

// ---- Goal ---------------------------------------------------------------------------------------------------
test("Goal helpers: the headline, the last day, the reading in words", () => {
  assert.equal(goal.opportunityHeadline("win_back", 1900), "1,900 customers are drifting away.");
  assert.equal(goal.opportunityHeadline("cart_recovery", 1), "1 customer left a cart behind.");
  assert.equal(goal.lastDay("2026-11-01T00:00:00Z"), "31 Oct 2026");
  assert.equal(goal.lastDay(null), null);
  const reading = { budget: 5000, currency: "USD", period: "this month", ends_at: "2026-11-01T00:00:00Z" };
  assert.equal(goal.readingWords(reading, null, "USD"), "We read $5,000 as your budget and 'this month' as the period, so it runs until 31 Oct 2026. Change it if that's wrong.");
  assert.match(goal.readingWords(reading, "300", "USD"), /The Budget box says \$300, and that is what will be used\.$/);
  assert.equal(goal.readingWords({ budget: null, period: null, ends_at: null }, null, "USD"), "");
  assert.equal(goal.EXAMPLES[0].text, "Win back customers who stopped buying, $5,000 this month", "the pitch's own example");
});

test("Goal: the pitch's example fills the budget, says what it read, and the goal keeps the end date", async () => {
  const api = mockApi();
  const { ctx, calls } = makeCtx(api);
  const root = C.el("div");
  await goal.render(root, ctx);
  assert.ok(root.querySelector("textarea").getAttribute("placeholder").includes("$5,000 this month"));
  await byText(root, "button", "Win back lapsed customers")[0].click();
  const budget = root.querySelector("input");
  assert.equal(budget.value, "5000");
  const note = byClass(root, "gl-reading")[0];
  assert.equal(note.hidden, false);
  assert.equal(note.textContent, "We read $5,000 as your budget and 'this month' as the period, so it runs until 31 Oct 2026. Change it if that's wrong.");
  await root.querySelector("form").dispatch("submit");
  await settle();
  assert.equal(calls.navigate.length, 1);
  const made = (await api.listGoals()).find((g) => g.goal_id === calls.navigate[0][1]);
  assert.equal(made.budget, 5000);
  assert.equal(made.currency, "USD");
  assert.equal(made.ends_at, "2026-11-01T00:00:00Z");
  assert.ok(calls.toast.some(([m]) => m.includes("It runs until 31 Oct 2026.")));
});

test("Goal: a budget the marketer typed is never overwritten by the words", async () => {
  const seen = [];
  const api = liveApi(
    {
      "GET /v2/health": HEALTH,
      "GET /v2/goals": [],
      "GET /v2/opportunities/sized": [],
      "POST /v2/goals/read": { text: "x", budget: 5000, currency: "USD", ends_at: "2026-11-01T00:00:00Z", period: "this month", words: "" },
      "POST /v2/goals": { goal: { goal_id: "g_new", ends_at: "2026-11-01T00:00:00Z" }, brief: null },
    },
    seen,
  );
  const { ctx, calls } = makeCtx(api);
  const root = C.el("div");
  await goal.render(root, ctx);
  root.querySelector("input").value = "300";
  root.querySelector("textarea").value = "Win back customers who stopped buying, $5,000 this month";
  await root.querySelector("textarea").dispatch("blur");
  assert.equal(root.querySelector("input").value, "300");
  assert.match(byClass(root, "gl-reading")[0].textContent, /The Budget box says \$300/);
  await root.querySelector("form").dispatch("submit");
  const posted = JSON.parse(seen.find(([k]) => k === "POST /v2/goals")[1]);
  assert.deepEqual(posted, { text: "Win back customers who stopped buying, $5,000 this month", budget: 300, currency: "USD", ends_at: "2026-11-01T00:00:00Z" });
  assert.deepEqual(calls.navigate, [["plan", "g_new"]]);
});

test("Goal: words with no amount and an empty budget ask for one, and send nothing", async () => {
  const api = mockApi();
  const { ctx, calls } = makeCtx(api);
  const root = C.el("div");
  await goal.render(root, ctx);
  await byText(root, "button", "Recover carts")[0].click();
  await root.querySelector("form").dispatch("submit");
  assert.equal(calls.navigate.length, 0);
  assert.match(root.textContent, /Type a budget, or say it in your words/);
  assert.equal(byClass(root, "gl-reading")[0].hidden, true);
});

test("Goal: opportunities show how many customers, why, and 'Want me to handle them?'", async () => {
  const api = mockApi();
  const { ctx, calls } = makeCtx(api);
  const root = C.el("div");
  await goal.render(root, ctx);
  const sized = await sample("opportunities_sized.json");
  const counts = byClass(root, "gl-opp-n").map((n) => n.textContent);
  assert.deepEqual(counts, sized.map((o) => C.int(o.audience_n)));
  const text = root.textContent;
  for (const o of sized) {
    assert.ok(text.includes(`Why: ${o.reason}`), o.reason);
    assert.ok(text.includes(goal.opportunityHeadline(o.goal.kind, o.audience_n).slice(C.int(o.audience_n).length + 1)));
  }
  assert.equal(byText(root, "p", "Want me to handle them?").length, sized.length);
  const yes = byText(root, "button", "Yes, draft a plan");
  assert.equal(yes.length, sized.length);
  await yes[1].click();
  const made = (await api.listGoals()).find((g) => g.goal_id === calls.navigate[0][1]);
  assert.equal(made.kind, sized[1].goal.kind);
  assert.equal(made.budget, sized[1].goal.budget);
});

test("Goal on an API without sized opportunities: no count is made up", async () => {
  const opp = { goal_id: "opp_x", text: "Win back customers who have not bought in two months", kind: "win_back", objective: "gross_profit", budget: 1000, currency: "USD" };
  const api = liveApi({ "GET /v2/health": HEALTH, "GET /v2/goals": [], "GET /v2/opportunities": [opp] });
  const { ctx } = makeCtx(api);
  const root = C.el("div");
  await goal.render(root, ctx);
  assert.equal(byClass(root, "gl-opp-n").length, 0);
  assert.ok(root.textContent.includes(opp.text));
  assert.ok(root.textContent.includes("Want me to handle this?"));
  assert.equal(byText(root, "button", "Yes, draft a plan").length, 1);
});
