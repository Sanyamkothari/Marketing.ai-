// Node checks for zero-touch setup in the marketer app (docs/v2/ZERO_TOUCH.md): the five clicks from "Connect Shopify"
// to a running Shadow plan on Getting started (#/start), and, once it runs, Live's exceptions-only inbox and the
// one-click Autopilot offer. Same tiny DOM shim as tests/v2/promise/shell_onboarding.test.mjs (no jsdom). Run by
// test_flow.py, or directly with node.

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
  appendChild(node) {
    this.append(node);
    return node;
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
  createElementNS: (_ns, t) => new Element(t),
  createTextNode: (t) => new Text(t),
  getElementById(id) {
    return [...this.body.walk()].find((n) => n.id === id) || null;
  },
};

// ---- the code under test ---------------------------------------------------------------------------------
const UI = new URL("../../../ui/v2/", import.meta.url);
const { createApi } = await import(new URL("api.js", UI).href);
const C = await import(new URL("components.js", UI).href);
const ob = await import(new URL("screens/onboarding.js", UI).href);
const live = await import(new URL("screens/live.js", UI).href);
const mock = async (name) => JSON.parse(await readFile(fileURLToPath(new URL(`mock/${name}`, UI)), "utf8"));
const text = (n) => n.textContent.replace(/\s+/g, " ");
const settle = () => new Promise((r) => setTimeout(r, 0));

async function fileFetch(url) {
  const u = String(url);
  if (!u.startsWith("file:")) throw new TypeError("fetch failed");
  try {
    const body = await readFile(fileURLToPath(u), "utf8");
    return { ok: true, status: 200, text: async () => body, headers: { get: () => "application/json" } };
  } catch {
    return { ok: false, status: 404, text: async () => "", headers: { get: () => "text/plain" } };
  }
}
const mockApi = (scenario = null) => createApi({ mock: true, scenario, fetch: fileFetch, mockBase: new URL("mock/", UI).href, now: () => new Date("2026-10-20T08:00:00Z") });

/** The buttons whose words start with `label`. */
const buttons = (root, label) => root.querySelectorAll("button").filter((b) => b.textContent.trim().startsWith(label));

/**
 * Press one control the way a person would: a submit button submits its form, any other button is clicked. Every
 * press is counted, so the test can hold the setup to its five clicks.
 */
function presser(root) {
  const pressed = [];
  const press = async (label) => {
    // The screen renders after mock reads from disk; on a busy machine that takes more than a few ticks.
    const until = Date.now() + 5000;
    while (!buttons(root, label)[0] && Date.now() < until) await new Promise((r) => setTimeout(r, 5));
    const [b] = buttons(root, label);
    assert.ok(b, `a button "${label}" is on screen: ${text(root).slice(0, 400)}`);
    assert.equal(b.disabled, false, `"${label}" can be pressed`);
    pressed.push(label);
    if (b.getAttribute("type") === "submit") {
      let form = b.parentNode;
      while (form && form.tagName !== "FORM") form = form.parentNode;
      await form.dispatch("submit");
    } else await b.click();
    for (let i = 0; i < 5; i += 1) await settle();
  };
  return { press, pressed };
}

const noHoldout = (root) => assert.doesNotMatch(text(root), /holdout/i, "people we never contact, never 'holdout'");

// ---- a shop going through the setup: the real round trips, faked -------------------------------------------------
async function setupShop({ scan = null } = {}) {
  const proposals = scan || (await mock("onboarding_proposals.json"));
  const limits = await mock("onboarding_limits.json");
  const brandPage = await mock("onboarding_brand.json");
  const st = { shopify: false, klaviyo: false, goals: [], mandates: [], brand: "not_read", calls: [], redirects: [] };
  const connection = (kind) => ({
    connection_id: `conn_${kind}`, kind, label: "Corner Coffee", external_id: kind === "shopify" ? "corner-coffee.myshopify.com" : null,
    status: "active", phase: kind === "klaviyo" ? "backfilling" : "healthy",
    words: kind === "klaviyo" ? "Loading history, 40%" : "Up to date, just now",
    detail: "Autonomy reads your past data newest first. It starts deciding once it has 60 days; the rest keeps loading in the background.",
    streams: kind === "klaviyo" ? [{ stream: "consent", label: "Marketing consent", history_progress: 0.4, history_words: "History 40% loaded" }] : [],
  });
  const platform = (kind, label, fields) => ({
    kind, label, tagline: kind === "shopify" ? "Brings in orders, customers and products." : "Sends Autonomy's messages through your own flows.",
    description: `${label}.`, oauth_ready: true, key_allowed: true, scopes: [], sends: kind === "klaviyo", config_fields: fields,
    connections: st[kind] ? [connection(kind)] : [],
  });
  const api = {
    st,
    listGoals: async () => st.goals,
    getDataReport: async () => ({ customers: st.shopify ? 600 : 0, orders: st.shopify ? 1450 : 0, sources: st.shopify ? [{ name: "Shopify", kind: "shop" }] : [] }),
    listConnectors: async () => ({
      platforms: [
        platform("shopify", "Shopify", [{ name: "shop", label: "Shop address", help: "Ends in .myshopify.com", required: true }]),
        platform("klaviyo", "Klaviyo", []),
      ],
    }),
    startConnect: async (kind, config) => (st.calls.push(["connect", kind, config]), { kind, url: `https://${kind}.example/consent` }),
    getConnectorSetup: async () => ({ state: "ready", words: "Sending is set up in Klaviyo.", steps: [] }),
    getProposals: async () => (st.shopify ? proposals : { ...proposals, proposals: [], preselected_id: null, enough_history: false, not_enough: "Not enough history yet." }),
    getLimits: async (id) => (st.calls.push(["limits", id]), limits[id]),
    listMandates: async () => st.mandates,
    acceptProposal: async (body) => {
      st.calls.push(["accept", body]);
      const p = proposals.proposals.find((x) => x.opportunity_id === (body.opportunity_id || proposals.preselected_id));
      const goal = { goal_id: "goal_1", text: p.goal_text, kind: p.kind, status: "active", autonomy: "shadow", budget: 1, currency: "USD" };
      st.goals = [goal];
      const record = { goal_id: "goal_1", unmet: [], single_owner: true, mandate: { mandate_id: "m1", status: "active", valid_until: "2026-11-19T08:00:00Z" } };
      st.mandates = [record];
      return { goal, mandate: record, proposal: p, words: `${p.title} is running: it decides every day and sends nothing. Limits signed by you alone (single-owner path).` };
    },
    getBrand: async (goalId) => (st.calls.push(["brand", goalId]), st.brand === "not_read" ? { status: "not_read", words: "", found: [], samples: [] } : { ...brandPage, status: st.brand }),
    readBrand: async (goalId) => {
      st.calls.push(["read", goalId]);
      st.brand = "waiting";
      return { ...brandPage, status: "waiting" };
    },
    approveBrand: async (fingerprint) => {
      st.calls.push(["approve", fingerprint]);
      st.brand = "approved";
      return { ...brandPage, status: "approved", approved_by: "the owner", words: "Approved by the owner; every message is checked against it." };
    },
  };
  return api;
}

function setupCtx(api, root) {
  const toasts = [];
  const ctx = {
    api, goalId: null, goal: null, navigate() {}, toast: (m, t) => toasts.push([m, t]), refreshGoals: async () => api.st.goals,
    // The platform's consent screen, then its redirect back: the round trip lands on Getting started again.
    redirect: (url) => {
      api.st.redirects.push(url);
      if (url.includes("shopify")) api.st.shopify = true;
      if (url.includes("klaviyo")) api.st.klaviyo = true;
      return ob.render(root, ctx);
    },
  };
  return { ctx, toasts };
}

// ---- the five clicks ---------------------------------------------------------------------------------------------
test("from Connect Shopify to Shadow running in five clicks, keeping every default", async () => {
  const api = await setupShop();
  const root = C.el("div");
  const { ctx, toasts } = setupCtx(api, root);
  const { press, pressed } = presser(root);
  await ob.render(root, ctx);
  assert.match(text(root), /Five clicks to Shadow/);
  assert.match(text(root), /0 of 5 done/);
  assert.equal(root.querySelectorAll("li.zt-step").length, 5);

  // 1: the shop address is typed in the row, so connecting Shopify is one click.
  root.querySelector("input").value = "corner-coffee.myshopify.com";
  await press("Continue to Shopify");
  for (let i = 0; i < 5; i += 1) await settle();
  assert.deepEqual(api.st.calls[0], ["connect", "shopify", { shop: "corner-coffee.myshopify.com" }]);
  assert.match(text(root), /Corner Coffee · Up to date, just now/);

  // 2: Klaviyo connects in one click, and sets up sending on its own; the history loads with a calm line.
  await press("Connect Klaviyo");
  for (let i = 0; i < 5; i += 1) await settle();
  let t = text(root);
  assert.match(t, /Sending is set up in your Klaviyo\./);
  assert.match(t, /Reading your history\. Klaviyo: marketing consent 40%/);

  // 3: the proposals, each sized in money with its range; the best one is preselected.
  const scan = await mock("onboarding_proposals.json");
  const best = scan.proposals.find((p) => p.opportunity_id === scan.preselected_id);
  assert.match(t, /Refill reminders/);
  assert.match(t, /\$60 a month/);
  assert.match(t, /\$29 – \$94/, "the money carries its range");
  for (const p of scan.proposals.filter((x) => x.monthly_profit)) assert.ok(t.includes(p.title), p.title);
  const chosen = root.querySelectorAll("input").find((i) => i.getAttribute("type") === "radio" && i.checked);
  assert.equal(chosen.getAttribute("value"), best.opportunity_id, "the best proposal is preselected");
  noHoldout(root);
  await press("Use this goal");

  // 4: what we found -> what we will use, then one button signs the mandate and starts Shadow.
  t = text(root);
  const lim = (await mock("onboarding_limits.json"))[best.opportunity_id];
  for (const l of lim.limits.filter((x) => x.key !== "discount_frequency")) assert.ok(t.includes(l.found), `found: ${l.key}`);
  assert.match(t, /At most 2 a week/);
  assert.match(t, /Quiet 21:00 to 09:00/);
  assert.match(t, /Safe default/, "a default not learned from the data says so");
  assert.ok(t.includes(lim.limits.find((x) => x.key === "discount_frequency").found), "the reference-only limit is in the Discounts row's description");
  noHoldout(root);
  await press("Sign and start Shadow");
  const accept = api.st.calls.find((c) => c[0] === "accept");
  assert.deepEqual(accept[1], { opportunity_id: best.opportunity_id, offers: false }, "the preselected proposal, offers off");
  assert.ok(toasts.some(([m]) => /decides every day and sends nothing/.test(m)));
  // The voice is read on its own once a tool is connected: no click for it.
  assert.ok(api.st.calls.some((c) => c[0] === "read" && c[1] === "goal_1"));

  // 5: three samples in their own voice, then one approval.
  t = text(root);
  assert.match(t, /Signed by you alone\. Valid until 19 Nov 2026\./);
  assert.equal(root.querySelectorAll("li.zt-sample").length, 3);
  assert.match(t, /Read from 20 of your messages: 6 rules every message must follow\./);
  assert.doesNotMatch(t, /You can unsubscribe at any time/, "the footer every email keeps is in the description, not the card");
  await press("Approve this voice");
  assert.deepEqual(api.st.calls.at(-1), ["approve", (await mock("onboarding_brand.json")).fingerprint]);

  t = text(root);
  assert.match(t, /5 of 5 done/);
  assert.match(t, /Shadow is running\. It decides every day and sends nothing\./);
  assert.ok(root.querySelectorAll("a").some((a) => a.getAttribute("href") === "#/live/goal_1"));
  noHoldout(root);

  // The measure in ZERO_TOUCH.md: 5 clicks or fewer from Connect to Shadow, and these are they.
  assert.deepEqual(pressed, [...ob.SETUP_CLICKS]);
  assert.ok(pressed.length <= 5);
  assert.equal(api.st.goals[0].autonomy, "shadow");
});

test("without enough history, no money is shown and nothing is preselected", async () => {
  const scan = await mock("onboarding_proposals.json");
  const thin = {
    ...scan,
    preselected_id: null,
    enough_history: false,
    not_enough: "Not enough history yet: 27 people matched this group in your past 1 look-back; we need 30.",
    proposals: scan.proposals.map((p) => ({ ...p, monthly_profit: null, enough_history: false, preselected: false, not_enough: "Not enough history yet: 27 people in 1 look-back; we need 30." })),
  };
  const api = await setupShop({ scan: thin });
  api.st.shopify = true;
  api.st.klaviyo = true;
  const root = C.el("div");
  const { ctx } = setupCtx(api, root);
  await ob.render(root, ctx);
  const t = text(root);
  assert.match(t, /Not enough history yet: 27 people matched/);
  assert.doesNotMatch(t, /a month/, "no money without the history to size it");
  assert.equal(buttons(root, "Use this goal").length, 0);
  assert.ok(root.querySelectorAll("input").filter((i) => i.getAttribute("type") === "radio").every((i) => i.disabled));
});

test("a shop with a running goal gets its path; the setup stays only while its own goal waits for the voice", async () => {
  const setup = { connectors: { platforms: [{ kind: "klaviyo", connections: [{ phase: "healthy" }] }] }, scan: null, mandates: [] };
  const running = [{ goal_id: "g1", status: "active", autonomy: "shadow" }];
  assert.equal(ob.wantsSetup(null, [], null), false, "no setup routes: the older path");
  assert.equal(ob.wantsSetup(setup, [], null), true, "nothing runs yet");
  assert.equal(ob.wantsSetup(setup, running, null), false, "a goal runs that the setup did not make");
  const mine = { ...setup, mandates: [{ goal_id: "g1" }] };
  assert.equal(ob.wantsSetup(mine, running, { status: "waiting" }), true, "its voice waits");
  assert.equal(ob.wantsSetup(mine, running, { status: "approved" }), false, "all five done");
  assert.equal(ob.wantsSetup({ ...mine, connectors: { platforms: [] } }, running, { status: "not_read" }), false, "nothing to read the voice from");

  // On the sample shop (a running goal), Getting started is still the path it was.
  const root = C.el("div");
  await ob.render(root, { api: mockApi(), goalId: "goal_winback", goal: null, navigate() {}, toast() {}, refreshGoals: async () => [] });
  assert.match(text(root), /Your path/);
  assert.doesNotMatch(text(root), /Five clicks to Shadow/);
});

test("the sample data walks the same setup: proposals, limits, a Shadow goal, the voice", async () => {
  const api = mockApi("setup");
  const root = C.el("div");
  const ctx = { api, goalId: null, goal: null, navigate() {}, toast() {}, refreshGoals: async () => api.listGoals() };
  const { press, pressed } = presser(root);
  await ob.render(root, ctx);
  assert.match(text(root), /2 of 5 done/, "Shopify and Klaviyo are connected in the setup scenario");
  assert.match(text(root), /Sending is set up in your Klaviyo/);
  await press("Use this goal");
  await press("Sign and start Shadow");
  const goals = await api.listGoals();
  assert.equal(goals.length, 1);
  assert.equal(goals[0].autonomy, "shadow");
  assert.equal(goals[0].status, "active");
  assert.match(goals[0].text, /\$60 extra profit a month \(\$29 to \$94\)/, "the goal's words carry the money and its range");
  await press("Approve this voice");
  for (let t = Date.now() + 5000; !/Shadow is running/.test(text(root)) && Date.now() < t; ) await new Promise((r) => setTimeout(r, 5));
  assert.match(text(root), /Shadow is running/);
  assert.equal(pressed.length, 3, "after the two connects: three clicks");
  // Accepting the same kind again is refused, as the server refuses it.
  await assert.rejects(api.acceptProposal({ opportunity_id: "opp_refill_due" }), /already running/);
});

test("shortUse reads the signed terms, and only those", () => {
  const terms = { max_touches: 2, window_days: 7, max_offer_pct: 0.1, offers_on: false, margin_rate: 0.4, margin_floor_pct: 0.3, quiet_start: 21, quiet_end: 9, budget_ceiling: 1, currency: "USD", valid_days: 30 };
  assert.equal(ob.shortUse("cadence", terms), "At most 2 a week");
  assert.equal(ob.shortUse("cadence", { ...terms, window_days: 14 }), "At most 2 in 14 days");
  assert.equal(ob.shortUse("discount_depth", terms), "No offers; never over 10%");
  assert.equal(ob.shortUse("discount_depth", { ...terms, offers_on: true }), "Offers up to 10%");
  assert.equal(ob.shortUse("margin", terms), "40%, floor 30%");
  assert.equal(ob.shortUse("quiet_hours", terms), "Quiet 21:00 to 09:00");
  assert.equal(ob.shortUse("budget", terms), "$1.00 for 30 days");
  assert.equal(ob.shortUse("discount_frequency", terms), null, "not a term: the full sentence shows");
  assert.equal(ob.shortUse("cadence", {}), null, "no term, no figure");
});

// ---- Live: the exceptions-only inbox and the Autopilot offer ------------------------------------------------------
test("Live lists only what needs a person, each with one button, and offers Autopilot once it is earned", async () => {
  const api = mockApi();
  const root = C.el("main");
  document.body.replaceChildren(root);
  const toasts = [];
  const ctx = { api, goalId: "goal_winback", navigate() {}, toast: (m, k) => toasts.push([m, k]), pauseAllInTopBar: true, refreshGoals: async () => [] };
  await live.render(root, ctx);
  await settle();
  let t = text(root);
  const box = await mock("inbox.json");
  assert.match(t, new RegExp(`Inbox \\(${box.items.length}\\)`));
  for (const item of box.items) {
    assert.ok(t.includes(item.sentence), item.item_id);
    assert.equal(buttons(root, item.button.label).length, 1, `one button: ${item.button.label}`);
  }
  // The learning note is briefing material, not an inbox item.
  assert.equal(root.querySelectorAll("li.lv-inbox-item").length, box.items.length);

  // The offer: what it would make a month with its range, the proof against the people we never contact, one button.
  const ready = (await mock("autopilot.json")).goal_winback;
  assert.match(t, /Turn on Autopilot\?/);
  assert.match(t, /\$4,600/);
  assert.match(t, /\$3,900 to \$5,300/);
  assert.match(t, /\$900 to \$7,700/);
  assert.ok(t.includes(ready.sentence), "the server's own sentence is the line's description");
  assert.match(t, /Earned in 28 days of Approve: every check passed\./);
  for (const cls of ["lv-offer", "lv-inbox-card"]) noHoldout(root.querySelector(`section.${cls}`));

  await buttons(root, "Mark as read")[0].click();
  for (let i = 0; i < 5; i += 1) await settle();
  t = text(root);
  assert.match(t, /Inbox \(1\)/);
  assert.ok(toasts.some(([m]) => /mark as read/i.test(m)));

  await buttons(root, "Turn on Autopilot")[0].click();
  for (let i = 0; i < 5; i += 1) await settle();
  const after = await api.getGoal("goal_winback");
  assert.equal(after.goal.autonomy, "autopilot");
  assert.equal(buttons(root, "Turn on Autopilot").length, 0, "the offer leaves once taken");
  assert.ok(toasts.some(([m]) => /Autopilot is on/.test(m)));
});

test("an API without the inbox route keeps the older alert list", async () => {
  const api = mockApi();
  api.listInbox = undefined;
  api.getAutopilot = undefined;
  const root = C.el("main");
  document.body.replaceChildren(root);
  await live.render(root, { api, goalId: "goal_winback", navigate() {}, toast() {}, pauseAllInTopBar: true });
  await settle();
  const alerts = await mock("alerts.json");
  assert.match(text(root), new RegExp(`Inbox \\(${alerts.length}\\)`));
  assert.doesNotMatch(text(root), /Turn on Autopilot\?/);
});
