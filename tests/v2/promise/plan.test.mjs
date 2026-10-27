// Node checks for the Plan screen against the promises it carries (docs/v2/PROMISE_MAP.md rows 15-26): who it
// will contact (with the counts the API sent), offers, channels, how often, quiet hours and budget, the rules it
// can never break, when it pauses itself, the messages with the strict editor's checklist ("nothing goes out
// without passing"), pictures, the product fact labelled as one, and "you approve once". Every number checked
// here is one the sample files or the fake server sent. Run by tests/v2/promise/test_plan.py, or directly:
// `node tests/v2/promise/plan.test.mjs`.

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
const plan = await import(new URL("screens/plan.js", UI).href);

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
const articles = (root) => root.querySelectorAll("article");
const cardFor = (root, contentId) =>
  articles(root).find((a) => a.querySelectorAll("button").some((b) => (b.getAttribute("aria-label") || "").endsWith(` ${contentId}`))) || null;
/** A message's card by its content id (the cards that need a look come first, so the order is not the API's). */
const cardById = (root, contentId) => articles(root).find((a) => a.getAttribute("data-content-id") === contentId) || null;
/** The "Checked by the editor" group a card sits in, or null for a card that carries its own editor line. */
const groupOf = (root, art) => byClass(root, "message-group").find((g) => g.querySelectorAll("article").includes(art)) || null;
/** What a person sees without opening a description: the text with every `role="tooltip"` box left out. */
const visibleText = (node) =>
  node.childNodes.map((n) => (n instanceof Element ? (n.getAttribute("role") === "tooltip" || n.hidden ? "" : visibleText(n)) : n.textContent)).join("");

// ---- with the sample files (mock mode) ---------------------------------------------------------------------

test("Plan (sample data): who it will contact, counted by the API, in words for people", async () => {
  const facts = await sample("plan.json");
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const text = root.textContent;
  for (const w of facts.audience_words) assert.ok(text.includes(w), w);
  assert.ok(!text.includes("Last purchase at:"), "the filters read for people, not as field names");
  assert.ok(text.includes(`${C.int(facts.audience_n)} customers match this today.`));
  assert.ok(text.includes(`${C.int(facts.holdout_n)} of them are people we never contact`));
  assert.ok(text.includes(`${C.int(facts.baseline_n)} get a simple fixed rule`));
  assert.ok(text.includes(`It looks after the other ${C.int(facts.agent_n)} one by one.`));
  // "people we never contact", never the word "holdout" (DESIGN.md section 6)
  assert.ok(text.includes("are people we never contact"));
  assert.ok(!/holdout/i.test(text), "the screen never says holdout");
  // offers, channels, how often, quiet hours and budget (row 17)
  for (const s of ["Offers it may use", "Free shipping", "10% off", "Email, WhatsApp", "2 per customer per week", "from 21:00 to 09:00", "$5,000"]) assert.ok(text.includes(s), s);
});

test("Plan (sample data): every rule it can never break, and when it pauses itself", async () => {
  const facts = await sample("plan.json");
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const rules = byClass(root, "plan-rules")[0];
  assert.ok(rules, "the card is there");
  assert.ok(rules.textContent.includes("Rules it can never break"));
  for (const r of facts.hard_rules) assert.ok(rules.textContent.includes(r.words), r.code);
  // each rule shows by name; its own words, with the plan's value, are that name's description
  for (const r of facts.hard_rules) {
    const tip = rules.querySelectorAll("span").find((s) => s.getAttribute("role") === "tooltip" && s.textContent === r.words);
    assert.ok(tip, `${r.code} has its words in a description`);
  }
  const names = rules.querySelectorAll(".rule-name").map((n) => n.textContent);
  for (const name of ["Consent", "Unsubscribes", "Quiet hours", "Weekly limit", "Budget", "Complaints", "No-discount requests", "People we never contact"]) assert.ok(names.includes(name), name);
  const stops = byClass(root, "plan-stops")[0];
  assert.ok(stops.textContent.includes("When it pauses itself"));
  assert.ok(stops.textContent.includes("If complaints go above 0.08% of delivered messages, pause the whole goal."));
  assert.ok(stops.textContent.includes("If unsubscribes reach 3× your usual rate, pause the whole goal."));
});

test("Plan (sample data): the editor's checklist on every message, and nothing goes out without passing", async () => {
  const facts = await sample("plan.json");
  const detail = await sample("goal_detail.json");
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const text = root.textContent;
  assert.equal(articles(root).length, detail.content.length);
  assert.ok(text.includes("Nothing goes out without passing."));
  assert.ok(
    text.includes(`${facts.editor_passed} passed the editor; ${facts.editor_failed} failed and will never be sent; ${facts.not_reviewed} not checked by the editor yet.`),
    "the editor's counts, from the API",
  );
  assert.ok(text.includes("turning one off is optional"), "the cards do not read as more approvals");
  const reviews = new Map(facts.content.map((r) => [r.content_id, r]));
  detail.content.forEach((item) => {
    const art = cardById(root, item.content_id);
    const review = reviews.get(item.content_id);
    if (review.passed === null) {
      assert.ok(art.textContent.includes("Not checked by the editor yet"), item.content_id);
      return;
    }
    // A message the editor passed on every check sits under the group's ONE "Checked by the editor" line, whose
    // description holds the checklist; any other reviewed message carries its own line and checklist.
    const group = groupOf(root, art);
    assert.equal(Boolean(group), plan.passedEveryCheck(review, true), `${item.content_id} grouped only when every check passed`);
    const head = group ? byClass(group, "message-group-head")[0] : art;
    assert.ok(head.textContent.includes("Checked by the editor"), item.content_id);
    for (const c of plan.CHECKS) assert.ok(head.textContent.includes(c.words), `${item.content_id} ${c.check}`);
    if (group) assert.ok(!visibleText(art).includes("Checked by the editor"), `${item.content_id} does not repeat the group's line`);
    // the raw notes are kept, folded under "Editor's notes"
    const fold = art.querySelector("details");
    assert.ok(fold && fold.textContent.includes("Editor's notes") && fold.textContent.includes(item.critic_notes[0]));
  });
  // the message the editor failed: the failed check with its reason, and no way to approve it
  const failed = facts.content.find((r) => r.passed === false);
  const art = cardById(root, failed.content_id);
  assert.ok(art.textContent.includes("Failed the editor: never sent"));
  const bad = failed.checks.find((c) => c.passed === false);
  assert.ok(art.textContent.includes(bad.note));
  assert.ok(art.querySelectorAll(".check-fail").length >= 1);
  assert.ok(!art.querySelectorAll("button").some((b) => (b.getAttribute("aria-label") || "").startsWith("Approve")), "no approve button");
  assert.ok(art.textContent.includes("a message the editor failed is never sent"));
  // ... said once on the card (the pill), with the why in the pill's description, not four times
  assert.equal(visibleText(art).match(/never (be )?sent/gi).length, 1, visibleText(art));
  const pillTip = art.querySelectorAll("span").find((n) => n.getAttribute("role") === "tooltip" && n.textContent.includes("There is no Approve button"));
  assert.ok(pillTip, "the no-Approve sentence is the pill's description");
  // WhatsApp template names are shown (and wrap on a phone: plan.css)
  assert.ok(text.includes("WhatsApp template winback_6"));
});

test("Plan (sample data): a message the editor has not checked cannot be approved; the server's rule, kept by the screen", async () => {
  const api = mockApi();
  const facts = await sample("plan.json");
  const { ctx } = makeCtx(api, "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const ready = (await api.getContent("goal_winback")).filter((c) => c.status === "approved").length;
  assert.ok(root.textContent.includes(`${ready} ready to send`));
  const unchecked = facts.content.find((r) => r.passed === null);
  const art = cardFor(root, unchecked.content_id);
  const labels = art.querySelectorAll("button").map((b) => b.getAttribute("aria-label") || "");
  assert.ok(!labels.some((l) => l.startsWith("Approve")), "no Approve button before the editor has passed it");
  assert.ok(labels.includes(`Reject message ${unchecked.content_id}`), "it can still be turned off");
  // the mock API answers as the server does (api/v2/deps.py set_content_status): 409, and nothing changes
  await assert.rejects(api.setContentStatus(unchecked.content_id, "approved"), (err) => err.status === 409 && /editor/.test(err.message));
  assert.equal((await api.getContent("goal_winback")).find((c) => c.content_id === unchecked.content_id).status, "draft");
  const failed = facts.content.find((r) => r.passed === false);
  await assert.rejects(api.setContentStatus(failed.content_id, "approved"), (err) => err.status === 409);
  assert.equal((await api.getContent("goal_winback")).filter((c) => c.status === "approved").length, ready, "the count of ready messages did not move");
});

test("Plan (sample data): a message the editor passed can be turned off and used again", async () => {
  const api = mockApi();
  const facts = await sample("plan.json");
  const { ctx } = makeCtx(api, "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const ready = (await api.getContent("goal_winback")).filter((c) => c.status === "approved").length;
  const passed = facts.content.find((r) => r.passed === true);
  await api.setContentStatus(passed.content_id, "rejected");
  assert.equal((await api.getContent("goal_winback")).filter((c) => c.status === "approved").length, ready - 1);
  const again = await api.setContentStatus(passed.content_id, "approved");
  assert.equal(again.status, "approved");
  assert.equal((await api.getContent("goal_winback")).filter((c) => c.status === "approved").length, ready);
});

test("Plan (sample data): says Simulated shop before the plan", async () => {
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const label = root.querySelectorAll(".simulated-label")[0];
  assert.ok(label && label.textContent.startsWith("Simulated shop."));
  assert.ok(root.textContent.indexOf("Simulated shop.") < root.textContent.indexOf("The plan in plain words"));
});

test("Plan (sample data): products it may suggest, and the product fact labelled as one", async () => {
  const data = await sample("data_status.json");
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const box = byClass(root, "plan-products")[0];
  assert.ok(box.textContent.includes(`Your order data names ${C.int(data.products)} products.`));
  const factBox = byClass(root, "product-fact")[0];
  assert.ok(factBox.textContent.includes("How we tested this"));
  assert.ok(factBox.textContent.includes("not a result from your shop"));
  assert.ok(factBox.textContent.includes(plan.CATALOG_DOC));
  // the general product numbers appear only inside the labelled fact, never as this shop's numbers
  const outside = root.textContent.replace(factBox.textContent, "");
  for (const n of ["58%", "34.7%", "22.0%"]) {
    assert.ok(factBox.textContent.includes(n), n);
    assert.ok(!outside.includes(n), `${n} outside the product fact`);
  }
});

test("Plan (sample data): you approve once, with the dial and Shadow recommended", async () => {
  const { ctx } = makeCtx(mockApi(), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const dial = byClass(root, "dial-card")[0];
  assert.ok(dial.textContent.includes("You approve once"));
  for (const a of plan.AUTONOMY) assert.ok(dial.textContent.includes(a.text), a.value);
  assert.ok(dial.textContent.includes("Recommended to start"));
  assert.ok(dial.textContent.includes("It has not yet run with a real business, so start in Shadow"));
  assert.ok(dial.querySelectorAll("a").some((a) => a.getAttribute("href") === "#/start"));
  assert.ok(dial.textContent.includes("At this level since 1 Oct 2026."));
  // each radio has a label
  for (const input of dial.querySelectorAll("input")) assert.ok(dial.querySelectorAll("label").some((l) => l.getAttribute("for") === input.id));
});

// ---- against an API -----------------------------------------------------------------------------------------

/** The API answers its health probe; without it api.js falls back to the sample files. */
const HEALTH = { as_of: "2026-10-20T00:00:00Z", delivery_rate: 1, complaint_rate: 0, unsubscribe_rate: 0, block_rate: 0, contacts_per_customer_week: 0, alerts: [] };

test("Plan on an API without the plan facts: no count is made up, the rules come from the plan's own values", async () => {
  const detail = await sample("goal_detail.json");
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/goals/goal_winback": detail }), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const text = root.textContent;
  assert.ok(text.includes("The plan in plain words"));
  assert.ok(text.includes("How many customers match is not available from this server yet."));
  assert.ok(!text.includes("customers match this today"));
  assert.ok(!/\d+ of them are people we never contact/.test(text));
  const rules = byClass(root, "plan-rules")[0].textContent;
  for (const s of ["Only messages people who agreed", "Never messages anyone who unsubscribed", "between 21:00 and 09:00", "At most 2 messages per person per week", "budget of $5,000", "open complaint"]) assert.ok(rules.includes(s), s);
  assert.ok(text.includes("The editor's checklist is not available from this server"));
  assert.ok(!text.includes("passed the editor;"), "no editor counts it was not sent");
  assert.ok(text.includes("How many products your order data names is not available from this server."));
});

test("Plan: a picture is shown with alt text when a message has one, and said to be missing when it does not", async () => {
  const detail = await sample("goal_detail.json");
  const facts = await sample("plan.json");
  const base = detail.content[0];
  const withImage = { ...base, content_id: "ci_img", format: "text_image", asset_url: "/assets/serum.jpg" };
  const noImage = { ...base, content_id: "ci_noimg", format: "text_image", asset_url: null };
  const body = { ...detail, content: [withImage, noImage] };
  const planFacts = { ...facts, content: [] };
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/goals/goal_winback": body, "GET /v2/goals/goal_winback/plan": planFacts }), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  const [first, second] = articles(root);
  const img = first.querySelector("img");
  assert.ok(img, "a thumbnail");
  assert.equal(img.getAttribute("src"), "/assets/serum.jpg");
  assert.match(img.getAttribute("alt"), /^Picture sent with the Email message about /);
  assert.ok(!second.querySelector("img"));
  assert.ok(second.textContent.includes("has room for a picture, but none is attached yet"));
});

test("Plan: a message the editor passed that still waits for you is never folded away, and leads the group", async () => {
  const detail = await sample("goal_detail.json");
  const facts = await sample("plan.json");
  const base = detail.content.find((c) => c.status === "approved");
  const passed = facts.content.find((r) => r.passed === true);
  // eight messages the editor passed on every check: seven ready, and the last one a draft waiting for you
  const content = Array.from({ length: 8 }, (_, i) => ({ ...base, content_id: `cx_${i}`, status: i === 7 ? "draft" : "approved" }));
  const reviews = content.map((c) => ({ ...passed, content_id: c.content_id }));
  const { ctx } = makeCtx(
    liveApi({ "GET /v2/health": HEALTH, "GET /v2/goals/goal_winback": { ...detail, content }, "GET /v2/goals/goal_winback/plan": { ...facts, content: reviews } }),
    "goal_winback",
  );
  const root = C.el("div");
  await plan.render(root, ctx);
  const groups = byClass(root, "message-group");
  assert.equal(groups.length, 1, "one group");
  const inGroup = groups[0].querySelectorAll("article");
  assert.equal(inGroup.length, 8, "every message passed on every check sits under the one line");
  assert.equal(inGroup[0].getAttribute("data-content-id"), "cx_7", "the one waiting for you comes first");
  assert.ok(!inGroup[0].hidden, "and it is never folded away");
  assert.ok(visibleText(inGroup[0]).includes("Passed the editor, waiting for you"), "its own state still shows");
  const shown = inGroup.filter((a) => !a.hidden).map((a) => a.getAttribute("data-content-id"));
  assert.deepEqual(shown, ["cx_7", "cx_0", "cx_1"], "then the first two quiet ones");
  assert.equal(byText(groups[0], "span", "Checked by the editor").length, 1, "the line is said once");
});

test("Plan on a simulated shop with no product names: says it cannot suggest one", async () => {
  const detail = await sample("goal_detail.json");
  const report = { as_of: "2026-10-20T00:00:00Z", customers: 400, orders: 570, products: null, sources: [], fixes: [], problems: [], questions: [], doctor_connected: false };
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH, "GET /v2/goals/goal_winback": detail, "GET /v2/data/status": report }), "goal_winback");
  const root = C.el("div");
  await plan.render(root, ctx);
  assert.ok(byClass(root, "plan-products")[0].textContent.includes("Your order data does not name products yet"));
});

// ---- helpers -----------------------------------------------------------------------------------------------

test("Plan helpers: audience, editor summary, message state", () => {
  assert.equal(plan.audienceSentences(null), null);
  assert.deepEqual(plan.audienceSentences({ audience_n: 0 }).headline, "No customers match this today.");
  const one = plan.audienceSentences({ audience_n: 1, holdout_n: 0, baseline_n: 0, agent_n: 1 });
  assert.equal(one.headline, "1 customer matches this today.");
  assert.equal(plan.editorSummary({ editor_passed: 14, editor_failed: 2, not_reviewed: 0 }), "14 passed the editor; 2 failed and will never be sent.");
  assert.equal(plan.editorSummary({ editor_passed: 14, editor_failed: 0, not_reviewed: 0 }), "14 passed the editor; none failed.");
  assert.equal(plan.editorSummary(null), null);
  assert.equal(plan.editorSummary({ editor_passed: 0, editor_failed: 0, not_reviewed: 0 }), null, "no messages yet: no summary");
  assert.equal(plan.messageState({ status: "draft" }, { passed: false, reviewed_by: "critic" }).key, "failed");
  assert.equal(plan.messageState({ status: "rejected" }, { passed: true, reviewed_by: "person" }).label, "Turned off by you");
  assert.equal(plan.messageState({ status: "draft" }, { passed: null, reviewed_by: null }).label, "Not checked by the editor yet");
  assert.equal(plan.messageState({ status: "approved" }, { passed: true }).label, "Ready to send");
});
