// Node checks for the Autonomy shell, api.js mock layer, components and the Connect, Goal and Plan screens.
// No jsdom (the light v2 CI has none): a tiny DOM shim below is enough to draw the screens and click through
// them. Run by tests/v2/ui/test_shell_goal_plan.py, or directly: `node tests/v2/ui/shell_goal_plan.test.mjs`.

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
const { createApi, mockParam, ApiError } = await import(new URL("api.js", UI).href);
const C = await import(new URL("components.js", UI).href);
const { parseHash, hashFor, SCREENS } = await import(new URL("app.js", UI).href);
const goalScreen = await import(new URL("screens/goal.js", UI).href);
const planScreen = await import(new URL("screens/plan.js", UI).href);
const connectScreen = await import(new URL("screens/connect.js", UI).href);

/** fetch for file: URLs (the mock folder) and, optionally, a fake /v2 server. */
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
const mockApi = () => createApi({ mock: true, fetch: fakeFetch(), now: () => new Date("2026-10-20T08:00:00Z") });

function makeCtx(api, goalId) {
  const calls = { navigate: [], toast: [], refreshed: 0 };
  return {
    calls,
    ctx: {
      api,
      goalId,
      goal: null,
      navigate: (...a) => calls.navigate.push(a),
      toast: (m, t) => calls.toast.push([m, t]),
      refreshGoals: async () => {
        calls.refreshed += 1;
      },
    },
  };
}

const byText = (root, tag, text) => root.querySelectorAll(tag).filter((n) => n.textContent.trim() === text);

// ---- components ----------------------------------------------------------------------------------------------
test("money uses the goal currency and an em dash for nothing", () => {
  assert.equal(C.money(5000, "USD"), "$5,000");
  assert.equal(C.money(6, "USD"), "$6.00");
  assert.match(C.money(1500, "EUR"), /€1,500/);
  assert.match(C.money(1500, "INR"), /₹1,500/);
  assert.equal(C.money(null, "USD"), "—");
  assert.equal(C.money(undefined, "USD"), "—");
  assert.equal(C.money(12, "NOPE"), "12 NOPE");
});

test("range always shows low to high, never a bare point", () => {
  const e = { point: 4300, low: 900, high: 7700 };
  assert.equal(C.range(e, "USD"), "$900 – $7,700");
  assert.equal(C.range(e, "USD", { withPoint: true }), "about $4,300 ($900 – $7,700)");
  assert.equal(C.range({ point: 0.38, low: 0.05, high: 0.71 }, null, { format: "pct" }), "5% – 71%");
  assert.equal(C.range(null, "USD"), "—");
  assert.equal(C.range({ point: 1 }, "USD"), "—");
});

test("small formatters", () => {
  assert.equal(C.pct(0.0008), "0.08%");
  assert.equal(C.humanize("win_back"), "Win back");
  assert.equal(C.hour(9), "09:00");
  assert.equal(C.dash(0), "0");
  assert.equal(C.dash(null), "—");
  assert.equal(C.when("not a date"), "—");
  const slots = C.slotted("Hi {{first_name}}, new in {{ top_category }}.");
  assert.equal(slots.filter((s) => s instanceof Element).length, 2);
});

test("el sets text as text, never as markup", () => {
  const n = C.el("p", { class: ["a", false, "b"], "aria-label": "x", hidden: false }, "<b>hi</b>", null, [1, 2]);
  assert.equal(n.textContent, "<b>hi</b>12");
  assert.equal(n.className, "a b");
  assert.equal(n.hasAttribute("hidden"), false);
});

// ---- descriptions, the page head, the segmented dial ---------------------------------------------------------
test("withInfo: the words sit in the DOM as a hidden tooltip, linked to the target, opened on hover and tap", async () => {
  const label = C.el("span", {}, "Extra profit");
  const host = C.withInfo(label, "Orders from people we contact, minus what the same number of people we never contact bought.");
  const tip = host.querySelector(".info-tip");
  assert.ok(host.className.includes("info-host"));
  assert.equal(tip.getAttribute("role"), "tooltip");
  assert.equal(tip.hidden, true, "closed until asked: no visible words");
  assert.equal(label.getAttribute("aria-describedby"), tip.id);
  assert.equal(label.getAttribute("tabindex"), "0", "a label becomes reachable by keyboard");
  assert.ok(label.className.includes("has-info"));
  assert.ok(host.textContent.includes("people we never contact"), "tests and screen readers find the words");
  await label.dispatch("pointerenter", { pointerType: "mouse" });
  assert.equal(tip.hidden, true, "not before the hover delay");
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(tip.hidden, false, "open on hover after ~150 ms");
  await label.dispatch("pointerleave", { pointerType: "mouse" });
  assert.equal(tip.hidden, true);
  await label.dispatch("click", { pointerType: "touch" });
  assert.equal(tip.hidden, false, "a tap opens it");
  await label.dispatch("click", { pointerType: "touch" });
  assert.equal(tip.hidden, true, "a second tap closes it");
  await label.dispatch("click", { pointerType: "touch" });
  await label.dispatch("keydown", { key: "Escape" });
  assert.equal(tip.hidden, true, "Esc closes it");
  assert.equal(C.withInfo(label, ""), label, "no words, no description");
});

test("info: a quiet (i) button holding the description; on a button the action keeps the click", async () => {
  const node = C.info("Counted from the orders data.", { label: "How this is counted" });
  const dot = node.querySelector(".info-dot");
  assert.equal(dot.tagName, "BUTTON");
  assert.equal(dot.getAttribute("aria-label"), "How this is counted");
  await dot.dispatch("click", { pointerType: "mouse" });
  assert.equal(node.querySelector(".info-tip").hidden, false, "a click on the (i) opens it");
  await dot.dispatch("keydown", { key: "Escape" });
  assert.equal(C.info(null), null);
  const pause = C.button("Pause", null);
  C.withInfo(pause, "Stops all sending.");
  assert.equal(pause.getAttribute("tabindex"), null, "a button is focusable already");
  assert.equal(pause.listeners.click, undefined, "no tap toggle on a button: the action wins");
});

test("pageHead: title with its status pills, one line, the action on the right, the rest in a description", () => {
  const head = C.pageHead({ title: "Proof", sub: "Did it make money?", status: [C.pill("Running", "good")], actions: [C.button("Share", null)], info: "How Proof is measured." });
  assert.ok(head.className.includes("page-head"));
  const row = head.querySelector(".page-title-row");
  assert.equal(row.querySelector("h1").textContent, "Proof");
  assert.ok(row.querySelector(".page-status").textContent.includes("Running"));
  assert.ok(row.querySelector(".info-tip").textContent.includes("How Proof is measured."));
  assert.equal(head.querySelector(".page-sub").textContent, "Did it make money?");
  assert.ok(head.querySelector(".page-actions").textContent.includes("Share"));
});

test("segmented: one sentence under the track for the chosen option; every option's sentence in a description", async () => {
  const options = [
    { value: "shadow", label: "Shadow", hint: "Recommended to start", text: "Sends nothing." },
    { value: "approve", label: "Approve", text: "You approve each batch." },
    { value: "autopilot", label: "Autopilot", text: "Acts on its own." },
  ];
  const chosen = [];
  const field = C.segmented({ legend: "Level", options, value: "approve", onChange: (v) => chosen.push(v) });
  const note = field.querySelector(".segmented-note");
  assert.equal(note.textContent, "You approve each batch.");
  const tips = field.querySelectorAll(".info-tip");
  assert.equal(tips.length, 3);
  assert.ok(tips.every((t) => t.hidden && t.getAttribute("role") === "tooltip"));
  for (const o of options) assert.ok(field.textContent.includes(o.text), o.value);
  assert.ok(tips[0].textContent.startsWith("Recommended to start. "));
  const inputs = field.querySelectorAll("input");
  assert.equal(inputs[0].getAttribute("aria-describedby"), tips[0].id);
  inputs[2].checked = true;
  await inputs[2].dispatch("change");
  assert.equal(note.textContent, "Acts on its own.", "the sentence follows the choice");
  assert.deepEqual(chosen, ["autopilot"]);
});

// ---- routing -------------------------------------------------------------------------------------------------
test("hash routes", () => {
  assert.deepEqual(parseHash("#/plan/goal_1"), { screen: "plan", goalId: "goal_1" });
  assert.deepEqual(parseHash("#/goal"), { screen: "goal", goalId: null });
  assert.deepEqual(parseHash(""), { screen: null, goalId: null });
  assert.deepEqual(parseHash("#/live/a%20b"), { screen: "live", goalId: "a b" });
  assert.equal(hashFor("proof", "g 1"), "#/proof/g%201");
  assert.equal(hashFor("goal", "g1"), "#/goal");
  assert.equal(hashFor("operator", "g1"), "#/operator/g1");
  assert.deepEqual(
    SCREENS.map((s) => s.id),
    ["connect", "goal", "plan", "live", "proof"],
  );
});

// ---- api.js ----------------------------------------------------------------------------------------------------
test("?mock= is read", () => {
  assert.equal(mockParam("?mock=1"), true);
  assert.equal(mockParam("?mock=0"), false);
  assert.equal(mockParam(""), null);
});

test("an unreachable /v2 falls back to the mock files and says why", async () => {
  const api = createApi({ fetch: fakeFetch(), mock: undefined });
  const seen = [];
  api.onModeChange((m) => seen.push(m.mode));
  const goals = await api.listGoals();
  assert.equal(api.mode, "mock");
  assert.match(api.reason, /could not be reached/);
  assert.deepEqual(seen, ["mock"]);
  assert.ok(goals.length >= 1);
});

test("a live /v2 is used, and its errors are errors, not a fallback", async () => {
  const seen = [];
  const server = (url, init) => {
    seen.push([init.method || "GET", url, init.body]);
    if (url.endsWith("/v2/health")) return json(200, { as_of: "2026-10-20T00:00:00Z", alerts: [] });
    if (url.endsWith("/v2/goals") && init.method === "GET") return json(200, [{ goal_id: "real" }]);
    if (url.endsWith("/v2/goals") && init.method === "POST") return json(200, { goal: { goal_id: "new" }, brief: null });
    return json(404, { detail: "Goal not found" });
  };
  const api = createApi({ base: "", fetch: fakeFetch(server), mock: undefined });
  assert.deepEqual(await api.listGoals(), [{ goal_id: "real" }]);
  assert.equal(api.mode, "live");
  await api.createGoal({ text: "x", budget: "250", currency: "EUR" });
  assert.deepEqual(JSON.parse(seen.at(-1)[2]), { text: "x", budget: 250, currency: "EUR" });
  await assert.rejects(api.getGoal("missing"), (e) => e instanceof ApiError && e.status === 404 && e.message === "Goal not found");
  assert.equal(api.mode, "live");
  await api.listDecisions({ goalId: "g", limit: 5 }).catch(() => null);
  assert.equal(seen.at(-1)[1], "/v2/decisions?goal_id=g&limit=5");
});

test("requests made at once share one probe: none reaches a server that has no /v2", async () => {
  const hits = [];
  const server = async (url) => {
    hits.push(url);
    await new Promise((r) => setTimeout(r, 5)); // a slow answer, so the second call starts mid-probe
    return { ok: false, status: 404, text: async () => "<html>", headers: { get: () => "text/html" } };
  };
  const api = createApi({ base: "", fetch: fakeFetch(server), mock: undefined });
  const [goals, opps] = await Promise.all([api.listGoals(), api.listOpportunities()]);
  assert.equal(api.mode, "mock");
  assert.ok(goals.length >= 1 && opps.length >= 1, "both answered from the mock files, neither threw a 404");
  assert.deepEqual(hits, ["/v2/health"], "only the probe went to the server");
});

test("a server without /v2 (404 on health) means mock", async () => {
  const api = createApi({ fetch: fakeFetch(() => ({ ok: false, status: 404, text: async () => "<html>", headers: { get: () => "text/html" } })), mock: undefined });
  assert.equal(await api.init(), "mock");
});

test("mock writes behave for the page load and never invent results", async () => {
  const api = mockApi();
  const made = await api.createGoal({ text: "Grow repeat purchases", budget: 1200, currency: "gbp" });
  assert.equal(made.goal.status, "draft");
  assert.equal(made.goal.currency, "GBP");
  assert.equal(made.brief.goal_id, made.goal.goal_id);
  assert.equal(made.brief.envelope.budget, 1200);
  assert.equal(made.brief.approved_at, null);
  const detail = await api.getGoal(made.goal.goal_id);
  assert.equal(detail.proof, null);
  assert.equal(await api.getProof(made.goal.goal_id), null);
  assert.equal(await api.getBriefing(made.goal.goal_id), null);
  await assert.rejects(api.pauseGoal(made.goal.goal_id), (e) => e.status === 409);
  const approved = await api.approveGoal(made.goal.goal_id, "autopilot");
  assert.equal(approved.goal.autonomy, "autopilot");
  assert.equal(approved.goal.status, "active");
  assert.ok(approved.brief.approved_at);
  const changed = [];
  api.onGoalChange((g) => changed.push(g.status));
  assert.equal((await api.pauseGoal(made.goal.goal_id)).status, "paused");
  assert.equal((await api.resumeGoal(made.goal.goal_id)).status, "active");
  assert.deepEqual(changed, ["paused", "active"], "the shell hears about pauses made on any screen");
  const item = detail.content[0];
  assert.equal((await api.setContentStatus(item.content_id, "rejected")).status, "rejected");
  assert.equal((await api.getContent(made.goal.goal_id))[0].status, "rejected");
  await assert.rejects(api.createGoal({ text: "  ", budget: 1 }), (e) => e.status === 422);
  await assert.rejects(api.getGoal("nope"), (e) => e.status === 404);
  assert.equal((await api.listOpportunities()).length >= 1, true);
});

// ---- screens --------------------------------------------------------------------------------------------------
test("Goal screen: the big box, the examples, the opportunities, and starting one", async () => {
  const api = mockApi();
  const { ctx, calls } = makeCtx(api, "goal_winback");
  const root = C.el("div");
  await goalScreen.render(root, ctx);
  const text = root.textContent;
  assert.match(text, /What do you want to achieve\?/);
  for (const ex of ["Win back lapsed customers", "Grow repeat purchases", "Recover carts"]) assert.equal(byText(root, "button", ex).length, 1, ex);
  const opps = await api.listOpportunities();
  for (const o of opps) assert.ok(text.includes(o.text), o.text);
  const starts = byText(root, "button", "Yes, draft a plan");
  assert.equal(starts.length, opps.length);
  await starts[0].click();
  assert.equal(calls.navigate.length, 1);
  assert.equal(calls.navigate[0][0], "plan");
  const created = (await api.listGoals()).find((g) => g.goal_id === calls.navigate[0][1]);
  assert.equal(created.text, opps[0].text);
  assert.equal(created.budget, opps[0].budget);
  assert.equal(created.kind, opps[0].kind, "starting an opportunity keeps the kind the Analyst found");
  assert.equal(created.objective, opps[0].objective);
  assert.equal((await api.getGoal(created.goal_id)).brief.objective, opps[0].objective);
});

test("Goal screen form: refuses an empty goal, then submits words, budget and currency", async () => {
  const api = mockApi();
  const { ctx, calls } = makeCtx(api, null);
  const root = C.el("div");
  await goalScreen.render(root, ctx);
  const form = root.querySelector("form");
  await form.dispatch("submit");
  assert.equal(calls.navigate.length, 0);
  assert.match(root.querySelector("textarea").getAttribute("aria-invalid"), /true/);
  byText(root, "button", "Recover carts")[0].click();
  root.querySelector("input").value = "750";
  root.querySelector("select").value = "EUR";
  await form.dispatch("submit");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.navigate.length, 1);
  const goal = (await api.listGoals()).find((g) => g.goal_id === calls.navigate[0][1]);
  assert.equal(goal.budget, 750);
  assert.equal(goal.currency, "EUR");
  assert.equal(goal.text, goalScreen.EXAMPLES[2].text);
});

test("Plan screen: the brief in plain words, the messages, the dial", async () => {
  const api = mockApi();
  const { ctx } = makeCtx(api, "goal_winback");
  const root = C.el("div");
  await planScreen.render(root, ctx);
  const text = root.textContent;
  for (const s of [
    "The plan in plain words",
    "Bought before, nothing in the last 60 days",
    "Free shipping",
    "10% off",
    "Email, WhatsApp",
    "2 per customer",
    "from 21:00 to 09:00",
    "$5,000",
    "If complaints go above 0.08% of delivered messages, pause the whole goal.",
    "If delivery falls below 95%, pause that channel.",
    "Lapsed buyers respond to a reminder",
    "Shadow",
    "Approve each batch",
    "Autopilot",
  ]) assert.ok(text.includes(s), s);
  for (const a of planScreen.AUTONOMY) assert.ok(text.includes(a.text), a.value);
  // The plain agent sends no later step, so the plan does not list "After 3 days, follow up" as something that will happen.
  assert.ok(!text.includes("follow up on the channel that suits them best"));
  assert.ok(text.includes("Follow-ups") && text.includes("Not sent"));
  const detail = await api.getGoal("goal_winback");
  assert.equal(root.querySelectorAll("article").length, detail.content.length);
  assert.ok(text.includes("Save autonomy level"), "an approved plan offers to change the level");
  // a draft the editor has not checked has no Approve button (the server refuses it); it can be turned off from its card
  const draft = detail.content.find((c) => c.status === "draft");
  const buttons = root.querySelectorAll("button");
  assert.ok(!buttons.some((b) => b.getAttribute("aria-label") === `Approve message ${draft.content_id}`));
  const rejectBtn = buttons.find((b) => b.getAttribute("aria-label") === `Reject message ${draft.content_id}`);
  await rejectBtn.click();
  assert.equal((await api.getContent("goal_winback")).find((c) => c.content_id === draft.content_id).status, "rejected");
});

test("Plan screen: approving a new plan with the chosen autonomy", async () => {
  const api = mockApi();
  const made = await api.createGoal({ text: "Win back", budget: 300, currency: "USD" });
  const { ctx, calls } = makeCtx(api, made.goal.goal_id);
  const root = C.el("div");
  await planScreen.render(root, ctx);
  assert.ok(root.textContent.includes("Nothing is sent until you approve."));
  const radios = root.querySelectorAll("input");
  for (const r of radios) r.checked = r.getAttribute("value") === "autopilot";
  await byText(root, "button", "Approve plan")[0].click();
  const goal = (await api.listGoals()).find((g) => g.goal_id === made.goal.goal_id);
  assert.equal(goal.autonomy, "autopilot");
  assert.equal(goal.status, "active");
  assert.ok(calls.toast.some(([m]) => m.startsWith("Plan approved")));
  assert.ok(calls.refreshed >= 1);
});

test("plain-words helpers", () => {
  assert.equal(planScreen.filterWords({ field: "last_purchase_at", op: "days_since_gt", value: 60 }), "Last purchase at: more than 60 days ago");
  assert.equal(planScreen.filterWords({ field: "purchases", op: "gte", value: 1 }), "Purchases: at least 1");
  assert.equal(planScreen.stopRuleWords({ metric: "spend_pace", threshold: 1.2, action: "pause_goal" }), "If spending runs at 1.2× the planned pace, pause the whole goal.");
  assert.equal(planScreen.stopRuleWords({ metric: "whatsapp_quality", threshold: 1, action: "pause_channel" }), "If the WhatsApp quality rating turns yellow, pause that channel.");
  assert.equal(planScreen.followUpWords({ after_days: 1, when: "no_response", channel: "email" }), "After 1 day, if they have not responded, follow up by Email.");
});

test("Connect screen: explains, links to the Data Doctor, and shows what the Data Doctor reports", async () => {
  const { ctx } = makeCtx(mockApi(), null);
  const root = C.el("div");
  await connectScreen.render(root, ctx);
  const text = root.textContent;
  assert.match(text, /Bring your data in/);
  assert.ok(root.querySelectorAll("a").some((a) => a.getAttribute("href") === "/" && a.textContent === "Open the Data Doctor"));
  // The sample report (mock/data_status.json) is shown; the "not available" wording is for an API without it
  // (covered in tests/v2/promise/connect_goal.test.mjs).
  assert.match(text, /What the Data Doctor fixed/);
  assert.match(text, /Waiting for your answer \(1\)/);
});

// ---- the shell itself ------------------------------------------------------------------------------------------
async function until(check, what) {
  for (let i = 0; i < 200; i += 1) {
    if (check()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.fail(`timed out waiting for ${what}`);
}

test("the shell boots in mock mode: bar, nav, switcher, banner, a screen, one-click pause", async () => {
  // The elements index.html gives the shell, by id.
  const ids = ["skip-link:a", "goal-select:select", "pause-button:button", "inbox-link:a", "inbox-count:span", "nav-list:ol", "operator-link:a", "mode-banner:div", "main:main"];
  document.body.replaceChildren(...ids.map((x) => C.el(x.split(":")[1], { id: x.split(":")[0] })));
  const hashListeners = [];
  globalThis.location = { hash: "#/plan/goal_winback", search: "?mock=1" };
  globalThis.history = { replaceState: (_s, _t, h) => (globalThis.location.hash = h) };
  globalThis.window = globalThis;
  globalThis.addEventListener = (type, fn) => type === "hashchange" && hashListeners.push(fn);
  globalThis.fetch = fakeFetch();
  const go = async (hash) => {
    globalThis.location.hash = hash;
    await Promise.all(hashListeners.map((fn) => fn()));
  };
  try {
    const shell = await import(new URL("app.js?shell-test", UI).href);
    const $ = (id) => document.getElementById(id);
    await until(() => $("main").textContent.includes("The plan in plain words"), "the Plan screen");

    const nav = $("nav-list").querySelectorAll("a");
    assert.deepEqual(nav.map((a) => a.textContent.slice(1)), ["Connect", "Goal", "Plan", "Live", "Proof"]);
    assert.equal(nav.find((a) => a.getAttribute("aria-current") === "page").getAttribute("href"), "#/plan/goal_winback");
    assert.equal(nav[3].getAttribute("href"), "#/live/goal_winback");
    assert.equal($("operator-link").getAttribute("href"), "#/operator/goal_winback");
    const goals = JSON.parse(await readFile(fileURLToPath(new URL("mock/goals.json", UI)), "utf8"));
    assert.equal($("goal-select").querySelectorAll("option").length, goals.length);
    assert.equal($("mode-banner").hidden, false);
    assert.match($("mode-banner").textContent, /Sample data/);
    assert.equal(document.title, "Plan · Autonomy");

    // The skip link moves focus to the content without changing the address (which the router would call
    // an unknown page).
    let focused = 0;
    $("main").focus = () => {
      focused += 1;
    };
    let prevented = false;
    await $("skip-link").dispatch("click", { preventDefault: () => (prevented = true) });
    assert.equal(prevented, true);
    assert.equal(focused, 1);
    assert.equal(globalThis.location.hash, "#/plan/goal_winback");

    const pause = $("pause-button");
    assert.equal(pause.textContent, "Pause");
    assert.equal(pause.disabled, false);
    await pause.click();
    await until(() => pause.textContent === "Resume", "the Resume button");
    await pause.click();
    await until(() => pause.textContent === "Pause", "the Pause button again");
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(focused, 1, "pausing redraws the screen but leaves focus on the button");

    await go("#/nowhere");
    assert.match($("main").textContent, /There is no such page/);
    await go("#/goal");
    await until(() => $("main").textContent.includes("What do you want to achieve?"), "the Goal screen");
    await go("#/plan");
    await until(() => globalThis.location.hash === "#/plan/goal_winback", "the goal id put into the address");
    assert.equal(typeof shell.navigate, "function");
  } finally {
    delete globalThis.window;
    delete globalThis.location;
    delete globalThis.history;
  }
});

test("follow-ups are listed only when the server sends them (journeys on)", () => {
  const steps = [{ after_days: 3, when: "no_purchase", channel: null }];
  const text = (n) => (typeof n === "string" ? n : n.textContent);
  assert.match(text(planScreen.followUpsFact(steps, { follow_ups_run: true })), /After 3 days, if they have not bought, follow up/);
  assert.equal(text(planScreen.followUpsFact([], { follow_ups_run: true })), "None");
  for (const facts of [{ follow_ups_run: false }, {}, null]) {
    const shown = text(planScreen.followUpsFact(steps, facts));
    assert.ok(shown.startsWith("Not sent"), JSON.stringify(facts));
    assert.ok(!shown.includes("After 3 days, if they have not bought, follow up"), "a follow-up that never goes out is not promised");
  }
});
